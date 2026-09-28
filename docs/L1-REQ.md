# Aero1553 — Level 1 Requirements

## Purpose

This document establishes the Level 1 (L1) SHALL-statement requirements for Aero1553: maintained Rust, Python and C++ libraries plus CLIs that decode proprietary binary recording files produced by Data Device Corporation (DDC) MIL-STD-1553 PCI cards into CSV output that is column-compatible with DDC's own recording software.

L1 requirements define **what** the product must do at the highest level of abstraction. They are the root of the requirements tree; L2 requirements decompose each L1 into architectural decisions, and L3 requirements decompose each L2 into implementation-level obligations. All three levels are traced through `docs/TRACE-MATRIX.md`.

## Scope

This document covers the active **three-implementation** release: Rust under `rust/`, Python under `python/`, and C++ under `cpp/`. All three ship from a single repository tag at one version number, and every requirement below binds all three unless it names an implementation explicitly. Where a statement below says "both implementations" it predates the C++ port and is to be read as binding all three; the cross-implementation conformance suite (`tests/conformance/`) runs every registered implementation and fails if one is missing, so the binding is enforced rather than asserted.

Items explicitly out of scope are recorded in the **Non-Requirements** section below. Items deferred to future releases are recorded in `docs/ROADMAP.md` rather than in this document.

## Conventions

### Requirement identifier format

Each L1 requirement is assigned a stable identifier of the form `L1-<CATEGORY>-<NNN>`, where `<CATEGORY>` is a code drawn from the table below and `<NNN>` is a zero-padded sequence number within that category. Identifiers are permanent: if a requirement is retired, its identifier is retired with it and never reused.

L2 and L3 requirements derived from an L1 use `L2-<CATEGORY>-<NNN>` and `L3-<CATEGORY>-<NNN>` respectively, with parent links recorded in the trace matrix.

Non-requirements (explicit out-of-scope items) use the prefix `NR-<NNN>` and are listed in the **Non-Requirements** section.

### SHALL language

Every requirement in this document uses the verb SHALL to express a mandatory obligation, per DO-178C and MIL-STD-498 conventions. SHOULD, MAY, and WILL are reserved for L2/L3 derivations where they carry their conventional meanings.

### Requirement metadata

Each requirement carries the following fields:

- **Statement** — the SHALL obligation itself
- **Rationale** — the reason the requirement exists, for the benefit of future maintainers
- **Verification Method** — how compliance is demonstrated, drawn from the DO-178 vocabulary: Test (T), Analysis (A), Inspection (I), Demonstration (D). Multiple methods may apply to one requirement.

**Status and verification artifacts** are tracked in [`docs/TRACE-MATRIX.md`](TRACE-MATRIX.md) — regenerated from `@pytest.mark.requirement` markers in `python/tests/`, the `/// Requirements:` doc-comment tags above Rust `#[test]` items, and the parent links in this file by `scripts/build-trace-matrix.py`. The matrix is the single source of truth for live status; this file, `L2-REQ.md`, and `L3-REQ.md` carry only the spec content above.

### Verification method vocabulary

- **Test (T)**: Executable verification by running code or the system and observing outcomes against expected behavior. Implemented as pytest test functions tagged with `@pytest.mark.requirement()` markers and Rust test functions tagged with `/// Requirements:` doc-comments referencing the requirement ID, plus the cross-implementation conformance suite under `tests/conformance/`.
- **Analysis (A)**: Logical or mathematical evaluation, including static analysis, bounded-loop proofs, and model arguments. Implemented as analysis notes in `docs/analysis/` (forthcoming) or inline in `docs/ARCHITECTURE.md`.
- **Inspection (I)**: Visual examination of code, documents, or configuration. Implemented as review records or as direct citation in the trace matrix.
- **Demonstration (D)**: Operational observation of the running system by a human operator. Implemented as procedure documents in `docs/procedures/` (forthcoming).

## Table of categories

