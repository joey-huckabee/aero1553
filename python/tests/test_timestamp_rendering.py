"""Tests for operator-selectable ``TIME_STAMP`` rendering (v3.0.0).

Mirrors the Rust coverage in ``rust/src/models.rs`` and ``rust/tests/cli.rs``.
The leap-year matrix is the centre of this module: the same day-of-year lands
one day apart either side of a leap day, which is the entire reason the
calendar renderings need a year at all, and the defect class the rest of the
feature is built to refuse.
"""

from __future__ import annotations

import datetime
import io
from collections.abc import Callable
from pathlib import Path

import pytest

from aero1553 import MieFileReader, columns
from aero1553.cli import EXIT_NO_RECORDS, EXIT_OK, EXIT_USAGE, main
from aero1553.models import (
    YEAR_MAX,
    YEAR_MIN,
    CalendarUnavailableError,
    IrigTimestamp,
    OutputTimeFormat,
    StandardTimestamp,
    TimeRender,
    day_of_year_to_month_day,
    format_utc_offset,
    is_leap_year,
    parse_output_time_format,
)
from aero1553.writer import WriteOptions, message_to_row, write_csv, write_csv_split
from tests.conftest import RunCli, conformance_input

DOY_RENDER = TimeRender()


def _sample_irig(**overrides: object) -> IrigTimestamp:
    fields: dict[str, object] = {
        "day": 192,
        "hour": 15,
        "minute": 54,
        "second": 50,
        "microsecond": 456_225,
        "freerun": False,
    }
    fields.update(overrides)
    return IrigTimestamp(**fields)  # type: ignore[arg-type]


class TestCalendarArithmetic:
    """Day-of-year resolution and the leap-year rule (L2-WRT-025)."""

    @pytest.mark.requirement("L2-WRT-025")
    def test_leap_year_rule_is_proleptic_gregorian(self) -> None:
        for year in (1996, 2000, 2004, 2020, 2024, 2028, 1600):
            assert is_leap_year(year), f"{year} should be a leap year"
        for year in (1900, 1999, 2001, 2025, 2026, 2027, 2100, 2200, 2300):
            assert not is_leap_year(year), f"{year} should be a common year"

    @pytest.mark.requirement("L2-WRT-025", "L2-WRT-026")
    def test_day_192_shifts_by_one_across_leap_and_common_years(self) -> None:
        """The example from the format docs, and the hazard in one line."""
        assert day_of_year_to_month_day(2024, 192) == (7, 10)
        assert day_of_year_to_month_day(2028, 192) == (7, 10)
        assert day_of_year_to_month_day(2025, 192) == (7, 11)
        assert day_of_year_to_month_day(2026, 192) == (7, 11)

    @pytest.mark.requirement("L2-WRT-025")
    def test_month_boundaries_resolve(self) -> None:
        # Common year: day 59 is Feb 28, day 60 is Mar 1.
        assert day_of_year_to_month_day(2026, 1) == (1, 1)
        assert day_of_year_to_month_day(2026, 31) == (1, 31)
        assert day_of_year_to_month_day(2026, 32) == (2, 1)
        assert day_of_year_to_month_day(2026, 59) == (2, 28)
        assert day_of_year_to_month_day(2026, 60) == (3, 1)
        assert day_of_year_to_month_day(2026, 365) == (12, 31)

        # Leap year: 60 is the leap day itself; everything after shifts.
        assert day_of_year_to_month_day(2024, 59) == (2, 28)
        assert day_of_year_to_month_day(2024, 60) == (2, 29)
        assert day_of_year_to_month_day(2024, 61) == (3, 1)
        assert day_of_year_to_month_day(2024, 366) == (12, 31)

    @pytest.mark.requirement("L2-WRT-026")
    def test_day_366_has_no_date_in_a_common_year(self) -> None:
        assert day_of_year_to_month_day(2026, 366) is None
        assert day_of_year_to_month_day(1900, 366) is None
        assert day_of_year_to_month_day(2024, 366) == (12, 31)

        for year in (2024, 2026):
            assert day_of_year_to_month_day(year, 0) is None
            assert day_of_year_to_month_day(year, 367) is None
            assert day_of_year_to_month_day(year, 100_000) is None

    @pytest.mark.requirement("L2-WRT-025")
    @pytest.mark.parametrize("year", [1, 4, 100, 400, 1900, 1999, 2000, 2024, 2026, 9999])
    def test_every_day_matches_the_standard_library(self, year: int) -> None:
        """Exhaustive cross-check against :mod:`datetime` for a spread of years.

        The implementation is hand-rolled (C++11 has no date type, so all three
        share one rule rather than three library behaviours). This is what
        pins the hand-rolled version to a known-correct calendar.
        """
        length = 366 if is_leap_year(year) else 365
        for day in range(1, length + 1):
            expected = datetime.date(year, 1, 1) + datetime.timedelta(days=day - 1)
            assert day_of_year_to_month_day(year, day) == (expected.month, expected.day)
        assert day_of_year_to_month_day(year, length + 1) is None

    @pytest.mark.requirement("L2-WRT-025")
    def test_utc_offset_designator_forms(self) -> None:
        assert format_utc_offset(0) == "Z"
        assert format_utc_offset(-300) == "-05:00"
        assert format_utc_offset(330) == "+05:30"
        assert format_utc_offset(60) == "+01:00"
        assert format_utc_offset(-1) == "-00:01"
        assert format_utc_offset(1439) == "+23:59"


