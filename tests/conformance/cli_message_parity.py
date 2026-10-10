"""Differential check of what the CLIs tell an operator about the command line.

``config_parity.py`` holds every implementation to the same diagnostics for a
config file. This holds them to the same output for the command line itself:
usage errors, help and version (L2-CLI-022). For each case it compares the exit
code, stdout and stderr of every implementation, all-pairs, with run-specific
paths replaced by placeholders, and fails on any byte outside ASCII
(L2-CLI-014).

The usage layer had never been compared. When it first was, the three CLIs
agreed on the exit code of every case here and on the wording of almost none:
each had its own messages, Rust and C++ printed help texts of 130 and 75
lines, C++ followed a usage error with the whole help where Rust did so only
for errors found while parsing, and C++ logged an ``ERROR`` line before every
usage error where Rust did not.

Run automatically by ``run.py`` when two or more implementations are under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from differential import describe_stderr_problems, normalized_stderr

# ``{IN}`` is a valid single-record recording, ``{IN2}`` a copy of it, ``{OUT}``
# a destination that does not exist, ``{EMPTY}`` an empty manifest and
# ``{MANIFEST}`` a manifest naming ``{IN}``.
CASES: list[tuple[str, list[str]]] = [
    # -- no command ---------------------------------------------------------
    ("no-arguments", []),
    ("globals-only", ["--log-level", "INFO"]),
    ("separator-only", ["--"]),
    ("separator-then-help", ["--", "-h"]),
    ("unknown-command", ["frobnicate", "{IN}"]),
    ("unknown-global-option", ["--bogus", "count", "{IN}"]),
    ("version-after-command", ["count", "{IN}", "--version"]),
    # -- help and version: stdout, exit 0 ------------------------------------
    ("help", ["--help"]),
    ("help-short", ["-h"]),
    ("decode-help", ["decode", "--help"]),
    ("count-help", ["count", "-h"]),
    ("dump-help", ["dump", "--help"]),
    ("help-outranks-unknown-option", ["decode", "--bogus", "--help"]),
    ("help-outranks-unknown-command", ["frobnicate", "--help"]),
    ("help-is-a-value-after-a-flag", ["--log-level", "-h"]),
    ("version", ["--version"]),
    ("version-short", ["-V"]),
    # -- missing inputs and values ------------------------------------------
    ("decode-no-input", ["decode"]),
    ("count-no-input", ["count"]),
    ("dump-no-input", ["dump"]),
    ("count-two-inputs", ["count", "{IN}", "{IN2}"]),
    ("dump-two-inputs", ["dump", "{IN}", "{IN2}"]),
    ("output-no-value", ["decode", "{IN}", "-o"]),
    ("output-option-as-value", ["decode", "{IN}", "-o", "--no-mux"]),
    ("log-level-no-value", ["--log-level"]),
    ("config-no-value", ["--config"]),
    ("glob-no-value", ["decode", "--glob"]),
    # -- unknown and retired options ----------------------------------------
    ("decode-unknown-option", ["decode", "{IN}", "--bogus"]),
    ("count-unknown-option", ["count", "{IN}", "--bogus"]),
    ("dump-unknown-option", ["dump", "{IN}", "--bogus"]),
    ("retired-inline-errors", ["decode", "{IN}", "--inline-errors"]),
    ("retired-time-format", ["decode", "{IN}", "--time-format", "irig"]),
    ("valueless-flag-with-value", ["decode", "{IN}", "--no-mux=true"]),
    # -- bad values ---------------------------------------------------------
    ("bad-log-level", ["--log-level", "LOUD", "count", "{IN}"]),
    ("bad-log-level-and-unknown-option", ["--log-level", "LOUD", "count", "{IN}", "--bogus"]),
    ("detect-records-text", ["decode", "{IN}", "--detect-records", "x"]),
    ("detect-records-zero", ["decode", "{IN}", "--detect-records", "0"]),
    ("detect-records-huge", ["decode", "{IN}", "--detect-records", "99999999999999999999"]),
    ("lookahead-records-over", ["decode", "{IN}", "--lookahead-records", "33"]),
    ("tick-rate-text", ["decode", "{IN}", "--standard-tick-rate-hz", "x"]),
    ("tick-rate-zero", ["decode", "{IN}", "--standard-tick-rate-hz", "0"]),
    ("tick-rate-inf", ["decode", "{IN}", "--standard-tick-rate-hz", "inf"]),
    ("tick-rate-overflow", ["decode", "{IN}", "--standard-tick-rate-hz", "1e400"]),
    ("year-zero", ["decode", "{IN}", "--year", "0"]),
    ("year-text", ["decode", "{IN}", "--year", "x"]),
    ("utc-offset-bad", ["decode", "{IN}", "--utc-offset", "+24:00"]),
    ("calendar-without-year", ["decode", "{IN}", "--output-time-format", "iso"]),
    ("output-time-format-bad", ["decode", "{IN}", "--output-time-format", "elapsed"]),
    ("input-time-format-bad", ["decode", "{IN}", "--input-time-format", "gps"]),
    ("delta-scope-bad", ["decode", "{IN}", "--delta-scope", "whole"]),
    ("format-bad", ["decode", "{IN}", "--format", "json"]),
    ("mux-delimiter-empty", ["decode", "{IN}", "--mux-delimiter="]),
    ("mux-field-text", ["decode", "{IN}", "--mux-field", "x"]),
    ("collapse-window-negative", ["decode", "{IN}", "--collapse-window-us", "-5"]),
    ("max-sort-group-zero", ["decode", "{IN}", "--max-sort-group", "0"]),
    ("max-collapse-survivors-over", ["decode", "{IN}", "--max-collapse-survivors", "1048577"]),
    ("exclude-types-bad", ["decode", "{IN}", "--exclude-types", "BOGUS"]),
    ("exclude-types-signed-hex", ["decode", "{IN}", "--exclude-types", "0x+1"]),
    ("exclude-rts-over", ["decode", "{IN}", "--exclude-rts", "32"]),
    ("exclude-rts-text", ["decode", "{IN}", "--exclude-rts", "x"]),
    ("exclude-buses-bad", ["decode", "{IN}", "--exclude-buses", "C"]),
    ("include-subaddresses-negative", ["decode", "{IN}", "--include-subaddresses=-1"]),
    ("non-ascii-option", ["decode", "{IN}", "--str\u00efct"]),
    ("non-ascii-value", ["decode", "{IN}", "--delta-scope", "gl\u00f6bal"]),
    ("dump-offset-text", ["dump", "{IN}", "--offset", "x"]),
    ("dump-records-negative", ["dump", "{IN}", "--records=-1"]),
    ("dump-length-text", ["dump", "{IN}", "--length", "x"]),
    # -- which error comes first, and the edges of each value grammar ---------
    ("count-second-input-then-unknown-option", ["count", "{IN}", "{IN2}", "--bogus"]),
    ("detect-records-negative", ["decode", "{IN}", "--detect-records=-1"]),
    ("detect-records-hex", ["decode", "{IN}", "--detect-records", "0x8"]),
    ("year-negative", ["decode", "{IN}", "--year=-1"]),
    ("year-huge", ["decode", "{IN}", "--year", "99999999999999999999"]),
    ("mux-field-empty", ["decode", "{IN}", "--mux-field="]),
    ("tick-rate-empty", ["decode", "{IN}", "--standard-tick-rate-hz="]),
    ("tick-rate-hex-float", ["decode", "{IN}", "--standard-tick-rate-hz", "0x1p4"]),
    ("delta-scope-padded", ["decode", "{IN}", "--delta-scope", " global"]),
    ("exclude-rts-hex-over", ["decode", "{IN}", "--exclude-rts", "0x20"]),
    ("exclude-rts-huge", ["decode", "{IN}", "--exclude-rts", "99999999999999999999"]),
    ("dump-offset-negative", ["dump", "{IN}", "--offset=-8"]),
    ("non-ascii-after-flag", ["decode", "{IN}", "-o", "-\u00e9"]),
    # -- input selection ------------------------------------------------------
    ("positional-and-manifest", ["decode", "{IN}", "--manifest", "{MANIFEST}"]),
    ("manifest-and-glob", ["decode", "--manifest", "{MANIFEST}", "--glob", "*.none"]),
    ("empty-manifest", ["decode", "--manifest", "{EMPTY}"]),
    ("glob-matches-nothing", ["decode", "--glob", "{DIR}/*.none"]),
]


def check_cli_message_parity(
    invocations: dict[str, list[str]], input_mie: Path, temp: Path
) -> None:
    """Run every case through every implementation; raise on any difference.

    Raises:
        AssertionError: if any two implementations differ in exit code, stdout
            or stderr for a case, or any of them writes a non-ASCII byte.
    """
    work = temp / "cli-message-parity"
    work.mkdir(exist_ok=True)
    second = work / "copy.mie"
    second.write_bytes(input_mie.read_bytes())
    empty = work / "empty.manifest"
    empty.write_text("# nothing here\n", encoding="ascii")
    manifest = work / "one.manifest"
    manifest.write_text(f"{input_mie}\n", encoding="utf-8")
    places = {
        "{IN}": str(input_mie),
        "{IN2}": str(second),
        "{EMPTY}": str(empty),
        "{MANIFEST}": str(manifest),
        "{DIR}": str(work),
    }
    placeholders = {path: name.strip("{}").join("<>") for name, path in places.items()}

    failures: list[str] = []
    for name, template in CASES:
        argv = []
        for token in template:
            for key, path in places.items():
                token = token.replace(key, path)
            argv.append(token)
        outcomes: dict[str, str] = {}
        stderrs: dict[str, str] = {}
        for impl, prefix in invocations.items():
            result = subprocess.run(
                [*prefix, *argv], cwd=work, capture_output=True, check=False, timeout=30
            )
            stdout = normalized_stderr(result.stdout, placeholders)
            stderr = normalized_stderr(result.stderr, placeholders)
            stderrs[impl] = stderr
            # Exit code and stdout folded into one compared value, with stderr
            # alongside, so a report shows all three for each implementation.
            outcomes[impl] = f"exit {result.returncode}\n--- stdout\n{stdout}--- stderr\n{stderr}"
        problems = describe_stderr_problems(outcomes)
        if problems is None:
            # The combined comparison already checked stderr; this adds nothing
            # unless a stream carried non-ASCII, which it reports per stream.
            problems = describe_stderr_problems(stderrs, compare=False)
        if problems is not None:
            failures.append(f"{name} ({' '.join(template)}): {problems}")
    if failures:
        raise AssertionError(
            f"cli-message parity failures ({len(failures)} of {len(CASES)}):\n  "
            + "\n  ".join(failures)
        )
    print(f"PASS cli-message-parity ({len(CASES)} cases across {', '.join(invocations)})")
