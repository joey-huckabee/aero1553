//! Record alignment, validation, and sync recovery.
//!
//! Pure functions: no logging, no side effects. The reader is responsible
//! for emitting any log output based on the returned values.

use crate::decode::{
    MIN_RECORD_WORDS_STANDARD, decode_type_word, is_terminator_type_word, message_type_is_valid,
    read_u16,
};
use crate::models::{TimestampFormat, timestamp_word_count};
use std::fmt;

/// 64 KB scan cap. Covers any reasonable header or corruption gap without
/// risking a runaway scan over multi-gigabyte files.
pub const MAX_SCAN_BYTES: usize = 65_536;

/// Word count field is 6 bits → max record = 63 × 2 = 126 bytes.
pub const MAX_RECORD_BYTES: usize = 126;

/// L2-SYN-026 default look-ahead depth. Configurable via
/// `decode.lookahead_records` (TOML) or `--lookahead-records` (CLI),
/// range `[1, 32]`.
///
/// Deliberately left at 2. Raising it does reject non-MIE input (ordinary prose
/// yields a 2-record chain by chance; depth 8 rejects all but one file in this
/// repo) — but the depth applies to *entry* decisions only. Continuous
/// validation of an already-locked chain does no look-ahead at all, because a
/// well-formed record must never be discarded on account of its successor
/// (L2-SYN-005). Operators who want the stricter wrong-input screening can set
/// `--lookahead-records` higher per invocation.
pub const DEFAULT_LOOKAHEAD_RECORDS: usize = 2;

/// Precise reason a candidate record failed sync validation.
///
/// The existing [`validate_record`] boolean API remains the compatibility
/// wrapper for callers that only need a yes/no answer. Readers and diagnostic
/// tooling can use [`validate_record_detailed`] to distinguish failures without
/// reimplementing the validation rules.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValidationFailure {
    TypeWordUnreadable,
    UnknownMessageType,
    InvalidWordCount,
    RecordTruncated,
    IrigHourOutOfRange,
    IrigMinuteOutOfRange,
    IrigSecondOutOfRange,
    IrigMicrosecondOutOfRange,
    IrigDayOutOfRange,
    LookaheadUnknownMessageType,
    LookaheadInvalidWordCount,
}

impl fmt::Display for ValidationFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let detail = match self {
            Self::TypeWordUnreadable => "Type Word is not readable",
            Self::UnknownMessageType => "message type is unknown",
            Self::InvalidWordCount => "word count is outside the valid range",
            Self::RecordTruncated => "record extends beyond end of file",
            Self::IrigHourOutOfRange => "IRIG hour is out of range",
            Self::IrigMinuteOutOfRange => "IRIG minute is out of range",
            Self::IrigSecondOutOfRange => "IRIG second is out of range",
            Self::IrigMicrosecondOutOfRange => "IRIG microsecond is out of range",
            Self::IrigDayOutOfRange => "IRIG day-of-year is out of range",
            Self::LookaheadUnknownMessageType => "look-ahead message type is unknown",
            Self::LookaheadInvalidWordCount => "look-ahead word count is outside the valid range",
        };
        f.write_str(detail)
    }
}

/// Minimum word count for a record under `ts_format`. If `None`, uses
/// the smaller (Standard) minimum so an unknown format is permissive.
#[inline]
fn min_word_count(ts_format: Option<TimestampFormat>) -> u16 {
    match ts_format {
        Some(fmt @ (TimestampFormat::Irig | TimestampFormat::Standard)) => {
            1 + timestamp_word_count(fmt) + 1
        }
        _ => MIN_RECORD_WORDS_STANDARD,
    }
}

/// True if a valid MIE record starts at `offset` per all heuristics
/// including an N-record look-ahead (L2-SYN-005, L2-SYN-026).
///
/// `lookahead_records` is the total number of records checked, including
/// the candidate itself: `1` means no look-ahead beyond the candidate,
/// `2` means one additional record (the historical default), and so on.
/// The look-ahead walk advances through each subsequent candidate by its
/// declared `word_count`; EOF terminates the walk gracefully without
/// rejecting the original candidate.
#[must_use]
pub fn validate_record(
    data: &[u8],
    offset: usize,
    file_len: usize,
    ts_format: Option<TimestampFormat>,
    lookahead_records: usize,
) -> bool {
    validate_record_detailed(data, offset, file_len, ts_format, lookahead_records).is_ok()
}

