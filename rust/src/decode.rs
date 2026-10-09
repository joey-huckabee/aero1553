//! Pure binary → struct decoders for MIE record fields.
//!
//! All functions operate on `&[u8]` slices and use little-endian encoding,
//! matching `struct.unpack_from("<H", ...)` in the Python reference.

use crate::error::{MieError, MieResult};
use crate::models::{
    Bus, CommandWord, Direction, IrigTimestamp, MessageFormat, MessageType, StandardTimestamp,
    TimestampFormat, TypeWord, is_valid_message_type,
};

/// Minimum record size in bytes for IRIG: Type(2) + TS(6) + Cmd(2) = 10
pub const MIN_RECORD_BYTES: usize = 10;
/// Minimum record size in bytes for Standard: Type(2) + TS(4) + Cmd(2) = 8
pub const MIN_RECORD_BYTES_STANDARD: usize = 8;
/// Minimum record size in 16-bit words for IRIG: Type(1) + TS(3) + Cmd(1) = 5
pub const MIN_RECORD_WORDS: u16 = 5;
/// Minimum record size in 16-bit words for Standard: Type(1) + TS(2) + Cmd(1) = 4
pub const MIN_RECORD_WORDS_STANDARD: u16 = 4;

/// End-of-records terminator (L2-RDR-021). DDC recorders cap the record
/// stream with a null Type Word — all sixteen bits zero. Because the
/// Word Count field (bits 8–13) is zero, `0x0000` can never be a valid
/// record (the minimum is 4 words); the value is therefore unambiguous
/// as a stream terminator rather than a record. A file whose record
/// stream is empty consists solely of this word (an empty recording,
/// e.g. an unused MIL-STD-1553 channel that captured no traffic).
pub const TERMINATOR_TYPE_WORD: u16 = 0x0000;

/// True if `raw` is the end-of-records terminator (a null Type Word).
/// See [`TERMINATOR_TYPE_WORD`] and L2-RDR-021 / L2-SYN-028.
#[inline]
#[must_use]
pub fn is_terminator_type_word(raw: u16) -> bool {
    raw == TERMINATOR_TYPE_WORD
}

// ── Primitive readers ─────────────────────────────────────────────────

/// Read a single little-endian `u16` at `offset`. Returns `None` if OOB.
#[inline]
#[must_use]
pub fn read_u16(data: &[u8], offset: usize) -> Option<u16> {
    let bytes = data.get(offset..offset + 2)?;
    Some(u16::from_le_bytes([bytes[0], bytes[1]]))
}

/// Read `count` little-endian `u16`s starting at `offset` into `out`.
/// Returns false if OOB; on success `out[..count]` is populated.
pub fn read_u16_array(data: &[u8], offset: usize, count: usize, out: &mut [u16]) -> bool {
    let needed = count * 2;
    let Some(slice) = data.get(offset..offset + needed) else {
        return false;
    };
    // `as_chunks` rather than `chunks_exact(2)`: the chunk type is `[u8; 2]`,
    // so its length is proved by the type instead of by indexing a slice, and
    // `from_le_bytes` takes it directly. `needed` is always even, so the
    // remainder is empty by construction.
    let (chunks, _remainder) = slice.as_chunks::<2>();
    for (i, chunk) in chunks.iter().enumerate() {
        out[i] = u16::from_le_bytes(*chunk);
    }
    true
}

// ── Field decoders ────────────────────────────────────────────────────

#[inline]
#[must_use]
pub fn decode_type_word(raw: u16) -> TypeWord {
    let message_type = (raw & 0x7F) as u8;
    let bus = if (raw >> 7) & 1 == 0 { Bus::A } else { Bus::B };
    let word_count = (raw >> 8) & 0x3F;
    let error = ((raw >> 14) & 1) != 0;
    TypeWord {
        message_type,
        bus,
        word_count,
        error,
        raw,
    }
}

#[must_use]
pub fn decode_irig_timestamp(upper: u16, middle: u16, lower: u16) -> IrigTimestamp {
    let freerun = ((upper >> 15) & 1) != 0;
    let day = (upper >> 5) & 0x01FF;
    let hour = (upper & 0x1F) as u8;
    let minute = ((middle >> 10) & 0x3F) as u8;
    let second = ((middle >> 4) & 0x3F) as u8;
    let us_hi = u32::from(middle & 0xF);
    let us_lo = u32::from(lower);
    let microsecond = (us_hi << 16) | us_lo;
    IrigTimestamp {
        day,
        hour,
        minute,
        second,
        microsecond,
        freerun,
    }
}

#[must_use]
pub fn decode_standard_timestamp(upper: u16, lower: u16) -> StandardTimestamp {
    let raw_value = (u32::from(upper) << 16) | u32::from(lower);
    StandardTimestamp {
        raw_value,
        upper_word: upper,
        lower_word: lower,
    }
}

#[must_use]
pub fn decode_command_word(raw: u16) -> CommandWord {
    let rt = ((raw >> 11) & 0x1F) as u8;
    let direction = if (raw >> 10) & 1 == 0 {
        Direction::Receive
    } else {
        Direction::Transmit
    };
    let subaddress = ((raw >> 5) & 0x1F) as u8;
    let mut data_word_count = (raw & 0x1F) as u8;
    if data_word_count == 0 {
        data_word_count = 32;
    }
    CommandWord {
        rt,
        direction,
        subaddress,
        data_word_count,
        raw,
    }
}

// ── MUX from file name (L2-WRT-020) ───────────────────────────────────

/// Default: MUX population is on (derive from the file name).
pub const DEFAULT_MUX_ENABLED: bool = true;
/// Default MUX field delimiter.
pub const DEFAULT_MUX_DELIMITER: &str = ".";
/// Default MUX field index (0-based): the 5th dot-separated field, matching the
/// `name.part.part.part.<MUX>.part.ext` recorder convention.
pub const DEFAULT_MUX_FIELD: i64 = 4;

