"""Gather everything a build on a disconnected network needs (docs/BUILDING.md).

    python3 scripts/offline-bundle.py OUT [--ref REF] [--platform windows] [--platform linux]
                                          [--rust VERSION] [--cargo-libc VERSION]
                                          [--no-rust-installers] [--no-release-binaries]

Run on a connected machine, from a clone of the repository, with Python 3.10+, git and
Rust (cargo) installed. It writes the directory OUT, which is then copied to the
disconnected machine as it is:

    OUT/source/                    the repository at REF (git archive)
    OUT/vendor/ + OUT/.cargo/      every crate the Python extension and cargo-libc need,
                                   and the config that makes Cargo use them offline
    OUT/cargo-libc-VERSION/        cargo-libc's source (Linux only), to build it offline
    OUT/wheels/                    maturin, the Python build tool, for each platform
    OUT/toolchains/                the Rust standalone installers for each platform
    OUT/release/                   the release binaries of REF, to compare a build against
    OUT/SHA256SUMS                 a checksum for every file in OUT

The CLI needs only tablekit's CSV writer, which the repository vendors, and its itoa and
zmij (L3-RS-002); the other vendored crates are for the Python extension (PyO3) and for
cargo-libc. Every download is checked against the SHA-256 its
publisher gives (PyPI, crates.io, static.rust-lang.org, the GitHub release notes). Only
the standard library is used.

The C toolchains are not gathered: Visual Studio Build Tools on Windows and Clang, GCC and
binutils on Linux. docs/BUILDING.md says how to carry those.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "joey-huckabee/aero1553"
#: The release binary of each platform, named as each release attaches it.
RELEASE_BINARIES = {
    "linux": "aero1553-x86_64-linux-glibc2.22",
    "windows": "aero1553-x86_64-windows.exe",
}
RUST_INSTALLERS = {
    "linux": "rust-{v}-x86_64-unknown-linux-gnu.tar.xz",
    "windows": "rust-{v}-x86_64-pc-windows-msvc.msi",
}


def wheel_fits(filename: str, platform: str) -> bool:
    """An x86-64 wheel for ``platform``: manylinux on Linux (it installs on any glibc
    distribution newer than its tag), win_amd64 on Windows."""
    if not filename.endswith(".whl"):
        return False
    if platform == "windows":
        return filename.endswith("-win_amd64.whl")
    return "manylinux" in filename and filename.endswith("x86_64.whl")


USER_AGENT = "aero1553-offline-bundle (https://github.com/joey-huckabee/aero1553)"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect without the GitHub token. A release asset redirects to a
    pre-signed storage URL, which refuses a second credential and must never see the
    token anyway."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def github_token() -> str | None:
    """A GitHub token, if there is one: GH_TOKEN or GITHUB_TOKEN, else the GitHub
    CLI's (``gh auth login``). None if there is none."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def fetch_github(url: str, token: str | None, accept: str) -> bytes:
    """``url`` on api.github.com, with the token if there is one."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_DropAuthOnRedirect)
    with opener.open(urllib.request.Request(url, headers=headers), timeout=300) as response:
        return response.read()


def save(data: bytes, path: Path, sha256: str, what: str) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got != sha256.lower():
        sys.exit(f"{what}: SHA-256 {got} does not match the published {sha256}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"  {path.name} ({len(data):,} bytes, SHA-256 checked)")


def extract(archive: tarfile.TarFile, into: Path) -> None:
    """Extract with the "data" filter (no absolute paths, links out of ``into`` or
    device files) where this Python has it: 3.11.4, 3.12 and later."""
    if hasattr(tarfile, "data_filter"):
        archive.extractall(into, filter="data")
    else:  # pragma: no cover -- older 3.10 / 3.11
        archive.extractall(into)


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def source(out: Path, ref: str) -> str:
    """The repository at ``ref``; returns its crate version."""
    print(f"source: {ref}")
    data = subprocess.run(["git", "archive", "--format=tar", ref], cwd=ROOT, check=True,
                          capture_output=True).stdout  # fmt: skip
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        extract(archive, out / "source")
    cargo = (out / "source" / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', cargo, re.M)
    assert match, "rust/Cargo.toml declares no version"
    print(f"  aero1553 {match.group(1)}")
    return match.group(1)


def cargo_libc(out: Path, version: str) -> Path:
    print(f"cargo-libc {version}")
    meta = json.loads(fetch(f"https://crates.io/api/v1/crates/cargo-libc/{version}"))
    data = fetch(f"https://crates.io/api/v1/crates/cargo-libc/{version}/download")
    crate = out / f"cargo-libc-{version}.crate"
    save(data, crate, meta["version"]["checksum"], "cargo-libc")
    with tarfile.open(crate) as archive:
        extract(archive, out)
    crate.unlink()
    return out / f"cargo-libc-{version}"


def vendor(out: Path, manifests: list[Path]) -> None:
    """Every crate the manifests need, for every platform, and the config that points
    Cargo at them. The config sits in OUT/.cargo, so any Cargo run from inside OUT
    finds it; ``directory`` is relative to OUT."""
    print("vendored crates")
    first, *rest = manifests
    args = ["cargo", "vendor", "--locked", "--manifest-path", str(first)]
    for manifest in rest:
        args += ["--sync", str(manifest)]
    run(*args, str(out / "vendor"))
    (out / ".cargo").mkdir(exist_ok=True)
    (out / ".cargo" / "config.toml").write_text(
        "# Written by scripts/offline-bundle.py: Cargo uses the vendored crates and\n"
        "# never the network. Found by any Cargo command run inside this directory.\n"
        '[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
        '[source.vendored-sources]\ndirectory = "vendor"\n\n'
        "[net]\noffline = true\n",
        encoding="utf-8",
    )
    print(f"  {sum(1 for _ in (out / 'vendor').iterdir())} crates")


def maturin(out: Path, platforms: list[str], requirement: str) -> None:
    """The newest maturin release inside the pyproject's range, one wheel per platform."""
    low, high = re.fullmatch(r"maturin>=([\d.]+),<([\d.]+)", requirement).groups()  # type: ignore[union-attr]

    def key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    meta = json.loads(fetch("https://pypi.org/pypi/maturin/json"))
    versions = [
        v for v in meta["releases"] if re.fullmatch(r"[\d.]+", v) and key(low) <= key(v) < key(high)
    ]
    version = max(versions, key=key)
    print(f"maturin {version}")
    files = meta["releases"][version]
    for platform in platforms:
        wheels = [f for f in files if wheel_fits(f["filename"], platform)]
        if not wheels:
            sys.exit(f"maturin {version} publishes no {platform} x86-64 wheel")
        wheel = wheels[0]
        save(fetch(wheel["url"]), out / "wheels" / wheel["filename"], wheel["digests"]["sha256"],
             wheel["filename"])  # fmt: skip