/// Validate a candidate record and return the precise failure reason.
///
/// The look-ahead honors the L2-SYN-028 end-of-records terminator: a null
/// Type Word (`0x0000`) at the candidate's next-record boundary confirms the
/// candidate as the last record in the stream. This is the trusted-boundary
/// path used by forward decode and first-record detection. Sync **recovery**,
/// which probes arbitrary un-aligned offsets, must NOT honor the terminator
/// (a mis-aligned candidate could otherwise land its boundary on a stray zero
/// *data* word and validate as a bogus "last record"); `recover_sync` calls
/// the strict form via the private `validate_record_detailed_impl` with
/// `honor_terminator = false`.
///
/// # Errors
///
/// Returns the [`ValidationFailure`] that rejected the candidate — an
/// unreadable Type Word, an unknown message type, an out-of-range word count, a
/// record whose extent runs past `file_len`, an implausible timestamp, or a
/// look-ahead follower that itself fails. The variant is what lets the reader
/// narrate *why* sync was lost rather than only that it was.
pub fn validate_record_detailed(
    data: &[u8],
    offset: usize,
    file_len: usize,
    ts_format: Option<TimestampFormat>,
    lookahead_records: usize,
) -> Result<(), ValidationFailure> {
    validate_record_detailed_impl(data, offset, file_len, ts_format, lookahead_records, true)
}

/// Core validator. `honor_terminator` controls whether a null Type Word
/// (`0x0000`) at the candidate's look-ahead boundary is treated as the
/// L2-SYN-028 end-of-records terminator (graceful confirmation) or as an
/// invalid follower (rejection). See [`validate_record_detailed`].
fn validate_record_detailed_impl(
    data: &[u8],
    offset: usize,
    file_len: usize,
    ts_format: Option<TimestampFormat>,
    lookahead_records: usize,
    honor_terminator: bool,
) -> Result<(), ValidationFailure> {
    // Check 1: Type Word readable.
    let Some(type_raw) = read_u16(data, offset) else {
        return Err(ValidationFailure::TypeWordUnreadable);
    };
    if offset.checked_add(2).is_none_or(|end| end > file_len) {
        return Err(ValidationFailure::TypeWordUnreadable);
    }
    let tw = decode_type_word(type_raw);

    // Check 2: Valid message type.
    if !message_type_is_valid(tw.message_type) {
        return Err(ValidationFailure::UnknownMessageType);
    }

    // Check 3: Plausible word count.
    let min_wc = min_word_count(ts_format);
    if tw.word_count < min_wc || tw.word_count > 63 {
        return Err(ValidationFailure::InvalidWordCount);
    }

    // Check 4: Record fits in file.
    let record_bytes = usize::from(tw.word_count) * 2;
    if offset
        .checked_add(record_bytes)
        .is_none_or(|end| end > file_len)
    {
        return Err(ValidationFailure::RecordTruncated);
    }

    // Check 5: IRIG timestamp field ranges (L2-SYN-004 / L2-SYN-019).
    if ts_format == Some(TimestampFormat::Irig) {
        validate_irig_fields(data, offset, file_len)?;
    }

    // Check 6: N-record look-ahead (L2-SYN-005 / L2-SYN-026 / L2-SYN-028).
    validate_lookahead_records(
        data,
        offset,
        record_bytes,
        file_len,
        min_wc,
        lookahead_records,
        honor_terminator,
    )
}

/// Check 5: IRIG timestamp field range checks (L2-SYN-004 / L2-SYN-019). We
/// need all three timestamp words to evaluate microsecond and day; when they
/// are not all readable (near EOF) this is a no-op — the in-bounds checks in
/// the caller remain authoritative.
fn validate_irig_fields(
    data: &[u8],
    offset: usize,
    file_len: usize,
) -> Result<(), ValidationFailure> {
    if offset.checked_add(8).is_none_or(|end| end > file_len) {
        return Ok(());
    }
    let (Some(ts_upper), Some(ts_middle), Some(ts_lower)) = (
        read_u16(data, offset + 2),
        read_u16(data, offset + 4),
        read_u16(data, offset + 6),
    ) else {
        return Ok(());
    };

    let freerun = (ts_upper >> 15) & 1 == 1;
    let day = (ts_upper >> 5) & 0x1FF; // bits 13-5
    let hour = ts_upper & 0x1F;
    let minute = (ts_middle >> 10) & 0x3F;
    let second = (ts_middle >> 4) & 0x3F;
    let microsecond = (u32::from(ts_middle & 0xF) << 16) | u32::from(ts_lower);

    if hour >= 24 {
        return Err(ValidationFailure::IrigHourOutOfRange);
    }
    if minute >= 60 {
        return Err(ValidationFailure::IrigMinuteOutOfRange);
    }
    if second >= 60 {
        return Err(ValidationFailure::IrigSecondOutOfRange);
    }
    if microsecond > 999_999 {
        return Err(ValidationFailure::IrigMicrosecondOutOfRange);
    }
    // L2-SYN-019: skip the day-of-year range check when freerun is set —
    // the free-running oscillator is not calendar-locked. Hour/minute/second/
    // microsecond still apply (they are a function of the counter modulus).
    if !freerun && !(1..=366).contains(&day) {
        return Err(ValidationFailure::IrigDayOutOfRange);
    }
    Ok(())
}

