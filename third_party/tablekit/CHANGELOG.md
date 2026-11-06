# Changelog

All notable changes to this repository are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases are tagged following
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). A change to what is written is
called out under **Format**.

## [Unreleased]

### Added -- cell formats and the row writer (tablekit-csv)

- **`CsvOptions::cell_formats`**: a column can be written as upper- or lower-case
  hexadecimal (`CellFormat::HexUpper(w)`, `HexLower(w)`) or with a fixed number of
  decimals (`CellFormat::Fixed(d)`), for tools whose users compare raw machine words and
  measured intervals. Each matches what pandas writes after Python's own `format(v, "04X")`
  or `f"{v:.6f}"`; `testdata/expected/csv/table-formats.csv` is that file, written by pandas
  3.0.6 and NumPy 2.5.3 (`make.py`). A format the column's type cannot take is refused when
  the format is built. Every way in -- a batch, `CsvFormat::row`, the row writer -- writes
  the same bytes for it.
- **`RowWriter`** (`CsvFormat::begin_row`, `CsvWriter::write_row_with`): a row written a cell
  at a time, one method per column named for its type and format (`u16`, `text`,
  `hex::<4>`, `fixed::<6>`, `text_with` for text built from numbers through a
  `TextBuilder`). It writes the bytes `CsvFormat::row` writes for the same values (20,000
  random rows of every type and format, both `na_rep`s), quotes text wherever it needs it,
  and refuses a row with the wrong number of cells, in every build; whether each method
  suits its column is checked in debug builds.
- Measured on aero1553's 21 MB golden 1553 recording (460,575 records, 46 columns: a
  timestamp built from numbers, 32 hex words, a six-decimal interval), rendering only, one
  thread, best of 15 interleaved rounds, three runs, on an ARM (aarch64) host: aero1553's own
  hand-written writer 112.6 ns per record, `RowWriter` 109.0 ns (0.97x), the bytes
  identical; `CsvFormat::row` with the same cell formats 450 ns, since it is the general,
  fully checked way in. Callgrind: 1,787 against 1,876 instructions per record (1.05x).

### Changed

- `CsvOptions` adds `cell_formats`; exhaustive Rust literals must include it or use the
  default update syntax. Nothing written changes for a table without cell formats.
- `True` and `False` are written with two fixed-size stores (`True`/`Fals`, then `e`), so
  the compiler cannot merge them into a variable-length copy -- a `memcpy` call per value,
  which it made of the old form in `CsvFormat::row` once this crate had more code. On its
  own, against `main`, that is 1.9% fewer instructions for the batch writer and 1.3% fewer
  for `CsvFormat::row` (callgrind, 20,000 rows of seven columns, one of them `Bool`).
- **Open before merging:** for a table without cell formats, the batch writer matches that
  baseline (+551 instructions in all), but `CsvFormat::row` runs 3.5% more instructions
  per row (34,170,933 against 33,015,029). `row` itself is unchanged apart from one check,
  which measures at nothing; removing it, keeping `CsvFormat::new` inlinable and moving the
  formatted paths out of line did not change the figure. The two compiled loops differ in
  layout throughout. To be measured on x86 before deciding; the README's rule is that a
  slower change is not merged.

### Documentation

- Reconcile project maps and workflow status with implemented direct ASCII Parquet,
  canonical convert/load routes, source-versus-release boundaries and deferred binary UTC.
  Documentation only; no runtime, dependency, schema or specification change.


## [0.6.1] - 2026-10-29

### Added

- `tablekit-profile::render_into`: append template substitutions into caller-owned capacity.
  Existing `render`, substitution rules, first-key selection and error text are preserved.
  Frozen-parser regression tests cover errors, Unicode, inserted literal braces and reuse.
- CMM-Code-Generator's synthetic wide SQL prototype removes 130944 generation allocation
  events and 3274880 cumulative requested bytes versus its owned-comment implementation.
  Caller measurements and peak-memory limits belong in its SQL comment-buffer report.

### Release scope

- Only `tablekit-profile` advances to 0.6.1. Model/CSV/Parquet remain at 0.6.0 to preserve
  existing writer version metadata. No mappings, profile files, CSV/Parquet bytes or
  external dependencies change. Appending can leave partial output on a template error;
  callers must discard that output. Scratch capacity persists until the caller drops it.

