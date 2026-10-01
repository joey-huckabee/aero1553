# Aero1553 — User Guide

End-to-end walkthrough for analysts and operators who need to turn a DDC MIE binary recording into CSV. Covers:

- Picking an implementation and installing it.
- Decoding your first file.
- The three CLI subcommands and when to use each.
- The common workflows: stdout piping, error separation, partial decoding, filtering, site-wide config.
- Reading the CSV output.
- Diagnosing failures.

If you're using the Python package from your own code rather than the command line, see [`PYTHON-GUIDE.md`](PYTHON-GUIDE.md). If you're modifying the code, see [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md). For the full TOML schema, see [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md). For every CLI exit code and error class, see [`ERROR-CATALOG.md`](ERROR-CATALOG.md).

---

## 1. What this is

Aero1553 reads proprietary binary recording files produced by Data Device Corporation (DDC) MIL-STD-1553 PCI cards and emits CSV output. The CSV layout is column-compatible with DDC's own recording software, so you can:

- Open the CSV in Excel, pandas, or any tooling that consumes flat tabular data.
- `diff` decoded output against vendor-generated CSV for validation.
- Feed it into a downstream analysis pipeline.

The decoder is shipped as two interoperable implementations — a Rust crate + CLI, and a Python package + CLI. Both produce byte-identical CSV for the same input (verified by a cross-implementation conformance suite). Pick whichever fits your platform.

---

## 2. Pick an implementation

| Implementation | Use when |
|----------------|----------|
| **Rust** | You want a single self-contained native binary with no runtime to install, or the fastest decode throughput. |
| **Python** | You want to drop into an existing Python analysis pipeline, you're on Windows / macOS for ad-hoc work, or you'd rather `pip install` than build from source. |

