"""Record synchronization and alignment for MIE binary files.

This module provides functions for detecting valid record boundaries,
recovering from sync loss, and detecting file headers. It is the first
line of defense against corrupted data, unexpected file layouts, and
mid-file sync loss caused by error records or truncated writes.

These are **pure** functions: no logging, no I/O, no side effects. The reader
inspects what they return and emits every user-facing message itself, which is
what keeps the two implementations' log output aligned (``rust/src/sync.rs``
carries the same contract). It also avoids narrating outcomes the caller has
more context about: ``find_first_record`` returning ``None`` is not by itself a
problem — for a valid but empty recording it is the *expected* result, and this
module logging "no valid record found" there produced a warning that
contradicted the reader's own (correct) "empty capture" message on the very
next line.

Synchronization Strategy:

    1. **Initial Alignment (Header Detection):**
       Before decoding begins, the reader must find the first valid
       record. Some files start immediately with records, while others
       carry leading non-record bytes (e.g. a proprietary header with
       embedded ASCII identifiers like equipment names). The
       :func:`find_first_record` function scans from offset 0 looking
       for the first position that passes multi-point validation.

    2. **Continuous Validation:**
       At each record boundary during iteration, :func:`validate_record`
       confirms the current position is a valid record before committing
       to decode it. This catches corruption that occurs mid-file.

    3. **Sync Recovery (Walk Forward):**
       If a record fails validation, :func:`recover_sync` scans forward
       from the current position in 2-byte (word-aligned) increments
       looking for the next valid record. This handles:
       - Corrupted records in the middle of a file
       - Unexpected padding or filler bytes
       - Error record sequences that produced unusual layouts
       - Partial writes from recording termination

    4. **Look-Ahead Confirmation:**
       A single valid-looking Type Word is not sufficient — random data
       can coincidentally match. :func:`validate_record` uses an
       **N-record look-ahead** (configurable depth, default 2 per
       L2-SYN-026): a candidate is confirmed valid only if the next
       N-1 records (starting at offset + word_count * 2) also start with
       a valid Type Word. This dramatically reduces false positives.

Validation Heuristics (applied in order, fast checks first):

    1. Type Word message type (bits 0–6) must be in VALID_MESSAGE_TYPES.
    2. Word count (bits 8–13) must be >= minimum for the timestamp
       format (4 for Standard, 5 for IRIG) and <= 63 (6-bit field max).
    3. The record must not extend past the end of file.
    4. If IRIG timestamp: hour < 24, minute < 60, second < 60,
       microsecond <= 999_999, and (unless the freerun bit is set, per
       L2-SYN-019) day-of-year in [1, 366].
    5. Look-ahead: the next record's Type Word must also have a valid
       message type and plausible word count.

Performance Considerations:

    - All checks use O(1) bit operations on already-read 16-bit words.
    - No string allocations or complex parsing during scanning.
    - Look-ahead reads only 2 bytes (the next Type Word), not the full
      next record.
    - The scan in :func:`recover_sync` advances 2 bytes per step
      (word-aligned) and caps at a configurable maximum distance to
      prevent scanning entire multi-gigabyte files.
    - :func:`find_first_record` uses the same capped scan, defaulting
      to :data:`MAX_SCAN_BYTES` (64 KB) — sufficient for any known DDC
      file header.

Error Records and Sync:

    Error records (Type Word bit 14 set) and their SPURIOUS_DATA
    continuations are valid records with valid Type Words. They pass
    sync validation normally. The sync machinery does not need special
    error-aware logic — the reader's error handling (in ``reader.py``)
    processes them after sync validation confirms the record boundary.

    The one exception is if an error causes the DDC card to write a
    corrupt record (e.g., truncated mid-word). In that case, the word
    count will point past valid data, and the look-ahead check on the
    NEXT record will fail, triggering sync recovery. This is the
    correct behavior: skip the corrupt record, find the next good one.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from aero1553.decode import (
    MIN_RECORD_WORDS_STANDARD,
    decode_type_word,
    is_terminator_type_word,
    is_valid_message_type,
    read_u16,
)
from aero1553.models import TIMESTAMP_WORD_COUNTS, ByteSource, TimestampFormat

#: Maximum number of bytes to scan when searching for sync.
#: 64 KB covers any reasonable header or corruption gap.
MAX_SCAN_BYTES: Final[int] = 65_536

#: Maximum plausible record size in bytes. The word count field is 6 bits
#: (max 63), so the absolute maximum record is 63 × 2 = 126 bytes.
MAX_RECORD_BYTES: Final[int] = 126

#: L2-SYN-026 default look-ahead depth. Configurable via
#: ``decode.lookahead_records`` (TOML) or ``--lookahead-records`` (CLI),
#: range ``[1, 32]``.
#:
#: Deliberately left at 2. Raising it does reject non-MIE input (ordinary prose
#: yields a 2-record chain by chance; depth 8 rejects all but one file in this
#: repo) — but the depth applies to *entry* decisions only. Continuous
#: validation of an already-locked chain does no look-ahead at all, because a
#: well-formed record must never be discarded on account of its successor
#: (L2-SYN-005). Operators who want the stricter wrong-input screening can set
#: ``--lookahead-records`` higher per invocation.
DEFAULT_LOOKAHEAD_RECORDS: Final[int] = 2


class ValidationFailure(Enum):
    """Precise reason a candidate record failed sync validation."""

    TYPE_WORD_UNREADABLE = "Type Word is not readable"
    UNKNOWN_MESSAGE_TYPE = "message type is unknown"
    INVALID_WORD_COUNT = "word count is outside the valid range"
    RECORD_TRUNCATED = "record extends beyond end of file"
    IRIG_HOUR_OUT_OF_RANGE = "IRIG hour is out of range"
    IRIG_MINUTE_OUT_OF_RANGE = "IRIG minute is out of range"
    IRIG_SECOND_OUT_OF_RANGE = "IRIG second is out of range"
    IRIG_MICROSECOND_OUT_OF_RANGE = "IRIG microsecond is out of range"
    IRIG_DAY_OUT_OF_RANGE = "IRIG day-of-year is out of range"
    LOOKAHEAD_UNKNOWN_MESSAGE_TYPE = "look-ahead message type is unknown"
    LOOKAHEAD_INVALID_WORD_COUNT = "look-ahead word count is outside the valid range"

    def __str__(self) -> str:
        return self.value


def validate_record(
    data: ByteSource,
    offset: int,
    file_len: int,
    ts_format: TimestampFormat | None = None,
    lookahead_records: int = DEFAULT_LOOKAHEAD_RECORDS,
) -> bool:
    """Return whether a valid MIE record starts at the given offset.

    Returns:
        ``True`` when every check passes. Use
        :func:`validate_record_detailed` when the caller needs to know WHICH
        check failed.
    """
    return (
        validate_record_detailed(
            data,
            offset,
            file_len,
            ts_format,
            lookahead_records,
        )
        is None
    )


def validate_record_detailed(
    data: ByteSource,
    offset: int,
    file_len: int,
    ts_format: TimestampFormat | None = None,
    lookahead_records: int = DEFAULT_LOOKAHEAD_RECORDS,
    honor_terminator: bool = True,
) -> ValidationFailure | None:
    """Return the precise reason a candidate record fails validation.

    Applies fast heuristic checks in order of increasing cost. Does
    NOT decode the full record — only reads the Type Word (2 bytes)
    and optionally the timestamp words (4–6 bytes) for range validation.

    Args:
        data: Raw byte buffer (mmap or bytes).
        offset: Candidate byte offset to test.
        file_len: Total file length in bytes.
        ts_format: Known timestamp format, or None to skip timestamp
            field-range checks.
        lookahead_records: Total records checked (candidate + N-1 followers).
        honor_terminator: When ``True`` (the trusted-boundary path used by
            forward decode and first-record detection), a null Type Word
            (``0x0000``) at the candidate's look-ahead boundary is treated as
            the L2-SYN-028 end-of-records terminator and confirms the
            candidate. Sync **recovery** passes ``False`` so a mis-aligned
            candidate cannot validate off a stray zero data word.

    Returns:
        ``None`` for a valid record, otherwise the failure reason.
    """
    # ── Check 1: Enough bytes for a minimal Type Word read ─────────
    if offset + 2 > file_len:
        return ValidationFailure.TYPE_WORD_UNREADABLE

    type_raw = read_u16(data, offset)
    tw = decode_type_word(type_raw)

    # ── Check 2: Valid message type ────────────────────────────────
    if not is_valid_message_type(tw.message_type):
        return ValidationFailure.UNKNOWN_MESSAGE_TYPE

    # ── Check 3: Plausible word count ──────────────────────────────
    if ts_format is not None:
        min_wc = 1 + TIMESTAMP_WORD_COUNTS[ts_format] + 1
    else:
        # Use the smaller minimum if format unknown
        min_wc = MIN_RECORD_WORDS_STANDARD
    if tw.word_count < min_wc or tw.word_count > 63:
        return ValidationFailure.INVALID_WORD_COUNT

    # ── Check 4: Record fits within file ───────────────────────────
    record_bytes = tw.word_count * 2
    if offset + record_bytes > file_len:
        return ValidationFailure.RECORD_TRUNCATED

    # ── Check 5: IRIG timestamp field ranges (L2-SYN-004, L2-SYN-019) ──
    if ts_format == TimestampFormat.IRIG:
        irig_failure = _validate_irig_timestamp_ranges(data, offset, file_len)
        if irig_failure is not None:
            return irig_failure

    # ── Check 6: N-record look-ahead (L2-SYN-005, L2-SYN-026, L2-SYN-028) ──
    return _validate_lookahead_records(
        data, offset, record_bytes, file_len, min_wc, lookahead_records, honor_terminator
    )


def _validate_irig_timestamp_ranges(
    data: ByteSource, offset: int, file_len: int
) -> ValidationFailure | None:
    """Check 5: IRIG timestamp field ranges (L2-SYN-004 / L2-SYN-019). All three
    timestamp words are needed; a near-EOF record where they are not all readable
    is a no-op (the caller's in-bounds checks remain authoritative).

    Returns:
        The specific :class:`ValidationFailure` for the first field found out of
        range, or ``None`` when every field is in range -- and also when the
        three words are not all readable, which is a no-op rather than a
        failure.
    """
    if offset + 8 > file_len:
        return None
    ts_upper = read_u16(data, offset + 2)
    ts_middle = read_u16(data, offset + 4)
    ts_lower = read_u16(data, offset + 6)
    freerun = bool((ts_upper >> 15) & 1)
    day = (ts_upper >> 5) & 0x1FF  # bits 13-5
    hour = ts_upper & 0x1F
    minute = (ts_middle >> 10) & 0x3F
    second = (ts_middle >> 4) & 0x3F
    microsecond = ((ts_middle & 0xF) << 16) | ts_lower

    if hour >= 24:
        return ValidationFailure.IRIG_HOUR_OUT_OF_RANGE
    if minute >= 60:
        return ValidationFailure.IRIG_MINUTE_OUT_OF_RANGE
    if second >= 60:
        return ValidationFailure.IRIG_SECOND_OUT_OF_RANGE
    if microsecond > 999_999:
        return ValidationFailure.IRIG_MICROSECOND_OUT_OF_RANGE
    # L2-SYN-019: freerun skips the day-of-year check — the free-running
    # oscillator is not calendar-locked. Hour/minute/second/microsecond still
    # apply (they are a function of the counter modulus).
    if not freerun and not 1 <= day <= 366:
        return ValidationFailure.IRIG_DAY_OUT_OF_RANGE
    return None


def _validate_lookahead_records(
    data: ByteSource,
    offset: int,
    record_bytes: int,
    file_len: int,
    min_wc: int,
    lookahead_records: int,
    honor_terminator: bool,
) -> ValidationFailure | None:
    """Check 6: N-record look-ahead (L2-SYN-005 / L2-SYN-026). Validate each of
    the next ``lookahead_records - 1`` records' Type Word fields. EOF — or the
    L2-SYN-028 ``0x0000`` terminator when ``honor_terminator`` — ends the walk
    gracefully without rejecting the original candidate (recovery passes
    ``False`` so a mis-aligned candidate cannot validate off a stray zero).

    Returns:
        The :class:`ValidationFailure` for the first look-ahead record whose
        Type Word does not check out, or ``None`` when they all pass. Running
        out of data -- EOF, or the terminator when honoured -- returns ``None``:
        an incomplete walk does not reject the candidate.
    """
    n = max(1, lookahead_records)
    next_offset = offset + record_bytes
    for _ in range(1, n):
        if next_offset + 2 > file_len:
            break
        next_raw = read_u16(data, next_offset)
        if honor_terminator and is_terminator_type_word(next_raw):
            break
        next_tw = decode_type_word(next_raw)
        if not is_valid_message_type(next_tw.message_type):
            return ValidationFailure.LOOKAHEAD_UNKNOWN_MESSAGE_TYPE
        if next_tw.word_count < min_wc or next_tw.word_count > 63:
            return ValidationFailure.LOOKAHEAD_INVALID_WORD_COUNT
        next_record_bytes = next_tw.word_count * 2
        if next_record_bytes == 0:
            break
        next_offset += next_record_bytes
    return None


def find_first_record(
    data: ByteSource,
    file_len: int,
    ts_format: TimestampFormat | None = None,
    max_scan: int = MAX_SCAN_BYTES,
    lookahead_records: int = DEFAULT_LOOKAHEAD_RECORDS,
) -> int | None:
    """Find the byte offset of the first valid record in the file.

    Scans from offset 0 in 2-byte (word-aligned) increments, applying
    :func:`validate_record` at each position. Returns the offset of
    the first position that passes all validation checks, or None if
    no valid record is found within the scan distance.

    This handles files with and without headers:
    - Files starting directly with records return offset 0.
    - Files with proprietary headers (e.g., embedded ASCII equipment
      names) return the offset immediately after the header.

    Args:
        data: Raw byte buffer.
        file_len: Total file length in bytes.
        ts_format: Known timestamp format, or None for auto-detection
            (skips timestamp range checks during header scan).
        max_scan: Maximum bytes to scan before giving up.
        lookahead_records: Total records checked per candidate
            (candidate + N-1 followers), per L2-SYN-026.

    Returns:
        Byte offset of the first valid record, or None if not found.
        ``None`` is not inherently an error — the caller decides (an empty
        recording legitimately has no first record).
    """
    scan_end = min(file_len, max_scan)

    for offset in range(0, scan_end, 2):
        if validate_record(data, offset, file_len, ts_format, lookahead_records):
            return offset

    return None


def recover_sync(
    data: ByteSource,
    offset: int,
    file_len: int,
    ts_format: TimestampFormat | None = None,
    max_scan: int = MAX_SCAN_BYTES,
    lookahead_records: int = DEFAULT_LOOKAHEAD_RECORDS,
) -> int | None:
    """Recover sync by scanning forward for the next valid record.

    Called when the reader encounters an invalid record at the current
    position. Scans forward in 2-byte increments from ``offset + 2``,
    applying :func:`validate_record` at each position.

    Args:
        data: Raw byte buffer.
        offset: Current (invalid) position.
        file_len: Total file length.
        ts_format: Known timestamp format, or None.
        max_scan: Maximum bytes to scan from the current offset.
        lookahead_records: Total records checked per candidate
            (candidate + N-1 followers), per L2-SYN-026.

    Returns:
        Byte offset of the next valid record, or None if sync cannot
        be recovered within the scan distance.
    """
    scan_start = offset + 2
    scan_end = min(file_len, offset + max_scan)

    for candidate in range(scan_start, scan_end, 2):
        # Strict look-ahead during recovery (honor_terminator=False): a
        # mis-aligned candidate whose declared length lands its boundary on a
        # zero data word must NOT validate as a bogus "last record before
        # terminator". Recovery requires a real follower record (or EOF).
        if (
            validate_record_detailed(
                data,
                candidate,
                file_len,
                ts_format,
                lookahead_records,
                honor_terminator=False,
            )
            is None
        ):
            return candidate

    return None
