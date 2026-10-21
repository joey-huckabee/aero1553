# Aero1553 — Maintainer Guide

Operational reference for anyone modifying the Aero1553 codebase. Covers the workflows you'll repeat: adding requirements, tests, conformance fixtures, error variants, and CLI flags; running the trace matrix; bumping coverage; releasing.

This guide is for **maintainers**. End-user CLI usage belongs in [`docs/USER-GUIDE.md`](USER-GUIDE.md); error/exit-code reference for operators lives in [`docs/ERROR-CATALOG.md`](ERROR-CATALOG.md); LLM-session project conventions live in [`CLAUDE.md`](../CLAUDE.md).

---

## 1. Repo layout

```
aero1553/
├── rust/                   Rust crate (edition 2024, MSRV 1.88)
│   ├── Cargo.toml / Cargo.lock
│   ├── .cargo/config.toml  cargo-llvm-cov coverage aliases (cov / cov-lcov / cov-ci)
│   ├── src/
│   │   ├── reader.rs        mmap-backed sequential reader; the central pipeline
│   │   ├── sync.rs          validate_record, find_first_record, recover_sync
│   │   ├── decode.rs        Type Word, IRIG/Standard timestamps, Cmd Word, classification
│   │   ├── models.rs        plain structs + enums + DDC/decoder error code constants
│   │   ├── error.rs         single MieError enum + MieErrorKind discriminant
│   │   ├── writer.rs        streaming CSV writer with atomic temp + rename
│   │   ├── filter.rs        message exclusion / inclusion filtering
│   │   ├── order.rs         canonical row order (equal-TIME_STAMP ties)
│   │   ├── config.rs        hand-rolled TOML loader for the L2-CFG schema
│   │   ├── cli.rs           hand-rolled argparse + run() entry
│   │   ├── dump.rs          raw + record-aware hex dump
│   │   └── log.rs           ~50-line stderr logger
│   └── tests/
│       ├── cli.rs           CLI acceptance tests (built binary as a subprocess)
│       └── integration.rs   cargo integration tests (multi-record, fuzz harness)
├── tests/
│   └── conformance/        cross-implementation suite (Rust / Python)
│       ├── manifest.json   case catalog
│       ├── inputs/*.hex    reviewable hex fixtures (NOT committed binaries)
│       ├── expected/*.csv  byte-exact CSV oracles
│       ├── expected/*.stderr  exact stderr oracles (none = stderr must be empty)
│       ├── configs/*.toml  per-case TOML config
│       └── run.py          the runner
├── python/                 Python package (supports 3.10–3.14)
│   ├── pyproject.toml      PEP 621 metadata, maturin build, PEP 735 dev group; pytest markers
│   ├── uv.lock             pinned dependencies; committed
│   ├── native/             PyO3 binding crate, built as aero1553._native
│   ├── src/aero1553/       package source (mirrors Rust module names)
│   └── tests/              pytest suite
├── scripts/
│   ├── build-trace-matrix.py    generates docs/TRACE-MATRIX.md
│   ├── pytest-by-requirement.py runs pytest filtered by requirement marker
│   ├── diagnose-vendor-delta.py identifies which DELTA rule a vendor CSV follows
│   ├── repo-hygiene.sh          CI backstop for the pre-commit file checks
│   ├── assert-ascii-output.py   no non-ASCII in any shipped string literal
│   ├── coverage.sh              local coverage run (Rust + Python)
│   └── install-hooks.sh         points core.hooksPath at .githooks/
├── docs/
│   ├── L1-REQ.md / L2-REQ.md / L3-REQ.md   spec docs (source of truth)
│   ├── TRACE-MATRIX.md     auto-generated from L1/L2/L3 + test markers
│   ├── ARCHITECTURE.md     module diagram, error pipeline, configuration hierarchy
│   ├── CLI-REFERENCE.md    complete per-flag CLI reference
│   ├── CONFIG-REFERENCE.md normative TOML key reference
│   ├── DATA-SCENARIOS.md   data conditions -> CSV / log / exit outcomes
│   ├── ERROR-CATALOG.md    operator reference for every error / exit code
│   ├── EXAMPLES.md         runnable cookbook of operator tasks
│   ├── FUZZING.md          fuzz surfaces, per-impl coverage, parity gaps
│   ├── MIE-FORMAT.md       comprehensive binary format + CSV column reference
│   ├── ROADMAP.md          forward-looking roadmap (planned work + commitments)
│   ├── USER-GUIDE.md       end-to-end walkthrough for analysts / operators
│   ├── VENDOR-CSV-DIFFS.md alignment statement vs DDC vendor CSV
│   ├── MAINTAINER-GUIDE.md (this file)
│   └── diagrams/           hand-written SVG diagrams (the source of truth)
├── config/default.toml     fully-commented reference TOML schema
└── .github/workflows/      ci.yml, differential.yml, fuzz.yml,
                            perf.yml, codeql.yml, sonarcloud.yml
```

---

## 2. Local development setup

### Rust

```bash
rustup update stable
rustup default stable
rustup component add clippy rustfmt llvm-tools-preview
cd rust
cargo build
cargo test
```

### Python

```bash
pipx install uv              # or via your usual install; CI pins 0.12.20
uv --directory python sync   # creates python/.venv, installs locked deps, builds + installs aero1553
uv --directory python run pytest
```

`uv sync` installs the package in editable mode, and that includes compiling the PyO3 extension (`python/native/`), so a Rust toolchain is needed as well. An editable install rebuilds the extension only when one of the `[tool.uv] cache-keys` in `python/pyproject.toml` changes -- those list the Rust sources of both the binding and the core crate, so `uv run` picks up a Rust edit by itself. If `python -m aero1553` ever fails to import, re-run sync.

### Cross-impl conformance (needs both)

```bash
# Build the Rust binary first so the runner doesn't have to:
(cd rust && cargo build)
# Then run the suite (uses the uv environment's interpreter for the Python side):
uv --directory python run python ../tests/conformance/run.py
```

The runner reads `tests/conformance/manifest.json`, materializes each `.hex` fixture into a temp `.mie` file, invokes every registered CLI against it, and diffs the produced CSVs against the checked-in oracle (or asserts the exit code for negative cases), and every case's stderr against its stderr oracle.

