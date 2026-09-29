#!/usr/bin/env python3
"""Run pytest filtered by requirement marker value.

Pytest's ``-m`` expression language does not natively support matching the
string argument of a parameterised marker, so this wrapper walks the
collected items and selects those carrying a ``requirement`` marker whose
first argument matches the requested id.

Usage:
    python scripts/pytest-by-requirement.py L3-PY-004
    python scripts/pytest-by-requirement.py L2-WRT-           # prefix match
    python scripts/pytest-by-requirement.py L3-PY-004 -- -v --tb=short

Everything after ``--`` is forwarded to pytest. When no ``--`` is given,
``-v`` is added by default.

The wrapper invokes the Python implementation's test suite under
``python/tests/`` via ``uv --directory python run pytest``; Rust requirement tags
are tracked separately via doc-comments and are not in scope for this tool.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_ROOT = ROOT / "python"
TESTS_REL = "tests"


def main() -> int:
    """CLI entry point: run pytest filtered by requirement marker id."""
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    req_filter = sys.argv[1]

    if "--" in sys.argv:
        idx = sys.argv.index("--")
        extra_args = sys.argv[idx + 1 :]
    else:
        extra_args = ["-v"]

    collect = subprocess.run(
        ["uv", "--directory", str(PY_ROOT), "run", "pytest", "--collect-only", "-q", TESTS_REL],
        cwd=PY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if collect.returncode not in (0, 5):
        print(collect.stdout)
        print(collect.stderr, file=sys.stderr)
        return collect.returncode

    test_ids = [
        line.strip()
        for line in collect.stdout.splitlines()
        if "::" in line and not line.startswith("=")
    ]

    selected: list[str] = []
    for test_id in test_ids:
        file_part = test_id.split("::")[0]
        func_part = test_id.rsplit("::", 1)[-1].split("[")[0]
        try:
            source = (PY_ROOT / file_part).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = source.splitlines()
        for idx, line in enumerate(lines):
            if f"def {func_part}(" in line:
                window = "\n".join(lines[max(0, idx - 10) : idx])
                if f'requirement("{req_filter}' in window:
                    selected.append(test_id)
                break

    if not selected:
        print(f"No tests found matching requirement filter {req_filter!r}")
        return 1

    print(f"Selected {len(selected)} tests matching {req_filter!r}:")
    for t in selected:
        print(f"  {t}")
    print()

    result = subprocess.run(
        ["uv", "--directory", str(PY_ROOT), "run", "pytest", *selected, *extra_args],
        cwd=PY_ROOT,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
