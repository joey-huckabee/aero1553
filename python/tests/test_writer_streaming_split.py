"""Streaming-specific behavior tests for ``write_csv_split`` (PY-streaming).

Covers the split partial-commit path when error rows are present before
an unrecoverable sync loss — both the main and errors temp files are
renamed to ``.partial`` and surfaced on the WriteOutcome. The
main-before-errors commit ordering and errors-commit-failure cleanup are
covered by ``test_e2e.py``; the byte-image equivalence by
``test_writer_streaming_golden.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aero1553.exceptions import (
    MieClobberRefusedError,
    MieUnrecoverableSyncLossError,
    MieWriterError,
)
from aero1553.models import MieMessage
from aero1553.reader import MieFileReader
from aero1553.writer import WriteOptions, write_csv_split
from tests.conftest import RECORD_RT15_SA11_RCV, replace


def _normal_and_errored(tmp_path: Path) -> tuple[MieMessage, MieMessage]:
    fpath = tmp_path / "in.mie"
    fpath.write_bytes(RECORD_RT15_SA11_RCV)
    normal = next(iter(MieFileReader(fpath)))
    errored = replace(normal, type_word=replace(normal.type_word, error=True))
    assert errored.error_label == "ERROR"
    return normal, errored


@pytest.mark.requirement("L2-WRT-016")
@pytest.mark.requirement("L1-EXIT-004")
def test_split_allow_partial_with_errors_commits_both_partials(
    tmp_path: Path,
) -> None:
    """A sync loss after a normal + an errored row commits BOTH files as
    ``.partial`` and reports them on the outcome."""
    normal, errored = _normal_and_errored(tmp_path)

    def stream() -> Iterator[MieMessage]:
        yield normal
        yield errored
        raise MieUnrecoverableSyncLossError(offset=0x1234, sync_losses=2)

    dest = tmp_path / "out.csv"
    outcome = write_csv_split(stream(), dest, WriteOptions(allow_partial=True))

    assert outcome.partial is not None
    assert outcome.partial.offset == 0x1234
    assert outcome.partial.sync_losses == 2
    assert outcome.normal_count == 1
    assert outcome.error_count == 1

    main_partial = dest.with_name("out.csv.partial")
    errors_partial = (tmp_path / "out_errors.csv").with_name("out_errors.csv.partial")
    assert outcome.partial.main_path == main_partial
    assert outcome.partial.errors_path == errors_partial

    # The non-.partial destinations must NOT exist (loss was unrecoverable).
    assert not dest.exists()
    assert not (tmp_path / "out_errors.csv").exists()

    # Both partials hold their header + one row, LF-only.
    main_bytes = main_partial.read_bytes()
    err_bytes = errors_partial.read_bytes()
    assert main_bytes.startswith(b"TIME_STAMP,RT,MSG,")
    assert main_bytes.count(b"\n") == 2  # header + 1 normal row
    assert b"\r" not in main_bytes
    assert err_bytes.count(b"\n") == 2  # header + 1 error row
    assert b",ERROR," in err_bytes

    # No temp files left behind.
    assert list(tmp_path.glob("*.aero1553.tmp.*")) == []


@pytest.mark.requirement("L2-WRT-016")
@pytest.mark.requirement("L1-EXIT-004")
def test_split_allow_partial_no_errors_omits_errors_partial(
    tmp_path: Path,
) -> None:
    """When no error rows precede the sync loss, only the main ``.partial``
    is committed and ``errors_path`` stays None."""
    normal, _ = _normal_and_errored(tmp_path)

    def stream() -> Iterator[MieMessage]:
        yield normal
        raise MieUnrecoverableSyncLossError(offset=0x10, sync_losses=1)

    dest = tmp_path / "out.csv"
    outcome = write_csv_split(stream(), dest, WriteOptions(allow_partial=True))

    assert outcome.partial is not None
    assert outcome.partial.errors_path is None
    assert outcome.error_count == 0
    assert (dest.with_name("out.csv.partial")).exists()
    assert not (tmp_path / "out_errors.csv.partial").exists()
    assert list(tmp_path.glob("*.aero1553.tmp.*")) == []


@pytest.mark.requirement("L2-WRT-016")
@pytest.mark.requirement("L2-WRT-019")
def test_split_partial_main_commit_failure_leaves_no_orphan_errors_partial(
    tmp_path: Path,
) -> None:
    """The ``--allow-partial`` half of the main-before-errors rule.

    Force the MAIN ``.partial`` rename to fail (a directory sits on it) and
    assert that no errors ``.partial`` appears. Before the fix this branch
    committed errors first, so the errors ``.partial`` was already on disk by
    the time the main one failed -- an orphan forensic artifact with no main
    output beside it, which is precisely the residue the order exists to make
    impossible.
    """
    normal, errored = _normal_and_errored(tmp_path)

    def stream() -> Iterator[MieMessage]:
        yield normal
        yield errored
        raise MieUnrecoverableSyncLossError(offset=0x99, sync_losses=2)

    dest = tmp_path / "out.csv"
    main_partial = dest.with_name("out.csv.partial")
    errors_partial = tmp_path / "out_errors.csv.partial"
    main_partial.mkdir()

    # Built outside the `raises` block so only `write_csv_split` can satisfy it
    # -- a generator or options constructor that threw would otherwise pass this
    # test for the wrong reason (S5778).
    rows = stream()
    options = WriteOptions(allow_partial=True)
    with pytest.raises(MieWriterError):
        write_csv_split(rows, dest, options)

    assert not errors_partial.exists(), (
        "errors .partial must not appear when the main .partial commit fails first"
    )
    assert not dest.exists()
    assert not (tmp_path / "out_errors.csv").exists()
    assert list(tmp_path.glob("*.aero1553.tmp.*")) == []


@pytest.mark.requirement("L2-WRT-016")
@pytest.mark.requirement("L2-WRT-019")
def test_split_partial_errors_commit_failure_leaves_the_main_partial(
    tmp_path: Path,
) -> None:
    """The mirror image: the MAIN ``.partial`` commits, then the errors one
    fails. The residue is the primary artifact, which is the whole point."""
    normal, errored = _normal_and_errored(tmp_path)

    def stream() -> Iterator[MieMessage]:
        yield normal
        yield errored
        raise MieUnrecoverableSyncLossError(offset=0x99, sync_losses=2)

    dest = tmp_path / "out.csv"
    main_partial = dest.with_name("out.csv.partial")
    errors_partial = tmp_path / "out_errors.csv.partial"
    errors_partial.mkdir()

    rows = stream()
    options = WriteOptions(allow_partial=True)
    with pytest.raises(MieWriterError):
        write_csv_split(rows, dest, options)

    assert main_partial.read_bytes().startswith(b"TIME_STAMP,RT,MSG,"), (
        "the main .partial must survive an errors-commit failure"
    )
    assert errors_partial.is_dir(), "the errors .partial target should be untouched"
    assert list(tmp_path.glob("*.aero1553.tmp.*")) == []


@pytest.mark.requirement("L2-WRT-017")
@pytest.mark.requirement("L2-WRT-023")
def test_split_no_clobber_refuses_an_errors_file_that_appears_mid_decode(
    tmp_path: Path,
) -> None:
    """``WriteOptions.no_clobber`` has to reach the commit, not just the
    pre-flight -- and it has to reach the *errors* writer as well as the main
    one. The errors destination is created while the stream is still draining,
    which is after its pre-flight and before its commit."""
    normal, errored = _normal_and_errored(tmp_path)
    errors_dest = tmp_path / "out_errors.csv"

    def stream() -> Iterator[MieMessage]:
        yield normal
        yield errored
        errors_dest.write_text("theirs\n")

    dest = tmp_path / "out.csv"
    rows = stream()
    options = WriteOptions(no_clobber=True)
    with pytest.raises(MieClobberRefusedError):
        write_csv_split(rows, dest, options)

    assert errors_dest.read_text() == "theirs\n", "a refused commit must not touch it"
    # Main is committed first and its own destination was free, so it survives.
    assert dest.read_bytes().startswith(b"TIME_STAMP,RT,MSG,")
    assert list(tmp_path.glob("*.aero1553.tmp.*")) == []
