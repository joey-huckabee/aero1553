# Aero1553 — Examples

Runnable cookbook for the common operator tasks. Each example is a self-contained recipe: the setup, the exact command, the expected output, and what to do if something deviates.

If you're new, read [`USER-GUIDE.md`](USER-GUIDE.md) first — it explains how each piece works. This doc shows the pieces composed for real workflows. For the full reference of every TOML key, see [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md); for every exit code and error class, see [`ERROR-CATALOG.md`](ERROR-CATALOG.md).

All examples work identically with the Rust and Python CLIs — both ship the same argument surface (same subcommands, same `--separate-errors`, same global `--config`, same comma-separated filter syntax).

---

## 1. Decode an MIE file you just received

You have `flight.mie` from a DDC recording card. You want CSV.

```bash
aero1553 decode flight.mie -o flight.csv
```

That's the whole command. Auto-detect picks the timestamp format from the first few records; the header (if any) is skipped automatically; output is written atomically (no half-written file on crash).

Add `--log-level INFO` to see what happened:

```bash
$ aero1553 --log-level INFO decode flight.mie -o flight.csv
INFO  beginning decode of flight.mie
INFO  auto-detected timestamp format: Irig (Decisive: IRIG=40 STD=0 over 8 record(s))
INFO  decode complete: 14523 messages, 0 sync recoveries, format=Irig, file=flight.mie
INFO  decode exit class: complete (sync_losses=0)
```

A successful row looks like this in the output CSV:

```
192:15:54:50.456225,15,11R,0400,0000,0000,002F,CA22,002F,CA22,0000,...,C771,,,7800,797E,,,A,0.000000,,,,,
```

