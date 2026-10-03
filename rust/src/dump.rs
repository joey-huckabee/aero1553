//! Hex dump utilities. Two modes:
//!   - raw: classic hex+ASCII view of any byte range
//!   - record-aware: parses Type Word + IRIG timestamp + Command Word and
//!     prints an annotated header per record before the bytes
//!
//! Mirrors the Python `dump.py` output format closely enough for diffing.

use std::fmt::Write as _;
use std::fs::File;
use std::io::{BufWriter, Read, Write};
use std::path::Path;

use crate::decode::{
    MIN_RECORD_BYTES, MIN_RECORD_WORDS, classify_message_format, decode_command_word,
    decode_irig_timestamp, decode_type_word, read_u16,
};
use crate::error::{MieError, MieResult};
use crate::log_warn;
use crate::models::{
    Direction, MessageFormat, MessageType, TypeWord, ddc_error_description_or_unknown,
};

fn type_name(code: u8) -> String {
    match MessageType::from_code(code) {
        Some(MessageType::ModeCommand) => "Mode Command".into(),
        Some(MessageType::BcToRt) => "BC->RT (Receive)".into(),
        Some(MessageType::RtToBc) => "RT->BC (Transmit)".into(),
        Some(MessageType::RtToRt) => "RT->RT".into(),
        Some(MessageType::BroadcastBcToRt) => "Broadcast BC->RT".into(),
        Some(MessageType::BroadcastRtToRt) => "Broadcast RT->RT".into(),
        Some(MessageType::SpuriousData) => "Spurious Data".into(),
        None => format!("UNKNOWN(0x{code:02X})"),
    }
}

fn read_file(path: &Path) -> MieResult<Vec<u8>> {
    if !path.exists() {
        return Err(MieError::FileNotFound {
            path: path.to_path_buf(),
        });
    }
    let mut file = File::open(path).map_err(|source| MieError::FileIo {
        path: path.to_path_buf(),
        source,
    })?;
    let mut buf = Vec::new();
    file.read_to_end(&mut buf)
        .map_err(|source| MieError::FileIo {
            path: path.to_path_buf(),
            source,
        })?;
    if buf.is_empty() {
        return Err(MieError::FileEmpty {
            path: path.to_path_buf(),
        });
    }
    Ok(buf)
}

/// Map an output `io::Error` to a `MieError::WriterError`. The dump always
/// writes to stdout in production (the `*_to_stdout` wrappers), so the
/// destination is labelled accordingly. The underlying error kind is
/// preserved so the CLI can treat a broken pipe as a clean exit (L2-WRT-018).
fn writer_error(source: std::io::Error) -> MieError {
    MieError::WriterError {
        destination: "stdout".to_string(),
        source,
    }
}

fn write_hex_line<W: Write>(
    out: &mut W,
    indent: &str,
    addr: usize,
    chunk: &[u8],
) -> std::io::Result<()> {
    write!(out, "{indent}{addr:08X}  ")?;
    let mut hex_part = String::with_capacity(48);
    for b in chunk {
        // Infallible: writing to a String never errors.
        let _ = write!(hex_part, "{b:02X} ");
    }
    if hex_part.ends_with(' ') {
        hex_part.pop();
    }
    write!(out, "{hex_part:<48}  |")?;
    for &b in chunk {
        let c = if (32..127).contains(&b) {
            b as char
        } else {
            '.'
        };
        out.write_all(&[c as u8])?;
    }
    out.write_all(b"|\n")
}

/// Print a raw hex+ASCII dump of `[start_offset, start_offset + length)`.
///
/// Inputs are user-supplied (`--offset`, `--length`); both default to
/// `usize::MAX`-tolerant arithmetic. A start beyond EOF or a length
/// that would overflow simply yields an empty dump rather than panicking.
/// # Errors
///
/// Returns [`MieError::FileNotFound`] or [`MieError::FileEmpty`] for an
/// unusable input, [`MieError::FileIo`] for a read failure, and
/// [`MieError::WriterError`] if the report cannot be written. A closed
/// downstream pipe surfaces as the latter with its kind preserved, so the CLI
/// can treat `dump | head` as the success it is (L2-WRT-018).
pub fn hex_dump_raw(
    path: &Path,
    start_offset: usize,
    length: Option<usize>,
    mut out: impl Write,
) -> MieResult<()> {
    let data = read_file(path)?;
    write_hex_dump_raw(&data, path, start_offset, length, &mut out).map_err(writer_error)
}

