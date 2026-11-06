# Parquet format

The explicit type mapping is supplied by `profiles/cloudera-impala-hive-v1.toml`, shared
with the SQL generator through tablekit-profile. The bundled profile preserves the contract
below. See [PROFILES.md](PROFILES.md) for validation, custom profiles and supported coercions.

What `tablekit-parquet` writes: a Parquet file Impala reads into the tables `cmm-gen
generate --lang impala` makes (CMM-Code-Generator, `docs/IMPALA.md`), and any other Parquet
reader (pyarrow, pandas, DuckDB, Spark) reads as well.

## Types

Impala has no unsigned integers, so each unsigned type is written as the next wider signed
one. This is the same widening `cmm-gen`'s tables use, so a column's file type and table type
agree.

| tablekit | Parquet physical type | Parquet logical type | Impala |
|---|---|---|---|
| `Bool` | `BOOLEAN` | | `BOOLEAN` |
| `I8` | `INT32` | `INTEGER(8, signed)` | `TINYINT` |
| `I16`, `U8` | `INT32` | `INTEGER(16, signed)` | `SMALLINT` |
| `I32`, `U16` | `INT32` | `INTEGER(32, signed)` | `INT` |
| `I64`, `U32` | `INT64` | `INTEGER(64, signed)` | `BIGINT` |
| `U64` | `FIXED_LEN_BYTE_ARRAY(9)` | `DECIMAL(20, 0)` | `DECIMAL(20,0)` |
| `F32` | `FLOAT` | | `FLOAT` |
| `F64` | `DOUBLE` | | `DOUBLE` |
| `Text` | `BYTE_ARRAY` | `STRING` | `STRING` |

A `u64` is a `DECIMAL(20,0)`, exact for every value: big-endian two's complement in 9 bytes,
the first always zero. `BIGINT` would turn values from 2^63 up negative.

## The file

| | |
|---|---|
| Columns | In the table's order, with its names, which must be unique |
| Nulls | Every column is `OPTIONAL`; a missing value is a null |
| Row groups | One per batch written; one for a table written whole |
| Format version | 1.0, with data pages of version 1 and both the logical type annotations and the legacy ones (`INT_16`, `DECIMAL`, `UTF8`) older readers use |
| Compression | `Snappy` (default), `None`, `Gzip` or `Zstd`; Gzip/Zstandard use codec default levels |
| Dictionary encoding | On (option `dictionary`); see below |
| Data page size | `data_page_bytes`: approximate uncompressed limit, default 1048576; positive |
| Encoder batch | `write_batch_rows`: values per internal encoder batch, default 1024; positive |
| `created_by` | `tablekit` and its version |
| Partition columns | No extra capture columns in the file. Calling tools use the selected profile/order in directory names; see PROFILES.md. |

`testdata/expected/parquet/schema.txt` is the schema a table of every type gives, as the
`parquet` crate prints it, and `crates/tablekit-parquet/tests/round_trip.rs` reads every value
back. Files are also read with pyarrow before each release (README).

## Dictionary encoding and older readers

A column whose values repeat is dictionary-encoded by default, which makes files smaller. The
parquet crate labels such a page `RLE_DICTIONARY`, the name Parquet format 2.0 gave the
encoding, even in a version 1.0 file. Readers of the last several years, Impala on Cloudera
CDP included, read it. A reader that knows only version 1's name, `PLAIN_DICTIONARY` (an old
Impala, as on CDH 5), may refuse the file. If one does, set `dictionary` to `false`: every
value is then written `PLAIN`, which every Parquet reader reads, in somewhat larger files.
The values are the same either way.

## Column names for Impala

Impala matches a file's columns to a table's by position by default
(`PARQUET_FALLBACK_SCHEMA_RESOLUTION=position`), and by name if that is set to `name`. So a
file meant for an Impala table should hold that table's columns, in its order, under its
names. tablekit doesn't know any table; the tool that builds the batch chooses names and
order. FATMOPs-Binary's `export_parquet` uses `cmm-gen`'s: `record_time`, then each field as
a lowercase identifier.

## Writer controls

Compression, dictionary encoding, data page size and encoder batch size belong to
`ParquetOptions`, shared by every caller. Compression names parse through
`ParquetCompression::from_str`: `snappy`, `none`, `gzip`, `zstd`. Unknown names fail.
Gzip uses the Rust flate2 backend; Zstandard uses the zstd codec enabled in the Parquet
crate. Defaults retain the previous writer settings and schema/value contract.

Page size is approximate: the encoder checks boundaries between internal batches, and
an individual value can exceed the limit. Encoder batches are not row groups. Each call
to `write_batch` still writes one row group; gathering batches to a target row-group size
remains roadmap work. These storage settings never change the shared profile's types or
coercions. Selecting a codec does not establish acceptance by a particular cluster version.

Adding `data_page_bytes` and `write_batch_rows` extends the Rust options struct; callers
that construct every field should add them or use `..ParquetOptions::default()`.