| Code     | Title                                       |
|----------|---------------------------------------------|
| `DEC`    | Binary decoding                             |
| `OUT`    | CSV output and output destination integrity |
| `DLT`    | DELTA inter-arrival tracking                |
| `CLI`    | CLI capability surface                      |
| `LOG`    | Diagnostic logging                          |
| `MODE`   | Strict and lenient handling                 |
| `SYN`    | Synchronization and sync recovery           |
| `ERR`    | DDC error records and SPURIOUS_DATA         |
| `CFG`    | Configuration                               |
| `CONF`   | Cross-implementation conformance            |
| `EXIT`   | Exit-code semantics and operational contract|
| `ROB`    | Robustness against arbitrary input          |
| `MRG`    | Multi-file time-sorted merge                |

(Per-category and total requirement counts are intentionally omitted — they
drift as requirements are added. The requirement entries below, and the
auto-generated [`TRACE-MATRIX.md`](TRACE-MATRIX.md), are the source of truth.)

Out-of-scope items are listed separately under **Non-Requirements**.

---

## L1-DEC: Binary decoding

### L1-DEC-001

**Statement**: Each implementation SHALL decode DDC MIE binary recording files containing MIL-STD-1553 bus-monitor captures.

**Rationale**: Decoding MIE recordings is the core purpose of the product. The MIE format is proprietary to DDC and is distinct from IRIG 106 Chapter 10 packetized formats (see NR-001). Both implementations must agree on the format interpretation; shared behavior is verified by the cross-implementation conformance suite.

**Verification Method**: Test (T)

### L1-DEC-002

**Statement**: Each implementation SHALL decode IRIG timestamps with microsecond resolution and SHALL support Standard free-running timestamps.

**Rationale**: Real flight-test recordings overwhelmingly use IRIG timestamps; some bench and replay captures use the Standard 32-bit free-running counter format. The decoder must auto-detect the format from the first valid records of each file (a bounded multi-record probe; see L2-DEC-015) and apply the chosen format consistently for the remainder of the decode invocation.

**Verification Method**: Test (T)

### L1-DEC-003

**Statement**: Each implementation SHALL correctly decode all supported MIL-STD-1553 message formats and preserve bus wire order.

**Rationale**: The CSV output is used to reconstruct the on-bus sequence of transactions for replay, analysis, and validation against the bus controller's intended schedule. The supported formats are enumerated in L2-MSG-001.

**Verification Method**: Test (T)

### L1-DEC-004

**Statement**: Each implementation SHALL support Bus A and Bus B recordings.

**Rationale**: MIL-STD-1553 is a dual-redundant bus. Both buses are equally valid carriers of transactions; the recording card reports which one carried each transaction via the Type Word's bus bit.

**Verification Method**: Test (T)

### L1-DEC-005

**Statement**: Each implementation SHALL handle truncated final records without crashing.

**Rationale**: Recordings are often interrupted by operator action, power loss, or storage exhaustion. The last record may be truncated mid-word. The decoder must terminate cleanly in lenient mode and surface a structured error in strict mode rather than panic or segfault.

**Verification Method**: Test (T)

---

## L1-OUT: CSV output and output destination integrity

### L1-OUT-001

**Statement**: Each implementation SHALL produce CSV output whose leading columns are column-name and column-order compatible with DDC's vendor recording software, per the layout defined in `docs/MIE-FORMAT.md`. That vendor block is the first **44** columns, `TIME_STAMP` through `XMT_GAP`. Columns the decoder adds beyond the vendor layout SHALL be **appended after** the vendor block, so that for every column index `N` in `1..=44`, column `N` of a decoded CSV is the same field as column `N` of the vendor CSV.