/// Inner writer for `hex_dump_raw`. Returns `io::Result` so every write —
/// including the final flush — propagates; the public wrapper maps the
/// failure to `MieError::WriterError`. Previously these writes were
/// discarded with `let _ =`, so an output failure (disk full, broken pipe)
/// was silently reported as success (L2-WRT-018).
fn write_hex_dump_raw<W: Write>(
    data: &[u8],
    path: &Path,
    start_offset: usize,
    length: Option<usize>,
    out: &mut W,
) -> std::io::Result<()> {
    let file_len = data.len();

    // Clamp start to [0, file_len] up-front; reads at start >= file_len
    // produce an empty chunk.
    let chunk_start = start_offset.min(file_len);
    // saturating_add clamps to usize::MAX on overflow; .min(file_len)
    // then bounds it. .max(chunk_start) handles the case where length=0
    // or arithmetic produced an end before start.
    let end = match length {
        Some(n) => start_offset.saturating_add(n).min(file_len),
        None => file_len,
    }
    .max(chunk_start);
    let chunk = &data[chunk_start..end];

    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    writeln!(out, "File: {name} ({file_len} bytes)")?;
    writeln!(out, "Range: 0x{chunk_start:08X}-0x{end:08X}")?;
    writeln!(out)?;

    let mut i = 0;
    while i < chunk.len() {
        let line = &chunk[i..(i + 16).min(chunk.len())];
        // chunk_start is bounded by file_len; i is bounded by chunk.len()
        // which is bounded by file_len - chunk_start. Sum can't overflow.
        write_hex_line(out, "  ", chunk_start.saturating_add(i), line)?;
        i += 16;
    }
    out.flush()
}

/// Print a record-aware hex dump. Each record is preceded by a one-line
/// header summarising decoded Type Word, timestamp, and Command Word.
/// # Errors
///
/// Same set as [`hex_dump_raw`]. A malformed record is **not** an error: the
/// scan writes an inline `!! …` note, logs it (L2-CLI-013), and stops.
pub fn hex_dump_records(
    path: &Path,
    max_records: Option<u64>,
    start_offset: usize,
    mut out: impl Write,
) -> MieResult<()> {
    let data = read_file(path)?;
    write_hex_dump_records(&data, path, max_records, start_offset, &mut out).map_err(writer_error)
}

/// Inner writer for `hex_dump_records`; see `write_hex_dump_raw` for why
/// this returns `io::Result` (every write, including the trailing flush,
/// must propagate so an output failure is not silently swallowed).
fn write_hex_dump_records<W: Write>(
    data: &[u8],
    path: &Path,
    max_records: Option<u64>,
    start_offset: usize,
    out: &mut W,
) -> std::io::Result<()> {
    let file_len = data.len();
    let mut offset = start_offset;
    let mut record_num: u64 = 0;

    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    writeln!(out, "File: {name} ({file_len} bytes)")?;
    writeln!(out, "Record dump starting at offset 0x{start_offset:08X}")?;
    writeln!(out)?;

    // Loop guard uses checked_add so a start_offset of usize::MAX (or a
    // file shorter than MIN_RECORD_BYTES) exits cleanly instead of
    // wrapping arithmetic and panicking.
    while let Some(min_end) = offset.checked_add(MIN_RECORD_BYTES) {
        if min_end > file_len {
            break;
        }
        if let Some(max) = max_records
            && record_num >= max
        {
            break;
        }

        let Some(type_raw) = read_u16(data, offset) else {
            break;
        };
        let tw = decode_type_word(type_raw);

        // Validate the record's extent; a stop reason is written inline and
        // logged (L2-CLI-013) inside the helper, so here we just break.
        let Some(record_end) = dump_record_extent(tw, offset, file_len, out)? else {
            break;
        };
        let record_bytes = record_end - offset;

        write_record_annotation(out, data, tw, offset, record_bytes, record_num)?;
        write_record_hex_payload(out, &data[offset..record_end], offset)?;
        writeln!(out)?;

        // offset can advance to record_end; checked again at the top of the
        // next iteration.
        offset = record_end;
        record_num += 1;
    }

    writeln!(out, "{}", "-".repeat(72))?;
    writeln!(out, "{record_num} records dumped.")?;
    out.flush()
}