- `192:15:54:50.456225` — IRIG timestamp (day 192 of the year, 15:54:50 UTC, microsecond 456225).
- `15,11R` — Remote Terminal 15, subaddress 11, Receive direction.
- `0400,0000,…,C771` — 30 data words, 4-char uppercase hex.
- `7800` — Status Word; `797E` — Command Word.
- `A` — Bus A.
- `0.000000` — DELTA in seconds (first occurrence of RT15/11R so it's zero).

See [`USER-GUIDE.md`](USER-GUIDE.md) §7 or [`MIE-FORMAT.md`](MIE-FORMAT.md) for the full column reference.

---

## 2. Quickly count records before a long decode

For multi-GB recordings, you may want to know the message count before committing to a full decode.

```bash
$ aero1553 count flight.mie
14523
# (stderr, always emitted: "counted 14523 messages in flight.mie")
```

Both implementations follow the same two-channel contract (L3-RS-008 / L3-PY-010): only the integer goes to stdout (so `n=$(aero1553 count flight.mie)` works cleanly), while the human-readable status line goes to stderr.

The count walks the entire file but doesn't write CSV — much faster than a full decode, useful for sanity checks (does this file have the ~15K records I expect, or is something off?). Filters apply if configured, so a config-file `exclude_types = ["SPURIOUS_DATA"]` would count only non-spurious records.

---

## 3. Error output (inline by default) and the separate-file option

DDC vendor CSV mixes errored, SPURIOUS, and clean records into one file, and so does Aero1553 by default — no flag needed, which is what makes a default decode directly diffable against vendor output:

```bash
aero1553 decode flight.mie -o flight.csv
```

Errored records appear in `flight.csv` with the `ERROR` column set to `ERROR` and `ERROR_CODE` carrying the DDC hardware code:

```
192:15:54:50.456225,15,11R,...,7800,797E,,,A,0.000000,,,,,         ← clean
192:15:54:50.789012,15,11R,...,,2402,,,A,,ERROR,011E,,,            ← Manchester/parity error
192:15:54:50.789012,,,1234,5678,,,...,,,,,A,,SPURIOUS,2000,,,      ← orphan SPURIOUS following the error
```

See [`ERROR-CATALOG.md`](ERROR-CATALOG.md) §6 for the DDC code reference and §7 for the decoder-assigned `0x2000` / `0x2001` codes.

If you would rather keep the main CSV clean, pull the errored and spurious rows into a sibling file:

```bash
aero1553 decode flight.mie --separate-errors -o flight.csv
# -> flight.csv (clean rows) + flight_errors.csv (errored + SPURIOUS rows)
```

For a full vendor-CSV diff workflow, see [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §6.

---

## 4. Filter to a specific RT for focused analysis

You're investigating a specific Remote Terminal. Drop everything else:

```bash
# Keep only RT 15 by excluding all other RTs (comma-separated list):
aero1553 decode flight.mie \
    --exclude-rts 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31 \
    -o rt15.csv
```

Awkward. Both CLIs provide `--include-rts` for the positive form:

```bash
aero1553 decode flight.mie --include-rts 15 -o rt15.csv
```

For drop-by-type / drop-by-bus / drop-by-subaddress, both CLIs share the same exclusion flags:

```bash
# Drop spurious noise, drop mode codes, keep only Bus A:
aero1553 decode flight.mie \
    --exclude-types SPURIOUS_DATA,MODE_COMMAND \
    --exclude-buses B \
    -o focused.csv
```

Filter axes use **OR logic** (a message is dropped if it matches ANY criterion) per L2-FLT-002. CLI filter values **add to** config-file filters per L2-CFG-004.

---

## 5. Get DELTA timing from a Standard-counter recording

Your recording uses the Standard (free-running counter) timestamp format, not IRIG. Decoded as-is, `TIME_STAMP` is a raw hex counter and `DELTA` is empty for every row — the counter's tick rate isn't stored in the file, so the decoder won't guess at elapsed time:

```bash
aero1553 decode counter.mie --input-time-format standard -o out.csv
# DELTA column blank on every row
```

You know from the card's configuration that the counter runs at 1 MHz. Supply it and `DELTA` comes to life:

```bash
aero1553 decode counter.mie --input-time-format standard --standard-tick-rate-hz 1000000 -o out.csv
```

Now each RT/MSG key gets `0.000000` on first sight and real elapsed seconds thereafter — e.g. two records of the same key 16 ticks apart show `DELTA = 0.000016`. The conversion is `round(raw_ticks × 1_000_000 / rate)`; the rate must be `> 0`.

Prefer to bake it into site config (handy when every recording from a given card shares a rate):

```toml
# counter-card.toml
[decode]
input_time_format = "standard"
standard_tick_rate_hz = 1000000.0
```

```bash
aero1553 --config counter-card.toml decode counter.mie -o out.csv
```

The flag overrides the config value if both are present (CLI > config > default). The setting is a no-op on IRIG recordings. See [`USER-GUIDE.md` → Calibrating Standard timestamps](USER-GUIDE.md#calibrating-standard-timestamps) for background on where the rate comes from.

---

## 5b. Emit ISO-8601 timestamps for a downstream system

The analytics platform ingesting your CSVs wants calendar dates, not
day-of-year. The recording is from **11 July 2026**:

```bash
aero1553 decode flight.mie -o out.csv --output-time-format iso --year 2026
```

```
TIME_STAMP,RT,MSG,WD01,...
2026-07-11T15:54:50.456225Z,15,11R,0400,...
```

`--year` is not optional here, and it is not a preference. IRIG-B encodes
day-of-year but **not the year**, and day 192 is July 10 in a leap year and July
11 in a common one — so without a year there is no date to render. The decoder
refuses rather than guessing:

```bash
aero1553 decode flight.mie -o out.csv --output-time-format iso
# Error: --output-time-format iso needs a calendar year, and an MIE recording
#   does not carry one: IRIG-B encodes day-of-year but not the year.
#   Set [output] year = YYYY in a config file or pass --year YYYY.
echo $?   # 4 — and no output file was created
```

If your site's IRIG source is not UTC, say so; the offset is appended as a
designator and never shifts the time fields:

```bash
aero1553 decode flight.mie -o out.csv \
    --output-time-format iso --year 2026 --utc-offset -05:00
# 2026-07-11T15:54:50.456225-05:00,15,11R,...
```

Set it once for a whole site instead of typing it every run:

```toml
# site-iso.toml
[output]
output_time_format = "iso"
year = 2026
utc_offset = "Z"
```

```bash
aero1553 --config site-iso.toml decode flight.mie -o out.csv
```

**Two things to know before you turn this on.** It voids a vendor-CSV diff —
`doy` is the only byte-comparable rendering (§11). And the decoder starts warning
about the day-of-year field: it has a known firmware quirk on some DDC card
models, which matters much more once it is being resolved into a date that reads
as a fact rather than printed as a day number. See
[`VENDOR-CSV-DIFFS.md` §5](VENDOR-CSV-DIFFS.md).

---

## 6. Recover what you can from a corrupt recording

The recording has unrecoverable mid-file corruption. Default behavior exits 3 with no output (so you can't mistake a partial result for a complete one). To preserve what was decoded before the corruption:

```bash
aero1553 decode corrupt.mie --allow-partial -o decoded.csv
```

On unrecoverable sync loss:

- A `decoded.csv.partial` file is created with all rows decoded before the loss.
- The main `decoded.csv` is **not** created. The `.partial` suffix is deliberate — downstream consumers shouldn't pick it up automatically.
- The CLI exits 0 with a WARN summary naming the sync-loss count.

```bash
$ aero1553 --log-level INFO decode corrupt.mie --allow-partial -o decoded.csv
INFO  beginning decode of corrupt.mie
INFO  auto-detected timestamp format: Irig (Decisive: IRIG=40 STD=0 over 8 record(s))
WARN  sync lost at 0x12340 (type=0x7F wc=0); scanning forward
ERROR unrecoverable sync loss at 0x12340 after 287 messages
WARN  unrecoverable sync loss at 0x12340 after 1 recovery attempt(s); \
      wrote 287 rows to decoded.csv.partial (--allow-partial)
INFO  decode exit class: partial-unrecoverable (sync_losses=1)

$ ls
corrupt.mie  decoded.csv.partial
```

Note the exit class is `partial-unrecoverable` even though the exit **code** is
`0`: `--allow-partial` converts the failure into a successful run that preserves
what was decoded, and the class keeps the corruption visible in the log.

Inspect `decoded.csv.partial` to see what was salvageable; investigate the source recording for storage / transmission issues separately.

---

## 7. Stream to a downstream pipeline (pandas / awk / a script)

Omit `-o` to write CSV to stdout. The decoder forces inline-error mode (you can't split stdout into two streams) and a broken-pipe condition exits 0 with no error per L2-WRT-018.

### Pipe into pandas

```python
# count_by_rt.py
import sys
import pandas as pd
df = pd.read_csv(sys.stdin)
print(df.groupby("RT").size().sort_values(ascending=False))
```

```bash
aero1553 decode flight.mie | python count_by_rt.py
```

### Pipe into awk for quick counts

```bash
# Count rows per Remote Terminal:
aero1553 decode flight.mie | awk -F, 'NR > 1 { c[$2]++ } END { for (rt in c) print rt, c[rt] }'

# Sum data word 1 across all RT15/SA11 receives (e.g. a counter field):
aero1553 decode flight.mie | \
    awk -F, 'NR>1 && $2=="15" && $3=="11R" { sum += strtonum("0x" $4) } END { print sum }'
```

### Tail with `head` (early termination)

```bash
# First 100 records, no error if the decoder is still running:
aero1553 decode flight.mie | head -100
```

The decoder's writer detects the broken pipe and exits 0 silently. You won't see a `BrokenPipeError` or a non-zero exit.

---

## 8. Site-wide config + per-invocation override

Site-wide defaults in TOML, per-invocation tweaks on the CLI. CLI arguments win on conflict (L2-CFG-003); filter arrays merge (L2-CFG-004).

```toml
# /etc/aero1553/site.toml — read by every analyst on this host
[logging]
level = "INFO"

[decode]
error_mode = "inline"     # everything in one CSV by site convention

[filter]
exclude_types = ["SPURIOUS_DATA"]   # we never look at spurious noise
exclude_rts   = [31]                # we never look at broadcast
```

Daily use:

```bash
aero1553 --config /etc/aero1553/site.toml decode flight.mie -o flight.csv
```

Override for a specific investigation that needs the broadcast traffic:

```bash
aero1553 decode flight.mie \
    --config /etc/aero1553/site.toml \
    -o investigation.csv
# RT 31 is still filtered (config). To bring it back in this run, you'd
# need a different config; CLI cannot "un-exclude" something the config
# excluded. Workaround: keep a separate config for investigations.
```

Per-invocation merge (CLI filter adds to config filter):

```bash
aero1553 decode flight.mie \
    --config /etc/aero1553/site.toml \
    --exclude-rts 0 \
    -o flight.csv
# Effective: SPURIOUS_DATA filtered (config), RT 31 filtered (config), RT 0 filtered (CLI merge).
```

See [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md) for every accepted key and its CLI override.

---

## 9. CI / batch script that handles exit codes properly

The CLI exits with one of seven codes per L1-EXIT-001 through L1-EXIT-010 (see
[`ERROR-CATALOG.md`](ERROR-CATALOG.md) §1 for the full table). A robust batch script:

```bash
#!/usr/bin/env bash
set -u   # NOT set -e — we want to inspect the exit code ourselves

input="$1"
output="$2"

aero1553 decode "$input" -o "$output"
rc=$?

case $rc in
    0)
        # complete OR partial-recovered. Both are clean termination.
        echo "OK: decoded $input -> $output"
        ;;
    1)
        # Runtime / decode error: record error in strict mode, input I/O,
        # or writer failure.
        echo "FAIL: $input — see stderr" >&2
        exit 1
        ;;
    2)
        # No valid records: the file isn't an MIE recording (wrong file type).
        # Or homogeneous payload (single-byte pad).
        echo "SKIP: $input is not a valid MIE recording" >&2
        exit 0   # skip cleanly in batch contexts
        ;;
    3)
        # Unrecoverable mid-file sync loss without --allow-partial.
        # Retry with --allow-partial to preserve what was decoded.
        echo "PARTIAL: $input has unrecoverable corruption; retrying with --allow-partial"
        aero1553 decode "$input" --allow-partial -o "$output"
        if [ -f "${output}.partial" ]; then
            echo "  recovered $(wc -l < "${output}.partial") rows to ${output}.partial"
        fi
        ;;
    4)
        # Usage error: the command line itself is wrong (bad flag, etc.).
        # This is a bug in the batch script, not a data problem — abort.
        echo "BUG: bad aero1553 invocation for $input" >&2
        exit 2
        ;;
    5)
        # Configuration error: the --config TOML is missing or invalid.
        # Fix the config; don't keep looping over inputs.
        echo "BUG: invalid aero1553 config" >&2
        exit 2
        ;;
    6)
        # Merge-incompatible inputs: a multi-file merge whose inputs cannot
        # share an absolute IRIG timeline. Only reachable when this script
        # passes more than one input; decode them individually instead.
        echo "SKIP: $input set is not all calendar-locked IRIG" >&2
        exit 0
        ;;
    *)
        echo "UNEXPECTED exit $rc for $input" >&2
        exit "$rc"
        ;;
esac
```

This pattern is the canonical batch loop: 0 succeeds, 2 means "not our file type, skip", 3 means "try `--allow-partial`", 4/5 mean the script or its config is wrong (abort the batch), 6 means the merge inputs can't share a timeline, and everything else is a real failure.

The `decode exit class:` log line in stderr (emitted at INFO per L1-EXIT-005) names the class explicitly for log-grep:

```
INFO  decode exit class: complete (sync_losses=0)
INFO  decode exit class: partial-recovered (sync_losses=3)
INFO  decode exit class: empty-recording (sync_losses=0)
INFO  decode exit class: no-records
INFO  decode exit class: merge-incompatible
INFO  decode exit class: partial-unrecoverable (sync_losses=12); pass --allow-partial to preserve...
```

---

## 10. Investigate a file the decoder rejected

The CLI exited 2 with `No valid MIE records found` or `Pathological homogeneous-payload input rejected`. Use the `dump` subcommand to see what's in the file:

```bash
# Raw hex dump of the first 256 bytes:
aero1553 dump suspect.mie --raw --length 256
```

```
File: suspect.mie (1024 bytes)
Range: 0x00000000-0x00000100

  00000000  20 20 20 20 20 20 20 20 20 20 20 20 20 20 20 20  |                |
  00000010  20 20 20 20 20 20 20 20 20 20 20 20 20 20 20 20  |                |
  ...
```

A wall of `0x20 0x20` (ASCII spaces) is the classic single-byte-pad pattern that triggers `HomogeneousPayload` rejection — the file isn't an MIE recording, it's a botched transfer that filled with spaces.

For files that DO look like MIE but still fail, use record-aware mode:

```bash
# Parse the first N records and show each one's decoded header + bytes:
aero1553 dump suspect.mie --records 3
```

```
File: suspect.mie (4096 bytes)
Record dump starting at offset 0x00000000

------------------------------------------------------------------------
  Record #0  @  0x00000000  (72 bytes, 36 words)
  Type:   0x2402  ->  BC->RT (Receive)  Bus A  error flag (bit 14): clear
  Format: RECEIVE
  Time:   192:15:54:50.456225
  Cmd:    0x797E  ->  RT15 SA11 R WC=30
    00000000  02 24 0F 18 26 DB 21 F6 7E 79 00 04 00 00 00 00  |.$..&.!.~y......|
    00000010  2F 00 22 CA 2F 00 22 CA 00 00 00 00 00 00 00 00  |/.".../.".......|
    ...
```

Record-aware dump tells you what the decoder THOUGHT it was looking at — Type Word interpretation, timestamp, Command Word, and the raw bytes. If the decoded annotation doesn't match what you expect from the recording metadata, the file is probably from a different recording format (not MIE) or has corruption at the start.

See [`USER-GUIDE.md`](USER-GUIDE.md) §5 for the full `dump` subcommand reference.

---

## 11. Diff against vendor CSV

You want to validate that Aero1553 reproduces vendor output for a known-good recording.

```bash
# 1. Generate both CSVs from the same input file.
#    --no-mux leaves the MUX column empty like the vendor's other placeholders;
#    --max-sort-group 1 disables canonical row ordering so rows stay in the
#    vendor's capture order. Both are needed for a byte-for-byte diff.
aero1553 decode flight.mie -o mie.csv --no-mux --max-sort-group 1
# Vendor tool produces flight-vendor.csv via whatever process you normally use.

# 2. Normalize line endings if your platforms differ.
tr -d '\r' < flight-vendor.csv > flight-vendor-lf.csv

# 3a. Simplest: truncate our output to the 44-column vendor block, diff whole rows.
#     Columns 1-44 are the DDC vendor layout and share indices with the vendor
#     CSV; 45-46 (ERROR, ERROR_CODE) are decoder additions with no vendor
#     equivalent, so they are simply cut away.
cut -d, -f1-44 mie.csv > mie-vendor-block.csv
diff flight-vendor-lf.csv mie-vendor-block.csv

# 3b. If the vendor file populates MUX / TERM_NAME / the gap columns and you want
#     those masked out too, compare only the columns both tools fill:
#     TIME_STAMP(1) RT(2) MSG(3) WD01-WD32(4-35) STAT(36) CMD(37) BUS(40) DELTA(41)
diff \
    <(awk -F, 'NR>0 {for (i=1; i<=37; i++) printf "%s%s", $i, (i==37 ? "" : ","); printf ",,,%s,%s\n", $40, $41}' flight-vendor-lf.csv) \
    <(awk -F, 'NR>0 {for (i=1; i<=37; i++) printf "%s%s", $i, (i==37 ? "" : ","); printf ",,,%s,%s\n", $40, $41}' mie.csv)
```

Expected output: nothing. A successful diff is silent.

If you see differences:

- **Day-of-year on IRIG `TIME_STAMP`** — known firmware-dependent discrepancy on some DDC card models. See [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §5. The decoder notes this once per decode at `INFO` (so it is silent by default); once you have confirmed the column against vendor output for your card model, `--no-irig-day-advisory` retires the note for good (§5a).
- **`MUX` column differs** — by default we populate `MUX` from the input file name (L2-WRT-020); pass `--no-mux` for vendor-exact (empty) output. See [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §3.
- **Empty `TERM_NAME` / `IM_GAP` / `RCV_GAP` / `XMT_GAP` on our side, populated on vendor** — expected; we leave these empty by spec. See [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §3.
- **Same rows, different order within one `TIME_STAMP`** — canonical row order (L1-OUT-003); we sort ties by
  `RT` then `MSG`, the vendor writes capture order. `--max-sort-group 1` (step 1) suppresses it. See
  [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §3a.
- **Anything else** — bug. Report per [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §7.

---

## 12. One-off ad-hoc filters with shell tools

The shell composes well with Aero1553 output. Some recipes that come up:

```bash
# Histogram of message types across the recording:
aero1553 decode flight.mie | awk -F, 'NR>1 { print substr($3, length($3), 1) }' | sort | uniq -c

# Find any transactions on RT 0 (often unused, so finding traffic there is interesting):
aero1553 decode flight.mie | awk -F, 'NR>1 && $2=="0"' | head

# Inter-arrival histogram for one RT/MSG (DELTA distribution):
aero1553 decode flight.mie | \
    awk -F, 'NR>1 && $2=="15" && $3=="11R" { print int($41 * 1000) }' | \
    sort -n | uniq -c    # bucketed to milliseconds

# Quick "is there any traffic on Bus B?" check:
aero1553 decode flight.mie | awk -F, 'NR>1 && $40=="B"' | head -1
```

Column indices reference the spec column order (1 = TIME_STAMP, 2 = RT, 3 = MSG, 4–35 = WD01–WD32, 36 = STAT, 37 = CMD, 38 = MUX, 39 = TERM_NAME, 40 = BUS, 41 = DELTA, 42 = IM_GAP, 43 = RCV_GAP, 44 = XMT_GAP, 45 = ERROR, 46 = ERROR_CODE). Indices 1–44 are the DDC vendor layout and match a vendor CSV position-for-position; 45–46 are decoder additions.

> **Changed in v2.10.0:** `ERROR` / `ERROR_CODE` moved from 42/43 to 45/46, and the three gap columns moved from 44/45/46 to their vendor positions 42/43/44. Scripts that referenced those six by number need updating — see [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) §8 for a mapping table and a name-based alternative that cannot drift again.

---

## 13. Merge several recordings into one time-sorted CSV

A session split across several IRIG recordings, combined into one timeline:

```bash
# Ad-hoc: a few files by name.
aero1553 decode run-a.mie run-b.mie run-c.mie -o session.csv

# Many files: list them in a manifest (one path per line; # comments ok).
cat > session.txt <<'EOF'
# Flight 4471, all recorders
/data/4471/recorder-1.mie
/data/4471/recorder-2.mie
/data/4471/recorder-3.mie
EOF
aero1553 decode --manifest session.txt -o session.csv

# A whole directory (the tool expands the glob — works in cmd.exe/PowerShell too):
aero1553 decode --glob '/data/4471/*.mie' -o session.csv
```

Robust batch that tolerates one corrupt recorder and reports cleanly:

```bash
aero1553 decode --glob '/data/4471/*.mie' -o session.csv --allow-partial
case $? in
  0) [ -f session.csv.partial ] && echo "merged (partial — one input failed): session.csv.partial" \
                                || echo "merged cleanly: session.csv" ;;
  6) echo "inputs aren't all calendar-locked IRIG — decode them individually" >&2; exit 6 ;;
  4) echo "usage error (combined input methods, or > 256 files)" >&2; exit 4 ;;
  *) echo "merge failed (exit $?)" >&2; exit 1 ;;
esac
```

Filter the merged stream just like a single decode — filters apply across all
inputs:

```bash
aero1553 decode run-a.mie run-b.mie -o rt15.csv --include-rts 15
```

If several recorders overlap (they all heard the same bus), collapse each
transaction's duplicate rows into one with `--collapse-duplicates` — off by
default, so you opt in only when you want it:

```bash
# Two recorders on the same bus → one row per transaction, not two.
aero1553 decode recorder-1.mie recorder-2.mie -o session.csv --collapse-duplicates

# Recorders whose IRIG clocks drift a little: allow a 200µs tolerance.
aero1553 decode --manifest recorders.txt -o session.csv \
  --collapse-duplicates --collapse-window-us 200
```

The Python CLI accepts the identical commands and produces byte-identical
output. Merge only IRIG recordings from the same calendar year; see
[`USER-GUIDE.md`](USER-GUIDE.md) §6 for the full contract.

---

## 14. See also

- [`USER-GUIDE.md`](USER-GUIDE.md) — How each piece works (vs this doc, which shows the pieces composed).
- [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md) — Every TOML key, type, default, CLI override.
- [`ERROR-CATALOG.md`](ERROR-CATALOG.md) — Every exit code, error variant, DDC error code, decoder code.
- [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) — Column-by-column alignment with vendor CSV and divergence-reporting protocol.
- [`MIE-FORMAT.md`](MIE-FORMAT.md) — Per-column reference (binary source and CSV format).
