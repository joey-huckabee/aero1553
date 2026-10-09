# Changelog

All notable changes to Aero1553 (named MIE-Decoder through v3.0.0) are
documented in this file. Entries before the rename keep the names that
were current when they shipped.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versioning model: **a release is a joint cut** — the Rust crate, the
Python package and the C++ implementation all ship from a single
repository tag at one version number, and a CI gate fails the build if
they disagree. (C++ joined the joint cut at v2.13.0; releases before
that were Rust + Python.) Subsequent releases may diverge in version via
impl-prefixed tags (`rust-vX.Y.Z`, `python-vX.Y.Z`, `cpp-vX.Y.Z`); the
cross-implementation conformance contract (byte-exact CSV equivalence on
shared behavior) holds at any compatible version pair. See
`docs/MAINTAINER-GUIDE.md` §11 for the full release workflow.

## [Unreleased]

### Fixed

- **C++ rounds a calibrated Standard timestamp the same way as Rust and
  Python** (L2-DEC-017). It computed `floor(x + 0.5)`, where the addition
  rounds before `floor` sees it, so two kinds of input came out 1 µs high:
  `0.49999999999999994` µs (1 tick at `--standard-tick-rate-hz
  2000000.0000000002`), and every odd count from 2^52 to 2^53 µs, where
  doubles are spaced 1 apart -- reached at tick rates below about 0.95 Hz,
  where `0xFFFFFFFF` ticks at `0.9` Hz read `DELTA` `1.111112` instead of
  `1.111111`. It now uses `std::round`. Two conformance cases pin both
  inputs across all three implementations.
- **An errored record with no room for its Error Word is rejected, not
  decoded with its Command Word as the error code** (Rust and C++, and so
  the Python package; L2-ERR-002 amended). The Error Word is an errored
  record's last word, but sync validation admits a record that ends at its
  Command Word -- 5 words with an IRIG timestamp, 4 with a Standard one --
  and for that record "the last word" is the Command Word. Its value came
  out as `ERROR_CODE`; when it happened to be a known `0x01xx` code
  (`0x0120`, RT0 SA9 R, reads as "no response") the row passed without a
  WARN, strict mode included. Such a record is now malformed, handled like
  a structural-invariant failure: `--strict` exits 1 with a `PayloadError`
  naming the missing Error Word; lenient mode skips it with a WARN, and a
  `SPURIOUS_DATA` record after it is standalone (`2001`). **Behaviour
  change:** such a file decoded under `--strict` exits 1 where it exited 0,
  and loses a row in lenient mode.
- **The Rust non-monotonic-timestamp WARN is one well-formed line again**
  (Rust, and so the Python package; L2-RDR-017 amended). A refactor in
  v2.13.0 joined its three-line string literal without the `\` line
  continuations, so the message carried two runs of 18 spaces
  (`0x000F0B00:                  prev_us=...`). C++ was unaffected. Every
  test matched only the words "non-monotonic timestamp"; the requirement now
  pins the whole line, and the conformance case checks it for all three
  implementations.
- **An output name ending in `.` gets the same `_errors` file name from
  every implementation** (Rust, and so the Python package; L2-ERR-008
  clarified). `-o o. --separate-errors` wrote `o_errors` from Rust,
  `o_errors.` from C++, and Python's `error_path_for` claimed `o._errors` --
  `o_errors.` on Python 3.14, whose `pathlib` changed how it splits a
  trailing dot. All three now follow the requirement's string rule:
  `_errors` goes before the final `.`, so `o.` -> `o_errors.`, and
  `error_path_for` takes its answer from the writer. On Windows nothing
  visible changes: Win32 strips the trailing dot, giving `o` and `o_errors`
  as before. **Behaviour change** on POSIX for such names: the errors file
  is `o_errors.`, where Rust wrote `o_errors`.
- **C++ `dump` names a file with a backslash in it correctly on Linux.**
  The report's `File:` line split the path on `/` and `\` on every
  platform, so a recording named `odd\name.mie` was reported as
  `File: name.mie` on POSIX, where a backslash is an ordinary filename
  character and Rust and Python print the whole name. It now takes the name
  from the platform layer's `path_filename`, and `--glob`, which already
  asked the platform layer what a separator is but kept its own copy of the
  split, uses the same `path_filename` / `path_parent` pair -- so one rule
  decides where a C++ path's file name begins. Windows is unchanged.

### Maintenance

- **`cargo-semver-checks` runs again**, against `v4.0.0`. It was disabled
  from the rename until that tag existed, because it matches the baseline by
  package name and `v3.0.0` has no `aero1553`.

## [4.0.0] — 2026-10-04

### Changed — BREAKING

- **The project is renamed from MIE-Decoder to Aero1553**, aligning it with
  the companion Aero1394 project. The GitHub repository is now
  `joey-huckabee/aero1553`; GitHub redirects the old URL.

  | Surface | Was | Now |
  |---|---|---|
  | CLI command (Rust, Python, C++) | `mie-decoder` | `aero1553` |
  | Rust crate / import path | `mie-decoder` / `use mie_decoder::…` | `aero1553` / `use aero1553::…` |
  | Python distribution / import | `mie-decoder` / `import mie_decoder` | `aero1553` / `import aero1553` |
  | Python base exception | `MieDecoderError` | `Aero1553Error` |
  | C++ CMake project / targets | `mie_decoder`, `mie_decoder_lib`, `mie_decoder_tests` | `aero1553`, `aero1553_lib`, `aero1553_tests` |
  | Atomic-write temp file | `<dest>.mie-decoder.tmp.…` | `<dest>.aero1553.tmp.…` |
  | Log module tags | `[mie_decoder::reader]` | `[aero1553::reader]` |
  | `--version` output | `mie-decoder 3.0.0` | `aero1553 <version>` |

  Update scripts that invoke `mie-decoder`, code that imports `mie_decoder`
  or catches `MieDecoderError`, and anything that parses log tags or looks
  for leftover `.mie-decoder.tmp.*` files.

- **The Python package's command line is the Rust CLI.** `aero1553` (the
  console script), `python -m aero1553` and `aero1553.cli.main()` now run
  the Rust implementation in-process through the package's compiled
  extension, so flags, help text, log lines, exit codes and CSV bytes are
  identical to the Rust binary's by construction. A whole-file decode is
  about 22x faster (`count` over 100x). Two consequences for code that
  drives the CLI from Python:

  | Was | Now |
  |---|---|
  | `aero1553.cli.build_parser()` returned an `argparse` parser | **Removed.** The command line has one parser, in Rust. Run a command line with `aero1553.cli.main(argv)` and read its exit status. |
  | A usage error raised `SystemExit` | `main()` **returns** the status (`EXIT_USAGE`, 4); `--help` / `--version` return `EXIT_OK` |
  | Output could be captured by swapping `sys.stdout` (or pytest's `capsys`) | The CLI writes to the stdout / stderr **file descriptors**, as the binary does. Capture there (pytest's `capfd`, or a subprocess). Log lines arrive on stderr, not through Python's `logging`. |

  One parser also retires the three places the Python CLI used to differ
  from Rust and C++, all inherited from `argparse`. Each is now the same in
  every implementation on every supported Python, and L2-CLI-015/016/017
  bind them (eight new conformance cases):

  - number-leading values such as `--mux-delimiter -5e3`, `-0x5` or `-1a`
    are values (they were usage errors on Python 3.10–3.13);
  - `--` before the subcommand (`-- decode rec.mie`) works (it needed
    Python 3.12+), and a trailing `--` after a flag's value is a no-op (it
    was a usage error);
  - `--max-sort-group abc --help` prints help and exits 0 (it exited 4).

  `main()` and `main_cli()` and the `EXIT_*` constants are unchanged.

- **The Python reader is the Rust reader.** `aero1553.MieFileReader` keeps its
  constructor, properties and behaviour, and iterating it is about 29x
  faster (500,000 records: 28,601 -> 835,511 records/s on the reference
  machine). The records it yields -- `MieMessage` and the `TypeWord`,
  `CommandWord`, `IrigTimestamp` and `StandardTimestamp` values in them --
  are compiled classes with the same fields, properties, constructors,
  equality, hashing, repr, pickling and `with_delta()`; nested values are
  built only when a field is read. The enums are unchanged, still `IntEnum`
  (`msg.bus is Bus.A`). Decoder log lines still reach Python `logging` under
  the same logger names (`aero1553.reader`), and errors are the same
  `aero1553.exceptions` classes with the same messages and attributes.

  | Was | Now |
  |---|---|
  | The record types were frozen dataclasses | **They are not dataclasses**: `dataclasses.replace` / `asdict` / `fields` do not apply. Use `copy.replace(msg, delta=...)` (Python 3.13+) or `msg.__replace__(...)`, and read fields by name. |
  | `aero1553.sync.is_homogeneous_payload`, `diagnose_header_scan_failure` and `HOMOGENEITY_SAMPLE_RECORDS` | **Removed.** They were the pure-Python reader's header-scan helpers and had no other caller; the reader's own checks (L2-SYN-018, L2-RDR-004) run in Rust and behave as before. |
  `main_cli()` (the console-script entry point) now restores the default
  SIGINT handler, so Ctrl-C ends a long decode immediately, as it ends the
  binary.

  **Unchanged**: names that describe the MIE *file format* rather than the
  product — `MieFileReader`, `MieMessage`, `MieError`, the `Mie*Error`
  subclasses, the C++ `mie::` namespace and `include/mie/` headers, and the
  `MIE_FUZZ_*` environment variables. CSV output is byte-identical.

- **The rest of the Python package is the Rust crate too.** Every
  user-level module keeps its public API and now runs the core crate's code
  through the compiled extension, so the library and the CLI cannot disagree:

  | Module | Now runs |
  |---|---|
  | `aero1553.filters.apply_filters`, `aero1553.order.order_rows` | `filter.rs`, `order.rs` |
  | `aero1553.writer` (`write_csv`, `write_csv_split`, `message_to_row`, ...) | `writer.rs` -- the CLI's CSV bytes, the same atomic commit, `.partial` and `--no-clobber` rules |
  | `aero1553.merge` (`merge_readers`, `read_manifest`, `glob_match`, `expand_glob`) | `merge.rs` |
  | `aero1553.delta.DeltaTracker` | `delta.rs` |
  | `aero1553.dump` (`hex_dump_raw`, `hex_dump_records`) | `dump.rs` |
  | `aero1553.config.load_config`, `parse_utc_offset` | `config.rs`; `DecoderConfig` / `FilterConfig` are still Python dataclasses |

  A chain `reader -> apply_filters -> order_rows -> write_csv` (or a merge
  feeding it) never leaves Rust: each stage returns a native `RecordIterator`
  the next stage takes over. Any Python iterable of `MieMessage` is still
  accepted, and an exception it raises reaches the caller unchanged. What a
  caller can observe differently:

  | Was | Now |
  |---|---|
  | `aero1553.decode` and `aero1553.sync` (the pure-Python word decoders and sync helpers) | **Removed.** Nothing called them once the reader became Rust's. Their defaults are on the extension (`DEFAULT_DETECT_RECORDS`, `DEFAULT_LOOKAHEAD_RECORDS`, `DEFAULT_MUX_*`) and remain `MieFileReader`'s parameter defaults. |
  | `aero1553.models.ByteSource` | **Removed** with the helpers it typed. |
  | Config rejection messages, e.g. `Invalid [decode] strict: expected boolean, got str ('yes')` | The CLI's wording, e.g. `[decode] strict must be a boolean`. Exception classes are unchanged, except that a duplicate key or re-declared section raises `ValueError` rather than its `tomllib` subclass `TOMLDecodeError`. |
  | `message_to_row(msg)` listed `WD01`..`WD32` last | Keys follow CSV column order. Dict equality is unaffected. |
  | A calendar-rendering refusal named the record, `(record at offset 0x..)`, for freerun / Standard records | The writer's message, without the offset. |
  | A negative `start_offset` / `length` / `max_records` to the dump produced a meaningless report | `ValueError` naming the argument. |

  **The Python package now declares no runtime dependency.** `tomli` served
  only the Python 3.10 TOML path, and the TOML loader is Rust's.

### Changed

- **`--no-clobber --allow-partial` refuses a run when a `.partial` file is
  already there** (all three implementations; L2-WRT-017 amended). The
  pre-flight now checks every file the options could write -- the `-o` file,
  its `_errors` file with `--separate-errors`, and the `.partial` of each with
  `--allow-partial` -- whether or not this run would write it. Until now a
  stale `.partial` was deliberately not checked, so the run decoded the whole
  file and only failed at the commit. **Behaviour change:** a pipeline that
  runs `--no-clobber --allow-partial` into a location holding an old
  `.partial` now exits 1 before decoding. Without `--no-clobber` nothing
  changes: a file this run does not write, such as an earlier run's
  `_errors.csv`, is left alone with no message (L2-WRT-017 now says so).
- **`max_sort_group` defaults to 65536, and `--max-sort-group 1` is silent**
  (all three implementations; L2-WRT-022 amended, `docs/DATAFLOW.md` decision
  4). The cap on one buffered run of same-`TIME_STAMP` records rises from
  4096 to 65536 (about 10 MB worst case); only a recording with a broken clock
  reaches it -- the longest real run measured is 7 records. `--max-sort-group
  1`, the documented way to get vendor capture order, no longer WARNs once per
  record: on a 780,000-record file that flood made the decode 40x slower. A
  run longer than the cap still WARNs once per cap-sized chunk, which
  L2-WRT-022 now states (it had said once per run, which the code never did),
  and the WARN now says the rest of the run is gathered afresh. The merge's
  `max_collapse_survivors` stays at 4096 on purpose: that cap costs time per
  record rather than memory, and at 65536 a broken-clock merge under
  `--collapse-duplicates` ran 13x slower.
- **The Python package is built with maturin and developed with uv; Poetry
  is gone.** This is the groundwork for the Python package becoming a PyO3
  binding over the Rust decoder: it now contains a compiled extension,
  `aero1553._native` (built from `python/native/`), so **installing from
  source needs a Rust toolchain**. The build produces one `cp310-abi3`
  wheel per platform that serves every CPython from 3.10 up. For
  contributors: `poetry.lock` is replaced by `uv.lock`, the dev tools are a
  PEP 735 `[dependency-groups]` table, and every `poetry -C python …`
  command becomes `uv --directory python …` (same working directory, same
  relative paths). CI installs with `uv sync --locked`.
- **The Python wheel and sdist now include the Apache-2.0 license text.**
  They previously carried only the SPDX identifier: the license file sits at
  the repository root, outside the Python project. `python/LICENSE` is a
  copy, and `scripts/repo-hygiene.sh` keeps it identical to the original.
- The Python CLI's startup log line now reads `aero1553 v<version>`, the
  same as the Rust CLI's. It previously said `MIE-Decoder v<version>` while
  Rust said `mie-decoder v<version>`.
- **The architecture diagrams are hand-written SVG, and PlantUML is gone.**
  `docs/diagrams/{class,component,dataflow}.svg` are now their own source;
  the `.puml` files and the CI `diagrams` job are removed. The redraw also
  corrects content that had gone stale: all three implementations are shown
  (C++ was missing), the temp file is `<dest>.aero1553.tmp.<pid>.…`, several
  output-safety requirement IDs were wrong, the class diagram's enums and
  fields now match the code, and the data flow gains the empty-recording and
  `0x0000` terminator paths and drops an fsync step no implementation has.

### Added

- **A Python library guide**, [`docs/PYTHON-GUIDE.md`](docs/PYTHON-GUIDE.md):
  reading and the record, reading options, filtering and canonical order,
  writing CSV (to a file or stream, with separate errors, no-clobber, calendar
  dates), merging recorders, choosing a table route, NumPy, pandas,
  dataclasses, time with and without a year, configuration files, the hex
  dump, logging, errors, running the CLI from Python, performance and
  supported platforms. It is executable documentation: every example runs in
  the test suite against the golden recordings, and every output it shows is
  checked exactly, so the guide cannot drift from the library.
- **Python: decoded records as tables, for NumPy, pandas and dataclasses.**
  `aero1553.columns(stream)` turns a whole record stream -- a reader, a
  filtered or ordered stream, or a merge -- into one typed buffer per field,
  built in Rust with no Python object per record; `np.asarray(...)` and
  `pd.DataFrame(...)` wrap the buffers without copying. `MieMessage.to_dict()`
  returns one record's fields as plain values, so
  `MyRow(**msg.to_dict(fields=...))` fills a user-defined dataclass. One
  schema (`aero1553.table.FIELDS`) serves both; `fields=` selects a subset.
  Time uses the decoder's own rules: `time_us` follows the DELTA rule
  (Standard records need `standard_tick_rate_hz`), and a `datetime` field
  appears when `year=` is given, as a NumPy `datetime64` column or an aware
  `datetime`. Without a year there is no `datetime` field and no error --
  `day_of_year` and `time_of_day_us` are always there. Records with no date
  (freerun, Standard, day 366 of a common year) are `NaT` / `None`. Measured
  on 500,000 records, `columns()` into pandas is about 7x faster than
  building a DataFrame from per-record dicts and uses about a third of the
  memory; the performance suite (`perf/`) compares every route. NumPy and
  pandas are not dependencies of the package.
- Rust: `log::set_sink` / `LogSink` route log lines to an embedder (the
  Python binding forwards them to `logging`); `log::with_stderr` pins a scope
  to stderr, which is how the CLI keeps its log output where it was.
- Rust: `merge::MergedRecordIter::new_detached` builds a merge that outlives
  its readers, sharing each reader's mapping as `iter_detached` does.
- Rust: `delta` is a public module (`DeltaTracker`, `DeltaOutcome`,
  `delta_key`) and `config::parse_utc_offset` is public, so an embedder uses
  the one definition of the DELTA key and of the UTC-offset grammar.
- Rust: `MieFileReader::iter_detached()` returns a `RecordIter<'static>` --
  the same records as `iter()`, from an iterator that shares the file mapping
  instead of borrowing the reader, so it can outlive it. For embedders whose
  iterator lifetime is not a Rust scope (the Python binding keeps one inside
  a Python object). No cost per record: decode throughput is unchanged.
- Rust: `cli::run_to_code(argv) -> u8`, the exit status as a number.
  `cli::run` returns `std::process::ExitCode`, which cannot be read back.
- **A performance suite for the Rust and Python implementations** (`perf/`,
  run on every pull request by `.github/workflows/perf.yml`). Every case --
  the Rust library, the Python library, both CLIs as processes, and each
  route into NumPy, pandas and dataclasses -- is timed on a generated golden
  recording (`tests/golden/`, pinned by hash) and its output checked against
  the pinned CSV hash, row count and RT checksum. The report gives every
  run's time, per-phase times, each library's import time (measured in a
  fresh interpreter) and a total that adds it back, records/s and peak
  memory. The build fails on a wrong answer or on a gate: a ratio between two
  cases of the same run (for example the Python library decode against the
  Rust library's), which runner noise cannot trip the way an absolute time
  limit would. It replaces the ad-hoc `python/benchmarks/` scripts.

### Fixed

- **The Python library writes LF to a Windows text stream, as the CLI does**
  (new L3-PY-023). `write_csv` to `sys.stdout` or to a file opened with
  `open(path, "w")`, and the `aero1553.dump` hex dumps, handed the stream
  text, and on Windows its newline translation turned every `\n` into `\r\n`
  -- against L2-WRT-012, while the CLI wrote LF. A stream with a binary layer
  is now given the bytes through it, so the output is the CLI's bytes
  whatever the stream's newline and encoding settings; one without (an
  `io.StringIO`, a notebook's output) is given text as before.
- **The Python library's `write_csv` to a stream no longer hides a sync loss
  behind `allow_partial`** (new L3-PY-022). Writing to `sys.stdout` or any
  other stream with `allow_partial=True`, an unrecoverable sync loss stopped
  the write and returned the `WriteOutcome` of a complete decode, so the caller
  could not tell the recording had been cut short. A stream cannot hold a
  `.partial`, so `allow_partial` no longer applies there: the rows decoded
  before the loss are written and `MieUnrecoverableSyncLossError` is raised,
  as the CLIs' stdout output exits 3 -- and as a merge that left inputs out
  was already raised on the same path.
- **The sync-loss advice no longer tells you to pass a flag you passed**
  (all three implementations). With `--allow-partial` and output to stdout
  the decode exits 3, correctly, but the error and the exit-class line both
  said "Pass --allow-partial to keep what was decoded". They now say to pass
  it *with an output file (-o)*, which is true either way and names what was
  missing.
- **Ctrl-C stops the Python library's `write_csv`, `write_csv_split` and
  `columns`, and an interrupted write commits nothing** (new L3-PY-021).
  These run in the compiled extension with the GIL released, and CPython
  handles signals only between bytecodes, so `KeyboardInterrupt` waited for
  the whole call -- by which time a file write had already renamed its output
  over the destination, leaving a complete-looking file from a cancelled run.
  The record stream now checks for a pending signal every 1024 records; on
  one it stops, the writer abandons its temp file, and the
  `KeyboardInterrupt` is raised.
- **A file name that is not valid Unicode can be named on the command line**
  (all three implementations; new L2-CLI-021). The Rust binary collected
  `std::env::args()`, which panics on the first argument that is not UTF-8
  (exit 101); the Python CLI raised an uncaught `UnicodeEncodeError` on the
  same argument; and the C++ CLI on Windows turned an unpaired surrogate into
  U+FFFD and so looked for a different file. Rust now parses `args_os`
  (`cli::run_os`, with `run` kept for `String` arguments), Python hands the
  CLI OS strings, and C++ on Windows carries paths as WTF-8. An input,
  `--output`, `--config` or `--manifest` may now be any name the platform
  allows and is used as given; any other value that is not valid Unicode is a
  usage error (exit 4), quoted in escaped ASCII.
- **The C++ CLI on Windows no longer refuses an output at a drive root as
  "the same file as the input"**. The parent of `C:\a.mie` came back as `C:`,
  which Windows reads as the current directory on drive C, not its root; so
  when the output did not exist yet, the input/output identity check resolved
  `-o C:\a.mie` to `<working directory>\a.mie` and refused the run whenever
  the input was `a.mie` in the working directory on that drive. The parent of
  a drive-root path now keeps its separator (`C:\`), as the parent of `/x` on
  POSIX is `/`. Rust and Python were not affected.
- **The C++ CLI accepts the `0x...` form where Rust does** (L2-CLI-020).
  `dump --offset/--length/--records` are documented to take hexadecimal, and
  Rust also takes it on the RT and subaddress filters (`--include-rts 0x0F`);
  the C++ CLI refused both. It now takes `0x` or `0X` followed by hex digits
  on exactly those flags. Rust, for its part, no longer accepts a sign after
  the prefix: `from_str_radix` read `0x+1` as 1.
- **The C++ `--standard-tick-rate-hz` refuses `inf`, `1e400` and
  hexadecimal values** (L2-CLI-020). It parsed with `strtod`, which also
  reads hexadecimal floats, and checked only that the result was positive,
  so `0x10`, `0x1p4`, `inf`, `Infinity` and `1e400` (which overflows to
  infinity) were valid rates in C++ and usage errors in Rust. A grammar gate
  matching Rust's `f64` parser now sits in front of `strtod`, and a
  non-finite rate is refused as L2-CLI-012 requires.
- **Numeric flag values follow one whitespace rule in every implementation**
  (new L2-CLI-020). Surrounding ASCII whitespace is ignored and nothing else
  is: `"5 "` was accepted by Rust and refused by C++, `" 2026"` was accepted
  by C++ and refused by Rust's `--year` (the one Rust numeric flag that
  trimmed nothing), and Rust's other flags trimmed any Unicode whitespace.
  Now every numeric flag, and each element of a numeric list such as
  `--include-rts`, trims space, tab, `\n`, `\v`, `\f` and `\r` in both, and
  refuses whitespace inside the number or a non-ASCII space such as U+00A0
  around it. **Behaviour change:** Rust now refuses a value padded with a
  non-ASCII space, and `--year` accepts surrounding whitespace.
- **The C++ implementation refuses an integer too large for its type**
  instead of clamping it. The CLI and the TOML loader parsed integers with
  `strtoll`, which saturates on overflow and reports it only through `errno`,
  which neither checked. On the values with no upper bound --
  `--collapse-window-us` / `[merge] collapse_window_us`, `--mux-field` /
  `[mux] field`, and `dump --offset/--length/--records` -- a value such as
  `99999999999999999999` was accepted as 9223372036854775807 where Rust and
  Python reject it (exit 4 on the CLI, 5 in a config). Both now use one
  overflow-checked parser with Rust's grammar and ranges: signed 64-bit, and
  unsigned 64-bit for `dump`, whose offsets Rust takes up to 2^64 - 1. The
  config fuzzer's palette gains the 64-bit boundary literals and the two keys
  it lacked, so this class of divergence is caught automatically.
- **A recording cut off inside its last record is no longer reported as a
  sync loss** (all three implementations; L2-RDR-002 amended). Lenient mode
  logged `sync lost ... scanning forward` for a truncated final record of 8
  bytes or more, counted it as a sync recovery and classed the run
  `partial-recovered`, though nothing was lost or recovered. It now gives
  every truncated tail the same WARN (`N trailing byte(s) at 0x... do not
  hold a complete record (Type Word 0x....); stopping`), counts no sync loss,
  and classes the run `complete`. The forward scan still runs first, so a
  Type Word corrupted near the end with whole records behind it is still a
  sync loss and still recovered. Exit codes and CSV output are unchanged;
  only the WARN text and the `decode exit class` INFO line differ.
- **`--strict` reports a recording that ends 2-7 bytes into a record**
  (all three implementations; L2-RDR-002 and L2-RDR-003 amended). The
  reader stopped silently whenever fewer than 8 bytes -- the smallest
  record -- remained, before reading the Type Word, so such a tail passed
  strict mode as a clean end. Strict mode now classifies it from its Type
  Word like a longer tail: `RecordTruncated` (exit 1), or `UnknownTypeWord`
  / `InvalidTypeWord` for an implausible one. Lenient mode keeps every whole
  record as before and now logs a WARN naming the dropped bytes, where it
  said nothing. A trailing `00 00` terminator and a single stray byte still
  end the decode cleanly. **Behaviour change:** `--strict` exits 1 on such a
  file, where it exited 0.
- **A `SPURIOUS_DATA` record with Type Word bit 14 set is labelled
  `SPURIOUS`, not `ERROR`** (all three implementations; L2-ERR-010
  amended). Such a record was already decoded, coded (`2000`/`2001`) and
  timed as spurious, but the `ERROR` column tested bit 14 first, so the row
  read `ERROR` beside a decoder-assigned code -- where an `ERROR` row
  promises a DDC hardware code. The message type now decides the label. The
  raw bit is still reported by the library's `is_error` and by `dump`.
- **`dump` no longer decodes a `SPURIOUS_DATA` record's leftover words as a
  Command Word and an Error Word** (all three implementations). It printed
  `Cmd: 0xAAAA -> RT21 SA21 ...` for every spurious record -- an RT and
  subaddress that were not there -- and, with bit 14 set, an `Error:` line
  reading the last leftover word as an unknown DDC code. A spurious record
  now gets `Data: N leftover word(s); SPURIOUS_DATA carries no Command Word`.
- **A merge in which every input fails no longer reports success under
  `--allow-partial`** (all three implementations; L2-MRG-004 amended).
  `--allow-partial` keeps what a merge could decode; when every input failed
  at open or on its first record there was nothing to keep, yet Rust and
  Python exited `0` with an empty `.partial` -- as did C++ for non-MIE inputs,
  while it exited `2` for missing ones. Such a run now fails exactly as it
  would without the flag: with the **first** failing input's own error and
  exit code (`1` for a missing or unreadable file, `2` for one that is not a
  recording), and no output file. A valid empty recording counts as an input
  that succeeded. **Behaviour change:** a batch script that ran a merge of
  only bad inputs with `--allow-partial` now sees exit `1` or `2` instead of
  `0`.
- **A partial merge names what ended it** (all three implementations;
  L2-MRG-004 amended). When `--allow-partial` left out a merge input that
  could not be opened or whose first record could not be read, the closing
  WARN reported "unrecoverable sync loss at 0x0 after 0 recovery attempt(s)"
  -- a sync loss that never occurred -- and the Python library raised
  `MieUnrecoverableSyncLossError`. It now reports `1 of 2 merge inputs could
  not be read and was left out (see the per-input warnings above)`, adding how
  many inputs were truncated mid-file, through a new error:
  `MieError::MergeInputsDropped` (Rust), `MieMergeInputsDroppedError`
  (Python, a `MieRecordError` like the class it replaces here) and
  `KIND_MERGE_INPUTS_DROPPED` (C++). Exit codes and the `.partial` output are
  unchanged; a merge whose inputs were only truncated mid-file still ends in
  the real sync loss. **API change:** the new `MieError` variant breaks an
  exhaustive `match` on `MieError` or `MieErrorKind` in Rust.
- **Filters and `--collapse-duplicates` treat an error and its `SPURIOUS_DATA`
  continuation as one unit** (all three implementations; new L2-FLT-003,
  L2-FLT-001 and L2-MRG-007 amended). A continuation has no RT or subaddress,
  so it used to be judged as if it stood alone: `--exclude-rts 15` removed
  RT15's error rows but kept their `0x2000` continuations, each now under an
  unrelated row, and `--collapse-duplicates` could collapse an error while
  keeping its continuation, or the reverse. A continuation now shares its
  error's fate: the RT, subaddress and bus filters and a type exclusion naming
  the error's type keep or remove both, and collapse removes both or neither.
  **Behaviour change:** `--include-rts` / `--include-subaddresses` /
  `--include-buses` now keep the continuations of the errors they keep (they
  used to drop every spurious row). Type filters still judge a continuation by
  its own type: `--exclude-types SPURIOUS_DATA` removes it and
  `--include-types SPURIOUS_DATA` keeps it. Standalone `0x2001` records are
  unaffected.
- **A `SPURIOUS_DATA` continuation now always sits directly under its error
  row, in one recording and in a multi-file merge** (all three
  implementations; L2-ERR-005, L1-OUT-003 and L2-WRT-021 amended). Each
  spurious record carries its own timestamp, and the card can stamp the
  leftover words later than the error. With that later stamp, two things put
  another record between the pair and left the `0x2000` code pointing at the
  wrong row: sorting an equal-timestamp tie the error sat in, and, in a merge,
  any record another recorder logged in between. A continuation is now
  reported at its **error's timestamp**, so the pair shares one time and every
  stage keeps it together. **Visible change:** the `TIME_STAMP` cell of a
  `0x2000` row can differ from the vendor CSV, which shows the card's stamp
  (`docs/VENDOR-CSV-DIFFS.md` §3d); `dump` still shows it. A standalone
  `0x2001` record keeps its own time; when one is stamped later than an
  equal-timestamp run it follows, and sorting would move the record it
  followed away from the run's end, that run is written in arrival order
  (logged at DEBUG), as the `max_sort_group` cap already does.
  `docs/DATAFLOW.md` records the analysis. Filters and duplicate collapse
  keep the pair together too (the entry above).
- **A `--glob` with a wildcard in its directory part is now a usage error
  (exit `4`) with a message that says so** (all three implementations;
  L2-MRG-001). Wildcards have only ever applied to the filename, so
  `captures/**/*.ch10` read `**` as a literal directory name and failed with
  "No such file or directory" (exit `1`) -- a missing folder, when the
  mistake is the pattern. It now reports `wildcards are only supported in
  the filename, not in the directory part (no recursive **)` before touching
  the filesystem. The Python library's `expand_glob` raises `ValueError` for
  it. For a set spread over several directories, use `--manifest`.
- **Rust and Python: `--glob` now expands as L2-MRG-001 specifies, as C++
  already did.** A `*` in the pattern failed to match a filename containing
  a literal `*` at that position, so on POSIX `--glob 'dir/*'` silently left
  out `*x.mie`. And the pattern was split with path normalisation, which
  dropped a trailing separator: `dir/*.mie/` decoded the files in `dir`
  instead of failing on the missing directory `*.mie` (exit `1`).
- **Rust and Python: RT and subaddress filter values above 31 are now a
  usage error (exit `4`)** (L2-CLI-010). `--include-rts`, `--exclude-rts`,
  `--include-subaddresses` and `--exclude-subaddresses` accepted anything
  that fit in a byte, so `--include-rts 40` decoded to an empty CSV and
  exited `0`. The same value in a config file was already refused (exit
  `5`), as was the flag in C++.
- **A file whose first record is truncated now exits `2` in lenient mode,
  not `0`** (all three implementations; L2-RDR-004). Lenient is the
  default, so this changes the exit code of ordinary invocations: such a
  file used to "decode" successfully to a header-only CSV. That included
  plain text files whose bytes happen to form one plausible Type Word --
  `hello world, this is not an MIE file!!` exited `0`. The WARN naming the
  truncation is kept; the outcome is now the no-valid-records class
  (`NoValidRecords` / `MieNoValidRecordsError`), and no output file is
  created. Strict mode is unchanged (`FirstRecordTruncated`, exit `1`). A
  script that relied on exit `0` here was relying on the defect.
- **C++: a consumer closing stdout early now exits `0`** (L2-WRT-018). On
  Linux, `aero1553 decode x.mie | head -1` was killed by SIGPIPE and exited
  `141`, failing any pipeline under `set -o pipefail`. Rust and Python were
  already correct.
- **C++: `--separate-errors` with `--output-time-format iso` or `dom` now
  renders both files the same way.** The main CSV ignored the option and
  wrote day-of-year while `_errors.csv` honoured it. Rust and Python were
  already correct.
- **Python: `to_dict(fields=...)` and `columns(fields=...)` no longer hang
  when called from several threads at once.** The binding held a
  process-wide lock while iterating `fields`, which runs Python code and can
  release the GIL; one thread then waited on the GIL while holding the lock
  and another waited on the lock while holding the GIL, and the process hung
  for good. The lock now covers only the cache lookup and store.
- **C++ on Windows: paths longer than 260 characters now work.** A recording
  in a deep directory, or an output written into one, failed as "MIE file
  not found" -- even with `LongPathsEnabled` set, since the executable does
  not opt in. The Win32 backend now adds the `\\?\` extended-length prefix
  when a resolved path needs it, as Rust's standard library does. Shorter
  paths reach Windows exactly as before, so device names such as `NUL` keep
  their meaning.
- **A final record found by sync recovery is no longer dropped when the
  recording ends with the `0x0000` terminator** (all three implementations;
  L2-SYN-028 amended). Every DDC recording ends that way, so when corruption
  sat just before the last record, that record vanished -- though the same
  file without the terminator kept it. Recovery now accepts the terminator
  when it is the file's last word, which ends a candidate exactly where
  end-of-file does. A zero word anywhere else still does not confirm a
  recovery candidate, so the false-positive guard is unchanged.

## [3.0.0] — 2026-08-28

### Changed — BREAKING

- **`--time-format` has been split into `--input-time-format` and
  `--output-time-format`, and the config key `decode.time_format` is now
  `decode.input_time_format`.** The old flag selected how timestamps were
  *parsed* from the file and said nothing about how they were *written* to the
  CSV. Once the `TIME_STAMP` rendering became selectable (below), one flag named
  `--time-format` could not honestly carry both concerns, so each is now named
  for the job it does.

  **`--input-time-format`** takes the same values (`auto` / `irig` / `standard`),
  has the same default, and behaves identically. Only the name changed.

  Rationale: whichever concern kept the old name would have made the other look
  like an afterthought, and an operator reaching for "the timestamp flag" from
  memory would have had even odds of getting the wrong one. Naming the two
  symmetrically — input versus output — makes that mistake impossible to make
  silently.

  **Migration.**

  | Before | After |
  |---|---|
  | `decode rec.mie --time-format irig` | `decode rec.mie --input-time-format irig` |
  | `[decode] time_format = "irig"` | `[decode] input_time_format = "irig"` |
  | *(no equivalent)* | `--output-time-format doy` — the default, and what every pre-v3.0.0 decode emitted |

  `--time-format` is **not** accepted as a deprecated alias: passing it is a
  usage error (exit `4`) whose diagnostic names *both* replacements and states
  which concern each covers. That is deliberately more than the
  `--inline-errors` removal in v2.8.0 got, and for a specific reason — that flag
  had no successor, so a generic "unknown option" lost nothing, whereas here the
  operator's intent is still fully expressible and the only open question is
  which of two flags they meant. The generic message cannot answer it; this one
  does.

  **The retired config key is rejected, not ignored** (exit `5`, diagnostic
  naming the replacement). An unknown key is normally a WARN, which is right for
  a key from a newer version — but for a *renamed* one it would discard the
  operator's explicit choice while reporting success, silently reverting a
  forced timestamp format to auto-detection. Auto-detection is right often
  enough that the loss would not be noticed until it wasn't.

  Note this breaks the **CLI and config** contract, not the library API. As in
  v2.8.0, `cargo-semver-checks` cannot see this class of change; the version
  decision is a human one.

### Added

- **`--output-time-format doy|iso|dom` and `[output] output_time_format`:
  operator-selectable rendering of the `TIME_STAMP` column** (`L1-OUT-004`,
  `L2-WRT-025`).

  | Value | Rendering | Example |
  |---|---|---|
  | `doy` (default) | `DAY:HH:MM:SS.uuuuuu` | `192:15:54:50.456225` |
  | `iso` | `YYYY-MM-DDTHH:MM:SS.uuuuuu` + zone | `2026-07-11T15:54:50.456225Z` |
  | `dom` | `DD:HH:MM:SS.uuuuuu` | `11:15:54:50.456225` |

  **`doy` is the default and is byte-identical to what every previous version
  emitted**, so an invocation that selects nothing is unaffected and remains
  diffable against DDC vendor CSV. `iso` and `dom` depart from that deliberately
  and are documented as an exception in `docs/VENDOR-CSV-DIFFS.md` §3c.

  `dom` does not identify a date on its own — the month is deliberately not in
  the cell. That buys a column the same width and shape as `doy`; use `iso` when
  the date must be recoverable from the CSV alone.

- **`--year YYYY` and `[output] year`** (`L2-WRT-026`). An IRIG-B timestamp
  carries day-of-year but **no year**, and day 192 is July 10 in a leap year and
  July 11 in a common one — so `iso` and `dom` require one and `doy` ignores it.
  There is no year the decoder could default to that would be right: the system
  clock would date a 2019 archive with the year it was decoded in, and a file's
  mtime does not survive a copy. Selecting a calendar rendering without a year is
  therefore a usage error (exit `4`) naming both the config key and the flag,
  never a guess.

- **`--utc-offset Z|+HH:MM|-HH:MM` and `[output] utc_offset`** (`L2-WRT-025`).
  IRIG-B carries no timezone either. The `iso` rendering appends `Z` by default;
  this states what the recording could not, and is applied as a designator only —
  the time fields are never shifted.

- **Refusals rather than fabricated dates** (`L2-WRT-026`). A calendar rendering
  is declined, before any row is written, with the new `CalendarUnavailable` /
  `MieCalendarUnavailableError` class (exit `2`) when the recording cannot supply
  a date: a **Standard** recording (a free-running counter has no epoch), a
  **freerun IRIG** recording (the fields are relative, and would render as a
  perfectly ordinary meaningless date), or a record carrying **day-of-year 366
  against a common year**. The last fails the whole run rather than the row,
  because it says the configured *year* is wrong, which makes every date in the
  output suspect.

- **A year-rollover warning.** A recording that crosses midnight on 31 December
  has its day-of-year wrap while one configured year applies throughout, so rows
  after the wrap are dated a year early. The decoder emits one WARN per input and
  carries on; it deliberately does **not** auto-increment, because sync-loss
  corruption produces the same backward step and advancing the year on corrupt
  data would be the worse error.

### Changed

- **The IRIG day-of-year advisory is emitted at `WARNING` when a calendar
  rendering is active**, and stays at `INFO` under `doy` (`L2-LOG-002`). The
  reasoning of `L2-LOG-001` is unchanged — it is a standing disclaimer about card
  firmware, not an observation about the file. What changes is the consequence if
  it applies: under `doy` a firmware-skewed day is one field a human reads as
  "day 192" and can re-examine, while under `iso` it is resolved into
  `2026-07-11`, which looks like a fact and gets correlated against systems with
  no way to know it is suspect. Selecting a calendar rendering *is* the operator
  saying that field is load-bearing for them. `--no-irig-day-advisory` /
  `[logging] irig_day_advisory = false` still suppresses it at every level.

### Maintenance

- Requirements added: `L1-OUT-004`, `L2-WRT-025`, `L2-WRT-026`, `L2-CFG-012`,
  `L2-CLI-018`, `L2-CLI-019`, `L2-LOG-002`. `L2-WRT-011` is rescoped to state
  what the *default* rendering emits, so "vendor-compatible" remains a property
  of an invocation that passes no rendering flag.
- The roadmap's 3.0 and 4.0 milestones (data-word decoders, Parquet) shift to 4.0
  and 5.0. One breaking topic per major: a flag-rename migration note and a
  new-output-format one have nothing to say to each other.
- The shared conformance suite gains 20 cases covering the three renderings
  byte-exactly, the six refusal paths, and both halves of the rename; the
  config-parity corpus and the config fuzzer palette gain the new keys and the
  retired one. Renaming the key without adding the retired-key case would have
  quietly turned an existing parity case into a no-op unknown-key test.

## [2.18.0] — 2026-08-27

### Changed

- **The one-time IRIG day-of-year advisory moved from `WARNING` to `INFO`, and
  gained a switch of its own** (`L2-LOG-001`): `--no-irig-day-advisory` on the
  CLI, `[logging] irig_day_advisory = false` in TOML. All three
  implementations. This is a **stderr-surface change**: a default-level decode
  of a calendar-locked IRIG recording no longer prints the advisory at all.

  The advisory is a standing disclaimer about card firmware, not an observation
  about the recording being decoded. Nothing in the decoder compares the decoded
  day-of-year against anything — it cannot, because the card model and firmware
  revision are not recorded in the file — so it fired on every calendar-locked
  IRIG file from every card, affected or not, with identical text every time. At
  `WARNING` that put it in the default output of every run, competing with the
  warnings that *are* observations about the input: sync loss and recovery,
  non-monotonic `DELTA`, a hit sort-group cap, a skipped invalid record.

  The separate opt-out exists because the level alone cannot express "we already
  checked this." A site that has diffed its card model's day-of-year against
  vendor CSV has answered the advisory's question permanently, and should still
  be able to run `--log-level INFO` for genuine troubleshooting without that one
  known-noise line. `--log-level ERROR` silences it too, but discards the
  observational warnings along with it — `docs/VENDOR-CSV-DIFFS.md` §5 used to
  recommend exactly that, and now documents why not.

  The switch lives in each implementation's **logging** module rather than in
  the reader's options, so it is applied where `--log-level` is applied and
  covers `decode` / `count` / `dump` from one place. In Rust that also keeps the
  change additive: `ReaderOptions` is public with public fields and no
  `#[non_exhaustive]`, so a new field there would have tripped
  `constructible_struct_adds_field` and forced a major bump for what is
  behaviourally a minor change. `cargo-semver-checks` reports no semver update
  required.

  Coverage note: Rust had **no test for this advisory at all** — the
  once-per-decode flag was set and nothing asserted it fired. Python and C++
  each had one. All three now pin the level, the once-per-decode cadence and the
  opt-out.

- **The C++ analyser gates moved from LLVM 20 to LLVM 22** (`clang-tidy` and
  `clang-format` 22.1.8). The version is now declared **once per file** —
  `LLVM_VERSION` in `cpp/Makefile`, `env.LLVM_VERSION` in
  `.github/workflows/cpp-ci.yml` — and `scripts/repo-hygiene.sh` fails the build
  when the two disagree, or when a literal major is spelled out alongside the
  variable.

  Both files previously carried a comment asking the other to be edited
  alongside it. A comment is not a mechanism, and the failure it invites is
  silent in the worse direction: `make verify-ci` exists solely to reproduce the
  CI gates locally, so a drifted number means it either passes on an older
  analyser and the PR fails, or reports findings CI will never show. Both
  outcomes end with the target being distrusted rather than the number being
  noticed.

  22 found **20 real `misc-const-correctness` misses** in the test tree —
  tables of string literals declared `const char* x[]` that are iterated and
  never reassigned, so the pointee should be `const` too. Fixed. This is the
  second time a bump has paid for itself: 18 → 20 surfaced a genuine
  fread-past-EOF loop in the platform tests.

- **Every GitHub Action moved off the deprecated Node 20 runtime.**
  `actions/checkout@v4` → `@v6` at twelve sites (eleven in `cpp-ci.yml`, one in
  `differential.yml`); the runner had been forcing those steps onto Node 24 and
  annotating every run. Nothing else was affected — `setup-python`,
  `upload-artifact` and `download-artifact` are already `@v6`, `cache` is `@v5`,
  and the rest are on current majors.

### Documentation

- `cpp/README.md` records that the compilation database must be generated from a
  **clean** tree: `bear` records the compiler invocations it intercepts, and an
  incremental build issues none for targets already up to date, so
  `bear -- make all` over a warm tree writes a database missing whatever did not
  rebuild. `make tidy`'s `tidy-db-check` is what makes that visible rather than a
  silently empty analysis.

- `cpp/Makefile` records that `tidy-db-check` is **permanent, not scaffolding**.
  A CMake + Ninja build would emit `compile_commands.json` at configure time and
  make the failure mode impossible; that migration was considered and declined,
  because the `gcc:4.8` fidelity container has no CMake and cannot install one
  (Debian 7's repositories are archived and `apt-get update` there exits 100),
  and a hand-built CMake inside the tier whose job is faithful reproduction of
  the target defeats the tier.

## [2.17.0] — 2026-08-27

### Added

- **`[merge] max_collapse_survivors` / `--max-collapse-survivors`** (L2-MRG-008),
  a positive integer in `[1, 1048576]` defaulting to `4096`. Caps how many
  survivors the cross-recorder collapse window retains at once. On reaching the
  cap the oldest survivor is evicted to make room, **one** WARN is emitted for
  the whole run, and collapsing continues best-effort; no row is ever dropped
  from the output.

  This is the second of two bounds. `collapse_window_us` bounds retention in
  **time** and cannot bound it in **count** — a window admits however many
  records fall inside it, and that is a property of the data. A recording whose
  timestamps all decode to one value puts *every* record inside a single window,
  and so does a legitimately dense bus under a wide operator-set window. The
  reorder stage already had this bound (`max_sort_group`, L2-WRT-022) for the
  identical reason; these two are the only pipeline stages whose memory is a
  function of the data rather than of the file count, and they now share a
  default so an operator who has reasoned about one need not re-derive the other.

### Fixed

- **`--no-clobber` did not actually prevent overwrites.** All three
  implementations. The guard was an `exists()` test run before the output was
  opened, paired with a replacing rename at the end — and `exists()` answers a
  question about the *past*. Between that answer and the rename, any other
  process could create the destination; the rename then destroyed it. Two
  concurrent runs of the same command both passed the pre-flight, and the second
  silently overwrote the first's output — the exact outcome the flag exists to
  prevent.

  The `.partial` targets were not merely racy: they were **never checked at
  all**. `--no-clobber --allow-partial` over an existing `<dest>.partial`
  overwrote it unconditionally, which on a forensic re-run means destroying the
  artifact from the previous attempt.

  The refusal now happens **at the commit**, where the filesystem can make it
  atomic, and covers every path a run commits: the destination, the derived
  errors file, and the `.partial` of each (new **L2-WRT-023**). A refusal
  surfaces the same `ClobberRefused` class as the pre-flight and leaves the
  target's contents untouched. The pre-flight is kept as an *early*, friendlier
  report — before a temp file exists and before a whole recording is decoded —
  and is now documented as not being the guarantee.

  No portable single call does this, so the requirement is stated over the
  *property* — atomic and non-replacing — rather than over a syscall:

  | | Mechanism |
  |---|---|
  | Win32 (C++) | `MoveFileExW` with **no** flags: natively atomic and non-replacing, works on FAT, no fallback needed |
  | POSIX (C++) | `link(2)` (fails `EEXIST`), then unlink the temp; an `O_EXCL` reservation plus a rename over our own zero-byte file where hard links do not exist |
  | Rust | `std::fs::hard_link` — `link(2)` on POSIX, `CreateHardLinkW` on Windows — with the same reservation fallback. One standard-library call spans both platforms, so the crate stays dependency-free |
  | Python | `os.link` with the same fallback |

  `renameat2(RENAME_NOREPLACE)` would be more direct and is deliberately not
  used: it needs glibc 2.28 and SLES 12 SP5 ships 2.22, so reaching it means a
  raw `syscall()` plus a hand-written fallback for every kernel without it —
  more OS surface, in the layer that exists to contain OS surface, for no
  behaviour `link(2)` does not already provide.

- **A split `--allow-partial` run committed the errors output before the main
  one.** All three implementations. L2-WRT-019 requires main-before-errors so
  that a mid-commit failure can never leave an orphan errors file beside no main
  output. The normal path obeyed it. The `--allow-partial` path did the
  opposite — in every implementation — so when the errors `.partial` rename
  succeeded and the main one failed, what was left on disk was
  `<dest>_errors.csv.partial` and nothing else: the precise residue the rule
  forbids.

  That the branch which got it wrong was the *failure* branch is not a
  coincidence; it is the branch nobody re-reads. L2-WRT-019 is now stated over
  **every path that commits the pair** rather than over "the commit".

- **`--glob` resolved a different input set in each implementation.** L2-MRG-001
  says every implementation "SHALL expand it identically", and the only thing any
  test compared was the **wildcard matcher**. All three matchers agreed. The
  expansions did not, in three ways, none of them about wildcards:

  | Divergence | Effect |
  |---|---|
  | C++ matched every entry `list_directory` returned | A **directory** named `archive.mie` was an input there and invisible to Rust and Python. C++ then failed to map it, so a batch the other two decoded whole came back short. |
  | Rust filtered on `DirEntry::file_type`, which does not follow symlinks | A **symlinked** recording was dropped there and kept by Python. |
  | C++ split the pattern on `\` on both platforms | On POSIX a backslash is an ordinary filename character, so `--glob 'odd\name*.mie'` named one file in the current directory to two implementations and a pattern inside a directory called `odd` to the third. |

  L2-MRG-001 now carries an **expansion grammar** alongside its manifest grammar:
  the split uses the platform's separator set, and only entries resolving to a
  regular file are matched — symlinks followed, so a symlinked recording counts
  and a dangling one does not; directories, sockets, fifos and devices never do;
  and an entry whose type cannot be read is skipped rather than raised (a
  directory listing racing a deletion is not a reason to fail a run).

  The cross-implementation gate is the new
  `tests/conformance/glob_parity.py`, which compares the **resolved set** on a
  directory built to hold exactly the entries the three disagreed about. A
  per-implementation test could not have caught any of this: each pins its
  implementation against its own reading of the rule, which is the thing in
  question when two readings differ.

- **Python let a final-flush failure escape as a raw `OSError`, and could leak
  the temp file with it.** `_AtomicCsvFile.commit()` closed the stream *outside*
  its exception wrapper. The final flush is where the last buffered rows actually
  reach the filesystem — so it is the likeliest place for a disk-full error to
  land, not an unlikely one — and it surfaced as a raw `OSError` from a method
  documented to raise `MieWriterError`. The CLI reported an unexpected crash
  rather than a write failure.

  The follow-on was worse. The context manager's `close()` then re-raised while
  flushing the same unwritten buffer, which skipped the unlink and left the temp
  file sitting on the disk that had just filled up.

  New **L2-WRT-024** states that the final flush, the close and the move are all
  part of the commit for classification purposes, and that cleanup on the failure
  path may not mask the failure or skip its own unlink. Rust and C++ already
  behaved this way; the requirement pins behaviour two implementations had and
  one did not, which is the kind of gap a cross-implementation contract exists to
  catch.

### Changed

- **`commit` and `commit_partial` now share one move routine** in every
  implementation (new **L3-WRT-005**), with the commit mode carried as state set
  once at construction. Each having spelled the flush / close / move sequence out
  for itself is what let three of the four fixes above take the same shape: a
  rule applied at one commit target and not the other, and error handling that
  wrapped the rename but not the flush.

- **De-duplication could retain every record and go quadratic after a backward
  timestamp step.** All three implementations. The collapse window's *matching*
  test used the absolute time distance, but its *eviction* did not: each
  implementation evicted only from the front of an arrival-ordered deque, testing
  the one-sided `current_us - front_us`. That is correct only while the stream is
  sorted. After a lenient non-monotonic step (L2-MRG-006) the front can hold a
  timestamp in the **future** of the current record, the one-sided test is then
  never true, the front never leaves — and it blocks eviction of everything
  behind it.

  Measured on an alternating 1000 us / 0 us probe with a zero-width window:

  | records | survivors retained | time |
  |---|---|---|
  | 2 500 | 2 500 (100%) | 0.376 s |
  | 5 000 | 5 000 (100%) | 1.462 s |
  | 10 000 | 10 000 (100%) | 5.725 s |
  | 10 000, monotonic (control) | **1** | 0.071 s |

  Twice the records for four times the time — the quadratic scan L2-MRG-002's
  streaming guarantee and L1-ROB-001's no-unbounded-growth rule both exclude.
  After the fix the same probe retains **1** survivor and runs linearly (0.021 /
  0.042 / 0.082 s).

  Retention is now stated as a property of the retained set — a survivor is kept
  iff `|survivor_us - current_us| <= collapse_window_us` — rather than as a loop
  over one end of a container, which is what makes it checkable independently of
  how survivors happen to be stored. L2-MRG-007 is amended to require that
  order-independence explicitly, and the new L2-MRG-008 adds the count bound that
  the window cannot provide.


- **A decode could overwrite one of its own input files, and exit 0 while doing
  it.** All three implementations. The L2-WRT-014 collision guard checked the
  destination the operator named — and only that. It never checked the paths the
  writer *derives* from it, each of which is an ordinary path that can name an
  ordinary file:

  | Derived target | Written when | Destroys an input named |
  |---|---|---|
  | `<stem>_errors<suffix>` | `--separate-errors` | `capture_errors.mie`, with `-o capture.mie` |
  | `<destination>.partial` | `--allow-partial` + sync loss | `out.csv.partial`, with `-o out.csv` |
  | `<stem>_errors<suffix>.partial` | both | the same, for the errors file |

  All three trees carried a comment reasoning that the errors path needed no
  collision check because it "is derived from output, which was already
  checked". That does not follow: being derived from a *safe* path says nothing
  about whether it collides with a *different* input, and `capture_errors.mie`
  is a perfectly plausible name for one of the recordings being decoded. On the
  merge path it was worse — the writer is handed `input_path: None` there, so
  the derived paths were checked against nothing at all.

  Confirmed destructive before the fix, on all three targets: a 98-byte
  recording came back as a 433-byte CSV, a 70 KB one as 665 bytes, **exit 0
  both times**. On POSIX the damage is silent, because the reader's mapping
  survives on the old inode and the decode completes normally against bytes no
  longer on disk.

  The fix puts each half where it belongs rather than duplicating the rule: the
  writer gained a `commit_targets(output, split_errors, allow_partial)`
  enumeration — the same code `commit_partial` now renames through, so the path
  guarded and the path written cannot drift — and the CLI's merge guard runs
  that enumeration against **every** resolved input. The mode comes from the
  resolved config, not the flags, so a site config selecting separate-errors or
  allow-partial is guarded exactly like the command line. `.partial` targets are
  refused up front even on a run that would never lose sync: before the output
  is opened is the only point at which refusing is still safe.

  L2-WRT-014 is restated to enumerate all four targets, require every one be
  tested against every input, and require the enumeration be the writer's own.
  Three conformance cases pin the behaviour across implementations, and the
  conformance runner now asserts on **every** case that no implementation
  modified an input — an invariant that would have caught this and that no CSV
  oracle in the suite could.

- **Rust: a manifest whose last line is unterminated now has its trailing `\r`
  stripped like every other line.** L2-MRG-001 rule 3 requires at most one
  trailing `\r` to be removed from each line; `read_manifest` used
  `str::lines()`, which strips the CR only from a line an `\n` actually
  followed. A CRLF manifest whose final terminator was lost — ending
  `"b.mie\r"` — therefore resolved to a path with a carriage return in it, and
  failed on a file the operator had named correctly; Python and C++ resolved the
  same file to `b.mie`. The nightly fuzz burn-in's cross-implementation summary
  comparison caught it on the degenerate one-byte manifest `"\r"`, which Rust
  counted as one path and the other two as none (`manifest_paths=299` vs `296`
  over 25 000 generated manifests). Rust now splits on `\n` and strips the CR
  itself. The grammar test in all three trees gained the unterminated-final-line
  case, and rules 2 and 3 of L2-MRG-001 now say outright that the text after the
  last `\n` is a line and is read no differently — the previous "each line" was
  precise right up until a standard library decided an unterminated tail was not
  quite a line.

## [2.16.0] — 2026-08-26

Closes the fuzzing parity gaps v2.15.1 opened a ledger for, and adds the
per-input differential fuzzer that ledger pointed at. Doing so found **four real
divergences in `read_manifest`**, in all three implementations — the first time
this project has had a check capable of finding them.

### Changed — behaviour

- **The `--manifest` grammar is now specified exactly, and all three
  implementations obey it.** L2-MRG-001 said only "one path per line; blank lines
  and `#`-prefixed comment lines ignored", and each implementation filled the
  gaps with whatever its standard library made easy. They filled them four
  different ways:

  | Gap | What happened | Outlier |
  |---|---|---|
  | Text vs bytes | A `std::string` validates nothing, so C++ accepted arbitrary bytes as paths. 498 of 512 generated manifests were rejected by Rust and Python and decoded by C++. | C++ |
  | Line separators | `str.splitlines()` also breaks on vertical tab, form feed, U+0085 and U+2028/9 — none of which ends a line in a manifest, all legal in a POSIX filename. One file became two nonexistent ones. | Python |
  | Carriage returns | C++ dropped **every** `\r` in a line, editing a filename containing one; Python's reader translated a lone `\r` to `\n` before the parser saw it, splitting one path into two. | C++, Python |
  | Trimming | `str::trim` and `str.strip` remove Unicode whitespace (U+00A0, U+3000, …); the locale-free C++ implementation cannot, so two implementations edited a filename the third passed through. | Rust, Python |

  The grammar is now normative in L2-MRG-001: **UTF-8 required**, `\n` the only
  separator, at most one trailing `\r` stripped, trimming of **ASCII space and
  tab only**, blank and `#` lines ignored. The last rule resolves *toward* the
  constrained implementation — C++ cannot classify Unicode whitespace without
  embedding a table, and "trim spaces and tabs" is what the format needs.

  **What this changes for you.** A manifest that is not valid UTF-8 is now
  rejected by C++ as it always was by the other two. A manifest containing a
  form feed, vertical tab, U+0085 or U+2028 resolves to *one* path in Python now,
  not two. A path padded with non-breaking spaces keeps them in Rust and Python.
  Ordinary manifests — UTF-8, one path per line, LF or CRLF — are unaffected.

### Added — fuzzing parity (Stage 2)

- **C++ gained `dump` and merge-input-resolution fuzz harnesses.** It had only
  the reader one, so the tree with manual bounds arithmetic and no borrow checker
  was the one fuzzing least. `make check-fuzz` now runs all three.
- **The merge harness is real fuzzing in all three now.** Python's seeded
  `random.Random` and covered only the manifest; it now shares the xorshift64
  generator and drives the glob matcher too. Patterns are drawn from a shared
  alphabet weighted toward `*` and `?` — the first version drew uniformly over
  patterns up to 95 characters and matched a probe **zero** times in 512
  iterations, which is to say it fuzzed the reject path and nothing else. Two
  alphabet entries and two probes are non-ASCII, because Rust and Python match
  over scalar values while C++ advances `?` by a whole UTF-8 character.
- **Python gained the exhaustive decode-digest check** (`L3-PY-017`), which the
  C++ tree has had since it joined the joint cut. Every one of the 65 536 Type
  Words, every Command Word and every timestamp field bit is swept through an
  FNV-1a digest against the constants `rust/examples/decode_digest.rs` prints.
  All four matched on the first run.
- **The burn-in now has an instrumented deep run** (`fuzz-cpp-asan`). Until now
  the raised iteration count applied only to the uninstrumented `-O2` build — the
  sweep ran precisely where a non-faulting out-of-bounds read goes unnoticed.
  Valgrind deliberately stays at the default count: one to two orders of
  magnitude slower for the same fault class here.
- **The cross-implementation comparison runs on every push**, not only in the
  nightly burn-in. `differential.yml` runs all three implementations' harnesses
  at default counts and compares the summaries. A PR introducing a divergence now
  fails that PR.

### Added — differential record fuzzing (Stage 3)

- **`tests/conformance/record_fuzz.py`** — the record-stream twin of
  `config_fuzz.py`, and the only check anywhere that compares what the three
  *decoders produce* on input nobody wrote. It generates a recording, runs
  `decode` through every implementation's CLI, and compares the exit-code class
  **and the CSV bytes** all-pairs.

  It is **structure-aware**, because uniform noise is the wrong tool: across a
  25 000-iteration burn-in random bytes never once produced enough consecutive
  equal-timestamp records to reach the canonical-order cap branch that all three
  harnesses' comments claimed they reached "often". The generator instead starts
  from the committed hex fixtures — every valid record shape the project knows
  about — concatenates one to three, and then damages them: bit flips,
  truncation, appended noise, zeroed words, duplicated slices, spliced runs.

  All implementations read **one shared input file**, which is not tidiness:
  `MUX` is populated from the input file name (L2-WRT-020), so per-implementation
  copies would put a different value in every CSV and the comparison would fail
  on the harness rather than on the decoders. On a divergence it prints the input
  as a ready-to-commit `inputs/*.hex` fixture and names the first differing CSV
  line.

### Fixed

- **A summary counter that could never have agreed.** `python/tests/` gained
  `fuzz_support.py` so the generator, the knobs and the summary writer live in
  one place. They were private helpers on a test class, which is how the merge
  harness came to use a different PRNG from every other harness in the project.

## [2.15.1] — 2026-08-25

Test and CI infrastructure only. No decoder, library-API or CLI behaviour
changes; the three implementations decode exactly what they decoded at 2.15.0.

### Fixed

- **The fuzz burn-in's `stream_logs` input only ever controlled one of the three
  jobs.** It was documented as choosing whether the harnesses' WARN/ERROR
  diagnostics stream into the job log, implemented as `cargo test --nocapture` /
  `pytest -s`. Neither flag reaches the code that writes: Rust's `log::emit`
  goes through `std::io::stderr().lock()` and libtest's capture only intercepts
  the `print!` / `eprint!` macros, and the C++ logger writes to the stderr file
  descriptor, which Catch2 does not redirect. So the Rust and C++ jobs streamed
  unconditionally — a measured ~43 MB and ~40 MB per scheduled run — while
  pytest captured Python's and showed 257 lines total. The input controlled the
  one job whose output it was hardest to see.

  Silencing is now the harness's job. All three read `MIE_FUZZ_STREAM_LOGS`, and
  default to setting the decoder's log level to `OFF` — which also skips the
  formatting work, not just the write. Python previously formatted every one of
  roughly a quarter-million WARN records before pytest discarded them.

- **A Catch2 test-ordering hazard, found by the above.** Silencing the C++
  logger inside the fuzz case left the process-global level at `OFF` for every
  case that ran after it, and `test_log.cpp`'s "the default level is WARN"
  failed with `4 == 2` — pointing at a file the fuzz harness never touches. The
  level is now set through an RAII guard, so a `FAIL` unwinding past it still
  restores. Rust deliberately does *not* restore: libtest runs that binary's
  tests in parallel threads, where a scope guard would restore the level while a
  sibling test was still running.

### Changed

- **The C++ burn-in runs the fuzz cases only.** It ran `make -C cpp check` — the
  whole suite, from a cold build — so a job whose entire purpose was to raise the
  fuzz iteration count spent most of its wall time elsewhere, and Catch2 reports
  no per-case duration without `-d yes` to recover the fuzz portion from the
  total. New `make -C cpp check-fuzz` target runs `[fuzz]`; `make check` is
  unchanged and still the single unparameterised command a developer runs, with
  the fuzz cases riding along in it at their default iteration count.

- **The burn-in covers Windows as well as Linux.** The C++ platform layer is
  entirely different code there — file mapping, path identity, binary stdout
  (ADR-0003) — and mapping a file is the first thing every one of these
  harnesses does with its garbage input. Rust's and Python's mapping paths
  differ by platform too. `ci.yml` and `cpp-ci.yml` already built and tested all
  three on Windows, but only ever at the 256-iteration default; the deep sweep
  ran on Linux alone.

### Added

- **Every fuzz harness now emits one `FUZZ-SUMMARY` line, and a new
  `fuzz-compare` job diffs them all-pairs.** The three harnesses have always
  shared a generator — same xorshift64, same seed, same draw order, so iteration
  N is the same bytes in all three — but they never shared an assertion: each
  proved, in its own process and its own framework, that it did not crash. Three
  implementations could each survive the same input while decoding it
  differently and nothing would notice. The summary reports inputs, bytes
  generated, readers opened, records yielded and errors, and
  `scripts/compare-fuzz-summaries.py` fails the run if any two disagree — no
  majority rule and no reference implementation, the same reasoning recorded in
  `tests/conformance/differential.py`.

  It is a coarse check by design: it compares run *totals*, so it catches a
  disagreement about how many records a file yields but not two implementations
  decoding the same record to different values. Per-input differential fuzzing
  is the follow-on, written up in `docs/FUZZING.md` 6.1.

  Every counter is path-independent, which is not a detail: the first version
  counted dump output in bytes and Rust and Python disagreed by a constant
  offset that turned out to be the length of the input path in the dump header.
  The harnesses count output *lines*.

- **`docs/FUZZING.md`** — the map of what is fuzzed, by which implementation,
  under which driver, and what is deliberately not fuzzed yet. Records the two
  fuzzing architectures in the repo (per-language in-process harnesses versus
  the shared all-pairs differential drivers in `tests/conformance/`), the parity
  gaps, and the rule that governs the area: **a fuzz surface is exercised by all
  three implementations or by none.**

- **`repo-hygiene.sh` now scans `fuzz.yml` for undocumented CI jobs.** The
  MAINTAINER-GUIDE §9 check covered `ci.yml`, `cpp-ci.yml` and
  `differential.yml`, so the fuzz jobs were the one set nothing required to be
  described. Verified to fail on a planted violation as well as to pass clean.

## [2.15.0] — 2026-08-25

### Changed

- **An unsupported `--format` is now a usage error (exit `4`), not a runtime
  error (exit `1`).** `L1-EXIT-007` says an invalid flag value exits `4`, and
  all three CLIs returned `1` — because the check ran *after* the config layer
  had merged CLI overrides rather than at parse time. One mistake therefore
  produced three different exit codes depending on where it was written: `4`
  for a bad `--time-format`, `1` for a bad `--format`, and `5` for the same
  value in a config file. Only the middle one was wrong.

  The CLI value is now validated where every other flag value is validated, and
  the post-merge check is gone rather than left unreachable. **A config-file
  `[output] format` is unchanged and still exits `5`** — a bad value in a file
  is a configuration error, not a mistyped command. Two conformance cases pin
  both spellings (`--format json` and `--format=json`) across all three.

  `docs/CONFIG-REFERENCE.md` had codified the exit-`1` behaviour as a
  deliberate exception; that paragraph is replaced rather than deleted, so the
  history of the decision stays readable.

### Fixed

- **Two Rust tests that validated `config/default.toml` had never validated
  anything.** Both used `Path::new("config/default.toml")`, which looks right
  but is relative to the process working directory — and cargo runs test
  binaries from the package root, so it resolved to `rust/config/default.toml`,
  a path that has never existed. Both bodies sat behind `if path.exists()`, so
  they passed by skipping, silently, from the day they were written. Meanwhile
  `docs/TRACE-MATRIX.md` reported **L2-CFG-008 as Implemented on the strength of
  one of them**, which made a verified requirement out of a test that ran no
  assertions.

  They now resolve from `CARGO_MANIFEST_DIR` and the skip guard is gone, so an
  absent file fails loudly. Python's equivalent resolved correctly but skipped
  if the file was missing; that guard is also gone. C++ had no such test at all.

  The real fix is cross-implementation: a new conformance case decodes a fixture
  with `--config config/default.toml` and compares against the existing oracle,
  so **all three** now prove the shipped starter config parses *and* reproduces
  default behaviour byte-for-byte.

- **`L2-SYN-026` and `L2-SYN-005` specified a look-ahead default of `8`; every
  implementation ships `2`.** A normative requirement contradicting the code —
  and contradicting the L2-CFG schema table in the same document, which has
  always said `2`, as do `config/default.toml`, `CONFIG-REFERENCE.md` and
  `CLI-REFERENCE.md`.

  The code is right. The raise to `8` was reverted in the same work that
  introduced it: once continuous per-record validation stopped performing
  look-ahead, the depth no longer cost `N-1` valid records per corruption site,
  and raising it became a pure operator choice via `--lookahead-records`. The
  requirement text was left behind. It now says `2` and records why.

  The gap that let this persist was that **no implementation had a test pinning
  the default value** — every look-ahead test passes an explicit depth, so the
  default could disagree with the specification and nothing would notice. All
  three now pin it, deliberately as a literal rather than by reference to the
  constant, which would reproduce the blind spot.

- **The ASCII gate was blind to f-strings on Python 3.12+, and five non-ASCII
  Python messages had survived the v2.14.0 sweep because of it.** Under PEP 701
  an f-string is no longer a single `STRING` token — 3.12 emits
  `FSTRING_START` / `FSTRING_MIDDLE` / `FSTRING_END` — and
  `scripts/assert-ascii-output.py` inspected only `STRING`. Run on 3.12 it
  reported the tree clean; run on 3.10, where an f-string is still one `STRING`,
  it reported five failures.

  The five were real, and two of them were the **Python twins of C++ messages
  v2.14.0 had just fixed**: the `BC→RT` / `RT→BC` invariant diagnostics in
  `decode.py` and `exceptions.py`, and the empty-recording `count` message in
  `cli.py`. Python operators were still seeing mojibake on a Windows console
  for exactly the lines the previous release claimed to have fixed everywhere.

  The gate now handles both token shapes, so it gives the same answer on every
  supported interpreter. Found by running the identical gate under both 3.12
  (Windows) and 3.10 (WSL2) — a single-environment run would have missed it in
  either direction.

- **`tests/conformance/run.py --python-bin` could not point at a virtualenv.**
  It called `.resolve()` on the interpreter path, and a virtualenv's
  `bin/python` is a *symlink* to the base interpreter — so the venv was thrown
  away and the runner failed with "mie_decoder is not importable from
  /usr/bin/python3.10", naming an interpreter the caller never asked for. It
  now uses `.absolute()`, which normalises a relative path without following
  symlinks. CI never hit this: Poetry's Windows venv python is a real file, and
  the Linux job installs into the system interpreter.

- **The `diagrams` CI job pinned a PlantUML version that crashes, and reported
  success anyway.** The pin was `1.2026.5`, on which `component.puml` dies in
  the smetana layout engine and PlantUML **still exits `0`**, leaving a
  14,489-byte stub where the whole diagram is 59,904. Nothing noticed, because
  the job's verification step compared nothing (below).

  Pinned to **`1.2026.7`**, released 2026-08-25 — the first *stable* build that
  renders all three diagrams. Until it existed, the only working build was the
  rolling `snapshot` pre-release, whose asset name carries no version and is
  overwritten in place, which is why the pin had been knowingly left on a
  crashing release. The job now scans the render log for exceptions and requires
  every output SVG to exceed 20 KB; both halves were checked against `1.2026.5`,
  which they correctly reject.

- **Coverage floors: the docs said 87% lines / 86% regions; the gate enforces
  90 / 89.** Four places were stale, including the rationale comment sitting
  three lines above the alias that sets them, in the same file. A new
  `repo-hygiene.sh` check compares each documented floor against the file that
  enforces it (`cov-ci` in `rust/.cargo/config.toml`, `fail_under` in
  `python/pyproject.toml`, `COVERAGE_MIN_*` in `cpp/Makefile`), so prose and
  gate cannot diverge again. Python's and C++'s documented numbers were correct.

### Changed

- **The documentation now describes the three-implementation product it
  actually is.** `README.md`, `docs/L1-REQ.md` (the normative scope statement),
  `tests/conformance/README.md` and `docs/ARCHITECTURE.md` still defined a
  Rust-and-Python product. ARCHITECTURE additionally warned that C++ was
  "mid-port" and marked `cli.cpp`, `merge.cpp` and `dump.cpp` as *planned* —
  all three have been implemented and conformance-gated since v2.13.0.

  **C++ ships as source in the joint cut**, and that is now stated rather than
  asked. `docs/ROADMAP.md` still posed "does the C++ implementation ship in the
  next joint cut?" as open, months after v2.13.0 and v2.14.0 had both shipped
  with all three version-aligned and a CI gate failing the build when they
  disagree. The genuinely open Phase 3 questions — prebuilt artifacts, the
  static-CRT choice — remain open and are unchanged. `MAINTAINER-GUIDE.md` §11
  gains the C++ build commands alongside Rust and Python.

- **`cpp/README.md` no longer denies tiers it has.** It claimed "no coverage
  gate" (`cpp/Makefile` sets 90% line / 76% branch floors and `cpp-ci.yml` runs
  a gcovr job) and "deliberately **no fuzz tier**… the targets are unwritten"
  (`fuzz.yml` runs a `fuzz-cpp` burn-in over the existing L1-ROB-001 harness).
  A README that under-claims sends a contributor looking for work already done.
  The absence of *libFuzzer* targets is real and is now presented as the design
  decision it is. A second hygiene check fails the build if either claim
  returns.

## [2.14.0] — 2026-08-24

### Added

- **Rust and C++ now honour `--`, the POSIX end-of-options separator.** Without
  it there is no way to decode a recording whose name begins with a dash — the
  CLI reads it as a flag — and renaming the file is not always the operator's
  to do. Python inherited the behaviour from `argparse` and had it from the
  start; the other two reported `--` itself as an unknown option, so the same
  command line worked or failed depending on which implementation was
  installed. Demonstrated end to end: a file called `-weird-name.mie` decodes
  in all three with `--` and is a usage error in all three without it.

  The **first** `--` in a parser's own token stream is discarded and everything
  after it is a path, however it is spelled. A later `--` is an ordinary
  positional — which is the only way to name a file genuinely called `--`.

  The separator is **scoped to the parser that consumes it**, and that is the
  part worth stating because the plausible alternative is wrong. A `--` before
  the subcommand means only "the next token is the subcommand name": it makes
  `-- --version` an unknown command rather than a version request, but it does
  **not** put the subcommand's parser into end-of-options mode, so
  `-- decode rec.mie --no-mux` still honours `--no-mux`. Carrying it across the
  boundary would have made that invocation silently ignore the flag — the same
  silent-wrong-answer failure the previous entry exists to remove.

  Verified as a 15-shape three-way comparison covering both sides of the
  subcommand boundary, doubled separators, `count` and `dump`, and `--` offered
  as a flag's value: all three agree on every one. `L2-CLI-016` states the
  contract, with 4 Rust / 1 C++ (five sections) / 6 Python tests and 3
  conformance cases.

  **The contract binds the position immediately before the input paths**
  (`decode -- rec.mie`), which is the position operators actually need and the
  only one every supported interpreter handles identically. Two other positions
  are less portable, both because of `argparse`, and neither is levelled:

  - **Before the subcommand** (`-- decode rec.mie`) works in Rust, C++ and
    Python **3.12+**. Earlier `argparse` did not strip a leading `--` ahead of a
    subparser choice and passed `--` itself as the subcommand name, so it is a
    usage error on 3.10 and 3.11. Levelling downward would mean deleting a
    working capability from two implementations to match the oldest supported
    interpreter.
  - **Trailing behind a flag** (`decode rec.mie -o out.csv --`) is a no-op in
    Rust and C++ and a usage error in Python on *every* version — consistent
    across 3.10–3.14, so a defect rather than a version split.

  The first of those was found the same way the `-5e3` corner was: CI failed on
  the versions the development machine does not run. Python 3.10 and 3.11
  rejected a conformance case that passed locally on 3.12. The cases now use
  the portable position, and the Python tests assert the **interpreter's own**
  answer rather than a fixed one, so they stay honest across all five versions
  instead of encoding whichever happened to be installed.

- **Every flag accepts both `--flag value` and `--flag=value` in all three
  implementations — and nothing shared proved it.** The two spellings are
  standard, both have always worked, and neither was mentioned anywhere in the
  documentation. Coverage was thin and lopsided: three Rust unit tests, one C++
  test, zero Python tests, and zero conformance cases. `cli-surface-parity`
  compares `--help` flag *names*, so it cannot see a difference in how a value
  is attached to one.

  That matters more here than it would in a project using an argument-parsing
  library, because two of the three parsers are hand-rolled and the `=` form is
  handled per-flag: Python inherits it from `argparse` for free, C++ resolves it
  in one place (`ArgReader::take_value`), and **Rust repeats the same
  `starts_with("--x=")` idiom at 26 separate sites**. A flag added to Rust with
  only the space form would be a real divergence that every existing gate would
  report as green.

  Four conformance cases now pin the contract across all three, chosen to cover
  the parse paths that differ rather than to enumerate flags — a list-valued
  filter, two numeric flags in one invocation, and a **global** flag:

  - `flag-eq-form-filter` — `--exclude-subaddresses=22`
  - `flag-eq-form-numeric` — `--time-format=standard --standard-tick-rate-hz=1000000`
  - `flag-eq-form-global` — `--log-level=ERROR`, before the subcommand
  - `flag-eq-form-global-rejects-bad-value` — `--log-level=bogus`, expecting exit 4

  The global cases needed a new `global_args` manifest field: the existing
  `args` is appended *after* the subcommand, so it could never reach a global
  flag, and Rust parses globals in a **separate loop** from subcommand flags —
  meaning the `=` spelling had two independent implementations and the suite
  could only ever have exercised one of them.

- **`L2-CLI-015` states the flag-value contract as a requirement.** The repo
  already had the precedent: `L2-CLI-005` writes down that `-o -` is a path and
  not a stdout selector, "because its absence is exactly what let the three
  drift apart unnoticed". This is the same situation — a contract every
  implementation was assumed to meet, that none of them stated, and that one of
  them broke. The new requirement fixes both spellings, the three boundaries
  above, and the one permitted divergence, and is traced to tests in all three
  implementations.

- **`docs/CLI-REFERENCE.md` now documents how a value attaches to a flag.**
  Both spellings have always worked and neither appeared in `CLI-REFERENCE.md`,
  `USER-GUIDE.md` or the README. The new *Attaching a value to a flag* section
  states the equivalence and the three rules that each have a defensible
  opposite: `--flag=` is an empty value the individual flag then accepts or
  rejects, only the **first** `=` separates (`--mux-delimiter==` sets the
  delimiter to `=`), and a value-less flag given a value (`--no-mux=true`) is a
  usage error rather than a way to spell "on". Every claim was measured against
  all three implementations rather than read off the source.

  It also records the one place they genuinely disagree, which the same probing
  turned up: given a value spelled like another flag — `--mux-delimiter --no-mux`
  — Rust and C++ consume `--no-mux` as the value, so it never acts as a flag,
  while Python's `argparse` refuses to consume a `--`-prefixed token and exits
  `4`. This is documented rather than fixed: two of three agree, `argparse`
  cannot be made to accept it without leaving `argparse`, and forcing the other
  two to refuse would break a legitimate value that happens to start with `--`.
  The joined form (`--mux-delimiter=--no-mux`) means the same thing in all
  three, and is the recommendation.

  The last case exists because the third is vacuous on its own. `--log-level`
  does not change the CSV, so `flag-eq-form-global` passes whether or not the
  flag ever reaches the binary. The negative case is self-checking: it asserts
  exit 4 *and* that stderr names the valid levels, which distinguishes "the
  value `bogus` was rejected" from "the whole token was an unknown option" —
  the outcome if the `=` form were not parsed. Both were proven by planting a
  runner that silently drops `global_args`: the positive case still passed, the
  negative one failed.

### Changed

- **Rust and C++ no longer swallow a following flag as a flag's value — this is
  a behaviour change.** `--mux-delimiter --no-mux` used to set the delimiter to
  the literal string `"--no-mux"`, which meant `--no-mux` **silently never
  ran** and the decode succeeded, exit `0`, with a wrong `MUX` column. Python's
  `argparse` had always refused it. An invocation that previously "worked" in
  those two now exits `4`.

  That is the point. Of the three possible behaviours — refuse, or produce a
  wrong answer quietly, or disagree across implementations — the one we had was
  the worst two at once. Levelling toward Python turns a silent wrong result
  into an immediate usage error and makes all three agree; levelling the other
  way was not available, since `argparse` cannot be made permissive without
  abandoning `argparse`.

  A token "looks like an option" when it starts with `-`, is longer than one
  character, contains no space, and does not **begin** like a number (a dash,
  an optional `.`, then a digit). The three exemptions each keep a real
  invocation working:

  - a lone `-` is a path, so `-o -` still writes a file named `-` (`L2-CLI-005`)
  - a negative number is a value, so `--mux-field -1` still counts from the end,
    and `--collapse-window-us -5` still reaches its own validator to be refused
    for being *negative* rather than for looking like a flag
  - a token containing a space is a value, since no flag is spelled that way

  **The joined form is unaffected and is the fix**: `--mux-delimiter=--no-mux`
  is unambiguous, remains legal everywhere, and the error message names it.

  Note **begins** like a number, not *is* one: `-1a` is a value, so a mistyped
  number is reported by the flag itself naming the bad value
  (`invalid --mux-field: "-1a"`) rather than by the guard, which could only
  have said it looked like an option.

  **`argparse` does not agree with itself across the Python versions this
  project supports, and CI is what found that.** The first implementation of
  this guard matched Python 3.13, because that is what the development machine
  runs; the Python **3.14** jobs then failed on exactly `-5e3`, `-0x5` and
  `-1a`. The exemption had changed from an anchored full match
  (`^-\d+$|^-\d*\.\d+$`, plain decimals only) to a prefix test (`-\.?\d`,
  anything starting dash-digit). Supporting 3.10 through 3.14 means **no** rule
  can match every version. Rust and C++ follow **3.14**: simpler to state,
  where Python is going, and the more permissive of the two, so it cannot newly
  reject an invocation that used to work.

  `L2-CLI-015` is therefore bounded to the shapes every supported version
  agrees on — option-name-like tokens are refused; a lone `-`, plain decimals,
  and space-containing tokens are values. Tokens of the form *dash, digit, then
  something else* are explicitly outside it and appear in no conformance case.

  Verified as a differential sweep over 29 token shapes — decimals, exponent
  and hex forms, embedded spaces, single and double dashes, empty — run against
  all three binaries: **Rust and C++ agree on every one**. Against Python the
  remainder is the documented version corner plus `--`, a different feature
  (below). Pinned by 3 conformance cases and 5 Rust / 3 C++ / 11 Python
  tests. Two new oracles prove the joined form is taken as a *string* rather
  than as the flag: with delimiter `--no-mux` and field `1`, the file
  `alpha--no-muxbravo.mie` yields `MUX=bravo.mie`, which no other reading
  produces.

- **Rust: the `--flag value` / `--flag=value` handling is one cursor instead of
  26 copies.** Every valued flag carried two match arms — an exact-name arm and
  a `starts_with("--flag=")` arm that re-sliced the token by a hard-coded prefix
  length — across four separate parse loops (globals, `decode`, `count`,
  `dump`). C++ already resolved both spellings in one place
  (`ArgReader::take_value`) and Python inherits it from `argparse`; Rust was the
  outlier, and a flag added with only the first arm would silently reject the
  joined spelling with nothing to catch it.

  `Arg::split` now resolves the token once and each flag gets a single arm.
  `parse_decode` went from 202 lines to 113. Three rules the split has to get
  right, each of which had an established behaviour worth preserving exactly:

  - **Only `--` tokens split**, so a positional path may contain `=`
    (`a=b.mie`) and `-o=out.csv` remains the non-spelling it always was.
  - **Only the first `=` separates**, so `--mux-delimiter==` sets the delimiter
    to `=`.
  - **A value-less flag declines a joined value** rather than setting itself
    and discarding it. `--no-mux=true` was already a usage error; the arm now
    guards on `bare()` so the token falls through to the same unknown-option
    message, quoting itself in full.

  The same guard is what keeps `--version=1` from being read as a version
  request: it declines in the global loop and is handed back as the subcommand
  token, reporting `Unknown command: "--version=1"` exactly as before.

  **No behaviour change.** A 177-case battery — every valued flag in both
  spellings with a good value, a bad value, an empty value and a missing value;
  the value-less flags; both globals; and the shapes where the two forms could
  part company — was recorded from a binary built at the previous commit and
  replayed against the refactored one, comparing exit code, the first two lines
  of stderr, stdout, and the SHA of any CSV produced. All 177 identical.

  Seven Rust tests now cover this directly, where there were three. The sweep
  compares *parsed structures* rather than exit codes, because a spelling that
  parsed but dropped its value would still exit 0 — and it is only writable as
  a sweep because the cursor exists. Both were proven non-vacuous by planting:
  disabling the split fails 7 tests, and porting C++'s exact off-by-one into
  Rust fails precisely the two empty-value tests.

  Python had no tests for either spelling — it gets the joined form free from
  `argparse`, so nothing looked like it needed proving. It has seven now, so a
  future move away from `argparse` cannot drift silently.

### Fixed

- **Help text and log messages rendered as garbage on a Windows console.**
  `mie-decoder --help` opened with `mie-decoder ΓÇö DDC MIL-STD-1553 MIE binary
  decoder`, and the IRIG day-of-year advisory ended `... ΓÇö see
  docs/VENDOR-CSV-DIFFS.md ┬º5`. All three implementations did it, identically.

  Nothing was wrong with the bytes. Every implementation writes UTF-8
  unconditionally, and a stock Windows console runs at the OEM code page — 437
  in a US install — where the em dash's three UTF-8 bytes `e2 80 94` are drawn
  as three unrelated glyphs. It is worth naming the real cost: unexplained
  characters in a decoder's own diagnostics look like a memory-safety bug, and
  that is how this was reported. The C++ suite passes clean under ASan, UBSan,
  LSan and Valgrind; the same three bytes come out of the Rust binary.

  Only three characters were ever involved — U+2014 em dash, U+2192 arrow,
  U+00A7 section sign — over 46 sites. They are now `--`, `->` and `section`.
  Wording is unchanged otherwise, and all three implementations still emit
  byte-identical text.

  **This had been half-fixed once already, and the half is the interesting
  part.** `L2-CLI-014` (v2.12.0) moved the `dump` report to ASCII but wrote an
  explicit carve-out: stdout *payload* was bound by the rule, stderr *prose*
  was not, on the reasoning that payload is piped and diffed while prose is
  merely read. That inverts the risk. A payload is usually read by a program,
  which either copes or fails loudly; prose is read by a person, on whatever
  console they have. Every one of the 46 sites was in the exempted half. The
  requirement now binds **every byte written to stdout or stderr**, and forbids
  fixing a rendering problem with `SetConsoleOutputCP` or a stream-encoding
  reconfiguration — both are per-platform, both leave redirected output wrong,
  and the former adds OS surface to a platform layer confined to five concerns.

  Enforced by `scripts/assert-ascii-output.py`, wired into
  `scripts/repo-hygiene.sh` and the pre-commit hook. It parses string literals
  rather than grepping bytes, which is what the previous pass needed and did
  not have: C++ spelled its em dash `"\xE2\x80\x94"`, pure ASCII on disk and
  non-ASCII on the wire, so a byte-level scan of the tree reported it clean.
  Comments and doc comments are exempt — they are never written to a stream —
  and test sources are not scanned, since they legitimately build non-ASCII
  strings to prove the decoder handles them (the UTF-8 `?` glob cases).

  Removing the characters also retired two C++ workarounds that existed only to
  survive them. C++ hex escapes are greedy, so `"\x92BC"` is one out-of-range
  escape rather than an arrow followed by `BC`; two literals had been split
  mid-word to dodge it, one of them with a comment noting that the neighbouring
  near-identical string did not need the split only because `R` is not a hex
  digit.

- **C++ answered help and version from a scan over the whole command line.**
  It could not tell a help token apart from one being consumed as a flag's
  **value**, and did not know where the subcommand began. C++ was the only
  implementation doing either, on six shapes:

  | invocation | was | now (and in the other two) |
  |---|---|---|
  | `decode rec.mie --version` | prints version, `0` | unknown option, `4` |
  | `--log-level -h` | prints help, `0` | usage error, `4` |
  | `--config --help` | prints help, `0` | usage error, `4` |
  | `decode --mux-delimiter -h rec.mie` | prints help, `0` | usage error, `4` |
  | `decode --mux-delimiter -h --help` | prints help, `0` | usage error, `4` |
  | `--log-level --nonsense --help` | prints help, `0` | usage error, `4` |

  Help and version are now resolved **positionally**, and the property the
  scan actually existed for — help remaining reachable from a broken command
  line — is kept by asking whether a help request is still *pending* when the
  parse gives up, rather than assuming one anywhere on the line counts. The
  deferred-versus-consumption split is expressed exactly as in Rust: the
  argument reader abandons the rest of the line when a flag cannot take its
  value, so `--log-level -h` has nothing pending.

  Across a 26-shape three-way matrix, outliers went from **7 to 1**. The
  survivor is `--max-sort-group abc --help`, where Python exits `4` because
  `argparse` wires that one flag's conversion in via `type=int` — an
  implementation accident, recorded in `L2-CLI-017` rather than reproduced.

  Four conformance cases now pin this cross-implementation; none of them could
  be written before, because pinning requires all three to agree. Both
  directions were proven non-vacuous by planting: making the reader's abandon
  a no-op fails the consumption case, and making "is help pending" always
  false fails the deferred case.

- **`expected_stderr_contains` was never evaluated on a negative conformance
  case.** The assertion sat *after* the `expected_exit != 0` short-circuit, so
  on any case with a non-zero expected exit the needle could not fail. **Seven
  cases** carried a dormant assertion — five of them pre-existing, including
  the ones meant to pin *which* error a strict-mode or merge-incompatible run
  produces rather than merely that one occurred.

  Found while adding a case that depended on it, and proven by planting an
  impossible needle: the case still passed. All seven turn out to have been
  correct, so nothing was hiding behind it — but the check now runs, and the
  same plant fails.

  This is the third gate in this area found to be asserting less than it
  appeared to, after the trace-matrix collector dropping requirement ids and
  the diagrams job never comparing its renders.

- **Rust reported the error instead of answering a pending `--help`.**
  `decode --nonsense --help` printed help in C++ and Python and reported the
  unknown option in Rust, which was the last shape in the CLI where Rust was
  the odd one out. An operator whose command line is wrong is the one most
  likely to be asking for help, and answering the question they did not ask is
  the least useful response available.

  Help now outranks a **deferred** diagnostic — an unrecognised option, or a
  value this CLI validates after parsing — in all three. It still does **not**
  outrank a failed value *consumption*: `--log-level -h` and `--config --help`
  remain usage errors, because a flag that cannot take a value leaves the rest
  of the command line uninterpretable, so nothing in it can be attributed to
  the right flag.

  That split is `argparse`'s, and it is a real distinction rather than an
  accident: it raises immediately when it cannot structurally take a value and
  defers everything else until after the help action has had its chance. Rust
  expresses it by draining the argument iterator at the point of a consumption
  failure, so the "is help still pending" test answers `false` there — which
  also avoids adding a variant to the public `ParseError` and tripping the
  SemVer gate.

  Six shapes moved from Rust-is-the-outlier to all-three-agree. One moved the
  other way: `--max-sort-group abc --help` now exits `0` in Rust as it does in
  C++, where Python exits `4`. Python's answer there comes from `argparse`
  wiring that flag's conversion into parsing via `type=int` while
  `--time-format` and the filter flags are validated afterwards — a fact about
  which validations live inside `argparse`, not a rule, and reproducing it
  would mean copying a list rather than a principle. `L2-CLI-017` records both
  that and the one shape where **C++** is still the outlier (it answers help
  offered as a flag's value), which is what currently blocks pinning any of
  this in the conformance suite.

- **The trace-matrix collector read only the first requirement id per marker.**
  `@pytest.mark.requirement("L2-WRT-022", "L3-WRT-003", "L2-CFG-001")` is used
  23 times in the Python suite, and the collector took `args[0]` and discarded
  the rest — so a requirement named second or third lost the artifact that
  verifies it and the matrix understated its coverage. Nothing failed: the row
  still rendered, just with fewer artifacts than exist, which is the same
  silent-undercount the collector had once before with Rust attributes. Found
  because it dropped one of the links added by this change. Twelve
  requirements regained artifacts.

- **C++ answered `--version` anywhere on the command line, and `--HELP` as
  help.** Both were found while making room for `--`, and in both C++ was the
  only implementation doing it. The version and help scan ran over the whole
  argument vector, so `decode rec.mie --version` printed the version and exited
  `0` where Rust and Python both report an unknown option — and the scan would
  equally have answered a `--version` sitting *after* a `--`, which is what
  made it incompatible with the new separator. The scan now stops at the first
  `--`.

  Separately, `is_help_flag` compared `--help` case-insensitively, so `--HELP`
  and `--Help` printed help. Version **is** case-insensitive in all three by
  design; help is not, in any of them. The asymmetry is deliberate and is now
  spelled out where the predicate is defined.

  Deliberately **not** changed: `decode --nonsense --help` prints help in C++
  and Python and is a usage error in Rust. That is a pre-existing difference of
  opinion about whether help outranks a malformed command line, C++ agrees with
  Python today, and levelling it is a separate decision from adding `--`.

- **C++: `--flag=` (an empty value) was reported as an unknown option, exit 4,
  where Rust and Python exited 0.** Writing the conformance cases above turned
  up a live divergence rather than the documentation gap they were meant to
  close.

  `ArgReader::take_value` matched the joined form on
  `token.size() > prefix.size()` — one character too strict. `--exclude-rts=`
  is exactly the prefix, so it failed that test, then failed the exact-name
  test below it, and fell through to "unknown option". Rust and Python both
  hand the empty string to the flag's **own validator** and let it decide:
  `--exclude-rts=` is an empty filter and decodes normally, while
  `--mux-delimiter=` is refused as non-empty-required. C++ now does the same,
  and the three agree on exit code and on output bytes.

  Two things had hidden it. The existing C++ sweep across sixteen flags
  compares the two spellings **byte for byte**, which is the right check, but
  every case carries a non-empty value so none could reach the boundary. And
  `test_cli.cpp` already asserted that `--mux-delimiter=` exits 4 — which was
  true before the fix for the wrong reason, as an unknown option that never
  reached the validator. The exit code alone could not distinguish the two, so
  the new test asserts the *accepting* half by bytes: `--exclude-rts=` must
  produce the same CSV as omitting the flag entirely.

  A third sign was already in the source: `parse_integer` carries a
  "requires a number, got an empty value" message that the joined form could
  never trigger.

  Rust and Python are unaffected — no behaviour change in either.
  `flag-eq-form-empty-value` pins it for all three.

## [2.13.0] — 2026-08-23

### Changed

- **Nothing checked that the three implementations agreed on the version, and
  the release checklist named four of the five places it lives.** C++ was added
  to this repository after that checklist was written and never got added to it,
  so `mie-decoder --version` could have reported a different number depending on
  which implementation an operator ran — while every release to date has been a
  **joint cut** shipping one version from one tag.

  `repo-hygiene.sh` now fails when any of **six** sources disagree, naming the
  offenders: `rust/Cargo.toml`, `rust/Cargo.lock`, `python/pyproject.toml`,
  `cpp/src/cli.cpp` (`kVersion`, what `--version` actually prints),
  `cpp/CMakeLists.txt`, and the **two CLI smoke assertions** in `cpp-ci.yml`
  that compare `--version` output against a literal string.

  Six, not five, because the first cut to use this gate still missed the smoke
  assertions — the gate passed locally and CI failed on
  `mie-decoder 2.12.0` != `mie-decoder 2.13.0`, on both the Linux and Windows
  C++ jobs. The gate was widened to cover them and then proven against exactly
  that case: with one assertion left at the old version it reports
  `cpp-ci.yml --version smoke 2.12.0 2.13.0` and fails. The `MAINTAINER-GUIDE`
  checklist names all six, so the written procedure and the gate agree.

- **`docs/OPEN-DECISIONS.md` is retired.** It held questions blocked on a
  maintainer's judgement rather than on effort. Three of its four are now
  settled — the merge-overwrite question (closed, no change, pinned by the
  `clobber-*` conformance cases), the C++ coverage and fuzz gates (both built),
  and SonarCloud C++ analysis (added, zero findings) — and the fourth, Phase 3
  release artifacts and the versioning scheme, moves to `docs/ROADMAP.md`, which
  is where work waiting on direction belongs.

  **The reasoning was not deleted; it was already living where it is acted on.**
  The libFuzzer decision is in `cpp/tests/test_fuzz.cpp`'s header, the coverage
  floors in `cpp/Makefile`'s threshold comment, every Sonar suppression carries
  its own justification in `.github/workflows/sonarcloud.yml`, and this
  CHANGELOG records all three. A register that only pointed at those was one
  more place to keep in step.

  All seven files that referenced it were rewritten rather than left with
  dangling `#N` pointers, and `CLAUDE.md`'s documentation index drops the entry.

- **The one check that compares implementations was gated behind one
  implementation's path filter.** `cross-impl-differential` runs `run.py` with
  no `--only` / `--skip`, which makes it the only job that exercises all three
  together and therefore the only one that can run the differential
  config-parser checks. It lived in `cpp-ci.yml`, filtered to `cpp/**` — so the
  check written to catch a divergence **between** implementations only ran when
  the C++ tree changed. A Rust or Python change introducing a config-parser
  divergence would not have been caught by it. It now lives alone in
  `.github/workflows/differential.yml`, unfiltered.

- **A conformance-manifest change was not verified against C++.**
  `cpp-ci.yml`'s filter covered `cpp/**`, its own file and the three
  `assert-*.sh` scripts — not `tests/conformance/**`. But `manifest.json`
  defines what **every** implementation must satisfy, and `ci.yml`'s conformance
  job runs `--skip cpp`. So a case added to the manifest could break C++ and
  merge green with nothing having run it. `tests/conformance/**` is now in the
  filter.

  Both gaps surfaced from a single observation — that a PR showed 54 checks
  where the usual number is 80 — rather than from any failure. Neither had bitten
  yet: the manifest change that exposed them was verified by hand against all
  three before pushing.

- **Moving the job would have dropped it from doc-drift coverage**, because
  `repo-hygiene.sh` reads only `ci.yml` and `cpp-ci.yml` when checking that
  every CI job has a row in the maintainer guide. It now reads
  `differential.yml` too, and that widening was proven by removing the row and
  confirming the check fails.

  `cpp-ci.yml`'s header also still advertised libFuzzer among the modern tier's
  instrumentation; the fuzz harness is a deterministic Catch2 case that runs on
  every tier, so that note is corrected.

- **The output-overwrite contract is now pinned by conformance, and
  the merge-overwrite question is closed as no-change.** Two cases:
  `clobber-default-overwrites` (an existing destination is replaced, exit 0,
  output matches the oracle) and `clobber-refuses-when-configured`
  (`no_clobber = true`, exit 1).

  The behaviour itself did not change and was never wrong. `preflight_output`
  runs two guards that are easy to conflate: the **input/output collision**
  check (L2-WRT-014) is always on and cannot be disabled, while **no-clobber**
  (L2-WRT-017) is off by default in all three implementations. So the default
  protects the thing you are decoding and not an unrelated output — which is the
  intended asymmetry, since destroying your input is never what you meant while
  replacing an output usually is.

  **What was missing was a test, not a change.** The three agreed by
  construction and nothing would have caught one drifting.

  The pair is self-checking: the refusal case can only pass if the destination
  really was pre-created, so a runner that ignored the new `pre_existing_output`
  manifest field would **fail** it rather than pass vacuously. Confirmed by
  planting exactly that — with the field removed the decode exits 0 and the case
  fails.

  The manifest was edited textually rather than via a JSON round-trip:
  `json.dumps` reformats the whole document, which turned a two-case addition
  into a 1,418-line diff on the first attempt.

- **The C++ tree has a fuzz harness, and the decision went against the delivery
  plan's assumption.** The open question was whether the fuzz gate
  should be corpus replay only or also a timed exploratory run. Both options
  presupposed **libFuzzer** — and checking what Rust and Python actually do
  dissolved the question. Neither uses it. Both run a **deterministic seeded
  generator inside the ordinary test suite**: xorshift64, fixed seed, 256
  iterations by default, scaled by `MIE_FUZZ_ITERATIONS` in a nightly burn-in.
  C++ now does the same, in `cpp/tests/test_fuzz.cpp`.

  Three reasons that shape beats libFuzzer **in this tree specifically**:

  1. **libFuzzer needs clang.** The defining constraint here is GCC 4.8.5
     (ADR-0001). A libFuzzer target would run on one of four C++ tiers and *not*
     the one this implementation exists for. An ordinary Catch2 case runs on all
     of them, and again under ASan/UBSan and Valgrind — where a real memory
     fault on random input actually surfaces.
  2. **Same generator, same seed, same inputs in all three implementations.**
     `L1-ROB-001` is a shared requirement; testing it three different ways would
     produce three incomparable results. This makes a behavioural divergence on
     identical bytes detectable.
  3. **It is deterministic**, so a required gate cannot go red on an input
     nobody's change produced — the exact objection #2 raised against a blocking
     timed run.

  **There is no corpus, and that is not an omission:** the generator reproduces
  its inputs exactly, so a committed corpus would be storage for something
  already derivable. The "timed run" is simply a larger iteration count, so both
  halves of the original question are satisfied without either being a separate
  artifact.

  No new CI job was needed for the required gate — the harness is a test case,
  so `make check` already runs it on every tier. `fuzz.yml` gained a `fuzz-cpp`
  job purely to turn the count up nightly, alongside the existing Rust and
  Python ones.

  Verified non-vacuous rather than assumed: assertion counts scale with the
  iteration count (1 -> 7, 256 -> 1,826, 2000 -> 14,021), which also shows the
  random bytes really are decoding into records rather than being rejected at
  open. And the sanitizer tier runs over those inputs -- the concrete payoff of
  the shape choice, since a clang-only libFuzzer target would have given that on
  one tier instead of all of them.

  `make verify-ci` caught the one problem: clang-tidy 20's `bugprone-empty-catch`
  on the deliberately empty `catch (const MieError&)`. That catch IS the
  assertion -- swallowing the documented error path is the point, and any other
  exception type escaping is the failure. It now carries a NOLINT, matching the
  precedent already in `filter.cpp`, which is the machine-readable way to say
  what a comment alone could not.

- **The last two findings, both created by batch 4's own fixes.** Moving a
  `shared_ptr` to `make_shared` made the explicit type redundant (`cpp:S5827`),
  and making two reader helpers `const` exposed their **caller** as const-able
  too (`cpp:S5817`). Neither existed before the fix that caused it.

  That is the third time in this sweep that resolving one rule created work
  under another — `S3230` produced `S3490` in batch 2, range-for produced
  `misc-const-correctness` and `useStlAlgorithm` in batch 3, and now this. Worth
  stating plainly: on a codebase this size, a static-analysis count is a moving
  target while you are working it, and "fixed N" is only true after the next
  full branch analysis, not after the change that fixed them.

- **Fourth and final batch of the SonarCloud C++ sweep. 402 findings became
  zero: 313 fixed across four batches, 89 recorded here with the reason each is
  not a defect in this tree.** That ratio is what working down a list until only
  judgement calls remain actually looks like, and suppressing the remainder is
  what makes the count **actionable** — a new finding now stands out against
  zero rather than being lost among 89.

  Fixed in this batch: `cpp:S5817` (two reader helpers that decide without
  mutating are now `const`) and `cpp:S5950` (two `shared_ptr` constructions
  moved to `make_shared` — one allocation, and no window where the raw pointer
  is unowned).

- **Three reasons account for most of the suppressions, and none of them is
  "we disagree with the rule".**

  **Cross-implementation contract.** `cpp:S3642` wants `enum class` at 16
  sites. Those enums carry **wire values** and are used as integers throughout,
  mirroring Rust's `#[repr(u8)]` and Python's `IntEnum`. `enum class` removes
  the implicit conversion, so adopting it means a cast at every use and a
  C++-only divergence from a shape the other two implementations share.

  **The C++11 / GCC 4.8.5 floor.** Already the reason `cpp:S3230`'s in-class
  initializers are refused; here it also shapes what is worth doing at all.

  **The C API leaves no choice.** `free` on a `realpath(path, nullptr)` result
  is *required* — `delete` there is undefined behaviour. The `const_cast` for
  `munmap` is required by its signature. The `void*` in the fd encoding **is**
  the platform layer's opaque handle, spanning a POSIX `int` and a Windows
  `HANDLE`; that is the abstraction's entire purpose.

  The rest are decisions already recorded at the code: mutable globals that are
  mutable by design, `memory_order_relaxed` on a standalone flag, `Optional`'s
  deliberately implicit constructors, a captureless sink pointer, a lookahead
  scanner whose index is the point, and three catch sites that already carry
  `NOLINT`. `cpp:S107`, `S3776` and `S924` get the same answer as Rust's
  `too_many_lines` and `cognitive_complexity`: exhaustive dispatch tables read
  by lookup, not top to bottom.

- **Two are deferred rather than rejected, and are written into
  `docs/ROADMAP.md` so the suppression does not quietly become a decision
  nobody revisits.** `cpp:S3642` (`enum class`) is worth doing as a deliberate
  three-implementation decision about how message types are spelled — not as a
  lint cleanup. `cpp:S2807` (hidden friends, 18 sites) is genuinely better C++
  and a reasonable rider on the next change that touches `models.hpp`.

- **Third batch of the SonarCloud C++ code smells — and the batch where the two
  analysers turned out to contradict each other.** `cpp:S1669` (`module` is a
  keyword in C++20, so the log parameter is renamed) and `cpp:S5350`
  (`readdir`'s result is only read, so the pointer is pointer-to-const). Small,
  because the large part of the batch was attempted and reverted.

- **`cpp:S5566` (39 findings, "use a range for-loop") is suppressed, because
  satisfying it fails a required gate.** All 39 were applied with clang-tidy's
  `modernize-loop-convert` and the result was backed out. Converting an index
  loop to a range-for is precisely the shape cppcheck's `useStlAlgorithm`
  matches, so **cppcheck — which runs with `--error-exitcode=1` and blocks the
  merge — then demanded a dozen plain loops be rewritten as `std::transform` /
  `std::any_of` / `std::find_if` with lambdas.**

  Each attempt to settle it surfaced more:

  | round | what broke |
  |---|---|
  | 1 | clang-tidy 20 `misc-const-correctness` ×11 — every generated element was non-const, and the check does not exist before LLVM 15, so the local clang-tidy 14 was silent about work it had just done |
  | 2 | a compile error from an incomplete rename |
  | 3 | cppcheck `internalAstError` — `modernize-use-equals-default` had re-applied the `throw() = default` that batch 1 reverted **for cppcheck's sake** |
  | 4 | cppcheck `constVariablePointer` + `useStlAlgorithm` ×14 |

  `S5566` is **MINOR**; cppcheck blocks the merge. Rewriting readable loops as
  algorithm calls with lambdas is a legibility trade this project has not
  chosen, and it is not worth making to satisfy a minor rule that a required
  gate then rejects. The C++11 floor sharpens it: no structured bindings, no
  ranges — the lambdas carry the whole readability cost with none of the modern
  compensations.

  **A prose comment does not stop an automated fixer.** Round 3 is the lesson:
  batch 1 reverted two `throw() = default` destructors and left a comment saying
  why, and clang-tidy re-applied one of them *directly beneath that comment*.
  Both sites now carry `NOLINT(modernize-use-equals-default)`, which it reads.

- **Two more findings suppressed because acting on them would be wrong, not
  merely unnecessary.** `cpp:S1905` on `reader.cpp` calls a cast redundant; it
  is **load-bearing on Windows**, and the comment above it already said so —
  GCC is quiet, MSVC at `/W4 /WX` makes C4244 an error. SonarCloud analyses this
  tree on Linux and sees only the half where the cast does nothing. `cpp:S125`
  on `decode.hpp` flags a **section banner** — a requirement id inside a dashed
  rule reads as commented-out code to the heuristic.

- **Second batch of the SonarCloud C++ code smells: redundant member
  initializers removed** (`cpp:S3230`, the largest single rule at 176 findings).
  100 lines deleted across 13 files; five constructors now have empty
  initializer lists because every entry in them was redundant.

  **This batch could have introduced uninitialised reads, so it was not applied
  by hand.** `std::string s` with `s()` is genuinely redundant — value-init and
  default-init do the same thing for a class type — but `int n` with `n()` is
  not: value-init zeroes it while default-init leaves it indeterminate. Sonar's
  message does not draw that line. clang-tidy's
  `readability-redundant-member-init` does, so it made the edits.

  Two things confirm it drew the line correctly. It left `optional.hpp` alone:
  `Optional<T>`'s own `value_()` cannot be proven redundant for a template `T`
  that might be POD, and removing it *would* have been a bug. And every removal
  in the outer classes was an `Optional<T>` member, whose user-provided
  `Optional() : value_(), has_value_(false) {}` initialises the flag either way.

  **Valgrind is the proof, not the reasoning:** `ERROR SUMMARY: 0 errors from 0
  contexts` over the full suite. Use of an uninitialised value is exactly what
  memcheck detects, so a wrongly removed initializer would have surfaced there
  rather than as a silent field of garbage months later.

  clang-tidy deletes the entry text but not the layout around it, and
  clang-format does not collapse what is left — an emptied list leaves
  `Foo::Foo()\n\n{}`, a partly emptied one leaves a blank line mid-list. Those
  14 sites were tidied separately, as pure whitespace with no token added or
  removed.

  **The first attempt at this batch made the gate worse, which is worth
  recording.** Emptying seven constructors turned each into a CRITICAL
  `cpp:S3490` ("use `= default`"), and `new_maintainability_rating` fell to D.
  Removing findings under one rule created them under another — visible only
  once SonarCloud analysed the branch, because this was the first C++ change
  large enough for pull-request analysis to have anything to say. All seven are
  now `= default`, which is also strictly safer: under value-initialisation a
  defaulted constructor zero-initialises first, where a user-provided empty body
  does not. None carries a dynamic exception specification, so the cppcheck 2.13
  `throw() = default` parser bug from batch 1 does not apply.

- **`cpp:S3230` is suppressed for `cpp/**`, and the reason is a real constraint
  rather than a preference.** The rule has two distinct messages behind one key.
  The first — "redundant with default initialization behavior" — was a genuine
  cleanup and is fixed above. The second prescribes a **default member
  initializer**, and in C++11 a class with an NSDMI is *not an aggregate*:
  adopting them would make `Foo{1, 2}` ill-formed on **GCC 4.8.5**, the SLES 12
  system compiler this implementation exists to support. MSVC compiles as C++14
  where the same code is legal, so the breakage would appear only on the target
  platform, only at the fidelity tier. That is precisely why `CONTRIBUTING.md`
  lists aggregate-initialising an NSDMI class as one of two banned constructs.

  Coverage of the useful half is not lost: clang-tidy's
  `readability-redundant-member-init` runs at LLVM 20 in the clang-tidy gate and
  is the sharper tool anyway, since it distinguishes a redundant
  `std::string s()` from a load-bearing `int n()`. Sonar cannot suppress one
  message of a rule, so the trade is the whole key for the better checker.

- **`cpp:S5950`:** `MieError`'s message is now built with `std::make_shared`
  rather than `new` — one allocation instead of two, and no window where the raw
  pointer is unowned. The `<memory>` include went in unconditionally; its first
  placement landed inside a `_WIN32` `#else` branch, which would have compiled
  on Linux and failed on the shipping Windows build.

  Verified: format-check, the full suite on host g++ (475 cases), **valgrind
  clean**, ASan + UBSan + LSan, the **GCC 4.8.5** fidelity tier (475 cases,
  3,692,836 assertions), MSVC at `/W4 /WX` (476 cases, no warnings),
  `make verify-ci` across all six stages at CI tool versions, 90/90 conformance,
  and the three invariant gates.

- **First batch of the SonarCloud C++ code smells: 94 mechanical modernisations.**
  `cpp:S4962` use `nullptr` (45), `cpp:S5827` use `auto` where the type is
  already spelled in the cast (32), `cpp:S3490` `= default` (7), `cpp:S6003`
  `emplace_back` (5), `cpp:S5416` `using` over `typedef` (3), `cpp:S3471`
  `override` (2). Applied with clang-tidy's own fixers rather than by hand, from
  the real compilation database — which carries `-std=c++11`, so no C++14
  construct can enter this way.

  The `auto` conversions were reviewed rather than accepted wholesale, since
  `decode.cpp` is a wire-format decoder where the declared width of a field is
  documentation. They are kept because nothing is lost: `const auto day =
  static_cast<uint16_t>(...)` still names `uint16_t` on the same line. That is a
  different case from the Rust `match_same_arms` rejection, where collapsing arms
  would have deleted per-format comments.

- **Two of those fixes were reverted, and the reason is worth keeping.**
  `~MieError()` and `~ConfigError()` carry a dynamic exception specification, and
  **cppcheck 2.13 cannot parse `throw() = default`** — it reports
  `internalAstError`, which degrades its analysis of the entire translation unit.
  Suppressing that would have blinded cppcheck to real findings in `config.cpp`,
  so the empty body stays and `cpp:S3490` keeps flagging those two sites. A
  parser error costs more than a style finding.

  Found by `make verify-ci` — CI's tool versions in a container — and by nothing
  else. clang-tidy 20 passed, the host g++ build passed, the **GCC 4.8.5**
  fidelity tier passed, MSVC passed, and all 90 conformance cases passed. Only
  the cppcheck version CI actually runs objected. This is the third time this
  release that a local tool a version behind CI reported clean on something CI
  would reject.

- **SonarCloud's C++ analysis turned `main` red, and this fixes it.** Two gate
  conditions failed on the first branch analysis after the C++ tree joined:
  `new_coverage` at 51.7% against an 80% threshold, and `new_security_rating` at
  E against A.

  **The coverage half was self-inflicted.** `cpp/src` and `cpp/include` were
  added as Sonar sources without a C++ coverage report, so all 66 files counted
  as zero-covered — a failure about the configuration, not the code. The
  SonarCloud job now builds the coverage tier and emits gcovr's native
  `--sonarqube` XML.

  That emitter has a trap worth recording: run from `cpp/`, it writes paths like
  `src/reader.cpp`, which Sonar resolves against its project base dir, matches
  against nothing, and applies as **zero coverage while every step reports
  success**. gcovr therefore runs from the repository root so the paths come out
  as `cpp/src/reader.cpp`, and the step asserts both that the report is
  non-empty and that it contains repo-relative paths — because the failure is
  otherwise indistinguishable from a pass.

  **The security half is two findings, both suppressed with the reasoning
  written out**, scoped per-rule and per-file exactly as the existing
  `reader.py` / `config.py` entries are:

  - `cpp:S2083` on `dump.cpp` — "tainted value is leaking" at the `fwrite` in
    `emit()`. S2083 is *path injection*, and there is no path: the tainted value
    is the report **text**, the sink is stdout, and rendering an operator-named
    file to stdout is the entire purpose of `dump`. The value reaches no
    filesystem call, no shell and no interpreter.
  - `cpp:S2612` on `platform_posix.cpp` — the `0644` mode on the atomic-write
    temp file. It is the mode *argument*, still subject to umask, and it is
    **stricter than the other two implementations request**: Rust's
    `File::create` and Python's `open(..., "w")` both pass 0666. Tightening C++
    alone would make the three emit different permissions from the same input.

- **Correcting a claim made when the analysis first landed.** It was reported
  here as adding "zero findings". That number came from a pull-request analysis,
  which only examines **changed files** — and no C++ file changed in that PR. The
  full branch analysis on `main` reports **404 findings, every one from `cpp/`**:
  402 code smells, 2 vulnerabilities, 0 bugs. Rust and Python contribute none.

  So the caveat attached to adding it — *measure before making it blocking* — was
  the right one to keep, and the first measurement was taken against the wrong
  scope. The 402 code smells do not fail the gate (maintainability on new code
  is still A) and are being addressed separately; they are concentrated in a few
  rules, `cpp:S3230` alone accounting for 176.

- **The C++ tree has a coverage gate, and all three implementations now gate on
  90% lines.** `make -C cpp coverage` rebuilds from clean with `--coverage` at
  `-O0`, runs the full Catch2 suite and gates with `gcovr`; it is wired as a
  required `cpp-ci.yml` job and is the same target a developer runs. Closes the
  coverage half of the C++ gate question.

  Measured rather than guessed, which mattered:

  | | lines | regions / branches | floor |
  |---|---|---|---|
  | Rust | 90.40% | 89.37% (llvm-cov regions) | 90 / 89 (was 87 / 86) |
  | Python | 95.50% | 92.89% (branches) | 92 combined, unchanged |
  | C++ | 90.9% CI / 91.1% local | 81.5% CI / 76.5% local (gcov branches) | 90 / 76 (new) |

  That entry had recommended "85% initially". 85% would have been **below** the
  line coverage the C++ suite already had, so it would have gated nothing — the
  case for its own advice to measure first rather than pick a number in advance.

  Both C++ floors sit at today's numbers so nothing can regress, and both are
  meant to rise as tests land.

- **The second column is not the same metric in each language, and the floors say
  so rather than pretending otherwise.** Rust gates on llvm-cov *regions*, Python
  and C++ on *branches*. `gcov` counts every conditional the compiler emits,
  including ones the source never spells out, so it reads systematically lower
  than LLVM region coverage for equally well-tested code. Forcing the three
  numbers to match would not make them mean the same thing.

  The honest figure, named rather than buried: **C++ branch coverage is 81.5% on
  CI and 76.5% on the WSL2 host** — several hundred uncovered branch outcomes
  either way, concentrated in `reader.cpp`, `writer.cpp` and `merge.cpp`. Some of
  that gap is the metric; not all of it. Raising it is real work that has not
  been done.

  **That 5pp spread is itself worth recording: gcov branch coverage is not
  portable across compiler versions.** The same suite over the same sources
  measures 76.5% on g++ 11.4 and 81.5% on CI's ubuntu-24.04 g++, because branch
  counts follow the conditionals the *compiler* emits rather than the ones the
  source spells out. Lines barely move (91.1% vs 90.9%). The single threshold is
  therefore set to hold on the **oldest** compiler in use, leaving CI ~5pp of
  slack; pinning CI to its own higher number would make a local `make coverage`
  pass while CI failed, which is the precise local/CI divergence that has cost
  this project several red builds already.

- **No coverage gate counts the conformance suite** — not Rust, not Python, and
  not the new C++ one. Each measures its own in-process tests. The conformance
  runner drives every CLI out-of-process, so counting it would measure a
  different thing in each implementation and make the three numbers
  incomparable. Paths reached only that way read as uncovered, which is the
  conservative direction. Including it was measured (it moves C++ lines 91.1 ->
  92.8 and branches 76.5 -> 79.5) and deliberately not adopted.

  Two exclusions in the C++ gate are also deliberate: `src/main.cpp`, the
  executable entry point whose body is three calls — the Rust gate ignores its
  own `bin/mie-decoder.rs` for the same reason, which keeps the numbers
  comparable — and `src/platform_win32.cpp`, which is not compiled on Linux so
  gcov never sees it. The second is named in the exclusion list anyway, so it is
  a stated fact rather than an accident of which host ran the build. It is
  covered by the MSVC tier, which has **no** coverage measurement: a real gap
  this gate does not close.

- **The C++ tree is now covered by CodeQL and SonarCloud.** It had been analysed
  by neither: `sonar.sources` was `rust/src,python/src`, and CodeQL's matrix was
  `rust` and `python`. Closes the C++-analysis question, which had been
  deferred on the grounds that "adding a language to a red gate obscures both
  problems" -- a premise that stopped holding once the gate went green.

  - **CodeQL** gains `c-cpp` with `build-mode: manual` and an explicit
    `make -C cpp all`. Deliberately not `autobuild`: this tree has a specific
    build driven by `sources.txt`, and an extractor left to guess at it is how
    you end up analysing less than you think while the job still reports
    success.
  - **SonarCloud** gains `cpp/src` and `cpp/include` as sources and `cpp/tests`
    as tests, with the CFamily analyser reading the **compilation database**
    that `bear` writes rather than using the build-wrapper. That database is
    already what clang-tidy consumes, so Sonar sees the flags the real build
    uses instead of a second description of the build that could drift from
    `sources.txt`. Vendored Catch2 and the per-toolchain `build/` trees are
    excluded.

  Half of that decision's recommendation is **still open**: *measure before
  making C++ blocking*. The tree joins a job that already waits on the quality
  gate, so if the first analysis returns a large pile of style findings, the
  answer is to scope C++ to the security and taint rules -- the ones no other
  gate here runs -- rather than to unpick the integration.

- **Three stale claims corrected in `cpp/README.md`.** Its status block said the
  differential config-parser checks "still compare Rust against Python only" and
  that "there is no conformance gate on `dump` output". Both had stopped being
  true: `config_parity.py`, `config_fuzz.py` and `config_path_parity.py` each
  take an implementation-to-CLI mapping and compare **all pairs**, so the third
  hand-rolled TOML parser is held to the same grammar as the other two, and the
  manifest carries **eight** `dump` cases. Separately, the CI-tier table
  advertised that the modern-g++ tier runs "the sanitizers and fuzzing" -- there
  are no fuzz targets at all.

  Read together, those made the C++ implementation look **less** finished than
  it is on two counts and **more** covered than it is on a third. What is
  actually outstanding is tooling and release work rather than decoder code:
  every module is ported, the binary passes all 90 conformance cases on Linux
  and Windows, and all 31 `L3-CPP` requirements trace to tests. The absent fuzz
  tier is now listed **as absent**, because a missing tier nobody names reads
  like a tier that passes.

- **Python docstrings now document what a function returns, raises and yields
  --- 171 sections across `src`.** This closes the last real asymmetry between
  the Rust and Python lint gates: Rust has denied `missing_errors_doc` since the
  pedantic work, while Python's equivalent family sat unselected.

  Deferred once, on the grounds that ruff's pydoclint family is **preview** and
  gating on it ties the build to rules that shift between versions. That
  premise turned out to be checkable rather than merely plausible: CI installs
  with `poetry sync` against `poetry.lock`, so ruff is pinned to an exact
  version and a preview rule can only change behaviour on a deliberate lock
  bump, never under an unattended run.

  Counts: 111 `DOC201` (returns), 58 `DOC501` (raises), 2 `DOC402` (yields).
  None are auto-fixable, and none were written mechanically -- the same
  judgement as the 34 Rust `# Errors` sections. Several state contracts that
  are the opposite of the obvious reading:

  - `expand_glob` matching nothing and `read_manifest` finding no usable lines
    return an **empty list, not an error** -- which is what lets the CLI tell
    "matched no files" from "could not read the directory".
  - `paths_refer_to_same_file` answers `False` when a path cannot be resolved:
    a destination that cannot be resolved cannot collide.
  - `StandardTimestamp.to_microseconds` returning `None` means *uncalibrated*,
    and is never a timestamp of zero.
  - `_classify_decode_error` can return `EXIT_OK` -- a broken pipe and an
    `--allow-partial` sync loss are both clean exits.

- **`DOC502` is deliberately not selected, and the reason is not "noise".** It
  flags a documented exception the function does not `raise` in its own body,
  and every finding here documents one that **propagates from a helper** --
  `write_csv` naming `MieClobberRefusedError` and `MieWriterError`, `decode.py`
  naming `struct.error` from an unpack. Which exceptions escape is exactly what
  a caller needs; where in the call tree they originate is not. Complying would
  mean deleting true documentation.

- **`explicit-preview-rules` added alongside `preview`.** pydoclint needs
  preview mode, but turning it on alone silently enabled preview rules across
  every other selected family -- 35 findings in `RUF`, `ISC` and friends that
  have nothing to do with docstrings. `explicit-preview-rules = true` means a
  preview rule fires only when selected by its exact code, so the gate widens
  because someone chose to widen it. The four families it defers
  (`RUF069` float-equality in tests, `ISC004`, `RUF067`, `RUF031`) are a
  separate decision, not a silent inheritance.

  `preview` is set under `[tool.ruff.lint]`, **not** at the `[tool.ruff]` top
  level, and that distinction is not cosmetic: the top-level key also switches
  the **formatter** into preview style, which silently restyled six unrelated
  files (hugging brackets in call arguments) on the first attempt at this
  change. Formatting churn has nothing to do with docstring rules, and a
  reviewer would have had to separate the two by hand.

  One genuine finding did surface from preview and was fixed rather than
  deferred: `RUF003` flagged `µ` (U+00B5 MICRO SIGN) in comments, suggesting
  U+03BC GREEK SMALL LETTER MU. MICRO SIGN is the correct character for
  microseconds -- the unit this decoder works in throughout -- so it joins
  `allowed-confusables`.

  Tests opt out of the `DOC` rules: a test helper has no callers outside its own
  file, so a `Returns:` section there restates the one-line summary. The rules
  stay on in `src`, where the caller is someone else.
- **`clippy::pedantic` is now denied as a group, not lint by lint.** Enabling
  lints by name only ever catches the ones already known about; the group also
  catches the ones nobody thought to look for. 76 distinct sites — deduped by
  `(lint, file, line)`, because the `--all-targets` counts quoted during the
  earlier slices were inflated 2–20× by compiling the lib once per test binary.
  51 fixed, 25 rejected with the reason recorded beside the exception.

  Most fixes are cosmetic and `cargo clippy --fix` applied them. Two are not:
  the hex-dump loop no longer allocates a `String` per byte, and `DataWords`
  gained `IntoIterator` so `for w in &words` works.

  **The rejections carry the content.** Six lints are allowed wholesale because
  every site is deliberate — most clearly `match_same_arms`, where
  `ModeCodeTxData => 2, // status + data` sits beside
  `ModeCodeRxData => 2, // data + status`. The bodies coincide; the reasons do
  not, and merging arms by equal value would delete the specification the table
  exists to be. `struct_excessive_bools` is the same argument: `DecodeArgs`
  mirrors the CLI surface 1:1 and `DecoderConfig` mirrors the TOML keys 1:1, and
  that correspondence is the point. `decimal_bitwise_operands` is rejected at
  four test helpers where the literals are field values rather than masks —
  `192` is the day-of-year and `15` the hour of "day 192, 15:54:50" — and where
  the lint is itself inconsistent, flagging the hour but not the minute or
  second in the same expression because those sit inside shifts.

  **Consequence, stated in `rust/Cargo.toml` and `CONTRIBUTING.md`:** a clippy
  release can now turn the build red on code nobody touched, because it can add
  pedantic lints. That is the intended trade — it surfaces on a PR rather than
  in a release — but the fix is sometimes a documented exception rather than a
  code change.

- **Two silent gates found while doing the above, both worse than the lint work
  they interrupted.**

  `scripts/build-trace-matrix.py` was **dropping requirement links without
  saying so**. A multi-line attribute between a `/// Requirements:` marker and
  its `fn` fell through the collector's reset, so the marker stayed in the
  source while the artifact vanished from the matrix — which reads as an
  *untested requirement*. The collector now tracks attribute depth. Proven by
  planting a multi-line attribute and confirming `--check` stays clean where it
  previously lost the row.

  A local toolchain that lags CI runs a **weaker gate while reporting clean** —
  not a differently-configured one. This is not hypothetical: this change passed
  a clean local `cargo clippy -- -D warnings` on 1.93 and failed CI on 1.98,
  where `map_unwrap_or` had grown to cover `Result` as well as `Option`, and
  again on `decimal_bitwise_operands`, which 1.98 added outright. The same shape
  applies to C++: `misc-const-correctness` does not exist before LLVM 15, so a
  clean `make tidy` on Ubuntu 22.04's clang-tidy 14 says nothing about the check
  CI runs at LLVM 20 — and 14 emits `bugprone-throw-keyword-missing` false
  positives that 20 does not, inviting "fixes" to code CI is happy with.

  `make tidy` now prints a loud advisory when its clang-tidy does not match
  `LLVM_VERSION`, naming both versions and the two ways to get a real answer
  (`TIDY=clang-tidy-20`, or `make verify-ci` for CI's versions in a container).
  Advisory rather than fatal: an approximate local run is still useful, it just
  must not be mistaken for the gate. `CONTRIBUTING.md` carries the Rust half.

- **Cross-implementation review found one real divergence.** C++ `DataWords`
  exposed `data()`/`size()` but no `begin()`/`end()`, so range-for did not work
  there while Python's `data_words` tuple and Rust's new `IntoIterator` both
  allow it — the one place the three implementations disagreed on how a payload
  is traversed. Added with a test. Python's hexspeak test seed picked up the
  same digit grouping as the Rust fixtures; **C++11 has no digit separator**
  (`'` is C++14), so that half deliberately cannot be ported.

- **`missing_errors_doc` cleared and denied — 34 public fallible functions now
  document when they fail.** Unlike the previous two pedantic slices this one is
  not mechanical: "returns an error if it fails", written 34 times, would be
  worse than nothing. Each section names the actual variants and conditions.

  Writing them surfaced several contracts worth stating precisely, because they
  are the opposite of what a reader would assume:

  - `expand_glob` matching nothing and `read_manifest` finding no usable lines
    are **not** errors — both return an empty list, which is what lets the CLI
    distinguish "matched no files" from "could not read the directory".
  - Under `--allow-partial`, `write_csv` does **not** return an unrecoverable
    sync loss: it commits the rows decoded so far as `<destination>.partial` and
    succeeds (`L2-WRT-016`).
  - `MieFileReader::with_options` fails only on an unusable *file*. A file that
    opens but holds no valid records surfaces from the first `next()`, so
    "unreadable" and "readable but not a recording" stay distinguishable.
  - `paths_refer_to_same_file` errors only on the **input**; a destination that
    cannot be canonicalized cannot collide, so that is `Ok(false)`.

  Verified with `RUSTDOCFLAGS="-D warnings" cargo doc` — the new intra-doc links
  all resolve — and with `cargo-semver-checks` (194/194) before pushing.

- **The Python analogue is real but scoped separately.** Ruff's pydoclint family
  reports 58 `DOC501` (missing `Raises:`) and 111 `DOC201` in `src`. Two reasons
  it is not in this change: the family is **preview**, so enabling it in a gate
  ties us to rules that can shift between ruff versions; and 169 docstring
  sections is its own piece of work, not a rider on a Rust lint.

  **Since landed** — see the "Python docstrings now document what a function
  returns, raises and yields" entry earlier in this section. The
  preview objection was resolved by checking rather than assuming: `poetry.lock`
  pins ruff exactly in CI, so a preview rule cannot shift under an unattended
  run.

  Its `DOC502` findings (5) were checked and are **false positives here**: they
  flag docstrings documenting exceptions that *propagate* from called helpers
  rather than being `raise`d directly — which is precisely what a caller needs
  to know. C++ documents throws in its public headers by convention already,
  with no lint available to enforce it.

- **`doc_markdown` and `must_use_candidate` cleared and denied.** Both were
  `pedantic`, so `-D warnings` had never seen either.

  `doc_markdown` (33): every finding was a real code identifier named in prose
  without backticks — `SPURIOUS_DATA`, `RecordIter`, `iter()` — which rustdoc
  therefore rendered as plain text. None were domain acronyms needing an
  exception, so `doc-valid-idents` was left alone.

  `must_use_candidate` (64): all pure functions or accessors where discarding
  the result is meaningless. The clearest is `cli::run`, which returns the
  process **exit code** — dropping it makes the process exit 0 whatever
  happened. Adding `#[must_use]` is additive, confirmed by running
  `cargo-semver-checks` locally before pushing: 194 checks, 194 pass.

  Counts are lib-only. The `--all-targets` figures (101 and 128) that I quoted
  earlier are inflated, because that flag compiles the library once per test
  binary and counts each copy.

- **Neither lint has a cross-implementation analogue, and the C++ reason is
  structural.** `doc_markdown` is a rustdoc rendering rule. `#[must_use]`'s C++
  equivalent is `[[nodiscard]]`, which is **C++17** — unavailable at the C++11
  floor ADR-0001 sets for GCC 4.8.5 on SLES 12 SP5. The compiler-specific
  spellings (`__attribute__((warn_unused_result))`, `_Check_return_`) differ
  between GCC and MSVC, so adopting one would mean a new portability macro for
  modest benefit. C++ already gets partial coverage free: `bugprone-*` is
  enabled in `.clang-tidy`, which includes `bugprone-unused-return-value`.
  Python has no equivalent construct at all.

- **`log::_emit` is now `log::emit`, marked `#[doc(hidden)]`.** It was `pub`
  purely so the exported `log_*!` macros could resolve it at a call site in
  another crate — a real need, expressed with the wrong tool. `_name`
  conventionally means *unused*, so an item that is used contradicts its own
  name; `#[doc(hidden)]` is the marker that actually says "reachable, but not
  API".

  The old path is kept as a re-export rather than removed. Renaming a `pub`
  item is an API removal, and `cargo-semver-checks` exclusions are **per-lint,
  not per-item** — allowing `function_missing` crate-wide to rename one internal
  helper would have disabled a check worth having. A re-export costs nothing and
  keeps the gate intact; both paths were verified to resolve by compiling
  against each.

  `clippy::used_underscore_items` is now `deny`. My earlier estimate of 77
  findings was wrong: `--all-targets` counts the same library once per target,
  and there were **4**, all the same function.

- **The same class was checked in C++ and Python, and neither needed changing.**
  C++ already calls `mie::log::emit` with no underscore. Python `src` has **zero**
  private-member accesses; the 53 in `tests/` are white-box tests reaching in
  deliberately, which is what a white-box test is for. Both states are now
  gated — ruff's `SLF` family is selected, with `tests/**` exempt — so "already
  clean" becomes "cannot regress" rather than a fact that happened to be true
  today.

- **Fixed, in all three implementations: a valid tick rate could produce a
  timestamp that was fabricated, undefined, or a crash.** Found by turning on
  clippy's cast lints, which are `pedantic` and so had never run.

  `--standard-tick-rate-hz 1e-300` is finite and positive. It passes every
  existing guard, and the conversion then overflows. Each implementation failed
  differently:

  - **Rust** — `as u64` **saturates**, so the answer was `u64::MAX`: a
    fabricated timestamp, indistinguishable downstream from a real one.
  - **C++** — `static_cast<uint64_t>` from an out-of-range `double` is
    **undefined behaviour**, not a clamp. An existing test asserted this call
    *succeeded*, so the suite was pinning UB and passing because it happened to
    produce a number.
  - **Python** — the division overflows to `inf` and `math.floor` raised an
    uncaught **`OverflowError`** out of a decode.

  All three now decline, which is the answer the uncalibrated case already
  gets and for the same reason: a number that cannot be a timestamp must not be
  presented as one. `L2-DEC-017` states the rule, each implementation has a
  test, and a conformance case pins the three together on an input that
  previously produced three different failures.

- **The clippy cast lints are enabled and the crate is clean under them.**
  `cast_possible_truncation`, `cast_sign_loss`, `cast_possible_wrap`,
  `cast_precision_loss` and `cast_lossless` are `deny` in `[lints.clippy]`.

  Every other site turned out to be safe — but the safety lived in a comment
  above a guard, which is a guarantee the compiler cannot check and a later edit
  can separate from its cast. Most are now `try_from`, so the range check and
  the conversion are one expression; the few that remain are `#[allow]` with a
  `reason` naming the invariant that holds them. One fix introduced a cast of
  its own (`u64::MAX as f64`, which rounds *up* and would have admitted a value
  the conversion could not take) and is now an exact `2^64` constant.
- **Ruff was running almost nothing, and now runs a curated rule set.**
  `pyproject.toml` set only `line-length` and `target-version` — no `select` —
  so the gate ran ruff's default `E4`, `E7`, `E9`, `F` and reported "All checks
  passed" against a small slice of the ~800 rules it ships. The same shape as
  the clippy gap that let SonarCloud report five Rust findings the local gate
  called clean: **a tool is only as strict as the rules you asked it for.**

  Twenty-one families are selected. `ALL` is deliberately not the target — it
  includes 1003 `S101` hits for `assert` in a suite where assert is the point.
  Every exclusion is a judgement recorded next to itself, not a silent omission:

  - `PTH123` / `PTH105` — `open()` and `os.replace()` are correct and
    deliberate. `os.replace` **is** the atomic-rename primitive `L2-WRT-015`
    needs, and `open()` in `reader.py` sits on the input path SonarCloud has a
    documented scoped suppression for; rewriting it to satisfy a style rule
    would disturb a taint flow that was analysed on purpose.
  - `PERF203` — a `try`/`except` inside the record loop is the per-record error
    contract, not an oversight.
  - `T201` in `cli.py` only — printing to stdout is that module's whole job;
    the rule stays live everywhere else, which is where a stray debugging
    `print` would be a real defect.
  - `SIM115` in `reader.py` / `writer.py` — those files outlive the opening
    scope by design; a `with` block would close them at exactly the wrong
    moment.
  - `ARG002` in `tests/**` — a pytest fixture is often requested for its side
    effect (`capsys` starts capturing), so an unused parameter is the idiom.

  The confusable-character rules stay **on**, with `–`, `—` and `×` allowed:
  those are prose punctuation used consistently across all three
  implementations, and keeping the rule live still catches a genuinely
  deceptive homoglyph.

  **One real test defect fell out.** Three `pytest.raises(match=...)` patterns
  contained unescaped `.` metacharacters — `match="output.format"` also matches
  `outputXformat`, so those assertions were weaker than they read. Now escaped.

- **The six open SonarCloud findings are fixed, and the reason the Rust ones
  were invisible to `cargo clippy` is now closed.** Four `rust:S1612`
  (redundant closure where a method reference would do, in `filter.rs`,
  `reader.rs` and twice in `writer.rs`), one `rust:S3776`
  (`FilterConfig::should_exclude`, cognitive complexity 17 against a limit of
  15), and one `python:S9073` (a composite assertion in
  `test_conformance_wiring.py`).

  **Why clippy reported none of them.** `cargo clippy -- -D warnings` denies
  only lints that actually *fire*, and clippy's `pedantic` and `nursery` groups
  are allow-by-default — so the gate never saw them. SonarCloud's Rust rules map
  onto clippy's including those tiers. Enabling `pedantic` + `nursery` in this
  crate surfaces 413 warnings the gate reports none of.

  `redundant_closure_for_method_calls` is now denied by name in
  `[lints.clippy]`, so `S1612` cannot recur silently. `should_exclude` split
  into its two genuinely different rules — an *exclude* set drops a record that
  IS listed, an *include* set drops one that is not — which is the boundary its
  own comments already marked, with the four filter fields read off the message
  once instead of twice.

  **`cognitive_complexity` was deliberately NOT enabled.** Matching clippy's
  threshold to Sonar's 15 looked obvious and does not work: it flags
  `should_exclude` *and* two functions Sonar does not report at all
  (`parse_decode` at 23, `with_overrides` at 18). The two tools compute the
  metric differently, so a matching number buys a stricter local rule rather
  than a predictive one — and `parse_decode` scores high because it is a flat
  dispatch chain over two dozen flags, which is a lookup table written as
  control flow. The reasoning is recorded beside the lint list rather than
  rediscovered.

- **`-o -` now means the same thing in all three implementations: a file called
  `-`.** It did not. Rust and Python treated the value as an ordinary path; the
  C++ build, from its CLI port, treated `-` as stdout and said so in its help.
  The CLI-surface-parity gate could not catch it — that gate compares flag
  *names*, not what their values mean.

  The special case is removed rather than added to the other two. `-` for stdout
  is a genuine Unix convention, but every implementation *already* writes to
  stdout when `-o` is omitted, so the magic filename only added a second
  spelling for something that worked — at the cost of making one filename
  unreachable for anyone whose file really is called `-`.

  `L2-CLI-002` previously said only that decode "SHALL accept an optional output
  path", which is silent on this and is precisely what let the three drift. It
  now states that the value is a path and nothing else, that stdout is selected
  by omitting the flag, and that this is the only way to select it — pinned by a
  test in each implementation.

- **The `dump` report is now identical across all three implementations, and
  gated.** It was not: Python used box-drawing (`─`), arrow (`→`) and
  en-dash (`–`) characters where Rust and C++ used `-`, `->` and `-`, so 11
  of 34 lines differed on a typical fixture. `rust/src/dump.rs` claimed to
  mirror `dump.py` "closely enough for diffing", which was false on every one of
  those lines.

  **Python moved to ASCII.** The report is a stdout *payload* — piped,
  redirected, diffed — not prose. Log messages and help text still use non-ASCII
  punctuation and are unaffected; the new `L2-CLI-014` draws that line
  explicitly. ASCII also removes the encoding hazard at its source rather than
  compensating for it: those characters could not be encoded on a redirected
  Windows stdout at cp1252, and the CLI had been forcing the stream to UTF-8 to
  stop the dump aborting.

  Aligning the other way would have required the **C++** implementation to call
  `SetConsoleOutputCP` — new OS surface in a platform layer deliberately
  confined to five concerns, and a global process-state change — to make a
  diagnostic tool render on the one platform (SLES 12 SP5) it was written to
  reach.

  **Fixed on the way: every stdout payload from the Python CLI emitted CRLF on
  Windows.** The UTF-8 stream reconfiguration set the encoding only, leaving
  text-mode newline translation in place. That affected `dump` *and* CSV written
  with `-o -`, in violation of `L2-WRT-012` ("CSV output SHALL use LF line
  endings on every supported platform"). No conformance case caught it, because
  the runner always decodes to a file.

  **Fixed on the way: `dump --raw --offset` past end of file printed a range
  running backwards** — `Range: 0x000186A0-0x000000B2` — because only the end
  bound was clamped. The bytes shown were right (the slice is empty); the header
  said something impossible and disagreed with the other two.

  The runner gained a `dump` mode and eight cases covering both views, a
  windowed range, a record limit, an errored record, both reachable scan-stop
  anomalies, and an offset past EOF. 120 invocations across twelve fixtures and
  ten flag combinations are byte-identical across the three.

- **Every C++ test suite now builds records from one set of wire-format
  helpers.** The Type Word layout, the IRIG triple and the Command Word
  encoding had been copied into four test files. That is the drift
  `writer_fixtures.hpp` already exists to prevent, and where the copy that is
  *wrong* is the one that keeps passing.

  The migration was checked rather than assumed: each shared helper was diffed
  against the copy it replaced before the copy was deleted. Eight of nine were
  identical; `command_word` differed only in returning its final expression
  instead of assigning and returning, which produces the same bits.

  The ninth found a real trap. `test_sync.cpp`'s local `type_word` masked
  nothing, so its "unknown message type" fixture used `0x99` — a value whose
  **bit 7 is set**, and bit 7 of a Type Word is the *bus*, not part of the type.
  The fixture therefore meant "unknown type, bus B" while the test said only
  "unknown type". The shared builder takes the bus as its own argument and masks
  the type to seven bits, so the value became `0x7F`: still an unknown code, and
  now unable to smuggle a second field in through the first.

  `test_decode.cpp`'s `irig_record` hand-packed the same fields at the same
  offsets, including an unmasked Type Word. It now builds from the shared
  helpers — whose `push_irig` defaults turned out to be exactly the time the
  fixture had been spelling out — which is also why the suite gained three
  assertions: `push_irig` carries a microsecond range guard the hand-rolled
  version did not.

  What stays local to a suite is a fixture with one caller. A fixture with one
  caller is not shared code, it is that suite's own setup, and hoisting it would
  make the shared header a junk drawer rather than a statement of the format.

- **The differential config checks now cover every implementation, not a fixed
  pair — and found two real defects on their first run.** `config_parity`,
  `config_fuzz` and `config_path_parity` compared Rust against Python and
  nothing else; the C++ parsers were held only by their own unit tests, which
  pin an implementation against its own reading of the grammar — exactly the
  thing in question when two readings differ.

  The comparison is **all-pairs**: every implementation must agree with every
  other, and a disagreement names the split (`accept: Rust, Python | reject:
  C++`) without nominating a winner. A majority rule would let two
  implementations sharing a bug outvote the correct one; nominating a reference
  would make that reference's quirks normative. Both alternatives are recorded
  in `tests/conformance/differential.py` with the reasoning.

  **Fixed: the C++ build could not open any file at a non-ASCII path on
  Windows** — config, input recording, or output. Two independent causes, both
  invisible to every existing gate. `main(argc, argv)` receives arguments in the
  process ANSI codepage, so a path containing an unrepresentable character
  arrived as `?` before a single line ran; and `std::fopen` interprets a narrow
  path in that same codepage, so even a correct UTF-8 path would not have
  opened. Arguments now come from the platform layer (which re-reads the real
  UTF-16 command line on Windows) and files are opened through it too.
  `scripts/assert-platform-confined.sh` cannot catch either — it bans OS
  *headers*, and neither `argv` nor `std::fopen` needs one.

  **Fixed: the C++ config loader collapsed three distinct path failures into one
  message.** Absent, not-a-regular-file, and unreadable have three different
  remedies; Rust and Python name which, and C++ said only "Cannot open config
  file". The not-a-regular-file case is now rejected before any read, because
  reading a directory fails opaquely and reading a character device can block
  indefinitely.

  A new `cross-impl-differential` CI job runs all three together. It had to
  exist: `conformance` passes `--skip cpp`, `cpp-conformance` passes
  `--only cpp`, and a differential check with one implementation selected skips
  itself — so the three-way contract was checked by nothing while every gate
  stayed green.

- **Fixed: a C++ merge would write over one of its own inputs.** `decode a.mie
  b.mie -o a.mie` truncated `a.mie` while the merge was still reading it. Rust
  and Python both guard this; the C++ merge shipped without the check, and a
  comment in `cli.cpp` asserted — wrongly — that the other two did not guard it
  either.

  The confusion was real rather than careless: the *writer's* guard takes a
  single input path, and all three implementations correctly pass it none on the
  merge path, because the writer receives one stream and cannot know how many
  files fed it. Rust and Python compensate with a separate CLI-level check
  across the whole input set (`check_output_collision` /
  `_check_merge_output_collision`); C++ had only the half that does not apply.

  The check is gated on whether a merge was **requested**, not on how many
  readers survived — `--allow-partial` can drop a multi-input merge to a single
  open reader, and the writer's guard is off for the whole run either way, so
  keying off the surviving count would leave exactly that case unguarded. The
  regression test was confirmed to fail against the shipped behaviour before the
  fix went in.

- **The conformance runner takes a registry of implementations instead of a
  hard-wired Rust/Python pair.** The suite spent its whole life comparing
  exactly two implementations, with every skip, comparison and oracle check
  written as `rust`/`python` variables side by side. Adding a third that way
  meant tripling roughly two hundred lines and getting each one right; it is now
  one `ImplSpec` entry.

  `--rust-only` / `--python-only` become `--only rust,cpp` / `--skip cpp`, with
  the old flags kept as deprecated aliases. The default is **every** registered
  implementation, and opting out is explicit — the alternative, running whatever
  binaries happen to be present, would let a job whose build step quietly failed
  still report a full pass. `tests/conformance/run.py --skip cpp` is what the
  existing Rust/Python CI job now says.

  An implementation still being delivered in phases declares what it cannot run,
  and those cases are **skipped by name in the log** rather than dropped
  silently, so the gap reads as a gap. The phase limitation lives on the
  implementation, not on the case: a merge case is a perfectly good case, it is
  the C++ build that has no merge yet. The CLI-surface-parity check became
  all-pairs and names any implementation it left out.

- **`DELTA` tracking extracted to one module per implementation, so the key has
  one definition.** No behaviour change — every conformance oracle is
  byte-identical — but a latent correctness risk is now closed by construction
  rather than by care.

  `DELTA` was computed in more than one place, with the key spelled differently
  in each. Rust kept a `HashMap<u32, u64>` in `reader.rs`, keyed by a packed
  `(rt, subaddress, direction)` integer, and a separate `HashMap<String, u64>`
  in `merge.rs`, keyed by the `"<rt>:<sa><T|R>"` display spelling. Python was
  worse: its reader called `_compute_delta` twice, once with `msg.delta_key` and
  once with a hand-built f-string, because on the errored-record path the
  message does not exist yet — three spellings of one concept. **Nothing
  anywhere asserted that they agreed on what "the same RT/MSG" means.** They
  did agree, by care rather than by construction, and a change to any one of
  them would have made `DELTA` mean one thing in a single-file decode and
  another in a merged one, silently.

  The arithmetic was duplicated with the key, and had already drifted: the
  reader warns once per key when a clock steps backwards, and the merge path
  did the same computation silently.

  - `rust/src/delta.rs`, `python/src/mie_decoder/delta.py` and
    `cpp/src/delta.cpp` now own the key, the per-key cursor and the gap
    arithmetic. The Rust module is **private** (`mod delta;`), so the extraction
    is invisible to the published API — `cargo-semver-checks` reports no
    required version change.
  - **The tracker does not log.** `observe()` returns a `DeltaOutcome` and the
    caller narrates, the same rule `sync` follows and for the same reason: a
    tracker cannot know whether a backward step deserves a WARN (single-file
    decode: yes, once per key) or is already reported at file granularity (a
    merge naming its unsorted inputs, L2-MRG-006). The "have I mentioned this
    key" bookkeeping does live in the tracker, so the once-per-key promise has
    one owner instead of a set in each caller.
  - `observe()` takes the **Command Word**, not a whole message. That is what
    removes Python's hand-built key: the reader can ask for a `DELTA` before the
    message it will attach to exists.
  - `DeltaOutcome` is deliberately richer than the `Option<f64>` the CSV needs.
    The column cannot distinguish "no gap yet" from "no honest gap" from "no key
    at all", and the caller has to.
  - C++ reached this point with `merge` still unwritten, which is why the shared
    type landed there **before** the second caller: the merge can be built on it
    instead of reproducing the split and having it extracted back out later.
  - New requirement **L3-RDR-001** states the property and why it needed
    stating.

  Each implementation gained a unit test sweeping all 2048 encodable
  `(rt, subaddress, direction)` triples and asserting that the packed tracking
  key and the published display key partition that space identically in both
  directions — the check that could not exist while the two representations
  lived in different modules.

  Two conformance cases were added, because the existing
  `merge-delta-scope-global` case carries exactly **one** key (RT 15, 11R)
  through both inputs, so a key that dropped RT, subaddress or direction
  entirely would still have produced byte-identical output:

  - `delta-keys-single` — reader path, per-file scope
  - `delta-keys-global` — merged timeline, `--delta-scope global`

  Their fixtures use keys differing from a reference key in exactly one field
  each. Both were verified to **fail** against a planted key that drops the
  direction bit, in all three implementations, then restored — a conformance
  case that cannot fail is worse than none. The oracles were also checked by
  hand against a computed table rather than merely accepted from
  `--update-expected`.

### Added

- **The C++ `dump` subcommand — Phase 2 is complete, and all three
  implementations now expose an identical CLI surface.** `dump` was the last
  unported command and the last reason the C++ build sat out the
  `cli-surface-parity` gate. That gate now compares **three** implementations
  and passes: 35 flags, identical across Rust, Python and C++.

  Both views are ported. The record-aware view annotates each record with its
  decoded Type Word, message format, timestamp and Command Word — plus, for an
  errored record, the Error Word and its DDC description, which is what makes
  the reason legible without a separate trip to the error catalogue. The raw
  view parses nothing at all, which is the point: it is what an operator reaches
  for once the decoder has already refused the file.

  The output was diffed against the Rust implementation across twelve fixtures
  and ten flag combinations — 120 invocations, byte-identical including exit
  codes. That comparison caught a real defect on the way: the report's format
  name must be the SCREAMING_SNAKE spelling (`RECEIVE`), while
  `models::message_format_name` returns the CamelCase spelling (`Receive`) used
  elsewhere. Two different strings for the same value, and reusing the wrong one
  compiled cleanly and broke the diff on every classified record.

  Bounds are clamped rather than validated. An offset past end of file, a zero
  length, or a length that would overflow each produce a well-formed empty or
  truncated range instead of an argument error — the operator is exploring, and
  the empty output already says what a rejection would.

- **The C++ implementation gained the multi-file merge, and now passes the
  entire shared conformance manifest.** Every case the Rust and Python
  implementations are held to — including all fifteen that need the k-way merge
  — now runs against the C++ binary and compares byte-for-byte against the same
  committed oracles. The conformance runner's C++ entry no longer declares any
  case unsupported.

  `decode` accepts several positional inputs, `--manifest` or `--glob`, and the
  three are mutually exclusive: each names the input set completely, so
  combining two would leave the ORDER of the result undefined, which is the one
  thing a time-sorted merge exists to establish. `--delta-scope`,
  `--collapse-duplicates` and `--collapse-window-us` land with it.

  The merge holds one record per open input in a `std::priority_queue`, so
  resident memory is O(number of files) rather than O(records) — the same
  constant-memory property the single-file path has. It is a `MessageSource`,
  which means the filter, canonical-order and writer stages downstream are the
  *same code* operating on the same contract; the only thing a merge changes is
  which source sits at the head of the pipeline.

  Two details worth naming because they are easy to get wrong and silent when
  wrong. The glob's `?` consumes one whole **UTF-8 character**, not one byte:
  Rust and Python match over Unicode scalar values, and a byte-wise `?` would
  have disagreed with them on any non-ASCII filename while passing every ASCII
  test. And the duplicate-collapsing window compares **absolute** time distance,
  because a lenient non-monotonic input can step backward and a one-sided
  subtraction would wrap to an enormous unsigned value that reads as "outside
  the window" only by luck.

  `dump` remains unported. It is now the only thing keeping the C++ flag surface
  a subset of the other two, and therefore the last reason its registry entry
  sits out the CLI-surface-parity gate.

- **Shared test fixtures for records on the wire (`cpp/tests/record_fixtures.hpp`).**
  The Type Word layout, the IRIG triple and the Command Word encoding had been
  copied into four test files. That is the drift `writer_fixtures.hpp` already
  exists to prevent: two suites encoding the format slightly differently
  disagree for reasons unrelated to the code under test, and the copy that is
  *wrong* is the one that keeps passing. `test_cli.cpp` and the new
  `test_merge.cpp` share one definition; `test_reader.cpp`, `test_sync.cpp` and
  `test_decode.cpp` still carry theirs and are noted for migration.

- **The C++ implementation reached Phase 1: `decode` and `count` work, and are
  gated by the same byte-exact CSV oracles as Rust and Python.** Every shared
  conformance case that does not need the unported multi-file merge now runs
  against the C++ binary, on Linux and on Windows, comparing against the very
  same `tests/conformance/expected/` files the other two are held to. Up to this
  point the C++ tree was a library with a `--version` flag; it is now a decoder
  whose output is proved identical rather than asserted to be.

  The argument parser handles `--flag value` and `--flag=value` through **one**
  cursor rather than spelling both out at each of the twenty-odd valued flags,
  which is twenty-odd chances for one spelling to drift from the other. Numeric
  flags reject values that are not wholly numeric — `strtoll` stops at the first
  non-digit and reports success, so `--detect-records 4x` would otherwise have
  silently become 4 — and bounded flags reject the same out-of-range values
  their config-file counterparts reject, so a number is refused identically
  whichever way it was supplied.

  Flags belonging to the unported merge (`--manifest`, `--glob`,
  `--delta-scope`, `--collapse-duplicates`, `--collapse-window-us`) and the
  `dump` subcommand are refused with a message naming **the missing module**
  rather than reported as unknown. An operator moving a working invocation
  across from Rust needs to tell "this build does not have that yet" from "you
  have mistyped this", because the remedies are nothing alike.

  Two failures found while building it, both of which would have reached an
  operator. A bad `--exclude-types` value threw the config layer's `ConfigError`
  through every handler in `run()` and **aborted the process** — a caller cannot
  distinguish an abort from a signal, and what should have been exit 4 was a
  crash. And the CLI's own reporting streams were the process globals, so
  nothing about `count`'s "the integer, and only the integer, on stdout"
  contract was testable in-process; the streams are now injected, which is the
  same seam `log::set_sink` and the abstract `CsvSink` already established.
  A catch-all backstop now guarantees every failure leaves as an exit code.

- **A third implementation: C++11 under `cpp/`, targeting SLES 12 SP5.** Neither
  existing implementation can be deployed there — SLES 12 has no Python ≥3.10
  package, and the Rust build needs an approved toolchain on the build host. A
  C++11 binary compiled against the platform's own GCC 4.8.5 removes the
  prerequisite entirely. The same source also builds with MSVC, so Windows is a
  shipping target rather than a development convenience.

  This is **Phase 0 of a phased delivery**: the platform layer, both build
  descriptions and the full gate suite. The CLI implements `--version` and
  `--help` only — `decode`, `count` and `dump` arrive with the decoder modules
  in later phases. Nothing about the Rust or Python implementations changes.
  See `cpp/README.md` for current status and `docs/adr/` for the three decisions
  that shape the tree.

  - `cpp/include/mie/platform.hpp` and its POSIX and Win32 backends. The decoder
    proper has no operating-system surface; exactly five concerns do — mapping
    the input read-only, the atomic temp-file-and-rename output, directory
    enumeration for `--glob`, byte-exact (binary-mode) output, and path identity
    plus encoding — and all five are confined here, so the rest of the tree
    needs no conditional compilation.
  - `cpp/Makefile` (authoritative on Linux, and the only build the GCC 4.8.5
    fidelity container can run — it ships CMake 2.8) and `cpp/CMakeLists.txt`
    (authoritative for MSVC; Visual Studio 2022 opens `cpp/` directly). Both
    read one `cpp/sources.txt`, so the file list cannot drift.
  - A Catch2 v2.13.10 suite over the platform layer, green on g++ 11,
    GCC 4.8.5, MSVC 19.44 at `/W4 /WX /permissive-`, AddressSanitizer +
    UndefinedBehaviorSanitizer + LeakSanitizer, and Valgrind memcheck with zero
    leaks. Catch2 is pinned to the v2 line because v3 requires C++14.
  - `.github/workflows/cpp-ci.yml` — nine jobs gating independently of `ci.yml`,
    so a Rust change never waits on a Valgrind run. The GCC 4.8.5 tier runs the
    **full suite**, not a compile check: it is the only tier that enforces the
    C++11 boundary, because MSVC has no `/std:c++11` and compiles this source as
    C++14.
  - Three invariant gates, each verified to fail on a planted violation as well
    as to pass on clean code: `scripts/assert-platform-confined.sh` (OS headers
    only in the platform backends), `scripts/assert-locale-free.sh` (no
    `setlocale`, no `<cctype>` classification — `DELTA` is formatted `%.6f`,
    whose decimal separator the locale chooses), and
    `scripts/assert-sources-agree.sh` (both builds resolve the same sources).
  - `docs/adr/` — the repository's first architecture decision records.
    ADR-0001 sets the C++11 / GCC 4.8.5 floor and records why the SLE 12
    Toolchain module was considered and not taken; ADR-0002 explains the
    two-build one-source-list arrangement; ADR-0003 makes Windows a shipping
    target and lists the five behaviours that commits to.

- **C++ decoding core: the model, error and pure-decode layers.** The second
  phase of the C++ port. Still no `decode` / `count` / `dump` subcommands — this
  is the computation underneath them, landed on its own so the wire-format work
  is reviewable before the reader and writer build on it.

  - `mie/models.hpp` — the wire-format types, with the same field names and enum
    discriminants as the Rust and Python implementations so the three stay
    readable against each other. `DataWords` is a fixed-capacity inline buffer,
    not a `std::vector`: the 32-word cap is a property of MIL-STD-1553B, and an
    inline buffer is what keeps the per-record path allocation-free.
  - `mie/error.hpp` — one error type with a kind discriminant, mirroring Rust's
    `MieError` rather than Python's class hierarchy. The classification contract
    (`is_file_error()` / `is_record_error()`) is pinned by an exhaustive table,
    with a companion test that fails if a kind is ever added without a row.
  - `mie/decode.hpp` — Type Word, IRIG and Standard timestamps, Command Word,
    message-format classification including the mode-code shapes, the structural
    invariants (L2-SYN-020 through L2-SYN-027), and the L2-DEC-015 timestamp
    auto-detection probe.
  - `mie/text.hpp` — locale-free formatting and ASCII classification, the single
    place L3-CPP-007 has to hold. `fixed6` normalises the decimal separator
    rather than assuming the C locale, because the decoder can be linked into a
    host that has already called `setlocale` — verified against a real `de_DE`
    locale, where raw `snprintf("%.6f")` does emit `1,234500`.
  - `mie/optional.hpp` — a minimal `std::optional` stand-in, since that type is
    C++17. Absent and zero are genuinely different in this format: a missing
    Status Word is not `0x0000`, and an uncomputable DELTA is not `0.0`.

- **C++ record alignment, validation and sync recovery (`mie/sync.hpp`).** The
  layer between the raw bytes and the reader: candidate validation with the
  N-record look-ahead (L2-SYN-005 / L2-SYN-026), IRIG field-range checks
  (L2-SYN-004 / L2-SYN-019), first-record detection, the homogeneous-payload
  defence (L2-SYN-018), the L2-RDR-004 truncated-first-record diagnostic, and
  forward sync recovery.

  Pure functions throughout — no logging and no I/O, the same rule the Rust and
  Python sync modules follow. `CLAUDE.md` records why: these helpers lack the
  caller's context, so they narrate outcomes wrongly. Finding no first record is
  the *expected* result for a valid empty recording, and saying "no valid record
  found" there contradicts the reader's own correct message.

  The tests target the behaviours hardest to reconstruct from the code, each
  with its reason stated: the end-of-records terminator is honoured on trusted
  boundaries and deliberately **not** during recovery (a mis-aligned candidate
  could otherwise validate off a stray zero data word); an undetermined
  timestamp format uses the permissive Standard floor rather than the IRIG one;
  freerun suppresses the day-range check and nothing else; and the look-ahead
  advances by each record's *declared* length rather than two bytes. The IRIG
  day, hour, minute and second ranges are swept over their full encoded width
  rather than sampled — day 0 and 367..511 are encodable and must be rejected.

- **The C++ filter and canonical-order stages, and the pipeline contract they
  share.** `mie/filter.hpp` and `mie/order.hpp`, plus `mie/source.hpp`.

  **`MessageSource` moved out of `writer.hpp`.** It lived there when the writer
  was its only consumer; it moved when the second one arrived, because `filter`
  depending on `writer` to borrow a type would have been a dependency edge that
  describes nothing real. Every stage between the reader and the writer now both
  consumes one and *is* one, so the pipeline is assembled by nesting rather than
  by a fixed sequence:

  ```
  RecordIter -> FilteredSource -> OrderedSource -> write_csv
  ```

  That is the same shape as the Rust iterator-adapter chain
  (`reader.iter().filter_messages(f).order_rows(n)`), expressed with the tool
  C++ has for it. The value is not the pattern name but the consequence: stages
  can be added, reordered or omitted without any of them knowing what the others
  are, and the writer needs no knowledge of whether filtering happened.

  **Filter: exclude and include are not opposites**, and the asymmetry is the
  semantics. An empty include set is *no constraint*, never "include nothing" —
  reading it the other way would make a config that sets only `exclude_rts` drop
  every record in the file. And a record with no Command Word cannot *satisfy*
  an RT or subaddress include filter, so an active one drops it, while the same
  record is untouched by an RT or subaddress *exclude* filter, which can only
  match a value it does not have. Those two rules pull in opposite directions on
  the same record, so each has its own test rather than being covered
  incidentally.

  The passed/excluded tally is emitted from the **destructor**, matching Rust's
  `Drop`: a consumer can stop early — a broken pipe, `| head` — and an
  end-of-stream hook would simply never run, losing the tally exactly when the
  operator most wants to know how much was dropped. The destructor swallows any
  exception from building that message, because a throwing destructor during
  unwinding is `std::terminate` and a courtesy line must never turn a decode
  failure into a crash.

  **Order: pinning is the subtle part.** A record with no Command Word has no
  sort key, and its position is defined *relative to its predecessor* — the card
  writes the leftover words of an errored transaction as a SPURIOUS record
  immediately after it, and `0x2000` means "continues the record before me". So
  a pin travels with the record it followed, by being sorted as part of that
  record's *chunk*. Holding a pin at a preserved index while sorting looks
  equivalent and is not: it leaves the pin trailing whichever record the sort
  moved into that slot, breaking exactly the adjacency the `0x2000` code
  depends on. `a pin travels with the record it followed` is the test that tells
  a correct implementation from an index-preserving one.

  Two more decisions carried across: the sort key is the **decoded fields**, not
  the rendered `MSG` string (`"11R"` sorts before `"2R"` lexicographically,
  which is not the required order), and timestamp grouping compares the
  **variant** as well as the value, so an IRIG microsecond count can never be
  mistaken for an equal Standard tick count.

  A throw from the inner source is **deferred until the buffered run has been
  emitted**. Under `--allow-partial` the rows the stage is holding must reach the
  committed `.partial`; letting the throw through first would drop a whole
  equal-timestamp group from the operator's only record of what decoded. C++
  does this with `std::exception_ptr` rather than by catching one error type, so
  the guarantee does not depend on which exception arrives.

- **The C++ configuration loader, split into a liftable TOML parser and an
  MIE-specific schema.** `mie/toml.hpp` parses; `mie/config.hpp` decides what
  the result means.

  The division is the point, and it is deliberate rather than incidental:

  ```
  toml.hpp    what the file SAYS   -- sections, keys, values, line numbers
  config.hpp  what it MEANS        -- which keys exist, ranges, enum spellings,
                                      defaults, precedence
  ```

  `toml.hpp` / `toml.cpp` contain **no MIE type, include no decoder header, and
  cannot fail in a way that mentions a recording**. The header states its lift
  set explicitly — those two files plus `text.hpp` / `text.cpp` plus
  `test_toml.cpp`, and a namespace rename. `text` is depended on rather than
  duplicated because it is itself domain-free, and because the parser must not
  use `<cctype>`: those functions read the locale table, and under `tr_TR` the
  case mapping of `I` changes.

  Consequences worth naming:

  - Parse failures are `bool` + an out-parameter, not exceptions, matching the
    platform layer. The component works in a codebase compiled without
    exceptions, and `config.cpp` translates `toml::ParseError` into
    `ConfigError` at exactly one line.
  - The typed accessors (`get_string`, `get_int`, …) live in `config.cpp`, not
    on `toml::Document`. "This key must be a string" is a schema statement, and
    the parser has no schema.
  - `ConfigError` is **not** a `MieError`, mirroring Rust: `MieErrorKind` is an
    exhaustive table pinned by test and mapped to exit codes, and a malformed
    config file is not a decode failure.

  **The subset rejects on purpose.** Where full TOML accepts a form the flat
  schema cannot model — a dotted key, an inline table, a nested table header,
  an array of tables — the parser refuses rather than storing it somewhere the
  schema will never look. A silently dropped `output.no_clobber = true` is a
  safety option that appears to be set and is not.

  **The number grammar is swept exhaustively**, because it is the most
  divergence-prone rule in the subset: every language's native number parser is
  more permissive than TOML. `strtod` takes `08`, `1.`, `0x10` and leading
  whitespace; `tomllib` takes none of them. The sweep covers all 2800 strings
  of length ≤ 4 over `{0,1,+,-,.,e,_}` and asserts a **property** — an accepted
  literal is one the C library also consumes completely — rather than a second
  copy of the productions, because two copies of one mistake agree with each
  other. It is paired with a corpus generated *from* the productions and a
  curated table of the forms a native parse accepts and TOML does not.

  **That rigour found a real divergence in this port.** The C++ identifier rule
  accepted `-`; Rust uses `is_ascii_alphanumeric() || '_'` and Python's
  `_IDENT_RE` is `^\w+$` under `re.ASCII`. `[with-dash]` would have been a
  section C++ accepted and the other two refused — a shared config file meaning
  different things per implementation. The 256-byte identifier sweep is what
  made it concrete.

  **The parity corpus runs now, at the layer that causes divergence.**
  `tests/conformance/config_parity.py` drives the CLI, which C++ does not have
  yet, so joining that harness waits for the CLI module. Its 41 snippets are
  ported into `test_config.cpp` and run directly against the loader in the
  meantime — building the loader without them would have meant building it
  blind to the exact forms the three implementations have had to be aligned on.

  Also landed: `mie/filter.hpp`, the `FilterConfig` data and its `is_active`
  predicate. The pipeline stage that applies it comes with the filter module;
  the data exists now because `config` populates it from a `[filter]` section,
  and a loader that invented its own container would be the second definition
  of what a filter is.

  `cpp/.clang-tidy` records `misc-no-recursion` as disabled, with the reasoning:
  `toml::Value` is a recursive type by definition, the check also fires inside
  libstdc++ where a `NOLINT` cannot reach, and the hand-written recursion it
  would legitimately catch was removed instead — `parse_value` and `parse_array`
  were mutually recursive and are now split so the array parser calls a
  scalar-only parser, making "no nested arrays" structural rather than a check
  that runs after the fact.

- **The C++ streaming CSV writer (`mie/writer.hpp`).** The stage that turns
  records into the vendor-compatible CSV: the 44-column vendor block with
  `ERROR` / `ERROR_CODE` appended, atomic temp-and-rename output, the
  input/output and no-clobber safety gates, `--allow-partial`, split-error
  output, and stdout. The `decode` / `count` / `dump` subcommands still need
  `config` and `cli`.

  Two properties define the module, and both have been broken in this project
  before:

  - **The column boundary.** Column *N* of a decoded CSV is column *N* of a
    vendor CSV for all 44, which is what makes a vendor diff a validation
    rather than a re-mapping exercise. Through v2.9.0 the two decoder-added
    columns sat between `DELTA` and `IM_GAP`, shifting the three gap columns
    off their vendor indices while every column *name* still matched. The
    boundary is therefore asserted **by index as well as by name**, plus a
    cell-count property that catches a stray comma anywhere in the row.
  - **Rows stream.** A row is formatted and handed to the sink as it arrives;
    nothing accumulates a container of rows or of messages. `AtomicFile` holds
    one fixed-capacity buffer that is flushed rather than grown, which is what
    makes the O(1)-in-record-count claim true (L3-CPP-011).

  Design notes worth having:

  - `CsvSink` is an interface rather than a template, which is where this
    departs from the Rust `CsvWriter<W: Write>`. Staying generic in C++ would
    put the whole writer in a header, and the dispatch cost is one indirect
    call per *write* rather than per byte.
  - `MessageSource` is the C++ stand-in for the Rust entry points'
    `IntoIterator` bound, so the merge can feed the writer when it lands and
    the tests can drive it without a file.
  - `format_row` is public because it is the unit worth testing: every column
    decision lives in it, and a test that asserts on a string needs no
    filesystem.
  - The errors file in split mode is opened **lazily**, on the first error row,
    so a clean recording leaves neither an empty `_errors.csv` nor a temp file.
  - Main is committed **before** errors (L2-WRT-019): if the errors commit then
    fails, what is on disk is the primary artifact rather than an orphan errors
    file beside no main output.

  `platform::capture_stream_error` is new, and it translates rather than
  passes through. `fwrite` reports failure via `errno` on every platform, but
  `OsError::code` means `GetLastError()` on Windows and that is the space the
  broken-pipe classification matches against — 32 is `EPIPE` in one space and
  `ERROR_SHARING_VIOLATION` in the other, so the collision is real. Getting
  this wrong is what once made `mie-decoder decode x.mie | head` exit 1 on
  Windows while exiting 0 on Linux (L2-WRT-018).

  `tests/temp_path.hpp` gained `also_remove`, because the split-output errors
  file is `<stem>_errors<ext>` — an **infix**, which `sibling()` cannot
  express. A test that computed the name and removed it by hand would
  reintroduce exactly the leak-on-throw that helper exists to prevent.

- **The C++ reader and logger (`mie/reader.hpp`, `mie/log.hpp`).** The stage that
  turns a file into records: memory-mapped input, header skip, one-time
  timestamp-format resolution, the decode loop, internal sync recovery, and
  DELTA tracking. Still no `decode` / `count` / `dump` subcommands — the writer
  and CLI follow.

  `sync` and `decode` are pure by rule, so the reader is the module that
  **talks**: every operator-facing WARN about a freerun timestamp, a backwards
  clock, a skipped record or a recovered sync loss is emitted here, because only
  here is the caller's context available. The same call cannot know whether
  "no first record" means a valid empty recording or the wrong file entirely;
  the reader decides, and says so.

  Behaviours worth naming, because each is a decision rather than an arithmetic
  result:

  - Mid-stream validation runs at look-ahead depth **1**, while first-record
    detection and recovery keep the configured depth (L2-SYN-026). Using the
    configured depth mid-stream discards the last good record before every
    corrupt region — N-1 records per corruption site at depth N — because a
    record was rejected on account of its *successor*.
  - The `0x2000` / `0x2001` spurious classification depends on whether the
    **previous** record errored, which is why it cannot live in `sync` or
    `decode`. The flag is consumed by the record that uses it, so a second
    consecutive SPURIOUS_DATA record continues nothing.
  - DELTA is **absent**, not zero, wherever no honest gap exists: a backwards
    clock (L2-RDR-017, warned once per RT/MSG key rather than once per record),
    an uncalibrated Standard counter (L2-RDR-019), and SPURIOUS_DATA, which has
    no key at all (L2-RDR-018).
  - Errors are thrown and are **terminal**. The walk latches closed before the
    throw, so a caller that catches and resumes gets a clean end of stream
    rather than a second attempt at the record that just failed — matching the
    Python generator, which dies when it raises.
  - A directory is reported as an I/O failure, never as an empty recording. It
    stats as zero bytes on Windows, and calling that an empty *capture* would
    send an operator looking for a recorder problem instead of a typo.

  The logger is ~120 lines with no dependency, mirroring `rust/src/log.rs`:
  one global level and four macros. They are macros so the message argument is
  not evaluated when the level filters it out — the per-record DEBUG lines in
  the reader would otherwise allocate on every record at the default level, and
  no assertion about visible output can detect that, so the suite asserts it
  directly with a side effect. Messages are assembled from `mie/text.hpp`'s
  formatters rather than an `ostringstream`, which formats through its imbued
  locale and would put L3-CPP-007 back in play.

  A `set_sink` hook exists for the tests. That is worth stating plainly: the
  reader's log output is part of its contract, and the alternative was to assert
  it by reading the process's own stderr.

- **Exhaustive cross-implementation verification of the C++ decoders.**
  `rust/examples/decode_digest.rs` sweeps every one of the 65 536 possible Type
  Words and Command Words, and every bit position of every IRIG and Standard
  timestamp field, through the **Rust** decoders and emits an FNV-1a digest.
  `cpp/tests/test_decode_exhaustive.cpp` recomputes those digests from the C++
  decoders, so the two are proved identical across the entire input space rather
  than across a sample. Each digest is paired with a test that feeds a
  deliberately-wrong decoding through the same hash and asserts it differs — a
  digest test that cannot fail is worse than no test.

  Alongside it, the formatters are swept against `snprintf` as an independent
  oracle (every microsecond value from 0 to 999 999, every encodable day, hour,
  minute and second), the classifiers are swept over all 256 byte values under
  `tr_TR` and `de_DE`, and the field decoders are checked for bit-partition
  completeness — reassembling the raw word from the decoded fields must
  reproduce it exactly, which catches an overlapping mask or a dropped bit that
  both implementations could in principle share.

  That last check found a real gap in the port's understanding of the format:
  bit 14 of the IRIG upper word belongs to no decoded field. It is documented as
  Reserved in `docs/MIE-FORMAT.md`, the decoder was already correct, and the
  test now carries it explicitly.
  - An `L3-CPP` requirement category in `docs/L3-REQ.md`, and Catch2 tag
    scanning in `scripts/build-trace-matrix.py`. Each implementation now uses
    whatever its own test framework can select on — pytest markers, Rust doc
    comments, Catch2 tags — so every artifact the trace matrix names is
    directly runnable.

### Fixed

- **The SonarCloud quality gate on `main` is green again.** It had failed since
  the v2.12.0 cut on `new_security_rating` (5 against a required 1), driven by
  two vulnerabilities on the input-file `open()` in `reader.py`:
  `pythonsecurity:S2083` (blocker) and `pythonsecurity:S8707` (major). Every
  other gate condition passed throughout.

  Both are now suppressed for `reader.py`, scoped to those rules in that one
  file. The reasoning is recorded next to the exclusion and in
  `docs/CONFIG-REFERENCE.md`'s "Trust boundary" section.

  **The recorded analysis was wrong about where the taint came from**, and
  correcting it changed the argument. `docs/ROADMAP.md` described the finding as
  the CLI input path being operator-supplied; the flow SonarCloud reports starts
  at the *contents of a `--manifest` file*, whose lines become input paths
  (`merge.py` → `cli.py` → `reader.py`). So the justification is not the
  `config.py` one ("the path is the interface") but a narrower claim: a
  manifest's contents are as trusted as the operator who chose that manifest,
  and anyone who can write it can already invoke the decoder with any argument.

  `S8707` on this same flow was already resolved Won't Fix at `merge.py:58`,
  `dump.py:70` and `dump.py:119` — but only in SonarCloud, where the source does
  not show it. These two are recorded in the repository instead. This is the
  first `S2083` suppression here, taken as its own change rather than as a step
  in a release, which is what the ROADMAP entry asked for.

- **A manual or scheduled SonarCloud run on a non-default branch no longer
  files itself as `main`.** The workflow never passed `sonar.branch.name`, so a
  `workflow_dispatch` on a feature branch was recorded under `branch=main` and
  the dashboard reported an unmerged commit as main until the next push
  re-analysed. Found by doing exactly that while verifying the suppression
  above. Non-default refs now label themselves; pull requests are untouched,
  since those use the `sonar.pullrequest.*` parameters instead.

- `scripts/repo-hygiene.sh`'s CI-job check read only `ci.yml`. Adding a second
  gating workflow left its jobs invisible to the check, which would have gone on
  reporting success while an entire implementation's CI ran undocumented. It now
  scans both workflows.

## [2.12.0] — 2026-08-09

### Notes

- **This release ships with the SonarCloud quality gate red, knowingly.**
  `new_security_rating` is 5 against a required 1; every other condition passes
  (reliability 1, maintainability 1, coverage 94.7%, duplication 0.0%, hotspots
  reviewed 100%) and all 51 non-Sonar CI checks pass. Two findings,
  `pythonsecurity:S2083` (blocker) and `pythonsecurity:S8707` (major), both on
  the `open(self._path, "rb")` in `python/src/mie_decoder/reader.py` added by the
  `MieFileIoError` conversion above.

  They were not excluded at the cut because the existing exclusion is scoped to
  one rule in one file *by design* — "this rule anywhere else, still fails the
  build" — and `S2083` has never been suppressed in this repository. Worth
  stating plainly: **v2.11.1 was cut specifically to stop releases merging
  against a failing gate**, and that fix is what surfaced this. Merging anyway
  restores the state v2.11.1 removed, deliberately and with the cost recorded.
  The analysis and the two ways out are in the "SonarCloud security findings on
  the input path" section of `docs/ROADMAP.md`.

- The `diagrams` CI gap is **still open** and is now fully diagnosed — see the
  entry under *Fixed* and the "Diagram rendering" section of `ROADMAP.md`.

### Added

- **A "Trust boundary" section in `docs/CONFIG-REFERENCE.md`** stating what
  `--config` does and does not promise: the file is read with the invoking user's
  permissions and never with elevated ones, must resolve to a regular file, is
  parsed as TOML data with no include directive / interpolation / code execution
  / network access, and may live at **any readable path** — site configs under
  `/etc`, a mounted share, or a relative path all remain valid.

  It also records *why* the location is unrestricted, and where the corresponding
  SonarCloud `pythonsecurity:S8707` exclusion lives, so a decision that was
  previously visible only in a CI workflow file is now discoverable by the people
  it affects. A closing note names the contexts the guarantee does **not** cover
  — a setuid wrapper, a shared service account, or a job runner accepting a
  config path from an untrusted submitter — where restricting the path is the
  caller's responsibility. `docs/CLI-REFERENCE.md` links to it from the
  `--config` row.

- **A fourteenth `scripts/repo-hygiene.sh` check: no `TRACE-MATRIX.md` row may
  claim Implemented with a `_(TBD)_` artifact.** The generator now makes that
  state unreachable; this fails on the rendered output, so reverting the rule
  in `build-trace-matrix.py` is caught mechanically rather than by a reader
  noticing the contradiction. Proven to fail on a planted row.

- **A thirteenth `scripts/repo-hygiene.sh` check: the Python exception
  hierarchy must match both of its drawings.** `exceptions.py` is the truth;
  `ERROR-CATALOG.md` §2 redraws it as an ASCII tree and
  `docs/diagrams/class.puml` redraws it as UML, and both drawings assert they
  are complete. Both had drifted (see *Fixed*). The check parses all three and
  compares each class's **parent**, not just the set of names, so a leaf
  reparented in code — the `MieNonMonotonicInputError` case — fails as loudly
  as one that is missing. Proven against three planted defects: a dropped tree
  entry, a dropped UML edge, and a misparented class.

- **A twelfth `scripts/repo-hygiene.sh` check: `docs/ROADMAP.md` may not
  restate a `docs/TRACE-MATRIX.md` status.** The roadmap is hand-written and the
  matrix is generated, so any status copied across is guaranteed to rot — and
  did (see the `L2-DEC-012` entry under *Fixed*). The check rejects the
  generator's own status tokens (`Draft`, `Implemented`, `Verified`) appearing
  in the roadmap, case-sensitively, so ordinary prose like "not yet
  implemented" still reads naturally. Link to the matrix; don't copy it.

- **An eleventh `scripts/repo-hygiene.sh` check: the declared Rust MSRV must
  agree everywhere it is written down.** `rust-version` in `rust/Cargo.toml` is
  the only copy a toolchain enforces, but the number is also spelled out in
  `CLAUDE.md`, `CONTRIBUTING.md`, `rust/README.md`, `L3-RS-001`,
  `MAINTAINER-GUIDE.md` §9, `ci.yml` (four times) and `sonarcloud.yml`. The
  check extracts the declared version and requires every *floor-declaring*
  statement across the tracked tree to match it, and requires the seven files
  that are supposed to state it to still do so — so a bump can neither land in
  six places and miss the seventh, nor quietly vanish.

  The patterns are deliberately narrow (`MSRV <v>`, `toolchain ≥ <v>`,
  `cargo +<v>`, `rustup default <v>`, `pinned **<v>** toolchain`), so contrast
  statements — "edition 2024 requires only 1.85", "`memmap2` declares
  `rust-version = "1.65"`" — are facts about other things and don't trip it.
  `CHANGELOG.md` is exempt as a historical record. Proven to fail on both a
  planted mismatch and a planted omission before being committed.

- **`tests/conformance/config_path_parity.py`** — a third cross-implementation
  config guard, covering the `--config` **path** rather than its contents.
  `config_parity.py` and `config_fuzz.py` both always hand the CLIs a perfectly
  ordinary file, so the path's own behavior had no cross-implementation check:
  the regular-file rule, its exit code and its message were pinned only by
  per-implementation unit tests that could drift apart silently. (The v2.11.0
  claim that both loaders reject a non-regular file "with identical message
  text" was, until now, never actually compared across the two.)

  It runs inside `run.py` alongside the other two and is stricter than the
  content corpus: it compares the **exact exit code** rather than an
  accept/reject class, and requires the promised message substring from both
  CLIs. Cases: a regular file, a missing path, a directory, names with spaces,
  non-ASCII names, `..` traversal segments, a character device, and a symlink to
  a regular file. The last two skip themselves where the platform lacks support
  and the skip is printed, so a corpus that quietly shrinks on one OS is visible.

- **Unit tests in both implementations pinning the "TOML data, never
  interpolated" promise** — a value of `$(whoami)${HOME}`` `id` ``%PATH%` is
  stored verbatim rather than expanded, executed or resolved — and pinning that a
  `..` path loads normally, so reversing the unrestricted-location decision fails
  a test rather than silently contradicting the documentation.

- **Expansion and traversal syntax in the config fuzzer's value palette**
  (`$(…)`, `${…}`, backticks, `%VAR%`, `../../../etc/passwd`, an extended-length
  Windows path). Wherever the generator lands one of these, both parsers must
  reach the same accept/reject class — holding two parsers that share no code to
  treating the forms as inert data.

- **`MieFileIoError`, closing the last Rust/Python error-mapping gap.** Opening,
  stat-ing, reading or memory-mapping the input now converts to a decoder
  exception instead of escaping as a bare `OSError`. The originating error is
  preserved as `.source` and chained with `raise ... from`, so nothing is lost.

  Every `MieError` variant now has exactly one Python class and vice versa;
  `FileIo` was the sole unmatched one. `docs/diagrams/class.puml` had **already
  declared `MieFileIoError`** — the diagram described the intended design and
  the implementation simply never had the class, which is also why
  `MieFileError`'s docstring claimed it covered failures to open.

  **Ordering is now mirrored too.** Both implementations attempt the open
  *before* checking the size, matching `MieFileReader::new` in Rust. Python
  previously stat-ed first, so an unopenable path was mis-reported as empty — a
  directory stats as zero bytes on Windows, and `decode <dir>` said "MIE file is
  empty" where Rust said "I/O error". Both now report the same class with the
  same `I/O error on <path>: ` prefix and the same exit code (`1` for `decode`,
  `4` for `dump`); only the OS-supplied detail differs, which no amount of
  alignment can fix across two runtimes.

  Taking the size from the open handle also closes the stat/open race.

- **`scripts/repo-hygiene.sh` and the `repo-hygiene` CI job**, the backstop the
  bypass advice always assumed existed. It re-runs the hook's file-level checks
  over the whole *tracked tree* rather than a staged diff, so it catches
  anything already committed however it got there.

  Two details worth recording, because both were bugs in the first draft. The
  CRLF check reads the **index** (`git ls-files --eol`), not the worktree: a
  Windows checkout legitimately holds CRLF while the committed blob is LF, so a
  worktree-based check fails locally and passes on the Linux runner — worse than
  no check. And the `unsafe`/`SAFETY:` detection mirrors the hook's regex
  exactly rather than being stricter; a backstop that rejects what the hook
  accepts just moves the surprise from commit time to merge time.

  Each of the eight checks was verified to **fail** on a planted violation, not
  merely to pass on the clean tree — the `diagrams` job in this release is a
  standing reminder of what an unverified gate is worth.

### Changed — behavior

- **Python callers catching `OSError` around the reader must widen to
  `MieFileIoError`.** It extends `MieFileError` → `MieDecoderError`, so anything
  already catching a decoder exception now covers this case (that is the point).
  Code catching *only* `OSError` around `MieFileReader` construction or
  iteration will no longer match. The CLI is unaffected — it catches
  `(MieDecoderError, OSError)` — and no exit code, log line or CSV output
  changes.

- **`MieError::is_record_error()` now returns `true` for `UnrecoverableSyncLoss`.**
  It was the one variant misfiled against the repo's own definitions: Python's
  `MieUnrecoverableSyncLossError` extends `MieRecordError`, the variant carries
  the `offset` field the predicate's doc comment describes, `ERROR-CATALOG.md`
  lists it under "Record-level errors" as catchable via that very predicate, and
  `MAINTAINER-GUIDE.md` requires every variant to be classified. The predicate
  disagreed with all four.

  **Scope of the change:** nothing in either implementation calls these
  predicates — exit-code classification matches on `kind()` — so no CSV output,
  log line or exit code changes. The effect is confined to downstream Rust
  callers branching on `is_record_error()`, which now see `true` where they saw
  `false`. `cargo-semver-checks` cannot detect a behavioral change of this kind,
  so it is called out here rather than caught by a gate.

  `is_file_error()` is **unchanged** and stays deliberately narrower than
  Python's `MieFileError`: it answers "did input I/O fail" (`FileNotFound`,
  `FileEmpty`, `FileIo`). The whole-file rejections (`NoValidRecords`,
  `HomogeneousPayload`, `TimestampFormatMismatch`, `IncompatibleMergeInputs`)
  and the destination guards (`InputOutputCollision`, `ClobberRefused`) extend
  `MieFileError` in Python but answer `false` to both Rust predicates; folding
  them in would make the predicate mean less, since none of them is an I/O
  failure on the input.

### Fixed

- **`L2-CLI-013`'s offset-overflow diagnostic was credited to tests that cannot
  reach it.** The requirement mandates a WARN plus an inline `!! ...` note for
  three dump scan-stop anomalies, and `TRACE-MATRIX.md` showed it Implemented
  against two truncated-record tests. The overflow branch in `dump.rs` is
  unreachable through a real scan: the loop advances only while
  `offset + MIN_RECORD_BYTES <= file_len`, and `file_len` is a mapped file
  length, so `offset` stays far below `usize::MAX` while a record's declared
  extent is capped at 126 bytes (`word_count` is the Type Word's 6-bit field).
  The sum cannot wrap.

  Rather than delete a harmless guard or leave the claim unbacked, the branch is
  now verified where it *is* reachable — `dump_record_extent` accepts an
  arbitrary `offset`, so `dump_record_extent_notes_offset_overflow` calls it
  directly with `usize::MAX - 4` and asserts both the returned stop and the
  inline note. `L2-CLI-013` gains a note stating the branch is defense in depth
  for that helper's contract, unreachable via the scan in both implementations
  (Python integers do not overflow at all), and that it must not be credited to
  the truncated-record tests.

- **The trace matrix reported `Implemented (I)` from a declared verification
  method alone.** `build-trace-matrix.py` credited any leaf whose spec said
  Inspection / Analysis / Demonstration, with no artifact required, so
  `L2-SYN-014`, `L2-CONF-001` and `L2-CONF-004` each read `Implemented (I)`
  beside a literal `_(TBD)_` in their own artifact column. A declared method
  describes how a requirement *would* be checked; on its own it is a plan, not
  a result.

  Requirements may now carry an `**Evidence**` line naming what actually
  carries the check, and an I/A/D requirement without one is **Draft** —
  exactly as a `Test (T)` requirement with no marker already was. The backticked
  names fill the artifact column, so the row shows its own justification. The
  coverage summary's "Verified" count applies the same rule, having previously
  counted the same unbacked requirements. L3 statements are one-liners, so
  theirs rides on the line as a trailing `· Evidence: ...`.

  The three requirements were then resolved on their merits rather than by
  annotation: **`L2-SYN-014`** (the boolean and detailed validators are one
  rule set and cannot disagree) is now genuinely Test-verified, by a new
  agreement test in each implementation running a valid record and every
  distinct rejection reason through both forms at three look-ahead depths — the
  property holds today only because one delegates to the other, which a
  refactor could quietly undo. **`L2-CONF-001`** (hex fixtures, never committed
  `.mie` binaries) cites the `repo-hygiene` check that already enforces it.
  **`L2-CONF-004`** (oracles move only when both implementations agree) cites
  `tests/conformance/run.py` and its CI job: the process rule cannot be
  violated silently because no single implementation can ratify an oracle
  change alone.

- **All three `docs/diagrams/*.svg` regenerated**, so `class.svg` reflects the
  three exception classes added to `class.puml` above. Rendered with PlantUML
  **1.2026.7beta11**, the same build that produced the previous set, and
  verified whole rather than merely changed: no exception in the render output,
  every one of the 44 types declared in `class.puml` present in the SVG, and
  each file terminated. `component.svg` and `dataflow.svg` shift their canvas
  slightly despite unchanged sources — PlantUML lays out using the JVM's font
  metrics, so the same jar on a different host reflows. That is a rendering
  artifact, not a content change.

- **The `diagrams` CI job has never verified an SVG, and now says so.** Three
  independent defects, none of which could be seen from the job's green tick:
  PlantUML names its output after `@startuml <name>`, so rendering into
  `docs/diagrams/` writes untracked `MIE-Decoder Class Diagram.svg` and leaves
  the tracked `class.svg` alone — `git diff --exit-code` inspects tracked files
  only, so it was trivially clean; PlantUML exits **0** when a diagram crashes
  mid-layout, so even correct filenames would have passed (`component.puml`
  dies in smetana's `qsort` on stable 1.2026.5 and 1.2026.6, emitting ~14 KB
  where a whole file is ~60 KB); and the pinned `1.2026.5` never matched the
  committed SVGs' `1.2026.7beta11`, which exists only in PlantUML's rolling
  `snapshot` pre-release and cannot be pinned by URL.

  The job is left running and is now labelled a known no-op in `ci.yml`, in
  `MAINTAINER-GUIDE.md` §9 (which had claimed it "fails if a `.puml` source was
  changed without regenerating the matching `.svg`") and in §3, which now
  documents both traps and the render-verification steps for doing it by hand.
  The full write-up — including a fourth constraint, that font-metric-dependent
  layout makes a byte-diff guard unsound unless one fixed environment renders
  everything — is a new "Diagram rendering" section in `docs/ROADMAP.md`.

- **`docs/ERROR-CATALOG.md` contradicted itself about the error predicates, and
  both drawings of the exception tree were incomplete.** §2 introduced
  `is_file_error()` / `is_record_error()` as mirroring "the Python class split"
  while the paragraph below it correctly explained that `is_file_error()` is
  **narrower** than `MieFileError`; only `is_record_error()` mirrors anything
  exactly. The §2 Python tree had also never gained `MieFileIoError` (added in
  v2.12.0 — the Rust listing and the §3 table were updated, the tree was
  missed), and §3 said its rows are "catchable in Python as `MieFileError`"
  while listing `MieNonMonotonicInputError`, which extends `MieDecoderError`
  directly and would escape such a handler. `docs/diagrams/class.puml` claimed
  its leaf classes "correspond one to one, in both directions" with the Rust
  variants while omitting `MieTimestampFormatMismatchError`,
  `MieIncompatibleMergeInputsError` and `MieNonMonotonicInputError` entirely.
  All corrected, and the diagram now shows `MieNonMonotonicInputError` hanging
  off `MieDecoderError` where it belongs.

- **`docs/ROADMAP.md` deferred work that had already shipped, on a status that
  was already wrong.** Its `L2-DEC-012` entry deferred the IRIG-wins-on-tie
  conformance test, said the requirement was "still listed as Draft in
  `docs/TRACE-MATRIX.md`", and gave the blocker as needing to reverse-engineer
  the detection heuristic to force an equal-score tie. The matrix lists it
  **Implemented**, with `test_zero_score_ties_to_irig` and
  `probe_zero_score_ties_to_irig` on the two sides, and the blocker never
  existed: the tie-break is the single comparison `irig_score >= std_score`,
  which an all-zero buffer exercises directly. Wrong three ways over, in a file
  whose own header says completed work is not tracked there. Entry removed.

- **The Python package docstring described a format the decoder doesn't
  read.** `mie_decoder/__init__.py` called the records "fixed-length" (in the
  same sentence that says a Type Word determines their size) and said the files
  carry "IRIG-format time tags", when Standard timestamps have been supported
  and auto-detected throughout. It now states the real shape — no file header,
  variable-length records, null Type Word as terminator, up to 32 data words,
  and both timestamp formats with per-file auto-detection. Its `Version
  history` block, frozen at `1.0.0` since the joint cut, is replaced by a
  pointer to `CHANGELOG.md` and `__version__`.

- **`.githooks/pre-commit` and `scripts/repo-hygiene.sh` assumed a bare
  `python` on PATH.** Both invoked `python` directly — the hook for the
  trace-matrix check, the hygiene script for the config-key check. On Debian,
  most WSL images, and anywhere else that ships only `python3`, the hook failed
  on every commit touching a spec doc and the hygiene check failed spuriously,
  even though `CONTRIBUTING.md` asks for Python 3 and never for a particular
  alias. Both now probe `python3`, `python`, then `py` and use the first that
  actually reports `sys.version_info[0] == 3` — which also rejects Windows' App
  Execution Alias stub, a `python` that exists on PATH and runs nothing. When
  none is found, the hook fails with a message naming what it tried instead of
  a bare `command not found`, and the hygiene script reports the affected
  checks as skipped rather than passing them silently.

- **The stated reason for the Rust MSRV was wrong in four places.** `CLAUDE.md`,
  `CONTRIBUTING.md`, `rust/README.md`, and `L3-RS-001` all credited the 1.88
  floor to `memmap2`. The locked `memmap2` 0.9.11 declares `rust-version =
  "1.65"` and edition 2021, so it constrains nothing here. The floor comes from
  the decoder's own source: **let-chains** (`if let` / `while let` joined by
  `&&`), stabilized in 1.88 and used at seven sites — `cli.rs`, `dump.rs`,
  `filter.rs` ×2, `merge.rs` ×2, `writer.rs` — plus `u64::is_multiple_of` in
  `reader.rs`, which independently needs 1.87. Proven by
  `cargo +1.85.1 check --all-targets --ignore-rust-version`, which reports
  exactly those eight sites and nothing from the dependency.

  **1.88 remains correct**; only the rationale was false, which is the dangerous
  kind of stale doc — it would have justified *lowering* the floor once someone
  checked `memmap2`'s manifest and found 1.65. `scripts/repo-hygiene.sh` now
  cross-checks the declared `rust-version` against every place the number is
  written down, so the *value* cannot drift even where prose explains it
  differently. The v2.4.0 entry below preserves the original claim as it was
  published; this entry supersedes it. (That entry also asserted `memmap2`
  itself used let-chains — it could not have: it is an edition-2021 crate, and
  let-chains are edition-2024-only. On 1.85 the dependency compiles cleanly and
  only our own eight sites fail.)

- **The diagrams named the `--allow-partial` output file backwards.** All three
  showed `<stem>.partial.csv`, implying `out.partial.csv`. Both implementations
  append the suffix to the whole name — `writer.rs` does `name.push(".partial")`
  — so the real files are **`out.csv.partial`** and, in separate-error mode,
  **`out_errors.csv.partial`**. Verified by running both CLIs against a fixture
  built to produce error rows *and* an unrecoverable sync loss, rather than
  inferred from the code.

  Every other reference was already correct — fifteen of them across the
  writers, CLIs, `run.py`, the tests and `USER-GUIDE.md`, and `L3-REQ.md` even
  cites `out.csv.partial` explicitly. Only the generated artifacts said
  otherwise, which is the failure mode this release keeps meeting: the copy
  nobody executes is the copy that rots.

- **`L2-MRG-004` mis-stated the separate-mode partial name** as
  `<stem>_errors.partial`, dropping the `.csv`. It now gives both concrete
  names, and says explicitly that `.partial` is appended to the whole file name.

- **The diagrams called a method that does not exist.** `class.puml` and
  `dataflow.puml` referred to `commit_as_partial()`; both implementations define
  **`commit_partial`**. `class.puml` also gave `commit()` a `PathBuf` return —
  it returns nothing in Python and `MieResult<()>` in Rust; only
  `commit_partial` returns a path.

- **`CONFIG-REFERENCE.md` omitted a key from its own copyable block, and dated a
  change relatively.** The document presents itself as the reference for *every*
  TOML key and states each one twice — a copyable quick-reference block and a
  normative table. `merge.delta_scope` shipped in v2.11.0 into the table and
  `config/default.toml` but not the block, so anyone copying the block as a
  starting config silently lost the key. It is the only key that had drifted.

  Separately, the `error_mode` note read "**Changed in this release**" for a flip
  that happened in **v2.8.0** — the only relative-version phrase in the docs;
  every other such note names an absolute version. Relative wording ages badly
  in a file nobody re-reads at release time, so it now says v2.8.0.

  `repo-hygiene` gained a check that the three text sources — block, table and
  `config/default.toml` — list the same key set, verified by re-planting the
  exact `delta_scope` omission and confirming it fails. (The two loaders are the
  fourth and fifth copies of that list; their agreement is already covered by
  `tests/conformance/config_parity.py`.)

- **Coverage and CI numbers had drifted out of the docs.** The Rust gate
  enforces `--fail-under-lines 87 --fail-under-regions 86`
  (`rust/.cargo/config.toml`), but `rust/README.md` and three separate places in
  `CONTRIBUTING.md` still advertised the pre-ratchet **84% / 83%**. The Python
  coverage floor is `fail_under = 92`, while the comment on the CI job that
  enforces it said **88%**. And `MAINTAINER-GUIDE.md` §9 opened with
  "`ci.yml` has seven jobs" when it defines **sixteen**.

  Notably the guide's *table* was accurate throughout — every job present, and
  its `rust` row already quoted 87/86. Only the prose above it had rotted, which
  is the usual pattern: the structure people edit stays right, the sentence
  nobody re-reads does not.

  So the count is now gone rather than corrected — it would only drift again
  (this release adds a job, taking it from fifteen to sixteen). The table is the
  list, and `repo-hygiene` now **enforces** that: every job in `ci.yml` must
  have a row in §9, or the build fails. Verified against a planted
  undocumented job.

- **The documented conformance command used the wrong Python.**
  `CONTRIBUTING.md`, `tests/conformance/README.md` and `MAINTAINER-GUIDE.md`'s
  test-suite table all said `python tests/conformance/run.py`. The runner drives
  the Python CLI with `sys.executable` — the interpreter it is itself running
  under — so after the `poetry -C python sync` those same docs prescribe, that
  command uses the system Python and dies with `ModuleNotFoundError: No module
  named 'mie_decoder'`. Verified by running it. CI is unaffected: it does
  `pip install -e ./python` into the runner's own interpreter first, which is
  why the same string works there and nowhere else.

  All contributor-facing invocations now use
  `poetry -C python run python ../tests/conformance/run.py`, with the reason
  stated so the next person does not "simplify" it back. `--rust-only` is left
  bare on purpose — it never touches the Python side, so it genuinely needs no
  package, and that is now called out rather than left to be inferred.

  Two path traps are documented alongside, both hit while verifying the fix:
  `poetry -C python run` executes with the working directory set to `python/`,
  so every relative path needs `../`; and `--rust-bin` is used exactly as given,
  so a Windows path without `.exe` is reported as `failed to build the Rust CLI`
  — the symptom rather than the cause. The `--python-bin` help text, which
  claimed the default was "Poetry's environment", now states the real default.

- **Contributor docs promised guarantees neither the hook nor CI provided.**
  Three separate overstatements in `CONTRIBUTING.md`:

  *The whitespace check never did what it said.* Check 1 was labelled
  "Whitespace + missing-final-newline", but `git diff --cached --check`
  implements `core.whitespace`, whose `blank-at-eof` means a **new blank line
  at** EOF — the opposite problem. Staging a file with no trailing newline
  exits `0` with no output. Rather than just correcting the wording, the hook
  now has a real final-newline check and the mislabelled step in
  `.githooks/pre-commit` is renamed. `*.svg` is exempt from that check: the
  committed diagrams are PlantUML output, which ends at `>` with no trailing
  newline — the three of them are the *only* tracked text files without one, and
  hand-appending it would be undone by the next render. The exemption matches
  `scripts/repo-hygiene.sh`; a hook stricter than its own backstop blocks
  legitimate commits, which is exactly how this surfaced (the check rejected the
  very SVG re-render that follows it in this branch). The list is also renumbered — it had grown a `1b.`, which is not
  valid Markdown list syntax.

  *"CI runs the same checks and will fail the merge anyway" was untrue for most
  of the list.* Nine of the hook's fourteen checks had no CI equivalent —
  verified by grepping each one, and by confirming that the apparent `CRLF` and
  `Cargo.lock` matches in the workflows are `core.autocrlf` configuration and
  cache keys, not checks. A `--no-verify` commit could land a CRLF blob, a stray
  `dbg!()`, a merge marker, an oversized file or a committed `*.mie` with
  nothing downstream to catch it. That advice is now a table of what backs up
  what, and the gap is closed by the new job below.

  *Two hook steps were undocumented* — the trace-matrix check, and the
  `cargo test --doc` half of the test step (`--all-targets` excludes doctests).

- **Nothing pinned the error-classification boundary, which is why it drifted.**
  Coverage was two spot-check assertions in Rust and six in Python; adding a
  variant to neither predicate failed no test. Both implementations now assert
  the *whole* boundary — `every_error_kind_is_deliberately_classified`
  (`rust/src/error.rs`) walks every `MieErrorKind` and requires it to be
  record-class, file-class, or on an explicit "neither" list, with a
  wildcard-free match so a new variant cannot compile without a decision;
  `test_every_exception_class_is_classified` (`python/tests/test_exceptions.py`)
  does the same over the exception classes and asserts the record-class set
  matches Rust's name for name. This makes the MAINTAINER-GUIDE "classify the
  new variant" step mechanical rather than a matter of reviewer memory.

- **Four documents disagreed about what the predicates cover.**
  `ARCHITECTURE.md` claimed both predicates "mirror" the Python intermediate
  classes (true for records only after the fix above, never true for files) and
  its reconciliation note omitted `TimestampFormatMismatch` and
  `IncompatibleMergeInputs` from the file-side extras. `ERROR-CATALOG.md`
  contradicted itself — §2 called `UnrecoverableSyncLoss` "non-classified" while
  §4 promised it was catchable via `is_record_error()` — and its §3 preamble
  implied `is_file_error()` covers the whole file-level section. `CLAUDE.md` said
  the predicates "approximate" the Python classes without saying how they differ.
  All four now state the same rule, and the tree annotation in `ERROR-CATALOG.md`
  §2 marks `UnrecoverableSyncLoss` and `WriterError` explicitly.

- **The README's error-mode example did the opposite of what it said.** It was
  captioned "Errors inline with normal messages" but passed `--separate-errors`,
  which routes errored and spurious rows *out* of the main CSV — and the output
  name `clean-plus-errors.csv` reinforced the wrong reading. An operator
  following it would inspect only the main file and reasonably conclude records
  had been dropped, when they were in the sibling `_errors.csv` all along. The
  README contradicted itself: its "Error output modes" section thirty lines later
  describes the flag correctly. Same v2.8.0 polarity-reversal residue as the
  `CLAUDE.md` entry below — the example predates the reversal, when the flag was
  `--inline-errors`, and only the flag name was updated. This was the last
  surviving instance; `EXAMPLES.md`, `USER-GUIDE.md`, `DATA-SCENARIOS.md` and
  `VENDOR-CSV-DIFFS.md` all describe it correctly.

- **`CLAUDE.md` stated the wrong default error mode.** Its "Output modes" section
  opened with ``Default (`error_mode = separate`)``, contradicting the code
  (`ErrorMode::Inline` / `ErrorMode.INLINE`), `config/default.toml`,
  `CONFIG-REFERENCE.md`, and `CLAUDE.md`'s own module summary a few lines above.
  The two bullets were also near-duplicates, both describing the split-file
  behavior, so the section never actually documented what `inline` does — the
  v2.8.0 polarity reversal appears to have rewritten one bullet and left the
  superseded one in place. Rewritten to describe both modes, with the v2.8.0
  reversal noted.

- **`docs/CLI-REFERENCE.md` described `--help` as printing "help for the program
  or the given subcommand"**, which only Python does; the Rust build prints one
  combined screen covering every subcommand regardless of where `--help` appears.
  The entry now states the difference and points at the `cli-surface-parity`
  check that keeps the flag *surface* identical even though the help *shape*
  differs.

- **`docs/diagrams/class.puml` claimed `MieError` has 15 variants** — it has 18.
  The count is now dropped rather than corrected: it is re-broken by the next
  error variant, and the useful part of the note is the mapping, not the number.
  That mapping is also stated correctly: every Python leaf class has exactly one
  matching variant, and the reverse does not hold — `FileIo` has no Python
  counterpart, because Python lets an unwrapped `OSError` propagate from
  open/mmap rather than converting it.

- **The exception base classes documented a guarantee the library does not make.**
  `MieDecoderError` claimed "all exceptions raised by the MIE-Decoder library
  inherit from this class" and showed a bare `except MieDecoderError` example —
  but opening and reading the input is left to the standard library, so a
  permission error, a non-regular path, or a device I/O error propagates as a
  plain `OSError` straight past that handler. A library consumer following the
  docstring would have an unhandled failure mode. `MieFileError` compounded it by
  claiming it is "raised when the input file cannot be opened", which is both the
  one case it does *not* cover and something it never does at all — it is a base
  class, never raised directly. Both docstrings now state what is and is not
  converted, and point at `(MieDecoderError, OSError)` as the catch-both form the
  CLI itself uses.

- **`docs/diagrams/component.puml` double-counted a sync check.** It called
  `validate_record` a "six-check shape probe" while listing the look-ahead
  separately as phase 3 of the same note — but the look-ahead *is* Check 6 inside
  `validate_record` (`rust/src/sync.rs`), so it was counted twice.
  `docs/ARCHITECTURE.md` already had it right ("5 checks + look-ahead"); the
  diagram now agrees and says where Check 6 lives.

## [2.11.1] — 2026-08-08

### Fixed

- **The SonarCloud quality gate passes on `main` again.** It had been failing on
  every merge since v2.8.0 — `#70`, `#71`, `#72` and `#73` each turned `main` red
  after merging — on `new_security_rating = 3`, from a single unresolved
  `pythonsecurity:S8707` finding at `config.py:740`.
- **The gate is now enforced on pull requests, which is why this went unnoticed.**
  `sonar.qualitygate.wait` was set only for pushes to the default branch, so a
  PR's "SonarCloud Code Analysis" check reported success for merely *uploading*
  the scan — it never evaluated the gate. Four releases merged green against a
  gate that was already failing. The wait now covers `pull_request` events too,
  so a failing gate blocks the PR while it can still be acted on.
- **`python:S3776`, the last open code smell** — cognitive complexity in
  `merge.py`'s `_merge_drain`. Split into two helpers with no behavior change:
  `_resolve_emission` (de-duplicate, then apply the DELTA scope) and `_pull_next`
  (advance one input, check monotonicity, surface a deferred `--allow-partial`
  terminal). Verified behavior-preserving by the full Python suite and all 79
  byte-exact conformance oracles.

### Changed

- **`pythonsecurity:S8707` is now ignored for `python/src/mie_decoder/config.py`**,
  scoped to that one rule in that one file, with the rationale recorded in
  `.github/workflows/sonarcloud.yml`. The rule ("Agentic workflows should not be
  vulnerable to path injection") fires on `--config <path>` reaching
  `Path.read_text()`. It is a false positive for this program shape: MIE-Decoder
  is an operator-run CLI, the config path **is** the interface, and the process
  holds exactly the permissions of the user who typed the command — there is no
  sandbox to escape and no privilege boundary to cross. The rule presumes a
  confinement this tool does not have and does not claim.

  The read-safety hardening that *was* warranted shipped in v2.11.0 and is kept:
  `load_config` requires a **regular file** before reading, so `--config <dir>`
  and `--config /dev/zero` are rejected rather than raising `IsADirectoryError`
  or hanging. Because the exclusion is scoped, any other finding in that file —
  and this rule anywhere else — still fails the build.

### Notes

- No decoder behavior changes in this release: no CSV output, CLI surface,
  configuration key, exit code or public API differs from v2.11.0. The Rust crate
  is unchanged apart from its version, and ships at 2.11.1 to keep the joint cut.
- The `diagrams` CI gap recorded in v2.11.0 is **still open** — the pinned
  PlantUML 1.2026.5 crashes on a smetana-layout diagram, exits `0`, and the drift
  check compares a file the render never touched. Unrelated to the SonarCloud
  work and still deferred.

## [2.11.0] — 2026-08-08

### Changed — BREAKING

- **Merged `DELTA` is now measured per input file by default.** In a multi-file
  decode, a record's `DELTA` is the gap to the previous same-RT/MSG record **from
  its own file** — the value that record gets when its file is decoded alone.
  Through v2.10.0 it was always measured across the merged timeline. Selectable
  via the new **`--delta-scope per-file|global`** flag and `[merge] delta_scope`
  config key (`L2-MRG-005`, `L3-WRT-004`); `--delta-scope global` restores the
  previous behavior exactly.

  **What actually changes.** Only RT/MSG keys that appear in **more than one
  input file** are affected. A key found in just one file gets the same value
  under either scope, because only that file's records can advance its tracker —
  so a merge of recordings covering disjoint equipment produces byte-identical
  output to v2.10.0. Single-file decodes are entirely unaffected: with one file
  the two scopes are the same computation, and the flag is accepted as a no-op.

  **Rationale.** The two scopes answer different questions — "how long since
  *this recorder* last saw this key" versus "how long since *any* recorder last
  saw it" — and which is wanted depends on what the inputs are, which the decoder
  cannot infer. `global` as a default had two problems. It made merged `DELTA`
  depend on the operator's choice of input set rather than on the bus traffic:
  two recorders each observing a 0.2 s cadence reported an apparent 0.1 s, and
  fully overlapping recorders degenerated to alternating `0.000000` values, one
  per duplicate pair. And it diverged from the DDC vendor tool for every shared
  key, since the vendor has no merge feature and always reports per-file — which
  is what surfaced this: a user comparing merged output against vendor CSV.
  `per-file` is the reading that composes: it matches a single-file decode, it
  matches vendor output, and it stays meaningful however the input set is
  assembled.

  **Migration.** Add `--delta-scope global` (or `[merge] delta_scope = "global"`)
  to any pipeline that depends on the previous values.

  Implementation note: `per-file` is realised by **not** recomputing DELTA during
  the merge — each reader already computed it for its own file — so the guarantee
  that a merged record's value equals its single-file value holds by construction
  rather than by a second implementation agreeing with the first (`L3-WRT-004`).

### Added

- **`--delta-scope <SCOPE>` / `[merge] delta_scope`** on both CLIs, accepting
  `per-file` (default) and `global`, case-insensitively. An unrecognised name is
  a config error (exit `5`) from TOML and a usage error (exit `4`) from the CLI.
- Conformance case `merge-delta-scope-global` pins the `global` behavior so the
  pre-v2.11.0 numbers stay covered, alongside five `delta_scope` snippets in the
  config-parser parity corpus and the key in the fuzzer palette.
- **`scripts/diagnose-vendor-delta.py`** — a maintainer/operator diagnostic for a
  vendor CSV whose `DELTA` column disagrees with ours. It recomputes the column
  under a range of candidate rules and reports which one reproduces the vendor's
  own values (a rule at 100% is the vendor's definition), answering *what rule
  the vendor follows* rather than merely *that* it differs. It reads the vendor
  CSV only — no MIE file and no decoder run — so it works on a machine that has
  the vendor output and nothing else. Wired into the divergence-reporting
  checklist in `docs/VENDOR-CSV-DIFFS.md` §6/§7.

### Fixed

- **SonarCloud: the quality gate's only failing condition** — a `MAJOR`
  path-traversal finding (`pythonsecurity:S8707`) on the `--config` path. The
  operator-supplied path was read after only an `exists()` check, which is true
  for directories, FIFOs and character devices: `--config <dir>` surfaced a raw
  `IsADirectoryError` and `--config /dev/zero` would read forever. Both loaders
  now require a **regular file** before reading, with identical message text.
- **Six `CRITICAL` cognitive-complexity findings** (`S3776`), refactored without
  behavior change: `order.rs::next` (18 → split into `accept`),
  `config.rs::parse_toml` (32 → `parse_section_header` + `parse_key_value`),
  `config.rs::is_toml_number_literal` (29 → rewritten as the grammar's own
  productions), `config.rs::with_overrides` (16 → a declarative field list),
  and the two Python config functions that mirror them.
- **Regex character classes** (`S6353`) now use `\w` / `\d` — compiled with
  `re.ASCII`. Taking the suggestion literally would have been a **bug**: Python's
  `\w`/`\d` are Unicode-aware and would have accepted identifiers like `stricté`
  and digits like `٤٢` that the Rust parser's `is_ascii_alphanumeric` /
  `is_ascii_digit` reject, silently diverging the two config parsers. The fuzzer
  palette gains non-ASCII identifiers and digits so the flag cannot be dropped
  unnoticed.
- **A redundant exception class** (`S5713`): `except (BrokenPipeError, OSError)`
  → `except OSError`, since the former is a subclass of the latter.
- **22 test-file findings**: `S5778` (15) hoists setup calls out of
  `pytest.raises` blocks so only the call under test can satisfy them — a
  constructor failure would otherwise pass those tests for the wrong reason;
  `S9073` (7) splits composite assertions so a failure names which half broke.
- **Six module comments that still described merged `DELTA` as recomputed on the
  global timeline** — the behavior this release changes. They sat in exactly the
  places a reader checks first: the `merge` module docs, the `_drain_heap`
  docstring, and the branch comment above the single-file/merge split in both
  CLIs (`rust/src/merge.rs`, `rust/src/cli.rs`,
  `python/src/mie_decoder/merge.py`, `python/src/mie_decoder/cli.py`). The
  `apply_global_delta` docstrings, which correctly describe the `global` branch
  they belong to, are unchanged.
- **The PlantUML sources had drifted several releases behind the code** — the
  known gap recorded (and deferred) in the pre-release notes for this version,
  now closed. `component.puml` and `dataflow.puml` were missing the `merge`
  module entirely (v2.6) as well as `order` (v2.9); `dataflow.puml` labelled the
  writer's row as "DDC vendor column order", stale since v2.10.0 moved `ERROR` /
  `ERROR_CODE` to the tail; `class.puml` was missing `DeltaScope` and eight
  `DecoderConfig` fields; and both diagrams' exit-code summaries stopped at `3`,
  predating the usage / configuration / merge-incompatible codes `4`, `5` and
  `6` and the `empty-recording` class at `0`. The CI drift check only verifies
  that the committed SVGs match their PUML sources, so it cannot detect a PUML
  that is missing a module — these were found by reading, and the same class of
  gap can recur.

### Notes

- The `docs/ROADMAP.md` "per-recorder DELTA" item is partly delivered: the
  common case (one file per recorder) is covered by `--delta-scope`. What remains
  is keying DELTA on a recorder *identity* parsed from the file name, which
  matters only if one recorder's output is split across files or several
  recorders' output is combined into one.
- **Known gap: the `diagrams` CI job is not currently enforcing anything.** The
  pinned PlantUML 1.2026.5 throws `IllegalStateException` (smetana's `qsort`
  during `mincross`) on a `!pragma layout smetana` diagram; PlantUML catches it
  per-file, exits `0`, and never writes that diagram's SVG — so the
  `git diff --exit-code` drift check compares a file the render never touched
  and reports success. Two independent symptoms confirm it: the committed SVGs
  are a mix of renderer versions (`1.2026.5` and `1.2026.7beta1`), which the
  check should have rejected, and the PUML-only commit in this release passed
  the job with knowingly stale SVGs. The SVGs here were rendered with
  `1.2026.7beta11`, which does not crash. Resolving this means choosing a
  renderer version that renders all three diagrams and failing the step on a
  non-empty stderr or a missing output file, rather than trusting the exit
  code — deferred so it can be done as its own change with its own CI run.
- The canonical row order of L1-OUT-003 is unchanged by this release: at a tied
  `TIME_STAMP` a merged decode still orders rows by `RT` then `MSG` across all
  inputs. Only the `DELTA` *value* is now per-file. So a merged CSV restricted to
  one input file carries that file's single-file `DELTA` values, while a row's
  position within a tie may still interleave inputs — a deliberate split between
  the value and its presentation.

## [2.10.0] — 2026-07-30

### Changed — BREAKING

- **`ERROR` and `ERROR_CODE` moved to the end of the CSV, after `XMT_GAP`.** They
  were at columns 42–43, between `DELTA` and `IM_GAP`; they are now columns 45–46.
  The three gap columns move with it, from 44/45/46 to their vendor positions
  42/43/44. Column *names* and cell *contents* are unchanged — this is purely a
  reordering.

  | Column | ≤ v2.9.0 | ≥ v2.10.0 |
  |---|---|---|
  | `DELTA` | 41 | 41 *(unchanged)* |
  | `ERROR` | 42 | **45** |
  | `ERROR_CODE` | 43 | **46** |
  | `IM_GAP` | 44 | **42** |
  | `RCV_GAP` | 45 | **43** |
  | `XMT_GAP` | 46 | **44** |

  Everything at index ≤ 41 is unaffected — `TIME_STAMP`, `RT`, `MSG`, all 32 data
  words, `STAT`, `CMD`, `MUX`, `TERM_NAME`, `BUS`, `DELTA` — so most consumers need
  no change. Only code that referenced the six columns above **by number** is
  affected; anything reading by header name already works.

  **Rationale — this fixes a latent vendor-compatibility bug.** `ERROR` and
  `ERROR_CODE` have no DDC vendor counterpart: the vendor tool emits **44**
  columns and does not report bus errors as CSV fields at all. Surfacing them is a
  decoder feature (L2-ERR-002). Placing them *inside* the vendor block pushed
  `IM_GAP` / `RCV_GAP` / `XMT_GAP` two positions right of their vendor indices, so
  every positional comparison against vendor output past `DELTA` was silently
  comparing the wrong fields — while all the column *names* still matched, which
  is why it went unnoticed. With the decoder's columns appended instead, column
  *N* of a decoded CSV is column *N* of a vendor CSV for all 44, and
  `cut -d, -f1-44` now recovers the vendor layout exactly.

  **Migration.** Prefer resolving columns by header name — `csv.DictReader` in
  Python, or an awk header scan — which cannot drift again:

  ```bash
  awk -F, 'NR==1 {for (i=1;i<=NF;i++) if ($i=="ERROR_CODE") c=i; next} $c!="" {print}' mie.csv
  ```

  `docs/VENDOR-CSV-DIFFS.md` §8 has the full mapping table and both name-based
  recipes.

### Fixed

- **Documentation that described `ERROR` / `ERROR_CODE` as vendor columns.**
  `docs/VENDOR-CSV-DIFFS.md` listed both under "cells that match exactly" — i.e.
  claimed byte-identical content against the vendor tool — and counted the vendor
  layout as 46 columns. Neither is true: the vendor CSV has 44 columns and neither
  of these among them. The document now separates the 44-column vendor block from
  the 2 decoder additions, and its `awk` comparison recipes (which used the
  pre-move indices) are corrected.

### Changed

- `L1-OUT-001` and `L2-WRT-001` amended to state the rule that made this fixable
  and keeps it fixed: the vendor block occupies columns 1–44, and **decoder-added
  columns are appended after it, never interleaved within it**. Any column added
  in a future release goes at the tail, so vendor indices stay stable.
- Both writers now expose a `VENDOR_COLUMN_COUNT` constant (44) marking the
  boundary, and both test suites gained a test pinning it —
  `vendor_block_precedes_decoder_added_columns` in `rust/src/writer.rs` and its
  mirror in `python/tests/test_e2e.py`. They assert the gap columns sit at their
  vendor indices and that no decoder column appears inside the vendor block.
- `python/tests/test_e2e.py::test_csv_first_row_fields` now resolves columns by
  **name** rather than hardcoded index. It had been written against the wrong
  positions, so it asserted the bug was correct — a positional test that encodes
  the layout it is meant to verify cannot catch a layout error.
- All 43 conformance oracles and the 3 Python golden files regenerated. Each
  change was verified to be a **pure column permutation** — same cells, different
  order — before being accepted; no cell content changed anywhere.

## [2.9.0] — 2026-07-29

### Changed — BREAKING

- **CSV rows are now written in a canonical order: `TIME_STAMP`, then `RT`, then
  `MSG`.** Records that share a timestamp used to be emitted in whatever order
  they happened to arrive — raw DDC capture order for a single file, and
  "whichever input you listed first" for a multi-file merge. They are now ordered
  by remote terminal, then by subaddress, then receive (`R`) before transmit
  (`T`) at the same subaddress (`L1-OUT-003`). Subaddress ordering is **numeric**,
  so `2R` precedes `11R`.

  This is shipped as a **minor** release because it breaks no API and no
  configuration: every existing flag, config key, and column keeps its meaning,
  and every cell keeps its value. What changes is the *order of rows* in the
  output file, which is a behavioral break for anything that diffs decoded CSV
  byte-for-byte. `cargo-semver-checks` cannot see a change of this kind, so it is
  called out here rather than caught by a gate.

  Rationale: the old equal-timestamp order carried no data meaning. RT 21 could
  precede RT 3 purely because it came from `recorder_a.mie`, so listing the same
  recordings in a different order produced a differently-ordered CSV, and the two
  implementations had no shared rule to be held to. Analysts read a decoded
  recording by remote terminal and message; ordering ties by the `RT` and `MSG`
  columns makes equal-timestamp traffic comparable between runs, between input
  orderings, and between the Rust and Python implementations.

  **Who is affected.** A 1553 bus carries one transaction at a time, so on a
  single-bus recording same-microsecond ties essentially do not occur and output
  is unchanged. The cases that do change:

  | Situation | Effect |
  |---|---|
  | Single-bus recording | No change — no ties to reorder. |
  | **Dual-bus** recording | Bus A and bus B transactions are genuinely concurrent and can share a microsecond; those rows may reorder. |
  | Multi-file merge with overlapping recorders | Ties now order by RT/MSG instead of by input position — merged output no longer depends on how you listed the files. |
  | Byte-for-byte diff against DDC vendor CSV | Row order may differ within one timestamp. See migration below. |

  **Migration.** If you need the previous behavior — most importantly for a
  byte-for-byte vendor-CSV diff — disable reordering with the new
  **`--max-sort-group 1`** (or `[output] max_sort_group = 1`). A cap of `1` makes
  every record its own sort group, so nothing is reordered and output is raw
  capture order.

  | Goal | Command |
  |---|---|
  | Canonical order (new default) | `decode rec.mie -o out.csv` |
  | Previous capture order | `decode rec.mie -o out.csv --max-sort-group 1` |
  | Vendor-exact decode | `decode rec.mie -o out.csv --no-mux --max-sort-group 1` |

  `docs/VENDOR-CSV-DIFFS.md` §3a documents the vendor-diff implications, and the
  documented validation workflow there now passes both flags.

- **`SPURIOUS_DATA` rows are pinned, not sorted.** A spurious record carries no
  Command Word, so it has no `RT`/`MSG` to sort on. Rather than sorting it to one
  end of its timestamp group, it is excluded from the sort and kept immediately
  after the record it followed on input. This preserves the adjacency that
  `ERROR_CODE = 0x2000` ("continues the preceding errored record", `L2-ERR-005`)
  is defined in terms of — a sort that separated the pair would leave that code
  describing nothing.

### Added

- **`--max-sort-group N` / `[output] max_sort_group`** (`L2-WRT-022`,
  `L3-WRT-003`) — bounds how many consecutive same-`TIME_STAMP` records are
  buffered while ordering rows. Range `[1, 1048576]`, default `4096`. `1`
  disables reordering entirely (the migration path above). Present and identical
  on both CLIs. On overflow the run is written in **arrival order** with one WARN
  and decoding continues; no record is dropped and the decode does not fail. The
  cap exists because the reorder stage is the only part of the pipeline whose
  buffer depends on the data rather than on the input count — without it, a
  corrupt recording whose timestamps all decode to the same value would buffer
  the whole file, breaking the constant-memory guarantee and `L1-ROB-001`.

- New shared module implementing the ordering stage in both implementations:
  `rust/src/order.rs` (an `Ordered<I, E>` iterator adapter reached via
  `OrderIterExt::order_rows`, `L3-RS-016`) and
  `python/src/mie_decoder/order.py` (an `order_rows` generator, `L3-PY-016`). It
  is wired as the **last** stage before the writer on both the single-input and
  merge paths, so the ordering guarantee holds over exactly the rows that reach
  the CSV. No new external dependency — Rust uses the standard library's stable
  `slice::sort_by_key`, Python uses `list.sort`.

- Six conformance cases pinning the new behavior cross-implementation:
  `tie-canonical-order` (all three key levels plus R-before-T from a deliberately
  wrong input order), `tie-spurious-pinned` (error → `0x2000` continuation
  adjacency survives the sort), `tie-across-timestamps` (no reordering across
  timestamps), `tie-cap-disabled` (`--max-sort-group 1` restores capture order),
  `tie-cap-overflow` (cap degrades to arrival order without losing rows), and
  `tie-merge-across-recorders` (a merged tie orders by RT, not by input
  position). Eight `max_sort_group` snippets were added to the config-parser
  parity corpus and the key to the config fuzzer's palette.

### Changed

- Coverage floors ratcheted to baseline−2pp per the `docs/MAINTAINER-GUIDE.md`
  §10 policy, now that the new module raised both numbers: Rust `cargo cov-ci`
  from 84 line / 83 region to **87 line / 86 region** (measured 89.50 / 88.63),
  and the Python `fail_under` from 88 to **92** (measured 94.65).
- Both fuzz harnesses (`L1-ROB-001`) now run the reorder stage on the fuzzed
  path with a deliberately small cap, so random bytes that decode to repeated or
  all-zero timestamps exercise the cap-overflow branch rather than only a
  pathological hand-written input.

### Notes

- `L2-MRG-002`'s statement was amended: its `(microseconds, file index,
  within-file sequence)` key is now explicitly the merge heap's **internal**
  order, not the order the CSV shows. A heap key alone cannot produce canonical
  equal-timestamp order — the heap holds one record per input, so it never sees
  two same-timestamp records from the *same* input at once, which is why the
  reorder stage sits downstream instead.
- The `docs/ROADMAP.md` "identity-based merge tiebreak" item was restated: input
  position survives only as the **residual** tiebreak, below RT and MSG.

## [2.8.0] — 2026-07-28

### Changed — BREAKING

- **Errored and SPURIOUS records now go inline in the main CSV by default, and
  `--inline-errors` has been removed.** The polarity of the error-mode flag is
  reversed: what used to require `--inline-errors` is now what you get with no
  flag at all, and the old split-file layout is opt-in via the new
  **`--separate-errors`** (`[decode] error_mode = "separate"`). The built-in
  default of `decode.error_mode` changes from `"separate"` to `"inline"`.

  Rationale: inline is the layout the DDC vendor tool itself emits, so a default
  decode is directly diffable against vendor output with no flags — and no
  errored record is silently absent from the file the operator actually opened.

  **Migration.**

  | Before | After |
  |---|---|
  | `decode rec.mie -o out.csv --inline-errors` | `decode rec.mie -o out.csv` |
  | `decode rec.mie -o out.csv` (split output) | `decode rec.mie -o out.csv --separate-errors` |
  | `[decode] error_mode = "inline"` | unchanged (now also the default) |
  | `[decode] error_mode = "separate"` | unchanged (still honoured) |

  `--inline-errors` is **not** accepted as a deprecated alias: passing it is a
  usage error (exit `4`) naming the flag. A script that silently kept working
  would have been relying on behaviour that is now the default anyway, so the
  failure is deliberate and loud. A config file that sets `error_mode`
  explicitly is unaffected — only the *default* moved.

  Note this breaks the **CLI** contract, not the library API: `cargo-semver-checks`
  reports no required semver bump because `write_csv` / `write_csv_split` and the
  public types are untouched. The automated gate cannot see this class of change,
  so the version decision is a human one.

- With `--separate-errors` and stdout output, Python now emits the same
  "stdout output forces inline error mode" WARN as Rust. Previously only Rust
  warned; that was near-harmless while separate was the default (Rust warned on
  every stdout decode) but now the flag is an explicit request the writer cannot
  honour, so silence would hide it.

### Changed

- **The two implementations' operator-facing log output is now aligned.** A
  scenario-by-scenario diff of both CLIs' stderr (clean decode, header skip,
  inline and separate errors, sync recovery, empty recording, wrong file,
  exclude and include filters, `count`, strict failure) showed a difference in
  every one; all are now resolved:
  - Rust gained the filter diagnostics Python already had — an INFO summary of
    the active sets, a DEBUG line per dropped record, and an INFO
    passed/excluded tally. The tally is emitted from `Drop`, so unlike a
    generator's end-of-stream hook it still appears when a consumer stops early
    (`| head`); Python's now runs from a `finally` for the same reason.
  - Both render filter sets **sorted** (`exclude_rts=[0, 15, 31]`). Python holds
    these as `set`s, whose iteration order is not guaranteed, so the line was
    previously unstable between runs as well as different from Rust.
  - Rust's error-record line now names the transfer direction, and Python's
    "no valid records" error now names the scan window in bytes — each side was
    missing a detail the other reported.
  - Python no longer logs a second write summary duplicating the writer's own,
    and the split-mode wording matches on both.
- `Bus` derives `Ord` so filter diagnostics can sort it (additive; no API break).
- **A broken pipe no longer leaks CPython's shutdown-failure exit code.** The CLI
  returned `0` correctly, but the interpreter then flushed `sys.stdout`, hit the
  dead pipe, and overrode the status with **120** — so `decode … | head` still
  exited non-zero, violating L2-WRT-018. Caught by the new real-pipe subprocess
  test on Python 3.14 / Linux (earlier versions happened to leave an empty buffer
  and escaped it). The console script and `python -m mie_decoder` now run through
  a `main_cli` wrapper that repoints fd 1 at the null device once stdout is known
  to be dead. `main()` itself is unchanged and side-effect-free, so in-process
  callers (tests, embedders) are unaffected — the fd surgery happens only at the
  real process boundary.

Findings from a no-change audit of both implementations and the full document
set. Four behavioral defects — one of them silent data loss — plus a sweep of
documentation that had drifted from the code.

### Fixed

- **Python: `--time-format auto` was silently ignored when a config file set a
  different format.** `DecoderConfig.with_overrides` resolved each override by
  truthiness, and `TimestampFormat.AUTO` is `0`, so the override was discarded
  and the config-file value won. Decoding an IRIG recording with
  `--config <file setting standard> --time-format auto` therefore dropped every
  record as a structural-invariant violation and still reported **exit 0 /
  `complete`** — silent data loss — while Rust honored `auto` and decoded
  normally. Override resolution is now presence-based, matching Rust's
  `Option<T>` semantics exactly. The same defect silently discarded
  `--format ''` (Rust exits 1, Python exited 0) and would have discarded an
  `ErrorMode.SEPARATE` override.
- **Python: `dump` had no broken-pipe handling at all** (L2-WRT-018).
  `mie-decoder dump big.mie | head` aborted with an uncaught traceback and
  exit 1, where the Rust CLI exits 0. `dump` now classifies a closed consumer
  as a clean stop and keeps real output failures (disk full, permission) as
  runtime errors.
- **Python: broken pipes were not recognized on Windows.** CPython raises
  `BrokenPipeError` only on POSIX; on Windows a write to a closed pipe surfaces
  as a bare `OSError` with `EINVAL`, so the `except BrokenPipeError` guard in
  the streaming writer never fired and `decode … | head` exited 1 with an error.
  A shared `is_broken_pipe` predicate (the analogue of Rust's
  `MieError::is_broken_pipe`) now classifies both forms; the widened `errno`
  match is scoped to Windows so a genuine POSIX `EINVAL` write failure stays a
  failure.
- **Rust: a strict-mode error-record failure left the iterator live.** The
  error-record arm yielded its `Err` (`UnknownErrorCode`, or an out-of-bounds
  Error Word) without setting `done`, unlike every other error path in the
  reader, so a library caller iterating `RecordIter` directly kept receiving
  records after the failure — where the Python reader's generator is already
  dead. The CLI masked it because the writer returns on the first `Err`.
- **Test isolation:** `configure_logging()` leaked the `mie_decoder` logger
  level across tests, so a bare `caplog.at_level(...)` captured nothing once any
  earlier test had reconfigured logging. The full suite passed only by accident
  of file ordering (`pytest tests/test_config.py tests/test_cli.py` failed). An
  autouse fixture now restores the package logger, and the two bare `at_level`
  call sites name their logger like every other one.

### Documentation

- `VENDOR-CSV-DIFFS.md`: the vendor-diff `awk` recipe had off-by-one column
  indices — it compared the always-empty `TERM_NAME` and silently dropped
  `ERROR_CODE` from the comparison. Also corrects the "15 CSV columns" heading
  (there are 46, in 15 named groups).
- `EXAMPLES.md`: removes an unfinished editing note (`← no, see actual`) that
  shipped in the `--allow-partial` walkthrough with the wrong exit-class line;
  corrects "one of four codes" (there are seven) and adds the missing exit-6 arm
  to the canonical batch script; refreshes the stale `dump` sample output.
- `CONFIG-REFERENCE.md`: `decode.strict` and `output.format` were both
  documented as having no CLI flag; `--strict` and `--format` exist on both
  CLIs. Also completes the quick-reference block, which omitted `[merge]`,
  `detect_records`, and `lookahead_records`.
- `config/default.toml`: the `detect_records` comment described the sync
  look-ahead (which is `lookahead_records`, documented correctly directly
  below) rather than the timestamp-format detection probe it actually controls.
- `DATA-SCENARIOS.md`: documented `-o -` for stdout output; there is no such
  convention — it writes a file literally named `-`. Omitting `-o` is the
  mechanism.
- `USER-GUIDE.md`: the exit-code table omitted exit 6 entirely and miscounted
  the classes; the exit-class list omitted `empty-recording` and
  `merge-incompatible`.
- `ARCHITECTURE.md`: documented six structural invariants (there are seven —
  `L2-SYN-027` was absent from the file), and both error-type listings omitted
  `TimestampFormatMismatch`, `IncompatibleMergeInputs`, and `NonMonotonicInput`.
- Writer docstrings in both implementations still described `MUX` as an
  always-empty vendor placeholder; it has been populated from the input file
  name by default since `L2-WRT-020`.
- `decode.py`: the mode-code decision tree contradicted the implementation for
  transmit mode codes with no data word.
- `L3-PY-014` mandated `dataclasses.replace` for the merged-DELTA stage, which
  the implementation deliberately avoids (it erases the concrete return type
  under strict `mypy`); the requirement now describes `with_delta`.
  `L3-RS-001` understated the MSRV floor as 1.85 where the crate pins and CI
  gates 1.88.
- **`sync.py` is now pure, matching `sync.rs`.** The Python sync helpers logged;
  the Rust ones deliberately do not (the reader owns all user-facing messaging).
  Because a helper has none of the caller's context, `find_first_record` logged
  `WARNING: No valid record found in first N bytes of file` whenever it returned
  `None` — including for a **valid empty recording**, where returning `None` is
  the expected result. An operator saw a warning claiming a healthy recording had
  no records, immediately contradicted by the reader's own correct "empty
  capture" line. Rust never emitted it. The six log statements move to
  `reader.py`, carrying the same detail as their Rust counterparts (sync loss now
  names the offending type and word count), so the two implementations' log
  streams correspond line for line.
- `L2-SYN-012` (header size logged at INFO) had **no Rust verification at all** —
  the trace matrix listed a single Python test, and that test asserted against
  `find_first_record` rather than the reader that emits the line. Adds a Rust CLI
  test, retargets the Python one at the reader, and pins the purity contract so
  logging cannot creep back into the validation helpers.
- The Rust error-record log rendered an unknown DDC code as `code=0x0199 ()` —
  an empty description where Python printed a word. `dump.rs` already had a
  fallback for this; it is now a shared `ddc_error_description_or_unknown`
  helper used by both call sites.
- `MIE-FORMAT.md` gave the Type Word's minimum word count as "5 (Type Word +
  **Standard** timestamp + Command Word)" — 5 is the IRIG minimum; the Standard
  minimum is 4, as both implementations enforce. Also corrects the claim that
  lenient mode emits `UNKNOWN` for an unrecognized error code (the CSV always
  carries the raw code) and drops the retired `microsecond-hi < 16` scoring term
  from the auto-detection table.
- `sync.py`'s module docstring documented a 4096-byte header scan (it is 64 KB)
  and listed only three of the five IRIG range checks.
- The PlantUML diagram titles were stamped `MIE-Decoder v2.0` while the project
  is at 2.7.1. The stamp is dropped rather than bumped — a title version is
  exactly the drift-prone number this repo omits elsewhere. The `(v2.0)` labels
  *inside* the diagrams are provenance ("new in v2.0", confirmed by the 2.0.0
  release notes) and are left as history.
- README and `CLI-REFERENCE.md` claimed each CLI's `--help` "is generated from
  the same definitions". It is not: Python's help comes from `argparse`, Rust's
  is a hand-maintained string. The parity is real but comes from the
  `cli-surface-parity` conformance check, which diffs the long-option set across
  both CLIs and fails CI on divergence — including a flag the Rust parser still
  accepts after its help stopped listing it. Both docs now say that.
- Repairs a scrambled rustdoc comment block in `cli.rs` (two functions' docs had
  been interleaved), a stale "exit 2" comment on a path that exits 4, and drops
  a dead `Ok(None)` branch. Removes a vacuous `us_hi < 16` term from the
  timestamp-detection score in both implementations (`ts_middle & 0xF` is always
  below 16). Doc indexes in `README.md` and `MAINTAINER-GUIDE.md` were missing
  entries, and the documented conformance command omitted its interpreter
  requirement.

## [2.7.1] — 2026-07-11

Patch release from an extended round of team review. Resolves a large batch of
Rust↔Python parity findings — most of them in the hand-rolled TOML config
parser, now aligned to a single explicit grammar and guarded by a curated parity
corpus plus a differential fuzzer — alongside `-V` / `-v` and `--time-format` CLI
parity, hex `dump` arguments, `[merge]` config-file support on Rust, merge
output-collision safety, removal of two latent Rust panic sites, Standard-
timestamp rounding parity, atomic-writer temp-file safety, and expanded
conformance coverage. No decode-output or public-API change: the full Rust and
Python suites, the byte-exact conformance oracle, and the new config parity
corpus and fuzzer all pass.

### Fixed

- **Version short flag parity across implementations.** Both CLIs now accept
  `-V`, `-v`, and `--version` (with any letter case in the long form). Previously
  Rust accepted only `-V` and Python only `-v`, so a flag that worked on one
  implementation failed on the other.
- **`--time-format` is now case-insensitive on both implementations.** The Python
  CLI previously rejected `--time-format IRIG` / `Auto` (argparse `choices` are
  case-sensitive) while Rust accepted them. Both CLIs and both config loaders now
  route every spelling through one shared case-insensitive parser per
  implementation, so the CLI and `[decode] time_format` can never disagree on
  which names are accepted. An unrecognized name is still rejected (CLI: usage
  error, exit 4; config file: config error, exit 5).
- **`dump` numeric arguments accept the same notation across both flags and
  implementations.** Python's `dump --records` was decimal-only while its own
  `--offset` / `--length` already accepted `0x` hex (and Rust accepted hex on all
  three) — an internal and cross-implementation inconsistency. All three now go
  through one shared parser per implementation that accepts decimal and `0x` hex
  (Python also accepts `0o` / `0b`) and rejects negative values, matching the
  Rust unsigned semantics. Invalid values are a usage error (exit 4).
- **Schema-invalid config values are now clean config errors on both
  implementations.** A TOML-valid but schema-invalid value — a non-string
  `time_format` / `error_mode` (e.g. `time_format = 1`), or a known section name
  assigned a scalar (`decode = true` instead of a `[decode]` header) — previously
  crashed the Python CLI with an unclassified `AttributeError` (exit 1), and the
  section-as-scalar case was silently ignored by the Rust loader (exit 0). Both
  now reject these as configuration errors (exit 5): Python validates section
  shape and string types at load time, and Rust rejects a known section name used
  as a scalar (the same silent-drop footgun as a section the loader never reads).
- **Python now caps a record's data-word payload at 32, matching Rust.** A
  crafted record whose Type Word claims more than 32 data words previously left
  Python's `MieMessage.data_words` unbounded, while Rust's `DataWords` inline
  buffer (`[u16; 32]`) truncates it — so a library consumer saw different payload
  lengths across implementations for such a record. `MieMessage` now truncates to
  32 on construction. No CSV impact (the writer emits at most 32 `WDnn` columns
  either way), and MIL-STD-1553B caps a transaction at 32 words, so conforming
  records are unaffected.
- **Standard-timestamp microsecond rounding is now bit-identical across
  implementations.** Python computed `int(x + 0.5)` while Rust used `f64::round`;
  for a value one ULP below a half-integer these differ by 1 µs (Python rounds
  `x + 0.5` up to `1.0`), a latent break of the byte-exact CSV contract on
  calibrated Standard timestamps (`--standard-tick-rate-hz`). Python now rounds
  half-away-from-zero via `floor(x) + (frac >= 0.5)`, matching `f64::round`
  exactly. Rate-gated and rare, but the two decoders can no longer disagree.
- **Removed two latent panic sites in the Rust decoder (L1-ROB-001).** The
  `unreachable!()` in the reader's timestamp decode and the `assert!` in the
  public `DataWords::from_slice` were both provably unreachable / uncallable past
  their guards, but they nicked the no-panic invariant. The reader now decodes a
  stray `Auto` format as IRIG (its documented fallback) instead of panicking, and
  `DataWords::from_slice` truncates an over-length slice to the 1553B 32-word cap
  (matching `from_iter_capped`) instead of asserting. The `#[deny]`-on-`warn`
  clippy gate is extended to `panic` / `unreachable` / `todo` / `unimplemented`
  (in addition to `unwrap` / `expect`) so the invariant is now enforced in CI.
- **Python merge output-collision guard now fires when `--allow-partial` drops a
  merge to a single surviving input.** The guard was gated on the surviving
  reader count, so a multi-input merge that lost all but one input to
  `--allow-partial` skipped it — and because the writer's own single-input check
  is bypassed for any requested merge, an output path pointing at one of the
  inputs could overwrite it in place. It is now gated on whether a merge was
  *requested* (checking the full requested input set), matching the Rust CLI.
- **Atomic CSV writers use a unique, exclusively-created temp file.** Both
  implementations named their temp file `<dest>.mie-decoder.tmp.<pid>` and opened
  it with truncation, so two writers targeting the same destination *within one
  process* could collide on the temp path and clobber each other before the
  atomic rename. The temp name now also carries a per-process monotonic counter
  and a nanosecond timestamp, and the file is created with exclusive-create
  (Rust `create_new` / Python mode `"x"`, i.e. `O_EXCL`) with retry — so
  concurrent same-destination writers can no longer share a temp file. Output
  content, permissions, and the atomic-rename behavior are unchanged.
- **The Rust config loader now rejects non-identifier section headers.** A
  section name with a hyphen, space, or quote (`[bad-section]`, `[bad section]`,
  `["bad"]`) was stored as an oddly-named section on Rust while Python's whitelist
  rejected it. Rust now applies the same simple-identifier check to section
  headers that it already applied to keys, so both reject the form (exit 5).
- **Python now warns for a root-level unknown scalar key, matching Rust.** A
  stray top-level key such as `bogus = true` loaded silently on Python
  (`_warn_unknown_keys` skipped non-table entries), while Rust logged
  `unknown TOML key: [] bogus`. Python now emits the same warning, honoring the
  documented WARN-and-continue contract for unknown keys. (Both still accept the
  config — this is observability only.)
- **Python's config array splitter now handles escaped quotes like Rust.** An
  array whose string element contained an escaped quote followed by a comma
  (e.g. `["a\", b"]`) was mis-split on the interior comma and rejected on Python,
  while Rust (which tracks the preceding backslash) accepted it. The Python
  splitter now mirrors Rust's `push_quoted_char`, so both treat the comma as
  inside the string. Added to the parity corpus and the fuzzer's value set.
- **Config numeric and string-escape grammars are now aligned, and a
  differential fuzzer guards config parsing.** Rust routed numeric literals
  through native `i64` / `f64` parsing, which accepts non-TOML forms (`08`, `1.`,
  `[01]`) that Python's `tomllib` rejects, and the two supported different string
  escape sets. Both now enforce one explicit grammar: numbers match
  `[+-]?(0|[1-9][0-9]*)(.[0-9]+)?([eE][+-]?[0-9]+)?` (no leading zeros / bare
  trailing dot / `0x`-`0o`-`0b` / `_`), and strings accept only the `\"` `\\`
  `\n` `\t` escapes on both. A new differential fuzzer
  (`tests/conformance/config_fuzz.py`) generates config documents and asserts
  both CLIs agree on accept/reject, so a divergence is caught in CI rather than
  reported after the fact.
- **Config parsing is now aligned by an explicit whitelist grammar on both
  implementations, closing a whole class of Rust↔Python divergences.** The two
  parsers accepted different TOML subsets — full-TOML `tomllib` on Python vs a
  minimal hand-rolled parser on Rust — so forms outside the flat `[section]` +
  `key = value` schema (inline tables, multi-line arrays, `1_000` / `0x08` /
  `0o` / `0b` numbers, date-times, quoted keys) were honored by Python but
  rejected by Rust. Rather than continue rejecting each divergent form one at a
  time, Python now validates every config line against the *same* flat grammar
  the Rust parser accepts — a whitelist run before `tomllib` — and Rust rejects
  non-identifier keys, so both refuse anything outside the subset with a config
  error (exit 5). A new differential parity corpus (config snippets run through
  both CLIs asserting identical accept/reject) guards the alignment in CI.
- **Dotted keys, dotted section headers, and array-of-tables headers are now
  rejected by both implementations.** `tomllib` nested a dotted key
  (`decode.strict = true`) or dotted header (`[output.no_clobber]`) and accepted
  an array-of-tables header (`[[decode]]`), while the Rust hand-rolled parser
  silently dropped the dotted key, stored a dotted header as a section literally
  named `output.no_clobber` (ignoring its keys), or misread `[[decode]]` — so the
  same config behaved differently on each implementation, and a mis-typed safety
  option such as `[output.no_clobber]` / `output.no_clobber = true` was silently
  ignored on Rust (dropping overwrite protection). Both now refuse these forms as
  config errors (exit 5); the config schema is the flat `[section]` +
  `key = value` form only.
- **The Rust config loader now rejects duplicate TOML keys and re-declared
  section headers.** A repeated `(section, key)`, or a `[section]` header
  declared more than once (even with different keys inside), previously kept the
  first value / silently merged the blocks on Rust while Python's `tomllib`
  rejected it — so a malformed config could decode differently on each
  implementation. Rust now raises a config error (exit 5) to match, per the TOML
  spec (which forbids defining a table twice). `CONFIG-REFERENCE.md` now
  documents the exact TOML subset the hand-rolled Rust parser accepts (and the
  full-TOML features it does not, e.g. multi-line arrays and `1_000_000`
  underscore separators).
- **The Rust config loader now honors the `[merge]` section.** Setting
  `[merge] collapse_duplicates` / `collapse_window_us` in a TOML config file had
  no effect on the Rust CLI — the section was silently ignored and even reported
  as an unknown key — while the Python CLI applied it. Cross-recorder duplicate
  collapsing (L2-MRG-007) can now be configured from a file on both
  implementations, exactly as `config/default.toml` and `CONFIG-REFERENCE.md`
  already document; the `--collapse-duplicates` / `--collapse-window-us` CLI
  flags still override it. A negative `collapse_window_us` is rejected at load
  time (exit 5). A new conformance case pins the config path to the CLI path.

## [2.7.0] — 2026-07-08

Minor release from a second round of team review. Adds a small public-API
accessor and two diagnostic/tooling improvements, plus documentation
clarifications. No change to decode behavior or the byte-exact CSV output — the
full Rust and Python suites and the 60-case conformance oracle pass unchanged.

### Added

- **`MieMessage.subaddress()` convenience accessor** in both implementations
  (Rust `subaddress(&self) -> Option<u8>`, Python `subaddress` property),
  mirroring the existing `rt()` — returns `None` for SPURIOUS_DATA. This is the
  additive public API that makes the release a minor bump; `cargo-semver-checks`
  confirms no breaking change.
- **`dump` now explains errored records.** The record-aware hex dump annotates
  each record with an explicit `error flag (bit 14): SET/clear`, a classified
  `Format:` line (e.g. `TRANSMIT`, `MODE_CODE_NO_DATA`), and — for errored
  records — an `Error: 0xNNNN → <DDC description>` line, so *why* a record is
  errored is legible at a glance. The classifier is guarded (an unclassifiable
  record degrades to `(unclassifiable)`, never throws) since `dump` runs on
  suspect files.
- **Conformance runner single-implementation modes** (`--python-only` /
  `--rust-only`). Because the byte-exact oracles are committed under
  `tests/conformance/expected/`, each implementation can now be validated on its
  own with no other toolchain present — e.g. a Python-only, air-gapped host runs
  `python tests/conformance/run.py --python-only` with no cargo. `--update-expected`
  still requires both implementations (it regenerates the oracles only when Rust
  and Python agree).

### Changed

- **Docs: default error routing made prominent.** Errored and SPURIOUS records
  go to `<output>_errors.csv` by default (separate mode); `--inline-errors` puts
  them in the main CSV and matches the DDC vendor tool. A README callout and
  cross-references now state this up front (it had surprised operators expecting
  an errored record in the main CSV).
- **Docs: `--glob` matches filenames, not recordings.** Documented that a glob
  which catches a non-recording (a `README`, a log) fails with exit 2 — and in a
  merge aborts before any output — so precise patterns (`dir/*.mie`) are the
  right default. (`docs/CLI-REFERENCE.md`, `docs/USER-GUIDE.md`.)
- **Docs: config schema no longer duplicated in the README.** The drifted TOML
  snippet (wrong filter defaults / logging level, missing keys) is replaced by
  links to the two authoritative sources, `config/default.toml` and
  `docs/CONFIG-REFERENCE.md`.
- **Docs: recorded why the Python wheel build uses `poetry -P` (not `-C`).**
  `poetry -C python build` doubles the source path on Windows (verified on Poetry
  2.3.4); `-P` (`--project`, Poetry ≥ 2.0) is the deliberate workaround.

### Fixed

- **README library examples can no longer silently rot.** The Rust example is now
  compiled in CI (as a `no_run` doctest via `include_str!` in `src/lib.rs`, plus a
  checked `examples/library_usage.rs`, with `cargo test --doc` added to CI and the
  pre-commit hook); the Python example is executed against a fixture by
  `tests/test_readme_examples.py`. Both would have caught the earlier
  `write_csv`-arity and `message.subaddress` breakages.

## [2.6.2] — 2026-07-07

Documentation and packaging release from a team review: corrects a license
declaration contradiction, hardens `dump` output on Windows, fixes the README
library examples, and consolidates the CLI reference. No functional change to
decode behavior or CSV output — the conformance oracle and full test suites are
unchanged.

### Changed

- **License declaration corrected to Apache-2.0.** The `LICENSE` file has always
  been the Apache License 2.0, but the package metadata and READMEs declared
  `MIT` — a contradiction that is legally ambiguous for downstream users and
  fails automated license scans. Every declaration (`rust/Cargo.toml`,
  `python/pyproject.toml`, the three READMEs, `rust/deny.toml`) now correctly
  states **Apache-2.0**, matching the `LICENSE` file. This corrects the declared
  license shown on crates.io / PyPI; the `LICENSE` text itself is unchanged.
- **`docs/ROADMAP.md` is now forward-looking only.** Removed ~1,840 lines of
  completed history (the "Team Review Backlog", "Production-Readiness Audit",
  "Architecture Audit", and "Documentation Initiative" sections) that duplicated
  the requirements docs and had begun minting requirement IDs which collided with
  real assignments (e.g. a proposed `L2-CONF-006` vs. the assigned
  public-library-API `L2-CONF-006`). Completed work is now tracked solely in
  `CHANGELOG.md`, the `L1/L2/L3-REQ.md` requirements, and git; the roadmap no
  longer mints `L2-*`/`L3-*` IDs.

### Added

- **`docs/CLI-REFERENCE.md`** — a complete per-flag reference for the `decode`,
  `count`, and `dump` subcommands and the global options: value, default, range,
  and config-key equivalent for every flag. The root README now links to it
  rather than duplicating an (already-incomplete) exhaustive flag table, giving
  the CLI parameter documentation a single home.

### Fixed

- **`dump` no longer crashes on a redirected stdout on Windows.** The
  record-aware dump annotation contains non-ASCII characters (box-drawing,
  en-dash, `§`); piping `mie-decoder dump file.mie > out.txt` on a stock Windows
  shell (cp1252 code page) previously raised `UnicodeEncodeError` and aborted the
  dump. The CLI now forces UTF-8 on stdout/stderr (encoding only — `newline`
  handling is untouched, so the byte-exact CSV contract is unaffected), matching
  the POSIX default and the raw UTF-8 the Rust build already emitted.
- **README library examples now compile / run.** The Rust example called
  `write_csv` with two arguments — it takes three (added
  `WriteOptions::default()`); the Python example printed `message.subaddress`,
  which does not exist (now `message.msg_label`). Both fixes were verified by
  compiling / running the examples against a real fixture.

## [2.6.1] — 2026-07-07

Maintainability release: resolves the SonarCloud findings from the v2.6.0
analysis. No change to the CLI, the public library API, or the byte-exact CSV
output — the full Rust and Python test suites and the 60-case
cross-implementation conformance oracle pass unchanged.

### Security

- **Log injection hardened (CWE-117).** User-controlled values written to the
  logs (notably input file paths) are now escaped for carriage-return and
  line-feed, so a crafted path can no longer forge or split log records.
  (SonarCloud `S5145`.)

### Changed

- **Cognitive-complexity sweep — behavior-preserving.** 28 functions across both
  implementations were brought under the complexity threshold by pure helper
  extraction, spanning config parsing, timestamp-format detection, the hex
  `dump`, filters, the CSV writers, sync validation, the multi-file merge, the
  CLI argument parsers and decode entrypoint, and the reader hot paths (the Rust
  decode loop and the Python `__iter__` generator). Internal plumbing only — a `MieMessage`
  DELTA-copy helper was factored out and the reader's per-format payload
  extraction was split by message family; no public API, CLI, or CSV change.
- **Minor code-health fixes.** Return-type precision on the merge DELTA copy,
  exception-logging hygiene (log-and-return instead of `logging.exception`
  outside the handler), removal of an unused timestamp-conversion parameter, and
  a redundant `isinstance` guard dropped. (SonarCloud `S5886`, `S5890`, `S8572`,
  `S1172`, `S2589`.)

## [2.6.0] — 2026-07-06

### Added

- **End-of-records terminator awareness (`0x0000`) and the empty-recording
  outcome.** DDC recorders cap the record stream with a null Type Word
  (`0x0000`) and, for a channel that captured no traffic, produce a file that is
  *only* that terminator (e.g. an unused MIL-STD-1553 station — literally the two
  bytes `00 00`). The decoder now understands the terminator: a file whose stream
  opens on it is recognized as a **valid but empty recording** and decodes to a
  **header-only CSV at exit 0** (new `empty-recording` exit class, with a WARN),
  instead of the previous `NoValidRecords` error (exit 2) that halted batch
  processing. `count` prints `0` and exits 0. The wrong-file guard is preserved —
  only a genuine `0x0000` lead word qualifies; arbitrary non-MIE bytes still exit
  2. New requirements **L1-EXIT-010**, **L2-RDR-021**, **L2-SYN-028**; L1-EXIT-002
  amended to carve out the empty case. Documented in `docs/MIE-FORMAT.md` §2.4.

### Fixed

- **The last record of every recording is no longer silently dropped.** Because
  each recording ends `…record, 00 00`, the final record's look-ahead follower is
  the terminator, which the N-record look-ahead (L2-SYN-005) treated as an invalid
  follower — so that record failed validation and was dropped. Single-record files
  failed entirely (`NoValidRecords`). The look-ahead now honors the terminator on
  the trusted-boundary path (forward decode / first-record detection) per
  L2-SYN-028; sync **recovery** stays strict so a mis-aligned candidate can't
  validate off a stray zero data word. This bug went unnoticed because the
  conformance fixtures never included a real terminator — now they do
  (`multi-record-then-terminator` pins all rows survive).
- **`count` now exits 2 (not 1) on a wrong-file input**, matching `decode` and the
  L2-CLI-011 exit-code table.

### Tests

- New Rust unit/CLI tests and Python pytest cases for the terminator (forward
  vs. recovery), single-record-then-terminator, last-record survival, empty
  recording (exit 0 + header-only CSV, `count` → 0), and the wrong-file guard.
- Six new cross-implementation conformance cases, including three fixtures
  carrying a real `00 00` terminator (`empty-recording`,
  `single-record-then-terminator`, `multi-record-then-terminator`) plus two
  `count`-path cross-checks — `count-no-valid-records` (both impls exit 2 on a
  wrong file, guarding the count/decode exit-code parity of L2-CLI-011) and
  `count-multi-record-then-terminator` (both impls count 3, proving the last
  record survives via the count path).

### Documentation

- **`docs/MIE-FORMAT.md` §2.3 now explains *why* the N-record look-ahead
  exists**, not just what it checks: the format has no per-record sync marker,
  so the Type Word's `word_count` is the only framing; a single Type Word
  passes by chance ~1 in 20 of the time, so the look-ahead chains consecutive
  self-consistent lengths into a synthetic sync check that drives the
  false-positive rate toward zero. §2.4 explains, as design rationale, why the
  end-of-records terminator is accepted as an end-of-chain on the forward
  paths but not during sync recovery. `docs/ARCHITECTURE.md` Phase 3 points to
  the deep rationale.

## [2.5.3] — 2026-06-28

### Fixed

- **Transmit mode codes carrying no data word are no longer dropped from the
  CSV.** A non-broadcast transmit mode code (the common MIL-STD-1553 mode codes
  0–15 — "transmit status word", "transmitter shutdown", "reset remote
  terminal", …) was unconditionally classified `MODE_CODE_TX_DATA`, which expects
  a Status **and** a data word; with no data word the record was too short for
  the L2-SYN-022 word-count capacity check and was **silently skipped in lenient
  mode** (the default), so these messages never reached the output. A no-data
  mode code now classifies as `MODE_CODE_NO_DATA` regardless of direction (the
  wire shape is `ModeCmd + Status` either way; the `CMD` column preserves the
  direction) and is written with empty `WD*` columns. Fixed in both
  implementations; the requirement `L2-MSG-004` (which had specified the buggy
  "classified by direction independent of word count") is corrected.

### Tests

- **End-to-end, cross-implementation conformance coverage for all 11 decoded
  message formats** (previously 4 had none: `RECEIVE_BROADCAST`,
  `RT_TO_RT_BROADCAST`, `MODE_CODE_BCAST_NO_DATA`, `MODE_CODE_BCAST_DATA`, plus an
  IRIG `MODE_CODE_RX_DATA` and the headline no-data-transmit regression). Every
  raw Type Word type / decoded format is now proven to decode to a byte-identical
  CSV row in both the Rust and Python CLIs.

### Documentation

- **`MIE-FORMAT.md` §6 now opens with an at-a-glance index of all 11 decoded
  message formats** — each format's source Type Word code, its identification
  rule, and a link to its per-format byte-shape subsection — plus a note on the
  two-layer (raw type code → decoded format) classification and how a flagged
  error record layers on top of any format. Consolidates a standalone
  message-types reference into the format authority (the raw type-code table
  already lived in §4.1 and the `0x01xx` / `0x20xx` error-code tables in §7).

## [2.5.2] — 2026-06-28

### Fixed

- **`merge --allow-partial` now writes a `.partial` for a per-file failure
  detected at *open* or *priming*, not only mid-file** (L2-MRG-004). Previously a
  bad input whose first record was unreadable / non-MIE (e.g. all-0xFF) was
  skipped during priming **without** arming the deferred-partial state, so a
  `good.mie + bad.mie --allow-partial` merge wrote a normal `out.csv` (no
  `.partial`) and reported success — silently losing the signal that an input had
  been dropped. An input that fails at *open* (empty / unreadable / missing)
  likewise aborted the batch even under `--allow-partial`. Both now drop the
  offending input with a WARN naming it, complete the merge from the rest, and
  commit the combined output as `<output>.partial` (and `<stem>_errors.partial`
  in separate mode), exit 0 — uniform with the existing mid-file behavior. New
  Rust + Python regression and CLI tests plus a cross-impl conformance case
  (`merge-allow-partial-priming`, which also implements the previously-declared
  `expected_partial` oracle in the conformance runner) pin it.

### Documentation

- **New `docs/DATA-SCENARIOS.md`** — a plain-language, scenario-indexed reference
  for how the tool handles every data condition (clean / error / spurious
  records, IRIG / Standard / freerun timestamps, empty / truncated / non-MIE
  files, multi-file merge including per-file `--allow-partial` and duplicate
  collapsing, output modes, filters, MUX), each with its CSV / log / exit
  outcome, plus a glossary that defines the jargon (including "oracle"). Linked
  from the docs index and USER-GUIDE.md; cross-references ERROR-CATALOG.md and
  MIE-FORMAT.md.

## [2.5.1] — 2026-06-28

### Fixed

- **Duplicate collapsing no longer faults or over-collapses on a lenient
  non-monotonic merge input** (`--collapse-duplicates`). When an input file is
  not internally time-sorted (L2-MRG-006) the merged stream can step backward;
  the dedup window's one-sided `us - survivor_us` gap then underflowed —
  **panicking the Rust CLI in debug builds (exit 101)** in `merge.rs` — and, in
  Python, bypassed the window check so a record could collapse against a survivor
  far outside `--collapse-window-us`. The window comparison now uses the
  **absolute** time distance (with saturating eviction in Rust), so collapsing is
  panic-free and never drops a row outside the window; on such known-bad order it
  is best-effort. Sorted input is unaffected (byte-identical output). Pinned by
  new Rust + Python regression tests and a cross-impl conformance case
  (`L2-MRG-007`).

## [2.5.0] — 2026-06-27

### Added

- **Cross-recorder duplicate collapsing in the multi-file merge** (opt-in,
  `--collapse-duplicates` / `[merge] collapse_duplicates`, both implementations).
  When several recorders witness the same MIL-STD-1553 transaction, the merge can
  now collapse those duplicate rows into one instead of inflating the message
  count. A duplicate is the same wire content (Type / Command / Status Words,
  data words, error word) seen by a **different** input within
  `--collapse-window-us` microseconds (default `0` = exact-µs match; widen it for
  recorders whose clocks differ slightly). Same-recorder repeats and single-file
  decodes are unaffected; de-duplication runs before the global-DELTA stage so
  DELTA reflects the deduped timeline, and the suppressed count is logged.
  **Off by default — the default never drops a row.** Adds a `[merge]` config
  section; specified by `L1-MRG-003` / `L2-MRG-007` and pinned by cross-impl
  conformance fixtures.
- **Rust public-API SemVer gate** (`cargo-semver-checks` CI job). Runs
  `cargo semver-checks check-release` against the latest release tag
  (`baseline-rev`, with `release-type: minor`), failing a PR that makes a
  **breaking** public-API change to the crate without a major version bump;
  additive changes pass. Completes the Rust CI tooling deferred in v2.4.0 — the
  v2.4.0 tag (the first under the `rust/` layout) is the baseline.
- **Bandit security gate** (`bandit` dev dependency + a `bandit` CI job). Runs
  `bandit -r src/mie_decoder` — security static analysis (SAST) over the Python
  package source — and blocks merge on any finding at the default
  severity/confidence. The initial scan is clean (no issues across the package).

## [2.4.0] — 2026-06-27

### Added

- **SonarCloud (SonarQube Cloud) analysis workflow**
  (`.github/workflows/sonarcloud.yml`). On pushes/PRs to `main` it scans both
  implementations — Rust coverage (LCOV via `cargo cov-lcov`) and Python
  coverage (Cobertura XML via `pytest --cov`) feed the SonarCloud Quality Gate —
  and exports BUG/VULNERABILITY findings to GitHub code scanning (SARIF). Project
  identity and token come from `SONAR_*` repository secrets; the workflow is a
  clean no-op when they are absent. A SonarCloud Quality Gate badge was added to
  the root `README.md`.
- **CodeQL security-scan workflow** (`.github/workflows/codeql.yml`). Analyzes
  the Rust crate and the Python package (both `build-mode: none`) and reports to
  GitHub code scanning. (CodeQL's Rust support is no-build only; macro-heavy
  files extract with reduced fidelity — a known upstream limitation.)
- **Pylint lint gate** (`pylint` dev dependency + a `pylint` CI job). Lints the
  Python package source (`src/mie_decoder`) and blocks merge below 10/10.
  Configuration lives in `python/pyproject.toml` `[tool.pylint.*]` — curated
  disables for the deliberate function-local-import pattern and the `too-many-*`
  complexity family already covered by SonarCloud, plus line length. Reaching a
  clean pass also removed several dead imports and added exception chaining
  (`raise ... from`) in `cli.py` / `config.py`. No behavior change (conformance
  unaffected).
- **Ruff lint + format gate** (`ruff` dev dependency + a `ruff` CI job running
  `ruff check` and `ruff format --check`). Covers the Python package **and
  tests**; config in `python/pyproject.toml` `[tool.ruff]` (line length 100, to
  match pylint). Adopting `ruff format` reformatted the package + tests to its
  Black-style canonical form (mechanical, no behavior change — conformance
  unaffected); `ruff check` also caught two real import bugs in the test suite
  (a missing `from pathlib import Path` and an unused one).
- **Vulture dead-code gate** (`vulture` dev dependency + a `vulture` CI job).
  Scans the package **and tests** together (so test-only usage counts); config
  in `python/pyproject.toml` `[tool.vulture]`. The initial pass removed a genuinely
  unused test fixture (`single_transmit_record`) and ignores three intentional
  names — the argparse `option_string` interface arg and the documented module
  constants `MAX_RECORD_BYTES` / `ALL_KNOWN_ERROR_CODES`. No behavior change.
- **Rust rustdoc gate** (`rust-doc` CI job): `cargo doc --no-deps` with
  `RUSTDOCFLAGS="-D warnings"`, failing on broken intra-doc links and other
  rustdoc lints. The initial pass fixed a broken intra-doc link in `reader.rs`.
- **Rust MSRV gate** (`rust-msrv` CI job): `cargo check --all-targets` on a
  pinned **1.88** toolchain, so the declared `rust-version` is actually enforced
  (the main `rust` job builds on stable and would otherwise mask a sub-MSRV
  feature). Adding this gate surfaced that the MSRV claim was already untrue —
  see Changed.
- **Rust supply-chain gate** (`cargo-deny` CI job): `cargo deny check` over the
  dependency tree — RustSec advisories, a license allow-list (MIT / Apache-2.0),
  duplicate/wildcard bans, and crates.io-only sources (config in
  `rust/deny.toml`). Its first run flagged a live advisory (see Security).
- **Rust `[lints]` table** in `rust/Cargo.toml`, so lint policy is enforced on a
  plain `cargo build` / `cargo clippy`, not just via CI flags:
  `unsafe_op_in_unsafe_fn` and `clippy::undocumented_unsafe_blocks` (the latter
  promotes the repo's `// SAFETY:` convention from a pre-commit grep to a real
  lint), plus `unreachable_pub`. Enforcing `unreachable_pub` tightened six
  crate-internal exit-code constants in `cli.rs` from `pub` to `pub(crate)`.

### Security

- **Upgraded `memmap2` 0.9.10 → 0.9.11** to resolve `RUSTSEC-2026-0186` (unsound
  unchecked pointer offset in `memmap2`, fixed upstream in 0.9.11), surfaced by
  the new `cargo-deny` gate. No API or behavior change (conformance unaffected).

### Changed

- **Rust MSRV raised from 1.85 to 1.88** (`rust-version` in `rust/Cargo.toml`).
  The sole dependency `memmap2` (≥0.9.7, and the locked 0.9.10) uses `let`-chains
  and so requires Rust 1.88, even though it under-declares its own MSRV as 1.63.
  The crate had therefore not actually built on 1.85 for several `memmap2`
  releases; 1.88 reflects the true floor. Edition 2024 itself still only requires
  1.85, so the L3 "MSRV 1.85 or newer" requirement remains satisfied.
- **Repository layout: the Rust crate now lives under `rust/`, mirroring
  `python/`.** The crate source (`src/`), integration tests
  (`tests/cli.rs`, `tests/integration.rs`), `Cargo.toml` / `Cargo.lock`, and the
  `.cargo/` coverage aliases moved from the repository root into `rust/`. The
  shared artifacts stay at the root: `config/default.toml`, the cross-impl
  `tests/conformance/` oracle, and `docs/`. **No runtime, CLI, or library-API
  change** — decoded output is byte-identical and the cross-implementation
  conformance contract is unaffected. Building from source now runs from the
  crate directory (`cd rust && cargo build`; cargo's `-C` flag is still
  unstable); CI, the pre-commit hook, the coverage wrapper
  (`scripts/coverage.sh`), the conformance runner, and the trace-matrix
  generator were updated to match, and documentation/source cross-references
  that named root paths (`src/…`, `tests/…`) were re-rooted under `rust/`.

- **Internal: decomposed the Python `_run_decode` CLI handler into focused
  helpers** (override building/validation, reader opening, message-stream
  building, writing, and the success/error exit-code classifiers), mirroring the
  existing decomposition of `run_decode` in `rust/src/cli.rs` and cutting the
  function's cognitive complexity well under the SonarCloud threshold. **No
  behavior change** — exit codes, stderr, and decoded output are identical
  (conformance unaffected); new direct unit tests (`python/tests/test_cli.py`)
  raise `cli.py` coverage.

### Documentation

- **Per-implementation READMEs.** Added `rust/README.md` and `python/README.md`
  holding the language-specific build, library-usage, development, and structure
  sections. The root `README.md` is slimmed to the shared content (project
  overview, CLI reference, configuration, error handling, supported formats) and
  links both implementation READMEs from the overview, the new Building section,
  Project Structure, and Development. `rust/README.md` also resolves the dangling
  `readme = "README.md"` in `rust/Cargo.toml`; `python/README.md` remains the
  package long-description referenced by `python/pyproject.toml`.

## [2.3.0] — 2026-06-23

### Added

- **MUX column populated from the input file name (L2-WRT-020, both
  implementations, behavior change).** The previously-always-empty `MUX` CSV
  column is now filled from a field of each input's file name, so a decoded CSV
  can carry the source/recorder identity that operators encode in the name (e.g.
  `full_loadout.draw.data.1553.aa.unused.mie_irig` → `MUX = aa`). The basename
  is split on a configurable delimiter (default `.`) and a configurable 0-based
  field index (default `4`; negative counts from the end) selects the value; an
  out-of-range/empty field leaves MUX empty. In a multi-file merge each row
  carries the MUX of the file it was decoded from. Configurable via the new
  `[mux]` config section (`enabled` / `delimiter` / `field`) and the new
  `--no-mux` / `--mux-delimiter` / `--mux-field` CLI flags (identical in both
  CLIs). Extraction is hand-rolled (delimiter + index, **no new dependency**).
  Pinned by the `mux-from-filename`, `mux-merge-per-file` (per-file values
  through the merge), and `mux-disabled` conformance cases plus unit tests.
  - **Behavior / vendor-compat change:** because this is **on by default**,
    default output is no longer byte-for-byte identical to the DDC vendor CSV
    when the input name carries the field. Pass **`--no-mux`** (or set
    `[mux] enabled = false`) to restore empty MUX for a vendor-exact diff. The
    other placeholder columns (`TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`)
    remain empty; the `MUX` column position is unchanged. `L2-WRT-013`,
    `VENDOR-CSV-DIFFS.md`, and the "Shared Commitments" / "Conventions" notes
    were updated accordingly.

### Fixed

- **Standard-timestamp mode-code records with data are no longer misclassified
  (L2-MSG-004, both implementations, behavior change).** The mode-code
  data-vs-no-data classifier used absolute word-count thresholds that assumed an
  IRIG timestamp (3 timestamp words): broadcast data iff `word_count > 5`,
  receive data iff `word_count >= 7`. A Standard timestamp is only 2 words, so a
  Standard broadcast-with-data record (`word_count = 5`) and a Standard
  receive-with-data record (`word_count = 6`) were classified as *no-data* — the
  data word was emitted under `STAT` instead of `WD01`. The thresholds are now
  derived from the resolved timestamp word count (broadcast data iff
  `word_count >= timestamp_words + 3`, receive data iff
  `word_count >= timestamp_words + 4`), so classification is correct under both
  formats. **IRIG output is byte-identical** (the IRIG thresholds are unchanged);
  only Standard mode-code-with-data records change. Pinned by the new
  `standard-mode-code-data` conformance case (the data word now appears in
  `WD01`) plus Standard-timestamp classifier unit tests in both implementations.

### Documentation

- Corrected the Rust coverage gate in `CONTRIBUTING.md`: it stated the
  `cargo cov-ci` floor was 70% line / 70% region, but the enforced value in
  `.cargo/config.toml` is **84% line / 83% region**. Updated the gate references
  and the stale headroom rationale.
- Corrected the look-ahead description in `docs/MIE-FORMAT.md`: it claimed the
  following Type Word during look-ahead "must also pass checks 1–4", but
  L2-SYN-005 and the implementation (`src/sync.rs`) only require a **plausible
  Type Word** (known message type + in-range word count). Reworded to match the
  normative requirement and the code.

## [2.2.0] — 2026-06-22

### Added

- **Within-file monotonicity detection in the multi-file merge (L2-MRG-006,
  both implementations, identical).** The time-sorted merge assumes each input
  is internally chronological (capture order). It now verifies this: when a
  record's absolute IRIG microsecond key steps strictly backward relative to the
  previous record from the **same** input (caused by sync-loss recovery or a
  day/year rollover), the merge detects it in O(1) per record. In the default
  (lenient) mode it logs a WARN once per offending input — naming the file and
  the backward step — and still emits every record in heap order (it never
  re-sorts, preserving the O(number-of-files) streaming guarantee). Previously
  such an input produced silently out-of-order merged rows with no diagnostic.
  A new `merge-non-monotonic-within-file` conformance case pins the cross-impl
  WARN + byte-identical output. A single-file decode is unaffected (the merge
  path only runs for two or more inputs).
- **The Python `decode` CLI gains `--strict` and `--format`, completing
  argument-surface parity with the Rust CLI.** `--strict` (raise on invalid
  records) was previously settable only via `[decode] strict = true` in a config
  file on the Python side, even though the Rust CLI and the docs already exposed
  it as a flag; it is now a `decode` flag in both, overriding the config like
  every other CLI override. `--format` (output format; `csv` only at present)
  likewise matches Rust: `csv` is accepted and any other value is a **runtime
  error (exit 1)** applied after config load, distinct from the config-file
  `output.format` validation (a config error, exit 5) that both already shared.
  With these, both CLIs accept an identical set of flags across `decode`,
  `count`, and `dump`. The `merge-non-monotonic-strict` conformance case now
  drives strict via the shared `--strict` arg (rather than a config file),
  directly proving both CLIs accept it.

### Changed

- **Strict mode now fails a merge whose input is not internally time-sorted
  (behavior change, both implementations).** Under `--strict` or
  `[decode] strict = true`, a within-file backward timestamp step
  (L2-MRG-006, above) is treated as a record error — `NonMonotonicInput` /
  `MieNonMonotonicInputError`, mapped to **exit 1** (the runtime/decode-error
  class, like other strict record failures) — rather than being silently
  accepted. Lenient mode (the default) is unchanged apart from the new WARN.
  Pinned by the `merge-non-monotonic-strict` conformance case.
- **The Rust `decode --help` now documents `--manifest` and `--glob`** (and
  shows `decode <INPUT>...` for the multi-input merge). Both flags were already
  accepted by the parser since v2.1.0 but were missing from the help text; no
  behavior change.

### Fixed

- **Python: `[filter] exclude_types` / `include_types` now accept integer codes
  and bound-check them, matching Rust (cross-impl parity).** A TOML config with
  a bare integer type code (e.g. `exclude_types = [2]`) previously crashed the
  Python CLI with an uncaught `AttributeError`, while the Rust CLI accepted it;
  Python now accepts integer codes (bounded to `0..=255`) like Rust. An
  out-of-range code (e.g. `"0x100"`) was previously **silently accepted** by
  Python — making the filter a no-op — whereas Rust rejected it; Python now
  rejects it too (a CLI usage error → exit 4, or a config error → exit 5),
  matching Rust. **Python: a non-string `[filter] exclude_buses` entry** (e.g.
  `exclude_buses = [1]`) likewise crashed with an uncaught `AttributeError`; it
  now raises a clean config error (exit 5) naming the problem, as Rust does.
  Found by a cross-implementation parity audit. Pinned by the new
  `config-exclude-types-int` (int code == name, byte-identical) and
  `usage-error-exclude-types-out-of-range` (exit 4 both impls) conformance
  cases, plus Python unit tests.

### Maintenance

- **CLI flag-surface parity is now gated against drift.** The conformance
  runner (`tests/conformance/run.py`) gained a `check_cli_surface` step that
  extracts the full long-flag set from each CLI's `--help` (top-level +
  `decode` / `count` / `dump`) and fails the run — naming the offending flag —
  if the Rust and Python surfaces diverge. A flag added to only one
  implementation now fails CI even when no conformance case exercises it. (A
  one-word Rust `--help` reword — `--flag=value` → `=value` — removes a phantom
  token so the check is exact.)

## [2.1.0] — 2026-06-21

### Added

- **Multi-file, time-sorted merge (L1-MRG, both implementations, identical).**
  The `decode` command now accepts more than one input and emits a single CSV
  whose records are in global time order. Inputs are supplied by **multiple
  positionals** (`decode a.mie b.mie -o out.csv`), a **`--manifest <file>`**
  (one path per line; blank lines and `#`-comments ignored), or a
  **`--glob <pattern>`** (single-directory `*`/`?` filename match, expanded by
  the tool so it works on Windows); the three methods are **mutually exclusive**.
  A single input behaves exactly as before. The merge is a streaming k-way heap
  merge keyed on absolute IRIG microseconds, so resident memory stays O(number
  of files) and O(1) in record count, and DELTA is recomputed on the unified
  timeline (L2-MRG-005). Time-sorted merge requires every input to be
  calendar-locked IRIG: a Standard-format input, a freerun-leading input, or a
  mixed-format set is rejected before any output with a new **exit code 6**
  (L1-EXIT-009 / L2-MRG-003). More than `MAX_MERGE_FILES` (256) inputs, or
  combining input methods, is a usage error (exit 4). Per-file failure follows
  the existing `--strict` / lenient / `--allow-partial` policy across the batch:
  `--allow-partial` truncates a failed file, completes the merge from the rest,
  and writes the combined output as `.partial` (L2-MRG-004). Rust uses
  `std::collections::BinaryHeap` and a hand-rolled glob matcher; Python uses
  `heapq` — no new dependency in either. Output is byte-identical across the two
  implementations (pinned by the `merge-ordered` conformance case).

## [2.0.1] — 2026-06-21

### Added

- **L2-SYN-027: RT-to-RT Command-Word `data_word_count` agreement check (both
  implementations).** An RT-to-RT or RT-to-RT-broadcast record whose two
  Command Words declare different `data_word_count` values is now rejected as
  corruption: strict mode surfaces a record error, lenient mode logs a WARN and
  skips the record. The bus protocol carries a single count for the transfer
  (`docs/MIE-FORMAT.md` §6.3), so a mismatch is internally inconsistent. This is
  a post-extract check mirroring the sibling L2-SYN-023 (Cmd2 direction).
  **Behavior change:** 2.0.0 silently accepted such records (emitting truncated
  data); they are now rejected. Valid DDC recordings always agree, so
  conformance is unaffected.

### Changed

- **The record-aware `dump` now logs its scan-stop anomalies (L2-CLI-013, both
  implementations).** Invalid `word_count`, truncated-record, and (Rust)
  offset-overflow stops are emitted through the logger at `WARN` — to stderr,
  subject to `--log-level` — in addition to the existing inline `!! …` note in
  the hex report. This makes the dump's diagnostics consistent with the
  reader's and visible on the normal log channel; the hex-report format is
  unchanged.

### Fixed

- **Reader: RT-to-RT payload extraction could read past the record extent
  (Python).** For RT-to-RT and RT-to-RT-broadcast records, the data-word count
  comes from the second Command Word (Cmd2), but the L2-SYN-022 capacity
  invariant is computed from Cmd1. A malformed record with a small Cmd1 count
  (passing the capacity check) and an over-claiming Cmd2 caused
  `_extract_payload` to read beyond the Type Word's declared extent — into the
  following record, or past EOF as a `struct.error` (caught by the L1-ROB-001
  fuzz harness). Payload reads are now bounded to the record extent so an
  over-claim can no longer overrun, matching the Rust reader (L2-DEC-009); the
  record is then rejected by the new L2-SYN-027 invariant. The Rust
  implementation was already bounded; this brings Python to parity. New
  regression tests in both implementations
  (`rt_to_rt_cmd2_overclaim_does_not_overrun`) plus an arbitrary-bytes
  robustness test for the `dump` subcommand in both implementations
  (`dump_arbitrary_bytes_never_panics`).

## [2.0.0] — 2026-06-18

A joint Rust + Python major release whose theme is **parity**: the two
tools now function the same way. The Python CLI gained the capabilities and
the exact argument surface of the Rust v2 CLI, and the Python writer now
streams in constant memory like Rust. Breaking changes are confined to the
Python CLI and the Python library API (see **Removed**); CSV and count output
are byte-for-byte unchanged, and the Rust CLI is unchanged. Both
implementations ship from the single tag `v2.0.0`.

### Added

- **Include filters in the Python CLI** — `--include-types`, `--include-rts`,
  `--include-buses`, `--include-subaddresses`, the positive complement of the
  exclude filters and the last filtering capability Python lacked. A message
  passes only if it matches no active exclude set and is contained in every
  active include set; SPURIOUS_DATA (no RT/SA) is dropped when an RT/SA include
  filter is active. Include filters are CLI-only overrides (no config-file
  key), matching Rust. Pinned by the new `L3-PY-013`; `L3-RS-010` was reworded
  from "Python is not required to expose equivalent CLI syntax" to require it.
- **A `count` subcommand in the Python CLI** (`mie-decoder count rec.mie`),
  matching the Rust `count` subcommand. Counts valid records after the config
  file's `[filter]` section, printing the integer to stdout and a status line
  to stderr (`L3-PY-010`).
- **The Python package root now exposes its decoder entry point** —
  `from mie_decoder import MieFileReader` (and `MieMessage`) now works without
  reaching into submodules, advertised via `__all__`. Previously the package
  root exposed only `__version__`, so `L3-PY-007` ("expose the decoder entry
  point as a typed callable importable from the package root") was unsatisfied
  in code and traced only through the conformance-runner requirement rather
  than a real root-API check. The re-export is additive (submodule paths are
  unchanged), and `L3-PY-007` now traces to a dedicated root-API test
  (`tests/test_package_api.py`); its verification method moved from Inspection
  to Test + Inspection. The requirement was also re-parented off the
  conformance-wiring requirement (`L2-CONF-002`) onto a new public-API-surface
  requirement, `L2-CONF-006` ("each maintained implementation SHALL expose a
  documented public library API with its decode entry point importable from
  the package/crate root", under `L1-CONF-001`), with a Rust counterpart
  `L3-RS-013` verifying the crate-root `pub use` re-exports — so both
  implementations' library surfaces are now pinned and tested.

### Changed

- **The Python CLI now shares one identical argument surface with the Rust
  CLI.** `--inline-errors` (a boolean flag; separate is the default) replaces
  `--error-mode {separate,inline}` (`L3-PY-011`); `--config` is now a global
  option placed *before* the subcommand
  (`mie-decoder --config site.toml decode rec.mie`) rather than a
  per-subcommand flag; and every filter flag takes one comma-separable,
  repeatable value (`--exclude-rts 15,31` ≡ `--exclude-rts 15 --exclude-rts 31`)
  instead of space-separated `nargs`, with RT/SA values bounded to u8 (0–255)
  exactly like Rust. The cross-implementation conformance suite dropped its
  per-impl argument translation: a single `args` vector now drives both CLIs,
  so the byte-for-byte conformance cases are a direct proof that the two tools
  accept the same arguments.
- **PY-streaming: the Python writer now streams in constant memory.** Both
  `write_csv` and `write_csv_split` previously collected every row into a list
  and materialized a full `pandas.DataFrame` before flushing, making Python
  decode memory `O(record_count)` — a multi-GB recording could exhaust RAM
  while the Rust CLI streamed the same input in constant memory. The writer
  now streams each row straight to the output handle through the
  standard-library `csv` module via two new primitives — `_AtomicCsvFile`
  (temp-file + `os.replace`, with `commit()` / `commit_partial()` /
  cleanup-on-failure) and `_StreamingCsvRowWriter` — ported from the Rust
  `AtomicCsvFile` / `CsvWriter` shapes. Python decode memory is now `O(1)` in
  the record count, matching Rust (`L3-PY-012` reworded from `O(record_count)`;
  verification raised from Inspection to a `tracemalloc` memory test). CSV
  output is unchanged, pinned by a new byte-exact golden characterization suite
  and the full conformance suite.

### Removed

- **The Python `decode --count` flag** (use the `count` subcommand) and the
  **`--error-mode` flag** (use `--inline-errors`; separate is the default).
  Python filter flags no longer accept space-separated values
  (`--exclude-rts 15 31`) — use commas or repeat the flag — and `--config` is
  no longer accepted after the subcommand.
- **The `pandas` runtime dependency**, leaving `tomli` (Python 3.10 only) as
  the Python package's sole runtime dependency — the same dependency-light
  story as the Rust crate.
- **The public Python helpers `mie_decoder.writer.messages_to_dataframe` and
  `dataframe_to_csv`.** Build a DataFrame from the public message stream
  instead: `pandas.DataFrame(map(message_to_row, MieFileReader(path)))`.

### Fixed

- **`logging.level = "OFF"` no longer crashes the Python CLI; it now silences
  all output, matching Rust.** Both implementations accepted `OFF` at config
  load, but Python then raised an uncaught `ValueError` when applying it
  (stdlib `logging` has no `OFF` level), while Rust correctly mapped it to
  "silence all" (`Level::Off`). Python now maps `OFF` to a level above
  `CRITICAL`, so a config with `logging.level = "OFF"` decodes cleanly and
  silently in both implementations. `OFF` was also added to the normative
  L2-CFG schema table, `CONFIG-REFERENCE.md`, `config/default.toml`, and both
  implementations' "invalid level" error messages (which under-reported the
  accepted set — Rust's also omitted `WARN`). Corrected the docs that claimed
  `CRITICAL` "behaves the same as `ERROR`": the decoder emits no
  `CRITICAL`-level messages, so `CRITICAL` (like `OFF`) suppresses all output.
  A new `log-level-off` conformance case pins the cross-impl behavior.
- **The `--log-level` CLI flag now accepts the same level set as the config
  file in both implementations.** The Python CLI previously accepted only
  `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` and was case-sensitive (rejecting
  `WARN`, `OFF`, and lowercase like `debug`), while the Rust CLI already
  accepted all seven case-insensitively but its `--help` and invalid-value
  message under-reported the set (omitting `WARN`/`OFF`, and `CRITICAL` in the
  help). Both CLIs now accept `DEBUG`/`INFO`/`WARNING`/`WARN`/`ERROR`/
  `CRITICAL`/`OFF` case-insensitively (matching `logging.level`), with help,
  invalid-value text, and the README aligned. The Python change is additive
  (a superset of what it accepted before). `--version` and `--help` are also
  now honored even alongside an invalid `--log-level` in both CLIs (Python
  previously failed on the bad flag before reaching `--version`/`--help`); the
  level is validated after those flags short-circuit, matching Rust.

## [1.5.1] — 2026-06-15

### Changed

- **Separate-mode output now commits the main CSV before the errors CSV in
  both implementations.** Rust previously committed errors-then-main while
  Python committed main-then-errors — a cross-impl divergence in the
  mid-commit failure residue. Aligned Rust to Python's main-first order so
  that, since the two files are committed sequentially (each is atomic on its
  own, but there is no cross-file atomic rename), a failure of the second
  commit leaves the **primary `main.csv`** behind rather than an orphan
  errors file, and a failure of the first commit leaves neither file. This
  only affects the rare partial-failure path; successful writes are
  unchanged, as is all CSV content. The order is now pinned by a new
  requirement (`L2-WRT-019`) and verified by mid-commit-failure tests in both
  implementations (the failing commit is forced by making the destination a
  directory).

### Fixed

- **Corrected the Python `count` help and README, which claimed the count is
  printed to stderr.** Both implementations print the integer count to
  **stdout** (the machine-readable datum) and only the human-readable status
  summary to stderr (`L3-PY-010` / `L3-RS-008`); the Rust help and the
  `count-one` conformance oracle were already correct. Fixed the
  `--count` flag help string and the README `count` description to match.
- **Completed the reference configuration `config/default.toml`.** The file
  is advertised as a fully-commented starter config, but omitted four
  documented, parsed keys: `decode.allow_partial`, `decode.detect_records`,
  `decode.lookahead_records`, and `output.no_clobber`. Added all four with
  commented descriptions, valid ranges (`[1, 32]` for the two record-count
  knobs), CLI-override notes, and their default values, so the starter file
  now covers every key in `CONFIG-REFERENCE.md`. Guarded by a new test in
  each implementation: a completeness check that the file mentions every
  documented key (Rust) and a parity check that the shared file loads with
  the documented defaults (both Rust and Python).
- **The Rust `dump` subcommand no longer reports success after an output
  write failure.** `hex_dump_raw` / `hex_dump_records` discarded every
  `writeln!` / hex-line result with `let _ =` and unconditionally returned
  `Ok(())`, and the stdout `BufWriter` swallowed flush errors on drop — so a
  dump whose output hit disk-full or a permission error (e.g.
  `dump > out.txt`) exited `0` with truncated or empty output, violating
  `L2-WRT-018`. The writers now propagate every write and an explicit final
  flush as a `WriterError`; the CLI surfaces it as a runtime failure (exit
  `1`) while still treating a broken pipe on stdout (`dump | head`) as a
  clean exit `0`. The Python `dump` already propagated via `print`, so this
  aligns Rust to the existing behavior. Covered by new write-failure and
  broken-pipe tests in both `dump.rs` and `cli.rs`.
- Corrected a false atomicity guarantee in the docs and source comments for
  separate-mode output. `ARCHITECTURE.md` §8 previously claimed the main and
  errors CSVs "both either succeed atomically or neither appears" — implying
  cross-file atomicity that does not exist. Rewrote the §8 note and the
  misleading `src/writer.rs` commit-ordering comment (whose stated rationale
  was backwards) to describe the per-file, main-first guarantee honestly.

### Documentation

- Fixed two factually-wrong source comments / doc descriptions. (1) The
  `src/reader.rs` mmap `SAFETY` comment claimed the file is "moved into the
  closure" and that "the mmap holds it alive" — there is no closure, and
  `Mmap::map(&file)` borrows the file rather than owning it; rewrote it to
  state the real contract (the OS mapping outlives the dropped `File`; the
  input must not be mutated while mapped, per `L1-EXIT-006`). (2) Several
  reader/sync module docs and `MIE-FORMAT.md` still described a *fixed*
  "two-record look-ahead" although the depth has been configurable
  (`N`-record, default 2) since `L2-SYN-026`; reworded them (and the
  `CLAUDE.md` / `CONTRIBUTING.md` preservation notes) to say "N-record
  look-ahead (default 2)".
- Removed stale hardcoded conformance-suite case counts from the reference
  docs, extending the `9b47121` "no drift-prone counts" policy to the docs
  that earlier cleanup missed. `ARCHITECTURE.md` (§1 and the conformance
  section), `USER-GUIDE.md`, and `VENDOR-CSV-DIFFS.md` cited "19-case" /
  "20-case" suites — both wrong (the suite has grown) and guaranteed to
  re-stale each release — so they now refer to the conformance suite
  generically; the live count lives only in `tests/conformance/manifest.json`.
  Also reworded a drift-prone "`[Unreleased]` is empty as of the v1.3.0 cut"
  note in `ROADMAP.md` to be version-agnostic. (The `README.md` and
  `MAINTAINER-GUIDE.md` locations flagged in review were already clean;
  ROADMAP's historical per-release case counts are intentionally kept.)
- Scoped the "IRIG day-field decoding across DDC card models" ROADMAP item
  (Decode correctness) as **blocked on external data**: recorded what is
  already known (the bits 13–5 binary slice is per-spec; only day-of-year
  diverges, only on some card models), the sample set required to make
  progress (recording + vendor CSV + true date + model/firmware id per
  card model), and the diff-and-solve method for when ground-truth data is
  available. No behavior change — the v1.5.0 advisory WARN remains the
  interim treatment.
- Designed the **multi-file time-sorted merge** Planned feature (Rust v1.x)
  and documented how it works. `ROADMAP.md` carries the full design — the
  streaming k-way (min-heap) merge that keeps memory O(file-count) rather
  than O(record-count), the IRIG microseconds-from-start-of-year merge key,
  the hard absolute-time and single-year constraints (Standard counters /
  freerun IRIG / cross-year inputs are not cross-file orderable), the
  file-local error-classification requirement, deterministic tie-breaking,
  and the open CLI/failure-policy decisions. `ARCHITECTURE.md` §12 explains
  the streaming merge mechanism (clearly marked not-yet-implemented) so the
  memory model is understood before the feature lands. No behavior change —
  design/docs only.

## [1.5.0] — 2026-06-15

### Added

- One-time IRIG day-of-year advisory (ROADMAP PRA-9). Both readers now emit
  a single WARN per decode the first time a calendar-locked (non-freerun)
  IRIG record is decoded, pointing to the documented day-of-year
  firmware-discrepancy limitation (`docs/VENDOR-CSV-DIFFS.md` §5). Advisory
  only — not a decode failure; freerun records don't trigger it and
  `--log-level ERROR` silences it.
- Targeted `L2-DEC-009` payload-bounding test in both implementations
  (ROADMAP PRA-8): an over-declaring record before a valid one is rejected
  in strict mode and skipped in lenient mode, with the following record
  decoded intact at its true offset — proving extraction never overruns
  into the next record. `L2-DEC-009` is now Test + Inspection verified.
- Scheduled fuzz burn-in (ROADMAP PRA-5). The L1-ROB-001 no-panic
  harnesses now honor a `MIE_FUZZ_ITERATIONS` override (default 256,
  deterministic), and a new `.github/workflows/fuzz.yml` runs them daily
  (and on manual dispatch) at 25 000 iterations per implementation. Added
  an explicit `L1-SYN-002` cumulative-scan-bound test in both impls
  (`recovery_scan_is_forward_only_and_bounded`) asserting that repeated
  recoveries advance strictly forward and never re-traverse already-scanned
  bytes.

### Fixed

- **Forcing the wrong `--time-format` no longer silently emits garbage
  timestamps** (ROADMAP PRA-2, implements the previously-unimplemented
  half of `L2-DEC-013`). When an explicit `--time-format` /
  `decode.time_format` contradicts the recording — the detection probe is
  *Decisive* for the other format — strict mode now raises a
  timestamp-format mismatch (exit `2`) and lenient mode logs a WARN and
  proceeds with the forced format. Marginal/ambiguous recordings are not
  flagged, so an intentional override of a misdetection still works.
  Verified by new forced-mismatch tests in both implementations and the
  `forced-format-mismatch-strict` conformance case.

### Changed

- **CLI exit codes are now a granular, cross-impl-identical taxonomy**
  (ROADMAP PRA-1). Behavior change: CLI **usage errors** (unknown/invalid/
  missing flag or argument, bad flag value, no subcommand) now exit **4**,
  and **configuration errors** (config file missing, malformed, or invalid
  value) now exit **5**. Previously Rust exited `2` for usage errors
  (colliding with the no-records class) and Python was inconsistent
  (`1` vs `2`). The shipped meanings of `0`, `2` (no records), and `3`
  (unrecoverable sync loss) are unchanged; `1` is now specifically the
  runtime/decode-error class. `count`/`dump` inherit `0/1/2/4/5` but never
  `3`. Both implementations return identical codes for the same condition,
  verified by the new `usage-error-bad-flag-value` (4) and
  `config-error-invalid-value` (5) conformance cases. Spec: new
  `L1-EXIT-007`/`L1-EXIT-008` and the revised `L2-CLI-011` table; docs:
  `ERROR-CATALOG.md`, `USER-GUIDE.md`, `EXAMPLES.md`.

### Documentation

- Flagged the CHANGELOG compare-URL footer (version-bump checklist step 2)
  as the most-often-missed release step in `MAINTAINER-GUIDE.md` §11. It was
  silently skipped on the `1.4.0` and `1.4.1` cuts — the footer's
  `[Unreleased]` link kept pointing at `v1.3.0...HEAD` with no `1.4.0`/
  `1.4.1` entries — and was repaired during this cut. Added a concrete
  post-edit cross-check against `git tag --sort=-creatordate`.
- Surfaced the Python large-file memory ceiling for operators (ROADMAP
  PRA-4). New `USER-GUIDE.md` §10 "Performance and large recordings" gives
  the per-implementation memory table, the "~5 GB RAM per ~10 M records"
  planning rule, and the recommendation to use the Rust CLI for multi-GB /
  10M+-record recordings (byte-identical output). `PY-streaming` (the
  constant-memory Python writer that will remove the ceiling) is now an
  explicit Planned entry and the next Python work item.
- Trace-matrix generator now surfaces and counts L1-level test markers
  (ROADMAP PRA-3). The "L1 → L2" table gained a Test Artifacts column, so
  direct-L1-marked leaves (e.g. `L1-ROB-001`'s fuzz harness) list their
  tests instead of appearing untested, and the coverage denominator folds
  in the Test-verifiable L1 *leaves* (composite L1s stay excluded to avoid
  double-counting their L2/L3 children). Headline moved 118/134 → 124/140
  tested, 100% verified.
- Added a Production-Readiness Audit backlog (`PRA-1`–`PRA-9`) to
  `docs/ROADMAP.md`, capturing findings from a comment/docs hygiene sweep
  and a requirements deep analysis: CLI exit-code taxonomy alignment (open
  decision), the unimplemented `L2-DEC-013` forced-format validation,
  trace-matrix L1-marker coverage, Python large-file memory limits and
  `PY-streaming`, a fuzz CI burn-in plus the `L1-SYN-002` cumulative-scan
  test, normative-doc/source-comment staleness, and lower-priority
  test/diagnostic items. Items are unscheduled — no target version is
  assigned until each is planned for closure.
- Cleared version-anchored source comments (ROADMAP PRA-7): removed the
  "v2 redesign" framing from the `src/cli.rs` / `src/filter.rs` module
  docs, and reworded the "empty in v1.0" / "csv for v1.0" anchors in
  `python/.../writer.py`, `python/.../config.py`, and `src/config.rs` to
  describe current behavior — the empty vendor columns now cite
  `L2-WRT-013` and the output-format notes read "currently only csv".
- Removed drift-prone release versions and hardcoded counts from
  `CLAUDE.md`, `README.md`, and the requirements docs (ROADMAP PRA-6).
  These now live only in their source of truth — the conformance suite
  for case counts, the requirements docs / `TRACE-MATRIX.md` for
  requirement counts, and `git tag` / `CHANGELOG.md` for versions.

## [1.4.1] — 2026-06-14

Joint Rust + Python maintenance release: close the CI dev-tool gap, tighten
the coverage gates, and clear stale comments. No public API or decode-output
changes. Both implementations ship together from the `v1.4.1` repository tag.

### Added

- A `mypy` CI job and dev dependency. `python/pyproject.toml` declared
  `[tool.mypy] strict = true` but nothing installed or ran it; strict
  type-checking is now gated in CI (`poetry run mypy src`).

### Fixed

- Latent crash in the Python filter: `apply_filters` dereferenced
  `command_word.rt` / `.subaddress` on records with no Command Word
  (SPURIOUS_DATA), raising `AttributeError` whenever such a record reached
  the filter. RT/subaddress filters now treat a missing Command Word as
  "no match" (excludable only by type or bus), matching the Rust filter.
  Surfaced by the new strict mypy gate.

### Changed

- Coverage gates ratcheted from baseline-5pp to baseline-2pp: Rust
  `cov-ci` to 84% line / 83% region (from 70/70), Python `fail_under` to
  88% combined line+branch (from 85%, now config-driven in
  `[tool.coverage.report]`).
- mypy-strict cleanups across the Python package: a shared `ByteSource`
  buffer alias for the `mmap`-backed decode/sync helpers, an explicit
  `TextIO` stream type in `dump`, removal of stale `# type: ignore`
  comments, and minor annotation fixes. No runtime behavior change.

### Removed

- Stale "Deferred (Phase 7b)" comments in `decode.rs` / `decode.py` (the
  RT-to-RT Cmd2-direction and anomaly invariants shipped as
  L2-SYN-023/024/025) and a stale SPURIOUS_DATA "raises ValueError"
  docstring (0x20 classifies as `SPURIOUS_DATA`).

## [1.4.0] — 2026-06-14

Joint Rust + Python feature release. Adds opt-in **Standard-timestamp
tick calibration**: when an operator supplies the card's free-running
counter frequency, Standard-format records are converted to microseconds
and participate in `DELTA` tracking like IRIG records. Without a rate,
behavior is unchanged (empty `DELTA`), so all existing CSV output stays
byte-identical. Both implementations ship together from the `v1.4.0`
repository tag.

### Added

- New `decode.standard_tick_rate_hz` TOML key and `--standard-tick-rate-hz`
  CLI flag (both implementations). When set to a finite value `> 0`,
  Standard timestamps convert to microseconds as
  `round(raw_ticks × 1_000_000 / rate)` (half-away-from-zero, identical
  across implementations) and join per-RT/MSG `DELTA` tracking
  (L2-DEC-017, L2-CFG-011, L2-CLI-012).
- Float value support in the Rust hand-rolled TOML parser (`TomlValue::Float`,
  `TomlDoc::get_float`), required by the new key. No new crate dependency.
- Two cross-implementation conformance cases — `standard-tick-calibrated-cli`
  and `standard-tick-calibrated-toml` — sharing one oracle to prove the CLI
  and TOML paths produce byte-identical calibrated output.

### Changed

- L2-RDR-019 generalized: Standard-format records have an empty `DELTA`
  only when no tick rate is configured; with a valid rate they participate
  in `DELTA` on the same terms as IRIG. `Timestamp::to_microseconds` /
  `Timestamp.to_microseconds` now take an optional Standard tick rate.

## [1.3.0] — 2026-06-11

Joint Rust + Python hardening release. Adds precise sync-validation
failure APIs and strict-mode diagnostics, bounded DEBUG context logging,
production Rust unwrap/expect linting, Rust LCOV artifact publishing,
and complete verification coverage for all 131 active requirements.
Both implementations ship together from the `v1.3.0` repository tag.

### Added

- Additive detailed sync-validation APIs in both implementations:
  Rust `sync::validate_record_detailed(...) -> Result<(), ValidationFailure>`
  and Python `sync.validate_record_detailed(...) -> ValidationFailure | None`.
  Existing boolean `validate_record(...)` APIs remain unchanged.
- DEBUG-level validation context diagnostics capped at 32 bytes in both
  readers.
- Rust CI now uploads `lcov.info` as the `rust-lcov` workflow artifact.

### Changed

- Strict-mode IRIG-range and look-ahead failures now name the precise
  validation reason instead of the combined "IRIG-range or look-ahead"
  fallback detail.
- Rust production crates enable Clippy's `unwrap_used` and `expect_used`
  lints outside test builds; former production unwrap/expect sites now
  return defensive errors.
- Rust CLI acceptance coverage now pins both `--inline-errors` and the
  stdout-forces-inline behavior required by L3-RS-009.

### Removed

- Python's unconditional multi-line unknown-Type-Word stderr dump. The
  bounded DEBUG context diagnostic replaces it and respects log-level
  configuration.

### Maintenance

- Close the remaining partial traceability row with an L2-CONF-002
  conformance-runner wiring inspection test. All 131 active requirements
  are now verified.

## [1.2.0] — 2026-06-08

Configurable sync look-ahead with TOML + CLI controls, a Python
TOML `[logging] level` precedence fix, the new Python coverage
gate in CI (85% combined line+branch floor mirroring the Rust
70/70 model), retirement of the static-musl SLES 12 deployment
target, retirement of the `docs/FIELDS.md` redirect stub, and
three new cross-impl conformance fixtures (L2-DEC-015 borderline,
L2-DEC-016 lenient-mode WARN, L2-SYN-026 N>2 catches what N=2
misses). Both implementations ship together at v1.2.0 from a
single repository tag (`v1.2.0`), continuing the joint-cut model
established by v1.0.0.

### Added

- **Configurable N-record sync look-ahead** (L2-SYN-005, L2-SYN-026).
  `sync::validate_record` (Rust) / `sync.validate_record` (Python) now
  accept a look-ahead depth parameter `N`. The function checks `N − 1`
  subsequent records' Type Words after the candidate, advancing by each
  record's declared `word_count`. Default `N = 2` preserves the
  historical two-record look-ahead behavior; higher values catch wider
  classes of consecutive-same-shape corruption that previously defeated
  the validator (e.g., two adjacent fake-record headers that align on
  plausible Type Words). Configurable via `decode.lookahead_records` in
  TOML or `--lookahead-records N` on the CLI, range `[1, 32]`. See
  `docs/ARCHITECTURE.md` §3 Phase 3 for the design.
- New TOML key `decode.lookahead_records` with load-time range
  validation.
- New CLI flag `--lookahead-records N` with parse-time range
  validation, exposed in both the Rust and Python CLIs.
- **Python coverage gate in CI** (`python-coverage` job). Mirrors
  the Rust `cargo cov-ci` model: runs once on Linux/Python 3.12,
  not across the full matrix (coverage isn't platform- or
  interpreter-dependent). `pytest-cov ^7.0` added as a dev
  dependency; `[tool.coverage.run]` and `[tool.coverage.report]`
  sections added to `python/pyproject.toml` (branch coverage on,
  `__main__.py` excluded as the entry shim — parallel to Rust's
  `bin/mie-decoder.rs` exclusion). Floor is **85% combined
  line+branch**, set as `--cov-fail-under=85` in the CI job.
  Baseline at integration was 88.92% across the 245-case pytest
  suite, giving ~4 percentage points of headroom before drift
  starts failing the build (same ratchet model as the Rust 70/70
  floor's ~5pp headroom against its 74.81% baseline).
  `docs/MAINTAINER-GUIDE.md` §10 rewritten to cover both Rust and
  Python coverage workflows; cheat sheet (§3) gains the
  `poetry -C python run pytest --cov --cov-fail-under=85`
  invocation alongside `cargo cov-ci`; CI architecture table (§9)
  updated from five jobs to six.
- **Conformance fixture: L2-DEC-015 borderline detection** (two
  cases). `timestamp-format-borderline-default` and
  `timestamp-format-borderline-n1` share a hand-crafted 5-record
  input where the multi-record probe genuinely changes the format
  choice cross-impl: at `--detect-records 1` both impls pick
  Standard and decode 1 row; at default `--detect-records 8` both
  impls pick IRIG (Decisive) and decode 4 rows. The two oracles
  are byte-identical across Rust and Python, pinning the cross-
  impl behavior at each N.
- **Conformance fixture: L2-DEC-016 lenient-mode WARN**
  (`timestamp-format-ambiguous-lenient`). Reuses the (now-fixed)
  ambiguous input bytes with default (lenient) mode. Asserts
  exit 0 with a header-only CSV oracle, plus a stderr substring
  assertion that pins the lenient-mode WARN's score breakdown
  (`"Ambiguous: IRIG=4 STD=4"`). Companion to the strict
  fixture; together they pin both branches of L2-DEC-016
  cross-impl.
- **Stderr substring assertions on both L2-DEC-016 conformance
  fixtures.** Strict fixture now asserts
  `expected_stderr_contains: "auto-detection is ambiguous"`
  (substring unique to the `MieTimestampFormatMismatch` error
  message); lenient asserts the WARN substring as above. These
  defend against future regressions that exit with the right code
  via the wrong code path — the same class of bug-in-test the
  strict fixture had until v1.2.0's strict-fixture fix.
- **Conformance fixture: L2-SYN-026 N-record look-ahead value
  demonstration** (two cases). `lookahead-corruption-chain-n2`
  and `lookahead-corruption-chain-n4` share a hand-crafted
  5-record input (2 valid records, 32 bytes of 0xFF garbage, 1
  valid record at the end). At `--lookahead-records 2` both
  impls accept the file start, decode record 1, then sync-
  recover through the garbage and decode record 5 — 2 rows. At
  `--lookahead-records 4` both impls reject the file start
  (look-ahead chain reaches the garbage), scan forward, and
  accept only record 5 — 1 row. The contrast (2 rows vs 1 row)
  demonstrates the L2-SYN-026 value proposition: deeper look-
  ahead catches "valid prefix followed by corruption" patterns
  that defeat the default N=2 window. Both oracles byte-
  identical cross-impl.

Total conformance case count: 21 → 27 across this release.
Python test count: 242 → 248.

### Changed

- `docs/L2-REQ.md` L2-SYN-005 generalized in place: the original
  "two-record look-ahead" wording is now described as a special
  case of the configurable N-record rule with default `N = 2`.
  The generalization is non-breaking (existing files and configs
  continue to behave identically); the rationale for the
  in-place wording update is recorded in the L2-SYN-005
  Rationale field.

### Removed

- **Static-musl SLES 12 deployment target.** The Rust crate is
  no longer published with documentation or tooling for the
  `x86_64-unknown-linux-musl` cross-compile path. Native release
  builds (`cargo build --release`) are now the only documented
  artifact; deployers targeting older glibc hosts produce the
  static binary themselves out-of-tree if needed. Concrete
  changes: `docs/L3-REQ.md` L3-RS-007 marked *Withdrawn in
  v1.2.0* (the ID is reserved, not reused, so the trace matrix
  and historical references stay coherent); `README.md`,
  `CLAUDE.md`, `CONTRIBUTING.md`, `docs/MAINTAINER-GUIDE.md`,
  `docs/USER-GUIDE.md`, `docs/L1-REQ.md` rationale, and
  `.github/workflows/ci.yml` comments updated to remove musl /
  SLES references; historical CHANGELOG and ROADMAP entries
  describing the v1.0.0 musl scope preserved as-is. Trace matrix
  regenerated (active L3 count: 26 → 25; L3-RS subtotal:
  12 → 11).
- **`docs/FIELDS.md`** — the 3-line redirect stub kept for
  legacy external-link compatibility since the L2-DEC-015 /
  Documentation Initiative absorbed its content into
  `docs/MIE-FORMAT.md`. The stub has done its job; deleted. All
  active references repointed at `docs/MIE-FORMAT.md` directly
  or removed: `docs/L1-REQ.md` (L1-OUT-001), `docs/L2-REQ.md`
  (L2-DEC-002, L2-SYN-025 rationale, error-code family
  rationale) — repointed; `docs/MAINTAINER-GUIDE.md` (repo-tree
  listing), `CLAUDE.md` (Reference docs section), `README.md`
  (repo-tree listing) — stub row dropped; `docs/MIE-FORMAT.md` —
  the "absorbs FIELDS.md" note removed entirely and replaced
  with a direct "single source of truth" statement (no
  historical breadcrumb to the deleted predecessor). ROADMAP
  historical mentions of FIELDS.md (Documentation Initiative
  recap, deferred-audit notes, etc.) preserved as-is since they
  describe past state accurately.

### Fixed

- **Python: `[logging] level` in TOML config now honored**
  (regression of L2-CFG-003 precedence: CLI > TOML > default).
  `python/src/mie_decoder/config.py` was parsing
  `[logging] level` into `DecoderConfig.log_level` and
  validating it at load time, but `python/src/mie_decoder/cli.py`
  only called `configure_logging()` once at the top of `main()`
  with the CLI value (or `"WARNING"` default) — `_run_decode`
  then loaded the TOML but never re-applied the log level. Net
  effect: a TOML `[logging] level = "INFO"` was silently ignored
  unless the user also passed `--log-level` on the CLI. Rust
  applied the TOML value correctly via `resolve_config` in
  `src/cli.rs`. Fix introduces a small
  `_apply_config_log_level(args, config_log_level)` helper called
  immediately after `load_config(...)` in both `_run_decode` and
  `_run_dump`; it re-configures the logger from the TOML value
  when `--log-level` was not passed. The CLI value (when present)
  still wins because `main()` configured with it before the
  subcommand runner was entered. Also adds `--config` to the
  `dump` subparser so `mie-decoder dump file.mie --config
  foo.toml` can honor the TOML log level too (mirrors Rust, where
  `--config` is a global flag accepted by every subcommand). New
  Python e2e regressions in `python/tests/test_e2e.py`:
  `test_cli_toml_logging_level_is_honored_when_no_cli_override`
  (catches the original bug — fails without the fix),
  `test_cli_log_level_overrides_toml_logging_level` (pins the
  CLI-wins precedence), and
  `test_cli_dump_honors_toml_logging_level` (covers the
  dump-path fix). New cross-impl conformance fixture
  `log-level-from-toml-config` reuses the `basic-multi-record`
  input + oracle plus `configs/log-level-info.toml` and asserts
  the substring `"decode exit class"` appears on stderr — an
  INFO-level message both impls emit identically.
- **Conformance fixture `timestamp-format-ambiguous-strict` was
  exercising the wrong code path.** Discovered while adding the
  lenient-mode companion fixture. The fixture's input bytes had
  a Type Word declaring `word_count = 7` (14 bytes) but the
  actual hex block contained 16 bytes per record. The mismatch
  caused `find_first_record`'s look-ahead to land on filler
  bytes mid-record and reject the candidate, producing
  `MieNoValidRecords` (exit 2) instead of the intended
  `MieTimestampFormatMismatch` (also exit 2). The strict
  fixture's `expected_exit: 2` couldn't distinguish the two
  error paths, so the test passed for the wrong reason. Fix:
  change the Type Word's `word_count` field from 7 to 8 so the
  declared length matches the actual 16-byte record. The probe
  now reaches AMBIGUOUS classification correctly, and the
  strict fixture exercises the L2-DEC-016 path it was always
  meant to.

### Maintenance

- `docs/diagrams/dataflow.puml` `find_first_record` note
  updated: "(two-record look-ahead)" → "(L2-SYN-005 /
  L2-SYN-026; N defaults to 2, configurable via
  decode.lookahead_records)". The rendered
  `docs/diagrams/dataflow.svg` was regenerated to match
  (PlantUML 1.2026.5, matching the pin in the `diagrams` CI
  job).
- `docs/ROADMAP.md` refreshed for v1.2.0: v1.1.0 release-status
  entry added; "Queued for the next release" rewritten to
  summarize the full `[Unreleased]` contents (L2-SYN-026
  configurable look-ahead, FIELDS.md retirement, Python coverage
  gate, Python TOML log-level fix, three new cross-impl
  conformance fixtures, dataflow-diagram refresh); the two
  Robustness-backlog items resolved in v1.1.0 / v1.2.0 are
  struck through with their resolution commits; "Shared
  Commitments" text updated from "two-record look-ahead" to the
  N-record wording. A mid-cycle "Deferred follow-ups" section
  introduced during v1.2.0 development was removed before the
  release cut — every item it tracked shipped within v1.2.0.

## [1.1.0] — 2026-06-07

Stronger timestamp-format auto-detection via a multi-record probe,
plus a new ambiguous-detection error class. Both implementations
ship together at v1.1.0 from a single repository tag (`v1.1.0`),
continuing the joint-cut model established by v1.0.0.

### Added

- **Multi-record timestamp-format auto-detection** (L2-DEC-015). The
  IRIG-vs-Standard probe now walks up to *N* records (default `8`,
  configurable via `decode.detect_records` in TOML or `--detect-records N`
  on the CLI, range `1..=32`) and aggregates per-record scoring across the
  probe set rather than committing on the first record alone. The chosen
  format is still resolved before the first record is decoded and is final
  for the rest of the decode per L2-DEC-011 (no per-record re-detection).
  Strengthens detection on borderline files where the first record alone
  scores ambiguously between the two formats. See
  `docs/MIE-FORMAT.md` §5.3 for the per-record scoring signals and
  confidence thresholds.
- **`MieTimestampFormatMismatchError`** / `MieError::TimestampFormatMismatch`
  (L2-DEC-016). New file-level error variant raised when the L2-DEC-015
  probe completes with an aggregate score below the confidence floor
  (`max_score < 4`) OR a margin below `MIN_MARGIN = 3`. Strict mode only:
  lenient mode (the default) logs a single WARN with the score breakdown
  and proceeds with the chosen format, preserving backwards compatibility
  with borderline files that decoded acceptably under the previous
  single-record detection. Maps to CLI exit class `2` (`no-records`),
  same class as `NoValidRecords` and `HomogeneousPayload`.
- New TOML key `decode.detect_records` with load-time range validation.
- New CLI flag `--detect-records N` with parse-time range validation.

### Changed

- Auto-detection logs now include the per-format aggregated score
  breakdown plus the L2-DEC-016 confidence classification:
  - `Decisive` and `Marginal` outcomes log at INFO with score numbers
    plus a hint to `--time-format` on Marginal calls.
  - `Ambiguous` outcomes log at WARN (lenient) or ERROR + raise (strict).
- Conformance manifest schema validation in `tests/conformance/run.py` now
  checks field types in addition to field names. Rejects wrong scalar types
  (e.g. `"config": 12345`), wrong container types (e.g. `"rust_args": "a
  string"`), wrong list-element types (e.g. `"rust_args": [42]`), and invalid
  enum values (e.g. `"mode": "banana"`) with actionable error messages that
  name the offending field and the expected type.

### Fixed

- `tests/cli.rs::decode_emits_exit_class_summary_at_info_level` now surfaces
  in `docs/TRACE-MATRIX.md`. The L1 section of the matrix displays only L2
  children and rolled-up status, not direct L1 test markers, so the test's
  `L1-EXIT-005` tag alone was invisible. Added `L2-CLI-006` (stderr-only
  diagnostic obligation) to the `/// Requirements:` line — semantically
  correct (the summary line IS a human-readable stderr diagnostic) and the
  test now shows up under that L2's row.

### Maintenance

- `docs/MAINTAINER-GUIDE.md` §10 "220+ tests" updated to the actual count
  (236 as of v1.0.0).
- Spec additions: `L2-DEC-015` (multi-record probe) and `L2-DEC-016`
  (ambiguous-mismatch error class), both children of `L1-DEC-002`. See
  `docs/L2-REQ.md`.
- New conformance case `timestamp-format-ambiguous-strict` pins the
  cross-impl behavior on strict-mode ambiguous input: both Rust and
  Python raise their respective mismatch errors and exit `2` byte-for-byte
  equivalently. Conformance case count: 20 → 21.

## [1.0.0] — 2026-06-07

First joint release of the Rust crate and the Python package.
Both implementations ship from the same commit at v1.0.0.

### Highlights

- **Two implementations, one binary contract.** Rust crate at the
  repository root; Python package under `python/`. A 20-case
  conformance suite (`tests/conformance/`) holds the two to byte-exact
  CSV output (and matching exit code on negative cases).
- **DDC-vendor-compatible CSV.** Column names and ordering match DDC's
  own recording software output by spec (`L1-OUT-001`). See
  `docs/VENDOR-CSV-DIFFS.md` for the alignment statement and the five
  vendor-empty columns preserved as placeholders.
- **Streaming Rust writer (constant memory).** The Rust crate streams
  rows directly to a `BufWriter` — `O(1)` per record. Python remains
  pandas-buffered (`O(record_count)` memory) per `L3-PY-012`; a future
  Python-streaming feature is on the roadmap.
- **Static-musl Rust binary for SLES 12 deployment** via
  `x86_64-unknown-linux-musl`. Single self-contained binary, no glibc
  dependency.
- **Single external Rust dependency** (`memmap2`). Argument parsing,
  CSV writing, TOML loading, logging, and error types are all
  hand-rolled — see `CLAUDE.md` "Conventions worth preserving".

### Added — CLI

- `decode`, `count`, `dump` subcommands (Rust); `decode` with
  `--count` / `--dump` flags (Python). CLI shapes intentionally
  differ between impls per `L1-CLI-001` (capability parity, not
  exact spelling). See `docs/USER-GUIDE.md`.
- **Two-channel `count` output** (`L3-RS-008` / `L3-PY-010`): only
  the integer record count goes to stdout (pipeline-friendly:
  `n=$(mie-decoder count rec.mie)`); a human-readable
  `counted <N> messages in <basename>` status line goes to stderr.
- **Output safety subsystem** (`L1-OUT-002`):
  - `--no-clobber` refuses to overwrite an existing output
    (`L2-WRT-014`, `MieClobberRefusedError` / exit 1).
  - Input-equals-output rejection (`L2-WRT-016`,
    `MieInputOutputCollisionError` / exit 1).
  - Atomic write via `<stem>.<pid>.tmp` + `rename` (`L2-WRT-017`).
  - `--allow-partial` commits a partial decode to
    `<stem>.partial.csv` on unrecoverable sync loss (`L2-WRT-015`,
    exit 0); without it the partial is unlinked and the run exits 3.
- **Include filters** (`--include-types` / `--include-rts` /
  `--include-buses` / `--include-subaddresses`) as a Rust-only
  axis (`L3-RS-010`); exclude filters parity across both impls.
- **Exit-class taxonomy** (`L1-EXIT-005`): every decode emits a
  `decode exit class: <class>` INFO summary line and exits 0 / 1 /
  2 / 3 per `L1-EXIT-002`..`L1-EXIT-004`.
- **Inline error output mode** (`L2-ERR-011`): `--inline-errors`
  (Rust) / `--error-mode inline` (Python) keeps errored records
  in the main CSV with `ERROR` and `ERROR_CODE` columns populated
  rather than splitting them to `<stem>_errors.csv`.
- Hand-rolled **TOML config loader** with documented precedence
  (CLI flags > config file > built-in defaults); unknown-key
  warnings (`L2-CFG-009`); load-time validation (`L2-CFG-010`).
  See `docs/CONFIG-REFERENCE.md`.

### Added — decode pipeline

- **Four-phase sync strategy** (`docs/ARCHITECTURE.md` §3): header
  detection with diagnostic-rich failure (`diagnose_header_scan_failure`
  distinguishes `NoValidRecords` / `FirstRecordTruncated` /
  `HomogeneousPayload`), continuous per-record validation, two-record
  look-ahead confirmation, recovery scan with `MAX_SCAN_BYTES = 64 KB`.
- **Homogeneity-payload defense** (`L2-SYN-018`): rejects pathological
  inputs (all-zero, all-`0xFFFF`, etc.) that would otherwise pass shape
  validation. Maps to exit class 2.
- **First-record-truncated detection** (`L2-RDR-004`,
  `MieFirstRecordTruncatedError`): distinguishes a truncated initial
  record from a generic no-records error.
- **Structural invariants subsystem** (`L2-SYN-020`..`L2-SYN-025`):
  six rules check decoded records against MIL-STD-1553 transaction
  shape. Severity::Reject raises `MieRecordError`; Severity::AnomalyWarn
  logs and keeps the record. Errored records skip invariant checks
  (truncated payload by definition).
- **DELTA tracker** (`L2-RDR-016`..`L2-RDR-019`): per-RT/MSG key
  monotonicity tracking with non-monotonic-timestamp warnings.
- **Error pipeline**: DDC `0x01xx` hardware codes preserved verbatim;
  decoder-internal `0x20xx` codes (`0x2000` SPURIOUS_DATA continuation,
  `0x2001` standalone) assigned by classifying SPURIOUS_DATA records
  against the preceding record state. See `docs/ERROR-CATALOG.md`.

### Added — requirements traceability

- **L1 / L2 / L3 requirements docs** (`docs/L1-REQ.md`,
  `docs/L2-REQ.md`, `docs/L3-REQ.md`): 24 system requirements + 102
  architectural derivations + 26 implementation obligations,
  cross-linked by parent IDs and verification methods.
- **Auto-generated trace matrix** (`docs/TRACE-MATRIX.md`) produced by
  `scripts/build-trace-matrix.py`, gated in CI on every push.
- **Per-test requirement tagging** via `/// Requirements:` doc
  comments (Rust) and `@pytest.mark.requirement` markers (Python).

### Added — tooling

- **Cross-platform CI matrix** (`.github/workflows/ci.yml`): Rust on
  `ubuntu-latest` + `windows-latest`; Python on `ubuntu-latest`
  (3.10..3.14) + `windows-latest` (3.12, 3.14); conformance on both
  platforms; trace-matrix `--check`; PlantUML diagram drift gate.
- **`cargo-llvm-cov` coverage gate** at 70% line + region (Rust,
  Linux-only).
- **Pre-commit hook** (`.githooks/pre-commit`, installed via
  `bash scripts/install-hooks.sh`) mirroring the CI gates locally:
  whitespace/CRLF/merge-marker scans, file size cap, Cargo.lock
  parity, trace-matrix `--check`, `cargo fmt --check`, clippy,
  `cargo test --all-targets`, `dbg!()` scan, `// SAFETY:` comment
  requirement.
- **Conformance manifest schema enforcement**: typos in case fields
  (e.g. `rust_arg` for `rust_args`) fail fast with an actionable
  error and the list of allowed fields.

### Documentation

- `docs/USER-GUIDE.md` — end-to-end CLI walkthrough.
- `docs/EXAMPLES.md` — 11 runnable operator-task recipes.
- `docs/MIE-FORMAT.md` — comprehensive binary format reference with
  three worked hex-to-CSV decodes.
- `docs/ARCHITECTURE.md` (v2.0) — dual-implementation architecture
  with Rust↔Python module correspondence.
- `docs/CONFIG-REFERENCE.md` — normative TOML key reference.
- `docs/ERROR-CATALOG.md` — operator-facing error and exit-code
  reference.
- `docs/MAINTAINER-GUIDE.md` — repo layout, daily commands,
  workflows, CI architecture, release process.
- `docs/VENDOR-CSV-DIFFS.md` — alignment statement vs DDC vendor CSV.
- `docs/diagrams/{class,component,dataflow}.{puml,svg}` —
  dual-implementation PlantUML diagrams with committed rendered SVGs.

### Notes

- The Python package previously shipped at `1.1.0` from a pre-Rust-port
  lineage. As of this joint cut, the version aligns at `1.0.0`. This is
  a one-time downward alignment; future Python releases will increment
  forward from `1.0.0` per the impl-prefixed tagging scheme.
- The CHANGELOG starts here. Earlier history exists in `git log` but is
  not retroactively documented as separate entries.

[Unreleased]: https://github.com/joey-huckabee/aero1553/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/joey-huckabee/aero1553/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.18.0...v3.0.0
[2.18.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.17.0...v2.18.0
[2.17.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.16.0...v2.17.0
[2.16.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.15.1...v2.16.0
[2.15.1]: https://github.com/joey-huckabee/mie-decoder/compare/v2.15.0...v2.15.1
[2.15.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.14.0...v2.15.0
[2.14.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.13.0...v2.14.0
[2.13.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.12.0...v2.13.0
[2.12.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.11.1...v2.12.0
[2.11.1]: https://github.com/joey-huckabee/mie-decoder/compare/v2.11.0...v2.11.1
[2.11.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.10.0...v2.11.0
[2.10.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.8.0...v2.9.0
[2.8.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.7.1...v2.8.0
[2.7.1]: https://github.com/joey-huckabee/mie-decoder/compare/v2.7.0...v2.7.1
[2.7.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.6.2...v2.7.0
[2.6.2]: https://github.com/joey-huckabee/mie-decoder/compare/v2.6.1...v2.6.2
[2.6.1]: https://github.com/joey-huckabee/mie-decoder/compare/v2.6.0...v2.6.1
[2.6.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.5.3...v2.6.0
[2.5.3]: https://github.com/joey-huckabee/mie-decoder/compare/v2.5.2...v2.5.3
[2.5.2]: https://github.com/joey-huckabee/mie-decoder/compare/v2.5.1...v2.5.2
[2.5.1]: https://github.com/joey-huckabee/mie-decoder/compare/v2.5.0...v2.5.1
[2.5.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.4.0...v2.5.0
[2.4.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.3.0...v2.4.0
[2.3.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/joey-huckabee/mie-decoder/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/joey-huckabee/mie-decoder/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/joey-huckabee/mie-decoder/compare/v1.5.1...v2.0.0
[1.5.1]: https://github.com/joey-huckabee/mie-decoder/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/joey-huckabee/mie-decoder/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/joey-huckabee/mie-decoder/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/joey-huckabee/mie-decoder/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/joey-huckabee/mie-decoder/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/joey-huckabee/mie-decoder/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/joey-huckabee/mie-decoder/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/joey-huckabee/mie-decoder/releases/tag/v1.0.0
