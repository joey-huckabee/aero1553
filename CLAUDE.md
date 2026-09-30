# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Aero1553 contains actively maintained Rust, Python and C++ libraries + CLIs
that decode proprietary binary recording files produced by Data Device
Corporation (DDC) MIL-STD-1553 PCI cards. CSV output is column-compatible with
DDC's own recording software so a decoded file can be diffed against vendor
output for validation.

The implementations ship together as a joint cut from a single
repository tag; future releases may diverge via impl-prefixed tags
(`rust-vX.Y.Z`, `python-vX.Y.Z`, `cpp-vX.Y.Z`). The Rust
implementation lives under `rust/`; the Python
implementation lives under `python/`; the C++ implementation lives under
`cpp/`. See `CHANGELOG.md` for the release
history and `git tag` for the current version. The Rust implementation was a clean rewrite,
not a transliteration: its CLI was redesigned, its writer is streaming
(constant memory), and its data-words container is an inline `[u16; 32]`
buffer. Maintain each implementation according to its own architecture while
keeping shared format and CSV behavior aligned.

The C++ implementation exists so **SLES 12 SP5** is a first-class deployment
target rather than a documented exception: SLES 12 has no Python ≥3.10 package,
and the Rust build needs an approved toolchain on the build host. It is written
to C++11 as accepted by **GCC 4.8.5**, the SLES 12 system compiler, and the same
source ships a Windows binary via MSVC. It is being delivered in phases — the
platform layer and build/CI apparatus first, then the decoder modules — so check
`cpp/README.md` for what is actually implemented before assuming parity. As of
the `dump` landing it passes the **whole** shared conformance manifest on both
Linux and Windows, and its CLI exposes an identical flag surface to the other
two — the `cli-surface-parity` gate now compares all three. The
three decisions that shape it are recorded as ADRs in `docs/adr/`.

Edition 2024, MSRV 1.88 — the floor is set by the crate's own use of **let-chains** (stabilized 1.88), not by its dependency: edition 2024 floors at 1.85 and `memmap2` declares 1.65. (`is_multiple_of` independently needs 1.87.) The crate has exactly one external dependency: `memmap2`. Argument parsing, CSV writing, TOML config, logging, and error types are all hand-rolled — preserve this property when adding features.

## Common Commands

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
cargo semver-checks check-release --baseline-rev "$(git describe --tags --abbrev=0)" --release-type minor  # public-API break check (CI-gated)

# Run the CLI
cargo run --release -- decode path/to/recording.mie -o decoded.csv
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
uv --directory python run vulture                   # dead-code scan (CI-gated)
uv --directory python run bandit -r src/aero1553    # security scan / SAST (CI-gated)
uv --directory python run aero1553 --help
uv --directory python build   # sdist + one abi3 wheel, via maturin

# C++ build and test (run from cpp/; the Makefile is authoritative on Linux)
cd cpp
make check                # build + full Catch2 suite, -Werror
make check-fuzz           # the [fuzz] cases only (what the nightly burn-in runs)
make check SANITIZE=1     # ASan + UBSan + LSan, fatal on first finding
make check-valgrind       # memcheck, fails on any leak
make check-gcc48          # SLES 12 fidelity tier: FULL suite on GCC 4.8.5, in a container
make tidy                 # clang-tidy (needs `bear -- make all` first for compile_commands.json)
make format-check         # clang-format, non-mutating; `make format` applies
make verify-ci            # the version-sensitive gates, with CI's tool versions, in a container
make versions             # print resolved toolchain + build directory

# C++ on Windows: VS 2022 -> File -> Open -> Folder -> cpp\  (CMake configures itself)
# cmake -B build-msvc -S . -G "Visual Studio 17 2022" -A x64
# cmake --build build-msvc --config Release
# ctest --test-dir build-msvc -C Release --output-on-failure

# WSL2: run make from a Linux-NATIVE path, not /mnt/c. Rootless podman cannot
# bind-mount a DrvFs path, so check-gcc48 silently mounts nothing and g++ reports
# "no input files" as though the source had vanished.

# The three C++ invariant gates (run from the repo root)
cd ..
bash scripts/assert-platform-confined.sh   # OS headers only in the platform backends
bash scripts/assert-locale-free.sh         # no setlocale, no <cctype> classification
bash scripts/assert-sources-agree.sh       # Makefile and CMake resolve the same sources