**Rationale**: Index-for-index compatibility with the DDC vendor CSV enables positional (`awk $N`, column-slice) validation against vendor output and allows the decoder to drop into downstream analysis pipelines that already consume the DDC layout. The vendor placeholder columns (`MUX`, `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`) are preserved in layout position; `TERM_NAME` / `IM_GAP` / `RCV_GAP` / `XMT_GAP` stay empty, while `MUX` carries a filename-derived value by default (L2-WRT-020, restored to empty with `--no-mux` for a byte-exact vendor diff). The append-only rule for decoder-added columns exists because violating it is silent: through v2.9.0 the decoder's own `ERROR` / `ERROR_CODE` columns sat *inside* the vendor block (between `DELTA` and `IM_GAP`), which shifted the three gap columns two positions right of their vendor indices. Nothing failed loudly — the column *names* still all matched — but every positional comparison past `DELTA` was silently comparing the wrong fields. Pinning "vendor block first, decoder additions after" makes that class of drift impossible for any column added in future.

**Verification Method**: Test (T)

### L1-OUT-002

**Statement**: Each implementation SHALL preserve output destination integrity. The output file SHALL be produced via an atomic write strategy, SHALL refuse to write to the input path, and SHALL clean up partial output on failure unless the operator explicitly requested partial preservation.

**Rationale**: A decode invocation that crashes or is killed mid-stream must not leave the destination half-written and indistinguishable from a clean result. A decode invocation pointed at its own input file would corrupt the source data; this must be detected and refused before any write occurs. Together these constraints make the decoder safe to run in batch and pipeline contexts.

**Verification Method**: Test (T)

### L1-OUT-003

**Statement**: Each implementation SHALL emit CSV data rows in a canonical order: ascending `TIME_STAMP`, then ascending `RT`, then `MSG` ordered by ascending subaddress and, within one subaddress, receive (`R`) before transmit (`T`). The order SHALL be stable — records whose full sort key is equal SHALL retain their relative input order. Records that carry no Command Word (`SPURIOUS_DATA`, for which `RT` and `MSG` are empty) SHALL retain their position immediately following the record they followed on input. Reordering SHALL be confined to runs of **consecutive** records bearing an identical `TIME_STAMP`; records with differing timestamps SHALL NOT be reordered relative to one another. Resident memory SHALL remain bounded by a documented, configurable cap on the length of one such run.

**Rationale**: Before this requirement, equal-timestamp rows were emitted in an order with no data meaning: a single-file decode used raw DDC capture order, and a multi-file merge broke ties on the input's position in the resolved file list, so listing the same recordings in a different order produced a differently-ordered CSV. Analysts read a decoded recording by remote terminal and message, so ordering ties by the `RT` and `MSG` columns makes equal-timestamp traffic comparable between runs, between input orderings, and between the two implementations. Sorting by exactly the columns the reader sees (rather than by an internal key) keeps the rule verifiable from the CSV alone. Confining the permutation to runs of consecutive equal timestamps is what preserves the streaming constant-memory guarantee and leaves a non-monotonic input untouched (see L2-MRG-006, which forbids re-sorting a whole input); the cap bounds the one unbounded case, a corrupt file whose timestamps all decode to the same value. Pinning Command-Word-less records preserves the adjacency that the `0x2000` "continuation of a preceding error" code (L1-ERR-001, L2-ERR-005) is defined in terms of — a sort that separated a spurious record from its parent error would leave that code describing nothing.

**Verification Method**: Test (T)

### L1-OUT-004

**Statement**: Each implementation SHALL support operator-selectable rendering of the `TIME_STAMP` column. The default rendering SHALL be the day-of-year form, whose cell content is byte-identical to what L1-OUT-001 vendor compatibility requires, so that an invocation that selects no rendering explicitly remains vendor-diffable. Additional renderings MAY resolve the IRIG day-of-year field to a calendar date, but SHALL do so only from information supplied out of band: where the recording does not carry the year, or where its timestamps are not calendar-locked at all, the implementation SHALL refuse the request rather than emit a fabricated date.

