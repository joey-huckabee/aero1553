"""Core data structures for decoded MIL-STD-1553 MIE binary records.

This module defines the immutable data structures that represent decoded
1553 messages extracted from DDC MIE binary recording files.

The record types -- :class:`MieMessage` and the :class:`TypeWord`,
:class:`CommandWord`, :class:`IrigTimestamp` and :class:`StandardTimestamp`
values it holds -- are compiled classes from the package's extension: frozen,
each holding the decoder's record inline, with nested values built only when a
field is read. They keep the dataclass surface they replaced (field names,
constructors, equality, hashing, repr, pickling, ``copy.replace``) but are not
dataclasses, so :mod:`dataclasses` helpers do not apply to them. The enums,
:class:`TimeRender` and the error-code tables are plain Python.

Type Word Message Type Codes (bits 0–6):

    0x01  Mode Command — sub-classified into 5 formats by Command Word
    0x02  BC→RT (Receive) — Bus Controller sends data to Remote Terminal
    0x04  RT→BC (Transmit) — Remote Terminal sends data to Bus Controller
    0x08  RT→RT (Terminal-to-Terminal) — BC commands one RT to send to another
    0x10  Broadcast BC→RT — BC sends data to all RTs (RT address 31)
    0x18  Broadcast RT→RT — BC commands RT-to-RT transfer, all RTs listen
    0x20  Spurious Data — unstructured bus noise captured by the monitor

Error Handling:

    When the DDC card detects an error mid-transaction (Manchester error,
    parity error, missing response, etc.), it:
    1. Sets bit 14 of the Type Word to flag the record as errored.
    2. Captures bus words received up to the point of error.
    3. Appends a 16-bit Error Word (error code) as the last word.
    4. If remaining words exist from the original transaction, they are
       written as a separate SPURIOUS_DATA (0x20) record immediately
       following.

    DDC Hardware Error Codes (0x01xx range):
        0x011E  Manchester/Parity Error or Bit Count Error
        0x0120  No Status Response or Too Few Data Words
        0x0136  Inverted Sync on Data Word
        0x0140  Too Many Data Words
        0x0150  Unknown/TBD

    Aero1553 Custom Error Codes (0x20xx range):
        0x2000  Spurious Data: Continuation of preceding errored message
        0x2001  Spurious Data: Standalone (no preceding error record)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, unique
from typing import Final, TypeAlias

from aero1553 import _native


@unique
class Bus(IntEnum):
    """MIL-STD-1553 redundant bus identifier."""

    A = 0
    B = 1


@unique
class Direction(IntEnum):
    """MIL-STD-1553 message transfer direction.

    From the perspective of the Remote Terminal:
        RECEIVE: Bus Controller sends data TO the RT (BC→RT).
        TRANSMIT: RT sends data TO the Bus Controller (RT→BC).
    """

    RECEIVE = 0
    TRANSMIT = 1


@unique
class MessageType(IntEnum):
    """DDC MIE Type Word message type codes (bits 0–6).

    Values:
        MODE_COMMAND: 0x01 — Mode code message.
        BC_TO_RT: 0x02 — Bus Controller sends data to RT (Receive).
        RT_TO_BC: 0x04 — RT sends data to Bus Controller (Transmit).
        RT_TO_RT: 0x08 — Terminal-to-Terminal transfer.
        BROADCAST_BC_TO_RT: 0x10 — Broadcast Receive.
        BROADCAST_RT_TO_RT: 0x18 — Broadcast Terminal-to-Terminal.
        SPURIOUS_DATA: 0x20 — Spurious data / bus noise / error continuation.
    """

    MODE_COMMAND = 0x01
    BC_TO_RT = 0x02
    RT_TO_BC = 0x04
    RT_TO_RT = 0x08
    BROADCAST_BC_TO_RT = 0x10
    BROADCAST_RT_TO_RT = 0x18
    SPURIOUS_DATA = 0x20


#: Set of all valid message type codes for fast membership testing.
VALID_MESSAGE_TYPES: frozenset[int] = frozenset(m.value for m in MessageType)


@unique
class MessageFormat(IntEnum):
    """Classified message format determining the payload layout.

    Each format has a distinct sequence of command words, status words,
    and data words after the Type Word and timestamp.

    Values 1–10 are the standard MIL-STD-1553 message formats.
    Value 11 is the SPURIOUS_DATA format (raw bus words, no command).
    """

    RECEIVE = 1
    TRANSMIT = 2
    RT_TO_RT = 3
    RECEIVE_BROADCAST = 4
    RT_TO_RT_BROADCAST = 5
    MODE_CODE_TX_DATA = 6
    MODE_CODE_RX_DATA = 7
    MODE_CODE_NO_DATA = 8
    MODE_CODE_BCAST_NO_DATA = 9
    MODE_CODE_BCAST_DATA = 10
    SPURIOUS_DATA = 11


@unique
class TimestampFormat(IntEnum):
    """Timestamp encoding format used in the MIE binary file.

    Values:
        AUTO: Auto-detect from the first records (bounded multi-record probe).
        IRIG: 48-bit IRIG-B timestamp (3 × 16-bit words).
        STANDARD: 32-bit free-running counter (2 × 16-bit words).
    """

    AUTO = 0
    IRIG = 1
    STANDARD = 2


_TIMESTAMP_FORMAT_BY_NAME: dict[str, TimestampFormat] = {
    "auto": TimestampFormat.AUTO,
    "irig": TimestampFormat.IRIG,
    "standard": TimestampFormat.STANDARD,
}


def parse_timestamp_format(name: str) -> TimestampFormat:
    """Parse an ``input_time_format`` name (``auto`` / ``irig`` / ``standard``).

    Matching is case-insensitive, and the accepted spellings are those of the
    CLI's ``--input-time-format`` and the config loader's
    ``decode.input_time_format`` (both Rust; ``tests/test_config.py`` pins the
    agreement).

    Raises:
        ValueError: if ``name`` is not one of the recognized formats. The message
            lists the valid set.

    Returns:
        The matching :class:`TimestampFormat` member.
    """
    fmt = _TIMESTAMP_FORMAT_BY_NAME.get(name.lower())
    if fmt is None:
        raise ValueError(f"Invalid input_time_format: {name!r}. Valid: auto, irig, standard")
    return fmt


@unique
class OutputTimeFormat(IntEnum):
    """Rendering selected for the ``TIME_STAMP`` CSV column (L2-WRT-025).

    This is the *output* half of what used to be one ``time_format`` setting.
    :class:`TimestampFormat` decides how the bytes on disk are parsed;
    ``OutputTimeFormat`` decides how the resulting instant is written down. The
    two are independent, subject to the calendar preconditions of L2-WRT-026.

    Values:
        DOY: ``DAY:HH:MM:SS.uuuuuu`` -- the DDC vendor rendering, and the
            default. Byte-identical to what every version before v3.0.0 emitted.
        ISO: ``YYYY-MM-DDTHH:MM:SS.uuuuuu`` plus a zone designator.
        DOM: ``DD:HH:MM:SS.uuuuuu`` -- day of month, with the month
            deliberately absent from the cell.
    """

    DOY = 0
    ISO = 1
    DOM = 2

    def needs_calendar(self) -> bool:
        """Whether this rendering resolves day-of-year to a calendar date.

        Returns:
            True for ``ISO`` and ``DOM``, which carry the L2-WRT-026
            preconditions (a year is required; the recording must be
            calendar-locked). False for ``DOY``, which needs nothing.
        """
        return self in (OutputTimeFormat.ISO, OutputTimeFormat.DOM)


_OUTPUT_TIME_FORMAT_BY_NAME: dict[str, OutputTimeFormat] = {
    "doy": OutputTimeFormat.DOY,
    "iso": OutputTimeFormat.ISO,
    "dom": OutputTimeFormat.DOM,
}


def parse_output_time_format(name: str) -> OutputTimeFormat:
    """Parse an ``output_time_format`` name (``doy`` / ``iso`` / ``dom``).

    Matching is case-insensitive, mirroring :func:`parse_timestamp_format`, and
    shared by the CLI (``--output-time-format``) and the config loader
    (``output.output_time_format``).

    Raises:
        ValueError: if ``name`` is not one of the recognized renderings.

    Returns:
        The matching :class:`OutputTimeFormat` member.
    """
    fmt = _OUTPUT_TIME_FORMAT_BY_NAME.get(name.lower())
    if fmt is None:
        raise ValueError(f"Invalid output_time_format: {name!r}. Valid: doy, iso, dom")
    return fmt


#: Inclusive bounds on a configured calendar year (L2-WRT-026 clause 1).
#:
#: The upper bound is four digits because the ``iso`` rendering formats the year
#: as exactly ``YYYY``; a five-digit year would widen column 1 without warning.
#: The lower bound is 1 because year 0 does not exist in the proleptic
#: Gregorian numbering this decoder uses.
YEAR_MIN = 1
YEAR_MAX = 9999


class CalendarUnavailableError(Exception):
    """A calendar rendering could not be produced for one timestamp.

    Raised by the ``format_with`` methods and converted by the writer into
    :class:`~aero1553.exceptions.MieCalendarUnavailableError`. Two of the
    three L2-WRT-026 preconditions are checked before the first row is written
    (a missing year at CLI parse time, a Standard recording once the format
    resolves), so the case that normally reaches here is a day-of-year the
    configured year does not have.
    """


@dataclass(frozen=True, slots=True)
class TimeRender:
    """Everything the ``TIME_STAMP`` formatter needs beyond the timestamp.

    Resolved once per decode from the merged configuration (L2-WRT-025) and
    carried unchanged through the writer.

    Attributes:
        format: Which of the three renderings to produce.
        year: Calendar year for the day-of-year resolution. Required by ``ISO``
            and ``DOM``; ignored by ``DOY`` (L2-WRT-026 clause 5). ``None``
            means no year was configured, which the CLI refuses up front for a
            calendar rendering.
        utc_offset_minutes: Offset from UTC in minutes, used only by ``ISO``.
            Zero renders as ``Z``.

    A plain container: the year (``[1, 9999]``) and offset (``[-1439,
    1439]``) are range-checked by the decoder wherever a rendering is used,
    raising ``ValueError`` before any output (L3-PY-024).
    """

    format: OutputTimeFormat = OutputTimeFormat.DOY
    year: int | None = None
    utc_offset_minutes: int = 0


#: The default rendering: day-of-year, no calendar resolution. What ``dump``
#: uses unconditionally, and what every pre-v3.0.0 decode produced.
DOY_RENDER = TimeRender()

#: Days in each month of a common year, January first.
_COMMON_YEAR_MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def is_leap_year(year: int) -> bool:
    """Proleptic Gregorian leap-year test (L2-WRT-025).

    Divisible by 4, except centuries, which must also be divisible by 400.
    Hand-rolled rather than delegated to :mod:`datetime` so that all three
    implementations compute it identically -- C++11 has no date type at all, and
    one shared rule is easier to hold aligned than three library behaviours that
    merely agree today.

    Returns:
        True if ``year`` has 366 days.
    """
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def day_of_year_to_month_day(year: int, day_of_year: int) -> tuple[int, int] | None:
    """Resolve a 1-based day-of-year to ``(month, day_of_month)``, both 1-based.

    Returns:
        The resolved pair, or ``None`` when the day does not exist in that year:
        day 366 of a common year, day 0, or anything above 366. That is the
        L2-WRT-026 clause 3 condition, which the caller turns into a refusal
        rather than rolling forward into the next January.
    """
    leap = is_leap_year(year)
    year_length = 366 if leap else 365
    if day_of_year <= 0 or day_of_year > year_length:
        return None

    remaining = day_of_year
    for index, base_length in enumerate(_COMMON_YEAR_MONTH_LENGTHS):
        # February is the only month whose length depends on the year.
        length = base_length + 1 if index == 1 and leap else base_length
        if remaining <= length:
            return index + 1, remaining
        remaining -= length
    # Unreachable: the month lengths sum to year_length, which bounds
    # day_of_year above.
    return None  # pragma: no cover


def format_utc_offset(minutes: int) -> str:
    """Render a UTC offset in minutes as an ISO-8601 designator.

    Returns:
        ``Z`` at zero, otherwise ``+HH:MM`` / ``-HH:MM``.
    """
    if minutes == 0:
        return "Z"
    sign = "-" if minutes < 0 else "+"
    magnitude = abs(minutes)
    return f"{sign}{magnitude // 60:02d}:{magnitude % 60:02d}"


@unique
class DeltaScope(IntEnum):
    """Scope over which DELTA is measured in a multi-file merge (L2-MRG-005).

    Only meaningful when more than one input is decoded; with a single input the
    two are the same computation.

    Values:
        PER_FILE: Default. Each record's gap is to the previous same-key record
            **from its own file**, so the value matches what that file would
            produce decoded on its own -- and what the DDC vendor tool reports,
            since it has no merge feature.
        GLOBAL: Each record's gap is to the previous same-key record from **any**
            input, measured across the merged timeline. Compresses gaps whenever
            one key appears in several inputs.
    """

    PER_FILE = 0
    GLOBAL = 1


#: Accepted ``delta_scope`` spellings (lowercase) -> DeltaScope.
_DELTA_SCOPE_BY_NAME: dict[str, DeltaScope] = {
    "per-file": DeltaScope.PER_FILE,
    "global": DeltaScope.GLOBAL,
}


def parse_delta_scope(name: str) -> DeltaScope:
    """Parse a ``delta_scope`` name (``per-file`` / ``global``).

    Matching is case-insensitive, and the accepted spellings are those of the
    CLI's ``--delta-scope`` and the config loader's ``merge.delta_scope`` (both
    the Rust ``DeltaScope::from_name_ci``; ``tests/test_config.py`` pins the
    agreement).

    Raises:
        ValueError: if ``name`` is not a recognized scope; the message lists the
            valid set and matches the Rust wording.

    Returns:
        The matching :class:`DeltaScope` member.
    """
    scope = _DELTA_SCOPE_BY_NAME.get(name.strip().lower())
    if scope is None:
        raise ValueError(f"Invalid delta_scope: {name!r}. Valid: per-file, global")
    return scope


@unique
class ErrorMode(IntEnum):
    """Controls how errored messages are handled in output.

    Values:
        SEPARATE: Errored and spurious messages are written to a
            separate ``<output>_errors.csv`` file. The main CSV
            contains only clean messages. This is the default.
        INLINE: Errored and spurious messages are included in the
            main CSV with ERROR and ERROR_CODE columns populated.
    """

    SEPARATE = 0
    INLINE = 1


# ── DDC Hardware Error Codes ───────────────────────────────────────────
# These codes are written by the DDC recording card into the Error Word
# appended to errored records. The Error Word describes the 1553 bus
# word immediately preceding it.

#: Manchester encoding error, parity error, or incorrect bit count.
ERROR_MANCHESTER_PARITY: Final[int] = 0x011E

#: No status word response from RT, or too few data words received.
ERROR_NO_RESPONSE: Final[int] = 0x0120

#: Inverted sync pattern detected on a data word.
ERROR_INVERTED_SYNC: Final[int] = 0x0136

#: More data words received than the Command Word specified.
ERROR_TOO_MANY_WORDS: Final[int] = 0x0140

#: Unknown or undocumented DDC error condition.
ERROR_UNKNOWN_DDC: Final[int] = 0x0150

#: Set of all known DDC hardware error codes.
KNOWN_DDC_ERROR_CODES: frozenset[int] = frozenset(
    {
        ERROR_MANCHESTER_PARITY,
        ERROR_NO_RESPONSE,
        ERROR_INVERTED_SYNC,
        ERROR_TOO_MANY_WORDS,
        ERROR_UNKNOWN_DDC,
    }
)

#: Human-readable descriptions for DDC error codes.
DDC_ERROR_DESCRIPTIONS: dict[int, str] = {
    ERROR_MANCHESTER_PARITY: "Manchester/Parity Error or Bit Count Error",
    ERROR_NO_RESPONSE: "No Status Response or Too Few Data Words",
    ERROR_INVERTED_SYNC: "Inverted Sync on Data Word",
    ERROR_TOO_MANY_WORDS: "Too Many Data Words",
    ERROR_UNKNOWN_DDC: "Unknown DDC Error",
}

# ── Aero1553 Custom Error Codes ────────────────────────────────────
# These codes are assigned by the decoder (not the hardware) to identify
# spurious data records. The 0x20 prefix mirrors the SPURIOUS_DATA type
# code from Type Word bits 0–6.

#: Spurious data that is a continuation of a preceding errored message.
ERROR_SPURIOUS_CONTINUATION: Final[int] = 0x2000

#: Standalone spurious data with no preceding error record.
ERROR_SPURIOUS_STANDALONE: Final[int] = 0x2001

#: Set of all known Aero1553 custom error codes.
KNOWN_CUSTOM_ERROR_CODES: frozenset[int] = frozenset(
    {
        ERROR_SPURIOUS_CONTINUATION,
        ERROR_SPURIOUS_STANDALONE,
    }
)

#: All known error codes (DDC + custom).
ALL_KNOWN_ERROR_CODES: frozenset[int] = KNOWN_DDC_ERROR_CODES | KNOWN_CUSTOM_ERROR_CODES


# ── Record types ────────────────────────────────────────────────────────────
#
# Compiled classes (``aero1553._native``), re-exported here so this module stays
# where they live -- ``aero1553.models.MieMessage`` is their qualified name, which
# is also what pickling looks up. Their field and property docstrings live with
# them. (Their enum-valued getters return this module's IntEnum members, looked
# up on first use, so importing the extension first cannot cycle.)

CommandWord = _native.CommandWord
IrigTimestamp = _native.IrigTimestamp
MieMessage = _native.MieMessage
StandardTimestamp = _native.StandardTimestamp
TypeWord = _native.TypeWord

#: Union type for timestamps.
Timestamp: TypeAlias = IrigTimestamp | StandardTimestamp

#: Number of 16-bit words consumed by each timestamp format.
TIMESTAMP_WORD_COUNTS: dict[TimestampFormat, int] = {
    TimestampFormat.IRIG: 3,
    TimestampFormat.STANDARD: 2,
}

#: MIL-STD-1553B caps a single transaction at 32 data words. Mirrors the Rust
#: ``DataWords`` inline buffer (``[u16; 32]``), which enforces the same cap by
#: construction; a :class:`MieMessage` built with more keeps the first 32.
MAX_DATA_WORDS: Final[int] = 32
