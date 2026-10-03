"""Unit tests for aero1553.models module."""

from __future__ import annotations

import pytest

from aero1553.models import (
    VALID_MESSAGE_TYPES,
    Bus,
    CommandWord,
    Direction,
    IrigTimestamp,
    MessageFormat,
    MessageType,
    MieMessage,
    TypeWord,
)


class TestIrigTimestamp:
    """Tests for IrigTimestamp data structure."""

    @pytest.mark.requirement("L2-DEC-002")
    def test_frozen(self) -> None:
        ts = IrigTimestamp(192, 15, 54, 50, 456225, False)
        with pytest.raises(AttributeError):
            ts.hour = 10  # type: ignore[misc]

    @pytest.mark.requirement("L2-WRT-011")
    def test_format(self) -> None:
        ts = IrigTimestamp(192, 15, 54, 50, 456225, False)
        assert ts.format() == "192:15:54:50.456225"

    @pytest.mark.requirement("L2-WRT-011")
    def test_format_zero_padded(self) -> None:
        ts = IrigTimestamp(1, 0, 0, 0, 0, False)
        assert ts.format() == "1:00:00:00.000000"

    @pytest.mark.requirement("L2-DEC-002")
    def test_total_microseconds(self) -> None:
        ts = IrigTimestamp(0, 0, 0, 1, 500000, False)
        assert ts.to_total_microseconds() == 1_500_000

    @pytest.mark.requirement("L2-DEC-014")
    def test_format_truncates_out_of_range_microseconds(self) -> None:
        """L2-DEC-014: formatter SHALL emit exactly six microsecond
        digits even if the caller bypasses validation and constructs
        an IrigTimestamp with microsecond >= 1_000_000."""
        ts = IrigTimestamp(1, 0, 0, 0, 1_234_567, False)
        s = ts.format()
        assert s == "1:00:00:00.234567"
        # The microsecond suffix must be exactly six characters.
        assert len(s.rsplit(".", 1)[1]) == 6


class TestMessageType:
    """Tests for MessageType enum."""

    @pytest.mark.requirement("L2-MSG-001")
    def test_known_values(self) -> None:
        assert MessageType.MODE_COMMAND == 0x01
        assert MessageType.BC_TO_RT == 0x02
        assert MessageType.RT_TO_BC == 0x04
        assert MessageType.RT_TO_RT == 0x08
        assert MessageType.BROADCAST_BC_TO_RT == 0x10
        assert MessageType.BROADCAST_RT_TO_RT == 0x18
        assert MessageType.SPURIOUS_DATA == 0x20

    @pytest.mark.requirement("L2-MSG-001")
    def test_valid_message_types_set(self) -> None:
        assert len(VALID_MESSAGE_TYPES) == 7
        assert 0x02 in VALID_MESSAGE_TYPES
        assert 0x03 not in VALID_MESSAGE_TYPES


class TestCommandWordProperties:
    """Tests for CommandWord convenience properties."""

    @pytest.mark.requirement("L2-DEC-004")
    def test_is_broadcast(self) -> None:
        cmd = CommandWord(31, Direction.RECEIVE, 0, 1, 0xF820)
        assert cmd.is_broadcast is True

    @pytest.mark.requirement("L2-DEC-004")
    def test_not_broadcast(self) -> None:
        cmd = CommandWord(15, Direction.RECEIVE, 11, 30, 0x797E)
        assert cmd.is_broadcast is False

    @pytest.mark.requirement("L2-DEC-004")
    def test_is_mode_code_sa0(self) -> None:
        cmd = CommandWord(15, Direction.TRANSMIT, 0, 1, 0x7C01)
        assert cmd.is_mode_code is True

    @pytest.mark.requirement("L2-DEC-004")
    def test_is_mode_code_sa31(self) -> None:
        cmd = CommandWord(15, Direction.TRANSMIT, 31, 1, 0x7FE1)
        assert cmd.is_mode_code is True

    @pytest.mark.requirement("L2-DEC-004")
    def test_not_mode_code(self) -> None:
        cmd = CommandWord(15, Direction.RECEIVE, 11, 30, 0x797E)
        assert cmd.is_mode_code is False


