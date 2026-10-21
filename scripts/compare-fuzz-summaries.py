#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare the FUZZ-SUMMARY lines emitted by every fuzz burn-in job.

WHY THIS EXISTS. The L1-ROB-001 byte harnesses share a generator -- same
xorshift64, same seed, same size bands, same draw order -- so iteration N is the
same bytes in Rust and Python (and was in the C++ implementation removed in
5.0.0). What they did NOT share was an assertion: each one proved, in its own
process and its own test framework, that it did not crash. Two implementations
can each survive the same input while decoding it differently, and nothing
noticed.

Each harness now ends by writing one line of counters that are defined to mean
the same thing everywhere. This script is the part that actually compares them.
It is not a replacement for a real differential driver over the decoded CSV
(docs/FUZZING.md 6.1); it is the cheap precursor, and it is what turns "each
implementation survived" into "the implementations agreed".

ALL-PAIRS, NOT MAJORITY -- the same rule, and the same reasoning, as
tests/conformance/differential.py: two implementations sharing a bug would
outvote the correct one, and nominating a reference would make its quirks
normative. The report names the split without naming a winner.

WHAT IS COMPARED. Every field of the line except `impl`. That means the counters
have to be path-independent by construction: the harnesses name their temp
files differently, so anything derived from a path measures the harness rather
than the decoder. (The dump harness counts output LINES for exactly this reason
-- it counted bytes first, and the two implementations disagreed by a constant
offset that turned out to be the length of the file path in the dump header.)

A HARNESS PRESENT IN SOME IMPLEMENTATIONS AND NOT OTHERS IS REPORTED, NOT
FAILED. A harness one implementation lacks is a parity gap, tracked in
docs/FUZZING.md section 5, and failing the nightly on it would just make the job
permanently red and therefore ignored. A DIVERGENCE fails; a GAP is announced.

Usage:  compare-fuzz-summaries.py <artifacts-dir>

The directory is expected to hold one subdirectory per job, named
`fuzz-summary-<impl>-<os>`; the subdirectory name is used as the origin label so
the report can say *which runner* disagreed, not just which implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "FUZZ-SUMMARY "


def parse_line(line: str) -> dict[str, str]:
    """A summary line as a field mapping. Fields are `key=value`, space separated."""
    fields: dict[str, str] = {}
    for token in line[len(MARKER) :].split():
        key, _, value = token.partition("=")
        if key:
            fields[key] = value
    return fields


def collect(root: Path) -> list[tuple[str, dict[str, str]]]:
    """Every summary line under `root`, paired with the origin it came from.

    Sorted by origin so the report reads in a stable order across runs rather
    than in whatever order the filesystem walked.
    """
    found: list[tuple[str, dict[str, str]]] = []
    for path in sorted(root.rglob("*.txt")):
        origin = path.parent.name if path.parent != root else path.stem
        for raw in path.read_text(encoding="ascii", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith(MARKER):
                found.append((origin, parse_line(line)))
    return found


def comparable(fields: dict[str, str]) -> str:
    """The part of a line that every implementation must agree on.

    `impl` is dropped because it is the thing that differs on purpose. Sorted so
    two lines that carry the same fields in a different order still compare
    equal -- the harnesses emit a fixed order today, and nothing should depend
    on their continuing to.
    """
    return " ".join(f"{k}={v}" for k, v in sorted(fields.items()) if k != "impl")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: compare-fuzz-summaries.py <artifacts-dir>", file=sys.stderr)
        return 2

    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"compare-fuzz-summaries: no such directory: {root}", file=sys.stderr)
        return 2

    entries = collect(root)
    if not entries:
        # An empty comparison that reports success is worse than no comparison:
        # a job whose harness silently stopped writing its summary would read as
        # a clean cross-implementation pass forever.
        print(
            f"compare-fuzz-summaries: no FUZZ-SUMMARY lines found under {root}",
            file=sys.stderr,
        )
        return 1

    harnesses: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for origin, fields in entries:
        harnesses.setdefault(fields.get("harness", "<unnamed>"), []).append((origin, fields))

    print(f"Found {len(entries)} summary line(s) across {len(harnesses)} harness(es).\n")

    failures = 0
    for harness in sorted(harnesses):
        rows = harnesses[harness]
        impls = sorted({fields.get("impl", "?") for _, fields in rows})
        groups: dict[str, list[str]] = {}
        for origin, fields in rows:
            groups.setdefault(comparable(fields), []).append(
                f"{fields.get('impl', '?')} ({origin})"
            )

        if len(groups) == 1:
            shared = next(iter(groups))
            reporters = next(iter(groups.values()))
            print(f"PASS  harness={harness}: {len(rows)} run(s) agree")
            print(f"        {shared}")
            print(f"        reported by: {', '.join(sorted(reporters))}")
        else:
            failures += 1
            print(f"FAIL  harness={harness}: implementations disagree on identical inputs")
            for value, who in groups.items():
                print(f"        {', '.join(sorted(who))}")
                print(f"          {value}")

        missing = sorted({"rust", "python"} - set(impls))
        if missing:
            # Announced, never failed -- see the module docstring.
            print(
                f"        NOTE: no {harness} harness in: {', '.join(missing)} "
                f"(parity gap; see docs/FUZZING.md section 5)"
            )
        print()

    if failures:
        print(
            f"compare-fuzz-summaries: {failures} harness(es) diverged. The generator is "
            "shared and deterministic, so the same iteration index reproduces the same "
            "bytes in every implementation -- re-run the disagreeing pair at the same "
            "iteration count to isolate it."
        )
        return 1

    print("compare-fuzz-summaries: every harness agreed across every implementation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
