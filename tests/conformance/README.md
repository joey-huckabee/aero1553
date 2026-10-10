# Cross-Implementation Conformance

This suite verifies behavior shared by the Rust, Python and C++
implementations. The runner defaults to **every** registered implementation and
fails if one is missing, so a case cannot quietly test fewer than all three.
Each case provides:

- a text-based hexadecimal MIE input under `inputs/`;
- optional shared TOML configuration under `configs/`;
- expected vendor-compatible CSV output under `expected/`;
- the expected stderr under `expected/<case>.stderr`, where the case writes any
  (see [Stderr oracles](#stderr-oracles)); and
- optional extra CLI arguments in `manifest.json` (the `args` field), passed verbatim to all three CLIs — they share one identical argument surface, which the `cli-surface-parity` gate compares across every implementation.

The runner materializes temporary `.mie` files, invokes every CLI, and requires
each one's output to match the checked-in CSV oracle byte-for-byte, and its
stderr to match the stderr oracle.

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

When intentionally changing shared CSV or diagnostic behavior, update the
checked-in oracles only after every implementation produces identical output:

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
- **`long_paths.py`** — a path longer than the Windows legacy limit (260) is an
  ordinary path. It builds a directory over 320 characters deep and runs each
  implementation from it and into it (with `--separate-errors`, so the errors
  file is covered too), through `count`, and with a short output path spelled
  as a long run of `./` segments. Every result must match the same
  implementation's run at a short path byte for byte. It runs on every
  platform; only Windows needed work to pass it, since Win32 refuses such paths
  without the `\\?\` prefix that Rust's standard library adds and the C++ build
  had not.

## Manifest schema

`manifest.json` is a single object with one key, `"cases"`, whose value is an
array of case objects. Each case object accepts the following fields:

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `name` | string | yes | Unique case identifier used for temp files and log output. |
| `input` | string | yes, or `inputs` | Path (relative to `tests/conformance/`) to the hex-text input fixture. |
| `inputs` | array of string | instead of `input` | Several fixtures, passed as positionals in order: two or more is a multi-file merge. An entry of `"<missing>"` gives that input a path but writes no file, to pin how an implementation treats an input it cannot open (`L2-MRG-004`); the runner also asserts nothing was created there. |
| `expected` | string | when `expected_exit == 0` | Path to the checked-in oracle. For `mode == "decode"` (default) this is the expected CSV; for `mode == "count"` this is a text file containing the expected integer count plus a trailing newline. |
| `expected_errors` | string | no | Path to the expected `<stem>_errors.csv` oracle for split-error-mode (`mode == "decode"` only). |
| `config` | string | no | Optional path to a shared TOML config applied to both implementations. |
| `mode` | string | no | Either `"decode"` (default — both impls run their decode pipeline; CSV output is compared) or `"count"` (both impls run the `count` subcommand; stdout is compared). |
| `args` | array of string | no | Additional CLI arguments appended to both invocations verbatim. The Rust and Python CLIs share one argument surface, so a single vector serves both — there is no per-impl argument translation. |
| `expected_stderr_contains` | string | no | Substring the **raw** stderr must contain, for what the stderr oracle leaves out: a case whose point is that an `INFO` or `DEBUG` line appears (`log-level-from-toml-config`). Everything else a case writes to stderr is held by its [stderr oracle](#stderr-oracles), exactly. |
| `expected_exit` | integer | no | Expected exit code for both implementations. Defaults to `0`. Negative cases (exit `1`/`2`/`3` per `L1-EXIT-002`..`L1-EXIT-004`) may omit `expected`; the exit code and the stderr oracle are the assertion. |
| `global_args` | array of string | no | Arguments placed **before** the subcommand, where the global flags (`--log-level`, `--config`) are parsed. `args` goes *after* the subcommand, so it cannot reach them — and Rust parses globals in a separate loop from subcommand flags, so both paths need covering independently. |
| `subcommand_args` | array of string | no | Arguments placed immediately **after** the subcommand and **before** the input paths. `args` goes after the paths and after `-o`, which is too late to exercise anything positional — `--` in particular, whose whole job is to change how the paths that follow it are read. |
| `compare_stdout` | boolean | no | The payload is stdout rather than the CSV: it must be non-empty and byte-identical across every implementation, with no oracle file. For a case that ends in help, whose text `cli_message_parity.py` already pins. It replaced `expected_stdout_contains`, a substring check written while the help texts differed between implementations; since v4.0.0 Python runs Rust's CLI and C++ prints Rust's help, so the whole text is compared. |
| `pre_existing_output` | string | no | Content written to the destination **before** the run. Pins the overwrite contract (`L2-WRT-017`): with `no_clobber` off — the default — a decode must replace an unrelated existing file, and with it on must refuse. The sentinel is deliberately not valid CSV, so any of it surviving into the comparison means the destination was appended to rather than replaced. |

Unknown fields SHALL be rejected by the runner with a clear error so typos do
not silently disable per-case behavior.

## Stderr oracles

Every case's stderr is compared exactly, in every implementation, on every
platform (`L2-CLI-022`). The oracle is `expected/<case>.stderr`; a case with no
such file must write nothing to stderr at all. Before comparing, the runner:

- replaces each run-specific path with a placeholder: `<INPUT0>`, `<INPUT1>`,
  ... for the inputs, `<OUTPUT>` for the destination, `<ERRORS>` for the
  split-mode errors file, `<OUTDIR>` for the destination's directory,
  `<CONFIG>` for the case's config, `<TEMP>` for the run's temporary directory
  and `<ROOT>` for the repository. Every implementation repeats a path as it
  was given, so these are the only parts that legitimately differ. A derived
  path reads as one, e.g. `<OUTPUT>.partial`.
- leaves out `INFO` and `DEBUG` log lines, whose text `L2-CLI-022` leaves free.

Line endings are compared, not normalised: stderr is captured as bytes, and an
implementation that ended its lines CRLF on Windows fails.

The implementations are compared with each other first, so a divergence is
reported as their actual outputs side by side, and then with the oracle, which
is what catches a line that every implementation lost or changed at once.
`--update-expected` writes the oracle when every implementation agrees, and
deletes it when they now agree on writing nothing. An oracle whose case is no
longer in the manifest fails the run.

This replaced substring assertions. A substring cannot fail on what it does
not mention: three cases in four had no stderr assertion at all, so a new
stray WARN in any of them passed, and one case's substring stopped a word short
of a real divergence -- C++ named the message format `format 1` where Rust and
Python said `Receive`.
