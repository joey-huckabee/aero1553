# Aero1553

Rust and Python libraries and CLIs that decode the proprietary binary recordings made by Data
Device Corporation (DDC) MIL-STD-1553 PCI cards. The CSV output is column-compatible with DDC's
own recording software, so a decoded file can be diffed against vendor output for validation.

## Layout

```
rust/                 the decoder: one crate, no external dependencies
  src/                the pipeline (see Architecture) and the CLI
  tests/              integration.rs (library API), cli.rs (spawns the built binary)
  benches/, examples/ the Rust half of the performance suite; synthetic-recording tools
python/               the Python package: a binding over the Rust crate
  native/             the PyO3 extension (aero1553._native) that wraps rust/src
  src/aero1553/       the public Python API, its docs and Python-specific types
  tests/              pytest, including the executable docs/PYTHON-GUIDE.md
tests/conformance/    shared hexadecimal fixtures, byte-exact CSV oracles, and the
                      differential checks run against both CLIs
tests/golden/         generated golden recordings and their pinned outputs
perf/                 the performance suite, baseline and profiling tools (perf/README.md)
scripts/              repo hygiene, trace matrix, ASCII-output check, hook installer
config/default.toml   the fully commented reference configuration
docs/                 user, maintainer and requirements documentation (see Reference docs)
.githooks/pre-commit  local checks; install once per clone with scripts/install-hooks.sh
```

## The two implementations

**The implementations ship together as a joint cut from one repository tag.** Future releases may
diverge through impl-prefixed tags (`rust-vX.Y.Z`, `python-vX.Y.Z`); none has been needed, and
`scripts/repo-hygiene.sh` fails the build while the two declare different versions. See
`CHANGELOG.md` for the history and `git tag` for the current version.

**The Rust crate is a clean rewrite, not a transliteration.** Its CLI was redesigned, its writer
streams (constant memory), and its data-words container is an inline `[u16; 32]` buffer.
Maintain each implementation according to its own architecture while keeping shared format and
CSV behavior aligned.

**Since v4.0.0 the Python package is a binding over the Rust crate, not a second decoder.**
`python/native/` is a PyO3 extension (`aero1553._native`) that wraps `rust/src`; the modules
under `python/src/aero1553/` are the public Python API, its docs and its Python-specific types
(dataclass configs, the exception hierarchy, `columns()` / `to_dict()`). Reading, sync,
decoding, filtering, ordering, merging, `DELTA`, the TOML loader, CSV writing and the whole CLI
(`cli.py` calls `_native.run_cli`) run the Rust code. A decode or CSV change is therefore a Rust
change that Python picks up through the extension. "Both implementations" means one decoder
(Rust) plus Python's API surface over it, and the conformance suite still runs both CLIs.

**The C++ implementation under `cpp/` was removed in 5.0.0.** It existed to make **SLES 12 SP5**
a first-class deployment target; that target is now served by the Rust binary built against
glibc 2.22 and kernel 4.12 (`L3-RS-018`) with a pinned cargo-libc (since 5.9.0; cargo-zigbuild
before). It needs glibc and GCC's `libgcc_s.so.1`. Before changing how it is built, compare the
old and new binaries with `scripts/compare-binaries.py`. Comments and requirement rationales that mention C++ record history;
ADR-0001 to ADR-0003 in `docs/adr/` are superseded.

**Edition 2024, MSRV 1.88.** The floor is set by the crate's own use of **let-chains**
(stabilized 1.88); edition 2024 alone floors at 1.85, and `is_multiple_of` independently needs
1.87. `scripts/repo-hygiene.sh` checks that every file declaring the floor agrees with
`rust/Cargo.toml`, this one included.

## Commands