class TestMieMessage:
    """Tests for MieMessage properties."""

    @pytest.fixture
    def sample_msg(self) -> MieMessage:
        return MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x02, Bus.A, 36, False, 0x2402),
            message_format=MessageFormat.RECEIVE,
            command_word=CommandWord(15, Direction.RECEIVE, 11, 30, 0x797E),
            command_word_2=None,
            status_word=0x7800,
            status_word_2=None,
            data_words=(0x0400,) + (0,) * 29,
            error_word=None,
            delta=0.0,
            file_offset=0,
        )

    @pytest.mark.requirement("L2-MSG-003")
    def test_rt_shortcut(self, sample_msg: MieMessage) -> None:
        assert sample_msg.rt == 15

    @pytest.mark.requirement("L2-MSG-003")
    def test_subaddress_shortcut(self, sample_msg: MieMessage) -> None:
        assert sample_msg.subaddress == 11

    @pytest.mark.requirement("L2-MSG-002")
    def test_bus_shortcut(self, sample_msg: MieMessage) -> None:
        assert sample_msg.bus == Bus.A

    @pytest.mark.requirement("L2-MSG-003")
    def test_msg_label_receive(self, sample_msg: MieMessage) -> None:
        assert sample_msg.msg_label == "11R"

    @pytest.mark.requirement("L2-MSG-003")
    def test_msg_label_transmit(self) -> None:
        msg = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 457187, False),
            type_word=TypeWord(0x04, Bus.A, 36, False, 0x2404),
            message_format=MessageFormat.TRANSMIT,
            command_word=CommandWord(15, Direction.TRANSMIT, 22, 30, 0x7EDE),
            command_word_2=None,
            status_word=0x7800,
            status_word_2=None,
            data_words=(0x1020,) + (0,) * 29,
            error_word=None,
            delta=0.0,
            file_offset=72,
        )
        assert msg.msg_label == "22T"

    @pytest.mark.requirement("L2-RDR-009")
    def test_delta_key(self, sample_msg: MieMessage) -> None:
        assert sample_msg.delta_key == "15:11R"

    @pytest.mark.requirement("L2-DEC-010")
    def test_frozen(self, sample_msg: MieMessage) -> None:
        with pytest.raises(AttributeError):
            sample_msg.delta = 1.0  # type: ignore[misc]

    @pytest.mark.requirement("L2-MSG-001")
    def test_message_format_field(self, sample_msg: MieMessage) -> None:
        assert sample_msg.message_format == MessageFormat.RECEIVE

    @pytest.mark.requirement("L2-DEC-010")
    def test_optional_fields_none(self, sample_msg: MieMessage) -> None:
        assert sample_msg.command_word_2 is None
        assert sample_msg.status_word_2 is None

    @pytest.mark.requirement("L1-ROB-001")
    def test_data_words_capped_at_max(self) -> None:
        # A crafted record with more than 32 data words is truncated to
        # MAX_DATA_WORDS, mirroring the Rust DataWords [u16; 32] inline buffer,
        # so both implementations carry the same payload for an oversized record.
        from aero1553.models import MAX_DATA_WORDS

        msg = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x20, Bus.A, 40, False, 0x2820),
            message_format=MessageFormat.SPURIOUS_DATA,
            command_word=None,
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=tuple(range(40)),
            error_word=0x2001,
            delta=None,
            file_offset=0,
        )
        assert len(msg.data_words) == MAX_DATA_WORDS
        assert msg.data_words == tuple(range(MAX_DATA_WORDS))


class TestErrorProperties:
    """Tests for error-related MieMessage properties."""

    @pytest.mark.requirement("L2-ERR-010")
    def test_normal_message_error_label(self) -> None:
        msg = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x02, Bus.A, 36, False, 0x2402),
            message_format=MessageFormat.RECEIVE,
            command_word=CommandWord(15, Direction.RECEIVE, 11, 30, 0x797E),
            command_word_2=None,
            status_word=0x7800,
            status_word_2=None,
            data_words=(0x0400,),
            error_word=None,
            delta=0.0,
            file_offset=0,
        )
        assert msg.error_label == ""
        assert msg.is_error is False
        assert msg.is_spurious is False

    @pytest.mark.requirement("L2-ERR-001")
    def test_errored_message_label(self) -> None:
        msg = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x02, Bus.A, 10, True, 0x6402),  # bit 14 set
            message_format=MessageFormat.RECEIVE,
            command_word=CommandWord(15, Direction.RECEIVE, 11, 30, 0x797E),
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=(0x0400,),
            error_word=0x011E,
            delta=0.0,
            file_offset=0,
        )
        assert msg.error_label == "ERROR"
        assert msg.is_error is True
        assert msg.is_spurious is False

    @pytest.mark.requirement("L2-ERR-006")
    def test_spurious_message_label(self) -> None:
        msg = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x20, Bus.A, 8, False, 0x0820),
            message_format=MessageFormat.SPURIOUS_DATA,
            command_word=None,
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=(0x1234, 0x5678),
            error_word=0x2000,
            delta=0.0,
            file_offset=0,
        )
        assert msg.error_label == "SPURIOUS"
        assert msg.is_error is False
        assert msg.is_spurious is True
        assert msg.rt is None
        assert msg.subaddress is None
        assert msg.msg_label == ""
        assert msg.delta_key == ""

    @pytest.mark.requirement("L2-ERR-001", "L2-ERR-006")
    def test_spurious_with_error_bit_is_labelled_spurious(self) -> None:
        # The message type decides the label: bit 14 on a SPURIOUS_DATA record
        # does not make it an ERROR row (docs/DATAFLOW.md row S7). The raw bit
        # stays visible through is_error.
        msg = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x20, Bus.A, 6, True, 0x4620),  # bit 14 set
            message_format=MessageFormat.SPURIOUS_DATA,
            command_word=None,
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=(0xAAAA, 0xBBBB),
            error_word=0x2000,
            delta=None,
            file_offset=0,
        )
        assert msg.error_label == "SPURIOUS"
        assert msg.is_error is True
        assert msg.is_spurious is True
