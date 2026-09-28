"""Configuration loading and management for Aero1553.

Loads configuration from TOML files and merges with CLI arguments.
CLI arguments always take precedence over file-based configuration.

Configuration sources (in priority order, highest first):
    1. CLI arguments (``--log-level``, ``--input-time-format``, ``--exclude-types``, etc.)
    2. User-specified config file (``--config path/to/config.toml``)
    3. Built-in defaults (equivalent to ``config/default.toml``)

Usage::

    from aero1553.config import DecoderConfig, load_config

    # Load from file
    config = load_config("my-config.toml")

    # Override with CLI args
    config = config.with_overrides(log_level="DEBUG", exclude_types=["SPURIOUS_DATA"])
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aero1553.decode import DEFAULT_DETECT_RECORDS
from aero1553.merge import (
    DEFAULT_MAX_COLLAPSE_SURVIVORS,
    MAX_COLLAPSE_SURVIVORS_MAX,
    MAX_COLLAPSE_SURVIVORS_MIN,
)
from aero1553.models import (
    YEAR_MAX,
    YEAR_MIN,
    Bus,
    DeltaScope,
    ErrorMode,
    MessageType,
    OutputTimeFormat,
    TimestampFormat,
    parse_delta_scope,
    parse_output_time_format,
    parse_timestamp_format,
)
from aero1553.order import (
    DEFAULT_MAX_SORT_GROUP,
    MAX_SORT_GROUP_MAX,
    MAX_SORT_GROUP_MIN,
)
from aero1553.sync import DEFAULT_LOOKAHEAD_RECORDS

logger = logging.getLogger(__name__)

# Conditional import for TOML support:
# - Python 3.11+ has tomllib in the standard library
# - Python 3.10 requires the tomli package
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


#: Map of message type names (case-insensitive) to MessageType enum values.
_TYPE_NAME_MAP: dict[str, int] = {m.name.upper(): m.value for m in MessageType}

#: Accepted logging.level values (case-insensitive). Mirrors the Rust
#: log::Level::parse table so both implementations reject the same
#: inputs at config load time.
_VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {
        "DEBUG",
        "INFO",
        "WARNING",
        "WARN",
        "ERROR",
        "CRITICAL",
        "OFF",
    }
)

#: L2-CFG-009 schema membership. Any [section] key not in this set
#: triggers an unknown-key WARN at load time.
_KNOWN_SHARED_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        ("logging", "level"),
        ("logging", "irig_day_advisory"),
        ("decode", "input_time_format"),
        ("decode", "strict"),
        ("decode", "error_mode"),
        ("decode", "allow_partial"),
        ("decode", "detect_records"),
        ("decode", "lookahead_records"),
        ("decode", "standard_tick_rate_hz"),
        ("output", "format"),
        ("output", "no_clobber"),
        ("output", "max_sort_group"),
        ("output", "output_time_format"),
        ("output", "year"),
        ("output", "utc_offset"),
        ("mux", "enabled"),
        ("mux", "delimiter"),
        ("mux", "field"),
        ("merge", "collapse_duplicates"),
        ("merge", "collapse_window_us"),
        ("merge", "max_collapse_survivors"),
        ("merge", "delta_scope"),
        ("filter", "exclude_types"),
        ("filter", "exclude_rts"),
        ("filter", "exclude_buses"),
        ("filter", "exclude_subaddresses"),
    }
)

#: L2-DEC-015 valid range for ``decode.detect_records``. Values outside
#: this range are rejected at config-load time with a clear error.
DETECT_RECORDS_MIN: int = 1
DETECT_RECORDS_MAX: int = 32

#: L2-SYN-026 valid range for ``decode.lookahead_records``. Same shape
#: as DETECT_RECORDS_MIN/_MAX — both configurable record-count knobs
#: share their valid range for consistency.
LOOKAHEAD_RECORDS_MIN: int = 1
LOOKAHEAD_RECORDS_MAX: int = 32


def _require_bool(section: str, key: str, value: object) -> bool:
    """Validate that `value` is a real ``bool`` (not coerced from an
    int/str). Per L2-CFG-010 schema validations apply at load time.

    Returns:
        ``value``, unchanged, when it really is a ``bool``.

    Raises:
        ValueError: if the value is anything else, including an int or string
            that would otherwise coerce.
    """
    # NOTE: ``isinstance(True, int)`` is True in Python, but
    # ``isinstance(0, bool)`` is False — the bool check is sufficient
    # here, no special-case needed.
    if not isinstance(value, bool):
        raise ValueError(
            f"Invalid [{section}] {key}: expected boolean, got {type(value).__name__} ({value!r})"
        )
    return value


def _require_table(data: dict[str, Any], section: str) -> dict[str, Any]:
    """Return the ``[section]`` table (or an empty dict if absent).

    Raises ``ValueError`` if the name is present but is not a table — e.g.
    ``decode = true`` written instead of a ``[decode]`` header. Without this the
    downstream ``.get(...)`` on a scalar leaks an ``AttributeError`` (which the
    CLI does not classify as a config error). Matches the Rust loader, which
    rejects a known section name assigned a scalar value (L2-CFG-010).

    Returns:
        The section table, or an empty dict when the section is absent. Absent
        is not an error -- every section is optional.

    Raises:
        ValueError: if the name is present but bound to a scalar rather than a
            table.
    """
    value = data.get(section, {})
    if not isinstance(value, dict):
        raise ValueError(f"Invalid [{section}]: expected a table, got {type(value).__name__}")
    return value


#: A simple identifier (section name or key): letters, digits, underscores.
#:
#: ``re.ASCII`` is load-bearing, not decoration. Python's ``\w`` is
#: Unicode-aware by default and matches letters like ``é`` or ``中``, whereas the
#: Rust parser gates on ``is_ascii_alphanumeric``. Without the flag this pattern
#: would accept section names and keys that Rust rejects — a silent
#: cross-implementation divergence of exactly the kind L2-CFG-010 exists to
#: prevent. With it, ``\w`` is precisely ``[A-Za-z0-9_]``.
_IDENT_RE = re.compile(r"^\w+$", re.ASCII)

#: A numeric literal the flat schema accepts, matching the Rust
#: `is_toml_number_literal` grammar: `[+-]? (0 | [1-9][0-9]*) (.[0-9]+)?
#: ([eE][+-]?[0-9]+)?`. Rejects leading zeros (`08`, `01`), a bare trailing dot
#: (`1.`), and `0x`/`0o`/`0b` / underscore forms that `tomllib` and native Rust
#: parsing disagree on.
#:
#: ``re.ASCII`` for the same reason as `_IDENT_RE`: bare ``\d`` also matches
#: non-ASCII digits (Arabic-Indic ``٤``, Devanagari ``४``), which Rust's
#: ``is_ascii_digit`` does not. With the flag, ``\d`` is precisely ``[0-9]``.
_NUMBER_RE = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$", re.ASCII)


def _basic_string_accepted(tok: str) -> bool:
    """A double-quoted string using only the escapes the Rust parser supports
    (``\\"`` ``\\\\`` ``\\n`` ``\\t``).

    Rejects other escapes (``\\r``, ``\\uXXXX``) and an unescaped inner quote,
    mirroring `rust/src/config.rs::parse_string` exactly — `tomllib` accepts the
    full TOML escape set, so a naive regex would silently diverge from Rust.

    Returns:
        ``True`` if the token is a basic string the Rust parser would also
        accept.
    """
    if len(tok) < 2 or tok[0] != '"' or tok[-1] != '"':
        return False
    inner = tok[1:-1]
    i = 0
    while i < len(inner):
        if inner[i] == "\\":
            if i + 1 >= len(inner) or inner[i + 1] not in '"\\nt':
                return False
            i += 2
        elif inner[i] == '"':
            return False  # unescaped quote inside the string
        else:
            i += 1
    return True


def _scalar_accepted(tok: str) -> bool:
    """A single scalar value the flat schema accepts: a number, a boolean, or a
    basic string with the Rust-supported escapes.

    Returns:
        ``True`` if the token is an accepted scalar.
    """
    tok = tok.strip()
    return bool(_NUMBER_RE.match(tok)) or tok in ("true", "false") or _basic_string_accepted(tok)


def _strip_toml_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, preserving ``#`` inside a quoted string.

    Mirrors the Rust ``strip_comment`` so both parsers see the same value text.

    Returns:
        The line up to an unquoted ``#``, or the whole line when it has none.
    """
    in_quote = False
    prev_backslash = False
    for i, ch in enumerate(line):
        if in_quote:
            if ch == "\\" and not prev_backslash:
                prev_backslash = True
                continue
            if ch == '"' and not prev_backslash:
                in_quote = False
            prev_backslash = False
        elif ch == '"':
            in_quote = True
        elif ch == "#":
            return line[:i]
    return line


def _advance_inside_quote(ch: str, prev_backslash: bool) -> tuple[bool, bool]:
    """State transition for one character read *inside* a quoted string.

    Returns ``(still_in_quote, next_prev_backslash)``. Split out of
    :func:`_split_array_items` so the scanner's loop shows only the three
    top-level cases (inside a string / a separating comma / ordinary text) and
    the escape rule lives in one place.

    An unescaped backslash escapes whatever follows, so the string stays open; an
    unescaped quote closes it. Anything else is ordinary content.

    Returns:
        ``(still_in_quote, next_prev_backslash)`` for the scanner to carry into
        the next character.
    """
    if ch == "\\" and not prev_backslash:
        return True, True
    if ch == '"' and not prev_backslash:
        return False, False
    return True, False


def _split_array_items(inner: str) -> list[str]:
    """Split array element text on top-level commas, respecting quoted strings.

    A backslash escapes the next character (so an escaped ``\\"`` does not close
    the string), mirroring `rust/src/config.rs::split_array_items` /
    ``push_quoted_char`` — otherwise ``["a\\", b"]`` would be mis-split on the
    comma *inside* the string and rejected where Rust accepts it.

    Returns:
        The element texts, untrimmed of their own quoting but with empty
        entries dropped.
    """
    items: list[str] = []
    buf: list[str] = []
    in_quote = False
    prev_backslash = False
    for ch in inner:
        if in_quote:
            buf.append(ch)
            in_quote, prev_backslash = _advance_inside_quote(ch, prev_backslash)
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            if ch == '"':
                in_quote = True
            buf.append(ch)
    items.append("".join(buf))
    return [item for item in items if item.strip()]


def _value_accepted(value: str) -> bool:
    """True if ``value`` is a scalar or a single-line array of scalars the flat
    schema accepts. Rejects inline tables, multi-line arrays, date-times, and
    ``1_000`` / ``0x08`` numeric forms that ``tomllib`` would accept but the Rust
    value parser does not.

    Returns:
        ``True`` if the value text is one the flat schema accepts.
    """
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return all(_scalar_accepted(item) for item in _split_array_items(value[1:-1]))
    return _scalar_accepted(value)


def _reject_unsupported_toml_forms(text: str) -> None:
    """Reject any config line outside the flat ``[section]`` + ``key = value``
    schema, so Python's acceptance matches the minimal Rust parser exactly.

    This is a **whitelist**: a line must be blank, a comment, a flat ``[section]``
    header, or ``key = value`` with a simple-identifier key and a scalar / single-
    line-array value. ``tomllib`` is a full TOML parser that would otherwise
    *honor* forms the Rust hand-rolled parser rejects — dotted keys / headers,
    array-of-tables, inline tables, ``1_000`` / ``0x08`` numbers, date-times,
    multi-line arrays — so a config would behave differently across the two
    implementations (and a dotted/mis-typed safety option like ``no_clobber``
    could be silently ignored on Rust). Rejecting anything outside the subset up
    front keeps the two aligned by construction (exit 5, L2-CFG-010).
    """
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_toml_comment(raw).strip()
        if not line:
            continue
        if line.startswith("["):
            _reject_bad_section_header(line, lineno)
            continue
        _reject_bad_key_value(line, lineno)


def _reject_bad_section_header(line: str, lineno: int) -> None:
    """Whitelist check for a ``[section]`` header line.

    Mirrors ``parse_section_header`` in ``rust/src/config.rs`` rule for rule, so
    a header accepted by one implementation is accepted by the other.

    Raises:
        ValueError: for an array-of-tables header, a malformed header, a dotted
            header, or a section name outside the known set. The message names
            the line number.
    """
    if line.startswith("[["):
        raise ValueError(f"line {lineno}: array-of-tables headers ([[...]]) are not supported")
    if not line.endswith("]"):
        raise ValueError(f"line {lineno}: malformed section header: {line!r}")
    header = line[1:-1].strip()
    if "." in header:
        raise ValueError(
            f"line {lineno}: dotted section headers ([a.b]) are not "
            "supported; use a flat [section] header"
        )
    if not _IDENT_RE.match(header):
        raise ValueError(
            f"line {lineno}: unsupported section header [{header}]; "
            "use a flat [section] name (letters, digits, underscore)"
        )


def _reject_bad_key_value(line: str, lineno: int) -> None:
    """Whitelist check for a ``key = value`` line.

    Mirrors ``parse_key_value`` in ``rust/src/config.rs``.

    Raises:
        ValueError: for a line that is not ``key = value``, a dotted key, a key
            that is not a simple identifier, or a value the flat schema does not
            accept. The message names the line number.
    """
    if "=" not in line:
        raise ValueError(f"line {lineno}: expected 'key = value' or a [section] header")
    raw_key, value = line.split("=", 1)
    key = raw_key.strip()
    if "." in key and not key.startswith('"'):
        raise ValueError(
            f"line {lineno}: dotted keys (a.b = ...) are not supported; use a [section] header"
        )
    if not _IDENT_RE.match(key):
        raise ValueError(
            f"line {lineno}: unsupported key {key!r}; keys must be simple "
            "identifiers (letters, digits, underscore)"
        )
    if not _value_accepted(value):
        raise ValueError(
            f"line {lineno}: unsupported value for {key!r}: {value.strip()!r}; only "
            "strings, plain numbers, booleans, and single-line arrays are allowed"
        )


def _require_rt_sa_range(  # pylint: disable=redefined-outer-name
    field: str, values: object
) -> set[int]:
    """Validate a list of RT or subaddress values: each must be an int
    in [0, 31] per the L2-CFG schema reference.

    Returns:
        The values as a set, de-duplicated.

    Raises:
        ValueError: if the value is not an array, if an entry is not an integer,
            or if an entry falls outside the MIL-STD-1553 [0, 31] range. Note
            the CLI applies the wider u8 bound instead.
    """
    if not isinstance(values, list):
        raise ValueError(f"Invalid filter.{field}: expected array, got {type(values).__name__}")
    out: set[int] = set()
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(
                f"Invalid filter.{field} entry: expected integer, got {type(v).__name__} ({v!r})"
            )
        if not (0 <= v <= 31):
            raise ValueError(f"filter.{field} value out of MIL-STD-1553 range [0, 31]: {v}")
        out.add(v)
    return out


#: Map of bus name strings to Bus enum values.
_BUS_NAME_MAP: dict[str, Bus] = {"A": Bus.A, "B": Bus.B}

#: Map of error mode names to ErrorMode enum values.
_ERROR_MODE_MAP: dict[str, ErrorMode] = {
    "separate": ErrorMode.SEPARATE,
    "inline": ErrorMode.INLINE,
}


@dataclass
class FilterConfig:
    """Message filtering configuration.

    Both ``exclude_*`` (negative) and ``include_*`` (positive) filters
    are supported, mirroring the Rust ``FilterConfig``. A message passes
    if it matches no active ``exclude_*`` set AND every active
    ``include_*`` set contains its value. Empty (inactive) sets are
    ignored on both sides; excludes are checked first and take
    precedence.

    Attributes:
        exclude_types: MessageType values to exclude. Empty = no filter.
        exclude_rts: RT addresses (0–31) to exclude. Empty = no filter.
        exclude_buses: Bus values to exclude. Empty = no filter.
        exclude_subaddresses: subaddresses (0–31) to exclude. Empty = no
            filter.
        include_types: when non-empty, only these MessageType values pass.
        include_rts: when non-empty, only these RT addresses pass.
        include_buses: when non-empty, only these Bus values pass.
        include_subaddresses: when non-empty, only these subaddresses pass.
    """

    exclude_types: set[int] = field(default_factory=set)
    exclude_rts: set[int] = field(default_factory=set)
    exclude_buses: set[Bus] = field(default_factory=set)
    exclude_subaddresses: set[int] = field(default_factory=set)

    include_types: set[int] = field(default_factory=set)
    include_rts: set[int] = field(default_factory=set)
    include_buses: set[Bus] = field(default_factory=set)
    include_subaddresses: set[int] = field(default_factory=set)

    @property
    def is_active(self) -> bool:
        """True if any filter criteria are configured."""
        return bool(
            self.exclude_types
            or self.exclude_rts
            or self.exclude_buses
            or self.exclude_subaddresses
            or self.include_types
            or self.include_rts
            or self.include_buses
            or self.include_subaddresses
        )

    def should_exclude(
        self, message_type: int, rt: int | None, bus: Bus, subaddress: int | None
    ) -> bool:
        """Test whether a message should be excluded from output.

        Args:
            message_type: The message type code from the Type Word.
            rt: The Remote Terminal address from the Command Word, or
                ``None`` for records with no Command Word (SPURIOUS_DATA).
            bus: The bus identifier from the Type Word.
            subaddress: The subaddress from the Command Word, or ``None``
                for records with no Command Word (SPURIOUS_DATA).

        Returns:
            True if the message matches any ``exclude_*`` criterion, or
            fails any active ``include_*`` criterion. A ``None``
            ``rt``/``subaddress`` never matches an RT/subaddress exclude
            filter, and is always dropped when an RT/subaddress include
            filter is active (SPURIOUS_DATA has no RT/SA). Mirrors the
            Rust ``FilterConfig::should_exclude`` behavior.
        """
        # Negative filters take precedence over positive ones.
        return self._matches_exclude(message_type, rt, bus, subaddress) or self._fails_include(
            message_type, rt, bus, subaddress
        )

    def _matches_exclude(
        self, message_type: int, rt: int | None, bus: Bus, subaddress: int | None
    ) -> bool:
        """Whether the message matches any active ``exclude_*`` set.

        Returns:
            ``True`` if any active exclude set matches, meaning the message is
            dropped. An empty set is inactive and never matches.
        """
        return bool(
            (self.exclude_types and message_type in self.exclude_types)
            or (self.exclude_rts and rt in self.exclude_rts)
            or (self.exclude_buses and bus in self.exclude_buses)
            or (self.exclude_subaddresses and subaddress in self.exclude_subaddresses)
        )

    def _fails_include(
        self, message_type: int, rt: int | None, bus: Bus, subaddress: int | None
    ) -> bool:
        """Whether the message is absent from any active ``include_*`` set. A
        ``None`` rt/subaddress (SPURIOUS_DATA) fails an active RT/SA include.

        Returns:
            ``True`` if the message is absent from an active include set,
            meaning it is dropped. An empty set is inactive and admits
            everything.
        """
        return bool(
            (self.include_types and message_type not in self.include_types)
            or (self.include_buses and bus not in self.include_buses)
            or (self.include_rts and (rt is None or rt not in self.include_rts))
            or (
                self.include_subaddresses
                and (subaddress is None or subaddress not in self.include_subaddresses)
            )
        )


@dataclass
class DecoderConfig:
    """Complete decoder configuration.

    Attributes:
        log_level: Logging verbosity level name.
        irig_day_advisory: L2-LOG-001. Emit the one-time IRIG day-of-year
            advisory. True by default, but the advisory is logged at INFO,
            so at the default WARNING level it is already silent -- this
            exists so a site that has validated its card model against
            vendor CSV can also keep it out of a ``--log-level INFO`` run.
        input_time_format: Timestamp format (auto/irig/standard).
        strict: If True, raise on invalid records instead of skipping.
        error_mode: How errored messages appear in output.
        filters: Message filtering configuration.
        output_format: Output format name (currently only ``csv``).
        no_clobber: L2-WRT-017. Refuse to overwrite an existing
            destination. Defaults to False (overwrite permitted).
    """

    log_level: str = "WARNING"
    irig_day_advisory: bool = True
    input_time_format: TimestampFormat = TimestampFormat.AUTO
    strict: bool = False
    error_mode: ErrorMode = ErrorMode.INLINE
    filters: FilterConfig = field(default_factory=FilterConfig)
    output_format: str = "csv"
    no_clobber: bool = False
    #: L2-WRT-025: which rendering the TIME_STAMP column uses. Defaults to
    #: ``doy`` -- the DDC vendor rendering -- so a decode that selects nothing
    #: stays byte-comparable against vendor CSV.
    output_time_format: OutputTimeFormat = OutputTimeFormat.DOY
    #: L2-WRT-026: calendar year used to resolve the IRIG day-of-year field.
    #: Required by ``iso`` / ``dom``, inert under ``doy``. An MIE file carries
    #: no year, so None -- "not supplied" -- is the only honest default, and a
    #: calendar rendering with None here is refused rather than guessed at.
    year: int | None = None
    #: L2-WRT-025: offset from UTC in minutes for the ``iso`` zone
    #: designator. Zero renders as ``Z``.
    utc_offset_minutes: int = 0
    allow_partial: bool = False
    #: L2-DEC-015: number of records the timestamp-format auto-detect
    #: probe walks before committing to IRIG vs Standard. Range
    #: [DETECT_RECORDS_MIN, DETECT_RECORDS_MAX]. Default 8.
    detect_records: int = DEFAULT_DETECT_RECORDS
    #: L2-SYN-026: total number of records sync.validate_record checks
    #: (1 candidate + N-1 look-ahead). Range
    #: [LOOKAHEAD_RECORDS_MIN, LOOKAHEAD_RECORDS_MAX].
    #:
    #: Both defaults reference the defining constant rather than repeating its
    #: value. Repeating it is why the look-ahead default silently stayed at 2
    #: here when `sync.DEFAULT_LOOKAHEAD_RECORDS` moved to 4 — Rust had always
    #: referenced its constant, so only Python drifted.
    lookahead_records: int = DEFAULT_LOOKAHEAD_RECORDS
    #: L2-DEC-017: optional Standard-counter tick rate in Hz. None (the
    #: default) keeps the historical empty-DELTA behavior for Standard
    #: records; a finite, strictly-positive value enables tick->microsecond
    #: conversion and DELTA participation. Validated at load time.
    standard_tick_rate_hz: float | None = None
    #: L2-WRT-020: populate the MUX column from a field of the input file
    #: name. Enabled by default ([mux] enabled = false / --no-mux disables
    #: it for vendor-exact output). The name is split on mux_delimiter and the
    #: mux_field-th field (0-based; negative counts from the end) becomes MUX.
    mux_enabled: bool = True
    mux_delimiter: str = "."
    mux_field: int = 4

    #: L2-MRG-007: collapse the same bus transaction witnessed by multiple
    #: recorders into one row, in a multi-file merge. Off by default (loss-free).
    #: collapse_window_us is the timestamp tolerance in microseconds (0 = exact).
    collapse_duplicates: bool = False
    collapse_window_us: int = 0
    #: L2-MRG-008: cap on the survivors the de-duplication window (L2-MRG-007)
    #: retains at once. Validated at load time against
    #: [MAX_COLLAPSE_SURVIVORS_MIN, MAX_COLLAPSE_SURVIVORS_MAX]. Default 4096.
    #: The window bounds retention in time; this bounds it in count, so input
    #: whose timestamps all decode alike cannot grow the set without limit.
    max_collapse_survivors: int = DEFAULT_MAX_COLLAPSE_SURVIVORS

    #: L2-MRG-005: scope over which DELTA is measured in a multi-file merge.
    #: PER_FILE (default) leaves each reader's own DELTA in place, matching a
    #: single-file decode and the vendor tool; GLOBAL measures across the merged
    #: timeline. No effect on a single-input decode.
    delta_scope: DeltaScope = DeltaScope.PER_FILE

    #: L2-WRT-022: cap on the number of consecutive equal-TIME_STAMP records the
    #: canonical-order stage (L2-WRT-021) buffers at once. Range
    #: [MAX_SORT_GROUP_MIN, MAX_SORT_GROUP_MAX]. Default 4096; 1 disables
    #: reordering, restoring raw DDC capture order.
    max_sort_group: int = DEFAULT_MAX_SORT_GROUP

    def with_overrides(self, **kwargs: Any) -> DecoderConfig:
        """Return a new config with specified fields overridden.

        Only non-None values in kwargs are applied.

        Args:
            **kwargs: Field names and values to override.

        Returns:
            A new DecoderConfig with the overrides applied.
        """
        return DecoderConfig(
            log_level=self._override_present(kwargs, "log_level"),
            irig_day_advisory=self._override_present(kwargs, "irig_day_advisory"),
            input_time_format=self._override_present(kwargs, "input_time_format"),
            strict=self._override_present(kwargs, "strict"),
            error_mode=self._override_present(kwargs, "error_mode"),
            filters=self._merge_filter_overrides(kwargs),
            output_format=self._override_present(kwargs, "output_format"),
            no_clobber=self._override_present(kwargs, "no_clobber"),
            output_time_format=self._override_present(kwargs, "output_time_format"),
            year=self._override_present(kwargs, "year"),
            utc_offset_minutes=self._override_present(kwargs, "utc_offset_minutes"),
            allow_partial=self._override_present(kwargs, "allow_partial"),
            detect_records=self._override_present(kwargs, "detect_records"),
            lookahead_records=self._override_present(kwargs, "lookahead_records"),
            standard_tick_rate_hz=self._override_present(kwargs, "standard_tick_rate_hz"),
            mux_enabled=self._override_present(kwargs, "mux_enabled"),
            mux_delimiter=self._override_present(kwargs, "mux_delimiter"),
            mux_field=self._override_present(kwargs, "mux_field"),
            collapse_duplicates=self._override_present(kwargs, "collapse_duplicates"),
            collapse_window_us=self._override_present(kwargs, "collapse_window_us"),
            max_collapse_survivors=self._override_present(kwargs, "max_collapse_survivors"),
            max_sort_group=self._override_present(kwargs, "max_sort_group"),
            delta_scope=self._override_present(kwargs, "delta_scope"),
        )

    def _override_present(self, kwargs: dict[str, Any], name: str) -> Any:
        """Override resolution for every scalar field: only an explicit
        non-``None`` override replaces the current value, so an omitted CLI flag
        never resets a config-file value (e.g. ``no_clobber = true``).

        Presence — not truthiness — is the test, mirroring Rust's
        ``Option<T>``-based ``DecoderConfig::with_overrides`` (``rust/src/config.rs``)
        exactly. An earlier truthiness-based variant silently **dropped** any
        override whose value was falsy, which made ``--input-time-format auto``
        (``TimestampFormat.AUTO == 0``) a no-op against a config file that set
        ``input_time_format = "irig"`` — the config value won, Rust used ``auto``, and
        the two implementations decoded the same file differently. The same trap
        applied to ``ErrorMode.SEPARATE == 0`` and to any empty-string value.

        Returns:
            The override when it is not ``None``, otherwise the current value
            of the field. Presence, not truthiness, decides.
        """
        value = kwargs.get(name)
        return value if value is not None else getattr(self, name)

    def _merge_filter_overrides(self, kwargs: dict[str, Any]) -> FilterConfig:
        """CLI filters ADD to (not replace) config-file filters (Rust parity);
        ``include_*`` are CLI-only but merge the same way for symmetry.

        Returns:
            A new :class:`FilterConfig` whose sets are the union of the config
            file's and the CLI's. CLI filters add to, never replace.
        """
        return FilterConfig(
            exclude_types=self.filters.exclude_types | set(kwargs.get("exclude_types") or []),
            exclude_rts=self.filters.exclude_rts | set(kwargs.get("exclude_rts") or []),
            exclude_buses=self.filters.exclude_buses | set(kwargs.get("exclude_buses") or []),
            exclude_subaddresses=(
                self.filters.exclude_subaddresses | set(kwargs.get("exclude_subaddresses") or [])
            ),
            include_types=self.filters.include_types | set(kwargs.get("include_types") or []),
            include_rts=self.filters.include_rts | set(kwargs.get("include_rts") or []),
            include_buses=self.filters.include_buses | set(kwargs.get("include_buses") or []),
            include_subaddresses=(
                self.filters.include_subaddresses | set(kwargs.get("include_subaddresses") or [])
            ),
        )


def _parse_type_names(names: Sequence[object]) -> set[int]:
    """Parse message-type identifiers into type code values.

    Each element is either a **string** (an enum name like ``"BC_TO_RT"`` or a
    hex code like ``"0x02"``) or an **integer** type code. Mirrors the Rust
    ``parse_type_value`` (``rust/src/config.rs``): integer and hex codes are bounded
    to a ``u8`` (``0..=255``); anything else is rejected with a ``ValueError``
    (so the caller maps it to a config error / usage error, never a crash).

    Args:
        names: Sequence of type identifiers (strings and/or integers). Comes
            either from a TOML array (which may mix strings and integers) or
            from the comma-separated CLI flag (always strings).

    Returns:
        Set of integer message type codes.

    Raises:
        ValueError: an unrecognized name, a code outside ``0..=255``, or an
            element that is neither a string nor an integer.
    """
    return {_parse_type_code(name) for name in names}


def _parse_type_code(name: object) -> int:
    """Parse one message-type identifier (an int code, or a string) to a u8.

    Returns:
        The type code as an int in [0, 255].

    Raises:
        ValueError: if the value is a bool, an int outside [0, 255], or neither
            a string nor an int. A TOML boolean is not a valid type code even
            though ``bool`` is an ``int`` subclass.
    """
    # bool is an int subclass; a TOML boolean is not a valid type code.
    if isinstance(name, bool):
        raise ValueError(f"Invalid message type code: {name!r}")
    if isinstance(name, int):
        if not 0 <= name <= 255:
            raise ValueError(f"Type code out of range: {name}")
        return name
    if not isinstance(name, str):
        raise ValueError(
            "exclude_types/include_types entries must be strings or "
            f"integers, got {type(name).__name__}"
        )
    return _parse_type_code_str(name)


def _parse_type_code_str(name: str) -> int:
    """Parse a type-identifier string: an enum name or a ``0x`` hex code.

    Returns:
        The type code as an int.

    Raises:
        ValueError: if the string is neither a known message-type name nor a
            valid ``0x`` hex code. The message lists the valid names.
    """
    upper = name.strip().upper()
    if upper in _TYPE_NAME_MAP:
        return _TYPE_NAME_MAP[upper]
    if upper.startswith("0X"):
        try:
            code = int(upper, 16)
        except ValueError as exc:
            raise ValueError(f"Invalid hex type code: {name!r}") from exc
        if not 0 <= code <= 255:
            raise ValueError(f"Invalid hex type code: {name!r}")
        return code
    valid = ", ".join(sorted(_TYPE_NAME_MAP.keys()))
    raise ValueError(f"Unknown message type name: {name!r}. Valid names: {valid}")


def _parse_bus_names(names: Sequence[object]) -> set[Bus]:
    """Parse bus identifiers into Bus enum values.

    Each element must be a **string** (``"A"`` or ``"B"``, case-insensitive) —
    matching the Rust ``parse_bus_value`` (``rust/src/config.rs``), which rejects a
    non-string entry rather than crashing. Comes from a TOML array (which may
    contain non-strings) or the CLI flag (always strings).

    Args:
        names: Sequence of bus identifiers.

    Returns:
        Set of Bus enum values.

    Raises:
        ValueError: an entry that is not a string, or not "A"/"B".
    """
    result: set[Bus] = set()
    for name in names:
        if not isinstance(name, str):
            raise ValueError("exclude_buses entries must be strings")
        upper = name.strip().upper()
        if upper in _BUS_NAME_MAP:
            result.add(_BUS_NAME_MAP[upper])
        else:
            raise ValueError(f"Invalid bus name: {name!r}. Valid: A, B")
    return result


def load_config(path: str | Path | None = None) -> DecoderConfig:
    """Load configuration from a TOML file.

    Args:
        path: Path to the TOML configuration file. If None, returns
            the built-in defaults.

    Returns:
        A populated DecoderConfig.

    Raises:
        FileNotFoundError: If the specified config file does not exist.
        ValueError: If the config file contains invalid values.
        RuntimeError: If TOML parsing is unavailable (Python 3.10
            without the ``tomli`` package installed).
    """
    if path is None:
        logger.debug("No config file specified, using defaults")
        return DecoderConfig()

    # Validate the operator-supplied path BEFORE touching its contents
    # (`pythonsecurity:S8707`). `--config` is attacker-influenced in any context
    # where the decoder is driven by another program, and `exists()` alone is
    # not a sufficient guard: it is true for directories, FIFOs, and character
    # devices. Reading one of those gives either a confusing traceback
    # (`IsADirectoryError`) or, for something like `--config /dev/zero`, a read
    # that never returns. Requiring a *regular file* is the actual precondition
    # this function needs, so state it explicitly.
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    if not config_path.is_file():
        raise ValueError(f"Config path is not a regular file: {config_path}")

    if tomllib is None:
        raise RuntimeError(
            "TOML parsing requires Python 3.11+ or the 'tomli' package. "
            "Install with: pip install tomli"
        )

    logger.info("Loading config from %s", config_path)
    text = config_path.read_text(encoding="utf-8")
    _reject_unsupported_toml_forms(text)
    data = tomllib.loads(text)

    # Each `[section]` is validated by its own loader (L2-CFG-010: validate at
    # load time) so no single function carries the whole schema. The loaders
    # run in the original order — all validation first, then the unknown-key
    # WARN, then assembly — so error precedence is unchanged.
    decode_section = _require_table(data, "decode")
    output_section = _require_table(data, "output")

    logging_section = _require_table(data, "logging")
    log_level = _load_logging_level(logging_section)
    irig_day_advisory = _require_bool(
        "logging", "irig_day_advisory", logging_section.get("irig_day_advisory", True)
    )
    _reject_retired_time_format_key(decode_section)
    input_time_format = _load_time_format(decode_section)
    strict = _require_bool("decode", "strict", decode_section.get("strict", False))
    error_mode = _load_error_mode(decode_section)
    filters = _load_filter_section(_require_table(data, "filter"))
    output_format = _load_output_format(output_section)
    no_clobber = _require_bool("output", "no_clobber", output_section.get("no_clobber", False))
    output_time_format = _load_output_time_format(output_section)
    year = _load_year(output_section)
    utc_offset_minutes = _load_utc_offset(output_section)
    max_sort_group = _require_int_range(
        "output.max_sort_group",
        output_section.get("max_sort_group", DEFAULT_MAX_SORT_GROUP),
        MAX_SORT_GROUP_MIN,
        MAX_SORT_GROUP_MAX,
    )
    allow_partial = _require_bool(
        "decode", "allow_partial", decode_section.get("allow_partial", False)
    )
    detect_records = _require_int_range(
        "decode.detect_records",
        decode_section.get("detect_records", 8),
        DETECT_RECORDS_MIN,
        DETECT_RECORDS_MAX,
    )
    lookahead_records = _require_int_range(
        "decode.lookahead_records",
        decode_section.get("lookahead_records", DEFAULT_LOOKAHEAD_RECORDS),
        LOOKAHEAD_RECORDS_MIN,
        LOOKAHEAD_RECORDS_MAX,
    )
    standard_tick_rate_hz = _load_standard_tick_rate(decode_section)
    mux_enabled, mux_delimiter, mux_field = _load_mux_section(_require_table(data, "mux"))
    merge_section = _require_table(data, "merge")
    collapse_duplicates, collapse_window_us, max_collapse_survivors = _load_merge_section(
        merge_section
    )
    delta_scope = _load_delta_scope(merge_section)

    _warn_unknown_keys(data)

    config = DecoderConfig(
        log_level=log_level,
        irig_day_advisory=irig_day_advisory,
        input_time_format=input_time_format,
        strict=strict,
        error_mode=error_mode,
        filters=filters,
        output_format=output_format,
        no_clobber=no_clobber,
        output_time_format=output_time_format,
        year=year,
        utc_offset_minutes=utc_offset_minutes,
        allow_partial=allow_partial,
        detect_records=detect_records,
        lookahead_records=lookahead_records,
        standard_tick_rate_hz=standard_tick_rate_hz,
        mux_enabled=mux_enabled,
        mux_delimiter=mux_delimiter,
        mux_field=mux_field,
        collapse_duplicates=collapse_duplicates,
        collapse_window_us=collapse_window_us,
        max_collapse_survivors=max_collapse_survivors,
        max_sort_group=max_sort_group,
        delta_scope=delta_scope,
    )

    logger.debug("Loaded config: %s", config)
    return config


def _load_logging_level(logging_section: dict[str, Any]) -> str:
    """`[logging] level` — validated against the known level names.

    Returns:
        The canonical uppercase level name.

    Raises:
        ValueError: if the value is not a string, or is not a known level.
    """
    log_level_raw = logging_section.get("level", "WARNING")
    if not isinstance(log_level_raw, str):
        raise ValueError(
            f"Invalid [logging] level: expected string, got {type(log_level_raw).__name__}"
        )
    log_level = log_level_raw.upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid [logging] level: {log_level_raw!r}. "
            f"Valid: DEBUG, INFO, WARNING, WARN, ERROR, CRITICAL, OFF"
        )
    return log_level


