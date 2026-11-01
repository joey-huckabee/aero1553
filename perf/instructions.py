"""Instructions per thread for one decode, counted by Valgrind's callgrind.

Run from the repository root (Linux, with ``valgrind`` installed)::

    python3 perf/instructions.py                       # decode case, report
    python3 perf/instructions.py --json before.json    # keep the numbers
    python3 perf/instructions.py --compare before.json # what a change did

Timing on a shared machine moves by 5% or more from run to run, so it cannot
show a change worth 1-2%; an instruction count, for one binary on one input,
barely moves at all. That makes this the tool for deciding whether a small
change to the decoding thread pays, one change at a time. It does not replace
timing: instructions are not time (a stall costs time and no instructions),
so a series of changes is still judged by ``perf/baseline.py``.

It builds the release profile with symbols kept (into ``rust/target/instr``,
so the shipping build is untouched), decodes a fixed input -- the first 2 MB
of the golden recording ``a-large``, about 44,000 records -- with the default
thread count and ``--no-mux``, and counts each thread separately. Thread 1 is
the calling thread (read, validate, decode, filter, order, write); the others
render CSV rows (L2-WRT-027). It reports instructions per record for each,
and the calling thread's costliest functions.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST = ROOT / "rust"
TARGET = RUST / "target" / "instr"
INPUT_BYTES = 2_000_000

_spec = importlib.util.spec_from_file_location("golden", ROOT / "tests" / "golden" / "golden.py")
assert _spec is not None and _spec.loader is not None
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)


def build() -> Path:
    """The release CLI with symbols and line tables, in its own target dir."""
    env = {
        **os.environ,
        "CARGO_PROFILE_RELEASE_STRIP": "none",
        "CARGO_PROFILE_RELEASE_DEBUG": "line-tables-only",
        "CARGO_TARGET_DIR": str(TARGET),
    }
    subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=RUST, env=env, check=True)
    return TARGET / "release" / "aero1553"


def make_input(work: Path) -> Path:
    full = golden.write("a-large", work)
    data = full.read_bytes()[:INPUT_BYTES]
    path = work / "input.mie"
    path.write_bytes(data)
    full.unlink()
    return path


_LINE = re.compile(r"^\s*([\d,]+) \(\s*[\d.]+%\)\s+(.*)$")


def annotate(out: Path) -> tuple[int, list[tuple[int, str]]]:
    """(total instructions, [(instructions, function)]) for one thread's file,
    exclusive costs, functions merged across inlining sites."""
    text = subprocess.run(
        # --auto=no: no per-line source annotation after the table, whose
        # call-tree lines would otherwise parse as functions.
        ["callgrind_annotate", "--inclusive=no", "--threshold=100", "--auto=no", str(out)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    total = 0
    by_function: dict[str, int] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        count = int(m.group(1).replace(",", ""))
        where = m.group(2)
        if "PROGRAM TOTALS" in where:
            total = count
            continue
        # "file:function [object]" -> the function, with recursion marks and
        # the object dropped, so one function's inlined pieces add up.
        function = re.sub(r"\s*\[[^]]*\]\s*$", "", where).split(":", 1)[-1]
        function = re.sub(r"'\d+$", "", function)
        by_function[function] = by_function.get(function, 0) + count
    ranked = sorted(((n, f) for f, n in by_function.items()), reverse=True)
    return total, ranked


def measure(binary: Path, source: Path, work: Path) -> dict[str, object]:
    out = work / "cg.out"
    csv = work / "out.csv"
    subprocess.run(
        [
            "valgrind",
            "--tool=callgrind",
            "--separate-threads=yes",
            f"--callgrind-out-file={out}",
            str(binary),
            "decode",
            str(source),
            "--no-mux",
            "-o",
            str(csv),
        ],
        capture_output=True,
        check=True,
    )
    records = csv.read_bytes().count(b"\n") - 1
    threads = sorted(work.glob("cg.out-*"))
    if not threads:
        raise RuntimeError("callgrind wrote no per-thread files")
    calling_total, calling_top = annotate(threads[0])
    render = [annotate(t)[0] for t in threads[1:]]
    return {
        "records": records,
        "calling": calling_total,
        "calling_per_record": calling_total / records,
        "rendering": render,
        "rendering_per_record": sum(render) / records,
        "top": [[n, f] for n, f in calling_top[:20]],
    }


def report(result: dict[str, object], before: dict[str, object] | None) -> str:
    def change(key: str) -> str:
        if before is None:
            return ""
        old, new = float(before[key]), float(result[key])  # type: ignore[arg-type]
        return f"  ({(new - old) / old:+.2%} vs before: {old:,.0f})"

    calling = int(result["calling"])  # type: ignore[call-overload]
    lines = [
        f"records decoded: {result['records']:,}",
        f"calling thread:   {calling:>12,} instructions, "
        f"{float(result['calling_per_record']):,.1f} per record" + change("calling_per_record"),  # type: ignore[arg-type]
        f"rendering threads:{sum(result['rendering']):>12,} instructions, "  # type: ignore[arg-type]
        f"{float(result['rendering_per_record']):,.1f} per record" + change("rendering_per_record"),  # type: ignore[arg-type]
        "",
        "calling thread, costliest functions (exclusive):",
    ]
    for n, function in result["top"]:  # type: ignore[attr-defined]
        lines.append(f"  {n:>11,} {n / calling:6.1%}  {function[:110]}")
    return "\n".join(lines)


def main() -> int:
    if not sys.platform.startswith("linux") or shutil.which("valgrind") is None:
        print("instructions.py needs Linux and valgrind (apt install valgrind)", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", type=Path, help="Write the result here.")
    parser.add_argument("--compare", type=Path, help="A previous --json result to compare with.")
    args = parser.parse_args()
    binary = build()
    with tempfile.TemporaryDirectory(prefix="aero1553-instr-") as tmp:
        work = Path(tmp)
        result = measure(binary, make_input(work), work)
    before = json.loads(args.compare.read_text()) if args.compare else None
    print(report(result, before))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
