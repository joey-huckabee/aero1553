"""Unit tests for the decode CLI helper functions in ``aero1553.cli``.

White-box tests for the helpers extracted from ``_run_decode`` (the override
builders, validators, exit-code classifiers, and the merge output-collision
check). End-to-end behavior is covered by ``test_e2e.py`` / ``test_merge.py``;
these exercise each helper branch in isolation so the decomposition is fully
covered and individually verifiable.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from aero1553 import cli
from aero1553.cli import (
    EXIT_MERGE_INCOMPATIBLE,
    EXIT_NO_RECORDS,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_SYNC_LOSS,
)
from aero1553.exceptions import (
    MieClobberRefusedError,
    MieHomogeneousPayloadError,
    MieIncompatibleMergeInputsError,
    MieInputOutputCollisionError,
    MieNonMonotonicInputError,
    MieNoValidRecordsError,
    MieRecordError,
    MieTimestampFormatMismatchError,
    MieUnrecoverableSyncLossError,
    MieWriterError,
)
from aero1553.models import ErrorMode
from tests.conftest import normal_record_rt15_sa11_us

# ── validators ─────────────────────────────────────────────────────────────


class TestValidators:
    def test_int_range_accepts_bounds_and_interior(self) -> None:
        assert cli._validate_int_range(1, "--x", 1, 10) == 1
        assert cli._validate_int_range(10, "--x", 1, 10) == 10
        assert cli._validate_int_range(5, "--x", 1, 10) == 5

    @pytest.mark.parametrize("value", [0, 11, -1])
    def test_int_range_rejects_out_of_range(self, value: int) -> None:
        with pytest.raises(ValueError, match=r"invalid --x: .*; valid range: \[1, 10\]"):
            cli._validate_int_range(value, "--x", 1, 10)

    def test_positive_finite_accepts(self) -> None:
        assert cli._validate_positive_finite(1.0, "--hz") == 1.0

    @pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
    def test_positive_finite_rejects(self, value: float) -> None:
        with pytest.raises(ValueError, match="must be a finite value greater than 0"):
            cli._validate_positive_finite(value, "--hz")

    def test_nonempty_accepts(self) -> None:
        assert cli._validate_nonempty(".", "--mux-delimiter") == "."

    def test_nonempty_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must be a non-empty string"):
            cli._validate_nonempty("", "--mux-delimiter")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0", 0),
            ("16", 16),
            ("0x10", 16),
            ("0X10", 16),
            ("0o20", 16),
            ("0b10000", 16),
            ("  16  ", 16),
        ],
    )
    def test_nonneg_int_accepts_decimal_and_prefixed(self, text: str, expected: int) -> None:
        assert cli._nonneg_int(text) == expected

    @pytest.mark.parametrize("text", ["-1", "-0x10", "foo", "0x", "", "1.5"])
    def test_nonneg_int_rejects_invalid(self, text: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="non-negative integer"):
            cli._nonneg_int(text)

    @pytest.mark.parametrize("flag", ["--offset", "--length", "--records"])
    def test_dump_numeric_args_accept_hex(self, flag: str) -> None:
        # Every numeric dump argument accepts 0x hex identically (previously
        # --records was decimal-only, an internal inconsistency).
        args = cli.build_parser().parse_args(["dump", "f.mie", flag, "0x10"])
        assert getattr(args, flag.lstrip("-")) == 16

    def test_log_safe_neutralizes_crlf(self) -> None:
        # S5145: user-controlled values (e.g. an input path) must not be able to
        # inject newlines into the log. CR/LF are escaped; plain text is intact.
        assert cli._log_safe("plain/path.mie") == "plain/path.mie"
        assert cli._log_safe("evil\nINJECTED") == "evil\\nINJECTED"
        assert cli._log_safe("a\r\nb") == "a\\r\\nb"
        from pathlib import PurePosixPath

        assert cli._log_safe(PurePosixPath("dir/x.mie")) == "dir/x.mie"


# ── override building ───────────────────────────────────────────────────────


def _decode_ns(**overrides: object) -> argparse.Namespace:
    """A decode-args Namespace with every override field defaulted (None/False)."""
    base: dict[str, object] = {
        "input_time_format": None,
        "output_time_format": None,
        "year": None,
        "utc_offset": None,
        "time_format": None,
        "separate_errors": False,
        "no_clobber": False,
        "allow_partial": False,
        "strict": None,
        "format": None,
        "no_mux": False,
        "mux_field": None,
        "mux_delimiter": None,
        "collapse_duplicates": None,
        "collapse_window_us": None,
        "max_sort_group": None,
        "max_collapse_survivors": None,
        "delta_scope": None,
        "detect_records": None,
        "lookahead_records": None,
        "standard_tick_rate_hz": None,
        "exclude_types": None,
        "exclude_rts": None,
        "exclude_buses": None,
        "exclude_subaddresses": None,
        "include_types": None,
        "include_rts": None,
        "include_buses": None,
        "include_subaddresses": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestBuildDecodeOverrides:
    def test_empty_namespace_yields_no_overrides(self) -> None:
        assert cli._build_decode_overrides(_decode_ns()) == {}

    def test_simple_flag_passthroughs(self) -> None:
        ov = cli._build_decode_overrides(
            _decode_ns(no_clobber=True, allow_partial=True, no_mux=True, mux_field=2)
        )
        assert ov["no_clobber"] is True
        assert ov["allow_partial"] is True
        assert ov["mux_enabled"] is False
        assert ov["mux_field"] == 2

    def test_filter_values_parsed(self) -> None:
        ov = cli._build_decode_overrides(_decode_ns(include_rts=["15", "31"]))
        assert ov["include_rts"] == [15, 31]

    def test_all_filter_branches(self) -> None:
        ov = cli._build_decode_overrides(
            _decode_ns(
                exclude_types=["0x20"],
                exclude_rts=["31"],
                exclude_buses=["B"],
                exclude_subaddresses=["1"],
                include_types=["0x02"],
                include_rts=["15"],
                include_buses=["A"],
                include_subaddresses=["11"],
            )
        )
        for key in (
            "exclude_types",
            "exclude_buses",
            "exclude_subaddresses",
            "include_types",
            "include_buses",
            "include_subaddresses",
        ):
            assert key in ov
        assert ov["exclude_rts"] == [31]
        assert ov["include_rts"] == [15]

    def test_input_time_format_and_simple_value_overrides(self) -> None:
        from aero1553.models import ErrorMode, TimestampFormat

        ov = cli._build_decode_overrides(
            _decode_ns(
                input_time_format="standard",
                separate_errors=True,
                strict=True,
                format="csv",
            )
        )
        assert ov["input_time_format"] == TimestampFormat.STANDARD
        assert ov["error_mode"] == ErrorMode.SEPARATE
        assert ov["strict"] is True
        assert ov["output_format"] == "csv"

    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("IRIG", "IRIG"),
            ("Irig", "IRIG"),
            ("AUTO", "AUTO"),
            ("Standard", "STANDARD"),
        ],
    )
    def test_input_time_format_is_case_insensitive(self, spelling: str, expected: str) -> None:
        from aero1553.models import TimestampFormat

        ov = cli._build_decode_overrides(_decode_ns(input_time_format=spelling))
        assert ov["input_time_format"] == TimestampFormat[expected]

    def test_input_time_format_invalid_raises_value_error(self) -> None:
        # Build the namespace outside the `raises` block so only the call under
        # test can satisfy it -- otherwise a ValueError from the fixture helper
        # would pass this test for the wrong reason (S5778).
        ns = _decode_ns(input_time_format="bogus")
        # L2-CLI-018: a bad flag value names the FLAG, not the config key.
        with pytest.raises(ValueError, match="invalid --input-time-format"):
            cli._build_decode_overrides(ns)

    def test_detect_and_lookahead_valid_bounds(self) -> None:
        from aero1553.config import DETECT_RECORDS_MIN, LOOKAHEAD_RECORDS_MIN

        ov = cli._build_decode_overrides(
            _decode_ns(
                detect_records=DETECT_RECORDS_MIN,
                lookahead_records=LOOKAHEAD_RECORDS_MIN,
                standard_tick_rate_hz=1_000_000.0,
            )
        )
        assert ov["detect_records"] == DETECT_RECORDS_MIN
        assert ov["lookahead_records"] == LOOKAHEAD_RECORDS_MIN
        assert ov["standard_tick_rate_hz"] == 1_000_000.0

    def test_bad_filter_value_raises(self) -> None:
        ns = _decode_ns(include_rts=["999"])
        with pytest.raises(ValueError):
            cli._build_decode_overrides(ns)

    def test_empty_mux_delimiter_raises(self) -> None:
        ns = _decode_ns(mux_delimiter="")
        with pytest.raises(ValueError, match="must be a non-empty string"):
            cli._build_decode_overrides(ns)

    def test_detect_records_out_of_range_raises(self) -> None:
        ns = _decode_ns(detect_records=10**9)
        with pytest.raises(ValueError, match="--detect-records"):
            cli._build_decode_overrides(ns)

    def test_standard_tick_rate_nonpositive_raises(self) -> None:
        ns = _decode_ns(standard_tick_rate_hz=0.0)
        with pytest.raises(ValueError, match="--standard-tick-rate-hz"):
            cli._build_decode_overrides(ns)


# ── error classification ────────────────────────────────────────────────────


class TestClassifyDecodeError:
    def test_incompatible_merge(self, capsys: pytest.CaptureFixture[str]) -> None:
        exc = MieIncompatibleMergeInputsError(0, "a.mie", "freerun-leading")
        assert cli._classify_decode_error(exc) == EXIT_MERGE_INCOMPATIBLE
        assert "Error:" in capsys.readouterr().err

    def test_input_output_collision(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(MieInputOutputCollisionError("p")) == EXIT_RUNTIME
        assert "Error:" in capsys.readouterr().err

    def test_clobber_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(MieClobberRefusedError("p")) == EXIT_RUNTIME
        assert "Error:" in capsys.readouterr().err

    def test_no_valid_records(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(MieNoValidRecordsError("p", 64)) == EXIT_NO_RECORDS
        assert "Error:" in capsys.readouterr().err

    def test_homogeneous_payload(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(MieHomogeneousPayloadError("p", 0, 4)) == EXIT_NO_RECORDS
        assert "Error:" in capsys.readouterr().err

    def test_timestamp_format_mismatch(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert (
            cli._classify_decode_error(MieTimestampFormatMismatchError(0, 3, 2, 8))
            == EXIT_NO_RECORDS
        )
        assert "Error:" in capsys.readouterr().err

    def test_unrecoverable_sync_loss(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(MieUnrecoverableSyncLossError(0x10, 3)) == EXIT_SYNC_LOSS
        assert "Error:" in capsys.readouterr().err

    def test_broken_pipe_returns_ok_without_print(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(BrokenPipeError()) == EXIT_OK
        assert capsys.readouterr().err == ""

    def test_writer_error_uses_distinct_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        exc = MieWriterError("stdout", OSError("disk full"))
        assert cli._classify_decode_error(exc) == EXIT_RUNTIME
        assert "Error writing output" in capsys.readouterr().err

    def test_non_monotonic_input(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._classify_decode_error(MieNonMonotonicInputError(0, "p", 5, 4)) == EXIT_RUNTIME
        assert "Error:" in capsys.readouterr().err

    def test_generic_decoder_error_falls_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A MieRecordError that is not one of the specific handled subtypes
        # hits the generic "Decode failed" arm (exit 1).
        assert cli._classify_decode_error(MieRecordError(0x20, "boom")) == EXIT_RUNTIME
        assert "Error:" in capsys.readouterr().err


# ── success classification ──────────────────────────────────────────────────


class TestClassifyDecodeSuccess:
    def test_complete(self) -> None:
        outcome = SimpleNamespace(partial=None, normal_count=3, error_count=0)
        readers = [SimpleNamespace(sync_losses=0, empty_recording=False)]
        assert cli._classify_decode_success(outcome, readers) == EXIT_OK  # type: ignore[arg-type]

    def test_partial_recovered(self) -> None:
        outcome = SimpleNamespace(partial=None, normal_count=3, error_count=0)
        readers = [
            SimpleNamespace(sync_losses=2, empty_recording=False),
            SimpleNamespace(sync_losses=1, empty_recording=False),
        ]
        assert cli._classify_decode_success(outcome, readers) == EXIT_OK  # type: ignore[arg-type]

    def test_partial_unrecoverable(self) -> None:
        outcome = SimpleNamespace(partial=Path("out.csv.partial"), normal_count=1, error_count=0)
        readers = [SimpleNamespace(sync_losses=5, empty_recording=False)]
        assert cli._classify_decode_success(outcome, readers) == EXIT_OK  # type: ignore[arg-type]

    def test_empty_recording(self, caplog: pytest.LogCaptureFixture) -> None:
        # L1-EXIT-010: every input an empty recording + zero rows written →
        # the summary line names the empty-recording class.
        outcome = SimpleNamespace(partial=None, normal_count=0, error_count=0)
        readers = [SimpleNamespace(sync_losses=0, empty_recording=True)]
        with caplog.at_level("INFO", logger="aero1553"):
            assert cli._classify_decode_success(outcome, readers) == EXIT_OK  # type: ignore[arg-type]
        assert "empty-recording" in caplog.text


# ── merge output-collision check ────────────────────────────────────────────


class TestMergeOutputCollision:
    def test_collision_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "a.mie"
        f.write_bytes(b"x")
        msg = cli._merge_output_collision(f, [tmp_path / "b.mie", f])
        assert msg is not None
        assert "resolves to merge input" in msg

    def test_no_collision_distinct_paths(self, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        assert cli._merge_output_collision(out, [tmp_path / "a.mie", tmp_path / "b.mie"]) is None


class TestCheckMergeOutputCollision:
    @staticmethod
    def _config(*, separate: bool = False, allow_partial: bool = False) -> SimpleNamespace:
        """The two resolved-config fields the guard reads.

        The guard takes the *config*, not the argparse namespace, because a site
        config file can select either mode without the flag ever being typed.
        """
        return SimpleNamespace(
            error_mode=ErrorMode.SEPARATE if separate else ErrorMode.INLINE,
            allow_partial=allow_partial,
        )

    def test_no_merge_skips(self) -> None:
        # A single-input decode (merge not requested) defers to the writer's own
        # input/output check, which runs the same target enumeration.
        args = SimpleNamespace(output=Path("out.csv"))
        assert (
            cli._check_merge_output_collision(
                args, [Path("a.mie")], self._config(), merge_requested=False
            )
            is None
        )

    def test_no_output_skips(self) -> None:
        args = SimpleNamespace(output=None)
        rc = cli._check_merge_output_collision(
            args, [Path("a.mie"), Path("b.mie")], self._config(), merge_requested=True
        )
        assert rc is None

    def test_collision_returns_runtime(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "a.mie"
        f.write_bytes(b"x")
        args = SimpleNamespace(output=f)
        rc = cli._check_merge_output_collision(
            args, [tmp_path / "b.mie", f], self._config(), merge_requested=True
        )
        assert rc == EXIT_RUNTIME
        assert "Error:" in capsys.readouterr().err

    @pytest.mark.requirement("L2-WRT-014")
    def test_derived_targets_are_checked_from_config_not_flags(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A derived commit target that names an input is a collision too.

        ``-o capture.mie --separate-errors`` derives ``capture_errors.mie``,
        which is a plausible recording name; before this the guard checked only
        the destination, the errors file committed over the input, and the run
        exited 0. The mode comes from the resolved config, so a site config
        selecting separate mode is guarded exactly like the flag.
        """
        victim = tmp_path / "capture_errors.mie"
        victim.write_bytes(b"x")
        args = SimpleNamespace(output=tmp_path / "capture.mie")

        # Inline mode never writes an errors file, so there is nothing to hit.
        assert (
            cli._check_merge_output_collision(
                args, [tmp_path / "b.mie", victim], self._config(), merge_requested=True
            )
            is None
        )

        rc = cli._check_merge_output_collision(
            args,
            [tmp_path / "b.mie", victim],
            self._config(separate=True),
            merge_requested=True,
        )
        assert rc == EXIT_RUNTIME
        assert "derived output path" in capsys.readouterr().err

    @pytest.mark.requirement("L2-WRT-014")
    def test_partial_target_is_checked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``<destination>.partial`` is a commit target under ``--allow-partial``.

        Enumerated even though a clean decode never writes one: the guard runs
        before the output is opened, and by then nobody knows whether the decode
        will lose sync.
        """
        victim = tmp_path / "out.csv.partial"
        victim.write_bytes(b"x")
        args = SimpleNamespace(output=tmp_path / "out.csv")

        assert (
            cli._check_merge_output_collision(
                args, [tmp_path / "b.mie", victim], self._config(), merge_requested=True
            )
            is None
        )

        rc = cli._check_merge_output_collision(
            args,
            [tmp_path / "b.mie", victim],
            self._config(allow_partial=True),
            merge_requested=True,
        )
        assert rc == EXIT_RUNTIME
        assert "derived output path" in capsys.readouterr().err


# ── dump broken-pipe handling (L2-WRT-018) ──────────────────────────────────


class TestRunDumpBrokenPipe:
    """``dump`` must treat a closed stdout consumer as a clean exit.

    ``aero1553 dump big.mie | head`` is the documented diagnostic workflow;
    `dump` previously had no broken-pipe guard at all, so it exited 1 with a
    traceback while `finish_dump` in ``rust/src/cli.rs`` exited 0.
    """

    @staticmethod
    def _dump_args(tmp_path: Path) -> SimpleNamespace:
        mie = tmp_path / "rec.mie"
        mie.write_bytes(normal_record_rt15_sa11_us(100))
        return SimpleNamespace(
            input=mie,
            raw=False,
            offset=0,
            length=None,
            records=None,
            config=None,
            log_level=None,
            no_irig_day_advisory=False,
        )

    def test_broken_pipe_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            "aero1553.dump.hex_dump_records",
            lambda *_a, **_k: (_ for _ in ()).throw(BrokenPipeError("consumer closed")),
        )
        assert cli._run_dump(self._dump_args(tmp_path)) == EXIT_OK
        assert "Error:" not in capsys.readouterr().err

    def test_windows_broken_pipe_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("aero1553.writer.sys.platform", "win32")
        monkeypatch.setattr(
            "aero1553.dump.hex_dump_records",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EINVAL, "Invalid argument")),
        )
        assert cli._run_dump(self._dump_args(tmp_path)) == EXIT_OK

    def test_real_write_failure_still_exits_runtime(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-pipe output error stays a runtime failure (exit 1)."""
        import aero1553.dump as dump_mod

        original = dump_mod.hex_dump_records

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        dump_mod.hex_dump_records = _boom  # type: ignore[assignment]
        try:
            assert cli._run_dump(self._dump_args(tmp_path)) == EXIT_RUNTIME
        finally:
            dump_mod.hex_dump_records = original  # type: ignore[assignment]
        assert "Error:" in capsys.readouterr().err