def _reject_retired_time_format_key(decode_section: dict[str, Any]) -> None:
    """Reject the pre-v3.0.0 ``decode.time_format`` key by name (L2-CFG-012).

    Left to the generic unknown-key rule this would only WARN (L2-CFG-009) --
    and a WARN is the wrong answer for a *rename*, because the operator's
    explicit choice would be discarded while the run reported success, silently
    reverting a forced format to auto-detection.

    Presence is tested rather than type-checked so that a wrong-typed value
    (``time_format = 1``) reports the rename too, instead of a type error about
    a key that no longer exists.

    Raises:
        ValueError: whenever the retired key is present.
    """
    if "time_format" in decode_section:
        raise ValueError(
            "decode.time_format was renamed in v3.0.0. Use decode.input_time_format to "
            "choose how timestamps are PARSED (auto, irig, standard); use "
            "output.output_time_format to choose how they are WRITTEN (doy, iso, dom)."
        )


def _load_output_time_format(output_section: dict[str, Any]) -> OutputTimeFormat:
    """`[output] output_time_format` (L2-CFG-012 / L2-WRT-025).

    Returns:
        The parsed :class:`OutputTimeFormat`, defaulting to ``DOY``.

    Raises:
        ValueError: if the value is not a string, or names no known rendering.
    """
    raw = output_section.get("output_time_format", "doy")
    if not isinstance(raw, str):
        raise ValueError(
            f"Invalid [output] output_time_format: expected string, got {type(raw).__name__}"
        )
    return parse_output_time_format(raw)