**Rationale**: An IRIG-B timestamp carries day-of-year, hour, minute, second and microsecond — but no year and no timezone. Analysts feeding decoded recordings into time-series tooling, or correlating them against systems that speak calendar dates, need an ISO-8601 rendering; an analyst reading a single recording is better served by the day-of-year form the DDC tool emits. Neither is the right answer for everyone, so the rendering becomes a choice rather than a constant. Keeping day-of-year as the default is what preserves L1-OUT-001: byte compatibility is a property of the default path, not of a flag combination the operator has to remember. The refusal clause exists because the failure mode is silent and consequential in the opposite direction from the usual one — a fabricated year produces a date that is well-formed, authoritative-looking, and wrong, which survives into incident reports in a way an empty cell never would. That is the same reasoning L1-DLT-001 applies to an uncalibrated `DELTA`, and it is applied here for the same reason.

**Verification Method**: Test (T)

---

## L1-DLT: DELTA inter-arrival tracking

### L1-DLT-001

**Statement**: Each implementation SHALL compute per-RT-and-MSG inter-arrival time (`DELTA`) in seconds when the source timestamp has a known microsecond basis. When no microsecond basis is available, `DELTA` SHALL be empty.

**Rationale**: Analysts use `DELTA` to detect bus-scheduling anomalies and missed schedule slots. The semantics of "empty" matter as much as the semantics of "populated": a number with no meaningful units is more dangerous than no number at all. Standard free-running timestamps have no calibrated tick rate and therefore must surface as empty rather than as a misleading floating-point value.

**Verification Method**: Test (T)

---

## L1-CLI: CLI capability surface

### L1-CLI-001

**Statement**: Each implementation SHALL provide CLI capabilities for decoding, message counting, configuration, filtering, timestamp-format selection, logging-level control, and diagnostic dump output. CLI syntax MAY differ between implementations.

**Rationale**: Operators use both implementations interchangeably depending on platform (Rust for native compiled-binary deployments, Python for cross-platform development and analysis). The capabilities they need must be available in both — the exact subcommand or flag spelling is allowed to vary so each CLI can follow idiomatic conventions for its language ecosystem.

**Verification Method**: Test (T), Inspection (I)

### L1-CLI-002

**Statement**: Each implementation SHALL support message exclusion filtering by transaction type, RT address, bus, and subaddress.

**Rationale**: Recordings often contain millions of records; exclusion filters allow analysts to narrow output to the transactions relevant to a specific investigation without re-decoding. Filter axes match the four most-discriminating fields in the MIL-STD-1553 transaction header.

**Verification Method**: Test (T)

---

## L1-LOG: Diagnostic logging

### L1-LOG-001

**Statement**: Each implementation SHALL provide configurable diagnostic logging and SHALL accept the level names `DEBUG`, `INFO`, `WARNING` (alias `WARN`), `ERROR`, `CRITICAL`, and `OFF`. `CRITICAL` and `OFF` SHALL suppress all output (the decoder emits no `CRITICAL`-level messages).

**Rationale**: Operators routinely escalate from default `INFO` to `DEBUG` when investigating an anomalous file and de-escalate to `ERROR` for batch runs where stderr is being captured; `OFF` silences logging entirely for fully-quiet pipelines. The accepted level vocabulary follows Python's `logging` module (hence the `WARN`/`WARNING` alias) to avoid an implementation-specific glossary.

**Verification Method**: Test (T)

---

## L1-MODE: Strict and lenient handling

### L1-MODE-001

**Statement**: Each implementation SHALL support a strict mode that surfaces invalid records as errors and a lenient mode that skips them with a warning and continues decoding.

**Rationale**: Strict mode is used in CI and conformance contexts where any invalid record indicates a bug or a corrupt input that must be triaged. Lenient mode is used in field-deployed analysis pipelines where some recording-card-driven anomalies are routine and the operator wants the maximum number of valid records extracted regardless of localized corruption.

**Verification Method**: Test (T)

---

## L1-SYN: Synchronization and sync recovery

### L1-SYN-001

**Statement**: Each implementation SHALL detect the proprietary file header, SHALL validate every record before decoding, and SHALL recover from word-aligned mid-file sync loss.