class TestFlagValueSyntax:
    """``--flag value`` and ``--flag=value`` are the same invocation.

    Python gets the joined spelling from :mod:`argparse` for free, which is
    precisely why it had no test for it: nothing here was written by hand, so
    nothing looked like it needed proving. The contract is cross-implementation
    (L2-CLI-015), and the other two parse it by hand -- C++ rejected
    ``--exclude-rts=`` as an unknown option where this one accepts it. These
    tests pin Python's side of the contract so a future move away from
    ``argparse``, or a flag added with a custom action, cannot drift.
    """

    VALUED: tuple[tuple[str, str], ...] = (
        ("--input-time-format", "irig"),
        ("--format", "csv"),
        ("--detect-records", "4"),
        ("--lookahead-records", "2"),
        ("--standard-tick-rate-hz", "1000000"),
        ("--max-sort-group", "64"),
        ("--mux-delimiter", "_"),
        ("--mux-field", "0"),
        ("--delta-scope", "global"),
        ("--collapse-window-us", "10"),
        ("--exclude-rts", "31"),
        ("--include-rts", "15"),
        ("--exclude-buses", "B"),
        ("--include-buses", "A"),
        ("--exclude-subaddresses", "30"),
        ("--include-subaddresses", "11"),
        ("--exclude-types", "RT_TO_RT"),
        ("--include-types", "BC_TO_RT"),
    )

    @pytest.mark.requirement("L2-CLI-015")
    @pytest.mark.parametrize(("flag", "value"), VALUED)
    def test_both_spellings_parse_identically(self, flag: str, value: str) -> None:
        """The two spellings produce the same namespace, field for field.

        Comparing namespaces rather than exit codes matters: a spelling that
        parsed but dropped its value would still exit 0.
        """
        parser = cli.build_parser()
        separated = parser.parse_args(["decode", "rec.mie", flag, value])
        joined = parser.parse_args(["decode", "rec.mie", f"{flag}={value}"])
        assert vars(separated) == vars(joined)

    @pytest.mark.requirement("L2-CLI-015")
    def test_eq_form_with_empty_value_is_an_empty_value(self) -> None:
        """``--flag=`` is the flag carrying an empty value, not a bad token.

        This is the case C++ reported as an unknown option (exit 4) while this
        implementation accepted it. An empty filter adds nothing, so the decode
        is the one that never passed the flag.

        The namespaces are deliberately NOT compared. ``argparse`` records the
        two as different values -- ``[]`` for an explicit empty list, ``None``
        for an absent flag -- which is a real distinction here and not one
        Rust can make, since its field is a plain ``Vec``. It is not
        observable: ``_merge_filter_overrides`` UNIONS CLI filters onto
        config-file filters rather than replacing them, so an empty list
        contributes nothing either way. Asserting on the namespace would pin
        an internal representation instead of the contract.
        """
        parser = cli.build_parser()
        with_flag = parser.parse_args(["decode", "rec.mie", "--exclude-rts="])
        without = parser.parse_args(["decode", "rec.mie"])

        assert with_flag.exclude_rts == []
        assert without.exclude_rts is None

        # What actually matters: neither adds an exclusion.
        assert cli._filter_overrides(with_flag).get("exclude_rts") == []
        assert "exclude_rts" not in cli._filter_overrides(without)

    @pytest.mark.requirement("L2-CLI-015")
    def test_only_the_first_equals_separates(self) -> None:
        """``--mux-delimiter==`` sets the delimiter to ``=``, not to empty."""
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "rec.mie", "--mux-delimiter=="])
        assert parsed.mux_delimiter == "="

    @pytest.mark.requirement("L2-CLI-015")
    def test_a_valueless_flag_rejects_a_joined_value(self) -> None:
        """``--no-mux=true`` is a usage error, not a way to spell "on"."""
        parser = cli.build_parser()
        for token in ("--no-mux=true", "--separate-errors=1", "--strict=false"):
            with pytest.raises(SystemExit) as excinfo:
                parser.parse_args(["decode", "rec.mie", token])
            assert excinfo.value.code != EXIT_OK

    @pytest.mark.requirement("L2-CLI-015")
    def test_a_positional_path_may_contain_an_equals(self) -> None:
        """Splitting is confined to flags; ``a=b.mie`` is an input path."""
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "a=b.mie"])
        assert [str(p) for p in parsed.inputs] == ["a=b.mie"]

    @pytest.mark.requirement("L2-CLI-015")
    def test_global_flags_accept_both_spellings(self) -> None:
        """Globals precede the subcommand and take both spellings too."""
        parser = cli.build_parser()
        separated = parser.parse_args(["--log-level", "ERROR", "decode", "rec.mie"])
        joined = parser.parse_args(["--log-level=ERROR", "decode", "rec.mie"])
        assert vars(separated) == vars(joined)
        assert joined.log_level == "ERROR"

    # Only shapes every supported interpreter agrees on. ``argparse``'s
    # negative-number matcher changed in 3.14 -- an anchored full match
    # (``^-\d+$|^-\d*\.\d+$``) became a prefix test (``-\.?\d``) -- so
    # "-5e3", "-0x5" and "-1a" are options on 3.10-3.13 and values on 3.14.
    # This project supports 3.10 through 3.14, so those shapes are outside the
    # L2-CLI-015 contract and are asserted nowhere; see
    # ``test_the_version_dependent_corner_is_not_asserted`` below.
    OPTION_LIKE: tuple[str, ...] = ("--no-mux", "--foo", "-o", "-x", "-abc", "--1")
    VALUE_LIKE: tuple[str, ...] = ("-", "-5", "-5.5", "-.5", "- x")

    @pytest.mark.requirement("L2-CLI-015")
    @pytest.mark.parametrize("token", OPTION_LIKE)
    def test_separated_form_refuses_an_option_like_value(self, token: str) -> None:
        """``--mux-delimiter --no-mux`` is a usage error, not a delimiter.

        This is where Rust and C++ used to disagree: they consumed the
        following flag as the value, so ``--no-mux`` silently never ran and
        the decode succeeded with a wrong MUX column. Both now follow this
        rule. The assertions here are what stops ``argparse``'s side of the
        contract from drifting if the parser is ever hand-rolled.
        """
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["decode", "rec.mie", "--mux-delimiter", token])
        assert excinfo.value.code != EXIT_OK

    @pytest.mark.requirement("L2-CLI-015")
    @pytest.mark.parametrize("token", VALUE_LIKE)
    def test_separated_form_accepts_the_exemptions(self, token: str) -> None:
        """A lone dash, a negative number, and a token with a space are values.

        Each exemption keeps a real invocation working: ``-o -`` writes a file
        named ``-`` (L2-CLI-005), ``--mux-field -1`` counts from the end, and
        no option is spelled with a space in it. They are ``argparse``'s rules
        -- the other two implementations copied them from here, including the
        exact negative-number pattern, rather than inventing a tidier one.
        """
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "rec.mie", "--mux-delimiter", token])
        assert parsed.mux_delimiter == token

    @pytest.mark.requirement("L2-CLI-015")
    def test_joined_form_still_takes_an_option_like_value(self) -> None:
        """The joined form is unambiguous, so it accepts what the separated
        form refuses -- and is what the refusal should point the user at."""
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "rec.mie", "--mux-delimiter=--no-mux"])
        assert parsed.mux_delimiter == "--no-mux"
        # Falsy, not ``is False``: an unpassed flag is ``None`` here (meaning
        # "not specified", so a config file still decides) rather than a bool.
        # What matters is that ``--no-mux`` did not take effect.
        assert not parsed.no_mux

    @pytest.mark.requirement("L2-CLI-015")
    def test_the_version_dependent_corner_is_not_asserted(self) -> None:
        """Pin *that* the corner is version-dependent, not which way it falls.

        ``argparse`` classifies "-5e3" as an option on Python 3.10-3.13 and as
        a value on 3.14, because the negative-number matcher went from an
        anchored full match to a prefix test. Rust and C++ follow 3.14. This
        test asserts only that the interpreter's answer matches its own
        matcher, so it passes on every supported version and would fail if a
        future release changed the rule again -- which is the signal that the
        cross-implementation choice needs revisiting.
        """
        parser = cli.build_parser()
        prefix_test = bool(parser._negative_number_matcher.match("-5e3"))

        if prefix_test:
            # 3.14+: begins like a number, therefore a value.
            parsed = parser.parse_args(["decode", "rec.mie", "--mux-delimiter", "-5e3"])
            assert parsed.mux_delimiter == "-5e3"
        else:
            # 3.13 and earlier: not a full-match number, therefore an option.
            with pytest.raises(SystemExit):
                parser.parse_args(["decode", "rec.mie", "--mux-delimiter", "-5e3"])

    @pytest.mark.requirement("L2-CLI-005")
    def test_a_lone_dash_output_is_a_path_not_stdout(self) -> None:
        """``-o -`` names a file, and is exempt from the option-like guard."""
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "rec.mie", "-o", "-"])
        assert str(parsed.output) == "-"


