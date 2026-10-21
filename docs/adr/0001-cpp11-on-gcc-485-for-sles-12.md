---
status: superseded
date: 2026-08-18
superseded: 5.0.0
decision-makers: Joey
---

# Implement the C++ decoder in C++11 targeting GCC 4.8.5 on SLES 12 SP5

> **Superseded in 5.0.0.** The C++ implementation was removed in 5.0.0, so this
> decision no longer governs anything in the repository. SLES 12 SP5 remains a
> deployment target; it is now served by the Rust `aero1553` binary built
> against glibc 2.22 (the SLES 12 SP5 system glibc) with `cargo-zigbuild`. The
> record below is kept unchanged as the history of why the C++ tree existed and
> how it was built.

## Context and Problem Statement

MIE-Decoder ships two implementations from one repository tag: a Rust crate
under `rust/` and a Python package under `python/`. **Neither can be deployed to
SLES 12 SP5**, which is a required target.

- The Python package requires `>=3.10` (L3-PY-001). SLES 12 ships Python 3.4,
  3.6 at best via a package, and **no 3.10 package exists in the SLES 12
  repositories**. A source build of the interpreter is the only path, and it
  becomes a qualification item of its own on a locked-down host.
- The Rust crate produces a static-ish binary, but building it requires putting
  a Rust toolchain on a build host and keeping it approved — the same class of
  prerequisite, differently shaped.

A third implementation in C++, compiled against the platform's own system
compiler, removes the prerequisite entirely: a single binary, a config file, no
interpreter, no runtime dependency. That converts SLES 12 SP5 from an
unreachable target into a first-class one.

The open question is which C++ standard, which is really a question about which
compiler.

## Decision Drivers

* SLES 12 SP5 must be a supported target, not a documented exception
* Zero runtime dependencies on the target host
* No build-host prerequisite that needs separate approval on a locked-down site
* The decoder needs no language feature beyond C++11 — it is byte manipulation,
  a hand-rolled TOML parser, and a streaming CSV writer
* Windows must also be buildable from the same source (see ADR-0003)

## Considered Options

* **C++11 on the system GCC 4.8.5** — compiles with what the platform ships
* **C++17 via the SLE 12 Toolchain module (gcc9/gcc10)** — reachable using
  `-static-libstdc++ -static-libgcc` while still targeting glibc 2.22
* **Do nothing; document SLES 12 as unsupported** — the status quo

## Decision Outcome

Chosen option: **C++11 on GCC 4.8.5.**

C++17 via the Toolchain module is genuinely available and was not rejected as
impossible. It was rejected because it buys little *here*. What C++17 offers
this program is `std::optional`, `std::string_view`, structured bindings and
`std::filesystem`. The first three are ergonomic. The fourth — the one that
would actually change the design — is unusable anyway: the atomic-write
guarantee (L2-WRT-015, L3-WRT-001) needs exclusive-create and a rename with
replace semantics, which `std::filesystem` does not express portably, so the
platform layer would exist regardless.

Against that, requiring the Toolchain module adds a build-host prerequisite and
an approval step on precisely the kind of target this decision exists to serve.

The cost of C++11 is accepted as **ergonomic only**.

### Consequences

* Good: single binary plus config file; no interpreter or toolchain risk on the
  target.
* Good: SLES 12 SP5 becomes a first-class target rather than an exception.
* Good: `ghcr.io/joey-huckabee/gcc-4.8:4.8.5` already exists — published by the
  sibling `background-file-mover` repository, which made this same decision —
  so the fidelity CI tier costs nothing to stand up.
* Bad: no C++14+ conveniences. `std::make_unique` is reimplemented, `<regex>` is
  banned outright (libstdc++ did not implement it until GCC 4.9), and
  `std::optional` / `std::string_view` / `std::filesystem` are unavailable.
* Bad: GCC 4.8 cannot host libFuzzer, LSan, or full UBSan, so instrumentation
  runs only on the modern tier. **The mitigation is that the 4.8 job runs the
  full test suite, not just a compile** — do not reduce it to `make all`.
* Bad: two C++14 constructs that MSVC accepts and GCC 4.8 rejects become
  standing hazards, because the Windows build compiles as C++14 (MSVC has no
  `/std:c++11`). They are named in `CONTRIBUTING.md` and caught by the fidelity
  tier:
  1. aggregate-initialising a class that has a default member initialiser
  2. passing a `const_iterator` to a container mutator

### The recurring tax

`background-file-mover` records that GCC 4.8's standard library defeated two
vendoring candidates outright, and that the answer both times was to write the
component in-house. This tree is less exposed — it already hand-rolls its
parsers by convention across all three implementations, so there is less to
lose — but the same tax applies to test tooling: Catch2 v3 requires C++14, so
the test framework is pinned to the final v2 release (see `cpp/VENDORED.md`).

Expect this cost to recur rather than to be paid once. It should be weighed if
the Toolchain-module question is ever reopened.

## Verification

`gcc:4.8` is a **proxy** for the target, not the target: Debian 7 "wheezy" with
glibc 2.13 against SUSE's 2.22, and a different kernel. Being *older* than the
target makes it a conservative floor — passing there implies passing on SLES 12
— but it verifies **C++11 conformance only**, not deployability.

Deployability is verified by hand on real SLES 12 SP5 hardware and is
deliberately out of CI.
