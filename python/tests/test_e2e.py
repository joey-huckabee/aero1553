"""End-to-end tests for MIE file reading and CSV output.

These tests exercise the full pipeline: binary file → MieFileReader →
MieMessage → CSV writer, and validate against known-good CSV rows
from DDC vendor output.
"""

from __future__ import annotations

import csv
import errno
import io
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from aero1553.models import Bus, Direction, MessageFormat, MieMessage, TimestampFormat
from aero1553.order import order_rows
from aero1553.reader import MieFileReader
from aero1553.writer import CSV_HEADER, write_csv
from tests.conftest import RunCli, replace
from tests.fuzz_support import FUZZ_SEED, fuzz_logging
from tests.fuzz_support import fill as fuzz_fill
from tests.fuzz_support import iterations as fuzz_iterations
from tests.fuzz_support import summary as fuzz_summary
from tests.fuzz_support import xorshift64 as fuzz_xorshift64


class TestMieFileReader:
    """Integration tests for MieFileReader."""

    @pytest.mark.requirement("L2-RDR-015")
    def test_read_three_records(self, tmp_mie_file: Path) -> None:
        """Should decode exactly 3 messages from the multi-record fixture."""
        messages = list(MieFileReader(tmp_mie_file))
        assert len(messages) == 3

    @pytest.mark.requirement("L2-RDR-007")
    def test_first_record_matches_csv(self, tmp_mie_file: Path) -> None:
        """First record should match validated CSV row exactly.

        Expected: 192:15:54:50.456225,15,11R,...,7800,797E,...,A
        """
        msg = next(iter(MieFileReader(tmp_mie_file)))
        assert msg.timestamp.format() == "192:15:54:50.456225"
        assert msg.rt == 15
        assert msg.msg_label == "11R"
        assert msg.command_word.raw == 0x797E
        assert msg.status_word == 0x7800
        assert msg.bus == Bus.A
        assert msg.data_words[0] == 0x0400
        assert msg.data_words[3] == 0x002F
        assert msg.data_words[4] == 0xCA22
        assert msg.data_words[29] == 0xC771
        assert len(msg.data_words) == 30
        assert msg.message_format == MessageFormat.RECEIVE
        assert msg.command_word_2 is None
        assert msg.status_word_2 is None
        assert msg.error_word is None
        assert msg.is_error is False
        assert msg.is_spurious is False
        assert msg.error_label == ""

    @pytest.mark.requirement("L2-RDR-007")
    def test_second_record_receive(self, tmp_mie_file: Path) -> None:
        """Second record: RT15 SA22 Receive, 11 data words."""
        msg = list(MieFileReader(tmp_mie_file))[1]
        assert msg.rt == 15
        assert msg.msg_label == "22R"
        assert msg.command_word.subaddress == 22
        assert msg.command_word.direction == Direction.RECEIVE
        assert len(msg.data_words) == 11
        assert msg.status_word == 0x7800

    @pytest.mark.requirement("L2-RDR-008")
    def test_third_record_transmit(self, tmp_mie_file: Path) -> None:
        """Third record: RT15 SA22 Transmit, 30 data words.

        For Transmit, Status Word comes before Data Words in wire order.
        """
        msg = list(MieFileReader(tmp_mie_file))[2]
        assert msg.rt == 15
        assert msg.msg_label == "22T"
        assert msg.command_word.direction == Direction.TRANSMIT
        assert len(msg.data_words) == 30
        assert msg.status_word == 0x7800
        assert msg.data_words[0] == 0x1020
        assert msg.message_format == MessageFormat.TRANSMIT

    @pytest.mark.requirement("L2-MSG-002")
    def test_bus_b_record(self, tmp_busb_file: Path) -> None:
        """Bus B file should decode bus=B correctly."""
        msg = next(iter(MieFileReader(tmp_busb_file)))
        assert msg.bus == Bus.B
        assert msg.rt == 15
        assert msg.msg_label == "10T"

    @pytest.mark.requirement("L2-RDR-010")
    def test_delta_first_occurrence_is_zero(self, tmp_mie_file: Path) -> None:
        """First occurrence of any RT/MSG should have delta=0."""
        messages = list(MieFileReader(tmp_mie_file))
        for msg in messages:
            # All three have unique RT/MSG combos, so all should be 0
            assert msg.delta == 0.0

    @pytest.mark.requirement("L2-RDR-009")
    def test_delta_same_rtmsg(self, tmp_path: Path) -> None:
        """Two identical RT/MSG records should produce correct delta."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        # Concatenate same record twice
        fpath = tmp_path / "delta.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV * 2)
        messages = list(MieFileReader(fpath))
        assert len(messages) == 2
        assert messages[0].delta == 0.0
        # Same timestamp → delta = 0.0 (not a useful real-world case
        # but validates the calculation path)
        assert messages[1].delta == 0.0

    @pytest.mark.requirement("L2-RDR-005")
    def test_file_not_found(self) -> None:
        """Should raise MieFileNotFoundError for missing files."""
        from aero1553.exceptions import MieFileNotFoundError

        with pytest.raises(MieFileNotFoundError):
            MieFileReader("/nonexistent/file.mie")

    @pytest.mark.requirement("L2-RDR-006")
    def test_empty_file(self, tmp_path: Path) -> None:
        """Should raise MieFileEmptyError for empty files."""
        from aero1553.exceptions import MieFileEmptyError

        fpath = tmp_path / "empty.mie"
        fpath.write_bytes(b"")
        with pytest.raises(MieFileEmptyError, match="empty"):
            MieFileReader(fpath)

    @pytest.mark.requirement("L2-RDR-002")
    def test_truncated_record(self, tmp_path: Path) -> None:
        """Truncated final record should be silently skipped."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "truncated.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV[:20])
        messages = list(MieFileReader(fpath))
        assert len(messages) == 1

    @pytest.mark.requirement("L2-RDR-003")
    def test_truncated_record_strict(self, tmp_path: Path) -> None:
        """Strict mode should raise MieRecordTruncatedError on truncation."""
        from aero1553.exceptions import MieRecordTruncatedError
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "truncated_strict.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV[:20])
        with pytest.raises(MieRecordTruncatedError):
            list(MieFileReader(fpath, strict=True))

    @pytest.mark.requirement("L2-SYN-016")
    def test_invalid_record_strict(self, tmp_path: Path) -> None:
        """Strict mode should raise on invalid record after good data."""
        from aero1553.exceptions import Aero1553Error
        from tests.conftest import RECORD_RT15_SA11_RCV

        bad_record = b"\x03\x00" + b"\x00" * 18  # type 0x03, wc=0
        fpath = tmp_path / "bad_record.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV * 2 + bad_record)
        with pytest.raises(Aero1553Error):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))

    @pytest.mark.requirement("L2-DEC-009")
    def test_payload_extraction_does_not_overrun_into_next_record(self, tmp_path: Path) -> None:
        """L2-DEC-009: payload extraction is bounded by the Type Word's
        declared extent and never consumes bytes from the following record.
        A Command Word declaring more data words than `word_count` can hold
        is rejected by the L2-SYN-022 capacity invariant before extraction,
        and the reader slices to the record extent — so a malformed record
        cannot overrun its successor. Mirrors the Rust
        payload_extraction_does_not_overrun_into_next_record test.
        """
        from aero1553.exceptions import MiePayloadError
        from tests.conftest import RECORD_RT15_SA11_RCV

        # R1: Type Word word_count=10 (20 bytes) but Command Word 0x797E
        # declares data_word_count=30. R2: a normal valid record after it.
        r1 = (
            b"\x02\x0a"  # Type: wc=10, type=0x02 (BC->RT), little-endian 0x0A02
            b"\x0f\x18\x26\xdb\x21\xf6"  # IRIG timestamp (3 words)
            b"\x7e\x79"  # Cmd 0x797E (RT15 R SA11 dwc=30), little-endian
            + bytes(10)  # 5 payload words -> total 10 words = 20 bytes
        )
        assert len(r1) == 20
        data = r1 + RECORD_RT15_SA11_RCV
        fpath = tmp_path / "overrun.mie"
        fpath.write_bytes(data)

        # Strict: the over-declaration is rejected, not decoded into an overrun.
        with pytest.raises(MiePayloadError):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))

        # Lenient: R1 is skipped; R2 decodes intact at its true offset,
        # proving R1 consumed nothing beyond its 20-byte declared extent.
        messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 1
        assert messages[0].file_offset == 20
        assert messages[0].command_word is not None
        assert messages[0].command_word.rt == 15

    @pytest.mark.requirement("L2-DEC-009")
    @pytest.mark.requirement("L1-ROB-001")
    @pytest.mark.requirement("L2-SYN-027")
    def test_rt_to_rt_cmd2_overclaim_does_not_overrun(self, tmp_path: Path) -> None:
        """L2-DEC-009 / L1-ROB-001 / L2-SYN-027: an RT-to-RT record whose
        *second* Command Word over-declares ``data_word_count`` must not read
        past the Type Word's declared extent — the regression behind the
        fuzz-harness ``struct.error``.

        The L2-SYN-022 capacity invariant is computed from Cmd1, but RT-to-RT
        extraction takes its data-word count from Cmd2 (the transmit command).
        Fuzzed bytes can keep Cmd1's count small (so the capacity check passes
        and the record fits the file) while Cmd2 claims 30 words — which, on
        the full mmap, would overrun into the next record / past EOF and raise
        ``struct.error``. The reader bounds payload reads to the record extent
        (L2-DEC-009), so extraction completes safely with no raw exception;
        the over-claim is then a Cmd1/Cmd2 ``data_word_count`` disagreement, so
        the post-extract L2-SYN-027 invariant rejects the record (strict
        raises, lenient skips). Mirrors the Rust
        ``rt_to_rt_cmd2_overclaim_does_not_overrun``; complements
        ``test_payload_extraction_does_not_overrun_into_next_record`` (the Cmd1
        path the capacity invariant catches pre-extract).
        """
        from aero1553.exceptions import MiePayloadError
        from tests.conftest import RECORD_RT15_SA11_RCV

        # R1: Type Word word_count=10 (20 bytes), type 0x08 (RT-to-RT). Cmd1
        # 0x7961 declares dwc=1 (small → passes the Cmd1-based capacity check);
        # Cmd2 0x797E declares dwc=30 (the over-claim). R2: a valid record.
        r1 = (
            b"\x08\x0a"  # Type: wc=10, type=0x08 (RT_TO_RT)
            b"\x0f\x18\x26\xdb\x21\xf6"  # IRIG timestamp (3 words)
            b"\x61\x79"  # Cmd1 0x7961 (RT15 R SA11 dwc=1)
            b"\x7e\x79"  # Cmd2 0x797E (RT15 R SA11 dwc=30)
            b"\x00\x00" + bytes(6)  # tx_status  # 3 padding words → total 10 words
        )
        assert len(r1) == 20
        data = r1 + RECORD_RT15_SA11_RCV
        fpath = tmp_path / "rt_to_rt_overclaim.mie"
        fpath.write_bytes(data)

        # Strict: extraction completes without a raw struct.error (bounded
        # reads), then L2-SYN-027 rejects the Cmd1/Cmd2 mismatch.
        with pytest.raises(MiePayloadError):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))

        # Lenient: R1 is skipped; R2 decodes intact at its true offset, proving
        # R1's Cmd2 over-claim consumed nothing beyond its 20-byte extent.
        messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 1
        assert messages[0].file_offset == 20
        assert messages[0].command_word is not None
        assert messages[0].command_word.rt == 15

    @pytest.mark.requirement("L2-SYN-027")
    def test_rt_to_rt_cmd_word_count_mismatch_rejected(self, tmp_path: Path) -> None:
        """L2-SYN-027: an RT-to-RT record whose Cmd1 and Cmd2 disagree on
        ``data_word_count`` is rejected end-to-end — even when the record is
        large enough that neither the L2-SYN-022 capacity check nor the
        record-bounded reads would fire. This isolates the new invariant from
        the over-claim/bounds path. Mirrors the Rust
        ``rt_to_rt_cmd_word_count_mismatch_rejected``.
        """
        from aero1553.exceptions import MiePayloadError
        from tests.conftest import RECORD_RT15_SA11_RCV

        # R1: word_count=13 (26 bytes), type 0x08 (RT-to-RT). Cmd1 0x7963
        # (RT15 R SA11 dwc=3); Cmd2 0x7965 (RT15 R SA11 dwc=5, direction
        # Receive so L2-SYN-023 passes). word_count=13 clears the Cmd1-based
        # capacity minimum (1+3+1+(3+3)=11) and holds Cmd2's full 5-word
        # payload + rx_status, so neither the capacity check nor the
        # record-bounded reads fire — only the Cmd1/Cmd2 mismatch is at fault.
        r1 = (
            b"\x08\x0d"  # Type: wc=13, type=0x08 (RT_TO_RT)
            b"\x0f\x18\x26\xdb\x21\xf6"  # IRIG timestamp (3 words)
            b"\x63\x79"  # Cmd1 0x7963 (RT15 R SA11 dwc=3)
            b"\x65\x79"  # Cmd2 0x7965 (RT15 R SA11 dwc=5)
            b"\x00\x00"  # tx_status
            + bytes(10)  # 5 data words
            + b"\x00\x00"  # rx_status → total 13 words = 26 B
        )
        assert len(r1) == 26
        data = r1 + RECORD_RT15_SA11_RCV
        fpath = tmp_path / "rt_to_rt_mismatch.mie"
        fpath.write_bytes(data)

        with pytest.raises(MiePayloadError):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))

        messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 1
        assert messages[0].file_offset == 26
        assert messages[0].command_word is not None
        assert messages[0].command_word.rt == 15

    @pytest.mark.requirement("L2-DEC-002", "L2-LOG-001")
    def test_irig_day_of_year_advises_once_per_decode(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """PRA-9: decoding calendar-locked (non-freerun) IRIG records emits a
        one-time advisory about the known day-of-year firmware discrepancy —
        once per decode, not once per record.

        L2-LOG-001: at INFO, not WARNING. It is a standing disclaimer about
        card firmware rather than an observation about this recording, so it
        must not sit in the default output of every run.
        """
        import logging

        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "irig_day.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV * 3)  # 3 non-freerun IRIG records
        with caplog.at_level(logging.INFO, logger="aero1553.reader"):
            messages = list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 3
        advisories = [r for r in caplog.records if "day-of-year" in r.getMessage()]
        assert len(advisories) == 1, (
            "day-of-year advisory should fire exactly once per decode, got "
            f"{[a.getMessage() for a in advisories]}"
        )
        assert advisories[0].levelno == logging.INFO, (
            "the advisory is INFO, not WARNING -- at WARNING it appears in the "
            "default output of every decode of a calendar-locked IRIG file"
        )

    @pytest.mark.requirement("L2-LOG-001")
    def test_irig_day_of_year_advisory_is_silent_at_the_default_level(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """L2-LOG-001: nothing at the default WARNING level."""
        import logging

        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "irig_day.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV)
        with caplog.at_level(logging.WARNING, logger="aero1553.reader"):
            assert len(list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))) == 1
        assert not [r for r in caplog.records if "day-of-year" in r.getMessage()]

    @pytest.mark.requirement("L2-LOG-001")
    def test_irig_day_of_year_advisory_can_be_disabled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """L2-LOG-001: ``set_irig_day_advisory(False)`` suppresses it even at
        INFO -- the case for a site that has diffed its card model against
        vendor CSV and wants a verbose run without the known-noise line."""
        import logging

        from aero1553.logger import set_irig_day_advisory
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "irig_day.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV)
        set_irig_day_advisory(False)
        try:
            with caplog.at_level(logging.INFO, logger="aero1553.reader"):
                assert len(list(MieFileReader(fpath, input_time_format=TimestampFormat.IRIG))) == 1
        finally:
            # Module-level state: restore it or every later test in the process
            # runs with the advisory off.
            set_irig_day_advisory(True)
        assert not [r for r in caplog.records if "day-of-year" in r.getMessage()]

    @pytest.mark.requirement("L2-DEC-010")
    def test_file_offset_tracking(self, tmp_mie_file: Path) -> None:
        """Each message should report its byte offset in the file."""
        messages = list(MieFileReader(tmp_mie_file))
        assert messages[0].file_offset == 0
        assert messages[1].file_offset == 72  # 36 words * 2
        assert messages[2].file_offset == 72 + 34  # 36*2 + 17*2

    @pytest.mark.requirement("L2-RDR-004")
    def test_first_record_truncation_strict_raises_distinct_error(self, tmp_path: Path) -> None:
        """L2-RDR-004: a file containing a structurally-valid Type Word
        whose declared extent runs past EOF SHALL surface a distinct
        error class in strict mode (MieFirstRecordTruncatedError, NOT
        the generic MieRecordTruncatedError)."""
        from aero1553.exceptions import (
            MieFirstRecordTruncatedError,
            MieRecordTruncatedError,
        )
        from tests.conftest import RECORD_RT15_SA11_RCV

        # First 20 bytes of a 72-byte record: Type Word valid (msg_type
        # 0x02, wc=36), but the record needs 72 bytes and only 20 exist.
        fpath = tmp_path / "first_truncated.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV[:20])
        with pytest.raises(MieFirstRecordTruncatedError) as exc_info:
            list(MieFileReader(fpath, strict=True))
        # Distinct from the generic MieRecordTruncatedError.
        assert not isinstance(exc_info.value, MieRecordTruncatedError)
        assert exc_info.value.record_bytes == 72
        assert exc_info.value.available_bytes == 20

    @pytest.mark.requirement("L2-DEC-013")
    def test_forced_format_mismatch_strict_raises(self, tmp_path: Path) -> None:
        """L2-DEC-013: forcing the wrong timestamp format on a recording
        the probe is decisive about SHALL raise in strict mode rather than
        silently emit garbage timestamps."""
        from aero1553.exceptions import MieTimestampFormatMismatchError
        from tests.conftest import RECORD_RT15_SA11_RCV

        # Two valid IRIG records → the probe is decisive for IRIG.
        fpath = tmp_path / "forced_mismatch.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV * 2)
        with pytest.raises(MieTimestampFormatMismatchError):
            list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.STANDARD))

    @pytest.mark.requirement("L2-DEC-013")
    def test_forced_format_mismatch_lenient_proceeds(self, tmp_path: Path) -> None:
        """L2-DEC-013: in lenient mode the same forced-format mismatch
        SHALL log a WARN but proceed with the forced format, not abort."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "forced_mismatch_lenient.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV * 2)
        # Does not raise; records may be skipped on invariant violations,
        # but the stream completes.
        list(MieFileReader(fpath, input_time_format=TimestampFormat.STANDARD))

    @pytest.mark.requirement("L2-DEC-013")
    def test_forced_format_matching_is_not_flagged(self, tmp_path: Path) -> None:
        """L2-DEC-013: forcing the format the probe agrees with SHALL NOT
        trip the mismatch check — the records decode normally."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "forced_match.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV * 2)
        messages = list(MieFileReader(fpath, strict=True, input_time_format=TimestampFormat.IRIG))
        assert len(messages) == 2

    @pytest.mark.requirement("L2-RDR-004")
    def test_first_record_truncation_lenient_terminates_clean(self, tmp_path: Path) -> None:
        """L2-RDR-004: lenient mode SHALL terminate cleanly with zero
        records emitted on first-record truncation."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "first_truncated_lenient.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV[:20])
        messages = list(MieFileReader(fpath))
        assert messages == []


class TestCsvWriter:
    """Integration tests for CSV output."""

    @pytest.mark.requirement("L2-WRT-001")
    @pytest.mark.requirement("L2-WRT-013")
    def test_csv_header(self, tmp_mie_file: Path) -> None:
        """CSV output should start with the correct header row."""
        buf = io.StringIO()
        write_csv(MieFileReader(tmp_mie_file), output=buf)
        buf.seek(0)
        reader = csv.reader(buf)
        header = next(reader)
        assert header == CSV_HEADER

    @pytest.mark.requirement("L1-OUT-001")
    @pytest.mark.requirement("L2-WRT-001")
    def test_vendor_block_precedes_decoder_added_columns(self) -> None:
        """The 44-column DDC vendor block comes first, in vendor order, with the
        decoder's own columns appended after it — so column N of a decoded CSV is
        column N of a vendor CSV for every N in 1..=44.

        Mirrors ``vendor_block_precedes_decoder_added_columns`` in
        ``rust/src/writer.rs``.
        """
        from aero1553.writer import VENDOR_COLUMN_COUNT

        assert len(CSV_HEADER) == 46, "46 columns total"
        assert len(CSV_HEADER) - VENDOR_COLUMN_COUNT == 2, "two decoder additions"

        # The vendor block ends at XMT_GAP...
        assert CSV_HEADER[VENDOR_COLUMN_COUNT - 1] == "XMT_GAP"
        # ...and the gap columns sit at their vendor indices (1-based 42/43/44),
        # which is exactly what the pre-v2.10.0 interleaved layout got wrong.
        assert CSV_HEADER[41] == "IM_GAP"
        assert CSV_HEADER[42] == "RCV_GAP"
        assert CSV_HEADER[43] == "XMT_GAP"
        # Decoder additions are strictly at the tail.
        assert CSV_HEADER[VENDOR_COLUMN_COUNT:] == ["ERROR", "ERROR_CODE"]
        # No decoder-added column may appear inside the vendor block.
        vendor_block = CSV_HEADER[:VENDOR_COLUMN_COUNT]
        assert "ERROR" not in vendor_block
        assert "ERROR_CODE" not in vendor_block

    @pytest.mark.requirement("L2-WRT-001")
    def test_csv_row_count(self, tmp_mie_file: Path) -> None:
        """Should produce one header + 3 data rows."""
        buf = io.StringIO()
        write_csv(MieFileReader(tmp_mie_file), output=buf)
        buf.seek(0)
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 4  # header + 3 records

    @pytest.mark.requirement("L2-WRT-003")
    @pytest.mark.requirement("L2-WRT-004")
    @pytest.mark.requirement("L2-WRT-013")
    @pytest.mark.requirement("L2-ERR-007")
    def test_csv_first_row_fields(self, tmp_mie_file: Path) -> None:
        """First CSV data row should match validated vendor output."""
        buf = io.StringIO()
        write_csv(MieFileReader(tmp_mie_file), output=buf)
        buf.seek(0)
        reader = csv.reader(buf)
        next(reader)  # skip header
        row = next(reader)

        # Resolve by column NAME rather than by hardcoded index. Hardcoded
        # positions are what let the pre-v2.10.0 column-order bug hide: the
        # names all still matched, so only a positional check would have caught
        # ERROR/ERROR_CODE sitting inside the vendor block — and this test was
        # itself written against those wrong positions.
        col = {name: i for i, name in enumerate(CSV_HEADER)}
        assert row[col["TIME_STAMP"]] == "192:15:54:50.456225"
        assert row[col["RT"]] == "15"
        assert row[col["MSG"]] == "11R"
        assert row[col["WD01"]] == "0400"
        assert row[col["STAT"]] == "7800"
        assert row[col["CMD"]] == "797E"
        assert row[col["BUS"]] == "A"
        assert row[col["DELTA"]] == "0.000000"
        assert row[col["ERROR"]] == ""  # normal message
        assert row[col["ERROR_CODE"]] == ""  # normal message
        # The gap columns are empty and sit at their vendor indices (1-based
        # 42/43/44 → 0-based 41/42/43), immediately before the decoder's own
        # two appended columns.
        assert row[41:44] == ["", "", ""], "IM_GAP/RCV_GAP/XMT_GAP"
        assert col["ERROR"] == 44
        assert col["ERROR_CODE"] == 45

    @pytest.mark.requirement("L2-WRT-002")
    def test_csv_data_word_padding(self, tmp_mie_file: Path) -> None:
        """Records with <32 data words should have empty trailing WD cols."""
        buf = io.StringIO()
        write_csv(MieFileReader(tmp_mie_file), output=buf)
        buf.seek(0)
        reader = csv.reader(buf)
        next(reader)  # skip header
        next(reader)  # skip first row (30 words)
        row = next(reader)  # second row: 11 data words
        # WD12 through WD32 (indices 14..34) should be empty
        assert row[14] == ""  # WD12
        assert row[34 - 1] == ""  # WD32

    @pytest.mark.requirement("L2-WRT-012")
    def test_csv_to_file(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """Writing to a file path should produce a valid CSV file."""
        out = tmp_path / "output.csv"
        outcome = write_csv(MieFileReader(tmp_mie_file), output=out)
        assert out.exists()
        raw = out.read_bytes()
        assert b"\r\n" not in raw
        lines = raw.decode().strip().split("\n")
        assert len(lines) == 4
        assert outcome.normal_count == 3
        assert outcome.partial is None

    @pytest.mark.requirement("L3-PY-012")
    def test_write_csv_returns_count(self, tmp_mie_file: Path) -> None:
        """write_csv should return a WriteOutcome whose normal_count
        matches the number of messages written."""
        buf = io.StringIO()
        outcome = write_csv(MieFileReader(tmp_mie_file), output=buf)
        assert outcome.normal_count == 3
        assert outcome.partial is None


class TestAtomicWriteSafety:
    """L2-WRT-014 through L2-WRT-024 enforcement tests for the Python writer."""

    @pytest.mark.requirement("L2-WRT-023")
    def test_no_clobber_refuses_a_destination_created_after_the_preflight(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """``--no-clobber`` is enforced at the commit, not by the pre-flight.

        The destination does not exist when the write starts, so the pre-flight
        passes; the record source then creates it mid-decode, exactly the race
        a pre-flight cannot see. The commit must refuse rather than replace it.
        """
        from aero1553.exceptions import MieClobberRefusedError
        from aero1553.writer import WriteOptions, write_csv

        dest = tmp_path / "race.csv"

        def records_while_someone_creates_the_destination() -> Iterator[MieMessage]:
            for i, msg in enumerate(MieFileReader(tmp_mie_file)):
                if i == 1:
                    dest.write_text("created by someone else", encoding="utf-8")
                yield msg

        records = records_while_someone_creates_the_destination()
        options = WriteOptions(no_clobber=True)
        with pytest.raises(MieClobberRefusedError):
            write_csv(records, dest, options)
        assert dest.read_text(encoding="utf-8") == "created by someone else"
        assert [p.name for p in tmp_path.iterdir() if ".tmp." in p.name] == []

    @pytest.mark.requirement("L2-WRT-024")
    def test_a_failed_commit_is_a_writer_error_and_leaves_nothing_behind(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """A commit whose move fails surfaces as ``MieWriterError`` chaining
        the OS error, leaves the destination unmodified, and leaves no temp.

        The destination is an existing directory: every row writes fine to the
        temp, and the failure lands on the final move onto the destination.
        """
        from aero1553.exceptions import MieWriterError
        from aero1553.writer import write_csv

        dest = tmp_path / "out.csv"
        dest.mkdir()
        (dest / "keep.txt").write_text("untouched", encoding="utf-8")

        reader = MieFileReader(tmp_mie_file)
        with pytest.raises(MieWriterError) as caught:
            write_csv(reader, dest)
        assert isinstance(caught.value.__cause__, OSError)
        assert (dest / "keep.txt").read_text(encoding="utf-8") == "untouched"
        assert [p.name for p in tmp_path.iterdir() if ".tmp." in p.name] == []

    @pytest.mark.requirement("L2-WRT-014")
    def test_paths_refer_to_same_file_existing(self, tmp_path: Path) -> None:
        from aero1553.writer import paths_refer_to_same_file

        p = tmp_path / "x.dat"
        p.write_bytes(b"x")
        assert paths_refer_to_same_file(p, p) is True

    @pytest.mark.requirement("L2-WRT-014")
    def test_paths_refer_to_same_file_distinct(self, tmp_path: Path) -> None:
        from aero1553.writer import paths_refer_to_same_file

        a = tmp_path / "a.dat"
        a.write_bytes(b"a")
        b = tmp_path / "b.dat"  # doesn't exist
        assert paths_refer_to_same_file(a, b) is False

    @pytest.mark.requirement("L2-WRT-014")
    def test_write_csv_rejects_input_output_collision(self, tmp_mie_file: Path) -> None:
        """L2-WRT-014: refuse to write CSV over the input file."""
        from aero1553.exceptions import MieInputOutputCollisionError
        from aero1553.writer import WriteOptions

        opts = WriteOptions(input_path=tmp_mie_file, no_clobber=False)
        original_bytes = tmp_mie_file.read_bytes()
        reader = MieFileReader(tmp_mie_file)
        with pytest.raises(MieInputOutputCollisionError) as exc_info:
            write_csv(reader, output=tmp_mie_file, opts=opts)
        assert str(tmp_mie_file) in str(exc_info.value)
        # Input file MUST be unchanged.
        assert tmp_mie_file.read_bytes() == original_bytes

    @pytest.mark.requirement("L2-WRT-017")
    def test_write_csv_rejects_clobber_with_no_clobber(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """L2-WRT-017: refuse to overwrite an existing destination."""
        from aero1553.exceptions import MieClobberRefusedError
        from aero1553.writer import WriteOptions

        out = tmp_path / "out.csv"
        out.write_text("EXISTING\n", encoding="utf-8")
        opts = WriteOptions(no_clobber=True)
        reader = MieFileReader(tmp_mie_file)
        with pytest.raises(MieClobberRefusedError):
            write_csv(reader, output=out, opts=opts)
        # Existing file untouched.
        assert out.read_text(encoding="utf-8") == "EXISTING\n"

    @pytest.mark.requirement("L2-WRT-017")
    def test_write_csv_overwrites_by_default(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """No-clobber off (default): existing destination is replaced."""
        out = tmp_path / "out.csv"
        out.write_text("OLD\n", encoding="utf-8")
        outcome = write_csv(MieFileReader(tmp_mie_file), output=out)
        assert outcome.normal_count == 3
        text = out.read_text(encoding="utf-8")
        assert text.startswith("TIME_STAMP,RT,MSG,")

    @pytest.mark.requirement("L3-WRT-001")
    def test_write_csv_to_file_cleans_up_temp(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """L3-WRT-001: after a successful write, no temp file should remain."""
        out = tmp_path / "out.csv"
        write_csv(MieFileReader(tmp_mie_file), output=out)
        # Temp pattern: <output>.aero1553.tmp.<pid>
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("out.csv.aero1553.tmp.")]
        assert leftovers == [], f"unexpected temp file(s): {leftovers}"

    @pytest.mark.requirement("L2-WRT-014")
    def test_write_csv_split_rejects_input_output_collision(self, tmp_mie_file: Path) -> None:
        from aero1553.exceptions import MieInputOutputCollisionError
        from aero1553.writer import WriteOptions, write_csv_split

        opts = WriteOptions(input_path=tmp_mie_file, no_clobber=False)
        reader = MieFileReader(tmp_mie_file)
        with pytest.raises(MieInputOutputCollisionError):
            write_csv_split(reader, output=tmp_mie_file, opts=opts)

    @pytest.mark.requirement("L2-WRT-014")
    @pytest.mark.requirement("L2-WRT-016")
    @pytest.mark.requirement("L2-WRT-019")
    def test_write_csv_split_rejects_collision_on_the_errors_partial_path(
        self, tmp_path: Path
    ) -> None:
        """The FOURTH commit target: the errors file's own ``.partial``.

        Written when split mode meets an ``--allow-partial`` sync loss. It had
        only the enumeration test covering it -- that it appears in a list --
        and nothing exercising it end to end. "The other three are checked and
        this one is in the same list" is a composition argument, and a
        composition argument ("derived from output, which was already checked")
        is what put the hole here in the first place.

        Deliberately isolated: with ``-o capture.mie`` none of the other three
        targets matches this input, so only the target under test can make it
        pass.
        """
        from aero1553.exceptions import MieInputOutputCollisionError
        from aero1553.writer import (
            WriteOptions,
            commit_targets,
            error_path_for,
            partial_path_for,
            write_csv_split,
        )

        dest = tmp_path / "capture.mie"
        victim = partial_path_for(error_path_for(dest))
        victim.write_bytes(b"the recording being decoded")
        original = victim.read_bytes()

        # Confirm the isolation rather than trusting it: exactly one target may
        # name this file, or the test proves nothing.
        matching = [t for t in commit_targets(dest, True, True) if t == victim]
        assert len(matching) == 1

        # Without allow_partial the errors `.partial` is not a target at all.
        write_csv_split(iter([]), output=dest, opts=WriteOptions(input_path=victim))
        assert dest.exists()
        dest.unlink()

        # Built OUTSIDE the `raises` block so only `write_csv_split` can satisfy
        # it: a constructor failure would otherwise pass this test for the wrong
        # reason (S5778).
        partial_opts = WriteOptions(input_path=victim, allow_partial=True)
        empty: Iterator[MieMessage] = iter([])
        with pytest.raises(MieInputOutputCollisionError):
            write_csv_split(empty, output=dest, opts=partial_opts)
        assert victim.read_bytes() == original, "input modified despite the rejection"
        assert not dest.exists(), "main destination must not be created"

    @pytest.mark.requirement("L2-WRT-014")
    def test_commit_targets_enumerates_every_committable_path(self) -> None:
        """The enumeration itself, pinned: main, errors, then their ``.partial``.

        The errors file's own ``.partial`` is the one an audit forgets, and
        split mode commits it.
        """
        from aero1553.writer import commit_targets

        out = Path("dir") / "capture.csv"
        names = lambda *a: [p.name for p in commit_targets(out, *a)]  # noqa: E731

        assert names(False, False) == ["capture.csv"]
        assert names(True, False) == ["capture.csv", "capture_errors.csv"]
        assert names(False, True) == ["capture.csv", "capture.csv.partial"]
        assert names(True, True) == [
            "capture.csv",
            "capture_errors.csv",
            "capture.csv.partial",
            "capture_errors.csv.partial",
        ]
        # Every target stays beside the destination, so each rename is
        # same-filesystem and stays atomic (L2-WRT-015).
        assert {p.parent for p in commit_targets(out, True, True)} == {out.parent}

    @pytest.mark.requirement("L2-WRT-014")
    def test_python_path_helpers_name_the_paths_the_writer_commits(self) -> None:
        """``error_path_for`` / ``partial_path_for`` are Python; the commit
        target list is the Rust writer's own. They must name the same paths, or
        the path the API reports is not the path that gets written."""
        from aero1553.writer import commit_targets, error_path_for, partial_path_for

        for out in (Path("dir") / "capture.csv", Path("x.y.tar"), Path("noext"), Path(".hidden")):
            errors = error_path_for(out)
            assert commit_targets(out, True, True) == [
                out,
                errors,
                partial_path_for(out),
                partial_path_for(errors),
            ]

    @pytest.mark.requirement("L2-WRT-014")
    def test_write_csv_split_rejects_collision_on_the_derived_errors_path(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """Every path a run could commit is a collision candidate.

        The derived errors path is an ordinary path that can name a *different*
        input: ``-o capture.mie --separate-errors`` derives
        ``capture_errors.mie``, a plausible recording name. Before this, the
        errors file committed straight over that input and the run exited 0.
        """
        from aero1553.exceptions import MieInputOutputCollisionError
        from aero1553.writer import WriteOptions, error_path_for, write_csv_split

        dest = tmp_path / "capture.mie"
        victim = error_path_for(dest)
        victim.write_bytes(tmp_mie_file.read_bytes())
        original = victim.read_bytes()

        # Built OUTSIDE the `raises` block, per S5778 -- see the note above.
        opts = WriteOptions(input_path=victim, no_clobber=False)
        empty: Iterator[MieMessage] = iter([])
        with pytest.raises(MieInputOutputCollisionError):
            write_csv_split(empty, output=dest, opts=opts)

        assert victim.read_bytes() == original, "input modified despite the rejection"
        assert not dest.exists(), "main destination must not be created"

    @pytest.mark.requirement("L2-WRT-014")
    @pytest.mark.requirement("L2-WRT-016")
    def test_write_csv_rejects_collision_on_the_partial_path(self, tmp_path: Path) -> None:
        """``<destination>.partial`` is a commit target under ``allow_partial``.

        Enumerated even though a clean decode never writes one: the guard runs
        before the output is opened, which is the only point at which refusing
        is still safe, and by then nobody knows whether the decode will lose
        sync.
        """
        from aero1553.exceptions import MieInputOutputCollisionError
        from aero1553.writer import WriteOptions, partial_path_for

        dest = tmp_path / "out.csv"
        victim = partial_path_for(dest)
        victim.write_bytes(b"the recording being decoded")
        original = victim.read_bytes()

        # Without allow_partial there is no `.partial` target, so the same pair
        # of paths is a perfectly ordinary decode.
        write_csv(iter([]), output=dest, opts=WriteOptions(input_path=victim))
        assert dest.exists()
        dest.unlink()

        # Built OUTSIDE the `raises` block, per S5778 -- see the note above.
        partial_opts = WriteOptions(input_path=victim, allow_partial=True)
        empty: Iterator[MieMessage] = iter([])
        with pytest.raises(MieInputOutputCollisionError):
            write_csv(empty, output=dest, opts=partial_opts)
        assert victim.read_bytes() == original, "input modified despite the rejection"
        assert not dest.exists(), "main destination must not be created"

    @pytest.mark.requirement("L2-SYN-011")
    @pytest.mark.requirement("L1-EXIT-002")
    def test_no_valid_records_raises(self, tmp_path: Path) -> None:
        """L1-EXIT-002: input with no decodable records raises MieNoValidRecordsError."""
        from aero1553.exceptions import MieNoValidRecordsError

        bad = tmp_path / "garbage.bin"
        bad.write_bytes(b"\xff" * 1024)  # 1 KB of 0xFF — no valid Type Word
        reader = MieFileReader(bad)
        with pytest.raises(MieNoValidRecordsError):
            list(reader)

    @pytest.mark.requirement("L1-EXIT-010")
    @pytest.mark.requirement("L2-RDR-021")
    def test_empty_recording_terminator_only_yields_zero_records(self, tmp_path: Path) -> None:
        """L1-EXIT-010: a file whose record stream opens on the 0x0000
        end-of-records terminator is a valid but empty recording — zero records,
        NO error, and ``empty_recording`` is set. Models an unused channel
        recording (literally the two bytes ``00 00``)."""
        f = tmp_path / "empty.mie"
        f.write_bytes(b"\x00\x00")
        reader = MieFileReader(f)
        msgs = list(reader)  # must NOT raise
        assert msgs == []
        assert reader.empty_recording is True

    @pytest.mark.requirement("L1-EXIT-010")
    @pytest.mark.requirement("L2-RDR-021")
    def test_wrong_file_is_not_an_empty_recording(self, tmp_path: Path) -> None:
        """L1-EXIT-010: a wrong-file (no terminator, no valid record) still
        raises NoValidRecords and is not mistaken for an empty recording."""
        from aero1553.exceptions import MieNoValidRecordsError

        f = tmp_path / "junk.mie"
        f.write_bytes(b"\xff" * 256)
        reader = MieFileReader(f)
        with pytest.raises(MieNoValidRecordsError):
            list(reader)
        assert reader.empty_recording is False

    @pytest.mark.requirement("L2-SYN-028")
    def test_single_record_then_terminator_decodes(self, tmp_path: Path) -> None:
        """L2-SYN-028: one valid record followed by the terminator decodes.
        Regression: the lone record's look-ahead follower is the terminator,
        which the pre-fix look-ahead rejected."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        f = tmp_path / "single.mie"
        f.write_bytes(RECORD_RT15_SA11_RCV + b"\x00\x00")
        reader = MieFileReader(f)
        assert len(list(reader)) == 1
        assert reader.empty_recording is False

    @pytest.mark.requirement("L2-SYN-028")
    def test_last_record_before_terminator_not_dropped(self, tmp_path: Path) -> None:
        """L2-SYN-028: the last record before the terminator is not dropped.
        Every well-formed recording ends ``...record, 00 00``; the pre-fix
        look-ahead silently lost that final record."""
        from tests.conftest import RECORD_RT15_SA11_RCV

        f = tmp_path / "multi.mie"
        f.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV + b"\x00\x00")
        reader = MieFileReader(f)
        assert len(list(reader)) == 2

    @pytest.mark.requirement("L2-SYN-011")
    @pytest.mark.requirement("L1-EXIT-004")
    def test_lenient_unrecoverable_sync_loss_raises(self, tmp_path: Path) -> None:
        """L1-EXIT-004: lenient-mode mid-file sync loss (not truncation) raises."""
        from aero1553.exceptions import MieUnrecoverableSyncLossError
        from tests.conftest import RECORD_RT15_SA11_RCV

        # Two valid records + 70 KB of 0xFF — the second record's
        # look-ahead succeeds (next bytes are 0xFF Type Word — invalid),
        # so recover_sync from offset 72 scans the full 64 KB and finds
        # no valid record → unrecoverable corruption.
        fpath = tmp_path / "corruption.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV + b"\xff" * 70_000)
        reader = MieFileReader(fpath)
        with pytest.raises(MieUnrecoverableSyncLossError) as exc_info:
            list(reader)
        assert exc_info.value.sync_losses >= 1

    @pytest.mark.requirement("L3-WRT-002")
    @pytest.mark.requirement("L1-EXIT-004")
    def test_write_csv_with_allow_partial_commits_dot_partial(self, tmp_path: Path) -> None:
        """allow_partial converts UnrecoverableSyncLoss to a .partial file
        and a non-None WriteOutcome.partial."""
        from aero1553.writer import WriteOptions
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "corruption.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV + b"\xff" * 70_000)
        out = tmp_path / "out.csv"
        opts = WriteOptions(allow_partial=True)
        outcome = write_csv(MieFileReader(fpath), output=out, opts=opts)
        assert outcome.partial is not None
        assert outcome.partial.sync_losses >= 1
        partial_path = outcome.partial.main_path
        assert partial_path.exists()
        assert partial_path.name == "out.csv.partial"
        # Main destination must NOT exist.
        assert not out.exists()
        # The partial should contain the records that decoded
        # successfully before the sync loss.
        body = partial_path.read_text(encoding="utf-8")
        assert body.startswith("TIME_STAMP,RT,MSG,")
        assert "11R" in body

    @pytest.mark.requirement("L2-DEC-015")
    def test_cli_detect_records_flag_accepts_valid_size(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """--detect-records N accepts a value in [1, 32] and decodes
        normally on a valid fixture."""
        from aero1553.cli import main

        out = tmp_path / "out.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--detect-records",
                "2",
            ]
        )
        assert rc == 0
        assert out.exists()

    @pytest.mark.requirement("L2-DEC-015")
    def test_cli_detect_records_flag_rejects_out_of_range(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """--detect-records above the max (32) is rejected at parse
        time with a non-zero exit and the offending value in
        stderr."""
        from aero1553.cli import main

        out = tmp_path / "out.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--detect-records",
                "999",
            ]
        )
        assert rc != 0
        captured = capfd.readouterr()
        assert "--detect-records" in captured.err
        assert "999" in captured.err

    @pytest.mark.requirement("L2-CLI-012")
    @pytest.mark.requirement("L2-DEC-017")
    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-007")
    def test_cli_standard_tick_rate_hz_flag_rejects_nonpositive(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """--standard-tick-rate-hz <= 0 is a CLI usage error: exit 4
        (L2-CLI-011/L2-CLI-012) with the offending flag in stderr."""
        from aero1553.cli import main

        out = tmp_path / "out.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--standard-tick-rate-hz",
                "0",
            ]
        )
        assert rc == 4
        captured = capfd.readouterr()
        assert "--standard-tick-rate-hz" in captured.err

    @pytest.mark.requirement("L2-CLI-012")
    @pytest.mark.requirement("L2-DEC-017")
    @pytest.mark.requirement("L2-RDR-019")
    def test_cli_standard_tick_rate_hz_enables_delta(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """With --standard-tick-rate-hz set, Standard-timestamp records
        get a non-empty DELTA; without it, DELTA stays empty. Uses the
        shared conformance fixture so the bytes match what CI validates.

        The two records are 16 ticks apart (100000 -> 100016); at 1 MHz
        that is 0.000016 s.
        """
        import csv as _csv

        from aero1553.cli import main

        fixture = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "conformance"
            / "inputs"
            / "standard-timestamps.hex"
        )
        # Same hex-fixture parse as tests/conformance/run.py:read_hex —
        # strip per-line `#` comments, join, decode (fromhex ignores
        # whitespace).
        hex_text = "".join(line.split("#", 1)[0] for line in fixture.read_text().splitlines())
        data = bytes.fromhex(hex_text)
        rec = tmp_path / "standard.mie"
        rec.write_bytes(data)

        # Calibrated: DELTA populated.
        out = tmp_path / "calibrated.csv"
        rc = main(
            [
                "decode",
                str(rec),
                "-o",
                str(out),
                "--input-time-format",
                "standard",
                "--standard-tick-rate-hz",
                "1000000",
            ]
        )
        assert rc == 0
        rows = list(_csv.DictReader(out.read_text().splitlines()))
        assert rows[0]["DELTA"] == "0.000000"
        assert rows[1]["DELTA"] == "0.000016"

        # Uncalibrated: DELTA empty (unchanged historical behavior).
        out2 = tmp_path / "uncalibrated.csv"
        rc = main(
            [
                "decode",
                str(rec),
                "-o",
                str(out2),
                "--input-time-format",
                "standard",
            ]
        )
        assert rc == 0
        rows2 = list(_csv.DictReader(out2.read_text().splitlines()))
        assert rows2[0]["DELTA"] == ""
        assert rows2[1]["DELTA"] == ""

    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-002")
    def test_cli_no_valid_records_returns_exit_2(self, tmp_path: Path) -> None:
        """CLI maps MieNoValidRecordsError to exit code 2 (L1-EXIT-002)."""
        from aero1553.cli import main

        bad = tmp_path / "garbage.bin"
        bad.write_bytes(b"\xff" * 1024)
        rc = main(["decode", str(bad), "-o", str(tmp_path / "out.csv")])
        assert rc == 2
        # No output file should have been created.
        assert not (tmp_path / "out.csv").exists()

    @pytest.mark.requirement("L1-EXIT-010")
    @pytest.mark.requirement("L2-RDR-021")
    def test_cli_empty_recording_exits_0_with_header_only_csv(self, tmp_path: Path) -> None:
        """L1-EXIT-010: decoding an empty recording (record stream is just the
        0x0000 terminator) exits 0 and writes a header-only CSV — not the
        exit-2 wrong-file rejection."""
        from aero1553.cli import main

        f = tmp_path / "empty.mie"
        f.write_bytes(b"\x00\x00")
        out = tmp_path / "out.csv"
        rc = main(["decode", str(f), "-o", str(out)])
        assert rc == 0
        assert out.exists()
        lines = out.read_text().splitlines()
        assert len(lines) == 1  # header only, no data rows
        assert lines[0].startswith("TIME_STAMP,RT,MSG,")
        assert lines[0].endswith("XMT_GAP,ERROR,ERROR_CODE")

    @pytest.mark.requirement("L1-EXIT-010")
    def test_cli_count_empty_recording_prints_zero_exit_0(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """L1-EXIT-010: count of an empty recording prints 0 and exits 0."""
        from aero1553.cli import main

        f = tmp_path / "empty.mie"
        f.write_bytes(b"\x00\x00")
        rc = main(["count", str(f)])
        assert rc == 0
        assert capfd.readouterr().out.strip() == "0"

    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-002")
    def test_cli_count_no_valid_records_returns_exit_2(self, tmp_path: Path) -> None:
        """L2-CLI-011: count on a wrong-file input exits 2, matching decode.
        Regression: count previously flattened NoValidRecords to exit 1."""
        from aero1553.cli import main

        bad = tmp_path / "junk.mie"
        bad.write_bytes(b"\xff" * 256)
        assert main(["count", str(bad)]) == 2

    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-004")
    def test_cli_unrecoverable_default_returns_exit_3(self, tmp_path: Path) -> None:
        """CLI maps MieUnrecoverableSyncLossError to exit code 3 (L1-EXIT-004)."""
        from aero1553.cli import main
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "corruption.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV + b"\xff" * 70_000)
        out = tmp_path / "out.csv"
        rc = main(["decode", str(fpath), "-o", str(out)])
        assert rc == 3
        # No main output and no .partial under default behavior.
        assert not out.exists()
        assert not (tmp_path / "out.csv.partial").exists()

    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-004")
    def test_cli_unrecoverable_allow_partial_returns_exit_0(self, tmp_path: Path) -> None:
        """--allow-partial converts exit 3 into exit 0 + .partial file."""
        from aero1553.cli import main
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "corruption.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV + RECORD_RT15_SA11_RCV + b"\xff" * 70_000)
        out = tmp_path / "out.csv"
        rc = main(["decode", str(fpath), "-o", str(out), "--allow-partial"])
        assert rc == 0
        assert (tmp_path / "out.csv.partial").exists()
        assert not out.exists()

    @pytest.mark.requirement("L2-CLI-011")
    def test_cli_format_flag_parity_with_rust(self, tmp_path: Path) -> None:
        """The `--format` flag matches the Rust CLI: `csv` is accepted, any
        other value is a runtime error (exit 1) — not a parse error — applied
        after config load like the Rust override path."""
        from aero1553.cli import main
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "rec.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV)

        out = tmp_path / "out.csv"
        assert main(["decode", str(fpath), "--format", "csv", "-o", str(out)]) == 0
        assert out.exists()

        # L1-EXIT-007: an unsupported --format is an invalid FLAG VALUE, so it
        # is a usage error (4), not a runtime error. It returned 1 through
        # v2.14.0 because the check ran after the config merge.
        out2 = tmp_path / "out2.csv"
        assert main(["decode", str(fpath), "--format", "json", "-o", str(out2)]) == 4
        assert not out2.exists()

    @pytest.mark.requirement("L2-WRT-017")
    def test_write_csv_split_no_clobber_checks_errors_file(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """no_clobber must also refuse if the errors-file destination exists."""
        from aero1553.exceptions import MieClobberRefusedError
        from aero1553.writer import WriteOptions, write_csv_split

        out = tmp_path / "out.csv"
        err = tmp_path / "out_errors.csv"
        err.write_text("OLD ERRORS\n", encoding="utf-8")
        opts = WriteOptions(no_clobber=True)
        reader = MieFileReader(tmp_mie_file)
        with pytest.raises(MieClobberRefusedError) as exc_info:
            write_csv_split(reader, output=out, opts=opts)
        assert str(err) in str(exc_info.value)
        # Main destination must not have been created.
        assert not out.exists()
        # Errors file untouched.
        assert err.read_text(encoding="utf-8") == "OLD ERRORS\n"


class TestCliEndToEnd:
    """End-to-end CLI tests."""

    @pytest.mark.requirement("L1-EXIT-005")
    def test_cli_emits_exit_class_summary_on_complete_decode(
        self, tmp_mie_file: Path, tmp_path: Path, run_cli: RunCli
    ) -> None:
        """L1-EXIT-005: decode SHALL log a one-line exit-class summary
        naming one of {complete, partial-recovered, partial-unrecoverable,
        no-records}. This case exercises the `complete` branch."""
        out = tmp_path / "summary.csv"
        result = run_cli(["--log-level", "INFO", "decode", str(tmp_mie_file), "-o", str(out)])
        assert result.rc == 0
        summary_lines = [line for line in result.err.splitlines() if "decode exit class:" in line]
        assert summary_lines, (
            f"expected at least one `decode exit class:` summary line; got {result.err!r}"
        )
        assert any("complete" in line for line in summary_lines), (
            f"expected `complete` in summary; got {summary_lines}"
        )

    @pytest.mark.requirement("L1-EXIT-005")
    def test_cli_emits_no_records_exit_class_summary(self, tmp_path: Path, run_cli: RunCli) -> None:
        """L1-EXIT-005: the `no-records` exit-class summary branch."""
        bad = tmp_path / "garbage.bin"
        bad.write_bytes(b"\xff" * 1024)
        out = tmp_path / "summary.csv"
        result = run_cli(["--log-level", "INFO", "decode", str(bad), "-o", str(out)])
        assert result.rc == 2
        summary_lines = [line for line in result.err.splitlines() if "decode exit class:" in line]
        assert any("no-records" in line for line in summary_lines), (
            f"expected `no-records` in summary; got {summary_lines}"
        )

    @pytest.mark.requirement("L2-WRT-007")
    def test_cli_decode_stdout(self, tmp_mie_file: Path, run_cli: RunCli) -> None:
        """CLI decode should produce CSV on stdout."""
        result = run_cli(["decode", str(tmp_mie_file)])
        assert result.rc == 0
        assert len(result.out.strip().split("\n")) == 4

    @pytest.mark.requirement("L2-CLI-002")
    def test_cli_decode_output_file(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """CLI decode with -o should write to the specified file."""
        from aero1553.cli import main

        out = tmp_path / "cli_out.csv"
        rc = main(["decode", str(tmp_mie_file), "-o", str(out)])
        assert rc == 0
        assert out.exists()

    @pytest.mark.requirement("L2-CLI-002")
    def test_output_dash_is_a_filename_not_stdout(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """``-o -`` writes a file *called* ``-``. stdout is selected by omitting
        the flag, and that is the only way to select it.

        Pinned in all three implementations because this is a rule they once
        disagreed on: the C++ build treated ``-`` as stdout for a while, and the
        CLI-surface-parity gate could not see it — that gate compares flag
        *names*, not what their values mean.
        """
        from aero1553.cli import main

        dash = tmp_path / "-"
        rc = main(["decode", str(tmp_mie_file), "-o", str(dash)])
        assert rc == 0
        assert dash.exists(), "no file named '-' was created"
        assert "MSG" in dash.read_text(encoding="utf-8")
        assert capfd.readouterr().out == "", "`-o -` must not write to stdout"

    @pytest.mark.requirement("L3-PY-010")
    def test_cli_count_subcommand(
        self, tmp_mie_file: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The `count` subcommand prints the integer count to stdout and a
        human-readable status line to stderr."""
        from aero1553.cli import main

        rc = main(["count", str(tmp_mie_file)])
        assert rc == 0
        captured = capfd.readouterr()
        # L3-PY-010: stdout is ONLY the integer + a single newline — no
        # prose, no path, no leading/trailing whitespace (byte-exact so a
        # regression that prints extra whitespace is caught).
        assert captured.out == "3\n"
        assert "counted 3 messages in" in captured.err

    @pytest.mark.requirement("L2-CLI-005")
    def test_cli_decode_missing_file(self, capfd: pytest.CaptureFixture[str]) -> None:
        """CLI decode with nonexistent file should return exit code 1."""
        from aero1553.cli import main

        rc = main(["decode", "/nonexistent/file.mie"])
        assert rc == 1

    @pytest.mark.requirement("L2-CLI-004")
    def test_cli_log_level_info(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """CLI --log-level INFO should emit log messages to stderr."""
        from aero1553.cli import main

        out = tmp_path / "log_test.csv"
        rc = main(["--log-level", "INFO", "decode", str(tmp_mie_file), "-o", str(out)])
        assert rc == 0
        assert out.exists()

    @pytest.mark.requirement("L2-CLI-004")
    def test_cli_log_level_debug(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """CLI --log-level DEBUG should succeed without error."""
        from aero1553.cli import main

        out = tmp_path / "debug_test.csv"
        rc = main(["--log-level", "DEBUG", "decode", str(tmp_mie_file), "-o", str(out)])
        assert rc == 0

    @pytest.mark.requirement("L2-CFG-003")
    def test_cli_toml_logging_level_is_honored_when_no_cli_override(
        self, tmp_mie_file: Path, tmp_path: Path, run_cli: RunCli
    ) -> None:
        """L2-CFG-003 precedence: TOML [logging] level takes effect when
        no --log-level CLI flag is passed. Regression coverage for the
        bug where the TOML value was parsed into config.log_level but
        the CLI never re-configured the logger after loading."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[logging]\nlevel = "INFO"\n', encoding="utf-8")
        out = tmp_path / "decoded.csv"

        result = run_cli(
            ["--config", str(config_path), "decode", str(tmp_mie_file), "-o", str(out)]
        )

        assert result.rc == 0
        # `decode exit class:` is INFO-level; it appears only if the level
        # the TOML asked for actually took effect.
        assert "decode exit class:" in result.err, (
            f"expected `decode exit class:` (INFO) line after TOML set level=INFO; got: "
            f"{result.err!r}"
        )

    @pytest.mark.requirement("L2-CFG-003")
    def test_cli_log_level_overrides_toml_logging_level(
        self, tmp_mie_file: Path, tmp_path: Path, run_cli: RunCli
    ) -> None:
        """L2-CFG-003 precedence: --log-level CLI flag overrides the
        TOML [logging] level (CLI > TOML > default)."""
        # TOML asks for DEBUG (most verbose); CLI asks for ERROR. CLI must
        # win -- no line below ERROR may appear.
        config_path = tmp_path / "config.toml"
        config_path.write_text('[logging]\nlevel = "DEBUG"\n', encoding="utf-8")
        out = tmp_path / "decoded.csv"

        # The control: the TOML alone really does produce sub-ERROR lines, so
        # their absence below is the override and not a quiet decode.
        toml_only = run_cli(
            ["--config", str(config_path), "decode", str(tmp_mie_file), "-o", str(out)]
        )
        assert "INFO " in toml_only.err

        result = run_cli(
            [
                "--log-level",
                "ERROR",
                "--config",
                str(config_path),
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
            ]
        )

        assert result.rc == 0
        below_error = [
            line
            for line in result.err.splitlines()
            if line.startswith(("DEBUG ", "INFO ", "WARN "))
        ]
        assert not below_error, (
            f"CLI --log-level ERROR should suppress all sub-ERROR lines "
            f"even when TOML set level=DEBUG; got: {below_error}"
        )

    @pytest.mark.requirement("L2-CFG-003")
    def test_cli_dump_honors_toml_logging_level(self, tmp_path: Path) -> None:
        """L2-CFG-003 precedence: TOML [logging] level applies to the
        dump subcommand too (--config is global).

        `dump` logs exactly one line of its own: the INFO note that a closed
        stdout consumer is a clean exit. So the level is observed through it --
        dumping into a pipe that closes early -- with and without a TOML that
        raises the level to INFO.
        """
        from tests.conftest import normal_record_rt15_sa11_us

        big = tmp_path / "big.mie"
        big.write_bytes(b"".join(normal_record_rt15_sa11_us(i * 100) for i in range(3000)))
        config_path = tmp_path / "config.toml"
        config_path.write_text('[logging]\nlevel = "INFO"\n', encoding="utf-8")

        def dump_into_closed_pipe(*globals_: str) -> tuple[int, str]:
            proc = subprocess.Popen(
                [sys.executable, "-m", "aero1553", *globals_, "dump", str(big)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdout is not None
            assert proc.stderr is not None
            assert proc.stdout.read(64)
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="replace")
            return proc.wait(timeout=60), err

        rc, err = dump_into_closed_pipe()
        assert rc == 0
        assert "broken-pipe" not in err, "the default level (WARNING) hides the INFO note"

        rc, err = dump_into_closed_pipe("--config", str(config_path))
        assert rc == 0
        assert "broken-pipe" in err, f"TOML level=INFO should reach dump; stderr: {err!r}"

    @pytest.mark.requirement("L2-CFG-001")
    @pytest.mark.requirement("L2-CLI-004")
    def test_cli_logging_level_off_does_not_crash(
        self, tmp_mie_file: Path, tmp_path: Path, run_cli: RunCli
    ) -> None:
        """A config with `logging.level = "OFF"` decodes cleanly and silently.

        Regression: OFF was accepted at config-load but crashed with an
        uncaught ValueError when applied (stdlib has no logging.OFF). It
        now silences all output, matching Rust.
        """
        config_path = tmp_path / "off.toml"
        config_path.write_text('[logging]\nlevel = "OFF"\n', encoding="utf-8")
        out = tmp_path / "decoded.csv"
        result = run_cli(
            ["--config", str(config_path), "decode", str(tmp_mie_file), "-o", str(out)]
        )
        assert result.rc == 0  # not a crash, not exit 5
        assert out.exists()
        assert result.err == "", f"OFF must silence all logging; got {result.err!r}"

    @pytest.mark.requirement("L2-CLI-004")
    def test_cli_log_level_accepts_full_set_case_insensitively(self, tmp_mie_file: Path) -> None:
        """--log-level accepts the same 7 levels as the config file, in any
        case (parity with the Rust CLI) — WARN, OFF, and lowercase spellings
        included. A bogus level is a usage error (exit 4)."""
        from aero1553.cli import main

        for lvl in ["OFF", "WARN", "off", "warn", "Critical", "DEBUG"]:
            rc = main(["--log-level", lvl, "count", str(tmp_mie_file)])
            assert rc == 0, f"--log-level {lvl!r} should be accepted"

        # A bogus level is a usage error (exit 4). It is validated in main()
        # after parse_args, so it returns rather than raising SystemExit.
        assert main(["--log-level", "BOGUS", "count", str(tmp_mie_file)]) == 4

    @pytest.mark.requirement("L2-CLI-004")
    def test_cli_version_and_help_short_circuit_before_log_level_validation(
        self, run_cli: RunCli
    ) -> None:
        """--version / --help are honored even alongside an invalid
        --log-level: those flags are pulled before the level is validated.
        Regression for the prior order-dependent behavior where a bad
        --log-level before --version exited 4."""
        for tail in (["--version"], ["--help"]):
            assert run_cli(["--log-level", "BOGUS", *tail]).rc == 0, f"{tail} should exit 0"

    @pytest.mark.requirement("L2-CLI-005")
    def test_cli_version_flag_all_spellings(self, run_cli: RunCli) -> None:
        """Every accepted spelling of the version flag — both short forms
        (``-V``/``-v``) and the long form in any letter case — prints the version
        and exits 0."""
        for flag in ("-V", "-v", "--version", "--VERSION", "--Version", "--vErSiOn"):
            result = run_cli([flag])
            assert result.rc == 0, f"{flag} should exit 0"
            assert "aero1553" in result.out, f"{flag} should print the version"

    @pytest.mark.requirement("L2-CLI-009")
    def test_cli_dump_records(self, tmp_mie_file: Path, run_cli: RunCli) -> None:
        """CLI dump should print record-aware hex dump to stdout."""
        result = run_cli(["dump", str(tmp_mie_file), "--records", "2"])
        assert result.rc == 0
        assert "Record #0" in result.out
        assert "Record #1" in result.out

    @pytest.mark.requirement("L2-CLI-009")
    def test_cli_dump_raw(self, tmp_mie_file: Path, run_cli: RunCli) -> None:
        """CLI dump --raw should print raw hex to stdout."""
        result = run_cli(["dump", str(tmp_mie_file), "--raw", "--length", "32"])
        assert result.rc == 0
        assert "00000000" in result.out

    @pytest.mark.requirement("L2-CLI-009")
    def test_cli_dump_missing_file(self) -> None:
        """CLI dump with nonexistent file should return exit code 1."""
        from aero1553.cli import main

        rc = main(["dump", "/nonexistent/file.mie"])
        assert rc == 1

    @pytest.mark.requirement("L2-CLI-005")
    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-007")
    def test_cli_no_subcommand(self) -> None:
        """CLI with no subcommand is a usage error: exit 4 (L2-CLI-011)."""
        from aero1553.cli import main

        rc = main([])
        assert rc == 4

    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-007")
    def test_cli_unknown_flag_is_usage_error(self, run_cli: RunCli) -> None:
        """An unknown flag is a usage error: exit 4 -- never 2, which is the
        no-records status."""
        assert run_cli(["decode", "--no-such-flag", "rec.mie"]).rc == 4

    @pytest.mark.requirement("L2-CLI-011")
    @pytest.mark.requirement("L1-EXIT-008")
    def test_cli_malformed_config_is_config_error(self, tmp_path: Path) -> None:
        """A malformed/invalid config is a configuration error: exit 5
        (distinct from a usage error and from a runtime error)."""
        from aero1553.cli import main

        bad = tmp_path / "bad.toml"
        bad.write_text('[decode]\ninput_time_format = "potato"\n')
        # Config load fails before the input file is opened, so the input
        # path need not exist.
        rc = main(["--config", str(bad), "decode", str(tmp_path / "missing.mie")])
        assert rc == 5


class TestDeltaAndErrorRecords:
    """L2-RDR-016/017/018 and L2-ERR-002/005: DELTA edge cases and
    error/SPURIOUS record decoding. Synthetic records are built via the
    helpers in conftest.py so the fixtures stay reviewable in hex form."""

    @pytest.mark.requirement("L2-RDR-016")
    @pytest.mark.requirement("L2-ERR-002")
    def test_errored_record_participates_in_delta(self, tmp_path: Path) -> None:
        """L2-RDR-016: errored records (Type Word bit 14 set) update the per-
        RT/MSG cursor and SHALL receive a DELTA computed against the prior
        message sharing the same key.

        L2-ERR-002: the final word of an errored record decodes as the DDC
        Error Word.
        """
        from tests.conftest import errored_record_rt15_sa11_us, normal_record_rt15_sa11_us

        normal = normal_record_rt15_sa11_us(456_225)
        errored = errored_record_rt15_sa11_us(456_484)  # +0.000259 s
        anchor = normal_record_rt15_sa11_us(456_500)  # for look-ahead
        fpath = tmp_path / "errored_delta.mie"
        fpath.write_bytes(normal + errored + anchor)
        messages = list(MieFileReader(fpath))
        assert len(messages) == 3
        assert messages[0].delta == 0.0
        assert messages[1].is_error
        assert messages[1].error_word == 0x011E
        assert messages[1].delta is not None
        assert messages[1].delta == pytest.approx(0.000259, abs=1e-6)

    @pytest.mark.requirement("L2-RDR-017")
    def test_non_monotonic_timestamp_warns_once_per_key(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """L2-RDR-017: when a record's timestamp is older than the prior
        message for the same RT/MSG key, DELTA SHALL be empty and a WARN
        SHALL be logged, at most once per key per file."""
        import logging

        from tests.conftest import normal_record_rt15_sa11_us

        late = normal_record_rt15_sa11_us(500_000)
        early = normal_record_rt15_sa11_us(400_000)
        earlier = normal_record_rt15_sa11_us(350_000)
        fpath = tmp_path / "out_of_order.mie"
        fpath.write_bytes(late + early + earlier)
        with caplog.at_level(logging.WARNING, logger="aero1553.reader"):
            messages = list(MieFileReader(fpath))
        assert len(messages) == 3
        assert messages[0].delta == 0.0
        assert messages[1].delta is None
        assert messages[2].delta is None
        warns = [r for r in caplog.records if "non-monotonic" in r.getMessage().lower()]
        assert len(warns) == 1, (
            f"expected exactly one non-monotonic WARN per key; got "
            f"{[w.getMessage() for w in warns]}"
        )

    @pytest.mark.requirement("L2-RDR-018")
    @pytest.mark.requirement("L2-ERR-005")
    def test_spurious_data_empty_delta_and_continuation_code(
        self,
        tmp_path: Path,
    ) -> None:
        """L2-RDR-018: SPURIOUS_DATA records have no RT/MSG key, SHALL have
        an empty DELTA, and SHALL NOT update any per-key cursor.

        L2-ERR-005: SPURIOUS_DATA immediately following an errored record
        uses decoder code 0x2000 (continuation).
        """
        from tests.conftest import (
            errored_record_rt15_sa11_us,
            normal_record_rt15_sa11_us,
            spurious_record_us,
        )

        normal = normal_record_rt15_sa11_us(450_000)
        errored = errored_record_rt15_sa11_us(500_000)
        spurious = spurious_record_us(550_000)
        anchor = normal_record_rt15_sa11_us(560_000)
        fpath = tmp_path / "spurious.mie"
        fpath.write_bytes(normal + errored + spurious + anchor)
        messages = list(MieFileReader(fpath))
        assert len(messages) == 4
        # Index 2 is SPURIOUS — empty DELTA, continuation code 0x2000.
        assert messages[2].is_spurious
        assert messages[2].delta is None
        assert messages[2].error_word == 0x2000
        # The SPURIOUS record did NOT update the RT15:11R cursor, so the
        # anchor's DELTA tracks back to the errored record (index 1):
        # (560_000 - 500_000) microseconds = 0.06 seconds.
        assert messages[3].delta is not None
        assert messages[3].delta == pytest.approx(0.06, abs=1e-6)

    @pytest.mark.requirement("L2-SYN-017")
    def test_error_and_spurious_records_pass_validation(
        self,
        tmp_path: Path,
    ) -> None:
        """L2-SYN-017: valid error records and SPURIOUS_DATA records SHALL
        remain eligible record boundaries during validation and recovery —
        i.e. they pass validate_record like normal records."""
        from tests.conftest import (
            errored_record_rt15_sa11_us,
            normal_record_rt15_sa11_us,
            spurious_record_us,
        )

        normal = normal_record_rt15_sa11_us(450_000)
        errored = errored_record_rt15_sa11_us(500_000)
        spurious = spurious_record_us(550_000)
        anchor = normal_record_rt15_sa11_us(560_000)
        fpath = tmp_path / "err_spurious_validation.mie"
        fpath.write_bytes(normal + errored + spurious + anchor)
        messages = list(MieFileReader(fpath))
        # All four records emit cleanly without any skip-due-to-invalid path.
        assert len(messages) == 4
        assert messages[1].is_error
        assert messages[2].is_spurious


class TestFuzzHarness:
    """L1-ROB-001: fuzz harnesses asserting no unexpected escape on arbitrary bytes.

    Mirrors the Rust harnesses in ``rust/tests/integration.rs`` and the C++ one
    in ``cpp/tests/test_fuzz.cpp``: same xorshift64 generator, same seed, same
    size bands, same draw order — so iteration N is the same bytes in all three
    implementations.

    THREE KNOBS, SHARED WITH THE RUST AND C++ HARNESSES. All three read the same
    environment variables and mean the same thing by them, because a burn-in
    scoped differently per language cannot be compared across languages — which
    is the subject of ``docs/FUZZING.md`` section 1.

    ``MIE_FUZZ_ITERATIONS``
        Inputs to generate (default 256).
    ``MIE_FUZZ_STREAM_LOGS``
        ``1`` / ``true`` leaves the ``aero1553`` logger at WARNING so its
        diagnostics stream under ``pytest -s``; anything else silences it.
        Previously the handler was installed unconditionally, so every burn-in
        paid the cost of formatting a quarter of a million WARN records that
        pytest then discarded.
    ``MIE_FUZZ_SUMMARY``
        File to append this run's ``FUZZ-SUMMARY`` line to.

    The generator, the knobs and the summary writer live in
    ``tests/fuzz_support.py``, shared with the merge input-resolution harness in
    ``test_merge.py``. They used to be private helpers on this class, which is
    how that harness came to use ``random.Random`` and see different inputs from
    every other harness in the project.
    """

    @pytest.mark.requirement("L1-ROB-001")
    def test_arbitrary_bytes_never_raise_unexpected_exceptions(
        self,
        tmp_path,
    ) -> None:
        from aero1553.exceptions import Aero1553Error
        from aero1553.reader import MieFileReader

        state = FUZZ_SEED
        iterations = fuzz_iterations()
        total_bytes = 0
        opened = 0
        records = 0
        iter_errors = 0

        with fuzz_logging():
            for i in range(iterations):
                # Sizes range from 32 B (slightly above MIN_RECORD_BYTES_STANDARD)
                # to ~8 KB. The lower bound keeps record headers reachable; the
                # upper bound keeps each iteration fast.
                state, r = fuzz_xorshift64(state)
                size = 32 + (r % 8192)
                state, payload = fuzz_fill(state, size)
                total_bytes += size

                fpath = tmp_path / f"fuzz-{i}.bin"
                fpath.write_bytes(payload)

                try:
                    reader = MieFileReader(fpath)
                except Aero1553Error:
                    # Constructor errors (e.g., MieFileEmptyError) are
                    # documented and acceptable.
                    continue
                except Exception as exc:
                    fuzz_summary(
                        "reader",
                        iterations,
                        f"bytes={total_bytes} opened={opened} "
                        f"open_errors={i - opened} records={records} "
                        f"iter_errors={iter_errors} outcome=error",
                    )
                    raise AssertionError(
                        f"Unexpected non-Aero1553Error on construction "
                        f"(seed=0x{FUZZ_SEED:X}, iter={i}, size={size}): "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                opened += 1

                try:
                    # The canonical-order stage (L2-WRT-021) is on the fuzzed
                    # path deliberately: random bytes readily decode to repeated
                    # or all-zero timestamps, which is exactly the
                    # equal-timestamp run its max_sort_group cap (L2-WRT-022)
                    # exists to bound. A small cap is used so the cap-overflow
                    # branch is reachable at all. Mirrors the Rust harness in
                    # rust/tests/integration.rs.
                    for yielded, _ in enumerate(order_rows(reader, 8), start=1):
                        records += 1
                        if yielded > 100_000:
                            raise AssertionError(
                                f"iterator yielded over 100k items "
                                f"(seed=0x{FUZZ_SEED:X}, iter={i}, size={size}) "
                                f"— possible unbounded loop"
                            )
                except Aero1553Error:
                    # Decode-time errors are documented and acceptable —
                    # the fuzz harness exists to catch IndexError,
                    # struct.error, RecursionError, etc.
                    iter_errors += 1
                except AssertionError:
                    raise
                except Exception as exc:
                    fuzz_summary(
                        "reader",
                        iterations,
                        f"bytes={total_bytes} opened={opened} "
                        f"open_errors={i + 1 - opened} records={records} "
                        f"iter_errors={iter_errors} outcome=error",
                    )
                    raise AssertionError(
                        f"Unexpected non-Aero1553Error during iteration "
                        f"(seed=0x{FUZZ_SEED:X}, iter={i}, size={size}): "
                        f"{type(exc).__name__}: {exc}\n"
                        f"First 32 bytes: {payload[:32].hex()}"
                    ) from exc

        fuzz_summary(
            "reader",
            iterations,
            f"bytes={total_bytes} opened={opened} "
            f"open_errors={iterations - opened} records={records} "
            f"iter_errors={iter_errors} outcome=ok",
        )

    @pytest.mark.requirement("L1-ROB-001")
    @pytest.mark.requirement("L2-CLI-009")
    def test_dump_arbitrary_bytes_never_raise_unexpected_exceptions(
        self,
        tmp_path,
    ) -> None:
        """L1-ROB-001 for the ``dump`` subcommand: the record-aware and raw
        hex dumps must tolerate arbitrary bytes — only a documented
        Aero1553Error (e.g. MieFileEmptyError) may escape.

        The record dump's header reads are bounded by its
        ``offset + MIN_RECORD_BYTES <= file_len`` loop guard (the deepest
        read is the Command Word at ``offset + 8``, needing ``offset + 10``),
        and it slices to ``data[offset:offset + record_bytes]`` for the body —
        it never reads payload by a Command Word's ``data_word_count``, so it
        has no over-claim/overrun class like the reader's ``_extract_payload``
        had. This test guards that property against regression. Sizes are
        skewed small to exercise the truncation/guard paths densely.

        Both dumps are attempted on every input, into a sink cleared between
        them, and each is counted separately — so the summary line means the
        same thing as the Rust harness's on the same bytes. Mirrors the Rust
        ``dump_arbitrary_bytes_never_panics``.

        Output volume is counted in LINES, not bytes. Both dumps print the input
        path in their header, and the harnesses name their temp files
        differently in each implementation — so a byte count is a measure of the
        path, not of the decoder, and the two could never agree. Lines are
        path-independent.
        """
        from aero1553.dump import hex_dump_raw, hex_dump_records
        from aero1553.exceptions import Aero1553Error

        state = FUZZ_SEED  # same seed family as the reader harness
        iterations = fuzz_iterations()
        total_bytes = 0
        records_errors = 0
        records_lines = 0
        raw_errors = 0
        raw_lines = 0

        with fuzz_logging():
            for i in range(iterations):
                state, r = fuzz_xorshift64(state)
                size = r % 512  # small band → dense coverage of guard/truncation
                state, payload = fuzz_fill(state, size)
                total_bytes += size

                fpath = tmp_path / f"dumpfuzz-{i}.bin"
                fpath.write_bytes(payload)

                buf = io.StringIO()
                try:
                    hex_dump_records(fpath, max_records=64, stream=buf)
                except Aero1553Error:
                    # Empty-file / not-found are documented and acceptable.
                    records_errors += 1
                except Exception as exc:
                    raise AssertionError(
                        f"Unexpected non-Aero1553Error from hex_dump_records "
                        f"(iter={i}, size={size}): {type(exc).__name__}: {exc}\n"
                        f"First 32 bytes: {payload[:32].hex()}"
                    ) from exc
                records_lines += buf.getvalue().count("\n")

                buf = io.StringIO()
                try:
                    hex_dump_raw(fpath, start_offset=0, length=None, stream=buf)
                except Aero1553Error:
                    raw_errors += 1
                except Exception as exc:
                    raise AssertionError(
                        f"Unexpected non-Aero1553Error from hex_dump_raw "
                        f"(iter={i}, size={size}): {type(exc).__name__}: {exc}\n"
                        f"First 32 bytes: {payload[:32].hex()}"
                    ) from exc
                raw_lines += buf.getvalue().count("\n")

        fuzz_summary(
            "dump",
            iterations,
            f"bytes={total_bytes} records_errors={records_errors} "
            f"records_lines={records_lines} raw_errors={raw_errors} "
            f"raw_lines={raw_lines} outcome=ok",
        )


class TestDumpDiagnostics:
    """L2-CLI-013: logger diagnostics emitted by the record-aware dump."""

    @pytest.mark.requirement("L2-CLI-013")
    def test_dump_logs_warning_on_truncated_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """L2-CLI-013: a scan-stop anomaly is emitted through the logger at
        WARNING, in addition to the inline report note — so dump diagnostics
        are visible on stderr at the configured level. Mirrors the Rust
        ``record_dump_notes_and_warns_truncated_record``.
        """
        import logging

        from aero1553.dump import hex_dump_records

        # Type 0x2402 declares word_count=36 (72 bytes) but only 20 bytes
        # exist → the record-aware scan hits the truncated-record branch.
        fpath = tmp_path / "truncated.mie"
        fpath.write_bytes(b"\x02\x24" + bytes(18))

        out = io.StringIO()
        with caplog.at_level(logging.WARNING, logger="aero1553.dump"):
            hex_dump_records(fpath, max_records=1, stream=out)

        # Inline report note still present (unchanged report format)...
        assert "!! Truncated record" in out.getvalue()
        # ...and the same anomaly surfaced through the logger.
        assert any(
            r.levelno == logging.WARNING and "truncated record" in r.getMessage().lower()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    @pytest.mark.requirement("L2-CLI-014")
    def test_dump_report_is_pure_ascii_and_lf(
        self, tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """The dump report is a stdout *payload* -- piped, redirected, diffed
        against the Rust and C++ reports -- so it is pure ASCII with LF endings.

        This replaces a test that asserted the opposite. The annotation used to
        contain box-drawing, arrow and en-dash characters, which raised
        ``UnicodeEncodeError`` on a redirected Windows stdout (cp1252) and
        aborted the dump. The characters are gone instead, so the report
        matches the other two implementations byte for byte -- pinned by the
        ``dump-*`` conformance cases.

        Asserted on the BYTES that reach the file descriptor: a payload that
        decodes identically under cp1252 and UTF-8 is exactly one containing no
        byte above 0x7F, which is the property that makes it safe on any
        console.
        """
        from aero1553.cli import main
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "in.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV)

        capfdbinary.readouterr()
        rc = main(["dump", str(fpath)])
        payload = capfdbinary.readouterr().out

        assert rc == 0
        assert payload, "the dump produced no output"

        high = [b for b in payload if b > 0x7F]
        assert not high, f"dump emitted {len(high)} non-ASCII byte(s): {high[:8]}"
        assert b"\r" not in payload, "dump emitted CR; the report is LF on every platform"
        # The defining property: identical under a legacy codepage and UTF-8.
        assert payload.decode("cp1252") == payload.decode("utf-8")

    @pytest.mark.requirement("L2-CLI-014")
    def test_help_text_is_pure_ascii(self, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
        """Help text is *prose*, and L2-CLI-014 binds prose to the same ASCII
        rule as payload.

        It did not always. The rule originally exempted stderr prose on the
        reasoning that a payload is piped and diffed while prose is merely read
        by a human -- which inverts the risk, because a human reads it on
        whatever console they have. A stock Windows console runs at the OEM code
        page, not UTF-8, so the U+2014 em dash this banner opened with arrived as
        three unrelated CP437 glyphs and was reported as memory corruption.

        ``scripts/assert-ascii-output.py`` enforces the rule at the source across
        all three implementations; this is the runtime half, on the bytes that
        actually reach the file descriptor.
        """
        from aero1553.cli import main

        capfdbinary.readouterr()
        assert main(["--help"]) == 0
        payload = capfdbinary.readouterr().out
        assert payload, "--help produced no output"

        high = [b for b in payload if b > 0x7F]
        assert not high, f"--help emitted {len(high)} non-ASCII byte(s): {high[:8]}"

    @pytest.mark.requirement("L2-CLI-014")
    def test_diagnostics_are_pure_ascii(self, tmp_path: Path, multi_record_data: bytes) -> None:
        """The other kind of prose: a diagnostic an operator reads on stderr.

        The IRIG day-of-year advisory ended ``... -- see
        docs/VENDOR-CSV-DIFFS.md section 5``; both the dash and the section sign
        were non-ASCII, so the line ended in mojibake on a Windows console.
        Rust and C++ emit the identical wording.
        """
        import logging

        from aero1553.exceptions import MieRecordError
        from aero1553.reader import MieFileReader

        messages = [
            str(MieRecordError(0x40, "First record after header detection is truncated")),
        ]

        # And the live log path, which is where the advisory actually surfaced.
        fpath = tmp_path / "irig.mie"
        fpath.write_bytes(multi_record_data)
        records = logging.getLogger("aero1553")
        captured: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = _Capture()
        records.addHandler(handler)
        # The advisory is INFO (L2-LOG-001), so the logger's own level has to
        # be lowered or it never reaches the handler at all.
        previous_level = records.level
        records.setLevel(logging.INFO)
        try:
            list(MieFileReader(fpath))
        finally:
            records.removeHandler(handler)
            records.setLevel(previous_level)

        messages.extend(captured)
        assert captured, "the decode emitted no log messages to check"
        # Pin the advisory itself: it carried BOTH non-ASCII characters that
        # survived the payload-only rule, so it is the strongest single line.
        assert any("day-of-year" in m for m in captured), captured

        for message in messages:
            assert message.isascii(), f"diagnostic is not ASCII: {message!r}"

    @pytest.mark.requirement("L2-CLI-009")
    def test_dump_annotates_errored_record_with_code_and_format(self, tmp_path: Path) -> None:
        """The record-aware dump surfaces the bit-14 error flag, the classified
        message format, and the DDC Error Word + its description for an errored
        record. Mirrors the Rust ``record_dump_annotates_errored_record``."""
        from aero1553.dump import hex_dump_records

        # Errored RT->BC (Transmit): Type 0x4604 (bit 14 set), 6 words / 12
        # bytes, Error Word 0x0120 (No Status Response or Too Few Data Words).
        fpath = tmp_path / "errored.mie"
        fpath.write_bytes(bytes.fromhex("04460F18F2DA265D3E7C2001"))

        out = io.StringIO()
        hex_dump_records(fpath, max_records=1, stream=out)
        text = out.getvalue()

        assert "error flag (bit 14): SET" in text
        assert "Format: TRANSMIT" in text
        assert "Error:  0x0120" in text
        assert "No Status Response or Too Few Data Words" in text

    @pytest.mark.requirement("L2-CLI-009")
    def test_ddc_error_descriptions_are_the_dumps(self, tmp_path: Path) -> None:
        """``aero1553.models.DDC_ERROR_DESCRIPTIONS`` is published for callers;
        the dump that prints those descriptions is Rust's. Each entry is pinned
        against what the dump shows for a record carrying that Error Word."""
        from aero1553.dump import hex_dump_records
        from aero1553.models import DDC_ERROR_DESCRIPTIONS

        for code, description in DDC_ERROR_DESCRIPTIONS.items():
            fpath = tmp_path / f"err{code:04X}.mie"
            fpath.write_bytes(bytes.fromhex("04460F18F2DA265D3E7C") + code.to_bytes(2, "little"))
            out = io.StringIO()
            hex_dump_records(fpath, max_records=1, stream=out)
            assert f"Error:  0x{code:04X}  ->  {description}\n" in out.getvalue()

    @pytest.mark.requirement("L2-CLI-009")
    def test_dump_propagates_the_streams_own_exception(self, tmp_path: Path) -> None:
        """Whatever the output stream raises -- a broken pipe included --
        reaches the caller as that same exception object, as it did when the
        dump was a sequence of ``print`` calls. The dump is Rust now; this pins
        that the stream adapter hands the exception back rather than wrapping
        or swallowing it."""
        from aero1553.dump import hex_dump_raw, hex_dump_records

        fpath = tmp_path / "errored.mie"
        fpath.write_bytes(bytes.fromhex("04460F18F2DA265D3E7C2001"))

        for raised in (BrokenPipeError(32, "closed"), RuntimeError("stream gone")):

            class Failing(io.StringIO):
                def write(self, s: str) -> int:
                    raise raised  # noqa: B023

            for dump in (
                lambda out: hex_dump_raw(fpath, stream=out),
                lambda out: hex_dump_records(fpath, stream=out),
            ):
                stream = Failing()
                with pytest.raises(type(raised)) as caught:
                    dump(stream)
                assert caught.value is raised

    @pytest.mark.requirement("L2-CLI-009")
    def test_dump_refuses_a_negative_count(self, tmp_path: Path) -> None:
        """A negative offset, length or record count is a ``ValueError`` naming
        the argument, not an arithmetic surprise inside the scan."""
        from aero1553.dump import hex_dump_raw, hex_dump_records

        fpath = tmp_path / "errored.mie"
        fpath.write_bytes(bytes.fromhex("04460F18F2DA265D3E7C2001"))
        sink = io.StringIO()
        with pytest.raises(ValueError, match="start_offset"):
            hex_dump_raw(fpath, start_offset=-1, stream=sink)
        with pytest.raises(ValueError, match="length"):
            hex_dump_raw(fpath, length=-1, stream=sink)
        with pytest.raises(ValueError, match="max_records"):
            hex_dump_records(fpath, max_records=-1, stream=sink)
        assert sink.getvalue() == "", "a refused call writes nothing"

        # A count past anything addressable means "to the end", not an error.
        out = io.StringIO()
        hex_dump_raw(fpath, length=1 << 80, stream=out)
        assert "Range: 0x00000000-0x0000000C" in out.getvalue()


class TestSeparateModeCommitOrder:
    """L2-WRT-019: separate mode commits the main CSV before the errors CSV.

    The two files are committed sequentially (each atomic on its own, but
    there is no cross-file atomic rename), so on a mid-commit failure the
    residue must be the primary main CSV, never an orphan errors file.
    """

    @pytest.mark.requirement("L2-WRT-019")
    def test_errors_commit_failure_leaves_main_not_orphan_errors(self, tmp_path: Path) -> None:
        """If the errors-file commit fails, the already-committed main CSV
        remains and no orphan errors file (or temp) is left behind."""
        from aero1553.exceptions import MieWriterError
        from aero1553.writer import write_csv_split
        from tests.conftest import RECORD_RT15_SA11_RCV

        fpath = tmp_path / "in.mie"
        fpath.write_bytes(RECORD_RT15_SA11_RCV)
        normal = next(iter(MieFileReader(fpath)))
        errored = replace(normal, type_word=replace(normal.type_word, error=True))
        assert errored.error_label == "ERROR"

        dest = tmp_path / "out.csv"
        err_dest = tmp_path / "out_errors.csv"
        # Force the SECOND (errors) commit's os.replace to fail by making
        # the errors destination a directory.
        err_dest.mkdir()

        with pytest.raises(MieWriterError):
            write_csv_split([normal, errored], dest)

        # Main was committed first → present and complete.
        assert dest.read_text().startswith("TIME_STAMP,RT,MSG,")
        # No orphan errors *file*: the destination is still the directory.
        assert err_dest.is_dir()
        # No leftover temp files anywhere in the directory.
        leftover = list(tmp_path.glob("*.aero1553.tmp.*"))
        assert leftover == [], f"temp file leaked after failed commit: {leftover}"


class TestBrokenPipeSubprocess:
    """Real-OS-pipe coverage for L2-WRT-018.

    The in-process tests simulate a closed consumer by raising from a fake
    stream. That cannot catch a platform *mismatch* in how the pipe-closed
    condition is reported — and Windows reports it as a bare ``OSError``
    (``EINVAL``) rather than ``BrokenPipeError``, so both ``decode`` (whose
    guard caught only ``BrokenPipeError``) and ``dump`` (which had no guard at
    all) exited 1 with a traceback there while the Rust CLI exited 0. These
    tests spawn the real CLI against a real pipe and close the read end, so the
    platform decides the exception type.
    """

    @staticmethod
    def _big_recording(tmp_path: Path) -> Path:
        """A recording large enough that the CLI is still writing when the
        consumer closes the pipe (comfortably past the OS pipe buffer).

        Built from three *structurally different* records rather than one
        repeated record: the L2-SYN-018 homogeneous-payload defense compares
        candidate records while deliberately ignoring the timestamp bytes, so a
        run of one record varying only in timestamp is still "homogeneous" and
        the decode would be rejected (exit 2) before any pipe could break.
        """
        from tests.conftest import (
            RECORD_RT15_SA11_RCV,
            RECORD_RT15_SA22_RCV,
            RECORD_RT15_SA22_XMT,
        )

        triple = RECORD_RT15_SA11_RCV + RECORD_RT15_SA22_RCV + RECORD_RT15_SA22_XMT
        mie = tmp_path / "big.mie"
        mie.write_bytes(triple * 20_000)
        return mie

    @staticmethod
    def _run_and_close_pipe(argv: list[str]) -> tuple[int, str]:
        """Run the CLI, read a little stdout, close the pipe, return (rc, stderr)."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "aero1553", *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stderr is not None
            proc.stdout.read(64)
            proc.stdout.close()  # the consumer goes away mid-write
            stderr = proc.stderr.read()
            proc.stderr.close()
            return proc.wait(timeout=120), stderr
        finally:
            if proc.poll() is None:  # pragma: no cover - only on a hang
                proc.kill()
                proc.wait(timeout=30)

    @pytest.mark.requirement("L2-WRT-018")
    def test_decode_to_closed_pipe_exits_zero(self, tmp_path: Path) -> None:
        mie = self._big_recording(tmp_path)
        rc, stderr = self._run_and_close_pipe(["decode", str(mie)])
        assert rc == 0, f"expected clean exit on broken pipe, got {rc}; stderr:\n{stderr}"
        assert "Traceback" not in stderr
        assert "Exception ignored" not in stderr

    @pytest.mark.requirement("L2-WRT-018")
    def test_dump_to_closed_pipe_exits_zero(self, tmp_path: Path) -> None:
        mie = self._big_recording(tmp_path)
        rc, stderr = self._run_and_close_pipe(["dump", str(mie)])
        assert rc == 0, f"expected clean exit on broken pipe, got {rc}; stderr:\n{stderr}"
        assert "Traceback" not in stderr
        assert "Exception ignored" not in stderr


class _FailingStream:
    """A text stream whose every ``write`` raises ``exc``."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def write(self, _text: str) -> int:
        raise self.exc


class TestStreamWriteFailures:
    """How ``write_csv`` classifies a failing text stream (L2-WRT-018).

    The writer is the compiled one; the rule for which Python exceptions mean
    "the consumer closed the pipe" is ``aero1553.writer.is_broken_pipe``, which
    it asks.
    """

    @pytest.mark.requirement("L2-WRT-018")
    def test_broken_pipe_on_a_stream_is_a_clean_stop(self, tmp_mie_file: Path) -> None:
        from aero1553.writer import write_csv

        outcome = write_csv(
            MieFileReader(tmp_mie_file), _FailingStream(BrokenPipeError("consumer closed"))
        )
        assert outcome.partial is None

    @pytest.mark.requirement("L2-WRT-018")
    def test_a_real_write_failure_is_a_writer_error(self, tmp_mie_file: Path) -> None:
        from aero1553.exceptions import MieWriterError
        from aero1553.writer import write_csv

        reader = MieFileReader(tmp_mie_file)
        stream = _FailingStream(OSError(errno.ENOSPC, "No space left on device"))
        with pytest.raises(MieWriterError):
            write_csv(reader, stream)

    def test_any_other_exception_from_the_stream_propagates_unchanged(
        self, tmp_mie_file: Path
    ) -> None:
        from aero1553.writer import write_csv

        failure = TypeError("this stream takes bytes")
        reader = MieFileReader(tmp_mie_file)
        stream = _FailingStream(failure)
        with pytest.raises(TypeError) as caught:
            write_csv(reader, stream)
        assert caught.value is failure

    @pytest.mark.requirement("L2-WRT-018")
    @pytest.mark.parametrize(
        ("platform", "exc", "expected"),
        [
            ("linux", BrokenPipeError(), True),
            ("win32", BrokenPipeError(), True),
            # Windows reports a closed pipe as a bare OSError with EINVAL/EPIPE...
            ("win32", OSError(errno.EINVAL, "Invalid argument"), True),
            ("win32", OSError(errno.EPIPE, "Broken pipe"), True),
            # ...which on POSIX is a genuine failure and must stay one.
            ("linux", OSError(errno.EINVAL, "Invalid argument"), False),
            ("win32", OSError(errno.ENOSPC, "No space"), False),
            ("linux", ValueError("not an I/O error"), False),
        ],
    )
    def test_is_broken_pipe(
        self, monkeypatch: pytest.MonkeyPatch, platform: str, exc: BaseException, expected: bool
    ) -> None:
        from aero1553 import writer

        monkeypatch.setattr(writer.sys, "platform", platform)
        assert writer.is_broken_pipe(exc) is expected