class TestRendering:
    """The three renderings themselves (L2-WRT-025)."""

    @pytest.mark.requirement("L2-WRT-011", "L2-WRT-025")
    def test_doy_is_unaffected_by_year_or_offset(self) -> None:
        """The default must stay byte-identical -- that is what keeps a no-flag
        decode vendor-diffable (L1-OUT-004), and year/offset are inert under it
        (L2-WRT-026 clause 5)."""
        ts = _sample_irig()
        expected = "192:15:54:50.456225"
        assert ts.format() == expected
        assert ts.format_with(DOY_RENDER) == expected
        noisy = TimeRender(format=OutputTimeFormat.DOY, year=2024, utc_offset_minutes=-300)
        assert ts.format_with(noisy) == expected

    @pytest.mark.requirement("L2-WRT-025")
    @pytest.mark.parametrize(
        ("fmt", "year", "offset", "expected"),
        [
            (OutputTimeFormat.ISO, 2024, 0, "2024-07-10T15:54:50.456225Z"),
            (OutputTimeFormat.ISO, 2026, 0, "2026-07-11T15:54:50.456225Z"),
            (OutputTimeFormat.ISO, 2026, -300, "2026-07-11T15:54:50.456225-05:00"),
            (OutputTimeFormat.ISO, 2026, 330, "2026-07-11T15:54:50.456225+05:30"),
            (OutputTimeFormat.DOM, 2024, 0, "10:15:54:50.456225"),
            (OutputTimeFormat.DOM, 2026, 0, "11:15:54:50.456225"),
        ],
    )
    def test_calendar_renderings(
        self, fmt: OutputTimeFormat, year: int, offset: int, expected: str
    ) -> None:
        render = TimeRender(format=fmt, year=year, utc_offset_minutes=offset)
        assert _sample_irig().format_with(render) == expected

    @pytest.mark.requirement("L2-DEC-014", "L2-WRT-025")
    @pytest.mark.parametrize("micro", [0, 1, 999_999, 1_000_000, 1_234_567])
    def test_all_renderings_emit_exactly_six_microsecond_digits(self, micro: int) -> None:
        """The wider ISO cell does not relax L2-DEC-014."""
        ts = _sample_irig(microsecond=micro)
        for fmt in OutputTimeFormat:
            rendered = ts.format_with(TimeRender(format=fmt, year=2026))
            fraction = rendered.rsplit(".", 1)[1]
            digits = 0
            for char in fraction:
                if not char.isdigit():
                    break
                digits += 1
            assert digits == 6, f"{fmt.name} rendered {rendered!r}"

    @pytest.mark.requirement("L2-CLI-018", "L2-CFG-012")
    def test_names_parse_case_insensitively(self) -> None:
        for name, expected in (
            ("doy", OutputTimeFormat.DOY),
            ("DOY", OutputTimeFormat.DOY),
            ("Iso", OutputTimeFormat.ISO),
            ("dom", OutputTimeFormat.DOM),
            ("DoM", OutputTimeFormat.DOM),
        ):
            assert parse_output_time_format(name) is expected
        for name in ("", "day", "iso8601", "doy ", "elapsed"):
            with pytest.raises(ValueError, match="output_time_format"):
                parse_output_time_format(name)

        assert not OutputTimeFormat.DOY.needs_calendar()
        assert OutputTimeFormat.ISO.needs_calendar()
        assert OutputTimeFormat.DOM.needs_calendar()


