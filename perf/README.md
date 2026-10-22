# Performance suite

One suite for the Rust and Python implementations: every case timed on the
same golden recording, every case's output checked against the golden pins,
and one report.

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

## Baseline against candidate

The gates above cannot see a slowdown in the Rust core: it moves the Python
case and the Rust case it is divided by alike. `perf/baseline.py` compares two
*builds* instead -- the previous release's CLI and the candidate's, side by
side on one machine:

```bash
python perf/baseline.py --base OLD/aero1553 --cand NEW/aero1553 \
    --synth NEW/examples/synth_large [--data DIR] [--mib 256] [--rounds 7] [--margin 0.15]
```

It tiles the golden recording into three 256 MiB cases with
`rust/examples/synth_large.rs` -- one recording; the same with junk every 5000
records, decoded with `--separate-errors`; eight recordings merged -- and
requires both binaries to give the same exit code, stdout, stderr and output
files before it times anything. Then it runs the two in shuffled order, round
by round, and gates each case on the median of the per-round ratios
candidate / baseline: pairing each run with the one beside it cancels a runner
that drifts during the job.

CI runs it (`.github/workflows/perf-baseline.yml`) on release pull requests
(`release/*`), on any pull request labelled `perf-baseline`, and by hand, with
the latest `v*` tag as the baseline. It is not run on every pull request: it
builds twice and takes a few minutes.

**The margin, 15%, is measured, not chosen.** Calibration ran the candidate
against itself (A/A) on three runners, twice each, at two sizes:

| Inputs, rounds | A/A median ratios (18 per size) | Single rounds | Job |
|---|---|---|---|
| 256 MiB, 7 | 0.947-1.075 | 0.81-1.27 | 2-3 min |
| 512 MiB, 11 | 0.957-1.066 | 0.79-1.20 | 5-9 min |

Doubling the work did not narrow the noise -- it is the shared runner, not
start-up -- so the check uses the cheaper size, and the margin sits about twice
the worst A/A deviation seen (+7.5%). It catches a regression of 15% or more in
any case; a smaller one needs `--rounds` raised or a quiet machine. For scale,
5.3.0's candidate measured 0.64-0.73 of 5.2.0's time in the same runs.

A release that changes output on purpose fails the identity check, because
timing two different answers measures nothing. Compare against a baseline
that produces the new output (`workflow_dispatch` takes a `baseline` input),
or say in the release pull request why the check is skipped.

## Changing a gate

Calibrate on the CI runner, not a workstation: run the suite a few times
(`workflow_dispatch`), take the worst ratio seen, and set the limit with
margin above it. A limit tight enough to catch a 10% regression but not so
tight that noise trips it is the goal; say in the gate's `why` what it
protects.
