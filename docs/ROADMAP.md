# Aero1553 Roadmap

> **This roadmap is forward-looking only.** Completed work is not tracked here —
> it lives in `CHANGELOG.md` (release history), `docs/L1-REQ.md` /
> `docs/L2-REQ.md` / `docs/L3-REQ.md` (the normative requirements), and
> `docs/TRACE-MATRIX.md` (verification status), all backed by git history.
>
> **Do not mint requirement IDs (`L2-*`, `L3-*`) in this file.** An ID is born
> only when its requirement is written in `docs/L2-REQ.md` / `docs/L3-REQ.md`;
> describe intended work in prose and let the requirement process own the IDs.
> The former "Team Review Backlog", "Production-Readiness Audit", "Architecture
> Audit", and "Documentation Initiative" sections — all describing shipped work —
> were removed on 2026-07-07 for exactly this reason: provisional IDs minted here
> had begun to collide with real assignments (e.g. this file proposed
> `L2-CONF-006` for a conformance case while `docs/L2-REQ.md` had since assigned
> `L2-CONF-006` to the public-library-API requirement).

## Queued for the next release (`[Unreleased]`)

`[Unreleased]` is emptied at each release cut; whatever sits above the
most recent dated section in `CHANGELOG.md` is the live queue. Future work
accumulates here; when ready to cut a release, follow the version-bump
checklist in `docs/MAINTAINER-GUIDE.md` section 11.

## Planned

| Version | Feature |
|---------|---------|
| 6.0 (major) | **tablekit** for the new CSV and Parquet outputs, if it holds aero1553's performance. See [tablekit for CSV and Parquet (6.x)](#tablekit-for-csv-and-parquet-6x). |
| 6.x | Data-word decoder: engineering values from a site-supplied message definition, plus additional per-message-type CSVs, written through tablekit. See [Data-word decoder](#data-word-decoder-next-feature). |
| 6.x | Apache Parquet output, through tablekit. |

The maintainer decided on 2026-11-05 that the new output modes -- more CSVs and
Parquet -- come in a 6.x release built on tablekit
(github.com/joey-huckabee/tablekit), the table writer shared by the FATMOPs
tools. 6.0 is a major release because it ends the crate's zero-dependency
property (`L3-RS-002`): Parquet cannot reasonably be written without one, and
taking tablekit is that decision. The data-word decoder was 6.0 until 5.2.0,
then 5.x as an additive minor; it is back in 6.x because its per-message-type
CSVs are written through tablekit.

3.0 was taken by the timestamp-rendering split (`--input-time-format` /
`--output-time-format`, `L2-WRT-025` / `L2-WRT-026`), 4.0 by the rename to
Aero1553 and the Python package becoming a binding over the Rust crate, and 5.0
by removing the C++ implementation and replacing the `memmap2` reader with
streamed positional reads (see `CHANGELOG.md`). One breaking topic per major:
the migration note for a flag rename and the one for a new output format have
nothing to say to each other, and bundling them would have made both harder to
read.

## tablekit for CSV and Parquet (6.x)

**What it is.** tablekit (private, `joey-huckabee/tablekit`, 0.6 when assessed
on 2026-11-05) writes tables as CSV in pandas' format and as Parquet with the
types Impala reads, so every FATMOPs tool's files agree. aero1553 would write
each CSV a row at a time with tablekit's row writer (`RowWriter`, one method per
column, no per-cell dispatch) and Parquet as batches.

**Condition: it must hold aero1553's performance.** It is adopted only if:

- the existing 46-column decode is byte-identical and no slower: `perf/run.py`'s
  gates and `perf/baseline.py` against the last 5.x tag on the 256 MiB cases
  pass, measured on CI x86, A against B;
- each new output is measured the same way against a hand-written baseline
  before it ships, and memory stays constant in file size, as the CSV writer's
  does (`AGENTS.md`, "Streaming CSV");
