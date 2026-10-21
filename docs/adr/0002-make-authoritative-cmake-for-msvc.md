---
status: superseded
date: 2026-08-18
amended: 2026-08-27
superseded: 5.0.0
decision-makers: Joey
---

# GNU make is authoritative on Linux; CMake drives the MSVC build

> **Superseded in 5.0.0.** The C++ implementation was removed in 5.0.0, so this
> decision no longer governs anything in the repository. SLES 12 SP5 remains a
> deployment target; it is now served by the Rust `aero1553` binary built
> against glibc 2.22 (the SLES 12 SP5 system glibc) with `cargo-zigbuild`. The
> record below is kept unchanged as the history of why the C++ tree existed and
> how it was built.

## Context and Problem Statement

The C++ implementation must build in two very different places:

1. **SLES 12 SP5 and the `gcc:4.8` fidelity container**, where ADR-0001 puts the
   floor at GCC 4.8.5.
2. **Visual Studio 2022 on Windows**, where the build has to be something a
   developer can open, debug and step through — and which produces a shipping
   artifact (ADR-0003).

One build description for both would be the obvious preference. The question is
whether it is achievable.

## Decision Drivers

* The fidelity tier must be able to build; it is the only thing that proves
  C++11 conformance on the target compiler
* Visual Studio integration should be real — IntelliSense, breakpoints, the Test
  Explorer — not "run this shell script and attach"
* Two file lists is the drift hazard, not two build *files*
* The sibling `background-file-mover` repository's Makefile already encodes the
  tier structure, the sanitizer switching and the toolchain-keyed build
  directory; reusing it is worth real money

## Considered Options

* **CMake everywhere**, dropping the Makefile
* **make everywhere**, with a hand-maintained `.sln` / `.vcxproj` for Visual
  Studio
* **make on Linux + CMake on Windows**, both reading one shared source list

## Decision Outcome

Chosen option: **make on Linux, CMake on Windows, one shared `cpp/sources.txt`.**

### Why not CMake everywhere

The `gcc:4.8` image is Debian 7 "wheezy" (glibc 2.13, GNU Make 3.81) and has
**no CMake at all** — none on `PATH`, no `cmake` binary anywhere on the
filesystem, no `cmake` package installed. Nor can one be added: Debian 7 is long
past end of life and its repositories are archived, so `apt-get update` inside
the image exits **100** and `apt-cache policy cmake` reports
`Candidate: (none)`. There is no `ninja` either, and no `ninja-build` candidate.

So the objection is not "the CMake in there is too old to read a modern
`CMakeLists.txt`". It is that there is no CMake to be too old, and no supported
route to installing one. Making CMake universal means putting one there by hand,
which is one of:

* **Build it from source in the image.** CMake 3.20 and later require a C++17
  compiler and GCC 4.8.5 provides C++11, so this pins the fidelity tier to a
  CMake old enough to build with C++11 — and then the shared `CMakeLists.txt`
  must drop below the `cmake_minimum_required(VERSION 3.15)` the Windows build
  relies on. The floor of the *Windows* build would be set by a 2015 Linux
  compiler.
* **Drop in a prebuilt binary.** It has to run against glibc 2.13, which is
  older than the baseline current upstream builds target.
* **Publish a new GCC 4.8.5 image with CMake baked in.** This trades one
  maintained artifact for another and puts a hand-built tool inside the tier
  whose entire job is to be a faithful reproduction of the target. Whatever that
  tier then proves, it is no longer quite "this is what the target compiler
  does".

It would also mean re-expressing every gate — the sanitizer tiers, the
toolchain-keyed build directory, coverage, tidy, the fidelity-container
invocation — as CMake presets, discarding a Makefile that already works.

### Amendment (2026-08-27): the CMake claim was wrong, and the decision is firmer for it

As originally written, this section said the image "ships **CMake 2.8**" and that
`cmake_minimum_required(VERSION 3.15)` "fails outright" on it. Both are false.
The image ships no CMake whatsoever, and nothing fails outright because nothing
runs.

