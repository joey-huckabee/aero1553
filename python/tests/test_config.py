"""Tests for aero1553.config and aero1553.filters modules."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from aero1553.config import (
    DecoderConfig,
    FilterConfig,
    _parse_bus_names,
    _parse_type_names,
    load_config,
)
from aero1553.filters import apply_filters
from aero1553.models import (
    Bus,
    CommandWord,
    DeltaScope,
    Direction,
    ErrorMode,
    IrigTimestamp,
    MessageFormat,
    MieMessage,
    TimestampFormat,
    TypeWord,
)
from aero1553.sync import DEFAULT_LOOKAHEAD_RECORDS


def _make_msg(
    msg_type: int = 0x02,
    bus: Bus = Bus.A,
    rt: int = 15,
    sa: int = 11,
    direction: Direction = Direction.RECEIVE,
) -> MieMessage:
    """Helper to create a minimal MieMessage for filter testing."""
    return MieMessage(
        timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
        type_word=TypeWord(msg_type, bus, 36, False, 0x2402),
        message_format=MessageFormat.RECEIVE,
        command_word=CommandWord(rt, direction, sa, 30, 0x797E),
        command_word_2=None,
        status_word=0x7800,
        status_word_2=None,
        data_words=(0x0400,),
        error_word=None,
        delta=0.0,
        file_offset=0,
    )


class TestFilterConfig:
    """Tests for FilterConfig."""

    @pytest.mark.requirement("L2-FLT-001")
    def test_no_filters_active(self) -> None:
        fc = FilterConfig()
        assert fc.is_active is False

    @pytest.mark.requirement("L2-CFG-006")
    def test_type_filter_active(self) -> None:
        fc = FilterConfig(exclude_types={0x20})
        assert fc.is_active is True

    @pytest.mark.requirement("L2-CFG-006")
    def test_should_exclude_by_type(self) -> None:
        fc = FilterConfig(exclude_types={0x20})
        assert fc.should_exclude(0x20, 15, Bus.A, 11) is True
        assert fc.should_exclude(0x02, 15, Bus.A, 11) is False

    @pytest.mark.requirement("L2-CFG-006")
    def test_should_exclude_by_rt(self) -> None:
        fc = FilterConfig(exclude_rts={31})
        assert fc.should_exclude(0x02, 31, Bus.A, 11) is True
        assert fc.should_exclude(0x02, 15, Bus.A, 11) is False

    @pytest.mark.requirement("L2-CFG-006")
    def test_should_exclude_by_bus(self) -> None:
        fc = FilterConfig(exclude_buses={Bus.B})
        assert fc.should_exclude(0x02, 15, Bus.B, 11) is True
        assert fc.should_exclude(0x02, 15, Bus.A, 11) is False

    @pytest.mark.requirement("L2-CFG-006")
    def test_should_exclude_by_subaddress(self) -> None:
        fc = FilterConfig(exclude_subaddresses={0, 31})
        assert fc.should_exclude(0x02, 15, Bus.A, 0) is True
        assert fc.should_exclude(0x02, 15, Bus.A, 31) is True
        assert fc.should_exclude(0x02, 15, Bus.A, 11) is False

    @pytest.mark.requirement("L2-FLT-002")
    def test_or_logic(self) -> None:
        """Message matching ANY criterion should be excluded."""
        fc = FilterConfig(exclude_types={0x20}, exclude_rts={31})
        assert fc.should_exclude(0x02, 31, Bus.A, 11) is True
        assert fc.should_exclude(0x20, 15, Bus.A, 11) is True
        assert fc.should_exclude(0x02, 15, Bus.A, 11) is False


class TestParseTypeNames:
    """Tests for type name parsing."""

    @pytest.mark.requirement("L2-CFG-007")
    def test_by_name(self) -> None:
        result = _parse_type_names(["BC_TO_RT", "RT_TO_BC"])
        assert result == {0x02, 0x04}

    @pytest.mark.requirement("L2-CFG-007")
    def test_case_insensitive(self) -> None:
        result = _parse_type_names(["bc_to_rt"])
        assert result == {0x02}

    @pytest.mark.requirement("L2-CFG-007")
    def test_by_hex(self) -> None:
        result = _parse_type_names(["0x02", "0x20"])
        assert result == {0x02, 0x20}

    @pytest.mark.requirement("L2-CFG-007")
    def test_invalid_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            _parse_type_names(["NONEXISTENT"])

    @pytest.mark.requirement("L2-CFG-007")
    def test_invalid_hex_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid hex"):
            _parse_type_names(["0xZZ"])

    @pytest.mark.requirement("L2-CFG-007")
    def test_integer_codes_accepted(self) -> None:
        # A TOML array may carry bare integers; accept them bounded to u8,
        # matching the Rust parse_type_value (rust/src/config.rs). Previously this
        # crashed with an AttributeError on .strip().
        result = _parse_type_names([0x02, 32, "RT_TO_BC"])
        assert result == {0x02, 0x20, 0x04}

    @pytest.mark.requirement("L2-CFG-007")
    def test_integer_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            _parse_type_names([256])

    @pytest.mark.requirement("L2-CFG-007")
    def test_hex_out_of_range_raises(self) -> None:
        # 0x100 = 256 > u8 max; Rust rejects it, so Python must too (previously
        # it was silently accepted, making the filter a no-op).
        with pytest.raises(ValueError, match="Invalid hex"):
            _parse_type_names(["0x100"])

    @pytest.mark.requirement("L2-CFG-007")
    def test_non_str_non_int_raises(self) -> None:
        with pytest.raises(ValueError, match="must be strings or integers"):
            _parse_type_names([1.5])


class TestParseBusNames:
    """Tests for bus name parsing."""

    @pytest.mark.requirement("L2-CFG-006")
    def test_valid(self) -> None:
        result = _parse_bus_names(["A", "B"])
        assert result == {Bus.A, Bus.B}

    @pytest.mark.requirement("L2-CFG-006")
    def test_case_insensitive(self) -> None:
        result = _parse_bus_names(["a", "b"])
        assert result == {Bus.A, Bus.B}

    @pytest.mark.requirement("L2-CFG-006")
    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid bus"):
            _parse_bus_names(["C"])

    @pytest.mark.requirement("L2-CFG-006")
    def test_non_str_entry_raises_cleanly(self) -> None:
        # A non-string TOML entry (e.g. an integer) must raise a clean
        # ValueError matching Rust ("entries must be strings"), not crash with
        # an AttributeError on .strip().
        with pytest.raises(ValueError, match="must be strings"):
            _parse_bus_names([1])


class TestDecoderConfig:
    """Tests for DecoderConfig."""

    @pytest.mark.requirement("L2-CFG-003")
    def test_defaults(self) -> None:
        config = DecoderConfig()
        assert config.log_level == "WARNING"
        assert config.input_time_format == TimestampFormat.AUTO
        assert config.strict is False
        assert config.filters.is_active is False

    @pytest.mark.requirement("L2-CFG-003")
    def test_with_overrides(self) -> None:
        config = DecoderConfig()
        updated = config.with_overrides(
            log_level="DEBUG",
            exclude_types={0x20},
        )
        assert updated.log_level == "DEBUG"
        assert 0x20 in updated.filters.exclude_types
        assert config.log_level == "WARNING"  # original unchanged

    @pytest.mark.requirement("L2-CFG-004")
    def test_overrides_merge_filters(self) -> None:
        """CLI filters should merge with config file filters."""
        config = DecoderConfig(filters=FilterConfig(exclude_types={0x20}))
        updated = config.with_overrides(exclude_types={0x01})
        assert updated.filters.exclude_types == {0x20, 0x01}

    @pytest.mark.requirement("L2-CFG-003")
    def test_zero_valued_enum_override_is_applied(self) -> None:
        """An override whose value is falsy still wins over the config file.

        ``TimestampFormat.AUTO`` and ``ErrorMode.SEPARATE`` are both ``0``. A
        truthiness-based override check silently discarded them, so
        ``--input-time-format auto`` was a no-op against ``input_time_format = "irig"`` while
        Rust honored it — the two implementations then decoded the same file
        differently. Presence, not truthiness, decides (Rust ``Option<T>``).
        """
        config = DecoderConfig(
            input_time_format=TimestampFormat.IRIG,
            error_mode=ErrorMode.INLINE,
        )
        updated = config.with_overrides(
            input_time_format=TimestampFormat.AUTO,
            error_mode=ErrorMode.SEPARATE,
        )
        assert updated.input_time_format == TimestampFormat.AUTO
        assert updated.error_mode == ErrorMode.SEPARATE

    @pytest.mark.requirement("L2-CFG-003")
    def test_empty_string_override_is_applied(self) -> None:
        """An empty-string override is applied, not silently ignored.

        ``--format ''`` must reach the ``output.format`` validity check (a
        runtime error, exit 1) exactly as it does on Rust, rather than falling
        back to ``"csv"`` and decoding as if nothing were wrong.
        """
        updated = DecoderConfig().with_overrides(output_format="")
        assert updated.output_format == ""

    @pytest.mark.requirement("L2-CFG-003")
    def test_omitted_override_keeps_config_value(self) -> None:
        """An absent (``None``) override never resets a config-file value."""
        config = DecoderConfig(input_time_format=TimestampFormat.IRIG, no_clobber=True)
        updated = config.with_overrides(input_time_format=None, no_clobber=None)
        assert updated.input_time_format == TimestampFormat.IRIG
        assert updated.no_clobber is True


class TestLoadConfig:
    """Tests for TOML config loading."""

    @pytest.mark.requirement("L2-CFG-001")
    def test_none_returns_defaults(self) -> None:
        config = load_config(None)
        assert config.log_level == "WARNING"

    @pytest.mark.requirement("L2-CFG-005")
    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.toml")

    @pytest.mark.requirement("L3-PY-005")
    def test_load_valid_toml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "test.toml"
        cfg_file.write_text(
            '[logging]\nlevel = "DEBUG"\n\n'
            '[filter]\nexclude_types = ["SPURIOUS_DATA"]\n'
            "exclude_rts = [31]\n"
        )
        config = load_config(cfg_file)
        assert config.log_level == "DEBUG"
        assert 0x20 in config.filters.exclude_types
        assert 31 in config.filters.exclude_rts

    @pytest.mark.requirement("L2-DEC-013")
    def test_load_with_input_time_format(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "tf.toml"
        cfg_file.write_text('[decode]\ninput_time_format = "irig"\n')
        config = load_config(cfg_file)
        assert config.input_time_format == TimestampFormat.IRIG

    @pytest.mark.requirement("L2-CFG-010")
    def test_invalid_input_time_format_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "bad_tf.toml"
        cfg_file.write_text('[decode]\ninput_time_format = "bogus"\n')
        with pytest.raises(ValueError, match="Invalid input_time_format"):
            load_config(cfg_file)


class TestApplyFilters:
    """Tests for the filter generator wrapper."""

    @pytest.mark.requirement("L2-FLT-001")
    def test_no_filters_passes_all(self) -> None:
        msgs = [_make_msg(), _make_msg(), _make_msg()]
        fc = FilterConfig()
        result = list(apply_filters(msgs, fc))
        assert len(result) == 3

    @pytest.mark.requirement("L2-FLT-001")
    def test_exclude_by_type(self) -> None:
        msgs = [
            _make_msg(msg_type=0x02),
            _make_msg(msg_type=0x04),
            _make_msg(msg_type=0x20),
        ]
        fc = FilterConfig(exclude_types={0x20})
        result = list(apply_filters(msgs, fc))
        assert len(result) == 2
        assert all(m.type_word.message_type != 0x20 for m in result)

    @pytest.mark.requirement("L2-FLT-001")
    def test_exclude_by_rt(self) -> None:
        msgs = [_make_msg(rt=15), _make_msg(rt=30), _make_msg(rt=31)]
        fc = FilterConfig(exclude_rts={31})
        result = list(apply_filters(msgs, fc))
        assert len(result) == 2

    @pytest.mark.requirement("L2-FLT-001")
    def test_exclude_by_bus(self) -> None:
        msgs = [_make_msg(bus=Bus.A), _make_msg(bus=Bus.B)]
        fc = FilterConfig(exclude_buses={Bus.B})
        result = list(apply_filters(msgs, fc))
        assert len(result) == 1
        assert result[0].bus == Bus.A

    @pytest.mark.requirement("L2-FLT-001")
    def test_exclude_all(self) -> None:
        msgs = [_make_msg(rt=15), _make_msg(rt=15)]
        fc = FilterConfig(exclude_rts={15})
        result = list(apply_filters(msgs, fc))
        assert len(result) == 0

    @pytest.mark.requirement("L2-FLT-001")
    def test_spurious_without_command_word_survives_rt_filter(self) -> None:
        """SPURIOUS_DATA records have no Command Word (rt/subaddress are
        None). An RT or subaddress filter must not match them — and must
        not raise on the None Command Word — mirroring the Rust filter.
        They remain excludable by type or bus.
        """
        spurious = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x20, Bus.A, 12, False, 0x2420),
            message_format=MessageFormat.SPURIOUS_DATA,
            command_word=None,
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=(0x1234,),
            error_word=0x2001,
            delta=None,
            file_offset=0,
        )
        # RT/subaddress filters never match a None Command Word: the
        # spurious record passes through (and does not raise).
        assert list(apply_filters([spurious], FilterConfig(exclude_rts={15}))) == [spurious]
        assert list(apply_filters([spurious], FilterConfig(exclude_subaddresses={11}))) == [
            spurious
        ]
        # But type and bus filters still apply to it.
        assert list(apply_filters([spurious], FilterConfig(exclude_types={0x20}))) == []
        assert list(apply_filters([spurious], FilterConfig(exclude_buses={Bus.A}))) == []

    @pytest.mark.requirement("L2-FLT-001")
    def test_end_to_end_with_reader(self, tmp_mie_file: Path) -> None:
        """Filter should work with actual MieFileReader output."""
        from aero1553.reader import MieFileReader

        reader = MieFileReader(tmp_mie_file)
        # Exclude RT 15 SA 22 — should drop the 2nd and 3rd records
        fc = FilterConfig(exclude_subaddresses={22})
        result = list(apply_filters(reader, fc))
        assert len(result) == 1
        assert result[0].msg_label == "11R"

    # ── Include filters (L3-PY-013, parity with Rust L3-RS-010) ──────────

    @pytest.mark.requirement("L3-PY-013")
    def test_include_by_rt_drops_non_matches(self) -> None:
        msgs = [_make_msg(rt=15), _make_msg(rt=30), _make_msg(rt=31)]
        result = list(apply_filters(msgs, FilterConfig(include_rts={15})))
        assert len(result) == 1
        assert result[0].command_word is not None
        assert result[0].command_word.rt == 15

    @pytest.mark.requirement("L3-PY-013")
    def test_include_by_type(self) -> None:
        msgs = [_make_msg(msg_type=0x02), _make_msg(msg_type=0x04)]
        result = list(apply_filters(msgs, FilterConfig(include_types={0x02})))
        assert len(result) == 1
        assert result[0].type_word.message_type == 0x02

    @pytest.mark.requirement("L3-PY-013")
    def test_include_by_bus(self) -> None:
        msgs = [_make_msg(bus=Bus.A), _make_msg(bus=Bus.B)]
        result = list(apply_filters(msgs, FilterConfig(include_buses={Bus.B})))
        assert len(result) == 1
        assert result[0].bus == Bus.B

    @pytest.mark.requirement("L3-PY-013")
    def test_include_subaddress_drops_spurious_without_command_word(self) -> None:
        """An active RT/subaddress include filter drops SPURIOUS_DATA (no
        Command Word), mirroring the Rust filter — the complement of how
        exclude filters leave such records untouched."""
        spurious = MieMessage(
            timestamp=IrigTimestamp(192, 15, 54, 50, 456225, False),
            type_word=TypeWord(0x20, Bus.A, 12, False, 0x2420),
            message_format=MessageFormat.SPURIOUS_DATA,
            command_word=None,
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=(0x1234,),
            error_word=0x2001,
            delta=None,
            file_offset=0,
        )
        normal = _make_msg(rt=15, sa=11)
        result = list(apply_filters([spurious, normal], FilterConfig(include_rts={15})))
        assert result == [normal]
        result = list(apply_filters([spurious, normal], FilterConfig(include_subaddresses={11})))
        assert result == [normal]

    @pytest.mark.requirement("L3-PY-013")
    def test_exclude_takes_precedence_over_include(self) -> None:
        """Excludes are checked first: a value in both sets is dropped."""
        msgs = [_make_msg(rt=15), _make_msg(rt=30)]
        fc = FilterConfig(include_rts={15, 30}, exclude_rts={15})
        result = list(apply_filters(msgs, fc))
        assert len(result) == 1
        assert result[0].command_word is not None
        assert result[0].command_word.rt == 30

    @pytest.mark.requirement("L3-PY-013")
    def test_multiple_include_sets_all_must_match(self) -> None:
        """Every active include set must contain the message's value."""
        msgs = [
            _make_msg(rt=15, bus=Bus.A),
            _make_msg(rt=15, bus=Bus.B),
            _make_msg(rt=30, bus=Bus.A),
        ]
        fc = FilterConfig(include_rts={15}, include_buses={Bus.A})
        result = list(apply_filters(msgs, fc))
        assert len(result) == 1
        assert result[0].command_word is not None
        assert result[0].command_word.rt == 15
        assert result[0].bus == Bus.A


