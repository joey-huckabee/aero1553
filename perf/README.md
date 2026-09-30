# Performance suite

One suite for the Rust and Python implementations: every case timed on the
same golden recording, every case's output checked against the golden pins,
and one report. C++ is not part of it.

```bash
# from the repository root, in the Python package's environment
uv --directory python run python ../perf/run.py --repeat 5
uv --directory python run python ../perf/run.py --small --repeat 2    # a smoke run (gates reported, not enforced)
uv --directory python run python ../perf/run.py --data ~/.cache/aero1553-perf   # keep the recordings
```

It writes `perf-report.md` (the table below) and `perf-report.json` (every
number, for comparing runs), and appends the table to the GitHub job summary
when run in CI (`.github/workflows/perf.yml`, every pull request and push to
`main`, on Linux).

## What it measures

The input is the golden recording `a-large` (about 460,000 records, 21 MB;
`b-large` joins it for the merge cases), generated and checked against its
pinned hash by `tests/golden/golden.py`.

| Case | What is timed |
|---|---|
| `python.startup` | an empty `python -c pass`, for scale |
| `import.aero1553` / `import.numpy` / `import.pandas` | each import, in a fresh interpreter |
| `rust.iterate` | the Rust library: read every record |
| `rust.filter_order` | ... through a filter and the canonical-order stage |
| `rust.decode_csv` | ... to CSV, in canonical order (what `aero1553 decode` does) |
| `rust.merge_csv` | two recordings merged with duplicate collapsing, to CSV |
| `rust.cli_process` / `py.cli_process` | `aero1553 decode` as a whole process: the Rust binary, and `python -m aero1553` (start-up and imports included) |
| `py.iterate` / `py.filter_order` / `py.decode_csv` / `py.merge_csv` | the Python library doing what the matching `rust.*` case does |
| `py.cli_decode` | `aero1553.cli.main(["decode", ...])` in-process |
| `py.columns_numpy` | `aero1553.columns()`, then `np.asarray` on each column |
| `py.columns_pandas` | `columns()`, then a `DataFrame` |
| `py.columns_pandas_numeric` | the same with `fields=` leaving out the text fields |
| `py.to_dict_dataclass` | `MyRow(**msg.to_dict(fields=...))` per record |
| `py.attrs_dataclass` | the same dataclass filled by reading attributes by hand -- what a user writes without `to_dict()` |
| `py.to_dict_pandas` | `msg.to_dict()` per record, then `DataFrame.from_records` |
| `py.attrs_pandas` | reading attributes by hand, then `DataFrame.from_records` -- what a user writes without `to_dict()` |
| `py.csv_pandas` | decode to CSV, then `pandas.read_csv` |

The cases that write CSV write it to memory-backed storage (`/dev/shm`) where
there is some: on a CI runner's disk, writeback of the 100-150 MB each run
produces stalled some runs and not others, which decided a gate by luck.

Each Python case runs in its own process (so its peak memory is its own) and
is split into phases where that tells you something -- `columns` then
`dataframe`, `decode+write` then `read_csv`.

## Reading the report

- **Runs** -- every run's time, not just the best, so the spread is visible.
- **Best** -- the fastest run: the least disturbed by the machine.
- **Imports** -- the median import time of each library the case uses,
  measured separately in a fresh interpreter (a library imported inside a
  timed run would count once and then be free). `import pandas` alone is
  about a second.
- **Total** -- Best plus those imports: what a fresh script pays. Process
  cases already include start-up and imports.
- **Records/s**, **Peak MB** -- throughput of the best run; the process's
  peak resident memory (Linux always; Windows for Python when `psutil` is
  installed).

## What fails the build

1. **A wrong answer.** Every case's output is checked against the golden
   pins: the CSV's SHA-256 where it writes one, otherwise the row count and
   the sum of the RT column. A fast wrong answer is a failure, not a result.
2. **A gate.** `gates.json` holds ratios between two cases of the *same run*.

Why ratios: a shared CI runner's speed varies by more from run to run than
any regression worth catching, so "decode must take under N seconds" would
fail at random. Two cases measured side by side on one machine see the same
machine, so their ratio is stable -- and it is what the design promises
anyway: that the Python library costs little over the Rust library it wraps,
that `columns()` stays far ahead of building per-record objects. The first
gate paid for itself before it existed: while calibrating it, the Python
library decode ran 17% behind the Rust library, which traced to stages
iterating a reader through Python rather than taking its native stream over.

Everything else is reported, not gated. To compare two builds directly,
run the suite on each on the same machine and compare the JSON reports.

## Changing a gate

Calibrate on the CI runner, not a workstation: run the suite a few times
(`workflow_dispatch`), take the worst ratio seen, and set the limit with
margin above it. A limit tight enough to catch a 10% regression but not so
tight that noise trips it is the goal; say in the gate's `why` what it
protects.