/// Check 6: N-record look-ahead (L2-SYN-005 / L2-SYN-026). Walk up to
/// `lookahead_records - 1` subsequent records, each validated on the same Type
/// Word fields (message type + word-count plausibility) as the candidate.
/// Advance by each candidate's declared `word_count`; EOF — or the L2-SYN-028
/// end-of-records terminator when `honor_terminator` — ends the walk gracefully
/// without rejecting the original candidate.
fn validate_lookahead_records(
    data: &[u8],
    offset: usize,
    record_bytes: usize,
    file_len: usize,
    min_wc: u16,
    lookahead_records: usize,
    honor_terminator: bool,
) -> Result<(), ValidationFailure> {
    let n = lookahead_records.max(1);
    let Some(mut next_offset) = offset.checked_add(record_bytes) else {
        return Err(ValidationFailure::RecordTruncated);
    };
    for _ in 1..n {
        // EOF: the remaining unchecked records (if any) don't exist — not a
        // rejection.
        if next_offset + 2 > file_len {
            break;
        }
        let Some(next_raw) = read_u16(data, next_offset) else {
            break;
        };
        // L2-SYN-028: a null Type Word (0x0000) is the end-of-records
        // terminator, not an invalid follower — treat it like EOF so the last
        // real record of a recording is confirmed rather than dropped. Only on
        // the trusted-boundary path; sync recovery passes `honor_terminator =
        // false` so a mis-aligned candidate cannot validate off a stray zero
        // data word.
        if honor_terminator && is_terminator_type_word(next_raw) {
            break;
        }
        let next_tw = decode_type_word(next_raw);
        if !message_type_is_valid(next_tw.message_type) {
            return Err(ValidationFailure::LookaheadUnknownMessageType);
        }
        if next_tw.word_count < min_wc || next_tw.word_count > 63 {
            return Err(ValidationFailure::LookaheadInvalidWordCount);
        }
        // Advance by the candidate's declared length so the next iteration
        // validates the record AFTER it, not 2 bytes forward.
        let next_record_bytes = usize::from(next_tw.word_count) * 2;
        if next_record_bytes == 0 {
            // Defensive — wc=0 is already rejected by the min_wc floor above,
            // so this is unreachable; break rather than loop forever.
            break;
        }
        let Some(advance) = next_offset.checked_add(next_record_bytes) else {
            break;
        };
        next_offset = advance;
    }

    Ok(())
}

/// Outcome of a scan: where the next valid record is, and how many bytes
/// of header/garbage were skipped to reach it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ScanHit {
    pub offset: usize,
    pub skipped: usize,
}

/// Find the byte offset of the first valid record in the file.
///
/// Scans `[0, min(file_len, max_scan))` in 2-byte increments. If `offset == 0`
/// is valid, returns immediately (no header). If a header is present, returns
/// the offset just after it. Returns `None` if no valid record found within
/// the scan window.
#[must_use]
pub fn find_first_record(
    data: &[u8],
    file_len: usize,
    ts_format: Option<TimestampFormat>,
    max_scan: usize,
    lookahead_records: usize,
) -> Option<ScanHit> {
    let scan_end = file_len.min(max_scan);
    let mut offset = 0;
    while offset < scan_end {
        if validate_record(data, offset, file_len, ts_format, lookahead_records) {
            return Some(ScanHit {
                offset,
                skipped: offset,
            });
        }
        offset += 2;
    }
    None
}

/// Number of consecutive candidate records sampled by
/// [`is_homogeneous_payload`] for the L2-SYN-018 defense. Must be >= 4
/// per the spec.
pub const HOMOGENEITY_SAMPLE_RECORDS: usize = 4;

