"""Command-line interface for Aero1553.

Provides the ``aero1553`` CLI command for decoding DDC MIL-STD-1553
MIE binary recording files into CSV format, and for hex-dumping raw
binary content with record boundary awareness.

Configuration is loaded from an optional TOML file and merged with
CLI arguments. CLI arguments always take precedence.

Usage::

    # Decode to stdout
    aero1553 decode recording.mie

    # Decode with config file (--config is global: before the subcommand)
    aero1553 --config my-config.toml decode recording.mie

    # Decode excluding spurious data and mode codes
    aero1553 decode recording.mie --exclude-types SPURIOUS_DATA,MODE_COMMAND

    # Decode only RT 15 (include filter), excluding Bus B
    aero1553 decode recording.mie --include-rts 15 --exclude-buses B

    # Hex dump
    aero1553 dump recording.mie --records 10
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import math
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from aero1553 import __version__
from aero1553.exceptions import (
    Aero1553Error,
    MieCalendarUnavailableError,
    MieClobberRefusedError,
    MieFileError,
    MieHomogeneousPayloadError,
    MieIncompatibleMergeInputsError,
    MieInputOutputCollisionError,
    MieNonMonotonicInputError,
    MieNoValidRecordsError,
    MieTimestampFormatMismatchError,
    MieUnrecoverableSyncLossError,
    MieWriterError,
)
from aero1553.logger import configure_logging, set_irig_day_advisory

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aero1553.config import DecoderConfig
    from aero1553.models import ErrorMode, MieMessage, TimeRender
    from aero1553.reader import MieFileReader
    from aero1553.writer import WriteOptions, WriteOutcome

logger = logging.getLogger(__name__)

# Process exit codes — the normative contract pinned by L2-CLI-011 /
# L1-EXIT-002..006. Mirrors the Rust `cli::exit_code` module so both
# implementations return identical codes for the same condition.
EXIT_OK = 0  # complete / recovered / --allow-partial partial
EXIT_RUNTIME = 1  # runtime / decode error (I/O, writer, strict record failures)
EXIT_NO_RECORDS = 2  # input is not an MIE recording
EXIT_SYNC_LOSS = 3  # unrecoverable mid-file sync loss without --allow-partial
EXIT_USAGE = 4  # CLI usage error (bad/unknown/missing flag or argument)
EXIT_CONFIG = 5  # configuration error (missing/malformed/invalid config)
EXIT_MERGE_INCOMPATIBLE = 6  # merge inputs cannot share an absolute timeline (L1-EXIT-009)


def _log_safe(value: object) -> str:
    """Neutralize CR/LF in user-controlled values before logging.

    A crafted input path could otherwise embed a newline and forge or inject
    additional log lines (SonarQube S5145). Escaping the control characters
    keeps each logged value on a single line without altering the visible path.

    Returns:
        The value as a string, with CR and LF replaced by their escaped
        spellings. The visible path is otherwise unchanged.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _report_error(context: str, exc: BaseException, code: int) -> int:
    """Log and print a handled error, then return its exit code.

    Kept as a helper rather than inline in the ``except`` clause so the log
    call preserves the clean one-line operator message for these *expected*
    conditions (file-not-found, no-records) instead of a full stack trace —
    mirroring how :func:`_classify_decode_error` reports the decode-time
    failures it is delegated.

    Returns:
        ``code``, unchanged, so the caller can ``return _report_error(...)``.
    """
    logger.error("%s: %s", context, exc)
    print(f"Error: {exc}", file=sys.stderr)
    return code


class _UsageErrorParser(argparse.ArgumentParser):
    """``ArgumentParser`` that exits with :data:`EXIT_USAGE` on a usage error.

    argparse defaults to exit code 2 for command-line usage errors, but in
    this tool exit 2 means "no valid records" (L2-CLI-011), so usage errors
    are remapped to 4 to avoid the collision and match the Rust CLI. The
    subclass propagates to subparsers automatically (argparse builds them
    with ``type(self)``).
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


#: L2-CLI-019: what ``--time-format`` says now that it has been split in two.
#:
#: The message names both replacements and states which concern each covers,
#: because the operator's intent is still expressible and the only open question
#: is which of the two they meant -- exactly what a generic "unrecognized
#: arguments" cannot answer.
RETIRED_TIME_FORMAT_MESSAGE = (
    "--time-format was split in v3.0.0 and is no longer accepted. Use "
    "--input-time-format auto|irig|standard to choose how timestamps are PARSED "
    "from the file, or --output-time-format doy|iso|dom to choose how they are "
    "WRITTEN to the CSV."
)


def _parse_year_arg(value: str) -> int:
    """Parse and range-check ``--year`` (L2-CLI-018).

    Validated at parse time so a bad value is a usage error rather than a
    malformed cell a hundred thousand rows later.

    Raises:
        ValueError: if the value is not an integer in ``[YEAR_MIN, YEAR_MAX]``.

    Returns:
        The validated year.
    """
    from aero1553.models import YEAR_MAX, YEAR_MIN

    message = f"invalid --year: {value!r}; valid range: [{YEAR_MIN}, {YEAR_MAX}]"
    # Rejected rather than passed to int(): a leading sign or underscore would
    # otherwise be accepted here and nowhere else.
    if not (value.isascii() and value.isdigit()):
        raise ValueError(message)
    year = int(value)
    if not YEAR_MIN <= year <= YEAR_MAX:
        raise ValueError(message)
    return year


def _parse_utc_offset_arg(value: str) -> int:
    """Parse ``--utc-offset`` (L2-CLI-018).

    Delegates to the config loader's grammar so the CLI and TOML spellings
    cannot drift.

    Raises:
        ValueError: if the value does not match the grammar.

    Returns:
        The offset in minutes east of UTC.
    """
    from aero1553.config import parse_utc_offset

    try:
        return parse_utc_offset(value)
    except ValueError as exc:
        raise ValueError(
            f"invalid --utc-offset: {value!r}; valid: Z, or +HH:MM / -HH:MM "
            f"with HH in [0, 23] and MM in [0, 59]"
        ) from exc


def _nonneg_int(value: str) -> int:
    """Parse a non-negative integer for the ``dump`` byte/record arguments.

    Accepts decimal (``16``) and base-prefixed (``0x10`` / ``0o20`` / ``0b10000``)
    notation via ``int(value, 0)``. This is the single ``type=`` used by every
    numeric ``dump`` argument (``--offset`` / ``--length`` / ``--records``) so they
    all accept the same notation — previously ``--records`` was decimal-only while
    ``--offset`` / ``--length`` took hex, an internal inconsistency. Mirrors the
    Rust ``parse_int_value`` unsigned semantics, so a negative value is rejected
    on both implementations.

    Raises:
        argparse.ArgumentTypeError: if the value is not a non-negative integer.
            argparse turns this into a usage error (exit 4).

    Returns:
        The parsed non-negative integer.
    """
    try:
        parsed = int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a non-negative integer (decimal or 0x hex), got {value!r}"
        ) from None
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}")
    return parsed


class _CommaSeparatedAppend(argparse.Action):
    """Collect comma-separated, repeatable filter values into a flat list.

    Mirrors the Rust filter syntax (``split_csv``): each occurrence takes
    ONE value, split on commas with each token trimmed and empties
    dropped. ``--include-rts 15,31`` and
    ``--include-rts 15 --include-rts 31`` are equivalent. Tokens are
    collected as raw strings; per-filter conversion/validation happens in
    the override-building step.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        current = getattr(namespace, self.dest, None) or []
        tokens = [t.strip() for t in str(values).split(",") if t.strip()]
        setattr(namespace, self.dest, list(current) + tokens)