/// Extract the MUX column value from a file *name* (basename, not a path):
/// split on `delimiter` and return the `field`-th part (0-based; a negative
/// `field` counts from the end, e.g. `-1` is the last part), trimmed of spaces
/// and tabs only -- the same trim as a `--manifest` line. A no-break space or
/// other Unicode whitespace at the field's edge is part of the name and kept.
///
/// Returns `None` (→ empty MUX) when the index is out of range, the selected
/// field is empty after trimming, or `delimiter` is empty. Pure and
/// allocation-light; the caller wraps the result in a shared `Arc<str>`.
#[must_use]
pub fn mux_from_filename(file_name: &str, delimiter: &str, field: i64) -> Option<String> {
    if delimiter.is_empty() {
        return None;
    }
    let parts: Vec<&str> = file_name.split(delimiter).collect();
    // A negative field counts back from the end. The index is resolved in i64
    // and converted once, by `try_from` -- so the "is it in range" question and
    // the "can it be an index" question are answered by the same expression
    // rather than by a bounds check the cast has to be trusted to respect.
    let len = i64::try_from(parts.len()).unwrap_or(i64::MAX);
    let idx = if field < 0 { len + field } else { field };
    let value =
        crate::text::trim_ascii_blank(usize::try_from(idx).ok().and_then(|i| parts.get(i))?);
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

// ── Format classification ─────────────────────────────────────────────

/// # Errors
///
/// Returns [`MieError::UnknownTypeWord`] for a type code outside the known set.
/// The caller supplies the offset, which this function does not know.
pub fn classify_message_format(
    message_type: u8,
    command_word: &CommandWord,
    word_count: u16,
    timestamp_words: u16,
) -> MieResult<MessageFormat> {
    use MessageType::{
        BcToRt, BroadcastBcToRt, BroadcastRtToRt, ModeCommand, RtToBc, RtToRt, SpuriousData,
    };
    match MessageType::from_code(message_type) {
        Some(BcToRt) => Ok(MessageFormat::Receive),
        Some(RtToBc) => Ok(MessageFormat::Transmit),
        Some(RtToRt) => Ok(MessageFormat::RtToRt),
        Some(BroadcastBcToRt) => Ok(MessageFormat::ReceiveBroadcast),
        Some(BroadcastRtToRt) => Ok(MessageFormat::RtToRtBroadcast),
        Some(ModeCommand) => Ok(classify_mode_code(
            *command_word,
            word_count,
            timestamp_words,
        )),
        Some(SpuriousData) => Ok(MessageFormat::SpuriousData),
        None => Err(MieError::UnknownTypeWord {
            offset: 0,
            raw_type_word: 0,
            message_type,
        }),
    }
}

/// Which structural invariant a record violated. Used by callers
/// (the reader) to phrase a precise diagnostic; the strict-mode
/// path otherwise maps every violation to a single `PayloadError`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WhichInvariant {
    /// L2-SYN-020: Type 0x02 (BC→RT) requires Cmd direction = Receive.
    DirectionBcToRt,
    /// L2-SYN-021: Type 0x04 (RT→BC) requires Cmd direction = Transmit.
    DirectionRtToBc,
    /// L2-SYN-022: Type Word `word_count` too small for declared payload.
    WordCountCapacity,
    /// L2-SYN-023: Cmd2 direction for RT-to-RT must be Receive.
    DirectionRtToRtCmd2,
    /// L2-SYN-024: Status Word RT field does not match Cmd RT.
    /// AnomalyWarn-class — real-bus noise possible.
    StatusRtMismatch,
    /// L2-SYN-025: Type Word bit 15 (reserved) is set.
    /// AnomalyWarn-class — possible vendor extension.
    TypeWordReservedBit,
    /// L2-SYN-027: RT-to-RT Cmd1 and Cmd2 disagree on `data_word_count`.
    DataWordCountMismatch,
}

/// Policy class for a structural invariant violation, per the locked
/// schema in `docs/L2-REQ.md` (L2-SYN-020 through L2-SYN-025 severity classes).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvariantSeverity {
    /// Strict mode aborts with a record-error class; lenient mode
    /// WARN+skips the record (advance offset without emission).
    Reject,
    /// Both modes log a WARN and continue emitting the record. Used
    /// where outright rejection would produce false negatives on
    /// real-bus noise or vendor extensions (L2-SYN-024, 006).
    AnomalyWarn,
}

#[derive(Debug)]
pub struct InvariantViolation {
    pub kind: WhichInvariant,
    pub severity: InvariantSeverity,
    pub detail: String,
}

/// Per-format minimum payload word count, computed from the primary
/// Command Word's declared `data_word_count`. Used by the L2-SYN-022
/// capacity check. `SpuriousData` has variable size so returns 0
/// (skip the check). For RT-to-RT formats the second Command Word is
/// not yet decoded at the time we call this; we use Cmd1's
/// `data_word_count` as an approximation (the bus protocol requires
/// Cmd1 and Cmd2 to agree on `data_word_count`).
fn min_payload_words(fmt: MessageFormat, cmd: CommandWord) -> u16 {
    let dwc = u16::from(cmd.data_word_count);
    match fmt {
        MessageFormat::Receive | MessageFormat::Transmit => dwc + 1, // data + status
        MessageFormat::RtToRt => dwc + 3, // cmd2 + tx_status + data + rx_status
        MessageFormat::ReceiveBroadcast => dwc, // data only (no status)
        MessageFormat::RtToRtBroadcast => dwc + 2, // cmd2 + tx_status + data
        MessageFormat::ModeCodeTxData => 2, // status + data
        MessageFormat::ModeCodeRxData => 2, // data + status
        MessageFormat::ModeCodeNoData => 1, // status only
        MessageFormat::ModeCodeBcastNoData => 0,
        MessageFormat::ModeCodeBcastData => 1, // data only (no status)
        MessageFormat::SpuriousData => 0,      // variable; no capacity check
    }
}

/// L2-SYN-020..025: structural invariants per the locked schema. Caller
/// has already framing-validated (sync.rs) and classified the format
/// (`classify_message_format`). Returns Err describing the first
/// invariant the record violates; Ok if all hold.
///
/// Reject-class invariants checkable before payload extraction:
/// - INV-001 (L2-SYN-020): Type 0x02 → Cmd direction = Receive
/// - INV-002 (L2-SYN-021): Type 0x04 → Cmd direction = Transmit
/// - INV-003 (L2-SYN-022): `TW.word_count` >= 1 + `ts_words` + 1 + `min_payload_words(format`, cmd)
///
/// The remaining structural invariants live in companion functions
/// because they need data not available here: RT-to-RT Cmd2 direction
/// (L2-SYN-023) in [`validate_post_extract_invariants`] (Cmd2 sits inside
/// the payload), and the AnomalyWarn-class Status-RT-vs-Cmd-RT
/// (L2-SYN-024) and Type-Word reserved-bit (L2-SYN-025) checks in
/// [`detect_record_anomalies`].
///
/// # Errors
///
/// Returns [`InvariantViolation`] naming which invariant failed: per-type
/// Command Word direction (L2-SYN-020 / L2-SYN-021), or a declared data-word
/// count the record's own extent cannot hold (L2-SYN-022).
pub fn validate_structural_invariants(
    tw: &TypeWord,
    cmd: &CommandWord,
    msg_fmt: MessageFormat,
    ts_words: u16,
) -> Result<(), InvariantViolation> {
    // L2-SYN-020 / L2-SYN-021: per-type direction.
    if tw.message_type == MessageType::BcToRt as u8 && cmd.direction != Direction::Receive {
        return Err(InvariantViolation {
            kind: WhichInvariant::DirectionBcToRt,
            severity: InvariantSeverity::Reject,
            detail: format!(
                "Type 0x02 (BC->RT) requires Cmd direction = Receive; got Transmit \
                 (raw Cmd = 0x{:04X})",
                cmd.raw
            ),
        });
    }
    if tw.message_type == MessageType::RtToBc as u8 && cmd.direction != Direction::Transmit {
        return Err(InvariantViolation {
            kind: WhichInvariant::DirectionRtToBc,
            severity: InvariantSeverity::Reject,
            detail: format!(
                "Type 0x04 (RT->BC) requires Cmd direction = Transmit; got Receive \
                 (raw Cmd = 0x{:04X})",
                cmd.raw
            ),
        });
    }

    // L2-SYN-022: word-count capacity check.
    let min_wc = 1 + ts_words + 1 + min_payload_words(msg_fmt, *cmd);
    if tw.word_count < min_wc {
        return Err(InvariantViolation {
            kind: WhichInvariant::WordCountCapacity,
            severity: InvariantSeverity::Reject,
            detail: format!(
                "TW.word_count = {} is too small for declared payload \
                 (need at least {} for {:?} with data_word_count = {})",
                tw.word_count, min_wc, msg_fmt, cmd.data_word_count
            ),
        });
    }

    Ok(())
}

