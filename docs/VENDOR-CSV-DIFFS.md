# Aero1553 — Vendor CSV Alignment & Diffs

Documented column-by-column alignment between Aero1553's CSV output and DDC's vendor-generated CSV. Read this when:

- You're validating that Aero1553 produces the same data your existing pipelines expect from the vendor tool.
- You ran `diff` against vendor output and found a mismatch.
- You're integrating Aero1553 into a system that previously consumed vendor CSV.

The short version: by spec (`L1-OUT-001`) Aero1553's first **44 columns are the DDC vendor layout**, name-for-name and index-for-index, with two decoder-added columns (`ERROR`, `ERROR_CODE`) appended after them. The cross-implementation conformance suite asserts byte-identical CSV between the Rust and Python implementations; that suite's oracles are derived from validated vendor output. In practice, except for the documented exceptions below, a single `diff` should produce zero lines of difference between Aero1553 output and a vendor CSV of the same recording.

---

## 1. Quick verdict

| Category | Status |
|----------|--------|
| Column names | **Match** for all 44 vendor columns; we add 2 more (see §2) |
| Column order | **Match** — vendor columns occupy indices 1–44 exactly; `ERROR` / `ERROR_CODE` are appended at 45–46 |
| Cell formatting (hex width, casing, decimal precision) | **Match** |
| Line endings | **Match** (both produce LF; see §4) |
| Per-row data content for clean records | **Match** |
| Per-row data content for errored / SPURIOUS records | **Match**, except the `TIME_STAMP` of a `0x2000` continuation row (next row) |
| `TIME_STAMP` of a SPURIOUS continuation (`ERROR_CODE` `2000`) | **Deliberate difference** — we report its errored parent's time; the vendor reports the time the card stamped on it, which can be later (see §3d) |
| Placeholder columns `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP` | **Empty** (see §3) |
| `MUX` column | **Populated from the file name by default** (L2-WRT-020); empty with `--no-mux` for a vendor-exact diff (see §3) |
| IRIG `TIME_STAMP` day-of-year field | **Firmware-dependent discrepancy** on some DDC card models (see §5) |
| `TIME_STAMP` **rendering** | **Match** at the default `--output-time-format doy`; `iso` and `dom` diverge deliberately (see §3c) |
| `DELTA` on a **multi-file** decode | **Match** by default (`--delta-scope per-file`, since v2.11.0); `--delta-scope global` diverges deliberately (see §3b) |
| **Row order** for records sharing one `TIME_STAMP` | **May differ** — we sort ties by `RT` then `MSG` (L1-OUT-003); the vendor writes capture order. Restore with `--max-sort-group 1` (see §3a) |

If you find a divergence outside the documented exceptions, **it is a bug** in Aero1553. See §7.

---

## 2. The CSV columns

A decoded CSV is **two blocks**: the DDC vendor layout, then the columns
Aero1553 adds on top of it.

**Block 1 — the vendor layout, columns 1–44.** These are the columns the DDC tool
itself emits, in its order:

```
TIME_STAMP, RT, MSG, WD01, WD02, ..., WD32, STAT, CMD, MUX, TERM_NAME, BUS, DELTA, IM_GAP, RCV_GAP, XMT_GAP
```

That's 1 + 1 + 1 + 32 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 = **44 columns**.
Reordering or renaming any of them breaks the L1-OUT-001 compatibility contract.

**Block 2 — decoder additions, columns 45–46.** Appended after the vendor block:

```
ERROR, ERROR_CODE
```

`ERROR` and `ERROR_CODE` have **no vendor counterpart** — the DDC tool does not
report bus errors as CSV fields at all. Surfacing them is an Aero1553 feature
(L2-ERR-002), so they live at the tail where they cannot disturb vendor column
indices. Any column added in a future release goes here too, never inside block 1.

> **Changed in v2.10.0.** Through v2.9.0 these two columns sat *inside* the vendor
> block, between `DELTA` and `IM_GAP`. That silently pushed `IM_GAP` / `RCV_GAP` /
> `XMT_GAP` two positions to the right of their vendor indices, so any positional
> comparison (`awk '{print $42}'`, a column slice, a fixed-width import) past
> `DELTA` was comparing the wrong fields against vendor output — while every
> column *name* still matched, which is why it went unnoticed. If you have
> scripts written against the pre-v2.10.0 column numbers, see §8.