## [0.6.0] - 2026-10-25

### Added

- Ordered partition projection in the shared profile, preserving configured types/comments.
  Profiles can select/reorder known capture fields or declare no partitions; record columns
  and type mappings retain their existing validation.

- Shared Parquet compression selection: Snappy, none, Gzip and Zstandard; strict string
  parsing and explicit positive data-page/encoder-batch controls. Dictionary selection
  remains shared. Defaults preserve the previous schema, values and writer settings.
- Codec round trips check schema, values, nulls and the declared column compression.

### Changed

- `ParquetOptions` adds `data_page_bytes` and `write_batch_rows`; exhaustive Rust literals
  must include these or use the default update syntax.

## [0.5.0] - 2026-10-24

### Added

- `tablekit-profile`: a validated TOML contract shared by SQL generation and the Parquet
  writer. The bundled `cloudera-impala-hive-v1` profile preserves existing schemas, values
  and comments. Missing or lossy mappings and unknown settings are refused.
- Explicit-profile Parquet writer, batch and streaming APIs. The existing APIs use the
  bundled profile. Profile parsing is outside the value-writing loops, which are unchanged.
- Local A/B validation against the previous writer: 500,000 synthetic rows in six columns,
  Snappy and dictionaries enabled, 16 measured encodes per version after warm-up. Median
  wall time was 0.2104 s before and 0.2062 s with profiles; the 9,606,127-byte files were
  identical. This shows no measured regression, not a claim of a speedup. Callgrind could
  not run because WSL access was denied on the validation host.

## [0.4.2] - 2026-10-19

### Changed — the CSV batch loop compiles the same everywhere

- `CsvWriter::write` formats rows in a function of its own (`fill`), kept out of line, with
  the buffer moved out of the writer while it runs. Before, the loop was inlined into each
  caller and compiled worse where the writer was not a local variable: FATMOPs-Binary's
  batched export, which keeps its writer in a closure, ran 3% more instructions writing a
  million rows than when the writer was local. Now it is the same, and the writer itself runs
  fewer: 51.80 G against 53.31 G instructions (callgrind) for the row and batch benchmark
  of 738,433 dump rows, 2.8% fewer. The bytes written are unchanged (every expected-file
  test passes).

## [0.4.1] - 2026-10-18

### Added

- `Text::from_parts(buffer, ends)`: a text column from text already in one buffer, without
  copying it, checked (`None` if an end goes backward, past the buffer, or into a
  character). FATMOPs-Binary hands `cmm-decode`'s decoded text columns to its exports this
  way.

## [0.4.0] - 2026-10-18

Includes what was prepared as 0.3.2 (never released): zmij and faster rows, below.

### Changed (breaking)

- `Values::Text` holds a `Text`: every row's text in one buffer, with where each row ends,
  instead of a `Vec<String>`. A source fills a text column (`Text::push`, `Text::push_hex`)
  without allocating a string per row. `Text` converts from `Vec<String>` and `Vec<&str>`
  and collects from any iterator of strings.
- `write_atomically` is generic over its error, which must take a `TableError`, so a source's
  own error type passes through it.

### Added

- `Source` (a table produced a batch at a time, with its own error type), `BatchWriter`
  (implemented by `CsvWriter` and `ParquetWriter`) and `copy`, which drives one into the
  other with one batch in memory at a time.
- `tablekit_csv::write_source` and `write_source_file`, and the same in `tablekit_parquet`.

### Changed

- Floats are formatted by `zmij` (Victor Zverovich's Żmij algorithm, ported by David
  Tolnay) instead of `ryu`: about twice as fast for doubles (21.6 ms against 44.5 ms for a
  million record times) and 1.35 times for floats. It too takes the even digit on a tie.
  `ryu` is no longer a dependency. zmij is pinned exactly, since a new version could change
  digits; the NumPy corpus test must pass before moving it.
- `CsvWriter` formats each cell straight from its column's values, one dispatch on the
  column's type instead of two through `Value`.
