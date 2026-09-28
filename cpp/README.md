# Aero1553 — C++ implementation

The third implementation of the MIE decoder, alongside the Rust crate in
`../rust/` and the Python package in `../python/`. It exists so that
**SLES 12 SP5** is a first-class deployment target rather than a documented
exception: it compiles with the platform's own GCC 4.8.5, ships as a single
binary, and needs no interpreter or toolchain on the host (ADR-0001).

The same source builds with MSVC and Windows is a **shipping target**, not a
development convenience (ADR-0003).

> **Status: Phase 2 complete.** `decode` (single file and multi-file merge),
> `count` and `dump` all work. The binary passes the **whole** shared
> conformance manifest — every case the Rust and Python implementations are held
> to, byte for byte, on Linux and Windows — and its CLI exposes an **identical**
> long-flag surface to the other two, enforced by the `cli-surface-parity` gate.
>
> Ported and gated: `platform`, `text`, `optional`, `models`, `error`, `decode`,
> `sync`, `log`, `delta`, `reader`, `writer`, `toml`, `config`, `filter`,
> `order`, `cli`, `merge`, `dump`. See `../CHANGELOG.md` for what each landing
> covered and `../docs/adr/` for the decisions that shape the tree.
>
> The differential config-parser checks (`config_parity.py`, `config_fuzz.py`,
> `config_path_parity.py`) cover C++ too: each takes an implementation-to-CLI
> mapping and compares **all pairs**, so the third hand-rolled TOML parser is
> held to the same grammar as the other two. `dump` output is conformance-gated
> as well -- eight cases in the manifest -- since the report characters were
> reconciled across the three.
>
> **What is genuinely outstanding is release-artifact work, not decoder code:**
> Phase 3 prebuilt artifacts and the static-CRT question are undecided (#4).
> Coverage and fuzzing are **not** outstanding -- both are wired and gating; see
> "CI tiers" below. There are no libFuzzer targets, but that is a decided design
> rather than a gap: all three implementations run the same deterministic,
> identically-seeded `xorshift64` harness, so they see the same bytes and a
> divergence is detectable. libFuzzer would need clang, which would skip the
> GCC 4.8.5 tier this implementation exists for.
> Static analysis is now wired: CodeQL analyses `c-cpp` and SonarCloud analyses
> `cpp/src` and `cpp/include` from the `bear` compilation database.

## Build & test

### Linux (authoritative — see ADR-0002)

```bash
make check                # build and run the suite, -Werror
make check SANITIZE=1     # AddressSanitizer + UBSan + LeakSanitizer
make check-valgrind       # memcheck, fails on any leak or invalid access
make check-gcc48          # the SLES 12 fidelity tier, in a container
make tidy                 # clang-tidy (needs compile_commands.json, see below)
make format-check         # clang-format, non-mutating
make versions             # print the resolved toolchain and build directory
make clean                # remove only this toolchain's artifacts
make clean-all            # remove every toolchain's artifacts
```

Flags are `-std=c++11 -Wall -Wextra -Werror`. Vendored headers come in via
`-isystem third_party` so they cannot trip `-Werror`.

Build output goes to `build/<machine>-<version>-<tier>`, keyed on the compiler
that produced it, so the modern, `gcc:4.8` and sanitizer tiers cannot overwrite
one another. Overwrite with `make BUILD_DIR=/somewhere/else` when needed.

`make tidy` needs a compilation database:

```bash
make clean && bear -- make -j"$(nproc)" all   # writes compile_commands.json
make tidy
```

**Start from a clean tree.** `bear` records the compiler invocations it
*intercepts*, and an incremental build issues none for targets that are already
up to date — so `bear -- make all` over a warm tree writes a database missing
whatever did not rebuild. clang-tidy then prints `Skipping <file>. Compile
command not found.` for each one and **exits 0**, so the gate reports success
having analysed nothing. `make tidy` refuses to run in that state
(`tidy-db-check`), which is the only reason it is visible.

**Check the clang-tidy version.** `TIDY` defaults to whatever `clang-tidy` is
on `PATH`, and CI runs LLVM 22. Ubuntu 22.04 (the usual WSL2 image) ships
LLVM 14, which is a materially different check set — `misc-const-correctness`,
for one, does not exist before LLVM 15, so a clean run on 14 says nothing about
that check, and 14 emits `bugprone-throw-keyword-missing` false positives that
22 does not. `make tidy` prints a loud advisory when the versions disagree.
Either point it at a matching binary or use the container:

```bash
make tidy TIDY=clang-tidy-22       # if installed locally
make verify-ci                     # CI's versions, in a container
```

`make verify-ci` is the authoritative pre-push check for everything
version-sensitive: clang-format 22, clang-tidy 22, cppcheck 2.13 and the
sanitizers, all on the CI compiler. The version itself comes from
`LLVM_VERSION` in `cpp/Makefile`, which `scripts/repo-hygiene.sh` holds equal to
`env.LLVM_VERSION` in `.github/workflows/cpp-ci.yml` — so this target cannot
quietly stop predicting CI. Note that rootless podman cannot
bind-mount a DrvFs path, so under WSL2 it must be run from a Linux-native
directory, not `/mnt/c`.

### Windows / Visual Studio

**Visual Studio 2022 → File → Open → Folder → `cpp\`.** VS configures CMake,
wires up IntelliSense and the debugger, and picks the test target up in the Test
Explorer. No `.sln` or `.vcxproj` is checked in — hand-maintained project XML is
exactly the drift the shared source list exists to avoid.

From a command line:

```powershell
cmake -B build-msvc -S . -G "Visual Studio 17 2022" -A x64
cmake --build build-msvc --config Debug
ctest --test-dir build-msvc -C Debug --output-on-failure
```

Flags are `/std:c++14 /W4 /WX /permissive- /utf-8`. MSVC has no `/std:c++11` —
its floor is C++14 — so C++11-conformant source is compiled as C++14, which is
valid. **The compiler that actually enforces the C++11 boundary is GCC 4.8.5 in
the fidelity tier, not this one.**

### WSL2

Build on the **Linux-native filesystem** (`~/src/...`), not `/mnt/c`. Two
reasons, and the second is not obvious:

1. Cross-filesystem builds over DrvFs are slow and produce spurious rebuilds
   from mtime granularity.
2. **Rootless podman cannot bind-mount a `/mnt/c` path.** The container starts,
   the mount is silently empty, and `g++` reports "no input files" as though the
   source had vanished — so `make check-gcc48` fails in a way that looks like a
   code problem. Run it from a native path.

## Layout

```
include/mie/       public headers
src/               implementation
tests/             Catch2 v2 test suite
third_party/       vendored pinned single headers (see VENDORED.md)
LICENSES/          license texts for vendored dependencies
sources.txt        THE library source list, read by both builds
Makefile           authoritative on Linux
CMakeLists.txt     authoritative on Windows
```

Requirements and decisions live at the repository root, shared with the Rust and
Python implementations:

```
../docs/L1-REQ.md    system requirements
../docs/L2-REQ.md    architectural derivations
../docs/L3-REQ.md    implementation obligations (L3-CPP-* for this tree)
../docs/adr/         MADR-format architecture decision records
```

## Design rules

- **No runtime dependencies.** Argument parsing, CSV, TOML, logging and error
  types are hand-rolled, matching the Rust crate's single-dependency discipline.
  Catch2 is test-only and vendored (`VENDORED.md`).
- **The platform layer is the only thing that touches the OS.** Five concerns
  live behind `include/mie/platform.hpp` — mapping the input, atomic output,
  directory enumeration, binary stdout, path identity — and nothing else may
  include `<windows.h>` or `<sys/mman.h>`. Enforced by
  `../scripts/assert-platform-confined.sh`.
- **Never locale-sensitive.** `DELTA` is formatted `%.6f`, whose decimal
  separator is locale-dependent; the parsers classify characters with explicit
  ASCII ranges rather than `<cctype>`. Enforced by
  `../scripts/assert-locale-free.sh`.
- **RAII only**, no naked `new`/`delete`.
- **C++11, not C++14.** Two constructs MSVC accepts and GCC 4.8.5 rejects are
  banned by convention, because they produce "green on Windows, red on Linux":
  1. aggregate-initialising a class that has a default member initialiser
  2. passing a `const_iterator` to a container mutator

## CI tiers

| Tier | What it proves |
|---|---|
| modern g++ | fast feedback, plus the sanitizers GCC 4.8 cannot host |
| `gcc:4.8` container | C++11 conformance on the SLES 12 system compiler — **runs the full suite, not just a compile** |
| MSVC on `windows-2022` | the shipping Windows artifact, at `/W4 /WX /permissive-` |
| static analysis | clang-tidy 22, cppcheck, CodeQL `c-cpp`, SonarCloud CFamily |
| coverage (gcovr) | 90% line / 76% branch floors, from `COVERAGE_MIN_*` in the Makefile |
| fuzz burn-in | the L1-ROB-001 harness at a raised iteration count, on Linux and MSVC (`fuzz.yml`, via `make check-fuzz`) |
| real SLES 12 SP5 | deployability. Not in CI — verified by hand on hardware |

**On the fuzz tier.** This section previously said there was deliberately *no*
fuzz tier and that "the targets are unwritten". Both statements were wrong by
the time they were read: `fuzz.yml` runs a `fuzz-cpp` burn-in job, and the
harness it runs is an ordinary Catch2 case, so it already executes on **every**
tier above — modern g++, the GCC 4.8.5 fidelity tier, MSVC, and again under
ASan/UBSan and Valgrind, which is where a real memory fault on random input
actually surfaces. What does not exist is a *separate libFuzzer target*, and
that is a decision rather than a gap (see the note at the top of this file).

Both gaps this section named at v2.15.1 are closed as of **v2.16.0**: the tree
now carries `dump` and merge-input-resolution fuzz harnesses alongside the
reader one (`make check-fuzz` runs all three), and `fuzz-cpp-asan` gives the
burn-in an **instrumented** deep run — until then the raised iteration count
applied only to the uninstrumented `-O2` build, where a non-faulting
out-of-bounds read goes unnoticed.

Valgrind still runs at the default count, deliberately: it is one to two orders
of magnitude slower than ASan and finds the same class of fault on this code, so
a 25 000-input memcheck would cost hours to duplicate what the ASan job covers.

The merge harness earned its keep immediately. Comparing its `FUZZ-SUMMARY`
counters against Rust's and Python's found **four** `read_manifest` divergences
at once, two of them in this implementation: it accepted arbitrary bytes as
paths where the other two require UTF-8, and it dropped every `\r` in a line
rather than only a trailing one. The grammar all three now implement is
normative in L2-MRG-001.

The branch floor is lower than the line floor on purpose: gcov branch counts
vary by several points with compiler version, and one portable number keeps a
developer's `make coverage` and the CI job the same invocation with the same
threshold. See `MAINTAINER-GUIDE.md` §10 for the ratchet plan.

`gcc:4.8` is a *proxy* for the target, not the target: Debian 7 "wheezy" with
glibc 2.13 against SUSE's 2.22. Older, not merely different — which makes it a
conservative floor rather than an approximation.