```bash
# Build (Rust commands run from the rust/ crate directory)
cd rust
cargo build               # Dev build
cargo build --release     # Optimized

# Test
cargo test                                                     # All tests
cargo test --lib                                               # Unit tests only
cargo test --test integration                                  # Integration only (library API)
cargo test --test cli                                          # CLI acceptance only (spawns built binary)
cargo test --test cli -- --nocapture                           # CLI suite + show subprocess stderr
cargo test --test integration -- multi_record_stream           # Single integration test
cargo test config::tests::parses_default_toml_from_disk        # Single unit test

# Lint
cargo clippy --all-targets -- -D warnings
RUSTDOCFLAGS="-D warnings" cargo doc --no-deps    # doc-link check (CI-gated)
cargo +1.88 check --all-targets                   # MSRV 1.88 floor (CI-gated)
cargo deny check                                  # supply-chain audit: advisories/licenses (CI-gated)
cargo semver-checks check-release --baseline-rev origin/main --release-type minor  # public-API break check (CI-gated per PR; label semver-break-accepted for a deliberate break)

# Run the CLI
cargo run --release -- decode path/to/recording.mie -o decoded.csv
cargo run --release -- decode - < path/to/recording.mie        # standard input (L2-CLI-023)
cargo run --release -- count path/to/recording.mie
cargo run --release -- dump path/to/recording.mie --records 10

# Python setup, test, and CLI (run from the repo root)
cd ..
uv --directory python sync
uv --directory python run pytest
uv --directory python run mypy src    # strict type check (CI-gated)
uv --directory python run pylint src/aero1553       # lint (CI-gated, must stay 10/10)
uv --directory python run ruff check                # ruff lint (CI-gated)
uv --directory python run ruff format               # auto-format (CI runs ruff format --check)
# Repo Python outside the package (tests/, scripts/, perf/), same gates. The
# config must be passed: no pyproject sits above those files, and its `../`
# per-file-ignore globs resolve against the cwd -- uv --directory python sets it.
uv --directory python run ruff check --config pyproject.toml ../tests ../scripts ../perf
uv --directory python run ruff format --config pyproject.toml ../tests ../scripts ../perf
uv --directory python run vulture                   # dead-code scan (CI-gated)
uv --directory python run bandit -r src/aero1553    # security scan / SAST (CI-gated)
uv --directory python run aero1553 --help
uv --directory python build   # sdist + one abi3 wheel, via maturin

# Shared cross-implementation behavior. The runner defaults to EVERY registered
# implementation and fails if one is missing, so opting out is explicit --
# a run that silently tested fewer could report a full pass after a build failed.
(cd rust && cargo build)
uv --directory python run python ../tests/conformance/run.py
uv --directory python run python ../tests/conformance/run.py --only rust  # Rust vs the oracles
uv --directory python run python ../tests/conformance/selftest.py         # each check must catch a broken CLI

# Performance suite (perf/README.md): Rust + Python on the golden recording
# (tests/golden), outputs checked against the pins, gated on same-run ratios.
uv --directory python run python ../perf/run.py --repeat 5
uv --directory python run python ../perf/run.py --small --repeat 2   # smoke run
uv --directory python run python ../perf/baseline.py \
    --base BASE/aero1553 --cand CAND/aero1553 --synth CAND/synth_large  # two builds, 256 MiB inputs
python3 perf/instructions.py  # instructions per record (Linux, valgrind)

# Repository checks (also run by the pre-commit hook and CI)
bash scripts/repo-hygiene.sh
python3 scripts/build-trace-matrix.py   # regenerate docs/TRACE-MATRIX.md

# Fuzz harnesses (L1-ROB-001). Both read the SAME three knobs:
#   MIE_FUZZ_ITERATIONS (default 256) / MIE_FUZZ_STREAM_LOGS / MIE_FUZZ_SUMMARY
# Point them all at one MIE_FUZZ_SUMMARY file and compare the FUZZ-SUMMARY
# lines -- on identical inputs the counters must be identical.
MIE_FUZZ_ITERATIONS=25000 cargo test --test integration fuzz_arbitrary_bytes_never_panic
MIE_FUZZ_ITERATIONS=25000 uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness -s
python scripts/compare-fuzz-summaries.py <dir-of-summary-files>
```

## Architecture

**The decoder is a unidirectional pipeline.** It is best understood by tracing one record from
disk to CSV:

1. **`source.rs` -- `Window`**: the input. A fixed 256 KiB buffer refilled by positional reads
   (`pread` / `seek_read`), one per iterator, the file handle shared via `Arc`. Standard input
   (`decode -`, L2-CLI-023) goes through the same window, filled forwards only, since a pipe
   cannot be read at an offset; it decodes byte-identically to the file.
