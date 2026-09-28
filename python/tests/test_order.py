"""Canonical CSV row ordering (L1-OUT-003, L2-WRT-021, L2-WRT-022).

Mirrors the Rust unit tests in ``rust/src/order.rs`` case for case, so a
divergence between the two implementations shows up as a failing test on one
side rather than as a conformance-oracle mismatch later.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from aero1553 import cli
from aero1553.cli import EXIT_USAGE
from aero1553.exceptions import Aero1553Error, MieUnrecoverableSyncLossError
from aero1553.models import (
    Bus,
    CommandWord,
    Direction,
    IrigTimestamp,
    MessageFormat,
    MieMessage,
    StandardTimestamp,
    TypeWord,
)
from aero1553.order import (
    DEFAULT_MAX_SORT_GROUP,
    MAX_SORT_GROUP_MAX,
    MAX_SORT_GROUP_MIN,
    _group_value,
    order_rows,
)

# ── fixtures ────────────────────────────────────────────────────────────────


def rec(
    us: int,
    rt: int,
    sa: int,
    direction: Direction = Direction.RECEIVE,
    *,
    offset: int = 0,
    ts: IrigTimestamp | StandardTimestamp | None = None,
) -> MieMessage:
    """An IRIG record at `us` microseconds with the given RT / SA / direction.

    `ts` replaces the timestamp outright, for the grouping tests that need a
    Standard counter or a specific IRIG field breakdown. `MieMessage` is a frozen
    slots dataclass, so the timestamp is passed at construction rather than
    patched afterward.
    """
    return MieMessage(
        timestamp=ts if ts is not None else IrigTimestamp(192, 15, 54, 50, us, False),
        type_word=TypeWord(0x02, Bus.A, 36, False, 0x2402),
        message_format=MessageFormat.RECEIVE,
        command_word=CommandWord(rt, direction, sa, 30, 0x797E),
        command_word_2=None,
        status_word=0x7800,
        status_word_2=None,
        data_words=(0,) * 30,
        error_word=None,
        delta=0.0,
        file_offset=offset,
    )


def spurious(us: int) -> MieMessage:
    """A SPURIOUS_DATA record (no Command Word, so no RT/MSG to sort on)."""
    return MieMessage(
        timestamp=IrigTimestamp(192, 15, 54, 50, us, False),
        type_word=TypeWord(0x20, Bus.A, 5, False, 0x0520),
        message_format=MessageFormat.SPURIOUS_DATA,
        command_word=None,
        command_word_2=None,
        status_word=None,
        status_word_2=None,
        data_words=(0,),
        error_word=0x2000,
        delta=None,
        file_offset=0,
    )


def errored(us: int, rt: int, sa: int) -> MieMessage:
    """An errored record (Type Word bit 14), whose spurious continuation must
    stay adjacent to it."""
    return MieMessage(
        timestamp=IrigTimestamp(192, 15, 54, 50, us, False),
        type_word=TypeWord(0x02, Bus.A, 8, True, 0x4802),
        message_format=MessageFormat.RECEIVE,
        command_word=CommandWord(rt, Direction.RECEIVE, sa, 30, 0x797E),
        command_word_2=None,
        status_word=None,
        status_word_2=None,
        data_words=(0, 0),
        error_word=0x011E,
        delta=0.0,
        file_offset=0,
    )


def keys(msgs: list[MieMessage]) -> list[tuple[int, int, int] | None]:
    """`(rt, sa, direction)` per message, `None` for a pinned spurious record."""
    return [
        None
        if m.command_word is None
        else (m.command_word.rt, m.command_word.subaddress, int(m.command_word.direction))
        for m in msgs
    ]


def ordered(msgs: list[MieMessage], cap: int = DEFAULT_MAX_SORT_GROUP) -> list[MieMessage]:
    return list(order_rows(iter(msgs), cap))


# ── key ordering ────────────────────────────────────────────────────────────


class TestKeyOrdering:
    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_sorts_tied_rows_by_rt(self) -> None:
        got = ordered([rec(10, 21, 3), rec(10, 3, 3), rec(10, 15, 3)])
        assert keys(got) == [(3, 3, 0), (15, 3, 0), (21, 3, 0)]

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_sorts_by_subaddress_within_rt(self) -> None:
        got = ordered([rec(10, 5, 11), rec(10, 5, 2), rec(10, 5, 31)])
        # Numeric, not lexicographic: as strings "11" < "2".
        assert keys(got) == [(5, 2, 0), (5, 11, 0), (5, 31, 0)]

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_receive_before_transmit_at_equal_subaddress(self) -> None:
        got = ordered([rec(10, 5, 7, Direction.TRANSMIT), rec(10, 5, 7, Direction.RECEIVE)])
        assert keys(got) == [(5, 7, 0), (5, 7, 1)]

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_full_key_ordering(self) -> None:
        """All three key levels plus R-before-T from a deliberately wrong order."""
        got = ordered(
            [
                rec(10, 21, 3),
                rec(10, 3, 11, Direction.TRANSMIT),
                rec(10, 3, 11, Direction.RECEIVE),
                rec(10, 3, 2, Direction.TRANSMIT),
            ]
        )
        assert keys(got) == [(3, 2, 1), (3, 11, 0), (3, 11, 1), (21, 3, 0)]

    @pytest.mark.requirement("L1-OUT-003")
    def test_equal_keys_keep_arrival_order(self) -> None:
        got = ordered([rec(10, 5, 7, offset=0x100), rec(10, 5, 7, offset=0x200)])
        assert [m.file_offset for m in got] == [0x100, 0x200]

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_never_reorders_across_differing_timestamps(self) -> None:
        got = ordered([rec(10, 21, 1), rec(20, 15, 1), rec(30, 3, 1)])
        assert [m.rt for m in got] == [21, 15, 3]

    @pytest.mark.requirement("L1-OUT-003", "L2-MRG-006")
    def test_non_monotonic_run_is_left_in_place(self) -> None:
        """A lenient non-monotonic stream is not re-sorted: only the leading
        consecutive run is permuted, and the trailing repeat of an earlier
        timestamp stays where it arrived."""
        got = ordered([rec(10, 9, 1), rec(10, 4, 1), rec(20, 7, 1), rec(10, 1, 1)])
        assert [m.rt for m in got] == [4, 9, 7, 1]


# ── pinning ─────────────────────────────────────────────────────────────────


class TestPinning:
    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021", "L2-ERR-005")
    def test_spurious_stays_pinned_to_its_predecessor(self) -> None:
        """The clean RT 4 row sorts ahead of the errored RT 20 row, but the
        spurious continuation must still follow the record it continues."""
        got = ordered([errored(10, 20, 5), spurious(10), rec(10, 4, 5)])
        assert keys(got) == [(4, 5, 0), (20, 5, 0), None]

    @pytest.mark.requirement("L2-WRT-021")
    def test_leading_spurious_keeps_front_of_run(self) -> None:
        got = ordered([spurious(10), rec(10, 9, 1), rec(10, 2, 1)])
        assert keys(got) == [None, (2, 1, 0), (9, 1, 0)]

    @pytest.mark.requirement("L2-WRT-021")
    def test_multiple_pins_travel_with_their_anchors(self) -> None:
        got = ordered([rec(10, 9, 1), spurious(10), rec(10, 2, 1), spurious(10)])
        assert keys(got) == [(2, 1, 0), None, (9, 1, 0), None]

    @pytest.mark.requirement("L2-WRT-021")
    def test_all_pinned_run_is_untouched(self) -> None:
        got = ordered([spurious(10), spurious(10)])
        assert keys(got) == [None, None]


# ── the L2-WRT-022 cap ──────────────────────────────────────────────────────


class TestCap:
    @pytest.mark.requirement("L2-WRT-022")
    def test_cap_emits_run_in_arrival_order(self) -> None:
        got = ordered([rec(10, 9, 1), rec(10, 8, 1), rec(10, 7, 1), rec(10, 6, 1)], cap=2)
        assert [m.rt for m in got] == [9, 8, 7, 6]

    @pytest.mark.requirement("L2-WRT-022")
    def test_cap_keeps_every_row(self) -> None:
        got = ordered([rec(10, r, 1) for r in range(8)], cap=3)
        assert len(got) == 8, "no row may be dropped at the cap"

    @pytest.mark.requirement("L2-WRT-022")
    def test_cap_warns_once_per_capped_run(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="aero1553.order"):
            ordered([rec(10, 9, 1), rec(10, 8, 1)], cap=2)
        warnings = [r for r in caplog.records if "max_sort_group" in r.getMessage()]
        assert len(warnings) == 1, f"expected exactly one WARN, got {len(warnings)}"

    @pytest.mark.requirement("L2-WRT-022")
    def test_cap_of_one_disables_reordering(self) -> None:
        got = ordered([rec(10, 21, 3), rec(10, 3, 3)], cap=MAX_SORT_GROUP_MIN)
        assert [m.rt for m in got] == [21, 3]

    @pytest.mark.requirement("L2-WRT-022")
    def test_zero_cap_is_clamped_not_stalled(self) -> None:
        got = ordered([rec(10, 5, 1)], cap=0)
        assert [m.rt for m in got] == [5]

    @pytest.mark.requirement("L2-WRT-022")
    def test_constants_are_consistent_with_rust(self) -> None:
        assert MAX_SORT_GROUP_MIN == 1
        assert MAX_SORT_GROUP_MAX == 1_048_576
        assert DEFAULT_MAX_SORT_GROUP == 4096
        assert MAX_SORT_GROUP_MIN <= DEFAULT_MAX_SORT_GROUP <= MAX_SORT_GROUP_MAX


# ── error propagation and stream edges ──────────────────────────────────────


class TestStreamEdges:
    @pytest.mark.requirement("L2-WRT-021", "L3-PY-016")
    def test_buffered_run_is_flushed_before_a_raised_error(self) -> None:
        """An --allow-partial run must still commit the buffered rows, so the
        flush happens before the exception propagates."""

        def stream() -> object:
            yield rec(10, 9, 1)
            yield rec(10, 2, 1)
            raise MieUnrecoverableSyncLossError(0x100, 1)

        got: list[MieMessage] = []
        # Build the generator outside the block so only the drain can raise
        # (S5778); `got.append` is deliberately the only other statement.
        ordered_stream = order_rows(stream(), DEFAULT_MAX_SORT_GROUP)  # type: ignore[arg-type]
        with pytest.raises(MieUnrecoverableSyncLossError):
            got.extend(ordered_stream)
        assert [m.rt for m in got] == [2, 9], "the sorted run must reach the consumer"

    @pytest.mark.requirement("L3-PY-016")
    def test_error_with_empty_buffer_propagates(self) -> None:
        def stream() -> object:
            # `yield from ()` makes this a generator without leaving dead code
            # after the raise (which the CI-gated vulture scan flags).
            yield from ()
            raise MieUnrecoverableSyncLossError(0, 0)

        ordered_stream = order_rows(stream(), DEFAULT_MAX_SORT_GROUP)  # type: ignore[arg-type]
        with pytest.raises(Aero1553Error):
            list(ordered_stream)

    @pytest.mark.requirement("L3-PY-016")
    def test_early_close_does_not_raise(self) -> None:
        """A consumer that stops early (`| head`) closes the generator; the
        buffer is discarded rather than flushed, because yielding while
        GeneratorExit propagates would raise RuntimeError."""
        gen = order_rows(iter([rec(10, 9, 1), rec(10, 2, 1), rec(20, 5, 1)]))
        next(gen)  # buffers the first run, emits its first row
        gen.close()  # must not raise

    @pytest.mark.requirement("L2-WRT-021")
    def test_empty_stream_yields_nothing(self) -> None:
        assert ordered([]) == []

    @pytest.mark.requirement("L2-WRT-021")
    def test_single_record_passes_through(self) -> None:
        assert keys(ordered([rec(10, 5, 1)])) == [(5, 1, 0)]


# ── timestamp grouping ──────────────────────────────────────────────────────


class TestGrouping:
    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_standard_timestamps_group_on_raw_counter(self) -> None:
        def std(ticks: int, rt: int) -> MieMessage:
            return rec(0, rt, 1, ts=StandardTimestamp(ticks, 0, 0))

        got = ordered([std(500, 9), std(500, 2), std(600, 7)])
        assert [m.rt for m in got] == [2, 9, 7]

    @pytest.mark.requirement("L2-WRT-021")
    def test_differing_timestamp_variants_never_group(self) -> None:
        irig = rec(0, 9, 1)
        standard = rec(0, 2, 1, ts=StandardTimestamp(0, 0, 0))
        assert _group_value(irig) != _group_value(standard)
        # Separate runs, so RT 9 stays ahead of RT 2.
        assert [m.rt for m in ordered([irig, standard])] == [9, 2]

    @pytest.mark.requirement("L2-WRT-021")
    def test_irig_grouping_uses_absolute_microseconds(self) -> None:
        """The same instant expressed through different fields is one group."""
        a = rec(0, 1, 1, ts=IrigTimestamp(192, 15, 55, 0, 0, False))
        b = rec(0, 2, 1, ts=IrigTimestamp(192, 15, 54, 60, 0, False))
        assert _group_value(a) == _group_value(b)

    @pytest.mark.requirement("L2-WRT-021")
    def test_freerun_flag_does_not_split_a_group(self) -> None:
        """`freerun` is not part of the rendered TIME_STAMP, so it is not part
        of grouping."""
        a = rec(10, 9, 1, ts=IrigTimestamp(192, 15, 54, 50, 10, True))
        b = rec(10, 2, 1)
        assert _group_value(a) == _group_value(b)
        assert [m.rt for m in ordered([a, b])] == [2, 9]


# ── CLI / config wiring (L2-WRT-021 placement, L2-WRT-022 flag) ─────────────


class TestCliWiring:
    """The reorder stage must actually be wired into the decode pipeline, and the
    `--max-sort-group` flag must reach it. These drive `cli.main` in-process and
    read the CSV it writes."""

    @staticmethod
    def _rows(csv_path: Path) -> list[tuple[str, str]]:
        """`(RT, MSG)` of each data row."""
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        rows = []
        for line in lines[1:]:
            if line.strip():
                cols = line.split(",")
                rows.append((cols[1], cols[2]))
        return rows

    @staticmethod
    def _tie_recording(tmp_path: Path) -> Path:
        """Three records at one TIME_STAMP, RTs descending on input."""
        from tests.conftest import receive_record_rt_sa_us

        mie = tmp_path / "tie.mie"
        mie.write_bytes(
            receive_record_rt_sa_us(21, 3, 500)
            + receive_record_rt_sa_us(15, 7, 500)
            + receive_record_rt_sa_us(3, 11, 500)
        )
        return mie

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_decode_writes_canonical_order(self, tmp_path: Path) -> None:
        mie = self._tie_recording(tmp_path)
        out = tmp_path / "out.csv"
        assert cli.main(["decode", str(mie), "-o", str(out)]) == 0
        assert self._rows(out) == [("3", "11R"), ("15", "7R"), ("21", "3R")]

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021")
    def test_all_three_key_levels_and_r_before_t(self, tmp_path: Path) -> None:
        """One TIME_STAMP, input order violating every key level, driven through
        the real CLI: RT, then subaddress, then R before T."""
        from tests.conftest import receive_record_rt_sa_us, transmit_record_rt_sa_us

        mie = tmp_path / "keys.mie"
        mie.write_bytes(
            receive_record_rt_sa_us(21, 3, 500)  # highest RT first
            + transmit_record_rt_sa_us(3, 11, 500)  # T before R at the same SA
            + receive_record_rt_sa_us(3, 11, 500)
            + transmit_record_rt_sa_us(3, 2, 500)  # SA 2 last, though it sorts first
        )
        out = tmp_path / "out.csv"
        assert cli.main(["decode", str(mie), "-o", str(out)]) == 0
        assert self._rows(out) == [
            ("3", "2T"),
            ("3", "11R"),
            ("3", "11T"),
            ("21", "3R"),
        ]

    @pytest.mark.requirement("L2-WRT-022", "L3-WRT-003")
    def test_max_sort_group_one_restores_capture_order(self, tmp_path: Path) -> None:
        mie = self._tie_recording(tmp_path)
        out = tmp_path / "out.csv"
        rc = cli.main(["decode", str(mie), "-o", str(out), "--max-sort-group", "1"])
        assert rc == 0
        assert self._rows(out) == [("21", "3R"), ("15", "7R"), ("3", "11R")]

    @pytest.mark.requirement("L2-WRT-022", "L3-WRT-003")
    @pytest.mark.parametrize("bad", ["0", "1048577"])
    def test_max_sort_group_out_of_range_is_usage_error(self, tmp_path: Path, bad: str) -> None:
        mie = self._tie_recording(tmp_path)
        out = tmp_path / "out.csv"
        rc = cli.main(["decode", str(mie), "-o", str(out), "--max-sort-group", bad])
        assert rc == EXIT_USAGE

    @pytest.mark.requirement("L2-WRT-022", "L3-WRT-003", "L1-CFG-001")
    def test_config_key_drives_it_and_cli_overrides(self, tmp_path: Path) -> None:
        mie = self._tie_recording(tmp_path)
        cfg = tmp_path / "cfg.toml"
        cfg.write_text("[output]\nmax_sort_group = 1\n")

        # Config alone disables reordering.
        out1 = tmp_path / "out1.csv"
        assert cli.main(["--config", str(cfg), "decode", str(mie), "-o", str(out1)]) == 0
        assert self._rows(out1)[0] == ("21", "3R")

        # The CLI flag overrides the config, restoring canonical order.
        out2 = tmp_path / "out2.csv"
        rc = cli.main(
            ["--config", str(cfg), "decode", str(mie), "-o", str(out2), "--max-sort-group", "4096"]
        )
        assert rc == 0
        assert self._rows(out2)[0] == ("3", "11R")

    @pytest.mark.requirement("L2-WRT-022")
    def test_capped_run_keeps_every_row(self, tmp_path: Path) -> None:
        mie = self._tie_recording(tmp_path)
        out = tmp_path / "out.csv"
        rc = cli.main(["decode", str(mie), "-o", str(out), "--max-sort-group", "2"])
        assert rc == 0
        assert len(self._rows(out)) == 3, "no row may be dropped at the cap"

    @pytest.mark.requirement("L1-OUT-003", "L2-WRT-021", "L2-ERR-005")
    def test_spurious_continuation_stays_adjacent_through_the_cli(self, tmp_path: Path) -> None:
        """An errored record's 0x2000 continuation must still follow it in the
        CSV, even though a lower-RT clean record sorts ahead of the pair."""
        from tests.conftest import (
            errored_record_rt15_sa11_us,
            receive_record_rt_sa_us,
            spurious_record_us,
        )

        mie = tmp_path / "pin.mie"
        mie.write_bytes(
            errored_record_rt15_sa11_us(500)  # RT15 errored
            + spurious_record_us(500)  # its 0x2000 continuation
            + receive_record_rt_sa_us(3, 11, 500)  # sorts ahead of RT15
        )
        out = tmp_path / "out.csv"
        assert cli.main(["decode", str(mie), "-o", str(out)]) == 0
        rows = self._rows(out)
        assert rows[0] == ("3", "11R"), f"clean low-RT row should lead: {rows}"
        assert rows[1] == ("15", "11R"), f"errored row next: {rows}"
        assert rows[2] == ("", ""), f"its continuation must stay adjacent: {rows}"
