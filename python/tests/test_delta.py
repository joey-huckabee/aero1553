"""Per-RT/MSG DELTA tracking (L2-RDR-009/010/017/018/019, L3-RDR-001).

Mirrors the Rust unit tests in ``rust/src/delta.rs`` case for case, so a
divergence between the two implementations shows up as a failing test on one
side rather than as a conformance-oracle mismatch later.

The last test in this file is the one that could not exist before the
extraction: while the packed tracking key lived in ``reader.py`` and the display
key lived in ``models.py``, there was nowhere that both were in scope to be
compared.
"""

from __future__ import annotations

import pytest

from aero1553.delta import DeltaKind, DeltaTracker, delta_key
from aero1553.models import (
    Bus,
    CommandWord,
    Direction,
    IrigTimestamp,
    MessageFormat,
    MieMessage,
    StandardTimestamp,
    Timestamp,
    TypeWord,
)


def cmd(rt: int, subaddress: int, *, transmit: bool) -> CommandWord:
    """A Command Word carrying just the three fields the DELTA key uses."""
    return CommandWord(
        rt=rt,
        direction=Direction.TRANSMIT if transmit else Direction.RECEIVE,
        subaddress=subaddress,
        data_word_count=2,
        raw=0,
    )


def at(micros: int) -> Timestamp:
    """An IRIG timestamp ``micros`` microseconds into day 10."""
    return IrigTimestamp(
        day=10,
        hour=0,
        minute=0,
        second=micros // 1_000_000,
        microsecond=micros % 1_000_000,
        freerun=False,
    )


def us(micros: int) -> int:
    """The absolute microseconds ``at(micros)`` decodes to.

    IRIG timestamps are absolute -- day 10 alone contributes 864 000 000 000
    microseconds -- so a test that expects to see its own argument back is
    asserting against a number the tracker never had. Derived rather than
    written out, so the two cannot disagree.
    """
    value = at(micros).to_microseconds(None)
    assert value is not None, "IRIG is always calibrated"
    return value