The runner additionally cross-checks the **config parsers** (the hand-rolled Rust loader, which the Python package also runs, L3-PY-005, compared through both CLIs): a curated corpus (`config_parity.py`) plus a differential **fuzzer** (`config_fuzz.py`) that generates config documents and asserts both implementations agree on accept/reject. Both run inside `run.py` — there is no separate command. The fuzzer is deterministic (fixed seed + `MIE_CONFIG_FUZZ_ITERS` iterations, default 100); for a deeper local sweep run `MIE_CONFIG_FUZZ_ITERS=5000 uv --directory python run python ../tests/conformance/run.py` (a *distinct* knob from the reader/dump `MIE_FUZZ_ITERATIONS` in §11's fuzz workflow). A divergence prints the exact config to pin in `config_parity.py`. See `tests/conformance/README.md`.

A third guard, `config_path_parity.py`, covers the layer above the parsers: the `--config` **path** rather than its contents. The other two never vary the path, so what counts as a usable config file — and which exit code and message a bad one produces — was pinned only by per-implementation unit tests that could drift apart unnoticed. It compares the **exact exit code** (not just accept/reject) and requires the promised message text from both CLIs, over the surface documented in `CONFIG-REFERENCE.md` §"Trust boundary": regular files only, missing/unusable is exit `5`, and any readable location is accepted (spaces, non-ASCII names, `..` segments). Platform-dependent cases (character devices, symlinks) skip themselves and report the skip, so a corpus that quietly shrinks on one OS is visible.

---

## 3. Daily-command cheat sheet

```bash
# Rust (from rust/)
cargo test --all-targets                         # all unit + integration
cargo test --lib reader::tests::skips_proprietary_header   # single test
cargo clippy --all-targets -- -D warnings
cargo fmt --check
cargo cov-ci                                     # coverage gate (alias in rust/.cargo/config.toml)
RUSTDOCFLAGS="-D warnings" cargo doc --no-deps   # rustdoc link check (CI-gated)
cargo +1.88 check --all-targets                  # MSRV 1.88 floor (CI-gated)
cargo deny check                                 # supply-chain audit (CI-gated; config rust/deny.toml)
cargo semver-checks check-release --baseline-rev "$(git describe --tags --abbrev=0)" --release-type minor  # public-API break check (CI-gated)

# Python (from repo root)
uv --directory python run pytest                      # all tests
uv --directory python run pytest tests/test_e2e.py -k delta -v
uv --directory python run mypy src                    # strict type check (CI-gated)
uv --directory python run pylint src/aero1553         # lint (CI-gated, fails below 10/10)
uv --directory python run ruff check                  # ruff lint (CI-gated)
uv --directory python run ruff format                 # auto-format (CI runs `ruff format --check`)
# tests/, scripts/, perf/ Python: same gates, config passed explicitly (no pyproject above them)
uv --directory python run ruff check --config pyproject.toml ../tests ../scripts ../perf
uv --directory python run ruff format --config pyproject.toml ../tests ../scripts ../perf
uv --directory python run vulture                     # dead-code scan (CI-gated)
uv --directory python run bandit -r src/aero1553      # security scan / SAST (CI-gated)
uv --directory python run pytest --cov               # coverage gate (fail_under=92 in pyproject.toml)
uv --directory python run python ../tests/conformance/run.py

# Filter pytest by requirement marker
python scripts/pytest-by-requirement.py L2-WRT-015
python scripts/pytest-by-requirement.py L3-PY-          # whole L3-PY-* family

# Trace matrix
python scripts/build-trace-matrix.py             # regenerate docs/TRACE-MATRIX.md
python scripts/build-trace-matrix.py --check     # what CI does — exits 1 on drift

# Diagnose a vendor CSV whose DELTA column disagrees with ours
# (reads the vendor CSV only — no MIE file, no decoder run)
python scripts/diagnose-vendor-delta.py vendor.csv

# CLI dry-runs against a real file
(cd rust && cargo run --release -- decode path/to/recording.mie -o decoded.csv)
uv --directory python run aero1553 decode path/to/recording.mie -o decoded.csv
```

The diagrams in `docs/diagrams/` (`class.svg`, `component.svg`,
`dataflow.svg`) are hand-written SVG and are their own source -- there is no
generator. Edit the SVG directly: every box, note and edge is a `<g id="...">`
preceded by a comment naming it. Keep the text ASCII, the file LF with a final
newline, and the colours on the CSS custom properties at the top so the light
and dark themes stay in step. No CI job checks the diagrams, so update them in
the same change as the code they depict.

---

## 4. Adding a requirement

The three-tier system: **L1** = system SHALL statement; **L2** = architectural derivation with one L1 parent; **L3** = implementation obligation with one L2 parent.

### Choose the tier

- **L1** — a new product-level capability or constraint. Rare. The 12 categories in `docs/L1-REQ.md` (`DEC`, `OUT`, `DLT`, `CLI`, `LOG`, `MODE`, `SYN`, `ERR`, `CFG`, `CONF`, `EXIT`, `ROB`) are stable; pick the closest fit.
- **L2** — a behavior derived from an existing L1. Most new requirements land here. Pick the L2 category that names the behavior (`L2-DEC-*`, `L2-WRT-*`, `L2-SYN-*`, etc.).
- **L3** — an implementation detail. Use `L3-PY-*` for Python-only constraints, `L3-RS-*` for Rust-only, or the L2's category code for cross-impl detail (e.g., `L3-WRT-001` pins the temp-file naming pattern derived from `L2-WRT-015`).

### Add the ID

Pick the next integer in the category — retired IDs are never reused. The current max per category is visible in `docs/L1-REQ.md` / `L2-REQ.md` / `L3-REQ.md` category tables.

**L1 format** (in `L1-REQ.md`):

```markdown
### L1-XXX-NNN

**Statement**: [SHALL obligation]

**Rationale**: [why this requirement exists]

**Verification Method**: Test (T)
```

**L2 format** (in `L2-REQ.md`):

```markdown
#### L2-XXX-NNN

**Parent**: L1-XXX-NNN
**Statement**: ...
**Rationale**: ...
**Verification Method**: Test (T)
```

**L3 format** (in `L3-REQ.md`, compact two-line):

```markdown
**L3-XXX-NNN** · Parent: L2-XXX-NNN · Verification: T
Statement text on the next line.
```

### Choose the verification method

Single letters from DO-178: **T** = Test, **I** = Inspection, **A** = Analysis, **D** = Demonstration. Multiple methods comma-separated.

- **Test (T)** — there's an automated test asserting the behavior. The trace matrix expects a `@pytest.mark.requirement` marker or a `/// Requirements:` doc-comment.
- **Inspection (I)** — verified by reading the source. Use for structural properties (a single function called from three places, an enum being exhaustively matched, build config declarations).
- **Analysis (A)** — verified by logical/mathematical argument. Use for bounded-loop proofs, memory complexity claims.
- **Demonstration (D)** — verified by operator running the system. Use for things like "release binary runs on the target deployment host".

Don't mark `Test (T)` if no test exists or will exist. The matrix will surface it as **Draft** and the gap will be obvious.

**An I / A / D requirement needs an `**Evidence**` line too.** Add it under `**Verification Method**`, naming in backticks what carries the check — the script, CI job, or source symbol a reader can go look at:

```markdown
**Verification Method**: Inspection (I)
**Evidence**: `scripts/repo-hygiene.sh` — its no-MIE-recordings-tracked check
scans the whole tracked tree and fails the repo-hygiene CI job.
```

Those backticked names become the row's artifact column. Without an `**Evidence**` line the requirement stays **Draft**, exactly like a `Test (T)` requirement with no marker — before v2.12.0 a bare method letter was enough to report `Implemented (I)`, which is how three requirements came to claim they were met beside a literal `_(TBD)_` in their own artifact column. A declared method is a plan; evidence is a result. L3 statements are one-liners, so theirs rides on the same line: `· Evidence: \`path/to/thing\``.

### Tag the test

Once an L1/L2/L3 ID exists, tag whichever tests verify it:

**Python** (in `python/tests/`):

```python
@pytest.mark.requirement("L2-WRT-015")
def test_temp_file_rename_is_atomic(tmp_path: Path) -> None:
    ...
```

Multiple markers stack:

```python
@pytest.mark.requirement("L2-CLI-011")
@pytest.mark.requirement("L1-EXIT-002")
def test_cli_no_valid_records_returns_exit_2(...):
    ...
```

**Rust** (`rust/src/**/*.rs` and `rust/tests/*.rs`):

```rust
/// Requirements: L2-WRT-015
#[test]
fn temp_file_rename_is_atomic() { ... }
```

Multiple IDs comma-separated on one line:

```rust
/// Requirements: L2-WRT-015, L2-WRT-016, L3-WRT-001
#[test]
fn atomic_commit_renames_temp_over_destination() { ... }
```

### Regenerate the trace matrix

After adding the spec / tags:

```bash
python scripts/build-trace-matrix.py
git add docs/TRACE-MATRIX.md
```

If you forget, CI's `trace-matrix` job (`python scripts/build-trace-matrix.py --check`) will fail with a clear message.

---

## 5. Adding a test

### Test pyramid

The project uses four test tiers, narrowest scope at the bottom:

| Tier                  | Subject                                  | Location                            | Run with                                         | Cross-platform |
|-----------------------|------------------------------------------|-------------------------------------|--------------------------------------------------|----------------|
| **Unit**              | one function / one module, in-process    | `rust/src/<module>.rs` `#[cfg(test)] mod tests` (Rust); `python/tests/test_*.py` (Python) | `cargo test --lib` / `pytest`                    | Linux + Windows |
| **Integration**       | multiple modules via the library API, in-process | `rust/tests/integration.rs` (Rust); `python/tests/test_integration_*.py` (Python) | `cargo test --test integration` / `pytest`       | Linux + Windows |
| **CLI acceptance**    | the **built binary** as a subprocess — exit codes, stdout, stderr, filesystem effects | `rust/tests/cli.rs` (Rust)               | `cargo test --test cli`                          | Linux + Windows |
| **Conformance**       | byte-exact cross-impl equivalence (Rust ↔ Python CLI) | `tests/conformance/`                | `uv --directory python run python ../tests/conformance/run.py` | Linux + Windows |

The two upper tiers both spawn the actual binary, but they serve different purposes:

- **CLI acceptance** (`rust/tests/cli.rs`) is Rust-only. It covers behaviors that conformance can't or doesn't: `--no-clobber`, input/output collision rejection, `--include-*` filter syntax (L3-RS-010; Python provides the same filters per L3-PY-013 and covers them in its own test suite), `--help` / `--version`, exit-class taxonomy, and other CLI surfaces where stdout/stderr/exit-code semantics matter more than CSV byte-equality.
- **Conformance** (`tests/conformance/`) holds Rust and Python to byte-identical CSV output (or matching exit code for negative cases). Anything that affects the CSV contract should land here so both implementations stay aligned.

When you add a behavior, ask: **does this need to behave the same in Python?** If yes, add it to conformance. If no (Rust-only feature, atomic-write artifact, exit-class taxonomy detail), add it to `rust/tests/cli.rs`. Both tiers run on Linux and Windows automatically via `cargo test --all-targets`.

### Python

Tests live in `python/tests/test_*.py`. Use the synthetic-record builders in `python/tests/conftest.py` if you need varied IRIG timestamps or errored / SPURIOUS records:

- `normal_record_rt15_sa11_us(microseconds)` — varies only the timestamp on the canonical receive record.
- `errored_record_rt15_sa11_us(microseconds)` — Type Word bit 14 set, 0x011E error code.
- `spurious_record_us(microseconds, data_word)` — SPURIOUS_DATA shape.

For end-to-end CLI tests, use `pytest.LogCaptureFixture` to assert on log messages, e.g. the `decode exit class:` summary line.

### Rust unit and integration

Unit tests live next to the code they test in `rust/src/<module>.rs` under `#[cfg(test)] mod tests { ... }`. Library-level integration tests live in `rust/tests/integration.rs`.

Use the existing `TempFile` helper at the bottom of `rust/src/reader.rs` (private) or `rust/tests/integration.rs` (also private but copy-paste-friendly). For new variants, construct minimal byte sequences from the canonical `RECORD_RT15_SA11_RCV` shape.

Always tag new tests with `/// Requirements:` so the trace matrix credits them.

### Rust CLI acceptance

CLI acceptance tests in `rust/tests/cli.rs` spawn the actual built binary located via `env!("CARGO_BIN_EXE_aero1553")` (Cargo populates this per test target and appends `.exe` on Windows automatically — no per-OS code paths needed) and use `std::process::Command::output()` to invoke it. Style conventions:

- Use the `TempDir` helper in the same file: per-test scratch directories under `std::env::temp_dir()`, keyed by pid + atomic counter, removed on drop. Tests can then use plain `dir/input.mie`, `dir/output.csv` paths.
- Use the `run([...])` helper rather than `Command::new` directly. It echoes any captured stderr into test output so a Windows CI failure can be triaged from the runner log without re-running locally.
- Assert on exit code via the `exit_code(&out)` helper, on stdout/stderr via `String::from_utf8_lossy` + `.contains(...)`, and on filesystem effects with `std::fs::read[_to_string]`.
- Don't byte-compare CSV output — that's conformance's job. Acceptance tests should assert on coarser invariants (header row exists, row count >= 2, sentinel preserved when `--no-clobber` refused).
- Cross-platform considerations: never hard-code `/` or `\\` in paths (use `PathBuf::join` and pass paths as `OsStr`); never assert on `\n` vs `\r\n` (use substring `.contains()` on stdout/stderr).

Always tag new tests with `/// Requirements:` so the trace matrix credits them.

Run locally with:

```bash
cd rust
cargo test --test cli                  # CLI suite only
cargo test --test cli -- --nocapture   # also show stdout / stderr from the spawned binary
cargo test --all-targets               # unit + integration + cli together (what CI runs)
```

---

## 6. Adding a conformance fixture

Cross-implementation conformance fixtures verify byte-identical CSV output (or matching exit code), and identical stderr, across Rust and Python. Add a case only for behavior that's specified at L2 as shared.

### Steps

1. Build the hex fixture under `tests/conformance/inputs/`. Include a header comment naming the requirement(s) it exercises. Example: `tests/conformance/inputs/homogeneous-payload.hex`.

2. If the case expects a successful decode (default), generate the CSV oracle. Run both implementations against your fixture and compare manually until they agree, then commit the agreed output to `tests/conformance/expected/<name>.csv`.

3. For negative cases (no CSV oracle, just exit-code check), set `expected_exit` in the manifest and skip the oracle file.

   Every case, negative or not, is also held to a stderr oracle, `tests/conformance/expected/<name>.stderr`, with paths as placeholders and `INFO` / `DEBUG` lines left out (see the conformance README's "Stderr oracles"). Generate it with `run.py --update-expected` once all three agree, and review it like the CSV: it is the wording the operator will see. A case that writes nothing to stderr has no file. Don't reach for `expected_stderr_contains`; it is only for a case whose point is that an `INFO` or `DEBUG` line appears.

   **Multi-file merge cases** use `"inputs": ["inputs/a.hex", "inputs/b.hex", …]` (a list) instead of the single `"input"` — the runner materializes each hex to its own temp `.mie` and passes them all as positionals to both CLIs (L2-MRG-001). See the `merge-ordered` (oracle) and `merge-incompatible-freerun` (`expected_exit: 6`) cases. Note: the `--allow-partial` merge path needs >64 KB of garbage to force an unrecoverable sync loss, which isn't a small reviewable hex fixture, so it's covered by a library test in each implementation (`merge_allow_partial_*`) rather than a conformance case.

4. Register in `tests/conformance/manifest.json`:

   ```json
   {
     "name": "your-case",
     "input": "inputs/your-case.hex",
     "expected": "expected/your-case.csv"
   }
   ```

   For negative cases:

   ```json
   {
     "name": "your-case",
     "input": "inputs/your-case.hex",
     "expected_exit": 2
   }
   ```

   For strict-mode cases:

   ```json
   {
     "name": "your-case",
     "input": "inputs/your-case.hex",
     "expected_exit": 1,
     "config": "configs/strict.toml"
   }
   ```

5. For cases that need extra CLI flags, add a single `args` array — it is passed verbatim to both CLIs, which share one argument surface. Don't add a fixture for an implementation-specific behavior (those go in each impl's own test suite per L1-CONF-001).

6. Run the suite locally to confirm:

   ```bash
   cargo build
   uv --directory python run python ../tests/conformance/run.py
   ```

7. Update the count in any docs that mention "N conformance cases" (this guide, etc.).

See `tests/conformance/README.md` for the full manifest schema.

---

## 7. Adding an error variant

When a new error class is needed (per `docs/ERROR-CATALOG.md` taxonomy), land it in both crates and document.

### Rust (`rust/src/error.rs`)

1. Add the variant to `enum MieError { ... }` with `offset` and any structured detail fields.
2. Add a matching value to `enum MieErrorKind`.
3. Extend `MieError::kind()` to map the new variant.
4. Add a match arm to the `impl fmt::Display for MieError` block with the user-facing message.
5. Classify it: add it to `is_record_error()` (tied to one record's byte offset), to `is_file_error()` (an I/O failure on the input itself), or to **neither** — whole-file rejections and destination guards deliberately answer `false` to both. Then list it in the matching arm of `every_error_kind_is_deliberately_classified` in `rust/src/error.rs`, which fails if a variant appears in no list, and mirror the decision in the Python test. Carrying an `offset` does not by itself make a variant record-class: `HomogeneousPayload` and `TimestampFormatMismatch` cite an offset but reject the whole file.

### Python (`python/src/aero1553/exceptions.py`)

1. Add a new class extending `MieFileError` or `MieRecordError` as appropriate. Follow the existing pattern: `__init__` sets typed attributes and calls `super().__init__(message)`.
2. Add a class-level docstring naming the L1/L2 requirement(s) it satisfies.

### CLI exit-code mapping

`rust/src/cli.rs` and `python/src/aero1553/cli.py` both have a try/except (or `match`) chain that maps errors to exit codes. Decide which class the new error belongs to (see `docs/ERROR-CATALOG.md` section 1):

- Wrong-file-type / file-shape errors → exit 2 (alongside `NoValidRecords`)
- Unrecoverable mid-file → exit 3 (alongside `UnrecoverableSyncLoss`)
- Generic record / I/O / writer → exit 1 (the default branch)

Add the explicit handler and update the corresponding `decode exit class:` log line.

### Documentation

1. Update `docs/ERROR-CATALOG.md`:
   - Add a row in section 3 (file-level) or section 4 (record-level).
   - If the error introduces new operator-visible behavior, add to the decision tree in section 9.
2. Tag any new tests with the requirement ID and regenerate the trace matrix.
3. If the variant pins a NEW requirement (rather than implementing an existing one), add the L2 / L3 to the spec docs first.

### Cross-impl alignment

Both crates **must** raise the same variant for the same input. Add a conformance fixture (negative case, `expected_exit`) if the exit-code class is new, so future drift is caught.

---

## 8. Adding a CLI flag

L1-CLI-001 only requires the two CLIs to offer the same capabilities; their syntax MAY differ. In practice they have been kept to an **identical flag surface** across `decode` / `count` / `dump` — add the flag to both implementations with the same name and semantics so that parity holds. Two things enforce this: the conformance suite drives both CLIs from a single shared `args` vector (so a flag missing from one side breaks any case that uses it), and the `check_cli_surface` gate in `tests/conformance/run.py` compares the two CLIs' full long-flag sets (extracted from their `--help` output) and **fails the conformance run if they diverge** — naming the offending flag. So a flag added to only one implementation fails CI even if no case exercises it; add it to both (and keep the help text advertising it, since the gate reads `--help`).

### Rust (`rust/src/cli.rs`)

The CLI argparse is hand-rolled. Add the flag in the relevant subcommand's parser. Wire it into the appropriate path (`run_decode`, `run_count`, `run_dump`). Add `parse_*` unit tests for the new flag (greedy / non-greedy / repeats / `=value`) following the existing `filter_flag_*` pattern.

### Python (`python/src/aero1553/cli.py`)

Add an `argparse` argument to the relevant subparser. Wire it the same way.

### Config schema

If the flag has a TOML counterpart (which it usually should for site-wide config), update `config/default.toml` with the new key (commented out, with a description), the L2-CFG-008 schema reference in `docs/L2-REQ.md`, and both `config.rs` / `config.py` to load and validate the key.

### Tests

- Per-impl unit tests that the flag is parsed correctly.
- Per-impl end-to-end test that the flag changes behavior.
- A conformance fixture if the resulting behavior is cross-impl visible (typically yes).

---

## 9. CI architecture

`.github/workflows/ci.yml` defines the jobs below. The table is the list —
it is cross-checked against the workflow, so a job added without a row here
shows up as a gap rather than silently drifting:

| Job | What it gates | Platforms | Failure cost |
|-----|---------------|-----------|--------------|
| `rust` | `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test --all-targets` (unit + `rust/tests/integration.rs` + `rust/tests/cli.rs` CLI acceptance suite — see section 5 for the test pyramid); `cargo cov-ci` (90% line / 89% region coverage floors) Linux-only | `ubuntu-latest`, `windows-latest` | Block merge |
| `rust-doc` | `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps` — fails on broken intra-doc links and other rustdoc lints in the doc-heavy crate | `ubuntu-latest` | Block merge |
| `rust-glibc-2-22` | `cargo zigbuild --release --target x86_64-unknown-linux-gnu.2.22` -- the SLES 12 SP5 binary (L3-RS-018); fails on any symbol newer than `GLIBC_2.22`, runs the conformance suite against that binary, uploads it as the `aero1553-x86_64-linux-glibc2.22` artifact | `ubuntu-latest` | Block merge |
| `rust-msrv` | `cargo check --all-targets` on the pinned **1.88** toolchain — enforces the declared `rust-version` (the main `rust` job builds on stable) | `ubuntu-latest` | Block merge |
| `cargo-deny` | `cargo deny check` — RustSec advisories, license allow-list, bans (duplicates/wildcards), and crates.io-only sources (config in `rust/deny.toml`) | `ubuntu-latest` | Block merge |
| `cargo-semver-checks` | `cargo semver-checks check-release` vs the latest release tag (`baseline-rev`, `release-type: minor`) — fails a PR that makes a **breaking** public-API change without a major bump; additive changes pass | `ubuntu-latest` | Block merge |
| `python` | `uv sync --locked` + `uv run pytest`; `uv lock --check` + `uv build` (asserts one `cp310-abi3` wheel) Linux/3.12-only | 5 versions × Linux (3.10–3.14), 2 versions × Windows (3.12, 3.14) | Block merge |
| `mypy` | `uv run mypy src` — strict type check, analyzed as Python 3.10 (config in `python/pyproject.toml`) | `ubuntu-latest` (3.12) | Block merge |
| `pylint` | `uv run pylint src/aero1553` — lints the package; curated disables + line length in `python/pyproject.toml` `[tool.pylint.*]` (gate fails below 10/10) | `ubuntu-latest` (3.12) | Block merge |
| `ruff` | `uv run ruff check` + `uv run ruff format --check` — fast lint + formatter check over the package **and tests** (config in `python/pyproject.toml` `[tool.ruff]`); then both again with `--config pyproject.toml ../tests ../scripts ../perf` for the repo's Python outside the package; run `ruff format` to fix | `ubuntu-latest` (3.12) | Block merge |
| `vulture` | `uv run vulture` — dead-code scan over the package **and tests**; scan paths + intentional-name ignores (interface args, documented constants) in `python/pyproject.toml` `[tool.vulture]` | `ubuntu-latest` (3.12) | Block merge |
| `bandit` | `uv run bandit -r src/aero1553` — Python security static analysis (SAST) over the package source; fails on any finding at the default severity/confidence | `ubuntu-latest` (3.12) | Block merge |
| `python-coverage` | `uv run pytest --cov` — 92% combined line+branch floor (`fail_under` in `python/pyproject.toml`) | `ubuntu-latest` (3.12) | Block merge |
| `conformance` | `pip install -e ./python` then `python tests/conformance/run.py` — every fixture, Rust and Python. The runner defaults to every registered implementation and fails if one is missing, so this job cannot pass by silently testing fewer | `ubuntu-latest`, `windows-latest` | Block merge |
| `trace-matrix` | `python scripts/build-trace-matrix.py --check` — fails if `docs/TRACE-MATRIX.md` is stale relative to the spec docs + test markers | `ubuntu-latest` | Block merge |
| `repo-hygiene` | `bash scripts/repo-hygiene.sh` — re-runs the pre-commit hook's file-level checks (final newline, CRLF, merge markers, 1 MB cap, `*.mie`, `Cargo.lock` parity, `dbg!()`, `unsafe`/`SAFETY:`) over the whole tracked tree, so a `--no-verify` commit is still caught, plus the doc-drift checks that have no hook counterpart (this table lists every `ci.yml` job; the config-key set agrees across its three text sources; no TRACE-MATRIX row claims Implemented with no artifact; the declared Rust MSRV agrees across `Cargo.toml`, CI and the docs; `ROADMAP.md` doesn't restate a `TRACE-MATRIX.md` status; the Python exception hierarchy matches its ASCII-tree and UML drawings; every shipped string literal in both implementations is ASCII, via `scripts/assert-ascii-output.py` — L2-CLI-014) | `ubuntu-latest` | Block merge |

The jobs above are `.github/workflows/ci.yml`, which gates the Rust and Python
implementations. (A separate `cpp-ci.yml` gated the C++ implementation until it
was removed in 5.0.0.)

Three further workflows cut **across** the implementations rather than gating
one of them:

| Workflow / job | Covers | Notes |
|-----|--------------|--------|
| `cross-impl-differential` | Rust, Python | **The job that runs every implementation together**, with no path filter, and therefore the one that runs the differential config checks (`config_parity`, `config_fuzz`, `config_path_parity`), which compare **all pairs** and skip themselves when fewer than two implementations are selected. Lives alone in `.github/workflows/differential.yml`. It runs `run.py` with no `--only` / `--skip`, so a missing implementation is an error rather than a smaller pass. Deliberately **unfiltered**: it previously lived in the (since removed) C++ workflow behind a `cpp/**` filter, which meant the check written to catch a divergence *between* implementations only ran when the C++ tree changed. Its first run found two real divergences. |
| `.github/workflows/codeql.yml` | `rust`, `python` | Both use `build-mode: none` and extract from source. |
| `.github/workflows/sonarcloud.yml` | `rust/src`, `python/src` | Rust (clippy disabled; lcov coverage) and Python (coverage XML) analysis, waiting on the quality gate. |

The Rust and Python deployment targets are Linux. Windows cells exist to catch path / encoding / line-ending portability bugs early, not because Windows is a production target. Coverage gates (Rust + Python), lockfile-and-metadata check, and dist build run on Linux only — Windows is functional smoke. Coverage isn't platform- or interpreter-dependent, so neither coverage gate fans out across its respective matrix.

**Performance** (`.github/workflows/perf.yml`, every pull request and push to
`main`, Linux) runs the performance suite in `perf/`: the Rust library, the
Python library and both CLIs timed on the golden recording (`tests/golden/`),
every case's output checked against the golden pins. It fails on a wrong
answer or on a **gate** -- a ratio between two cases of the same run, such as
the Python library decode against the Rust library's, which runner noise
cannot trip the way an absolute time limit would. The full table (raw runs,
phases, import times, totals, records/s, peak memory) goes to the job summary
and the JSON report to an artifact. Run it locally with
`uv --directory python run python ../perf/run.py`; `perf/README.md` explains
the cases, the report and how to calibrate a gate.

A separate scheduled workflow, `.github/workflows/fuzz.yml`, runs a deeper L1-ROB-001 fuzz burn-in daily (and on manual `workflow_dispatch`), across **both** implementations and on **both** Linux and Windows. The normal `rust` / `python` suites run the fixed 256-iteration default; the burn-in sets `MIE_FUZZ_ITERATIONS` (default 25 000) so the deterministic harness sweeps a much larger input space. `docs/FUZZING.md` is the full map of what is fuzzed and what is not; this section covers the CI wiring.

| Workflow / job | Covers | Notes |
|-----|--------------|--------|
| `fuzz-rust` | `ubuntu-latest`, `windows-latest` | Reader, dump and merge harnesses as **separate steps**, so the Actions UI shows pass/fail and duration per harness. |
| `fuzz-python` | `ubuntu-latest`, `windows-latest` | `TestFuzzHarness` plus the merge harness in `test_merge.py`. Runs with `-s` so the harnesses' own stderr reaches the job log the way the Rust one already does. |
| `fuzz-compare` | both implementations, both platforms | Downloads every job's `FUZZ-SUMMARY` artifact and fails if any two disagree. Four runner configurations must produce identical counters. |

All harnesses use the **same xorshift64 generator with the same seed and the same draw order**, so iteration N is the same bytes everywhere. Until v2.15.1 that was as far as it went: nothing compared the results, so the implementations could each prove "I did not crash" while decoding the same bytes differently. Each harness now ends by writing one `FUZZ-SUMMARY` line of counters defined to mean the same thing in every implementation, and `fuzz-compare` diffs them all-pairs (`scripts/compare-fuzz-summaries.py`, same no-majority / no-reference reasoning as `tests/conformance/differential.py`). Every counter must be **path-independent**: the harnesses name their temp files differently, so anything derived from a path measures the harness rather than the decoder. The dump harness counts output *lines* for exactly this reason — it counted bytes first, and the implementations disagreed by a constant offset that turned out to be the length of the file path in the dump header.

**The comparison also runs on every push**, not only in the nightly burn-in: `differential.yml` runs both implementations' harnesses at their default counts into one summary file and compares them. A PR that introduced a divergence would otherwise stay green until the next scheduled run. That is how the four `read_manifest` divergences of v2.16.0 would have been caught the day they landed.

Three environment variables are shared by every harness:

| Variable | Default | Effect |
|---|---|---|
| `MIE_FUZZ_ITERATIONS` | 256 (reader, dump) / 512 (merge) | Inputs to generate. Unparseable or zero falls back rather than guessing. The default is per-harness because they cost different amounts per input; every implementation uses the same default for the same harness, which is what keeps the summaries comparable. |
| `MIE_FUZZ_STREAM_LOGS` | unset (silent) | `1` / `true` leaves the decoder's logger at WARN so diagnostics stream. |
| `MIE_FUZZ_SUMMARY` | unset | File to append the `FUZZ-SUMMARY` line to. |

Two further fuzzers live in `tests/conformance/` and are **differential by construction** — they generate one input, drive it through every implementation's CLI, and compare all-pairs:

| Driver | Generates | Compares | Knobs |
|---|---|---|---|
| `config_fuzz.py` | TOML-ish config documents | accept/reject verdict | `MIE_CONFIG_FUZZ_SEED` / `MIE_CONFIG_FUZZ_ITERS` (default 100) |
| `record_fuzz.py` | recordings built from the committed hex fixtures, then damaged | exit-code class **and CSV bytes** | `MIE_RECORD_FUZZ_SEED` / `MIE_RECORD_FUZZ_ITERS` (default 60) |

`record_fuzz.py` is the one that compares what the **decoders** produce. It is structure-aware on purpose: uniform random bytes reach the recovery paths densely and the valid-record paths almost never — across a 25 000-iteration burn-in they never once produced enough consecutive equal-timestamp records to reach the canonical-order cap branch. It starts from real fixtures, concatenates a few, and applies bit flips, truncations, spliced noise and duplicated slices. On a divergence it prints the input as a ready-to-commit `inputs/*.hex` fixture, because a seed stops reproducing anything the moment the generator changes.

`MIE_FUZZ_STREAM_LOGS` replaced a `--nocapture` / `-s` pair that did not work: Rust's logger writes through `std::io::stderr()` and libtest's capture only intercepts the `print!` / `eprint!` macros (and the former C++ harness wrote to the stderr fd, which Catch2 did not redirect) — so those jobs streamed unconditionally (tens of megabytes into every scheduled run) while pytest captured Python's and showed nothing. Silencing is the harness's job, not the runner's. In Rust the level is set and deliberately *not* restored — libtest runs that binary's tests in parallel threads, so a scope guard would restore the level while a sibling test was still running.

To reproduce locally:

```bash
MIE_FUZZ_ITERATIONS=25000 cargo test --test integration fuzz_arbitrary_bytes_never_panic
MIE_FUZZ_ITERATIONS=25000 uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness
MIE_RECORD_FUZZ_ITERS=500 python tests/conformance/run.py     # the differential one
```

`docs/FUZZING.md` is the authority on which surfaces are covered and which are not; section 5 tracks the remaining gaps.

Pre-commit hooks (set up locally via `bash scripts/install-hooks.sh`, which points `core.hooksPath` at `.githooks/`) run a subset of the above on staged content: trailing-whitespace / CRLF / merge-marker scans, rust/Cargo.lock parity, `python scripts/build-trace-matrix.py --check` (whenever Rust source, Python tests, the L1/L2/L3 docs, or the matrix itself are staged), `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test --all-targets`, a `dbg!()` scan in staged Rust, and a `// SAFETY:` comment requirement for new `unsafe` blocks. These mirror what CI checks so push-fails are rare. The pre-commit hooks do **not** regenerate diagrams or rebuild SVGs, and neither does CI — the `diagrams` job renders to a scratch directory to check the sources parse, but cannot detect a stale committed SVG (§9). Re-render by hand, following §3.

---

## 10. Coverage workflow

Both implementations are gated. Rust uses `cargo-llvm-cov`; Python uses `pytest-cov` (which wraps `coverage.py`). Each gate runs once on Linux only — coverage isn't platform-dependent, so fanning the gate across the full matrix would waste CI minutes.

**Neither includes the conformance suite.** Rust measures its `cargo test` targets, Python measures `python/tests`. The conformance runner drives each CLI out-of-process, so counting it would measure a different thing in each implementation and make the numbers incomparable. Paths reached only through conformance therefore read as uncovered — the conservative direction.

**The second metric is not the same thing in each language, and the floors reflect that.** Rust gates on llvm-cov *regions*, Python on *branches*. Forcing the numbers to match would not make them mean the same thing.

| | lines | regions / branches | floor |
|---|---|---|---|
| Rust | 90.40% | 89.37% (regions) | 90 / 89 |
| Python | 95.50% | 92.89% (branches) | 92 combined |

### Rust

The CI gate is `cargo cov-ci` (alias defined in `rust/.cargo/config.toml`) which fails if line OR region coverage falls below the floors (currently 87 line / 86 region). After the gate passes, CI runs `cargo cov-lcov` and uploads `lcov.info` as the `rust-lcov` artifact.

```bash
cd rust
cargo cov-ci         # what CI runs
cargo cov            # interactive HTML report
cargo cov-lcov       # lcov.info for IDE coverage overlays
```

### Python

The CI gate runs `uv --directory python run pytest --cov --cov-report=term-missing`. Configuration lives in `python/pyproject.toml` under `[tool.coverage.run]` (source set, branch tracking, exclusions) and `[tool.coverage.report]`. The floor is `fail_under = 92` (combined line+branch) in `[tool.coverage.report]` — the single source of truth, so a bare `pytest --cov` enforces it without a CLI flag. `__main__.py` is excluded because it's the `python -m aero1553` entry shim (parallel to Rust's `bin/aero1553.rs` exclusion).