def _load_year(output_section: dict[str, Any]) -> int | None:
    """`[output] year` (L2-CFG-012 / L2-WRT-026 clause 1).

    Validated here but NOT required here: a config may legitimately set a year
    it never uses, and a calendar rendering may legitimately take its year from
    the CLI instead. Whether one is actually needed is a question about the
    resolved pair, asked once both sources have merged (see
    ``cli._resolve_time_render``).

    Returns:
        The validated year, or ``None`` when the key is absent.

    Raises:
        ValueError: if the value is not an integer or falls outside
            ``[YEAR_MIN, YEAR_MAX]``.
    """
    if "year" not in output_section:
        return None
    raw = output_section["year"]
    # bool is an int subclass; `year = true` is a mistake, not a year.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"Invalid [output] year: expected integer, got {type(raw).__name__}")
    year = int(raw)
    if not YEAR_MIN <= year <= YEAR_MAX:
        raise ValueError(f"Invalid output.year: {year}. Valid range: [{YEAR_MIN}, {YEAR_MAX}]")
    return year


def _load_utc_offset(output_section: dict[str, Any]) -> int:
    """`[output] utc_offset` (L2-CFG-012 / L2-WRT-025).

    Returns:
        The offset in minutes east of UTC; ``0`` when the key is absent.

    Raises:
        ValueError: if the value is not a string or does not match the grammar.
    """
    if "utc_offset" not in output_section:
        return 0
    raw = output_section["utc_offset"]
    if not isinstance(raw, str):
        raise ValueError(f"Invalid [output] utc_offset: expected string, got {type(raw).__name__}")
    return parse_utc_offset(raw)


