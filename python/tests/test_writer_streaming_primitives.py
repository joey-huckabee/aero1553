"""Unit tests for the PY-streaming primitives in ``writer.py``.

Exercises ``_AtomicCsvFile`` (temp + atomic rename / partial / cleanup)
and ``_StreamingCsvRowWriter`` (header + streamed rows) in isolation,
ahead of wiring them into ``write_csv`` / ``write_csv_split``. The
byte-level output equivalence with the pandas path is pinned separately
by ``test_writer_streaming_golden.py``.
"""

from __future__ import annotations

import errno
import io
import os
import sys
from pathlib import Path

import pytest

from aero1553.exceptions import MieClobberRefusedError, MieWriterError
from aero1553.reader import MieFileReader
from aero1553.writer import (
    CSV_HEADER,
    _AtomicCsvFile,
    _StreamingCsvRowWriter,
    is_broken_pipe,
    write_csv,
)
from tests.conftest import normal_record_rt15_sa11_us


def _leftover_temps(dest: Path) -> list[Path]:
    """Any atomic-writer temp files still sitting next to ``dest``.

    Temp names are now unique/random (``<dest>.aero1553.tmp.<pid>.<n>.<ns>``),
    so tests glob for leftovers rather than reconstructing the exact path.
    """
    return list(dest.parent.glob(f"{dest.name}.aero1553.tmp.*"))


# ── _AtomicCsvFile ─────────────────────────────────────────────────────


@pytest.mark.requirement("L2-WRT-015")
def test_atomic_commit_renames_temp_over_destination(tmp_path: Path) -> None:
    dest = tmp_path / "out.csv"
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("hello\n")
    atomic.commit()
    assert dest.read_text() == "hello\n"
    # Temp must be gone after commit.
    assert _leftover_temps(dest) == []


