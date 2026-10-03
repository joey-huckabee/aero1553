# Aero1553 Dataflow

**What this page is.** The maintainer's view of the `decode` data path: the
stages a record passes through, what each stage **owes** the next (with the
L1/L2/L3 requirement behind each obligation), and what happens when conditions
**combine** — several files, several record kinds, and several options at once.
Every combination listed in [section 5](#5-combination-matrix) was run through
all three implementations and the outcomes compared byte for byte.

**What it is not.** [`DATA-SCENARIOS.md`](DATA-SCENARIOS.md) is the operator's
view, one condition at a time, in plain language.
[`ARCHITECTURE.md`](ARCHITECTURE.md) covers module structure and the sync
strategy. This page is about the guarantees that hold — or fail — *between*
stages.

**Status.** Written 2026-10-02 to analyse review finding M3 (a `0x2000`
continuation losing its pin). Observed behaviour is as of `main` at `721eeb8`.
[Section 6](#6-findings) lists the conflicts and gaps found; each is **open**
until its decision in [section 7](#7-decisions) is taken and the
requirements amended. When that happens, update this page in the same change.
Decisions 1 (M3), 2 (keeping a continuation with its parent), 3 (a
continuation sharing its parent's filter and collapse fate) and 5 (a merge in
which every input fails) are taken and implemented; decision 4 is open.

---

## 1. The pipeline

All three implementations run the same stages in the same order. The Python
CLI *is* the Rust CLI (`python/src/aero1553/cli.py` calls `_native.run_cli`,
which runs the Rust `cli::run_to_code` in process), and each Python library stage is a thin wrapper over the
Rust stage of the same name, so "three implementations" means two independent
pipelines (Rust, C++) and one shared one.

```
 single input:   reader ─────────────────────────────────────────────┐
                                                                     ▼
 2+ inputs:      reader ┐                                         filter ──► order ──► writer
                 reader ├─► merge heap ─► duplicate ─► global ──────►  (L2-FLT)   (L2-WRT-   (inline, or
                 reader ┘   (L2-MRG-002)   collapse     DELTA                      021/022)   split into
                                           (L2-MRG-007) (L2-MRG-005,                          main + _errors,
                                                        global only)                          L2-ERR-008)
```

| Stage | Rust | C++ | Python library |
|---|---|---|---|
| Reader | `rust/src/reader.rs` | `cpp/src/reader.cpp` | `MieFileReader` → Rust reader |
| Merge (heap, collapse, global DELTA) | `rust/src/merge.rs` `MergedRecordIter` | `cpp/src/merge.cpp` `MergedSource` | `merge_readers` → Rust merge |
| Filter | `rust/src/filter.rs` | `cpp/src/filter.cpp` | `apply_filters` → Rust filter |
| Order | `rust/src/order.rs` | `cpp/src/order.cpp` | `order_rows` → Rust `Ordered` |
| Writer | `rust/src/writer.rs` | `cpp/src/writer.cpp` | `write_csv` / `write_csv_split` → Rust writer |
| Wiring | `rust/src/cli.rs` (`decode`) | `cpp/src/cli.cpp` (`decode`) | composed by the caller |

Two properties of this shape matter for everything below:

- **Every per-record fact is decided in the reader, once, and never revisited.**
  The `0x2000` / `0x2001` code, the per-file DELTA, the `ERROR` label and the
  MUX value are all set before the merge, the filter or the order stage sees the
  record. No later stage recomputes them (global DELTA is the one deliberate
  exception, [2.2](#22-merge)).
- **Adjacency is not carried as data.** The relationship "this spurious record
  continues that errored record" exists only as *the order records arrive in*.
  Any stage that removes, inserts or moves records can break it, and nothing
  downstream can tell.

---

## 2. Stage contracts

### 2.1 Reader

**Owes:** a stream of decoded records in **file order** — the order the card
wrote them — each carrying its final classification. Errors surface as a
terminal `Err` item; the stream never yields after one (L3-CPP-013; Rust sets
`done` before yielding).

| Obligation | Requirement |
|---|---|
| Type Word bit 14 marks an errored record; its final word is the DDC Error Word | L2-ERR-001, L2-ERR-002 |
| Type `0x20` is `SPURIOUS_DATA`: no Command Word, no RT/MSG | L2-MSG-001, L2-MSG-003 |
| A spurious record *immediately following* an errored record gets `0x2000`; otherwise `0x2001`. "Immediately following" means the preceding **successfully decoded** record | L2-ERR-005, L2-ERR-006 |
| Per-file DELTA per `(RT, subaddress, direction)`; errored records take part; spurious records get none and update no cursor | L2-RDR-009/010/016/018, L3-RDR-001 |
| Format resolved once per file | L2-DEC-011 |
| Sync loss: strict → terminal error; lenient → recover or stop | L2-SYN-015/016, L1-EXIT-003/004 |

**How the continuation flag actually behaves** (`prev_was_error`, identical in
Rust and C++):

- **Set** only by an errored record that decoded successfully, including a
  lenient unknown-DDC-code record.
- **Cleared** by any spurious record (so only the *first* spurious record after
  an error is `0x2000`), any clean record, every lenient invariant skip, and a
  **successful** sync recovery. L2-ERR-005 names only "classification failure or
  unrecoverable validation error"; the implemented reset set is wider
  ([finding G4](#g-other-findings)).
- The spurious check comes **before** the bit-14 check, so a type-`0x20` record
  with bit 14 set takes the spurious path (gets `0x2000`/`0x2001`) but is
  *labelled* `ERROR`, because the label tests bit 14 first (matrix row S7).
- **A continuation takes its parent's timestamp** (decision 2, L2-ERR-005). The
  flag is held as the errored record's timestamp itself (`prev_error_timestamp`
  in Rust, `prev_error_timestamp_` in C++), so "is this a continuation" and
  "what time does it take" come from one value and cannot disagree. A standalone
  record keeps its own time; `dump` always shows the card's stamp.

### 2.2 Merge

**Owes:** one stream in **global time order** (L1-MRG-001), holding at most one
record per input (L2-MRG-002).

| Obligation | Requirement |
|---|---|
| Heap key `(absolute µs, input index, within-file seq)`; this orders records **leaving the heap**, not CSV rows | L2-MRG-002, L3-*-014/020 |
| All inputs calendar-locked IRIG; Standard or freerun-leading → exit 6 | L2-MRG-003, L1-EXIT-009 |
| Per-input backward step: lenient WARN once per input, **never re-sort**; strict → exit 1 | L2-MRG-006 |
| Opt-in collapse of cross-recorder duplicates by wire content within a window; first in heap order survives; never within one input | L2-MRG-007, L1-MRG-003 |
| Survivor set capped (`max_collapse_survivors`); degrades, never drops output | L2-MRG-008 |
| DELTA scope: `per-file` keeps the reader's value; `global` recomputes over the merged, de-duplicated stream | L2-MRG-005, L3-WRT-004 |
| Per-file failure under `--allow-partial` truncates that file; the merge completes from the rest | L2-MRG-004 |

**What the merge does to adjacency.** The heap knows nothing about it; a record
is ordered by its timestamp. Before decision 2 a continuation kept the card's
later stamp, so every record from another input whose timestamp fell between it
and its parent was emitted between them. A continuation now carries its parent's
timestamp (section 2.1), and a file's records at one timestamp leave the heap
back to back, so nothing can come between them (rows M1, M2, M6). The `0x2000`
code was fixed in the reader and is not revisited.

**What collapse does to adjacency.** Collapse decides per record, with one
exception (decision 3, L2-MRG-007): a `0x2000` continuation is never judged on
its own content. It is collapsed exactly when its parent — the previous record
from the same input — was, and it is never added to the survivor set. So an
error and its continuation are kept or collapsed as one unit (row M4; the
conformance cases `merge-collapse-parent-copy`,
`merge-collapse-continuation-copy`, `merge-collapse-pair-copy`). Before
decision 3, a parent could be suppressed while its continuation survived, or the
reverse. The dedup key still includes `error_word`, which for a standalone
record holds the decoder-assigned `0x2001` ([finding D4](#d-duplicate-collapse)).

### 2.3 Filter

**Owes:** "omit messages matching configured exclusion criteria and yield all
other messages **unchanged**" (L2-FLT-001); OR across exclusions (L2-FLT-002);
a record passes an include set only if it is in **every** active include set
(L3-RS-010, L3-PY-013).

**Records without a Command Word** (L2-FLT-003, decision 3):

| Filter | Standalone `0x2001` | `0x2000` continuation |
|---|---|---|
| `--exclude-rts`, `--exclude-subaddresses` | never matches it → **kept** | follows its parent |
| `--include-rts`, `--include-subaddresses` | cannot satisfy it → **dropped** | follows its parent |
| `--exclude-buses` / `--include-buses` | by its own bus | follows its parent |
| `--exclude-types` naming the parent's type | — | follows its parent (removed) |
| `--exclude-types SPURIOUS_DATA` | removed | removed (its own type) |
| `--include-types` | by its own type | by its own type — `SPURIOUS_DATA` keeps it, any other selection drops it |

The filter remembers one verdict: whether the errored record just before passed
the parent-side filters (RT, subaddress, bus, type exclusion). A continuation
always arrives directly after its parent — the reader emits them back to back,
decision 2 gives them one timestamp so a merge cannot separate them, and
collapse keeps or drops them together — so that verdict is its parent's. A
continuation with no errored record before it is judged on its own.
`FilterConfig::should_exclude` is still the per-record predicate; the pairing
lives in the `Filtered` adapter (`FilteredSource` in C++).

### 2.4 Order

**Owes:** canonical row order (L1-OUT-003) as the **last** stage before the
writer, on both paths (L2-WRT-021), with bounded memory (L2-WRT-022).

| Obligation | Requirement |
|---|---|
| Ascending `TIME_STAMP`; within equal timestamps `(RT, subaddress, direction)`, R before T; stable | L1-OUT-003, L2-WRT-021 |
| Reordering confined to runs of **consecutive** equal timestamps; differing timestamps never reordered | L1-OUT-003, L2-MRG-006 |
| A record with no Command Word keeps its place "immediately following the record [it] followed on input" | L1-OUT-003, L2-WRT-021 |
| Flush the buffered run before passing an `Err` through | L2-WRT-021, L3-RS-016, L3-PY-016 |
| Run length capped by `max_sort_group`; at the cap, arrival order and **one WARN per capped run**; `1` disables reordering | L2-WRT-022, L3-WRT-003 |

**How pinning is implemented** (identical in all three): a run is split into
*chunks* — one record with a Command Word plus the pinned records trailing it —
and the chunks are stable-sorted by their anchor's key. Pins that open a run
form a leading chunk that sorts first.

**The consequence:** a pin is protected only **within one run**. A run ends
where the timestamp changes, so a record without a Command Word whose timestamp
differs from its predecessor's is never in its predecessor's run. It becomes the
leading pin of the *next* run and follows whatever record the earlier run ends
with. Since decision 2 a `0x2000` continuation never does this — it carries its
parent's timestamp — so the case below concerns standalone `0x2001` records.

**The rule that closes that gap** (decision 1, L1-OUT-003 / L2-WRT-021): when a
run is closed by a record with no Command Word at a different timestamp, the
stage checks whether sorting would move the run's last-arrived chunk away from
the end. If it would, the run is emitted in arrival order and a DEBUG line is
logged; if not, the run is sorted as usual (rows S3 and S4). A run closed by a
record that has a Command Word, by end of stream or by an error is always
sorted. The check is made when the closing record arrives, so no extra record is
buffered.

### 2.5 Writer

**Owes:** the vendor-compatible columns (L1-OUT-001), streamed with constant
memory, committed atomically (L2-WRT-015).

| Obligation | Requirement |
|---|---|
| Inline (default): every record in one CSV with `ERROR` / `ERROR_CODE` populated | L2-ERR-011 |
| `--separate-errors`: clean → main; errored **or** spurious → `<stem>_errors<suffix>`, created lazily; each file individually in canonical order | L2-ERR-008, L2-WRT-021 |
| Stdout cannot be split: separate mode falls back to inline with a WARN | L2-ERR-011, L3-RS-009, L3-PY-011 |
| Main committed before errors on every path, including `.partial` | L2-WRT-019 |
| MUX from the file each record was decoded from | L2-WRT-020 |

**Split mode restores adjacency** whenever the record that came between a parent
and its continuation is clean — it goes to the main file and the pair is
adjacent again in `_errors` (rows E1, M8).

---

## 3. The parent → continuation relationship

The one relationship between records that the CSV is expected to show is
"this `0x2000` row continues that errored row". The reader establishes it from
**file adjacency** (L2-ERR-005). Each later stage orders or removes records, and
each could separate the pair while the two carried different timestamps:

| Stage | How it could separate the pair | Now | Row |
|---|---|---|---|
| Order | the parent's equal-timestamp run is sorted; a continuation at a later timestamp starts the next run | **cannot** — the continuation shares its parent's time (decision 2), so it is in the parent's run and the pin keeps it there | S3 |
| Merge heap | another input's record falls between the two timestamps | **cannot** — the pair has one timestamp, and a file's records at one timestamp leave the heap back to back | M1, M2, M6 |
| Duplicate collapse | the parent or the continuation is suppressed on its own | **cannot** — a continuation is collapsed exactly when its parent is (decision 3) | M4 |
| Filter | the parent is removed, the continuation (no RT) is kept | **cannot**, except by request — a continuation follows its parent through RT, subaddress, bus and type-exclusion filters (decision 3); only `--include-types SPURIOUS_DATA` keeps it without its parent, because that asks for spurious rows by type | F1, F5; F4 |
| Writer (split) | does not separate; restores a pair when the record between them is clean | — | E1, M8 |
| Order cap | does not separate; a capped run keeps arrival order | — | C1, C2 |

The CSV promise this supports, stated in `ERROR-CATALOG.md`: the row
immediately above a `0x2000` row **is** the error it continues — in a single
recording and in a multi-file merge, under any filter and with duplicate
collapse — unless `--include-types SPURIOUS_DATA` was asked to show spurious
rows without their errors.

**Why the continuation's own timestamp mattered.** Each spurious record carries
its own timestamp. The one fixture shaped like a real error capture
(`errors-inline`) stamps its continuation 279 µs after the parent; whether real
DDC cards do so is **not yet established**. With its own later stamp, a
continuation could be separated from its parent by a sorted tie (M3) and, in a
merge, by any record another recorder logged in between — the second of which no
ordering can avoid without putting a later time ahead of an earlier one. Giving
the continuation its parent's timestamp (decision 2) removes the gap at its
source, whichever way the hardware behaves.

---

## 4. Requirements in tension

L1-OUT-003 makes three promises about row order:

- **(a)** ascending `TIME_STAMP`, and records at differing timestamps are never
  reordered relative to one another;
- **(b)** equal-timestamp runs in canonical `(RT, MSG)` order;
- **(c)** a record without a Command Word stays immediately after its
  predecessor.

For an input like `clean RT3 @500`, `errored RT1 @500`, `continuation @501`,
the only output satisfying (b) and (c) is `RT1, continuation@501, RT3@500`,
which breaks (a). **No ordering satisfies all three** while the continuation
keeps its own later stamp.

**Decision 1 (M3):** (b) gives way, for that one run only and only when the sort
would actually separate the pair — the run is emitted in arrival order, as the
`max_sort_group` cap already does.

**Decision 2:** a `0x2000` continuation takes its parent's timestamp, so for a
continuation the conflict no longer arises at all: the pair is in one run, (b)
and (c) both hold, and the input above now reads `RT1, continuation, RT3`, all at
@500. Decision 1 still governs the remaining case, a **standalone** `0x2001`
record stamped later than a tie it follows (row S6b).

The alternatives considered for decision 2, recorded so they are not re-derived:

- **2A — define "on input" as the merged stream and document the limit.** No
  code; a merged CSV would separate a pair whenever another recorder logged
  something in between, and say so.
- **2B — a source index on every record**, so the order stage could anchor a pin
  to its own file's predecessor. Fixes only exact-microsecond ties, still fails
  for duplicate recorders, and adds a field to the public record type.
- **2C — let the pair travel as a unit** regardless of time. Breaks (a).
- **2D — an explicit parent-link CSV column.** Robust, but a CSV contract change;
  ruled out.

The cost of decision 2 is one cell: the `TIME_STAMP` of a `0x2000` row can differ
from the vendor CSV, which shows the card's stamp (`VENDOR-CSV-DIFFS.md` §3d).
`dump` still shows it.

---

## 5. Combination matrix

**How to read it.** Each row was decoded by Rust, Python and C++ with
`--no-mux`; "Agree" means identical exit code, main CSV and errors CSV, byte for
byte. Rows show `TIME_STAMP` (microseconds only), `RT`, `MSG`, `ERROR`,
`ERROR_CODE`. All timestamps are 192:15:54:50.000xxx, IRIG, bus A, SA 11
receive. Inputs give each record's **stamped** time; outputs give the time it is
reported at. **Verdict** is against the current requirement text:
**OK**, or **UNSPECIFIED** = no requirement decides it.

### Single file

| Row | Input (file order) | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| S1 | err RT15 @500, cont @500, clean RT3 @500 | — | RT3; err RT15; cont 2000 — all @500 | yes | OK | `tie-spurious-pinned` |
| S2 | err RT15 @500, cont @779, clean RT15 @1000 | — | err @500; cont 2000 **@500**; RT15 @1000 | yes | OK | `errors-inline` |
| S3 | clean RT3 @500, err RT1 @500, cont @501 | — | err RT1; cont 2000; RT3 — all @500 | yes | OK (was separated before decisions 1 and 2) | `tie-pin-later-timestamp` |
| S4 | clean RT3 @500, err RT15 @500, cont @501 | — | RT3; err RT15; cont 2000 — all @500 | yes | OK | `tie-pin-later-timestamp-tail-last` (four-record variant) |
| S5 | err RT15 @500, cont @600, clean RT20 @600, clean RT3 @600 | — | err @500; cont 2000 **@500**; RT3 @600; RT20 @600 | yes | OK | none |
| S6 | clean RT20 @500, spurious @500, clean RT3 @500 | — | RT3; RT20; spurious 2001 | yes | OK (pin follows RT20) | none |
| S6b | clean RT20 @500, clean RT3 @500, spurious @501 | — | RT20 @500; RT3 @500; spurious 2001 @501 (arrival order) | yes | OK — decision 1 | `tie-standalone-later-timestamp` |
| S7 | err RT15 @500, spurious **with bit 14** @600, spurious @700 | — | err @500; **ERROR** 2000 @500; SPURIOUS 2001 @700 | yes | **UNSPECIFIED** — labelled `ERROR`, coded and timed as a continuation | none |
| S8 | err RT15 @500, spurious @600, spurious @700 | — | err @500; SPURIOUS 2000 @500; SPURIOUS 2001 @700 | yes | OK (only the first is a continuation, and only it is re-timed) | C++, Rust and Python reader tests |

### Order-stage cap

| Row | Input | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| C1 | as S3 | `--max-sort-group 1` | RT3; err RT1; cont 2000 — all @500 | yes | OK (arrival order); **3 WARNs for 3 records** | `tie-cap-disabled` (no WARN count) |
| C2 | as S3 | `--max-sort-group 2` | RT3; err RT1; cont 2000 — all @500 | yes | OK (the cap → arrival order) | none |
| C3 | as S1 | `--max-sort-group 2` | err RT15; cont 2000; RT3 | yes | OK | none |
| C4 | five clean records RT 21, 9, 3, 30, 1 @500 | `--max-sort-group 2` | 21, 9, 3, 30, 1 (arrival) with **2 WARNs** | yes | **BREAKS L2-WRT-022** "exactly one WARN per capped run" | `tie-cap-overflow` (no WARN count) |

### Split output

| Row | Input | Options | Main | Errors | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|---|
| E1 | as S3 | `--separate-errors` | RT3 @500 | err RT1; cont 2000 — @500 | yes | OK | none |
| E2 | as S6 | `--separate-errors` | RT3; RT20 | spurious 2001 | yes | OK; a `0x2001`'s pin anchor (RT20) is in the other file — UNSPECIFIED what "following" means across files | none |

### Filters

| Row | Input | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| F1 | as S2 | `--exclude-rts 15` | RT15 @1000 only — error **and** its continuation removed | yes | OK (was an orphaned `0x2000` before decision 3) | `filter-continuation-exclude-rts-parent` |
| F2 | as S2 | `--include-rts 15` | err @500; cont 2000 @500; RT15 @1000 | yes | OK — the continuation is kept with its error (was dropped before decision 3) | `filter-continuation-include-rts-parent` |
| F3 | as S2 | `--exclude-types SPURIOUS_DATA` | err; RT15 | yes | OK | none |
| F4 | as S2 | `--include-types SPURIOUS_DATA` | cont 2000 @500 alone | yes | OK — explicitly requested by type (decision 3) | `filter-continuation-include-types-spurious` |
| F5 | as S1 | `--exclude-rts 15` | RT3 @500 only | yes | OK (was an orphan leading the run before decision 3) | `filter-continuation-exclude-rts-parent` (same rule) |

### Merge

File A = err RT15 stamped @500, cont stamped @510. The other file varies.

| Row | Inputs (in order) | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| M1 | A, B = clean RT3 @505 | — | err @500; cont 2000 @500; RT3 @505 | yes | OK (was separated before decision 2) | `merge-continuation-record-between` |
| M2 | B = clean RT3 @510, A | — | err @500; cont 2000 @500; RT3 @510 | yes | OK (was separated before decision 2) | none |
| M3 | A, B = clean RT3 @510 | — | err @500; cont 2000 @500; RT3 @510 | yes | OK | none |
| M3b | A, B = clean RT3 @500 (tie at the parent) | — | RT3; err RT15; cont 2000 — all @500 | yes | OK (decision 1 alone had separated it in a merge) | `merge-continuation-tie-at-parent` |
| M4 | A, B = duplicate err @500 + cont @510 with a different word | `--collapse-duplicates` | err @500; cont 2000 @500 (A's) | yes | OK — B's continuation collapsed with B's error (decision 3) | `merge-collapse-parent-copy` |
| M5 | A, B = exact duplicate pair | `--collapse-duplicates` | err @500; cont 2000 @500 | yes | OK | golden (hash only) |
| M6 | as M4 | — | A's err; A's cont; B's err; B's cont — all @500 | yes | OK — **both** pairs adjacent | none |
| M7 | as M1 | `--delta-scope global` | as M1; cont DELTA empty | yes | OK | none |
| M8 | as M1 | `--separate-errors` | RT3 @505 | err @500; cont 2000 @500 | yes | OK | none |

### Merge inputs that fail, under `--allow-partial`

Rust, Python and C++ agree on every row (decision 5). A **single** file is the
reference: `--allow-partial` never changes its open or non-MIE failure.

| Row | Inputs (in order) | Result | Pinned by |
|---|---|---|---|
| N1 | missing, missing | exit 1, "MIE file not found", no file | `merge-allow-partial-all-missing` |
| N1b | non-MIE, non-MIE | exit 2, "No valid MIE records", no file | `merge-allow-partial-all-unreadable` |
| N1c | missing, non-MIE | exit 1 — the first input's error | `merge-allow-partial-missing-first` |
| N1d | non-MIE, missing | exit 2 — the first input's error | `merge-allow-partial-unreadable-first` |
| N1e | empty recording, missing | exit 0, header-only `.partial` (the empty recording succeeded) | `merge-allow-partial-empty-and-missing` |
| N1f | missing, good | exit 0, `.partial` with the good file's rows | `merge-allow-partial-missing-and-good` |

Before decision 5, N1 exited 0 with an empty `.partial` in Rust and Python and
2 in C++, and N1b–N1d exited 0 with an empty `.partial` in all three.

---

## 6. Findings

### M. Ordering

- **M3 — a continuation loses its pin across a timestamp change** (row S3).
  **Resolved** by decisions 1 and 2.
- **M3b — L2-WRT-021 gave two locators for a pin**: "re-emitted at its original
  offset within the run" *and* "immediately after whichever record preceded it
  on input". **Resolved**: the requirement and the `order.rs` module doc now
  state only the second.
- **M4 — the cap's WARN is per flush, not per run** (rows C1, C4). L2-WRT-022
  requires exactly one per capped run; `--max-sort-group 1`, documented as
  "off", warns once per record. Records after a cap flush start a fresh buffer
  that is sorted on its own, so a long capped run is a mix of arrival-order and
  sorted segments.

### C. Merge

- **C1 — "on input" was undefined on the merge path** (rows M1, M2).
  **Resolved** by decision 2: a continuation shares its parent's timestamp, so
  file order and merged-stream order agree for the pair.
- **C2 — L2-MRG-005 says the merged stream is "monotonic per key by
  construction"**, which L2-MRG-006 contradicts: a lenient non-monotonic input is
  emitted unsorted. Global DELTA stays non-negative only because the shared
  tracker empties a backward step.

### D. Duplicate collapse

- **D4 — collapse ignored the parent → continuation pair** (row M4).
  **Resolved** by decision 3: a continuation is collapsed exactly when its
  parent is and is never matched on its own, so its decoder-assigned
  `0x2000` no longer matters to the key. The key still includes a standalone
  record's decoder-assigned `0x2001`, which L2-MRG-007 calls "wire content";
  harmless, since every standalone record carries the same value.

### F. Filters

- **F1 — removing a parent left an orphaned `0x2000`** (rows F1, F5).
  **Resolved** by decision 3 (L2-FLT-003). The one remaining way to see a
  continuation without its parent is to request it: `--include-types
  SPURIOUS_DATA` (row F4).
- **F2 — how filters treat records without a Command Word was in no
  requirement.** **Resolved**: L2-FLT-003 states it, for standalone records
  and continuations.

### E. Split output

- **E2 — what "immediately following" means across the two files is
  undefined** (row E2): a standalone spurious record's anchor can be in the
  other file.

### N. Cross-implementation

- **N1 — a merge in which every input fails under `--allow-partial`**
  diverged (C++ exit 2, Rust/Python exit 0 with an empty `.partial`), and the
  non-MIE variants exited 0 in all three. **Resolved** by decision 5.
- **N2 — the final WARN of a partial merge misnames the cause** as
  "unrecoverable sync loss at 0x0 after 0 recovery attempt(s)" when an input
  was missing or unreadable, and the Python library raises
  `MieUnrecoverableSyncLossError` at the end of such a merge. Being fixed
  with decision 5.

### G. Other findings

- **G1 — a spurious record with bit 14 set** is labelled `ERROR` but coded — and
  now timed — as a continuation (row S7). Already on the review's low-severity
  list.
- **G2 — `ERROR-CATALOG.md` promised CSV adjacency unconditionally.**
  **Resolved**: it now states the guarantee as it holds (single files and merges)
  and names the remaining exception (a filter or collapse removing the parent).
- **G3 — `rust/tests/cli.rs::canonical_order_holds_in_both_separate_mode_files`
  never reads the errors file**, and its comment says "all at one instant" while
  its records are at two timestamps.
- **G4 — L2-ERR-005's reset list is narrower than the code's**: a lenient
  invariant skip and a *successful* sync recovery also clear the flag. Neither
  case is tested in Rust.

---

## 7. Decisions

Each needs a choice, then an amendment to the requirement text, then code and
tests in all three implementations.

1. **M3 — which L1-OUT-003 promise gives way** — **decided 2026-10-02**: keep
   that run in **arrival order**, only when the sort would separate the pair,
   for every record without a Command Word (`0x2000` and `0x2001` alike),
   logged at DEBUG. See [section 4](#4-requirements-in-tension). The large
   golden recordings contain this shape — 143 runs across `a-large` and
   `b-large` (147 in their merge), every one closed by a later-stamped
   **standalone** `0x2001` record, since the generator keeps a `0x2000` on its
   parent's timestamp — so their CSV pins were re-pinned with the fix, after
   confirming each changed run was exactly that shape and that Rust, Python and
   C++ all produce the new CSVs. Since decision 2, only standalone records reach
   this rule.
2. **C1 — keeping a continuation with its parent, in a merge too** — **decided
   2026-10-03**: a `0x2000` continuation takes its errored parent's timestamp in
   the reader (L2-ERR-005), so the pair shares one time through every stage.
   `dump` keeps the card's stamp; the CSV cell is a documented vendor difference
   (`VENDOR-CSV-DIFFS.md` §3d). Alternatives considered are in
   [section 4](#4-requirements-in-tension). Golden pins unchanged: the generator
   already gives a continuation its parent's time.
3. **F1 / D4 — orphaned continuations** — **decided 2026-10-03**: a
   continuation shares its errored parent's fate. Filters: the RT, subaddress
   and bus filters (include and exclude) and a type exclusion naming the
   parent's type keep or remove both; type filters judge the continuation on
   its own type, so `--exclude-types SPURIOUS_DATA` removes it and
   `--include-types SPURIOUS_DATA` keeps it (L2-FLT-003). Collapse: a
   continuation is collapsed exactly when its parent is (L2-MRG-007). The
   alternative considered — re-coding an orphaned continuation as `0x2001` —
   would have made filtering change a field and still left the row in place.
   One visible change: `--include-rts` / `--include-subaddresses` /
   `--include-buses` now keep the continuations of the errors they keep. Golden
   pins unchanged; the PYTHON-GUIDE `include_rts={15}` example's count rose by
   exactly the 23 continuations of RT 15's bus-A errors (489 → 512).
4. **M4 — the cap**: what happens to records after a cap flush (stay in arrival
   order for the rest of the run?), and one WARN per run regardless of cap.
5. **N1 — every merge input failing under `--allow-partial`** — **decided
   2026-10-03**: `--allow-partial` keeps what could be decoded; when every
   input fails at open or priming there is nothing to keep, so the run fails
   exactly as without the flag — with the first failing input's own error and
   exit code, and no output file (L2-MRG-004). A valid empty recording counts
   as a success. The alternative considered — always exit 2 with one message —
   would report a mistyped path as "no records".
6. **Documentation** follows the decisions. Done for decisions 1 and 2
   (`ERROR-CATALOG.md`, `MIE-FORMAT.md` §7.3 and §9, `DATA-SCENARIOS.md` §6 and
   §9, `VENDOR-CSV-DIFFS.md` §3d); decisions 3–5 will need their own.

---

## 8. Coverage gaps

No conformance case combines a `0x2000` continuation with the sort-group cap
or global DELTA scope. Filters and collapse are pinned case by case for
decision 3: twelve `filter-continuation-*` cases (one per filter, on
`filter-continuation.hex`, whose third record differs from the parent in
every filtered field) and three `merge-collapse-*` cases (parent copied,
continuation copied, whole pair copied). Now pinned: a later-stamped
continuation next to a tie (`tie-pin-later-timestamp`,
`tie-pin-later-timestamp-tail-last`), a later-stamped standalone record
(`tie-standalone-later-timestamp`), and a continuation in a merge with another
recorder's record between its stamps or tied with its parent
(`merge-continuation-record-between`, `merge-continuation-tie-at-parent`).
Every row above marked "none" in its **Pinned by** column should become a
conformance case once its expected outcome is decided; the rows marked OK can be
added before then, since they pin current, intended behaviour.

The fixtures used for the matrix are built from the encodings in
`tests/conformance/inputs/tie-spurious-pinned.hex` — the same Type Words,
timestamp words and payload — varying only the microsecond word and the RT
field of the Command Word.