The error mattered in a specific way: it made the blocker sound *surmountable*.
A reader weighing "CMake everywhere" a second time sees "2.8", thinks *lower the
floor, or backport one*, and re-opens a question that is in fact more closed than
the record admitted. That is what happened — the question was re-opened, and the
facts above had to be re-derived from the container rather than read from here,
which is the one thing a decision record exists to prevent.

Verified directly against `ghcr.io/joey-huckabee/gcc-4.8:4.8.5`:

```
command -v cmake                  -> not on PATH
find / -name cmake -type f        -> none
dpkg -l | grep cmake              -> no cmake package installed
apt-get update                    -> exit 100 (archived repositories)
apt-cache policy cmake            -> Installed: (none) / Candidate: (none)
apt-cache policy ninja-build      -> no such package
dpkg-query -W -f='${Version}' libc6 -> 2.13-38+deb7u10
```

One new consideration was weighed on the re-examination and did not change the
outcome. A CMake + Ninja build would emit `compile_commands.json` at configure
time, which would retire both `bear` and the `tidy-db-check` guard in
`cpp/Makefile` — that guard exists only because `bear` records the compiler
invocations it *intercepts*, so an incremental build yields a database missing
whatever did not rebuild, and clang-tidy then skips those files and still exits
0. That is a genuine benefit and it is not enough: it buys tooling ergonomics at
the cost of the fidelity tier's credibility, and the drift hazard this ADR was
actually written to close (two *file lists*) is already closed by
`cpp/sources.txt` and `scripts/assert-sources-agree.sh`.

**Status is unchanged — `accepted`.** The decision did not move; only a
supporting fact was corrected. An ADR is superseded when its decision is
replaced, not when its evidence is fixed, so no ADR-0004 was written.

### Why not a checked-in `.vcxproj`

MSBuild project XML is hand-maintained and drifts on every file added, and the
symptom is a link error on Windows only. It also gives up nothing in return:
Visual Studio 2022 opens a CMake folder natively with full IntelliSense and
debugger support, so the `.sln` buys no integration that CMake does not already
provide.

### How the drift is actually closed

The hazard was never "two build files" — it is **two file lists**. So there is
one: `cpp/sources.txt`, a newline-delimited list read by both builds.

- `cpp/Makefile` reads it with a `sed` pipeline inside `$(shell …)`.
- `cpp/CMakeLists.txt` reads it with `file(STRINGS …)` and a regex loop.
- `scripts/assert-sources-agree.sh` configures both and compares what each one
  **actually resolved** — not what the file appears to say. That distinction is
  the point: the two parse the same text with different tools, and a
  comment-stripping difference is exactly the kind of quiet divergence that
  produces a module present in one binary and missing from the other.

The platform backends (`platform_posix.cpp`, `platform_win32.cpp`) are
deliberately **not** in `sources.txt`: everything named there is compiled
unconditionally, and those two are mutually exclusive. Each carries an `#error`
guard so a mis-selection is one clear message rather than a cascade of
missing-symbol noise.

### Consequences

* Good: the fidelity tier keeps working with the tools the container has.
* Good: `File → Open → Folder → cpp\` in VS 2022 just works.
* Good: the Makefile's tier structure carries over from a repository where it is
  already proven.
* Bad: two build descriptions to keep in step for *flags*, not for files. The
  warning settings, the C++ standard and the preprocessor defines are stated
  twice and are not gate-checked. Changing one without the other is the live
  hazard this decision leaves open — mitigated only by both builds being run in
  CI at `-Werror` / `/WX`.
* Bad: a contributor has to know which build is authoritative where. Stated at
  the top of both files.

## Verification

`scripts/assert-sources-agree.sh` runs in CI and fails if the two builds resolve
different source sets. Both builds run the full Catch2 suite in CI — `make
check` on Linux (modern and GCC 4.8.5 tiers) and `ctest` on `windows-2022` — so
a flag divergence that changes behaviour surfaces as a test failure even though
the flags themselves are not compared.