2. **`reader.rs` -- `MieFileReader`**: the top-level streaming iterator. Makes the window reach as
   far as a sync helper can look (`STREAM_REACH`) before start-up and every resync, one record
   before each locked record, so the helpers see what they would see in the whole file. Calls
   `find_first_record()` for header skip, then loops: `validate_record()` -> decode -> yield
   `Result<MieMessage>`. On validation failure it calls `recover_sync()` to walk forward in
   2-byte steps. Owns the `prev_was_error` flag used to classify SPURIOUS_DATA continuations,
   and warns about data after the terminator (L2-RDR-022).
3. **`sync.rs`**: pure validation helpers (`find_first_record`, `validate_record`,
   `recover_sync`). Validation uses a **configurable N-record look-ahead** (default 2,
   L2-SYN-026): a candidate is only confirmed valid if the next `N-1` records' Type Words also
   look valid. A single Type Word match alone produces too many false positives. No logging in
   this module; the reader emits any messages.
4. **`decode.rs`**: pure binary -> struct conversion. Type Word bit layout, IRIG vs Standard
   timestamp formats (auto-detected by probing the Command Word at both candidate offsets and
   scoring), Command Word, message format classification.
5. **`models.rs`**: plain structs (`MieMessage`, `TypeWord`, `CommandWord`, `IrigTimestamp`,
   `StandardTimestamp`), `IntEnum`-style enums with explicit `#[repr(u8)]` discriminants, DDC
   error code constants (0x01xx) and decoder-assigned spurious codes (0x20xx). `DataWords` is
   the fixed-capacity inline buffer that replaces `Vec<u16>` for the per-record payload.
6. **`filter.rs` -- `FilterIterExt::filter_messages`**: iterator adapter. Both `exclude_*` and
   `include_*` filters are supported. A spurious continuation is judged through its parent for
   RT, subaddress and bus filters, so the pair stays together (see `docs/DATAFLOW.md`).
7. **`order.rs` -- `OrderIterExt::order_rows`** (exposed to Python by
   `python/src/aero1553/order.py`): canonical row order (L1-OUT-003 / L2-WRT-021), the **last**
   iterator stage before the writer on both the single-file and merge paths. Buffers one run of
   *consecutive* equal-`TIME_STAMP` records and stable-sorts it by `(RT, subaddress, direction)`;
   `Direction::Receive = 0` / `Transmit = 1` gives R-before-T for free. Records with no Command
   Word (`SPURIOUS_DATA`) are **pinned**: excluded from the sort and carried along by whichever
   record preceded them, which keeps an errored record adjacent to its `0x2000` continuation.
   Buffering is capped by `max_sort_group` (L2-WRT-022, default 65536); each time the cap is
   reached the buffered records are flushed in arrival order with one WARN and the rest of the
   run is gathered afresh. A cap of 1 is the silent "off" switch.
8. **`writer.rs`**: `write_csv` (single file) and `write_csv_split` (separate `_errors.csv`).
   Streams rows through a `BufWriter`, with no DataFrame buffering. Column names and ordering
   match DDC vendor CSV byte-for-byte. Rows are rendered to bytes on worker threads by the
   private `parallel.rs` (`[output] threads` / `--threads`, default `min(cores - 1, 2)`,
   L2-WRT-027): batches of 2048 records are dealt round-robin and collected back in the order
   dealt, so the calling thread writes them in serial order; `std::thread::scope` + `mpsc`, no
   dependency. The public `write_csv` / `write_csv_split` / `WriteOptions` are unchanged
   (library callers get `default_threads()`); the CLI passes a count through the crate-internal
   `*_threaded` variants, and a caller with its own destination -- the Python binding's
   text-stream output -- uses the public `CsvWriter::write_messages(messages, threads)`.
9. **`config.rs`**: hand-rolled TOML loader for our flat schema: sections and `key = value` with
   strings, booleans, single-line arrays, and numbers in TOML 1.0's grammar (separators and
   `0x` / `0o` / `0b` since 5.9.0, L2-CFG-014). Produces `DecoderConfig`. Precedence: **CLI
   overrides > config file > defaults**, applied via `DecoderConfig::with_overrides(ConfigOverrides)`.