- A row is checked and written in one pass (a refused row is still taken back whole), its
  room is reserved once, and a float zmij already writes in
  NumPy's layout (the common case: `0.000921`, `123.0`) is copied as it is. With sluice
  writing rows a megabyte at a time, `sluice dump --format csv --hex 8` of a 738,433-record
  capture takes 0.473 s (best of nine), against 0.594 s with zmij alone and 0.633 s for
  sluice's hand-written CSV before tablekit.

### Measured against 0.3.0

- **The CSV writer:** a 300,000-row decoded-style table (floats, booleans, integers, `u64`,
  enum text, missing values) is written in 84.5 ms against 415 ms (best of 15, three
  alternating rounds), with 0.69 against 2.90 billion instructions (callgrind). The output is
  byte-identical.
- **sluice `dump --format csv --hex 8`** (738,433 records): a median of 0.474 s against
  0.629 s for sluice's own CSV before tablekit; 134.6 against 243.2 million instructions on
  a 40 MiB capture.

### Format

- Unchanged: the NumPy corpus, the pandas expected files, and a 738,433-record sluice dump
  are byte-identical to 0.3.1's. `created_by` is `tablekit 0.4.0`.

## [0.3.1] - 2026-10-18

### Changed

- CSV numbers are formatted by `ryu` (floats) and `itoa` (integers) on the stack, instead of
  through Rust's formatting machinery: a sluice dump row is formatted about as fast as
  sluice's own hand-written CSV was (0.640 s against 0.648 s for a 738,433-record capture,
  best of seven), where 0.3.0 took about half as long again. Ryu also takes the even digit
  when two shortest forms are equally close, as NumPy does, so 0.3.0's second formatting
  pass for that case is gone.

### Format

- Unchanged: the same bytes for every value, checked against the NumPy corpus and against
  0.3.0's output for the capture above.

## [0.3.0] - 2026-10-18

### Changed (breaking)

- `CsvOptions::bom` is replaced by `CsvOptions::encoding`, an `Encoding`: `Utf8` (the
  default) or `Utf8Sig`, parsed from Python's names (`"utf-8"`, `"utf-8-sig"`) as pandas'
  `encoding` takes them. Other encodings can be added without another interface change.

### Added

- `tablekit::Value`, one borrowed value of a row, and `Column::value(row)`.
- `CsvFormat`: formats the header and each row (a slice of `Value`s, checked against the
  columns) into the caller's buffer, for a tool that writes each record as it reads it with
  no allocation per row. `CsvWriter` is built on it, and gains `write_row`.
- `TableError::RowLength` and `TableError::ValueType`.

### Format

- Unchanged. The expected file `table-bom.csv` is now `table-utf-8-sig.csv`.

## [0.2.0] - 2026-10-18

### Added

- `ParquetOptions::dictionary` (default `true`, as before): `false` writes every value
  `PLAIN`, with no dictionary pages, for a reader that refuses the `RLE_DICTIONARY` label the
  parquet crate gives dictionary pages even in a version 1.0 file (an old Impala).

### Format

- Unchanged by default. `created_by` is now `tablekit 0.2.0`.

## [0.1.0] - 2026-10-18

The first release: single-threaded.

### Added

- `tablekit`: the table model. `Column` (a name, `Values` of one `DataType`, which rows are
  missing), `Batch` (equally long columns), `TableError`, and `write_atomically` (a file
  written whole or not at all).
- `tablekit-csv`: `write_file`, `write_to` and `CsvWriter` (a table in several batches).
  `CsvOptions`: `na_rep`, `line_ending`, `bom`.
- `tablekit-parquet`: `write_file`, `write_to` and `ParquetWriter` (a row group per batch).
  `ParquetOptions`: `compression`.
- `docs/ARCHITECTURE.md`, `docs/CSV-FORMAT.md`, `docs/PARQUET.md`.

### Format

- CSV is what pandas 3 writes with `to_csv(path, index=False)` on Windows: CRLF, no
  byte-order mark, `True`/`False`, empty for a missing value or NaN, minimal quoting, and
  floats as NumPy writes them. Checked byte for byte against files pandas wrote, and against
  about 16,000 floats NumPy formatted. Whole numbers stay whole when some are missing, where
  pandas would write `5.0`.
- Parquet has Impala's types: unsigned integers widened to the next signed type, `u64` as
  `DECIMAL(20,0)`, every column optional, Snappy compression.
