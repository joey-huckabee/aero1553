"""Baseline against candidate: the previous release's CLI and this one's, timed
side by side on large synthetic recordings.

Run from the repository root, in the Python package's environment::

    uv --directory python run python ../perf/baseline.py \\
        --base BASE/aero1553 --cand CAND/aero1553 --synth CAND/synth_large

``perf/run.py`` gates on ratios between two cases of *one* build -- Python
against Rust -- so a slowdown in the shared Rust core moves both sides and
every gate stays healthy. This compares two builds instead: the baseline
binary (CI builds the latest release tag) and the candidate, on the same
runner, on inputs big enough that start-up does not hide the decode.

The inputs are tiled from the golden recording ``a-large`` by
``rust/examples/synth_large.rs``: one large recording, the same with junk
every 5000 records (sync loss and recovery throughout, decoded with
``--separate-errors``), and eight smaller ones merged.

One case compares the candidate with itself: ``decode-stdin`` decodes the
large recording from standard input (``decode -``, L2-CLI-023) against the same
file by path. The baseline release cannot read standard input, and the
question is whether a stream costs more than a file -- not whether either got
slower since the release, which the ``decode`` case answers.

Each case first runs both binaries once and requires identical exit code,
stdout, stderr and output files: timing two programs that give different
answers measures nothing. Then the two run in shuffled order, round by round,
and the case's figure is the median of the per-round ratios candidate/baseline
-- pairing each candidate run with the baseline run beside it cancels the
runner drifting during the job. A case fails when that median exceeds
``1 + margin``. See ``perf/README.md`` for how the margin was measured.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MIB = 1 << 20
MERGE_FILES = 8

_spec = importlib.util.spec_from_file_location("golden", ROOT / "tests" / "golden" / "golden.py")
assert _spec is not None and _spec.loader is not None
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)


def synth(tool: Path, template: Path, out: Path, size: int, *extra: str) -> Path:
    """Tile ``template`` into ``out`` of ``size`` bytes, reusing an existing
    file of that size (the generator is deterministic)."""
    if not (out.exists() and out.stat().st_size >= size):
        cmd = [
            str(tool),
            "--template",
            str(template),
            "--out",
            str(out),
            "--bytes",
            str(size),
            *extra,
        ]
        subprocess.run(cmd, check=True)
    return out


#: One side of a case: the binary ("base" or "cand"), its arguments after the
#: binary, and a file to read as standard input (or None).
Side = tuple[str, list[str], Path | None]


def inputs(tool: Path, data: Path, mib: int) -> dict[str, dict[str, Side]]:
    """The cases: name -> the two sides to compare. ``{out}`` in an argument
    is the output directory."""
    data.mkdir(parents=True, exist_ok=True)
    template = golden.write("a-large", data)
    size = mib * MIB
    big = synth(tool, template, data / f"s{mib}m.mie", size)
    corrupt = synth(tool, template, data / f"s{mib}m-corrupt.mie", size, "--corrupt-every", "5000")
    part = size // MERGE_FILES
    merged = [
        str(synth(tool, template, data / f"m{i:02d}.mie", part, "--shift-us", str(13 * i)))
        for i in range(MERGE_FILES)
    ]
    plain: dict[str, list[str]] = {
        "decode": ["decode", str(big)],
        "merge": ["decode", *merged],
        "corrupt-split": ["decode", str(corrupt), "--separate-errors", "-o", "{out}/c.csv"],
    }
    cases = {
        name: {"base": ("base", a, None), "cand": ("cand", a, None)} for name, a in plain.items()
    }
    # The synthetic names carry no MUX field, so the file and the stream
    # decode to the same bytes.
    cases["decode-stdin"] = {
        "base": ("cand", ["decode", str(big)], None),
        "cand": ("cand", ["decode", "-"], big),
    }
    return cases


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(MIB):
            h.update(chunk)
    return h.hexdigest()


def _stdin(path: Path | None) -> Any:
    return path.open("rb") if path else Path(os.devnull).open("rb")


def outcome(binary: Path, args: list[str], out: Path, stdin: Path | None) -> tuple[object, ...]:
    """Exit code and SHA-256 of stdout, stderr and every output file."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    h = hashlib.sha256()
    with tempfile.TemporaryFile() as err, _stdin(stdin) as feed:
        proc = subprocess.Popen(
            [str(binary), *args], stdin=feed, stdout=subprocess.PIPE, stderr=err
        )
        assert proc.stdout is not None
        while chunk := proc.stdout.read(MIB):
            h.update(chunk)
        code = proc.wait()
        err.seek(0)
        stderr = hashlib.sha256(err.read()).hexdigest()
    files = tuple(sorted((p.name, digest_file(p)) for p in out.iterdir()))
    return code, h.hexdigest(), stderr, files