def rust_installers(out: Path, platforms: list[str], version: str) -> None:
    print(f"Rust {version} standalone installers")
    for platform in platforms:
        name = RUST_INSTALLERS[platform].format(v=version)
        url = f"https://static.rust-lang.org/dist/{name}"
        digest = fetch(url + ".sha256").decode().split()[0]
        save(fetch(url), out / "toolchains" / name, digest, name)


def release_binaries(out: Path, platforms: list[str], tag: str) -> None:
    """The binaries the release of ``tag`` attaches, checked against the SHA-256 in its
    notes, so the offline build can be compared with what shipped."""
    print(f"release binaries of {tag}")
    # A token is optional: it lifts GitHub's anonymous rate limit, and reads the release if
    # the repository is ever private again.
    token = github_token()
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"
    try:
        meta = json.loads(fetch_github(url, token, "application/vnd.github+json"))
    except OSError as exc:
        hint = "" if token else "; `gh auth login` or GH_TOKEN lifts GitHub's rate limit"
        print(f"  skipped: no GitHub release for {tag} ({exc}){hint}")
        return
    for platform in platforms:
        name = RELEASE_BINARIES[platform]
        asset = next((a for a in meta["assets"] if a["name"] == name), None)
        digest = re.search(rf"`{re.escape(name)}` SHA-256: `([0-9a-f]{{64}})`", meta["body"])
        if asset is None or digest is None:
            print(f"  skipped: {tag} attaches no {name} with a SHA-256")
            continue
        data = fetch_github(asset["url"], token, "application/octet-stream")
        save(data, out / "release" / name, digest.group(1), name)


def checksums(out: Path) -> None:
    lines = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(out).as_posix()}")
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"SHA256SUMS: {len(lines):,} files")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path, help="The directory to write (must not exist).")
    parser.add_argument("--ref", default="HEAD", help="The git ref to build (default HEAD).")
    parser.add_argument(
        "--platform",
        action="append",
        choices=sorted(RELEASE_BINARIES),
        help="A platform to build on (repeatable; default both).",
    )
    parser.add_argument("--rust", help="Rust version (default: this machine's rustc).")
    parser.add_argument("--cargo-libc", help="cargo-libc version (default: the one CI pins).")
    parser.add_argument("--no-rust-installers", action="store_true")
    parser.add_argument("--no-release-binaries", action="store_true")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        sys.exit(f"{out} already exists")
    platforms = args.platform or sorted(RELEASE_BINARIES)
    out.mkdir(parents=True)

    version = source(out, args.ref)
    manifests = [out / "source" / "python" / "native" / "Cargo.toml"]
    if "linux" in platforms:
        ci = (out / "source" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pinned = re.search(r"CARGO_LIBC_VERSION: (\S+)", ci)
        assert pinned, "ci.yml pins no cargo-libc version"
        crate = cargo_libc(out, args.cargo_libc or pinned.group(1))
        manifests.append(crate / "Cargo.toml")
    vendor(out, manifests)
    pyproject = (out / "source" / "python" / "pyproject.toml").read_text(encoding="utf-8")
    requirement = re.search(r'requires = \["(maturin[^"]+)"\]', pyproject)
    assert requirement, "pyproject.toml names no maturin requirement"
    maturin(out, platforms, requirement.group(1))
    if not args.no_rust_installers:
        rust = args.rust or re.search(r"rustc (\S+)", run("rustc", "--version")).group(1)  # type: ignore[union-attr]
        rust_installers(out, platforms, rust)
    if not args.no_release_binaries:
        release_binaries(out, platforms, f"v{version}")
    guide = out / "source" / "docs" / "BUILDING.md"
    if guide.is_file():
        shutil.copy(guide, out / "BUILDING.md")
    checksums(out)
    print(f"\ndone: copy {out} to the disconnected machine; see BUILDING.md there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
