#!/usr/bin/env bash
# Build aero1553 on a disconnected Linux x86-64 machine (docs/BUILDING.md).
#
#     bash source/scripts/offline-build.sh [STEP...]
#
# Run from anywhere, on a bundle made by scripts/offline-bundle.py. STEP is one or
# more of the following, in order (default: all of them):
#
#   check      every file matches the bundle's SHA256SUMS
#   toolchain  install the bundled Rust into BUNDLE/rust (no root needed) and use it
#   cli        the CLI for this machine           -> BUNDLE/out/aero1553
#   sles       the SLES 12 SP5 release binary     -> BUNDLE/out/aero1553-x86_64-linux-glibc2.22
#              (needs Clang, GCC and GNU binutils; builds cargo-libc from the bundle)
#   wheel      the Python wheel and sdist         -> BUNDLE/out/*.whl, *.tar.gz
#   verify     the conformance suite against each binary built, and a comparison
#              with the release binary when the bundle carries one
#
# Nothing is downloaded: Cargo reads the bundle's vendored crates (BUNDLE/.cargo) with
# --offline, and its cache is BUNDLE/.cargo-home, not ~/.cargo. CI runs this script
# with the network cut (the offline-build-linux job), so the steps are proven.
set -euo pipefail

BUNDLE=$(cd "$(dirname "$0")/../.." && pwd)
SOURCE=$BUNDLE/source
OUT=$BUNDLE/out
export CARGO_HOME=$BUNDLE/.cargo-home
mkdir -p "$OUT"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'offline-build: %s\n' "$*" >&2; exit 1; }
# The first existing path among the (glob-expanded) arguments, or nothing.
first() {
    local path
    for path in "$@"; do
        if [[ -e $path ]]; then
            printf '%s\n' "$path"
            return
        fi
    done
}

use_bundled_rust() {
    if [[ -x $BUNDLE/rust/bin/cargo ]]; then
        export PATH=$BUNDLE/rust/bin:$PATH
    fi
}

step_check() {
    say "check: every file against SHA256SUMS"
    (cd "$BUNDLE" && sha256sum --quiet -c SHA256SUMS) || die "the bundle is damaged or incomplete"
    echo "all files match"
}

step_toolchain() {
    say "toolchain"
    local installer
    installer=$(first "$BUNDLE"/toolchains/rust-*-x86_64-unknown-linux-gnu.tar.xz)
    if [[ -x $BUNDLE/rust/bin/cargo ]]; then
        echo "already installed in $BUNDLE/rust"
    elif [[ -n $installer ]]; then
        local work=$BUNDLE/.toolchain-unpack
        rm -rf "$work" && mkdir -p "$work"
        tar -xJf "$installer" -C "$work"
        local install
        install=$(first "$work"/rust-*/install.sh)
        "$install" --prefix="$BUNDLE/rust" --disable-ldconfig >/dev/null
        rm -rf "$work"
        echo "installed $(basename "$installer") into $BUNDLE/rust"
    elif ! command -v cargo >/dev/null; then
        die "no Rust: the bundle has no Linux installer and cargo is not on PATH"
    fi
    use_bundled_rust
    echo "using $(cargo --version) / $(rustc --version)"
}

step_cli() {
    say "cli: the CLI for this machine"
    (cd "$SOURCE/rust" && cargo build --release --locked --offline)
    cp "$SOURCE/rust/target/release/aero1553" "$OUT/aero1553"
    "$OUT/aero1553" --version
}

step_sles() {
    say "sles: the SLES 12 SP5 binary (glibc 2.22, kernel 4.12)"
    local tool
    for tool in clang gcc ld; do
        command -v "$tool" >/dev/null || die "the SLES build needs $tool (Clang, GCC and GNU binutils)"
    done
    local crate
    crate=$(first "$BUNDLE"/cargo-libc-*)
    [[ -n $crate ]] || die "the bundle has no cargo-libc (made with --platform windows only?)"
    (cd "$crate" && cargo build --release --locked --offline)
    local cargo_libc=$crate/target/release/cargo-libc
    local built=target/x86_64-unknown-linux-gnu/release/aero1553
    (cd "$SOURCE/rust" && "$cargo_libc" libc build --distro sles-12-sp5 --artifact "$built" \
        -- --release --locked --offline)
    "$cargo_libc" libc inspect "$SOURCE/rust/$built" --max-glibc 2.22
    cp "$SOURCE/rust/$built" "$OUT/aero1553-x86_64-linux-glibc2.22"
    "$OUT/aero1553-x86_64-linux-glibc2.22" --version
}

step_wheel() {
    say "wheel: the Python wheel and sdist"
    local wheel
    wheel=$(first "$BUNDLE"/wheels/maturin-*manylinux*x86_64.whl)
    [[ -n $wheel ]] || die "the bundle has no Linux maturin wheel"
    # maturin's wheel is one executable; take it out rather than pip-installing it.
    python3 - "$wheel" "$BUNDLE/.tools" <<'PY'
import sys, zipfile, pathlib, stat
wheel, tools = sys.argv[1], pathlib.Path(sys.argv[2])
tools.mkdir(exist_ok=True)
with zipfile.ZipFile(wheel) as z:
    member = next(n for n in z.namelist() if n.endswith(".data/scripts/maturin"))
    target = tools / "maturin"
    target.write_bytes(z.read(member))
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
PY
    (cd "$SOURCE/python" && "$BUNDLE/.tools/maturin" build --release --locked --offline -o "$OUT")
    (cd "$SOURCE/python" && "$BUNDLE/.tools/maturin" sdist -o "$OUT")
    ls -l "$OUT"/*.whl "$OUT"/*.tar.gz
}

step_verify() {
    say "verify"
    local binary any=0
    for binary in "$OUT/aero1553" "$OUT/aero1553-x86_64-linux-glibc2.22"; do
        [[ -x $binary ]] || continue
        any=1
        echo "conformance suite against $(basename "$binary"):"
        python3 "$SOURCE/tests/conformance/run.py" --only rust --rust-bin "$binary" | tail -1
    done
    [[ $any == 1 ]] || die "nothing to verify: build cli or sles first"
    local release=$BUNDLE/release/aero1553-x86_64-linux-glibc2.22
    if [[ -f $release && -x $OUT/aero1553-x86_64-linux-glibc2.22 ]]; then
        echo "comparison with the release binary (same program expected; see BUILDING.md):"
        python3 "$SOURCE/scripts/compare-binaries.py" "$release" "$OUT/aero1553-x86_64-linux-glibc2.22"
    fi
}

steps=("$@")
[[ ${#steps[@]} -gt 0 ]] || steps=(check toolchain cli sles wheel verify)
use_bundled_rust
for step in "${steps[@]}"; do
    case $step in
        check | toolchain | cli | sles | wheel | verify) "step_$step" ;;
        *) die "unknown step '$step' (check, toolchain, cli, sles, wheel, verify)" ;;
    esac
done
say "done: the results are in $OUT"