### Cells that match exactly

The following vendor-block columns produce byte-identical content between
Aero1553 and the vendor tool for any clean (non-errored, non-spurious) record:

| Column | Format | Notes |
|--------|--------|-------|
| `TIME_STAMP` | `DAY:HH:MM:SS.uuuuuu` (IRIG) or `0xNNNNNNNN` (Standard) | **At the default `--output-time-format doy`.** Day-of-year on IRIG has a known firmware discrepancy — see §5; the `iso` / `dom` renderings are a deliberate departure — see §3c. Otherwise identical. |
| `RT` | Integer 0–31, no padding | Empty for SPURIOUS_DATA. |
| `MSG` | `<subaddress><T\|R>` (e.g. `11R`, `22T`) | Empty for SPURIOUS_DATA. |
| `WD01` … `WD32` | 4-character uppercase hex, no `0x` prefix | Unused trailing columns are **empty cells** (not `0000`). |
| `STAT` | 4-character uppercase hex | Empty when not present (some Mode Code formats). |
| `CMD` | 4-character uppercase hex | Empty for SPURIOUS_DATA. |
| `BUS` | Single character `A` or `B` | |
| `DELTA` | `0.000000` (6 decimals) or empty | Empty for SPURIOUS_DATA, uncalibrated Standard-timestamp records (no tick rate configured — supply `standard_tick_rate_hz` to populate it, L2-DEC-017), and non-monotonic timestamps. See `docs/L2-REQ.md` L2-RDR-016 through L2-RDR-019 for the per-case rule. On a **multi-file** decode see §3b — the default scope matches vendor, but `--delta-scope global` deliberately does not. |

### Decoder-added columns (no vendor equivalent)

These have nothing to compare against — a vendor CSV has no such columns, so they
are **expected extra fields**, not divergences:

| Column | Format | Notes |
|--------|--------|-------|
| `ERROR` | `ERROR`, `SPURIOUS`, or empty | Empty in clean rows. Populated in the default inline mode; with `--separate-errors` the errored rows live in the sibling `_errors.csv` instead. |
| `ERROR_CODE` | 4-character uppercase hex code | Empty in clean rows. See `docs/ERROR-CATALOG.md` §6–7 for the full code reference (`0x01xx` DDC, `0x20xx` decoder-assigned). |

---

## 3. Placeholder columns (`MUX`, `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`)

Five columns sit between the meaningful payload columns and the trailing diagnostic columns. Four are emitted as empty cells, preserved by spec (L2-WRT-013). **`MUX` is the exception: by default Aero1553 populates it from the input file name (L2-WRT-020)** — so default output is *not* byte-for-byte identical to the vendor CSV when the input name carries that field.

| Column | What the vendor uses it for | Aero1553 |
|--------|---------------------------|----------------|
| `MUX` | Multiplexer channel identifier on multi-channel DDC cards. | **Populated from a field of the input file name by default** (L2-WRT-020); empty with `--no-mux` / `[mux] enabled = false`. |
| `TERM_NAME` | Operator-assigned symbolic name for the RT (loaded from a side-channel config file in the vendor tool). | Empty. |
| `IM_GAP` | Inter-message gap (microseconds since the previous transaction on either bus). | Empty. |
| `RCV_GAP` | Receive gap (between command and data on a receive transaction). | Empty. |
| `XMT_GAP` | Transmit gap (between command and status on a transmit transaction). | Empty. |

### Why we keep the columns

Removing them would break the L1-OUT-001 byte-compat contract: any downstream tool that consumes the vendor CSV by column index would point at the wrong field after we'd shifted the columns left. The columns are part of the layout per spec; `MUX` now carries a filename-derived value (its position is unchanged) and the other four stay empty.

### Getting vendor-exact output (`--no-mux`)