def parse_utc_offset(text: str) -> int:
    """Parse a UTC offset designator into minutes east of UTC (L2-CFG-012).

    Accepts ``Z`` (case-insensitively) for zero, or a signed ``+HH:MM`` /
    ``-HH:MM``. The shape is checked exactly rather than leniently: ``+5:00``,
    ``+0500`` and a trailing-space variant are all rejected, because a zone that
    parsed loosely here would silently shift every ISO timestamp in the file.

    Returns:
        The offset in minutes; negative west of UTC.

    Raises:
        ValueError: if the text does not match the grammar or is out of range.
    """
    if text.lower() == "z":
        return 0

    invalid = ValueError(
        f"Invalid output.utc_offset: {text!r}. Valid: Z, or +HH:MM / -HH:MM "
        f"with HH in [0, 23] and MM in [0, 59]"
    )
    if len(text) != 6 or text[3] != ":" or text[0] not in "+-":
        raise invalid
    hours_text, minutes_text = text[1:3], text[4:6]
    if not (hours_text.isascii() and hours_text.isdigit()):
        raise invalid
    if not (minutes_text.isascii() and minutes_text.isdigit()):
        raise invalid
    hours, minutes = int(hours_text), int(minutes_text)
    if hours > 23 or minutes > 59:
        raise invalid
    return (-1 if text[0] == "-" else 1) * (hours * 60 + minutes)