Both decode in constant memory and handle multi-GB / 10M+-record recordings — the choice is about ecosystem, not file size (see [§10 Performance and large recordings](#10-performance-and-large-recordings)).

CSV output is byte-identical between the two — your choice doesn't change the result.

---

## 3. Install

### Rust binary

If a prebuilt binary is available for your platform, just download and run it. To build from source:

```bash
git clone <repo-url>
cd aero1553/rust
cargo build --release
./target/release/aero1553 --help
```

### Python package

Install from a source checkout:

```bash
pip install -e ./python
aero1553 --help
```

The package compiles a small Rust extension during the install, so a source
install needs a Rust toolchain ([rustup](https://rustup.rs/)) on the machine.

If you prefer [uv](https://docs.astral.sh/uv/):

```bash
uv --directory python sync
uv --directory python run aero1553 --help
```

The Python package supports Python 3.10 through 3.14.

---

## 4. Decode your first file

The minimum command:

```bash
aero1553 decode flight.mie -o flight.csv
```

That's it. The decoder finds the MIE record stream (skipping any proprietary file header), auto-detects whether the recording uses IRIG-B or Standard timestamps, decodes every record into one CSV row, and writes the output atomically (so a crash or kill mid-run leaves no half-written file).

On success the CLI exits 0 with no output to stderr. If you want a one-line summary, add `--log-level INFO`:

```bash
$ aero1553 --log-level INFO decode flight.mie -o flight.csv
INFO  beginning decode of flight.mie
INFO  auto-detected timestamp format: Irig
INFO  decode complete: 14523 messages, 0 sync recoveries, format=Irig
INFO  decode exit class: complete (sync_losses=0)
```

The `decode exit class:` line is always emitted at INFO; it names one of `complete`, `partial-recovered`, `partial-unrecoverable`, `empty-recording`, `no-records`, `merge-incompatible`, or `non-monotonic-input (strict)` so pipeline logs can grep for it.

---

## 5. The three subcommands

### `decode` — primary

Reads an MIE file and writes CSV. The command you'll use most.

```bash
aero1553 decode flight.mie -o flight.csv
aero1553 decode flight.mie > flight.csv       # stdout
aero1553 decode flight.mie --separate-errors -o clean.csv
aero1553 --config site.toml decode flight.mie -o flight.csv
```

### `count` — message count, no CSV output

Counts decodable records without producing CSV. Useful for sanity-checking a file size before a long decode or comparing two recordings.

Both implementations follow a two-channel output contract (L3-RS-008 / L3-PY-010): **stdout** contains only the integer count followed by a newline (so it pipes cleanly), and **stderr** carries a human-readable status line with the input path so an interactive operator still sees context.

```bash
$ aero1553 count flight.mie
14523
# (stderr, always emitted: "counted 14523 messages in flight.mie")

$ n=$(aero1553 count flight.mie); echo "got $n"
got 14523
```

Both CLIs share the `count` subcommand, meet the L1-CLI-001 message-counting capability, and produce identical stdout output.

### `dump` — diagnostic hex dump

Two modes for investigating files the decoder rejects or behaves oddly on:

```bash
# Record-aware: parses each record header + IRIG timestamp + Cmd Word, then hex
aero1553 dump suspect.mie --records 10

# Raw hex: classic `hexdump -C` over any byte range
aero1553 dump suspect.mie --raw --offset 0 --length 256
```

Record-aware mode is the default and what you want most of the time — it annotates each record with its Type Word, timestamp, Command Word, RT/SA/direction, and word count, then dumps the record's bytes. Raw mode is for the cases where validation rejects everything and you want to look at the literal bytes.

---

## 6. Common workflows

### Stream to stdout for pipelining

Omit `-o` to write to stdout. The decoder forces inline-error mode (you can't split stdout into two streams), and a broken pipe (downstream consumer closed) exits 0 with no error.

```bash
aero1553 decode flight.mie | head -100
aero1553 decode flight.mie | awk -F, '$2=="15"'      # only RT 15
```

### Separate vs inline error handling

By default, **errored records** (DDC card detected a bus error) and **SPURIOUS_DATA** records (orphan data fragments) stay in the main CSV alongside clean records, flagged by the `ERROR` / `ERROR_CODE` columns. One file, nothing hidden, and the same layout the vendor tool produces:

```bash
$ aero1553 decode flight.mie -o flight.csv
$ ls
flight.csv                        # every record, errors flagged in-row
```

If you would rather keep the main CSV to clean records only, `--separate-errors` routes the errored and spurious rows to a sibling file named `<output_stem>_errors<output_suffix>`:

```bash
$ aero1553 decode flight.mie --separate-errors -o flight.csv
$ ls
flight.csv flight_errors.csv      # errors file only created if error rows exist
```

In inline mode (the default) the `ERROR` column contains `ERROR` / `SPURIOUS` / empty and `ERROR_CODE` contains the hardware code (`011E`, `0120`, etc.) or the decoder-assigned code (`2000` continuation, `2001` standalone). See `ERROR-CATALOG.md` sections 6 and 7 for the full code reference.

### Recovering data from a corrupt recording

If a recording has unrecoverable mid-file corruption, the default behavior is to exit 3 with no output (so you can't accidentally treat a partial result as complete). To preserve what was decoded before the corruption point:

```bash
aero1553 decode corrupt.mie --allow-partial -o decoded.csv
```

On unrecoverable loss, instead of exit 3 and an unlinked temp file, you get:

- A `decoded.csv.partial` file containing all rows decoded before the loss.
- The main `decoded.csv` is **not** created (the `.partial` suffix is deliberate — downstream consumers shouldn't pick it up automatically).
- The CLI exits 0 with a WARN summary naming the sync-loss count.

Inspect `decoded.csv.partial` to see what was salvageable; investigate the source recording separately.

### Filtering messages

All four filter axes use exclude lists and OR logic — a message is dropped if it matches **any** configured criterion. CLI flags **add to** config-file filters; they don't replace them (L2-CFG-004).

```bash
# Drop all SPURIOUS_DATA records:
aero1553 decode flight.mie --exclude-types SPURIOUS_DATA -o cleaned.csv

# Drop broadcast (RT 31) and the unused RT 0:
aero1553 decode flight.mie --exclude-rts 0,31 -o cleaned.csv

# Only Bus A (exclude Bus B):
aero1553 decode flight.mie --exclude-buses B -o busa.csv

# Drop mode-code subaddresses (SA 0 and SA 31):
aero1553 decode flight.mie --exclude-subaddresses 0,31 -o nomodes.csv

# Combine — drop anything matching ANY criterion:
aero1553 decode flight.mie \
    --exclude-types SPURIOUS_DATA,MODE_COMMAND \
    --exclude-rts 31 \
    -o filtered.csv
```

Type filter accepts both symbolic names (`SPURIOUS_DATA`, `BC_TO_RT`, etc.) and hex codes (`0x20`, `0x02`) interchangeably.

### Calibrating Standard timestamps

Some recordings use the **Standard** timestamp format — a 32-bit free-running counter — instead of IRIG. The counter ticks at a card-dependent rate that is **not stored in the file**, so the decoder cannot turn raw ticks into elapsed seconds on its own. By default, the `DELTA` column is therefore left empty for every Standard record:

```bash
aero1553 decode counter.mie --input-time-format standard -o out.csv
# TIME_STAMP in 0xNNNNNNNN form; DELTA column empty for all rows
```

If you know your card's counter frequency, pass it with `--standard-tick-rate-hz` (in Hz). The decoder then converts ticks to microseconds and fills in `DELTA` just as it would for an IRIG recording:

```bash
# Card runs a 1 MHz counter (1 tick = 1 microsecond):
aero1553 decode counter.mie --input-time-format standard --standard-tick-rate-hz 1000000 -o out.csv
```

With calibration on, two consecutive records of the same RT/MSG that are 16 ticks apart show `DELTA = 0.000016` at 1 MHz; the first occurrence of each RT/MSG key is still `0.000000`. The rate must be greater than 0, and it has no effect on IRIG recordings.

**Finding the rate.** The tick rate comes from the recording card's configuration (often documented in the card datasheet or your acquisition setup), not from the file. If you don't know it, leave the flag off — an empty `DELTA` is the honest answer, and the raw counter value is still shown in `TIME_STAMP`.

The same setting is available in a config file as `decode.standard_tick_rate_hz`:

```toml
[decode]
input_time_format = "standard"
standard_tick_rate_hz = 1000000.0
```

### Rendering timestamps as calendar dates

By default `TIME_STAMP` is the day-of-year form the DDC vendor tool emits:

```bash
aero1553 decode flight.mie -o out.csv
# 192:15:54:50.456225,15,11R,...
```

If a downstream system wants calendar dates, `--output-time-format iso` gives
you ISO-8601:

```bash
aero1553 decode flight.mie -o out.csv --output-time-format iso --year 2026
# 2026-07-11T15:54:50.456225Z,15,11R,...
```

**Why `--year` is required.** An IRIG-B timestamp carries day-of-year, hour,
minute, second and microsecond — but no year. Day 192 is **July 10 in a leap
year** and **July 11 in a common year**, so there is no year-free way to resolve
it. The decoder will not guess: no default it could pick would be right (the
system clock would date a 2019 archive with the year you decoded it), so a
calendar rendering without a year is a usage error naming both ways to supply
one.

`--output-time-format dom` renders the day of month instead —
`11:15:54:50.456225` — keeping the column the same shape as the default. Note
the month is not in the cell, so the value is a day number you match against a
calendar rather than a date the CSV identifies on its own.

**Timezone.** IRIG-B carries none either. The `iso` rendering appends `Z` (UTC)
by default; if your site's IRIG source is disciplined to something else, say so
with `--utc-offset`:

```bash
aero1553 decode flight.mie -o out.csv --output-time-format iso \
    --year 2026 --utc-offset -05:00
# 2026-07-11T15:54:50.456225-05:00,15,11R,...
```

The offset is a **designator only** — it never shifts the time fields.

**When it is refused.** A calendar date is not always available, and the decoder
refuses rather than inventing one (exit 2 in each case):

- a **Standard** recording — a free-running counter has no epoch;
- a **freerun IRIG** recording — the card had no IRIG-B lock, so its fields are
  relative and would render as a perfectly ordinary, meaningless date;
- **day-of-year 366 against a common year** — which fails the whole run, because
  it means the year you supplied is not the year the recording is from.

**In a config file**, so a site sets it once:

```toml
[output]
output_time_format = "iso"
year = 2026
utc_offset = "Z"
```

> **This voids a vendor diff.** `doy` is the only rendering byte-comparable with
> DDC vendor CSV. If you validate against vendor output, keep the default — and
> if a site config sets `iso`, override it with `--output-time-format doy` for
> that run. See [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §3c.

### Site-wide configuration

If you find yourself repeating the same flags across recordings, put them in a TOML file:

```toml
# /etc/aero1553/site.toml
[logging]
level = "INFO"

[decode]
error_mode = "inline"

[filter]
exclude_types = ["SPURIOUS_DATA"]
exclude_rts   = [31]
```

```bash
aero1553 --config /etc/aero1553/site.toml decode flight.mie -o flight.csv
```

CLI arguments still take precedence over config-file values per L2-CFG-003. For the full TOML schema with every key documented, see [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md). Copy [`config/default.toml`](../config/default.toml) as a fully-commented starting point.

### Merging multiple recordings into one timeline

When a session produced several recordings (e.g. one per recorder), `decode`
can merge them into a single CSV in global time order. Give it more than one
input, in any of three ways:

```bash
# 1. Positional paths (ad-hoc):
aero1553 decode flight-1.mie flight-2.mie flight-3.mie -o session.csv

# 2. A manifest file (one path per line; blank lines and #-comments ignored):
aero1553 decode --manifest session-files.txt -o session.csv

# 3. A glob the tool expands itself (works on Windows; * and ? over the
#    filename in one directory — no recursion):
aero1553 decode --glob 'recordings/*.mie' -o session.csv
```

The Python CLI takes the exact same forms (`python -m aero1553 decode …`
or the `aero1553` console script). The three methods are **mutually
exclusive** — pick one. A single input behaves exactly as a normal decode.

> **Match recordings, not everything.** `--glob` matches by filename only, and
> every match is decoded as a recording — so a pattern that also catches a
> non-recording (e.g. `recordings/*` in a directory that holds a `README.txt`)
> fails the run with exit 2. Keep the pattern specific (`recordings/*.mie`). See
> the [`--glob` note in the CLI reference](CLI-REFERENCE.md#input-selection) for
> the full behavior and the `--allow-partial` fallback for mixed directories.

#### How the rows get ordered (it is not arbitrary)

Each input file comes from an independent recorder, and each recorder may log
any given message at a different rate across the timeline. The merge does **not**
pull a row from each file in turn, or interleave them blindly — every row it
writes is the one with the earliest timestamp out of *all* the files at that
moment. It works like this:

1. It opens all the files at once and looks at the **first (earliest) record of
   each** — so it always has the current front record of every recorder in
   front of it simultaneously.
2. It writes out the single record with the **smallest timestamp** among those
   fronts.
3. It then pulls the **next** record from *only the file that record came
   from*, and adds it to the set of fronts being compared.
4. It repeats until every file is used up.

So before any row is written, its timestamp has been compared against the
current front of every other file, and the earliest one wins. A recorder that
goes quiet for a stretch simply doesn't win any rows until its next record's
time comes due; a recorder logging at a high rate contributes many rows in a
row — both end up correctly interleaved by time. This works because each
individual recording is already written in chronological order (bus traffic is
captured as it happens), so the tool only has to merge already-sorted streams,
never re-sort the whole set — which is also why memory stays flat no matter how
many total records there are.

When two records carry the **exact same timestamp** (common at coarse time
resolution), they are ordered by **RT, then MSG** — the same canonical order a
single-file decode uses. See [Row order](#row-order) below. That means merged
output does not depend on the order you happened to list the files in: the same
set of recordings produces the same CSV whichever way you name them. Only a tie
that is *also* equal on RT and MSG falls back to input position, then to the
record's position within its file.

What to expect:

- **Ordering** is by absolute IRIG time, so **every input must be
  calendar-locked IRIG.** If any file is Standard-format, leads with a freerun
  record, or the set mixes formats, the merge refuses before writing anything
  and exits **6**, naming the offending file — it never emits a misleadingly
  "sorted" CSV. Decode such files individually instead.
- **Memory** stays flat regardless of total record count (it holds one record
  per open file), so merging many multi-GB recordings is fine. Up to **256**
  files per invocation; combining input methods or exceeding that is a usage
  error (exit 4).
- **DELTA** is measured **per input file** by default — each record's gap is to
  the previous same-RT/MSG record from its *own* file, so the value matches
  decoding that file alone (and matches DDC vendor output, which has no merge).
  Pass `--delta-scope global` to measure across the merged timeline instead. See
  [Which DELTA do you want?](#which-delta-do-you-want) below.
- **A bad file among many:** by default the batch fails. Add `--allow-partial`
  to skip/truncate the failing file with a warning, finish the merge from the
  rest, and write the combined result as `<output>.partial` (exit 0). Use
  `--strict` to fail on the first invalid record in any file.
- **Same year only:** IRIG carries day-of-year but no year, so a set spanning a
  New-Year boundary cannot be ordered from the timestamp alone.
- **A file that isn't internally time-sorted:** the merge assumes each input is
  in chronological capture order (true for normal recordings). If an input's own
  timestamps step backward — rare, from sync-loss recovery or a day/year
  rollover — the tool detects it and, by default (lenient), prints a one-time
  WARN naming that file and still merges everything (the out-of-order rows sort
  to their key positions; it never re-sorts a whole file). Under `--strict` (or
  `[decode] strict = true`) a backward step inside one file is treated as a
  record error and the merge fails (exit **1**), on the principle that a
  recorder's own clock running backward is corruption you probably want to know
  about before trusting the merge.

Both implementations produce byte-identical merged output.

### Which DELTA do you want?

When you merge several files, "seconds since the previous message on this
RT/MSG" has two reasonable readings, and the decoder can't guess which you mean:

| `--delta-scope` | The gap is measured to… | Use when |
|---|---|---|
| `per-file` *(default)* | the previous same-key record **from that record's own file** | you want each recorder's true cadence, or you're diffing against vendor CSV |
| `global` | the previous same-key record from **any** input | you want the bus-wide inter-arrival across the whole session |

The two differ **only for RT/MSG keys that appear in more than one file.** A key
found in just one file gets the same DELTA either way, so if your recordings
cover disjoint equipment you'll see no difference at all.

Where a key *is* shared, `global` compresses the gap. Two recorders each seeing
a 0.2 s cadence interleave into an apparent 0.1 s:

```bash
# Per-file (default): each file keeps its own 0.2 s cadence
aero1553 decode rec_a.mie rec_b.mie -o merged.csv
#   192:15:54:50.100000 … DELTA=0.000000     ← A's first
#   192:15:54:50.200000 … DELTA=0.000000     ← B's first
#   192:15:54:50.300000 … DELTA=0.200000     ← A, 0.2 s after A's previous
#   192:15:54:50.400000 … DELTA=0.200000     ← B, 0.2 s after B's previous

# Global: gaps measured across the merged timeline
aero1553 decode rec_a.mie rec_b.mie -o merged.csv --delta-scope global
#   192:15:54:50.100000 … DELTA=0.000000
#   192:15:54:50.200000 … DELTA=0.100000     ← 0.1 s after A's record
#   192:15:54:50.300000 … DELTA=0.100000
#   192:15:54:50.400000 … DELTA=0.100000
```

For **fully overlapping** recorders — both capturing the same transactions —
`global` degenerates to alternating `0.000000`, one per duplicate pair, because
each copy is zero microseconds after its twin. Either use the default, or add
`--collapse-duplicates` (next section) so only one copy survives.

> **Changed in v2.11.0:** `per-file` is the new default; through v2.10.0 merged
> DELTA was always global. If you depend on the old numbers, pass
> `--delta-scope global` or set `[merge] delta_scope = "global"`.

With a single input file the flag does nothing — the two scopes are the same
computation when there's only one file to measure within.

### Collapsing duplicate messages across recorders

On a 1553 bus every recorder hears the same wire, so recordings from
**overlapping recorders** capture the same transactions — and a plain merge
emits every copy, inflating the message count. Add `--collapse-duplicates` to
fold each transaction's cross-recorder copies into a single row:

```bash
aero1553 decode recorder-1.mie recorder-2.mie -o session.csv --collapse-duplicates
```

This is **opt-in and loss-free by default** — without the flag, every row is
kept. Two records are "the same message" when their *wire content* matches (the
Type Word, Command/Status Words, error word, and data words — the timestamp,
file name/MUX, and DELTA are ignored) **and** they come from **different input
files** within a timestamp tolerance. Identical traffic from one recorder (real
periodic messages) is never collapsed, and a single-file decode is unaffected.

Recorders rarely timestamp the same event to the exact microsecond, so set the
tolerance to match your recorders' clock sync with `--collapse-window-us N`
(microseconds; default `0` = exact match):

```bash
aero1553 decode --glob 'recorders/*.mie' -o session.csv \
  --collapse-duplicates --collapse-window-us 200
```

The first copy in time order survives; the rest are suppressed and the count is
logged (`merge: collapsed N duplicate message(s)…`). DELTA is recomputed on the
deduped timeline. All three flags below have config-file equivalents in a
`[merge]` section (`collapse_duplicates`, `collapse_window_us`,
`max_collapse_survivors`).

**One caveat, and its knob.** Widening the window widens the *set* the merge has
to hold: every record inside the window is a survivor future records get compared
against. A tolerance of a few hundred microseconds on ordinary traffic holds a
handful; a very wide window on a dense bus — or a corrupt recording whose
timestamps all decode to the same value — puts far more than a handful inside one
window, and a bound expressed in time cannot say how many that is. So the survivor
set has a second, count-based cap: `--max-collapse-survivors N` (default `4096`).

You will usually never see it. If you do, it is one WARN for the whole run:

```
WARN  merge: collapse survivor set reached the 4096 cap; evicting oldest
```

Past that point the oldest survivor is dropped to make room and collapsing
becomes best-effort — a duplicate whose partner has already been evicted is
emitted rather than suppressed. **No row is ever dropped from the output**, and
the exit code is unaffected. If you are deliberately running a very wide window
and want the collapsing to stay exact, raise the cap (`--max-collapse-survivors
65536`); if you are memory-constrained, lower it.

### Labeling output by recorder (MUX from the file name)

If your recordings are named so a field identifies the source or recorder, the
decoder can copy that field into the `MUX` column. For the convention
`full_loadout.draw.data.1553.aa.unused.mie_irig` — where the 5th dot-separated
field (`aa`, `bb`, `cc`, …) is the recorder — the **defaults already do the
right thing**:

```bash
aero1553 decode full_loadout.draw.data.1553.aa.unused.mie_irig -o out.csv
# every row's MUX column is "aa"
```

The default splits the file name on `.` and takes field index `4`. Adjust for a
different scheme with `--mux-delimiter` and `--mux-field` (a negative index
counts from the end), or set them in a config `[mux]` section. **In a merge**,
each row carries the MUX of the file it came from — so a merged CSV of several
recorders is self-labeling:

```bash
aero1553 decode --glob 'flight/*.mie_irig' -o merged.csv
# rows from …aa…  → MUX "aa";  rows from …bb… → MUX "bb";  etc.
```

MUX population is **on by default**. To produce output that matches a DDC vendor
CSV byte-for-byte (empty MUX), turn it off with **`--no-mux`** (or
`[mux] enabled = false`). See [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md).

---

## 7. Reading the CSV

46 columns in two blocks: the **first 44 are the DDC vendor layout**, matching
vendor output name-for-name and position-for-position, followed by two columns
Aero1553 adds. Columns in order:

| # | Column | Contents |
|---|--------|----------|
| 1 | `TIME_STAMP` | By default IRIG: `DAY:HH:MM:SS.uuuuuu` (e.g. `192:15:54:50.456225`); Standard: `0xNNNNNNNN` raw counter. `--output-time-format iso` renders `2026-07-11T15:54:50.456225Z` and `dom` renders `11:15:54:50.456225` instead — both need `--year`, because IRIG carries no year (see [Rendering timestamps as calendar dates](#rendering-timestamps-as-calendar-dates)). |
| 2 | `RT` | Remote Terminal address (0–31), or empty for SPURIOUS_DATA. |
| 3 | `MSG` | `<subaddress><T\|R>` (e.g. `11R` for SA 11 Receive, `22T` for SA 22 Transmit). Empty for SPURIOUS_DATA. |
| 4–35 | `WD01`–`WD32` | Up to 32 data words, 4-character uppercase hex without `0x` prefix. Unused trailing columns are empty (not `0000`). |
| 36 | `STAT` | Status Word, 4-character uppercase hex. Empty when not present (e.g. some Mode Code formats). |
| 37 | `CMD` | Command Word, 4-character uppercase hex. Empty for SPURIOUS_DATA. |
| 38 | `MUX` | Source/recorder label derived from the input **file name** by default (see [Labeling output by recorder](#labeling-output-by-recorder-mux-from-the-file-name)). Empty with `--no-mux`. |
| 39 | `TERM_NAME` | Vendor compatibility column. Always empty; reserved for future per-card metadata. |
| 40 | `BUS` | `A` or `B`. |
| 41 | `DELTA` | Seconds since the previous message on the same RT/MSG key. `0.000000` on first occurrence. Empty when the timestamp basis is unknown (uncalibrated Standard format — see [Calibrating Standard timestamps](#calibrating-standard-timestamps)), the record is SPURIOUS_DATA, or the timestamp is non-monotonic. |
| 42–44 | `IM_GAP`, `RCV_GAP`, `XMT_GAP` | Vendor compatibility columns. Always empty; reserved for future inter-message gap timing. |
| | *— end of the vendor block —* | |
| 45 | `ERROR` | `ERROR`, `SPURIOUS`, or empty. Empty in clean rows of separate-mode CSV. |
| 46 | `ERROR_CODE` | DDC hardware code (`011E`, `0120`, `0136`, `0140`, `0150`) or decoder-assigned code (`2000`, `2001`). Empty in clean rows of separate-mode CSV. |

`ERROR` and `ERROR_CODE` have no vendor counterpart — the DDC tool doesn't report
bus errors as CSV fields at all — so they sit after the vendor block rather than
inside it. That keeps `cut -d, -f1-44` a valid way to recover exactly the vendor
layout, and means any column added in a future release lands at the tail without
shifting the ones you already depend on.

> **Changed in v2.10.0:** these two moved from positions 42/43 to 45/46, and the
> three gap columns moved to their vendor positions 42/43/44. If you have scripts
> that index columns by number, see [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §8.

A typical receive row looks like:

```
192:15:54:50.456225,15,11R,0400,0000,0000,002F,CA22,...,7800,797E,,,A,0.000000,,,,,
```

(The five empty cells after `DELTA` are `IM_GAP`, `RCV_GAP`, `XMT_GAP`, `ERROR`,
and `ERROR_CODE` — a clean record populates none of them.)

(Only *trailing* `WD` columns are empty — the ones past this message's data-word
count. A `0000` data word inside the payload is written as `0000`, not left blank.)

Line endings are LF (`\n`) on every platform — including Windows — so the CSV diffs cleanly between machines (L2-WRT-012).

### Row order

Rows come out in a **canonical order** (L1-OUT-003), the same for both
implementations and for every way of invoking them:

1. `TIME_STAMP`, ascending.
2. `RT`, ascending, among rows sharing a timestamp.
3. `MSG`, among rows sharing a timestamp *and* an RT: subaddress ascending, and
   at the same subaddress **`R` before `T`**.

Subaddress ordering is **numeric**, so `2R` comes before `11R` — not the
string order you would get from sorting the `MSG` text.

```
192:15:54:50.000500,3,2T,...     ← RT 3 first
192:15:54:50.000500,3,11R,...    ← SA 2 before SA 11; R before T
192:15:54:50.000500,3,11T,...
192:15:54:50.000500,21,3R,...    ← then RT 21
192:15:54:50.000900,4,3R,...     ← next timestamp starts a fresh group
```

Three things worth knowing:

- **Only tied rows move.** Records with different timestamps are never reordered
  relative to one another. If a recording's own clock steps backward (rare — see
  the merge notes above), that anomaly stays visible exactly where it happened;
  the decoder does not silently re-sort a whole file.
- **`SPURIOUS_DATA` rows stay put.** They carry no `RT` or `MSG`, so there is
  nothing to sort them on. Each one keeps its place immediately after the record
  it followed — which is what makes an `ERROR_CODE` of `2000` ("continuation of
  the preceding error") readable at all.
- **You can turn it off.** `--max-sort-group 1` (or `[output] max_sort_group = 1`)
  makes every record its own group, so nothing is reordered and you get raw DDC
  capture order. That is the supported way to get byte-for-byte row parity with
  vendor CSV — see [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md).

`max_sort_group` otherwise just caps how many same-timestamp records are held in
memory at once (default `4096`). Real ties are tiny — a 1553 bus runs one
transaction at a time, so ties come from the two concurrent buses or from
overlapping recorders in a merge. The cap only matters for a corrupt recording
whose timestamps all decode to the same value; on hitting it, the decoder writes
that group in arrival order, warns once, and carries on without dropping rows.

For the binary-level field reference (what's in the Type Word, how IRIG packing works, etc.), see [`MIE-FORMAT.md`](MIE-FORMAT.md).

---

## 8. When something goes wrong

The CLI exits with one of seven codes (L1-EXIT-001 through L1-EXIT-010), identical across the Rust and Python implementations:

| Code | Class | Likely cause |
|------|-------|--------------|
| **0** | `complete` / `partial-recovered` | Decoded successfully (possibly after auto-recovery from in-stream corruption). |
| **0** | `complete (broken-pipe)` | stdout consumer closed early. Not an error. |
| **0** | `partial-unrecoverable` (with `--allow-partial`) | Unrecoverable corruption, but the rows decoded before it were preserved as `<output>.partial`. |
| **0** | `empty-recording` | A valid MIE recording that captured **zero** records (its stream is just the `0x0000` terminator). A header-only CSV is written. Distinct from exit 2. |
| **1** | runtime / decode error | Per-record validation failed in strict mode, the input couldn't be opened, or the output sink failed. Read the stderr error line. |
| **2** | `no-records` | The input file isn't an MIE recording at all (wrong file type, single-byte pad). No output file created. |
| **3** | `partial-unrecoverable` | Mid-file sync loss that couldn't be recovered. Re-run with `--allow-partial` to keep what was decoded. |
| **4** | usage error | The command line is wrong — unknown/invalid flag or argument, bad flag value, combined input methods, more than 256 merge inputs, or no subcommand. Run `--help`. |
| **5** | configuration error | The `--config` TOML file can't be found, parsed, or fails validation. Fix the file named in the error. |
| **6** | `merge-incompatible` | A multi-file merge whose inputs can't share an absolute IRIG timeline (a Standard-format, freerun-leading, or mixed-format set). Nothing is written. Decode those inputs individually. |

The `decode exit class:` summary log line names the class explicitly, even when stderr is captured to a pipeline log.

### Common diagnoses

**"No valid records found in flight.mie (scanned first 65536 bytes)"** (exit 2): The file isn't actually MIE, or the MIE records begin past the 64 KB header scan window. Use `aero1553 dump flight.mie --raw --length 256` to see what the file actually starts with.

**"Pathological homogeneous-payload input rejected"** (exit 2): The file is a single-byte pad (e.g. zero-fill, 0x20-fill from a botched recording transfer). Re-export from the source.

**"Unrecoverable mid-file sync loss at offset 0x... after N recovery attempts"** (exit 3): The recording has corruption the decoder can't skip past. Re-run with `--allow-partial` to inspect what was decoded before the loss; investigate the source recording for storage / transmission issues.

**"First record after header detection is truncated"** (exit 1 in strict mode): The first valid Type Word's declared extent runs past EOF. Usually means the recording was aborted before the first complete record was written. Lenient mode logs this as a WARN and then fails with "no valid records" (exit 2), because a decode that produced no rows is not a success; strict mode raises this distinct error instead (exit 1).

**WARN lines like `non-monotonic timestamp at 0x...` or `L2-SYN anomaly at 0x...`**: These don't fail the decode — they're observations about the recording. The first means a record's timestamp went backwards on the same RT/MSG key (DELTA is left empty for that row); the second means a Status Word RT didn't match its Command Word RT (possible bus interference). If you see high rates of either, investigate the recording source.

For the full error catalog with every variant, exit code, and "what to do" guidance, see [`ERROR-CATALOG.md`](ERROR-CATALOG.md).

---

## 9. Configuration — quick overview

Most workflows don't need a config file — the defaults are sensible and CLI flags cover the common overrides. But for repeated runs against many files, a config file removes the per-invocation noise.

Minimum-viable config:

```toml
[decode]
error_mode = "inline"     # everything in one CSV

[logging]
level = "INFO"            # see the exit-class summary in stderr
```

```bash
aero1553 --config my.toml decode flight.mie -o flight.csv
```

CLI arguments override matching config keys (L2-CFG-003). Filter arrays are the one exception — CLI values **add to** config values rather than replacing them (L2-CFG-004), so a site-wide `exclude_types = ["SPURIOUS_DATA"]` plus a CLI `--exclude-rts 31` yields both filters active.

For every accepted key, its type, default, validation behavior, and CLI override, see [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md).

---

## 10. Performance and large recordings

Both implementations produce byte-identical CSV, decode at broadly similar speed, and decode in **constant memory** — rows stream straight to the output, so a 10 GB recording uses the same memory as a 10 MB one.

| Implementation | Memory while decoding | Practical ceiling |
|----------------|-----------------------|-------------------|
| **Rust** | Constant — `O(1)` in the record count. | Bounded by disk, not RAM. |
| **Python** | Constant — `O(1)` in the record count. Each row streams to the output through the standard-library `csv` module; nothing accumulates across records. | Bounded by disk, not RAM. |

**Rule of thumb:** either CLI handles multi-GB recordings and **10M+ record** files without memory becoming a concern — the output is identical. Choose by ecosystem: the Rust CLI for a single self-contained binary, the Python CLI to stay inside a Python pipeline.

The constant-memory guarantee is tracked as `L3-PY-012` (Python) / `L3-RS-012` (Rust) and is load-bearing: a change that buffers rows would regress it. See [`ARCHITECTURE.md`](ARCHITECTURE.md) §12 (memory profile) and §14 (operational limits) for the underlying detail.

---

## 11. What's next

- **Hit a column you don't recognize?** [`MIE-FORMAT.md`](MIE-FORMAT.md) is the per-column reference (binary layout + CSV format).
- **Hit an exit code or error message you don't recognize?** [`ERROR-CATALOG.md`](ERROR-CATALOG.md) covers every variant with operator guidance.
- **Want to know how the tool handles a specific kind of input** (empty / corrupt / non-MIE file, odd timestamps, a bad input inside a merge, …)**?** [`DATA-SCENARIOS.md`](DATA-SCENARIOS.md) maps every data condition to its CSV / log / exit outcome in plain language.
- **Setting up site or campaign config?** [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md) is the normative TOML schema.
- **Modifying the decoder itself?** [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) covers the development workflows.
- **Curious how decoding works under the hood?** [`ARCHITECTURE.md`](ARCHITECTURE.md) walks the reader/sync/writer pipeline.
- **Need to know what the spec says?** [`L1-REQ.md`](L1-REQ.md), [`L2-REQ.md`](L2-REQ.md), [`L3-REQ.md`](L3-REQ.md), and [`TRACE-MATRIX.md`](TRACE-MATRIX.md).