For a byte-for-byte diff against a vendor CSV, disable MUX population: pass **`--no-mux`** (or set `[mux] enabled = false` in your config). Aero1553 then leaves `MUX` empty like the other four placeholders, and the only remaining differences are the genuinely vendor-populated cells (gap timing, `TERM_NAME`, and any `MUX` values the vendor itself wrote). Those remaining differences are expected and documented — not a bug; the contract is "column layout matches."

To make the diff easier to read, filter the comparison to the meaningful columns.
Since v2.10.0 the vendor columns share indices with the vendor CSV, so the **same**
field list works on both files:

```bash
# Compare only the vendor columns we populate. Same indices on both sides.
FIELDS='$1, $2, $3, $36, $37, $40, $41'
awk -F, "{print $FIELDS}" OFS=, vendor.csv > vendor-cmp.csv
awk -F, "{print $FIELDS}" OFS=, mie.csv    > mie-cmp.csv
diff vendor-cmp.csv mie-cmp.csv
```

(Columns 1–3 are `TIME_STAMP`, `RT`, `MSG`; 36–37 are `STAT`, `CMD`; 40–41 are
`BUS`, `DELTA`. Data word columns 4–35 are also typically worth including — adjust
as fits your validation needs. 38 is `MUX` and 39 is `TERM_NAME`; 42–44 are the
`IM_GAP` / `RCV_GAP` / `XMT_GAP` placeholders. The full 1-based index map is in
[`EXAMPLES.md`](EXAMPLES.md) §12.)

Because `ERROR` / `ERROR_CODE` are now at 45–46, a simpler approach also works —
truncate our output to the vendor block and compare whole rows:

```bash
cut -d, -f1-44 mie.csv > mie-vendor-block.csv
diff vendor.csv mie-vendor-block.csv
```

That was not possible before v2.10.0, when the two decoder columns were embedded
mid-row.

---

## 3a. Row order (`--max-sort-group 1`)

Since v2.9.0, Aero1553 writes rows in a **canonical order** (L1-OUT-003):
ascending `TIME_STAMP`, then ascending `RT`, then `MSG` (subaddress ascending,
`R` before `T`). The vendor tool writes rows in **capture order** — the order the
DDC card wrote them. For records with distinct timestamps the two agree, because
capture order *is* time order. They can differ only for records that share one
`TIME_STAMP` to the microsecond.

**When that actually happens.** A 1553 bus carries one transaction at a time, so
on a single-bus recording it essentially never does. The realistic case is a
**dual-bus recording**: bus A and bus B transactions are genuinely concurrent and
can land on the same microsecond with different RTs. There, our row order may
differ from the vendor's while every cell still matches.

**For a byte-for-byte diff, disable reordering:**

```bash
aero1553 decode recording.mie -o mie.csv --no-mux --max-sort-group 1
```

`--max-sort-group 1` makes every record its own sort group, so nothing is
reordered and the output is raw capture order — exactly what the vendor writes.
It is silent: the cap's overflow WARN is for a cap that is *reached*, not one
you set to 1 on purpose.
Combine it with `--no-mux` (§3) for a full vendor-exact decode. The equivalent
config keys are `[output] max_sort_group = 1` and `[mux] enabled = false`.

**A row-order-only difference is not a bug.** If a diff shows the same set of
rows in a different order within one timestamp, and re-running with
`--max-sort-group 1` makes the diff clean, that is this documented exception —
not a divergence to report under §7. What *would* be a bug: a row-order
difference that persists with `--max-sort-group 1`, a row-order difference
between records with *different* timestamps, or any cell-content difference.

---

## 3b. `DELTA` on a multi-file decode (`--delta-scope`)

The DDC vendor tool has **no merge feature** — it decodes one recording at a
time, so its `DELTA` is always measured within a single file.

Since v2.11.0 that is also Aero1553's default (`--delta-scope per-file`), so a
merged decode's `DELTA` matches vendor output for every record: each gap is to
the previous same-RT/MSG record from that record's *own* file, which is by
construction the value that file produces decoded alone.

`--delta-scope global` measures across the merged timeline instead. That is a
deliberate divergence, not a bug — it answers "how long since *any* recorder last
saw this key" rather than "how long since *this* recorder did". If you are diffing
against vendor CSV, leave the scope at its default.

