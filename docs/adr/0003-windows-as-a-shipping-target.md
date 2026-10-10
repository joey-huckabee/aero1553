---
status: accepted
date: 2026-08-18
decision-makers: Joey
---

# Windows is a shipping target for the C++ implementation, not a build convenience

## Context and Problem Statement

ADR-0001 puts the C++ implementation on SLES 12 SP5. The same source also
compiles with MSVC, because the decoder itself is byte manipulation with no
operating-system surface. That raises a question that has to be answered
explicitly rather than drifting: **is the Windows build a released artifact, or
just somewhere a developer can debug?**

The two answers cost very different amounts. "Developer convenience" means the
build has to link and the tests have to pass. "Shipping artifact" means the
Windows binary has to behave *identically* to the Linux one on inputs an
operator will actually hand it — including paths with non-ASCII characters,
destinations that already exist, and output piped into another tool.

## Decision Drivers

* The existing Rust and Python implementations both already run on Windows and
  are tested there in CI; a C++ implementation that did not would be the odd one
  out of three
* The shared conformance oracles are **byte-exact**, so any platform-dependent
  behaviour is a test failure, not a footnote
* Analysts run this tool on Windows workstations against recordings pulled off a
  test rig

## Decision Outcome

Chosen option: **Windows is a shipping target, held to behavioural parity with
Linux.**

Concretely, that commits to five things, all of which live behind
`cpp/include/mie/platform.hpp`:

1. **UTF-8 in, UTF-16 at the boundary.** Paths are carried as UTF-8
   `std::string` throughout and widened with `MultiByteToWideChar` before the
   `...W` entry points. Narrow `...A` calls would route through the active ANSI
   codepage and mangle any non-ASCII path — which the Rust implementation
   handles correctly, so using them would make C++ the only implementation that
   corrupts a Spanish or Japanese directory name.
2. **`MoveFileExW(..., MOVEFILE_REPLACE_EXISTING)`**, never `std::rename`. The
   MSVC CRT's `rename()` *fails* when the destination exists. Rust's
   `std::fs::rename` uses `MoveFileEx` internally, so a port written with
   `rename()` is green on Linux and broken on every Windows re-run over an
   existing CSV.
3. **Binary stdout.** `_setmode(_fileno(stdout), _O_BINARY)` is called before
   anything writes a byte. Without it the CRT rewrites every newline to CRLF and
   every stdout oracle fails on Windows alone (L2-WRT-012). *Amended:* stderr
   is switched too (`platform::prepare_output_streams`), once its diagnostics
   became part of what every implementation must print alike (L2-CLI-022) and a
   Windows C++ run was found ending each of them CRLF.
4. **Shared read on the in-progress temp file.** `CreateFileW` with
   `FILE_SHARE_READ | FILE_SHARE_DELETE` rather than exclusive access, so an
   antivirus or backup agent touching the file mid-write does not become a
   spurious decode failure. `FILE_SHARE_WRITE` is withheld: readers are
   harmless, a second writer would corrupt the CSV.
5. **Handles closed before the rename.** NTFS refuses to rename a file that
   still has an open handle; POSIX does not care. The ordering therefore has to
   be deliberate rather than incidental.

Points 2 and 4 were both found by running the suite on Windows during the
initial platform spike, not by reading documentation. That is the argument for
the CI job.

### Consequences

* Good: one source tree, three implementations, all three tested on both
  operating systems.
* Good: the failure modes above are caught by a gate rather than by an operator.
* Bad: a `windows-2022` CI job and a Windows conformance run to maintain.
* Bad: MSVC compiles as C++14 (it has no `/std:c++11`), so it silently accepts
  two constructs GCC 4.8.5 rejects. This is the cost ADR-0001 names; it is
  mitigated by the fidelity tier running the full suite, not by anything on the
  Windows side.
* Bad: `/W4 /WX /permissive-` occasionally disagrees with `-Wall -Wextra
  -Werror` about what is worth warning on. Both are kept at error level anyway —
  a warning one compiler finds and the other does not is usually a real
  ambiguity.

### What this does NOT commit to

* **Network shares.** Memory-mapping over SMB works but its failure modes under
  connection loss are not characterised, and the atomic-rename guarantee depends
  on the remote filesystem's semantics. Decoding from a UNC path is untested and
  unsupported; copy locally first.
* **32-bit Windows.** x64 only. MIE recordings routinely exceed 2 GiB and a
  32-bit address space cannot map them.
* **A code-signed installer.** The release artifact is a plain `.exe`.

## Verification

* `cpp/tests/test_platform.cpp` pins each of the five behaviours above, and runs
  on both platforms. The replace-an-existing-destination case and the
  no-CRLF-translation case are the ones that fail on a naive port.
* CI builds and tests on `windows-2022` with MSVC at `/W4 /WX /permissive-`.
* The shared conformance suite runs against the C++ binary on both Linux and
  Windows, so a platform-dependent CSV difference is a failing oracle rather
  than a discovery in the field.