def _argparse_strips_marker_before_subcommand() -> bool:
    """Whether this interpreter's argparse accepts ``-- <subcommand>``.

    Asked of a bare parser with one subcommand, so the answer is the
    interpreter's and not the decoder's.
    """
    probe = argparse.ArgumentParser(prog="probe")
    probe.add_subparsers(dest="command").add_parser("sub")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return probe.parse_args(["--", "sub"]).command == "sub"
    except SystemExit:
        return False


class TestEndOfOptions:
    """``--`` ends option parsing (L2-CLI-016).

    ``argparse`` has always honoured the POSIX separator; Rust and C++ used to
    report ``--`` itself as an unknown option. These tests pin Python's side of
    the contract, which is the side the other two were made to match.
    """

    @pytest.mark.requirement("L2-CLI-016")
    def test_after_the_marker_every_token_is_a_path(self) -> None:
        """A flag spelling after ``--`` is an input, not a flag."""
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "--", "rec.mie", "--no-mux"])
        assert [str(p) for p in parsed.inputs] == ["rec.mie", "--no-mux"]
        assert not parsed.no_mux

    @pytest.mark.requirement("L2-CLI-016")
    def test_only_the_first_marker_is_consumed(self) -> None:
        """A second ``--`` is an ordinary positional.

        That is the only way to name a file that is actually called ``--``.
        """
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "--", "--", "rec.mie"])
        assert [str(p) for p in parsed.inputs] == ["--", "rec.mie"]

    @pytest.mark.requirement("L2-CLI-016")
    def test_flags_before_the_marker_still_work(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(["decode", "--no-mux", "--", "rec.mie"])
        assert parsed.no_mux is True
        assert [str(p) for p in parsed.inputs] == ["rec.mie"]

    @pytest.mark.requirement("L2-CLI-016")
    def test_the_marker_is_scoped_to_one_parser(self) -> None:
        """A ``--`` before the subcommand does not carry into it.

        The next token is the subcommand *name*; the subcommand then gets a
        fresh scan, so a flag after it still acts as a flag. Getting this wrong
        in the other two would have made ``-- decode rec.mie --no-mux`` ignore
        ``--no-mux``.

        **This position depends on the interpreter's argparse.** Older ones do
        not strip a leading ``--`` ahead of a subparser choice and pass ``--``
        itself as the subcommand name, so the invocation is a usage error there.
        Rust and C++ support it everywhere; the contract (L2-CLI-016) therefore
        binds only the *post*-subcommand position, which every supported
        interpreter handles the same way, and the conformance suite uses that
        one. Asserting the interpreter's own answer here keeps the test honest
        on every version.

        The answer is *probed*, not read off ``sys.version_info``: the change
        was not a minor-version boundary. It reached 3.12 in a patch release,
        so Ubuntu 24.04's system 3.12.3 still rejects the invocation while
        3.12.10 accepts it, and a ``< (3, 12)`` gate failed on the former.
        """
        parser = cli.build_parser()
        argv = ["--", "decode", "rec.mie", "--no-mux"]

        if not _argparse_strips_marker_before_subcommand():
            with pytest.raises(SystemExit):
                parser.parse_args(argv)
            return

        parsed = parser.parse_args(argv)
        assert parsed.command == "decode"
        assert parsed.no_mux is True
        assert [str(p) for p in parsed.inputs] == ["rec.mie"]

    @pytest.mark.requirement("L2-CLI-016")
    def test_the_marker_suppresses_the_global_flags(self) -> None:
        """``-- --version`` asks for a subcommand called ``--version``.

        A usage error on every supported version, though for two different
        reasons: 3.12+ takes ``--version`` as an invalid subcommand name, while
        3.10 and 3.11 never strip the ``--`` and reject *that* as the name.
        Either way the version is not printed, which is the property that
        matters and the one Rust and C++ were made to match.
        """
        parser = cli.build_parser()
        for token in ("--version", "-h", "--help", "-V"):
            with pytest.raises(SystemExit) as excinfo:
                parser.parse_args(["--", token])
            assert excinfo.value.code != EXIT_OK

    @pytest.mark.requirement("L2-CLI-016")
    def test_count_and_dump_honour_it(self) -> None:
        parser = cli.build_parser()
        # `count` and `dump` take one path each, under `input` (singular);
        # only `decode` has the plural `inputs`, which is what feeds the merge.
        assert str(parser.parse_args(["count", "--", "-weird.mie"]).input) == "-weird.mie"
        assert str(parser.parse_args(["dump", "--", "-weird.mie"]).input) == "-weird.mie"


class TestHelpPrecedence:
    """A pending ``-h``/``--help`` outranks a *deferred* diagnostic
    (L2-CLI-017).

    ``argparse`` gives this for free: the help action fires while parsing,
    whereas unrecognised arguments are reported afterwards. Rust reported the
    unknown option instead and was the odd one out until it was changed to
    match. These tests pin Python's side so the reference cannot drift.
    """

    @pytest.mark.requirement("L2-CLI-017")
    def test_help_wins_over_an_unrecognised_option(self) -> None:
        """The operator with a broken command line is the one asking."""
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["decode", "rec.mie", "--nonsense", "--help"])
        assert excinfo.value.code == EXIT_OK

    @pytest.mark.requirement("L2-CLI-017")
    def test_help_does_not_rescue_a_failed_value_consumption(self) -> None:
        """A flag that cannot take a value is a hard stop.

        ``argparse`` raises immediately rather than deferring, so the help
        action is never reached. Rust reproduces this by draining its argument
        iterator at the same point; C++ does not, and answers help here — the
        one shape in this area where it is the outlier, which is why there is
        no conformance case for it yet.
        """
        parser = cli.build_parser()
        for argv in (
            ["--log-level", "-h", "decode", "rec.mie"],
            ["--config", "--help", "decode", "rec.mie"],
            ["decode", "rec.mie", "--mux-delimiter", "-h", "--help"],
        ):
            with pytest.raises(SystemExit) as excinfo:
                parser.parse_args(argv)
            assert excinfo.value.code != EXIT_OK, argv

    @pytest.mark.requirement("L2-CLI-016", "L2-CLI-017")
    def test_help_after_the_end_of_options_marker_is_a_path(self) -> None:
        """``--`` demotes a later help flag to an argument, so it cannot
        rescue a broken command line."""
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["decode", "--nonsense", "--", "--help"])
        assert excinfo.value.code != EXIT_OK