```bash
# What CI runs (use this before pushing)
uv --directory python run pytest --cov --cov-report=term-missing

# HTML report (opens in browser; written to htmlcov/)
uv --directory python run pytest --cov --cov-report=html
```

### Ratcheting the floor

When coverage is consistently above the floor by >2pp, bump it. For Rust, edit the `cov-ci` alias in `rust/.cargo/config.toml`. For Python, edit `fail_under` in `python/pyproject.toml`'s `[tool.coverage.report]` block (the CI job has no `--cov-fail-under` flag — the config value is authoritative). Update the rationale comment in each file when you do.

---

## 11. Releasing

### Rust crate

```bash
cd rust
cargo build --release
```

The resulting binary at `rust/target/release/aero1553` is the deliverable artifact.

### Python package

```bash
uv --directory python lock --check
uv --directory python build   # sdist + one abi3 wheel, via maturin
```

This produces `python/dist/aero1553-<version>.tar.gz` and one `aero1553-<version>-cp310-abi3-<platform>.whl`: a single wheel per platform that serves every CPython from 3.10 up. The sdist is self-contained -- it bundles the core crate from `rust/` -- so it builds anywhere a Rust toolchain is available. A local Linux build is tagged `linux_x86_64`, which pip installs locally but PyPI refuses; publishable `manylinux` wheels come from CI.