/// Validate the record at `offset` for the dump scan. Returns `Some(record_end)`
/// to proceed, or `None` to stop scanning — writing the inline anomaly note to
/// `out` and logging it (L2-CLI-013) on each stop path.
fn dump_record_extent<W: Write>(
    tw: TypeWord,
    offset: usize,
    file_len: usize,
    out: &mut W,
) -> std::io::Result<Option<usize>> {
    if tw.word_count < MIN_RECORD_WORDS {
        writeln!(
            out,
            "  !! Invalid word_count={} at 0x{:08X}, stopping",
            tw.word_count, offset
        )?;
        log_warn!(
            "dump: invalid word_count={} at 0x{:X}; stopping record scan",
            tw.word_count,
            offset
        );
        return Ok(None);
    }
    let record_bytes = usize::from(tw.word_count) * 2;
    // record_bytes maxes at 63 * 2 = 126; offset is bounded by the loop
    // guard. checked_add belt-and-suspenders.
    let Some(record_end) = offset.checked_add(record_bytes) else {
        writeln!(
            out,
            "  !! Offset overflow at 0x{offset:08X} (record_bytes={record_bytes}), stopping"
        )?;
        log_warn!(
            "dump: offset overflow at 0x{:X} (record_bytes={}); stopping record scan",
            offset,
            record_bytes
        );
        return Ok(None);
    };
    if record_end > file_len {
        writeln!(
            out,
            "  !! Truncated record at 0x{:08X} ({} bytes needed, {} available)",
            offset,
            record_bytes,
            file_len - offset
        )?;
        log_warn!(
            "dump: truncated record at 0x{:X} ({} bytes needed, {} available); \
             stopping record scan",
            offset,
            record_bytes,
            file_len - offset
        );
        return Ok(None);
    }
    Ok(Some(record_end))
}

/// Write the decoded-header annotation block (Type / Time / Cmd) for one record.
/// The IRIG timestamp decode is a best-effort summary — for Standard-format
/// files the raw bytes below the header remain authoritative.
fn write_record_annotation<W: Write>(
    out: &mut W,
    data: &[u8],
    tw: TypeWord,
    offset: usize,
    record_bytes: usize,
    record_num: u64,
) -> std::io::Result<()> {
    let ts_upper = read_u16(data, offset + 2).unwrap_or(0);
    let ts_middle = read_u16(data, offset + 4).unwrap_or(0);
    let ts_lower = read_u16(data, offset + 6).unwrap_or(0);
    let ts = decode_irig_timestamp(ts_upper, ts_middle, ts_lower);
    let cmd = decode_command_word(read_u16(data, offset + 8).unwrap_or(0));
    let dir_char = if cmd.direction == Direction::Transmit {
        'T'
    } else {
        'R'
    };

    // Classified message format. dump runs on suspect files, so a record that
    // cannot be classified degrades to a label rather than propagating an error.
    // `3` = IRIG timestamp words (the timestamp above is decoded as IRIG).
    let fmt_name = classify_message_format(tw.message_type, &cmd, tw.word_count, 3)
        .map_or("(unclassifiable)", message_format_name);

    writeln!(out, "{}", "-".repeat(72))?;
    writeln!(
        out,
        "  Record #{record_num}  @  0x{offset:08X}  ({record_bytes} bytes, {wc} words)",
        wc = tw.word_count
    )?;
    writeln!(
        out,
        "  Type:   0x{:04X}  ->  {}  Bus {}  error flag (bit 14): {}",
        tw.raw,
        type_name(tw.message_type),
        tw.bus.as_str(),
        if tw.error { "SET" } else { "clear" }
    )?;
    writeln!(out, "  Format: {fmt_name}")?;
    writeln!(
        out,
        "  Time:   {}{}",
        ts.format(),
        if ts.freerun { "  [FREERUN]" } else { "" }
    )?;
    // A SPURIOUS_DATA record has no Command Word and no Error Word, whatever
    // bit 14 says: everything after its timestamp is leftover bus words. Reading
    // the first as a Command Word (and, with bit 14 set, the last as an Error
    // Word) printed an RT, a subaddress and a "DDC error" that were not there.
    // `3` = IRIG timestamp words, as above.
    if tw.message_type == MessageType::SpuriousData as u8 {
        let words = i32::from(tw.word_count) - 1 - 3;
        writeln!(
            out,
            "  Data:   {} leftover word(s); SPURIOUS_DATA carries no Command Word",
            words.max(0)
        )?;
        return Ok(());
    }
    writeln!(
        out,
        "  Cmd:    0x{:04X}  ->  RT{} SA{} {} WC={}",
        cmd.raw, cmd.rt, cmd.subaddress, dir_char, cmd.data_word_count
    )?;
    // For an errored record the Error Word is the last word of the record; show
    // its value and the DDC description so the reason is legible at a glance.
    if tw.error {
        let code = read_u16(data, offset + (tw.word_count as usize - 1) * 2).unwrap_or(0);
        let desc = ddc_error_description_or_unknown(code);
        writeln!(out, "  Error:  0x{code:04X}  ->  {desc}")?;
    }
    Ok(())
}