def _load_time_format(decode_section: dict[str, Any]) -> TimestampFormat:
    """`[decode] input_time_format`.

    Returns:
        The parsed :class:`TimestampFormat`.

    Raises:
        ValueError: if the value is not a string, or names no known format.
    """
    raw = decode_section.get("input_time_format", "auto")
    if not isinstance(raw, str):
        raise ValueError(
            f"Invalid decode.input_time_format: expected string, got {type(raw).__name__}"
        )
    return parse_timestamp_format(raw)


def _load_error_mode(decode_section: dict[str, Any]) -> ErrorMode:
    """`[decode] error_mode`.

    Returns:
        The parsed :class:`ErrorMode`.

    Raises:
        ValueError: if the value is not a string, or is not ``separate`` or
            ``inline``.
    """
    raw = decode_section.get("error_mode", "inline")
    if not isinstance(raw, str):
        raise ValueError(f"Invalid decode.error_mode: expected string, got {type(raw).__name__}")
    em_str = raw.lower()
    if em_str not in _ERROR_MODE_MAP:
        raise ValueError(f"Invalid error_mode: {em_str!r}. Valid: separate, inline")
    return _ERROR_MODE_MAP[em_str]


def _load_filter_section(filter_section: dict[str, Any]) -> FilterConfig:
    """`[filter]` exclude arrays (RT/SA values validated to [0, 31]).

    Returns:
        The assembled :class:`FilterConfig`. Config files carry only the
        ``exclude_*`` sets; ``include_*`` are CLI-only (L3-PY-013).
    """
    return FilterConfig(
        exclude_types=_parse_type_names(filter_section.get("exclude_types", [])),
        exclude_rts=_require_rt_sa_range("exclude_rts", filter_section.get("exclude_rts", [])),
        exclude_buses=_parse_bus_names(filter_section.get("exclude_buses", [])),
        exclude_subaddresses=_require_rt_sa_range(
            "exclude_subaddresses", filter_section.get("exclude_subaddresses", [])
        ),
    )


