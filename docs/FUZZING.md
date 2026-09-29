# Aero1553 Fuzzing

**What this page is.** The single map of *what is fuzzed*, *by which
implementation*, *under which driver*, and *what is deliberately not fuzzed yet*.
Read it before adding a fuzz harness, before interpreting a burn-in log, and
before concluding that two implementations disagree.

**The one rule that governs this page.** A fuzz surface is either exercised by
**all three** implementations or by **none**. A surface that only one tree
fuzzes is a parity gap, and it is tracked in [section 5](#5-parity-gaps) until it
is closed — not left as a virtue of the tree that happens to have it.

Related: [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) for CI architecture,
[`ERROR-CATALOG.md`](ERROR-CATALOG.md) for the exit codes a fuzz finding is
classified by, [`DATA-SCENARIOS.md`](DATA-SCENARIOS.md) for what the decoder is
*supposed* to do with each kind of bad input.

---

## 1. Two architectures, and what each can prove

The repository contains two structurally different kinds of fuzzing. Almost
every surprise about "why don't the three implementations agree?" traces back to
which of the two a given surface uses.

### A. In-process robustness harnesses (per-language)

Each implementation carries its own harnesses, written in its own test
framework, running in its own process:

| | location | framework |
|---|---|---|
| Rust | `rust/tests/integration.rs` | libtest (`#[test]`) |
| Python | `python/tests/test_e2e.py`, `python/tests/test_merge.py` | pytest |
| C++ | `cpp/tests/test_fuzz.cpp` | Catch2, tagged `[fuzz]` |

They share a **generator** — the same xorshift64, the same seed
(`0x0DDCD1ECDDC0DEC0`), the same size bands, the same draw order — so all three
see the *same bytes* at iteration N. The Python side keeps that machinery in
`python/tests/fuzz_support.py` so harnesses in different files cannot drift
apart; they did once, and the merge harness spent a year seeding
`random.Random`.

What each harness asserts on its own is only a local, negative property — *this
process did not crash*:

- Rust: `std::panic::catch_unwind` — panic / no panic. That nothing but a
  `MieError` escapes is a compile-time guarantee of the `Result<T, MieError>`
  signature, so the harness does not need to check it.
- Python: `except Aero1553Error: pass` ahead of a bare `except Exception:`
  that fails the test — a runtime assertion that the exception *type* is
  documented.
- C++: `catch (const mie::MieError&)` with an empty body; any other exception
  type escapes and fails the case.

**What makes them comparable is the summary line.** Every harness ends by
writing one `FUZZ-SUMMARY` record of counters — inputs, bytes generated, readers
opened, records yielded, errors — defined to mean the same thing in all three,
and `scripts/compare-fuzz-summaries.py` diffs them all-pairs. It runs in the
nightly burn-in across seven runner configurations, **and on every push** in
`differential.yml` at default iteration counts, so a divergence fails the PR
that introduced it rather than the next scheduled run.

It is a coarse check: it compares run *totals*, so it catches a disagreement
about how many records a file yields but not two implementations decoding the
same record to different values. That is what architecture B is for.

Every counter must be **path-independent**. The harnesses name their temp files
differently, so anything derived from a path measures the harness rather than the
decoder. The dump harness counts output *lines* for exactly this reason: it
counted bytes first, and Rust and Python disagreed by a constant offset that
turned out to be the length of the input path in the dump header.

### B. Shared differential drivers (cross-implementation)

`tests/conformance/` holds the other architecture, and it is the one that
answers "do the three agree on this input?". A single Python driver:

1. generates one input,
2. runs it through **every registered implementation's CLI** as a subprocess,
3. classifies each outcome, and
4. compares **all pairs** — any two disagreeing is a finding.

The comparator lives in `tests/conformance/differential.py`. It is deliberately
*not* majority-rule (two implementations sharing a bug would outvote the correct
one) and *not* reference-implementation (that would make one tree's quirks
normative). It reports the split — `accept: Rust, Python | reject: C++` — without
naming a winner, because which side is right is a maintainer's judgement.

Two drivers use it: `config_fuzz.py` for config documents and `record_fuzz.py`
for the record stream. Both print the generating input in a form you can commit
as a permanent fixture, because a seed stops reproducing anything the moment the
generator changes.

---

## 2. Inventory: what is fuzzed today

| # | Surface | Rust | Python | C++ | Architecture | Compared how |
|---|---------|:----:|:------:|:---:|--------------|--------------|
| 1 | Reader / binary record stream | yes | yes | yes | in-process | run totals |
| 2 | `dump` (record-aware + raw hex) | yes | yes | yes | in-process | run totals |
| 3 | Merge input resolution (manifest, glob) | yes | yes | yes | in-process | run totals |
| 4 | Config-parser documents (TOML) | yes | yes | yes | shared differential | per input |
| 5 | Record stream, structure-aware | yes | yes | yes | shared differential | per input, CSV bytes |
| 6 | CLI argument vectors | no | no | no | — | — |

Row 6 is listed to record that it is *absent everywhere*, which under the rule in
the header means it is not a parity gap — it is a future item
([section 6](#6-what-could-be-added)).

Adjacent but **not fuzzing** — exhaustive rather than generated, and the
strongest cross-implementation check in the tree:
`rust/examples/decode_digest.rs` sweeps every possible Type Word, every possible
Command Word and every timestamp field bit through the Rust decoders and prints
an FNV-1a digest. `cpp/tests/test_decode_exhaustive.cpp` recomputes those four
constants from its own decoders, so one differing field in ~390 000 decodes
fails the build. (The Python package has no decoders of its own since v4.0.0 --
it runs the Rust ones -- so it has nothing to recompute; see L3-PY-017.)

---

## 3. Where each harness runs

| Driver | Default effort | Deep run | Workflow |
|---|---|---|---|
| In-process byte harnesses | `MIE_FUZZ_ITERATIONS` unset -> 256 (reader, dump) / 512 (merge) | `MIE_FUZZ_ITERATIONS` override | `ci.yml` and `cpp-ci.yml` (default, every tier), `differential.yml` (default, plus the cross-impl comparison), `fuzz.yml` (burn-in, seven configurations) |
| Config-parser fuzz | `MIE_CONFIG_FUZZ_ITERS` unset -> 100 | env override | `differential.yml` only |
| Record-stream fuzz | `MIE_RECORD_FUZZ_ITERS` unset -> 60 | env override | `differential.yml` only |

The byte harnesses share three environment variables, and mean the same thing by
each. That is load-bearing rather than tidy: a burn-in scoped differently per
language cannot be compared across languages.

| Variable | Default | Effect |
|---|---|---|
| `MIE_FUZZ_ITERATIONS` | 256 / 512 | Inputs to generate. Unparseable or zero falls back rather than guessing. The default is per-harness because they cost different amounts per input; every implementation uses the same default for the same harness. |
| `MIE_FUZZ_STREAM_LOGS` | unset (silent) | `1` / `true` leaves the decoder's logger at WARN so its diagnostics stream. |
| `MIE_FUZZ_SUMMARY` | unset | File to append the run's `FUZZ-SUMMARY` line to. |

How the log level is set differs by language, and the difference is deliberate.
C++ uses an RAII guard, because Catch2 runs its cases sequentially in one
process and leaving the level at `OFF` broke `test_log.cpp` two files away.
Python uses a context manager for the same reason. Rust sets it and does **not**
restore it: libtest runs that binary's tests in parallel threads, so a scope
guard would restore the level while a sibling test was still running — the race
would be worse than the leak, and no other test in that binary asserts on
logging.

The burn-in covers **seven** runner configurations: Rust, Python and C++ on
Linux; Rust, Python and MSVC-built C++ on Windows; and C++ again under
ASan/UBSan/LSan. Windows matters because the C++ platform layer is entirely
different code there — file mapping, path identity, binary stdout (ADR-0003) —
and mapping a file is the first thing every harness does with its garbage input.
The sanitized job matters because until v2.16.0 the deep sweep ran only against
an uninstrumented `-O2` build, where a non-faulting out-of-bounds read goes
unnoticed.

Valgrind is deliberately **not** given a deep run. It is one to two orders of
magnitude slower than ASan and finds the same class of fault on this code, so a
25 000-input memcheck would cost hours to duplicate what the ASan job already
covers. It stays at the default count in `cpp-ci.yml`.

---

## 4. The surfaces in detail

### 4.1 Reader / binary record stream

**Requirement:** L1-ROB-001.

Generates 32 B – ~8 KB of random bytes, writes a temp file, opens it with the
reader, and drains the iterator through the canonical-order stage with a
deliberately small `max_sort_group` cap. The property asserted is *no crash*,
plus a bound that fails if the iterator yields an implausible number of records.

Random bytes are a good generator for this surface because they reach the
recovery paths densely: sync loss, freerun timestamps, unknown error codes,
reserved bits and structural-invariant rejection all fire in volume. They are a
*bad* generator for the valid-record paths — see 4.5.

One claim the harnesses' own comments make that a burn-in does not bear out: the
`max_sort_group` cap branch is described as "reached often". Across a full
burn-in the Rust log contains no `aero1553::order` diagnostics at all. Random
bytes essentially never produce enough consecutive records with an equal decoded
timestamp to hit a small cap. `record_fuzz.py`'s duplicated-slice mutation is the
generator that actually reaches it.

### 4.2 `dump`

**Requirements:** L1-ROB-001, L2-CLI-009.

Same generator, a much smaller size band (0 – 512 B) so the truncation and
loop-guard paths are hit densely, including zero-length inputs that reach the
empty-file rejection. Drives both the record-aware dump and the raw hex dump
into a throwaway sink, counting each separately.

The property this guards is specific: the record dump reads headers under a
bounds guard and slices to the record extent for the body — it never reads
payload by a Command Word's `data_word_count`, so it has no over-claim/overrun
class of its own. The harness exists to keep it that way.

### 4.3 Merge input resolution

**Requirements:** L1-ROB-001, L2-MRG-001.

Feeds arbitrary bytes to the manifest reader, and drives the hand-rolled glob
matcher and directory expansion with generated patterns.

The patterns are **not** lossily-decoded random bytes. That was the obvious
choice and it is wrong twice over: random bytes almost never contain `*` or `?`,
so the matcher's interesting branches are never reached; and the three
languages' lossy decoders do not agree character-for-character on how many
U+FFFD an ill-formed sequence produces, so the counters could diverge without
the matchers disagreeing about anything. Patterns are instead drawn from a
shared 15-entry alphabet weighted toward `*` and `?` — the first version drew
uniformly over patterns up to 95 characters and matched a probe **zero** times
in 512 iterations. Two alphabet entries and two of the three probe names are
non-ASCII, because Rust and Python match over scalar values while the C++
matcher advances `?` by a whole UTF-8 character, and that agreement is either
real or it is not.

`expand_glob` is called for crash-safety only and its result is deliberately
**not** counted: it reads the working directory, so what it returns depends on
where the suite ran, and a summary field has to mean the same thing on every
host.

**This harness found four real divergences the day it started comparing
counters**, all in `read_manifest` and all now pinned by a
`read_manifest_grammar_is_exactly_specified` test in each tree. The grammar they
led to is normative in L2-MRG-001; the short version is that a manifest is
UTF-8, `\n` is the only separator, one trailing `\r` is stripped, and trimming
is ASCII space and tab only.

It then found a fifth on the first nightly burn-in after that grammar landed —
which is the argument for running the deep sweep on a schedule rather than
treating the 256-iteration default as coverage. Writing the grammar down was not
the same as agreeing on it: Rust's `str::lines()` strips a trailing `\r` only
from a line an `\n` actually followed, so an unterminated final line kept its
CR while Python and C++ stripped it, and the whole disagreement rested on the
single generated input `"\r"` — three of 25 000 manifests, and none of the first
256. **A summary counter differing by three is the shape these findings have.**
Read `manifest_paths=299` against `296` as a decoding difference on some input,
not as noise or as a flaky runner; the generator is deterministic, so the
divergent input is always recoverable by replaying the same iteration indices.

### 4.4 Config-parser documents

`tests/conformance/config_fuzz.py` generates small TOML-ish documents from
palettes chosen to hit historically divergent ground — leading zeros, bare
trailing dots, hex/octal/binary literals, underscore separators, string escapes,
inline tables, datetimes, dotted and quoted keys, non-ASCII identifiers and
non-ASCII digits — and runs every implementation's CLI against each with
`--config`. It compares the accept/reject verdict all-pairs and prints the
generating document verbatim on a divergence.

It compares a **verdict, not a message**: the implementations legitimately differ
in wording, and a rejection may surface as a config error or a usage error
depending on where the value was caught. What they must agree on is whether the
document is *admissible*.

The non-ASCII entries in the palettes are load-bearing: they are what keeps
Python's `re.ASCII` flags from being silently dropped, since a bare `\w` or `\d`
is Unicode-aware and would accept literals that Rust's `is_ascii_alphanumeric` /
`is_ascii_digit` refuse.

### 4.5 Record stream, structure-aware

`tests/conformance/record_fuzz.py` is the record-stream twin of 4.4 and the only
check anywhere that compares what the **decoders produce** on inputs nobody
wrote. It generates a recording, runs `decode` through every implementation's
CLI, and compares the exit-code class **and the CSV bytes** all-pairs.

It is structure-aware because uniform noise is the wrong tool here. The
generator starts from the committed conformance fixtures — every valid record
shape the project knows about — concatenates one to three of them, and then
*damages* the result: bit flips, single-byte writes, truncation, appended noise,
zeroed words, duplicated slices, spliced runs. Each operation breaks a different
layer: a flip inside a Type Word changes a record's shape, truncation cuts a
record in half, spliced noise forces a sync recovery, and a duplicated slice
produces the repeated timestamps the canonical-order stage exists to handle.

All three implementations write to different output paths but read **one shared
input file**. That is not tidiness: `MUX` is populated from the input file name
by default (L2-WRT-020), so per-implementation copies would put a different
value in every CSV and the comparison would fail on the harness rather than on
the decoders.

On a divergence it prints the input as a ready-to-commit `inputs/*.hex` fixture
and names the first differing CSV line.

---

## 5. Parity gaps

Under the rule at the top of this page, each row is a defect, not a feature of
the tree in the "has it" column. The v2.16.0 release closed six; these remain.

| Gap | Has it | Missing from | Why it matters |
|---|---|---|---|
| Differential fuzzing of `count` and `dump` | — | all three | `record_fuzz.py` compares `decode` only. The other two subcommands are covered by the in-process harnesses (no crash) and by curated conformance cases, but not by generated input compared across implementations. |
| Multi-input merge topology | — | all three | Every generated input is a single file. The merge heap, the DELTA scope rules and the duplicate-collapse window interact, and only curated fixtures test them. See [6.2](#6-what-could-be-added). |
| `expand_glob` results compared | — | all three | Called for crash-safety; its return value is filesystem-dependent and so cannot be a summary field. A driver that controlled the directory could compare it. |
| Deep run under Valgrind | — | C++ | Deliberate, not an oversight: one to two orders of magnitude slower than ASan for the same fault class here. Recorded so the decision is visible rather than inferred. |

---

## 6. What could be added

Listed as candidates, not commitments. Each is written so that adopting it means
adopting it in **all three** implementations at once.

**6.1 CLI argument-vector fuzzing.** Generated `argv` arrays over the real flag
surface, compared all-pairs on exit-code class. Deliberately **not** implemented
today in any tree, so under the rule at the top of this page it is a clean
addition rather than a gap. The `cli-surface-parity` gate already proves the
three expose the same *flags*; this would test that they agree on the same
*combinations*, including mutually-exclusive ones and malformed values. It is
the obvious next differential driver: the machinery in `record_fuzz.py`
generalises to it almost unchanged.

**6.2 Merge-topology fuzzing.** Generated *sets* of small input files with
overlapping and inverted timelines, mixed timestamp formats, and duplicate
records, compared across implementations. `record_fuzz.py` already produces
damaged single recordings; extending it to emit N of them and pass `--manifest`
would reach the heap, the DELTA scopes and the collapse window together.

**6.3 A committed corpus of past divergences.** Every divergence found so far
has been reproduced by hand from the printed hex. Committing them under
`inputs/` as ordinary conformance cases would make each one a permanent
regression test rather than a story in a changelog.

**6.4 Corpus-guided fuzzing (libFuzzer / AFL++).** Explicitly considered and
rejected for the C++ tree when it was scoped, for reasons recorded in
`cpp/tests/test_fuzz.cpp`: libFuzzer requires clang, which would mean the fuzz
target ran on one C++ tier and not on the GCC 4.8.5 tier the implementation
exists for; and coverage-guided exploration is non-deterministic, so a required
job can fail on an input nobody's change produced. If it is ever revisited it
should be as a **non-blocking** job alongside the deterministic harnesses, not as
a replacement for them.

---

## 7. Running and triaging

```bash
# Deep run of the in-process byte harnesses (all three honour the same knobs)
MIE_FUZZ_ITERATIONS=25000 cargo test --test integration fuzz_arbitrary_bytes_never_panic
MIE_FUZZ_ITERATIONS=25000 uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness -s
MIE_FUZZ_ITERATIONS=25000 make -C cpp check-fuzz

# The same, instrumented -- what the fuzz-cpp-asan job runs
MIE_FUZZ_ITERATIONS=25000 make -C cpp check-fuzz SANITIZE=1

# See the decoder's own diagnostics while it runs (any of the three)
MIE_FUZZ_ITERATIONS=256 MIE_FUZZ_STREAM_LOGS=1 make -C cpp check-fuzz

# Deep run of the two DIFFERENTIAL fuzzers, across every implementation
(cd rust && cargo build) && (cd cpp && make all)
MIE_RECORD_FUZZ_ITERS=500 MIE_CONFIG_FUZZ_ITERS=2000 \
    uv --directory python run python ../tests/conformance/run.py

# Reproduce the cross-implementation summary comparison locally: point every
# harness at ONE file, then compare. The file is appended to, so order is free.
export MIE_FUZZ_SUMMARY=/tmp/fuzz-summary.txt && rm -f "$MIE_FUZZ_SUMMARY"
(cd rust && cargo test --test integration -- fuzz_arbitrary_bytes_never_panic \
    dump_arbitrary_bytes_never_panics merge_input_resolution_tolerates_arbitrary_bytes)
uv --directory python run pytest tests/test_e2e.py::TestFuzzHarness \
    tests/test_merge.py::test_merge_input_resolution_tolerates_arbitrary_bytes
make -C cpp check-fuzz
mkdir -p /tmp/fz/local && cp "$MIE_FUZZ_SUMMARY" /tmp/fz/local/
python scripts/compare-fuzz-summaries.py /tmp/fz
```

**Triaging an in-process finding.** The failure prints the seed, the iteration
index and the input size, and the harnesses share a generator — so the same
iteration index reproduces the same bytes in the other two trees. Run the other
two at the same iteration count before assuming the defect is local. The
harnesses also emit their `FUZZ-SUMMARY` line on the failure path, with
`outcome=panic` / `outcome=error` in place of `outcome=ok`, so a run that died at
iteration 24 000 still reports the counters it had.

**Triaging a summary-comparison failure.** The report names the split without
naming a winner. Reproduce with the local recipe above, then bisect by
iteration: the generator is deterministic, so halving `MIE_FUZZ_ITERATIONS`
tells you whether the divergence is in the first half. Remember that the
counters are *totals* — a difference of one says nothing about which iteration
caused it.

**Triaging a differential-driver failure.** Both drivers print the generating
input in committable form: `config_fuzz.py` prints the TOML document,
`record_fuzz.py` prints a hex fixture and names the first differing CSV line.
Pin it as a regression case **first** — `config_parity.py` for a config
document, `tests/conformance/inputs/` plus a `manifest.json` entry for a
recording — and only then decide which side is wrong. The comparator
deliberately does not tell you; that judgement should be recorded in the fix.

**When a fuzz finding is not a bug.** Random input legitimately produces
`MieError` / `Aero1553Error` in volume; that is the documented response and is
what the harnesses are written to accept. A finding is a bug when the escape is
an *undocumented* exception type, a panic, a hang, or — for the differential
drivers and the summary comparison — an *agreement* failure, regardless of
whether either side crashed.

---

## 8. Rules for adding a fuzz harness

1. **All three or none.** If a surface is worth fuzzing in one tree it is worth
   fuzzing in the other two. Land the third before the first is merged, or record
   the gap in section 5 with an owner.
2. **Deterministic.** A fixed seed and a fixed default iteration count, with an
   environment override for depth. A required job must not fail on an input
   nobody's change produced.
3. **One generator.** If the harnesses are meant to see the same inputs, they
   must share a PRNG *and* a consumption order. Two different PRNGs is not a
   smaller version of the same test — that is exactly how the merge harness
   drifted.
4. **Path-independent counters.** Anything derived from a filename measures the
   harness, not the decoder, and can never agree across implementations.
5. **Print the reproducer, not just the seed.** A seed stops reproducing the
   moment the generator changes. Print the input in a form that can be committed
   as a fixture.
6. **Prefer the differential driver.** An in-process harness proves a negative
   about one implementation. A differential driver proves a positive about all of
   them. Reach for `tests/conformance/` unless the surface genuinely has no CLI
   expression.
7. **Check what your generator actually reaches.** Both non-obvious defects in
   this area were generators that fuzzed nothing useful: a glob harness that
   matched a probe zero times in 512 iterations, and a byte harness whose
   comments claimed it reached a branch it never once hit. Count the interesting
   outcomes and look at the number.
