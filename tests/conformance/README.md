# Cross-Implementation Conformance

This suite verifies behavior shared by the Rust, Python and C++
implementations. The runner defaults to **every** registered implementation and
fails if one is missing, so a case cannot quietly test fewer than all three.
Each case provides:

- a text-based hexadecimal MIE input under `inputs/`;
- optional shared TOML configuration under `configs/`;
- expected vendor-compatible CSV output under `expected/`; and
- optional extra CLI arguments in `manifest.json` (the `args` field), passed verbatim to all three CLIs — they share one identical argument surface, which the `cli-surface-parity` gate compares across every implementation.

The runner materializes temporary `.mie` files, invokes both CLIs, and requires
both outputs to match the checked-in CSV oracle byte-for-byte.

Run from the repository root, **through uv**:

```bash
uv --directory python run python ../tests/conformance/run.py
```

The runner drives the Python CLI with `sys.executable` — the interpreter it is
itself running under. A bare `python tests/conformance/run.py` therefore uses
your system Python, which does not have `aero1553` after `uv --directory python
sync` installs it into uv's virtualenv (`python/.venv`). The runner probes for the import and
fails fast with the fix, but the form above is the one that works. (CI uses the
bare form because it `pip install -e ./python` into its system interpreter
first.) `--python-bin <path>` overrides the interpreter explicitly.

To use an already-built Rust binary:

```bash
uv --directory python run python ../tests/conformance/run.py --rust-bin ../rust/target/debug/aero1553
```

Two path traps here, both easy to hit:

- `uv --directory python run` executes with the working directory set to `python/`,
  so **every relative path is relative to `python/`** — hence the `../` on both
  the script and `--rust-bin`.
- `--rust-bin` is used exactly as given; the runner only appends `.exe` when it
  locates the binary itself. On Windows pass
  `../rust/target/debug/aero1553.exe`, or omit the flag and let the runner
  find it. A path that does not exist is reported as `failed to build the Rust
  CLI`, which names the symptom rather than the cause.

### Single-implementation runs (one toolchain)

Because the `expected/` oracles are committed, each implementation can be
validated on its own — useful where only one toolchain is available (e.g. an
air-gapped host with Python but no Rust/cargo). These skip the other
implementation's setup and the cross-impl CLI-surface check; each side is still
held to the same byte-exact oracle.

```bash
uv --directory python run python ../tests/conformance/run.py --python-only   # no cargo needed
python tests/conformance/run.py --rust-only --rust-bin <path>           # no aero1553 package needed
```

(`--update-expected` still requires both implementations, since it regenerates
the oracles only after confirming Rust and Python agree.)

When intentionally changing shared CSV behavior, update the checked-in
oracles only after both implementations produce identical output:

```bash
uv --directory python run python ../tests/conformance/run.py --update-expected
```

Keep implementation-specific CLI behavior in each implementation's own test
suite. Add cases here only for shared MIE decoding and CSV semantics.

## Config-parser parity

The two implementations parse config with different engines (`tomllib` vs the
minimal Rust parser), so `run.py` cross-checks them when both are present:

- **`config_parity.py`** — a *curated* corpus of TOML snippets, each labeled
  `accept` / `reject`. Every snippet is run through both CLIs; they must land in
  the same class and match the label. Add one whenever a new form could diverge.
- **`config_fuzz.py`** — a differential *fuzzer* that **generates** many small
  config documents (heavily sampling numeric/escape/structural edges) and asserts
  both CLIs agree on accept/reject. Deterministic by default (fixed seed +
  iteration count); on a divergence it prints the exact config so it can be
  pinned in `config_parity.py`. Set `MIE_CONFIG_FUZZ_SEED` / `MIE_CONFIG_FUZZ_ITERS` to explore
  further locally.
- **`record_fuzz.py`** — the record-stream twin of `config_fuzz.py`, and the only
  check anywhere that compares what the three **decoders produce** on input
  nobody wrote. It builds a recording by concatenating one to three of the
  committed hex fixtures, then *damages* it (bit flips, truncation, spliced
  noise, zeroed words, duplicated slices), runs `decode` through every
  implementation's CLI, and compares the exit-code class **and the CSV bytes**
  all-pairs. Structure-aware on purpose: uniform random bytes reach the recovery
  paths densely and the valid-record paths almost never. All implementations
  read **one shared input file** — `MUX` comes from the input file name
  (L2-WRT-020), so per-implementation copies would make every CSV differ on the
  harness rather than the decoder. On a divergence it prints the input as a
  ready-to-commit `inputs/*.hex` fixture and names the first differing CSV line.
  `MIE_RECORD_FUZZ_SEED` / `MIE_RECORD_FUZZ_ITERS` (default 60) tune it.
