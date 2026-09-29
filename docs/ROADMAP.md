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
| 4.0 | Data word decoders, additional per-message-type CSVs. |
| 5.0 | Apache Parquet output. |

3.0 was taken by the timestamp-rendering split (`--input-time-format` /
`--output-time-format`, `L2-WRT-025` / `L2-WRT-026`), which renumbered the two
rows above. One breaking topic per major: the migration note for a flag rename
and the one for a new output format have nothing to say to each other, and
bundling them would have made both harder to read.

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

- **Standard-tick rounding diverges between C++ and the other two.** Found
  during the v3.0.0 timestamp work, in code that release did not touch, so
  it is recorded here rather than fixed opportunistically alongside an
  unrelated change.

  `StandardTimestamp::to_microseconds` is specified as half-away-from-zero
  (L2-DEC-017), and Rust and Python implement it that way -- Rust with
  `f64::round`, Python with the explicit `floor(x) + (1 if frac >= 0.5)`.
  **C++ uses `std::floor(micros + 0.5)`**, which is the one formulation
  Python's own docstring singles out as wrong, because `x + 0.5` can round
  up across the boundary before `floor` sees it.

  - **Reachable, and pinned to a single value.** Exactly one double in the
    whole range diverges: `0.49999999999999994`, the largest double below
    `0.5`. Everywhere else the three agree, including every double in the
    2^64 neighbourhood. Reproducer: `raw_value = 1` with
    `--standard-tick-rate-hz 2000000.0000000002` yields **1 us** in C++ and
    **0 us** in Rust and Python. It takes a deliberately adversarial tick
    rate to hit, which is why no fixture has caught it -- but the rate is
    ordinary operator input, so "unreachable" would be too strong.
  - **The comment above it is wrong twice over**, which is the part worth
    fixing regardless of the arithmetic. It claims parity with Python's
    `int(x + 0.5)` -- Python does not do that and explicitly warns against
    it -- and then observes that `std::round` "has the same tie behaviour"
    without using it. A future reader checking this line against the
    requirement would be told it already agrees.
  - **Fix when taken:** `std::round(micros)` in `cpp/src/models.cpp`, the
    comment corrected to name the real reference (Rust's `f64::round` and
    Python's explicit form), and a shared conformance case pinning that
    one input across all three. The change is two lines; the conformance
    case is the part that keeps it fixed.

- **Two adjacent findings from the same sweep, both benign, recorded so they
  are not re-investigated.**
  - *Range-check ordering.* Python range-checks the **unrounded** value and
    then rounds; Rust and C++ round first and check after. The orderings
    genuinely differ, but **no input distinguishes them**: every double at
    or above 2^52 is already an integer, so rounding is a no-op exactly
    where the `[0, 2^64)` bound bites. Checked over 400,000 values
    including every double in the 200 immediately below 2^64 -- zero
    divergences. This is a readability difference, not a defect; leaving it
    alone is a defensible answer.
  - *Stray-`Auto` fallback points opposite ways.* In the per-record
    timestamp decode, Rust and C++ fall back to IRIG when handed an
    unresolved `Auto`, while Python's `if fmt == IRIG: ... else: standard`
    falls back to Standard. **Unreachable in all three** -- every
    implementation resolves `Auto` to a concrete format before iteration
    begins -- so this is a defensive path only. Worth aligning on the next
    edit to that function for the same reason the L3-RDR-001 delta-key
    extraction was worth doing: two implementations quietly disagreeing
    about a "cannot happen" case is how a later change makes it happen.

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

## Phase 3: release artifacts and the versioning scheme (needs direction)

Carried over from the retired open-decisions register, where this was the only
entry still unresolved. Nothing here is built.

`CLAUDE.md` anticipates impl-prefixed tags (`rust-vX.Y.Z`, `python-vX.Y.Z`,
`cpp-vX.Y.Z`) while every release so far has been a joint cut from one tag.

**Decided — C++ ships in the joint cut, as source.** It has shipped that way
since **v2.13.0**: all three implementations declare one version from one tag,
and `scripts/repo-hygiene.sh` fails the build when they disagree. No `cpp-v*`
tag is needed unless the implementations later diverge. This entry asked the
question long after the facts had answered it; recorded here as settled so it
is not re-opened. Note the scope: *source* ships, in the sense every release so
far has shipped — no release attaches prebuilt binaries for any implementation.

**Still needs deciding** (the artifact questions, which are genuinely open):

- Linux artifact built in the `gcc:4.8` container (runs on glibc 2.13 and up,
  so SLES 12 SP5 is covered) -- confirm that is the intended build host.
- Windows artifact: MSVC Release x64. Is a static CRT wanted so the binary does
  not depend on the VC++ redistributable being present?
- Real SLES 12 SP5 deployment verification is **manual and out of CI** -- the
  `gcc:4.8` container is Debian 7 / glibc 2.13, a conservative proxy that proves
  C++11 conformance, not deployability. Who does that check, and against what?

**The three decisions that shared that file are settled**, and their reasoning
lives where it is acted on rather than in a register of its own: the merge
overwrite question (closed, no change -- pinned by the `clobber-*` conformance
cases), the C++ coverage and fuzz gates (built -- see `cpp/Makefile`'s coverage
threshold comment and `cpp/tests/test_fuzz.cpp`'s header), and SonarCloud C++
analysis (added -- see the suppression list in
`.github/workflows/sonarcloud.yml`, where each entry carries its own
justification). `CHANGELOG.md` records all three.

## C++ modernisations deferred from the SonarCloud sweep (deferred)

Two SonarCloud C++ rules are suppressed in `.github/workflows/sonarcloud.yml`
as **deferred, not rejected**. Both are real improvements; both are recorded
here so the suppression does not quietly become a decision nobody revisits.

**`cpp:S3642` — replace `enum` with `enum class` (16 sites).** Blocked on
cross-implementation contract rather than on C++ taste. These enums carry
**wire values** and are used as integers throughout, mirroring Rust's
`#[repr(u8)]` discriminants and Python's `IntEnum`. `enum class` removes the
implicit conversion to `int`, so adopting it means an explicit cast at every
use and a C++-only divergence from a shape the other two implementations
share. Worth doing only as a deliberate three-implementation decision about
how message types are spelled, not as a lint cleanup.

**`cpp:S2807` — make member operator overloads hidden friends (18 sites).**
A broad reshuffle of operator declarations with no behavioural effect and no
effect on the conformance oracles. Genuinely better C++; simply not worth a
sweeping diff across `models.hpp`, `merge.hpp` and `order.cpp` on its own.
A reasonable rider on the next change that touches those types.

Everything else from that sweep is either fixed or suppressed for a reason
that will not expire — a C API signature, the C++11 floor, a documented design
decision, or a tool that cannot parse the alternative. Those are catalogued at
the suppression list itself.

## Performance refinements (deferred)

Surfaced by a source review and **verified real but deliberately deferred** —
these are per-record heap churn, not unbounded growth. Both implementations
already stream in constant memory (the design guarantee that matters), so these
would trade code churn in the hottest paths for a marginal, unprofiled gain.
Recorded here so the option isn't lost; revisit only if profiling shows
per-record allocation is a real bottleneck.

- **Rust per-row allocations.** The writer/merge path allocates a `String` DELTA
  key and a `Vec<u16>` dedup key, and formats `format!`/`msg_label()` strings per
  row. Candidate: borrow or intern the keys and reuse scratch buffers so a decode
  makes O(1) allocations rather than O(rows).
- **Python double-build on the error path.** The reader constructs a `MieMessage`
  twice for an errored record. Candidate: build it once and reuse.

Neither changes CSV output, so both are guarded by the existing byte-exact
conformance oracle if attempted.

## Config ergonomics (deferred)

The config parsers (Rust's, which the Python package also runs, and C++'s) are
aligned on a **whitelist** of the flat `[section]` + `key = value` schema:
anything outside it is a config error (exit 5) on every implementation, and a differential parity corpus keeps them aligned. When that
whitelist was drawn, a few *readable* full-TOML number forms were **rejected on
both** for strictness and consistency:

- underscore digit separators — `standard_tick_rate_hz = 1_000_000`;
- `0x` / `0o` / `0b` integer prefixes — e.g. `detect_records = 0x08`.

These are genuinely handy for a human writing a config, and `tomllib` already
accepts them; the decision to reject was about keeping the Rust hand-rolled
parser minimal and the two implementations identical, not because the forms are
harmful. A future release could instead **accept them on both** — teach the Rust
number parser to strip `_` and honor the `0x` / `0o` / `0b` prefixes, teach the C++
loader the same, and flip the corresponding
`config_parity.py` snippets from `reject` to `accept`. It is an additive
ergonomics change with no effect on `config/default.toml` (which uses plain
numbers); not scheduled, recorded here so the option isn't lost. Any change must
keep the two parsers byte-for-byte aligned via the parity corpus.

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