/// L2-SYN-018: detect pathological homogeneous-payload inputs.
///
/// After header detection finds a candidate record at `offset`, compare
/// the next [`HOMOGENEITY_SAMPLE_RECORDS`] consecutive `record_bytes`-
/// sized chunks. If they are byte-identical in every position except
/// the timestamp triple (bytes 2..8 of each record), the input is
/// pathological — most likely a single-byte pad (e.g. 0x20-fill, where
/// `0x20 0x20` parses as a valid `SPURIOUS_DATA` Type Word and look-ahead
/// validation alone admits the stream). Returns `true` iff the input
/// SHALL be rejected.
#[must_use]
pub fn is_homogeneous_payload(data: &[u8], offset: usize, record_bytes: usize) -> bool {
    let total = HOMOGENEITY_SAMPLE_RECORDS.saturating_mul(record_bytes);
    if offset.saturating_add(total) > data.len() {
        return false;
    }
    let first = &data[offset..offset + record_bytes];
    for i in 1..HOMOGENEITY_SAMPLE_RECORDS {
        let rec_start = offset + i * record_bytes;
        let other = &data[rec_start..rec_start + record_bytes];
        // Compare positions [0..2) (Type Word) and [8..record_bytes)
        // (Cmd + payload). Skip bytes 2..8 (IRIG timestamp triple).
        // For Standard-format records (4-byte timestamp), this skip
        // is conservative — it ignores 2 extra bytes of Cmd, which
        // only weakens the rejection slightly.
        if first[..2] != other[..2] {
            return false;
        }
        if record_bytes > 8 && first[8..] != other[8..] {
            return false;
        }
    }
    true
}

/// Diagnostic for L2-RDR-004: locate the first structurally-valid Type
/// Word that fails only the length check.
///
/// Called after [`find_first_record`] returns `None` to distinguish
/// "no MIE record at all" (`MieError::NoValidRecords`) from "valid
/// Type Word found but its declared extent runs past EOF"
/// (`MieError::FirstRecordTruncated`).
///
/// Walks the same 2-byte-aligned grid as `find_first_record` but
/// omits the fits-in-file check and the look-ahead confirmation, so it
/// matches a Type Word that *would have been valid* if the file were
/// long enough. Returns `Some((offset, record_bytes, available))` for
/// the first such candidate, or `None`.
#[must_use]
pub fn diagnose_header_scan_failure(
    data: &[u8],
    file_len: usize,
    ts_format: Option<TimestampFormat>,
    max_scan: usize,
) -> Option<(usize, usize, usize)> {
    use crate::decode::{decode_type_word, message_type_is_valid, read_u16};
    use crate::models::timestamp_word_count;

    let scan_end = file_len.min(max_scan);
    let resolved = ts_format.unwrap_or(TimestampFormat::Irig);
    let ts_words = timestamp_word_count(resolved);
    let min_wc: u16 = 1 + ts_words + 1;
    let mut offset = 0;
    while offset + 2 <= scan_end {
        let Some(type_raw) = read_u16(data, offset) else {
            break;
        };
        let tw = decode_type_word(type_raw);
        if !message_type_is_valid(tw.message_type) {
            offset += 2;
            continue;
        }
        if tw.word_count < min_wc || tw.word_count > 63 {
            offset += 2;
            continue;
        }
        let record_bytes = usize::from(tw.word_count) * 2;
        if offset + record_bytes > file_len {
            return Some((offset, record_bytes, file_len - offset));
        }
        // Otherwise the Type Word looks valid AND fits; find_first_record
        // would have already returned it unless IRIG range / look-ahead
        // rejected it. Either way, not a length-driven failure — keep
        // scanning for a candidate whose only problem is length.
        offset += 2;
    }
    None
}

