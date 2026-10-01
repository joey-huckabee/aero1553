#!/usr/bin/env python3
"""Work out which rule a vendor CSV's DELTA column actually follows.

Aero1553 computes DELTA as "seconds since the previous record with the same
``(RT, subaddress, direction)`` key" (L1-DLT-001). If a vendor CSV disagrees, the
useful question is not *whether* it differs but *what rule it follows instead* —
this script answers that by recomputing the column under a range of candidate
rules and reporting which reproduces the vendor's own values.

It reads the vendor CSV **only**; no MIE file and no decoder run is needed, so it
works on a machine that has the vendor output and nothing else.

Usage::

    python scripts/diagnose-vendor-delta.py vendor.csv
    python scripts/diagnose-vendor-delta.py vendor.csv --delta-column DELTA

Output is a ranked table of candidate rules by how many rows each reproduces
exactly. A rule at 100% is the vendor's definition.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

# ── timestamp parsing ───────────────────────────────────────────────────────


def parse_timestamp_us(value: str) -> int | None:
    """``DAY:HH:MM:SS.uuuuuu`` -> absolute microseconds, or None if unparseable.

    Mirrors ``IrigTimestamp::to_total_microseconds``. A ``0x...`` Standard
    counter has no calibrated basis and returns None.
    """
    value = value.strip()
    if not value or value.lower().startswith("0x"):
        return None
    try:
        head, _, frac = value.partition(".")
        parts = head.split(":")
        if len(parts) != 4:
            return None
        day, hour, minute, second = (int(p) for p in parts)
        micro = int((frac or "0").ljust(6, "0")[:6])
    except ValueError:
        return None
    return ((day * 86400 + hour * 3600 + minute * 60 + second) * 1_000_000) + micro


def split_msg(msg: str) -> tuple[str, str]:
    """``11R`` -> ``("11", "R")``. Returns ``("", "")`` for an empty MSG."""
    msg = msg.strip()
    if not msg:
        return ("", "")
    return (msg[:-1], msg[-1]) if msg[-1] in "RT" else (msg, "")


# ── candidate key rules ─────────────────────────────────────────────────────

KeyFn = Callable[[dict[str, str]], object]

#: Each entry is (name, key function). A key of ``None`` means "this row does not
#: participate" (no key), matching how SPURIOUS_DATA behaves in Aero1553.
CANDIDATE_KEYS: list[tuple[str, KeyFn]] = [
    ("RT + MSG  (Aero1553's rule)", lambda r: (r["RT"], r["MSG"]) if r["RT"] else None),
    (
        "RT + subaddress, ignoring direction",
        lambda r: (r["RT"], split_msg(r["MSG"])[0]) if r["RT"] else None,
    ),
    ("RT only", lambda r: r["RT"] or None),
    ("RT + MSG + BUS", lambda r: (r["RT"], r["MSG"], r.get("BUS", "")) if r["RT"] else None),
    ("MSG only", lambda r: r["MSG"] or None),
    ("BUS only", lambda r: r.get("BUS") or None),
    ("previous row, whatever it was (inter-message gap)", lambda _r: "*"),
]

#: Unit scalings to try: name -> divisor applied to the microsecond difference.
CANDIDATE_UNITS: list[tuple[str, float]] = [
    ("seconds", 1_000_000.0),
    ("milliseconds", 1_000.0),
    ("microseconds", 1.0),
]

#: What the vendor might emit for a key's first appearance.
CANDIDATE_FIRST: list[tuple[str, str | None]] = [
    ("0.000000", "zero"),
    ("empty cell", "empty"),
]


def format_delta(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def evaluate(
    rows: list[dict[str, str]],
    delta_col: str,
    key_fn: KeyFn,
    divisor: float,
    first: str,
    decimals: int,
) -> tuple[int, int]:
    """Return ``(matched, comparable)`` row counts for one candidate rule."""
    prev: dict[object, int] = {}
    matched = 0
    comparable = 0
    for row in rows:
        actual = (row.get(delta_col) or "").strip()
        us = parse_timestamp_us(row.get("TIME_STAMP", ""))
        key = key_fn(row)

        if us is None or key is None:
            expected = ""
        elif key not in prev:
            expected = "" if first == "empty" else format_delta(0.0, decimals)
        elif us >= prev[key]:
            expected = format_delta((us - prev[key]) / divisor, decimals)
        else:
            expected = ""

        if us is not None and key is not None:
            prev[key] = us

        comparable += 1
        if expected == actual:
            matched += 1
    return matched, comparable


def infer_decimals(rows: list[dict[str, str]], delta_col: str) -> int:
    """Decimal places used by the vendor's own column, so formatting matches."""
    for row in rows:
        value = (row.get(delta_col) or "").strip()
        if "." in value:
            return len(value.split(".", 1)[1])
    return 6