# Shared cross-implementation behavior. The runner defaults to EVERY registered
# implementation and fails if one is missing, so opting out is explicit --
# a run that silently tested fewer could report a full pass after a build failed.
(cd rust && cargo build) && (cd cpp && make all)
uv --directory python run python ../tests/conformance/run.py
uv --directory python run python ../tests/conformance/run.py --skip cpp   # no C++ build
uv --directory python run python ../tests/conformance/run.py --only cpp   # C++ vs the oracles

# Performance suite (perf/README.md): Rust + Python on the golden recording
# (tests/golden), outputs checked against the pins, gated on same-run ratios.
uv --directory python run python ../perf/run.py --repeat 5
uv --directory python run python ../perf/run.py --small --repeat 2   # smoke run

# Fuzz harnesses (L1-ROB-001). All three read the SAME three knobs:
#   MIE_FUZZ_ITERATIONS (default 256) / MIE_FUZZ_STREAM_LOGS / MIE_FUZZ_SUMMARY
# Point them all at one MIE_FUZZ_SUMMARY file and compare the FUZZ-SUMMARY
# lines -- on identical inputs the counters must be identical.
MIE_FUZZ_ITERATIONS=25000 cargo test --test integration fuzz_arbitrary_bytes_never_panic
MIE_FUZZ_ITERATIONS=25000 uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness -s
MIE_FUZZ_ITERATIONS=25000 make -C cpp check-fuzz
python scripts/compare-fuzz-summaries.py <dir-of-summary-files>
```

## Architecture

The decoder is a unidirectional pipeline. The big picture is best understood by tracing one record from disk to CSV:

1. **`reader.rs` — `MieFileReader`**: Top-level mmap-backed iterator. Calls `find_first_record()` for header skip, then loops: `validate_record()` → decode → yield `Result<MieMessage>`. On validation failure it calls `recover_sync()` to walk forward in 2-byte steps. Owns the `prev_was_error` flag used to classify SPURIOUS_DATA continuations.
2. **`sync.rs`**: Pure validation helpers (`find_first_record`, `validate_record`, `recover_sync`). Validation uses a **configurable N-record look-ahead** (default 2, per L2-SYN-026) — a candidate is only confirmed valid if the next `N-1` records' Type Words also look valid. This is critical: a single Type Word match alone produces too many false positives. No logging in this module — the reader emits any messages.
3. **`decode.rs`**: Pure binary → struct conversion. Type Word bit layout, IRIG vs Standard timestamp formats (auto-detected by probing the Command Word at both candidate offsets and scoring), Command Word, message format classification.
4. **`models.rs`**: Plain structs (`MieMessage`, `TypeWord`, `CommandWord`, `IrigTimestamp`, `StandardTimestamp`), `IntEnum`-style enums with explicit `#[repr(u8)]` discriminants, DDC error code constants (0x01xx) and decoder-assigned spurious codes (0x20xx). `DataWords` is the fixed-capacity inline buffer that replaces `Vec<u16>` for the per-record payload.
5. **`filter.rs` — `FilterIterExt::filter_messages`**: Iterator adapter. Both `exclude_*` and `include_*` filters are supported (the include set is the v2 redesign).
6. **`order.rs` — `OrderIterExt::order_rows`** (mirrored by `python/src/aero1553/order.py`): canonical row order (L1-OUT-003 / L2-WRT-021). The **last** iterator stage before the writer, on both the single-file and merge paths. Buffers one run of *consecutive* equal-`TIME_STAMP` records and stable-sorts it by `(RT, subaddress, direction)` — `Direction::Receive = 0` / `Transmit = 1` gives R-before-T for free in both languages. Records with no Command Word (`SPURIOUS_DATA`) are **pinned**: excluded from the sort and carried along by whichever record preceded them, which is what keeps an errored record adjacent to its `0x2000` continuation. Buffering is capped by `max_sort_group` (L2-WRT-022, default 4096); at the cap the run is flushed in arrival order with one WARN.
7. **`writer.rs`**: `write_csv` (single file) and `write_csv_split` (separate `_errors.csv`). Streams rows through a `BufWriter` — no DataFrame buffering. Column names and ordering match DDC vendor CSV byte-for-byte.
8. **`config.rs`**: Hand-rolled TOML loader for our schema (sections + key=value with strings/ints/bools/primitive arrays). Produces `DecoderConfig`. Precedence: **CLI overrides > config file > defaults**, applied via `DecoderConfig::with_overrides(ConfigOverrides)`.
9. **`cli.rs` — `run(argv)`**: Hand-rolled argparse with three subcommands (`decode`, `count`, `dump`). `count` is its own subcommand in v2 (was `--count` flag in v1). Default error mode is `inline` (every record in one CSV with `ERROR`/`ERROR_CODE` populated); `--separate-errors` toggles the split-file mode. The polarity was reversed and the former `--inline-errors` flag removed — passing it is a usage error.
10. **`log.rs`**: Tiny stderr logger. Single `AtomicU8` for the global level + `log_debug!`/`log_info!`/`log_warn!`/`log_error!` macros that format only when the level passes.
11. **`merge.rs` — `MergedRecordIter`** (mirrored by `python/src/aero1553/merge.py` and `cpp/src/merge.cpp`): multi-file time-sorted k-way merge (L1-MRG / L2-MRG). When `decode` resolves more than one input (positionals / `--manifest` / `--glob`, mutually exclusive, capped at `MAX_MERGE_FILES = 256`), this holds one record per open reader in a min-heap (`BinaryHeap`+`Reverse` in Rust, `heapq` in Python, `std::priority_queue` in C++), ordered by absolute IRIG microseconds with a `(us, file_index, seq)` tiebreak — O(files) memory, O(1) in records. That tiebreak is the **heap's internal** order only; the equal-timestamp order the CSV shows is `order.rs`'s canonical RT/MSG order (a heap key can't do it — the heap never holds two same-timestamp records from one input at once). It validates every input is calendar-locked IRIG up front (Standard / freerun / mixed → `IncompatibleMergeInputs`, exit 6) and applies the L2-MRG-005 DELTA scope: `per-file` (the default) leaves each reader's own DELTA in place — which is what makes a merged record's value identical to a single-file decode — while `global` (`--delta-scope global`) recomputes on the merged timeline. A single input bypasses this module entirely. No new dependency (the `*`/`?` glob matcher is hand-rolled in all three; the C++ one advances `?` by a whole UTF-8 character, because Rust and Python match over scalar values and a byte-wise `?` would disagree on any non-ASCII filename). The matcher is only half of `--glob`: L2-MRG-001 also fixes what it is applied *to* — entries that resolve to a **regular file**, symlinks followed, directories never — and how the pattern is **split**, using the platform's separator set (`\` only on Windows). All three had matchers that agreed and expansions that did not.
12. **`delta.rs` — `DeltaTracker`** (mirrored by `python/src/aero1553/delta.py` and `cpp/src/delta.cpp`): per-RT/MSG `DELTA` state, and **the one definition of the key** — `(rt, subaddress, direction)` packed into a `u32`. Used by BOTH `reader.rs` (per-file scope) and `merge.rs` (`--delta-scope global`). Before the extraction each kept its own tracker keyed differently — a packed integer in the reader, the `"<rt>:<sa><T|R>"` display string in the merge — and nothing asserted the two agreed on what "the same RT/MSG" meant (L3-RDR-001). It is **pure**: `observe()` returns a `DeltaOutcome` and the caller narrates, because a tracker cannot know whether a backward step deserves a WARN (single-file: yes, once per key) or is already reported at file granularity (merge, L2-MRG-006).

### Error handling model (important and non-obvious)

When the DDC card detects a bus error, it sets **bit 14 of the Type Word**, truncates the payload, and appends a 16-bit Error Word containing the code. If words remain from the original transaction, the card writes them as a separate `SPURIOUS_DATA` (type `0x20`) record immediately after.

The reader tracks `prev_was_error` across records so it can classify a following `SPURIOUS_DATA` as either `0x2000` (continuation of a preceding error) or `0x2001` (standalone). These `0x20xx` codes are decoder-assigned, not DDC hardware codes — see `models.rs` for the full code table.

Error records and SPURIOUS_DATA continuations are **valid records** that pass sync validation normally. Sync loss only happens on truly corrupt data (truncated mid-word, power loss). Don't conflate "errored record" with "sync loss."

### Output modes

- **Default (`error_mode = inline`)**: every record goes to the one CSV, with the `ERROR` / `ERROR_CODE` columns populated on errored and spurious rows. Calls `write_csv`. This is the mode a vendor-CSV diff uses, since the vendor tool also emits a single file.
- `--separate-errors` (`error_mode = separate`): clean messages → main CSV, errored + spurious → `<stem>_errors<suffix>` (lazy — the file isn't created if there are no error rows). Calls `write_csv_split`. Ignored on stdout (you can't split stdout), with a WARN.

The polarity was reversed in v2.8.0 — `separate` was the old default and the former `--inline-errors` flag was removed, so passing it is a usage error.

### Error type

All fallible APIs return `Result<T, MieError>`. `MieError` is a single enum (not a hierarchy). `kind()` returns a `MieErrorKind` discriminant. `is_record_error()` matches Python's `MieRecordError` exactly; `is_file_error()` is deliberately narrower than `MieFileError` (input I/O only — the whole-file rejections and destination guards answer `false` to both predicates). Both implementations pin the full classification by test, so a new variant can't be added without classifying it on both sides.

## Reference docs

- `docs/ARCHITECTURE.md` — module diagram, four-phase sync strategy, error pipeline, configuration hierarchy, error type, logging levels. Read this when changing the reader/sync code.
- `docs/USER-GUIDE.md` — end-to-end walkthrough for analysts and operators: install, decode-your-first-file, the three subcommands, common workflows (stdout / inline errors / allow-partial / filtering / site config), reading the CSV, diagnosing failures. The "front door" for non-maintainer readers.
- `docs/VENDOR-CSV-DIFFS.md` — alignment statement between Aero1553's CSV output and DDC vendor-generated CSV: which columns match byte-for-byte, the five vendor-empty columns we preserve as placeholders, the known IRIG day-of-year firmware discrepancy, the validation workflow, and the protocol for reporting a divergence as a bug.
- `docs/EXAMPLES.md` — runnable cookbook of common operator tasks: first-time decode, record counting, inline error output for vendor diff, RT-focused filtering, recovering from corrupt recordings with `--allow-partial`, stdout piping into pandas/awk, site-wide config plus per-invocation overrides, CI batch scripts with proper exit-code handling, investigating rejected files with `dump`, full vendor-CSV diff workflow, and a handful of shell ad-hoc filter patterns. Pairs with USER-GUIDE.md (which explains how the pieces work) by showing the pieces composed for real workflows.
- `docs/PYTHON-GUIDE.md` — the Python library guide: reading, filtering, merging, CSV output, `columns()` / `to_dict()` into NumPy, pandas and dataclasses, time with and without a year, config, dump, logging, errors, performance. Executable documentation: `python/tests/test_python_guide.py` runs every ```python block against the golden recordings and checks each ```text output block exactly, so an example that stops matching the library fails CI.
- `docs/CLI-REFERENCE.md` — complete per-flag reference for the `decode` / `count` / `dump` subcommands and global options: value, default, range, and config-key equivalent for every flag. The canonical home for CLI parameter docs (the root README links here rather than duplicating the flag surface); mirror of `CONFIG-REFERENCE.md` on the CLI side.
- `docs/CONFIG-REFERENCE.md` — normative reference for every TOML key the decoder accepts, with type / default / CLI override / validation behavior per key, plus precedence and unknown-key handling.
- `docs/ERROR-CATALOG.md` — operator-facing reference for every CLI exit code, error class, DDC error code (`0x01xx`), and decoder-assigned code (`0x20xx`). Updated when error variants are added or removed.
- `docs/DATA-SCENARIOS.md` — plain-language, scenario-indexed map of how the tool handles every data condition (clean / error / spurious records, IRIG / Standard / freerun timestamps, empty / truncated / non-MIE files, multi-file merge including per-file `--allow-partial` and duplicate collapsing, output modes, filters, MUX), each with its CSV / log / exit outcome and a glossary that defines the jargon (including "oracle"). Summarizes and links to ERROR-CATALOG.md (codes / exits) and MIE-FORMAT.md (binary). The "which scenario am I in, and what will the tool do?" front door.
- `docs/MAINTAINER-GUIDE.md` — repo layout, local dev setup, command cheat sheet, workflows for adding requirements / tests / conformance fixtures / error variants / CLI flags, CI architecture, coverage workflow, release process, cross-impl alignment principles. Start here when onboarding to make changes to the codebase.
- `docs/FUZZING.md` — the map of what is fuzzed: the two fuzzing architectures (per-language in-process robustness harnesses, whose `FUZZ-SUMMARY` counters are compared all-pairs; and the shared differential drivers in `tests/conformance/`, which compare per input), a per-surface inventory of which implementations cover it, where each runs in CI, the remaining parity gaps, and candidate future surfaces. Read this before adding a fuzz harness or interpreting a burn-in log. **The governing rule: a fuzz surface is exercised by all three implementations or by none.** Two non-obvious lessons are recorded there and worth knowing before you write a generator: summary counters must be **path-independent** (a byte count of dump output measures the temp-file name, not the decoder), and you must **check what your generator actually reaches** — a glob harness matched a probe zero times in 512 iterations, and the byte harnesses' comments claimed a branch they never once hit.
- `docs/MIE-FORMAT.md` — comprehensive MIE binary format reference: file-level framing, the three-section record shape, Type Word / IRIG and Standard timestamp / Command Word / Status Word bit layouts, per-format payload shapes for all 11 transaction types, error-record lifecycle (Type Word bit 14 → truncated payload → Error Word → optional SPURIOUS continuation), the DDC `0x01xx` and decoder-assigned `0x20xx` error code tables, full CSV output reference, three worked hex-to-CSV decodes. The deep reference for reverse-engineering or adding format support.
- `docs/L1-REQ.md` — Level 1 SHALL statements (system requirements grouped by category, plus the NR-001 out-of-scope note).
- `docs/L2-REQ.md` — Level 2 architectural derivations (each with a single L1 parent).
- `docs/L3-REQ.md` — Level 3 implementation obligations (cross-impl `L3-WRT-*`, plus per-impl `L3-PY-*` / `L3-RS-*` / `L3-CPP-*`; `L3-RS-007` is withdrawn and its ID reserved, from when static-musl support was retired).
- `docs/TRACE-MATRIX.md` — auto-generated trace matrix produced by `scripts/build-trace-matrix.py`. Forward trace from L1 through L2 and L3 to test artifacts (`@pytest.mark.requirement` markers in `python/tests/`, `/// Requirements:` doc-comments above Rust `#[test]` items, and Catch2 tag strings in `cpp/tests/`). Treat as the single source of truth for live status; the source docs hold spec content only.
- `docs/ROADMAP.md` — forward-looking roadmap: planned work plus pinned "do not drop" commitments (TOML config, CSV byte-compat, sync semantics). Completed work is not tracked here — it lives in `CHANGELOG.md` and the L1/L2/L3 requirements.
- `config/default.toml` — fully commented reference configuration; preserved across the port.
- `docs/adr/` — MADR-format architecture decision records. ADR-0001 sets the
  C++11 / GCC 4.8.5 floor and records why the SLE 12 Toolchain module was not
  taken; ADR-0002 explains the two-build-one-source-list arrangement; ADR-0003
  makes Windows a shipping target and lists the five behaviours that commits to.
  Read these before changing anything about how the C++ tree is built or which
  language features it uses.
- `python/` — maintained Python package and CLI with its own source and tests.
- `cpp/` — maintained C++ implementation; see `cpp/README.md` for build
  instructions, the WSL2 caveat, and what is implemented so far.
- `tests/conformance/` — shared hexadecimal fixtures and byte-exact CSV
  oracles exercised against every implementation.

## Conventions worth preserving

- **Single external dependency.** Only `memmap2`. Adding crates requires justification — argument parsing, CSV, TOML, logging, error types are all hand-rolled by design and the user values keeping it that way.
- **Streaming CSV.** Rows must flow through a `Write` impl as they are produced. Do not introduce `Vec<MieMessage>` or `Vec<Row>` buffering in the writer — constant memory is the design point.
- **N-record look-ahead in `sync.rs`** (default 2, configurable per L2-SYN-026). Don't remove it. Removing the look-ahead reintroduces false-positive resyncs.
- **`DataWords` is fixed-capacity by design.** MIL-STD-1553B caps a single transaction at 32 data words. Don't switch to `Vec<u16>` "for flexibility."
- **The canonical-order stage is run-scoped and capped.** `order.rs` / `order.py` permute only a run of
  *consecutive* equal-`TIME_STAMP` records, and only up to `max_sort_group` of them. Don't turn it into a
  whole-file sort (that breaks the constant-memory design point and L2-MRG-006's "never re-sort" rule) and
  don't remove the cap (a corrupt file whose timestamps all decode alike would buffer everything). Don't
  "simplify" the pinning of Command-Word-less records into a sentinel sort key either: a pin's position is
  defined relative to its *predecessor*, so it has to travel with that record — preserving its index is not
  the same thing, and getting this wrong silently breaks the `0x2000` continuation semantics.
- **Two stages hold data-sized buffers, and both are capped.** The reorder stage above
  (`max_sort_group`, L2-WRT-022) and the merge's de-duplication window
  (`max_collapse_survivors`, L2-MRG-008) are the only pipeline stages whose memory is a function
  of the *data* rather than of the file count; both default to `4096`, on purpose, so the two
  bounds read as one decision. `collapse_window_us` bounds the survivor set in **time** and cannot
  bound it in **count** — a window admits however many records fall inside it. Both stages degrade
  (one WARN, best-effort, never drop an output row) rather than fail. Also: state the dedup
  window's retention as a predicate over the retained **set** (`|survivor_us - current_us| <=
  window`), never as a loop over one end of the deque — after a lenient backward step the front can
  hold a *future* timestamp, and a one-sided test then never evicts it and blocks everything behind
  it. That bug was quadratic and retained 100% of records.
- **The commit is where `--no-clobber` is enforced, not the pre-flight** (`L2-WRT-023`). An
  `exists()` test before the output is opened answers a question about the past; between the answer
  and the rename another process can create the destination, and a replacing rename then destroys
  it. Every commit target — destination, errors file, and the `.partial` of each — goes through an
  atomic **non-replacing** move under `--no-clobber` (`MoveFileExW` with no flags on Win32;
  `link(2)` plus an exclusive-create fallback everywhere else). Keep the pre-flight as an early,
  friendlier report; don't mistake it for the guarantee, and don't pre-flight `.partial` targets (a
  stale one must not refuse a run that was never going to write one).
- **`commit` and `commit_partial` share one move routine** (`L3-WRT-005`), in all three trees. When
  each spelled the flush/close/move sequence out for itself, the two drifted in exactly the ways
  that shape predicts: a rule applied at one target and not the other, and error handling that
  wrapped the rename but not the flush. The final flush is part of the commit for classification
  purposes (`L2-WRT-024`) — it is where a disk-full error actually lands.
- **Main output commits before errors output on EVERY path** (`L2-WRT-019`), the `--allow-partial`
  path included. All three implementations got the normal path right and the `.partial` path
  backwards, which left an orphan `_errors.csv.partial` beside no main output whenever the main
  rename failed. State rules like this over "every commit", not over "the commit".
- **CSV column names and order are dictated by DDC vendor output — for the first 44 columns.** Don't "clean up" `MUX`, `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP` — they're columns by spec (`L2-WRT-013`). `TERM_NAME`/`IM_GAP`/`RCV_GAP`/`XMT_GAP` stay empty; `MUX` is populated from the input file name by default (`L2-WRT-020`) and is restored to empty (vendor-exact) by `--no-mux` / `[mux] enabled = false`.
- **Decoder-added columns go at the TAIL, never inside the vendor block** (`L1-OUT-001`, `L2-WRT-001`). The vendor layout is columns 1–44, `TIME_STAMP` through `XMT_GAP`; `ERROR` and `ERROR_CODE` (45–46) are decoder features with no vendor counterpart — the DDC tool doesn't emit them at all. Through v2.9.0 they sat between `DELTA` and `IM_GAP`, which silently shifted the three gap columns two positions off their vendor indices and made every positional comparison past `DELTA` wrong while all the column *names* still matched. If you add a column, append it; and prefer resolving columns by header name over index in tests (`writer.rs`'s `vendor_block_precedes_decoder_added_columns` and its Python mirror pin the boundary).
- **The sync modules are pure** (no logging, no I/O) — `rust/src/sync.rs` (which the Python package also runs, through its extension) and `cpp/src/sync.cpp`. The reader handles all user-facing messaging based on the values they return. Don't move logging into validation helpers: they lack the caller's context, so they narrate outcomes wrongly (`find_first_record` returning `None` is the *expected* result for a valid empty recording, and logging "no valid record found" there contradicted the reader's own correct message).
- **Shared conformance fixtures are byte-exact.** Treat
  `tests/conformance/` as the cross-implementation oracle; update expected CSV
  only after both implementations agree.
- **All three implementations are maintained.** Keep Rust-specific design
  decisions in the Rust crate, Python-specific ones in `python/`, and
  C++-specific ones in `cpp/`; align shared format semantics and
  vendor-compatible CSV behavior.
- **The C++ tree is C++11 on GCC 4.8.5, and two constructs are banned.** MSVC has
  no `/std:c++11` — its floor is C++14 — so it silently accepts things the target
  compiler rejects, producing "green on Windows, red on Linux". Never
  aggregate-initialize a class that has a default member initializer, and never
  pass a `const_iterator` to a container mutator. `<regex>` is banned outright
  (libstdc++ had none until GCC 4.9). The fidelity tier runs the **full suite**
  on GCC 4.8.5, not a compile check — don't reduce it to `make all`.
- **Only the C++ platform layer touches the OS.** Five concerns live behind
  `cpp/include/mie/platform.hpp` — mapping the input, atomic output, directory
  enumeration, binary stdout, path identity. Nothing else may include
  `<windows.h>` or `<sys/mman.h>`; `scripts/assert-platform-confined.sh` enforces
  it. If a new OS capability is needed, add it to the header and implement it in
  **both** backends — Windows is a shipping target, not a build that happens to
  link.
- **The C++ tree is never locale-sensitive.** `DELTA` is `%.6f`, whose decimal
  separator the locale chooses, so the program never calls `setlocale` and
  classifies characters with explicit ASCII ranges rather than `<cctype>`.
  `scripts/assert-locale-free.sh` enforces it. Rust and Python get this by
  construction; C++ is the only one where it has to be checked.
- **Everything written to stdout or stderr is ASCII, in all three
  implementations** (`L2-CLI-014`). Not just payload — log messages, usage
  errors and help text too. A Windows console runs at the OEM code page (437 in
  a US install), not UTF-8, so a single em dash reaches the operator as `ΓÇö`;
  mojibake in a decoder's own diagnostics gets reported as memory corruption.
  Use `--`, `->`, `section`. The rule once exempted stderr prose, and that
  carve-out is exactly how 46 sites accumulated across the three trees. Never
  fix a rendering problem with `SetConsoleOutputCP` or by reconfiguring a
  stream's encoding — that is per-platform, leaves redirected output wrong, and
  adds OS surface to a platform layer confined to five concerns.
  `scripts/assert-ascii-output.py` enforces it (wired into
  `scripts/repo-hygiene.sh` and the pre-commit hook). It parses string literals
  rather than grepping bytes, because C++ had spelled the character
  `"\xE2\x80\x94"` — ASCII on disk, non-ASCII on the wire. Comments and doc
  comments are exempt and may use whatever punctuation reads best. The separate
  test trees (`cpp/tests`, `rust/tests`, `python/tests`) are not scanned, since
  they legitimately build non-ASCII strings to prove the decoder handles them
  (the UTF-8 `?` glob cases). Rust's `#[cfg(test)]` modules live inside `src/`
  and so *are* scanned — deliberately: an assertion message only prints on
  failure, so ASCII costs nothing there, and excluding them would mean tracking
  module attributes through the scanner.
- **`cpp/sources.txt` is the one source list.** The Makefile (authoritative on
  Linux) and CMakeLists (authoritative on Windows) both read it, because the
  gcc:4.8 fidelity container has **no CMake at all** and cannot install one
  (Debian 7's repositories are archived; `apt-get update` there exits 100), so
  the tier that proves SLES 12 conformance has to be driven by make. Don't add a
  file to one build only — `scripts/assert-sources-agree.sh` compares what each
  build actually resolves. See ADR-0002, whose amendment records why "just use
  CMake everywhere" is more closed than it looks.

## Git conventions

Do **not** add `Co-Authored-By: Claude ...` trailers to commit messages on this repo, even if the harness's default instructions suggest it. Commit messages are the human-authored record of intent; tool attribution belongs in tool logs, not history. This overrides the default trailer behavior.