def timed(binary: Path, args: list[str], stdin: Path | None) -> float:
    with _stdin(stdin) as feed:
        start = time.perf_counter()
        subprocess.run(
            [str(binary), *args],
            stdin=feed,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return time.perf_counter() - start


def scratch_dir() -> str | None:
    """Memory-backed storage for the split case's CSV when there is some, as
    in perf/run.py: disk writeback stalls some runs and not others."""
    return "/dev/shm" if Path("/dev/shm").is_dir() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", type=Path, required=True, help="The baseline aero1553 binary.")
    parser.add_argument("--cand", type=Path, required=True, help="The candidate aero1553 binary.")
    parser.add_argument("--synth", type=Path, required=True, help="The synth_large example binary.")
    parser.add_argument(
        "--data", type=Path, help="Where to keep the recordings (default: a temporary directory)."
    )
    parser.add_argument(
        "--mib", type=int, default=256, help="Size of each case's input in MiB (default 256)."
    )
    parser.add_argument("--rounds", type=int, default=7, help="Timed rounds per case (default 7).")
    parser.add_argument(
        "--margin", type=float, default=0.15, help="Allowed slowdown, 0.15 = 15%% (default)."
    )
    parser.add_argument("--only", action="append", help="Run only this case (repeatable).")
    parser.add_argument("--out", type=Path, help="Write baseline-report.json and .md here.")
    parser.add_argument(
        "--scratch",
        type=Path,
        help="Where the cases write their output (default: /dev/shm when present). "
        "Needs room for about one input's worth of CSV.",
    )
    a = parser.parse_args()

    with tempfile.TemporaryDirectory(dir=a.scratch or scratch_dir()) as tmp:
        data = a.data or Path(tmp) / "data"
        out = Path(tmp) / "out"
        binaries = {"base": a.base, "cand": a.cand}
        cases = {
            name: {
                side: (
                    binaries[which],
                    [arg.replace("{out}", str(out)) for arg in args],
                    stdin,
                )
                for side, (which, args, stdin) in sides.items()
            }
            for name, sides in inputs(a.synth, data, a.mib).items()
            if not a.only or name in a.only
        }

        for name, sides in cases.items():
            base, cand = (
                outcome(*sides["base"][:2], out, sides["base"][2]),
                outcome(*sides["cand"][:2], out, sides["cand"][2]),
            )
            if base != cand:
                print(f"{name}: OUTPUT DIFFERS\n  base {base}\n  cand {cand}", flush=True)
                return 1
            print(f"{name}: output identical (exit {base[0]}, stdout, stderr, files)", flush=True)

        rng = random.Random(17)
        results = []
        for name, sides in cases.items():
            runs: dict[str, list[float]] = {"base": [], "cand": []}
            for _ in range(a.rounds):
                order = ["base", "cand"]
                rng.shuffle(order)
                for side in order:
                    runs[side].append(timed(*sides[side]))
            ratios = [c / b for b, c in zip(runs["base"], runs["cand"], strict=True)]
            ratio = statistics.median(ratios)
            results.append(
                {
                    "case": name,
                    "base_median_s": statistics.median(runs["base"]),
                    "cand_median_s": statistics.median(runs["cand"]),
                    "ratio": ratio,
                    "ratio_min": min(ratios),
                    "ratio_max": max(ratios),
                    "runs": runs,
                    "pass": ratio <= 1 + a.margin,
                }
            )
            r = results[-1]
            print(
                f"{name:14s} base {r['base_median_s']:7.3f}s  cand {r['cand_median_s']:7.3f}s  "
                f"cand/base {ratio:.3f} ({min(ratios):.3f}-{max(ratios):.3f})  "
                f"{'ok' if r['pass'] else 'REGRESSION'}",
                flush=True,
            )

    lines = [
        f"### Baseline against candidate ({a.mib} MiB inputs, {a.rounds} rounds, "
        f"margin {a.margin:.0%})",
        "",
        "| Case | Baseline median | Candidate median | Candidate / baseline (median of rounds) "
        "| Range | |",
        "|---|---:|---:|---:|---:|---|",
    ]
    lines += [
        f"| {r['case']} | {r['base_median_s']:.3f} s | {r['cand_median_s']:.3f} s "
        f"| {r['ratio']:.3f} | {r['ratio_min']:.3f}-{r['ratio_max']:.3f} "
        f"| {'ok' if r['pass'] else '**regression**'} |"
        for r in results
    ]
    lines += [
        "",
        "Every case's output was identical between its two sides. `decode-stdin` "
        "compares the candidate with itself: standard input (`decode -`) against "
        "the same file by path.",
    ]
    report = "\n".join(lines) + "\n"
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "baseline-report.json").write_text(json.dumps(results, indent=2) + "\n")
        (a.out / "baseline-report.md").write_text(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write(report)
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
