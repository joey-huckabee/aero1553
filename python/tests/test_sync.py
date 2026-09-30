"""Sync behaviour -- header detection, record validation, sync-loss recovery
-- as the public reader exhibits it.

The validation helpers are the Rust crate's (``rust/src/sync.rs``) and are
unit-tested there; these pin what a Python caller observes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aero1553.models import TimestampFormat


class TestFindFirstRecord:
    """Header detection."""

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
    """Sync-loss recovery."""

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
    def test_header_scan_is_capped_at_64_kib(self, tmp_path: Path) -> None:
        """L2-SYN-007: header detection SHALL cap its scan at 64 KB. Valid
        records placed past that cap are not found, so the file is refused as
        having no records; the same records inside the cap decode."""
        from aero1553.exceptions import MieNoValidRecordsError
        from aero1553.reader import MieFileReader
        from tests.conftest import RECORD_RT15_SA11_RCV

        records = RECORD_RT15_SA11_RCV * 2  # two, so the look-ahead would confirm
        past = tmp_path / "past.mie"
        past.write_bytes(b"\xff" * (65_536 + 1024) + records)
        with pytest.raises(MieNoValidRecordsError):
            list(MieFileReader(past, input_time_format=TimestampFormat.IRIG))

        inside = tmp_path / "inside.mie"
        inside.write_bytes(b"\xff" * 1024 + records)
        assert len(list(MieFileReader(inside, input_time_format=TimestampFormat.IRIG))) == 2

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