def _load_output_format(output_section: dict[str, Any]) -> str:
    """`[output] format` — `csv` is currently the only supported value (L2-CFG-010).

    Returns:
        The output format name, currently always ``csv``.

    Raises:
        ValueError: if the value is anything other than ``csv``.
    """
    output_format: str = output_section.get("format", "csv")
    if output_format != "csv":
        raise ValueError(f"Invalid output.format: {output_format!r}. Valid: csv")
    return output_format


def _load_standard_tick_rate(decode_section: dict[str, Any]) -> float | None:
    """`[decode] standard_tick_rate_hz` (L2-DEC-017): when present, a real,
    strictly-positive frequency. Accept int or float (not bool); reject
    non-finite or non-positive values so a bad rate can't silently produce
    garbage microseconds.

    Returns:
        The rate in Hz, or ``None`` when the key is absent. ``None`` means
        "uncalibrated", which leaves Standard timestamps unconvertible rather
        than converted wrongly.

    Raises:
        ValueError: if the value is not a number, or is not finite and strictly
            positive.
    """
    if "standard_tick_rate_hz" not in decode_section:
        return None
    raw_hz = decode_section["standard_tick_rate_hz"]
    if isinstance(raw_hz, bool) or not isinstance(raw_hz, (int, float)):
        raise ValueError(f"Invalid decode.standard_tick_rate_hz: {raw_hz!r}; must be a number")
    hz = float(raw_hz)
    if not math.isfinite(hz) or hz <= 0.0:
        raise ValueError(
            f"Invalid decode.standard_tick_rate_hz: {hz}. Must be a finite value greater than 0"
        )
    return hz


