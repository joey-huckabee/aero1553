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
| 5.x (minor) | Data-word decoder: engineering values from a site-supplied message definition, plus additional per-message-type CSVs. See [Data-word decoder](#data-word-decoder-next-feature). |
| Major, provisional | Apache Parquet output. |

The data-word decoder was listed as 6.0 until 5.2.0. It is additive and opt-in
-- a decode that does not ask for it produces exactly today's CSV -- so under
SemVer it is a minor release, not a major one. Parquet keeps a provisional major
slot until its design is settled: a Parquet writer without a dependency is a
large piece of work, and taking one would end the crate's zero-dependency
property (`L3-RS-002`), which is a decision in its own right.

3.0 was taken by the timestamp-rendering split (`--input-time-format` /
`--output-time-format`, `L2-WRT-025` / `L2-WRT-026`), 4.0 by the rename to
Aero1553 and the Python package becoming a binding over the Rust crate, and 5.0
by removing the C++ implementation and replacing the `memmap2` reader with
streamed positional reads (see `CHANGELOG.md`). One breaking topic per major:
the migration note for a flag rename and the one for a new output format have
nothing to say to each other, and bundling them would have made both harder to
read.

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
- **An explicit `MUX` value** (`--mux-value TEXT`, `[mux] value`) -- for a
  file whose name does not encode its recorder, and for standard input
  (`decode -`, 5.6.0), whose `MUX` is empty because it has no name.
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

## SonarCloud security findings on the input path (resolved)

**Resolved.** `main` failed the SonarCloud quality gate from the v2.12.0 cut
until this was taken. One condition was failing — `new_security_rating` 5 where
the gate requires 1 — driven by exactly two vulnerabilities, both on the
`open(self._path, "rb")` in `python/src/aero1553/reader.py`:

- **`pythonsecurity:S2083`** (blocker) — "Change this code to not construct the
  path from user-controlled data."
- **`pythonsecurity:S8707`** (major) — the agentic path-injection rule.

Every other condition passed throughout (reliability 1, maintainability 1,
coverage 94.2%, duplication 0.0%, hotspots reviewed 100%).

**Correction to what this entry previously said.** It described the findings as
the *CLI input path* being operator-supplied, and reasoned from there. The taint
flow SonarCloud actually reports says otherwise:

```
merge.py:58     Source: the CONTENTS of a --manifest file
merge.py:60-61  -> line -> trimmed
cli.py:637      -> paths
cli.py:1131     -> input_paths
reader.py:240   -> open()
```

The tainted value is not the argument the operator typed. It is each **line of a
manifest file**. That matters because it makes the `config.py` justification —
"the path *is* the interface" — the wrong argument to reach for, and the entry's
two proposed options were both framed around it.

**What was done, and why.** Both rules are now suppressed for `reader.py` in
`.github/workflows/sonarcloud.yml`, scoped to those rules in that one file, with
the flow and the reasoning written out next to the exclusion.

**Since v4.0.0** the Python reader, manifest read and dump are the Rust crate's,
run through the compiled extension, so the `reader.py` / `merge.py` / `dump.py`
suppressions were removed: the code they covered no longer exists. The judgement
below is unchanged (the C++ `dump.cpp` entry recorded the same one until the C++
implementation was removed in 5.0.0).

The justification is narrower than the `config.py` one: a manifest's contents are
exactly as trusted as the operator who chose that manifest. Reading the files it
lists is the documented purpose of `--manifest` (`L2-MRG-001`); the process holds
precisely the permissions of the user who ran it; and anyone able to write the
manifest can already invoke the decoder with any argument they like, so the flow
confers no capability that was not already there.

**Precedent.** `S8707` on this *same flow* was already resolved Won't Fix at
`merge.py:58` (the source itself), `dump.py:70` and `dump.py:119`. Those
judgements live only in SonarCloud; these two are recorded in the repository so
they survive for someone reading the source.

This is the **first `S2083` suppression** in this repository. The previous
version of this entry asked that it be taken deliberately rather than as a step
in a release, and it was: as its own change, with the decision put to the
maintainer and the alternatives (hardening the manifest reader, or resolving in
the SonarCloud UI) considered and declined.

**Why not harden instead.** Validating manifest lines would be a behaviour change
to what a manifest may contain, would need matching Rust work and a conformance
case, and would not reliably clear the finding: Sonar recognises specific
sanitiser shapes, and a file-type or existence check is not one of them.
`config.py`'s `is_file()` guard is worth having on its own merits — and is kept —
but the record shows its `S8707` cleared as `FIXED` only alongside the exclusion,
so it is not evidence that hardening alone satisfies the rule.

**The cost that was being carried, now discharged.** v2.11.1 was cut
*specifically* to stop releases merging against a failing gate: before it,
`sonar.qualitygate.wait` was set only for pushes to the default branch, so four
PRs merged green while `main` was red. That fix works — the gate is blocking, and
the previous version of this entry existed because it blocked. `main` merging red
restored the very state v2.11.1 removed; it no longer does.

**If either rule fires again elsewhere, it still fails the build.** The exclusions
are per-rule and per-file by design. `docs/CONFIG-REFERENCE.md`'s "Trust boundary"
section carries the operator-facing statement of the same decision — keep the two
in step.

## Release artifacts and process (partly done)

`CLAUDE.md` anticipates impl-prefixed tags (`rust-vX.Y.Z`, `python-vX.Y.Z`)
while every release so far has been a joint cut from one tag, and
`scripts/repo-hygiene.sh` fails the build when the implementations declare
different versions. No impl-prefixed tag is needed unless they later diverge.

**Done.** The Linux x86-64 binary that serves SLES 12 SP5 is built on every push
by the `rust-glibc-2-22` CI job with
`cargo zigbuild --release --target x86_64-unknown-linux-gnu.2.22`, checked to
reference no symbol newer than `GLIBC_2.22`, and run against the whole
conformance suite (`L3-RS-018`). Releases 5.0.0 to 5.2.0 attach it as
`aero1553-x86_64-linux-glibc2.22`, with its SHA-256 in the release notes.

**Still to do:**

- **Test on a real SLES 12 SP5 host.** The glibc-2.22 link target proves which
  symbols the binary asks for, not that it runs on the real host; no release
  has been run on one yet. Decide who does that check, on what machine, and how
  it is recorded for each release.
- **Automate the release.** Attaching the binary is manual today: download the
  artifact from the tag's CI run, check `--version` and the glibc floor, upload
  it, and add its SHA-256 to the notes. A workflow triggered by a `v*` tag could
  do all of it, and could create the release from the `CHANGELOG.md` section.
- **Windows x64 binary**, built with the MSVC toolchain
  (`x86_64-pc-windows-msvc`). Needs deciding: is a static CRT
  (`-C target-feature=+crt-static`) wanted, so the binary does not depend on the
  VC++ redistributable being present?

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
bug on long Windows paths and changed no decode code. Beyond that, in the
order they are expected to pay:

- **Profile the calling thread again.** Measure before choosing the next
  target; `perf/profile.py` (5.6.0) reports user and system CPU per thread
  from `/proc`. Its first run, on a 256 MiB decode to a 906 MB CSV on native
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
- **Indexed duplicate lookup in the merge.** With
  `--collapse-duplicates`, every record scans the whole survivor set, once to
  expire and once to match (`DedupWindow::is_duplicate`, `rust/src/merge.rs`).
  A wide `--collapse-window-us` or dense timestamps make that expensive. A
  content-keyed index for matching plus a timestamp-ordered index for expiry
  would make both near-constant time; keep the full equality check, the
  different-input rule and the `max_collapse_survivors` cap (L2-MRG-008). Only
  merges with collapsing enabled are affected; a single-file decode never
  reaches this code.
- **Gates that differ by runner, not within one.** Since 5.6.0 each gate is
  the median of interleaved rounds, which cancels a runner slowing down during
  the job. It cannot cancel a runner whose machine type shifts the ratio
  itself: on the 5.6.1 fix's pull request (#205) one runner put
  `py.decode_csv / rust.decode_csv` at 1.226 (rounds 1.07-1.36) and
  `py.columns_pandas_numeric / rust.iterate` at 3.144 (rounds 3.10-3.19, tight,
  so not noise within the run) against limits of 1.15 and 3.0; a re-run read
  1.033 and 2.669, as usual, and the change touched neither timed path. Record
  the CPU model in the report, and if one machine type keeps doing this,
  calibrate per type or widen those two limits.
- **Multi-gigabyte benchmark cases.** Release pull requests and a weekly run
  against `main` time the candidate against the previous release on 256 MiB
  generated recordings (`perf/baseline.py`, 15% margin from A/A runs). The
  multi-gigabyte cases that justified 5.0.0 to 5.2.0 are still run by hand
  (`rust/examples/synth_large.rs`, the full experiment tagged
  `archive/perf-io-backends`), and nothing under 15% is caught automatically.
- **Enforce the Windows performance gates.** Since 5.6.0 the suite runs on
  Windows report-only. Once a few runs show where its ratios sit, set limits
  and enforce them.
- **PGO and fat LTO for the release binaries.** Typically 5-15% with no source
  change; the work is in CI, including the glibc 2.22 zigbuild job. Best done
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

## Config ergonomics (deferred)

The config parser (Rust's, which the Python package also runs) is
held to a **whitelist** of the flat `[section]` + `key = value` schema:
anything outside it is a config error (exit 5) on every implementation, and a differential parity corpus keeps them aligned. When that
whitelist was drawn, a few *readable* full-TOML number forms were **rejected on
both** for strictness and consistency:

- underscore digit separators — `standard_tick_rate_hz = 1_000_000`;
- `0x` / `0o` / `0b` integer prefixes — e.g. `detect_records = 0x08`.

These are genuinely handy for a human writing a config, and `tomllib` already
accepts them; the decision to reject was about keeping the Rust hand-rolled
parser minimal and the two implementations identical, not because the forms are
harmful. A future release could instead **accept them on both** — teach the Rust
number parser to strip `_` and honor the `0x` / `0o` / `0b` prefixes, and flip the corresponding
`config_parity.py` snippets from `reject` to `accept`. It is an additive
ergonomics change with no effect on `config/default.toml` (which uses plain
numbers); not scheduled, recorded here so the option isn't lost. Any change must
keep the two parsers byte-for-byte aligned via the parity corpus.

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

## Non-ASCII text in output (deferred, needs direction)

Found while fixing non-UTF-8 command-line arguments (#153). `L2-CLI-014` says
every byte written to stdout or stderr is ASCII, and that holds for the
program's own text -- but text taken from outside (file names, operating-system
error messages) is written as it comes.

**What is wrong today**

| | Where | Rust (and the Python CLI, which is Rust) | Effect |
|---|---|---|---|
| A | File names in diagnostics and log lines ("MIE file not found: ...", "opened ...", writer and merge messages) | about 47 `path.display()` sites: UTF-8 as-is, invalid bytes as U+FFFD | non-ASCII on stderr; `café.mie` reads as `cafÃ©.mie` on a code-page-437 Windows console |
| B | `dump`'s `File:` line (stdout payload) | `to_string_lossy` (U+FFFD) | not ASCII, as `L2-CLI-014` requires |
| C | Operating-system error text inside messages | localized by Windows | non-ASCII on any non-English Windows |
| D | The `_errors` file name under `--separate-errors` | **fixed**: built from the OS name, byte-exact (was a lossy stem: `out<FF>.csv` yielded `out<U+FFFD>_errors.csv`) | none now (was a wrong file name, reachable since #153) |
| E | The `MUX` column, derived from the input file name | lossy | a name that is not UTF-8 is not reproduced exactly |

**Proposed fix (not started)**

1. One ASCII rendering rule for anything printed: printable ASCII as-is (backslash included, so Windows paths
   read normally); any other valid character as `\u{e9}`; a byte that is not
   UTF-8 as `\xFF`; an unpaired surrogate as `\u{d800}`; control characters
   escaped, so a file name holding a newline cannot forge log lines. The #153
   argument stand-ins move to it (they use Rust's `Debug` form today, which
   doubles every backslash in a Windows path).
2. Apply it at every site in A and B. (D, building the `_errors` name from the
   raw OS name, is done.)
3. Tests: one table of names and expected renderings; conformance cases for `dump` and a "not found" message on a
   non-ASCII name (the runner needs a field to name a case's input file).

**Open decisions**

- **`MUX` data (E).** *Always escaped*: the column, stdout and the Python
  library all show `caf\u{e9}`; the CSV becomes pure ASCII and byte-identical
  across implementations for any name, and Excel or Windows PowerShell 5.1
  cannot mangle it -- but an analyst matching on `MUX` must use the escaped
  form, the escape is not strictly reversible, and it changes output for anyone
  already using non-ASCII names. *Verbatim UTF-8*: natural to match on, but the
  CSV is no longer guaranteed ASCII. The vendor diff is unaffected either way
  (the vendor tool leaves `MUX` empty; `--no-mux` is the vendor-exact mode).
- **Printed text (A-C).** *ASCII plus escaping* keeps `L2-CLI-014`, with a final
  ASCII guard on diagnostics and log lines to catch localized OS text: correct
  on every console, pipe and server. *Unicode output* replaces the rule with
  UTF-8 and `WriteConsoleW` for Windows consoles; Rust's standard library
  already writes to a console that way, but Windows pipes and non-UTF-8 server
  locales would still garble.

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