class TestCliFilters:
    """CLI integration tests for filtering."""

    @pytest.mark.requirement("L2-CLI-010")
    def test_exclude_types_cli(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        out = tmp_path / "filtered.csv"
        # Exclude RT_TO_BC (type 0x04) — should remove 3rd record
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--exclude-types",
                "RT_TO_BC",
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows

    @pytest.mark.requirement("L2-CLI-010")
    def test_exclude_rts_cli(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        out = tmp_path / "rt_filtered.csv"
        # All records are RT 15, so excluding it should leave 0 data rows
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--exclude-rts",
                "15",
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 1  # header only, no data rows

    @pytest.mark.requirement("L2-CFG-004")
    def test_config_file_cli(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        cfg = tmp_path / "test.toml"
        cfg.write_text('[filter]\nexclude_types = ["RT_TO_BC"]\n')
        out = tmp_path / "cfg_filtered.csv"
        rc = main(
            [
                "--config",
                str(cfg),  # global: before the subcommand
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 3  # header + 2 (RT_TO_BC excluded)

    # ── Include filters + comma syntax (L3-PY-013, parity with Rust) ─────
    # The multi-record fixture is RT15: record 1 SA11, records 2/3 SA22.

    @pytest.mark.requirement("L3-PY-013")
    def test_include_subaddresses_cli(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        out = tmp_path / "inc.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--include-subaddresses",
                "11",
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 2  # header + the single SA11 record

    @pytest.mark.requirement("L3-PY-013")
    def test_include_comma_separated_values(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        out = tmp_path / "inc_comma.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--include-subaddresses",
                "11,22",
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 4  # header + all 3 records (SA11 + SA22 x2)

    @pytest.mark.requirement("L3-PY-013")
    def test_include_repeated_flag_accumulates(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        out = tmp_path / "inc_rep.csv"
        # Equivalent to the comma form above.
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--include-subaddresses",
                "11",
                "--include-subaddresses",
                "22",
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 4

    @pytest.mark.requirement("L3-PY-013")
    def test_include_rts_no_match_yields_header_only(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        from aero1553.cli import main

        out = tmp_path / "inc_none.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--include-rts",
                "30",  # fixture is all RT15
            ]
        )
        assert rc == 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 1  # header only

    @pytest.mark.requirement("L2-CLI-010")
    def test_bad_rt_value_is_usage_error(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        from aero1553.cli import main

        out = tmp_path / "bad.csv"
        rc = main(
            [
                "decode",
                str(tmp_mie_file),
                "-o",
                str(out),
                "--exclude-rts",
                "notanumber",
            ]
        )
        assert rc == 4  # EXIT_USAGE
        assert not out.exists()


class TestErrorModeConfig:
    """Tests for error mode configuration."""

    @pytest.mark.requirement("L2-CFG-001")
    def test_default_is_inline(self) -> None:
        """Inline is the built-in default; separate is opt-in (L2-ERR-011)."""
        from aero1553.models import ErrorMode

        config = DecoderConfig()
        assert config.error_mode == ErrorMode.INLINE

    @pytest.mark.requirement("L2-CFG-001")
    def test_override_to_separate(self) -> None:
        from aero1553.models import ErrorMode

        config = DecoderConfig()
        updated = config.with_overrides(error_mode=ErrorMode.SEPARATE)
        assert updated.error_mode == ErrorMode.SEPARATE

    @pytest.mark.requirement("L2-ERR-011")
    def test_load_from_toml(self, tmp_path: Path) -> None:
        from aero1553.models import ErrorMode

        cfg = tmp_path / "em.toml"
        cfg.write_text('[decode]\nerror_mode = "separate"\n')
        config = load_config(cfg)
        assert config.error_mode == ErrorMode.SEPARATE

    @pytest.mark.requirement("L2-ERR-011")
    def test_omitted_error_mode_key_defaults_to_inline(self, tmp_path: Path) -> None:
        """A config saying nothing about error_mode gets the new default."""
        from aero1553.models import ErrorMode

        cfg = tmp_path / "quiet.toml"
        cfg.write_text("[decode]\nstrict = false\n")
        assert load_config(cfg).error_mode == ErrorMode.INLINE

    @pytest.mark.requirement("L2-CFG-010")
    def test_invalid_error_mode_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad_em.toml"
        cfg.write_text('[decode]\nerror_mode = "bogus"\n')
        with pytest.raises(ValueError, match="Invalid error_mode"):
            load_config(cfg)

    @pytest.mark.requirement("L3-PY-011")
    def test_cli_inline_is_the_default(self, tmp_path: Path) -> None:
        """With no flag, errored/spurious records stay in the SAME CSV with
        ERROR/ERROR_CODE populated and no separate errors file is created
        (L3-PY-011 / L2-ERR-010). Inline is the default; --separate-errors
        opts out."""
        import csv

        from aero1553.cli import main
        from tests.conftest import (
            errored_record_rt15_sa11_us,
            normal_record_rt15_sa11_us,
        )

        mie = tmp_path / "with_error.mie"
        mie.write_bytes(normal_record_rt15_sa11_us(100) + errored_record_rt15_sa11_us(16100))
        out = tmp_path / "inline.csv"
        rc = main(["decode", str(mie), "-o", str(out)])
        assert rc == 0

        # Inline mode: a single file, no <stem>_errors.csv sibling.
        assert out.exists()
        assert not (tmp_path / "inline_errors.csv").exists()

        rows = list(csv.DictReader(out.read_text().splitlines()))
        assert len(rows) == 2  # normal + errored, both inline
        error_rows = [r for r in rows if r["ERROR"] == "ERROR"]
        assert len(error_rows) == 1
        assert error_rows[0]["ERROR_CODE"] == "011E"  # the errored record's code

    @pytest.mark.requirement("L2-ERR-008")
    def test_cli_separate_errors_opts_into_split(self, tmp_path: Path) -> None:
        """--separate-errors restores the split layout: clean records in the main
        CSV, errored/spurious in <stem>_errors.csv (L2-ERR-008). Inline is the
        default, so this flag is the opt-out."""
        import csv

        from aero1553.cli import main
        from tests.conftest import (
            errored_record_rt15_sa11_us,
            normal_record_rt15_sa11_us,
        )

        mie = tmp_path / "with_error.mie"
        mie.write_bytes(normal_record_rt15_sa11_us(100) + errored_record_rt15_sa11_us(16100))
        out = tmp_path / "split.csv"
        rc = main(["decode", str(mie), "-o", str(out), "--separate-errors"])
        assert rc == 0

        errors = tmp_path / "split_errors.csv"
        assert out.exists()
        assert errors.exists()
        main_rows = list(csv.DictReader(out.read_text().splitlines()))
        err_rows = list(csv.DictReader(errors.read_text().splitlines()))
        assert [r["ERROR"] for r in main_rows] == [""]
        assert [r["ERROR"] for r in err_rows] == ["ERROR"]

    @pytest.mark.requirement("L2-ERR-008")
    def test_cli_separate_errors_file_lazy_when_no_errors(
        self, tmp_mie_file: Path, tmp_path: Path
    ) -> None:
        """Even with --separate-errors, a clean decode creates no errors file."""
        from aero1553.cli import main

        out = tmp_path / "main.csv"
        rc = main(["decode", str(tmp_mie_file), "-o", str(out), "--separate-errors"])
        assert rc == 0
        assert out.exists()
        assert not (tmp_path / "main_errors.csv").exists()

    @pytest.mark.requirement("L3-PY-011")
    def test_separate_errors_on_stdout_warns_and_falls_back_to_inline(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stream cannot be split, so --separate-errors degrades to inline.

        The request was explicit, so it must be reported rather than silently
        ignored — matching the same WARN in the Rust CLI.
        """
        import logging

        from aero1553.cli import main
        from tests.conftest import (
            errored_record_rt15_sa11_us,
            normal_record_rt15_sa11_us,
        )

        mie = tmp_path / "with_error.mie"
        mie.write_bytes(normal_record_rt15_sa11_us(100) + errored_record_rt15_sa11_us(16100))
        with caplog.at_level(logging.WARNING, logger="aero1553"):
            rc = main(["decode", str(mie), "--separate-errors"])
        assert rc == 0
        assert any("forces inline" in r.getMessage() for r in caplog.records), (
            f"expected a WARN that stdout forces inline; got "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    @pytest.mark.requirement("L3-PY-011")
    def test_cli_removed_inline_errors_flag_is_a_usage_error(self, tmp_path: Path) -> None:
        """--inline-errors was removed when inline became the default.

        It must fail loudly (exit 4) rather than be quietly accepted, so a script
        still carrying the old flag gets corrected instead of silently relying on
        behaviour that is now the default anyway.
        """
        from aero1553.cli import EXIT_USAGE, main
        from tests.conftest import normal_record_rt15_sa11_us

        mie = tmp_path / "rec.mie"
        mie.write_bytes(normal_record_rt15_sa11_us(100))
        with pytest.raises(SystemExit) as excinfo:
            main(["decode", str(mie), "-o", str(tmp_path / "o.csv"), "--inline-errors"])
        assert excinfo.value.code == EXIT_USAGE


class TestSchemaValidation:
    """L2-CFG schema validation tests."""

    @pytest.mark.requirement("L2-CFG-010")
    def test_unknown_log_level_rejected(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad.toml"
        cfg.write_text('[logging]\nlevel = "NOPE"\n')
        with pytest.raises(ValueError, match="logging"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_known_log_levels_accepted_case_insensitively(self, tmp_path: Path) -> None:
        for level in ["DEBUG", "info", "Warning", "WARN", "error", "CRITICAL"]:
            cfg = tmp_path / f"l_{level}.toml"
            cfg.write_text(f'[logging]\nlevel = "{level}"\n')
            config = load_config(cfg)
            assert config.log_level == level.upper()

    @pytest.mark.requirement("L2-CFG-010")
    def test_unknown_output_format_rejected(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad_fmt.toml"
        cfg.write_text('[output]\nformat = "json"\n')
        with pytest.raises(ValueError, match=r"output\.format"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_output_format_csv_accepted(self, tmp_path: Path) -> None:
        cfg = tmp_path / "ok_fmt.toml"
        cfg.write_text('[output]\nformat = "csv"\n')
        config = load_config(cfg)
        assert config.output_format == "csv"

    @pytest.mark.requirement("L2-CFG-010")
    def test_strict_must_be_bool(self, tmp_path: Path) -> None:
        # TOML supports bool natively. A string here is rejected by
        # tomllib at parse time (TypeError), so we test the dataclass
        # path instead.
        from aero1553.config import _require_bool

        with pytest.raises(ValueError, match="expected boolean"):
            _require_bool("decode", "strict", "yes")

    @pytest.mark.requirement("L2-CFG-010")
    def test_exclude_rts_out_of_range_rejected(self, tmp_path: Path) -> None:
        cfg = tmp_path / "rt_high.toml"
        cfg.write_text("[filter]\nexclude_rts = [32]\n")
        with pytest.raises(ValueError, match=r"\[0, 31\]"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_exclude_subaddresses_negative_rejected(self, tmp_path: Path) -> None:
        cfg = tmp_path / "sa_neg.toml"
        cfg.write_text("[filter]\nexclude_subaddresses = [-1]\n")
        with pytest.raises(ValueError, match=r"\[0, 31\]"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_exclude_rts_boundary_values_accepted(self, tmp_path: Path) -> None:
        cfg = tmp_path / "rt_bounds.toml"
        cfg.write_text("[filter]\nexclude_rts = [0, 31]\n")
        config = load_config(cfg)
        assert config.filters.exclude_rts == {0, 31}

    @pytest.mark.requirement("L2-CFG-010")
    def test_non_string_input_time_format_rejected(self, tmp_path: Path) -> None:
        # TOML-valid but schema-invalid: an int where a string is expected must
        # be a clean config error (ValueError -> exit 5), not a leaked
        # AttributeError. Matches the Rust "must be a string" rejection.
        cfg = tmp_path / "tf_int.toml"
        cfg.write_text("[decode]\ninput_time_format = 1\n")
        with pytest.raises(ValueError, match=r"decode\.input_time_format"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_non_string_error_mode_rejected(self, tmp_path: Path) -> None:
        cfg = tmp_path / "em_int.toml"
        cfg.write_text("[decode]\nerror_mode = 1\n")
        with pytest.raises(ValueError, match=r"decode\.error_mode"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_dotted_key_rejected(self, tmp_path: Path) -> None:
        # tomllib would honor a dotted key (nesting it), but the Rust parser does
        # not support the form; both reject it so a config behaves identically
        # (and a dotted safety option like output.no_clobber can't be silently
        # dropped on one implementation).
        cfg = tmp_path / "dotted.toml"
        cfg.write_text("decode.strict = true\n")
        with pytest.raises(ValueError, match="dotted keys"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    def test_array_of_tables_rejected(self, tmp_path: Path) -> None:
        cfg = tmp_path / "arraytable.toml"
        cfg.write_text("[[decode]]\nstrict = true\n")
        with pytest.raises(ValueError, match="array-of-tables"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    @pytest.mark.parametrize(
        "snippet",
        [
            "[decode]\nx = { a = 1 }\n",  # inline table
            "[decode]\ndetect_records = 1_0\n",  # underscore separator
            "[decode]\ndetect_records = 0x08\n",  # hex integer
            "[decode]\ndetect_records = 0o10\n",  # octal integer
            "[decode]\ndetect_records = 0b1000\n",  # binary integer
            "[decode]\nx = 1979-05-27\n",  # date-time
            "[filter]\nexclude_rts = [\n1,\n]\n",  # multi-line array
            '[decode]\n"strict" = true\n',  # quoted key
            "[decode]\ndetect_records = 08\n",  # leading zero (native i64 takes it)
            "[decode]\nstandard_tick_rate_hz = 1.\n",  # bare trailing dot
            "[filter]\nexclude_rts = [01]\n",  # leading zero in an array element
            '[mux]\ndelimiter = "\\r"\n',  # \r escape — not in the supported set
            '[mux]\ndelimiter = "\\u002C"\n',  # \u escape — not supported
        ],
    )
    def test_non_flat_toml_forms_rejected(self, tmp_path: Path, snippet: str) -> None:
        # tomllib would accept each of these; the Rust value/key grammar does not,
        # so the whitelist rejects them up front to keep the two aligned.
        cfg = tmp_path / "bad.toml"
        cfg.write_text(snippet)
        with pytest.raises(ValueError):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    @pytest.mark.parametrize(
        "snippet",
        [
            '[decode]\ninput_time_format = "irig"\n',
            "[decode]\ndetect_records = 8\n",
            "[decode]\nstandard_tick_rate_hz = 1000000.0\n",
            "[decode]\nstandard_tick_rate_hz = 1e6\n",  # exponent float is fine
            "[filter]\nexclude_rts = [0, 31]\n",
            '[filter]\nexclude_buses = ["A", "B"]\n',
            "[mux]\nfield = -1\n",
            "[decode]\nstrict = true  # trailing comment\n",
            '[mux]\ndelimiter = "\\t"\n',  # \t is a supported escape
            '[mux]\ndelimiter = "\\""\n',  # \" is a supported escape
            # array string with an escaped quote + comma: one element, accepted
            # (unknown key → warn-and-continue on both).
            '[bogus]\nunknown_key = ["a\\", b"]\n',
        ],
    )
    def test_flat_forms_still_accepted(self, tmp_path: Path, snippet: str) -> None:
        # The whitelist must not over-reject valid flat configs.
        cfg = tmp_path / "ok.toml"
        cfg.write_text(snippet)
        load_config(cfg)  # must not raise

    @pytest.mark.requirement("L2-CFG-010")
    def test_array_splitter_respects_escaped_quotes(self) -> None:
        from aero1553.config import _split_array_items

        # A comma that follows an escaped quote is *inside* the string, so this
        # array has exactly one element — mirroring the Rust splitter. Before the
        # fix Python broke it into two and rejected the config where Rust accepts.
        assert _split_array_items(r'"a\", b"') == [r'"a\", b"']
        # A plain top-level comma still separates.
        assert len(_split_array_items("0, 31")) == 2
        assert len(_split_array_items(r'"A", "B"')) == 2

    @pytest.mark.requirement("L2-CFG-009")
    def test_root_level_scalar_key_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A root-level scalar key is unknown (the schema has no root keys); both
        # implementations warn-and-continue. Rust logs `[] bogus`; Python must
        # too, rather than dropping it silently.
        cfg = tmp_path / "root.toml"
        cfg.write_text("bogus = true\n")
        with caplog.at_level("WARNING", logger="aero1553"):
            load_config(cfg)  # accepts (does not raise)
        assert "[] bogus" in caplog.text

    @pytest.mark.requirement("L2-CFG-010")
    def test_dotted_section_header_rejected(self, tmp_path: Path) -> None:
        # `[output.no_clobber]` nests a table; both implementations reject the
        # dotted header rather than diverging (Rust previously stored it
        # literally and silently ignored the keys).
        cfg = tmp_path / "dotted_header.toml"
        cfg.write_text("[output.no_clobber]\nenabled = true\n")
        with pytest.raises(ValueError, match="dotted section headers"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-010")
    @pytest.mark.parametrize("section", ["decode", "logging", "output", "mux", "merge", "filter"])
    def test_section_used_as_scalar_rejected(self, tmp_path: Path, section: str) -> None:
        # A known section name assigned a scalar (e.g. `decode = true` instead of
        # a `[decode]` header) is a config error on both implementations, rather
        # than a leaked AttributeError (Python) or a silent drop (Rust).
        cfg = tmp_path / f"{section}_scalar.toml"
        cfg.write_text(f"{section} = true\n")
        with pytest.raises(ValueError, match=rf"\[{section}\]"):
            load_config(cfg)

    @pytest.mark.requirement("L2-CFG-011")
    @pytest.mark.requirement("L2-DEC-017")
    def test_standard_tick_rate_hz_default_is_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / "std.toml"
        cfg.write_text('[decode]\ninput_time_format = "standard"\n')
        config = load_config(cfg)
        assert config.standard_tick_rate_hz is None

    @pytest.mark.requirement("L2-CFG-011")
    def test_standard_tick_rate_hz_accepts_float_and_int(self, tmp_path: Path) -> None:
        cfg = tmp_path / "std_float.toml"
        cfg.write_text("[decode]\nstandard_tick_rate_hz = 1000000.0\n")
        assert load_config(cfg).standard_tick_rate_hz == 1_000_000.0
        # A bare integer is accepted and coerced to float.
        cfg2 = tmp_path / "std_int.toml"
        cfg2.write_text("[decode]\nstandard_tick_rate_hz = 1000000\n")
        assert load_config(cfg2).standard_tick_rate_hz == 1_000_000.0

    @pytest.mark.requirement("L2-CFG-011")
    @pytest.mark.requirement("L2-CFG-010")
    def test_standard_tick_rate_hz_rejects_nonpositive(self, tmp_path: Path) -> None:
        for bad in ["0", "0.0", "-1.0"]:
            cfg = tmp_path / f"std_bad_{bad}.toml"
            cfg.write_text(f"[decode]\nstandard_tick_rate_hz = {bad}\n")
            with pytest.raises(ValueError, match="standard_tick_rate_hz"):
                load_config(cfg)

    @pytest.mark.requirement("L2-CFG-003")
    def test_standard_tick_rate_hz_override_applies(self) -> None:
        config = load_config(None).with_overrides(standard_tick_rate_hz=2_000_000.0)
        assert config.standard_tick_rate_hz == 2_000_000.0

    @pytest.mark.requirement("L2-CFG-009")
    def test_unknown_top_level_key_is_warned_not_rejected(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = tmp_path / "unknown.toml"
        cfg.write_text('[output]\nformat = "csv"\nunknown_thing = true\n')
        import logging

        with caplog.at_level(logging.WARNING, logger="aero1553.config"):
            config = load_config(cfg)
        assert config.output_format == "csv"
        # The WARN should mention the offending key.
        assert any("unknown_thing" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.requirement("L2-CFG-009")
    def test_unknown_filter_key_warned_not_rejected(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Common typo: exclude_subdresses (missing "ad").
        cfg = tmp_path / "typo.toml"
        cfg.write_text("[filter]\nexclude_subdresses = [0]\n")
        import logging

        with caplog.at_level(logging.WARNING, logger="aero1553.config"):
            config = load_config(cfg)
        assert config.filters.exclude_subaddresses == set()
        assert any("exclude_subdresses" in rec.getMessage() for rec in caplog.records)


class TestSharedDefaultConfig:
    """The repo-root config/default.toml is a shared artifact; the Python
    loader must accept it and resolve every documented key (L2-CFG-001).
    Guards against the starter file drifting into something only one
    implementation can parse."""

    @pytest.mark.requirement("L2-CFG-001")
    def test_loads_shared_default_toml(self) -> None:
        # No skip-if-absent guard: the file is tracked, so its absence is a
        # broken checkout and should fail loudly. The Rust mirror of this test
        # skipped silently on a path that could never resolve, and passed
        # without asserting anything for its whole life (L2-CFG-008).
        default_toml = Path(__file__).resolve().parents[2] / "config" / "default.toml"
        assert default_toml.is_file(), f"missing shared starter config: {default_toml}"
        cfg = load_config(default_toml)
        # The four keys added to keep the starter file complete must parse to
        # their documented defaults under the Python loader too.
        assert cfg.allow_partial is False
        assert cfg.no_clobber is False
        assert cfg.detect_records == 8
        assert cfg.lookahead_records == DEFAULT_LOOKAHEAD_RECORDS
        assert cfg.output_format == "csv"
        assert cfg.max_sort_group == 4096

    @pytest.mark.requirement("L2-SYN-026")
    def test_shipped_defaults_for_the_record_count_knobs(self) -> None:
        """The shipped *default values*, pinned as literals.

        Nothing asserted these before v2.15.0. Every look-ahead test passes an
        explicit depth, so the default could disagree with the specification
        without any test noticing -- which is what happened: L2-SYN-026 read
        "default 8" while all three implementations shipped 2. Referencing the
        constant would reproduce the blind spot, so the literals are
        deliberate. Changing one means changing docs/L2-REQ.md,
        docs/CONFIG-REFERENCE.md, docs/CLI-REFERENCE.md and
        config/default.toml in the same commit.
        """
        cfg = DecoderConfig()
        assert cfg.lookahead_records == 2, "L2-SYN-026 default"
        assert cfg.detect_records == 8, "L2-DEC-015 default"
        assert DEFAULT_LOOKAHEAD_RECORDS == 2


class TestConfigPathValidation:
    """`--config` is operator-supplied, so the path is validated before it is
    read (`pythonsecurity:S8707`). `exists()` alone is not enough — it is true
    for directories and device files. Held to the same behavior and message as
    the Rust loader's `load_config`."""

    @pytest.mark.requirement("L2-CFG-001")
    def test_missing_path_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(tmp_path / "nope.toml")

    @pytest.mark.requirement("L2-CFG-001")
    def test_directory_is_rejected_not_read(self, tmp_path: Path) -> None:
        """A directory passes `exists()`; without the regular-file check this
        surfaced as an OS-level `IsADirectoryError` traceback instead of a
        config error."""
        d = tmp_path / "conf.d"
        d.mkdir()
        with pytest.raises(ValueError, match="not a regular file"):
            load_config(d)

    @pytest.mark.requirement("L2-CFG-001")
    def test_string_values_are_data_never_interpolated(self, tmp_path: Path) -> None:
        """Config values are TOML *data*. Shell, make and cmd expansion syntax is
        stored verbatim — loading never spawns a subprocess, expands an
        environment variable, or resolves an include. Pins the
        `docs/CONFIG-REFERENCE.md` "Trust boundary" claim on the one key that
        takes a free-form string."""
        literal = "$(whoami)${HOME}`id`%PATH%"
        cfg = tmp_path / "literal.toml"
        cfg.write_text(f'[mux]\ndelimiter = "{literal}"\n', encoding="utf-8")
        assert load_config(cfg).mux_delimiter == literal

    @pytest.mark.requirement("L2-CFG-001")
    def test_traversal_segments_resolve_and_load(self, tmp_path: Path) -> None:
        """A path containing `..` is ordinary input, not an attack to block: the
        config location is unrestricted by design (see "Trust boundary"). If that
        is ever reversed, this test fails and forces the doc to change with it."""
        nested = tmp_path / "sub"
        nested.mkdir()
        cfg = nested / "site.toml"
        cfg.write_text("[decode]\nstrict = true\n", encoding="utf-8")
        assert load_config(nested / ".." / "sub" / "site.toml").strict is True


class TestMaxSortGroupKey:
    """`[output] max_sort_group` — the L2-WRT-022 canonical-order run cap.
    Held to the same value and validation as the Rust loader (L3-WRT-003) so a
    shared config file behaves identically on both."""

    @pytest.mark.requirement("L2-WRT-022", "L3-WRT-003", "L2-CFG-001")
    def test_parses_value(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "c.toml"
        cfg_file.write_text("[output]\nmax_sort_group = 64\n")
        assert load_config(cfg_file).max_sort_group == 64

    @pytest.mark.requirement("L2-WRT-022")
    def test_defaults_when_absent(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "c.toml"
        cfg_file.write_text("[output]\nno_clobber = true\n")
        assert load_config(cfg_file).max_sort_group == 4096

    @pytest.mark.requirement("L2-WRT-022")
    def test_accepts_documented_off_value_and_upper_bound(self, tmp_path: Path) -> None:
        for value, expected in ((1, 1), (1_048_576, 1_048_576)):
            cfg_file = tmp_path / f"c{value}.toml"
            cfg_file.write_text(f"[output]\nmax_sort_group = {value}\n")
            assert load_config(cfg_file).max_sort_group == expected

    @pytest.mark.requirement("L2-WRT-022", "L2-CFG-010", "L3-WRT-003")
    @pytest.mark.parametrize("bad", ["0", "1048577", "-5", "true", '"64"'])
    def test_rejects_out_of_range_and_wrong_type(self, tmp_path: Path, bad: str) -> None:
        cfg_file = tmp_path / "c.toml"
        cfg_file.write_text(f"[output]\nmax_sort_group = {bad}\n")
        with pytest.raises(ValueError, match="max_sort_group"):
            load_config(cfg_file)

    @pytest.mark.requirement("L2-CFG-009")
    def test_key_is_recognized_no_unknown_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "c.toml"
        cfg_file.write_text("[output]\nmax_sort_group = 8\n")
        with caplog.at_level(logging.WARNING):
            load_config(cfg_file)
        assert not any("max_sort_group" in rec.getMessage() for rec in caplog.records)


class TestDeltaScopeKey:
    """`[merge] delta_scope` (L2-MRG-005). Same names, default, and rejection
    behavior as the Rust loader (L3-WRT-004)."""

    @pytest.mark.requirement("L2-MRG-005", "L3-WRT-004")
    def test_defaults_to_per_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text("[merge]\ncollapse_duplicates = true\n")
        assert load_config(cfg).delta_scope == DeltaScope.PER_FILE

    @pytest.mark.requirement("L2-MRG-005", "L3-WRT-004")
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("per-file", DeltaScope.PER_FILE),
            ("global", DeltaScope.GLOBAL),
            ("Per-File", DeltaScope.PER_FILE),
            ("GLOBAL", DeltaScope.GLOBAL),
        ],
    )
    def test_parses_both_names_case_insensitively(
        self, tmp_path: Path, value: str, expected: DeltaScope
    ) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text(f'[merge]\ndelta_scope = "{value}"\n')
        assert load_config(cfg).delta_scope == expected

    @pytest.mark.requirement("L2-MRG-005", "L2-CFG-010")
    @pytest.mark.parametrize("bad", ['"whole"', '"per_file"', "1", "true"])
    def test_rejects_unknown_or_non_string(self, tmp_path: Path, bad: str) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text(f"[merge]\ndelta_scope = {bad}\n")
        with pytest.raises(ValueError, match="delta_scope"):
            load_config(cfg)