- output is byte-identical at every `--threads` count (`L2-WRT-027`).

**The vendor CSV can move to tablekit too, once it holds the bar above.** The
46-column CSV must match DDC's output byte for byte (`L1-OUT-001`): 4-digit
upper-case hex words (`0400`), `DELTA` with six decimals (`0.000000`), text such
as `11R`. tablekit's `CsvFormat::row` could not write that without formatting
each cell twice, and measured 3.7x to 5.6x aero1553's writer. The change
proposed to tablekit (tablekit PR [#18](https://github.com/joey-huckabee/tablekit/pull/18)) adds what it needs:

- **Cell formats**: hexadecimal (`HexUpper(4)`) and fixed decimals (`Fixed(6)`)
  per column, checked against pandas and Python's own formatting;
- **a row writer** that writes each cell straight into the row, named for its
  type and format, with a separator after each cell rather than a test before
  it.

Measured on 2026-11-05 on the 21 MB golden recording (460,575 records),
rendering only, one thread, on the sandbox's ARM host: aero1553's writer
112.6 ns per record, tablekit's row writer 109.0 ns (0.97x), 71 MB of output
byte-identical. Callgrind counts 1,787 against 1,876 instructions per record
(1.05x). Before the move, the full gates above must pass on CI x86, with
`parallel.rs` keeping its threads and only the per-record rendering changing.
Moving it would put every CSV the tool writes on one library.

**What aero1553 must change to take it.**

- **Supply-chain gate (`rust/deny.toml`).** Assessed with aero1553's own policy
  on 2026-11-05, `tablekit-parquet` fails it:
  - licences outside `MIT` / `Apache-2.0`: `BSD-3-Clause` (`snap`, `zstd-safe`,
    `zstd-sys`), `CC0-1.0` (`tiny-keccak`), `Unicode-3.0` (`unicode-ident`);
  - duplicate versions, refused by `multiple-versions = "deny"`: `syn` 2 and 3,
    `getrandom` 0.2 / 0.3 / 0.4, `r-efi`;
  - the tablekit crates are unlicensed, and come from git, not crates.io.

  Each needs an explicit allowance with its reason. `tablekit-csv` alone adds
  only `itoa` and `zmij` (both `MIT OR Apache-2.0`, no dependencies).
- **Opt-in at build time.** Parquet behind a Cargo feature, so that a build
  without it keeps the zero-dependency core and the gate checks only the core.
  Whether the release binaries enable it is a 6.0 decision.
- **C code in the build.** `zstd-sys` compiles C. The SLES 12 SP5 binary
  (cargo-libc, glibc 2.22) and the Windows exe (static C runtime, `L3-RS-020`)
  must be re-proven with `scripts/compare-binaries.py` and
  `scripts/pe-imports.py`, as for every build-method change.
- **A private git dependency.**
  - CI needs a read-only deploy key or token secret for tablekit. A workflow's own `GITHUB_TOKEN` cannot read another private repository.
  - The dependency is pinned by tag (`rev =`).
  - `scripts/offline-bundle.py` must vendor `rust/`'s dependencies as well, now that the CLI has some; `cargo vendor` handles git sources.

**Changes suggested to tablekit** (to raise there, not here):

1. **`ParquetWriter::finish` should return the writer**, as `CsvWriter::finish`
   does (`(W, u64)`). Today it drops `W`. aero1553 commits every output itself:
   the final flush is part of the commit and is where disk-full is classified
   (`L2-WRT-024`), and the move is non-replacing under `--no-clobber`
   (`L2-WRT-023`). Without `W` back, a flush error is lost on drop.
2. **Make `tablekit-profile` optional for `tablekit-parquet`, or compile the
   built-in profile at build time.** `ParquetWriter::new` parses the built-in
   TOML at run time, which brings `serde`, `serde_derive`, `toml` 0.7 and
   `toml_edit` 0.19 (and with them the second `syn`) into every Parquet writer,
   for a table that never changes. The crate already has a `build.rs`.
3. **Row groups by size, not by batch** -- already item 2 on tablekit's
   roadmap. aero1553 needs a stated memory cap for a Parquet row group, like
   `max_sort_group`, so the cap must be tablekit's to enforce.
4. **Tagged releases.** tablekit is private and, by its own rule, has no CI:
   its checks are run locally before each push (its `README.md`). aero1553 pins
   a dependency by tag and runs its own gates on every build that uses it, so
   what it needs from tablekit is a tag per release, not CI. (This roadmap
   first asked for CI there; that contradicted tablekit's rules.)
5. **A licence** (even a proprietary one) in its `Cargo.toml`, so dependents'
   licence gates can name it rather than special-case "unlicensed".
6. **Cell formats and a row writer** for the vendor CSV -- proposed in
   tablekit PR [#18](https://github.com/joey-huckabee/tablekit/pull/18), with the figures above.

## Data-word decoder (next feature)

Today the CSV carries each message's data words as raw hex (`WD01`..`WD32`). The
decoder would turn them into engineering values -- `airspeed_kts = 245.3`,
`weight_on_wheels = true` -- using a message definition (an ICD) supplied by the
site: for each RT / subaddress / direction, which words and bits hold which
signal, with scaling, signedness, units and multi-word values.

**Open questions, to settle with the maintainer before any code:**

- **Definition format.** What do sites have today -- a spreadsheet, XML, a
  vendor format? The loader would most naturally extend the hand-rolled TOML
  schema, but a converter from what sites already maintain may matter more than
  the native format.
- **Output shape.** Extra columns appended after column 46 (the vendor block
  must not move, `L1-OUT-001`), or a separate engineering-units CSV per run or
  per message type (the "additional per-message-type CSVs" in the table above).
- **Ownership.** Who writes and maintains the definitions, and how a definition
  that disagrees with a recording (wrong word count, unknown RT/SA) is reported:
  a WARN per message type, an error, or an empty cell.
- **Python.** Decoded values should reach `MieMessage` / `to_dict()` /
  `columns()` as well as the CSV.

It must keep the existing guarantees: streaming in constant memory, byte-identical
output for a decode that does not enable it, and no new dependency.

## Merge follow-ups

The multi-file time-sorted merge has shipped (see `CHANGELOG.md`); the items
below are candidate refinements on top of it, **none built yet**. The first two
groups are next-release candidates (investigate-then-schedule); `--order file`
is distant-future. None has a committed version.

**Recorder-identity-aware merge.** The `MUX` column is already populated from a
configurable filename field (delimiter + 0-based index). Two related ideas could
reuse that same parsed identity:

- **Per-recorder DELTA — partly delivered in v2.11.0.** Merged DELTA is now
  selectable between `per-file` (the default) and `global` via `--delta-scope` /
  `[merge] delta_scope` (L2-MRG-005), which covers the common case: one file per
  recorder. What remains is the *identity* half of the original idea — keying
  DELTA on a recorder parsed from the file name (the MUX mechanism) rather than
  on the input file itself, which would matter only if one recorder's output were
  split across several files, or several recorders' output combined into one.
- **Identity-based residual tiebreak.** Equal-timestamp rows now order by
  `RT` then `MSG` (L1-OUT-003), so the input's position in the resolved list is
  no longer the *primary* tiebreak. It survives as the **residual** one: two
  records that share a timestamp *and* an RT *and* a MSG still fall back to
  `(file_index, within-file sequence)` — which for a genuine cross-recorder
  duplicate means "whichever file you listed first wins". A future release could
  make that residual order key on the parsed recorder identity instead, so the
  survivor is chosen by which recorder it came from rather than by argument
  order. (`--collapse-duplicates` already addresses the common case by emitting
  one row instead of choosing between two.)
- **Optional `TERM_NAME` from the filename** — the same delimiter+index
  mechanism (a second configurable field) could populate the still-empty
  `TERM_NAME` column, if a terminal-name field is encoded in the name.
- **Richer locator** — if delimiter+index proves insufficient for some naming
  scheme, a hand-rolled wildcard locator (reusing the `--glob` matcher) could be
  added without a new dependency.

**Cross-recorder de-duplication refinements.** Collapsing duplicate transactions
seen by multiple recorders already ships behind `--collapse-duplicates` /
`[merge] collapse_duplicates` (off by default, loss-free; window via
`--collapse-window-us`). Possible follow-ups, none built:

- **Witness annotation** — an optional column recording how many / which
  recorders saw a collapsed event (changes the CSV schema, so it would be its
  own opt-in).
- **Clock-skew alignment** — estimate / correct a per-recorder time offset
  rather than relying on a fixed `--collapse-window-us` tolerance.
- **Survivor-selection policy** — prefer the cleanest copy (e.g. the non-error
  read) rather than always keeping the first in heap order.
- **Deduped `count`** — collapsing applies to the `decode` CSV path today; a
  deduped `count` mode could follow.

**Merging streams (deferred, possible future change).** Deferred 2026-11-01 by
the maintainer; not scheduled. A merge needs its inputs side by side, so
one pipe cannot carry it: `cat a.mie b.mie | aero1553 decode -` is one stream,
which ends at `a`'s terminator (5.7.0 warns, `L2-RDR-022`). Process
substitution names each stream separately --
`aero1553 decode <(ssh rec1 cat a.mie) <(ssh rec2 cat b.mie)` -- and the merge
already reads each input forwards one record at a time, so the forward-only
window behind `decode -` (`L2-CLI-023`) could back each one. Today such an input
fails as `MIE file is empty (0 bytes): /dev/fd/63`, because inputs are opened as
regular files (size first, then positional reads). To settle before building:
- telling a pipe or FIFO input from a file (and refusing a terminal);
- the empty and collision checks, as `decode -` settled them;
- **a `MUX` label per input**, since `/dev/fd/63` names nothing -- a second
  manifest column, or one `--mux-value` per input matched by position. The
  single `--mux-value` is refused on a merge (`L2-WRT-028`), so allowing a
  per-input form later breaks nothing;
- Windows, which has no `<(...)`: named pipes, or Linux and macOS only.

**`--order file` (non-time merge).** A distant-future opt-in that would
concatenate inputs in CLI/manifest order **without** time-sorting, for sets that
are not calendar-locked IRIG (Standard counters, freerun, or mixed-format).
Today such sets are hard-rejected (exit 6) because they cannot share an absolute
timeline; `--order file` would let an operator explicitly accept a non-time
ordering. It is gated behind an explicit flag precisely so the default can never
silently emit a misleadingly "sorted" CSV — the operator must opt out of the
time guarantee. Output rows would carry their source-file order; DELTA would be
per file (no global timeline exists in this mode). Not scheduled; recorded here
so the request isn't folded into the time-merge contract without separate design.

## Decode correctness

- **IRIG day-field decoding across DDC card models.** Known limitation in
  v1.0.0 (carried from Python). The bit layout for the day-of-year field
  appears to vary between firmware versions; needs reverse-engineering
  across a sample set with cross-references against vendor CSV.
  - **Status: blocked on external data** — cannot proceed without real
    sample recordings. The v1.5.0 PRA-9 work only made the discrepancy
    *visible* (a one-time advisory, demoted from WARN to INFO and given a
    dedicated opt-out per L2-LOG-001); the actual decode fix is deferred
    until ground-truth data is available.
  - **What's known.** The decoder extracts day-of-year as a 9-bit binary
    integer from Upper-Word bits 13–5 (`(upper >> 5) & 0x1FF`), which is
    correct per the DDC specification. Hour, minute, second, microsecond,
    and the freerun bit all decode correctly and match vendor CSV on every
    observed card model. Only the day-of-year field diverges, and only on
    *some* models — suggesting the firmware encodes that field differently
    (leading hypotheses: BCD rather than binary, or a different field width
    / bit offset). Freerun recordings are unaffected (the field carries no
    calendar meaning when the internal oscillator is running).
  - **What we need to collect** (the external dependency): for each of
    several DDC card models / firmware revisions — (1) a real `.mie`
    recording, (2) the vendor-generated CSV for that *same* file (the
    oracle), and (3) the known real calendar date the recording was made.
    A model/firmware identifier per sample is needed to tell a per-model
    encoding from a universal mis-slice.
  - **What we're looking for / method.** For each sample, pull the raw
    16-bit Upper Word and tabulate decoded-day vs vendor-day vs true-day.
    Solve for the transform that maps our value to the vendor's
    (BCD-decode of the bits? a shifted/widened bit window? an offset?), and
    determine whether it correlates with card model/firmware. Outcome:
    either a single corrected extraction or a model-keyed decode, landed as
    a spec'd requirement with byte-exact conformance fixtures so output
    matches vendor CSV. Until then the advisory stays -- at INFO, and
    suppressible outright with `--no-irig-day-advisory` (L2-LOG-001) for a
    site that has already validated its own card model.

## Release artifacts and process (partly done)

`AGENTS.md` anticipates impl-prefixed tags (`rust-vX.Y.Z`, `python-vX.Y.Z`)
while every release so far has been a joint cut from one tag, and
`scripts/repo-hygiene.sh` fails the build when the implementations declare
different versions. No impl-prefixed tag is needed unless they later diverge.

**Done.** The Linux x86-64 binary that serves SLES 12 SP5 is built on every push
by the `rust-glibc-2-22` CI job with a pinned
[cargo-libc](https://github.com/rustcommons/cargo-libc)
(`cargo libc build --distro sles-12-sp5`, since 5.9.0; cargo-zigbuild before),
checked to reference no symbol newer than `GLIBC_2.22`, tested with every system
call kernel 4.12 lacks returning `ENOSYS`, and run against the whole conformance
suite (`L3-RS-018`). Releases since 5.0.0 attach it as
`aero1553-x86_64-linux-glibc2.22`, with its SHA-256 in the release notes.

**Still to do:**

- **Wheels for an older glibc.** cargo-libc can also build a PyO3 extension for
  an older glibc through maturin. Today's wheels are built separately.
- **Test on a real SLES 12 SP5 host.** Since 5.10.1 CI runs the conformance
  suite on the userland of SUSE's SLES 12 SP5 base image (its loader, glibc 2.22
  and `libgcc_s.so.1`), and the tests under a simulated kernel 4.12. What is left
  is the combination on one machine -- SUSE's own kernel under that userland --
  which only a real host or VM gives. Decide who runs it, and how it is recorded
  for each release.
- **Automate the release.** Attaching the binary is manual today: download the
  artifact from the tag's CI run, check `--version` and the glibc floor, upload
  it, and add its SHA-256 to the notes. A workflow triggered by a `v*` tag could
  do all of it, and could create the release from the `CHANGELOG.md` section.
- **Code-sign the Windows exe.** Since 5.10.0 each release attaches
  `aero1553-x86_64-windows.exe` (`L3-RS-020`), unsigned: Windows SmartScreen
  warns on first run, and some antivirus products quarantine unsigned programs.
  Signing needs a code-signing certificate, a cost and a decision of its own.
- **Windows on ARM64** (`aarch64-pc-windows-msvc`), if a site asks for it.

Merged branches are deleted by hand, on the remote and locally together, so
the two stay consistent; the repository's "delete head branches on merge"
setting stays off (decided 2026-10-31).

## Performance

5.0.0 to 5.2.0 made the reader stream, the row formatter about 3x faster and row
rendering parallel (see `CHANGELOG.md`). Rendering is no longer the bottleneck:
from two rendering threads on, a decode waits on the **calling thread** -- read,
validate, decode, filter, order, merge, and write -- which is why eight threads
measured no faster than four. 5.3.0 took the two largest costs off that thread
-- ordering stopped allocating per timestamp (1.5x-1.8x on a whole decode) and
`DELTA` moved to a fixed table (a further 6-16%). 5.4.0 profiled that thread:
a third of it was copying the 152-byte record between stages through `memmove`.
Skipping the filter stage when no filter is set, and shrinking `MieMessage` to
128 bytes so those copies inline, cut `memmove` from 12.2% to 1.6% of the thread
and made a decode 0.70x-0.80x the time of 5.3.0. 5.5.0 removed false sharing
on the per-file `MUX` string (a `MUX`-populated decode 6-12% faster) and kept
records out of the merge heap (a merge 10-19% faster). 5.6.0 changed no
decode speed; it added `decode -` (measured at the same cost as a file),
steadier gates and a per-thread profiler. 5.6.1 fixed a Python config-loading
bug on long Windows paths and changed no decode code. 5.7.0 cut the decoding
thread's instructions per record by 11% (decode 0.911 of 5.6.1's time on arm64,
within noise on the x86 CI runners). Beyond that, in the
order they are expected to pay:

- **Profile the calling thread again.** Measure before choosing the next
  target; `perf/profile.py` (5.6.0) reports user and system CPU per thread
  from `/proc`, and `perf/instructions.py` counts the calling thread's
  instructions per record (callgrind), steady enough to price a 1-2% change.
  Its first use took the calling thread from 1,037 to 919 instructions per
  record (-11.4%; 256 MiB decode 0.911 of 5.6.1's time on arm64, and within
  noise on the x86 CI runners, which cannot resolve a few percent): a payload
  read straight into `DataWords`, log levels tested before a line's
  arguments are built, and the read window's per-record test kept inline.
  Tried and dropped as under 2%: caching the canonical-order run's timestamp
  key, a one-record fast path in the order stage, and inlining the validator.
  What remains is spread thin: the order stage moving each record in and out
  of its buffer (~14%), DELTA (~7%), and validation re-reading the Type Word
  and timestamp words the decoder then reads again (~7%, and the "one
  validation implementation" commitment applies). Its first run, on a 256 MiB decode to a 906 MB CSV on native
  Linux (arm64, 4 CPUs, 2 rendering threads), put the calling thread at 97%
  busy -- 0.80 s user against 0.12 s system -- and each rendering thread at
  31%, with 5 MB peak memory, the same from standard input. So on that host
  the next target is the calling thread's own decoding, not the kernel's
  writes; the WSL figures that follow disagree, so profile the deployment hosts
  (SLES 12, Windows) before choosing. A WSL profile after the merge-heap change found the decoding thread
  spending about as long in the kernel writing the CSV as decoding (1.1 s
  system against 1.45 s user on a 930 MB output; perf at
  `perf_event_paranoid=2` does not show kernel time -- read
  `/proc/<pid>/task/*/stat`); a writer thread would overlap the two but
  changes the `L2-WRT-027` rule that every write stays on the calling
  thread. Copies into the rendering batch are about 2% now. Any change must
  keep the output byte-identical and the N-record look-ahead (`L2-SYN-026`).
- **Parallel decoding and writing.** Workers only format CSV; reading,
  decoding, ordering, merging and the output writes stay on one thread, so more
  formatting workers cannot speed those up. For **multi-file** decodes: decode
  each input on its own thread into a bounded queue, then merge
  deterministically on the calling thread -- the merge already holds one record
  per input. For **single-file** decodes: profile whether a separate writer
  thread would overlap decoding with storage waits. Either way the
  error-continuation state (`prev_was_error`, the `0x2000` / `0x2001`
  classification) and `DELTA` state must carry across every boundary exactly as
  they do today. `rust/src/parallel.rs`, `rust/src/merge.rs`.
- **Indexed duplicate lookup in the merge (deferred, possible future
  change).** Deferred 2026-11-01: the maintainer's sites do not see duplicate
  messages in their output, so `--collapse-duplicates` is not in use there and
  this would speed up a path nobody runs. Pick it up if a site starts merging
  with collapsing on wide windows or busy buses. With
  `--collapse-duplicates`, every record scans the whole survivor set, once to
  expire and once to match (`DedupWindow::is_duplicate`, `rust/src/merge.rs`).
  A wide `--collapse-window-us` or dense timestamps make that expensive. A
  content-keyed index for matching plus a timestamp-ordered index for expiry
  would make both near-constant time; keep the full equality check, the
  different-input rule and the `max_collapse_survivors` cap (L2-MRG-008). Only
  merges with collapsing enabled are affected; a single-file decode never
  reaches this code.
- **Measure small speed-ups on x86.** The CI runners' rounds spread +-25%
  (5.6.1 to the decoding-thread work: a 21-round baseline's control, with no
  Rust change, read 0.970), so `perf/baseline.py` there can only catch the
  15% regressions it was calibrated for, not confirm a 5-10% gain. A quiet
  x86 host -- WSL on the maintainer's machine, as for 5.0.0 to 5.5.0 -- or
  far more rounds would be needed.
- **Gates that differ by runner, not within one.** Since 5.6.0 each gate is
  the median of interleaved rounds, which cancels a runner slowing down during
  the job. It cannot cancel a runner whose machine type shifts the ratio
  itself: on the 5.6.1 fix's pull request (#205) one runner put
  `py.decode_csv / rust.decode_csv` at 1.226 (rounds 1.07-1.36) and
  `py.columns_pandas_numeric / rust.iterate` at 3.144 (rounds 3.10-3.19, tight,
  so not noise within the run) against limits of 1.15 and 3.0; a re-run read
  1.033 and 2.669, as usual, and the change touched neither timed path. The
  numeric gate has since been recalibrated to 3.5 and reports name the CPU
  model (`perf/README.md`, "Gate calibration"). To do: if one machine type
  keeps failing a gate, calibrate that gate per type rather than widen it for
  every runner.
- **Multi-gigabyte benchmark cases.** Release pull requests and a weekly run
  against `main` time the candidate against the previous release on 256 MiB
  generated recordings (`perf/baseline.py`, 15% margin from A/A runs). The
  multi-gigabyte cases that justified 5.0.0 to 5.2.0 are still run by hand
  (`rust/examples/synth_large.rs`, the full experiment tagged
  `archive/perf-io-backends`), and nothing under 15% is caught automatically.
- **PGO and fat LTO for the release binaries.** Typically 5-15% with no source
  change; the work is in CI, including the glibc 2.22 cargo-libc job. Best done
  after the calling-thread work, since PGO tunes whatever the code is.
- **Per-record `MUX` handle.** Every record holds a clone of one `Arc<String>` per
  input file. 5.2.0 stopped worker threads from dropping records (which moved
  the reference count between cores on every record), and rendering threads
  now copy each file's cell instead of reading the string through the `Arc`,
  whose reference count shares its cache line (`MuxCells`, `writer.rs`). Not
  cloning per record at all -- the writer looking the value up once per input
  -- would remove the remaining, now uncontended, atomic increment and
  decrement, and the 17th and later files of a merge still read through the
  `Arc`. Measure first.
- **Rust per-row allocations in the merge.** The de-duplication key and any
  remaining per-row `String` building on the merge path. Verify against the
  current code first: the 5.1.0 formatter removed the writer's per-row
  `format!` calls, and the `DELTA` key is already a packed integer
  (`delta.rs`).
- **Sequential-read hints** (`posix_fadvise(SEQUENTIAL)` on Linux). Probably
  small -- cold-cache reads already matched the memory map -- and it needs a few
  lines of `unsafe` FFI in a crate that has none. Recorded, not recommended.

None changes CSV output, so all are guarded by the byte-exact conformance
oracle.

## Cross-implementation review follow-ups (2026-09)

A review of all three implementations of the time (Rust, Python and the C++
tree removed in 5.0.0) in September 2026 found six
high-severity defects; those were fixed in #136 and are recorded in
`CHANGELOG.md` under `[4.0.0]`. Its medium-severity findings have since
been fixed or decided (#139 to #156, each recorded in `CHANGELOG.md`), the
three Python stream items -- Ctrl-C during `write_csv`, `allow_partial` on a
stream destination, and CRLF on Windows `sys.stdout` -- last. The
lower-severity findings are deferred past v4.0.0 and are still to be triaged
against the current code. Known follow-ups:

- **Merge throughput is sensitive to code layout.** While verifying #156, two
  changed error-message strings made the Python merge (`py.merge_csv`) land per
  process at either its usual time or about 12% slower, on WSL2. Interleaved
  A/B builds placed the cause in the binary's layout, not the change: the
  extension change alone was neutral. The local perf gate failed on it; CI's
  passed (0.992). To do: find which loop or buffer in the merge hot path is
  alignment-sensitive.

## Housekeeping

- **Keep this file current.** Remove an entry when it ships -- the shipped
  record lives in `CHANGELOG.md` and the requirements -- and add new candidates
  here so they are not lost.

## Shared Commitments

- **`config/default.toml` and TOML config support remain a first-class feature.** The Rust build ships a hand-rolled TOML loader for our config schema; the file format is stable, and key names are stable **within a major version**. The v3.0.0 rename of `decode.time_format` to `decode.input_time_format` is the first and so far only exception, and it was taken at a major bump precisely because this commitment exists: the retired key is *rejected* by name rather than ignored, so no configuration silently changes meaning across the boundary (`L2-CFG-012`).
- **CSV column layout matches DDC vendor output byte-for-byte.** No reordering or renaming of columns, including the vendor placeholder columns (`MUX`, `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`). `TERM_NAME`/`IM_GAP`/`RCV_GAP`/`XMT_GAP` remain empty. As of L2-WRT-020 the `MUX` *cell* is populated from the input file name by default (its column position is unchanged); `--no-mux` / `[mux] enabled = false` restores empty MUX for a byte-for-byte vendor diff. As of L2-WRT-025 the `TIME_STAMP` *cell* has a selectable rendering whose **default is the vendor form**, so this commitment continues to hold for an invocation that selects nothing; `iso` and `dom` depart from it deliberately and are documented as an exception in `VENDOR-CSV-DIFFS.md` §3c.
- **Sync recovery semantics preserved.** N-record look-ahead (default `N = 2` per L2-SYN-005, configurable via L2-SYN-026), 64 KB scan cap, error records and SPURIOUS_DATA continuations remain valid records that pass validation.
- **One validation implementation.** Header skip, normal forward decode, and post-loss recovery share the same validation rules through the boolean compatibility wrapper or the detailed failure API. There is no weaker fast path.
- **Cross-implementation conformance.** Text-based fixtures under
  `tests/conformance/` exercise shared decoding, recovery, filtering, config,
  error, and CSV behavior against byte-exact output oracles in CI.

## Out of Scope (Pinned)

### IRIG 106 1553 decode support is out of scope for Aero1553

See `docs/L1-REQ.md` NR-001. MIE files use a DDC proprietary
record format that is distinct from IRIG 106 Chapter 10 1553 packet
formats. Adding IRIG 106 1553 decode is a new capability — separate
requirements, design analysis, architecture review, and approval —
not an incremental extension of Aero1553. Any inbound feature
request that says "just add IRIG 106 support" SHALL be redirected
to a new requirements + design review.