No release attaches prebuilt binaries, for any implementation; whether to start doing so is the open Phase 3 question in `ROADMAP.md`. (The C++ implementation, which shipped as source in the joint cut from v2.13.0, was removed in 5.0.0.)

### Version coordination

**A release is a joint cut** — both implementations ship together at one version from a single repository tag. Subsequent releases may diverge in version, but the cross-implementation conformance contract (CSV byte-for-byte equivalence on shared behavior) holds at any compatible version pair.

Tagging scheme:

- **`v1.0.0`** — single tag for the v1.0.0 joint cut. Used because both impls ship simultaneously from one commit.
- **`rust-vX.Y.Z` / `python-vX.Y.Z`** — impl-prefixed tags for future divergent releases. Avoid SemVer-style suffix tags like `v1.0.0-rust` because the hyphen marks a pre-release identifier and tools treat such tags as *less than* `v1.0.0`.

Bump versions when:

- **Rust (`rust/Cargo.toml`)** — any change to the public crate API, the CLI surface, or the on-disk output.
- **Python (`python/pyproject.toml` only)** — same axes for the Python package. `python/src/aero1553/__init__.py::__version__` reads from package metadata via `importlib.metadata.version("aero1553")`, so `pyproject.toml` is where it is read from. The binding crate (`python/native/Cargo.toml`, and its `Cargo.lock` entry) carries the same number because it is compiled into the wheel; `scripts/repo-hygiene.sh` fails the build if any of the version sources disagree. `uv lock --check` catches `pyproject.toml`/`uv.lock` drift in CI.