class TestRefusals:
    """L2-WRT-026: every unmet precondition is refused, never approximated."""

    @pytest.mark.requirement("L2-WRT-026")
    @pytest.mark.parametrize("fmt", [OutputTimeFormat.ISO, OutputTimeFormat.DOM])
    def test_missing_year_and_impossible_day_are_refused(self, fmt: OutputTimeFormat) -> None:
        sample = _sample_irig()
        no_year = TimeRender(format=fmt, year=None)
        with pytest.raises(CalendarUnavailableError, match="no year"):
            sample.format_with(no_year)

        leap_day = _sample_irig(day=366)
        non_leap = TimeRender(format=fmt, year=2026)
        with pytest.raises(CalendarUnavailableError, match="does not exist in 2026"):
            leap_day.format_with(non_leap)

        # The same record renders once the year actually has that day.
        assert leap_day.format_with(TimeRender(format=fmt, year=2024))

        # `doy` never needs a calendar, so it never refuses.
        assert leap_day.format_with(DOY_RENDER)

    @pytest.mark.requirement("L2-WRT-025", "L2-WRT-026")
    def test_standard_counter_refuses_calendar_renderings(self) -> None:
        """A free-running counter has no epoch, so no year places it on a
        calendar -- and emitting hex into a column the operator asked to be
        ISO-8601 would be its own kind of lie."""
        ts = StandardTimestamp(raw_value=100_000, upper_word=0x0001, lower_word=0x86A0)
        assert ts.format_with(DOY_RENDER) == "0x000186A0"
        for fmt in (OutputTimeFormat.ISO, OutputTimeFormat.DOM):
            render = TimeRender(format=fmt, year=2026)
            with pytest.raises(CalendarUnavailableError, match="free-running counter"):
                ts.format_with(render)

    @pytest.mark.requirement("L2-WRT-026")
    def test_freerun_irig_refuses_calendar_renderings(self) -> None:
        """The most dangerous of the three: freerun fields are calendar-shaped
        but not calendar-anchored, so they would render as an ordinary date."""
        freerun = _sample_irig(freerun=True)
        assert freerun.format_with(DOY_RENDER) == "192:15:54:50.456225"
        for fmt in (OutputTimeFormat.ISO, OutputTimeFormat.DOM):
            render = TimeRender(format=fmt, year=2026)
            with pytest.raises(CalendarUnavailableError, match="freerun"):
                freerun.format_with(render)
            # Calendar-locked, the same instant renders fine -- so the refusal
            # is about the freerun bit and nothing else.
            assert _sample_irig().format_with(render)


