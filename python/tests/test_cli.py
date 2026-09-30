"""The ``aero1553`` command-line interface, as Python reaches it.

The CLI is the Rust implementation, run in-process through the compiled
extension, so the flag grammar itself is pinned exhaustively by
``rust/tests/cli.rs`` and across implementations by ``tests/conformance/``.
What these tests pin is the *Python interface* to it: ``aero1553.cli.main`` and
``main_cli``, the ``EXIT_*`` constants, where output lands, and -- as black-box
runs, observed through exit status, output and the files written -- the
grammar rules a Python caller depends on.

Every run goes through the ``run_cli`` fixture (``tests/conftest.py``), which
captures at the file-descriptor level: the CLI writes to the process's stdout
and stderr descriptors, not to ``sys.stdout``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aero1553 import __version__, cli
from aero1553.cli import (
    EXIT_CONFIG,
    EXIT_MERGE_INCOMPATIBLE,
    EXIT_NO_RECORDS,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_SYNC_LOSS,
    EXIT_USAGE,
)
from tests.conftest import RunCli, conformance_input, normal_record_rt15_sa11_us

# A file name whose default MUX field (index 4, split on ".") is "MUX7", so the
# MUX column shows whether MUX population ran and which delimiter it used.
MUX_NAME = "a.b.c.d.MUX7.mie"


def _csv_rows(text: str) -> list[list[str]]:
    return [line.split(",") for line in text.splitlines()]


def _mux_column(csv_text: str) -> set[str]:
    rows = _csv_rows(csv_text)
    index = rows[0].index("MUX")
    return {row[index] for row in rows[1:]}


@pytest.fixture
def rec(tmp_path: Path, multi_record_data: bytes) -> Path:
    """A clean three-record IRIG recording whose name carries a MUX field."""
    path = tmp_path / MUX_NAME
    path.write_bytes(multi_record_data)
    return path


# ── entry points ────────────────────────────────────────────────────────────


class TestEntryPoints:
    def test_version_matches_the_package(self, run_cli: RunCli) -> None:
        """The CLI's version is the package's: one joint-cut number."""
        result = run_cli(["--version"])
        assert result.rc == EXIT_OK
        assert result.out == f"aero1553 {__version__}\n"

    def test_help_goes_to_stdout(self, run_cli: RunCli) -> None:
        result = run_cli(["--help"])
        assert result.rc == EXIT_OK
        assert "USAGE:" in result.out
        assert result.err == ""

    def test_no_argv_reads_sys_argv(self, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["aero1553", "--version"])
        assert cli.main() == EXIT_OK

    def test_python_output_is_flushed_before_the_cli_writes(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Text buffered in ``sys.stdout`` comes out BEFORE the CLI's output.

        The CLI writes to the descriptor directly; without the flush in
        ``main`` the earlier Python text would surface after it.
        """
        sys.stdout.write("before|")
        assert cli.main(["--version"]) == EXIT_OK
        out, _ = capfd.readouterr()
        assert out == f"before|aero1553 {__version__}\n"

    def test_main_cli_restores_the_default_sigint_handler(self) -> None:
        """Ctrl-C must end a running decode at once, as it ends the binary.

        While the CLI runs no Python bytecode executes, so Python's own SIGINT
        handler (which only sets a flag) would leave Ctrl-C waiting for the
        decode to finish. ``main_cli`` is the process boundary and restores the
        default; ``main`` leaves an embedding application's handler alone.
        """
        original = signal.getsignal(signal.SIGINT)
        try:
            assert cli.main(["--version"]) == EXIT_OK
            assert signal.getsignal(signal.SIGINT) is original
            assert cli.main_cli(["--version"]) == EXIT_OK
            assert signal.getsignal(signal.SIGINT) == signal.SIG_DFL
        finally:
            signal.signal(signal.SIGINT, original)

    def test_main_cli_off_the_main_thread_leaves_signals_alone(self) -> None:
        """``signal.signal`` raises outside the main thread, so the SIGINT
        restore is skipped there -- and the command line still runs."""
        original = signal.getsignal(signal.SIGINT)
        results: list[int] = []
        worker = threading.Thread(target=lambda: results.append(cli.main_cli(["--version"])))
        worker.start()
        worker.join(timeout=60)
        assert results == [EXIT_OK]
        assert signal.getsignal(signal.SIGINT) is original

    @pytest.mark.requirement("L2-WRT-018")
    def test_main_cli_neutralises_a_dead_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stdout that cannot be flushed must not turn a clean exit into
        CPython's shutdown-failure 120 (L2-WRT-018).

        ``main_cli`` repoints the descriptor at the null device, so the flush
        CPython performs at shutdown writes nowhere and cannot fail. Observed
        here on a descriptor the test owns: after the call, writes to it no
        longer reach the file behind it.
        """
        target = tmp_path / "stdout.bin"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:

            class DeadStdout:
                def write(self, text: str) -> int:
                    return len(text)

                def flush(self) -> None:
                    raise BrokenPipeError("consumer went away")

                def fileno(self) -> int:
                    return fd

            monkeypatch.setattr(sys, "stdout", DeadStdout())
            assert cli.main_cli(["no-such-command"]) == EXIT_USAGE
            os.write(fd, b"after")
        finally:
            os.close(fd)
        assert target.read_bytes() == b"", "the descriptor now points at the null device"

    def test_python_dash_m_runs_the_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "aero1553", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert proc.returncode == EXIT_OK
        assert proc.stdout == f"aero1553 {__version__}\n"


# ── the exit taxonomy (L2-CLI-011) ──────────────────────────────────────────


class TestExitCodes:
    """Each ``EXIT_*`` constant is the status the CLI actually returns.

    The constants are Python's names for the Rust CLI's ``exit_code`` values;
    reaching every class from Python is what keeps the two from drifting.
    """

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_success(self, run_cli: RunCli, rec: Path, tmp_path: Path) -> None:
        assert run_cli(["decode", str(rec), "-o", str(tmp_path / "o.csv")]).rc == EXIT_OK

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_runtime_missing_input(self, run_cli: RunCli, tmp_path: Path) -> None:
        result = run_cli(["count", str(tmp_path / "missing.mie")])
        assert result.rc == EXIT_RUNTIME
        assert "Error:" in result.err
        assert "Traceback" not in result.err

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_no_records(self, run_cli: RunCli, tmp_path: Path) -> None:
        path = tmp_path / "not-mie.mie"
        path.write_bytes(conformance_input("no-valid-records"))
        assert run_cli(["decode", str(path), "-o", str(tmp_path / "o.csv")]).rc == EXIT_NO_RECORDS

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_sync_loss(self, run_cli: RunCli, tmp_path: Path) -> None:
        path = tmp_path / "broken.mie"
        path.write_bytes(conformance_input("partial-unrecoverable"))
        out = tmp_path / "o.csv"
        assert run_cli(["decode", str(path), "-o", str(out)]).rc == EXIT_SYNC_LOSS
        assert not out.exists()

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_usage(self, run_cli: RunCli) -> None:
        result = run_cli(["no-such-command"])
        assert result.rc == EXIT_USAGE
        assert "Unknown command" in result.err

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_config(self, run_cli: RunCli, rec: Path, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        assert run_cli(["--config", str(missing), "count", str(rec)]).rc == EXIT_CONFIG

    @pytest.mark.requirement("L2-CLI-011", "L3-PY-006")
    def test_merge_incompatible(self, run_cli: RunCli, tmp_path: Path) -> None:
        a = tmp_path / "a.mie"
        a.write_bytes(conformance_input("merge-a"))
        freerun = tmp_path / "freerun.mie"
        freerun.write_bytes(conformance_input("merge-freerun"))
        out = tmp_path / "o.csv"
        result = run_cli(["decode", str(a), str(freerun), "-o", str(out)])
        assert result.rc == EXIT_MERGE_INCOMPATIBLE


# ── where output goes ───────────────────────────────────────────────────────


class TestOutputRouting:
    @pytest.mark.requirement("L2-WRT-007")
    def test_decode_without_output_writes_csv_to_stdout(self, run_cli: RunCli, rec: Path) -> None:
        result = run_cli(["decode", str(rec)])
        assert result.rc == EXIT_OK
        rows = _csv_rows(result.out)
        assert rows[0][0] == "TIME_STAMP"
        assert len(rows) == 4  # header + three records

    def test_count_prints_the_count(self, run_cli: RunCli, rec: Path) -> None:
        result = run_cli(["count", str(rec)])
        assert result.rc == EXIT_OK
        assert result.out.strip() == "3"

    def test_diagnostics_go_to_stderr_only(self, run_cli: RunCli, tmp_path: Path) -> None:
        result = run_cli(["decode", str(tmp_path / "missing.mie")])
        assert result.out == ""
        assert "Error:" in result.err


# ── merge output collisions (L2-WRT-014) ────────────────────────────────────


class TestMergeOutputCollision:
    """A merge must never commit over one of its own inputs.

    Checked for every commit target -- the destination and the paths derived
    from it -- because the mode that derives them can come from a config file
    as easily as from a flag.
    """

    @pytest.fixture
    def recording(self, multi_record_data: bytes) -> bytes:
        return multi_record_data

    def test_output_naming_an_input_is_refused(
        self, run_cli: RunCli, tmp_path: Path, recording: bytes
    ) -> None:
        a = tmp_path / "a.mie"
        b = tmp_path / "b.mie"
        a.write_bytes(recording)
        b.write_bytes(recording)
        result = run_cli(["decode", str(a), str(b), "-o", str(a)])
        assert result.rc == EXIT_RUNTIME
        assert "resolves to merge input" in result.err
        assert a.read_bytes() == recording

    @pytest.mark.requirement("L2-WRT-014")
    def test_derived_errors_file_naming_an_input_is_refused(
        self, run_cli: RunCli, tmp_path: Path, recording: bytes
    ) -> None:
        """``-o capture.mie --separate-errors`` derives ``capture_errors.mie``.

        Inline mode never writes an errors file, so the same inputs are fine
        there; separate mode would commit the errors file over the input.
        """
        victim = tmp_path / "capture_errors.mie"
        other = tmp_path / "b.mie"
        victim.write_bytes(recording)
        other.write_bytes(recording)
        out = tmp_path / "capture.mie"

        inline = run_cli(["decode", str(other), str(victim), "-o", str(out)])
        assert inline.rc == EXIT_OK

        out.unlink()
        separate = run_cli(["decode", str(other), str(victim), "-o", str(out), "--separate-errors"])
        assert separate.rc == EXIT_RUNTIME
        assert "derived output path" in separate.err
        assert victim.read_bytes() == recording

    @pytest.mark.requirement("L2-WRT-014")
    def test_partial_target_naming_an_input_is_refused(
        self, run_cli: RunCli, tmp_path: Path, recording: bytes
    ) -> None:
        """``<destination>.partial`` is a commit target under ``--allow-partial``,
        even though a clean decode never writes one: nobody knows in advance
        whether the decode will lose sync."""
        victim = tmp_path / "out.csv.partial"
        other = tmp_path / "b.mie"
        victim.write_bytes(recording)
        other.write_bytes(recording)
        out = tmp_path / "out.csv"

        assert run_cli(["decode", str(other), str(victim), "-o", str(out)]).rc == EXIT_OK

        out.unlink()
        result = run_cli(["decode", str(other), str(victim), "-o", str(out), "--allow-partial"])
        assert result.rc == EXIT_RUNTIME
        assert "derived output path" in result.err


# ── a closed stdout consumer (L2-WRT-018) ───────────────────────────────────


class TestBrokenPipe:
    """``aero1553 dump big.mie | head`` is the documented diagnostic workflow.

    A consumer that stops reading is a clean exit, not an error -- and at the
    real process boundary, which only a subprocess has, CPython's shutdown
    flush must not turn that 0 into 120.
    """

    @pytest.mark.requirement("L2-WRT-018")
    def test_dump_into_a_closed_pipe_exits_zero(self, tmp_path: Path) -> None:
        big = tmp_path / "big.mie"
        big.write_bytes(b"".join(normal_record_rt15_sa11_us(i * 100) for i in range(3000)))
        proc = subprocess.Popen(
            [sys.executable, "-m", "aero1553", "dump", str(big)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        assert proc.stdout.read(64)  # the dump started ...
        proc.stdout.close()  # ... and the consumer went away
        # Not communicate(): it would try to read the stdout just closed.
        err = proc.stderr.read()
        proc.wait(timeout=60)
        assert proc.returncode == EXIT_OK, err.decode(errors="replace")
        assert b"Error:" not in err

    def test_an_unwritable_destination_is_a_runtime_error(
        self, run_cli: RunCli, rec: Path, tmp_path: Path
    ) -> None:
        """A real write failure stays exit 1 -- only a closed consumer is clean."""
        result = run_cli(["decode", str(rec), "-o", str(tmp_path / "no-such-dir" / "o.csv")])
        assert result.rc == EXIT_RUNTIME
        assert "Error:" in result.err


# ── flag spelling (L2-CLI-015) ──────────────────────────────────────────────


class TestFlagValueSyntax:
    """``--flag value`` and ``--flag=value`` are the same invocation."""

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
    def test_both_spellings_decode_identically(
        self, run_cli: RunCli, rec: Path, flag: str, value: str
    ) -> None:
        """Same status and the same CSV bytes, whichever spelling is used."""
        separated = run_cli(["decode", str(rec), flag, value])
        joined = run_cli(["decode", str(rec), f"{flag}={value}"])
        assert separated.rc == EXIT_OK
        assert (joined.rc, joined.out) == (separated.rc, separated.out)

    @pytest.mark.requirement("L2-CLI-015")
    def test_eq_form_with_empty_value_is_an_empty_value(self, run_cli: RunCli, rec: Path) -> None:
        """``--exclude-rts=`` is the flag with nothing in it, not a bad token.

        An empty filter adds nothing, so the decode is the one that never
        passed the flag.
        """
        with_flag = run_cli(["decode", str(rec), "--exclude-rts="])
        without = run_cli(["decode", str(rec)])
        assert with_flag.rc == EXIT_OK
        assert with_flag.out == without.out

    @pytest.mark.requirement("L2-CLI-015")
    def test_only_the_first_equals_separates(
        self, run_cli: RunCli, tmp_path: Path, multi_record_data: bytes
    ) -> None:
        """``--mux-delimiter==`` sets the delimiter to ``=``, not to empty."""
        path = tmp_path / "a=b=c=d=MUXQ=e.mie"
        path.write_bytes(multi_record_data)
        result = run_cli(["decode", str(path), "--mux-delimiter=="])
        assert result.rc == EXIT_OK
        assert _mux_column(result.out) == {"MUXQ"}

    @pytest.mark.requirement("L2-CLI-015")
    @pytest.mark.parametrize("token", ["--no-mux=true", "--separate-errors=1", "--strict=false"])
    def test_a_valueless_flag_rejects_a_joined_value(
        self, run_cli: RunCli, rec: Path, token: str
    ) -> None:
        """``--no-mux=true`` is a usage error, not a way to spell "on"."""
        assert run_cli(["decode", str(rec), token]).rc == EXIT_USAGE

    @pytest.mark.requirement("L2-CLI-015")
    def test_a_positional_path_may_contain_an_equals(
        self, run_cli: RunCli, tmp_path: Path, multi_record_data: bytes
    ) -> None:
        """Splitting is confined to flags; ``a=b.mie`` is an input path."""
        path = tmp_path / "a=b.mie"
        path.write_bytes(multi_record_data)
        assert run_cli(["count", str(path)]).out.strip() == "3"

    @pytest.mark.requirement("L2-CLI-015")
    def test_global_flags_accept_both_spellings(self, run_cli: RunCli, rec: Path) -> None:
        """Globals precede the subcommand and take both spellings too.

        DEBUG is observable: it logs the startup line that WARNING hides.
        """
        separated = run_cli(["--log-level", "DEBUG", "count", str(rec)])
        joined = run_cli(["--log-level=DEBUG", "count", str(rec)])
        assert separated.rc == joined.rc == EXIT_OK
        assert "aero1553 v" in separated.err
        assert "aero1553 v" in joined.err

    OPTION_LIKE: tuple[str, ...] = ("--no-mux", "--foo", "-o", "-x", "-abc", "--1")
    VALUE_LIKE: tuple[str, ...] = ("-", "-5", "-5.5", "-.5", "- x", "-5e3")

    @pytest.mark.requirement("L2-CLI-015")
    @pytest.mark.parametrize("token", OPTION_LIKE)
    def test_separated_form_refuses_an_option_like_value(
        self, run_cli: RunCli, rec: Path, token: str
    ) -> None:
        """``--mux-delimiter --no-mux`` is a usage error, not a delimiter.

        Consuming the next flag as the value would mean ``--no-mux`` silently
        never ran and the decode succeeded with a wrong MUX column.
        """
        assert run_cli(["decode", str(rec), "--mux-delimiter", token]).rc == EXIT_USAGE

    @pytest.mark.requirement("L2-CLI-015")
    @pytest.mark.parametrize("token", VALUE_LIKE)
    def test_separated_form_accepts_the_exemptions(
        self, run_cli: RunCli, rec: Path, token: str
    ) -> None:
        """A lone dash, a number (``-5e3`` included), and a token with a space
        are values: ``-o -`` names a file, ``--mux-field -1`` counts from the
        end, and no option is spelled with a space in it."""
        assert run_cli(["decode", str(rec), "--mux-delimiter", token]).rc == EXIT_OK

    @pytest.mark.requirement("L2-CLI-015")
    def test_joined_form_still_takes_an_option_like_value(
        self, run_cli: RunCli, tmp_path: Path, multi_record_data: bytes
    ) -> None:
        """The joined form is unambiguous, so it accepts what the separated form
        refuses -- and ``--no-mux`` inside it is a value, not a flag."""
        path = tmp_path / "p--no-muxq--no-muxr--no-muxs--no-muxMUXZ.mie"
        path.write_bytes(multi_record_data)
        result = run_cli(["decode", str(path), "--mux-delimiter=--no-mux"])
        assert result.rc == EXIT_OK
        assert _mux_column(result.out) == {"MUXZ.mie"}

    @pytest.mark.requirement("L2-CLI-005")
    def test_a_lone_dash_output_is_a_path_not_stdout(
        self, run_cli: RunCli, rec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-o -`` names a file called ``-``; it is not a spelling of stdout."""
        monkeypatch.chdir(tmp_path)
        result = run_cli(["decode", str(rec), "-o", "-"])
        assert result.rc == EXIT_OK
        assert result.out == ""
        assert (tmp_path / "-").read_text(encoding="utf-8").startswith("TIME_STAMP,")