def _load_mux_section(mux_section: dict[str, Any]) -> tuple[bool, str, int]:
    """`[mux]` MUX-from-filename configuration (L2-WRT-020).

    The caller validates the section is a table (see :func:`_require_table`).

    Returns:
        ``(enabled, delimiter, field)``.

    Raises:
        ValueError: if the delimiter is not a non-empty string, or the field is
            not an integer.
    """
    mux_enabled = _require_bool("mux", "enabled", mux_section.get("enabled", True))
    mux_delimiter_raw = mux_section.get("delimiter", ".")
    if not isinstance(mux_delimiter_raw, str) or mux_delimiter_raw == "":
        raise ValueError(
            f"Invalid mux.delimiter: {mux_delimiter_raw!r}; must be a non-empty string"
        )
    mux_field_raw = mux_section.get("field", 4)
    if isinstance(mux_field_raw, bool) or not isinstance(mux_field_raw, int):
        raise ValueError(f"Invalid mux.field: {mux_field_raw!r}; must be an integer")
    return mux_enabled, mux_delimiter_raw, mux_field_raw


def _load_delta_scope(merge_section: dict[str, Any]) -> DeltaScope:
    """`[merge] delta_scope` (L2-MRG-005). Defaults to ``per-file``.

    Returns:
        The parsed :class:`DeltaScope`, defaulting to ``PER_FILE``.

    Raises:
        ValueError: if the value is not a string, or names no known scope.
    """
    raw = merge_section.get("delta_scope", "per-file")
    if not isinstance(raw, str):
        raise ValueError(f"Invalid merge.delta_scope: expected string, got {type(raw).__name__}")
    return parse_delta_scope(raw)