**Rationale**: The MIE format places a variable-length proprietary header before the first record; the decoder must skip past it without prior knowledge of the header size. Mid-file sync loss can occur when the recording medium experienced a transient write error or when the card was reset mid-recording; the decoder must walk forward to the next valid record boundary rather than abandon the rest of the file.

**Verification Method**: Test (T)

### L1-SYN-002

**Statement**: Sync recovery scanning SHALL be bounded. Per-recovery scan distance SHALL NOT exceed `MAX_SCAN_BYTES` (64 KB). Across a full decode invocation, cumulative recovery scan distance SHALL NOT exceed the file size — recovery scans SHALL NOT re-traverse already-scanned bytes.

**Rationale**: Without bounded scanning, a pathological input could force the decoder into a quadratic or unbounded loop searching for a sync point that does not exist. Per-recovery and cumulative bounds together guarantee `O(file_size)` worst-case scan work even on heavily corrupted inputs.

**Verification Method**: Test (T), Analysis (A)

---

## L1-ERR: DDC error records and SPURIOUS_DATA

### L1-ERR-001

**Statement**: Each implementation SHALL decode DDC error records (Type Word bit 14 set) and SPURIOUS_DATA records (message type `0x20`), and SHALL support both inline-column (the default) and separate-file error output modes.

**Rationale**: Error records and their SPURIOUS_DATA continuations are first-class artifacts of the DDC card's behavior — they encode bus errors that the analyst needs to see. Separate output keeps the clean message stream uncluttered; inline output keeps everything in one CSV for diff against vendor output. Both modes must be available because both are used in practice.

**Verification Method**: Test (T)

---

## L1-CFG: Configuration

### L1-CFG-001

**Statement**: Each implementation SHALL support TOML configuration files. Configuration precedence SHALL be: CLI argument values override configuration-file values override built-in defaults.

**Rationale**: Operators routinely have site-wide or recording-campaign-wide configuration that they want to share across runs; the TOML file provides that. Per-invocation overrides via CLI are needed when a specific run needs a one-off behavior change. The precedence order ensures that CLI overrides always win — a developer or operator typing a flag on the command line expects it to take effect regardless of any persisted configuration.

**Verification Method**: Test (T)

---

## L1-CONF: Cross-implementation conformance

### L1-CONF-001

**Statement**: Shared CSV layout and decoding behavior across the Rust and Python implementations SHALL remain aligned through the cross-implementation conformance suite under `tests/conformance/`.

**Rationale**: With two maintained implementations, drift between them silently breaks the contract that operators can swap one for the other. The conformance suite uses hexadecimal input fixtures and byte-exact CSV oracles, runs both CLIs on each fixture, and requires byte-identical output. CI runs the suite on every push and pull request.

**Verification Method**: Test (T)

---

## L1-EXIT: Exit-code semantics and operational contract

### L1-EXIT-001

**Statement**: Each implementation SHALL provide actionable file, record, configuration, and output error reporting, and SHALL return a non-zero CLI exit code on failure.

**Rationale**: The decoder is run in pipelines and CI; any failure must be distinguishable from success by exit code alone. Error messages must name the offending file, record offset, or configuration key so the operator can act on the failure without re-running with `--debug`.

**Verification Method**: Test (T)

### L1-EXIT-002

**Statement**: A decode invocation that finds no valid records **and is not a recognized empty recording** (per L1-EXIT-010) SHALL exit with code `2` and SHALL NOT create an output file.

**Detection is heuristic, and this requirement is bounded by it.** "Finds no valid records" means the sync heuristics located none — not that the input was proven to be a recording. The converse does not hold: an input that is *not* a recording may still present byte sequences that satisfy those heuristics, in which case the decode succeeds and emits rows. Ordinary English prose does this readily — only ~5% of Type Word values name a valid message type, but a text file of any size offers thousands of candidate offsets, and a chain long enough to satisfy the configured look-ahead occurs by chance. Every Markdown file in this repository decodes to a non-zero record count at the default look-ahead depth of `2`.