# ── end of options (L2-CLI-016) ─────────────────────────────────────────────


class TestEndOfOptions:
    """``--`` ends option parsing, scoped to the parser that sees it."""

    @pytest.fixture(autouse=True)
    def _cwd_with_flag_named_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_record_data: bytes
    ) -> None:
        """Run each test in a directory holding recordings named like flags."""
        for name in (MUX_NAME, "--no-mux", "--", "-weird.mie"):
            (tmp_path / name).write_bytes(multi_record_data)
        monkeypatch.chdir(tmp_path)

    @pytest.mark.requirement("L2-CLI-016")
    def test_after_the_marker_every_token_is_a_path(self, run_cli: RunCli) -> None:
        """``--no-mux`` after ``--`` is a second INPUT (a merge), not the flag:
        the MUX column is still populated from the first file's name."""
        result = run_cli(["decode", "--", MUX_NAME, "--no-mux"])
        assert result.rc == EXIT_OK
        assert "MUX7" in _mux_column(result.out)
        assert len(_csv_rows(result.out)) == 7  # header + 3 + 3

    @pytest.mark.requirement("L2-CLI-016")
    def test_only_the_first_marker_is_consumed(self, run_cli: RunCli) -> None:
        """A second ``--`` is an ordinary positional -- the only way to name a
        file that is actually called ``--``."""
        result = run_cli(["decode", "--", "--", MUX_NAME])
        assert result.rc == EXIT_OK
        assert len(_csv_rows(result.out)) == 7

    @pytest.mark.requirement("L2-CLI-016")
    def test_flags_before_the_marker_still_work(self, run_cli: RunCli) -> None:
        result = run_cli(["decode", "--no-mux", "--", MUX_NAME])
        assert result.rc == EXIT_OK
        assert _mux_column(result.out) == {""}

    @pytest.mark.requirement("L2-CLI-016")
    def test_the_marker_is_scoped_to_one_parser(self, run_cli: RunCli) -> None:
        """A ``--`` before the subcommand does not carry into it: the next token
        is the subcommand NAME, which then gets a fresh scan, so a flag after it
        still acts as a flag.

        Deterministic now that the parser is Rust's. It used to depend on the
        interpreter: argparse only learned to strip a leading ``--`` ahead of a
        subcommand in a 3.12 patch release.
        """
        result = run_cli(["--", "decode", MUX_NAME, "--no-mux"])
        assert result.rc == EXIT_OK
        assert _mux_column(result.out) == {""}

    @pytest.mark.requirement("L2-CLI-016")
    @pytest.mark.parametrize("token", ["--version", "-h", "--help", "-V"])
    def test_the_marker_suppresses_the_global_flags(self, run_cli: RunCli, token: str) -> None:
        """``-- --version`` asks for a subcommand called ``--version``."""
        result = run_cli(["--", token])
        assert result.rc == EXIT_USAGE
        assert __version__ not in result.out

    @pytest.mark.requirement("L2-CLI-016")
    @pytest.mark.parametrize("command", ["count", "dump"])
    def test_count_and_dump_honour_it(self, run_cli: RunCli, command: str) -> None:
        assert run_cli([command, "--", "-weird.mie"]).rc == EXIT_OK