class TestDeltaTracker:
    """The five outcomes, and the state each leaves behind."""

    @pytest.mark.requirement("L2-RDR-010")
    def test_first_sighting_of_a_key_is_zero(self) -> None:
        tracker = DeltaTracker()
        outcome = tracker.observe(cmd(3, 5, transmit=False), at(0))
        assert outcome.kind is DeltaKind.FIRST
        assert outcome.value == 0.0

    @pytest.mark.requirement("L2-RDR-009")
    def test_a_later_record_reports_the_gap_in_seconds(self) -> None:
        tracker = DeltaTracker()
        tracker.observe(cmd(3, 5, transmit=False), at(0))
        outcome = tracker.observe(cmd(3, 5, transmit=False), at(250_000))
        assert outcome.kind is DeltaKind.ELAPSED
        assert outcome.value == pytest.approx(0.25)

    @pytest.mark.requirement("L2-RDR-009")
    def test_keys_are_tracked_independently(self) -> None:
        # Interleaved traffic from two terminals. A single cursor would make
        # every gap the inter-record spacing rather than the per-key period --
        # a plausible-looking wrong answer.
        tracker = DeltaTracker()
        assert tracker.observe(cmd(3, 5, transmit=False), at(0)).value == 0.0
        assert tracker.observe(cmd(9, 1, transmit=False), at(100_000)).value == 0.0
        assert tracker.observe(cmd(3, 5, transmit=False), at(200_000)).value == pytest.approx(0.2)
        assert tracker.observe(cmd(9, 1, transmit=False), at(300_000)).value == pytest.approx(0.2)

    @pytest.mark.requirement("L2-RDR-009")
    def test_direction_is_part_of_the_key(self) -> None:
        # Same RT, same subaddress, opposite direction: two different messages
        # on the bus, so two independent periods.
        tracker = DeltaTracker()
        assert tracker.observe(cmd(3, 5, transmit=False), at(0)).value == 0.0
        assert tracker.observe(cmd(3, 5, transmit=True), at(100_000)).value == 0.0
        assert tracker.observe(cmd(3, 5, transmit=False), at(400_000)).value == pytest.approx(0.4)

    @pytest.mark.requirement("L2-RDR-017")
    def test_a_backward_step_reports_no_gap_and_flags_only_the_first(self) -> None:
        tracker = DeltaTracker()
        tracker.observe(cmd(3, 5, transmit=False), at(500_000))

        first = tracker.observe(cmd(3, 5, transmit=False), at(100_000))
        assert first.kind is DeltaKind.BACKWARD
        assert first.prev_us == us(500_000)
        assert first.curr_us == us(100_000)
        assert first.first_for_key is True
        assert first.value is None

        # Reported against the PREVIOUS record, not the high-water mark.
        second = tracker.observe(cmd(3, 5, transmit=False), at(50_000))
        assert second.kind is DeltaKind.BACKWARD
        assert second.prev_us == us(100_000)
        assert second.first_for_key is False

    @pytest.mark.requirement("L2-RDR-017")
    def test_the_cursor_advances_across_a_backward_step(self) -> None:
        # The recovery is measured from the last record SEEN, not from the
        # high-water mark -- otherwise the gap reported would be one no pair of
        # records in the file actually has.
        tracker = DeltaTracker()
        tracker.observe(cmd(3, 5, transmit=False), at(500_000))
        tracker.observe(cmd(3, 5, transmit=False), at(100_000))
        outcome = tracker.observe(cmd(3, 5, transmit=False), at(600_000))
        assert outcome.kind is DeltaKind.ELAPSED
        assert outcome.value == pytest.approx(0.5)

    @pytest.mark.requirement("L2-RDR-017")
    def test_each_key_gets_its_own_first_backward_flag(self) -> None:
        tracker = DeltaTracker()
        tracker.observe(cmd(3, 5, transmit=False), at(500_000))
        tracker.observe(cmd(9, 1, transmit=False), at(500_000))
        for rt, subaddress in ((3, 5), (9, 1)):
            outcome = tracker.observe(cmd(rt, subaddress, transmit=False), at(100_000))
            assert outcome.kind is DeltaKind.BACKWARD
            assert outcome.first_for_key is True

    @pytest.mark.requirement("L2-RDR-018")
    def test_a_record_with_no_command_word_is_never_tracked(self) -> None:
        tracker = DeltaTracker()
        assert tracker.observe(None, at(0)).kind is DeltaKind.NO_KEY
        assert tracker.observe(None, at(0)).value is None
        # And it left no cursor behind for a real key to trip over.
        assert tracker.observe(cmd(3, 5, transmit=False), at(0)).kind is DeltaKind.FIRST

    @pytest.mark.requirement("L2-RDR-019")
    def test_an_uncalibrated_standard_counter_is_not_tracked(self) -> None:
        tracker = DeltaTracker()
        ticks = StandardTimestamp(raw_value=1_000, upper_word=0, lower_word=1_000)
        outcome = tracker.observe(cmd(3, 5, transmit=False), ticks)
        assert outcome.kind is DeltaKind.UNCALIBRATED
        assert outcome.value is None

    @pytest.mark.requirement("L2-DEC-017", "L2-RDR-019")
    def test_a_calibrated_standard_counter_is_tracked_like_irig(self) -> None:
        tracker = DeltaTracker(1_000_000.0)

        def ticks(value: int) -> StandardTimestamp:
            return StandardTimestamp(
                raw_value=value,
                upper_word=(value >> 16) & 0xFFFF,
                lower_word=value & 0xFFFF,
            )

        assert tracker.observe(cmd(3, 5, transmit=False), ticks(0)).value == 0.0
        assert tracker.observe(cmd(3, 5, transmit=False), ticks(1_000_000)).value == pytest.approx(
            1.0
        )


class TestKeyDefinition:
    """The packed key and the published display key must mean the same thing."""

    @pytest.mark.requirement("L2-RDR-009", "L2-MSG-003", "L3-RDR-001")
    def test_packed_key_and_display_key_agree(self) -> None:
        """Both keys must partition ``(rt, subaddress, direction)`` identically.

        If one ever collapses two distinct messages that the other keeps apart,
        DELTA means different things on the single-file and merge paths. This
        could not be written while the two representations lived in different
        modules; it is the check whose absence motivated the extraction.
        """
        packed_to_display: dict[int, str] = {}
        display_to_packed: dict[str, int] = {}

        for rt in range(32):
            for subaddress in range(32):
                for transmit in (False, True):
                    command = cmd(rt, subaddress, transmit=transmit)
                    packed = delta_key(rt, subaddress, transmit)
                    # Only the Command Word matters to delta_key; the rest is
                    # inert filler for a record that never came off a bus.
                    message = MieMessage(
                        timestamp=at(0),
                        type_word=TypeWord(
                            message_type=0x02, bus=Bus.A, word_count=8, error=False, raw=0
                        ),
                        message_format=MessageFormat.RECEIVE,
                        command_word=command,
                        command_word_2=None,
                        status_word=None,
                        status_word_2=None,
                        data_words=(),
                        error_word=None,
                        delta=None,
                        file_offset=0,
                    )
                    display = message.delta_key

                    previous_display = packed_to_display.setdefault(packed, display)
                    assert previous_display == display, f"packed key {packed:#010X} reused"
                    previous_packed = display_to_packed.setdefault(display, packed)
                    assert previous_packed == packed, f"display key {display} reused"

        assert len(packed_to_display) == 32 * 32 * 2
        assert len(display_to_packed) == 32 * 32 * 2