Operators who need tighter screening can raise `decode.lookahead_records` / `--lookahead-records` (L2-SYN-026), which costs a genuine recording nothing. It reduces the problem sharply but does not solve it: across this repository's own text files, depth `2` produced 34-163 fabricated records each, depth `4` produced 4-6, and depth `8` rejected most outright — yet a short prose file still decodes at any depth, because a lone plausible record near end-of-file has no successor to contradict it. There is no depth at which a non-recording is guaranteed to be rejected. Content sniffing beyond that — magic numbers, extension checks — is out of scope: the MIE format carries no file-level signature (see `docs/MIE-FORMAT.md`), which is precisely why detection is heuristic in the first place.

**Rationale**: A wrong-format input, or an input with no records that pass validation, is operationally distinct from an input the operator pointed at deliberately. Exit code `2` lets the operator distinguish "you pointed me at the wrong file" from "you pointed me at the right file and there are records in it". Not creating the output file prevents a downstream consumer from being handed an empty CSV that looks legitimately empty. **Exception**: a *valid but empty recording* — one whose record stream opens directly on the DDC end-of-records terminator (a null Type Word) — is a legitimate successful decode, not a wrong-file rejection; it is carved out by L1-EXIT-010 and exits `0` with a header-only CSV. The positive terminator signature is what separates the two: a file that opens on the terminator is a recognized (empty) MIE recording, whereas arbitrary non-MIE bytes are not.

**Verification Method**: Test (T)

### L1-EXIT-003

**Statement**: A decode invocation that recovers from one or more mid-file sync losses SHALL exit with code `0`, SHALL log an INFO-level summary naming the recovery count, and SHALL complete the CSV normally.

**Rationale**: Sync recovery is a routine, expected event on field recordings — exiting non-zero would cause operators to dismiss real errors as "the usual recovery noise". The INFO summary line gives the operator the information they need (how many recoveries, total bytes scanned) without forcing them to enable DEBUG.

**Verification Method**: Test (T)

### L1-EXIT-004

**Statement**: A decode invocation that suffers unrecoverable mid-file sync loss SHALL exit with code `3` by default and SHALL NOT preserve a partial output file. An optional `--allow-partial` CLI flag (and equivalent `decode.allow_partial` configuration key) SHALL downgrade the exit to `0`, SHALL log a WARN-level summary, and SHALL preserve the partial output with a `.partial` suffix appended to the configured destination path.

**Rationale**: Unrecoverable sync loss is a corruption event the operator needs to know about, distinct from any other failure class. Exit `3` is its dedicated code. By default the partial file is unlinked so it cannot be mistaken for a complete result; `--allow-partial` is the explicit opt-in for operators investigating a specific file who want to inspect what was decoded before the failure.

**Verification Method**: Test (T)

### L1-EXIT-005

**Statement**: The decode command SHALL log a one-line summary on exit naming the exit class: `complete`, `partial-recovered`, `partial-unrecoverable`, or `no-records`.

**Rationale**: The exit class is the single most operationally useful piece of information from a decode run. A one-line summary makes it grep-able in pipeline logs without requiring the consumer to map exit codes back to classes.

**Verification Method**: Test (T)

### L1-EXIT-006

**Statement**: The input file SHALL NOT be modified, truncated, or extended during decoding. Behavior under concurrent external modification of the input is implementation-defined and MAY cause process termination (POSIX mmap plus `ftruncate` on the underlying file can produce SIGBUS; Windows file locking generally prevents concurrent writers but mmap does not grow with extensions). This is an operational contract: implementations open the file read-only and rely on the operator for exclusive access while a decode is in progress.

**Rationale**: The decoder uses memory-mapped I/O for performance and constant-memory behavior. Mmap makes the input filesystem state load-bearing during the entire decode; concurrent modification is undefined per the underlying OS, and we surface that contract here rather than pretend we can defend against every concurrent-write scenario.

