"""Tests for aero1553.sync module.

Tests cover header detection, record validation, sync loss recovery,
and interaction with the reader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aero1553.models import TimestampFormat
from aero1553.sync import (
    ValidationFailure,
    find_first_record,
    recover_sync,
    validate_record,
    validate_record_detailed,
)


class TestValidateRecord:
    """Tests for validate_record."""

    @pytest.mark.requirement("L2-SYN-005")
    def test_valid_irig_record(self, single_receive_record: bytes) -> None:
        """Known-good IRIG receive record should validate."""
        # Append a second record for look-ahead
        data = single_receive_record * 2
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is True

    @pytest.mark.requirement("L2-SYN-001")
    def test_invalid_type_at_offset(self) -> None:
        """Random data should not validate."""
        data = b"\xff\xff" * 40
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is False

    @pytest.mark.requirement("L2-SYN-003")
    def test_too_short(self) -> None:
        """Data shorter than Type Word should not validate."""
        assert validate_record(b"\x02", 0, 1, TimestampFormat.IRIG) is False

    @pytest.mark.requirement("L2-SYN-002")
    def test_zero_word_count(self) -> None:
        """Type Word with zero word count should fail."""
        data = b"\x02\x00" + b"\x00" * 20
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is False

    # ── IRIG range validation (L2-SYN-004, L2-SYN-019) ──────────────

    @staticmethod
    def _irig_record(upper: int, middle: int, lower: int) -> bytes:
        """Build two minimal IRIG records back-to-back (so look-ahead
        succeeds) with the given timestamp word values. wc=5, type=0x02,
        Cmd raw 0x283E. Two records of 10 bytes each = 20 bytes."""
        type_raw = 0x0502  # type=0x02, bus A, wc=5, error=0
        cmd_raw = 0x283E  # rt=5, dir=Recv, sa=1, dwc=30
        rec = (
            type_raw.to_bytes(2, "little")
            + upper.to_bytes(2, "little")
            + middle.to_bytes(2, "little")
            + lower.to_bytes(2, "little")
            + cmd_raw.to_bytes(2, "little")
        )
        return rec * 2

    @staticmethod
    def _irig_upper(freerun: bool, day: int, hour: int) -> int:
        return ((1 if freerun else 0) << 15) | ((day & 0x1FF) << 5) | (hour & 0x1F)

    @staticmethod
    def _irig_middle(minute: int, second: int, us_hi4: int) -> int:
        return ((minute & 0x3F) << 10) | ((second & 0x3F) << 4) | (us_hi4 & 0xF)

    @pytest.mark.requirement("L2-SYN-004")
    def test_irig_accepts_valid_ranges(self) -> None:
        data = self._irig_record(
            self._irig_upper(False, 192, 15),
            self._irig_middle(54, 50, 6),
            0xF621,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is True

    @pytest.mark.requirement("L2-SYN-004")
    def test_irig_rejects_day_zero(self) -> None:
        data = self._irig_record(
            self._irig_upper(False, 0, 15),
            self._irig_middle(54, 50, 0),
            0,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is False

    @pytest.mark.requirement("L2-SYN-004")
    def test_irig_rejects_day_above_366(self) -> None:
        data = self._irig_record(
            self._irig_upper(False, 367, 15),
            self._irig_middle(54, 50, 0),
            0,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is False

    @pytest.mark.requirement("L2-SYN-019")
    def test_irig_accepts_day_zero_when_freerun(self) -> None:
        """L2-SYN-019: freerun bypasses the day-of-year range check."""
        data = self._irig_record(
            self._irig_upper(True, 0, 15),
            self._irig_middle(54, 50, 0),
            0,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is True

    @pytest.mark.requirement("L2-SYN-004")
    def test_irig_rejects_microsecond_at_one_million(self) -> None:
        # 1_000_000 = (0xF << 16) | 0x4240
        data = self._irig_record(
            self._irig_upper(False, 192, 15),
            self._irig_middle(54, 50, 0xF),
            0x4240,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is False

    @pytest.mark.requirement("L2-SYN-004")
    def test_irig_accepts_microsecond_at_max_valid(self) -> None:
        # 999_999 = (0xF << 16) | 0x423F
        data = self._irig_record(
            self._irig_upper(False, 192, 15),
            self._irig_middle(54, 50, 0xF),
            0x423F,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is True

    @pytest.mark.requirement("L2-SYN-019")
    def test_irig_rejects_microsecond_even_when_freerun(self) -> None:
        """L2-SYN-019 relaxes only the DAY check; microseconds still
        enforced."""
        data = self._irig_record(
            self._irig_upper(True, 0, 15),
            self._irig_middle(54, 50, 0xF),
            0x4240,
        )
        assert validate_record(data, 0, len(data), TimestampFormat.IRIG) is False

    @pytest.mark.requirement("L2-SYN-014")
    def test_boolean_and_detailed_validation_never_disagree(self) -> None:
        """L2-SYN-014: the two validators are one rule set, so they can
        never disagree about validity.

        True today because ``validate_record`` delegates to
        ``validate_record_detailed(...) is None`` — but delegation is an
        implementation choice a later refactor could quietly undo, and until
        v2.12.0 the requirement was reported as met with no artifact behind
        it. This pins the property across a valid record and every distinct
        rejection reason. Mirrors the Rust
        ``boolean_and_detailed_validation_never_disagree``.
        """
        valid = self._irig_record(
            self._irig_upper(False, 192, 15),
            self._irig_middle(54, 50, 0),
            0,
        )
        first = valid[:10]
        corpus = [
            valid,
            b"",
            b"\x02",
            b"\x03\x05" + bytes(8),
            b"\x02\x02" + bytes(8),
            b"\x02\x24" + bytes(8),
            self._irig_record(
                self._irig_upper(False, 192, 24),
                self._irig_middle(54, 50, 0),
                0,
            ),
            self._irig_record(
                self._irig_upper(False, 192, 15),
                self._irig_middle(60, 50, 0),
                0,
            ),
            first + b"\x03\x05",
            first + b"\x02\x02",
        ]
        for index, data in enumerate(corpus):
            for lookahead in (1, 2, 4):
                boolean = validate_record(data, 0, len(data), TimestampFormat.IRIG, lookahead)
                detailed = validate_record_detailed(
                    data, 0, len(data), TimestampFormat.IRIG, lookahead
                )
                assert boolean is (detailed is None), (
                    f"case {index} (lookahead {lookahead}): "
                    f"boolean said {boolean}, detailed said {detailed}"
                )

    @pytest.mark.requirement("L2-SYN-004")
    def test_detailed_validation_reports_each_failure_reason(self) -> None:
        valid = self._irig_record(
            self._irig_upper(False, 192, 15),
            self._irig_middle(54, 50, 0),
            0,
        )
        first = valid[:10]
        cases = [
            (b"\x02", ValidationFailure.TYPE_WORD_UNREADABLE),
            (b"\x03\x05" + bytes(8), ValidationFailure.UNKNOWN_MESSAGE_TYPE),
            (b"\x02\x02" + bytes(8), ValidationFailure.INVALID_WORD_COUNT),
            (b"\x02\x24" + bytes(8), ValidationFailure.RECORD_TRUNCATED),
            (
                self._irig_record(
                    self._irig_upper(False, 192, 24),
                    self._irig_middle(54, 50, 0),
                    0,
                ),
                ValidationFailure.IRIG_HOUR_OUT_OF_RANGE,
            ),
            (
                self._irig_record(
                    self._irig_upper(False, 192, 15),
                    self._irig_middle(60, 50, 0),
                    0,
                ),
                ValidationFailure.IRIG_MINUTE_OUT_OF_RANGE,
            ),
            (
                self._irig_record(
                    self._irig_upper(False, 192, 15),
                    self._irig_middle(54, 60, 0),
                    0,
                ),
                ValidationFailure.IRIG_SECOND_OUT_OF_RANGE,
            ),
            (
                self._irig_record(
                    self._irig_upper(False, 192, 15),
                    self._irig_middle(54, 50, 0xF),
                    0x4240,
                ),
                ValidationFailure.IRIG_MICROSECOND_OUT_OF_RANGE,
            ),
            (
                self._irig_record(
                    self._irig_upper(False, 0, 15),
                    self._irig_middle(54, 50, 0),
                    0,
                ),
                ValidationFailure.IRIG_DAY_OUT_OF_RANGE,
            ),
            (
                first + b"\x03\x05",
                ValidationFailure.LOOKAHEAD_UNKNOWN_MESSAGE_TYPE,
            ),
            (
                first + b"\x02\x02",
                ValidationFailure.LOOKAHEAD_INVALID_WORD_COUNT,
            ),
        ]
        for data, expected in cases:
            assert validate_record_detailed(data, 0, len(data), TimestampFormat.IRIG) == expected


class TestFindFirstRecord:
    """Tests for find_first_record (header detection)."""

    @pytest.mark.requirement("L2-SYN-006")
    def test_no_header(self, single_receive_record: bytes) -> None:
        """File starting directly with records should find offset 0."""
        data = single_receive_record * 2
        offset = find_first_record(data, len(data), TimestampFormat.IRIG)
        assert offset == 0

    @pytest.mark.requirement("L2-SYN-006")
    def test_with_header(self, single_receive_record: bytes) -> None:
        """File with a header before records should skip the header."""
        header = b"\x00" * 20  # 20 bytes of padding
        data = header + single_receive_record * 2
        offset = find_first_record(data, len(data), TimestampFormat.IRIG)
        assert offset == 20

    @pytest.mark.requirement("L2-SYN-008")
    def test_all_garbage(self) -> None:
        """File with no valid records should return None."""
        data = b"\xff\xff" * 100
        offset = find_first_record(data, len(data), TimestampFormat.IRIG)
        assert offset is None

    @pytest.mark.requirement("L2-SYN-006")
    def test_reader_skips_header(self, tmp_path: Path, single_receive_record: bytes) -> None:
        """MieFileReader should skip headers and decode records after."""
        from aero1553.reader import MieFileReader

        header = b"\x00" * 20
        fpath = tmp_path / "headed.mie"
        fpath.write_bytes(header + single_receive_record * 2)
        messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 2

    @pytest.mark.requirement("L2-SYN-006")
    def test_reader_real_header(self, tmp_path: Path, multi_record_data: bytes) -> None:
        """Simulate a file header (non-record data before first record)."""
        from aero1553.reader import MieFileReader

        # Build a header using 0xFF bytes (type 0x7F = invalid)
        # so find_first_record will skip past it
        header = b"\xff\x00" * 36  # 72 bytes, type 0x7F each word
        fpath = tmp_path / "header_sim.mie"
        fpath.write_bytes(header + multi_record_data)
        messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 3


class TestRecoverSync:
    """Tests for recover_sync."""

    @pytest.mark.requirement("L2-SYN-009")
    def test_recovery_after_garbage(self, single_receive_record: bytes) -> None:
        """Should find a valid record after a gap of garbage."""
        garbage = b"\xff\xff" * 10  # 20 bytes of garbage
        data = garbage + single_receive_record * 2
        recovered = recover_sync(data, 0, len(data), TimestampFormat.IRIG)
        assert recovered == 20

    @pytest.mark.requirement("L2-SYN-011")
    def test_no_recovery_possible(self) -> None:
        """Should return None when no valid record exists."""
        data = b"\xff\xff" * 100
        recovered = recover_sync(data, 0, len(data), TimestampFormat.IRIG)
        assert recovered is None

    @pytest.mark.requirement("L2-SYN-015")
    @pytest.mark.requirement("L1-EXIT-003")
    def test_reader_recovers_from_corruption(
        self, tmp_path: Path, single_receive_record: bytes
    ) -> None:
        """Reader should skip corruption and continue decoding."""
        from aero1553.reader import MieFileReader

        # 2 good records, corruption, 2 more good records
        good = single_receive_record * 2
        corruption = b"\xff\xff" * 10
        data = good + corruption + single_receive_record * 2
        fpath = tmp_path / "corrupt.mie"
        fpath.write_bytes(data)
        messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        # All four genuine records survive. Through v2.11.1 this was 3: the
        # second pre-corruption record was discarded because its *successor*
        # boundary was corrupt, even though the record itself was complete and
        # in-bounds. Continuous validation no longer looks ahead (L2-SYN-005),
        # so a well-formed record is never lost to its neighbour's damage; the
        # corruption is detected when the loop reaches it, and recovery
        # proceeds from there. Mirrors `reader_recovers_from_corruption` in
        # `rust/src/reader.rs`.
        assert len(messages) == 4

    @pytest.mark.requirement("L1-SYN-002")
    def test_recovery_scan_forward_only_and_bounded(
        self, tmp_path: Path, single_receive_record: bytes
    ) -> None:
        """L1-SYN-002: recovery scanning is forward-only and bounded — the
        cumulative scan never re-traverses already-scanned bytes. Exercise
        repeated recoveries (RR blocks separated by short recoverable
        garbage) and assert the decoded offsets advance strictly forward,
        stay within the file, and the recovery count is bounded (one per
        corruption region). Mirrors the Rust
        recovery_scan_is_forward_only_and_bounded integration test.
        """
        from aero1553.reader import MieFileReader

        block = single_receive_record * 2  # two records pass look-ahead
        garbage = b"\xff" * 16
        data = block + garbage + block + garbage + block
        fpath = tmp_path / "multi_recover.mie"
        fpath.write_bytes(data)

        reader = MieFileReader(fpath, input_time_format=TimestampFormat.IRIG)
        messages = list(reader)

        assert len(messages) >= 2, "recovery should reach later blocks"
        # Forward-only: offsets strictly increase and stay within the file —
        # the reader never rewinds into already-scanned bytes.
        offsets = [m.file_offset for m in messages]
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == len(offsets)
        assert offsets[-1] < len(data)
        # Bounded: one recovery per corruption region (two regions here).
        assert 1 <= reader.sync_losses <= 2

    @pytest.mark.requirement("L2-SYN-016")
    def test_reader_strict_raises_on_corruption(
        self, tmp_path: Path, single_receive_record: bytes
    ) -> None:
        """Strict mode should raise on sync loss."""
        from aero1553.exceptions import MieUnknownTypeWordError
        from aero1553.reader import MieFileReader

        good = single_receive_record * 2
        corruption = b"\x03\x00" * 5  # invalid type 0x03
        data = good + corruption
        fpath = tmp_path / "strict_corrupt.mie"
        fpath.write_bytes(data)
        # The reason names the offending position itself. Before v2.12.0 this
        # was "look-ahead message type is unknown", raised against the last good
        # record; that also discarded it. Now the corruption is reported where
        # it is, and the good records are kept.
        with pytest.raises(MieUnknownTypeWordError, match="Unknown message type"):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))

    @pytest.mark.requirement("L2-SYN-013")
    def test_debug_validation_context_is_bounded(
        self,
        tmp_path: Path,
        single_receive_record: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """DEBUG diagnostics include one bounded context line."""
        import logging

        from aero1553.exceptions import MieUnknownTypeWordError
        from aero1553.reader import MieFileReader

        fpath = tmp_path / "strict_corrupt.mie"
        fpath.write_bytes(single_receive_record * 2 + b"\x03\x00" * 5)
        with (
            caplog.at_level(logging.DEBUG, logger="aero1553.reader"),
            pytest.raises(MieUnknownTypeWordError),
        ):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))

        context = [
            record.getMessage()
            for record in caplog.records
            if "validation context" in record.getMessage()
        ]
        assert len(context) == 1
        assert "max 32" in context[0]

    @pytest.mark.requirement("L2-SYN-004")
    @pytest.mark.requirement("L2-SYN-016")
    def test_strict_irig_failure_names_precise_validation_reason(
        self,
        tmp_path: Path,
        single_receive_record: bytes,
    ) -> None:
        from aero1553.exceptions import MiePayloadError
        from aero1553.reader import MieFileReader

        invalid_day = bytearray(single_receive_record)
        invalid_day[2:4] = (0x000F).to_bytes(2, "little")
        fpath = tmp_path / "bad_irig.mie"
        fpath.write_bytes(single_receive_record + invalid_day)

        with pytest.raises(MiePayloadError, match="IRIG day-of-year is out of range"):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))


class TestSyncBoundsAndLogging:
    """L2-SYN-007, L2-SYN-012, L2-SYN-013: bounded scans and diagnostic logging."""

    @pytest.mark.requirement("L2-SYN-007")
    def test_find_first_record_capped_at_max_scan(self) -> None:
        """L2-SYN-007: header detection SHALL cap its scan at 64 KB. A valid
        record placed past that cap SHALL NOT be found."""
        from aero1553.sync import MAX_SCAN_BYTES, find_first_record
        from tests.conftest import RECORD_RT15_SA11_RCV

        garbage = b"\xff" * (MAX_SCAN_BYTES + 1024)
        # Two valid records past the cap so look-ahead would succeed if
        # the scanner reached them — but it shouldn't.
        data = garbage + RECORD_RT15_SA11_RCV * 2
        offset = find_first_record(data, len(data), TimestampFormat.IRIG)
        assert offset is None

    @pytest.mark.requirement("L2-SYN-012")
    def test_header_detection_logs_size_at_info(
        self,
        tmp_path: Path,
        single_receive_record: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """L2-SYN-012: header detection SHALL log detected header size at INFO.

        Asserted at the *reader*, not at ``find_first_record``: the sync helpers
        are pure (no logging) so both implementations narrate this from the
        reader, exactly as ``rust/src/reader.rs`` does.
        """
        import logging

        from aero1553.reader import MieFileReader

        header = b"\x00" * 24
        fpath = tmp_path / "headered.mie"
        fpath.write_bytes(header + single_receive_record * 2)
        with caplog.at_level(logging.INFO, logger="aero1553"):
            messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 2
        info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("header" in m.lower() for m in info_msgs), (
            f"expected INFO log naming the header; got {info_msgs}"
        )
        # The header size (24 bytes) should appear in the message.
        assert any("24" in m for m in info_msgs), (
            f"expected header-size byte count in INFO log; got {info_msgs}"
        )

    @pytest.mark.requirement("L2-SYN-012")
    def test_sync_helpers_emit_no_log_output(
        self,
        single_receive_record: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The sync helpers are pure — they never log (matches ``sync.rs``).

        Pinning this stops log statements from creeping back into the validation
        helpers, where they narrate an outcome without the caller's context.
        """
        import logging

        from aero1553.sync import find_first_record, recover_sync

        data = b"\x00" * 24 + single_receive_record * 2
        with caplog.at_level(logging.DEBUG, logger="aero1553"):
            assert find_first_record(data, len(data), TimestampFormat.IRIG) == 24
            # A scan that finds nothing must be just as quiet.
            assert find_first_record(b"\xff" * 64, 64, TimestampFormat.IRIG) is None
            recover_sync(data, 0, len(data), TimestampFormat.IRIG)
        sync_records = [r for r in caplog.records if r.name.startswith("aero1553.sync")]
        assert sync_records == [], (
            f"sync helpers must not log; got {[r.getMessage() for r in sync_records]}"
        )

    @pytest.mark.requirement("L2-RDR-021")
    def test_empty_recording_does_not_warn_about_missing_records(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A valid but empty recording must not be reported as having no records.

        ``find_first_record`` legitimately returns ``None`` here — the stream is
        just the ``0x0000`` terminator — and it used to log "No valid record
        found in first N bytes of file" on its way out. That warning flatly
        contradicted the reader's own "empty capture" message logged immediately
        after, and would make an operator grepping their logs flag a healthy
        recording. Rust never emitted it.
        """
        import logging

        from aero1553.reader import MieFileReader

        fpath = tmp_path / "empty_recording.mie"
        fpath.write_bytes(b"\x00\x00")
        with caplog.at_level(logging.DEBUG, logger="aero1553"):
            assert list(MieFileReader(fpath)) == []
        messages = [r.getMessage().lower() for r in caplog.records]
        assert not any("no valid record" in m for m in messages), (
            f"an empty recording must not be reported as having no valid records; got {messages}"
        )
        # The correct message is still emitted.
        assert any("empty capture" in m for m in messages), (
            f"expected the empty-capture WARN; got {messages}"
        )

    @pytest.mark.requirement("L2-SYN-018")
    def test_homogeneous_payload_input_rejected(self, tmp_path: Path) -> None:
        """L2-SYN-018: a file of pure 0x20-fill parses as a SPURIOUS_DATA
        Type Word (msg_type=0x20, wc=32) and passes basic validation,
        but every "record" is byte-identical to its successor. The reader
        SHALL reject such pathological inputs with
        MieHomogeneousPayloadError rather than emit a torrent of
        synthetic SPURIOUS_DATA frames.
        """
        from aero1553.exceptions import MieHomogeneousPayloadError
        from aero1553.reader import MieFileReader

        # 0x20-fill, 1 KB — enough for 4 candidate records of 64 bytes each.
        fpath = tmp_path / "all_spaces.mie"
        fpath.write_bytes(b"\x20" * 1024)
        with pytest.raises(MieHomogeneousPayloadError):
            list(MieFileReader(fpath))

    @pytest.mark.requirement("L2-SYN-018")
    def test_non_homogeneous_valid_records_accepted(
        self, tmp_path: Path, single_receive_record: bytes
    ) -> None:
        """L2-SYN-018: the defense SHALL NOT false-positive on legitimate
        recordings whose payload bytes vary between records — i.e. real
        MIE files don't trip the homogeneous-payload guard."""
        from aero1553.reader import MieFileReader

        # Two copies of RECORD_RT15_SA11_RCV (the canonical valid record).
        # Only 2 records is below the N=4 sample, so the check is
        # technically inapplicable. To exercise the negative case with
        # N=4 we need 4 distinct candidate-sized chunks. Easiest: pad to
        # 4 records by appending three more copies — payloads are
        # identical except timestamps would naturally vary; here they
        # don't (same fixture), so we expect the defense to fire.
        # Instead, use a tmp_mie_file-style multi-record stream where
        # records have different lengths (so candidate-sized chunks at
        # the same offsets are NOT identical to record 1).
        from tests.conftest import (
            RECORD_RT15_SA11_RCV,
            RECORD_RT15_SA22_RCV,
            RECORD_RT15_SA22_XMT,
        )

        # Pack four real records with varying types and lengths. The
        # candidate's record_bytes (72) doesn't divide evenly into the
        # combined stream, so chunks at offset 72, 144, 216 differ in
        # both Type Word and CMD/payload from the first record.
        fpath = tmp_path / "varied.mie"
        fpath.write_bytes(
            RECORD_RT15_SA11_RCV
            + RECORD_RT15_SA22_RCV
            + RECORD_RT15_SA22_XMT
            + RECORD_RT15_SA11_RCV
        )
        # Should decode normally — homogeneity defense must not fire.
        messages = list(MieFileReader(fpath))
        assert len(messages) == 4

    @pytest.mark.requirement("L2-SYN-013")
    def test_sync_loss_warns_and_recovery_logs_info(
        self,
        tmp_path: Path,
        single_receive_record: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """L2-SYN-013: sync recovery SHALL log sync loss at WARNING and
        successful recovery at INFO."""
        import logging

        from aero1553.reader import MieFileReader

        good = single_receive_record * 2
        corruption = b"\xff\xff" * 10
        data = good + corruption + single_receive_record * 2
        fpath = tmp_path / "sync_logging.mie"
        fpath.write_bytes(data)
        with caplog.at_level(logging.INFO, logger="aero1553.reader"):
            messages = list(MieFileReader(fpath))
        assert len(messages) >= 1
        warn_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("sync" in m.lower() for m in warn_msgs), (
            f"expected WARN about sync loss; got warns={warn_msgs}"
        )
        assert any("recover" in m.lower() for m in info_msgs), (
            f"expected INFO about recovery; got infos={info_msgs}"
        )


class TestNRecordLookahead:
    """L2-SYN-026 N-record configurable look-ahead.

    Mirrors rust/src/sync.rs::tests::validate_lookahead_*.
    """

    @staticmethod
    def _make_valid_record_36w(count: int) -> bytes:
        """count copies of a 72-byte record (Type 0x2402, wc=36)."""
        out = bytearray()
        for _ in range(count):
            out += bytes([0x02, 0x24]) + bytes(70)
        return bytes(out)

    @pytest.mark.requirement("L2-SYN-026")
    def test_n1_skips_lookahead(self) -> None:
        from aero1553.sync import validate_record

        # Valid record + 4 bytes of plausible-looking but invalid
        # Type-Word garbage. N=1 must not peek; N=2 must reject.
        buf = self._make_valid_record_36w(1) + b"\xff\xff\x00\x00"
        assert validate_record(buf, 0, len(buf), None, lookahead_records=1)
        assert not validate_record(buf, 0, len(buf), None, lookahead_records=2)

    @pytest.mark.requirement("L2-SYN-026")
    def test_n4_catches_second_corruption(self) -> None:
        from aero1553.sync import validate_record

        # Two valid records + invalid Type Word at the third record's
        # position. N=2 (default) only checks records 1 and 2 (both
        # valid) and accepts. N=4 reaches record 3 and rejects.
        buf = self._make_valid_record_36w(2) + b"\xff\xff\x00\x00"
        assert validate_record(buf, 0, len(buf), None, lookahead_records=2)
        assert not validate_record(buf, 0, len(buf), None, lookahead_records=4)

    @pytest.mark.requirement("L2-SYN-026")
    def test_eof_terminates_gracefully(self) -> None:
        from aero1553.sync import validate_record

        # Single valid record with no follower. Any N >= 1 must accept —
        # EOF mid-walk is not a rejection.
        buf = self._make_valid_record_36w(1)
        for n in (1, 2, 4, 8, 32):
            assert validate_record(buf, 0, len(buf), None, lookahead_records=n), (
                f"N={n}: EOF must not reject when the candidate itself is valid"
            )


class TestEndOfRecordsTerminator:
    """L2-SYN-028 end-of-records terminator (null Type Word 0x0000).

    Mirrors rust/src/sync.rs::tests::{validate_lookahead_terminator_confirms_
    last_record, recover_sync_does_not_honor_terminator}.
    """

    @staticmethod
    def _record_36w() -> bytes:
        return bytes([0x02, 0x24]) + bytes(70)

    @pytest.mark.requirement("L2-SYN-028")
    def test_lookahead_terminator_confirms_last_record(self) -> None:
        from aero1553.sync import validate_record

        # A valid record whose look-ahead boundary is the 0x0000 terminator is
        # confirmed as the last record — not dropped. Without this the final
        # record of every well-formed recording would be lost.
        buf = self._record_36w() + b"\x00\x00"
        assert validate_record(buf, 0, len(buf), None, lookahead_records=2)

    @pytest.mark.requirement("L2-SYN-028")
    def test_recover_sync_does_not_honor_terminator(self) -> None:
        from aero1553.sync import recover_sync, validate_record

        # [2 bytes garbage][valid record][0x0000]. Recovery must NOT validate
        # the record off its terminator follower (a mis-aligned candidate could
        # otherwise land on a stray zero); recovery requires a real follower.
        buf = b"\xff\xff" + self._record_36w() + b"\x00\x00"
        assert recover_sync(buf, 0, len(buf), None) is None
        # Forward validation of that same record (at its true boundary) accepts
        # it — proving the trusted-boundary vs recovery split is intentional.
        assert validate_record(buf, 2, len(buf), None, lookahead_records=2)
