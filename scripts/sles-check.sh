#!/usr/bin/env bash
# Build the SLES 12 SP5 binary with cargo-libc and check it on SUSE's own SLES 12 SP5
# userland (L3-RS-018). CI runs this (the rust-glibc-2-22 job); so can any x86-64 Linux
# with Docker, WSL2 included, so a local result is the CI result.
#
#     bash scripts/sles-check.sh [--no-build] [--keep]
#
# Needs: cargo-libc at the version CI pins (CARGO_LIBC_VERSION in .github/workflows/ci.yml),
# Clang, GCC, GNU binutils, Python 3 and Docker. Steps, each failing the script:
#
#   1. build    cargo libc build --distro sles-12-sp5           (skip with --no-build)
#   2. glibc    no symbol newer than GLIBC_2.22
#   3. kernel   the Rust tests with every syscall kernel 4.12 lacks returning ENOSYS
#               (cargo-libc's simulation, not a real kernel 4.12)
#   4. suite    the conformance suite against the binary
#   5. sles     SUSE's SLES 12 SP5 base image (pinned by digest): its libgcc_s checked
#               against the binary's needs, every GCC_* import bound to SUSE's libgcc_s at
#               start-up, and the conformance suite run on SUSE's loader, glibc and libgcc_s
#
# What it cannot test: SUSE's kernel under that userland. That needs a real SLES 12 SP5
# host or VM.
set -euo pipefail

SLES_IMAGE=registry.suse.com/suse/sles12sp5@sha256:4c7d476a692d4da927397d7fe4e6e9aaa93a2dea6927a69a45cb9a1fcd56ba35
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BIN=$ROOT/rust/target/x86_64-unknown-linux-gnu/release/aero1553
build=1
keep=0
for arg in "$@"; do
    case $arg in
        --no-build) build=0 ;;
        --keep) keep=1 ;;
        *) echo "usage: $0 [--no-build] [--keep]" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
notice() {
    if [[ -n ${GITHUB_ACTIONS:-} ]]; then echo "::notice::$*"; else echo "note: $*"; fi
}

[[ $(uname -m) == x86_64 ]] || { echo "sles-check: needs an x86-64 machine (this is $(uname -m))" >&2; exit 1; }
for tool in cargo-libc python3 docker objdump; do
    command -v "$tool" >/dev/null || { echo "sles-check: $tool not found (see the header of $0)" >&2; exit 1; }
done
pinned=$(sed -n 's/^ *CARGO_LIBC_VERSION: *//p' "$ROOT/.github/workflows/ci.yml" | head -1)
# The version the tool reports, else what `cargo install` recorded. CI restores the tool
# from a cache, so `cargo install --list` alone does not see it there.
have=$(cargo libc --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
[[ -n $have ]] || have=$(cargo install --list | sed -n 's/^cargo-libc v\([^ :]*\).*/\1/p')
if [[ -z $have ]]; then
    notice "could not tell cargo-libc's version; CI pins $pinned"
elif [[ $have != "$pinned" ]]; then
    echo "sles-check: cargo-libc $have, but CI pins $pinned: cargo install --locked cargo-libc@$pinned" >&2
    exit 1
fi

if ((build)); then
    say "build: cargo-libc $have, --distro sles-12-sp5"
    (cd "$ROOT/rust" && cargo libc build --distro sles-12-sp5 \
        --artifact target/x86_64-unknown-linux-gnu/release/aero1553 -- --release --locked)
fi
[[ -x $BIN ]] || { echo "sles-check: no binary at $BIN; run without --no-build" >&2; exit 1; }

say "glibc: no symbol newer than GLIBC_2.22"
cargo libc inspect "$BIN" --max-glibc 2.22

say "kernel: the tests under a simulated kernel 4.12"
(cd "$ROOT/rust" && cargo libc test --distro sles-12-sp5 --runner local -- --release --locked)

say "suite: the conformance suite against the binary"
python3 "$ROOT/tests/conformance/run.py" --only rust --rust-bin "$BIN"

say "sles: SUSE's SLES 12 SP5 userland"
work=$(mktemp -d)
cleanup() {
    if ((keep)); then echo "kept $work"; else rm -rf "$work"; fi
}
trap cleanup EXIT
sles=$work/sles12sp5
mkdir -p "$sles"
cid=$(docker create "$SLES_IMAGE" /bin/true)
docker export "$cid" | tar -x -C "$sles" --no-same-owner --exclude='dev/*'
docker rm "$cid" >/dev/null
grep -E '^(NAME|VERSION)=' "$sles/etc/os-release"
# cargo-libc's comparison: "missing" (2) fails. On a stripped binary 0.5.0 reports
# "unresolved" (3) for the symbols, though the version nodes match; the binding check
# below covers the symbols.
rc=0
cargo libc inspect "$BIN" --target-libgcc "$sles/lib64/libgcc_s.so.1" --max-glibc 2.22 || rc=$?
case $rc in
    0) ;;
    3) notice "cargo-libc left the libgcc_s symbol comparison unresolved (stripped binary); see the binding check" ;;
    *) exit "$rc" ;;
esac
on_sles=$work/on-sles
printf '#!/bin/sh\nexec "%s" --library-path "%s" "%s" "$@"\n' \
    "$sles/lib64/ld-linux-x86-64.so.2" "$sles/lib64:$sles/usr/lib64" "$BIN" > "$on_sles"
chmod +x "$on_sles"
"$on_sles" --version
# The binary is linked BIND_NOW, so SUSE's loader binds every import at start-up: each
# GCC_* import must bind to SUSE's libgcc_s.so.1.
want=$(objdump -T "$BIN" | awk '/\(GCC_/ {print $NF}' | sort)
got=$(LD_DEBUG=bindings "$on_sles" --version 2>&1 >/dev/null \
    | grep -F "binding file $BIN [0] to " \
    | sed -n "s/.*\/libgcc_s\.so\.1 \[0\]: normal symbol \`\([^']*\)'.*/\1/p" | sort -u)
[[ -n $want ]] || { echo "sles-check: the binary imports nothing from libgcc_s" >&2; exit 1; }
diff <(echo "$want") <(echo "$got")
echo "all $(echo "$want" | wc -l) GCC_* imports bind to SUSE's libgcc_s.so.1"
python3 "$ROOT/tests/conformance/run.py" --only rust --rust-bin "$on_sles"

say "passed: glibc 2.22, simulated kernel 4.12, SUSE's SLES 12 SP5 userland"
echo "not covered: SUSE's kernel; that needs a real SLES 12 SP5 host or VM"
