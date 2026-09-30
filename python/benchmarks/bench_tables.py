"""What does getting decoded records into pandas / NumPy / dataclasses cost?

Every case decodes the same synthetic recording and ends with the same table,
checked by a fingerprint so a fast wrong answer cannot pass. Each case runs in
its own process, so the peak-memory column is that case's alone.

Cases, grouped by what they need:

  Works today (no new API)
    iterate          baseline: iterate the reader, build nothing
    csv->pandas      write_csv to a file, then pandas.read_csv
    attrs->pandas    read attributes in Python, list of dicts, DataFrame
    attrs->dataclass read attributes in Python into a user @dataclass

  Per record: msg.to_dict()
    to_dict->pandas    msg.to_dict() per record, DataFrame.from_records
    to_dict->dataclass MyRow(**msg.to_dict()) per record

  Whole stream: aero1553.columns()
    columns->numpy     columns(reader): NumPy views, no per-record objects
    columns->pandas    columns(reader) -> DataFrame
    columns->pandas(numeric)
                       the same, with fields= leaving out the three text
                       fields -- the only per-record Python objects left

The lesson it measures: pandas and NumPy cost little themselves. What costs
is one Python object per field per record, whichever container they land in.

Usage::

    uv --directory python pip install pandas numpy psutil
    uv --directory python run python benchmarks/bench_tables.py --records 500000
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_decode import build_recording

CASES = [
    "iterate",
    "csv->pandas",
    "attrs->pandas",
    "attrs->dataclass",
    "to_dict->pandas",
    "to_dict->dataclass",
    "columns->numpy",
    "columns->pandas",
    "columns->pandas(numeric)",
]


@dataclass(slots=True)
class MyRow:
    """What a user might declare for their own analysis: the CSV's core
    fields. Built with ``MyRow(**msg.to_dict(fields=MY_FIELDS))``."""

    file_offset: int
    timestamp: str
    time_us: int | None
    message_type: int
    message_format: int
    bus: int
    error: bool
    rt: int | None
    subaddress: int | None
    direction: int | None
    msg_label: str
    command_word: int | None
    status_word: int | None
    error_word: int | None
    data_word_count: int
    data_words: tuple[int, ...]
    delta: float | None
    mux: str | None


MY_FIELDS = [f.name for f in fields(MyRow)]


def _attrs(m):  # what a user writes today, without to_dict()
    cw = m.command_word
    ts = m.timestamp
    return {
        "file_offset": m.file_offset,
        "timestamp": ts.format(),
        "time_us": ts.to_microseconds(),
        "message_type": m.type_word.message_type,
        "message_format": int(m.message_format),
        "bus": int(m.bus),
        "error": m.is_error,
        "rt": m.rt,
        "subaddress": m.subaddress,
        "direction": None if cw is None else int(cw.direction),
        "msg_label": m.msg_label,
        "command_word": None if cw is None else cw.raw,
        "status_word": m.status_word,
        "error_word": m.error_word,
        "data_word_count": len(m.data_words),
        "data_words": m.data_words,
        "delta": m.delta,
        "mux": m.mux,
    }


def run_case(case: str, path: Path) -> tuple[int, int]:
    """Build the table; return (rows, rt fingerprint)."""
    from aero1553 import MieFileReader, columns
    from aero1553.table import FIELDS

    if case == "iterate":
        n = rt = 0
        for m in MieFileReader(path):
            n += 1
            rt += m.rt or 0
        return n, rt
    if case == "csv->pandas":
        import pandas as pd

        from aero1553.writer import write_csv

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            write_csv(MieFileReader(path), out)
            df = pd.read_csv(out)
        return len(df), int(df["RT"].sum())
    if case == "attrs->pandas":
        import pandas as pd

        df = pd.DataFrame.from_records([_attrs(m) for m in MieFileReader(path)])
        return len(df), int(df["rt"].sum())
    if case == "attrs->dataclass":
        rows = [MyRow(**_attrs(m)) for m in MieFileReader(path)]
        return len(rows), sum(r.rt or 0 for r in rows)
    if case == "to_dict->pandas":
        import pandas as pd

        df = pd.DataFrame.from_records([m.to_dict(fields=MY_FIELDS) for m in MieFileReader(path)])
        return len(df), int(df["rt"].sum())
    if case == "to_dict->dataclass":
        rows = [MyRow(**m.to_dict(fields=MY_FIELDS)) for m in MieFileReader(path)]
        return len(rows), sum(r.rt or 0 for r in rows)
    if case.startswith("columns->"):
        import numpy as np

        text = {"timestamp", "msg_label", "mux"}
        fields = (
            [f for f in FIELDS if f not in text and f != "datetime"] if "numeric" in case else None
        )
        cols = columns(MieFileReader(path), fields=fields)
        arrays = {k: np.asarray(v) if isinstance(v, memoryview) else v for k, v in cols.items()}
        rt = arrays["rt"]
        if case == "columns->numpy":
            return len(rt), int(rt[rt >= 0].sum())
        import pandas as pd

        arrays.pop("data_words")  # a (n, 32) matrix: keep it in NumPy, beside the frame
        df = pd.DataFrame(arrays)
        return len(df), int(df.loc[df["rt"] >= 0, "rt"].sum())
    raise ValueError(case)


def child(case: str, path: Path, repeat: int) -> None:
    """Run one case `repeat` times in this process; print a JSON result."""
    import gc

    # Import what the case needs before timing: `import pandas` alone is
    # ~0.5-1 s and would swamp a small run.
    if "pandas" in case or "csv" in case:
        import pandas  # noqa: F401
    if "numpy" in case or "columns" in case:
        import numpy  # noqa: F401
    import aero1553.writer  # noqa: F401

    best = float("inf")
    result = (0, 0)
    for _ in range(repeat):
        gc.collect()
        start = time.perf_counter()
        result = run_case(case, path)
        best = min(best, time.perf_counter() - start)
    try:
        import psutil

        info = psutil.Process().memory_info()
        peak = getattr(info, "peak_wset", None) or info.rss
    except ImportError:
        peak = 0
    if sys.platform != "win32":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(
        json.dumps(
            {"case": case, "seconds": best, "rows": result[0], "fp": result[1], "peak": peak}
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=500_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--child", nargs=2, metavar=("CASE", "PATH"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args.child[0], Path(args.child[1]), args.repeat)
        return 0

    import aero1553

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bench.mie"
        size = build_recording(path, args.records)
        print(
            f"aero1553 {aero1553.__version__} | Python {platform.python_version()} | "
            f"{platform.system()} {platform.machine()}"
        )
        print(
            f"{args.records} records, {size / 1e6:.1f} MB, best of {args.repeat}, "
            "one process per case\n"
        )
        print(f"{'case':<20} {'seconds':>8} {'records/s':>12} {'vs iterate':>11} {'peak MB':>9}")
        results = []
        for case in CASES:
            out = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--repeat",
                    str(args.repeat),
                    "--child",
                    case,
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if out.returncode != 0:
                print(f"{case:<20} FAILED\n{out.stderr[-2000:]}")
                return 1
            results.append(json.loads(out.stdout.strip().splitlines()[-1]))
        base = results[0]
        for r in results:
            print(
                f"{r['case']:<20} {r['seconds']:>8.3f} {r['rows'] / r['seconds']:>12,.0f} "
                f"{r['seconds'] / base['seconds']:>10.1f}x {r['peak'] / 1e6:>9.0f}"
            )
        bad = [r["case"] for r in results if (r["rows"], r["fp"]) != (base["rows"], base["fp"])]
        if bad:
            print(f"\nFINGERPRINT MISMATCH: {bad}")
            return 1
        print(f"\nall cases agree: {base['rows']} rows, RT sum {base['fp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
