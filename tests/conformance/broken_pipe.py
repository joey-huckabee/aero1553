"""Contract check: a consumer that closes the pipe early is a clean exit.

L2-WRT-018 says a broken pipe on stdout SHALL exit ``0`` -- ``aero1553 decode
x.mie | head -1`` is how a shell pipeline is meant to stop a producer, and a
non-zero status there fails every script running under ``set -o pipefail``.

The manifest cannot express this: every case there drains the output. So this
check spawns each implementation writing to a pipe, reads one line, closes the
read end while the producer still has most of its CSV to write, and requires
exit ``0``.

It is not a differential check. Each implementation is held to the
specification on its own, so it runs even when only one is under test -- which
is the configuration (``--only cpp``) in which the defect it was written for
lived. Rust's runtime and CPython both ignore SIGPIPE before ``main``; the C++
build did not, so on POSIX it was killed by the signal (exit 141) before the
failed write could return the EPIPE that the writer already classified as a
clean stop.

The check guards against passing vacuously. A producer that finishes writing
before the read end is closed never meets a broken pipe at all, and would pass
having proved nothing. So the recording is sized to decode to far more than any
default pipe buffer, the size is measured from a drained decode rather than
assumed, and the drained decode must itself exit ``0`` so a failure in the
piped run is attributable to the pipe.

Run automatically by ``run.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Records in the generated recording. Each decodes to a CSV row of roughly two
#: hundred bytes, so the whole CSV is about 1.6 MB.
RECORDS = 8000

#: Spacing between consecutive timestamps, in microseconds. Strictly increasing
#: time keeps the canonical-order stage from buffering an equal-timestamp run.
STEP_US = 10

#: The drained CSV must exceed this for the check to mean anything. Linux and
#: macOS default to a 64 KiB pipe buffer and Windows' is smaller; sixteen times
#: that leaves the producer blocked with most of its output unwritten when the
#: read end closes.
MIN_CSV_BYTES = 16 * 64 * 1024


def _recording(template: bytes) -> bytes:
    """``RECORDS`` copies of ``template`` on a strictly increasing IRIG clock.

    The template is one IRIG record. The microseconds field moves: its high four
    bits are the low nibble of the middle timestamp word, its low sixteen the
    lower word. The template's own time plus ``RECORDS * STEP_US`` stays inside
    the same second, so no carry into the seconds field is needed.

    The first data word (after the Type Word, three timestamp words and the
    Command Word) carries the record's index. Without it every record would be
    byte-identical outside the timestamp, which all three decoders rightly
    reject as single-byte padding rather than a recording.
    """
    middle = int.from_bytes(template[4:6], "little")
    lower = int.from_bytes(template[6:8], "little")
    base_us = ((middle & 0xF) << 16) | lower
    if base_us + RECORDS * STEP_US >= 1_000_000:
        raise AssertionError("broken-pipe template's timestamp is too close to a second boundary")

    out = bytearray()
    for i in range(RECORDS):
        us = base_us + i * STEP_US
        new_middle = (middle & ~0xF) | (us >> 16)
        out += template[:4]
        out += new_middle.to_bytes(2, "little")
        out += (us & 0xFFFF).to_bytes(2, "little")
        out += template[8:10]
        out += (i & 0xFFFF).to_bytes(2, "little")
        out += template[12:]
    # The DDC end-of-records terminator, as a real recording carries.
    out += b"\x00\x00"
    return bytes(out)


def check_broken_pipe(invocations: dict[str, list[str]], root: Path, temp: Path) -> None:
    """Close each implementation's stdout early; require exit 0 from every one."""
    from run import read_hex  # local import: run.py imports this module

    template = read_hex(Path(__file__).resolve().parent / "inputs" / "count-one.hex")
    recording = temp / "broken-pipe.mie"
    recording.write_bytes(_recording(template))

    failures: list[str] = []
    for impl, prefix in invocations.items():
        command = [*prefix, "decode", str(recording), "--no-mux"]

        # Drained first: proves the recording decodes cleanly, and measures the
        # CSV rather than trusting the estimate in RECORDS.
        drained = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if drained.returncode != 0:
            failures.append(
                f"{impl}: drained decode exited {drained.returncode}\n"
                f"stderr:\n{drained.stderr.decode('utf-8', 'replace')}"
            )
            continue
        if len(drained.stdout) < MIN_CSV_BYTES:
            failures.append(
                f"{impl}: drained CSV is {len(drained.stdout)} bytes, under the "
                f"{MIN_CSV_BYTES} needed to outrun a pipe buffer -- the check would be vacuous"
            )
            continue

        # stderr goes to a file, not a pipe: with stdout already closed,
        # communicate() would try to read it on Windows, and an undrained stderr
        # pipe could block the producer for a reason that is not the one under
        # test.
        stderr_path = temp / f"broken-pipe-{impl}.stderr"
        with stderr_path.open("wb") as stderr_file:
            proc = subprocess.Popen(
                command,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            assert proc.stdout is not None
            first = proc.stdout.readline()
            proc.stdout.close()
            try:
                returncode = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                failures.append(f"{impl}: still running 120s after its stdout was closed")
                continue
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")

        if not first.startswith(b"TIME_STAMP"):
            failures.append(f"{impl}: first stdout line was not the CSV header: {first[:80]!r}")
        elif returncode != 0:
            failures.append(
                f"{impl}: exited {returncode} after its consumer closed the pipe "
                f"(L2-WRT-018 requires 0)\nstderr:\n{stderr}"
            )

    if failures:
        raise AssertionError("broken-pipe contract failed:\n" + "\n\n".join(failures))
    print(f"PASS broken-pipe ({', '.join(invocations)}; consumer closed after one line)")