- **`config_path_parity.py`** — the layer above: the `--config` **path**, not its
  contents. The two above always hand the CLIs a perfectly ordinary file, so the
  path's own behavior — what counts as usable, which exit code a bad one yields,
  what the operator is told — had no cross-implementation check at all. This one
  compares the **exact exit code** (not just accept/reject) and requires the
  promised message text from both, across the surface documented in
  `docs/CONFIG-REFERENCE.md` §"Trust boundary": regular files only; missing or
  unusable is exit `5`; any readable location is fine, including names with
  spaces, non-ASCII names and `..` segments. Cases needing platform support
  (character devices, symlinks) skip themselves and say so, so a corpus that
  shrinks on one OS is visible rather than silent.

This is what stops config divergences from being found one at a time: the fuzzer
searches the space so CI catches a mismatch before a reviewer does.

## Contract checks

Some behavior cannot be written as a manifest case, because every case drains
the output it compares. These checks hold each implementation to the
specification **on its own**, so unlike the differential checks above they run
at any implementation count, `--only` included:

- **`broken_pipe.py`** — L2-WRT-018: a consumer that closes stdout early
  (`decode x.mie | head -1`) is a clean exit `0`. It reads one line from each
  implementation's stdout, closes the pipe while the producer still has over a
  megabyte left to write, and requires exit `0`. A drained decode runs first to
  prove the recording decodes cleanly and that its CSV really is larger than a
  pipe buffer, since a producer that finishes before the pipe closes would pass
  without meeting a broken pipe at all.

## Manifest schema

`manifest.json` is a single object with one key, `"cases"`, whose value is an
array of case objects. Each case object accepts the following fields:

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `name` | string | yes | Unique case identifier used for temp files and log output. |
| `input` | string | yes | Path (relative to `tests/conformance/`) to the hex-text input fixture. |
| `expected` | string | when `expected_exit == 0` | Path to the checked-in oracle. For `mode == "decode"` (default) this is the expected CSV; for `mode == "count"` this is a text file containing the expected integer count plus a trailing newline. |
| `expected_errors` | string | no | Path to the expected `<stem>_errors.csv` oracle for split-error-mode (`mode == "decode"` only). |
| `config` | string | no | Optional path to a shared TOML config applied to both implementations. |
| `mode` | string | no | Either `"decode"` (default — both impls run their decode pipeline; CSV output is compared) or `"count"` (both impls run the `count` subcommand; stdout is compared). |
| `args` | array of string | no | Additional CLI arguments appended to both invocations verbatim. The Rust and Python CLIs share one argument surface, so a single vector serves both — there is no per-impl argument translation. |
| `expected_stderr_contains` | string | no | Substring assertion applied to each impl's captured stderr. Used by `mode == "count"` cases to pin the human-readable status line without byte-comparing a temp path. |
| `expected_exit` | integer | no | Expected exit code for both implementations. Defaults to `0`. Negative cases (exit `1`/`2`/`3` per `L1-EXIT-002`..`L1-EXIT-004`) may omit `expected`; the exit code alone is the assertion. |
| `global_args` | array of string | no | Arguments placed **before** the subcommand, where the global flags (`--log-level`, `--config`) are parsed. `args` goes *after* the subcommand, so it cannot reach them — and Rust parses globals in a separate loop from subcommand flags, so both paths need covering independently. |
| `subcommand_args` | array of string | no | Arguments placed immediately **after** the subcommand and **before** the input paths. `args` goes after the paths and after `-o`, which is too late to exercise anything positional — `--` in particular, whose whole job is to change how the paths that follow it are read. |
| `expected_stdout_contains` | string | no | Substring the stdout payload must contain, standing in for `expected` when the output is not byte-comparable across implementations. Help text is the case it exists for: its *shape* differs by design (Python prints per-subcommand help, Rust one combined screen), so only its presence can be asserted. |
| `pre_existing_output` | string | no | Content written to the destination **before** the run. Pins the overwrite contract (`L2-WRT-017`): with `no_clobber` off — the default — a decode must replace an unrelated existing file, and with it on must refuse. The sentinel is deliberately not valid CSV, so any of it surviving into the comparison means the destination was appended to rather than replaced. |

Unknown fields SHALL be rejected by the runner with a clear error so typos do
not silently disable per-case behavior.