class TestCliSurface:
    """The four flags and the retired one (L2-CLI-018 / L2-CLI-019)."""

    @staticmethod
    def _first_row(capfd: pytest.CaptureFixture[str]) -> str:
        lines = capfd.readouterr().out.splitlines()
        assert len(lines) > 1, "expected a header and at least one data row"
        return lines[1]

    @pytest.mark.requirement("L2-WRT-011", "L2-WRT-025")
    def test_default_rendering_is_unchanged(
        self, tmp_mie_file: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["decode", str(tmp_mie_file)]) == EXIT_OK
        assert self._first_row(capfd).startswith("192:15:54:50.456225,")

    @pytest.mark.requirement("L2-WRT-025", "L2-CLI-018")
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["--output-time-format", "iso", "--year", "2026"], "2026-07-11T15:54:50.456225Z"),
            (["--output-time-format", "iso", "--year", "2024"], "2024-07-10T15:54:50.456225Z"),
            (["--output-time-format", "dom", "--year", "2026"], "11:15:54:50.456225"),
            (["--output-time-format", "dom", "--year", "2024"], "10:15:54:50.456225"),
            (
                ["--output-time-format", "iso", "--year", "2026", "--utc-offset=-05:00"],
                "2026-07-11T15:54:50.456225-05:00",
            ),
        ],
    )
    def test_renderings_through_the_cli(
        self,
        tmp_mie_file: Path,
        capfd: pytest.CaptureFixture[str],
        args: list[str],
        expected: str,
    ) -> None:
        assert main(["decode", str(tmp_mie_file), *args]) == EXIT_OK
        assert self._first_row(capfd).startswith(f"{expected},")

    @pytest.mark.requirement("L2-WRT-026", "L2-CLI-018")
    @pytest.mark.parametrize("fmt", ["iso", "dom"])
    def test_calendar_rendering_without_a_year_is_a_usage_error(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str], fmt: str
    ) -> None:
        output = tmp_path / f"out-{fmt}.csv"
        code = main(["decode", str(tmp_mie_file), "-o", str(output), "--output-time-format", fmt])
        assert code == EXIT_USAGE
        stderr = capfd.readouterr().err
        assert "--year" in stderr, "must name the flag"
        assert "[output] year" in stderr, "must name the config key too"
        assert not output.exists(), "nothing may be written when the year is missing"

    @pytest.mark.requirement("L2-WRT-026")
    def test_doy_ignores_year_and_offset(
        self, tmp_mie_file: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "decode",
                str(tmp_mie_file),
                "--output-time-format",
                "doy",
                "--year",
                "2024",
                "--utc-offset=-05:00",
            ]
        )
        assert code == EXIT_OK
        assert self._first_row(capfd).startswith("192:15:54:50.456225,")

    @pytest.mark.requirement("L2-CLI-018")
    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("--output-time-format", "elapsed"),
            ("--output-time-format", ""),
            ("--year", "0"),
            ("--year", str(YEAR_MAX + 1)),
            ("--year", "-1"),
            ("--year", "twenty"),
            ("--utc-offset", "+24:00"),
            ("--utc-offset", "+05:60"),
            ("--utc-offset", "+5:00"),
            ("--utc-offset", "0500"),
            ("--utc-offset", "PST"),
        ],
    )
    def test_values_are_validated_at_parse_time(
        self,
        tmp_mie_file: Path,
        capfd: pytest.CaptureFixture[str],
        flag: str,
        value: str,
    ) -> None:
        assert main(["decode", str(tmp_mie_file), flag, value]) == EXIT_USAGE
        assert flag in capfd.readouterr().err

    @pytest.mark.requirement("L2-CLI-018")
    def test_year_bounds_are_accepted_at_the_edges(
        self, tmp_mie_file: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        for year in (YEAR_MIN, YEAR_MAX):
            code = main(
                ["decode", str(tmp_mie_file), "--output-time-format", "iso", "--year", str(year)]
            )
            assert code == EXIT_OK
            assert self._first_row(capfd).startswith(f"{year:04d}-")

    @pytest.mark.requirement("L2-CLI-019")
    def test_retired_time_format_flag_names_both_replacements(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Unlike ``--inline-errors`` this flag has a successor -- two of them --
        so the diagnostic must say which is which. That is the one question a
        generic "unrecognized arguments" cannot answer."""
        output = tmp_path / "out.csv"
        code = main(["decode", str(tmp_mie_file), "-o", str(output), "--time-format", "irig"])
        assert code == EXIT_USAGE
        stderr = capfd.readouterr().err
        for expected in ("--time-format", "--input-time-format", "--output-time-format"):
            assert expected in stderr, f"diagnostic should mention {expected}"
        assert not output.exists(), "no output on a retired-flag usage error"

    @pytest.mark.requirement("L2-CLI-018")
    def test_help_advertises_the_timestamp_flags(self, capfd: pytest.CaptureFixture[str]) -> None:
        """The cross-implementation parity gate scrapes ``--help``, so a flag
        the parser accepts but help omits fails the conformance run. The
        retired flag is deliberately NOT advertised."""
        assert main(["decode", "--help"]) == EXIT_OK
        stdout = capfd.readouterr().out
        for flag in (
            "--input-time-format",
            "--output-time-format",
            "--year",
            "--utc-offset",
        ):
            assert flag in stdout, f"help must advertise {flag}"
        assert "--time-format " not in stdout
        assert "--time-format\n" not in stdout


class TestConfigSurface:
    """The three new keys and the retired one (L2-CFG-012)."""

    @pytest.mark.requirement("L2-CFG-012")
    def test_retired_config_key_is_rejected_not_ignored(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The generic unknown-key rule only WARNs, which for a *rename* would
        silently discard a forced format and revert to auto-detection."""
        config = tmp_path / "old.toml"
        config.write_text('[decode]\ntime_format = "irig"\n', encoding="utf-8")
        code = main(["--config", str(config), "decode", str(tmp_mie_file)])
        assert code != EXIT_OK
        stderr = capfd.readouterr().err
        assert "decode.input_time_format" in stderr
        assert "output.output_time_format" in stderr

    @pytest.mark.requirement("L2-CFG-012", "L2-WRT-025")
    def test_config_supplies_the_rendering_and_the_year(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A site config alone is enough -- no flags on the command line."""
        config = tmp_path / "site.toml"
        config.write_text(
            '[output]\noutput_time_format = "iso"\nyear = 2024\nutc_offset = "-05:00"\n',
            encoding="utf-8",
        )
        code = main(["--config", str(config), "decode", str(tmp_mie_file)])
        assert code == EXIT_OK
        row = capfd.readouterr().out.splitlines()[1]
        assert row.startswith("2024-07-10T15:54:50.456225-05:00,")

    @pytest.mark.requirement("L2-CFG-012")
    def test_cli_year_overrides_the_config_year(
        self, tmp_mie_file: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "site.toml"
        config.write_text('[output]\noutput_time_format = "iso"\nyear = 2024\n', encoding="utf-8")
        code = main(["--config", str(config), "decode", str(tmp_mie_file), "--year", "2026"])
        assert code == EXIT_OK
        assert capfd.readouterr().out.splitlines()[1].startswith("2026-07-11T")

    @pytest.mark.requirement("L2-CFG-012")
    @pytest.mark.parametrize(
        "body",
        [
            '[output]\noutput_time_format = "elapsed"\n',
            "[output]\noutput_time_format = 1\n",
            "[output]\nyear = 0\n",
            "[output]\nyear = 10000\n",
            '[output]\nyear = "2026"\n',
            "[output]\nyear = true\n",
            '[output]\nutc_offset = "+24:00"\n',
            "[output]\nutc_offset = 5\n",
        ],
    )
    def test_invalid_values_are_rejected_at_load_time(
        self, tmp_mie_file: Path, tmp_path: Path, body: str
    ) -> None:
        config = tmp_path / "bad.toml"
        config.write_text(body, encoding="utf-8")
        assert main(["--config", str(config), "decode", str(tmp_mie_file)]) != EXIT_OK


class TestNonCalendarRecordings:
    """L2-WRT-026 clause 2 through the CLI: exit 2, nothing written."""

    @pytest.mark.requirement("L2-WRT-026")
    def test_iso_on_a_standard_recording_exits_two_and_writes_nothing(
        self,
        tmp_path: Path,
        standard_timestamp_data: bytes,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        source = tmp_path / "std.mie"
        source.write_bytes(standard_timestamp_data)
        output = tmp_path / "out.csv"
        code = main(
            [
                "decode",
                str(source),
                "-o",
                str(output),
                "--output-time-format",
                "iso",
                "--year",
                "2026",
            ]
        )
        assert code == EXIT_NO_RECORDS
        assert "free-running counter" in capfd.readouterr().err
        assert not output.exists()

    @pytest.mark.requirement("L2-WRT-026")
    def test_doy_on_a_standard_recording_still_works(
        self,
        tmp_path: Path,
        standard_timestamp_data: bytes,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        source = tmp_path / "std.mie"
        source.write_bytes(standard_timestamp_data)
        assert main(["decode", str(source)]) == EXIT_OK
        assert capfd.readouterr().out.splitlines()[1].startswith("0x")


class TestAdvisoryLevel:
    """L2-LOG-002: the day-of-year advisory follows the rendering."""

    @pytest.mark.requirement("L2-LOG-001", "L2-LOG-002")
    def test_advisory_escalates_under_a_calendar_rendering(
        self, tmp_mie_file: Path, run_cli: RunCli
    ) -> None:
        """`INFO` under ``doy``, where a skewed day is visibly a day number;
        `WARN` under a calendar rendering, where the same skew is resolved
        into something that reads as a fact.

        Read off the CLI's stderr: each log line opens with its level
        (``INFO [aero1553::reader] ...``), and ``--log-level DEBUG`` lets every
        level through so the escalation is what decides the prefix.
        """

        def advisory_levels(argv: list[str]) -> list[str]:
            result = run_cli(["--log-level", "DEBUG", *argv])
            assert result.rc == EXIT_OK
            return [
                line.split(" ", 1)[0] for line in result.err.splitlines() if "day-of-year" in line
            ]

        assert advisory_levels(["decode", str(tmp_mie_file)]) == ["INFO"]

        for fmt in ("iso", "dom"):
            levels = advisory_levels(
                ["decode", str(tmp_mie_file), "--output-time-format", fmt, "--year", "2026"]
            )
            assert levels == ["WARN"], f"{fmt} should escalate the advisory"

    @pytest.mark.requirement("L2-LOG-002")
    def test_opt_out_still_wins_under_a_calendar_rendering(
        self, tmp_mie_file: Path, run_cli: RunCli
    ) -> None:
        """``--no-irig-day-advisory`` silences the advisory even where a
        calendar rendering would escalate it to WARN.

        The same run without the flag is the control: it must show the
        advisory, so its absence below is the flag's doing and not a run that
        never logged at all.
        """
        argv = [
            "--log-level",
            "DEBUG",
            "decode",
            str(tmp_mie_file),
            "--output-time-format",
            "iso",
            "--year",
            "2026",
        ]
        assert "day-of-year" in run_cli(argv).err

        result = run_cli(["--no-irig-day-advisory", *argv])
        assert result.rc == EXIT_OK
        assert "day-of-year" not in result.err


# ---------------------------------------------------------------------------
# L3-PY-024: every entry point range-checks year and offset, through the core
# ---------------------------------------------------------------------------

#: (year, utc_offset_minutes, the ValueError's message) for values out of range.
#: -32768 is i16::MIN, the value a 16-bit `abs()` check let through; 40000 and
#: 2**40 do not fit 16 bits at all and must still read as out of range.
_OUT_OF_RANGE = [
    pytest.param(0, 0, r"year must be in \[1, 9999\], got 0", id="year-0"),
    pytest.param(10_000, 0, "got 10000", id="year-10000"),
    pytest.param(-1, 0, "got -1", id="year-negative"),
    pytest.param(2**40, 0, f"got {2**40}", id="year-huge"),
    pytest.param(
        2026, 1440, r"utc_offset_minutes must be in \[-1439, 1439\], got 1440", id="offset-1440"
    ),
    pytest.param(2026, -1440, "got -1440", id="offset-minus-1440"),
    pytest.param(2026, -32768, "got -32768", id="offset-i16-min"),
    pytest.param(2026, 40_000, "got 40000", id="offset-beyond-i16"),
]


def _entry_points(tmp_path: Path) -> dict[str, Callable[[int, int], object]]:
    """Each Python entry point that takes a year and an offset, as one call."""
    path = tmp_path / "rec.mie"
    path.write_bytes(conformance_input("basic-multi-record"))
    msg = next(iter(MieFileReader(path)))

    def render(year: int, offset: int) -> TimeRender:
        return TimeRender(OutputTimeFormat.ISO, year, offset)

    def opts(year: int, offset: int) -> WriteOptions:
        return WriteOptions(time_render=render(year, offset))

    return {
        "format_with": lambda y, o: msg.timestamp.format_with(render(y, o)),
        "message_to_row": lambda y, o: message_to_row(msg, render(y, o)),
        "write_csv": lambda y, o: write_csv([msg], tmp_path / "w.csv", opts(y, o)),
        "write_csv-stream": lambda y, o: write_csv([msg], io.StringIO(), opts(y, o)),
        "write_csv_split": lambda y, o: write_csv_split([msg], tmp_path / "s.csv", opts(y, o)),
        "columns": lambda y, o: columns([msg], year=y, utc_offset_minutes=o),
        "to_dict": lambda y, o: msg.to_dict(year=y, utc_offset_minutes=o),
    }


@pytest.mark.requirement("L3-PY-024")
@pytest.mark.requirement("L2-WRT-026")
@pytest.mark.parametrize(("year", "offset", "message"), _OUT_OF_RANGE)
def test_every_entry_point_rejects_an_out_of_range_year_or_offset(
    tmp_path: Path, year: int, offset: int, message: str
) -> None:
    """A bad value is a ValueError at every entry point, before any output.

    Before the core check was shared, a TimeRender went to the formatter
    unchecked -- year 10000 rendered ``10000-07-10T...`` and offset 5000
    rendered ``+83:20`` -- and only ``columns`` checked, through a copy that
    passed -32768.
    """
    for name, call in _entry_points(tmp_path).items():
        with pytest.raises(ValueError, match=message):
            call(year, offset)
        assert not list(tmp_path.glob("*.csv*")), f"{name} wrote output"


@pytest.mark.requirement("L3-PY-024")
@pytest.mark.parametrize("year", [YEAR_MIN, YEAR_MAX])
@pytest.mark.parametrize("offset", [-1439, 0, 1439])
def test_every_entry_point_accepts_the_range_limits(tmp_path: Path, year: int, offset: int) -> None:
    for call in _entry_points(tmp_path).values():
        call(year, offset)


@pytest.mark.requirement("L3-PY-024")
@pytest.mark.parametrize("year", [0, 10_000, 2**40])
def test_reader_rejects_an_out_of_range_calendar_year(tmp_path: Path, year: int) -> None:
    path = tmp_path / "rec.mie"
    path.write_bytes(conformance_input("basic-multi-record"))
    with pytest.raises(ValueError, match=f"year must be in .*, got {year}"):
        MieFileReader(path, calendar_year=year)
    assert list(MieFileReader(path, calendar_year=YEAR_MAX))