10. **`cli.rs` -- `run(argv)`**: hand-rolled argument parsing with three subcommands (`decode`,
    `count`, `dump`). `count` is its own subcommand since v2 (it was a `--count` flag in v1).
    The default error mode is `inline` (every record in one CSV with `ERROR` / `ERROR_CODE`
    populated); `--separate-errors` toggles the split-file mode. The former `--inline-errors`
    flag was removed when the polarity was reversed, so passing it is a usage error.
11. **`log.rs`**: tiny stderr logger. A process-wide `AtomicU8` level (the library's; the Python
    binding sets it from its `aero1553.*` loggers) + `log_debug!` / `log_info!` / `log_warn!` /
    `log_error!` macros that format only when the level passes. A CLI run holds a level and
    advisory switch of its own for its thread (`with_cli_scope`, L2-LOG-003), so `--log-level`
    never leaks into a library host's open iterators. Don't make the CLI write the
    process-wide level.
12. **`merge.rs` -- `MergedRecordIter`** (exposed to Python by `python/src/aero1553/merge.py`):
    multi-file time-sorted k-way merge (L1-MRG / L2-MRG). When `decode` resolves more than one
    input (positionals / `--manifest` / `--glob`, mutually exclusive, capped at
    `MAX_MERGE_FILES = 256`), this holds one record per open reader in a min-heap
    (`BinaryHeap` + `Reverse`), ordered by absolute IRIG microseconds with a
    `(us, file_index, seq)` tiebreak: O(files) memory, O(1) in records. That tiebreak is the
    **heap's internal** order only; the equal-timestamp order the CSV shows is `order.rs`'s
    canonical RT/MSG order (a heap key can't do it: the heap never holds two same-timestamp
    records from one input at once). It validates every input is calendar-locked IRIG up front
    (Standard / freerun / mixed -> `IncompatibleMergeInputs`, exit 6) and applies the L2-MRG-005
    DELTA scope: `per-file` (the default) leaves each reader's own DELTA in place, which is what
    makes a merged record's value identical to a single-file decode, while `global`
    (`--delta-scope global`) recomputes on the merged timeline. Optional cross-recorder
    de-duplication is `--collapse-duplicates` (L2-MRG-007 / 008). A single input bypasses this
    module entirely. The `*` / `?` glob matcher is hand-rolled; it matches over scalar values,
    so `?` consumes a whole character, never one byte of a non-ASCII filename. The matcher is
    only half of `--glob`: L2-MRG-001 also fixes what it is applied *to* -- entries that resolve
    to a **regular file**, symlinks followed, directories never -- and how the pattern is
    **split**, using the platform's separator set (`\` only on Windows). The three
    implementations of the time had matchers that agreed and expansions that did not.
13. **`delta.rs` -- `DeltaTracker`** (exposed to Python by `python/src/aero1553/delta.py`):
    per-RT/MSG `DELTA` state, and **the one definition of the key** -- `(rt, subaddress,
    direction)` packed into a `u32`. Used by BOTH `reader.rs` (per-file scope) and `merge.rs`
    (`--delta-scope global`). Before the extraction each kept its own tracker keyed differently
    and nothing asserted the two agreed on what "the same RT/MSG" meant (L3-RDR-001). It is
    **pure**: `observe()` returns a `DeltaOutcome` and the caller narrates, because a tracker
    cannot know whether a backward step deserves a WARN (single-file: yes, once per key) or is
    already reported at file granularity (merge, L2-MRG-006).
14. **`text.rs`**: the one rule for printing text the program did not write (L2-CLI-025); see
    Conventions. **`dump.rs`** is the `dump` subcommand, reading through the same `Window`.
    **`error.rs`** is `MieError` (below).

### Error handling model (important and non-obvious)

**An error record is a valid record, not sync loss.** When the DDC card detects a bus error, it
sets **bit 14 of the Type Word**, truncates the payload, and appends a 16-bit Error Word
containing the code. If words remain from the original transaction, the card writes them as a
separate `SPURIOUS_DATA` (type `0x20`) record immediately after.

