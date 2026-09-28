# Aero1553

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=aero1553&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=aero1553)

Decoder for DDC MIL-STD-1553 MIE binary recording files.

Aero1553 reads proprietary binary files produced by Data Device Corporation (DDC) MIL-STD-1553 PCI recording cards and outputs decoded messages in CSV format compatible with DDC's own recording software output.

Aero1553 is maintained in three implementations:

- **Rust** — streaming CSV writer (constant memory), hand-rolled CLI, single
  native release binary. See [`rust/README.md`](rust/README.md).
- **Python** — the Python package and CLI. See
  [`python/README.md`](python/README.md).
- **C++** — C++11 as accepted by GCC 4.8.5, so **SLES 12 SP5** is a first-class
  deployment target rather than a documented exception; the same source ships a
  Windows binary via MSVC. See [`cpp/README.md`](cpp/README.md).

All three ship together as a joint cut from a single repository tag, at one
version number — a CI gate fails the build if they disagree. Future releases
may diverge via impl-prefixed tags (`rust-vX.Y.Z`, `python-vX.Y.Z`,
`cpp-vX.Y.Z`). The implementations share the MIE format documentation, the
vendor-compatible CSV behavior, an identical CLI flag surface, and a byte-exact
cross-implementation conformance suite (`tests/conformance/`) that every one of
them must pass. See
[`CHANGELOG.md`](CHANGELOG.md) for the release history.

## Building

Build, install, and library-usage instructions live with each implementation:

- **Rust** — [`rust/README.md`](rust/README.md): native release binary, crate /
  library API, `cargo` workflow.
- **Python** — [`python/README.md`](python/README.md): `aero1553` CLI plus the
  importable `aero1553` package, Poetry workflow.
- **C++** — [`cpp/README.md`](cpp/README.md): `make` on Linux (authoritative),
  CMake/MSVC on Windows, and the GCC 4.8.5 fidelity tier.

The CLI surface, configuration schema, and CSV output documented below are
shared by both implementations.

## Quick Start

```bash
# Decode to CSV
aero1553 decode recording.mie -o decoded.csv

# Count messages (no CSV output)
aero1553 count recording.mie

# Hex dump with record annotations
aero1553 dump recording.mie --records 10

# Decode with config file
aero1553 --config config/default.toml decode recording.mie -o decoded.csv
```

## CLI Reference

`aero1553` has three subcommands — `decode` (binary → CSV), `count`, and
`dump` — with an identical flag surface in the Rust and Python builds. The
**complete per-flag reference** (every option, with its default, value range, and
config-key equivalent) lives in
**[`docs/CLI-REFERENCE.md`](docs/CLI-REFERENCE.md)**. Each CLI's own
`aero1553 <subcommand> --help` lists the same flags, but they are not one
generated source: Python's help is produced by `argparse` from its argument
definitions, while Rust's is a hand-maintained help string. The
`cli-surface-parity` check in `tests/conformance/run.py` fails CI if the two ever
advertise a different set of long options — including a flag the Rust parser
still accepts but its help stopped listing.

Config-file keys are documented in
[`docs/CONFIG-REFERENCE.md`](docs/CONFIG-REFERENCE.md); task-oriented
walkthroughs (multi-file merge, MUX from the filename, filtering, vendor diffs)
are in the [User Guide](docs/USER-GUIDE.md) and [Examples](docs/EXAMPLES.md).

### Common examples

```bash
# Decode to CSV
aero1553 decode recording.mie -o decoded.csv

# Drop spurious + broadcast traffic
aero1553 decode rec.mie -o clean.csv \
  --exclude-types SPURIOUS_DATA,BROADCAST_BC_TO_RT

# Only Bus A, only RT 15 (positive filters)
aero1553 decode rec.mie -o rt15.csv --include-buses A --include-rts 15

# Split errored + spurious rows out (the default keeps them inline)
aero1553 decode rec.mie -o split.csv --separate-errors      # -> split.csv + split_errors.csv

# Multi-file, time-sorted merge; de-dup overlapping recorders
aero1553 decode a.mie b.mie -o merged.csv --collapse-duplicates

# Count records; annotated hex dump
aero1553 count recording.mie
aero1553 dump recording.mie --records 10
```

