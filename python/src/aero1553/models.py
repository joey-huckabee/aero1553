"""Core data structures for decoded MIL-STD-1553 MIE binary records.

This module defines the immutable data structures that represent decoded
1553 messages extracted from DDC MIE binary recording files. All structures
use ``dataclass(frozen=True, slots=True)`` for memory efficiency and
immutability guarantees.

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

import math
import mmap
from dataclasses import dataclass
from enum import IntEnum, unique
from typing import Final

#: A read-only byte source accepted by the low-level decode/sync helpers:
#: an in-memory buffer (bytes/memoryview) or a memory-mapped file. The
#: reader passes an ``mmap.mmap``; tests and conformance fixtures pass
#: ``bytes``. All three support the indexing and slicing these helpers do.
ByteSource = bytes | memoryview | mmap.mmap


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

    Matching is case-insensitive. This is the single source of truth shared by
    the CLI (``--input-time-format``) and the config loader
    (``decode.input_time_format``) so the two can never disagree on which
    spellings are accepted.

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

    Matching is case-insensitive. Single source of truth shared by the CLI
    (``--delta-scope``) and the config loader (``merge.delta_scope``), mirroring
    the Rust ``DeltaScope::from_name_ci``.

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


@dataclass(frozen=True, slots=True)
class IrigTimestamp:
    """IRIG-format timestamp decoded from a 3-word binary field.

    Attributes:
        day: Day of year (1-366).
        hour: Hour of day (0-23).
        minute: Minute of hour (0-59).
        second: Second of minute (0-59).
        microsecond: Microsecond within the second (0-999999).
        freerun: True if external IRIG source was unavailable.
    """

    day: int
    hour: int
    minute: int
    second: int
    microsecond: int
    freerun: bool

    def to_total_microseconds(self) -> int:
        """Convert to absolute microseconds from start of year.

        Returns:
            Microseconds elapsed since the start of the year, treating ``day``
            as a one-based day-of-year.
        """
        return (
            self.day * 86_400 + self.hour * 3_600 + self.minute * 60 + self.second
        ) * 1_000_000 + self.microsecond

    def to_microseconds(self, _standard_tick_rate_hz: float | None = None) -> int | None:
        """Absolute microseconds from a known epoch.

        Always returns the IRIG conversion. Defined with the same
        (positional) signature as :meth:`StandardTimestamp.to_microseconds`
        so the reader can call ``timestamp.to_microseconds(rate)`` without a
        type check; the tick-rate argument is accepted and ignored here
        (hence the leading underscore) because IRIG already has an absolute
        microsecond basis.

        Returns:
            Absolute microseconds since the start of the year. Never ``None``
            for IRIG -- the optional return type exists only so this shares a
            signature with :meth:`StandardTimestamp.to_microseconds`.
        """
        return self.to_total_microseconds()

    def format(self) -> str:
        """Format as ``DAY:HH:MM:SS.uuuuuu`` string.

        Per L2-DEC-014 the microsecond field SHALL be exactly six
        digits. Validation in :mod:`aero1553.sync` should reject
        any record with ``microsecond >= 1_000_000`` (L2-SYN-004), so
        the modulo here is a defensive belt-and-suspenders: a caller
        constructing an :class:`IrigTimestamp` directly with an out-of-
        range microsecond still gets a well-formed string.

        Returns:
            ``DAY:HH:MM:SS.uuuuuu``, with the microsecond field always exactly
            six digits.
        """
        micro = self.microsecond % 1_000_000
        return f"{self.day}:{self.hour:02d}:{self.minute:02d}:{self.second:02d}.{micro:06d}"

    def format_with(self, render: TimeRender) -> str:
        """Format under the selected rendering (L2-WRT-025).

        ``DOY`` is infallible and byte-identical to :meth:`format`. The calendar
        renderings resolve ``day`` against ``render.year`` and refuse rather
        than approximate when that resolution has no answer -- day 366 of a
        common year does not become January 1st (L2-WRT-026 clause 3).

        Every rendering emits exactly six microsecond digits; L2-DEC-014 is not
        relaxed by the wider cell.

        Raises:
            CalendarUnavailableError: when a calendar rendering cannot be resolved --
                no year was configured, the timestamp is freerun, or the
                day-of-year does not exist in that year.

        Returns:
            The rendered ``TIME_STAMP`` cell.
        """
        if render.format is OutputTimeFormat.DOY:
            return self.format()

        # L2-WRT-026 clause 2: a freerun record's fields are relative, so
        # resolving them against a year would produce a date that looks
        # entirely ordinary and means nothing.
        if self.freerun:
            raise CalendarUnavailableError(
                "the record carries a freerun IRIG timestamp -- the card had no valid "
                "IRIG-B lock, so its day and time fields are relative rather than "
                "calendar-anchored"
            )
        if render.year is None:
            raise CalendarUnavailableError(
                "no year was configured; set [output] year or pass --year YYYY"
            )

        resolved = day_of_year_to_month_day(render.year, self.day)
        if resolved is None:
            raise CalendarUnavailableError(
                f"the record carries day-of-year {self.day}, which does not exist in "
                f"{render.year} ({render.year} is not a leap year). The configured year "
                f"is wrong for this recording"
            )
        month, day_of_month = resolved

        micro = self.microsecond % 1_000_000
        time_part = f"{self.hour:02d}:{self.minute:02d}:{self.second:02d}.{micro:06d}"
        if render.format is OutputTimeFormat.ISO:
            zone = format_utc_offset(render.utc_offset_minutes)
            return f"{render.year:04d}-{month:02d}-{day_of_month:02d}T{time_part}{zone}"
        return f"{day_of_month:02d}:{time_part}"


#: 2**64 — the largest microsecond count the Rust and C++ implementations
#: can represent. Beyond it all three decline rather than disagree.
_TWO_POW_64 = 1 << 64


@dataclass(frozen=True, slots=True)
class StandardTimestamp:
    """Standard-format timestamp decoded from a 2-word binary field.

    Attributes:
        raw_value: The full 32-bit counter value.
        upper_word: Raw upper 16-bit word (bits [31:16]).
        lower_word: Raw lower 16-bit word (bits [15:0]).
    """

    raw_value: int
    upper_word: int
    lower_word: int

    def raw_ticks(self) -> int:
        """Raw 32-bit free-running counter value, in unknown tick units.

        The tick rate is card-dependent and not encoded in the file, so
        callers cannot convert this to seconds without external calibration.

        Returns:
            The raw counter value, in card-dependent ticks -- not a duration.
        """
        return self.raw_value

    def to_microseconds(self, standard_tick_rate_hz: float | None = None) -> int | None:
        """Convert raw counter ticks to microseconds, if calibrated.

        ``standard_tick_rate_hz`` is the card-dependent counter frequency
        in Hz, supplied out-of-band (the file does not encode it). Returns
        ``None`` unless the rate is finite and strictly positive, so an
        uncalibrated or invalid rate can never be mistaken for real timing
        — callers (DELTA computation) treat the absence explicitly rather
        than using raw ticks as if they were microseconds.

        Rounding is half-away-from-zero, matching the Rust implementation's
        ``f64::round`` bit-for-bit (L2-DEC-017). It is computed as
        ``floor(x) + (1 if frac >= 0.5 else 0)`` with ``frac = x - floor(x)``
        (exact by Sterbenz for a non-negative ``x``) rather than the tempting
        ``int(x + 0.5)``: the latter rounds a value an ULP below a half-integer
        up to the wrong integer — e.g. ``x = 0.49999999999999994`` yields ``0``
        here and in Rust but ``1`` via ``int(x + 0.5)`` (Python evaluates
        ``x + 0.5 == 1.0``), which would silently diverge from Rust by 1 us.

        Returns:
            The tick count converted to whole microseconds, or ``None`` when the
            rate is not finite and strictly positive, or when the result falls
            outside the representable range. ``None`` means "uncalibrated", and
            is never a timestamp of zero.
        """
        if (
            standard_tick_rate_hz is None
            or not math.isfinite(standard_tick_rate_hz)
            or standard_tick_rate_hz <= 0.0
        ):
            return None
        micros = self.raw_value * 1_000_000 / standard_tick_rate_hz
        # Decline before converting, not after. A rate like 1e-300 is finite and
        # positive, so it passes the guard above, and the division then
        # overflows to ``inf`` — on which ``math.floor`` raises OverflowError.
        # This used to escape a decode as an uncaught exception.
        #
        # The bound is shared with Rust and C++, which cannot represent a result
        # at or above 2**64 at all: Rust's ``as`` saturated it to u64::MAX (a
        # fabricated timestamp that reads as real downstream) and the C++
        # ``static_cast`` from an out-of-range double was undefined behaviour.
        # All three decline instead, which is what keeps them byte-identical
        # (L2-DEC-017).
        if not math.isfinite(micros) or not 0.0 <= micros < _TWO_POW_64:
            return None
        floor_us = math.floor(micros)
        return floor_us + 1 if micros - floor_us >= 0.5 else floor_us

    def format(self) -> str:
        """Format as ``0xNNNNNNNN`` hexadecimal string.

        Returns:
            The raw counter as ``0x`` followed by eight uppercase hex digits.
        """
        return f"0x{self.raw_value:08X}"

    def format_with(self, render: TimeRender) -> str:
        """Format under the selected rendering (L2-WRT-025).

        Under ``DOY`` this is :meth:`format`. Under a calendar rendering it
        refuses (L2-WRT-026 clause 2): a free-running counter has no epoch, so
        no year can place it on a calendar, and quietly emitting raw hex into a
        column the operator asked to be ISO-8601 would be its own kind of lie.

        Raises:
            CalendarUnavailableError: under ``ISO`` or ``DOM``.

        Returns:
            The raw counter rendering, under ``DOY``.
        """
        if render.format.needs_calendar():
            raise CalendarUnavailableError(
                "this recording uses the Standard timestamp encoding, a free-running "
                "counter with no epoch. No year places it on a calendar"
            )
        return self.format()


#: Union type for timestamps.
Timestamp = IrigTimestamp | StandardTimestamp

#: Number of 16-bit words consumed by each timestamp format.
TIMESTAMP_WORD_COUNTS: dict[TimestampFormat, int] = {
    TimestampFormat.IRIG: 3,
    TimestampFormat.STANDARD: 2,
}


@dataclass(frozen=True, slots=True)
class TypeWord:
    """Decoded DDC MIE record Type Word.

    Attributes:
        message_type: DDC message type code (0x01–0x20).
        bus: Which redundant 1553 bus this message was captured on.
        word_count: Total record size in 16-bit words.
        error: True if the recording card flagged an error (bit 14).
        raw: The original 16-bit value for round-trip fidelity.
    """

    message_type: int
    bus: Bus
    word_count: int
    error: bool
    raw: int


@dataclass(frozen=True, slots=True)
class CommandWord:
    """Decoded MIL-STD-1553 Command Word.

    Attributes:
        rt: Remote Terminal address (0-30; 31 = broadcast).
        direction: TRANSMIT or RECEIVE.
        subaddress: Subaddress (0-31; 0 and 31 are mode codes).
        data_word_count: Number of data words (1-32; raw 0 = 32).
        raw: The original 16-bit value for round-trip fidelity.
    """

    rt: int
    direction: Direction
    subaddress: int
    data_word_count: int
    raw: int

    @property
    def is_broadcast(self) -> bool:
        """True if this command targets all RTs (RT address 31)."""
        return self.rt == 31

    @property
    def is_mode_code(self) -> bool:
        """True if this command is a mode code (SA 0 or SA 31)."""
        return self.subaddress in (0, 31)


#: MIL-STD-1553B caps a single transaction at 32 data words. Mirrors the Rust
#: ``DataWords`` inline buffer (``[u16; 32]``), which enforces the same cap by
#: construction, so both implementations carry an identically-bounded payload.
MAX_DATA_WORDS: Final[int] = 32


@dataclass(frozen=True, slots=True)
class MieMessage:
    """A single decoded MIL-STD-1553 message from an MIE binary file.

    This is the primary output structure of the decoder. Each instance
    represents one complete bus transaction as captured by the DDC
    recording card.

    The structure accommodates all 10 standard message formats plus
    SPURIOUS_DATA and errored records:

    - For simple BC→RT and RT→BC transfers, ``command_word`` and
      ``status_word`` are populated.
    - For RT-to-RT, ``command_word_2`` and ``status_word_2`` are also
      populated.
    - For broadcast messages, ``status_word`` is None.
    - For mode codes, ``data_words`` contains 0 or 1 words.
    - For **errored records** (Type Word bit 14 set), ``error_word``
      contains the DDC error code (0x01xx), data_words contains only
      the words received before the error, and status_word is typically
      None (transaction interrupted).
    - For **SPURIOUS_DATA** (type 0x20), ``command_word`` is None,
      ``error_word`` contains a custom 0x20xx code, and data_words
      contains the raw bus words.

    Attributes:
        timestamp: IRIG or Standard timestamp.
        type_word: Decoded Type Word containing message metadata.
        message_format: Classified message format.
        command_word: Primary Command Word. None for SPURIOUS_DATA
            records which have no command structure.
        command_word_2: Second Command Word for RT-to-RT. None otherwise.
        status_word: Primary Status Word. None for broadcast formats
            and errored records where the RT never responded.
        status_word_2: Second Status Word for RT-to-RT. None otherwise.
        data_words: Tuple of raw 16-bit data words in bus wire order.
            For errored records, contains only words received before
            the error. For SPURIOUS_DATA, contains raw bus words.
        error_word: DDC error code (0x01xx) for errored records, or
            custom decoder code (0x20xx) for SPURIOUS_DATA records.
            None for normal messages.
        delta: Seconds since prior message with same RT+MSG.
            ``0.0`` on first occurrence of an RT/MSG key with a calibrated
            timestamp. A positive float for a non-negative gap. ``None``
            when no DELTA is meaningful: SPURIOUS_DATA (no RT/MSG key),
            uncalibrated Standard timestamps (no known tick rate), and
            non-monotonic timestamps.
        file_offset: Byte offset of this record in the source file.
        mux: MUX column value derived from the source file name (L2-WRT-020),
            or None when MUX population is disabled or the configured filename
            field is absent. Shared (one str per input file) so per-record
            carry stays O(1) in resident memory.
    """

    timestamp: Timestamp
    type_word: TypeWord
    message_format: MessageFormat
    command_word: CommandWord | None
    command_word_2: CommandWord | None
    status_word: int | None
    status_word_2: int | None
    data_words: tuple[int, ...]
    error_word: int | None
    delta: float | None
    file_offset: int
    mux: str | None = None

    def __post_init__(self) -> None:
        """Cap ``data_words`` at :data:`MAX_DATA_WORDS`, mirroring the Rust
        ``DataWords`` inline buffer (``[u16; 32]``).

        A crafted record whose Type Word claims more than 32 data words is
        truncated to the first 32 so both implementations carry the same
        payload. Any standard-conforming record (≤ 32 words) is unaffected, and
        there is no CSV impact — the writer emits at most 32 ``WDnn`` columns
        regardless. Set via ``object.__setattr__`` because the dataclass is
        frozen.
        """
        if len(self.data_words) > MAX_DATA_WORDS:
            object.__setattr__(self, "data_words", self.data_words[:MAX_DATA_WORDS])

    def with_delta(self, delta: float | None) -> MieMessage:
        """Return a copy of this message carrying a new DELTA.

        ``MieMessage`` is frozen, so callers that recompute DELTA on a merged
        multi-file timeline (L2-MRG-005) build a fresh instance instead of
        mutating in place. Rebuilt field-by-field rather than via
        ``dataclasses.replace`` so every type checker infers the concrete
        ``MieMessage`` return type; only ``delta`` changes.

        Returns:
            A new :class:`MieMessage` identical to this one except for
            ``delta``. This instance is unchanged.
        """
        return MieMessage(
            timestamp=self.timestamp,
            type_word=self.type_word,
            message_format=self.message_format,
            command_word=self.command_word,
            command_word_2=self.command_word_2,
            status_word=self.status_word,
            status_word_2=self.status_word_2,
            data_words=self.data_words,
            error_word=self.error_word,
            delta=delta,
            file_offset=self.file_offset,
            mux=self.mux,
        )

    @property
    def rt(self) -> int | None:
        """Remote Terminal address, or None for SPURIOUS_DATA."""
        return self.command_word.rt if self.command_word is not None else None

    @property
    def subaddress(self) -> int | None:
        """Subaddress, or None for SPURIOUS_DATA."""
        return self.command_word.subaddress if self.command_word is not None else None

    @property
    def bus(self) -> Bus:
        """Bus identifier shortcut."""
        return self.type_word.bus

    @property
    def msg_label(self) -> str:
        """Message label in ``<SA><T|R>`` format, or empty for SPURIOUS_DATA."""
        if self.command_word is None:
            return ""
        suffix = "T" if self.command_word.direction == Direction.TRANSMIT else "R"
        return f"{self.command_word.subaddress}{suffix}"

    @property
    def delta_key(self) -> str:
        """Unique key for per-RT/MSG delta tracking.

        Returns empty string for SPURIOUS_DATA (no RT/MSG to track).
        """
        if self.command_word is None:
            return ""
        return f"{self.rt}:{self.msg_label}"

    @property
    def is_error(self) -> bool:
        """True if this record has the error flag set (bit 14)."""
        return self.type_word.error

    @property
    def is_spurious(self) -> bool:
        """True if this is a SPURIOUS_DATA record (type 0x20)."""
        return self.message_format == MessageFormat.SPURIOUS_DATA

    @property
    def error_label(self) -> str:
        """Error classification label for CSV output.

        Returns:
            ``""`` for normal messages, ``"ERROR"`` for errored records
            (bit 14 set), ``"SPURIOUS"`` for spurious data records.
        """
        if self.type_word.error:
            return "ERROR"
        if self.is_spurious:
            return "SPURIOUS"
        return ""