def _parse_u8_list(values: list[str], flag: str) -> list[int]:
    """Parse RT/subaddress filter tokens to ints, mirroring the Rust CLI.

    Each token is decimal or ``0x``-prefixed hex and must fit in a u8
    (0–255) — the same bound the Rust CLI applies (``parse_u8_value``).
    The tighter MIL-STD-1553 [0, 31] range is enforced only on the
    config-file path, not here, so the two CLIs accept the same inputs.

    Returns:
        The parsed values, in the order given.

    Raises:
        ValueError: if a token is not an integer, or does not fit in a u8. The
            caller maps this to a usage error.
    """
    out: list[int] = []
    for tok in values:
        s = tok.strip()
        try:
            n = int(s, 16) if s[:2].lower() == "0x" else int(s)
        except ValueError as exc:
            raise ValueError(f"{flag} expected integer, got {tok!r}") from exc
        if not (0 <= n <= 255):
            raise ValueError(f"{flag} value out of range (0-255): {n}")
        out.append(n)
    return out


def _normalize_log_level(value: str) -> str:
    """Validate ``--log-level`` case-insensitively against the shared level
    set, returning the canonical uppercase name; raises ``ValueError`` on an
    invalid value.

    Uses the same vocabulary as the config-file ``logging.level`` key and
    the Rust CLI (case-insensitive, accepting ``WARN`` and ``OFF``) rather
    than argparse ``choices`` (which is case-sensitive and would reject
    ``warn`` / ``off`` and lowercase spellings).

    This is applied in ``main()`` *after* ``parse_args`` rather than as an
    argparse ``type=`` so that ``--version`` / ``--help`` short-circuit
    before the level is validated — matching the Rust CLI, which pulls those
    flags before applying the log level (so ``--log-level bogus --version``
    still prints the version instead of failing on the bad flag).

    Returns:
        The canonical uppercase level name.

    Raises:
        ValueError: if ``value`` is not a recognised level. The message lists
            the valid set.
    """
    from aero1553.config import _VALID_LOG_LEVELS

    normalized = value.upper()
    if normalized not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"argument --log-level: invalid log level {value!r}; valid: "
            "DEBUG, INFO, WARNING, WARN, ERROR, CRITICAL, OFF (case-insensitive)"
        )
    return normalized


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI.

    Returns:
        Configured ArgumentParser with ``decode`` and ``dump`` subcommands.
    """
    parser = _UsageErrorParser(
        prog="aero1553",
        description=(
            "Decode DDC MIL-STD-1553 MIE binary recording files "
            "into CSV format, or dump raw/record hex content."
        ),
    )
    parser.add_argument(
        "-v",
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default=None,
        help=(
            "Set logging verbosity: DEBUG, INFO, WARNING (alias WARN), ERROR, "
            "CRITICAL, or OFF (case-insensitive; CRITICAL/OFF silence all "
            "output). Overrides config file. Validated after --version/--help."
        ),
    )
    parser.add_argument(
        "--no-irig-day-advisory",
        action="store_true",
        help=(
            "Never emit the one-time IRIG day-of-year advisory. It is logged "
            "at INFO, so it is already silent at the default level; this "
            "suppresses it at INFO/DEBUG too. Config: [logging] "
            "irig_day_advisory = false."
        ),
    )
    # Global option (before the subcommand), matching the Rust CLI:
    # `aero1553 --config site.toml decode rec.mie`. Applies to every
    # subcommand (decode/count use the full config; dump uses only
    # [logging] level).
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to TOML configuration file. Global (place before the subcommand).",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── decode subcommand ──────────────────────────────────────────
    decode_parser = subparsers.add_parser(
        "decode",
        help="Decode MIE binary file to CSV.",
    )
    decode_parser.add_argument(
        "inputs",
        type=Path,
        nargs="*",
        metavar="INPUT",
        help=(
            "Path(s) to MIE binary recording file(s). Give more than one to "
            "merge them into a single time-sorted CSV (requires calendar-locked "
            "IRIG inputs). Mutually exclusive with --manifest / --glob."
        ),
    )
    decode_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Read input paths from a file (one per line; blank lines and "
            "#-comments ignored). Mutually exclusive with positionals / --glob."
        ),
    )
    decode_parser.add_argument(
        "--glob",
        dest="glob",
        default=None,
        metavar="PATTERN",
        help=(
            "Expand a single-directory glob (e.g. 'dir/*.mie'); '*' and '?' "
            "wildcards over the filename only (no recursion). Mutually "
            "exclusive with positionals / --manifest."
        ),
    )
    decode_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output CSV file path. If omitted, writes to stdout.",
    )
    decode_parser.add_argument(
        "--input-time-format",
        metavar="FORMAT",
        default=None,
        help=(
            "How timestamps are PARSED from the file (auto, irig, standard; "
            "case-insensitive). Overrides config file. Default: auto."
        ),
    )
    decode_parser.add_argument(
        "--output-time-format",
        metavar="FORMAT",
        default=None,
        help=(
            "How TIME_STAMP is WRITTEN to the CSV (doy, iso, dom; "
            "case-insensitive). doy (default) DAY:HH:MM:SS.uuuuuu, the vendor "
            "rendering; iso YYYY-MM-DDTHH:MM:SS.uuuuuu plus zone; dom "
            "DD:HH:MM:SS.uuuuuu. iso and dom require a year. L2-WRT-025."
        ),
    )
    decode_parser.add_argument(
        "--year",
        metavar="YYYY",
        default=None,
        help=(
            "Calendar year used to resolve the IRIG day-of-year field "
            "(range 1..9999). An MIE file carries no year, so iso and dom "
            "require one; doy ignores it. L2-WRT-026."
        ),
    )
    decode_parser.add_argument(
        "--utc-offset",
        metavar="OFFSET",
        default=None,
        help=(
            "Zone designator for the iso rendering (Z, +HH:MM or -HH:MM; "
            "default Z, meaning UTC). IRIG-B carries no timezone, so this "
            "states what the recording could not. L2-WRT-025."
        ),
    )
    # L2-CLI-019: --time-format was split in v3.0.0. Declared with
    # help=SUPPRESS so it is NOT advertised (the cross-implementation parity
    # gate scrapes --help), but is still recognised -- which is the only way to
    # give the operator a diagnostic naming both replacements instead of
    # argparse's generic "unrecognized arguments".
    decode_parser.add_argument(
        "--time-format",
        metavar="FORMAT",
        default=None,
        help=argparse.SUPPRESS,
    )
    # Filter flags take ONE value each, comma-separable and repeatable
    # (`--exclude-rts 15,31` == `--exclude-rts 15 --exclude-rts 31`),
    # matching the Rust CLI exactly. exclude_* merge with the config file;
    # include_* are CLI-only (L3-PY-013 / L3-RS-010).
    decode_parser.add_argument(
        "--exclude-types",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help=(
            "Exclude message types from output. Comma-separated, repeatable. "
            "Accepts names (MODE_COMMAND, BC_TO_RT, RT_TO_BC, RT_TO_RT, "
            "BROADCAST_BC_TO_RT, BROADCAST_RT_TO_RT, SPURIOUS_DATA) "
            "or hex codes (0x01, 0x02, etc.). Merges with config file."
        ),
    )
    decode_parser.add_argument(
        "--exclude-rts",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help=(
            "Exclude messages by RT address. Comma-separated, repeatable. Merges with config file."
        ),
    )
    decode_parser.add_argument(
        "--exclude-buses",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help=(
            "Exclude messages by bus (A, B). Comma-separated, repeatable. Merges with config file."
        ),
    )
    decode_parser.add_argument(
        "--exclude-subaddresses",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help=(
            "Exclude messages by subaddress. Comma-separated, repeatable. Merges with config file."
        ),
    )
    decode_parser.add_argument(
        "--include-types",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help=(
            "Include only these message types (same syntax as --exclude-types). "
            "Comma-separated, repeatable. CLI-only (no config-file key)."
        ),
    )
    decode_parser.add_argument(
        "--include-rts",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help="Include only these RT addresses. Comma-separated, repeatable. CLI-only.",
    )
    decode_parser.add_argument(
        "--include-buses",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help="Include only these buses (A, B). Comma-separated, repeatable. CLI-only.",
    )
    decode_parser.add_argument(
        "--include-subaddresses",
        action=_CommaSeparatedAppend,
        metavar="VAL",
        default=None,
        help="Include only these subaddresses. Comma-separated, repeatable. CLI-only.",
    )
    decode_parser.add_argument(
        "--separate-errors",
        action="store_true",
        default=False,
        help=(
            "Route errored and SPURIOUS_DATA records to a separate "
            "<output>_errors.csv, leaving only clean records in the main CSV. "
            "Default (omitted): every record goes to one CSV with the "
            "ERROR/ERROR_CODE columns populated. Stdout output is always "
            "inline (you cannot split stdout), so this flag is ignored there "
            'with a WARN. Mirrors [decode] error_mode = "separate".'
        ),
    )
    decode_parser.add_argument(
        "--no-clobber",
        action="store_true",
        default=False,
        help=(
            "Refuse to overwrite an existing output file (L2-WRT-017). "
            "Mirrors the output.no_clobber config key."
        ),
    )
    decode_parser.add_argument(
        "--allow-partial",
        action="store_true",
        default=False,
        help=(
            "On unrecoverable mid-file sync loss, write <output>.partial "
            "and exit 0 instead of exit 3 (L1-EXIT-004). Mirrors the "
            "decode.allow_partial config key."
        ),
    )
    decode_parser.add_argument(
        "--detect-records",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of records the timestamp-format auto-detect probe "
            "walks before committing to IRIG vs Standard (range 1..=32, "
            "default 8). L2-DEC-015. Mirrors the decode.detect_records "
            "config key."
        ),
    )
    decode_parser.add_argument(
        "--lookahead-records",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Total records checked by sync validation per call "
            "(1 candidate + N-1 look-ahead, range 1..=32, default 2). "
            "L2-SYN-026. Mirrors the decode.lookahead_records config "
            "key."
        ),
    )
    decode_parser.add_argument(
        "--standard-tick-rate-hz",
        type=float,
        default=None,
        metavar="HZ",
        help=(
            "Standard-counter frequency in Hz. When set, Standard "
            "timestamps are converted to microseconds and join DELTA "
            "tracking; must be > 0 (default: unset -> empty DELTA for "
            "Standard records). L2-DEC-017. Mirrors the "
            "decode.standard_tick_rate_hz config key."
        ),
    )
    decode_parser.add_argument(
        "--strict",
        action="store_true",
        default=None,
        help=(
            "Raise on invalid records instead of skipping them. Overrides "
            "the config file (default: lenient). Mirrors the decode.strict "
            "config key and the Rust --strict flag."
        ),
    )
    decode_parser.add_argument(
        "--format",
        default=None,
        metavar="FORMAT",
        help=(
            "Output format (csv only at present). Overrides the "
            "output.format config key, matching the Rust --format flag."
        ),
    )
    decode_parser.add_argument(
        "--no-mux",
        action="store_true",
        default=None,
        help=(
            "Leave the MUX column empty (vendor-exact output). By default MUX "
            "is derived from the input file name (L2-WRT-020). Mirrors "
            "[mux] enabled = false."
        ),
    )
    decode_parser.add_argument(
        "--mux-delimiter",
        default=None,
        metavar="D",
        help="MUX field separator (default '.'). Mirrors the mux.delimiter key.",
    )
    decode_parser.add_argument(
        "--mux-field",
        type=int,
        default=None,
        metavar="N",
        help=(
            "0-based MUX field index; negative counts from the end "
            "(default 4). Mirrors the mux.field config key."
        ),
    )
    decode_parser.add_argument(
        "--collapse-duplicates",
        action="store_true",
        default=None,
        help=(
            "Collapse the same bus transaction witnessed by multiple recorders "
            "into one row (multi-file merge only). Off by default. Mirrors "
            "[merge] collapse_duplicates = true."
        ),
    )
    decode_parser.add_argument(
        "--collapse-window-us",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Timestamp tolerance in microseconds for collapsing (default 0 = "
            "exact match). Mirrors the merge.collapse_window_us config key."
        ),
    )
    decode_parser.add_argument(
        "--delta-scope",
        type=str,
        default=None,
        metavar="SCOPE",
        choices=None,  # validated by parse_delta_scope for a shared error message
        help=(
            "Scope DELTA is measured over in a multi-file merge: per-file "
            "(default) measures each gap against the previous same-key record "
            "from the record's OWN file, matching a single-file decode; global "
            "measures across the merged timeline. No effect on a single input. "
            "L2-MRG-005. Mirrors the merge.delta_scope config key."
        ),
    )
    decode_parser.add_argument(
        "--max-sort-group",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum consecutive same-TIME_STAMP records buffered to order rows "
            "by RT then MSG (range 1..=1048576, default 4096). Use 1 to disable "
            "reordering and emit raw capture order. L2-WRT-022. Mirrors the "
            "output.max_sort_group config key."
        ),
    )
    decode_parser.add_argument(
        "--max-collapse-survivors",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum records the --collapse-duplicates window retains at once "
            "(range 1..=1048576, default 4096). Bounds the survivor set by "
            "COUNT where the window bounds it by TIME; past the cap collapsing "
            "is best-effort, with one WARN. L2-MRG-008. Mirrors the "
            "merge.max_collapse_survivors config key."
        ),
    )

    # ── count subcommand ───────────────────────────────────────────
    # Its own subcommand, matching the Rust CLI (`count <INPUT>`).
    # Counts valid records after applying the config file's [filter]
    # section; CLI filter flags are decode-only. Global --config applies.
    count_parser = subparsers.add_parser(
        "count",
        help="Print the message count (no CSV).",
    )
    count_parser.add_argument(
        "input",
        type=Path,
        help="Path to the MIE binary recording file.",
    )

    # ── dump subcommand ────────────────────────────────────────────
    dump_parser = subparsers.add_parser(
        "dump",
        help="Hex dump MIE binary file with record annotations.",
    )
    dump_parser.add_argument(
        "input",
        type=Path,
        help="Path to the MIE binary file.",
    )
    dump_parser.add_argument(
        "--raw",
        action="store_true",
        default=False,
        help="Raw hex dump without record parsing.",
    )
    dump_parser.add_argument(
        "--offset",
        type=_nonneg_int,
        default=0,
        help="Start offset in bytes (decimal or 0x hex). Default: 0.",
    )
    dump_parser.add_argument(
        "--length",
        type=_nonneg_int,
        default=None,
        help="Number of bytes to dump (raw mode; decimal or 0x hex). Default: all.",
    )
    dump_parser.add_argument(
        "--records",
        type=_nonneg_int,
        default=None,
        help="Max number of records to dump (record mode; decimal or 0x hex). Default: all.",
    )
    # dump only consumes [logging] level from the global --config; the
    # other decode-time keys (input_time_format, filters, strict, etc.) don't
    # apply to a hex dump.

    return parser


def _apply_config_logging(args: argparse.Namespace, config: DecoderConfig) -> None:
    """Apply `[logging]` precedence for both keys: CLI > TOML > default.

    ``main()`` already configured logging with the CLI value (or the
    ``"WARNING"`` default) before the TOML config was loaded. If the
    user did not pass ``--log-level``, re-configure with the TOML
    value now. ``config.log_level`` falls back to ``"WARNING"`` when
    the file has no ``[logging]`` section, so this is a no-op in the
    common case. Mirrors ``resolve_config`` in ``rust/src/cli.rs``.

    The IRIG day-of-year advisory (L2-LOG-001) is applied here for the same
    reason it lives in the logger rather than in the reader signature: every
    subcommand reaches the reader through this path, so one call covers
    ``decode`` / ``count`` / ``dump``.
    """
    if args.log_level is None:
        configure_logging(config.log_level)
    set_irig_day_advisory(config.irig_day_advisory and not args.no_irig_day_advisory)


def _resolve_decode_inputs(args: argparse.Namespace) -> list[Path]:
    """Resolve the decode input set from exactly one method (positionals,
    ``--manifest``, or ``--glob``), enforcing mutual exclusivity and the
    ``MAX_MERGE_FILES`` cap (L2-MRG-001).

    Raises:
        ValueError: usage problems (no method / combined methods / empty
            resolution / over-cap) → the caller maps to exit 4.
        OSError: a manifest that cannot be read or a glob directory that does
            not exist → the caller maps to exit 1.

    Returns:
        The resolved input paths. Always at least one -- an empty resolution is
        raised as a usage error rather than returned.
    """
    from aero1553.merge import MAX_MERGE_FILES, expand_glob, read_manifest

    methods = sum([bool(args.inputs), args.manifest is not None, args.glob is not None])
    if methods == 0:
        raise ValueError("decode requires an input file (positional, --manifest, or --glob)")
    if methods > 1:
        raise ValueError(
            "decode accepts only one input method: positional paths, "
            "--manifest, or --glob -- not a combination"
        )

    if args.manifest is not None:
        try:
            paths = read_manifest(args.manifest)
        except UnicodeDecodeError as exc:
            # A non-text manifest is a runtime input error (exit 1), matching
            # the Rust reader's read_to_string failure — not a usage error.
            raise OSError(f"manifest {args.manifest} is not valid UTF-8 text") from exc
    elif args.glob is not None:
        paths = expand_glob(args.glob)
    else:
        paths = list(args.inputs)

    if not paths:
        if args.manifest is not None:
            raise ValueError(f"manifest {args.manifest} contains no input paths")
        if args.glob is not None:
            raise ValueError(f"--glob {args.glob!r} matched no files")
        raise ValueError("decode requires at least one input file")
    if len(paths) > MAX_MERGE_FILES:
        raise ValueError(
            f"too many input files: {len(paths)} (maximum is {MAX_MERGE_FILES}); "
            f"split the set into smaller batches"
        )
    return paths


def _merge_output_collision(
    output: Path,
    inputs: list[Path],
    *,
    split_errors: bool = False,
    allow_partial: bool = False,
) -> str | None:
    """Return an error message if a merge could commit over one of its inputs
    (L2-WRT-014 across the input set), else None. ``Path.resolve`` is
    non-strict, so a not-yet-existing output resolves fine.

    Checks **every** path the run could commit, not just the destination the
    operator named: :func:`writer.commit_targets` enumerates the derived errors
    file and the ``.partial`` variants alongside it. The writer runs the same
    enumeration for a single-input decode, where it knows the one input; on the
    merge path it is given ``input_path=None`` and this is the only guard that
    sees the input set at all.

    Args:
        output: The destination the operator named.
        inputs: Every resolved merge input.
        split_errors: True when errored rows go to their own file.
        allow_partial: True when a sync loss commits ``.partial`` output.

    Returns:
        The operator-facing error message, or ``None`` when there is no
        collision.
    """
    from aero1553.writer import commit_targets

    for target in commit_targets(output, split_errors, allow_partial):
        target_resolved = target.resolve()
        for inp in inputs:
            if inp.resolve() == target_resolved:
                # Naming which target collided matters: told only that the
                # output collides, an operator looks at ``-o`` and sees a name
                # that is plainly different from every input.
                role = "output path" if target == output else "derived output path"
                return (
                    f"{role} {target} resolves to merge input {inp}; choose a different output path"
                )
    return None


def _validate_int_range(value: int, flag: str, lo: int, hi: int) -> int:
    """Return ``value`` if within ``[lo, hi]``, else raise ``ValueError``.

    The bounds mirror the TOML load-time checks in ``config.load_config``;
    validating the CLI value here (post-parse) keeps an out-of-range value a
    usage error (the caller maps ``ValueError`` to EXIT_USAGE) rather than
    argparse's default exit code 2.

    Returns:
        ``value``, unchanged, when it is within ``[lo, hi]``.

    Raises:
        ValueError: if ``value`` is outside the range. The caller maps this to
            EXIT_USAGE.
    """
    if not (lo <= value <= hi):
        raise ValueError(f"invalid {flag}: {value}; valid range: [{lo}, {hi}]")
    return value


def _validate_positive_finite(value: float, flag: str) -> float:
    """Return ``value`` if finite and strictly positive, else raise
    ``ValueError`` (L2-DEC-017 / L2-CLI-012).

    Returns:
        ``value``, unchanged, when it is finite and strictly positive.

    Raises:
        ValueError: if ``value`` is not finite, or is zero or negative.
    """
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"invalid {flag}: {value}; must be a finite value greater than 0")
    return value


def _validate_nonempty(value: str, flag: str) -> str:
    """Return ``value`` if non-empty, else raise ``ValueError``.

    Returns:
        ``value``, unchanged, when it is non-empty.

    Raises:
        ValueError: if ``value`` is the empty string.
    """
    if value == "":
        raise ValueError(f"invalid {flag}: must be a non-empty string")
    return value


def _simple_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Build the CLI overrides that map straight onto config fields.

    A boolean flag flips a value on; its absence leaves the config value intact
    (there is no "off" form on the CLI). ``--separate-errors`` flips error_mode to
    SEPARATE; the default IS inline.

    Two of these do carry a validation: ``--input-time-format`` (via
    ``parse_timestamp_format``) and ``--format``. Both are value-set checks that
    belong at parse time, because an invalid flag value is a usage error
    (exit 4, L1-EXIT-007) rather than a runtime or configuration one.

    Returns:
        The override mapping. Only flags actually given are present, so an
        absent flag leaves the config value intact.

    Raises:
        ValueError: if a flag's value is outside its accepted set. The caller
            maps this to EXIT_USAGE.
    """
    from aero1553.models import (
        ErrorMode,
        parse_output_time_format,
        parse_timestamp_format,
    )

    overrides: dict[str, object] = {}
    # L2-CLI-019: refuse the retired spelling before anything else, so the
    # pointer is what the operator sees rather than a downstream complaint.
    if args.time_format is not None:
        raise ValueError(RETIRED_TIME_FORMAT_MESSAGE)
    # L2-CLI-018: the diagnostic names the FLAG, not the config key. The
    # shared parsers phrase their message for the TOML side, so each is wrapped
    # here -- an operator who typed a flag should be told which flag was wrong.
    if args.input_time_format is not None:
        try:
            overrides["input_time_format"] = parse_timestamp_format(args.input_time_format)
        except ValueError as exc:
            raise ValueError(
                f"invalid --input-time-format: {args.input_time_format!r}; "
                f"valid: auto, irig, standard"
            ) from exc
    if args.output_time_format is not None:
        try:
            overrides["output_time_format"] = parse_output_time_format(args.output_time_format)
        except ValueError as exc:
            raise ValueError(
                f"invalid --output-time-format: {args.output_time_format!r}; valid: doy, iso, dom"
            ) from exc
    if args.year is not None:
        overrides["year"] = _parse_year_arg(args.year)
    if args.utc_offset is not None:
        overrides["utc_offset_minutes"] = _parse_utc_offset_arg(args.utc_offset)
    if args.separate_errors:
        overrides["error_mode"] = ErrorMode.SEPARATE
    if args.no_clobber:
        overrides["no_clobber"] = True
    if args.allow_partial:
        overrides["allow_partial"] = True
    if args.strict is not None:
        overrides["strict"] = args.strict
    if args.format is not None:
        # L1-EXIT-007: validated here, alongside every other flag value, so an
        # unsupported format is a USAGE error (exit 4). The caller maps this
        # ValueError to EXIT_USAGE. It used to be checked after the config
        # merge, which made it a runtime error (exit 1) and contradicted the
        # requirement. The config-file spelling stays a load-time error
        # (exit 5, L2-CFG-010): a bad value in a file is a configuration
        # problem, not a mistyped command.
        if args.format != "csv":
            raise ValueError(f"invalid --format: {args.format!r}; valid: csv")
        overrides["output_format"] = args.format
    if args.no_mux:
        overrides["mux_enabled"] = False
    if args.mux_field is not None:
        overrides["mux_field"] = args.mux_field
    if args.collapse_duplicates:
        overrides["collapse_duplicates"] = True
    return overrides