def _load_merge_section(merge_section: dict[str, Any]) -> tuple[bool, int, int]:
    """`[merge]` cross-recorder duplicate collapsing (L2-MRG-007).

    The caller validates the section is a table (see :func:`_require_table`).

    Returns:
        ``(collapse_duplicates, collapse_window_us, max_collapse_survivors)``.

    Raises:
        ValueError: if ``collapse_window_us`` is not a non-negative integer, or
            ``max_collapse_survivors`` is outside its valid range.
    """
    collapse_duplicates = _require_bool(
        "merge", "collapse_duplicates", merge_section.get("collapse_duplicates", False)
    )
    collapse_window_us_raw = merge_section.get("collapse_window_us", 0)
    if (
        isinstance(collapse_window_us_raw, bool)
        or not isinstance(collapse_window_us_raw, int)
        or collapse_window_us_raw < 0
    ):
        raise ValueError(
            f"Invalid merge.collapse_window_us: {collapse_window_us_raw!r}; "
            "must be a non-negative integer"
        )
    # L2-MRG-008. Range-checked here so a bad value fails at load time rather
    # than silently clamping later; the message text matches the Rust and C++
    # loaders (L3-WRT-003).
    max_collapse_survivors = _require_int_range(
        "merge.max_collapse_survivors",
        merge_section.get("max_collapse_survivors", DEFAULT_MAX_COLLAPSE_SURVIVORS),
        MAX_COLLAPSE_SURVIVORS_MIN,
        MAX_COLLAPSE_SURVIVORS_MAX,
    )
    return collapse_duplicates, collapse_window_us_raw, max_collapse_survivors


def _require_int_range(key: str, value: object, lo: int, hi: int) -> int:
    """Validate a TOML integer within ``[lo, hi]`` at load time (L2-CFG-010),
    rejecting bools and non-integers. ``key`` names the offending TOML key.

    Returns:
        ``value``, unchanged, when it is an int within ``[lo, hi]``.

    Raises:
        ValueError: if the value is a bool or non-integer, or falls outside the
            range. The message names the TOML key.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid {key}: {value!r}; must be an integer")
    if value < lo or value > hi:
        raise ValueError(f"Invalid {key}: {value}. Valid range: [{lo}, {hi}]")
    return value


def _warn_unknown_keys(data: dict[str, Any]) -> None:
    """L2-CFG-009: WARN on unknown `[section] key` entries so typos surface to
    the operator instead of being silently dropped. Non-fatal."""
    for section_name, section_dict in data.items():
        if not isinstance(section_dict, dict):
            # A root-level scalar key (`bogus = true`) — the schema defines no
            # root keys, so it is always unknown. Rust logs `[] bogus`; match it
            # rather than dropping the entry without a warning.
            logger.warning("unknown TOML key: [] %s", section_name)
            continue
        for key in section_dict:
            if (section_name, key) not in _KNOWN_SHARED_KEYS:
                logger.warning("unknown TOML key: [%s] %s", section_name, key)
