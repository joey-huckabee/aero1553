"""The performance suite: Rust and Python, one report, gated on ratios.

Run from the repository root, in the Python package's environment::

    uv --directory python run python ../perf/run.py --repeat 5

It builds the golden recordings (``tests/golden``, checked against their
pinned hashes), builds the Rust harness (``rust/benches/perf.rs``) and CLI,
then runs every case -- Rust library, Python library, and both CLIs as whole
processes -- and checks what each produced against the golden pins, so a
fast wrong answer fails rather than winning. It writes ``perf-report.json``
and ``perf-report.md`` (and the GitHub job summary when there is one).

Gates (``perf/gates.json``) compare two cases *of the same run*. Shared CI
runners vary run to run by more than any regression worth catching, so an
absolute time limit would fail at random; a ratio between two cases measured
side by side on the same machine does not. Everything else is reported, not
gated. See ``perf/README.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PERF = ROOT / "perf"
RUST = ROOT / "rust"
EXE = ".exe" if sys.platform == "win32" else ""

_spec = importlib.util.spec_from_file_location("golden", ROOT / "tests" / "golden" / "golden.py")
assert _spec is not None and _spec.loader is not None
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)

sys.path.insert(0, str(PERF))
from cases import CASES as PY_CASES  # noqa: E402
from cases import NEEDS  # noqa: E402

RUST_CASES = ("rust.iterate", "rust.filter_order", "rust.decode_csv", "rust.merge_csv")
IMPORTS = ("aero1553", "numpy", "pandas")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recording(name: str, data: Path) -> Path:
    """The golden recording ``name`` in ``data``, reused when its hash checks."""
    pinned = golden.manifest()["recordings"][name]
    path = data / pinned["file"]
    if path.exists() and sha256(path) == pinned["sha256"]:
        return path
    return golden.write(name, data)


def memory_backed_dir() -> str | None:
    """Where the cases write their CSV: memory-backed storage when there is
    some (``/dev/shm`` on Linux), else the default temporary directory.

    The write cases produce 100-150 MB of CSV per run, five runs each. On a
    CI runner's disk, dirty-page writeback then stalls some runs and not
    others -- the first run of a case was reliably fast and the rest up to 60%
    slower -- so which case a stall landed on decided a gate: the merge ratio
    read 0.91 on one run and 1.25 on the next with no code change. Writing to
    memory takes the disk out of a measurement of the decoder.
    """
    shm = Path("/dev/shm")
    return str(shm) if shm.is_dir() and os.access(shm, os.W_OK) else None


def build_rust() -> tuple[Path, Path]:
    """Build the release CLI and the bench harness; return both executables.

    Raises:
        subprocess.CalledProcessError: if either cargo invocation fails.
        RuntimeError: if cargo's JSON messages name no ``perf`` bench executable.
    """
    subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=RUST, check=True)
    out = subprocess.run(
        ["cargo", "bench", "--bench", "perf", "--no-run", "--message-format=json", "--quiet"],
        cwd=RUST,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    harness = None
    for line in out.splitlines():
        msg = json.loads(line)
        if msg.get("reason") == "compiler-artifact" and msg.get("target", {}).get("name") == "perf":
            harness = msg.get("executable")
    if not harness:
        raise RuntimeError("could not find the perf bench executable")
    return RUST / "target" / "release" / f"aero1553{EXE}", Path(harness)


def run_json(cmd: list[str]) -> dict[str, Any]:
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def time_process(
    cmd: list[str], repeat: int, *, peak: bool = True
) -> tuple[list[float], int | None]:
    """Wall time of a whole process, and (POSIX) its peak memory."""
    runs = []
    for _ in range(repeat):
        start = time.perf_counter()
        subprocess.run(cmd, check=True, capture_output=True)
        runs.append(time.perf_counter() - start)
    peak_bytes = None
    if peak and sys.platform != "win32":
        # RUSAGE_CHILDREN is the largest child a process has had, so it is
        # read in a fresh wrapper whose only child is the command itself.
        wrapper = (
            "import resource, subprocess, sys;"
            "subprocess.run(sys.argv[1:], check=True, capture_output=True);"
            "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024)"
        )
        peak_bytes = int(
            subprocess.run(
                [sys.executable, "-c", wrapper, *cmd], check=True, capture_output=True, text=True
            ).stdout
        )
    return runs, peak_bytes


def time_import(lib: str, repeat: int) -> list[float]:
    """Seconds to import ``lib`` in a fresh interpreter, ``repeat`` times."""
    code = f"import time; t = time.perf_counter(); import {lib}; print(time.perf_counter() - t)"
    return [
        float(
            subprocess.run(
                [sys.executable, "-c", code], check=True, capture_output=True, text=True
            ).stdout
        )
        for _ in range(repeat)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--data", type=Path, help="Where to keep the golden recordings (cached).")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Where to write the report.")
    parser.add_argument(
        "--small",
        action="store_true",
        help="Use the small recordings (a smoke run). Gates are reported, not enforced: "
        "they are calibrated on the large recording, and on a few hundred records "
        "fixed per-call costs dominate the ratios.",
    )
    parser.add_argument(
        "--no-gates", action="store_true", help="Report only; never fail on a gate."
    )
    args = parser.parse_args()

    size = "small" if args.small else "large"
    a_name, b_name = f"a-{size}", f"b-{size}"
    pins = golden.manifest()
    data = args.data or Path(tempfile.mkdtemp(prefix="aero1553-perf-"))
    data.mkdir(parents=True, exist_ok=True)
    a, b = recording(a_name, data), recording(b_name, data)
    cli, harness = build_rust()
    scratch = Path(tempfile.mkdtemp(prefix="aero1553-perf-out-", dir=memory_backed_dir()))

    results: dict[str, dict[str, Any]] = {}
    problems: list[str] = []

    def check(case: str, **want: Any) -> None:
        got = results[case].get("fingerprint", {})
        for key, value in want.items():
            if got.get(key) != value:
                problems.append(f"{case}: {key} = {got.get(key)!r}, expected {value!r}")

    def check_csv(case: str, path: Path, pinned: str) -> None:
        if sha256(path) != pinned:
            problems.append(f"{case}: CSV output differs from the golden pin")
        # Checked, so no longer needed -- and in memory-backed storage, each
        # output kept would hold its size in RAM for the rest of the run.
        path.unlink(missing_ok=True)

    # Interpreter start-up and imports, each in a fresh process.
    startup, _ = time_process([sys.executable, "-c", "pass"], args.repeat, peak=False)
    results["python.startup"] = {"runs": [{"start": t} for t in startup]}
    for lib in IMPORTS:
        results[f"import.{lib}"] = {"runs": [{"import": t} for t in time_import(lib, args.repeat)]}

    # Rust library cases.
    for case in RUST_CASES:
        name = case.split(".", 1)[1]
        cmd = [str(harness), "--case", name, "--input", str(a), "--repeat", str(args.repeat)]
        out = scratch / f"{name}.csv"
        if name == "merge_csv":
            cmd += ["--input", str(b)]
        if name in ("decode_csv", "merge_csv"):
            cmd += ["--output", str(out)]
        res = run_json(cmd)
        results[case] = {
            "runs": [{"run": t} for t in res["runs"]],
            "fingerprint": {"rows": res["rows"], "rt_sum": res["rt_sum"]},
            "peak_bytes": res["peak_bytes"],
        }
        if name == "decode_csv":
            check_csv(case, out, pins["recordings"][a_name]["csv_sha256"])
        if name == "merge_csv":
            check_csv(case, out, pins["merges"][f"{a_name}+{b_name}"]["csv_sha256"])

    # Both CLIs as whole processes: what an operator waits for.
    for case, cmd_head in (
        ("rust.cli_process", [str(cli)]),
        ("py.cli_process", [sys.executable, "-m", "aero1553"]),
    ):
        out = scratch / f"{case}.csv"
        runs, peak = time_process([*cmd_head, "decode", str(a), "-o", str(out)], args.repeat)
        results[case] = {"runs": [{"process": t} for t in runs], "peak_bytes": peak}
        check_csv(case, out, pins["recordings"][a_name]["csv_sha256"])

    # Python library cases.
    for case in PY_CASES:
        out = scratch / f"{case}.csv"
        cmd = [
            sys.executable,
            str(PERF / "cases.py"),
            case,
            "--input",
            str(a),
            "--output",
            str(out),
            "--repeat",
            str(args.repeat),
        ]
        if case == "py.merge_csv":
            cmd += ["--input", str(b)]
        results[case] = run_json(cmd)
        if case in ("py.decode_csv", "py.cli_decode", "py.csv_pandas"):
            check_csv(case, out, pins["recordings"][a_name]["csv_sha256"])
        if case == "py.merge_csv":
            check_csv(case, out, pins["merges"][f"{a_name}+{b_name}"]["csv_sha256"])

    pinned_a = pins["recordings"][a_name]
    check("rust.iterate", rows=pinned_a["rows"], rt_sum=pinned_a["rt_sum"])
    check("rust.decode_csv", rows=pinned_a["rows"])
    check("rust.merge_csv", rows=pins["merges"][f"{a_name}+{b_name}"]["rows"])
    for case in (
        "py.iterate",
        "py.columns_numpy",
        "py.columns_pandas",
        "py.columns_pandas_numeric",
        "py.to_dict_dataclass",
        "py.attrs_dataclass",
        "py.to_dict_pandas",
        "py.attrs_pandas",
        "py.csv_pandas",
    ):
        check(case, rows=pinned_a["rows"], rt_sum=pinned_a["rt_sum"])
    check("py.decode_csv", rows=pinned_a["rows"])
    check("py.cli_decode", exit=0)
    check("py.filter_order", **results["rust.filter_order"]["fingerprint"])

    # Summaries: best total per case, import cost, and a total that adds it back.
    import_median = {
        lib: statistics.median(r["import"] for r in results[f"import.{lib}"]["runs"])
        for lib in IMPORTS
    }
    for case, res in results.items():
        totals = [sum(run.values()) for run in res["runs"]]
        res["best"] = min(totals)
        res["median"] = statistics.median(totals)
        needs = (
            ["aero1553", *NEEDS[case]]
            if case.startswith("py.") and case != "py.cli_process"
            else []
        )
        res["imports"] = {lib: import_median[lib] for lib in needs}
        res["total_with_imports"] = res["best"] + sum(res["imports"].values())
        rows = res.get("fingerprint", {}).get("rows") or (
            pinned_a["rows"] if "cli" in case else None
        )
        res["rows_per_s"] = rows / res["best"] if rows else None

    # Gates.
    gates = json.loads((PERF / "gates.json").read_text(encoding="utf-8"))["gates"]
    gate_results = []
    for gate in gates:
        ratio = results[gate["numerator"]]["best"] / results[gate["denominator"]]["best"]
        ok = ("max" not in gate or ratio <= gate["max"]) and (
            "min" not in gate or ratio >= gate["min"]
        )
        gate_results.append({**gate, "ratio": ratio, "ok": ok})

    report = {
        "environment": {
            "aero1553": _version(),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
            "recording": {"a": pinned_a, "b": pins["recordings"][b_name]},
            "repeat": args.repeat,
        },
        "cases": results,
        "gates": gate_results,
        "problems": problems,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "perf-report.json").write_bytes(
        (json.dumps(report, indent=2) + "\n").encode("utf-8")
    )
    markdown = render(report)
    (args.out / "perf-report.md").write_bytes(markdown.encode("utf-8"))
    print(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(markdown)

    if problems:
        return 1
    enforce = not args.no_gates and not args.small
    if enforce and not all(g["ok"] for g in gate_results):
        return 1
    return 0


def _version() -> str:
    import aero1553

    return aero1553.__version__


def render(report: dict[str, Any]) -> str:
    env = report["environment"]
    a = env["recording"]["a"]
    lines = [
        "## Performance report",
        "",
        f"aero1553 {env['aero1553']} | Python {env['python']} | {env['platform']} | "
        f"{a['rows']:,} records ({a['bytes'] / 1e6:.2f} MB golden recording) | "
        f"{env['repeat']} runs per case",
        "",
        "Times are seconds. **Best** is the fastest run; **Total** adds the median import time of "
        "every library the case uses (each measured in a fresh interpreter) to that best run. "
        "Process cases include interpreter start-up and imports already.",
        "",
        "| Case | Phases (best run) | Runs | Best | Imports | Total | Records/s | Peak MB |",
        "|---|---|---|---:|---|---:|---:|---:|",
    ]
    for case, res in report["cases"].items():
        best_run = min(res["runs"], key=lambda r: sum(r.values()))
        phases = ", ".join(f"{k} {v:.3f}" for k, v in best_run.items()) if len(best_run) > 1 else ""
        runs = " ".join(f"{sum(r.values()):.3f}" for r in res["runs"])
        imports = ", ".join(f"{k} {v:.3f}" for k, v in res["imports"].items())
        rate = f"{res['rows_per_s']:,.0f}" if res.get("rows_per_s") else ""
        peak = f"{res['peak_bytes'] / 1e6:,.0f}" if res.get("peak_bytes") else ""
        lines.append(
            f"| `{case}` | {phases} | {runs} | {res['best']:.3f} | {imports} | "
            f"{res['total_with_imports']:.3f} | {rate} | {peak} |"
        )
    lines += [
        "",
        "### Gates (ratios of best times, measured side by side)",
        "",
        "| Gate | Ratio | Limit | Result |",
        "|---|---:|---|---|",
    ]
    for g in report["gates"]:
        limit = " and ".join(f"{k} {g[k]}" for k in ("min", "max") if k in g)
        lines.append(
            f"| {g['name']} (`{g['numerator']}` / `{g['denominator']}`) | "
            f"{g['ratio']:.3f} | {limit} | {'pass' if g['ok'] else '**FAIL**'} |"
        )
    if report["problems"]:
        lines += ["", "### Wrong answers", ""] + [f"- {p}" for p in report["problems"]]
    else:
        lines += ["", "Every case's output matched the golden pins."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