> **Before v2.11.0** merged `DELTA` was always global, so any multi-file decode
> diffed against a per-file vendor CSV showed differences on every RT/MSG key
> present in more than one input. Keys unique to one file were unaffected.

A single-input decode is identical under either scope, so this section does not
apply to the ordinary one-file vendor comparison.

---

## 3c. `TIME_STAMP` rendering (`--output-time-format`)

Since v3.0.0 the `TIME_STAMP` column has a selectable rendering (`L2-WRT-025`).
**The default is `doy`, the DDC vendor rendering, so a decode that selects
nothing is unaffected by this section** — byte compatibility is deliberately a
property of the plain invocation rather than of a flag combination an operator
has to remember (`L1-OUT-004`).

| `--output-time-format` | Column 1 | Vendor-comparable? |
|---|---|---|
| `doy` (default) | `192:15:54:50.456225` | **Yes** — byte-identical |
| `iso` | `2026-07-11T15:54:50.456225Z` | No, by design |
| `dom` | `11:15:54:50.456225` | No, by design |

`iso` and `dom` are **the fifth documented exception** to the §1 byte-compat
contract, and the only one an operator turns on deliberately. They exist because
downstream tooling and cross-system correlation want calendar dates, which the
day-of-year form cannot give; the DDC tool has no equivalent, so there is
nothing to be compatible with. Selecting one is an explicit statement that this
run is not for a vendor diff.

Two practical consequences:

- **A site config can silently void a diff.** `output_time_format` is an
  ordinary configuration key, so a `--config` file that sets `iso` changes
  column 1 on every row without appearing on the command line. §6 step 1 says
  what to do about it.
- **`--year` becomes load-bearing.** Under `doy` the day-of-year field is
  printed verbatim and the known firmware discrepancy of §5 shows up as a
  suspicious day number. Under `iso` or `dom` the same field is resolved into a
  calendar date that reads as a fact. That is why the §5 advisory is emitted at
  **WARNING** rather than `INFO` whenever a calendar rendering is active
  (`L2-LOG-002`) — see §5a.

---

## 3d. `TIME_STAMP` of a SPURIOUS continuation (`ERROR_CODE` `2000`)

When the card detects a bus error it writes the errored record, then a
`SPURIOUS_DATA` record holding the transaction's leftover words, with **its own
timestamp** — which can be later than the error's. Aero1553 reports that
continuation row (`ERROR` = `SPURIOUS`, `ERROR_CODE` = `2000`) at its **errored
parent's** time instead (L2-ERR-005). The vendor tool shows the card's stamp.

So on a continuation row, and only there, the `TIME_STAMP` cell can differ. The
row's position and every other cell match. Standalone spurious rows
(`ERROR_CODE` = `2001`) keep their own time and match.

**Why we differ.** The `2000` code means "this row continues the error directly
above it", and every stage orders rows by time. With the card's later stamp, a
tie sorted by `RT`/`MSG` — or, in a multi-file merge, any record another
recorder logged in between — could land between the two rows and leave the code
pointing at the wrong record. Sharing the parent's time keeps them together in
every case, and the leftover words belong to the parent's transaction anyway.

**Seeing the card's own stamp.** `aero1553 dump` reports the bytes on disk,
including the continuation record's recorded timestamp.

**In a diff,** treat a `TIME_STAMP` mismatch on a row whose `ERROR_CODE` is
`2000` as this documented difference. There is no flag to turn it off.

---

## 4. Line endings

Both implementations emit LF (`\n`) line endings on every platform, including Windows. The vendor tool's output may use CRLF on Windows builds. If your `diff` flags every line as different, normalize line endings first:

```bash
# On Linux / WSL:
dos2unix vendor.csv

# Or in pure POSIX:
tr -d '\r' < vendor.csv > vendor-lf.csv
diff vendor-lf.csv mie.csv
```

A `git diff --ignore-cr-at-eol vendor.csv mie.csv` also handles the trailing-CR case cleanly.

