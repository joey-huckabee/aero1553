# Roadmap

What comes next, in order. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the design
behind each step.

**Performance only improves** (owner, 2026-10-18): a change to a pipeline or a hot path is
measured against what it replaces (A against B on the same input, byte-identical output,
wall time and callgrind instruction counts) and is merged only if it is no slower.

## Current implementation and ownership

The [project map](docs/PROJECT-MAP.md) records crate ownership, prerequisites and the
2026-10-30 reconciliation of workflow and release status. Older sketches below
include completed work and future command examples; use that status ledger before starting
an item. Plans have no delivery dates.

## Done

- 0.1.0: the table model, CSV as pandas writes it, Parquet with Impala's types.
- 0.3: sluice's CSV through tablekit (`CsvFormat`, a row at a time); `encoding` replaces
  `bom`.
- 0.4.0: text columns in one buffer; `Source`, `BatchWriter` and the serial driver `copy`;
  floats by zmij. The CSV writer is 4.9 times faster than 0.3.0's.
- 0.5.0: `tablekit-profile`, explicit SQL/Parquet type mappings and SQL comment templates
  in TOML, shared by CMM SQL generation and Parquet writers; cross-output agreement tests
  live in CMM-Code-Generator 0.3.0.

## 1. Concurrent export (`tablekit-run`)

After the first release. **Measure first:** time each stage per label (read, decode, build
the table, write) on a large capture, through FATMOPs-Binary's `perf/`. Then:

- **A job is a source and a writer.** The engine knows only jobs; each tool supplies its
  sources (`Source`).
- **One job per worker**, up to the core count, largest first. `std::thread::scope`, no new
  dependency.
- **Without an index, one pass for all labels**: a single scan routes each record to its
  label's job.
- **Stopping**: a shared flag, set by Ctrl-C or the first error, checked between batches.
  Finished files stay; unfinished ones are removed.
- **The same bytes either way**: a test writes with one worker and with many and compares.
- **Evaluated with and without threads** (owner, 2026-10-18): starting workers, handing
  batches between them and keeping the output in order cost something, and for a small
  table or a single label that can outweigh the gain. Time the serial and the concurrent
  engine side by side, from tiny tables to large ones, measure the fixed cost of starting the
  workers on its own, and use the concurrent one only where it wins (possibly above a size
  threshold). sluice's threaded dump is held to the same test.

## 2. Parquet row groups from small batches

`ParquetWriter` writes a row group per batch. A source that gives small batches therefore
makes small row groups, which Impala reads slowly. Gather batches up to a target size (rows
or bytes) before writing a group, so any source makes good files.

## 3. Fewer copies

FATMOPs-Binary decodes a message into its own columns and then copies them into a tablekit
batch. Decoding straight into tablekit columns would remove that copy. Column-at-a-time
decoding is already implemented in `cmm-decode`; direct buffer ownership/transfer into
tablekit still needs measurement and design.

## 4. Impala's types in one place: done

Implemented in tablekit 0.5.0 / CMM-Code-Generator 0.3.0. `tablekit-profile` loads the same
explicit TOML mappings for the Parquet writer and SQL generator, including unsigned widening
and exact uint64 decimal storage. CMM-Code-Generator tests SQL/Parquet schema agreement.
See [docs/PROFILES.md](docs/PROFILES.md); extend that contract rather than adding a new map.

## 5. More encodings, when asked

`encoding` takes Python's names, so another encoding (`utf-16`, `latin-1`) can be added
without changing the interface, when a user needs one.

## 6. Shared Parquet storage controls: implemented in 0.6.0

Snappy/none/Gzip/Zstandard, dictionary encoding, data-page bytes and encoder-batch rows
are owned by `tablekit-parquet`, used by sluice's convert/load routes. Type and comment
policy stays in the shared target profile. See [docs/PARQUET.md](docs/PARQUET.md).
Codec-specific compression levels, dictionary-page sizing, target row-group gathering
and real-cluster acceptance remain follow-up work; do not infer them from these controls.

## 7. Reusable profile rendering

Implemented: append-only `tablekit_profile::render_into`, preserving the owned renderer
and frozen substitution/error policy. CMM-Code-Generator reuses scratch capacity per
SQL table. This adds no profile settings, schema change, telemetry or concurrency.
Measure caller-specific timings and retained long-comment capacity alongside allocation
savings; CMM-Code-Generator owns its SQL benchmark report. Further count formatting or
compiled-template work requires separate evidence and exact-output tests.
