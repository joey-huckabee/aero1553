# Building Aero1553

How to build every artifact Aero1553 ships, on a machine **with no network** as well as on
a connected one. The offline route is the main subject: gather a bundle on a connected
machine, carry it across, and run one script. CI runs that route on every push (the
`offline-build-linux` and `offline-build-windows` jobs, `L3-RS-021`), so the steps below are
the ones proven to work, not a description of them.

| Artifact | Build it on | Needs, besides the bundle | Script step |
|---|---|---|---|
| `aero1553-x86_64-windows.exe` (the Windows release exe) | Windows 10 / Server 2016+, x64 | Visual Studio Build Tools (C++) | `exe` |
| `aero1553-x86_64-linux-glibc2.22` (the SLES 12 SP5 release binary) | any x86-64 Linux | Clang, GCC, GNU binutils | `sles` |
| `aero1553` for the build machine itself | any x86-64 Linux | nothing | `cli` |
| the Python wheel and sdist | Windows x64 or x86-64 Linux | Python 3.10+ | `wheel` |

A wheel works on the kind of machine that built it: one built on a modern Linux needs that
machine's glibc or newer, so build it on the oldest system that will install it.

## 1. On a connected machine: gather the bundle

You need Python 3.10 or later, `git` and Rust (from [rustup.rs](https://rustup.rs)). Clone the
repository and check out the release you want to build:

```bash
git clone https://github.com/joey-huckabee/aero1553.git
cd aero1553
git checkout v5.11.0            # the release to build; or stay on main
python3 scripts/offline-bundle.py ../aero1553-bundle --ref v5.11.0
```

`scripts/offline-bundle.py` writes `aero1553-bundle/`, about 600 MB with both platforms:

| In the bundle | What it is |
|---|---|
| `source/` | the repository at the ref |
| `vendor/`, `.cargo/config.toml` | every crate the CLI, the Python extension and cargo-libc need, and the setting that makes Cargo use them and never the network. The CLI needs only `itoa` and `zmij` from crates.io; its CSV writer, tablekit's, is part of `source/` (`third_party/tablekit`). |
| `cargo-libc-<version>/` | cargo-libc's source, the version CI pins (Linux only) |
| `wheels/` | maturin, the Python build tool, one wheel per platform |
| `toolchains/` | the Rust standalone installers: `rust-<version>-x86_64-pc-windows-msvc.msi` and `rust-<version>-x86_64-unknown-linux-gnu.tar.xz` |
| `release/` | that release's own binaries, to compare your build against |
| `BUILDING.md` | this guide |
| `SHA256SUMS` | a checksum for every file, checked by the build script |

Every download is checked against the SHA-256 its publisher gives: crates.io, PyPI,
static.rust-lang.org, and the release notes on GitHub. The release binaries are fetched
anonymously, or with your GitHub sign-in if there is one (`gh auth token`, or a token in
`GH_TOKEN`), which lifts GitHub's rate limit; if they cannot be fetched the script leaves them
out and says so, and the build still works, without the comparison. Options:

- `--platform windows` or `--platform linux` gathers for one platform only (smaller).
- `--rust 1.99.1` picks the Rust version; the default is the connected machine's `rustc`.
  Anything from 1.88 (the crate's minimum) builds it. To reproduce a release build most
  closely, use the stable Rust of the day the release was built.
- `--no-rust-installers` leaves the installers out, if the disconnected machine already has
  Rust.

### What the bundle does not carry

The C toolchains are the operating system's, so gather them the usual way for your site:

**Windows: Visual Studio Build Tools.** Rust on Windows links with Microsoft's linker.
Download `vs_BuildTools.exe` from <https://aka.ms/vs/17/release/vs_BuildTools.exe>, then make
an offline copy (a few GB) and carry the folder:

```powershell
vs_BuildTools.exe --layout C:\vsbt --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --lang en-US
```

Its licence allows using the Build Tools to build open-source projects such as this one.
Also carry the Python installer from <https://www.python.org/downloads/windows/> (64-bit,
3.10 or later) if you want the wheel or the verification step.

**Linux, for the SLES binary: Clang, GCC and GNU binutils.** cargo-libc needs a Clang that
supports `--gcc-install-dir` (cargo-libc is tested with Clang 18, 21 and 24) and GCC 11 to 16.
Most distributions have them as packages (`clang`, `gcc`, `binutils`); a machine that builds
C already has GCC and binutils. Python 3 is needed for the wheel and verification steps.

Copy `aero1553-bundle/` (and `C:\vsbt` for Windows) to the disconnected machine by whatever
route your site allows. The bundle works from any directory.

## 2. On the disconnected Windows machine

1. **Install the Build Tools** from the layout, as an administrator:

   ```powershell
   C:\vsbt\vs_BuildTools.exe --noweb --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended
   ```

   If the installer reports a certificate error, install the certificates in
   `C:\vsbt\certificates` first (double-click each, install for the local machine).
2. **Install Rust**: run `aero1553-bundle\toolchains\rust-<version>-x86_64-pc-windows-msvc.msi`
   (administrator). Open a **new** terminal and check `cargo --version`.
3. **Install Python** if you carried it (tick "Add python.exe to PATH").
4. **Build**, in PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File aero1553-bundle\source\scripts\offline-build.ps1
   ```

   That runs every step. To run some, name them: `... offline-build.ps1 check exe verify`.

| Step | What it does |
|---|---|
| `check` | every file in the bundle against `SHA256SUMS` |
| `exe` | `cargo build --release --locked --offline` in `source\rust`; the C runtime is linked in by `source\rust\.cargo\config.toml` (`L3-RS-020`). Writes `out\aero1553-x86_64-windows.exe`. |
| `wheel` | maturin from the bundle builds `out\aero1553-<version>-cp310-abi3-win_amd64.whl` and the sdist |
| `verify` | `scripts\pe-imports.py` shows the exe imports no C runtime DLL, the conformance suite runs against it, and, if the bundle has the release exe, `scripts\compare-binaries.py --behaviour-only` shows the two behave the same on 182 runs |

The exe is in `aero1553-bundle\out\`. It needs nothing installed to run, so copy it anywhere.

## 3. On the disconnected Linux machine

```bash
bash aero1553-bundle/source/scripts/offline-build.sh
```

That runs every step; to run some, name them: `offline-build.sh check toolchain cli verify`.
No root is needed: the Rust installer goes into `aero1553-bundle/rust`.

| Step | What it does |
|---|---|
| `check` | every file in the bundle against `SHA256SUMS` |
| `toolchain` | installs the bundled Rust into `aero1553-bundle/rust` and uses it (or uses the `cargo` on `PATH` if the bundle has no installer) |
| `cli` | `cargo build --release --locked --offline`: `out/aero1553`, for this machine |
| `sles` | builds cargo-libc from the bundle, then the SLES 12 SP5 binary with `cargo libc build --distro sles-12-sp5` and checks no symbol is newer than glibc 2.22: `out/aero1553-x86_64-linux-glibc2.22` |
| `wheel` | maturin from the bundle builds the wheel and sdist into `out/` |
| `verify` | the conformance suite against each binary built, and, if the bundle has the release binary, `scripts/compare-binaries.py` against it |

Without Clang, skip the SLES step: `offline-build.sh check toolchain cli wheel verify`.

## 4. What "verify" proves, and what it can't

- **The conformance suite** (224 cases) runs your binary on every shared fixture and compares
  its CSV byte for byte with the oracles. A pass means it decodes as the released tool does.
- **The SHA-256 of your build will not match the release's.** A binary records details of the
  machine and toolchain that built it, so two good builds differ in bytes. That is expected and
  is why the comparison checks behaviour rather than bytes.
- **The comparison with the release binary** runs both on 182 invocations (every golden
  recording and conformance input, decode, count, dump, merge, errors) and requires the same
  output, errors and exit code. On Linux it also compares what each binary asks of the system
  (libraries, symbol versions). With a different Rust version than the release used, that
  list can differ slightly while the behaviour is identical; behaviour is what matters.

## 5. On a connected machine, without the bundle

The same builds, the usual way:

```bash
# The CLI for this machine (Linux, macOS, Windows): rust/target/release/aero1553[.exe]
cd rust && cargo build --release

# The Windows exe is the same command on Windows; the C runtime is linked in by
# rust/.cargo/config.toml. Check it with:
python scripts/pe-imports.py rust/target/release/aero1553.exe

# The SLES 12 SP5 binary (x86-64 Linux, Clang/GCC/binutils), as CI builds it:
cargo install --locked cargo-libc@0.6.0      # the version CI pins (CARGO_LIBC_VERSION)
cd rust && cargo libc build --distro sles-12-sp5 \
    --artifact target/x86_64-unknown-linux-gnu/release/aero1553 -- --release --locked
cargo libc inspect target/x86_64-unknown-linux-gnu/release/aero1553 --max-glibc 2.22

# The Python sdist and wheel (needs uv): python/dist/
uv --directory python build
```

### Checking the SLES 12 SP5 binary as CI does, WSL2 included

`scripts/sles-check.sh` is CI's whole check of that binary, so running it on any x86-64
Linux with Docker -- WSL2 on a Windows machine included -- gives CI's result:

```bash
sudo apt install clang gcc binutils python3 docker.io   # or Docker Desktop's WSL integration
cargo install --locked cargo-libc@0.6.0                  # the version CI pins
bash scripts/sles-check.sh                               # builds, then checks
```

It builds the binary with cargo-libc, rejects a symbol newer than GLIBC_2.22, runs the
tests with every syscall kernel 4.12 lacks returning ENOSYS (a simulation), runs the
conformance suite against the binary, then pulls SUSE's SLES 12 SP5 base image (pinned by
digest), checks SUSE's `libgcc_s.so.1` against the binary, checks every `GCC_*` import binds
to it at start-up, and runs the conformance suite on SUSE's loader, glibc and `libgcc_s`.
What it cannot cover is SUSE's kernel under that userland: that needs a real SLES 12 SP5
host or VM.

## 6. Troubleshooting

| Message | Cause and fix |
|---|---|
| `skipped: no GitHub release for vX.Y.Z (HTTP Error 404 ...)` | No release has that tag, or GitHub refused the request (its anonymous rate limit, for one). Check the tag; `gh auth login` or `GH_TOKEN` lifts the limit. |
| `offline-build: the bundle is damaged or incomplete` / `file(s) damaged or missing` | A file changed in transit. Copy the bundle again; `SHA256SUMS` names the files. |
| `failed to download` or `no matching package` from Cargo | Cargo ran outside the bundle, so it did not find `aero1553-bundle/.cargo/config.toml`. Use the scripts, or run Cargo from inside the bundle directory. |
| `linker 'link.exe' not found` | The Visual Studio Build Tools (C++ workload) are not installed. See section 2. |
| `cargo` is not recognized (Windows) | Open a new terminal after installing Rust, so `PATH` includes it. |
| `running scripts is disabled on this system` | Start the script as shown, with `powershell -ExecutionPolicy Bypass -File ...`. |
| `the SLES build needs clang` | Install Clang, GCC and binutils, or skip the `sles` step. |
| `--cc must be a Clang driver supporting --gcc-install-dir` | The Clang is too old for cargo-libc; install a newer one. |
| `GCC N built objects that differ from the qualified container build` | cargo-libc does not accept this GCC version; use GCC 11 to 16. |
| The exe shows "Windows protected your PC" | Only for a copy downloaded from the internet; your own build is not marked as downloaded. For a downloaded release exe: More info, then Run anyway. |