def count_repeat_rows(rows: list[dict[str, str]]) -> int:
    """Rows whose ``(RT, MSG)`` key has already been seen.

    These are the only rows that carry information about the rule: a key's first
    appearance produces the same value under every candidate.
    """
    seen: set[tuple[str, str]] = set()
    repeats = 0
    for row in rows:
        if not row.get("RT"):
            continue
        key = (row["RT"], row.get("MSG", ""))
        if key in seen:
            repeats += 1
        seen.add(key)
    return repeats


def iter_candidates() -> Iterator[tuple[str, KeyFn, float, str]]:
    for key_name, key_fn in CANDIDATE_KEYS:
        for unit_name, divisor in CANDIDATE_UNITS:
            for first_name, first in CANDIDATE_FIRST:
                label = f"{key_name}  [{unit_name}, first={first_name}]"
                yield label, key_fn, divisor, first


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv_path", type=Path, help="vendor CSV to analyse")
    ap.add_argument(
        "--delta-column",
        default="DELTA",
        help="name of the delta column in the vendor CSV (default: DELTA)",
    )
    ap.add_argument("--top", type=int, default=10, help="how many candidates to show (default: 10)")
    args = ap.parse_args(argv)

    if not args.csv_path.is_file():
        print(f"error: not a readable file: {args.csv_path}", file=sys.stderr)
        return 2

    with args.csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("error: no data rows", file=sys.stderr)
        return 2

    if args.delta_column not in rows[0]:
        print(
            f"error: column {args.delta_column!r} not found. Columns are:\n  "
            + ", ".join(rows[0].keys()),
            file=sys.stderr,
        )
        return 2
    if "TIME_STAMP" not in rows[0]:
        print("error: no TIME_STAMP column; cannot recompute anything", file=sys.stderr)
        return 2

    decimals = infer_decimals(rows, args.delta_column)
    populated = sum(1 for r in rows if (r.get(args.delta_column) or "").strip())
    print(
        f"{args.csv_path.name}: {len(rows)} rows, {populated} with a non-empty "
        f"{args.delta_column}, {decimals} decimal places\n"
    )

    # How much evidence does this file actually carry? A rule can only be told
    # apart from another by rows that are NOT a key's first appearance — if every
    # row is a first occurrence, every rule emits 0.000000 everywhere and they
    # all tie. Reporting a winner from such a file would be pure noise.
    repeats = count_repeat_rows(rows)
    nonzero = sum(
        1
        for r in rows
        if (r.get(args.delta_column) or "").strip() not in ("", format_delta(0.0, decimals))
    )

    results = []
    for label, key_fn, divisor, first in iter_candidates():
        matched, comparable = evaluate(rows, args.delta_column, key_fn, divisor, first, decimals)
        results.append((matched / comparable if comparable else 0.0, matched, comparable, label))
    results.sort(key=lambda r: (-r[0], r[3]))

    print(f"{'match':>8}  {'rows':>12}  rule")
    print(f"{'-' * 8}  {'-' * 12}  {'-' * 60}")
    for pct, matched, comparable, label in results[: args.top]:
        print(f"{pct * 100:7.1f}%  {matched:6d}/{comparable:<5d}  {label}")

    best_score = results[0][0]
    tied = [label for score, _m, _c, label in results if score == best_score]
    print()

    if repeats == 0 or nonzero == 0:
        print("INCONCLUSIVE — this file carries no evidence.")
        print(f"  Repeat rows (a key seen more than once): {repeats}")
        print(f"  Non-zero {args.delta_column} values:            {nonzero}")
        print("Every candidate emits the same output here, so the ranking above is")
        print("meaningless. Re-run on a longer capture where RT/MSG keys recur.")
        return 1

    if len(tied) > 1 and best_score == 1.0:
        print(f"AMBIGUOUS — {len(tied)} rules reproduce the column exactly:")
        for label in tied[: args.top]:
            print(f"  - {label}")
        print("This file cannot tell them apart. A longer capture, or one with more")
        print("RT/MSG variety, will separate them.")
        return 0

    if best_score == 1.0:
        print(f"EXACT MATCH: {tied[0]}")
        if tied[0].startswith("RT + MSG  (Aero1553"):
            print("That is already Aero1553's rule — so the DELTA definition agrees and")
            print("the difference is elsewhere: merge scope (--delta-scope), row order,")
            print("or the timestamps themselves.")
        else:
            print("This differs from Aero1553's rule (RT + MSG, seconds, first=0.000000).")
    else:
        print(f"No candidate reproduced the column exactly. Best: {best_score * 100:.1f}%")
        print("The vendor may key on a field not present in the CSV, reset per file or")
        print("section, or use a different definition entirely. The per-rule match rates")
        print("above still narrow it down — a rule at 95% is likely right with an")
        print("edge case (first row, a reset, or an error record) handled differently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