### CHANGELOG discipline

`CHANGELOG.md` follows the [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) format. The convention:

- **Every commit that introduces a user- or maintainer-visible change adds an entry under `[Unreleased]` in the same commit.** This includes feature additions, behavior changes, bug fixes, error-message changes, exit-code changes, conformance-suite changes that affect the contract, and tooling changes that affect the developer workflow (e.g. new pre-commit hook steps, new CI jobs). Pure internal refactors with zero observable change can be skipped.
- Use the standard categories: `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`. A custom `### Maintenance` subsection is acceptable for genuinely-maintenance entries (e.g. stale doc count updates) that don't fit the standard categories.
- Write entries in the imperative voice describing the *outcome*, not the implementation steps. The git history captures the steps; the CHANGELOG captures the contract.
- At release cut: rename `[Unreleased]` to `[<version>] — YYYY-MM-DD`, leave a fresh empty `[Unreleased]` above it, and update the compare-URL footer.

### Version bump checklist

The version bump itself rolls up the accumulated `[Unreleased]` entries into a dated release section. Bump in the same commit that:

1. Renames `[Unreleased]` to `[<version>] — YYYY-MM-DD` in `CHANGELOG.md` and seeds a new empty `[Unreleased]` section.
2. Updates the compare-URL footer (`[<version>]: .../compare/<previous>...<version>`) — see the warning below; this is the step most often missed.
3. Updates the version in **all five places**, because a joint cut ships one number from one tag:
   `rust/Cargo.toml`, `rust/Cargo.lock` (the `aero1553` entry — a `cargo build` refreshes it),
   `python/pyproject.toml`, `python/native/Cargo.toml`, and `python/native/Cargo.lock` (the
   `aero1553-py` entry).

   > Until 5.0.0 the C++ implementation added three more places (its `kVersion`, its CMake
   > project version and two CI smoke assertions); the v2.13.0 cut failed CI on the smoke
   > assertions after the checklist missed them. `repo-hygiene.sh` fails when the version
   > sources disagree, so the checklist and the gate say the same thing.
