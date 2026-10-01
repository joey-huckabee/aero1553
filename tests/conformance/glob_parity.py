"""Differential parity check for what ``--glob`` actually resolves to.

L2-MRG-001 says every implementation "SHALL expand it identically", and until
v2.17.0 the only thing any test compared was the **wildcard matcher**. Each
implementation had its own unit tests for ``*`` and ``?``, all three agreed, and
all three resolved different input sets anyway — because the disagreements were
never about wildcards:

* **Directory entries.** Python and Rust filtered to files; C++ matched every
  name ``list_directory`` returned. A directory called ``archive.mie`` was an
  input on one implementation and invisible on the other two, and the one that
  took it then failed to map it — so a batch that decoded whole twice came back
  short the third time.
* **Symlinks.** Python's ``entry.is_file()`` follows them; Rust's
  ``DirEntry::file_type`` does not. A symlinked recording was kept by one and
  dropped by the other.
* **Separators.** C++ split on ``\\`` on both platforms. On POSIX a backslash is
  an ordinary filename character, so ``odd\\name*.mie`` named one file in the
  current directory to two implementations and a pattern inside a directory
  called ``odd`` to the third.

None of those is visible to a matcher test, and none is visible to a
single-implementation test either — each implementation's unit tests pin it
against its own reading, which is the thing in question when two readings
differ. So this compares the **resolved set**, across every implementation under
test, on a directory built to contain exactly the entries they disagreed about.

The set is read back through the CLI rather than through a library call, because
the CLI is the only surface all three share. ``decode --glob`` over a multi-file
merge writes one CSV whose row count is a function of how many inputs were
resolved, so a recording of a known record count per matched file turns "which
files matched" into an integer the three must agree on. (``count`` would be the
more direct spelling, but it takes a single input by design -- ``--glob`` is a
``decode`` flag.)

Cases that depend on platform capabilities (symlinks, backslash filenames) skip
themselves rather than fail, and the skips are reported so a silently-shrinking
corpus stays visible.

Run automatically by ``run.py`` when two or more implementations are under test.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from differential import describe_divergence

#: A case yields ``(directory, glob pattern, expected matched-file count)``, or
#: ``None`` to skip itself on this platform.
CaseResult = tuple[Path, str, int]
Case = tuple[str, Callable[[Path, bytes], CaseResult | None]]

#: Records per materialized recording. The merge writes the total across every
#: matched file, so the CSV holds ``RECORDS_PER_FILE * matched`` data rows --
#: which is what makes "how many files matched" observable from outside.
RECORDS_PER_FILE = 1


def _plain_files(temp: Path, recording: bytes) -> CaseResult:
    """The baseline. Two matching recordings and one non-matching name."""
    d = temp / "glob-plain"
    d.mkdir(exist_ok=True)
    (d / "a.mie").write_bytes(recording)
    (d / "b.mie").write_bytes(recording)
    (d / "notes.txt").write_bytes(recording)
    return (d, "*.mie", 2)


def _directory_named_like_a_recording(temp: Path, recording: bytes) -> CaseResult:
    """A DIRECTORY whose name matches the pattern is not an input.

    The C++ divergence: it matched, and the merge then failed to open it.
    """
    d = temp / "glob-dir"
    d.mkdir(exist_ok=True)
    (d / "a.mie").write_bytes(recording)
    (d / "b.mie").write_bytes(recording)
    (d / "archive.mie").mkdir(exist_ok=True)
    return (d, "*.mie", 2)


def _symlink_to_a_recording(temp: Path, recording: bytes) -> CaseResult | None:
    """A symlink to a recording IS a recording.

    The Rust divergence: ``DirEntry::file_type`` does not follow links, so this
    one was dropped there and kept in Python. Skipped where symlink creation is
    not permitted (unprivileged Windows).
    """
    d = temp / "glob-symlink"
    d.mkdir(exist_ok=True)
    real = d / "real.mie"
    real.write_bytes(recording)
    link = d / "link.mie"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        return None
    return (d, "*.mie", 2)


def _dangling_symlink(temp: Path, recording: bytes) -> CaseResult | None:
    """A broken link is not a recording, and must not fail the run either.

    Resolving it would only fail to open a moment later, so the expansion drops
    it -- and every implementation has to drop it at the same point, or one
    reports a missing file where the others report a smaller batch.
    """
    d = temp / "glob-dangling"
    d.mkdir(exist_ok=True)
    (d / "real.mie").write_bytes(recording)
    link = d / "dangling.mie"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(d / "absent-target.bin")
    except (OSError, NotImplementedError):
        return None
    return (d, "*.mie", 1)


def _backslash_is_not_a_separator_on_posix(temp: Path, recording: bytes) -> CaseResult | None:
    """On POSIX a backslash is an ordinary filename character.

    Windows genuinely disagrees -- a backslash IS a separator there, and is not
    even a legal filename character -- so this case is POSIX-only rather than
    skipped-on-failure: it is not a capability question.
    """
    if os.name == "nt":
        return None
    d = temp / "glob-backslash"
    d.mkdir(exist_ok=True)
    (d / "odd\\name.mie").write_bytes(recording)
    (d / "plain.mie").write_bytes(recording)
    return (d, "odd\\name*.mie", 1)


def _matches_nothing(temp: Path, _recording: bytes) -> CaseResult:
    """A pattern matching nothing is a usage error naming the pattern, in every
    implementation -- not a silent empty decode and not an I/O failure."""
    d = temp / "glob-empty"
    d.mkdir(exist_ok=True)
    return (d, "*.nosuchextension", 0)


CASES: list[Case] = [
    ("plain-files", _plain_files),
    ("directory-named-like-a-recording", _directory_named_like_a_recording),
    ("symlink-to-a-recording", _symlink_to_a_recording),
    ("dangling-symlink", _dangling_symlink),
    ("backslash-not-a-separator", _backslash_is_not_a_separator_on_posix),
    ("matches-nothing", _matches_nothing),
]


def _data_rows(csv_path: Path) -> int | None:
    """Data rows in a decoded CSV -- every line past the header.

    ``None`` when the file is absent or headerless, so "the decode did not run"
    reads as a failure rather than as zero rows.
    """
    if not csv_path.exists():
        return None
    text = csv_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line]
    if not lines or not lines[0].startswith("TIME_STAMP,"):
        return None
    return len(lines) - 1


def check_glob_parity(invocations: dict[str, list[str]], root: Path, temp: Path) -> None:
    """Drive ``CASES`` through every implementation; raise on any divergence.

    Both halves are compared: the implementations must agree with **each other**
    (a divergence is a finding regardless of which is right), and the agreed
    answer must be the one L2-MRG-001 specifies (unanimity on a wrong answer is
    a specification problem, not an implementation one -- and all three DID
    agree on the wildcards while resolving different sets).

    Raises:
        AssertionError: if the implementations diverge on any case, or agree
            on an answer other than the one L2-MRG-001 specifies.
    """
    recording = (temp / "glob-parity-source.mie").read_bytes()
    failures: list[str] = []
    skipped: list[str] = []
    checked = 0

    for name, build in CASES:
        case = build(temp, recording)
        if case is None:
            skipped.append(name)
            continue
        directory, pattern, expect_files = case
        checked += 1

        observed: dict[str, str] = {}
        for impl, prefix in invocations.items():
            out = temp / f"glob-parity-{name}-{impl}.csv"
            result = subprocess.run(
                [
                    *prefix,
                    "decode",
                    "--glob",
                    str(directory / pattern),
                    "-o",
                    str(out),
                    "--no-mux",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if expect_files == 0:
                # Nothing matched: the CLI turns that into a usage error naming
                # the pattern. Compare the exit code, since there are no rows.
                observed[impl] = f"exit {result.returncode}"
                continue
            rows = _data_rows(out)
            observed[impl] = (
                f"{rows} rows"
                if result.returncode == 0 and rows is not None
                else f"exit {result.returncode} ({result.stderr.strip()[:120]})"
            )

        divergence = describe_divergence(observed)
        if divergence is not None:
            failures.append(f"{name}: DIVERGENT -- {divergence}")
            continue

        agreed = next(iter(observed.values()))
        expected = "exit 4" if expect_files == 0 else f"{expect_files * RECORDS_PER_FILE} rows"
        if agreed != expected:
            failures.append(f"{name}: all reported {agreed!r}, expected {expected!r}")

    if failures:
        raise AssertionError("glob-expansion parity failures:\n  " + "\n  ".join(failures))
    note = f" ({len(skipped)} skipped: {', '.join(skipped)})" if skipped else ""
    print(f"PASS glob-parity ({checked} cases across {', '.join(invocations)}){note}")