The Aero1553 LF-only choice is pinned by L2-WRT-012 and is intentional — keeps CSV byte-exact across host operating systems so the same recording produces the same hash from any decode host.

---

## 5. IRIG day-of-year field — firmware-dependent discrepancy

`docs/MIE-FORMAT.md` §5.1 documents this in the IRIG Upper Word section. Summary:

> Empirical testing has shown a discrepancy between the binary-decoded value and vendor CSV output for the day-of-year field on some DDC card models. The bit extraction is correct per the DDC specification, but the card firmware may use a different encoding (possibly BCD or a different field width).

To make this limitation visible at decode time, the decoder emits a **one-time `INFO` advisory** per decode, the first time it decodes a calendar-locked (non-freerun) IRIG record, pointing back to this section. Freerun recordings (where day-of-year carries no calendar meaning) do not trigger it.

### 5a. Why the advisory is `INFO` (and when it is not), and how to turn it off

The advisory is a **standing disclaimer about card firmware, not a finding about your file.** The decoder never compares the decoded day-of-year against anything — it cannot, because the card model and firmware revision are not recorded in the `.mie` file. So it fires on every calendar-locked IRIG recording from every card, affected or not, and its text is identical every time.

That is why it sits at `INFO` rather than `WARNING`. `WARNING` is reserved for things the decoder *observed about the input in front of it* — a sync recovery, a non-monotonic `DELTA` step, a hit sort-group cap, a skipped invalid record. At `WARNING` the advisory appeared in the default output of every single decode and competed with those. At `INFO` it is silent by default and still one `--log-level INFO` away.

**One exception, since v3.0.0: it is emitted at `WARNING` when a calendar
rendering is active** (`--output-time-format iso` or `dom`, `L2-LOG-002`). The
reasoning above is unchanged — what changes is the consequence if the disclaimer
turns out to apply. Under `doy` a firmware-skewed day is one field an analyst
reads as "day 192", visibly a day number and easy to re-examine. Under a
calendar rendering the same skew is resolved into `2026-07-11`, which looks like
a fact, gets pasted into incident reports, and is correlated against systems with
no way to know it is suspect. Selecting a calendar rendering *is* the operator
stating that this field is load-bearing for them, which is exactly when a
standing caveat about it earns the default level's attention. The opt-out below
still suppresses it at every level and under every rendering.

| What you want | How |
|---|---|
| Default behaviour | Nothing to do — the advisory is not shown at the default `WARNING` level. |
| See it (e.g. handing a decode to someone new to the tool) | `--log-level INFO`. It appears once per decode. |
| Never see it, even at `INFO`/`DEBUG` | `--no-irig-day-advisory`, or `[logging] irig_day_advisory = false` in a config file. |

The dedicated opt-out exists because the level alone cannot express *"we already checked this."* Once you have run the §6 validation workflow against your card model and found the day-of-year column matches vendor output, the advisory is answered permanently for that fleet, and you should still be able to run a verbose troubleshooting decode without it.

**Do not use `--log-level ERROR` to silence it.** That works, but it also discards the sync-loss, sync-recovery, non-monotonic-`DELTA`, sort-cap and invalid-record warnings — the diagnostics that *are* about your recording, and the ones you most want during a decode you are troubleshooting. Prior versions of this document recommended it, before the dedicated switch existed.

This is the only known *unintended* column-content discrepancy -- the departures
in §3a, §3b and §3c are deliberate and operator-selected. If you see a day-of-year mismatch between Aero1553 output and vendor CSV for the same recording:

1. **Confirm both tools are looking at the same source file** (no transfer corruption).
2. **Note the card model and firmware version** that produced the recording.
3. **Open an issue with sample bytes** (a 72-byte canonical record from the file plus the corresponding vendor CSV row).

Hour, minute, second, microsecond, and freerun fields are not affected — they decode correctly across all observed card models. The investigation to reverse-engineer the per-firmware day-of-year encoding is tracked in `docs/ROADMAP.md` ("IRIG day-field decoding across DDC card models", Decode correctness section).

---

## 6. Validating a decode matches vendor output

The end-to-end workflow when you want a hard validation that Aero1553 reproduces vendor output:

1. **Decode the same recording with both tools.** Use the vendor tool's default settings; for Aero1553 use the two vendor-exact flags:

   ```bash
   aero1553 decode flight.mie -o mie.csv --no-mux --max-sort-group 1
   ```

   `--no-mux` leaves the `MUX` column empty like the vendor's other placeholders (§3), and `--max-sort-group 1` disables canonical row ordering so rows stay in capture order (§3a). Without those two flags a clean decode will still show expected differences, and you would be chasing documented behavior.

   There is no third flag to pass: `--output-time-format` defaults to `doy`, the vendor rendering (§3c). But if the value is set in a site config file, **unset it or override it with `--output-time-format doy`** — a configured `iso` or `dom` changes column 1 on every row and turns the whole diff into noise.

   Inline error mode matches the vendor tool's behavior of mixing errored and SPURIOUS records into the main CSV. (Separate-mode comparisons would need you to merge Aero1553's two files first.)

2. **Normalize line endings** if your platforms differ (see §4).

3. **Diff with the vendor-empty columns masked out** if your vendor CSV populates `MUX` / `TERM_NAME` / `IM_GAP` / `RCV_GAP` / `XMT_GAP` and Aero1553 doesn't (see §3).

4. **Expect zero differences** outside the documented exceptions. If you see a divergence:

   - Day-of-year column → known firmware discrepancy (§5).
   - `MUX` column → populated from the file name by default; pass `--no-mux` for a vendor-exact diff (§3).
   - Empty `TERM_NAME` / gap columns on our side → expected (§3).
   - Same rows, different order within one `TIME_STAMP` → canonical row order; pass `--max-sort-group 1` (§3a).
   - Two extra columns at the end of our rows (`ERROR`, `ERROR_CODE`) → expected; they have no vendor counterpart (§2). Compare `cut -d, -f1-44` of our output against the vendor file.
   - `DELTA` differs on a **multi-file** decode → check `--delta-scope`; the default (`per-file`) matches vendor, `global` does not (§3b).
   - `DELTA` differs on a **single-file** decode → the vendor may be following a different rule entirely. `python scripts/diagnose-vendor-delta.py vendor.csv` recomputes the column under a range of candidate rules and reports which one reproduces the vendor's own values; a rule at 100% is the vendor's definition. It reads the vendor CSV only — no MIE file and no decoder run needed. Include its output when reporting (§7).
   - Anything else → bug. See §7.

5. **For automated comparison** in a regression pipeline, Aero1553 ships a cross-implementation conformance suite under `tests/conformance/` that asserts byte-identical CSV between the Rust and Python implementations against checked-in oracles. The oracle generation method (manual validation against vendor output, then committed) is documented in [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) §6.

---

## 7. If you find a divergence outside the documented exceptions

Any column-content mismatch that isn't:

- A `MUX` / `TERM_NAME` / `IM_GAP` / `RCV_GAP` / `XMT_GAP` cell that the vendor populated and we left empty (§3), or
- The presence of the `ERROR` / `ERROR_CODE` columns at indices 45–46, which the vendor CSV does not have at all (§2), or
- A row-order difference *within a single `TIME_STAMP`* that goes away under `--max-sort-group 1` (§3a), or
- A line-ending CR/LF difference (§4), or
- A day-of-year discrepancy on the IRIG `TIME_STAMP` (§5), or
- A `DELTA` difference on a multi-file decode under `--delta-scope global` (§3b), or
- A `TIME_STAMP` rendered as ISO-8601 or day-of-month because
  `--output-time-format` / `[output] output_time_format` selected it (§3c)

…is a violation of the L1-OUT-001 byte-compat contract and a bug in Aero1553. To report it:

1. **Capture the divergent row from both CSVs** (one line each, with the column header for context).
2. **Capture the source binary record.** Run `aero1553 dump <file>.mie --records 1 --offset <byte>` to get the record-aware hex annotation. (For an arbitrary offset rather than the first record, `--raw --offset N --length 256` works.)
3. **Note the card model and firmware version** if known.
4. **If the divergent column is `DELTA`**, also run `python scripts/diagnose-vendor-delta.py vendor.csv` and include the ranked table. It answers the question that actually moves a `DELTA` bug forward — not *whether* the values differ but *what rule the vendor is following instead* — without needing the source recording.
5. **Open an issue** with all of the above. The Aero1553 maintainers will reproduce against the conformance suite, add a fixture if missing, and land a fix.