/// L2-SYN-023 / L2-SYN-027: post-extract checks for RT-to-RT formats.
///
/// Called post-extract because Cmd2 lives inside the payload and is
/// only available after `extract_payload`. For non-RT-to-RT formats
/// (or when cmd2 is None) this is a no-op.
///
/// - L2-SYN-023: Cmd2 direction must be Receive.
/// - L2-SYN-027: Cmd1 and Cmd2 must agree on `data_word_count`. The bus
///   carries one data-word count for the transaction; the capacity
///   invariant (L2-SYN-022) only sees Cmd1, so a Cmd2 that disagrees —
///   including the over-claim the record-bounded reads of L2-DEC-009
///   defend against — is caught here.
///
/// # Errors
///
/// Returns [`InvariantViolation`] when an RT-to-RT record's Cmd2 has the wrong
/// direction (L2-SYN-023), or when Cmd1 and Cmd2 disagree on `data_word_count`
/// (L2-SYN-027). Both need the payload, which is why they are not in
/// [`validate_structural_invariants`].
pub fn validate_post_extract_invariants(
    msg_fmt: MessageFormat,
    cmd: &CommandWord,
    cmd2: Option<&CommandWord>,
) -> Result<(), InvariantViolation> {
    let is_rt_to_rt = matches!(
        msg_fmt,
        MessageFormat::RtToRt | MessageFormat::RtToRtBroadcast
    );
    if !is_rt_to_rt {
        return Ok(());
    }
    let Some(c2) = cmd2 else {
        return Ok(());
    };
    if c2.direction != Direction::Receive {
        return Err(InvariantViolation {
            kind: WhichInvariant::DirectionRtToRtCmd2,
            severity: InvariantSeverity::Reject,
            detail: format!(
                "RT-to-RT Cmd2 requires direction = Receive; got Transmit \
                 (raw Cmd2 = 0x{:04X})",
                c2.raw
            ),
        });
    }
    if cmd.data_word_count != c2.data_word_count {
        return Err(InvariantViolation {
            kind: WhichInvariant::DataWordCountMismatch,
            severity: InvariantSeverity::Reject,
            detail: format!(
                "RT-to-RT Cmd1/Cmd2 data_word_count mismatch: Cmd1 = {}, Cmd2 = {} \
                 (raw Cmd1 = 0x{:04X}, Cmd2 = 0x{:04X})",
                cmd.data_word_count, c2.data_word_count, cmd.raw, c2.raw
            ),
        });
    }
    Ok(())
}

/// L2-SYN-024 / L2-SYN-025: AnomalyWarn-class observations.
///
/// Both invariants are anomaly detectors rather than corruption
/// rejections; the reader logs each violation as a WARN and continues
/// emitting the record. Returns a Vec because multiple anomalies can
/// fire on a single record (e.g., Status RT mismatch AND reserved
/// bit set simultaneously).
#[must_use]
pub fn detect_record_anomalies(
    tw: &TypeWord,
    cmd: &CommandWord,
    status_word: Option<u16>,
) -> Vec<InvariantViolation> {
    let mut out = Vec::new();

    // L2-SYN-024: Status RT vs Cmd RT.
    if let Some(status_raw) = status_word {
        let status_rt = ((status_raw >> 11) & 0x1F) as u8;
        if status_rt != cmd.rt {
            out.push(InvariantViolation {
                kind: WhichInvariant::StatusRtMismatch,
                severity: InvariantSeverity::AnomalyWarn,
                detail: format!(
                    "Status RT = {status_rt} does not match Cmd RT = {} \
                     (raw Status = 0x{status_raw:04X}); possible bus interference",
                    cmd.rt
                ),
            });
        }
    }

    // L2-SYN-025: Type Word bit 15 reserved.
    if (tw.raw >> 15) & 1 != 0 {
        out.push(InvariantViolation {
            kind: WhichInvariant::TypeWordReservedBit,
            severity: InvariantSeverity::AnomalyWarn,
            detail: format!(
                "Type Word bit 15 (reserved) is set in raw 0x{:04X}; \
                 possible undocumented vendor extension",
                tw.raw
            ),
        });
    }

    out
}

fn classify_mode_code(cmd: CommandWord, word_count: u16, timestamp_words: u16) -> MessageFormat {
    // The data-vs-no-data thresholds are relative to the record's timestamp
    // word count (IRIG = 3, Standard = 2), not absolute — a Standard record is
    // one word shorter than the IRIG equivalent (L2-MSG-004). The fixed
    // overhead per shape: Type(1) + timestamp_words + ModeCmd(1) [+ Status(1)
    // for non-broadcast] [+ Data(1) when present].
    let is_broadcast = cmd.rt == 31;
    if is_broadcast {
        // Broadcast mode codes have no status word.
        //   With data:    Type + TS + ModeCmd + Data = timestamp_words + 3
        //   Without data: Type + TS + ModeCmd        = timestamp_words + 2
        return if word_count >= timestamp_words + 3 {
            MessageFormat::ModeCodeBcastData
        } else {
            MessageFormat::ModeCodeBcastNoData
        };
    }

    // Non-broadcast mode codes always carry a Status Word; a data word is
    // present only on the longer records (1553 mode codes 0–15 carry none,
    // 16–31 carry one — L2-MSG-004). A mode code WITHOUT a data word — transmit
    // or receive — is Type + TS + ModeCmd + Status = timestamp_words + 3 and
    // classifies as `ModeCodeNoData` (its wire shape is ModeCmd + Status either
    // way; the `CMD` column preserves the direction). With a data word it is
    // `ModeCodeTxData` (Status + Data) or `ModeCodeRxData` (Data + Status).
    //   With data: Type + TS + ModeCmd + Data + Status = timestamp_words + 4
    //   No data:   Type + TS + ModeCmd        + Status = timestamp_words + 3
    if word_count >= timestamp_words + 4 {
        if cmd.direction == Direction::Transmit {
            MessageFormat::ModeCodeTxData
        } else {
            MessageFormat::ModeCodeRxData
        }
    } else {
        MessageFormat::ModeCodeNoData
    }
}

// ── Timestamp format auto-detection ───────────────────────────────────

/// L2-DEC-016 classification of an auto-detection outcome's strength.
///
/// `Decisive` and `Marginal` both result in the chosen format being
/// used silently or with a single INFO log line. `Ambiguous` is the
/// L2-DEC-016 mismatch class: strict mode surfaces it as
/// `MieError::TimestampFormatMismatch`; lenient mode logs WARN and
/// uses the chosen format anyway.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetectionConfidence {
    /// Comfortable winner: high absolute score AND wide margin.
    Decisive,
    /// Passed the L2-DEC-016 floor but did not reach decisive
    /// thresholds. Reasonable confidence; logged at INFO for
    /// operator visibility.
    Marginal,
    /// Both candidates scored too low or too close. L2-DEC-016
    /// mismatch class.
    Ambiguous,
}

