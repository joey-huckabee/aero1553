# Contributing to Aero1553

Thanks for working on Aero1553. This repository contains maintained Rust
and Python implementations. This document covers local setup, the pre-commit
workflow, and commit conventions.

> Note: the canonical filename for this document is `CONTRIBUTING.md`
> (Git/GitHub convention). If you arrive here looking for `CONTRIBUTION.md`,
> this is the same file.

## Prerequisites

- Rust toolchain ≥ 1.88 (`rustup toolchain install stable`). The crate
  uses edition 2024, which floors at 1.85; the 1.88 requirement comes from
  the crate's own use of let-chains (see `L3-RS-001`), not from `memmap2`.
- Python 3.10 or newer and [uv](https://docs.astral.sh/uv/) for work under
  `python/` (the package compiles a PyO3 extension, so this needs the Rust
  toolchain above as well). Commands in
  this repo's docs are written `python …`; read that as *your Python 3
  interpreter*. Many Linux distributions and WSL images provide only
  `python3`, and nothing here depends on the `python` alias existing —
  `.githooks/pre-commit` and `scripts/repo-hygiene.sh` probe for
  `python3`, `python`, then `py` and use whichever actually runs Python 3.
- A Bash shell. On Windows, Git for Windows ships **Git Bash**, which
  Git invokes for hooks transparently — no extra setup.

## One-time setup: install the pre-commit hook

The repo carries a pre-commit hook at `.githooks/pre-commit`. Activate
it on your clone with:

```bash
bash scripts/install-hooks.sh
```

This sets `core.hooksPath` to `.githooks/` and marks the hook
executable. You only do this once per clone.

To verify:

```bash
git config core.hooksPath
# → .githooks
```

The equivalent direct command (if you'd rather not run the script):

```bash
git config core.hooksPath .githooks
```

## What the hook checks

On every `git commit`, the hook runs (in order, failing fast). The
checks are split into a file-level group that runs on every commit
and a Rust group that runs only when `.rs` or `.toml` files are
staged.

### File-level (always)

1. **Whitespace** — `git diff --cached --check`. Reports `file:line` for
   trailing whitespace, space-before-tab, a *new blank line* at EOF, and
   (as a side-effect) leftover merge conflict markers. It does **not**
   report a *missing* final newline — `--check` implements
   `core.whitespace`, whose `blank-at-eof` is the opposite problem. That
   is check 2.
2. **Missing final newline** — every staged text file must end in `\n`.
   Added in v2.12.0, when check 1 was found to have never enforced the
   guarantee its description claimed.
3. **CRLF line endings** — staged text files must be LF-only.
   Belt-and-suspenders alongside `.gitattributes` if you add one.
4. **Merge conflict markers** — explicit scan for `<<<<<<<`, `=======`,
   `>>>>>>>` in staged blobs. (`git diff --cached --check` also
   catches these; the dedicated check is in case `--check` is ever
   bypassed for one path.)
5. **Large file guard** — staged files over 1 MB are rejected.
   Catches accidental binary commits (`git add -f` on a `*.mie`
   recording, etc.). Use git-lfs or extend `.gitignore` if you
   genuinely need a large file.
6. **`*.mie` recordings** — defense-in-depth on top of `.gitignore`.
   Sample binaries shouldn't be committed.
7. **`rust/Cargo.lock` parity** — if `rust/Cargo.toml` is staged, `rust/Cargo.lock`
   must also be staged (or already match). Catches the common
   "bumped a dep version, forgot to commit the lock update" mistake.
   Uses `cargo metadata --locked --offline` to confirm.
8. **`shellcheck` on hooks/scripts** — runs only if `shellcheck` is
   installed. Lints the hook itself and `scripts/*.sh`. Skipped
   silently if the tool isn't on `$PATH`.
9. **Requirements trace matrix** — `python scripts/build-trace-matrix.py
   --check`. Fails if `docs/TRACE-MATRIX.md` is stale relative to the
   L1/L2/L3 docs and the test markers. Regenerate with the same command
   minus `--check`.

### Rust-only (skipped if no `.rs`/`.toml` staged)

10. **`cargo fmt --check`** — formatting is consistent. Fix locally
   with `cargo fmt`, then re-stage.
11. **`cargo clippy --all-targets -- -D warnings`** — all clippy lints
   pass with warnings treated as errors. Either fix the lint or
   justify the suppression with a scoped `#[allow(...)]` and a
   comment explaining why.

   **`-D warnings` alone is weaker than it looks.** It only denies lints
   that actually *fire*, and clippy's `pedantic`, `nursery`,
   `restriction` and `cargo` groups are **allow-by-default** — so they
   never fire and the gate never sees them.

   That gap was visible from outside: SonarCloud's Rust rules map onto
   clippy's *including* those tiers, so it reported findings `cargo
   clippy` called clean. The tools were not disagreeing; they were
   running different lint sets.

   `rust/Cargo.toml`'s `[lints.clippy]` closes it. **The whole `pedantic`
   group is denied** (`priority = -1`, so the per-lint `allow`s below it
   win), plus `undocumented_unsafe_blocks` from `restriction`. `nursery`
   stays off — see the `cognitive_complexity` note in that file for why
   matching Sonar's threshold buys a stricter local rule rather than a
   predictive one.

   Two consequences worth knowing before you hit them:

   * **A clippy upgrade can turn this red on code you did not touch**,
     because a new release can add pedantic lints. That is the intended
     trade — it surfaces on a PR rather than in a release — but the right
     fix is sometimes a documented entry in the exception list rather
     than a change to the code.
   * **Every exception carries its reason in `[lints.clippy]`.** Six
     lints are allowed wholesale, each because every site in this crate
     is deliberate (format-specification tables, options structs taken by
     value on purpose, flag-bag structs that mirror the CLI and TOML
     surfaces 1:1). Narrow one-site exceptions use a scoped
     `#[allow(..., reason = "...")]` instead.

   When adding a scoped `#[allow]` above a `#[test]`, keep it on **one
   line** if the test carries a `/// Requirements:` marker, or make sure
   `python scripts/build-trace-matrix.py --check` still passes — the
   collector understands multi-line attributes, but the marker must still
   pair with its `fn`.

   **Run `rustup update stable` before trusting a local clippy run.** The
   repo pins no `rust-toolchain` file, so CI floats on current stable. A
   local toolchain that has fallen behind runs a genuinely *weaker* gate
   than CI — not a differently-configured one — because clippy widens
   existing lints between releases. This is not hypothetical: the commit
   that enabled the `pedantic` group passed a clean local `-D warnings`
   on 1.93 and failed CI on 1.98, where `map_unwrap_or` had grown to
   cover `Result` as well as `Option`. Two call sites the local run could
   not see.
12. **`cargo test --all-targets` + `cargo test --doc`** — all unit,
    integration **and documentation** tests pass. `--all-targets`
    excludes doctests, so the hook runs them as a second invocation;
    CI mirrors this at `ci.yml`'s `cargo test --locked --doc` step.
13. **`dbg!()` scan** — staged `.rs` files do not contain forgotten
    `dbg!` macros. (`todo!` and `unimplemented!` are sometimes
    intentional placeholders, so they're not blocked — but you'll
    see them in code review.)
14. **`unsafe` blocks require `// SAFETY:`** — every `unsafe { ... }`
    or `unsafe fn` in a staged `.rs` file must have a comment
    containing `SAFETY:` within the three preceding lines. Catches
    new unsafe code added without justifying its invariants.

If only docs are staged (no `.rs` or `.toml` files), the cargo group
is skipped — doc-only commits are fast.

### A note on `unwrap()` / `expect()`

We don't currently grep for `.unwrap()` calls in pre-commit. There
are two reasons:

1. **False-positive heavy in tests.** Test code legitimately uses
   `unwrap()` because panic-on-failure *is* the desired behavior.
2. **Better tool exists.** The clippy lints `clippy::unwrap_used`
   and `clippy::expect_used` flag every call and force a per-site
   `#[allow(clippy::unwrap_used)]` annotation, which doubles as
   documentation of *why* the unwrap is safe.

Production crates enable both lints outside `cfg(test)`. Test code may
continue to use `unwrap()` / `expect()` because panic-on-failure is the
intended assertion behavior. New production uses must be rewritten to
return a defensive error or carry a narrow documented lint allowance.

### Bypassing the hook

`git commit --no-verify` skips the hook. Reserve this for genuine
emergencies.

CI does back the hook up, but the two are not identical and it is worth
knowing which is which:

| Hook check | Backed by |
|---|---|
| Whitespace (`--check`) | `repo-hygiene` (final newline), plus `cargo fmt` for Rust |
| Missing final newline, CRLF, merge markers, large file, `*.mie`, `Cargo.lock` parity, `dbg!()`, `unsafe`/`SAFETY:` | **`repo-hygiene`** job |
| Non-ASCII in a shipped string literal (L2-CLI-014) | **`repo-hygiene`** job |
| Trace matrix | `trace-matrix` job |
| `cargo fmt` / `clippy` / tests / doctests | `rust` job |
| `shellcheck` | *nothing* — hook-only, and skipped there too unless installed |

The `repo-hygiene` job runs `scripts/repo-hygiene.sh`, which re-applies
the file-level checks to the whole tracked tree rather than to a diff, so
it catches anything already committed however it got there. Run it
locally the same way:

```bash
bash scripts/repo-hygiene.sh
```

That job was added in v2.12.0. Before it, this section claimed "CI runs
the same checks and will fail the merge anyway", which was untrue for
nine of the hook's fourteen checks — a `--no-verify` commit could land a
CRLF file, a stray `dbg!()`, a merge marker, an oversized blob or a
committed `*.mie` recording with nothing downstream to catch it.

### Why these checks (and not others)

- `cargo doc` is **not** in the hook because it's slow and rarely
  catches issues clippy doesn't already catch. Worth running manually
  before publishing a release.
- `cargo build --release` is **not** in the hook because debug builds
  exercise the same code path. Release builds are a release-time concern.
- `cargo audit` (CVE check) is **not** wired up because we have a
  single dependency. Revisit if the dep tree grows.

## Daily commands

Rust:

```bash
cd rust
# Build
cargo build               # Dev
cargo build --release     # Optimized

# Test
cargo test                                                # Everything
cargo test --lib                                          # Unit tests only
cargo test --test integration                             # Integration only
cargo test --test integration -- multi_record_stream      # Single integration test
cargo test config::tests::parses_default_toml_from_disk   # Single unit test

# Format / lint
cargo fmt                                  # Auto-format
cargo fmt --check                          # CI-style check (no rewrites)
cargo clippy --all-targets -- -D warnings  # Lint manually
```

Python:

```bash
uv --directory python sync
uv --directory python run pytest
uv --directory python run pylint src/aero1553      # lint (CI-gated, must stay 10/10)
uv --directory python run ruff check               # ruff lint (CI-gated)
uv --directory python run ruff format              # auto-format (CI runs ruff format --check)
uv --directory python run vulture                  # dead-code scan (CI-gated)
uv --directory python run bandit -r src/aero1553     # security scan / SAST (CI-gated)
uv --directory python run aero1553 --help
uv --directory python build   # sdist + one abi3 wheel, via maturin
```

Shared cross-implementation conformance:

```bash
uv --directory python run python ../tests/conformance/run.py
```

The runner defaults to **every** registered implementation and fails if one is
missing, so leaving one out is explicit — `--skip cpp` when you have no C++
build, `--only cpp` to check just that one. The alternative, running whichever
binaries happen to be present, would let the suite report a full pass after a
build step had quietly failed.

Run it **through uv**. The runner drives the Python CLI with
`sys.executable` — the interpreter it is itself running under — so a bare
`python tests/conformance/run.py` uses your system Python, which does not
have `aero1553` after a `uv --directory python sync` (uv installs into its
own virtualenv, `python/.venv`). It fails fast and tells you so, but the uv
form is the one that works. CI runs the bare form only because it does
`pip install -e ./python` into the runner's system interpreter first.

`--only rust` is the exception: it never touches the Python side, so plain
`python tests/conformance/run.py --only rust` is fine. (`--rust-only` and
`--python-only` still work as deprecated aliases.)

The conformance runner materializes text-based hexadecimal fixtures, invokes
each CLI, and compares their CSV output byte-for-byte against checked-in
oracles. Use `--update-expected` only for intentional shared-output changes;
the runner updates an oracle only after **every** implementation already
agrees, so no single one can ratify a change to the shared contract.

An implementation still being delivered in phases declares which cases it
cannot run, and the runner **names each one it skips**. That is deliberate: a
skip that printed nothing would read as coverage.

When both CLIs are present, the same runner also cross-checks the two **config
parsers** — a curated corpus (`tests/conformance/config_parity.py`) plus a
differential **fuzzer** (`config_fuzz.py`) that generates config documents and
asserts both implementations agree on accept/reject. No separate command; it
runs inside `run.py`. For a deeper local sweep, raise the iteration count (this
is a *different* knob from the reader/dump `MIE_FUZZ_ITERATIONS` below):

```bash
MIE_CONFIG_FUZZ_ITERS=5000 uv --directory python run python ../tests/conformance/run.py
# optionally pin a starting point: MIE_CONFIG_FUZZ_SEED=<n>
```

A divergence prints the exact config; pin it in `config_parity.py`, then fix.

The current pre-commit hook runs the Rust checks documented above. Run the
Python tests manually when changing `python/`.

## VS Code setup

The repo ships a `.vscode/` folder so the editor matches the CI gates out of the
box: `settings.json` (workspace settings) and `launch.json` (debug configs).

### Workspace settings (`settings.json`)

Recommended extensions to get the full benefit:

- **rust-analyzer** (`rust-lang.rust-analyzer`)
- **CodeLLDB** (`vadimcn.vscode-lldb`) — Rust debugging
- **Python** + **Python Debugger** (`ms-python.python`, `ms-python.debugpy`)
- **Ruff** (`charliermarsh.ruff`) — Python format + lint

What the settings do:

- **rust-analyzer** is pointed at `rust/Cargo.toml` (there is no root
  `Cargo.toml`) and runs `clippy --all-targets` for on-save diagnostics, matching
  CI. Format-on-save uses rustfmt.
- **Python** format-on-save uses Ruff (matching the `ruff format --check` gate),
  and test discovery is pytest run from `python/`.
- `files.insertFinalNewline` / `trimTrailingWhitespace` are deliberately **off**:
  the conformance suite compares `.csv` oracles and `.hex` fixtures byte-for-byte,
  so a global whitespace fixup on save could silently break an oracle. The
  pre-commit hook already enforces these on staged source.

### Debug configurations (`launch.json`)

`launch.json` ships nine debug configurations covering both implementations.

**Rust** (needs the **CodeLLDB** extension — `vadimcn.vscode-lldb`):

- `Rust: decode <file> -> CSV`
- `Rust: count <file>`
- `Rust: dump <file>`
- `Rust: debug library unit tests`

Each uses CodeLLDB's cargo integration — it builds the `aero1553` bin from
`rust/Cargo.toml` and launches it under the debugger, so no `tasks.json` is
needed.

**Python** (needs the **Python** + **Python Debugger** extensions):

- `Python: decode <file> -> CSV`
- `Python: count <file>`
- `Python: dump <file>` (these run `python -m aero1553`)
- `Python: pytest (current file)`
- `Python: pytest (all)`

The Python configs set `justMyCode: false` (so you can step into `aero1553`),
run from `python/` for pytest, and rely on the interpreter selected in VS Code.

Two things to know:

- **Python interpreter** — run **"Python: Select Interpreter"** and pick the
  uv venv at `python/.venv` so `aero1553` is importable. The config uses
  the *selected* interpreter rather than a hardcoded path, so it stays portable
  across Windows / macOS / Linux.
- **Input prompts** — the `decode` / `count` / `dump` configs prompt for the
  `.mie` recording path (and output CSV) at launch via `${input:mieFile}` /
  `${input:outCsv}`. Edit their `default` values in the `inputs` block at the
  bottom of `launch.json` to skip retyping.

## Fuzz testing

Each implementation carries a deterministic fuzz harness asserting the
**L1-ROB-001** robustness contract: arbitrary input bytes must never panic
(Rust) or raise anything other than a documented `Aero1553Error` / `MieError`
(Python, C++). All of them are seeded from the same `xorshift64` PRNG, with the
same seed, size bands and draw order, so iteration N is the same bytes in every
implementation.

`docs/FUZZING.md` is the full map — which surfaces are fuzzed, by which
implementation, what is deliberately not fuzzed yet, and the rule that governs
the area (**a fuzz surface is exercised by all three implementations or by
none**). What follows is how to run them.

| Harness | Test |
|---------|------|
| Rust reader | `rust/tests/integration.rs::fuzz_arbitrary_bytes_never_panic` |
| Rust dump | `rust/tests/integration.rs::dump_arbitrary_bytes_never_panics` |
| Python reader | `tests/test_e2e.py::TestFuzzHarness::test_arbitrary_bytes_never_raise_unexpected_exceptions` |
| Python dump | `tests/test_e2e.py::TestFuzzHarness::test_dump_arbitrary_bytes_never_raise_unexpected_exceptions` |
| Rust merge | `rust/tests/integration.rs::merge_input_resolution_tolerates_arbitrary_bytes` |
| Python merge | `tests/test_merge.py::test_merge_input_resolution_tolerates_arbitrary_bytes` |
| C++ (all three) | `cpp/tests/test_fuzz.cpp`, tagged `[fuzz]` |

Two further fuzzers live in `tests/conformance/` and are **differential by
construction** -- they generate one input, drive it through every
implementation's CLI, and compare all-pairs: `config_fuzz.py` over config
documents, and `record_fuzz.py` over recordings built from the committed hex
fixtures and then damaged. `record_fuzz.py` is the only check anywhere that
compares what the three *decoders* produce on input nobody wrote.

Run them (default 256 iterations):

```bash
# Rust (from rust/)
cargo test --test integration fuzz_arbitrary_bytes_never_panic
cargo test --test integration dump_arbitrary_bytes_never_panics

# Python (whole class = both reader + dump)
uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness

# C++ (the [fuzz] cases only; they also ride along in `make check`)
make -C cpp check-fuzz

# The differential fuzzers, across every implementation at once
python tests/conformance/run.py
```

### The three shared knobs

Every harness reads the same three environment variables and means the same
thing by each. That is deliberate: a burn-in scoped differently per language
cannot be compared across languages.

| Variable | Default | Effect |
|---|---|---|
| `MIE_FUZZ_ITERATIONS` | 256 | Inputs to generate. Unparseable or zero falls back rather than guessing. |
| `MIE_FUZZ_STREAM_LOGS` | unset (silent) | `1` / `true` leaves the decoder's logger at WARN so its diagnostics stream. |
| `MIE_FUZZ_SUMMARY` | unset | File to append the run's `FUZZ-SUMMARY` line to. |

The scheduled [`.github/workflows/fuzz.yml`](.github/workflows/fuzz.yml) job
runs 25 000 iterations daily, across all three implementations on both Linux and
Windows. The PRNG is deterministic, so a burn-in is a strict superset of the
default run (same first 256 inputs); a failure prints the reproducer seed.

```bash
(cd rust && MIE_FUZZ_ITERATIONS=25000 cargo test --test integration fuzz_arbitrary_bytes_never_panic)
MIE_FUZZ_ITERATIONS=25000 uv --directory python run pytest -s tests/test_e2e.py::TestFuzzHarness
MIE_FUZZ_ITERATIONS=25000 make -C cpp check-fuzz
```

On Windows PowerShell set the variable separately: `$env:MIE_FUZZ_ITERATIONS =
"25000"` (and `Remove-Item Env:\MIE_FUZZ_ITERATIONS` after).

### The summary line

Every harness ends by writing one `FUZZ-SUMMARY` record — inputs, bytes
generated, readers opened, records yielded, errors — with the same shape in all
three implementations. Point them all at one file and compare:

```bash
export MIE_FUZZ_SUMMARY=/tmp/fuzz-summary.txt && rm -f "$MIE_FUZZ_SUMMARY"
(cd rust && cargo test --test integration -- fuzz_arbitrary_bytes_never_panic dump_arbitrary_bytes_never_panics)
uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness
make -C cpp check-fuzz
mkdir -p /tmp/fz/local && cp "$MIE_FUZZ_SUMMARY" /tmp/fz/local/
python scripts/compare-fuzz-summaries.py /tmp/fz
```

On identical inputs the counters must be identical. The burn-in's
`fuzz-compare` job does exactly this across every implementation and both
platforms, and fails if any two disagree. Any counter added here must be
**path-independent** — the harnesses name their temp files differently, so
anything derived from a path measures the harness rather than the decoder.

### Output model (where the WARN noise comes from)

All the harnesses route diagnostics through the logger, and all of them are
noisy on random input:

- **Reader harnesses** emit WARN/ERROR for sync recovery and invariant
  rejection.
- **Dump harnesses** emit a WARN for each record-aware scan-stop anomaly —
  invalid `word_count`, truncated record, offset overflow (L2-CLI-013) — in
  addition to the inline `!! ...` note in the hex report (the report itself goes
  to a throwaway sink in the fuzz tests, so you see the WARNs, not the report).

**The test runners do not control this, which is why `MIE_FUZZ_STREAM_LOGS`
exists.** Rust's logger writes through `std::io::stderr()` and libtest's capture
only intercepts the `print!` / `eprint!` macros; the C++ logger writes to the
stderr file descriptor, which Catch2 does not redirect. So `--nocapture` is a
no-op for both and their output used to stream unconditionally — tens of
megabytes per burn-in — while pytest, which *does* capture, showed nothing at
all. The harnesses now silence the logger themselves by default and turn it back
on for `MIE_FUZZ_STREAM_LOGS=1`. Under pytest you still need `-s` on top of the
variable, since pytest captures what the harness emits either way.

## Continuous integration

GitHub Actions runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml) on
every push and pull request:

- **Rust:** `cargo fmt --check`, Clippy with warnings denied, all-target tests,
  and the `cargo cov-ci` 90% line / 89% region coverage gate.
- **Python 3.10 through 3.14:** locked dependency synchronization and the full
  pytest suite on every supported minor version.
- **Python 3.12:** strict package/lockfile validation and wheel + source
  distribution builds.
- **Rust/Python conformance:** both CLIs decode the shared fixtures and must
  produce byte-identical CSV matching the checked-in oracles.

The Python matrix makes the `>=3.10,<3.15` compatibility declaration
enforceable. In particular, Python 3.10 exercises the `tomli` compatibility
path while newer versions use the standard-library `tomllib`.

## Coverage

We use [`cargo-llvm-cov`](https://github.com/taiki-e/cargo-llvm-cov)
for source-based code coverage. It works on stable Rust (no nightly
needed — `-C instrument-coverage` has been stable since Rust 1.60).

### One-time install

```bash
rustup component add llvm-tools-preview
cargo install cargo-llvm-cov
```

### Daily use

Three cargo aliases are pre-wired in `rust/.cargo/config.toml`:

```bash
cd rust
cargo cov         # Local: build instrumented, run tests, open HTML report
cargo cov-lcov    # Generate target-relative lcov.info (for IDE / CI tooling)
cargo cov-ci      # Enforced gate: --fail-under-lines 87 --fail-under-regions 86
```

Or via the script wrapper, equivalent to `cargo cov`:

```bash
bash scripts/coverage.sh
```

### Thresholds

`cargo cov-ci` enforces:

- **Lines: 90%** floor
- **Regions: 89%** floor

These have been ratcheted up from the original 70/70 floor to roughly
two percentage points below the current baseline, so routine refactors
don't trip the gate while genuine coverage regressions do. Ratchet up
further by editing the `cov-ci` alias in `rust/.cargo/config.toml` — do it
in increments after watching baseline readings stabilize.

### Why coverage is NOT in the pre-commit hook

Building an instrumented test binary takes much longer than a normal
`cargo test`. The pre-commit hook is meant to be fast (under a few
seconds for a small change). CI enforces `cargo cov-ci` on every push
and pull request; run it locally before pushing material changes.

### Line-level exclusions

cargo-llvm-cov supports file-level exclusion via `--ignore-filename-regex`
(used in our aliases to skip the binary entry shim). **Line-level
exclusion** (the `#[coverage(off)]` attribute) is **nightly-only** at
present, so `unreachable!()` arms and other defensive branches show as
uncovered on stable. Either accept the percentage hit or refactor the
defensive arm out — don't try to game the threshold.

## Commit conventions

We use [Conventional Commits](https://www.conventionalcommits.org/)
prefixes:

| Prefix | Use for |
|--------|---------|
| `feat(<scope>):` | New feature or capability |
| `fix(<scope>):` | Bug fix |
| `chore:` | Version bumps, repo hygiene, non-code maintenance |
| `docs:` | Documentation only |
| `test:` | Test changes only |
| `build:` | Build system / dependency changes |
| `refactor:` | Code change without behavior change |

Examples from this repo:

- `feat(reader): port mmap-backed iterator with sync recovery`
- `fix(reader): apply full sync::validate_record path per record`
- `docs(roadmap): catalogue robustness corner cases for future work`
- `chore: rename Python project directory to python/`

Body conventions:

- Lead with the **why**, not the **what** — the diff already shows
  what.
- Wrap at ~72 columns.
- Co-author trailers (e.g., from pair programming) at the bottom.

## Code conventions worth preserving

These are codified in `CLAUDE.md`; the highlights:

- **Single external dependency.** Only `memmap2`. Adding crates
  requires justification — argument parsing, CSV, TOML, logging,
  error types are all hand-rolled by design.
- **Streaming CSV.** Don't introduce `Vec<MieMessage>` or row-level
  buffering in `writer.rs` — constant memory is the design point.
- **`DataWords` is fixed-capacity.** MIL-STD-1553B caps a transaction
  at 32 data words. Don't switch to `Vec<u16>` "for flexibility."
- **N-record look-ahead in `sync.rs`** (default 2, configurable per
  L2-SYN-026). Removing it reintroduces false-positive resyncs.
- **One validation path.** Header skip, normal forward decode, and
  recovery all share `sync::validate_record`. There is no weaker
  fast path.
- **`sync.rs` is pure.** No logging, no I/O. The reader handles
  user-facing diagnostics based on returned values.
- **CSV columns match DDC vendor output byte-for-byte.** Don't reorder
  or rename, including currently-empty columns
  (`MUX`, `TERM_NAME`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`).
- **Test fixtures are byte-exact** translations of records cross-
  referenced against vendor CSV. Treat them as oracles; if a test
  fails, suspect the code first.
- **All three implementations are maintained.** Changes under `rust/src/` and
  `rust/tests/` apply to Rust; `python/` to Python; `cpp/` to C++. Preserve
  shared MIE format semantics and vendor-compatible CSV behavior across all
  three.

## Working in the C++ tree

The C++ implementation targets **C++11 as accepted by GCC 4.8.5**, the SLES 12
SP5 system compiler (ADR-0001). The build and CI arrangement is explained in
`cpp/README.md`; this section covers the two things that will bite you if
nobody tells you first.

### Two C++14 constructs that MSVC accepts and GCC 4.8.5 rejects

MSVC has no `/std:c++11` — its floor is C++14 — so the Windows build compiles
C++11-conformant source as C++14. That is valid, but it means MSVC silently
accepts two things the target compiler refuses. Both produce a build that is
**green on Windows and red on Linux**, discovered by CI rather than by you:

1. **Aggregate-initializing a class that has a default member initializer.**

   ```cpp
   struct Options { int records = 8; };
   Options o{16};   // well-formed C++14, ill-formed C++11
   ```

   In C++11 the NSDMI makes `Options` a non-aggregate. Rule: a struct either
   has default member initializers **or** is brace-initialized, never both —
   give it a constructor if it needs one.

2. **Passing a `const_iterator` to a container mutator.** libstdc++ 4.8 still
   declares the pre-C++11 `iterator` signatures for `erase` and `insert`; the
   C++11 signatures landed in GCC 4.9.

Also banned outright: `<regex>` (libstdc++ had no working implementation until
GCC 4.9), `std::make_unique`, `std::optional`, `std::string_view`,
`std::filesystem`, `std::is_trivially_copyable`, and `std::put_time` /
`std::get_time` — all C++14-or-later, or missing from libstdc++ 4.8.

**Run `make check-gcc48` before pushing.** It builds and runs the *full* suite
inside the pinned GCC 4.8.5 image, which is the only thing that catches the
above. A compile-only check is not enough and the tier is deliberately not
reduced to one.

### The platform layer is the only thing that touches the OS

Five concerns live behind `cpp/include/mie/platform.hpp` — mapping the input,
the atomic temp-file-and-rename output, directory enumeration, binary stdout,
and path identity. No other file may include `<windows.h>`, `<sys/mman.h>` or
their neighbours; `scripts/assert-platform-confined.sh` fails the build if one
does.

If you need a new OS capability, add it to the header and implement it in
**both** backends. Windows is a shipping target, not a build that happens to
link (ADR-0003), and a capability implemented on one side only is a runtime
failure on the other rather than a compile error.

Related: the program must never call `setlocale` and must not use the
`<cctype>` classification functions — the `DELTA` column is formatted `%.6f`,
whose decimal separator the locale chooses, so a locale change turns
`1.234500` into `1,234500` and breaks every conformance oracle.
`scripts/assert-locale-free.sh` enforces this.

### Adding a source file

Add it to `cpp/sources.txt` — once. Both builds read that file, because the
GCC 4.8.5 fidelity container has **no CMake at all** and cannot install one
(Debian 7's repositories are archived), so the Makefile has to stay
authoritative on Linux. `scripts/assert-sources-agree.sh` compares what each
build actually resolves.

Test files are globbed rather than listed, deliberately: a source file left out
of the build fails loudly at link time, whereas a **test** file left out simply
never runs.

### Tagging a test with a requirement

Requirement ids go in the Catch2 tag string, so the trace matrix can find them
and so the artifact it names is directly runnable:

```cpp
TEST_CASE("AtomicFile replaces an existing destination", "[atomic][L3-CPP-006]") {
```

```bash
./build/<toolchain>/aero1553_tests "[L3-CPP-006]"
python scripts/build-trace-matrix.py --check
```

## Reporting issues / proposing changes

For non-trivial changes:

1. Open an issue describing the change first.
2. If it touches a known gap or planned refinement, link to the entry in
   `docs/ROADMAP.md` (see its "Decode correctness" / "Merge follow-ups"
   sections).
3. Keep the PR focused — one feat/fix per PR.

For trivial doc fixes or single-line bug fixes, a PR without a prior
issue is fine.
