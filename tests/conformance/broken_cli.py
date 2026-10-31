"""A deliberately broken aero1553, for proving the checks can fail.

    python broken_cli.py MODE REAL_CLI [aero1553 arguments...]

Runs the real CLI with the arguments, then breaks exactly ONE rule according
to MODE -- the rule one check exists to enforce. ``selftest.py`` runs each
check against each mode and requires it to fail: a check that passes against
a CLI that breaks its rule is not checking anything.

MODE ``none`` passes the real CLI through unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _patch(out: Path | None, stdout: bytes, change: object) -> bytes:
    """Apply ``change`` (bytes -> bytes) to the CSV: the -o file if the run
    wrote one, else stdout. Returns the stdout to emit."""
    assert callable(change)
    if out is not None and out.exists():
        out.write_bytes(change(out.read_bytes()))
        return stdout
    return change(stdout) if stdout else stdout


def main() -> int:
    mode, real, *args = sys.argv[1:]
    result = subprocess.run([real, *args], stdin=sys.stdin.buffer, capture_output=True, check=False)
    out = Path(args[args.index("-o") + 1]) if "-o" in args else None
    stdout, stderr, code = result.stdout, result.stderr, result.returncode

    def flag(name: str) -> str | None:
        return args[args.index(name) + 1] if name in args else None

    if mode == "threads" and flag("--threads") == "3":
        # Output that depends on the thread count (L2-WRT-027).
        stdout = _patch(out, stdout, lambda d: d.replace(b"\n", b"\n\n", 1))
    elif mode == "split" and "--separate-errors" in args and out is not None:
        # An errored row lost from the errors file.
        errors = out.with_name(out.stem + "_errors" + out.suffix)
        if errors.exists():
            lines = errors.read_bytes().splitlines(keepends=True)
            if len(lines) > 1:
                errors.write_bytes(b"".join(lines[:-1]))
    elif mode == "filter" and any(a.startswith(("--include-", "--exclude-")) for a in args):
        # A filter that invents a row.
        def add(d: bytes) -> bytes:
            lines = d.splitlines(keepends=True)
            return d + lines[-1].replace(b",", b",9", 1) if len(lines) > 1 else d

        stdout = _patch(out, stdout, add)
    elif mode == "stdin" and "-" in args:
        # Standard input decoding differently from the file (L2-CLI-023).
        stdout = _patch(out, stdout, lambda d: d.replace(b",A,", b",B,", 1))
    elif mode == "destination" and out is None and "--separate-errors" not in args:
        # Stdout output differing from file output.
        stdout = stdout.replace(b"\n", b"\r\n", 1)
    elif mode == "impl":
        # A second implementation that disagrees with the first.
        stdout = _patch(out, stdout, lambda d: d.replace(b"\n", b"\r\n", 1))
    elif mode == "panic" and flag("--max-sort-group") == "2":
        # A crash: Rust's panic exit code, undocumented (L2-CLI-011).
        code = 101
    elif mode == "refuse-all":
        # Every run refused early: every comparison then passes, vacuously.
        stdout, stderr, code = b"", b"Error: refused\n", 2
        if out is not None:
            out.unlink(missing_ok=True)
    elif mode != "none" and mode not in {
        "threads",
        "split",
        "filter",
        "stdin",
        "destination",
        "panic",
    }:
        sys.stderr.write(f"broken_cli.py: unknown mode {mode!r}\n")
        return 64

    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