**Verification Method**: Inspection (I), Analysis (A)

### L1-EXIT-007

**Statement**: A CLI usage error — an unknown, missing, or invalid command-line flag or argument, an invalid flag value, or an invocation with no subcommand — SHALL exit with code `4` and SHALL NOT create an output file. Both implementations SHALL return the same code for the same usage error.

**Rationale**: A usage error is an operator mistake at the command line, operationally distinct from a bad input file (`2`) or a bad configuration file (`5`); the fix is to correct the invocation, not the data or the config. A dedicated code lets a pipeline branch on "the command was wrong". Code `4` is used rather than the argparse / Unix default `2` because `2` is already the no-valid-records class — putting usage errors there would conflate "you typed a bad flag" with "you pointed me at the wrong file".

**Verification Method**: Test (T)

### L1-EXIT-008

**Statement**: A configuration error — a config file that cannot be found, cannot be parsed, or whose values fail validation — SHALL exit with code `5` and SHALL NOT create an output file. Both implementations SHALL return the same code for the same configuration error.

**Rationale**: A bad config file is distinct from a bad command line (`4`) and a bad input recording (`2`): the operator's fix is to edit the TOML. A dedicated code makes that actionable in automation rather than being lumped into a generic failure.

**Verification Method**: Test (T)

### L1-EXIT-009

**Statement**: A multi-file merge invocation whose inputs cannot be ordered on a common absolute timeline — because a file resolves to the Standard timestamp format, leads with a freerun IRIG record, or the input set mixes timestamp formats — SHALL exit with code `6`, SHALL name the offending file and its detected format, and SHALL NOT create an output file. Both implementations SHALL return the same code for the same condition.

**Rationale**: Time-sorted merge is only meaningful when every input shares one calendar-anchored clock (calendar-locked IRIG). Standard free-running counters have no shared epoch, and freerun IRIG carries no calendar meaning, so a merge across such inputs would silently emit a misleadingly "sorted" CSV. A dedicated exit code — distinct from a bad single input (`2`), a usage error (`4`), and a config error (`5`) — lets automation detect "these recordings are not mergeable" specifically, and refusing before any output prevents a wrong-order CSV from reaching a downstream consumer.

**Verification Method**: Test (T)

### L1-EXIT-010

**Statement**: A decode invocation of a **valid but empty recording** — an input whose record stream opens directly on the DDC end-of-records terminator (a null Type Word, `0x0000`), so it contains zero records but is a genuine MIE recording — SHALL exit with code `0`, SHALL write a header-only CSV (the column header row with no data rows), SHALL log a WARN naming the empty capture, and SHALL report a distinct `empty-recording` exit class. Both implementations SHALL behave identically. This is the sole case in which a successful decode produces zero data rows; it is explicitly carved out of L1-EXIT-002.

**Rationale**: Recorders routinely produce files that captured no bus traffic — e.g. an unused MIL-STD-1553 channel — which on disk are just the two-byte terminator. Operators run these files through the same batch pipelines as populated recordings and must not have the batch halt on them: "captured nothing" is a normal, expected outcome, not an error. Emitting a header-only CSV (rather than no file, as L1-EXIT-002 mandates for wrong-file inputs) gives downstream consumers a well-formed, legitimately empty artifact and keeps per-file output invariants intact. The distinct exit class and WARN let an operator still tell "captured nothing" apart from "captured and wrote rows" from logs alone. The guard against masking genuinely wrong inputs is the positive terminator signature: only a file that opens on `0x0000` is treated as empty, so this carve-out cannot be reached by accident. It does **not** follow that every non-MIE input exits `2` — detection is heuristic and a wrong file may decode to rows; see the bound stated under L1-EXIT-002. The `count` subcommand mirrors this — an empty recording prints `0` and exits `0`.

**Verification Method**: Test (T)

---

## L1-ROB: Robustness against arbitrary input

### L1-ROB-001