/// Result of an L2-DEC-015 multi-record probe.
#[derive(Debug, Clone)]
pub struct DetectionOutcome {
    /// Chosen format. IRIG wins ties per L2-DEC-012.
    pub format: TimestampFormat,
    /// Aggregated IRIG score across the probe set.
    pub irig_score: i32,
    /// Aggregated Standard score across the probe set.
    pub std_score: i32,
    /// Number of records actually probed. Always ≥ 1 on a successful
    /// probe; may be less than `max_records` if EOF was reached or a
    /// record's declared length was structurally impossible.
    pub records_probed: usize,
    /// L2-DEC-016 confidence classification.
    pub confidence: DetectionConfidence,
}

// L2-DEC-016 thresholds. Conservative — they fire only when the
// probe genuinely could not distinguish, not when the call is decisive
// but the absolute score is low because of a small probe set. The
// floor of 4 means even a single decisive record passes (one perfect
// IRIG record scores 5; one perfect Standard record scores 4).
const CONFIDENCE_FLOOR: i32 = 4;
const MIN_MARGIN: i32 = 3;
// Decisive thresholds: comfortably above the floor AND a wide margin.
// Two records that both score perfectly for one format easily clear this.
const DECISIVE_FLOOR: i32 = 8;
const DECISIVE_MARGIN: i32 = 6;

/// Default L2-DEC-015 probe size. Configurable via the
/// `decode.detect_records` TOML key or the `--detect-records` CLI flag.
pub const DEFAULT_DETECT_RECORDS: usize = 8;

/// L2-DEC-015 multi-record probe. Walks up to `max_records` starting
/// from `first_offset`, aggregating per-record IRIG vs Standard
/// scoring, and returns the chosen format with a confidence
/// classification per L2-DEC-016.
///
/// `max_records` is clamped to at least 1 (a no-probe call is
/// nonsensical). The probe is bounded by file length: when EOF is
/// reached before `max_records` records have been scored the function
/// returns with however many records it managed to score.
///
/// IRIG wins ties per L2-DEC-012.
#[must_use]
pub fn probe_timestamp_format(
    data: &[u8],
    first_offset: usize,
    max_records: usize,
) -> DetectionOutcome {
    let (irig_score, std_score, records_probed) =
        accumulate_probe_scores(data, first_offset, max_records);

    let format = if irig_score >= std_score {
        TimestampFormat::Irig
    } else {
        TimestampFormat::Standard
    };
    let max_score = irig_score.max(std_score);
    let margin = (irig_score - std_score).abs();

    DetectionOutcome {
        format,
        irig_score,
        std_score,
        records_probed,
        confidence: classify_confidence(max_score, margin),
    }
}

/// Walk up to `max_records` records from `first_offset`, accumulating the IRIG
/// and Standard scores and counting how many records were probed. Advances by
/// each record's declared length — the same walk the reader performs — and
/// stops at EOF or a structurally-impossible record.
fn accumulate_probe_scores(
    data: &[u8],
    first_offset: usize,
    max_records: usize,
) -> (i32, i32, usize) {
    let n = max_records.max(1);
    let file_len = data.len();
    let mut irig_score: i32 = 0;
    let mut std_score: i32 = 0;
    let mut records_probed: usize = 0;
    let mut offset = first_offset;

    for _ in 0..n {
        // Need at least the Type Word + minimum payload to score.
        if offset + MIN_RECORD_BYTES_STANDARD > file_len {
            break;
        }
        let Some(tw_raw) = read_u16(data, offset) else {
            break;
        };
        let tw = decode_type_word(tw_raw);
        // Defensively skip structurally-impossible records — these would also
        // fail the reader's validate_record path, and would skew the score.
        if tw.word_count < MIN_RECORD_WORDS_STANDARD {
            break;
        }

        let (i_delta, s_delta) = score_single_record(data, offset, tw);
        irig_score += i_delta;
        std_score += s_delta;
        records_probed += 1;

        let record_bytes = usize::from(tw.word_count) * 2;
        if record_bytes == 0 {
            break;
        }
        let Some(next_offset) = offset.checked_add(record_bytes) else {
            break;
        };
        if next_offset <= offset || next_offset > file_len {
            break;
        }
        offset = next_offset;
    }

    (irig_score, std_score, records_probed)
}

/// L2-DEC-016 confidence classification from the aggregate score and margin.
fn classify_confidence(max_score: i32, margin: i32) -> DetectionConfidence {
    if max_score < CONFIDENCE_FLOOR || margin < MIN_MARGIN {
        DetectionConfidence::Ambiguous
    } else if max_score >= DECISIVE_FLOOR && margin >= DECISIVE_MARGIN {
        DetectionConfidence::Decisive
    } else {
        DetectionConfidence::Marginal
    }
}

/// Per-record scoring extracted from the previous single-record
/// detector. Returns `(irig_delta, std_delta)` — the score contribution
/// from this record toward each candidate format.
///
/// IRIG can score up to `+5` per record (T/R: `2` + WC plausibility:
/// `2` + range validity: `1`). Standard can score up to `+4` per
/// record (T/R: `2` + WC plausibility: `2`; no range-validity bonus
/// because the Standard timestamp is a raw 32-bit counter with no
/// semantic field bounds to check against).
fn score_single_record(data: &[u8], offset: usize, type_word: TypeWord) -> (i32, i32) {
    (
        score_irig_candidate(data, offset, type_word),
        score_standard_candidate(data, offset, type_word),
    )
}

/// T/R consistency: a `BC_TO_RT` type expects a Receive command, `RT_TO_BC`
/// expects Transmit. Other message types never match.
fn tr_direction_matches(type_word: TypeWord, cmd: CommandWord) -> bool {
    (type_word.message_type == MessageType::BcToRt as u8 && cmd.direction == Direction::Receive)
        || (type_word.message_type == MessageType::RtToBc as u8
            && cmd.direction == Direction::Transmit)
}

/// IRIG candidate: Cmd at offset+8 (Type + 3 TS words). Up to `+5`
/// (T/R: 2, word-count plausibility: 2, field-range validity: 1).
fn score_irig_candidate(data: &[u8], offset: usize, type_word: TypeWord) -> i32 {
    let Some(cmd_raw) = read_u16(data, offset + 8) else {
        return 0;
    };
    let cmd = decode_command_word(cmd_raw);
    let mut score = 0;
    if tr_direction_matches(type_word, cmd) {
        score += 2;
    }
    // IRIG overhead = TS(3) + Cmd(1) + Stat(1) + Type(1) = 6
    if i32::from(type_word.word_count) - 6 == i32::from(cmd.data_word_count) {
        score += 2;
    }
    if let (Some(ts_upper), Some(ts_middle)) =
        (read_u16(data, offset + 2), read_u16(data, offset + 4))
    {
        // Only the fields with a semantic range are worth scoring. The
        // microsecond high nibble (`ts_middle & 0xF`) is *always* < 16 by
        // construction, so testing it added nothing to the score.
        let hour = ts_upper & 0x1F;
        let minute = (ts_middle >> 10) & 0x3F;
        let second = (ts_middle >> 4) & 0x3F;
        if hour < 24 && minute < 60 && second < 60 {
            score += 1;
        }
    }
    score
}