The reader tracks `prev_was_error` across records so it can classify a following
`SPURIOUS_DATA` as either `0x2000` (continuation of a preceding error) or `0x2001`
(standalone). These `0x20xx` codes are decoder-assigned, not DDC hardware codes; see
`models.rs` for the full code table.

Error records and SPURIOUS_DATA continuations pass sync validation normally. Sync loss only
happens on truly corrupt data (truncated mid-word, power loss). Don't conflate "errored record"
with "sync loss."

### Output modes

- **Default (`error_mode = inline`)**: every record goes to the one CSV, with the `ERROR` /
  `ERROR_CODE` columns populated on errored and spurious rows. Calls `write_csv`. This is the
  mode a vendor-CSV diff uses, since the vendor tool also emits a single file.
- **`--separate-errors` (`error_mode = separate`)**: clean messages -> main CSV, errored +
  spurious -> `<stem>_errors<suffix>` (lazy: the file isn't created if there are no error
  rows). Calls `write_csv_split`. Ignored on stdout (you can't split stdout), with a WARN.

The polarity was reversed in v2.8.0: `separate` was the old default.

### Error type

**All fallible APIs return `Result<T, MieError>`.** `MieError` is a single enum (not a
hierarchy). `kind()` returns a `MieErrorKind` discriminant. `is_record_error()` matches Python's
`MieRecordError` exactly; `is_file_error()` is deliberately narrower than `MieFileError` (input
I/O only -- the whole-file rejections and destination guards answer `false` to both
predicates). Both implementations pin the full classification by test, so a new variant can't
be added without classifying it on both sides.

## Conventions worth preserving

- **No external dependencies.** The crate has none (`memmap2` was dropped in 5.0.0). Adding a
  crate requires justification: argument parsing, CSV, TOML, logging, error types and file
  input are all hand-rolled by design, and the maintainer values keeping it that way. Build
  tools that run only in CI (cargo-libc) are not crate dependencies.
- **The input is streamed, never mapped or read whole.** `source.rs`'s `Window` bounds memory
  per open input to the window, independent of file size; `dump` reads through the same module.
  Before calling a sync helper, make the window reach as far as that helper can look: the
  helpers take `&[u8]` and cannot tell a window from the whole file, so a short window would
  silently change a decode result.
- **Streaming CSV.** Rows must flow through a `Write` impl as they are produced. Do not
  introduce `Vec<MieMessage>` or `Vec<Row>` buffering in the writer; constant memory is the
  design point.
- **N-record look-ahead in `sync.rs`** (default 2, configurable per L2-SYN-026). Don't remove
  it. Removing the look-ahead reintroduces false-positive resyncs.
- **`DataWords` is fixed-capacity by design.** MIL-STD-1553B caps a single transaction at 32
  data words. Don't switch to `Vec<u16>` "for flexibility."
- **The canonical-order stage is run-scoped and capped.** `order.rs` / `order.py` permute only
  a run of *consecutive* equal-`TIME_STAMP` records, and only up to `max_sort_group` of them.
  Don't turn it into a whole-file sort (that breaks the constant-memory design point and
  L2-MRG-006's "never re-sort" rule) and don't remove the cap (a corrupt file whose timestamps
  all decode alike would buffer everything). Don't "simplify" the pinning of
  Command-Word-less records into a sentinel sort key either: a pin's position is defined
  relative to its *predecessor*, so it has to travel with that record. Preserving its index is
  not the same thing, and getting this wrong silently breaks the `0x2000` continuation
  semantics.
- **Two stages hold data-sized buffers, and both are capped.** The reorder stage above
  (`max_sort_group`, L2-WRT-022) and the merge's de-duplication window
  (`max_collapse_survivors`, L2-MRG-008) are the only pipeline stages whose memory is a function
  of the *data* rather than of the file count. Their defaults differ on purpose -- `65536` and
  `4096` -- because the reorder cap costs memory while the dedup cap costs time (every record is
  compared against every survivor; raising it to `65536` made a broken-clock merge 13x slower).
  Don't "re-pair" them. `collapse_window_us` bounds the survivor set in **time** and cannot
  bound it in **count**: a window admits however many records fall inside it. Both stages
  degrade (one WARN, best-effort, never drop an output row) rather than fail. Also: state the
  dedup window's retention as a predicate over the retained **set**
  (`|survivor_us - current_us| <= window`), never as a loop over one end of the deque. After a
  lenient backward step the front can hold a *future* timestamp, and a one-sided test then
  never evicts it and blocks everything behind it. That bug was quadratic and retained 100% of
  records.
- **Rendering threads stay stateless** (`L2-WRT-027`, `parallel.rs`). Workers only turn a
  record into bytes. Everything stateful -- pulling from the iterator (and with it reading,
  ordering, the merge and `DELTA`), every log message, every write -- stays on the calling
  thread. Output must be **byte-identical at every thread count**, `0` included; an error
  surfaces only after every preceding row is written, and nothing is pulled after the iterator
  yields one. Keep the in-flight bound (`workers * 2` batches): an unbounded channel would
  break constant memory.
- **The commit is where `--no-clobber` is enforced, not the pre-flight** (`L2-WRT-023`). An
  `exists()` test before the output is opened answers a question about the past; between the
  answer and the rename another process can create the destination, and a replacing rename then
  destroys it. Every commit target -- destination, errors file, and the `.partial` of each --
  goes through an atomic **non-replacing** move under `--no-clobber` (`MoveFileExW` with no
  flags on Win32; `link(2)` plus an exclusive-create fallback everywhere else). Keep the
  pre-flight as an early, friendlier report; don't mistake it for the guarantee. The pre-flight
  checks every path the options *could* commit (`commit_targets`: destination, errors file with
  `--separate-errors`, and the `.partial` of each with `--allow-partial`) without predicting
  which this run will write. Without `--no-clobber`, never delete or warn about a stale file
  this run doesn't write.
- **`commit` and `commit_partial` share one move routine** (`L3-WRT-005`). When each spelled the
  flush/close/move sequence out for itself, the two drifted in exactly the ways that shape
  predicts: a rule applied at one target and not the other, and error handling that wrapped the
  rename but not the flush. The final flush is part of the commit for classification purposes
  (`L2-WRT-024`): it is where a disk-full error actually lands.
- **Main output commits before errors output on EVERY path** (`L2-WRT-019`), the
  `--allow-partial` path included. Every implementation once got the normal path right and the
  `.partial` path backwards, which left an orphan `_errors.csv.partial` beside no main output
  whenever the main rename failed. State rules like this over "every commit", not over "the
  commit".
- **CSV column names and order are dictated by DDC vendor output -- for the first 44
  columns.** Don't "clean up" `MUX`, `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`: they're
  columns by spec (`L2-WRT-013`). `TERM_NAME` / `IM_GAP` / `RCV_GAP` / `XMT_GAP` stay empty;
  `MUX` is populated from the input file name by default (`L2-WRT-020`), replaced by a fixed
  `--mux-value` (`L2-WRT-028`), and restored to empty (vendor-exact) by `--no-mux` /
  `[mux] enabled = false`.
- **Decoder-added columns go at the TAIL, never inside the vendor block** (`L1-OUT-001`,
  `L2-WRT-001`). The vendor layout is columns 1-44, `TIME_STAMP` through `XMT_GAP`; `ERROR` and
  `ERROR_CODE` (45-46) are decoder features with no vendor counterpart. Through v2.9.0 they sat
  between `DELTA` and `IM_GAP`, which silently shifted the three gap columns two positions off
  their vendor indices and made every positional comparison past `DELTA` wrong while all the
  column *names* still matched. If you add a column, append it; and prefer resolving columns by
  header name over index in tests (`writer.rs`'s
  `vendor_block_precedes_decoder_added_columns` and its Python mirror pin the boundary).
- **The sync module is pure** (no logging, no I/O). The reader handles all user-facing
  messaging based on the values it returns. Don't move logging into validation helpers: they
  lack the caller's context, so they narrate outcomes wrongly (`find_first_record` returning
  `None` is the *expected* result for a valid empty recording, and logging "no valid record
  found" there contradicted the reader's own correct message).
- **Shared conformance fixtures are byte-exact.** Treat `tests/conformance/` as the
  cross-implementation oracle; update expected CSV only after both implementations agree. When
  both CLIs share the code under test, their agreement proves nothing about it: use an
  independent referee (as `python/tests/test_config_numbers.py` uses Python's `tomllib`), and
  prove each check can fail (`tests/conformance/selftest.py`).
- **Both implementations are maintained.** Keep Rust-specific design decisions in the Rust crate
  and Python-specific ones in `python/`; align shared format semantics and vendor-compatible CSV
  behavior.
- **Everything written to stdout or stderr is ASCII, in both implementations**
  (`L2-CLI-014`). Not just payload: log messages, usage errors and help text too. A Windows
  console runs at the OEM code page (437 in a US install), not UTF-8, so a single em dash reaches
  the operator as `ΓÇö`; mojibake in a decoder's own diagnostics gets reported as memory
  corruption. Use `--`, `->`, `section`. Text the program did not write -- a path, an argument,
  a config value, an OS message -- follows `L2-CLI-025`: print a path with
  `crate::text::show(&p)`, never `p.display()`, and anything else through
  `text::escape_text`. The rule once exempted stderr prose, and that carve-out is exactly how 46
  sites accumulated across the source trees. Never fix a rendering problem with
  `SetConsoleOutputCP` or by reconfiguring a stream's encoding: that is per-platform, leaves
  redirected output wrong, and adds OS surface for no gain. `scripts/assert-ascii-output.py`
  enforces it (wired into `scripts/repo-hygiene.sh` and the pre-commit hook). It parses string
  literals rather than grepping bytes, because an escape such as `"\xE2\x80\x94"` is ASCII on
  disk and non-ASCII on the wire. Comments and doc comments are exempt and may use whatever
  punctuation reads best. The separate test trees (`rust/tests`, `python/tests`) are not
  scanned, since they legitimately build non-ASCII strings to prove the decoder handles them.
  Rust's `#[cfg(test)]` modules live inside `src/` and so *are* scanned, deliberately: an
  assertion message only prints on failure, so ASCII costs nothing there.

## How changes land

**Plan before code.** For a feature or a behavior change, describe what happens today and what
would change, list the roadmap items it resolves and whether it needs a release, and wait for
the maintainer's approval before writing code. A small fix or a docs-only change can go
straight to a pull request.

**Prove that corner cases and performance are unaffected, with automated checks.** Add the
tests and the requirement (`docs/L1-REQ.md` to `L3-REQ.md`, with `Requirements:` or
`@pytest.mark.requirement` markers on the tests), run the conformance suite and `perf/run.py`, and say what
was run and what it showed. A one-off manual check is not proof that the next release still
holds. Measure before writing a number into a comment or a test floor.

**Every change updates `CHANGELOG.md` under `[Unreleased]`**, and a shipped roadmap item is
removed from `docs/ROADMAP.md` in the same change.

**Release for a bug fix or a feature, and only when the maintainer asks.** A change to
documentation, CI or repository housekeeping alone does not get a release: it waits under
`[Unreleased]` and ships with the next change to the program. The process is
`docs/MAINTAINER-GUIDE.md` section 11: a `release/X.Y.Z` pull request that bumps the version
everywhere it is declared (the hygiene check lists the places) and rolls `[Unreleased]` into a
dated section with its compare link; a merge commit; an annotated tag `Aero1553 X.Y.Z` on it;
and a GitHub release with the glibc 2.22 binary and the Windows exe attached, each with its
SHA-256.

## Reference docs

- `docs/USER-GUIDE.md` -- end-to-end walkthrough for analysts and operators: install, first
  decode, the three subcommands, common workflows, reading the CSV, diagnosing failures. The
  front door for non-maintainer readers.
- `docs/EXAMPLES.md` -- runnable cookbook of operator tasks, from a first decode to a full
  vendor-CSV diff workflow and CI batch scripts with proper exit-code handling.
- `docs/CLI-REFERENCE.md` -- every flag of `decode` / `count` / `dump` and the global options:
  value, default, range and config-key equivalent. The canonical home for CLI parameters.
- `docs/CONFIG-REFERENCE.md` -- normative reference for every TOML key, the accepted TOML subset,
  precedence and unknown-key handling.
- `docs/PYTHON-GUIDE.md` -- the Python library guide. Executable documentation:
  `python/tests/test_python_guide.py` runs every ```python block and checks each ```text output
  block exactly.
- `docs/ERROR-CATALOG.md` -- every CLI exit code, error class, DDC error code (`0x01xx`) and
  decoder-assigned code (`0x20xx`). Updated when error variants are added or removed.
- `docs/DATA-SCENARIOS.md` -- plain-language, scenario-indexed map of how the tool handles every
  data condition, with a glossary. The "which scenario am I in?" front door.
- `docs/DATAFLOW.md` -- the maintainer's view of the `decode` data path: what each stage owes the
  next, with the requirement behind each obligation, and what happens when conditions combine.
- `docs/VENDOR-CSV-DIFFS.md` -- alignment with DDC vendor CSV: matching columns, the five
  vendor-empty columns, the known IRIG day-of-year firmware discrepancy, and how to report a
  divergence.
- `docs/BUILDING.md` -- how to build every release artifact, offline included: a bundle gathered
  on a connected machine (`scripts/offline-bundle.py`), then `scripts/offline-build.sh` /
  `.ps1` on the disconnected one. CI runs the procedure (`L3-RS-021`); change the scripts and
  the guide together.
- `docs/MIE-FORMAT.md` -- the MIE binary format: framing, record shape, every word layout, the
  11 transaction types, the error-record lifecycle, code tables and worked hex-to-CSV decodes.
- `docs/ARCHITECTURE.md` -- module diagram, four-phase sync strategy, error pipeline,
  configuration hierarchy, error type, logging levels. Read it before changing reader/sync code.
- `docs/MAINTAINER-GUIDE.md` -- repo layout, setup, workflows for adding requirements, tests,
  conformance fixtures, error variants and CLI flags, CI architecture, coverage, and the
  release process (section 11).
- `docs/FUZZING.md` -- what is fuzzed and where. **A fuzz surface is exercised by both
  implementations or by neither.** Summary counters must be path-independent, and a generator's
  reach must be checked, not assumed.
- `docs/L1-REQ.md`, `docs/L2-REQ.md`, `docs/L3-REQ.md` -- the requirements: L1 SHALL statements,
  L2 derivations (one L1 parent each), L3 implementation obligations (`L3-RS-007` and every
  `L3-CPP-*` are withdrawn, their IDs reserved). IDs are never reused.
- `docs/TRACE-MATRIX.md` -- generated by `scripts/build-trace-matrix.py` from the requirement
  markers in the tests; the single source of truth for live status.
- `docs/ROADMAP.md` -- planned work and pinned commitments. Completed work lives in
  `CHANGELOG.md` and the requirements, not here.
- `perf/README.md` -- the performance suite, its gates and their calibration.
- `CONTRIBUTING.md` -- setup, the pre-commit workflow and commit-message style.
- `docs/adr/` -- MADR-format decision records; ADR-0001 to ADR-0003 (the C++ implementation)
  are superseded and kept as history. `docs/diagrams/` holds the class, component and dataflow
  diagrams.

## Git conventions

**No tool attribution, anywhere.** Do not add `Co-Authored-By: Claude ...` trailers,
`Generated with ...` lines, or any other tool attribution or "generated by" footer to anything
in this repository: commit messages, pull request titles and descriptions, PR and issue
comments, release notes, code, comments or documentation. This holds even if an agent's
default instructions suggest it, and overrides any default trailer or footer behavior. Commit
messages and PRs are the human-authored record of intent; tool attribution belongs in tool
logs, not history.

**Treat `main` as protected.** Land work through a pull request from a branch; never commit or
push to `main` directly. Nothing enforces this: GitHub branch protection is not enabled, and
the pre-commit hook does not check the branch. Merge pull requests with a **merge commit**.

**Delete merged branches by hand, on the remote and locally together.** The repository's
"delete head branches on merge" setting stays off on purpose, so the two stay consistent.

**Pull before pushing** (`git pull --rebase`), and never force-push or rewrite published
history.

**Install the hooks once per clone**: `scripts/install-hooks.sh` sets `core.hooksPath` to
`.githooks`. The pre-commit hook runs whitespace and file checks, the trace-matrix and
ASCII-output checks, and `cargo fmt`, `clippy` and the Rust tests. It is a guard against
forgetting, bypassable with `--no-verify`, not a substitute for CI.