/// Canonical uppercase name of a classified message format, matching the Python
/// `MessageFormat` enum names shown in `dump`.
fn message_format_name(fmt: MessageFormat) -> &'static str {
    match fmt {
        MessageFormat::Receive => "RECEIVE",
        MessageFormat::Transmit => "TRANSMIT",
        MessageFormat::RtToRt => "RT_TO_RT",
        MessageFormat::ReceiveBroadcast => "RECEIVE_BROADCAST",
        MessageFormat::RtToRtBroadcast => "RT_TO_RT_BROADCAST",
        MessageFormat::ModeCodeTxData => "MODE_CODE_TX_DATA",
        MessageFormat::ModeCodeRxData => "MODE_CODE_RX_DATA",
        MessageFormat::ModeCodeNoData => "MODE_CODE_NO_DATA",
        MessageFormat::ModeCodeBcastNoData => "MODE_CODE_BCAST_NO_DATA",
        MessageFormat::ModeCodeBcastData => "MODE_CODE_BCAST_DATA",
        MessageFormat::SpuriousData => "SPURIOUS_DATA",
    }
}

/// Hex-dump a record's raw bytes, 16 per line, each line offset-annotated.
fn write_record_hex_payload<W: Write>(
    out: &mut W,
    record_data: &[u8],
    offset: usize,
) -> std::io::Result<()> {
    let mut i = 0;
    while i < record_data.len() {
        let line = &record_data[i..(i + 16).min(record_data.len())];
        // offset + i: offset is bounded by the caller's loop guard, i by
        // record_bytes (≤ 126). saturating_add belt-and-suspenders.
        write_hex_line(out, "    ", offset.saturating_add(i), line)?;
        i += 16;
    }
    Ok(())
}

/// Convenience wrapper that writes to stdout (buffered).
/// # Errors
///
/// As [`hex_dump_raw`]; the destination is this process's stdout.
pub fn hex_dump_raw_to_stdout(path: &Path, offset: usize, length: Option<usize>) -> MieResult<()> {
    let stdout = std::io::stdout();
    let buf = BufWriter::new(stdout.lock());
    hex_dump_raw(path, offset, length, buf)
}