@pytest.mark.requirement("L2-WRT-016")
def test_atomic_close_without_commit_unlinks_temp_and_keeps_destination(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.csv"
    dest.write_text("original\n")
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("discarded\n")
    atomic.close()  # simulates a decode failure before commit
    assert _leftover_temps(dest) == [], "temp should be unlinked on uncommitted close"
    assert dest.read_text() == "original\n", "destination must be untouched"


@pytest.mark.requirement("L2-WRT-016")
def test_atomic_context_manager_cleans_up_on_exception(tmp_path: Path) -> None:
    dest = tmp_path / "out.csv"

    def _fail_mid_write() -> None:
        """The single throwing call for the `raises` block: opens the atomic
        file, writes, then fails — exercising cleanup on an exception raised
        inside the context manager (S5778 wants one call under test)."""
        with _AtomicCsvFile(dest) as atomic:
            atomic.stream.write("partial work\n")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _fail_mid_write()
    assert _leftover_temps(dest) == [], "temp leaked after exception in context manager"
    assert not dest.exists(), "destination must not be created on failure"


@pytest.mark.requirement("L2-WRT-016")
def test_atomic_commit_partial_writes_dot_partial_and_keeps_destination(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.csv"
    dest.write_text("original\n")
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("partial decode\n")
    partial_path = atomic.commit_partial()
    assert partial_path == dest.with_name("out.csv.partial")
    assert partial_path.read_text() == "partial decode\n"
    # Original destination untouched; temp gone.
    assert dest.read_text() == "original\n"
    assert _leftover_temps(dest) == []


@pytest.mark.requirement("L2-WRT-016")
def test_atomic_commit_failure_wraps_writer_error(tmp_path: Path) -> None:
    # Force the rename to fail by making the destination a directory:
    # os.replace of a file over a non-empty dir fails on POSIX and Windows.
    dest = tmp_path / "out.csv"
    dest.mkdir()
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("data\n")
    with pytest.raises(MieWriterError):
        atomic.commit()
    # Temp must be cleaned up after the failed commit.
    assert _leftover_temps(dest) == []


# ── L2-WRT-023: the no-replace commit ──────────────────────────────────
#
# These drive ``_AtomicCsvFile`` directly rather than going through
# ``write_csv``, because the whole point is the window the pre-flight cannot
# see: the destination is created AFTER the writer opened its temp, which is
# exactly where a second process lands.


@pytest.mark.requirement("L2-WRT-017", "L2-WRT-023")
def test_no_clobber_commit_refuses_a_destination_created_after_the_preflight(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.csv"
    atomic = _AtomicCsvFile(dest, no_clobber=True)
    atomic.stream.write("ours\n")
    # The other process wins the race.
    dest.write_text("theirs\n")

    with pytest.raises(MieClobberRefusedError):
        atomic.commit()
    assert dest.read_text() == "theirs\n", "a refused commit must not touch the file"
    assert _leftover_temps(dest) == [], "a refused commit must leave no temp behind"


@pytest.mark.requirement("L2-WRT-016", "L2-WRT-023", "L3-WRT-005")
def test_no_clobber_commit_partial_refuses_an_existing_partial(tmp_path: Path) -> None:
    # The .partial target is never pre-flighted, so before L2-WRT-023 it was
    # overwritten unconditionally even under --no-clobber.
    dest = tmp_path / "out.csv"
    partial = dest.with_name("out.csv.partial")
    partial.write_text("earlier forensics\n")

    atomic = _AtomicCsvFile(dest, no_clobber=True)
    atomic.stream.write("ours\n")
    with pytest.raises(MieClobberRefusedError):
        atomic.commit_partial()
    assert partial.read_text() == "earlier forensics\n"
    assert _leftover_temps(dest) == []


@pytest.mark.requirement("L2-WRT-023")
def test_no_clobber_commit_writes_normally_when_the_destination_is_free(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.csv"
    atomic = _AtomicCsvFile(dest, no_clobber=True)
    atomic.stream.write("rows\n")
    atomic.commit()
    assert dest.read_text() == "rows\n"
    # The link mechanism has to unlink the temp explicitly -- it published a
    # second name for the same bytes rather than moving them.
    assert _leftover_temps(dest) == []


@pytest.mark.requirement("L2-WRT-017")
def test_default_commit_still_replaces_an_existing_destination(tmp_path: Path) -> None:
    dest = tmp_path / "out.csv"
    dest.write_text("stale\n")
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("fresh\n")
    atomic.commit()
    assert dest.read_text() == "fresh\n"


@pytest.mark.requirement("L2-WRT-016")
def test_default_commit_partial_still_replaces_an_existing_partial(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.csv"
    partial = dest.with_name("out.csv.partial")
    partial.write_text("stale\n")
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("fresh\n")
    assert atomic.commit_partial() == partial
    assert partial.read_text() == "fresh\n"


@pytest.mark.requirement("L2-WRT-023", "L3-PY-018")
def test_no_replace_falls_back_to_a_reservation_when_hard_links_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback arm, forced.

    On this machine ``os.link`` works, so the reservation path would otherwise
    never run -- and a fallback nothing exercises is a fallback nobody knows is
    broken. ``EPERM`` stands in for the real reasons a link fails on a
    filesystem that supports none: FAT/exFAT, and some network mounts.
    """

    def _no_links(_src: object, _dst: object) -> None:
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "link", _no_links)

    free = tmp_path / "free.csv"
    atomic = _AtomicCsvFile(free, no_clobber=True)
    atomic.stream.write("rows\n")
    atomic.commit()
    assert free.read_text() == "rows\n"
    assert _leftover_temps(free) == []

    # ...and the fallback must refuse a taken name just as the link does, or
    # --no-clobber would be silently disabled on exactly those filesystems.
    taken = _AtomicCsvFile(free, no_clobber=True)
    taken.stream.write("other\n")
    with pytest.raises(MieClobberRefusedError):
        taken.commit()
    assert free.read_text() == "rows\n", "no zero-byte reservation left in its place"


# ── L2-WRT-024: the final flush is part of the commit ──────────────────


class _FlushFailsStream(io.StringIO):
    """A stream that writes happily and fails on flush.

    This is the disk-full shape, not an invented one: rows land in a buffer and
    return success, and the failure appears only when that buffer is pushed to
    the filesystem -- which is what the commit's final flush does.
    """

    def flush(self) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.requirement("L2-WRT-018", "L2-WRT-024", "L3-PY-019")
def test_commit_classifies_a_final_flush_failure_as_a_writer_error(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.csv"
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("rows\n")
    # Swap in the failing stream AFTER construction: the temp file is real, so
    # the cleanup assertion below is testing the real unlink.
    atomic._stream = _FlushFailsStream()

    with pytest.raises(MieWriterError):
        atomic.commit()
    assert not dest.exists(), "a failed commit must not create the destination"
    assert _leftover_temps(dest) == [], "the temp must be unlinked on a flush failure"


@pytest.mark.requirement("L2-WRT-024", "L3-PY-019")
def test_commit_partial_classifies_a_final_flush_failure_too(tmp_path: Path) -> None:
    dest = tmp_path / "out.csv"
    partial = dest.with_name("out.csv.partial")
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("rows\n")
    atomic._stream = _FlushFailsStream()

    with pytest.raises(MieWriterError):
        atomic.commit_partial()
    assert not partial.exists()
    assert _leftover_temps(dest) == []


@pytest.mark.requirement("L2-WRT-024", "L3-PY-019")
def test_close_still_unlinks_the_temp_when_the_stream_cannot_be_closed(
    tmp_path: Path,
) -> None:
    """Cleanup must not be defeated by the same failure it is cleaning up after.

    A stream holding unflushable data raises again from ``close()``. Letting
    that through skips the unlink -- leaking the temp onto the disk that just
    filled up -- and replaces whatever error actually caused the failure.
    """

    class _CloseFails(io.StringIO):
        def close(self) -> None:
            raise OSError(errno.EIO, "input/output error")

    dest = tmp_path / "out.csv"
    atomic = _AtomicCsvFile(dest)
    atomic.stream.write("rows\n")
    atomic._stream = _CloseFails()

    atomic.close()
    assert _leftover_temps(dest) == [], "close() must unlink the temp regardless"


@pytest.mark.requirement("L2-WRT-015")
def test_two_writers_same_destination_use_distinct_temps(tmp_path: Path) -> None:
    # Same-process concurrent writers to one destination must not share a temp
    # name — otherwise they would clobber each other before either renames into
    # place. The unique name + exclusive create (mode "x") prevents it.
    dest = tmp_path / "out.csv"
    first = _AtomicCsvFile(dest)
    second = _AtomicCsvFile(dest)
    assert first._temp != second._temp
    assert first._temp.exists()
    assert second._temp.exists()
    first.close()
    second.close()
    assert _leftover_temps(dest) == []


@pytest.mark.requirement("L2-WRT-015")
def test_atomic_writer_wraps_create_error(tmp_path: Path) -> None:
    # A create failure that is not a name clash (here: a missing parent
    # directory) is wrapped as MieWriterError rather than leaked.
    dest = tmp_path / "no-such-dir" / "out.csv"
    with pytest.raises(MieWriterError):
        _AtomicCsvFile(dest)


@pytest.mark.requirement("L2-WRT-015")
def test_atomic_writer_fails_after_persistent_clash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If every candidate temp name already exists, the writer exhausts its
    # retries and raises rather than looping forever or overwriting a file.
    from aero1553 import writer as writer_mod

    clash = tmp_path / "always-there.tmp"
    clash.write_text("x")  # exists → open(mode="x") always raises FileExistsError
    monkeypatch.setattr(writer_mod, "_unique_temp_path", lambda _fp: clash)
    with pytest.raises(MieWriterError):
        _AtomicCsvFile(tmp_path / "out.csv")


# ── _StreamingCsvRowWriter ─────────────────────────────────────────────


@pytest.mark.requirement("L2-WRT-001")
def test_streaming_writer_emits_header_on_construction() -> None:
    buf = io.StringIO()
    writer = _StreamingCsvRowWriter(buf, "memory")
    assert writer.rows_written == 0
    header_line = buf.getvalue().rstrip("\n")
    assert header_line.split(",") == CSV_HEADER


@pytest.mark.requirement("L2-WRT-001")
@pytest.mark.requirement("L3-PY-004")
def test_streaming_writer_streams_rows_incrementally(tmp_path: Path) -> None:
    """Rows must reach the stream as they are written, not buffered."""
    data = normal_record_rt15_sa11_us(100) + normal_record_rt15_sa11_us(16100)
    mie = tmp_path / "two.mie"
    mie.write_bytes(data)

    buf = io.StringIO()
    writer = _StreamingCsvRowWriter(buf, "memory")
    messages = list(MieFileReader(mie))

    writer.write_message(messages[0])
    after_one = buf.getvalue()
    assert writer.rows_written == 1
    # The first data row is already present in the stream before the
    # second is written — i.e. output is streamed, not materialized.
    assert after_one.count("\n") == 2  # header + 1 row

    writer.write_message(messages[1])
    assert writer.rows_written == 2
    assert buf.getvalue().count("\n") == 3  # header + 2 rows
    # LF-only, no CR.
    assert "\r" not in buf.getvalue()


class _PipeBreaker(io.StringIO):
    """A text stream that raises BrokenPipeError after ``break_after``
    successful writes — simulates a downstream consumer closing the pipe
    mid-stream (e.g. ``aero1553 decode ... | head``)."""

    def __init__(self, break_after: int) -> None:
        super().__init__()
        self._writes = 0
        self._break_after = break_after

    def write(self, s: str) -> int:
        self._writes += 1
        if self._writes > self._break_after:
            raise BrokenPipeError("consumer closed")
        return super().write(s)


@pytest.mark.requirement("L2-WRT-018")
def test_write_csv_to_stream_swallows_broken_pipe(tmp_path: Path) -> None:
    """A broken pipe mid-stream is treated as a clean success (exit 0),
    not propagated."""
    data = normal_record_rt15_sa11_us(100) + normal_record_rt15_sa11_us(16100)
    mie = tmp_path / "two.mie"
    mie.write_bytes(data)

    # Allow the header write through, break on the first data row.
    breaker = _PipeBreaker(break_after=1)
    outcome = write_csv(MieFileReader(mie), output=breaker)

    # No exception escaped; the run reports success.
    assert outcome.partial is None


# ── broken-pipe classification (L2-WRT-018) ────────────────────────────


@pytest.mark.requirement("L2-WRT-018")
def test_is_broken_pipe_accepts_broken_pipe_error() -> None:
    assert is_broken_pipe(BrokenPipeError(errno.EPIPE, "Broken pipe"))


@pytest.mark.requirement("L2-WRT-018")
@pytest.mark.parametrize("code", [errno.EINVAL, errno.EPIPE])
def test_is_broken_pipe_accepts_windows_oserror(code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows surfaces a closed pipe as a bare ``OSError``, not ``BrokenPipeError``.

    Writing to a pipe whose read end has closed comes out of CPython's text
    layer as ``OSError(EINVAL)`` there, so a plain ``except BrokenPipeError``
    never fires — which is why ``decode … | head`` and ``dump … | head`` exited
    1 with a traceback on Windows while the Rust CLI exited 0.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    assert is_broken_pipe(OSError(code, "Invalid argument"))


@pytest.mark.requirement("L2-WRT-018")
def test_is_broken_pipe_rejects_real_write_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine write failure must stay a failure on both platforms."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert not is_broken_pipe(OSError(errno.ENOSPC, "No space left on device"))
    assert not is_broken_pipe(ValueError("not an OSError"))


@pytest.mark.requirement("L2-WRT-018")
def test_is_broken_pipe_does_not_widen_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EINVAL`` is only pipe-closed on Windows.

    On POSIX an ``EINVAL`` write error is a real failure, so widening the match
    there would silently convert write failures into clean exits.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    assert not is_broken_pipe(OSError(errno.EINVAL, "Invalid argument"))


class _WindowsPipeBreaker(io.StringIO):
    """Like :class:`_PipeBreaker`, but raising the *Windows* form of the
    pipe-closed condition (a bare ``OSError``) rather than ``BrokenPipeError``."""

    def __init__(self, break_after: int) -> None:
        super().__init__()
        self._writes = 0
        self._break_after = break_after

    def write(self, s: str) -> int:
        self._writes += 1
        if self._writes > self._break_after:
            raise OSError(errno.EINVAL, "Invalid argument")
        return super().write(s)


@pytest.mark.requirement("L2-WRT-018")
def test_write_csv_to_stream_swallows_windows_broken_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows pipe-closed form is a clean success too, not a writer error."""
    monkeypatch.setattr(sys, "platform", "win32")
    data = normal_record_rt15_sa11_us(100) + normal_record_rt15_sa11_us(16100)
    mie = tmp_path / "two.mie"
    mie.write_bytes(data)

    outcome = write_csv(MieFileReader(mie), output=_WindowsPipeBreaker(break_after=1))

    assert outcome.partial is None


@pytest.mark.requirement("L2-WRT-018")
def test_write_csv_to_stream_still_raises_on_real_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-pipe ``OSError`` must still surface as a writer error (exit 1)."""
    monkeypatch.setattr(sys, "platform", "win32")

    class _DiskFull(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    data = normal_record_rt15_sa11_us(100)
    mie = tmp_path / "one.mie"
    mie.write_bytes(data)

    reader = MieFileReader(mie)
    sink = _DiskFull()
    with pytest.raises(MieWriterError):
        write_csv(reader, output=sink)