/// Standard candidate: Cmd at offset+6 (Type + 2 TS words). Up to `+4`
/// (T/R: 2, word-count plausibility: 2; no range bonus — the 32-bit counter
/// has no semantic field bounds to check).
fn score_standard_candidate(data: &[u8], offset: usize, type_word: TypeWord) -> i32 {
    let Some(cmd_raw) = read_u16(data, offset + 6) else {
        return 0;
    };
    let cmd = decode_command_word(cmd_raw);
    let mut score = 0;
    if tr_direction_matches(type_word, cmd) {
        score += 2;
    }
    // Standard overhead = TS(2) + Cmd(1) + Stat(1) + Type(1) = 5
    if i32::from(type_word.word_count) - 5 == i32::from(cmd.data_word_count) {
        score += 2;
    }
    score
}

#[inline]
#[must_use]
pub fn message_type_is_valid(code: u8) -> bool {
    is_valid_message_type(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Requirements: L2-WRT-020
    #[test]
    fn mux_from_filename_extraction() {
        let name = "full_loadout.draw.data.1553.aa.unused.mie_irig";
        // Default field 4 → the recorder identity.
        assert_eq!(mux_from_filename(name, ".", 4).as_deref(), Some("aa"));
        // Other operator files at the same index.
        assert_eq!(
            mux_from_filename("full_loadout.draw.data.1553.bb.unused.mie_irig", ".", 4).as_deref(),
            Some("bb")
        );
        // Negative index counts from the end (-3 == index 4 here).
        assert_eq!(mux_from_filename(name, ".", -3).as_deref(), Some("aa"));
        // Index 0 and the last field.
        assert_eq!(
            mux_from_filename(name, ".", 0).as_deref(),
            Some("full_loadout")
        );
        assert_eq!(
            mux_from_filename(name, ".", -1).as_deref(),
            Some("mie_irig")
        );
        // Out-of-range → None (empty MUX).
        assert_eq!(mux_from_filename(name, ".", 99), None);
        assert_eq!(mux_from_filename(name, ".", -99), None);
        // A different delimiter.
        assert_eq!(mux_from_filename("a_b_c", "_", 1).as_deref(), Some("b"));
        // Empty delimiter / empty field → None.
        assert_eq!(mux_from_filename(name, "", 4), None);
        assert_eq!(mux_from_filename("a..b", ".", 1), None);
        // No delimiter present and field != 0 → None.
        assert_eq!(mux_from_filename("plain", ".", 4), None);
        assert_eq!(mux_from_filename("plain", ".", 0).as_deref(), Some("plain"));
    }

    /// Requirements: L2-WRT-020, L2-MRG-001
    #[test]
    fn mux_trims_spaces_and_tabs_only() {
        // `str::trim` removed Unicode whitespace here while C++ kept it, so a
        // no-break space at the field's edge gave two different MUX values.
        assert_eq!(
            mux_from_filename("a. \tB7\t .c", ".", 1).as_deref(),
            Some("B7")
        );
        assert_eq!(mux_from_filename("a. \t .c", ".", 1), None);
        // Built at run time: a non-ASCII source literal would trip the
        // shipped-literal ASCII gate (L2-CLI-014).
        let nbsp = char::from_u32(0xA0).unwrap();
        let ideographic = char::from_u32(0x3000).unwrap();
        let padded = format!("{nbsp}B7{ideographic}");
        assert_eq!(
            mux_from_filename(&format!("a.{padded}.c"), ".", 1),
            Some(padded)
        );
        // A field of only Unicode spaces is a value, not an empty field.
        assert_eq!(
            mux_from_filename(&format!("a.{nbsp}.c"), ".", 1),
            Some(nbsp.to_string())
        );
        assert_eq!(
            mux_from_filename("a.\rB7\x0b.c", ".", 1).as_deref(),
            Some("\rB7\x0b")
        );
    }

    /// Requirements: L2-DEC-001
    #[test]
    fn type_word_layout() {
        // 0x2402: word_count=36 in upper byte (0x24), type=0x02 in lower
        let tw = decode_type_word(0x2402);
        assert_eq!(tw.message_type, 2);
        assert_eq!(tw.bus, Bus::A);
        assert_eq!(tw.word_count, 36);
        assert!(!tw.error);
        assert_eq!(tw.raw, 0x2402);
    }

    /// Requirements: L2-DEC-001, L2-ERR-001
    #[test]
    fn type_word_bus_b_and_error_bit() {
        // bit 7 set → Bus B, bit 14 set → error
        let raw = 0b0100_0000_1000_0000 | 0x02; // type 0x02
        let tw = decode_type_word(raw);
        assert_eq!(tw.bus, Bus::B);
        assert!(tw.error);
    }

    /// Requirements: L2-DEC-002
    #[test]
    fn irig_timestamp_known_value() {
        // From Python doctest fixture
        let ts = decode_irig_timestamp(0x180F, 0xDB26, 0xF621);
        assert_eq!(ts.hour, 15);
        assert_eq!(ts.minute, 54);
        assert_eq!(ts.second, 50);
        assert_eq!(ts.microsecond, 456_225);
        assert!(!ts.freerun);
    }

    /// Requirements: L2-DEC-003
    #[test]
    fn irig_freerun_bit() {
        let ts = decode_irig_timestamp(0x8000, 0, 0);
        assert!(ts.freerun);
    }

    /// Requirements: L2-DEC-007
    #[test]
    fn standard_timestamp_round_trip() {
        let ts = decode_standard_timestamp(0x0001, 0x86A0);
        assert_eq!(ts.raw_value, 100_000);
        assert_eq!(ts.upper_word, 0x0001);
        assert_eq!(ts.lower_word, 0x86A0);
    }

    /// Requirements: L2-DEC-004
    #[test]
    fn command_word_known_value() {
        // 0x797E → RT 15, Receive, SA 11, 30 data words
        let cw = decode_command_word(0x797E);
        assert_eq!(cw.rt, 15);
        assert_eq!(cw.direction, Direction::Receive);
        assert_eq!(cw.subaddress, 11);
        assert_eq!(cw.data_word_count, 30);
    }

    /// Requirements: L2-DEC-004
    #[test]
    fn command_word_zero_means_thirty_two() {
        // raw bits 0..4 = 0 → 32 data words
        let cw = decode_command_word(0b0000_1000_0010_0000); // arbitrary upper bits
        assert_eq!(cw.data_word_count, 32);
    }

    /// Requirements: L2-DEC-008
    #[test]
    fn read_u16_le() {
        assert_eq!(read_u16(&[0x34, 0x12], 0), Some(0x1234));
        assert_eq!(read_u16(&[0x34], 0), None);
    }

    /// Requirements: L2-DEC-008
    #[test]
    fn read_u16_array_into_slice() {
        let bytes = [0x01, 0x00, 0x02, 0x00, 0x03, 0x00];
        let mut out = [0u16; 3];
        assert!(read_u16_array(&bytes, 0, 3, &mut out));
        assert_eq!(out, [1, 2, 3]);
    }

    /// Requirements: L2-MSG-001
    #[test]
    fn classify_simple_types() {
        // Non-mode-code formats are direct type mappings; the timestamp word
        // count is irrelevant to them (pass 3 = IRIG).
        let cmd = decode_command_word(0x797E);
        assert_eq!(
            classify_message_format(0x02, &cmd, 36, 3).unwrap(),
            MessageFormat::Receive
        );
        assert_eq!(
            classify_message_format(0x04, &cmd, 10, 3).unwrap(),
            MessageFormat::Transmit
        );
        assert_eq!(
            classify_message_format(0x08, &cmd, 12, 3).unwrap(),
            MessageFormat::RtToRt
        );
        assert_eq!(
            classify_message_format(0x20, &cmd, 8, 3).unwrap(),
            MessageFormat::SpuriousData
        );
    }

    /// Broadcast mode-code data/no-data boundary is relative to the timestamp
    /// word count: data iff `word_count` >= `timestamp_words` + 3 (L2-MSG-004). For
    /// IRIG (3) that is WC >= 6; for Standard (2) that is WC >= 5.
    /// Requirements: L2-MSG-001, L2-MSG-004
    #[test]
    fn classify_mode_code_broadcast_data_vs_no_data() {
        let cmd = CommandWord {
            rt: 31,
            direction: Direction::Receive,
            subaddress: 0,
            data_word_count: 0,
            raw: 0,
        };
        // IRIG (3 timestamp words): boundary at WC 5/6.
        assert_eq!(
            classify_message_format(0x01, &cmd, 5, 3).unwrap(),
            MessageFormat::ModeCodeBcastNoData
        );
        assert_eq!(
            classify_message_format(0x01, &cmd, 6, 3).unwrap(),
            MessageFormat::ModeCodeBcastData
        );
        // Standard (2 timestamp words): boundary shifts down to WC 4/5.
        assert_eq!(
            classify_message_format(0x01, &cmd, 4, 2).unwrap(),
            MessageFormat::ModeCodeBcastNoData
        );
        assert_eq!(
            classify_message_format(0x01, &cmd, 5, 2).unwrap(),
            MessageFormat::ModeCodeBcastData
        );
    }

    /// A transmit mode code is `ModeCodeTxData` only when the record is long
    /// enough to carry a data word (`word_count` >= `timestamp_words` + 4); without
    /// one it is `ModeCodeNoData` — the wire shape is `ModeCmd` + Status either
    /// way, and the `CMD` column preserves the direction. Regression: a no-data
    /// transmit mode code (1553 mode codes 0–15 — "transmit status word", etc.)
    /// was previously forced to `ModeCodeTxData`, failed the L2-SYN-022 capacity
    /// check, and was silently dropped from the CSV in lenient mode.
    /// Requirements: L2-MSG-001, L2-MSG-004
    #[test]
    fn classify_mode_code_tx_data_vs_no_data() {
        let cmd = CommandWord {
            rt: 5,
            direction: Direction::Transmit,
            subaddress: 0,
            data_word_count: 1,
            raw: 0,
        };
        // IRIG (3 timestamp words): boundary at WC 6/7.
        assert_eq!(
            classify_message_format(0x01, &cmd, 7, 3).unwrap(),
            MessageFormat::ModeCodeTxData
        );
        assert_eq!(
            classify_message_format(0x01, &cmd, 6, 3).unwrap(),
            MessageFormat::ModeCodeNoData,
            "a transmit mode code with no data word must be NoData, not dropped"
        );
        // Standard (2 timestamp words): boundary shifts down to WC 5/6.
        assert_eq!(
            classify_message_format(0x01, &cmd, 6, 2).unwrap(),
            MessageFormat::ModeCodeTxData
        );
        assert_eq!(
            classify_message_format(0x01, &cmd, 5, 2).unwrap(),
            MessageFormat::ModeCodeNoData
        );
    }

    /// Non-broadcast receive mode-code data/no-data boundary is relative to the
    /// timestamp word count: data iff `word_count` >= `timestamp_words` + 4
    /// (L2-MSG-004). For IRIG (3) that is WC >= 7; for Standard (2) WC >= 6.
    /// Requirements: L2-MSG-001, L2-MSG-004
    #[test]
    fn classify_mode_code_rx_vs_no_data() {
        let cmd = CommandWord {
            rt: 5,
            direction: Direction::Receive,
            subaddress: 0,
            data_word_count: 1,
            raw: 0,
        };
        // IRIG (3 timestamp words): boundary at WC 6/7.
        assert_eq!(
            classify_message_format(0x01, &cmd, 7, 3).unwrap(),
            MessageFormat::ModeCodeRxData
        );
        assert_eq!(
            classify_message_format(0x01, &cmd, 6, 3).unwrap(),
            MessageFormat::ModeCodeNoData
        );
        // Standard (2 timestamp words): boundary shifts down to WC 5/6 — the
        // previously-misclassified case (WC=6 Standard was treated as no-data).
        assert_eq!(
            classify_message_format(0x01, &cmd, 6, 2).unwrap(),
            MessageFormat::ModeCodeRxData
        );
        assert_eq!(
            classify_message_format(0x01, &cmd, 5, 2).unwrap(),
            MessageFormat::ModeCodeNoData
        );
    }

    /// Requirements: L2-SYN-001
    #[test]
    fn classify_unknown_type() {
        let cmd = decode_command_word(0);
        assert!(classify_message_format(0x03, &cmd, 5, 3).is_err());
    }

    // ── L2-SYN-020..025 structural invariants (Phase 7a) ──────────────────

    fn tw(message_type: u8, word_count: u16) -> TypeWord {
        TypeWord {
            message_type,
            bus: Bus::A,
            word_count,
            error: false,
            raw: 0,
        }
    }

    fn cmd_with(direction: Direction, dwc: u8) -> CommandWord {
        CommandWord {
            rt: 15,
            direction,
            subaddress: 11,
            data_word_count: dwc,
            raw: 0,
        }
    }

    /// Requirements: L2-SYN-020
    #[test]
    fn invariants_pass_for_canonical_bc_to_rt() {
        let t = tw(0x02, 36); // 1 + 3 + 1 + 30 + 1 = 36
        let c = cmd_with(Direction::Receive, 30);
        validate_structural_invariants(&t, &c, MessageFormat::Receive, 3).unwrap();
    }

    /// Requirements: L2-SYN-021
    #[test]
    fn invariants_pass_for_canonical_rt_to_bc() {
        let t = tw(0x04, 36);
        let c = cmd_with(Direction::Transmit, 30);
        validate_structural_invariants(&t, &c, MessageFormat::Transmit, 3).unwrap();
    }

    /// Requirements: L2-SYN-020
    #[test]
    fn invariants_reject_bc_to_rt_with_transmit_cmd() {
        let t = tw(0x02, 36);
        let c = cmd_with(Direction::Transmit, 30); // wrong
        let err = validate_structural_invariants(&t, &c, MessageFormat::Receive, 3).unwrap_err();
        assert_eq!(err.kind, WhichInvariant::DirectionBcToRt);
    }

    /// Requirements: L2-SYN-021
    #[test]
    fn invariants_reject_rt_to_bc_with_receive_cmd() {
        let t = tw(0x04, 36);
        let c = cmd_with(Direction::Receive, 30); // wrong
        let err = validate_structural_invariants(&t, &c, MessageFormat::Transmit, 3).unwrap_err();
        assert_eq!(err.kind, WhichInvariant::DirectionRtToBc);
    }

    /// Requirements: L2-SYN-022
    #[test]
    fn invariants_reject_capacity_short() {
        // wc=5 too small for Receive with dwc=30 (needs 1+3+1+31=36).
        let t = tw(0x02, 5);
        let c = cmd_with(Direction::Receive, 30);
        let err = validate_structural_invariants(&t, &c, MessageFormat::Receive, 3).unwrap_err();
        assert_eq!(err.kind, WhichInvariant::WordCountCapacity);
    }

    /// Requirements: L2-SYN-022
    #[test]
    fn invariants_accept_capacity_exact() {
        // wc=36 is exactly the minimum for Receive with dwc=30.
        let t = tw(0x02, 36);
        let c = cmd_with(Direction::Receive, 30);
        validate_structural_invariants(&t, &c, MessageFormat::Receive, 3).unwrap();
    }

    /// Requirements: L2-SYN-022
    #[test]
    fn invariants_skip_capacity_for_spurious() {
        // SpuriousData has variable payload so the capacity check is
        // intentionally skipped (min_payload_words returns 0).
        let t = tw(0x20, 5); // wc=5 — only TW + 3 TS + 1 extra
        let c = cmd_with(Direction::Receive, 0);
        validate_structural_invariants(&t, &c, MessageFormat::SpuriousData, 3).unwrap();
    }

    /// Requirements: L2-SYN-020
    #[test]
    fn invariants_mode_code_not_constrained_by_direction() {
        // Mode codes (type 0x01) can be Transmit OR Receive. The
        // direction invariants only apply to 0x02 and 0x04.
        let t = tw(0x01, 7);
        let c_tx = cmd_with(Direction::Transmit, 1);
        validate_structural_invariants(&t, &c_tx, MessageFormat::ModeCodeTxData, 3).unwrap();
        let c_rx = cmd_with(Direction::Receive, 1);
        validate_structural_invariants(&t, &c_rx, MessageFormat::ModeCodeRxData, 3).unwrap();
    }

    // ── L2-SYN-023 (post-extract Cmd2 direction) ─────────────────

    /// Requirements: L2-SYN-023
    #[test]
    fn post_extract_invariant_rt_to_rt_cmd2_receive_passes() {
        // Cmd1 and Cmd2 agree on data_word_count (L2-SYN-027) and Cmd2 is
        // Receive (L2-SYN-023) → no violation.
        let c1 = cmd_with(Direction::Transmit, 3);
        let c2 = CommandWord {
            rt: 5,
            direction: Direction::Receive,
            subaddress: 10,
            data_word_count: 3,
            raw: 0,
        };
        validate_post_extract_invariants(MessageFormat::RtToRt, &c1, Some(&c2)).unwrap();
    }

    /// Requirements: L2-SYN-023
    #[test]
    fn post_extract_invariant_rt_to_rt_cmd2_transmit_rejected() {
        let c1 = cmd_with(Direction::Transmit, 3);
        let c2 = CommandWord {
            rt: 5,
            direction: Direction::Transmit, // WRONG: should be Receive
            subaddress: 10,
            data_word_count: 3,
            raw: 0xABCD,
        };
        let err =
            validate_post_extract_invariants(MessageFormat::RtToRt, &c1, Some(&c2)).unwrap_err();
        assert_eq!(err.kind, WhichInvariant::DirectionRtToRtCmd2);
        assert_eq!(err.severity, InvariantSeverity::Reject);
    }

    /// Requirements: L2-SYN-023
    #[test]
    fn post_extract_invariant_rt_to_rt_broadcast_also_checked() {
        let c1 = cmd_with(Direction::Transmit, 3);
        let c2 = CommandWord {
            rt: 5,
            direction: Direction::Transmit,
            subaddress: 10,
            data_word_count: 3,
            raw: 0,
        };
        let err = validate_post_extract_invariants(MessageFormat::RtToRtBroadcast, &c1, Some(&c2))
            .unwrap_err();
        assert_eq!(err.kind, WhichInvariant::DirectionRtToRtCmd2);
    }

    /// Requirements: L2-SYN-023
    #[test]
    fn post_extract_invariant_non_rt_to_rt_is_noop() {
        let c1 = cmd_with(Direction::Transmit, 3);
        // No cmd2 for non-RT-to-RT formats; function returns Ok.
        validate_post_extract_invariants(MessageFormat::Receive, &c1, None).unwrap();
        // Even if a stray Cmd2 is passed in (shouldn't happen), other
        // formats don't enforce the post-extract invariants.
        let c2 = CommandWord {
            rt: 5,
            direction: Direction::Transmit,
            subaddress: 10,
            data_word_count: 3,
            raw: 0,
        };
        validate_post_extract_invariants(MessageFormat::Receive, &c1, Some(&c2)).unwrap();
    }

    // ── L2-SYN-027 (post-extract Cmd1/Cmd2 data_word_count agreement) ─

    /// Requirements: L2-SYN-027
    #[test]
    fn post_extract_invariant_rt_to_rt_cmd_word_count_mismatch_rejected() {
        // Cmd2 direction is valid (Receive) so L2-SYN-023 passes, but Cmd1
        // and Cmd2 disagree on data_word_count → L2-SYN-027 rejects.
        let c1 = cmd_with(Direction::Transmit, 3);
        let c2 = CommandWord {
            rt: 5,
            direction: Direction::Receive,
            subaddress: 10,
            data_word_count: 5, // disagrees with Cmd1's 3
            raw: 0x1234,
        };
        for fmt in [MessageFormat::RtToRt, MessageFormat::RtToRtBroadcast] {
            let err = validate_post_extract_invariants(fmt, &c1, Some(&c2)).unwrap_err();
            assert_eq!(err.kind, WhichInvariant::DataWordCountMismatch);
            assert_eq!(err.severity, InvariantSeverity::Reject);
        }
    }

    // ── L2-SYN-024 / L2-SYN-025 (anomaly detectors) ─────────────────

    /// Requirements: L2-SYN-024
    #[test]
    fn anomaly_status_rt_match_no_violation() {
        // RT=15 in Cmd; status's bits 15-11 also = 15 (raw 0x7800).
        let t = tw(0x02, 36);
        let c = cmd_with(Direction::Receive, 30); // rt=15
        let anomalies = detect_record_anomalies(&t, &c, Some(0x7800));
        assert!(anomalies.is_empty());
    }

    /// Requirements: L2-SYN-024
    #[test]
    fn anomaly_status_rt_mismatch_logged() {
        // Cmd RT=15 but Status raw 0x2800 → status RT = 5.
        let t = tw(0x02, 36);
        let c = cmd_with(Direction::Receive, 30); // rt=15
        let anomalies = detect_record_anomalies(&t, &c, Some(0x2800));
        assert_eq!(anomalies.len(), 1);
        assert_eq!(anomalies[0].kind, WhichInvariant::StatusRtMismatch);
        assert_eq!(anomalies[0].severity, InvariantSeverity::AnomalyWarn);
    }

    /// Requirements: L2-SYN-024
    #[test]
    fn anomaly_no_status_no_violation() {
        // Broadcast formats and SPURIOUS_DATA have no Status Word;
        // INV-005 is silent.
        let t = tw(0x02, 36);
        let c = cmd_with(Direction::Receive, 30);
        let anomalies = detect_record_anomalies(&t, &c, None);
        assert!(anomalies.is_empty());
    }

    /// Requirements: L2-SYN-025
    #[test]
    fn anomaly_type_word_reserved_bit_set_logged() {
        // Type word raw with bit 15 set: 0x8402 (wc=4, bit15=1, type=0x02).
        // (The framing parts here are irrelevant — the anomaly check
        // only looks at bit 15.)
        let t = TypeWord {
            message_type: 0x02,
            bus: Bus::A,
            word_count: 4,
            error: false,
            raw: 0x8402,
        };
        let c = cmd_with(Direction::Receive, 1);
        let anomalies = detect_record_anomalies(&t, &c, None);
        assert_eq!(anomalies.len(), 1);
        assert_eq!(anomalies[0].kind, WhichInvariant::TypeWordReservedBit);
        assert_eq!(anomalies[0].severity, InvariantSeverity::AnomalyWarn);
    }

    /// Requirements: L2-SYN-025
    #[test]
    fn anomaly_multiple_can_fire_on_one_record() {
        // Status RT mismatch + reserved bit set: expect TWO anomalies.
        let t = TypeWord {
            message_type: 0x02,
            bus: Bus::A,
            word_count: 36,
            error: false,
            raw: 0xA402, // bit 15 set
        };
        let c = cmd_with(Direction::Receive, 30); // rt=15
        let anomalies = detect_record_anomalies(&t, &c, Some(0x2800)); // status RT=5
        assert_eq!(anomalies.len(), 2);
    }

    // ── L2-DEC-015 / L2-DEC-016 probe tests ──────────────────────────

    /// Canonical 72-byte RT15 SA11 Receive record under IRIG framing.
    /// Byte-exact with the fixture used by rust/tests/integration.rs.
    ///
    /// What matters about the values: the layout scores perfectly
    /// under IRIG (T/R: +2, WC: +2, range: +1 = +5 per record) and
    /// only weakly under Standard.
    fn irig_record_bytes() -> Vec<u8> {
        let mut s = String::new();
        s.push_str("02240F1826DB21F6"); // Type 0x2402 (wc=36) + IRIG TS
        s.push_str("7E79"); // Cmd 0x797E (RT15 R SA11 30dw)
        s.push_str("0004");
        s.push_str("0000");
        s.push_str("0000");
        s.push_str("2F00");
        s.push_str("22CA");
        s.push_str("2F00");
        s.push_str("22CA");
        for _ in 0..22 {
            s.push_str("0000");
        }
        s.push_str("71C7");
        s.push_str("0078"); // Status 0x7800
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }

    /// Requirements: L2-DEC-015
    #[test]
    fn probe_single_irig_record_picks_irig() {
        let data = irig_record_bytes();
        let out = probe_timestamp_format(&data, 0, 8);
        assert_eq!(out.format, TimestampFormat::Irig);
        assert_eq!(out.records_probed, 1);
        assert!(out.irig_score > out.std_score);
    }

    /// Requirements: L2-DEC-015
    #[test]
    fn probe_eight_irig_records_aggregates_decisively() {
        // Stitch 8 copies of the canonical IRIG record together.
        let one = irig_record_bytes();
        let mut data = Vec::new();
        for _ in 0..8 {
            data.extend_from_slice(&one);
        }
        let out = probe_timestamp_format(&data, 0, 8);
        assert_eq!(out.format, TimestampFormat::Irig);
        assert_eq!(out.records_probed, 8);
        assert_eq!(out.confidence, DetectionConfidence::Decisive);
        // Each IRIG record scores +5 (T/R + WC + range); eight of
        // them yields 40 IRIG vs much lower Standard.
        assert!(out.irig_score >= 40);
        assert!(out.irig_score - out.std_score >= DECISIVE_MARGIN);
    }

    /// Requirements: L2-DEC-015
    #[test]
    fn probe_stops_at_eof_records_probed_reflects_truncation() {
        // Only 3 records' worth of data — probe should report 3 even
        // though max_records=8.
        let one = irig_record_bytes();
        let mut data = Vec::new();
        for _ in 0..3 {
            data.extend_from_slice(&one);
        }
        let out = probe_timestamp_format(&data, 0, 8);
        assert_eq!(out.records_probed, 3);
        assert_eq!(out.format, TimestampFormat::Irig);
    }

    /// Requirements: L2-DEC-012, L2-DEC-015
    #[test]
    fn probe_zero_score_ties_to_irig() {
        // All-zero buffer — neither format scores anything. IRIG
        // wins the tie per L2-DEC-012.
        let data = vec![0u8; 64];
        let out = probe_timestamp_format(&data, 0, 8);
        assert_eq!(out.format, TimestampFormat::Irig);
        // Both scores zero → Ambiguous by definition of L2-DEC-016
        // (max_score < CONFIDENCE_FLOOR).
        assert_eq!(out.confidence, DetectionConfidence::Ambiguous);
    }

    /// Requirements: L2-DEC-016
    #[test]
    fn probe_ambiguous_below_floor_classifies_ambiguous() {
        // A single record whose scoring is mostly indistinguishable
        // — IRIG range check passes but neither T/R nor WC match,
        // and the corresponding Standard signals are also weak.
        let data = vec![0u8; 16];
        let out = probe_timestamp_format(&data, 0, 8);
        let max_score = out.irig_score.max(out.std_score);
        let margin = (out.irig_score - out.std_score).abs();
        // Confirm we're in the L2-DEC-016 ambiguous region.
        assert!(max_score < CONFIDENCE_FLOOR || margin < MIN_MARGIN);
        assert_eq!(out.confidence, DetectionConfidence::Ambiguous);
    }

    /// Requirements: L2-DEC-015
    #[test]
    fn probe_max_records_one_still_works() {
        // max_records=0 clamps to 1; max_records=1 probes exactly
        // the first record.
        let data = irig_record_bytes();
        let out_zero = probe_timestamp_format(&data, 0, 0);
        let out_one = probe_timestamp_format(&data, 0, 1);
        assert_eq!(out_zero.records_probed, 1);
        assert_eq!(out_one.records_probed, 1);
        assert_eq!(out_zero.format, out_one.format);
        assert_eq!(out_zero.irig_score, out_one.irig_score);
    }
}