/// # Errors
///
/// As [`hex_dump_records`]; the destination is this process's stdout.
pub fn hex_dump_records_to_stdout(
    path: &Path,
    max_records: Option<u64>,
    offset: usize,
) -> MieResult<()> {
    let stdout = std::io::stdout();
    let buf = BufWriter::new(stdout.lock());
    hex_dump_records(path, max_records, offset, buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempFile(std::path::PathBuf);
    impl TempFile {
        fn write(bytes: &[u8]) -> Self {
            static C: AtomicU64 = AtomicU64::new(0);
            let n = C.fetch_add(1, Ordering::Relaxed);
            let pid = std::process::id();
            let p = std::env::temp_dir().join(format!("mie-dump-test-{pid}-{n}.bin"));
            std::fs::write(&p, bytes).unwrap();
            Self(p)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TempFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    /// Requirements: L2-CLI-014
    #[test]
    fn dump_report_is_pure_ascii_and_lf() {
        // The report is a stdout PAYLOAD -- piped, redirected, and diffed
        // against the Python and C++ reports -- so it carries no byte above
        // 0x7F and no CR. Python's report used box-drawing, arrow and en-dash
        // characters until this was written down; those could not be encoded on
        // a redirected Windows stdout at cp1252 and made 11 of 34 lines differ.
        //
        // Both views, and a record with an anomaly note, because the rule, the
        // arrow and the `!!` note are the three places a non-ASCII character
        // would plausibly be reintroduced.
        let f = TempFile::write(&[
            0x02, 0x24, 0x0F, 0x18, 0x26, 0xDB, 0x21, 0xF6, 0x7E, 0x79, 0x00, 0x04,
        ]);

        for raw in [true, false] {
            let mut out = Vec::new();
            if raw {
                hex_dump_raw(f.path(), 0, None, &mut out).unwrap();
            } else {
                hex_dump_records(f.path(), None, 0, &mut out).unwrap();
            }
            assert!(!out.is_empty(), "raw={raw}: the dump produced no output");
            let offending: Vec<u8> = out.iter().copied().filter(|b| *b > 0x7F).collect();
            assert!(
                offending.is_empty(),
                "raw={raw}: dump emitted non-ASCII bytes {offending:?}"
            );
            assert!(
                !out.contains(&b'\r'),
                "raw={raw}: dump emitted CR; the report is LF on every platform"
            );
        }
    }

    /// Requirements: L2-CLI-009
    #[test]
    fn raw_dump_format() {
        let f = TempFile::write(b"AB\x00\x01\x02\x03");
        let mut out = Vec::new();
        hex_dump_raw(f.path(), 0, None, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("00000000"));
        assert!(s.contains("41 42")); // "AB"
        assert!(s.contains("|AB"));
    }

    /// Requirements: L2-CLI-009
    #[test]
    fn record_dump_handles_one_record() {
        // Minimal valid record: type 0x2402, then 35 zero words = 70 bytes
        let mut buf = Vec::with_capacity(72);
        buf.extend_from_slice(&[0x02, 0x24]);
        buf.extend_from_slice(&[0u8; 70]);
        let f = TempFile::write(&buf);
        let mut out = Vec::new();
        hex_dump_records(f.path(), Some(1), 0, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("Record #0"));
        assert!(s.contains("BC->RT (Receive)"));
        assert!(s.contains("1 records dumped"));
    }

    /// The record-aware dump surfaces the bit-14 error flag, the classified
    /// message format, and the DDC Error Word + description for an errored
    /// record. Mirrors the Python
    /// `test_dump_annotates_errored_record_with_code_and_format`.
    /// Requirements: L2-CLI-009
    #[test]
    fn record_dump_annotates_errored_record() {
        // Errored RT->BC (Transmit): Type 0x4604 (bit 14 set), 6 words / 12
        // bytes, Error Word 0x0120 (No Status Response or Too Few Data Words).
        let buf = [
            0x04, 0x46, 0x0F, 0x18, 0xF2, 0xDA, 0x26, 0x5D, 0x3E, 0x7C, 0x20, 0x01,
        ];
        let f = TempFile::write(&buf);
        let mut out = Vec::new();
        hex_dump_records(f.path(), Some(1), 0, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("error flag (bit 14): SET"), "{s}");
        assert!(s.contains("Format: TRANSMIT"), "{s}");
        assert!(s.contains("Error:  0x0120"), "{s}");
        assert!(
            s.contains("No Status Response or Too Few Data Words"),
            "{s}"
        );
    }

    /// A `SPURIOUS_DATA` record has no Command Word and no Error Word, bit 14
    /// or not: its words after the timestamp are leftover bus words, and are
    /// reported as such rather than decoded as an RT/subaddress and a DDC code.
    /// Requirements: L2-CLI-009
    #[test]
    fn record_dump_reports_spurious_words_as_leftovers() {
        // Type 0x4620: SPURIOUS_DATA, bit 14 set, 6 words; two leftover words.
        let buf = [
            0x20, 0x46, 0x0F, 0x18, 0x26, 0xDB, 0x38, 0xF7, 0xAA, 0xAA, 0xBB, 0xBB,
        ];
        let f = TempFile::write(&buf);
        let mut out = Vec::new();
        hex_dump_records(f.path(), Some(1), 0, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("error flag (bit 14): SET"), "{s}");
        assert!(
            s.contains("Data:   2 leftover word(s); SPURIOUS_DATA carries no Command Word"),
            "{s}"
        );
        assert!(!s.contains("Cmd:"), "{s}");
        assert!(!s.contains("Error:"), "{s}");
    }

    /// L2-CLI-013: a scan-stop anomaly emits a logger WARN in addition to the
    /// inline report note. The crate logger writes to process stderr and is
    /// not capturable in-process, so this asserts the inline note (the same
    /// branch that emits `log_warn!`); the WARN emission is verified by
    /// inspection and exercised live by `dump_arbitrary_bytes_never_panics`
    /// under `--nocapture`. Mirrors the Python `test_dump_logs_warning_on_truncated_record`.
    /// Requirements: L2-CLI-013
    #[test]
    fn record_dump_notes_and_warns_truncated_record() {
        // Type 0x2402 declares word_count=36 (72 bytes) but only 20 bytes
        // exist → the record-aware scan hits the truncated-record branch.
        let mut buf = Vec::with_capacity(20);
        buf.extend_from_slice(&[0x02, 0x24]);
        buf.extend_from_slice(&[0u8; 18]);
        let f = TempFile::write(&buf);
        let mut out = Vec::new();
        hex_dump_records(f.path(), Some(1), 0, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("!! Truncated record"));
        assert!(s.contains("72 bytes needed, 20 available"));
    }

    /// Requirements: L2-RDR-005
    #[test]
    fn missing_file_returns_error() {
        let mut out = Vec::new();
        let err = hex_dump_raw(Path::new("/no/such/file.bin"), 0, None, &mut out).unwrap_err();
        assert_eq!(err.kind(), crate::error::MieErrorKind::FileNotFound);
    }

    /// Regression test: `--raw --offset usize::MAX --length 1` must not
    /// panic on the `start_offset + n` computation. The fix uses
    /// `saturating_add` and clamps to `file_len`; the result is an empty
    /// dump rather than a crash.
    /// Requirements: L1-ROB-001
    #[test]
    fn raw_dump_offset_max_length_one_does_not_panic() {
        let f = TempFile::write(b"AB\x00\x01\x02\x03");
        let mut out = Vec::new();
        hex_dump_raw(f.path(), usize::MAX, Some(1), &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        // Header is still printed; the data range is empty.
        assert!(s.contains("File:"));
    }

    /// Requirements: L1-ROB-001
    #[test]
    fn raw_dump_offset_max_length_max_does_not_panic() {
        let f = TempFile::write(b"AB\x00\x01");
        let mut out = Vec::new();
        hex_dump_raw(f.path(), usize::MAX, Some(usize::MAX), &mut out).unwrap();
        // No assertion on contents; the test passes if no panic.
    }

    /// Requirements: L2-CLI-009
    #[test]
    fn raw_dump_offset_beyond_eof_yields_empty() {
        let f = TempFile::write(b"AB\x00\x01");
        let mut out = Vec::new();
        hex_dump_raw(f.path(), 1000, Some(16), &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        // No hex line in the body; only the header.
        assert!(!s.contains("41 42"));
    }

    /// Regression test for the record-mode loop guard.
    /// `offset + MIN_RECORD_BYTES <= file_len` overflows if offset is
    /// near `usize::MAX`; the fix uses `checked_add` so the loop simply
    /// doesn't enter.
    /// Requirements: L1-ROB-001
    #[test]
    fn record_dump_offset_max_does_not_panic() {
        let f = TempFile::write(b"AB\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09");
        let mut out = Vec::new();
        hex_dump_records(f.path(), Some(1), usize::MAX, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("0 records dumped"));
    }

    /// Requirements: L1-ROB-001
    #[test]
    fn record_dump_offset_just_short_of_max_does_not_panic() {
        let f = TempFile::write(b"AB\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09");
        let mut out = Vec::new();
        // offset large enough that offset + MIN_RECORD_BYTES overflows
        // but offset itself does not.
        hex_dump_records(f.path(), Some(1), usize::MAX - 4, &mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("0 records dumped"));
    }

    /// The L2-CLI-013 offset-overflow anomaly, exercised where it is
    /// actually reachable.
    ///
    /// `hex_dump_records` cannot reach it: its loop guard requires
    /// `offset + MIN_RECORD_BYTES <= file_len`, and `file_len` is a real
    /// mapped length, so `offset` is bounded well below `usize::MAX` while
    /// `record_bytes` maxes out at 126 (`word_count` is a 6-bit Type Word
    /// field, so at most 63 words). The two tests above pin that guard by
    /// showing the loop declines to enter.
    ///
    /// The guard inside `dump_record_extent` is therefore defense in depth
    /// for the helper's own contract — it accepts any `offset` — and this
    /// calls it there directly rather than leaving the requirement's third
    /// anomaly credited to truncated-record tests that never touch it.
    /// Requirements: L2-CLI-013
    #[test]
    fn dump_record_extent_notes_offset_overflow() {
        // word_count=36 (72 bytes), comfortably above MIN_RECORD_WORDS, so
        // the invalid-word_count branch is not the one that fires.
        let tw = decode_type_word(0x2402);
        assert!(tw.word_count >= MIN_RECORD_WORDS);

        let mut out = Vec::new();
        let stop = dump_record_extent(tw, usize::MAX - 4, usize::MAX, &mut out).unwrap();

        assert!(stop.is_none(), "overflow must stop the scan");
        let s = String::from_utf8(out).unwrap();
        assert!(s.contains("!! Offset overflow"), "inline note missing: {s}");
        assert!(s.contains("record_bytes=72"), "record_bytes missing: {s}");
    }

    /// A `Write` that fails every write/flush with a configurable error
    /// kind — used to prove the dump surfaces output I/O failures rather
    /// than reporting success (L2-WRT-018).
    struct FailingWriter(std::io::ErrorKind);
    impl Write for FailingWriter {
        fn write(&mut self, _buf: &[u8]) -> std::io::Result<usize> {
            Err(std::io::Error::new(self.0, "injected write failure"))
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Err(std::io::Error::new(self.0, "injected flush failure"))
        }
    }

    /// Requirements: L2-WRT-018
    #[test]
    fn raw_dump_surfaces_write_failure() {
        let f = TempFile::write(b"AB\x00\x01");
        let err =
            hex_dump_raw(f.path(), 0, None, FailingWriter(std::io::ErrorKind::Other)).unwrap_err();
        assert_eq!(err.kind(), crate::error::MieErrorKind::WriterError);
    }

    /// Requirements: L2-WRT-018
    #[test]
    fn records_dump_surfaces_write_failure() {
        let mut buf = Vec::with_capacity(72);
        buf.extend_from_slice(&[0x02, 0x24]);
        buf.extend_from_slice(&[0u8; 70]);
        let f = TempFile::write(&buf);
        let err = hex_dump_records(
            f.path(),
            Some(1),
            0,
            FailingWriter(std::io::ErrorKind::Other),
        )
        .unwrap_err();
        assert_eq!(err.kind(), crate::error::MieErrorKind::WriterError);
    }

    /// A broken pipe on the dump's stdout must be *classified* as such so
    /// the CLI can exit 0 (L2-WRT-018) rather than fail.
    /// Requirements: L2-WRT-018
    #[test]
    fn dump_broken_pipe_is_classified_for_exit_zero() {
        let f = TempFile::write(b"AB\x00\x01");
        let err = hex_dump_raw(
            f.path(),
            0,
            None,
            FailingWriter(std::io::ErrorKind::BrokenPipe),
        )
        .unwrap_err();
        assert!(
            err.is_broken_pipe(),
            "broken pipe must be classified so run_dump can exit 0 (L2-WRT-018)"
        );
    }
}
