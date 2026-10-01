"""Contract check: a path longer than the Windows legacy limit is an ordinary path.

Win32's classic file APIs refuse a full path longer than ``MAX_PATH`` (260)
unless it carries the ``\\\\?\\`` extended-length prefix -- or the executable's
manifest opts in to long paths AND the machine enables ``LongPathsEnabled``.
Rust's standard library adds the prefix itself. The C++ build did not, so a
recording in a deep directory failed there as "MIE file not found" while Rust
decoded it, and enabling the registry switch did not help: the C++ executable
does not opt in.

The manifest cannot express this. A case's paths are chosen by the runner, in
a temp directory that is nowhere near the limit, and a hex fixture cannot make
its own directory deep. So this check builds one, and runs every
implementation against it on three shapes:

* **long, both ends** -- the input in a deep directory, and the output written
  beside it with ``--separate-errors``, so the lazily created errors file goes
  through the same path code as the main one;
* **long only as typed** -- a short output path spelled with a run of ``./``
  segments, which resolves to something short. Resolving it is the operating
  system's job; the decoder must not reject the spelling;
* **count** -- the read side alone, through a different subcommand.

Each result is compared with the same implementation's run at a short path,
byte for byte, so the check proves the long path changed nothing rather than
merely that something was written. ``--no-mux`` keeps the input file's name
out of the CSV, since the two runs read differently named copies.

It is a contract check, not a differential one, so it runs at any
implementation count. It runs on every platform: a POSIX path of a few hundred
characters is unremarkable, which is the point -- the check states what is
true everywhere, and only one platform needed work to make it so.

Run automatically by ``run.py``.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
from pathlib import Path

#: The deep directory's full path must be at least this long. Comfortably past
#: both Windows limits: 260 for a file, 248 for a directory.
MIN_PATH_CHARS = 320

#: One directory level. Sixty characters is well inside every filesystem's
#: per-component limit (255), so only the total length is unusual.
SEGMENT = "d" * 60


def _native(path: Path) -> str:
    """``path`` as this process must spell it to create or read it.

    The runner itself is subject to the limit it is testing: without the
    registry switch, Python on Windows cannot make a deep directory through an
    ordinary path either. The extended-length form sidesteps that. It is used
    only for the runner's own file operations -- the implementations under
    test always receive the ordinary spelling, which is the thing being tested.
    """
    if sys.platform == "win32":
        return "\\\\?\\" + str(path.resolve())
    return str(path)


def _read(path: Path) -> bytes:
    with Path(_native(path)).open("rb") as handle:
        return handle.read()


def _exists(path: Path) -> bool:
    return Path(_native(path)).exists()


def _attempt(
    impl: str,
    prefix: list[str],
    args: list[str],
    what: str,
    root: Path,
    failures: list[str],
) -> subprocess.CompletedProcess[bytes] | None:
    """Run one invocation; on a non-zero exit, record why and return None."""
    result = subprocess.run(
        [*prefix, *args],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        failures.append(
            f"{impl}: {what} exited {result.returncode}\n"
            f"stderr:\n{result.stderr.decode('utf-8', 'replace')}"
        )
        return None
    return result


def _errors_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}_errors{output.suffix}")


def check_long_paths(invocations: dict[str, list[str]], root: Path, temp: Path) -> None:
    """Run each implementation at long paths; require the short-path result.

    Raises:
        AssertionError: The deep directory came out shorter than
            ``MIN_PATH_CHARS``, or any implementation failed a run, wrote a
            different result at a long path than at a short one, or left out
            an output file the short-path run produced.
    """
    from run import read_hex  # local import: run.py imports this module

    recording = read_hex(Path(__file__).resolve().parent / "inputs" / "errors-inline.hex")

    deep = temp.resolve() / "long-paths"
    while len(str(deep / "input.mie")) < MIN_PATH_CHARS:
        deep = deep / SEGMENT
    Path(_native(deep)).mkdir(parents=True, exist_ok=True)
    long_input = deep / "input.mie"
    if len(str(long_input)) < MIN_PATH_CHARS:
        raise AssertionError(f"long-paths fixture is only {len(str(long_input))} characters")
    with Path(_native(long_input)).open("wb") as handle:
        handle.write(recording)

    short_dir = temp / "long-paths-reference"
    short_dir.mkdir(exist_ok=True)
    short_input = short_dir / "input.mie"
    short_input.write_bytes(recording)

    failures: list[str] = []
    for impl, prefix in invocations.items():
        tag = impl.lower().replace("+", "p")

        # Bound now, not looked up later: each iteration's `run` names its own
        # implementation.
        run = functools.partial(_attempt, impl, prefix, root=root, failures=failures)

        # Long at both ends, with the errors file split out.
        short_out = short_dir / f"split-{tag}.csv"
        long_out = deep / f"split-{tag}.csv"
        split = ["--separate-errors", "--no-mux"]
        reference_run = run(
            ["decode", str(short_input), "-o", str(short_out), *split], "short-path decode"
        )
        long_run = run(["decode", str(long_input), "-o", str(long_out), *split], "long-path decode")
        if reference_run and long_run:
            for reference, produced in (
                (short_out, long_out),
                (_errors_path(short_out), _errors_path(long_out)),
            ):
                if not _exists(produced):
                    failures.append(f"{impl}: long-path decode did not write {produced.name}")
                elif _read(produced) != _read(reference):
                    failures.append(f"{impl}: {produced.name} differs from the short-path run")

        # Long only as typed: a run of `./` segments in front of a short name.
        plain_out = short_dir / f"plain-{tag}.csv"
        dotted_name = f"dotted-{tag}.csv"
        dotted = str(short_dir) + os.sep + ("." + os.sep) * 120 + dotted_name
        reference_run = run(
            ["decode", str(short_input), "-o", str(plain_out), "--no-mux"], "short-path decode"
        )
        dotted_run = run(
            ["decode", str(short_input), "-o", dotted, "--no-mux"], "dotted-path decode"
        )
        if reference_run and dotted_run:
            written = short_dir / dotted_name
            if not _exists(written):
                failures.append(f"{impl}: dotted-path decode did not write {dotted_name}")
            elif _read(written) != _read(plain_out):
                failures.append(f"{impl}: dotted-path output differs from the short-path run")

        # The read side alone, through `count`.
        short_count = run(["count", str(short_input)], "short-path count")
        long_count = run(["count", str(long_input)], "long-path count")
        if short_count and long_count and long_count.stdout != short_count.stdout:
            failures.append(
                f"{impl}: count at a long path printed {long_count.stdout!r}, "
                f"at a short path {short_count.stdout!r}"
            )

    if failures:
        raise AssertionError("long-path contract failed:\n" + "\n\n".join(failures))
    print(
        f"PASS long-paths ({', '.join(invocations)}; {len(str(long_input))}-character input path)"
    )
