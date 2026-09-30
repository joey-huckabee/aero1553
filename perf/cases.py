"""The Python half of the performance suite: one case per process.

``perf/run.py`` starts this module once per case::

    python perf/cases.py CASE --input A [--input B] [--output OUT] --repeat N

and reads the one JSON line it prints: the raw time of every run, split into
the case's phases (for example ``columns`` then ``dataframe``), a fingerprint
of what the run produced, and the process's peak memory.

Imports are not timed here -- a library imported by the case is imported
before the first run, and ``run.py`` times imports separately, each in a
fresh interpreter (the ``import.*`` cases). The report adds them back into
the case's total.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Libraries each case needs, beyond ``aero1553``: imported before timing,
#: and named in the report so their import cost is added to the total.
NEEDS: dict[str, tuple[str, ...]] = {
    "py.iterate": (),
    "py.filter_order": (),
    "py.decode_csv": (),
    "py.cli_decode": (),
    "py.merge_csv": (),
    "py.columns_numpy": ("numpy",),
    "py.columns_pandas": ("numpy", "pandas"),
    "py.columns_pandas_numeric": ("numpy", "pandas"),
    "py.to_dict_dataclass": (),
    "py.attrs_dataclass": (),
    "py.to_dict_pandas": ("pandas",),
    "py.attrs_pandas": ("pandas",),
    "py.csv_pandas": ("pandas",),
}
CASES = tuple(NEEDS)

#: The fields a user-defined dataclass declares in the dataclass cases.
DATACLASS_FIELDS = (
    "timestamp",
    "rt",
    "subaddress",
    "msg_label",
    "status_word",
    "data_words",
    "delta",
)


@dataclasses.dataclass(slots=True)
class Transaction:
    """What a user might declare for their own analysis."""

    timestamp: str
    rt: int | None
    subaddress: int | None
    msg_label: str
    status_word: int | None
    data_words: tuple[int, ...]
    delta: float | None


def _peak_bytes() -> int | None:
    if sys.platform != "win32":
        import resource  # noqa: PLC0415 -- POSIX only

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    try:
        import psutil  # noqa: PLC0415 -- optional on Windows
    except ImportError:
        return None
    return int(psutil.Process().memory_info().peak_wset)


class Phases:
    """Times the named phases of one run."""

    def __init__(self) -> None:
        self.times: dict[str, float] = {}

    def __call__(self, name: str, fn: Callable[[], Any]) -> Any:
        start = time.perf_counter()
        result = fn()
        self.times[name] = self.times.get(name, 0.0) + time.perf_counter() - start
        return result


def _rt_fingerprint(rts: Any) -> tuple[int, int]:
    values = rts.tolist() if hasattr(rts, "tolist") else list(rts)
    return len(values), sum(v for v in values if v is not None and v >= 0)


def run_case(case: str, inputs: list[Path], output: Path | None, p: Phases) -> dict[str, Any]:
    """Run ``case`` once; return its fingerprint."""
    from aero1553 import MieFileReader, columns  # noqa: PLC0415
    from aero1553.config import FilterConfig  # noqa: PLC0415
    from aero1553.filters import apply_filters  # noqa: PLC0415
    from aero1553.merge import merge_readers  # noqa: PLC0415
    from aero1553.order import order_rows  # noqa: PLC0415
    from aero1553.writer import write_csv  # noqa: PLC0415

    a = inputs[0]
    if case == "py.iterate":

        def iterate() -> tuple[int, int]:
            n = s = 0
            for m in MieFileReader(a):
                n += 1
                s += m.rt or 0
            return n, s

        rows, rt_sum = p("decode", iterate)
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.filter_order":
        cfg = FilterConfig(exclude_types={0x20})

        def filtered() -> tuple[int, int]:
            n = s = 0
            for m in order_rows(apply_filters(MieFileReader(a), cfg)):
                n += 1
                s += m.rt or 0
            return n, s

        rows, rt_sum = p("decode", filtered)
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.decode_csv":
        out = p("decode+write", lambda: write_csv(order_rows(MieFileReader(a)), output))
        return {"rows": out.normal_count + out.error_count}
    if case == "py.cli_decode":
        from aero1553.cli import main  # noqa: PLC0415

        code = p("decode+write", lambda: main(["decode", str(a), "-o", str(output)]))
        return {"exit": code}
    if case == "py.merge_csv":
        readers = [MieFileReader(path) for path in inputs]
        out = p(
            "merge+write",
            lambda: write_csv(order_rows(merge_readers(readers, collapse_duplicates=True)), output),
        )
        return {"rows": out.normal_count + out.error_count}
    if case == "py.columns_numpy":
        import numpy as np  # noqa: PLC0415

        cols = p("columns", lambda: columns(MieFileReader(a)))
        arrays = p(
            "numpy",
            lambda: {k: np.asarray(v) for k, v in cols.items() if isinstance(v, memoryview)},
        )
        rows, rt_sum = _rt_fingerprint(arrays["rt"])
        return {"rows": rows, "rt_sum": rt_sum}
    if case in ("py.columns_pandas", "py.columns_pandas_numeric"):
        import numpy as np  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415

        from aero1553.table import FIELDS  # noqa: PLC0415

        text = {"timestamp", "msg_label", "mux", "datetime", "data_words"}
        fields = [f for f in FIELDS if f not in text] if case.endswith("numeric") else None
        cols = p("columns", lambda: columns(MieFileReader(a), fields=fields))
        cols.pop("data_words", None)  # a (n, 32) matrix stays in NumPy
        df = p(
            "dataframe",
            lambda: pd.DataFrame(
                {k: np.asarray(v) if isinstance(v, memoryview) else v for k, v in cols.items()}
            ),
        )
        rows, rt_sum = _rt_fingerprint(df["rt"].to_numpy())
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.to_dict_dataclass":
        rows_ = p(
            "to_dict+dataclass",
            lambda: [Transaction(**m.to_dict(fields=DATACLASS_FIELDS)) for m in MieFileReader(a)],
        )
        rows, rt_sum = _rt_fingerprint([r.rt for r in rows_])
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.attrs_dataclass":
        # The same dataclass filled by reading attributes -- what a user
        # writes without to_dict(); the baseline to_dict_dataclass is measured
        # against.
        rows_ = p(
            "attributes+dataclass",
            lambda: [
                Transaction(
                    timestamp=m.timestamp.format(),
                    rt=m.rt,
                    subaddress=m.subaddress,
                    msg_label=m.msg_label,
                    status_word=m.status_word,
                    data_words=m.data_words,
                    delta=m.delta,
                )
                for m in MieFileReader(a)
            ],
        )
        rows, rt_sum = _rt_fingerprint([r.rt for r in rows_])
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.to_dict_pandas":
        import pandas as pd  # noqa: PLC0415

        dicts = p("to_dict", lambda: [m.to_dict() for m in MieFileReader(a)])
        df = p("dataframe", lambda: pd.DataFrame.from_records(dicts))
        rows, rt_sum = _rt_fingerprint(df["rt"].fillna(-1).astype(int).to_numpy())
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.attrs_pandas":
        import pandas as pd  # noqa: PLC0415

        def by_hand() -> list[dict[str, Any]]:
            out = []
            for m in MieFileReader(a):
                cw = m.command_word
                out.append(
                    {
                        "timestamp": m.timestamp.format(),
                        "rt": m.rt,
                        "subaddress": m.subaddress,
                        "msg_label": m.msg_label,
                        "command_word": None if cw is None else cw.raw,
                        "status_word": m.status_word,
                        "data_words": m.data_words,
                        "delta": m.delta,
                        "mux": m.mux,
                    }
                )
            return out

        dicts = p("attributes", by_hand)
        df = p("dataframe", lambda: pd.DataFrame.from_records(dicts))
        rows, rt_sum = _rt_fingerprint(df["rt"].fillna(-1).astype(int).to_numpy())
        return {"rows": rows, "rt_sum": rt_sum}
    if case == "py.csv_pandas":
        import pandas as pd  # noqa: PLC0415

        p("decode+write", lambda: write_csv(order_rows(MieFileReader(a)), output))
        df = p("read_csv", lambda: pd.read_csv(output, dtype=str, keep_default_na=False))
        rt = [int(v) for v in df["RT"] if v]
        return {"rows": len(df), "rt_sum": sum(rt)}
    raise ValueError(f"unknown case {case}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    import aero1553  # noqa: F401, PLC0415 -- imported before timing, like the libraries below

    for lib in NEEDS[args.case]:
        __import__(lib)
    runs: list[dict[str, float]] = []
    fingerprint: dict[str, Any] = {}
    for _ in range(args.repeat):
        phases = Phases()
        fingerprint = run_case(args.case, args.input, args.output, phases)
        runs.append(phases.times)
    print(
        json.dumps(
            {
                "case": args.case,
                "needs": list(NEEDS[args.case]),
                "runs": runs,
                "fingerprint": fingerprint,
                "peak_bytes": _peak_bytes(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