def _filter_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Parse the ``--exclude-*`` / ``--include-*`` filter values.

    types/buses parse via name-or-hex; rts/subaddresses via the u8 (0-255)
    parser mirroring the Rust CLI. Any bad value raises ``ValueError`` (the
    caller maps it to EXIT_USAGE). include_* are CLI-only (L3-PY-013).

    Returns:
        The filter override mapping, holding only the filters actually given.
    """
    from aero1553.config import _parse_bus_names, _parse_type_names

    overrides: dict[str, object] = {}
    if args.exclude_types is not None:
        overrides["exclude_types"] = _parse_type_names(args.exclude_types)
    if args.exclude_rts is not None:
        overrides["exclude_rts"] = _parse_u8_list(args.exclude_rts, "--exclude-rts")
    if args.exclude_buses is not None:
        overrides["exclude_buses"] = _parse_bus_names(args.exclude_buses)
    if args.exclude_subaddresses is not None:
        overrides["exclude_subaddresses"] = _parse_u8_list(
            args.exclude_subaddresses, "--exclude-subaddresses"
        )
    if args.include_types is not None:
        overrides["include_types"] = _parse_type_names(args.include_types)
    if args.include_rts is not None:
        overrides["include_rts"] = _parse_u8_list(args.include_rts, "--include-rts")
    if args.include_buses is not None:
        overrides["include_buses"] = _parse_bus_names(args.include_buses)
    if args.include_subaddresses is not None:
        overrides["include_subaddresses"] = _parse_u8_list(
            args.include_subaddresses, "--include-subaddresses"
        )
    return overrides


def _validated_numeric_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Build overrides for the numeric/string args that carry range/format
    checks, raising ``ValueError`` (usage) on an invalid value. The bounds
    mirror the TOML load-time checks (L2-DEC-015 / L2-SYN-026 / L2-DEC-017 /
    L2-CLI-012).

    Returns:
        The override mapping for the numeric and string arguments given.

    Raises:
        ValueError: on any out-of-range or malformed value. The caller maps
            this to EXIT_USAGE.
    """
    from aero1553.config import (
        DETECT_RECORDS_MAX,
        DETECT_RECORDS_MIN,
        LOOKAHEAD_RECORDS_MAX,
        LOOKAHEAD_RECORDS_MIN,
    )
    from aero1553.merge import (
        MAX_COLLAPSE_SURVIVORS_MAX,
        MAX_COLLAPSE_SURVIVORS_MIN,
    )
    from aero1553.order import MAX_SORT_GROUP_MAX, MAX_SORT_GROUP_MIN

    overrides: dict[str, object] = {}
    if args.detect_records is not None:
        overrides["detect_records"] = _validate_int_range(
            args.detect_records,
            "--detect-records",
            DETECT_RECORDS_MIN,
            DETECT_RECORDS_MAX,
        )
    if args.lookahead_records is not None:
        overrides["lookahead_records"] = _validate_int_range(
            args.lookahead_records,
            "--lookahead-records",
            LOOKAHEAD_RECORDS_MIN,
            LOOKAHEAD_RECORDS_MAX,
        )
    if args.standard_tick_rate_hz is not None:
        overrides["standard_tick_rate_hz"] = _validate_positive_finite(
            args.standard_tick_rate_hz, "--standard-tick-rate-hz"
        )
    if args.mux_delimiter is not None:
        overrides["mux_delimiter"] = _validate_nonempty(args.mux_delimiter, "--mux-delimiter")
    if args.collapse_window_us is not None:
        if args.collapse_window_us < 0:
            raise ValueError("--collapse-window-us must be a non-negative integer")
        overrides["collapse_window_us"] = args.collapse_window_us
    if args.delta_scope is not None:
        from aero1553.models import parse_delta_scope

        overrides["delta_scope"] = parse_delta_scope(args.delta_scope)
    if args.max_collapse_survivors is not None:
        overrides["max_collapse_survivors"] = _validate_int_range(
            args.max_collapse_survivors,
            "--max-collapse-survivors",
            MAX_COLLAPSE_SURVIVORS_MIN,
            MAX_COLLAPSE_SURVIVORS_MAX,
        )
    if args.max_sort_group is not None:
        overrides["max_sort_group"] = _validate_int_range(
            args.max_sort_group,
            "--max-sort-group",
            MAX_SORT_GROUP_MIN,
            MAX_SORT_GROUP_MAX,
        )
    return overrides


