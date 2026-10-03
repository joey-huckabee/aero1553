"""Tests for aero1553.exceptions and aero1553.logger modules."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from aero1553.exceptions import (
    Aero1553Error,
    MieFileEmptyError,
    MieFileError,
    MieFileNotFoundError,
    MieInvalidTypeWordError,
    MiePayloadError,
    MieRecordError,
    MieRecordTruncatedError,
    MieUnknownErrorCodeError,
    MieUnknownTypeWordError,
    MieWriterError,
)
from aero1553.logger import LOGGER_NAME, configure_logging


class TestExceptionHierarchy:
    """Verify the exception inheritance chain."""

    @pytest.mark.requirement("L3-PY-006")
    def test_file_not_found_is_file_error(self) -> None:
        exc = MieFileNotFoundError("/tmp/missing.mie")
        assert isinstance(exc, MieFileError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_file_empty_is_file_error(self) -> None:
        exc = MieFileEmptyError("/tmp/empty.mie")
        assert isinstance(exc, MieFileError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_invalid_type_word_is_record_error(self) -> None:
        exc = MieInvalidTypeWordError(0x100, 0x0000, 0)
        assert isinstance(exc, MieRecordError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_record_truncated_is_record_error(self) -> None:
        exc = MieRecordTruncatedError(0x200, 72, 20)
        assert isinstance(exc, MieRecordError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_payload_error_is_record_error(self) -> None:
        exc = MiePayloadError(0x300, "bad payload")
        assert isinstance(exc, MieRecordError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_unknown_type_word_is_record_error(self) -> None:
        exc = MieUnknownTypeWordError(0x400, 0x0503, 0x03)
        assert isinstance(exc, MieRecordError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_unknown_error_code_is_record_error(self) -> None:
        exc = MieUnknownErrorCodeError(0x500, 0x9999)
        assert isinstance(exc, MieRecordError)
        assert isinstance(exc, Aero1553Error)

    @pytest.mark.requirement("L3-PY-006")
    def test_writer_error_is_decoder_error(self) -> None:
        cause = OSError("disk full")
        exc = MieWriterError("output.csv", cause)
        assert isinstance(exc, Aero1553Error)
        assert exc.cause is cause

    @pytest.mark.requirement("L3-PY-006")
    def test_catch_all_with_base_class(self) -> None:
        """All custom exceptions should be catchable via Aero1553Error."""
        exceptions = [
            MieFileNotFoundError("/x"),
            MieFileEmptyError("/x"),
            MieInvalidTypeWordError(0, 0, 0),
            MieUnknownTypeWordError(0, 0x0503, 0x03),
            MieUnknownErrorCodeError(0, 0x9999),
            MieRecordTruncatedError(0, 72, 10),
            MiePayloadError(0, "test"),
            MieWriterError("out", OSError()),
        ]
        for exc in exceptions:
            with pytest.raises(Aero1553Error):
                raise exc

    @pytest.mark.requirement("L3-PY-006")
    def test_unopenable_input_raises_file_io_error(self, tmp_path: Path) -> None:
        """An input that exists but cannot be opened converts to
        :class:`MieFileIoError`, not a bare ``OSError``.

        A directory is the portable way to provoke this — `open()` on one fails
        with `PermissionError` on Windows and `IsADirectoryError` on POSIX, both
        `OSError`. Rust reports the same condition as `MieError::FileIo`; before
        v2.12.0 Python let the `OSError` escape, so a caller catching
        `Aero1553Error` (as the class docstring advertises) missed it.
        """
        from aero1553.exceptions import MieFileIoError
        from aero1553.reader import MieFileReader

        target = tmp_path / "not-a-file.mie"
        target.mkdir()

        with pytest.raises(MieFileIoError) as caught:
            MieFileReader(target)
        exc = caught.value
        assert isinstance(exc, MieFileError), "must be catchable as a file error"
        assert isinstance(exc, Aero1553Error), "must be catchable as a decoder error"
        assert isinstance(exc.source, OSError), "the originating OSError is preserved"
        assert exc.__cause__ is exc.source, "and chained, so the traceback keeps it"
        assert str(exc).startswith("I/O error on "), "message prefix mirrors Rust's FileIo"

    @pytest.mark.requirement("L3-PY-006")
    def test_unopenable_input_is_not_reported_as_empty(self, tmp_path: Path) -> None:
        """The open must be attempted *before* the size check.

        A directory stats as zero bytes on Windows, so checking size first
        reported "MIE file is empty" for something that was never readable.
        `MieFileReader::new` in Rust opens first; this pins the same order here.
        """
        from aero1553.exceptions import MieFileEmptyError, MieFileIoError
        from aero1553.reader import MieFileReader

        target = tmp_path / "stats-as-zero.mie"
        target.mkdir()

        with pytest.raises(MieFileIoError):
            MieFileReader(target)
        # Belt and braces: the emptiness class must not be what surfaces.
        try:
            MieFileReader(target)
        except MieFileEmptyError:  # pragma: no cover - fails the assert below
            pytest.fail("an unopenable path must not be reported as empty")
        except MieFileIoError:
            pass

    @pytest.mark.requirement("L3-PY-006")
    def test_every_exception_class_is_classified(self) -> None:
        """Every concrete exception must deliberately be file-class,
        record-class, or an explicitly-listed direct child of the base.

        The mechanical form of the "extend `MieFileError` or `MieRecordError` as
        appropriate" step in MAINTAINER-GUIDE.md §"Adding an error variant".
        Adding a class without deciding fails here rather than passing review.

        The record-class set is also asserted to match Rust's
        `is_record_error()` exactly — that pairing is what drifted:
        `MieUnrecoverableSyncLossError` extended `MieRecordError` here while the
        Rust predicate omitted it until v2.12.0. Mirrors
        `every_error_kind_is_deliberately_classified` in `rust/src/error.rs`.
        """
        import inspect

        from aero1553 import exceptions as exc_mod

        # Direct children of the base by design: neither a file-level nor a
        # record-level failure. Mirrors Rust's NEITHER list.
        direct = {"MieNonMonotonicInputError", "MieWriterError"}
        # Must equal Rust's is_record_error() set, name for name.
        expected_record = {
            "MieInvalidTypeWordError",
            "MieUnknownTypeWordError",
            "MieRecordTruncatedError",
            "MieFirstRecordTruncatedError",
            "MiePayloadError",
            "MieUnknownErrorCodeError",
            "MieUnrecoverableSyncLossError",
            "MieMergeInputsDroppedError",
        }

        bases = {"Aero1553Error", "MieFileError", "MieRecordError"}
        record_class: set[str] = set()
        for name, obj in inspect.getmembers(exc_mod, inspect.isclass):
            if not issubclass(obj, Aero1553Error) or name in bases:
                continue
            if issubclass(obj, MieRecordError):
                record_class.add(name)
            elif issubclass(obj, MieFileError):
                pass  # file-class
            else:
                assert name in direct, (
                    f"{name} extends Aero1553Error directly but is not a known "
                    "direct child — classify it under MieFileError or "
                    "MieRecordError, or add it to `direct` here and to Rust's "
                    "NEITHER list"
                )
        assert record_class == expected_record, (
            "the record-class set must match Rust's is_record_error() exactly; "
            "update both sides together"
        )


class TestExceptionAttributes:
    """Verify exception attributes are accessible."""

    @pytest.mark.requirement("L2-RDR-005")
    def test_file_not_found_path(self) -> None:
        exc = MieFileNotFoundError("/data/test.mie")
        assert exc.path == "/data/test.mie"
        assert "not found" in str(exc)

    @pytest.mark.requirement("L2-RDR-006")
    def test_file_empty_path(self) -> None:
        exc = MieFileEmptyError("/data/empty.mie")
        assert exc.path == "/data/empty.mie"
        assert "empty" in str(exc)

    @pytest.mark.requirement("L2-SYN-001")
    def test_invalid_type_word_fields(self) -> None:
        exc = MieInvalidTypeWordError(0x48, 0x0003, 0)
        assert exc.offset == 0x48
        assert exc.raw_type_word == 0x0003
        assert exc.word_count == 0
        assert "0x0003" in str(exc)

    @pytest.mark.requirement("L2-RDR-003")
    def test_record_truncated_fields(self) -> None:
        exc = MieRecordTruncatedError(0x100, 72, 20)
        assert exc.offset == 0x100
        assert exc.record_bytes == 72
        assert exc.available_bytes == 20
        assert "72" in str(exc)
        assert "20" in str(exc)

    @pytest.mark.requirement("L2-SYN-001")
    def test_unknown_type_word_fields(self) -> None:
        exc = MieUnknownTypeWordError(0x200, 0x0503, 0x03)
        assert exc.offset == 0x200
        assert exc.raw_type_word == 0x0503
        assert exc.message_type == 0x03
        assert "0x03" in str(exc)
        assert "Unknown" in str(exc)

    @pytest.mark.requirement("L2-ERR-004")
    def test_unknown_error_code_fields(self) -> None:
        exc = MieUnknownErrorCodeError(0x300, 0x9999)
        assert exc.offset == 0x300
        assert exc.error_code == 0x9999
        assert "0x9999" in str(exc).lower()
        assert "Unknown" in str(exc)

    @pytest.mark.requirement("L2-WRT-018")
    def test_writer_error_fields(self) -> None:
        cause = OSError("permission denied")
        exc = MieWriterError("/output/decoded.csv", cause)
        assert exc.destination == "/output/decoded.csv"
        assert exc.cause is cause
        assert "permission denied" in str(exc)


class TestConfigureLogging:
    """Tests for logger.configure_logging()."""

    @pytest.mark.requirement("L2-CLI-004")
    def test_sets_level(self) -> None:
        configure_logging("DEBUG")
        log = logging.getLogger(LOGGER_NAME)
        assert log.level == logging.DEBUG

    @pytest.mark.requirement("L2-CLI-004")
    def test_sets_info_level(self) -> None:
        configure_logging("INFO")
        log = logging.getLogger(LOGGER_NAME)
        assert log.level == logging.INFO

    @pytest.mark.requirement("L2-CLI-004")
    def test_case_insensitive(self) -> None:
        configure_logging("debug")
        log = logging.getLogger(LOGGER_NAME)
        assert log.level == logging.DEBUG

    @pytest.mark.requirement("L2-CLI-004")
    def test_invalid_level_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid log level"):
            configure_logging("BOGUS")

    @pytest.mark.requirement("L2-CLI-004")
    def test_off_level_silences_all_output(self) -> None:
        """OFF is accepted (no crash) and suppresses all output, including
        ERROR — matching the Rust logger's Level::Off. stdlib has no
        logging.OFF, so this is the regression guard for the prior
        ValueError crash on `configure_logging("OFF")`."""
        import io

        buf = io.StringIO()
        configure_logging("OFF", stream=buf)
        log = logging.getLogger(LOGGER_NAME)
        assert log.level > logging.CRITICAL  # nothing passes the filter
        log.error("this must not be emitted")
        log.critical("nor this")
        assert buf.getvalue() == ""

    @pytest.mark.requirement("L2-CLI-004")
    def test_off_level_case_insensitive(self) -> None:
        configure_logging("off")
        log = logging.getLogger(LOGGER_NAME)
        assert log.level > logging.CRITICAL

    @pytest.mark.requirement("L2-CLI-004")
    def test_no_duplicate_handlers(self) -> None:
        """Calling configure_logging twice should not add duplicate handlers."""
        configure_logging("INFO")
        configure_logging("DEBUG")
        log = logging.getLogger(LOGGER_NAME)
        assert len(log.handlers) == 1

    @pytest.mark.requirement("L2-CLI-006")
    def test_outputs_to_stderr_by_default(self) -> None:
        configure_logging("WARNING")
        log = logging.getLogger(LOGGER_NAME)
        assert len(log.handlers) == 1
        import sys

        assert log.handlers[0].stream is sys.stderr  # type: ignore[attr-defined]