/// Walk forward from `offset` looking for the next valid record. Used after
/// validation fails mid-file.
#[must_use]
pub fn recover_sync(
    data: &[u8],
    offset: usize,
    file_len: usize,
    ts_format: Option<TimestampFormat>,
    max_scan: usize,
    lookahead_records: usize,
) -> Option<ScanHit> {
    let scan_start = offset.saturating_add(2);
    let scan_end = file_len.min(offset.saturating_add(max_scan));
    let mut candidate = scan_start;
    while candidate < scan_end {
        // Strict look-ahead during recovery (honor_terminator = false): a
        // mis-aligned candidate whose declared length happens to land its
        // boundary on a zero data word must NOT validate as a bogus
        // "last record before terminator". Recovery requires a real
        // follower record (or EOF), never a stray zero.
        if validate_record_detailed_impl(
            data,
            candidate,
            file_len,
            ts_format,
            lookahead_records,
            false,
        )
        .is_ok()
        {
            return Some(ScanHit {
                offset: candidate,
                skipped: candidate - offset,
            });
        }
        candidate += 2;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One synthetic record:
    ///   Type Word 0x2402 → type=0x02, bus A, `word_count=36`, no error
    ///   Followed by 35 zeroed words to fill to 36 total
    fn make_valid_record_36w(count: usize) -> Vec<u8> {
        let mut buf = Vec::with_capacity(count * 36 * 2);
        for _ in 0..count {
            // Type word 0x2402 LE
            buf.extend_from_slice(&[0x02, 0x24]);
            // 35 zero words (timestamp + cmd + data + status)
            buf.extend_from_slice(&[0u8; 70]);
        }
        buf
    }

    /// Requirements: L2-SYN-005
    #[test]
    fn validate_accepts_clean_record() {
        let buf = make_valid_record_36w(2);
        assert!(validate_record(
            &buf,
            0,
            buf.len(),
            None,
            DEFAULT_LOOKAHEAD_RECORDS
        ));
    }

    /// Requirements: L2-SYN-001
    #[test]
    fn validate_rejects_invalid_type() {
        // Type word with bad message type 0x03
        let buf = vec![0x03, 0x24, 0x00, 0x00];
        assert!(!validate_record(
            &buf,
            0,
            buf.len(),
            None,
            DEFAULT_LOOKAHEAD_RECORDS
        ));
    }

    /// Requirements: L2-SYN-003
    #[test]
    fn validate_rejects_truncated() {
        // Type word claims 36 words = 72 bytes, but only 4 bytes available
        let buf = vec![0x02, 0x24, 0x00, 0x00];
        assert!(!validate_record(
            &buf,
            0,
            buf.len(),
            None,
            DEFAULT_LOOKAHEAD_RECORDS
        ));
    }

    /// Requirements: L2-SYN-006
    #[test]
    fn find_first_record_at_zero() {
        let buf = make_valid_record_36w(2);
        let hit = find_first_record(
            &buf,
            buf.len(),
            None,
            MAX_SCAN_BYTES,
            DEFAULT_LOOKAHEAD_RECORDS,
        )
        .unwrap();
        assert_eq!(hit.offset, 0);
        assert_eq!(hit.skipped, 0);
    }

    /// Requirements: L2-SYN-006
    #[test]
    fn find_first_record_skips_header() {
        // 16-byte ASCII header + valid records
        let mut buf = b"DDC-HEADER-1234\n".to_vec(); // 16 bytes
        buf.extend(make_valid_record_36w(2));
        let hit = find_first_record(
            &buf,
            buf.len(),
            None,
            MAX_SCAN_BYTES,
            DEFAULT_LOOKAHEAD_RECORDS,
        )
        .unwrap();
        assert_eq!(hit.offset, 16);
        assert_eq!(hit.skipped, 16);
    }

    /// Requirements: L2-SYN-007
    #[test]
    fn find_first_record_scan_is_capped_at_max_scan_bytes() {
        let find = |buf: &[u8]| {
            find_first_record(
                buf,
                buf.len(),
                None,
                MAX_SCAN_BYTES,
                DEFAULT_LOOKAHEAD_RECORDS,
            )
        };
        // Valid records (two, so the look-ahead would confirm them) placed
        // past the cap are not reached.
        let mut past = vec![0xFF; MAX_SCAN_BYTES + 1024];
        past.extend(make_valid_record_36w(2));
        assert!(find(&past).is_none());
        // Control: the same records inside the cap are found, so the miss
        // above is the cap and not the garbage.
        let mut inside = vec![0xFF; 1024];
        inside.extend(make_valid_record_36w(2));
        assert_eq!(find(&inside).map(|hit| hit.offset), Some(1024));
    }

    /// Requirements: L2-SYN-008
    #[test]
    fn find_first_record_returns_none_when_no_valid() {
        let buf = vec![0xFFu8; 64];
        assert!(
            find_first_record(
                &buf,
                buf.len(),
                None,
                MAX_SCAN_BYTES,
                DEFAULT_LOOKAHEAD_RECORDS
            )
            .is_none()
        );
    }

    /// Requirements: L2-SYN-009
    #[test]
    fn recover_sync_walks_forward() {
        let mut buf = vec![0xFFu8; 6]; // 6 bytes of garbage
        buf.extend(make_valid_record_36w(2));
        let hit = recover_sync(
            &buf,
            0,
            buf.len(),
            None,
            MAX_SCAN_BYTES,
            DEFAULT_LOOKAHEAD_RECORDS,
        )
        .unwrap();
        assert_eq!(hit.offset, 6);
        assert_eq!(hit.skipped, 6);
    }

    /// Requirements: L2-SYN-010, L1-SYN-002
    #[test]
    fn recover_sync_capped_at_max_scan() {
        let mut buf = vec![0xFFu8; 200];
        // Valid record only after 200 bytes of garbage
        buf.extend(make_valid_record_36w(2));
        // Cap scan at 100 → can't find it
        assert!(recover_sync(&buf, 0, buf.len(), None, 100, DEFAULT_LOOKAHEAD_RECORDS).is_none());
        // Cap at 400 → found
        assert!(recover_sync(&buf, 0, buf.len(), None, 400, DEFAULT_LOOKAHEAD_RECORDS).is_some());
    }

    // ── IRIG range validation (L2-SYN-004, L2-SYN-019) ──────────────

    /// Build a minimal IRIG-shaped record from explicit timestamp word
    /// values. `word_count` = 5 (Type + 3 TS + Cmd, no data, no status).
    /// Two records back-to-back so the look-ahead check passes.
    fn make_irig_record_with_ts(upper: u16, middle: u16, lower: u16) -> Vec<u8> {
        // Type word 0x0502: type=0x02 BcToRt, bus A, wc=5, error=0.
        const TYPE_RAW: u16 = 0x0502;
        // Command Word: rt=5, dir=Recv, sa=1, dwc=30 (raw 0x283E).
        const CMD_RAW: u16 = 0x283E;

        let mut buf = Vec::with_capacity(20);
        for _ in 0..2 {
            buf.extend_from_slice(&TYPE_RAW.to_le_bytes());
            buf.extend_from_slice(&upper.to_le_bytes());
            buf.extend_from_slice(&middle.to_le_bytes());
            buf.extend_from_slice(&lower.to_le_bytes());
            buf.extend_from_slice(&CMD_RAW.to_le_bytes());
        }
        buf
    }

    /// Helper: build upper TS word from explicit fields.
    fn irig_upper(freerun: bool, day: u16, hour: u8) -> u16 {
        (u16::from(freerun) << 15) | ((day & 0x1FF) << 5) | u16::from(hour & 0x1F)
    }

    /// Helper: build middle TS word from explicit fields.
    fn irig_middle(minute: u8, second: u8, us_hi4: u8) -> u16 {
        ((u16::from(minute) & 0x3F) << 10)
            | ((u16::from(second) & 0x3F) << 4)
            | (u16::from(us_hi4) & 0xF)
    }

    /// L2-SYN-014 requires the boolean and detailed validators to be the
    /// same rules, so the two can never disagree about validity. That is
    /// true today because `validate_record` delegates to
    /// `validate_record_detailed(..).is_ok()` — but delegation is an
    /// implementation choice a later refactor could quietly undo, and until
    /// v2.12.0 the requirement was reported as met with no artifact behind
    /// it at all. This pins the property itself, across a corpus spanning a
    /// valid record and every distinct rejection reason, so re-implementing
    /// either form independently fails here.
    /// Requirements: L2-SYN-014
    #[test]
    fn boolean_and_detailed_validation_never_disagree() {
        let valid = make_irig_record_with_ts(irig_upper(false, 192, 15), irig_middle(54, 50, 0), 0);
        let first = &valid[..10];

        let corpus: Vec<Vec<u8>> = vec![
            valid.clone(),
            vec![],
            vec![0x02],
            [0x0503u16.to_le_bytes().as_slice(), &[0; 8]].concat(),
            [0x0202u16.to_le_bytes().as_slice(), &[0; 8]].concat(),
            [0x2402u16.to_le_bytes().as_slice(), &[0; 8]].concat(),
            make_irig_record_with_ts(irig_upper(false, 192, 24), irig_middle(54, 50, 0), 0),
            make_irig_record_with_ts(irig_upper(false, 192, 15), irig_middle(60, 50, 0), 0),
            [first, 0x0503u16.to_le_bytes().as_slice()].concat(),
            [first, 0x0202u16.to_le_bytes().as_slice()].concat(),
        ];

        for (i, data) in corpus.iter().enumerate() {
            for lookahead in [1usize, DEFAULT_LOOKAHEAD_RECORDS, 4] {
                let boolean =
                    validate_record(data, 0, data.len(), Some(TimestampFormat::Irig), lookahead);
                let detailed = validate_record_detailed(
                    data,
                    0,
                    data.len(),
                    Some(TimestampFormat::Irig),
                    lookahead,
                );
                assert_eq!(
                    boolean,
                    detailed.is_ok(),
                    "case {i} (lookahead {lookahead}): boolean said {boolean}, \
                     detailed said {detailed:?}"
                );
            }
        }
    }

    /// Requirements: L2-SYN-004, L2-SYN-005
    #[test]
    fn detailed_validation_reports_each_failure_reason() {
        let valid = make_irig_record_with_ts(irig_upper(false, 192, 15), irig_middle(54, 50, 0), 0);
        let first = &valid[..10];

        let cases = [
            (vec![0x02], ValidationFailure::TypeWordUnreadable),
            (
                [0x0503u16.to_le_bytes().as_slice(), &[0; 8]].concat(),
                ValidationFailure::UnknownMessageType,
            ),
            (
                [0x0202u16.to_le_bytes().as_slice(), &[0; 8]].concat(),
                ValidationFailure::InvalidWordCount,
            ),
            (
                [0x2402u16.to_le_bytes().as_slice(), &[0; 8]].concat(),
                ValidationFailure::RecordTruncated,
            ),
            (
                make_irig_record_with_ts(irig_upper(false, 192, 24), irig_middle(54, 50, 0), 0),
                ValidationFailure::IrigHourOutOfRange,
            ),
            (
                make_irig_record_with_ts(irig_upper(false, 192, 15), irig_middle(60, 50, 0), 0),
                ValidationFailure::IrigMinuteOutOfRange,
            ),
            (
                make_irig_record_with_ts(irig_upper(false, 192, 15), irig_middle(54, 60, 0), 0),
                ValidationFailure::IrigSecondOutOfRange,
            ),
            (
                make_irig_record_with_ts(
                    irig_upper(false, 192, 15),
                    irig_middle(54, 50, 0xF),
                    0x4240,
                ),
                ValidationFailure::IrigMicrosecondOutOfRange,
            ),
            (
                make_irig_record_with_ts(irig_upper(false, 0, 15), irig_middle(54, 50, 0), 0),
                ValidationFailure::IrigDayOutOfRange,
            ),
            (
                [first, 0x0503u16.to_le_bytes().as_slice()].concat(),
                ValidationFailure::LookaheadUnknownMessageType,
            ),
            (
                [first, 0x0202u16.to_le_bytes().as_slice()].concat(),
                ValidationFailure::LookaheadInvalidWordCount,
            ),
        ];

        for (data, expected) in cases {
            assert_eq!(
                validate_record_detailed(
                    &data,
                    0,
                    data.len(),
                    Some(TimestampFormat::Irig),
                    DEFAULT_LOOKAHEAD_RECORDS,
                ),
                Err(expected)
            );
        }
    }

    /// Requirements: L2-SYN-004
    #[test]
    fn validate_accepts_irig_with_valid_ranges() {
        // day=192, hour=15, minute=54, second=50, microsecond=456_225,
        // freerun=0 — matches the canonical conformance fixture.
        let upper = irig_upper(false, 192, 15);
        let middle = irig_middle(54, 50, 6); // us_hi4 = 6
        let lower = 0xF621u16; // us_lo16 = 0xF621
        let buf = make_irig_record_with_ts(upper, middle, lower);
        assert!(validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-004
    #[test]
    fn validate_rejects_irig_day_zero() {
        // day=0 is out of range per L2-SYN-004.
        let upper = irig_upper(false, 0, 15);
        let middle = irig_middle(54, 50, 0);
        let buf = make_irig_record_with_ts(upper, middle, 0);
        assert!(!validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-004
    #[test]
    fn validate_rejects_irig_day_above_366() {
        // day=367 is out of range per L2-SYN-004.
        let upper = irig_upper(false, 367, 15);
        let middle = irig_middle(54, 50, 0);
        let buf = make_irig_record_with_ts(upper, middle, 0);
        assert!(!validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-019
    #[test]
    fn validate_accepts_irig_day_zero_when_freerun() {
        // L2-SYN-019: freerun bypasses the day-of-year check.
        let upper = irig_upper(true, 0, 15);
        let middle = irig_middle(54, 50, 0);
        let buf = make_irig_record_with_ts(upper, middle, 0);
        assert!(validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-004
    #[test]
    fn validate_rejects_irig_microsecond_at_one_million() {
        // microsecond = 1_000_000 = (0xF << 16) | 0x4240 = 0xF4240.
        // us_hi4 = 0xF, us_lo16 = 0x4240. Rejected per L2-SYN-004.
        let upper = irig_upper(false, 192, 15);
        let middle = irig_middle(54, 50, 0xF);
        let lower = 0x4240u16;
        let buf = make_irig_record_with_ts(upper, middle, lower);
        assert!(!validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-004
    #[test]
    fn validate_accepts_irig_microsecond_at_max_valid() {
        // microsecond = 999_999 = (0xF << 16) | 0x423F = 0xF423F.
        let upper = irig_upper(false, 192, 15);
        let middle = irig_middle(54, 50, 0xF);
        let lower = 0x423Fu16;
        let buf = make_irig_record_with_ts(upper, middle, lower);
        assert!(validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-019
    #[test]
    fn validate_rejects_irig_microsecond_when_freerun_too() {
        // L2-SYN-019 only relaxes the DAY check, not microsecond.
        // freerun=true with out-of-range microseconds is still rejected.
        let upper = irig_upper(true, 0, 15);
        let middle = irig_middle(54, 50, 0xF);
        let lower = 0x4240u16; // 1_000_000
        let buf = make_irig_record_with_ts(upper, middle, lower);
        assert!(!validate_record(
            &buf,
            0,
            buf.len(),
            Some(TimestampFormat::Irig),
            DEFAULT_LOOKAHEAD_RECORDS,
        ));
    }

    /// Requirements: L2-SYN-002
    #[test]
    fn min_word_count_helper() {
        assert_eq!(min_word_count(Some(TimestampFormat::Irig)), 5);
        assert_eq!(min_word_count(Some(TimestampFormat::Standard)), 4);
        assert_eq!(min_word_count(None), MIN_RECORD_WORDS_STANDARD);
    }

    // ── L2-SYN-026 N-record look-ahead tests ─────────────────────────

    /// Requirements: L2-SYN-026
    #[test]
    fn validate_lookahead_n1_skips_lookahead() {
        // N=1 means no look-ahead. A single valid record with garbage
        // bytes right after it should still validate.
        let mut buf = make_valid_record_36w(1);
        // Append 4 bytes of plausible but invalid Type-Word garbage —
        // would fail the N=2 look-ahead but N=1 doesn't peek.
        buf.extend_from_slice(&[0xFF, 0xFF, 0x00, 0x00]);
        assert!(
            validate_record(&buf, 0, buf.len(), None, 1),
            "N=1 must not peek at the next record"
        );
        assert!(
            !validate_record(&buf, 0, buf.len(), None, 2),
            "N=2 must peek and reject the invalid follower"
        );
    }

    /// Requirements: L2-SYN-026
    #[test]
    fn validate_lookahead_n4_catches_second_corruption() {
        // Two valid records followed by garbage. N=2 (current default)
        // looks at the candidate + the next, both valid, accepts. N=4
        // also peeks at records 3 and 4 — those are garbage and
        // rejected. Demonstrates the value of higher N.
        let mut buf = make_valid_record_36w(2); // records 1 and 2: valid
        buf.extend_from_slice(&[0xFF, 0xFF, 0x00, 0x00]); // record 3 start: invalid Type Word
        assert!(
            validate_record(&buf, 0, buf.len(), None, 2),
            "N=2 only checks records 1 and 2 (both valid) -- accepts"
        );
        assert!(
            !validate_record(&buf, 0, buf.len(), None, 4),
            "N=4 reaches record 3's invalid Type Word and rejects"
        );
    }

    /// L2-SYN-028: a valid record whose look-ahead boundary is the null
    /// Type Word terminator (0x0000) is confirmed as the last record in the
    /// stream — not rejected. Without this the final record of every
    /// well-formed recording (which the recorder caps with 0x0000) would be
    /// dropped.
    /// Requirements: L2-SYN-028
    #[test]
    fn validate_lookahead_terminator_confirms_last_record() {
        let mut buf = make_valid_record_36w(1);
        buf.extend_from_slice(&[0x00, 0x00]); // end-of-records terminator
        assert!(
            validate_record(&buf, 0, buf.len(), None, DEFAULT_LOOKAHEAD_RECORDS),
            "a record followed by the 0x0000 terminator must validate"
        );
    }

    /// L2-SYN-028: sync recovery must NOT honor the terminator. During
    /// recovery the scan probes un-aligned offsets, so a candidate whose
    /// declared length lands its boundary on a stray zero *data word* must
    /// not validate as a bogus "last record". A record followed immediately
    /// by the terminator is therefore invisible to `recover_sync` (recovery
    /// requires a real follower record or EOF), even though forward
    /// validation accepts it.
    /// Requirements: L2-SYN-028
    #[test]
    fn recover_sync_does_not_honor_terminator() {
        // [2 bytes garbage][valid 72-byte record][0x0000 terminator].
        // recover_sync from offset 0 scans forward; the only structurally
        // valid record (at offset 2) is followed by the terminator, so the
        // strict recovery look-ahead rejects it and recovery fails.
        let mut buf = vec![0xFF, 0xFF];
        buf.extend(make_valid_record_36w(1));
        buf.extend_from_slice(&[0x00, 0x00]);
        assert!(
            recover_sync(
                &buf,
                0,
                buf.len(),
                None,
                MAX_SCAN_BYTES,
                DEFAULT_LOOKAHEAD_RECORDS
            )
            .is_none(),
            "recovery must not validate a record off a terminator follower"
        );
        // But forward validation of that same record (at its true boundary)
        // does accept it — proving the split is intentional.
        assert!(validate_record(
            &buf,
            2,
            buf.len(),
            None,
            DEFAULT_LOOKAHEAD_RECORDS
        ));
    }

    /// Requirements: L2-SYN-026
    #[test]
    fn validate_lookahead_eof_terminates_gracefully() {
        // A single valid record with no follower at all. Any N >= 1
        // must accept — EOF mid-walk doesn't reject.
        let buf = make_valid_record_36w(1);
        for n in [1usize, 2, 4, 8, 32] {
            assert!(
                validate_record(&buf, 0, buf.len(), None, n),
                "N={n}: EOF must not reject when the candidate itself is valid"
            );
        }
    }
}