# ── help precedence (L2-CLI-017) ────────────────────────────────────────────


class TestHelpPrecedence:
    """A pending ``-h``/``--help`` outranks a *deferred* diagnostic."""

    @pytest.mark.requirement("L2-CLI-017")
    def test_help_wins_over_an_unrecognised_option(self, run_cli: RunCli) -> None:
        """The operator with a broken command line is the one asking."""
        result = run_cli(["decode", "rec.mie", "--nonsense", "--help"])
        assert result.rc == EXIT_OK
        assert "USAGE:" in result.out

    @pytest.mark.requirement("L2-CLI-017")
    @pytest.mark.parametrize(
        "argv",
        [
            ["--log-level", "-h", "decode", "rec.mie"],
            ["--config", "--help", "decode", "rec.mie"],
            ["decode", "rec.mie", "--mux-delimiter", "-h", "--help"],
        ],
    )
    def test_help_does_not_rescue_a_failed_value_consumption(
        self, run_cli: RunCli, argv: list[str]
    ) -> None:
        """A flag that cannot take its value is a hard stop."""
        assert run_cli(argv).rc == EXIT_USAGE

    @pytest.mark.requirement("L2-CLI-016", "L2-CLI-017")
    def test_help_after_the_end_of_options_marker_is_a_path(self, run_cli: RunCli) -> None:
        """``--`` demotes a later help flag to an argument, so it cannot
        rescue a broken command line."""
        assert run_cli(["decode", "--nonsense", "--", "--help"]).rc == EXIT_USAGE