The conformance suite (`tests/conformance/`) is the regression net — every reported divergence that turns out to be a real bug becomes a permanent fixture so it can't silently regress.

---

## 8. Why this contract matters

The L1-OUT-001 byte-compat commitment is load-bearing for adoption:

- **Existing pipelines** that consume vendor CSV can drop in Aero1553 without changing any downstream parser.
- **Validation campaigns** can diff Aero1553 output against vendor CSV as a sanity check on every new recording.
- **Audits** can show that an alternative decoder produces byte-identical output to a vendor reference.

The contract is enforced at three levels:

1. **L2-WRT-001** pins the column order, including the rule that decoder-added columns are appended *after* the 44-column vendor block and never interleaved within it.
2. **L2-WRT-002 / L2-WRT-003 / L2-WRT-004** pin the per-cell formatting (empty cells for unused fields, 4-char uppercase hex for words, 6-decimal DELTA).
3. **L2-WRT-013** explicitly preserves the vendor-empty columns even though Aero1553 doesn't populate them.

Plus the cross-implementation conformance suite, which asserts the Rust and Python implementations agree on every byte for every fixture. If both implementations drift, the suite fails CI; if only one drifts, the suite fails CI louder.

### Migrating scripts written against pre-v2.10.0 column numbers

Through v2.9.0, `ERROR` and `ERROR_CODE` sat at indices 42–43, pushing the three
gap columns to 44–46. From v2.10.0 the gaps are at 42–44 (their vendor positions)
and the two decoder columns are at 45–46.

| Column | Index ≤ v2.9.0 | Index ≥ v2.10.0 |
|---|---|---|
| `DELTA` | 41 | 41 *(unchanged)* |
| `ERROR` | 42 | **45** |
| `ERROR_CODE` | 43 | **46** |
| `IM_GAP` | 44 | **42** |
| `RCV_GAP` | 45 | **43** |
| `XMT_GAP` | 46 | **44** |

Everything at index ≤ 41 is unaffected, which covers `TIME_STAMP`, `RT`, `MSG`,
all 32 data words, `STAT`, `CMD`, `MUX`, `TERM_NAME`, `BUS`, and `DELTA` — so most
scripts need no change at all. Only code that referenced the six columns above by
number is affected.

The durable fix is to resolve columns **by header name** rather than by index:

```bash
# Resolve ERROR_CODE by name, whatever position it occupies
awk -F, 'NR==1 {for (i=1; i<=NF; i++) if ($i=="ERROR_CODE") c=i; next} $c!="" {print}' mie.csv
```

```python
import csv
with open("mie.csv", newline="") as fh:
    for row in csv.DictReader(fh):      # DictReader is index-independent
        if row["ERROR_CODE"]:
            ...
```

Name-based access is what the decoder's own tests now use, precisely because the
positional variants are what let this bug hide for so long.

---

## 9. See also

- [`L1-REQ.md`](L1-REQ.md) — `L1-OUT-001` byte-compat contract; `L1-CONF-001` cross-impl conformance suite.
- [`L2-REQ.md`](L2-REQ.md) — `L2-WRT-001` through `L2-WRT-013` writer contract; `L2-CONF-001` through `L2-CONF-005` conformance suite specifics.
- [`MIE-FORMAT.md`](MIE-FORMAT.md) — Comprehensive binary format + CSV column reference.
- [`ERROR-CATALOG.md`](ERROR-CATALOG.md) — `ERROR_CODE` column values (DDC `0x01xx` and decoder `0x20xx` families).
- [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) §6 — Adding a new conformance fixture.
- [`USER-GUIDE.md`](USER-GUIDE.md) §7 — Reading the CSV from an operator perspective.
- [`tests/conformance/`](../tests/conformance/) — The cross-impl regression suite.
