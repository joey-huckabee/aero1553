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
[Section 6](#6-findings) lists the conflicts and gaps found; they are
**open** until the decisions in [section 7](#7-decisions-required) are taken
and the requirements amended. When that happens, update this page in the same
change.

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

**What the merge does to adjacency.** The heap knows nothing about it. A
spurious record is ordered by **its own** timestamp. When a continuation's
timestamp is later than its parent's, every record from another input whose
timestamp falls between them is emitted between them (row M1). The `0x2000`
code was fixed in the reader and is not revisited.

**What collapse does to adjacency.** Collapse decides per record. A parent can
be suppressed while its continuation survives, or the reverse (row M4). The
dedup key includes the record's `error_word`, which for a spurious record holds
the **decoder-assigned** `0x2000`/`0x2001` — not wire content
([finding D4](#d-duplicate-collapse)).

### 2.3 Filter

**Owes:** "omit messages matching configured exclusion criteria and yield all
other messages **unchanged**" (L2-FLT-001); OR across exclusions (L2-FLT-002);
a record passes an include set only if it is in **every** active include set
(L3-RS-010, L3-PY-013).

**Records without a Command Word** — defined by tests and code only; **no
requirement states it** ([finding F2](#f-filters)):

| Filter | Effect on a spurious record |
|---|---|
| `--exclude-rts`, `--exclude-subaddresses` | never matches it → **kept** |
| `--include-rts`, `--include-subaddresses` | cannot satisfy it → **dropped** |
| `--exclude-types` / `--include-types`, `--exclude-buses` / `--include-buses` | applied normally (type `0x20`, bus from the Type Word) |

Because filtering runs **after** the reader, removing a parent leaves its
continuation still coded `0x2000` (rows F1, F5).

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
where the timestamp changes, so a continuation whose timestamp differs from its
parent's is never in its parent's run. It becomes the leading pin of the *next*
run and follows whatever record the parent's run happened to end with
(row S3 — finding M3).

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
**file adjacency** (L2-ERR-005); every later stage can break it:

| Stage | How it can separate the pair | Row |
|---|---|---|
| Merge heap | another input's record falls between the two timestamps | M1, M2 |
| Duplicate collapse | the parent or the continuation is suppressed on its own | M4 |
| Filter | the parent is removed, the continuation (no RT) is kept | F1, F5 |
| Order | the parent's equal-timestamp run is sorted and the continuation, at a different timestamp, starts the next run | S3 |
| Writer (split) | does not separate; restores the pair when the interloper is clean | E1, M8 |
| Order cap | does not separate; a capped run keeps arrival order | C1, C2 |

Three different meanings of "adjacent" are in use, and they agree only for one
file, no filter, no collapse, and a parent and continuation sharing a timestamp:

1. **File order** — L2-ERR-005: "the immediately preceding *successfully
   decoded* record".
2. **Stream order at the order stage** — L1-OUT-003 / L2-WRT-021: "the record
   they followed **on input**", where the input to that stage is the merged,
   de-duplicated, filtered stream.
3. **CSV order** — `ERROR-CATALOG.md` ("Adjacency in the CSV is guaranteed",
   "the row immediately above it in the CSV *is* the error it continues").

`DATA-SCENARIOS.md` scopes the promise correctly ("a spurious continuation
**sharing its parent's timestamp**"); no requirement carries that scope.

**Whether this is a corner case depends on the hardware.** Each spurious record
carries its own timestamp. The one fixture shaped like a real error capture
(`errors-inline`) stamps its continuation 279 µs after the parent; the fixture
that tests pinning (`tie-spurious-pinned`) was built with equal timestamps, and
the golden-recording generator always retimes a pair onto one timestamp. Whether
real DDC cards stamp the continuation later is **not yet established**; this
page treats both as real.

---

## 4. Requirements in tension

L1-OUT-003 makes three promises about row order:

- **(a)** ascending `TIME_STAMP`, and records at differing timestamps are never
  reordered relative to one another;
- **(b)** equal-timestamp runs in canonical `(RT, MSG)` order;
- **(c)** a record without a Command Word stays immediately after its
  predecessor.

For an input like row S3 — `clean RT3 @500`, `errored RT1 @500`,
`continuation @501` — the only output satisfying (b) and (c) is
`RT1, continuation@501, RT3@500`, which breaks (a). **No ordering satisfies all
three.** Any resolution chooses which promise gives way for that input; see
[section 7](#7-decisions-required).

---

## 5. Combination matrix

**How to read it.** Each row was decoded by Rust, Python and C++ with
`--no-mux`; "Agree" means identical exit code, main CSV and errors CSV, byte for
byte. Rows show `TIME_STAMP` (microseconds only), `RT`, `MSG`, `ERROR`,
`ERROR_CODE`. All timestamps are 192:15:54:50.000xxx, IRIG, bus A, SA 11
receive. **Verdict** is against the current requirement text:
**OK**, **BREAKS (c)** = a continuation no longer follows its parent,
or **UNSPECIFIED** = no requirement decides it.

### Single file

| Row | Input (file order) | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| S1 | err RT15 @500, cont @500, clean RT3 @500 | — | RT3 @500; err RT15 @500; cont 2000 @500 | yes | OK | `tie-spurious-pinned` |
| S2 | err RT15 @500, cont @779, clean RT15 @1000 | — | err @500; cont 2000 @779; RT15 @1000 | yes | OK | `errors-inline` |
| S3 | clean RT3 @500, err RT1 @500, cont @501 | — | err RT1 @500; **RT3 @500**; cont 2000 @501 | yes | **BREAKS (c)** — M3 | none |
| S4 | clean RT3 @500, err RT15 @500, cont @501 | — | RT3 @500; err RT15 @500; cont 2000 @501 | yes | OK (by luck of the key) | none |
| S5 | err RT15 @500, cont @600, clean RT20 @600, clean RT3 @600 | — | err @500; cont 2000 @600; RT3 @600; RT20 @600 | yes | OK (leading pin keeps its slot) | none |
| S6 | clean RT20 @500, spurious @500, clean RT3 @500 | — | RT3; RT20; spurious 2001 | yes | OK (pin follows RT20) | none |
| S7 | err RT15 @500, spurious **with bit 14** @600, spurious @700 | — | err @500; **ERROR** 2000 @600; SPURIOUS 2001 @700 | yes | **UNSPECIFIED** — labelled `ERROR`, coded as a continuation | none |
| S8 | err RT15 @500, spurious @600, spurious @700 | — | err; SPURIOUS 2000; SPURIOUS 2001 | yes | OK (only the first is a continuation) | C++ unit test only |

### Order-stage cap

| Row | Input | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| C1 | as S3 | `--max-sort-group 1` | RT3; err RT1; cont 2000 | yes | OK (arrival order); **3 WARNs for 3 records** | `tie-cap-disabled` (no WARN count) |
| C2 | as S3 | `--max-sort-group 2` | RT3; err RT1; cont 2000 | yes | OK (the tie hits the cap → arrival order) | none |
| C3 | as S1 | `--max-sort-group 2` | err RT15; cont 2000; RT3 | yes | OK | none |
| C4 | five clean records RT 21, 9, 3, 30, 1 @500 | `--max-sort-group 2` | 21, 9, 3, 30, 1 (arrival) with **2 WARNs** | yes | **BREAKS L2-WRT-022** "exactly one WARN per capped run" | `tie-cap-overflow` (no WARN count) |

### Split output

| Row | Input | Options | Main | Errors | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|---|
| E1 | as S3 | `--separate-errors` | RT3 | err RT1; cont 2000 | yes | OK (split restores the pair) | none |
| E2 | as S6 | `--separate-errors` | RT3; RT20 | spurious 2001 | yes | OK; a `0x2001`'s pin anchor (RT20) is in the other file — UNSPECIFIED what "following" means across files | none |

### Filters

| Row | Input | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| F1 | as S2 | `--exclude-rts 15` | **cont 2000 @779 alone** | yes | **UNSPECIFIED** — orphaned `0x2000` | none (Python golden equality test only) |
| F2 | as S2 | `--include-rts 15` | err; RT15 (continuation dropped) | yes | OK per code; UNSPECIFIED in requirements | none |
| F3 | as S2 | `--exclude-types SPURIOUS_DATA` | err; RT15 | yes | OK | none |
| F4 | as S2 | `--include-types SPURIOUS_DATA` | cont 2000 alone | yes | **UNSPECIFIED** — orphaned `0x2000` | none |
| F5 | as S1 | `--exclude-rts 15` | **cont 2000 @500; RT3 @500** | yes | **UNSPECIFIED** — orphan now *leads* the run | none |

### Merge

File A = err RT15 @500, cont @510. The other file varies.

| Row | Inputs (in order) | Options | Output | Agree | Verdict | Pinned by |
|---|---|---|---|---|---|---|
| M1 | A, B = clean RT3 @505 | — | err @500; **RT3 @505**; cont 2000 @510 | yes | **BREAKS (c)** if "input" means the file; OK if it means the merged stream | none |
| M2 | B = clean RT3 @510, A | — | err @500; **RT3 @510**; cont 2000 @510 | yes | as M1 (B's lower index puts it first in the @510 tie) | none |
| M3 | A, B = clean RT3 @510 | — | err @500; cont 2000 @510; RT3 @510 | yes | OK | none |
| M4 | A, B = duplicate err @500 + cont @510 with a different word | `--collapse-duplicates` | err @500; cont 2000 @510; **cont 2000 @510** (B's, parent collapsed) | yes | **UNSPECIFIED** — B's continuation has no parent of its own in the output | none |
| M5 | A, B = exact duplicate pair | `--collapse-duplicates` | err @500; cont 2000 @510 | yes | OK | golden (hash only) |
| M6 | as M4 | — | both errs @500; both conts @510 | yes | OK (no collapse) | none |
| M7 | as M1 | `--delta-scope global` | as M1; cont DELTA empty | yes | OK | none |
| M8 | as M1 | `--separate-errors` | RT3 @505 | err @500; cont 2000 @510 | yes | OK (split restores the pair) | none |

### Merge with every input unreadable

| Row | Inputs | Options | Rust / Python | C++ | Verdict |
|---|---|---|---|---|---|
| N1 | two missing files | `--allow-partial` | exit 0, header-only `.partial`, WARN "unrecoverable sync loss at 0x0" | exit 2, "no input file could be opened", no file | **DIVERGENT; UNSPECIFIED** — L2-MRG-004 says the merge "complete[s] from the remaining inputs" and does not say what happens when none remain |
| N1b | two files of `0xFF` bytes (open, then fail) | `--allow-partial` | exit 0, header-only `.partial` | exit 0, header-only `.partial` | agree; same open question |

---

## 6. Findings

### M. Ordering

- **M3 — a continuation loses its pin across a timestamp change** (row S3). The
  three L1-OUT-003 promises conflict for this input ([section 4](#4-requirements-in-tension)).
- **M3b — L2-WRT-021 gives two locators for a pin**: "re-emitted at its original
  offset within the run" *and* "immediately after whichever record preceded it
  on input". Once the predecessor moves these differ. The code implements the
  second and calls the first "the original bug"; the requirement text still
  carries both (and so does the `order.rs` module doc).
- **M4 — the cap's WARN is per flush, not per run** (rows C1, C4). L2-WRT-022
  requires exactly one per capped run; `--max-sort-group 1`, documented as
  "off", warns once per record. Records after a cap flush start a fresh buffer
  that is sorted on its own, so a long capped run is a mix of arrival-order and
  sorted segments.

### C. Merge

- **C1 — "on input" is undefined on the merge path** (rows M1, M2). Whether the
  pin is relative to the file or to the merged stream decides whether M1 is a
  defect.
- **C2 — L2-MRG-005 says the merged stream is "monotonic per key by
  construction"**, which L2-MRG-006 contradicts: a lenient non-monotonic input is
  emitted unsorted. Global DELTA stays non-negative only because the shared
  tracker empties a backward step.

### D. Duplicate collapse

- **D4 — collapse ignores the parent → continuation pair** (row M4), and its key
  includes the decoder-assigned `0x2000`/`0x2001`, which L2-MRG-007 calls "wire
  content" but is not on the wire. The same spurious words seen as a
  continuation by one recorder and as standalone by another never collapse.

### F. Filters

- **F1 — removing a parent leaves an orphaned `0x2000`** (rows F1, F4, F5).
  L2-FLT-001's "yield all other messages unchanged" keeps the code as it was.
- **F2 — how filters treat records without a Command Word is in no
  requirement** (section 2.3 table). It is pinned only by unit tests.

### E. Split output

- **E2 — what "immediately following" means across the two files is
  undefined** (row E2): a standalone spurious record's anchor can be in the
  other file.

### N. Cross-implementation

- **N1 — every merge input failing to open under `--allow-partial` diverges**
  (row N1): Rust/Python exit 0 with a header-only `.partial` and a misleading
  "sync loss at 0x0" WARN; C++ exits 2. The requirement does not decide it.

### G. Other findings

- **G1 — a spurious record with bit 14 set** is labelled `ERROR` but coded as a
  continuation (row S7). Already on the review's low-severity list.
- **G2 — `ERROR-CATALOG.md` promises CSV adjacency unconditionally** ("Adjacency
  in the CSV is guaranteed"); rows S3, M1, M4, F1 show it is not.
- **G3 — `rust/tests/cli.rs::canonical_order_holds_in_both_separate_mode_files`
  never reads the errors file**, and its comment says "all at one instant" while
  its records are at two timestamps.
- **G4 — L2-ERR-005's reset list is narrower than the code's**: a lenient
  invariant skip and a *successful* sync recovery also clear the flag. Neither
  case is tested in Rust.

---

## 7. Decisions required

These are not decided here. Each needs a choice, then an amendment to the
requirement text, then code and tests in all three implementations.

1. **M3 — which L1-OUT-003 promise gives way** when a continuation's timestamp
   differs from its parent's and the parent's run is sorted:
   - keep that run in **arrival order** (gives up (b) for that run only, as the
     cap already does);
   - let the continuation **join its parent's run** (gives up (a));
   - **scope the pin** to a shared timestamp in the requirement text (keeps
     current behaviour, gives up (c) whenever timestamps differ).
2. **C1 — what "on input" means in a merge**: the file (then M1 is a defect, and
   fixing it means the merge must keep a pair together, against global time
   order) or the merged stream (then M1 is correct and the requirement says so).
3. **F1 / D4 — orphaned continuations**: leave the `0x2000` code as decoded, or
   re-code a continuation whose parent did not survive the filter or collapse
   (which would make filtering change a field, against L2-FLT-001's
   "unchanged").
4. **M4 — the cap**: what happens to records after a cap flush (stay in arrival
   order for the rest of the run?), and one WARN per run regardless of cap.
5. **N1 — every merge input unreadable under `--allow-partial`**: exit 2 (as
   C++) or a header-only `.partial` and exit 0 (as Rust/Python).
6. **Documentation** follows the decisions: `ERROR-CATALOG.md` (G2),
   `MIE-FORMAT.md` §9, `DATA-SCENARIOS.md` §9, and the `order.rs` module doc.

---

## 8. Coverage gaps

No conformance case combines a `0x2000` continuation with a merge, a filter,
duplicate collapse, the sort-group cap or global DELTA scope, and none has a
continuation whose timestamp differs from its parent's inside a tie. Every row
above marked "none" in its **Pinned by** column should become a conformance
case once its expected outcome is decided; the rows marked OK can be added
before then, since they pin current, intended behaviour.

The fixtures used for the matrix are built from the encodings in
`tests/conformance/inputs/tie-spurious-pinned.hex` — the same Type Words,
timestamp words and payload — varying only the microsecond word and the RT
field of the Command Word.
