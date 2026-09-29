"""Throughput benchmark for the aero1553 Python package.

Measures records per second for the three ways Python code consumes a
recording:

* ``iterate`` -- ``MieFileReader`` iteration, touching a few fields per record.
  This is the path that creates one Python object per record, so it is the one
  the PyO3 binding's object-creation cost shows up in.
* ``decode``  -- a whole-file decode to CSV through the CLI entry point,
  in-process.
* ``count``   -- the ``count`` subcommand, in-process.

It also times the Rust release binary on the same file when one is built
(``rust/target/release/aero1553``), as the native ceiling the binding's
whole-file paths should approach.

The recording is synthetic but realistic: receive and transmit messages
across 31 RTs and 30 subaddresses, 1..32 data words, monotonic IRIG time.
Every run is checked -- record count and CSV row count -- so a fast wrong
answer cannot pass as a result.

Usage (from the repo root)::

    python python/benchmarks/bench_decode.py
    python python/benchmarks/bench_decode.py --records 200000 --repeat 5
    python python/benchmarks/bench_decode.py --json results.json

Run it before and after a change to the package on the same machine; the
absolute numbers mean little across machines.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import aero1553
from aero1553 import MieFileReader
from aero1553.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[2]

# Message type codes (Type Word bits 0..7).
BC_TO_RT = 0x02
RT_TO_BC = 0x04


def _irig(total_us: int) -> bytes:
    """The 3-word IRIG timestamp for `total_us` microseconds into day 192."""
    seconds, us = divmod(total_us, 1_000_000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    day = 192 + hours // 24
    hour = hours % 24
    upper = (day << 5) | hour
    middle = (minute << 10) | (sec << 4) | ((us >> 16) & 0xF)
    lower = us & 0xFFFF
    return b"".join(w.to_bytes(2, "little") for w in (upper, middle, lower))


def _record(index: int, total_us: int) -> bytes:
    """One clean record; the index picks direction, RT, SA and word count."""
    transmit = index % 3 == 2
    rt = 1 + index % 31
    sa = 1 + (index // 31) % 30
    n = 1 + index % 32
    command = (rt << 11) | (int(transmit) << 10) | (sa << 5) | (n % 32)
    status = rt << 11
    data = b"".join(((index + k) & 0xFFFF).to_bytes(2, "little") for k in range(n))
    word_count = 1 + 3 + 1 + n + 1
    msg_type = RT_TO_BC if transmit else BC_TO_RT
    type_word = msg_type | (word_count << 8)
    head = type_word.to_bytes(2, "little") + _irig(total_us) + command.to_bytes(2, "little")
    status_bytes = status.to_bytes(2, "little")
    return head + (status_bytes + data if transmit else data + status_bytes)


def build_recording(path: Path, records: int, step_us: int = 50) -> int:
    """Write `records` records plus the null terminator; return the byte size."""
    with path.open("wb") as fh:
        for i in range(records):
            fh.write(_record(i, i * step_us))
        fh.write(b"\x00\x00")
    return path.stat().st_size


def _best_of(repeat: int, fn: Callable[[], int | None]) -> tuple[float, int | None]:
    """Run `fn` `repeat` times; return (best seconds, its result)."""
    best = float("inf")
    result: int | None = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - start)
    return best, result


def bench_iterate(path: Path) -> int:
    count = 0
    for msg in MieFileReader(path):
        _ = msg.timestamp
        _ = msg.type_word
        cmd = msg.command_word
        if cmd is not None:
            _ = cmd.rt
        _ = len(msg.data_words)
        count += 1
    return count


def bench_decode(path: Path, out: Path) -> int:
    code = cli_main(["decode", str(path), "-o", str(out)])
    if code != 0:
        raise SystemExit(f"decode exited {code}")
    with out.open("rb") as fh:
        return sum(1 for _ in fh) - 1  # minus the header row


def bench_count(path: Path) -> int:
    """Run ``count``, capturing its stdout at the FILE DESCRIPTOR.

    The CLI writes to fd 1 directly (as the binary does), so swapping
    ``sys.stdout`` would capture nothing; fd 2 is silenced the same way so the
    status line does not interleave with the report.
    """
    with tempfile.TemporaryFile() as out_sink, tempfile.TemporaryFile() as err_sink:
        sys.stdout.flush()
        sys.stderr.flush()
        saved = (os.dup(1), os.dup(2))
        os.dup2(out_sink.fileno(), 1)
        os.dup2(err_sink.fileno(), 2)
        try:
            code = cli_main(["count", str(path)])
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
        out_sink.seek(0)
        text = out_sink.read().decode("ascii", errors="replace")
    if code != 0:
        raise SystemExit(f"count exited {code}")
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else -1


def bench_rust(path: Path, out: Path) -> int | None:
    exe = REPO_ROOT / "rust" / "target" / "release" / "aero1553"
    if sys.platform == "win32":
        exe = exe.with_suffix(".exe")
    if not exe.exists():
        return None
    subprocess.run([str(exe), "decode", str(path), "-o", str(out)], check=True)
    with out.open("rb") as fh:
        return sum(1 for _ in fh) - 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--records", type=int, default=500_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", type=Path, help="Also write the results here.")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="aero1553-bench-") as tmp:
        rec = Path(tmp) / "bench.mie"
        out = Path(tmp) / "bench.csv"
        size = build_recording(rec, args.records)

        cases: list[tuple[str, Callable[[], int | None]]] = [
            ("iterate", lambda: bench_iterate(rec)),
            ("decode", lambda: bench_decode(rec, out)),
            ("count", lambda: bench_count(rec)),
            ("rust-cli-decode", lambda: bench_rust(rec, out)),
        ]
        results: dict[str, dict[str, float]] = {}
        print(
            f"aero1553 {aero1553.__version__} | Python {platform.python_version()} "
            f"| {platform.system()} {platform.machine()}"
        )
        print(f"{args.records} records, {size / 1e6:.1f} MB, best of {args.repeat}\n")
        print(f"{'case':<16} {'seconds':>9} {'records/s':>12}")
        for name, fn in cases:
            seconds, got = _best_of(args.repeat, fn)
            if got is None:
                print(f"{name:<16} {'skipped (no release binary)':>22}")
                continue
            if got != args.records:
                raise SystemExit(f"{name}: expected {args.records} records, got {got}")
            rate = args.records / seconds
            results[name] = {"seconds": seconds, "records_per_second": rate}
            print(f"{name:<16} {seconds:>9.3f} {rate:>12,.0f}")

    if args.json:
        payload = {
            "version": aero1553.__version__,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "records": args.records,
            "bytes": size,
            "repeat": args.repeat,
            "results": results,
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