4. Updates any per-version doc references (e.g. "X-test suite (as of vN.M.0)" in MAINTAINER-GUIDE.md §10).

> **Watch the footer (step 2).** This step was silently skipped on the `1.4.0` and `1.4.1` cuts: the body sections were dated correctly but the footer's `[Unreleased]` link was left pointing at `v1.3.0...HEAD` and no `[1.4.0]`/`[1.4.1]` entries were added. It was repaired during the `1.5.0` cut. The body roll-up (step 1) is visible in the rendered changelog so it's hard to forget; the footer is easy to miss because nothing breaks without it. When cutting, after editing, confirm the footer's `[Unreleased]` line points at the *new* version (`compare/v<new>...HEAD`) and that every released version since the last footer update has its own `compare/<prev>...<this>` line — `git tag --sort=-creatordate` is the cross-check.

The CHANGELOG entry, the version bump, and any user-visible behavior changes all land together so a tag points at a coherent unit of release.

---

## 12. Cross-implementation alignment principles

These are the operating rules that keep the two crates from drifting:

1. **Spec first.** New behavior lands as an L2 / L3 requirement before code. Both implementations then satisfy it.
2. **Conformance fixtures for cross-impl behavior.** Anything that affects CSV output or exit codes belongs in `tests/conformance/`.
3. **Per-impl detail goes in L3.** Python-specific constraints (the compiled extension, uv + maturin) live as `L3-PY-*`. Rust-specific constraints (memmap2, BufWriter) live as `L3-RS-*`. The shared L2 stays implementation-agnostic.
4. **Error variants ship together.** When you add a new variant in one language, add it in the other in the same PR.
5. **What an operator reads to fix their command is a contract; the decoder's narration is not.** Configuration errors, the unknown-key WARN, usage errors, and the help and version text read the same from every implementation, byte for byte and line ending included (L2-CLI-022). The config differential checks and `tests/conformance/cli_message_parity.py` compare them, paths aside. The help text is Rust's `HELP`, which the Python CLI runs. Quote a value the operator wrote with `text::quote` -- never `{:?}` or a hand-built `"\"" + s + "\""` -- so a non-ASCII byte or an embedded quote is shown the same way everywhere. The decoder's own INFO and DEBUG lines (record traces, progress, detection scores) may still be worded per implementation. This rule replaced "log wording can drift", under which the two CLIs' config errors had come to differ for 60 of 79 corpus snippets and one printed a rejected value as an empty string.
6. **CLI capability parity is the contract** (per L1-CLI-001) — capability parity matters, exact spelling doesn't. Today the two CLIs share one identical argument surface: the same subcommands (`decode` / `count` / `dump`), the same `--separate-errors` flag, the same global `--config`, and the same comma-separated filter syntax. They are free to diverge in spelling so long as capability parity holds.
7. **`memmap2` is the only Rust runtime dep.** Argument parsing, CSV writing, TOML loading, logging, and error types are all hand-rolled. Adding a crate requires explicit justification — see `docs/ROADMAP.md` and `CLAUDE.md` "Conventions worth preserving".

---

## 13. Quick links

- [`CLAUDE.md`](../CLAUDE.md) — project conventions for LLM sessions
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — module-level architecture
- [`docs/ERROR-CATALOG.md`](ERROR-CATALOG.md) — every error / exit code
- [`docs/L1-REQ.md`](L1-REQ.md) / [`L2-REQ.md`](L2-REQ.md) / [`L3-REQ.md`](L3-REQ.md) — spec
- [`docs/TRACE-MATRIX.md`](TRACE-MATRIX.md) — auto-generated trace matrix
- [`tests/conformance/README.md`](../tests/conformance/README.md) — conformance suite manifest schema
- [`config/default.toml`](../config/default.toml) — fully-commented reference TOML