Library usage (the Rust crate API and the Python `aero1553` package) is
documented in each implementation's README — [`rust/README.md`](rust/README.md)
and [`python/README.md`](python/README.md).

## Error Handling

When the DDC card detects an error mid-transaction (Manchester error, parity error, missing response), it writes a truncated record with bit 14 set in the Type Word and appends an Error Word containing the error code.

### Error modes

> **By default every record — clean, errored, and SPURIOUS — goes to one CSV**,
> with the `ERROR`/`ERROR_CODE` columns populated. One file for everything is also
> how the DDC vendor tool writes its output, so a default decode lines up with
> vendor output row-for-row (see
> [`docs/VENDOR-CSV-DIFFS.md`](docs/VENDOR-CSV-DIFFS.md)). The `ERROR` /
> `ERROR_CODE` columns themselves are an Aero1553 addition — the vendor tool
> does not report bus errors as CSV fields — which is why they sit at the end,
> after the 44-column vendor layout. Pass **`--separate-errors`** if you want the
> errored and SPURIOUS records pulled out into a sibling `<output>_errors.csv` so
> the main file holds only clean rows.

- **Default (inline)**: All messages in one CSV. `ERROR` column is `"ERROR"` or `"SPURIOUS"` (empty for clean rows); `ERROR_CODE` holds the code.
- **`--separate-errors`**: Normal messages go to the main CSV; errored and spurious records go to `<output>_errors.csv` (created only if there are error rows).

> **Changed in v2.8.0:** inline used to be opt-in via `--inline-errors`, and
> separate was the default. The polarity is now reversed and `--inline-errors` has
> been **removed** — passing it is a usage error (exit 4). Drop the flag to keep
> the same output, or add `--separate-errors` for the old default layout.

### Error codes

| Code | Source | Description |
|------|--------|-------------|
| 0x011E | DDC | Manchester/Parity Error or Bit Count Error |
| 0x0120 | DDC | No Status Response or Too Few Data Words |
| 0x0136 | DDC | Inverted Sync on Data Word |
| 0x0140 | DDC | Too Many Data Words |
| 0x0150 | DDC | Unknown DDC Error |
| 0x2000 | Decoder | Spurious Data: Continuation of preceding error |
| 0x2001 | Decoder | Spurious Data: Standalone (no preceding error) |

## Sync Recovery

Aero1553 automatically handles:

- **File headers**: Scans from offset 0 to find the first valid record, skipping proprietary headers.
- **Mid-file corruption**: If a record fails validation, scans forward in 2-byte steps to find the next valid record.
- **Unified validation**: The same validation rules are used for header skip, normal forward decode, and post-loss recovery. The additive detailed API reports a `ValidationFailure` reason while the existing boolean API remains compatible.
- **Validation checks** (in order): valid message type → plausible word count → record fits in file → IRIG timestamp fields in range → configurable N-record look-ahead.
- **DEBUG diagnostics**: Validation failures include one context hex line capped at 32 bytes.
- **Error records maintain sync**: Error records (bit 14) and SPURIOUS_DATA continuations are valid records with valid Type Words.

## Configuration

`aero1553` reads settings from an optional TOML file passed with
`--config PATH`. Precedence is **CLI flags > config file > built-in defaults**.

The schema is not reproduced here (where it drifts). See the two authoritative
sources instead:

- **[`config/default.toml`](config/default.toml)** — the fully-commented reference
  file: every key with its real default value and inline notes. Copy it and edit.
- **[`docs/CONFIG-REFERENCE.md`](docs/CONFIG-REFERENCE.md)** — the normative
  per-key reference: type, default, validation behavior, and the CLI flag that
  overrides each key (the config-side companion to
  [`docs/CLI-REFERENCE.md`](docs/CLI-REFERENCE.md)).

## Supported Message Formats

All 10 MIL-STD-1553 message formats plus SPURIOUS_DATA:

| Type Code | Format | Payload Layout |
|-----------|--------|----------------|
| 0x02 | Receive (BC→RT) | Cmd → Data(N) → Status |
| 0x04 | Transmit (RT→BC) | Cmd → Status → Data(N) |
| 0x08 | RT-to-RT | RxCmd → TxCmd → TxStatus → Data(N) → RxStatus |
| 0x10 | Broadcast Receive | Cmd → Data(N) |
| 0x18 | Broadcast RT-to-RT | RxCmd → TxCmd → TxStatus → Data(N) |
| 0x01 | Mode Code TX Data | ModeCmd → Status → DataWord |
| 0x01 | Mode Code RX Data | ModeCmd → DataWord → Status |
| 0x01 | Mode Code No Data | ModeCmd → Status |
| 0x01 | Mode Code Bcast No Data | ModeCmd |
| 0x01 | Mode Code Bcast Data | ModeCmd → DataWord |
| 0x20 | Spurious Data | Raw bus words (no command structure) |

## Project Structure

```
rust/                Rust crate (single dependency: memmap2) — see rust/README.md

config/
└── default.toml     Fully commented reference configuration

docs/
├── ARCHITECTURE.md     Module diagram, sync strategy, data flow
├── CLI-REFERENCE.md    Complete per-flag CLI reference (all subcommands)
├── CONFIG-REFERENCE.md Normative TOML key reference (type / default / CLI override)
├── DATA-SCENARIOS.md   Every data condition mapped to its CSV / log / exit outcome
├── ERROR-CATALOG.md    Operator reference: exit codes, error classes, DDC codes
├── EXAMPLES.md         Runnable cookbook of common operator tasks
├── FUZZING.md          What is fuzzed, by which implementation, and what is not
├── L1-REQ.md           Level 1 SHALL statements (system requirements)
├── L2-REQ.md           Level 2 architectural derivations
├── L3-REQ.md           Level 3 implementation obligations (incl. PY/RS)
├── MAINTAINER-GUIDE.md Repo layout, dev setup, workflows for adding things
├── MIE-FORMAT.md       Comprehensive binary format + CSV column reference
├── USER-GUIDE.md       End-to-end CLI walkthrough for analysts and operators
├── VENDOR-CSV-DIFFS.md Alignment statement vs DDC vendor CSV (column-by-column)
├── TRACE-MATRIX.md     Auto-generated trace matrix (L1 -> L2 -> L3 -> tests)
├── ROADMAP.md          Versioned roadmap
└── diagrams/           Hand-written SVG diagrams (the source of truth)

tests/
└── conformance/     Cross-implementation suite (Rust ↔ Python oracle)

python/              Python package and CLI — see python/README.md
```

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md).

## Development

Per-implementation development commands (build, test, lint, coverage) live in
[`rust/README.md`](rust/README.md) and [`python/README.md`](python/README.md);
[`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/MAINTAINER-GUIDE.md`](docs/MAINTAINER-GUIDE.md) cover the full workflow.

Shared Rust/Python conformance suite (run with an interpreter that has
`aero1553` installed, and build the Rust binary first):

```bash
(cd rust && cargo build)
poetry -C python run python ../tests/conformance/run.py
```

## Known Limitations

- The Day field in IRIG timestamps may not decode correctly on all DDC card models.
- `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP` columns are present for format compatibility but empty (by spec). `MUX` is populated from the input file name by default (L2-WRT-020); pass `--no-mux` for vendor-exact (empty) output.
- The CSV is 46 columns: the 44-column DDC vendor layout followed by the decoder-added `ERROR` and `ERROR_CODE`. `cut -d, -f1-44` recovers exactly the vendor layout. (In v2.10.0 those two moved from positions 42/43 to the end — see [`docs/VENDOR-CSV-DIFFS.md`](docs/VENDOR-CSV-DIFFS.md) §8 if you index columns by number.)
- Rows are written in canonical order — `TIME_STAMP`, then `RT`, then `MSG` (L1-OUT-003) — so records sharing a timestamp may appear in a different order than DDC's capture-order output. Pass `--max-sort-group 1` (with `--no-mux`) for a byte-for-byte vendor diff.
- Standard timestamp tick-to-microsecond conversion requires external calibration.
- SPURIOUS_DATA payload structure is raw words with no further interpretation.

## License

Apache-2.0 — see [LICENSE](LICENSE).
