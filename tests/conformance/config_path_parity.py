"""Differential parity check for how the two CLIs handle the ``--config`` *path*.

``config_parity.py`` and ``config_fuzz.py`` both vary the config file's
**contents** and hold the two parsers to the same accept/reject class. Neither
varies the **path**, so everything the path itself decides — what counts as a
usable config file, which exit code a bad one produces, and what the operator is
told — was pinned only by per-implementation unit tests that could drift apart
without any cross-implementation check noticing.

That surface is the one documented as a promise in
``docs/CONFIG-REFERENCE.md`` §"Trust boundary": the path must resolve to a
**regular file**, a missing or unusable one is a configuration error (exit ``5``)
and never a silent fallback to defaults, and the file may live at **any readable
location** — traversal segments and absolute paths included, because a caller who
can pass ``--config`` can already read that file directly.

This module asserts the two implementations agree on all of it, and — unlike the
content corpus, which only compares accept/reject classes — compares the **exact
exit code** and requires the promised message text in both. The v2.11.0 claim
that both loaders reject a non-regular file "with identical message text" was
previously untested across implementations.

Cases that depend on platform capabilities (character devices, symlinks) skip
themselves rather than fail, and the skips are reported so a silently-shrinking
corpus is visible.

Run automatically by ``run.py`` when both implementations are under test.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from differential import describe_divergence

#: A case yields ``(path to pass to --config, expected exit code, message
#: substring both implementations must print)``, or ``None`` to skip itself on
#: this platform. ``0`` expects a clean decode; ``5`` is the configuration-error
#: exit (L2-CLI-011).
CaseResult = tuple[str, int, str | None]
Case = tuple[str, Callable[[Path], CaseResult | None]]

_VALID_TOML = "[decode]\nstrict = true\n"


def _regular_file(temp: Path) -> CaseResult:
    """The ordinary case: a plain readable TOML file is accepted."""
    cfg = temp / "path-regular.toml"
    cfg.write_text(_VALID_TOML, encoding="utf-8")
    return (str(cfg), 0, None)


def _missing(temp: Path) -> CaseResult:
    """A path that does not exist is a config error, never a silent default."""
    return (str(temp / "path-does-not-exist.toml"), 5, "Config file not found")


def _directory(temp: Path) -> CaseResult:
    """A directory is not a regular file. Reading it would raise a raw
    ``IsADirectoryError`` in Python and an opaque I/O error in Rust."""
    d = temp / "path-directory.toml"
    d.mkdir(exist_ok=True)
    return (str(d), 5, "not a regular file")


def _spaces_in_name(temp: Path) -> CaseResult:
    """Spaces are ordinary path characters, not an argument separator."""
    cfg = temp / "path with spaces.toml"
    cfg.write_text(_VALID_TOML, encoding="utf-8")
    return (str(cfg), 0, None)


def _non_ascii_name(temp: Path) -> CaseResult:
    """A non-ASCII file name is a path, not an identifier — the ASCII-only rule
    that governs config *keys* must not leak into path handling."""
    cfg = temp / "config-ünïcode-配置.toml"
    cfg.write_text(_VALID_TOML, encoding="utf-8")
    return (str(cfg), 0, None)


def _dot_dot_traversal(temp: Path) -> CaseResult:
    """``..`` segments resolve normally and are accepted.

    Pinned deliberately: the location of a config is unrestricted by design, so a
    traversing path is ordinary input, not an attack to be blocked. If that
    decision is ever revisited, this case fails and forces the doc to be updated
    with it.
    """
    nested = temp / "path-nested"
    nested.mkdir(exist_ok=True)
    cfg = nested / "traversal.toml"
    cfg.write_text(_VALID_TOML, encoding="utf-8")
    return (str(nested / ".." / "path-nested" / "traversal.toml"), 0, None)


def _character_device(_temp: Path) -> CaseResult | None:
    """``/dev/zero`` would read forever and ``/dev/null`` parses as an empty
    config; both are rejected up front as non-regular files. POSIX only."""
    if os.name == "nt" or not Path("/dev/null").exists():
        return None
    return ("/dev/null", 5, "not a regular file")


def _symlink_to_regular(temp: Path) -> CaseResult | None:
    """A symlink to a regular file is itself usable — ``is_file()`` follows the
    link, so site configs behind a symlinked path keep working. Skipped where
    symlink creation is not permitted (unprivileged Windows)."""
    target = temp / "path-symlink-target.toml"
    target.write_text(_VALID_TOML, encoding="utf-8")
    link = temp / "path-symlink.toml"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return None
    return (str(link), 0, None)


CASES: list[Case] = [
    ("regular-file", _regular_file),
    ("missing", _missing),
    ("directory", _directory),
    ("spaces-in-name", _spaces_in_name),
    ("non-ascii-name", _non_ascii_name),
    ("dot-dot-traversal", _dot_dot_traversal),
    ("character-device", _character_device),
    ("symlink-to-regular", _symlink_to_regular),
]


def check_config_path_parity(
    invocations: dict[str, list[str]], root: Path, input_mie: Path, temp: Path
) -> None:
    """Drive ``CASES`` through every implementation; raise on any divergence or
    mismatch.

    Compares the EXACT exit code rather than mere accept/reject: these cases pin
    specific codes (a missing config is 5, not merely "rejected"), so collapsing
    them to a verdict would let a config error and a usage error look alike.

    ``input_mie`` is a materialized, valid single-record recording, so a usable
    config decodes to exit 0 and only the ``--config`` path differs between cases.

    Raises:
        AssertionError: if the implementations' exit codes diverge on any case,
            agree on a code other than the expected one, or any stderr lacks
            the case's expected message.
    """
    failures: list[str] = []
    skipped: list[str] = []
    checked = 0

    for name, build in CASES:
        case = build(temp)
        if case is None:
            skipped.append(name)
            continue
        cfg_path, expect_code, expect_msg = case
        checked += 1
        codes: dict[str, int] = {}
        stderrs: dict[str, str] = {}
        for impl, prefix in invocations.items():
            out = temp / f"path-parity-{name}-{impl}.csv"
            result = subprocess.run(
                [*prefix, "--config", cfg_path, "decode", str(input_mie), "-o", str(out)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            codes[impl] = result.returncode
            stderrs[impl] = result.stderr

        divergence = describe_divergence({impl: f"exit {code}" for impl, code in codes.items()})
        if divergence is not None:
            failures.append(f"{name}: DIVERGENT exit codes — {divergence}")
            continue
        agreed = next(iter(codes.values()))
        if agreed != expect_code:
            # Unanimous and unanimously wrong: a specification or case
            # problem rather than an implementation one.
            failures.append(f"{name}: all exited {agreed}, expected {expect_code}")
            continue
        if expect_msg is not None:
            for impl, err in stderrs.items():
                if expect_msg not in err:
                    failures.append(
                        f"{name}: {impl} stderr missing {expect_msg!r} — got {err.strip()[:160]!r}"
                    )

    if failures:
        raise AssertionError("config-path parity failures:\n  " + "\n  ".join(failures))
    note = f" ({len(skipped)} skipped: {', '.join(skipped)})" if skipped else ""
    print(f"PASS config-path-parity ({checked} cases across {', '.join(invocations)}){note}")
