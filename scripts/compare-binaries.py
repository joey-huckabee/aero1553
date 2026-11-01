"""Compare two builds of the aero1553 CLI: is the new one the same program?

    python3 scripts/compare-binaries.py OLD NEW [--rounds N]

Run from the repository root on Linux with binutils (``objdump``,
``readelf``). Two builds of one commit made by different tools -- linkers,
optimisation settings -- are never byte-identical, so "the same" is judged on
what reaches a user:

1. **What the binary asks of the system.** The program interpreter, the
   shared libraries it needs, and every symbol it imports with its version.
   These must be identical: a symbol imported at a different version is a
   different requirement on the target machine.
2. **What it does.** Every golden recording (``tests/golden``) and every
   conformance input (``tests/conformance/inputs``) is run through both
   binaries under several commands and options -- decode, count, dump, a
   two-file merge, an error -- and stdout, stderr and the exit code must
   match byte for byte.
3. **How fast it is**, for information only: a whole decode of the largest
   recording, interleaved rounds, medians. Never a pass or fail -- a shared
   runner's timing moves by far more than a link difference would.

Exit 0 when (1) and (2) match. Written for the cargo-zigbuild to cargo-libc
switch (5.9.0); reusable for any change to how the release binary is built.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFORMANCE = ROOT / "tests" / "conformance"

_spec = importlib.util.spec_from_file_location("golden", ROOT / "tests" / "golden" / "golden.py")
assert _spec is not None and _spec.loader is not None
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)


def _tool(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def requirements(binary: Path) -> dict[str, list[str]]:
    """Interpreter, needed libraries and versioned imports of ``binary``."""
    interp = re.findall(
        r"Requesting program interpreter: ([^\]]+)\]", _tool("readelf", "-l", str(binary))
    )
    needed = re.findall(
        r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", _tool("readelf", "-d", str(binary))
    )
    imports = []
    for line in _tool("objdump", "-T", str(binary)).splitlines():
        if "*UND*" in line:
            fields = line.split()
            # "... *UND* <size> <version> <name>": weak symbols add a flag
            # column, so take the last two fields.
            imports.append(f"{fields[-2]} {fields[-1]}")
    return {"interpreter": interp, "needed": sorted(needed), "imports": sorted(set(imports))}


def newest_glibc(imports: list[str]) -> str:
    versions = [m.group(1) for i in imports if (m := re.search(r"GLIBC_([\d.]+)", i))]
    return max(versions, key=lambda v: tuple(int(p) for p in v.split(".")), default="none")


def _hex_input(spec: Path) -> bytes:
    """A conformance input: hexadecimal, `#` comments, any whitespace."""
    text = "".join(line.split("#", 1)[0] for line in spec.read_text(encoding="utf-8").splitlines())
    return bytes.fromhex("".join(text.split()))


def cases(work: Path) -> list[list[str]]:
    """Every invocation both binaries must agree on, paths relative to ``work``."""
    recordings = {name: golden.write(name, work).name for name in golden.NAMES}
    out: list[list[str]] = []
    for path in recordings.values():
        out += [
            ["decode", path],
            ["decode", path, "--no-mux", "--threads", "0"],
            ["decode", path, "--output-time-format", "iso", "--year", "2026"],
            ["decode", path, "--exclude-types", "SPURIOUS_DATA", "--max-sort-group", "1"],
            ["count", path],
            ["dump", path, "--records", "200"],
            ["--log-level", "debug", "decode", path, "--no-mux"],
        ]
    a_small, b_small = recordings["a-small"], recordings["b-small"]
    a_large, b_large = recordings["a-large"], recordings["b-large"]
    out += [
        ["decode", a_small, b_small],
        ["decode", a_large, b_large, "--collapse-duplicates", "--delta-scope", "global"],
        ["decode", "missing.mie"],
        ["decode", "--no-such-flag"],
        ["--help"],
        ["--version"],
    ]
    inputs = sorted((CONFORMANCE / "inputs").glob("*.hex"))
    for i, spec in enumerate(inputs):
        name = f"c{i:03d}.mie"
        (work / name).write_bytes(_hex_input(spec))
        out += [["decode", name], ["--log-level", "debug", "count", name]]
    return out


def run(binary: Path, args: list[str], work: Path) -> tuple[int, bytes, bytes]:
    result = subprocess.run([str(binary), *args], cwd=work, capture_output=True, check=False)
    return result.returncode, result.stdout, result.stderr


def timing(old: Path, new: Path, work: Path, rounds: int) -> tuple[float, float]:
    path = golden.write("a-large", work).name
    times: dict[Path, list[float]] = {old: [], new: []}
    for _ in range(rounds):
        for binary in (old, new):
            start = time.perf_counter()
            subprocess.run(
                [str(binary), "decode", path, "-o", "t.csv"],
                cwd=work,
                check=True,
                stderr=subprocess.DEVNULL,
            )
            times[binary].append(time.perf_counter() - start)
            (work / "t.csv").unlink()
    return statistics.median(times[old]), statistics.median(times[new])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--rounds", type=int, default=7, help="Timing rounds (default 7).")
    args = parser.parse_args()
    old, new = args.old.resolve(), args.new.resolve()
    problems: list[str] = []

    print(f"old: {old} ({old.stat().st_size:,} bytes)")
    print(f"new: {new} ({new.stat().st_size:,} bytes)")
    req_old, req_new = requirements(old), requirements(new)
    for key in ("interpreter", "needed", "imports"):
        if req_old[key] == req_new[key]:
            print(f"same {key}: {len(req_old[key])} entr{'y' if len(req_old[key]) == 1 else 'ies'}")
        else:
            gone = sorted(set(req_old[key]) - set(req_new[key]))
            added = sorted(set(req_new[key]) - set(req_old[key]))
            problems.append(f"{key} differ: only in old {gone}, only in new {added}")
    newest = [newest_glibc(r["imports"]) for r in (req_old, req_new)]
    print(f"newest glibc: old {newest[0]}, new {newest[1]}")

    with tempfile.TemporaryDirectory(prefix="aero1553-compare-") as tmp:
        work = Path(tmp)
        invocations = cases(work)
        differing = 0
        for argv in invocations:
            a, b = run(old, argv, work), run(new, argv, work)
            if a != b:
                differing += 1
                parts = zip(("exit", "stdout", "stderr"), a, b, strict=True)
                which = [name for name, x, y in parts if x != y]
                problems.append(f"{' '.join(argv)}: {', '.join(which)} differ")
        print(f"same behaviour: {len(invocations) - differing} of {len(invocations)} invocations")
        t_old, t_new = timing(old, new, work, args.rounds)
        print(
            f"decode of a-large, median of {args.rounds} interleaved rounds (information only): "
            f"old {t_old:.3f}s, new {t_new:.3f}s, new/old {t_new / t_old:.3f}"
        )

    if problems:
        print("\nDIFFERENT\n" + "\n".join(problems), file=sys.stderr)
        return 1
    print("the two binaries are the same program: same requirements, same behaviour")
    return 0


if __name__ == "__main__":
    sys.exit(main())
