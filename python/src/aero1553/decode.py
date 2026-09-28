"""Binary decoding routines for DDC MIE record fields.

This module contains pure functions that decode raw bytes into the
structured types defined in :mod:`aero1553.models`. All functions
operate on ``bytes`` or ``memoryview`` objects and use ``struct`` for
portable little-endian unpacking.

No external packages are required.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Final

from aero1553.models import (
    VALID_MESSAGE_TYPES,
    Bus,
    ByteSource,
    CommandWord,
    Direction,
    IrigTimestamp,
    MessageFormat,
    MessageType,
    StandardTimestamp,
    TimestampFormat,
    TypeWord,
)

#: Minimum record size in bytes for IRIG: Type(2) + TS(6) + Cmd(2) = 10
MIN_RECORD_BYTES: Final[int] = 10

#: Minimum record size in bytes for Standard: Type(2) + TS(4) + Cmd(2) = 8
MIN_RECORD_BYTES_STANDARD: Final[int] = 8

#: Minimum record size in 16-bit words for IRIG: Type(1) + TS(3) + Cmd(1) = 5
MIN_RECORD_WORDS: Final[int] = 5

#: Minimum record size in 16-bit words for Standard: Type(1) + TS(2) + Cmd(1) = 4
MIN_RECORD_WORDS_STANDARD: Final[int] = 4

#: End-of-records terminator (L2-RDR-021). DDC recorders cap the record stream
#: with a null Type Word — all sixteen bits zero. Because the Word Count field
#: (bits 8–13) is zero, ``0x0000`` can never be a valid record (the minimum is
#: 4 words); the value is unambiguous as a stream terminator rather than a
#: record. A file whose record stream is empty consists solely of this word (an
#: empty recording, e.g. an unused MIL-STD-1553 channel that captured no
#: traffic).
TERMINATOR_TYPE_WORD: Final[int] = 0x0000

#: struct format for a single little-endian unsigned 16-bit word
_LE_U16: Final[str] = "<H"

#: L2-WRT-020 MUX-from-filename defaults.
DEFAULT_MUX_ENABLED: Final[bool] = True
DEFAULT_MUX_DELIMITER: Final[str] = "."
DEFAULT_MUX_FIELD: Final[int] = 4


def mux_from_filename(file_name: str, delimiter: str, field: int) -> str | None:
    """Extract the MUX column value from a file *name* (basename, not a path):
    split on ``delimiter`` and return the ``field``-th part (0-based; a negative
    ``field`` counts from the end, e.g. ``-1`` is the last part), trimmed.

    Mirrors the Rust ``mux_from_filename`` (rust/src/decode.rs).

    Returns:
        The selected field, trimmed. ``None`` -- which renders as an empty MUX
        column -- when the index is out of range, the field is empty after
        trimming, or ``delimiter`` is empty.
    """
    if not delimiter:
        return None
    parts = file_name.split(delimiter)
    idx = len(parts) + field if field < 0 else field
    if idx < 0 or idx >= len(parts):
        return None
    value = parts[idx].strip()
    return value or None


def decode_type_word(raw: int) -> TypeWord:
    """Decode a 16-bit Type Word into its constituent fields.

    Args:
        raw: The raw 16-bit unsigned integer value read from the binary
            file in little-endian byte order.

    Returns:
        A populated TypeWord with message_type, bus, word_count, error,
        and the original raw value.

    Examples:
        >>> tw = decode_type_word(0x2402)
        >>> tw.message_type
        2
        >>> tw.bus
        <Bus.A: 0>
        >>> tw.word_count
        36
    """
    message_type = raw & 0x7F
    bus = Bus((raw >> 7) & 1)
    word_count = (raw >> 8) & 0x3F
    error = bool((raw >> 14) & 1)
    return TypeWord(
        message_type=message_type,
        bus=bus,
        word_count=word_count,
        error=error,
        raw=raw,
    )


def decode_irig_timestamp(upper: int, middle: int, lower: int) -> IrigTimestamp:
    """Decode a 3-word IRIG timestamp into structured fields.

    The three 16-bit words are read from the binary file in little-endian
    order immediately following the Type Word.

    Args:
        upper: Upper word containing freerun flag, day, and hour.
        middle: Middle word containing minutes, seconds, and upper
            microsecond bits [19:16].
        lower: Lower word containing microsecond bits [15:0].

    Returns:
        A populated IrigTimestamp.

    Examples:
        >>> ts = decode_irig_timestamp(0x180F, 0xDB26, 0xF621)
        >>> ts.hour
        15
        >>> ts.minute
        54
        >>> ts.microsecond
        456225
    """
    freerun = bool((upper >> 15) & 1)
    day = (upper >> 5) & 0x1FF
    hour = upper & 0x1F
    minute = (middle >> 10) & 0x3F
    second = (middle >> 4) & 0x3F
    us_hi = middle & 0xF
    us_lo = lower
    microsecond = (us_hi << 16) | us_lo
    return IrigTimestamp(
        day=day,
        hour=hour,
        minute=minute,
        second=second,
        microsecond=microsecond,
        freerun=freerun,
    )


def decode_standard_timestamp(upper: int, lower: int) -> StandardTimestamp:
    """Decode a 2-word Standard-format timestamp.

    The Standard timestamp is a 32-bit monotonic counter split across
    two 16-bit little-endian words. The upper word contains bits [31:16]
    and the lower word contains bits [15:0].

    Args:
        upper: Upper word (bits [31:16] of the 32-bit counter).
        lower: Lower word (bits [15:0] of the 32-bit counter).

    Returns:
        A populated StandardTimestamp with the reconstructed 32-bit value.

    Examples:
        >>> ts = decode_standard_timestamp(0x0001, 0x86A0)
        >>> ts.raw_value
        100000
    """
    raw_value = (upper << 16) | lower
    return StandardTimestamp(
        raw_value=raw_value,
        upper_word=upper,
        lower_word=lower,
    )


class DetectionConfidence(Enum):
    """L2-DEC-016 classification of an auto-detection outcome's strength.

    ``DECISIVE`` and ``MARGINAL`` both result in the chosen format
    being used silently or with a single INFO log line. ``AMBIGUOUS``
    is the L2-DEC-016 mismatch class: strict mode surfaces it as
    :class:`aero1553.exceptions.MieTimestampFormatMismatchError`;
    lenient mode logs WARN and uses the chosen format anyway.
    """

    DECISIVE = "decisive"
    MARGINAL = "marginal"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class DetectionOutcome:
    """Result of an L2-DEC-015 multi-record probe."""

    #: Chosen format. IRIG wins ties per L2-DEC-012.
    format: TimestampFormat
    #: Aggregated IRIG score across the probe set.
    irig_score: int
    #: Aggregated Standard score across the probe set.
    std_score: int
    #: Number of records actually probed (>= 1 on a successful probe;
    #: may be less than ``max_records`` if EOF was reached or a
    #: record's declared length was structurally impossible).
    records_probed: int
    #: L2-DEC-016 confidence classification.
    confidence: DetectionConfidence


#: L2-DEC-016 thresholds. Conservative — they fire only when the
#: probe genuinely could not distinguish, not when the call is
#: decisive but the absolute score is low because the probe set was
#: small. The floor of 4 means even a single decisive record passes
#: (one perfect IRIG record scores 5; one perfect Standard scores 4).
_CONFIDENCE_FLOOR: Final[int] = 4
_MIN_MARGIN: Final[int] = 3
#: Decisive thresholds — comfortably above the floor AND a wide
#: margin. Two records that both score perfectly for one format
#: easily clear these.
_DECISIVE_FLOOR: Final[int] = 8
_DECISIVE_MARGIN: Final[int] = 6

#: L2-DEC-015 default probe size. Configurable via the
#: ``decode.detect_records`` TOML key or the ``--detect-records`` CLI
#: flag.
DEFAULT_DETECT_RECORDS: Final[int] = 8


def probe_timestamp_format(
    data: ByteSource,
    first_offset: int,
    max_records: int,
) -> DetectionOutcome:
    """L2-DEC-015 multi-record probe.

    Walks up to ``max_records`` starting from ``first_offset``,
    aggregating per-record IRIG vs Standard scoring, and returns the
    chosen format with a confidence classification per L2-DEC-016.

    ``max_records`` is clamped to at least 1 (a no-probe call is
    nonsensical). The probe is bounded by file length: when EOF is
    reached before ``max_records`` records have been scored, the
    function returns with however many records it managed to score.

    IRIG wins ties per L2-DEC-012.

    Args:
        data: Raw byte buffer (file contents or mmap).
        first_offset: Byte offset of the first record to probe.
        max_records: Maximum number of records to probe before
            committing to a format.

    Returns:
        A :class:`DetectionOutcome` describing the chosen format,
        per-format aggregated scores, the number of records actually
        probed, and the L2-DEC-016 confidence classification.
    """
    irig_score, std_score, records_probed = _accumulate_probe_scores(
        data, first_offset, max_records
    )
    fmt = TimestampFormat.IRIG if irig_score >= std_score else TimestampFormat.STANDARD
    max_score = max(irig_score, std_score)
    margin = abs(irig_score - std_score)
    return DetectionOutcome(
        format=fmt,
        irig_score=irig_score,
        std_score=std_score,
        records_probed=records_probed,
        confidence=_classify_detection_confidence(max_score, margin),
    )


def _accumulate_probe_scores(
    data: ByteSource, first_offset: int, max_records: int
) -> tuple[int, int, int]:
    """Walk up to ``max_records`` records from ``first_offset``, returning the
    aggregate ``(irig_score, std_score, records_probed)``. Advances by each
    record's declared length (the reader's walk) and stops at EOF or a
    structurally-impossible record.

    Returns:
        ``(irig_score, std_score, records_probed)`` summed over the records
        actually walked. ``records_probed`` may be less than ``max_records``.
    """
    n = max(1, max_records)
    file_len = len(data)
    irig_score = 0
    std_score = 0
    records_probed = 0
    offset = first_offset

    for _ in range(n):
        # Need at least the Type Word + minimum payload to score.
        if offset + MIN_RECORD_BYTES_STANDARD > file_len:
            break
        if offset + 2 > file_len:
            break
        tw_raw = read_u16(data, offset)
        tw = decode_type_word(tw_raw)
        # Defensively skip structurally-impossible records — these would also
        # fail the reader's validate_record path, and would skew the score.
        if tw.word_count < MIN_RECORD_WORDS_STANDARD:
            break

        i_delta, s_delta = _score_single_record(data, offset, tw)
        irig_score += i_delta
        std_score += s_delta
        records_probed += 1

        record_bytes = tw.word_count * 2
        if record_bytes == 0:
            break
        next_offset = offset + record_bytes
        if next_offset <= offset or next_offset > file_len:
            break
        offset = next_offset

    return irig_score, std_score, records_probed


def _classify_detection_confidence(max_score: int, margin: int) -> DetectionConfidence:
    """L2-DEC-016 confidence classification from the aggregate score and margin.

    Returns:
        ``DECISIVE``, ``MARGINAL`` or ``AMBIGUOUS``, per the L2-DEC-016
        thresholds.
    """
    if max_score < _CONFIDENCE_FLOOR or margin < _MIN_MARGIN:
        return DetectionConfidence.AMBIGUOUS
    if max_score >= _DECISIVE_FLOOR and margin >= _DECISIVE_MARGIN:
        return DetectionConfidence.DECISIVE
    return DetectionConfidence.MARGINAL


def _score_single_record(
    data: ByteSource,
    offset: int,
    type_word: TypeWord,
) -> tuple[int, int]:
    """Per-record scoring extracted from the previous single-record
    detector. Returns ``(irig_delta, std_delta)``.

    IRIG can score up to ``+5`` per record (T/R: 2 + WC plausibility:
    2 + range validity: 1). Standard can score up to ``+4`` per
    record (T/R: 2 + WC plausibility: 2; no range bonus because the
    Standard timestamp is a raw 32-bit counter with no semantic field
    bounds to check against).

    Returns:
        ``(irig_delta, std_delta)`` for this one record, to be added to the
        running totals.
    """
    return (
        _score_irig_candidate(data, offset, type_word),
        _score_standard_candidate(data, offset, type_word),
    )


def _tr_direction_matches(type_word: TypeWord, cmd: CommandWord) -> bool:
    """T/R consistency: a BC_TO_RT type expects a Receive command, RT_TO_BC
    expects Transmit. Other message types never match.

    Returns:
        ``True`` when the Command Word's direction matches the one the Type
        Word's message type implies.
    """
    if type_word.message_type == MessageType.BC_TO_RT:
        return cmd.direction == Direction.RECEIVE
    if type_word.message_type == MessageType.RT_TO_BC:
        return cmd.direction == Direction.TRANSMIT
    return False


def _score_irig_candidate(data: ByteSource, offset: int, type_word: TypeWord) -> int:
    """IRIG candidate: Cmd at offset+8 (Type + 3 TS words). Up to +5
    (T/R: 2, word-count plausibility: 2, field-range validity: 1).

    Returns:
        The candidate's score, ``0`` when the Command Word cannot be read at
        that offset.
    """
    if offset + 8 + 2 > len(data):
        return 0
    cmd = decode_command_word(read_u16(data, offset + 8))
    score = 0
    if _tr_direction_matches(type_word, cmd):
        score += 2
    # IRIG overhead = TS(3) + Cmd(1) + Stat(1) + Type(1) = 6
    if type_word.word_count - 6 == cmd.data_word_count:
        score += 2
    # Only the fields with a semantic range are worth scoring. The microsecond
    # high nibble (``ts_middle & 0xF``) is *always* < 16 by construction, so
    # testing it added nothing to the score.
    ts_upper = read_u16(data, offset + 2)
    ts_middle = read_u16(data, offset + 4)
    hour = ts_upper & 0x1F
    minute = (ts_middle >> 10) & 0x3F
    second = (ts_middle >> 4) & 0x3F
    if hour < 24 and minute < 60 and second < 60:
        score += 1
    return score


def _score_standard_candidate(data: ByteSource, offset: int, type_word: TypeWord) -> int:
    """Standard candidate: Cmd at offset+6 (Type + 2 TS words). Up to +4
    (T/R: 2, word-count plausibility: 2; no range bonus — the 32-bit counter
    has no semantic field bounds to check).

    Returns:
        The candidate's score, ``0`` when the Command Word cannot be read at
        that offset.
    """
    if offset + 6 + 2 > len(data):
        return 0
    cmd = decode_command_word(read_u16(data, offset + 6))
    score = 0
    if _tr_direction_matches(type_word, cmd):
        score += 2
    # Standard overhead = TS(2) + Cmd(1) + Stat(1) + Type(1) = 5
    if type_word.word_count - 5 == cmd.data_word_count:
        score += 2
    return score


def decode_command_word(raw: int) -> CommandWord:
    """Decode a 16-bit MIL-STD-1553 Command Word.

    Args:
        raw: The raw 16-bit unsigned integer value.

    Returns:
        A populated CommandWord with RT address, direction, subaddress,
        and data word count.

    Examples:
        >>> cw = decode_command_word(0x797E)
        >>> cw.rt
        15
        >>> cw.subaddress
        11
        >>> cw.data_word_count
        30
    """
    rt = (raw >> 11) & 0x1F
    direction = Direction((raw >> 10) & 1)
    subaddress = (raw >> 5) & 0x1F
    data_word_count = raw & 0x1F
    if data_word_count == 0:
        data_word_count = 32
    return CommandWord(
        rt=rt,
        direction=direction,
        subaddress=subaddress,
        data_word_count=data_word_count,
        raw=raw,
    )


def classify_message_format(
    message_type: int,
    command_word: CommandWord,
    word_count: int,
    timestamp_words: int,
) -> MessageFormat:
    """Classify a record into one of the 10 MIL-STD-1553 message formats.

    Uses a multi-signal approach for robust classification:

    1. The Type Word's message_type code (bits 0–6) determines the
       high-level category:
       - 0x02 → RECEIVE
       - 0x04 → TRANSMIT
       - 0x08 → RT_TO_RT
       - 0x10 → RECEIVE_BROADCAST
       - 0x18 → RT_TO_RT_BROADCAST

    2. For 0x01 (MODE_COMMAND), the record word count is inspected first —
       it decides whether a data word is present at all — and only then the
       Command Word's direction:
       a. RT address == 31 → broadcast mode code (no status word)
          - WC >= ts + 3 (has data word) → MODE_CODE_BCAST_DATA
          - Else → MODE_CODE_BCAST_NO_DATA
       b. RT address != 31 → non-broadcast mode code (always has a status word)
          - WC >= ts + 4 (has data word) and T/R == 1 → MODE_CODE_TX_DATA
          - WC >= ts + 4 (has data word) and T/R == 0 → MODE_CODE_RX_DATA
          - WC <  ts + 4 (no data word), either direction → MODE_CODE_NO_DATA

       ``ts`` is the timestamp word count for the resolved format (IRIG = 3,
       Standard = 2), so the thresholds below are the IRIG case:
       - MODE_CODE_BCAST_NO_DATA: WC = 5 (Type + 3×TS + ModeCmd)
       - MODE_CODE_BCAST_DATA:    WC = 6 (+ 1 DataWord)
       - MODE_CODE_NO_DATA:       WC = 6 (+ Status)
       - MODE_CODE_TX_DATA:       WC = 7 (+ Status + DataWord)
       - MODE_CODE_RX_DATA:       WC = 7 (+ DataWord + Status)

    3. 0x20 (SPURIOUS_DATA) classifies as MessageFormat.SPURIOUS_DATA —
       raw bus words captured with no command structure.

    Args:
        message_type: The type code from bits 0–6 of the Type Word.
        command_word: The decoded primary Command Word.
        word_count: Total record word count from the Type Word.

    Returns:
        The classified MessageFormat.

    Raises:
        ValueError: If the message_type is not one of the known MIE
            message types and therefore cannot be classified.

    Examples:
        >>> cmd = decode_command_word(0x797E)  # RT15, Receive, SA11
        >>> classify_message_format(0x02, cmd, 36, 3)
        <MessageFormat.RECEIVE: 1>
    """
    # ── Direct type-to-format mappings (one code → one format) ──────
    if message_type == MessageType.BC_TO_RT:
        return MessageFormat.RECEIVE

    if message_type == MessageType.RT_TO_BC:
        return MessageFormat.TRANSMIT

    if message_type == MessageType.RT_TO_RT:
        return MessageFormat.RT_TO_RT

    if message_type == MessageType.BROADCAST_BC_TO_RT:
        return MessageFormat.RECEIVE_BROADCAST

    if message_type == MessageType.BROADCAST_RT_TO_RT:
        return MessageFormat.RT_TO_RT_BROADCAST

    # ── Mode Command sub-classification (0x01 → 5 possible formats) ─
    if message_type == MessageType.MODE_COMMAND:
        return _classify_mode_code(command_word, word_count, timestamp_words)

    # ── Spurious data — raw bus words, no command structure ──────
    if message_type == MessageType.SPURIOUS_DATA:
        return MessageFormat.SPURIOUS_DATA

    # ── Should not be reachable if VALID_MESSAGE_TYPES was checked ──
    raise ValueError(f"Cannot classify message_type=0x{message_type:02X}")


def _classify_mode_code(cmd: CommandWord, word_count: int, timestamp_words: int) -> MessageFormat:
    """Sub-classify a Mode Command (type 0x01) into one of five formats.

    Combines Command Word fields with the record word count. The data-vs-no-data
    thresholds are relative to the timestamp word count (IRIG = 3, Standard = 2),
    so a Standard record (one word shorter) classifies correctly (L2-MSG-004).

    Decision tree (``ts`` = ``timestamp_words``). Note the word-count test comes
    **before** the direction test on the non-broadcast branch: a transmit mode
    code with no data word is ``MODE_CODE_NO_DATA``, not ``MODE_CODE_TX_DATA``
    (its wire shape is ModeCmd + Status either way; the ``CMD`` column preserves
    the direction).

        ┌─ RT == 31 (broadcast, no status word)?
        │  ├─ WC >= ts + 3 → MODE_CODE_BCAST_DATA    (Type+TS+ModeCmd+Data)
        │  └─ WC <  ts + 3 → MODE_CODE_BCAST_NO_DATA (Type+TS+ModeCmd)
        └─ RT != 31 (non-broadcast, has status word)?
           ├─ WC >= ts + 4 (a data word is present)?
           │  ├─ T/R == 1 (transmit) → MODE_CODE_TX_DATA (ModeCmd+Status+Data)
           │  └─ T/R == 0 (receive)  → MODE_CODE_RX_DATA (ModeCmd+Data+Status)
           └─ WC <  ts + 4 → MODE_CODE_NO_DATA  (ModeCmd+Status)

    Args:
        cmd: The decoded Mode Command Word.
        word_count: Total record word count from the Type Word.
        timestamp_words: Timestamp word count for the resolved format
            (3 for IRIG, 2 for Standard).

    Returns:
        One of the five mode code MessageFormat variants.
    """
    # The data-vs-no-data thresholds are relative to the record's timestamp
    # word count (IRIG = 3, Standard = 2), not absolute — a Standard record is
    # one word shorter than the IRIG equivalent (L2-MSG-004). Fixed overhead per
    # shape: Type(1) + timestamp_words + ModeCmd(1) [+ Status(1) non-broadcast]
    # [+ Data(1) when present].
    is_broadcast = cmd.rt == 31

    if is_broadcast:
        # Broadcast mode codes have no status word.
        # With data: Type + TS + ModeCmd + Data = timestamp_words + 3
        # Without:   Type + TS + ModeCmd        = timestamp_words + 2
        if word_count >= timestamp_words + 3:
            return MessageFormat.MODE_CODE_BCAST_DATA
        return MessageFormat.MODE_CODE_BCAST_NO_DATA

    # Non-broadcast mode codes always carry a Status Word; a data word is present
    # only on the longer records (1553 mode codes 0–15 carry none, 16–31 carry
    # one). A mode code WITHOUT a data word — transmit or receive — is
    # Type + TS + ModeCmd + Status = timestamp_words + 3 and classifies as
    # MODE_CODE_NO_DATA (its wire shape is ModeCmd + Status either way; the CMD
    # column preserves the direction). With a data word it is TX_DATA
    # (Status → Data) or RX_DATA (Data → Status).
    #   With data: Type + TS + ModeCmd + Data + Status = timestamp_words + 4
    #   No data:   Type + TS + ModeCmd        + Status = timestamp_words + 3
    if word_count >= timestamp_words + 4:
        if cmd.direction == Direction.TRANSMIT:
            return MessageFormat.MODE_CODE_TX_DATA
        return MessageFormat.MODE_CODE_RX_DATA

    return MessageFormat.MODE_CODE_NO_DATA


class WhichInvariant(IntEnum):
    """Which structural invariant a record violated (L2-SYN-020 through L2-SYN-027).

    Used by callers (the reader) to phrase a precise diagnostic; the
    strict-mode path otherwise maps every violation to a single
    record-error class.
    """

    DIRECTION_BC_TO_RT = 1
    """L2-SYN-020: Type 0x02 requires Cmd direction = Receive."""

    DIRECTION_RT_TO_BC = 2
    """L2-SYN-021: Type 0x04 requires Cmd direction = Transmit."""

    WORD_COUNT_CAPACITY = 3
    """L2-SYN-022: TW.word_count too small for declared payload."""

    DIRECTION_RT_TO_RT_CMD2 = 4
    """L2-SYN-023: Cmd2 direction for RT-to-RT must be Receive."""

    STATUS_RT_MISMATCH = 5
    """L2-SYN-024: Status RT does not match Cmd RT.
    AnomalyWarn-class — real-bus noise possible."""

    TYPE_WORD_RESERVED_BIT = 6
    """L2-SYN-025: Type Word bit 15 (reserved) is set.
    AnomalyWarn-class — possible vendor extension."""

    DATA_WORD_COUNT_MISMATCH = 7
    """L2-SYN-027: RT-to-RT Cmd1 and Cmd2 disagree on data_word_count."""


class InvariantSeverity(IntEnum):
    """Policy class for a structural-invariant violation.

    - ``REJECT``: strict mode aborts; lenient mode WARN+skips the record.
    - ``ANOMALY_WARN``: both modes log a WARN and emit the record
      anyway. Used where outright rejection would produce false
      negatives on real-bus noise or vendor extensions (INV-005, 006).
    """

    REJECT = 1
    ANOMALY_WARN = 2


@dataclass(frozen=True)
class InvariantViolation:
    """Detail object returned by invariant-check functions."""

    kind: WhichInvariant
    severity: InvariantSeverity
    detail: str


def _min_payload_words(fmt: MessageFormat, command_word: CommandWord) -> int:
    """Per-format minimum payload word count, computed from Cmd1's
    declared data_word_count. Mirrors the Rust ``min_payload_words``
    helper. ``SPURIOUS_DATA`` returns 0 (capacity check skipped).

    Returns:
        The minimum number of payload words the format requires.

    Raises:
        ValueError: if ``fmt`` is not a known :class:`MessageFormat`. Every
            member is handled, so this is unreachable for a value produced by
            classification.
    """
    dwc = command_word.data_word_count
    if fmt in (MessageFormat.RECEIVE, MessageFormat.TRANSMIT):
        return dwc + 1
    if fmt == MessageFormat.RT_TO_RT:
        return dwc + 3
    if fmt == MessageFormat.RECEIVE_BROADCAST:
        return dwc
    if fmt == MessageFormat.RT_TO_RT_BROADCAST:
        return dwc + 2
    if fmt == MessageFormat.MODE_CODE_TX_DATA:
        return 2
    if fmt == MessageFormat.MODE_CODE_RX_DATA:
        return 2
    if fmt == MessageFormat.MODE_CODE_NO_DATA:
        return 1
    if fmt == MessageFormat.MODE_CODE_BCAST_NO_DATA:
        return 0
    if fmt == MessageFormat.MODE_CODE_BCAST_DATA:
        return 1
    if fmt == MessageFormat.SPURIOUS_DATA:
        return 0  # variable; no capacity check
    raise ValueError(f"Unhandled message format: {fmt}")


def validate_structural_invariants(
    type_word: TypeWord,
    command_word: CommandWord,
    msg_fmt: MessageFormat,
    ts_words: int,
) -> InvariantViolation | None:
    """L2-SYN-020..025: structural invariants per the locked schema.

    Returns ``None`` if all invariants hold; otherwise returns an
    :class:`InvariantViolation` describing the first failure.

    Reject-class invariants checkable before payload extraction:

    - INV-001 (L2-SYN-020): Type 0x02 (BC→RT) → Cmd direction = Receive
    - INV-002 (L2-SYN-021): Type 0x04 (RT→BC) → Cmd direction = Transmit
    - INV-003 (L2-SYN-022): ``TW.word_count >= 1 + ts_words + 1 + min_payload_words(format, cmd)``

    The remaining structural invariants live in companion functions
    because they need data not available here: RT-to-RT Cmd2 direction
    (L2-SYN-023) in :func:`validate_post_extract_invariants` (Cmd2 sits
    inside the payload), and the AnomalyWarn-class Status-RT-vs-Cmd-RT
    (L2-SYN-024) and Type-Word reserved-bit (L2-SYN-025) checks in
    :func:`detect_record_anomalies`.

    Returns:
        ``None`` if every checked invariant holds, otherwise the
        :class:`InvariantViolation` for the FIRST failure -- checks stop there.
    """
    # INV-001 / INV-002: per-type direction.
    if (
        type_word.message_type == MessageType.BC_TO_RT
        and command_word.direction != Direction.RECEIVE
    ):
        return InvariantViolation(
            kind=WhichInvariant.DIRECTION_BC_TO_RT,
            severity=InvariantSeverity.REJECT,
            detail=(
                f"Type 0x02 (BC->RT) requires Cmd direction = Receive; "
                f"got Transmit (raw Cmd = 0x{command_word.raw:04X})"
            ),
        )
    if (
        type_word.message_type == MessageType.RT_TO_BC
        and command_word.direction != Direction.TRANSMIT
    ):
        return InvariantViolation(
            kind=WhichInvariant.DIRECTION_RT_TO_BC,
            severity=InvariantSeverity.REJECT,
            detail=(
                f"Type 0x04 (RT->BC) requires Cmd direction = Transmit; "
                f"got Receive (raw Cmd = 0x{command_word.raw:04X})"
            ),
        )

    # INV-003: word-count capacity check.
    min_wc = 1 + ts_words + 1 + _min_payload_words(msg_fmt, command_word)
    if type_word.word_count < min_wc:
        return InvariantViolation(
            kind=WhichInvariant.WORD_COUNT_CAPACITY,
            severity=InvariantSeverity.REJECT,
            detail=(
                f"TW.word_count = {type_word.word_count} is too small for "
                f"declared payload (need at least {min_wc} for {msg_fmt.name} "
                f"with data_word_count = {command_word.data_word_count})"
            ),
        )

    return None


def validate_post_extract_invariants(
    msg_fmt: MessageFormat,
    command_word: CommandWord,
    cmd2: CommandWord | None,
) -> InvariantViolation | None:
    """L2-SYN-023 / L2-SYN-027: post-extract checks for RT-to-RT formats.

    Called post-extract because Cmd2 lives inside the payload. For
    non-RT-to-RT formats (or when cmd2 is None) this is a no-op.

    - L2-SYN-023: Cmd2 direction must be Receive.
    - L2-SYN-027: Cmd1 and Cmd2 must agree on ``data_word_count``. The bus
      protocol carries one data-word count for the transaction; the
      capacity invariant (L2-SYN-022) only sees Cmd1, so a Cmd2 that
      disagrees — including the over-claim that the record-bounded reads of
      L2-DEC-009 defend against — is caught here.

    Returns:
        ``None`` if the invariants hold, or if the format is not RT-to-RT or
        ``cmd2`` is ``None`` -- both are no-ops. Otherwise the
        :class:`InvariantViolation` for the first failure.
    """
    if msg_fmt not in (MessageFormat.RT_TO_RT, MessageFormat.RT_TO_RT_BROADCAST):
        return None
    if cmd2 is None:
        return None
    if cmd2.direction != Direction.RECEIVE:
        return InvariantViolation(
            kind=WhichInvariant.DIRECTION_RT_TO_RT_CMD2,
            severity=InvariantSeverity.REJECT,
            detail=(
                f"RT-to-RT Cmd2 requires direction = Receive; got "
                f"Transmit (raw Cmd2 = 0x{cmd2.raw:04X})"
            ),
        )
    if command_word.data_word_count != cmd2.data_word_count:
        return InvariantViolation(
            kind=WhichInvariant.DATA_WORD_COUNT_MISMATCH,
            severity=InvariantSeverity.REJECT,
            detail=(
                f"RT-to-RT Cmd1/Cmd2 data_word_count mismatch: "
                f"Cmd1 = {command_word.data_word_count}, "
                f"Cmd2 = {cmd2.data_word_count} "
                f"(raw Cmd1 = 0x{command_word.raw:04X}, "
                f"Cmd2 = 0x{cmd2.raw:04X})"
            ),
        )
    return None


def detect_record_anomalies(
    type_word: TypeWord,
    command_word: CommandWord,
    status_word: int | None,
) -> list[InvariantViolation]:
    """L2-SYN-024 / L2-SYN-025: anomaly-class observations.

    Both invariants are anomaly detectors rather than corruption
    rejections; the reader logs each violation as a WARN and continues
    emitting the record. Returns a list because multiple anomalies can
    fire on the same record (e.g., status RT mismatch AND reserved bit
    set simultaneously).

    Returns:
        Every anomaly found, possibly empty. An empty list means no anomaly,
        not an error -- these never reject a record.
    """
    out: list[InvariantViolation] = []

    # INV-005: Status RT vs Cmd RT.
    if status_word is not None:
        status_rt = (status_word >> 11) & 0x1F
        if status_rt != command_word.rt:
            out.append(
                InvariantViolation(
                    kind=WhichInvariant.STATUS_RT_MISMATCH,
                    severity=InvariantSeverity.ANOMALY_WARN,
                    detail=(
                        f"Status RT = {status_rt} does not match Cmd RT = "
                        f"{command_word.rt} (raw Status = 0x{status_word:04X}); "
                        f"possible bus interference"
                    ),
                )
            )

    # INV-006: Type Word bit 15 reserved.
    if (type_word.raw >> 15) & 1 != 0:
        out.append(
            InvariantViolation(
                kind=WhichInvariant.TYPE_WORD_RESERVED_BIT,
                severity=InvariantSeverity.ANOMALY_WARN,
                detail=(
                    f"Type Word bit 15 (reserved) is set in raw "
                    f"0x{type_word.raw:04X}; possible undocumented vendor "
                    f"extension"
                ),
            )
        )

    return out


def is_valid_message_type(message_type: int) -> bool:
    """Check if a message type code is one of the 7 known DDC types.

    Args:
        message_type: The type code from bits 0–6 of the Type Word.

    Returns:
        True if the code is recognized (0x01, 0x02, 0x04, 0x08, 0x10,
        0x18, or 0x20).
    """
    return message_type in VALID_MESSAGE_TYPES


def is_terminator_type_word(raw: int) -> bool:
    """Return whether ``raw`` is the end-of-records terminator (null Type Word).

    See :data:`TERMINATOR_TYPE_WORD` and L2-RDR-021 / L2-SYN-028.

    Returns:
        ``True`` when ``raw`` is the null Type Word that ends the record
        stream.
    """
    return raw == TERMINATOR_TYPE_WORD


def read_u16(data: ByteSource, offset: int) -> int:
    """Read a single little-endian unsigned 16-bit integer.

    Args:
        data: Raw byte buffer.
        offset: Byte offset to read from.

    Returns:
        The decoded 16-bit unsigned integer.

    Raises:
        struct.error: If there are not enough bytes at the given offset.
    """
    return int(struct.unpack_from(_LE_U16, data, offset)[0])


def read_u16_array(data: ByteSource, offset: int, count: int) -> tuple[int, ...]:
    """Read an array of little-endian unsigned 16-bit integers.

    Args:
        data: Raw byte buffer.
        offset: Starting byte offset.
        count: Number of 16-bit words to read.

    Returns:
        A tuple of decoded 16-bit unsigned integers.

    Raises:
        struct.error: If there are not enough bytes for the requested count.
    """
    fmt = f"<{count}H"
    return struct.unpack_from(fmt, data, offset)