**Statement**: For arbitrary input bytes within the size limits of `usize`, no implementation SHALL panic, segfault, or enter an unbounded loop. All failures SHALL surface as a documented decoder error variant (`MieError` in Rust, an `Aero1553Error` subclass in Python). Verified by a per-implementation deterministic-PRNG fuzz harness.

**Rationale**: The decoder is run on operator-supplied files, some of which are corrupt by accident and some of which are the output of misconfigured recording sessions. Crashing on bad input gives the operator no useful information and may take down a batch pipeline. Surfacing every failure as a structured error variant lets the operator diagnose and act. The deterministic-PRNG harness ensures regressions can be reproduced.

**Verification Method**: Test (T)

---

## L1-MRG: Multi-file time-sorted merge

### L1-MRG-001

**Statement**: Each implementation SHALL accept more than one input recording in a single `decode` invocation and emit one CSV whose records are in global time order, streaming the merge so that resident memory is bounded by the number of input files (not the total record count). A single input SHALL behave exactly as it does today.

**Rationale**: Analysts routinely need to combine several recordings from one session into a unified timeline. A streaming k-way merge keeps the constant-memory guarantee that lets the tools process multi-GB / 10M+-record inputs, while one ordered CSV is what downstream analysis expects. Leaving the single-input path unchanged guarantees no regression for the common case.

**Verification Method**: Test (T)

### L1-MRG-002

**Statement**: Multi-file merge ordering SHALL be by absolute IRIG time and SHALL require every input to be calendar-locked IRIG. Inputs that cannot be ordered on a common absolute timeline (Standard format, freerun IRIG, or mixed formats across files) SHALL be rejected before any output is written (see L1-EXIT-009). The number of input files SHALL be bounded by a documented fixed limit; exceeding it SHALL be a usage error (L1-EXIT-007).

**Rationale**: A merge is only correct when all inputs share one calendar-anchored clock; absolute IRIG microseconds give an exact, comparable key. Rejecting incompatible sets up front prevents a misleadingly-ordered CSV. Bounding the file count keeps open file handles / mappings within OS limits and makes resource use predictable.

**Verification Method**: Test (T)

### L1-MRG-003

**Statement**: Each implementation SHALL offer, behind an explicit opt-in, collapsing of duplicate messages witnessed by multiple recorders in a multi-file merge: the same bus transaction captured by more than one input within a configurable time tolerance SHALL be emitted once. The opt-in SHALL be off by default, so default output never drops a row. Collapsing SHALL apply only across distinct inputs (recorders); identical messages from a single input SHALL NOT be collapsed.

**Rationale**: Multiple recorders on one 1553 bus witness the same transactions, so an overlapping recorder set inflates the merged message count with rows that each represent one physical bus event. Optionally collapsing those cross-recorder duplicates restores an accurate event count for analysis. Keeping it opt-in and loss-free by default guarantees no analyst is surprised by silently dropped data; restricting collapsing to cross-recorder duplicates preserves genuinely repeated traffic from a single recorder.

**Verification Method**: Test (T)

---

## Non-Requirements

These items are explicitly OUT of scope for Aero1553. They are recorded here so future requests do not get folded into the existing requirements set without separate analysis. Non-requirement IDs use the prefix `NR-<NNN>` and are not subject to the L1/L2/L3 decomposition convention.

### NR-001

**Statement**: The Aero1553 SHALL NOT implement decode functionality for IRIG 106 Chapter 10 / 1553 data.

**Rationale**: MIE files use a DDC proprietary record format that is distinct from IRIG 106 Chapter 10 packet formats — they differ in file framing, timestamp encoding, metadata layout, and the sub-format conventions used to carry MIL-STD-1553 wire data. Conflating the two formats would silently produce wrong output on inputs of either kind. Any future request to add IRIG 106 1553 decode SHALL be treated as a new capability requiring separate requirements, design analysis, architecture review, and approval. It SHALL NOT be added as an extension of the existing Aero1553.

**Disposition**: Future request handled as a new capability per the rationale above.