def _build_decode_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Assemble all CLI → config overrides, raising ``ValueError`` (which the
    caller maps to EXIT_USAGE) on any invalid value.

    Returns:
        The merged override mapping from the simple, filter and validated
        numeric groups.
    """
    overrides: dict[str, object] = {}
    overrides.update(_simple_overrides(args))
    overrides.update(_filter_overrides(args))
    overrides.update(_validated_numeric_overrides(args))
    return overrides


def _resolve_time_render(config: DecoderConfig) -> TimeRender:
    """Resolve the merged configuration into a :class:`TimeRender`.

    Enforces the L2-WRT-026 clause 1 precondition. The check lives here rather
    than in either loader because neither can answer it alone: a config file may
    set the rendering and the CLI supply the year, or the reverse. It runs
    before the output is opened, so a run that cannot produce the requested
    dates writes nothing at all.

    Raises:
        ValueError: when a calendar rendering has no year from either source.

    Returns:
        The resolved rendering.
    """
    from aero1553.models import TimeRender

    if config.output_time_format.needs_calendar() and config.year is None:
        raise ValueError(
            f"--output-time-format {config.output_time_format.name.lower()} needs a "
            f"calendar year, and an MIE recording does not carry one: IRIG-B encodes "
            f"day-of-year but not the year. Set [output] year = YYYY in a config file "
            f"or pass --year YYYY. (--output-time-format doy needs no year.)"
        )
    return TimeRender(
        format=config.output_time_format,
        year=config.year,
        utc_offset_minutes=config.utc_offset_minutes,
    )


def _open_reader(path: Path, config: DecoderConfig) -> MieFileReader:
    """Open one input file with reader options from ``config`` (mirrors
    ``open_reader`` in ``rust/src/cli.rs``). Raises ``MieFileError`` on a
    file/open failure (the caller maps it to EXIT_RUNTIME).

    Returns:
        An open :class:`MieFileReader` for ``path``.
    """
    from aero1553.reader import MieFileReader

    return MieFileReader(
        path,
        input_time_format=config.input_time_format,
        strict=config.strict,
        detect_records=config.detect_records,
        lookahead_records=config.lookahead_records,
        standard_tick_rate_hz=config.standard_tick_rate_hz,
        mux_enabled=config.mux_enabled,
        mux_delimiter=config.mux_delimiter,
        mux_field=config.mux_field,
        # Some(_) only when a calendar rendering is actually in force, so a
        # year left in a site config does not change the reader's diagnostics
        # for a doy run (L2-WRT-026 clause 5).
        calendar_year=config.year if config.output_time_format.needs_calendar() else None,
    )


def _check_merge_output_collision(
    args: argparse.Namespace,
    input_paths: list[Path],
    config: DecoderConfig,
    *,
    merge_requested: bool,
) -> int | None:
    """For a merge writing to a file, reject any path the run could commit that
    resolves to one of the inputs (L2-WRT-014 across the set); a single input
    uses the writer's own check, which runs the same enumeration. Returns an
    exit code to short-circuit on, or ``None`` to continue.

    ``config``, not ``args``, decides the mode: a site config file can select
    separate-errors or allow-partial without the corresponding flag ever
    appearing on the command line, and enumerating from the flags alone would
    leave exactly those runs unguarded.

    Gated on whether a merge was *requested*, not on the surviving reader count:
    ``--allow-partial`` can drop a multi-input merge to a single open reader, and
    in that case the writer's own single-input guard is also bypassed (it receives
    ``input_path=None`` whenever a merge was requested), so this is the only guard
    that runs — mirroring the Rust CLI, which gates on ``merge_requested``.

    Returns:
        ``EXIT_RUNTIME`` to short-circuit on a collision, or ``None`` to
        continue.
    """
    if merge_requested and args.output is not None:
        from aero1553.models import ErrorMode

        collision = _merge_output_collision(
            args.output,
            input_paths,
            split_errors=config.error_mode == ErrorMode.SEPARATE,
            allow_partial=config.allow_partial,
        )
        if collision is not None:
            logger.error("%s", collision)
            print(f"Error: {collision}", file=sys.stderr)
            return EXIT_RUNTIME
    return None


def _build_message_stream(
    readers: list[MieFileReader],
    config: DecoderConfig,
    *,
    merge_requested: bool = False,
    open_dropped: bool = False,
) -> Iterator[MieMessage]:
    """Build the decoded-message stream: a single filtered reader, or the
    time-sorted k-way merge of several (L2-MRG-002). DELTA is per-file on both
    paths unless ``--delta-scope global`` is given (L2-MRG-005).

    ``merge_requested`` routes by the *requested* input count (not the surviving
    reader count) so an --allow-partial merge that dropped an input at open time
    still uses the merge path. ``open_dropped`` appends a terminal after the good
    rows so the writer commits a `.partial` (L2-MRG-004). ``merge_readers``
    validates the input set eagerly, so an incompatible set raises
    ``MieIncompatibleMergeInputsError`` here before any output.

    Returns:
        The message stream to hand the writer, already filtered and in
        canonical row order.
    """
    from aero1553.filters import apply_filters
    from aero1553.merge import merge_readers
    from aero1553.order import order_rows

    # L2-WRT-021: canonical row order is the LAST stage before the writer — after
    # the merge and after filtering — so the ordering guarantee holds over exactly
    # the rows that reach the CSV.
    if not merge_requested:
        return order_rows(apply_filters(readers[0], config.filters), config.max_sort_group)
    merged = merge_readers(
        readers,
        standard_tick_rate_hz=config.standard_tick_rate_hz,
        allow_partial=config.allow_partial,
        strict=config.strict,
        collapse_duplicates=config.collapse_duplicates,
        collapse_window_us=config.collapse_window_us,
        max_collapse_survivors=config.max_collapse_survivors,
        delta_scope=config.delta_scope,
    )
    stream = order_rows(apply_filters(merged, config.filters), config.max_sort_group)
    if open_dropped:
        # The terminal must stay last, so it wraps OUTSIDE the reorder stage.
        return _append_open_terminal(stream)
    return stream


def _append_open_terminal(stream: Iterator[MieMessage]) -> Iterator[MieMessage]:
    """Yield from ``stream`` then raise an unrecoverable-sync-loss terminal so the
    writer commits a `.partial` (L2-MRG-004). Used when a merge dropped an input
    at open time — that file contributed nothing (truncated at offset 0).

    Yields:
        Every message from ``stream``, unchanged.

    Raises:
        MieUnrecoverableSyncLossError: always, once ``stream`` is exhausted.
            The terminal is the point of this wrapper.
    """
    yield from stream
    raise MieUnrecoverableSyncLossError(0, 0)


def _write_messages(
    messages: Iterator[MieMessage],
    output: Path | None,
    error_mode: ErrorMode,
    write_opts: WriteOptions,
) -> WriteOutcome:
    """Write the stream and log the outcome (mirrors ``write_messages`` in
    ``rust/src/cli.rs``). Separate mode with a file output writes the split
    CSVs; INLINE mode (or stdout, which cannot be split) writes one CSV.

    Returns:
        The row counts from the writer, including any ``.partial`` commit.
    """
    from aero1553.models import ErrorMode
    from aero1553.writer import write_csv, write_csv_split

    if error_mode == ErrorMode.SEPARATE and output is None:
        # A stream cannot be split in two, so separate mode degrades to inline.
        # Now that separate is opt-in this WARN reports an explicit request the
        # writer could not honour, rather than (as before) firing on every
        # stdout decode because separate happened to be the default. Mirrors the
        # same warning in `write_messages` (rust/src/cli.rs).
        logger.warning("stdout output forces inline error mode")

    if error_mode == ErrorMode.SEPARATE and output is not None:
        outcome = write_csv_split(messages, output=output, opts=write_opts)
        logger.info(
            "wrote %d messages + %d errors to %s",
            outcome.normal_count,
            outcome.error_count,
            output,
        )
    else:
        # INLINE mode, or stdout (can't split stdout). The writer already logs
        # the row count and destination, so there is no second summary here —
        # Rust logs exactly one line for this path and a duplicate on only one
        # implementation is the kind of drift this file exists to avoid.
        outcome = write_csv(messages, output=output, opts=write_opts)
    return outcome


# Decode-time errors that follow the standard log + stderr (+ optional exit-class
# line) shape, in match order (specific before base). Mirrors the error arms of
# ``classify_decode_exit`` in ``rust/src/cli.rs``. The three non-standard arms
# (sync-loss's sync_losses detail, broken-pipe's exit-0, writer's own wording)
# stay explicit below.
_SIMPLE_DECODE_ERRORS: tuple[
    tuple[type[Exception] | tuple[type[Exception], ...], int, str | None], ...
] = (
    (MieIncompatibleMergeInputsError, EXIT_MERGE_INCOMPATIBLE, "merge-incompatible"),
    ((MieInputOutputCollisionError, MieClobberRefusedError), EXIT_RUNTIME, None),
    (MieNoValidRecordsError, EXIT_NO_RECORDS, "no-records"),
    (MieHomogeneousPayloadError, EXIT_NO_RECORDS, "no-records"),
    (MieTimestampFormatMismatchError, EXIT_NO_RECORDS, "no-records (timestamp-format-mismatch)"),
    # L2-WRT-026 + L1-EXIT-002: the operator asked for a rendering the recording
    # cannot supply. Same class as the format mismatch and for the same reason --
    # the flag and the file disagree, and the file wins.
    (MieCalendarUnavailableError, EXIT_NO_RECORDS, "no-records (calendar-unavailable)"),
    (MieNonMonotonicInputError, EXIT_RUNTIME, "non-monotonic-input (strict)"),
)


def _classify_decode_error(exc: Exception) -> int:
    """Map a decode-time exception to its exit code, emitting the stderr and
    exit-class log lines. Mirrors ``classify_decode_exit`` in ``rust/src/cli.rs``.

    Returns:
        The process exit code for this failure. Note ``EXIT_OK`` is among the
        possibilities: a broken pipe and an ``--allow-partial`` sync loss are
        both clean exits.
    """
    from aero1553.writer import is_broken_pipe

    for exc_types, code, exit_class in _SIMPLE_DECODE_ERRORS:
        if isinstance(exc, exc_types):
            return _report_decode_error(exc, code, exit_class)

    if isinstance(exc, MieUnrecoverableSyncLossError):
        # L1-EXIT-004 → exit 3 (allow_partial is caught inside the writer).
        logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        logger.info(
            "decode exit class: partial-unrecoverable (sync_losses=%d); "
            "pass --allow-partial to preserve the rows decoded so far",
            exc.sync_losses,
        )
        return EXIT_SYNC_LOSS
    if is_broken_pipe(exc):
        # L2-WRT-018 — usually handled inside the streaming writer; cover the
        # edge case where it escapes. Classified by `is_broken_pipe` rather than
        # `isinstance(exc, BrokenPipeError)` so the Windows form (a bare
        # `OSError` with EINVAL) is recognized too.
        logger.info("decode exit class: complete (broken-pipe on stdout)")
        return EXIT_OK
    if isinstance(exc, MieWriterError):
        logger.error("Write failed: %s", exc)
        print(f"Error writing output: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    # Any remaining Aero1553Error (record errors, generic file errors).
    logger.error("%s", exc)
    print(f"Error: {exc}", file=sys.stderr)
    return EXIT_RUNTIME


def _report_decode_error(exc: Exception, code: int, exit_class: str | None) -> int:
    """Standard decode-error reporting: log the error, print it to stderr, and
    emit the exit-class INFO line when one is given; return the exit code.

    Returns:
        ``code``, unchanged.
    """
    logger.error("%s", exc)
    print(f"Error: {exc}", file=sys.stderr)
    if exit_class is not None:
        logger.info("decode exit class: %s", exit_class)
    return code


def _classify_decode_success(outcome: WriteOutcome, readers: list[MieFileReader]) -> int:
    """Emit the L1-EXIT-005 exit-class summary for a successful run and return
    EXIT_OK (mirrors the ``Ok`` arm of Rust ``classify_decode_exit``).

    Returns:
        ``EXIT_OK``. A successful run has no other outcome.
    """
    sync_losses = sum(r.sync_losses for r in readers)
    # L1-EXIT-010: report the empty-recording class only when *every* opened
    # input was a valid empty recording (so a merge that also drew rows from a
    # non-empty input stays "complete"). The writer has already produced a
    # header-only CSV.
    empty_recording = bool(readers) and all(r.empty_recording for r in readers)
    if outcome.partial is not None:
        cls = "partial-unrecoverable"
    elif empty_recording and outcome.normal_count == 0 and outcome.error_count == 0:
        cls = "empty-recording"
    elif sync_losses > 0:
        cls = "partial-recovered"
    else:
        cls = "complete"
    logger.info("decode exit class: %s (sync_losses=%d)", cls, sync_losses)
    return EXIT_OK


def _open_readers_for_decode(
    input_paths: list[Path],
    config: DecoderConfig,
    *,
    merge_requested: bool,
) -> tuple[list[MieFileReader], bool]:
    """Open one reader per resolved input.

    Under a merge with ``--allow-partial``, a per-file open failure (empty /
    unreadable / missing input) is tolerated: that input is dropped with a WARN
    and the batch commits as ``.partial`` (L2-MRG-004), mirroring a priming-time
    or mid-file failure. Otherwise the failure propagates. Returns the readers
    and whether any input was dropped.

    Raises:
        MieFileError: An open failure the caller must map to a runtime exit.

    Returns:
        ``(readers, open_dropped)`` -- the successfully opened readers, and
        whether any input was dropped. A drop only happens under a merge with
        ``--allow-partial``.
    """
    readers: list[MieFileReader] = []
    open_dropped = False
    for p in input_paths:
        try:
            readers.append(_open_reader(p, config))
        except MieFileError as exc:
            if merge_requested and config.allow_partial:
                logger.warning(
                    "merge: input %s could not be opened; truncating it from the "
                    "merge (--allow-partial): %s",
                    _log_safe(p),
                    _log_safe(exc),
                )
                open_dropped = True
            else:
                raise
    return readers, open_dropped


def _run_decode(args: argparse.Namespace) -> int:
    """Execute the decode subcommand.

    Loads configuration from file (if specified), merges with CLI
    arguments, configures filtering, and runs the decode pipeline.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code from the L2-CLI-011 taxonomy documented on
        :func:`main` — the decode path can produce any class except the
        usage error (4), which is handled during argument parsing.
    """
    from aero1553.config import load_config

    # ── Load and merge configuration ───────────────────────────────
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    _apply_config_logging(args, config)

    try:
        overrides = _build_decode_overrides(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    config = config.with_overrides(**overrides)

    # No post-merge output_format check: both ways of setting it are rejected
    # before this point -- the CLI value at parse time (exit 4, in
    # _build_decode_overrides) and the config-file value at load time (exit 5,
    # L2-CFG-010). A third check here would be unreachable.

    # ── Resolve the input set (positionals / --manifest / --glob) ──
    try:
        input_paths = _resolve_decode_inputs(args)
    except ValueError as exc:
        # Usage problems: no method, combined methods, empty resolution,
        # over-cap (L2-MRG-001).
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        # Manifest unreadable / glob directory missing.
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    # ── Open a reader per input ────────────────────────────────────
    # Under --allow-partial a *merge* tolerates a per-file OPEN failure (an
    # empty / unreadable / missing input): it drops that input with a WARN and
    # commits the batch as `.partial` (L2-MRG-004), mirroring a priming-time or
    # mid-file failure. A single-input decode is unaffected.
    merge_requested = len(input_paths) > 1
    try:
        readers, open_dropped = _open_readers_for_decode(
            input_paths, config, merge_requested=merge_requested
        )
    except MieFileError as exc:
        return _report_error("Failed to open input file", exc, EXIT_RUNTIME)

    for r in readers:
        logger.info("Opened %s (%d bytes)", r.path.name, r.file_size)

    collision_code = _check_merge_output_collision(
        args, input_paths, config, merge_requested=merge_requested
    )
    if collision_code is not None:
        return collision_code

    from aero1553.writer import WriteOptions

    # L2-WRT-026 clause 1: a calendar rendering needs a year, and whether one
    # was supplied is a question about the *resolved* pair -- either source may
    # have provided it -- so it is asked here, after the merge, and before the
    # output is opened.
    try:
        time_render = _resolve_time_render(config)
    except ValueError as exc:
        return _report_error("usage", exc, EXIT_USAGE)

    write_opts = WriteOptions(
        input_path=None if merge_requested else input_paths[0],
        no_clobber=config.no_clobber,
        allow_partial=config.allow_partial,
        time_render=time_render,
    )

    # ── Build the message stream and write it ──────────────────────
    # One input → the single-file path; two or more → the time-sorted k-way
    # merge (L2-MRG-002), which validates eagerly. DELTA is per-file on both
    # paths unless --delta-scope global is given (L2-MRG-005). Build- and
    # write-time decode failures (and a broken pipe on stdout) map to exit codes
    # via _classify_decode_error; a clean run is classified by the cumulative
    # sync-loss count (L1-EXIT-005).
    try:
        messages = _build_message_stream(
            readers, config, merge_requested=merge_requested, open_dropped=open_dropped
        )
        outcome = _write_messages(messages, args.output, config.error_mode, write_opts)
    except (Aero1553Error, OSError) as exc:
        # OSError (rather than just BrokenPipeError) so the Windows broken-pipe
        # form reaches the classifier as a clean exit per L2-WRT-018; any other
        # OSError that escapes the writer is classified as a runtime failure
        # there, which beats surfacing a traceback.
        return _classify_decode_error(exc)

    return _classify_decode_success(outcome, readers)


def _run_count(args: argparse.Namespace) -> int:
    """Execute the count subcommand (L3-PY-010).

    Counts valid records after applying the config file's ``[filter]``
    section (CLI filter flags are decode-only), printing the integer
    count to stdout and a human-readable status line to stderr. Mirrors
    the Rust ``count`` subcommand.

    Returns:
        Process exit code from the L2-CLI-011 taxonomy documented on
        :func:`main`: success (0), no valid records (2), runtime error (1),
        or config error (5).
    """
    from aero1553.config import load_config
    from aero1553.filters import apply_filters
    from aero1553.reader import MieFileReader

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    _apply_config_logging(args, config)

    try:
        reader = MieFileReader(
            args.input,
            input_time_format=config.input_time_format,
            strict=config.strict,
            detect_records=config.detect_records,
            lookahead_records=config.lookahead_records,
            standard_tick_rate_hz=config.standard_tick_rate_hz,
        )
    except MieFileError as exc:
        return _report_error("Failed to open input file", exc, EXIT_RUNTIME)

    messages = apply_filters(reader, config.filters)
    try:
        count = sum(1 for _ in messages)
    except (
        MieNoValidRecordsError,
        MieHomogeneousPayloadError,
        MieTimestampFormatMismatchError,
    ) as exc:
        # Align with decode (L2-CLI-011): a wrong-file rejection maps to exit 2,
        # not the generic runtime exit 1. (An empty recording raises nothing —
        # the iterator simply yields zero records — so count prints 0 and exits
        # 0 per L1-EXIT-010.)
        return _report_error("Count failed", exc, EXIT_NO_RECORDS)
    except Aero1553Error as exc:
        # Any other decode error during the count maps to a runtime failure
        # (exit 1), matching the Rust count subcommand.
        return _report_error("Count failed", exc, EXIT_RUNTIME)

    # L3-PY-010: integer count to stdout (the machine-readable datum),
    # human-friendly status with path context to stderr (always emitted,
    # not gated by --log-level so an interactive operator sees context).
    print(count)
    if reader.empty_recording:
        print(
            f"no records in {reader.path.name} (empty recording -- opens on "
            f"the end-of-records terminator)",
            file=sys.stderr,
        )
    else:
        print(f"counted {count} messages in {reader.path.name}", file=sys.stderr)
    return EXIT_OK


def _run_dump(args: argparse.Namespace) -> int:
    """Execute the dump subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code from the L2-CLI-011 taxonomy documented on
        :func:`main`: success (0), runtime error (1), or config error (5).
    """
    from aero1553.config import load_config
    from aero1553.dump import hex_dump_raw, hex_dump_records
    from aero1553.writer import is_broken_pipe

    # dump only consumes log_level from config (input_time_format, strict,
    # filters, etc. don't apply to a raw / record hex dump). Load so
    # the TOML [logging] level is honored — same precedence as decode.
    # Mirrors the Rust dump path's resolve_config call.
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    _apply_config_logging(args, config)

    try:
        if args.raw:
            hex_dump_raw(
                args.input,
                start_offset=args.offset,
                length=args.length,
            )
        else:
            hex_dump_records(
                args.input,
                max_records=args.records,
                start_offset=args.offset,
            )
    except MieFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except OSError as exc:
        # L2-WRT-018: `aero1553 dump big.mie | head` closes the pipe long
        # before the dump finishes. That is a clean termination, not a failure —
        # mirrors `finish_dump` in `rust/src/cli.rs`. Any other output error
        # (disk full, permission) is a runtime failure, as it is for decode.
        if not is_broken_pipe(exc):
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_RUNTIME
        logger.info("dump: broken-pipe on stdout, exiting 0")
        return EXIT_OK

    return EXIT_OK


def _force_utf8_streams() -> None:
    """Emit UTF-8 with LF line endings on stdout/stderr, whatever the platform.

    Two separate problems, both Windows-only.

    **Encoding.** A redirected stdout defaults to the active code page (cp1252
    in most locales), which cannot encode the em-dash and section marks in log
    messages — they garble to ``?``. The ``dump`` report itself is now pure
    ASCII (it is a stdout *payload*, diffed and piped, so it matches the Rust
    and C++ reports byte for byte), but the diagnostics around it are prose and
    still use them.

    **Line endings.** A text-mode stream translates ``"\\n"`` to ``"\\r\\n"`` on
    Windows. This was previously left alone on the grounds that the byte-exact
    CSV contract was unaffected — true only of CSV written to a *file*, which is
    opened separately with ``newline=""``. Everything written to **stdout** went
    through the translation: ``decode -o -`` emitted CRLF where Rust and C++
    emit LF, in plain violation of ``L2-WRT-012`` ("CSV output SHALL use LF line
    endings on every supported platform"), and so did every line of ``dump``.
    No conformance case caught it, because the runner always decodes to a file.

    Streams that cannot be reconfigured (already detached, or replaced by a test
    harness / capture object without ``reconfigure``) are left as-is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # Detached buffer, or a stream that refuses reconfiguration — the dump
        # and log output are best-effort in that case, not a hard error.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", newline="\n")


def _normalize_version_flag(argv: list[str]) -> list[str]:
    """Accept ``--version`` in any letter case by rewriting a case-insensitive
    long version token to the canonical ``--version`` argparse knows. The short
    ``-v`` / ``-V`` forms are registered directly on the parser. Keeps the Python
    and Rust CLIs in agreement on every spelling of the version flag.

    Returns:
        The argument list with any case-insensitive long version token
        rewritten to ``--version``. Every other argument is passed through.
    """
    return [
        "--version" if arg.startswith("--") and arg[2:].lower() == "version" else arg
        for arg in argv
    ]


def _neutralise_dead_stdout() -> None:
    """Point fd 1 at the null device once stdout is known to be unwritable.

    CPython flushes ``sys.stdout`` during interpreter shutdown. If the pipe is
    already gone that flush raises, CPython prints "Exception ignored while
    flushing sys.stdout" and — the part that actually matters — **overrides the
    process exit status with 120**. So a decode that correctly returned 0 after
    a broken pipe still exited non-zero, violating L2-WRT-018 (observed on
    Python 3.14 / Linux; earlier versions happened to leave an empty buffer and
    so escaped it).

    Repointing the file descriptor makes the shutdown flush a silent no-op.
    This is deliberately *not* done inside :func:`main`: it is fd-level surgery
    that would corrupt the output capture of any in-process caller (pytest, an
    embedding application). It belongs at the real process boundary, which is
    what :func:`main_cli` is.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:  # pragma: no cover - os.devnull is always openable
        return
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):  # pragma: no cover
        pass
    finally:
        os.close(devnull)


def main_cli(argv: list[str] | None = None) -> int:
    """Console-script / ``python -m`` entry point.

    Runs :func:`main` and then makes sure a dead stdout cannot turn a clean exit
    code into CPython's shutdown-failure 120. Kept separate from :func:`main`
    so importing callers get a side-effect-free function.

    Returns:
        The process exit code, with a dead stdout already handled so it cannot
        become CPython's shutdown-failure 120.
    """
    code = main(argv)
    try:
        sys.stdout.flush()
    except OSError:
        # BrokenPipeError is the case this exists for, but it is a subclass of
        # OSError — naming both was redundant (S5713). Catching OSError also
        # covers the disk-full / closed-handle variants, which need the same
        # treatment: neutralise stdout so CPython's shutdown flush cannot turn
        # a clean exit code into 120.
        _neutralise_dead_stdout()
    return code


def main(argv: list[str] | None = None) -> int:
    """Entry point for the Aero1553 CLI.

    Args:
        argv: Command-line arguments. If ``None``, uses ``sys.argv[1:]``.

    Returns:
        Process exit code per L2-CLI-011: 0 success; 1 runtime/decode
        error; 2 no valid records; 3 unrecoverable sync loss; 4 CLI usage
        error; 5 configuration error; 6 incompatible merge inputs
        (L1-EXIT-009).
    """
    _force_utf8_streams()

    if argv is None:
        argv = sys.argv[1:]
    argv = _normalize_version_flag(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate --log-level here (not via an argparse `type=`) so --version
    # and --help short-circuit during parse_args before the level is
    # checked, matching the Rust CLI. An invalid value is a usage error (4).
    if args.log_level is not None:
        try:
            args.log_level = _normalize_log_level(args.log_level)
        except ValueError as exc:
            print(f"{parser.prog}: error: {exc}", file=sys.stderr)
            return EXIT_USAGE

    # Determine log level: CLI > TOML > default. main() configures
    # with the CLI value (or the "WARNING" default) so any logging
    # in main() / parsing has a level; the subcommand runners
    # re-configure from TOML via _apply_config_log_level after the
    # config file is loaded.
    log_level = args.log_level or "WARNING"
    configure_logging(log_level)

    logger.info("aero1553 v%s", __version__)
    logger.debug("Arguments: %s", args)

    if args.command == "decode":
        return _run_decode(args)
    if args.command == "count":
        return _run_count(args)
    if args.command == "dump":
        return _run_dump(args)
    # No subcommand given — a usage error, not a runtime failure.
    parser.print_help()
    return EXIT_USAGE
