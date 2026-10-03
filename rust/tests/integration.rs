//! End-to-end integration tests using byte-exact fixtures from the
//! Python reference's `tests/conftest.py`. Each fixture has been
//! cross-referenced against vendor-generated CSV output, so they serve
//! as oracles for the Rust port.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use aero1553::filter::{FilterConfig, FilterIterExt};
use aero1553::models::{Bus, Direction, MessageFormat, TimeRender};
use aero1553::reader::MieFileReader;
use aero1553::writer::write_csv;

/// Requirements: L3-RS-013
///
/// The crate root re-exports the public decode entry point and core types
/// via `pub use` (rust/src/lib.rs), so downstream crates can name them without
/// the internal module path. Each helper accepts a *module-path* type but
/// is bound to a function pointer over the *crate-root* path; that binding
/// compiles only if the root path resolves AND is the same type as the
/// module path (a genuine re-export, not a coincidental name).
#[test]
fn crate_root_reexports_public_decode_api() {
    fn takes_reader(_: aero1553::reader::MieFileReader) {}
    fn takes_message(_: aero1553::models::MieMessage) {}
    fn takes_error(_: aero1553::error::MieError) {}

    let _: fn(aero1553::MieFileReader) = takes_reader;
    let _: fn(aero1553::MieMessage) = takes_message;
    let _: fn(aero1553::MieError) = takes_error;
}

// ── Fixtures (byte-exact from python/tests/conftest.py) ───────────────

fn record_rt15_sa11_rcv() -> Vec<u8> {
    let mut s = String::new();
    s.push_str("02240F1826DB21F6"); // Type + IRIG TS
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
    hex(&s)
}

fn record_rt15_sa22_rcv() -> Vec<u8> {
    let mut s = String::new();
    s.push_str("02110F1826DB38F7"); // Type 0x1102 (wc=17), TS
    s.push_str("CB7A"); // Cmd 0x7ACB (RT15 R SA22 11dw)
    s.push_str("0010");
    s.push_str("0000");
    s.push_str("0700");
    s.push_str("0008");
    for _ in 0..5 {
        s.push_str("0000");
    }
    s.push_str("C880");
    s.push_str("E803");
    s.push_str("0078"); // Status
    hex(&s)
}

fn record_rt15_sa22_xmt() -> Vec<u8> {
    let mut s = String::new();
    s.push_str("04240F1826DBE3F9"); // Type 0x2404 (wc=36, type=0x04 transmit), TS
    s.push_str("DE7E"); // Cmd 0x7EDE (RT15 T SA22 30dw)
    s.push_str("0078"); // Status (transmit puts status before data)
    s.push_str("2010");
    s.push_str("8241");
    s.push_str("0000");
    s.push_str("0815");
    for _ in 0..4 {
        s.push_str("0000");
    }
    s.push_str("00FE");
    for _ in 0..9 {
        s.push_str("0000");
    }
    s.push_str("0300");
    for _ in 0..6 {
        s.push_str("0000");
    }
    s.push_str("0020");
    for _ in 0..4 {
        s.push_str("0000");
    }
    hex(&s)
}

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

// ── Temp file helper ──────────────────────────────────────────────────

struct TempFile(PathBuf);
impl TempFile {
    fn new(bytes: &[u8]) -> Self {
        static C: AtomicU64 = AtomicU64::new(0);
        let n = C.fetch_add(1, Ordering::Relaxed);
        let pid = std::process::id();
        let p = std::env::temp_dir().join(format!("mie-int-{pid}-{n}.bin"));
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(bytes).unwrap();
        f.flush().unwrap();
        Self(p)
    }
    fn path(&self) -> &std::path::Path {
        &self.0
    }
}
impl Drop for TempFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

// ── Tests ─────────────────────────────────────────────────────────────

/// Requirements: L2-RDR-007
#[test]
fn single_receive_record_decodes_to_expected_fields() {
    let bytes = record_rt15_sa11_rcv();
    assert_eq!(bytes.len(), 72);
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1);
    let m = &msgs[0];
    assert_eq!(m.command_word.unwrap().rt, 15);
    assert_eq!(m.command_word.unwrap().subaddress, 11);
    assert_eq!(m.command_word.unwrap().direction, Direction::Receive);
    assert_eq!(m.message_format, MessageFormat::Receive);
    assert_eq!(m.bus(), Bus::A);
    assert_eq!(m.data_words.len(), 30);
    assert_eq!(m.data_words.as_slice()[0], 0x0400);
    assert_eq!(m.data_words.as_slice()[3], 0x002F);
    assert_eq!(m.data_words.as_slice()[4], 0xCA22);
    assert_eq!(m.data_words.as_slice()[29], 0xC771);
    assert_eq!(m.status_word, Some(0x7800));
    assert_eq!(m.error_label(), "");
}

/// Requirements: L2-RDR-008
#[test]
fn single_transmit_record_layout() {
    let bytes = record_rt15_sa22_xmt();
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1);
    let m = &msgs[0];
    assert_eq!(m.command_word.unwrap().direction, Direction::Transmit);
    assert_eq!(m.command_word.unwrap().subaddress, 22);
    assert_eq!(m.message_format, MessageFormat::Transmit);
    assert_eq!(m.status_word, Some(0x7800));
    assert_eq!(m.data_words.len(), 30);
}

/// Requirements: L2-RDR-015
#[test]
fn multi_record_stream() {
    let mut bytes = Vec::new();
    bytes.extend(record_rt15_sa11_rcv());
    bytes.extend(record_rt15_sa22_rcv());
    bytes.extend(record_rt15_sa22_xmt());
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 3);
    assert_eq!(msgs[0].command_word.unwrap().subaddress, 11);
    assert_eq!(msgs[1].command_word.unwrap().subaddress, 22);
    assert_eq!(msgs[2].command_word.unwrap().subaddress, 22);
    assert_eq!(msgs[2].command_word.unwrap().direction, Direction::Transmit);
    // file_offsets cumulative
    assert_eq!(msgs[0].file_offset, 0);
    assert_eq!(msgs[1].file_offset, 72);
    assert_eq!(msgs[2].file_offset, 72 + 34); // sa22 rcv = 17 words = 34 bytes
}

/// Requirements: L2-SYN-011, L1-EXIT-004
#[test]
fn lenient_mode_unrecoverable_sync_loss_yields_terminal_error() {
    // L1-EXIT-004 lenient-mode contract: when sync recovery exhausts within
    // the 64 KB scan window, the iterator must yield a terminal
    // Err(UnrecoverableSyncLoss) item before stopping. Previously this
    // returned None silently and the CLI exited 0 with truncated data.
    use aero1553::error::{MieError, MieErrorKind};

    // Two valid records back-to-back so the first record's look-ahead
    // check sees the second record's Type Word and accepts. Then 70 KB
    // of 0xFF — guarantees recover_sync from the second-record boundary
    // exhausts the 64 KB scan window without finding any valid Type
    // Word.
    let mut bytes = record_rt15_sa11_rcv();
    bytes.extend(record_rt15_sa11_rcv());
    bytes.extend(vec![0xFFu8; 70_000]);
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let mut it = reader.iter();

    // First record decodes normally.
    match it.next() {
        Some(Ok(msg)) => assert_eq!(msg.command_word.unwrap().rt, 15),
        other => panic!("expected first record OK, got {other:?}"),
    }

    // Second record is kept too. Through v2.11.1 it was discarded here because
    // its *successor* boundary is the 0xFF tail; continuous validation no longer
    // looks ahead (L2-SYN-005), so a well-formed record is not lost to its
    // neighbour's damage and the terminal moves one call later.
    match it.next() {
        Some(Ok(msg)) => assert_eq!(msg.command_word.unwrap().rt, 15),
        other => panic!("expected second record OK, got {other:?}"),
    }

    // Third call: validation fails on the 0xFF tail, recover_sync
    // walks 64 KB without finding sync, terminal Err surfaces.
    match it.next() {
        Some(Err(e)) => {
            assert_eq!(e.kind(), MieErrorKind::UnrecoverableSyncLoss);
            if let MieError::UnrecoverableSyncLoss { sync_losses, .. } = e {
                assert!(sync_losses >= 1);
            } else {
                unreachable!();
            }
        }
        other => panic!("expected Some(Err(UnrecoverableSyncLoss)), got {other:?}"),
    }

    // Subsequent calls: None forever.
    assert!(it.next().is_none());
    assert!(it.next().is_none());

    drop(it);
    // Reader-level counter is consistent with what the terminal error
    // reported. (Reader's getter is now exposed for the CLI's L1-EXIT-005
    // exit-class summary.)
    assert!(reader.sync_losses() >= 1);
}

/// L1-SYN-002: recovery scanning is forward-only and bounded — across a
/// full decode the cumulative scan never re-traverses already-scanned
/// bytes. We exercise repeated recoveries (valid records separated by
/// short recoverable garbage) and assert the observable consequence: the
/// decoded record offsets advance strictly forward and stay within the
/// file, and the recovery count is bounded (one per corruption region,
/// never an unbounded re-scan).
/// Requirements: L1-SYN-002
#[test]
fn recovery_scan_is_forward_only_and_bounded() {
    // Three RR blocks separated by short recoverable garbage:
    //   RR [garbage] RR [garbage] RR
    // Two valid records per block so the leading record passes its
    // two-record look-ahead; each 0xFF run fails validation at the
    // post-block boundary, so recover_sync walks forward (well within the
    // 64 KB per-recovery cap) to the next block.
    const GARBAGE: usize = 16;
    let rec = record_rt15_sa11_rcv();
    let mut block = rec.clone();
    block.extend(&rec);
    let mut bytes = Vec::new();
    bytes.extend(&block);
    bytes.extend(vec![0xFFu8; GARBAGE]);
    bytes.extend(&block);
    bytes.extend(vec![0xFFu8; GARBAGE]);
    bytes.extend(&block);
    let file_len = bytes.len() as u64;

    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader
        .iter()
        .collect::<Result<_, _>>()
        .expect("recoverable corruption must decode to completion, not error");

    // Recovery fired (more than one block decoded) but bounded.
    assert!(msgs.len() >= 2, "recovery should reach later blocks");

    // Forward-only: offsets strictly increase and never exceed the file —
    // the reader never rewinds into already-scanned bytes (the core of the
    // L1-SYN-002 cumulative bound).
    for pair in msgs.windows(2) {
        assert!(
            pair[1].file_offset > pair[0].file_offset,
            "record offsets must advance strictly forward: {} then {}",
            pair[0].file_offset,
            pair[1].file_offset
        );
    }
    assert!(msgs.last().unwrap().file_offset < file_len);

    // Bounded: one recovery per corruption region (two regions here),
    // never an unbounded re-scan. A forward-only scanner can recover at
    // most once per 2-byte step, far below file_len/2.
    let losses = reader.sync_losses();
    assert!(
        (1..=2).contains(&losses),
        "expected 1-2 recoveries (one per corruption region), got {losses}"
    );
}

/// L2-DEC-009: payload extraction is bounded by the Type Word's declared
/// extent and never consumes bytes from the following record. A Command
/// Word that declares more data words than the Type Word's `word_count`
/// can hold is rejected by the L2-SYN-022 capacity invariant *before*
/// extraction runs, and the reader additionally slices to the record
/// extent (`&self.data[..record_end]`) — so a malformed record can never
/// overrun into its successor.
/// Requirements: L2-DEC-009
#[test]
fn payload_extraction_does_not_overrun_into_next_record() {
    use aero1553::error::MieErrorKind;
    use aero1553::models::TimestampFormat;
    use aero1553::reader::ReaderOptions;

    // R1: Type Word declares word_count = 10 words (20 bytes), but the
    // Command Word 0x797E declares data_word_count = 30 — far more payload
    // than 10 words can hold. R2: a normal valid record immediately after.
    let mut r1 = Vec::new();
    r1.extend_from_slice(&0x0A02u16.to_le_bytes()); // Type: wc=10, type=0x02 (BC->RT)
    r1.extend_from_slice(&[0x0F, 0x18, 0x26, 0xDB, 0x21, 0xF6]); // IRIG ts (3 words)
    r1.extend_from_slice(&0x797Eu16.to_le_bytes()); // Cmd: RT15 R SA11 dwc=30
    r1.extend_from_slice(&[0u8; 10]); // 5 payload words → total 10 words = 20 bytes
    assert_eq!(r1.len(), 20);

    let r2 = record_rt15_sa11_rcv();
    let mut bytes = r1.clone();
    bytes.extend_from_slice(&r2);
    let f = TempFile::new(&bytes);

    // Strict: the over-declaration is rejected (capacity invariant) rather
    // than silently decoded into an overrun.
    let reader = MieFileReader::with_options(
        f.path(),
        ReaderOptions {
            strict: true,
            input_time_format: TimestampFormat::Irig,
            ..Default::default()
        },
    )
    .unwrap();
    match reader.iter().next() {
        Some(Err(e)) => assert_eq!(
            e.kind(),
            MieErrorKind::PayloadError,
            "over-declaring record should be a capacity rejection, got {:?}",
            e.kind()
        ),
        other => panic!("expected Some(Err(PayloadError)), got {other:?}"),
    }

    // Lenient: R1 is skipped and the following R2 decodes intact at its
    // true offset — proving R1 consumed nothing beyond its 20-byte extent.
    let reader = MieFileReader::with_options(
        f.path(),
        ReaderOptions {
            input_time_format: TimestampFormat::Irig,
            ..Default::default()
        },
    )
    .unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1, "only the valid R2 survives");
    assert_eq!(
        msgs[0].file_offset, 20,
        "R2 begins exactly after R1's 20-byte declared extent"
    );
    assert_eq!(msgs[0].command_word.unwrap().rt, 15);
}

/// L2-DEC-009 / L1-ROB-001 / L2-SYN-027: an RT-to-RT record whose *second*
/// Command Word over-declares `data_word_count` must not read past the Type
/// Word's declared extent. The L2-SYN-022 capacity invariant is computed from
/// Cmd1, but RT-to-RT extraction takes its count from Cmd2 (the transmit
/// command); fuzzed bytes can keep Cmd1's count small (so the capacity check
/// passes and the record fits the file) while Cmd2 claims 30 words. Extraction
/// reads from the record-bounded slice (`&self.data[..record_end]`) so it
/// completes safely (L2-DEC-009); the over-claim is then a Cmd1/Cmd2
/// `data_word_count` disagreement, which the post-extract L2-SYN-027 invariant
/// rejects (strict errors, lenient skips). Mirrors the Python
/// `test_rt_to_rt_cmd2_overclaim_does_not_overrun`; complements
/// `payload_extraction_does_not_overrun_into_next_record` (the Cmd1 path the
/// capacity invariant catches pre-extract).
/// Requirements: L2-DEC-009, L1-ROB-001, L2-SYN-027
#[test]
fn rt_to_rt_cmd2_overclaim_does_not_overrun() {
    use aero1553::error::MieErrorKind;
    use aero1553::models::TimestampFormat;
    use aero1553::reader::ReaderOptions;

    // R1: Type Word word_count = 10 (20 bytes), type 0x08 (RT-to-RT). Cmd1
    // 0x7961 declares dwc = 1 (small → passes the Cmd1-based capacity check);
    // Cmd2 0x797E declares dwc = 30 (the over-claim). R2: a valid record.
    let mut r1 = Vec::new();
    r1.extend_from_slice(&0x0A08u16.to_le_bytes()); // Type: wc=10, type=0x08 (RT_TO_RT)
    r1.extend_from_slice(&[0x0F, 0x18, 0x26, 0xDB, 0x21, 0xF6]); // IRIG ts (3 words)
    r1.extend_from_slice(&0x7961u16.to_le_bytes()); // Cmd1: RT15 R SA11 dwc=1
    r1.extend_from_slice(&0x797Eu16.to_le_bytes()); // Cmd2: RT15 R SA11 dwc=30 (over-claim)
    r1.extend_from_slice(&[0u8; 2]); // tx_status
    r1.extend_from_slice(&[0u8; 6]); // 3 padding words → total 10 words = 20 bytes
    assert_eq!(r1.len(), 20);

    let mut bytes = r1.clone();
    bytes.extend_from_slice(&record_rt15_sa11_rcv());
    let f = TempFile::new(&bytes);

    // Strict: extraction completes without a panic/overrun (bounded reads),
    // then L2-SYN-027 rejects the Cmd1/Cmd2 mismatch.
    let reader = MieFileReader::with_options(
        f.path(),
        ReaderOptions {
            strict: true,
            input_time_format: TimestampFormat::Irig,
            ..Default::default()
        },
    )
    .unwrap();
    match reader.iter().next() {
        Some(Err(e)) => assert_eq!(e.kind(), MieErrorKind::PayloadError),
        other => panic!("expected Some(Err(PayloadError)), got {other:?}"),
    }

    // Lenient: R1 is skipped; R2 decodes intact at its true offset — proving
    // R1's Cmd2 over-claim consumed nothing beyond its 20-byte declared extent.
    let reader = MieFileReader::with_options(
        f.path(),
        ReaderOptions {
            input_time_format: TimestampFormat::Irig,
            ..Default::default()
        },
    )
    .unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1, "only the valid R2 survives");
    assert_eq!(msgs[0].file_offset, 20);
    assert_eq!(msgs[0].command_word.unwrap().rt, 15);
}

/// L2-SYN-027: an RT-to-RT record whose Cmd1 and Cmd2 disagree on
/// `data_word_count` is rejected end-to-end — even when the record is large
/// enough that neither the L2-SYN-022 capacity check nor the record-bounded
/// reads would fire. Isolates the new invariant from the over-claim/bounds
/// path. Mirrors the Python `test_rt_to_rt_cmd_word_count_mismatch_rejected`.
/// Requirements: L2-SYN-027
#[test]
fn rt_to_rt_cmd_word_count_mismatch_rejected() {
    use aero1553::error::MieErrorKind;
    use aero1553::models::TimestampFormat;
    use aero1553::reader::ReaderOptions;

    // R1: word_count = 13 (26 bytes), type 0x08. Cmd1 0x7963 (RT15 R SA11
    // dwc=3); Cmd2 0x7965 (RT15 R SA11 dwc=5, direction Receive so L2-SYN-023
    // passes). word_count=13 clears the Cmd1-based capacity minimum
    // (1+3+1+(3+3)=11) and holds Cmd2's full 5-word payload + rx_status, so
    // only the Cmd1/Cmd2 mismatch is at fault.
    let mut r1 = Vec::new();
    r1.extend_from_slice(&0x0D08u16.to_le_bytes()); // Type: wc=13, type=0x08 (RT_TO_RT)
    r1.extend_from_slice(&[0x0F, 0x18, 0x26, 0xDB, 0x21, 0xF6]); // IRIG ts (3 words)
    r1.extend_from_slice(&0x7963u16.to_le_bytes()); // Cmd1: RT15 R SA11 dwc=3
    r1.extend_from_slice(&0x7965u16.to_le_bytes()); // Cmd2: RT15 R SA11 dwc=5
    r1.extend_from_slice(&[0u8; 2]); // tx_status
    r1.extend_from_slice(&[0u8; 10]); // 5 data words
    r1.extend_from_slice(&[0u8; 2]); // rx_status → total 13 words = 26 bytes
    assert_eq!(r1.len(), 26);

    let mut bytes = r1.clone();
    bytes.extend_from_slice(&record_rt15_sa11_rcv());
    let f = TempFile::new(&bytes);

    // Strict rejects the mismatch.
    let reader = MieFileReader::with_options(
        f.path(),
        ReaderOptions {
            strict: true,
            input_time_format: TimestampFormat::Irig,
            ..Default::default()
        },
    )
    .unwrap();
    match reader.iter().next() {
        Some(Err(e)) => assert_eq!(e.kind(), MieErrorKind::PayloadError),
        other => panic!("expected Some(Err(PayloadError)), got {other:?}"),
    }

    // Lenient skips R1; only the valid R2 survives, at offset 26.
    let reader = MieFileReader::with_options(
        f.path(),
        ReaderOptions {
            input_time_format: TimestampFormat::Irig,
            ..Default::default()
        },
    )
    .unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1, "only the valid R2 survives");
    assert_eq!(msgs[0].file_offset, 26);
    assert_eq!(msgs[0].command_word.unwrap().rt, 15);
}

/// Requirements: L2-RDR-009
#[test]
fn delta_tracker_per_rt_msg_key() {
    let mut bytes = Vec::new();
    bytes.extend(record_rt15_sa11_rcv()); // RT15 SA11 R
    bytes.extend(record_rt15_sa11_rcv()); // RT15 SA11 R again — should yield non-zero delta
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 2);
    assert_eq!(msgs[0].delta, Some(0.0));
    // Same timestamp in both fixtures → delta should be exactly 0.0 (not negative)
    assert_eq!(msgs[1].delta, Some(0.0));
}

/// Requirements: L2-FLT-001
#[test]
fn filtering_drops_excluded_rts() {
    let mut bytes = Vec::new();
    bytes.extend(record_rt15_sa11_rcv());
    bytes.extend(record_rt15_sa22_xmt());
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();

    let cfg = FilterConfig {
        exclude_subaddresses: vec![11],
        ..Default::default()
    };
    let msgs: Vec<_> = reader
        .iter()
        .filter_messages(cfg)
        .collect::<Result<_, _>>()
        .unwrap();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0].command_word.unwrap().subaddress, 22);
}

/// Requirements: L2-WRT-001
#[test]
fn csv_output_has_one_row_per_message_plus_header() {
    use aero1553::writer::WriteOptions;
    let mut bytes = Vec::new();
    bytes.extend(record_rt15_sa11_rcv());
    bytes.extend(record_rt15_sa22_rcv());
    bytes.extend(record_rt15_sa22_xmt());
    let f = TempFile::new(&bytes);

    let out_path = std::env::temp_dir().join(format!("mie-int-out-{}.csv", std::process::id()));
    let reader = MieFileReader::new(f.path()).unwrap();
    let n = write_csv(reader.iter(), Some(&out_path), WriteOptions::default())
        .unwrap()
        .normal_count;
    assert_eq!(n, 3);

    let csv = std::fs::read_to_string(&out_path).unwrap();
    let lines: Vec<&str> = csv.lines().collect();
    assert_eq!(lines.len(), 4); // 1 header + 3 data
    assert!(lines[0].starts_with("TIME_STAMP,RT,MSG"));
    // Each data line begins with timestamp + RT 15
    for row in &lines[1..] {
        assert!(row.contains(",15,"), "row missing RT15: {row}");
    }
    let _ = std::fs::remove_file(&out_path);
}

/// Requirements: L2-SYN-015
#[test]
fn corrupt_irig_record_skipped_by_per_record_validation() {
    // Regression test for the validation-parity fix: a record that
    // passes the coarse 3-check filter (valid type, valid word_count,
    // fits in file) but has an out-of-range IRIG hour (31 > 23) must
    // be rejected by per-record validation and skipped via sync
    // recovery — not emitted as a garbage row.
    let mut corrupt = record_rt15_sa11_rcv();
    // Byte 2 is the low byte of the IRIG upper word (LE). The hour
    // field is bits 0..4 of that word. Setting it to 0x1F makes
    // hour = 31, which violates `hour < 24`. The two-record look-ahead
    // would still pass (the next record is valid), so the IRIG range
    // check is the sole discriminator here.
    corrupt[2] = (corrupt[2] & 0xE0) | 0x1F;

    let mut bytes = Vec::new();
    bytes.extend(corrupt); // corrupt-IRIG record (offset 0)
    bytes.extend(record_rt15_sa11_rcv()); // valid record (offset 72)
    let f = TempFile::new(&bytes);

    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();

    // The corrupt record should be dropped via sync recovery; only the
    // valid one is emitted.
    assert_eq!(msgs.len(), 1, "expected corrupt-IRIG record to be skipped");
    assert_eq!(
        msgs[0].file_offset, 72,
        "expected the valid record at offset 72"
    );
}

/// Requirements: L2-SYN-022
#[test]
fn payload_capacity_mismatch_skipped_in_lenient_mode() {
    // Originally a regression test for payload-extraction overrun: a
    // record whose Type Word claims wc=5 but whose Command Word
    // declares data_word_count=30 used to let extract_payload consume
    // bytes from the next record. The extract_payload bounding (Phase
    // 2-era) plus the new L2-SYN-022 capacity check (Phase 7a)
    // both defend against this. The capacity check now fires first:
    // in lenient mode the bad record is logged and skipped before
    // extract_payload runs.
    //
    // This test pins the lenient-mode behavior end-to-end. The strict
    // case is covered by a per-impl unit test and a conformance
    // fixture.
    let mut record_a = Vec::with_capacity(10);
    record_a.extend_from_slice(&0x0502u16.to_le_bytes()); // TW: type 0x02, wc=5
    record_a.extend_from_slice(&0x002Au16.to_le_bytes()); // IRIG upper (day=1, hour=10)
    record_a.extend_from_slice(&0x51E0u16.to_le_bytes()); // IRIG middle
    record_a.extend_from_slice(&0u16.to_le_bytes()); // IRIG lower
    record_a.extend_from_slice(&0x283Eu16.to_le_bytes()); // Cmd: rt=5 R sa=1 dwc=30
    assert_eq!(record_a.len(), 10);

    let mut bytes = Vec::new();
    bytes.extend(&record_a);
    bytes.extend(record_rt15_sa11_rcv()); // Record B at offset 10
    assert_eq!(bytes.len(), 82);

    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();

    // Record A is rejected by L2-SYN-022 (wc=5 < 1+3+1+31=36).
    // Lenient mode WARN+skips it and continues. Only Record B emits.
    assert_eq!(msgs.len(), 1);
    let m = &msgs[0];
    assert_eq!(m.file_offset, 10);
    assert_eq!(m.command_word.unwrap().rt, 15);
    assert_eq!(m.command_word.unwrap().subaddress, 11);
    assert_eq!(m.data_words.len(), 30);
    assert_eq!(m.status_word, Some(0x7800));
}

/// Requirements: L2-SYN-011, L1-EXIT-002
#[test]
fn non_mie_file_surfaces_error_not_silent_zero_messages() {
    // Regression test for the team's "Cargo.toml" reproducer: passing a
    // non-MIE file (this fixture mimics a TOML manifest) used to silently
    // produce zero messages and exit successfully. The fix surfaces a
    // NoValidRecords error from the iterator so `count` and `decode`
    // return non-zero exit codes and tell the user what went wrong.
    let toml = b"[package]\nname = \"aero1553\"\nversion = \"1.0.0\"\nedition = \"2024\"\n\n[dependencies]\nmemmap2 = \"0.9\"\n";
    // Pad with 0xFF so the rest of the file can't coincidentally form
    // a valid Type Word (low byte 0xFF & 0x7F = 0x7F, not in the
    // valid type set). Padding with spaces would not work — pairs of
    // 0x20 0x20 happen to parse as valid SPURIOUS_DATA Type Words with
    // word_count=32, which is a real surprise about how permissive the
    // 5-check heuristic is on highly regular inputs.
    let mut bytes = toml.to_vec();
    bytes.resize(1024, 0xFF);
    let f = TempFile::new(&bytes);

    let reader = MieFileReader::new(f.path()).unwrap();
    let collected: Result<Vec<_>, _> = reader.iter().collect();

    match collected {
        Err(e) => {
            assert!(
                e.to_string().contains("No valid MIE records"),
                "expected NoValidRecords-shaped error, got: {e}"
            );
        }
        Ok(msgs) => panic!(
            "expected an error on a non-MIE file, but got {} message(s)",
            msgs.len()
        ),
    }
}

/// Requirements: L2-SYN-006
#[test]
fn header_skip_via_proprietary_prefix() {
    let mut bytes = Vec::with_capacity(32 + 72);
    bytes.extend_from_slice(b"DDC-EQUIPMENT-NAME\0\0PADD\0\0\0\0\0\0"); // 28 bytes — 14 words
    let header_len = bytes.len();
    bytes.extend(record_rt15_sa11_rcv());
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader.iter().collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0].file_offset, header_len as u64);
}

// ── L1-ROB-001 fuzz harness ──────────────────────────────────────────────
//
// Deterministic xorshift64 PRNG keeps the tests fully reproducible and avoids
// pulling in `rand` (the crate stays at a single external dep, per L3-RS-002).
// Every iteration generates a random byte sequence, writes it to a temp file,
// drives it through the decoder, and asserts that ANY outcome other than a
// panic is acceptable — `Err(MieError::*)` items are the *expected* response to
// random bytes; we just need to confirm we never panic, segfault, or enter an
// unbounded loop.
//
// THREE KNOBS, SHARED WITH THE PYTHON AND C++ HARNESSES. All three
// implementations read the same environment variables and mean the same thing
// by them. That matters because a burn-in scoped differently per language
// cannot be compared across languages, which is the whole subject of
// `docs/FUZZING.md` section 1:
//
//   MIE_FUZZ_ITERATIONS   inputs to generate (default 256)
//   MIE_FUZZ_STREAM_LOGS  `1` / `true` -> leave the decoder's logger at WARN so
//                         its diagnostics stream; anything else -> Level::Off
//   MIE_FUZZ_SUMMARY      file to append this run's FUZZ-SUMMARY line to
//
// WHY THE LOG KNOB IS THE HARNESS'S JOB AND NOT THE RUNNER'S. This crate's
// logger writes through `std::io::stderr()`, and libtest's capture only
// intercepts the `print!` / `eprint!` macros — so `--nocapture` does not
// control it in either direction. Before this knob existed a scheduled burn-in
// streamed tens of megabytes of WARN lines into every CI run whether or not
// anyone had asked for them, while the Python job (which pytest *can* capture)
// showed nothing at all. Neither job was answering the question the workflow
// input claimed to ask.
//
// Setting the level is process-global and NOT restored afterwards, which is
// deliberate. libtest runs this binary's tests in parallel threads, so a scope
// guard would restore the level while a sibling test was still running — the
// race would be worse than the leak. Both fuzz harnesses set the same value,
// no other test in this binary asserts on logging, and the unit tests that DO
// (`src/log.rs`) build into a separate binary. The C++ harness is the opposite
// case and does use a guard: Catch2 runs its cases sequentially in one process,
// and leaving the level at OFF there broke `test_log.cpp` two files away.

// Module scope rather than inside the harness body: `order_rows` is called from
// within a `catch_unwind` closure, and an inner `use` after a statement trips
// `clippy::items_after_statements` (the pedantic group is denied crate-wide).
use aero1553::order::OrderIterExt;

/// The seed every fuzz harness in every implementation starts from.
const FUZZ_SEED: u64 = 0x0DDC_D1EC_DDC0_DEC0;

/// The shared generator: xorshift64, byte-for-byte the one in
/// `python/tests/test_e2e.py` and `cpp/tests/test_fuzz.cpp`. Not a good PRNG; a
/// *reproducible* one, which is the property that matters here.
fn fuzz_xorshift64(state: &mut u64) -> u64 {
    let mut x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    x
}

/// `MIE_FUZZ_ITERATIONS`, or `default`. A value that does not parse, or parses
/// to zero, falls back rather than guessing — same rule in all three harnesses.
///
/// The default is per-harness because the harnesses cost different amounts per
/// input. Every implementation uses the same default for the same harness,
/// which is what keeps the summary lines comparable.
fn fuzz_iterations_or(default: usize) -> usize {
    std::env::var("MIE_FUZZ_ITERATIONS")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|&n| n > 0)
        .unwrap_or(default)
}

/// Silence the decoder's logger unless `MIE_FUZZ_STREAM_LOGS` asks for it.
fn fuzz_configure_logging() {
    let stream = std::env::var("MIE_FUZZ_STREAM_LOGS")
        .is_ok_and(|v| matches!(v.as_str(), "1" | "true" | "TRUE"));
    aero1553::log::set_level(if stream {
        aero1553::log::Level::Warn
    } else {
        aero1553::log::Level::Off
    });
}

/// Fill `size` bytes from the shared generator: eight at a time little-endian,
/// then one at a time for the tail.
///
/// The *consumption order* is as much a part of the contract as the PRNG —
/// Python and C++ draw from the stream identically, which is what makes
/// iteration N the same bytes in all three implementations.
fn fuzz_bytes(state: &mut u64, size: usize) -> Vec<u8> {
    let mut bytes = vec![0u8; size];
    let mut j = 0;
    while j + 8 <= size {
        bytes[j..j + 8].copy_from_slice(&fuzz_xorshift64(state).to_le_bytes());
        j += 8;
    }
    while j < size {
        bytes[j] = (fuzz_xorshift64(state) & 0xFF) as u8;
        j += 1;
    }
    bytes
}

/// Emit this harness's one-line summary.
///
/// The line has the same shape in all three implementations, so the burn-in
/// jobs produce artifacts that can be *diffed* rather than three
/// differently-shaped pass messages ("1 passed" vs "2 passed" vs "484 test
/// cases"). On identical inputs the counters should be identical too; a
/// difference is a cross-implementation finding, and comparing them is the
/// cheap precursor to a real differential driver (`docs/FUZZING.md` 6.1).
///
/// Written to stderr — which libtest does not capture, for the same reason it
/// does not capture the logger — and appended to `MIE_FUZZ_SUMMARY` when that
/// names a file, because a file survives a job's log truncation.
fn fuzz_summary(harness: &str, iterations: usize, fields: &str) {
    let line = format!(
        "FUZZ-SUMMARY impl=rust harness={harness} seed=0x{FUZZ_SEED:016X} \
         iterations={iterations} {fields}"
    );
    let _ = std::io::Write::write_fmt(&mut std::io::stderr().lock(), format_args!("{line}\n"));
    if let Ok(path) = std::env::var("MIE_FUZZ_SUMMARY")
        && let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
    {
        let _ = writeln!(file, "{line}");
    }
}

/// What the reader harness counts. Every field is defined so that Python and
/// C++ can count the same thing on the same input.
#[derive(Default, Clone, Copy)]
struct ReaderFuzzCounts {
    opened: u64,
    records: u64,
    iter_errors: u64,
}

/// Decode one fuzz input and tally what it produced.
///
/// Split out of `fuzz_arbitrary_bytes_never_panic` so that harness reads as
/// generate / run / tally rather than nesting the whole decode inside a
/// `catch_unwind` closure inside the iteration loop. The extraction is what
/// keeps the harness's own control flow simple enough to audit -- a fuzz
/// harness whose bookkeeping is hard to follow is one whose counters are hard
/// to trust, and those counters are what `compare-fuzz-summaries.py` diffs
/// across implementations.
///
/// `iter_index` and `size` are carried in only for the unbounded-loop assertion
/// message, which has to name the input that tripped it to be reproducible.
fn fuzz_reader_counts(path: &Path, size: usize, iter_index: usize) -> ReaderFuzzCounts {
    let mut counts = ReaderFuzzCounts::default();
    // Reader construction itself may fail on FileEmpty etc. — that's a
    // documented error path, not a panic.
    let Ok(reader) = MieFileReader::new(path) else {
        return counts;
    };
    counts.opened = 1;
    // The canonical-order stage (L2-WRT-021) is on the fuzzed path
    // deliberately: random bytes readily decode to repeated or all-zero
    // timestamps, which is exactly the equal-timestamp run its `max_sort_group`
    // cap (L2-WRT-022) exists to bound. A small cap is used so the
    // cap-overflow branch is reachable at all.
    let mut yielded = 0u64;
    for item in reader.iter().order_rows(8) {
        // We accept any Result; we just must not panic.
        if item.is_ok() {
            counts.records += 1;
        } else {
            counts.iter_errors += 1;
        }
        yielded += 1;
        // Cap iteration count as a defense-in-depth bound: if the iterator
        // somehow enters an unbounded loop, this surfaces it as a failed
        // assertion rather than hanging the runner.
        assert!(
            yielded < 100_000,
            "iterator yielded over 100k items on a {size}-byte input — \
             possible unbounded loop (seed=0x{FUZZ_SEED:X}, iter={iter_index})"
        );
    }
    counts
}

/// Requirements: L1-ROB-001
#[test]
fn fuzz_arbitrary_bytes_never_panic() {
    fuzz_configure_logging();

    let mut state = FUZZ_SEED;
    let iterations = fuzz_iterations_or(256);
    let mut total_bytes = 0u64;
    let mut totals = ReaderFuzzCounts::default();

    for i in 0..iterations {
        // Sizes range from 32 B (slightly above MIN_RECORD_BYTES_STANDARD) to
        // ~8 KB. The lower bound keeps record headers reachable; the upper
        // bound keeps each iteration fast.
        let size = 32 + usize::try_from(fuzz_xorshift64(&mut state) % 8192).unwrap_or(0);
        let bytes = fuzz_bytes(&mut state, size);
        total_bytes += u64::try_from(size).unwrap_or(0);

        let f = TempFile::new(&bytes);

        // Use catch_unwind so an unexpected panic is surfaced with the
        // reproducer seed instead of bringing down the whole test process at
        // the first failure.
        let result = std::panic::catch_unwind(|| fuzz_reader_counts(f.path(), size, i));

        match result {
            Ok(counts) => {
                totals.opened += counts.opened;
                totals.records += counts.records;
                totals.iter_errors += counts.iter_errors;
            }
            Err(_) => {
                // Emit what we have before failing: the summary is how a
                // burn-in is compared, and a run that died at iteration 24 000
                // still has 24 000 iterations' worth of evidence in it.
                fuzz_summary(
                    "reader",
                    iterations,
                    &format!(
                        "bytes={total_bytes} opened={} open_errors={} records={} \
                         iter_errors={} outcome=panic",
                        totals.opened,
                        u64::try_from(i).unwrap_or(0) - totals.opened,
                        totals.records,
                        totals.iter_errors
                    ),
                );
                panic!(
                    "MieFileReader panicked on random input (seed=0x{FUZZ_SEED:X}, iter={i}, \
                     size={size}). First 32 bytes: {:02X?}",
                    &bytes[..bytes.len().min(32)]
                );
            }
        }
    }

    fuzz_summary(
        "reader",
        iterations,
        &format!(
            "bytes={total_bytes} opened={} open_errors={} records={} iter_errors={} outcome=ok",
            totals.opened,
            u64::try_from(iterations).unwrap_or(0) - totals.opened,
            totals.records,
            totals.iter_errors
        ),
    );
}

/// L1-ROB-001 for the `dump` subcommand: the record-aware and raw hex dumps
/// must tolerate arbitrary bytes without panicking. The record dump's header
/// reads use `read_u16(...).unwrap_or(0)`, a `checked_add` loop guard, and
/// slice to the record extent for the body — it never reads payload by a
/// Command Word's `data_word_count`, so it has no over-claim/overrun class
/// like the reader's `extract_payload`. This test guards that property
/// against regression. Sizes are skewed small to exercise the truncation /
/// loop-guard paths densely. Mirrors the Python
/// `test_dump_arbitrary_bytes_never_raise_unexpected_exceptions`.
///
/// Both dumps are attempted on every input, and each is counted separately, so
/// the summary line means the same thing as Python's on the same bytes.
///
/// Output volume is counted in LINES, not bytes. Both dumps print the input
/// path in their header, and the harnesses name their temp files differently in
/// each implementation — so a byte count is a measure of the path, not of the
/// decoder, and the two could never agree. Lines are path-independent.
/// Requirements: L1-ROB-001, L2-CLI-009
#[test]
fn dump_arbitrary_bytes_never_panics() {
    fuzz_configure_logging();

    let mut state = FUZZ_SEED; // same seed family as the reader harness
    let iterations = fuzz_iterations_or(256);
    let mut total_bytes = 0u64;
    let (mut records_errors, mut records_lines) = (0u64, 0u64);
    let (mut raw_errors, mut raw_lines) = (0u64, 0u64);

    for i in 0..iterations {
        // Modulo first, so the value provably fits a usize before conversion.
        let size = usize::try_from(fuzz_xorshift64(&mut state) % 512).unwrap_or(0);
        let bytes = fuzz_bytes(&mut state, size);
        total_bytes += u64::try_from(size).unwrap_or(0);

        let f = TempFile::new(&bytes);
        let result = std::panic::catch_unwind(|| {
            // Both dumps may return Err (e.g. FileEmpty) — a documented error
            // path, not a panic. We sink output into a Vec and discard it,
            // clearing between the two so each dump's output size is its own.
            // clippy::naive_bytecount suggests the `bytecount` crate. This crate
            // has exactly one external dependency by policy (L3-RS-002), and a
            // line count in a test harness is nowhere near a reason to add a
            // second — the fuzzed dumps here are at most a few hundred bytes.
            #[allow(clippy::naive_bytecount)]
            let count_lines = |sink: &[u8]| {
                u64::try_from(sink.iter().filter(|&&b| b == b'\n').count()).unwrap_or(0)
            };
            let mut sink = Vec::new();
            let records = aero1553::dump::hex_dump_records(f.path(), Some(64), 0, &mut sink);
            let records_len = count_lines(&sink);
            sink.clear();
            let raw = aero1553::dump::hex_dump_raw(f.path(), 0, None, &mut sink);
            let raw_len = count_lines(&sink);
            (
                u64::from(records.is_err()),
                records_len,
                u64::from(raw.is_err()),
                raw_len,
            )
        });

        match result {
            Ok((rec_err, rec_len, raw_err, raw_len)) => {
                records_errors += rec_err;
                records_lines += rec_len;
                raw_errors += raw_err;
                raw_lines += raw_len;
            }
            Err(_) => {
                fuzz_summary(
                    "dump",
                    iterations,
                    &format!(
                        "bytes={total_bytes} records_errors={records_errors} \
                         records_lines={records_lines} raw_errors={raw_errors} \
                         raw_lines={raw_lines} outcome=panic"
                    ),
                );
                panic!(
                    "dump panicked on random input (seed=0x{FUZZ_SEED:X}, iter={i}, size={size}). \
                     First 32 bytes: {:02X?}",
                    &bytes[..bytes.len().min(32)]
                );
            }
        }
    }

    fuzz_summary(
        "dump",
        iterations,
        &format!(
            "bytes={total_bytes} records_errors={records_errors} records_lines={records_lines} \
             raw_errors={raw_errors} raw_lines={raw_lines} outcome=ok"
        ),
    );
}

// ── L1-MRG / L2-MRG: multi-file time-sorted merge ─────────────────────────

/// An RT15 SA11 Receive record placed at a chosen IRIG instant, by patching
/// the timestamp triple of the canonical fixture (bytes 2..8 = the three IRIG
/// timestamp words). Lets merge tests position records at specific times.
fn rt15_record_at(
    day: u16,
    hour: u8,
    minute: u8,
    second: u8,
    micro: u32,
    freerun: bool,
) -> Vec<u8> {
    let mut rec = record_rt15_sa11_rcv();
    let fr = u16::from(freerun) << 15;
    let upper: u16 = fr | ((day & 0x1FF) << 5) | u16::from(hour & 0x1F);
    let middle: u16 = (u16::from(minute & 0x3F) << 10)
        | (u16::from(second & 0x3F) << 4)
        | ((micro >> 16) as u16 & 0xF);
    let lower: u16 = (micro & 0xFFFF) as u16;
    rec[2..4].copy_from_slice(&upper.to_le_bytes());
    rec[4..6].copy_from_slice(&middle.to_le_bytes());
    rec[6..8].copy_from_slice(&lower.to_le_bytes());
    rec
}

/// Requirements: L1-MRG-001, L2-MRG-002, L2-MRG-005
#[test]
fn merge_orders_records_across_files_by_absolute_time() {
    use aero1553::merge::MergedRecordIter;

    // File A: t=100µs, 300µs. File B: t=200µs, 400µs. Same day/h/m/s so the
    // microsecond field is the discriminator; merged order must interleave.
    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let b = [
        rt15_record_at(192, 15, 54, 50, 200, false),
        rt15_record_at(192, 15, 54, 50, 400, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];

    let merged = MergedRecordIter::new(&readers, None, false, false).unwrap();
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 4, "all four records survive the merge");

    // Absolute microseconds include the day/hour/min/sec base; the proof of a
    // correct interleave (A:100,300 + B:200,400 → 100,200,300,400) is that the
    // merged keys are strictly increasing.
    let us: Vec<u64> = msgs
        .iter()
        .map(|m| m.timestamp.to_microseconds(None).unwrap())
        .collect();
    assert!(
        us.windows(2).all(|w| w[0] < w[1]),
        "merged stream is not strictly time-ordered: {us:?}"
    );

    // Global DELTA (L2-MRG-005): first occurrence 0.0, then non-negative gaps
    // on the unified timeline (all four share one RT/SA/dir key).
    assert_eq!(msgs[0].delta, Some(0.0));
    for m in &msgs[1..] {
        assert!(m.delta.unwrap() >= 0.0);
    }
}

/// `new_detached` merges exactly what `new` merges, and its iterator outlives
/// the readers: it shares each file's mapping instead of borrowing the reader,
/// which is what the Python binding needs to keep a merge inside an object.
/// Requirements: L2-MRG-002
#[test]
fn detached_merge_matches_new_and_outlives_the_readers() {
    use aero1553::merge::MergedRecordIter;

    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let b = [
        rt15_record_at(192, 15, 54, 50, 200, false),
        rt15_record_at(192, 15, 54, 50, 400, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);
    let open = || {
        vec![
            MieFileReader::new(fa.path()).unwrap(),
            MieFileReader::new(fb.path()).unwrap(),
        ]
    };

    let readers = open();
    let borrowed: Vec<_> = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();

    let readers = open();
    let detached = MergedRecordIter::new_detached(&readers, None, false, false).unwrap();
    drop(readers);
    let owned: Vec<_> = detached.collect::<Result<_, _>>().unwrap();

    assert_eq!(borrowed.len(), 4);
    assert_eq!(owned, borrowed);
}

/// Requirements: L2-MRG-001
#[test]
fn merge_single_input_is_unchanged() {
    use aero1553::merge::MergedRecordIter;
    // A one-file "merge" yields exactly the file's records, in order.
    let a = [
        rt15_record_at(192, 15, 54, 50, 10, false),
        rt15_record_at(192, 15, 54, 50, 20, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let readers = vec![MieFileReader::new(fa.path()).unwrap()];
    let merged = MergedRecordIter::new(&readers, None, false, false).unwrap();
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 2);
}

/// Requirements: L1-MRG-002, L2-MRG-003
#[test]
fn merge_rejects_freerun_leading_input() {
    use aero1553::error::MieErrorKind;
    use aero1553::merge::MergedRecordIter;

    let good = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    // Leading record carries the freerun bit → no calendar time.
    let freerun = [
        rt15_record_at(0, 0, 0, 0, 0, true),
        rt15_record_at(0, 0, 0, 1, 0, true),
    ]
    .concat();
    let fa = TempFile::new(&good);
    let fb = TempFile::new(&freerun);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    match MergedRecordIter::new(&readers, None, false, false) {
        Err(e) => assert_eq!(e.kind(), MieErrorKind::IncompatibleMergeInputs),
        Ok(_) => panic!("expected IncompatibleMergeInputs for a freerun-leading input"),
    }
}

/// Requirements: L1-MRG-002, L2-MRG-003
#[test]
fn merge_rejects_standard_format_input() {
    use aero1553::error::MieErrorKind;
    use aero1553::merge::MergedRecordIter;
    use aero1553::models::TimestampFormat;
    use aero1553::reader::ReaderOptions;

    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    // Forcing the Standard timestamp format makes the records decode as
    // Standard timestamps, which carry no shared epoch → not mergeable.
    let readers = vec![
        MieFileReader::with_options(
            fa.path(),
            ReaderOptions {
                input_time_format: TimestampFormat::Standard,
                ..Default::default()
            },
        )
        .unwrap(),
        MieFileReader::with_options(
            fa.path(),
            ReaderOptions {
                input_time_format: TimestampFormat::Standard,
                ..Default::default()
            },
        )
        .unwrap(),
    ];
    match MergedRecordIter::new(&readers, None, false, false) {
        Err(e) => assert_eq!(e.kind(), MieErrorKind::IncompatibleMergeInputs),
        Ok(_) => panic!("expected IncompatibleMergeInputs for a Standard-format input"),
    }
}

/// Requirements: L2-MRG-001
#[test]
fn read_manifest_skips_blanks_and_comments() {
    let body = "# a comment\n\nfile1.mie\n  file2.mie  \n# another\nfile3.mie\n";
    let f = TempFile::new(body.as_bytes());
    let paths = aero1553::merge::read_manifest(f.path()).unwrap();
    assert_eq!(
        paths,
        vec![
            PathBuf::from("file1.mie"),
            PathBuf::from("file2.mie"),
            PathBuf::from("file3.mie"),
        ]
    );
}

/// The manifest grammar, pinned exactly, because leaving it to "one path per
/// line" is how three implementations came to disagree three different ways.
///
/// All three were found by the merge fuzz harness comparing its `FUZZ-SUMMARY`
/// counters, and all three are now spelled out in L2-MRG-001:
///
///   * `\n` is the ONLY line separator. Python used `str.splitlines()`, which
///     also breaks on vertical tab, form feed, U+0085 and U+2028/9 — none of
///     which ends a line in a manifest, all of which are legal in a POSIX
///     filename. One file became two nonexistent ones.
///   * At most ONE trailing `\r` is stripped, so CRLF works and a filename
///     containing a bare CR survives. C++ dropped every `\r` in the line.
///   * Trimming is ASCII space and tab ONLY. `str::trim` also removes U+00A0,
///     U+3000 and the rest; the C++ implementation is locale-free by rule and
///     cannot, so two implementations silently edited a filename the third
///     passed through.
///
/// Requirements: L2-MRG-001
#[test]
fn read_manifest_grammar_is_exactly_specified() {
    let read = |body: &[u8]| {
        let f = TempFile::new(body);
        aero1553::merge::read_manifest(f.path())
    };

    // Only `\n` separates. A form feed is part of the filename.
    assert_eq!(
        read(b"a.mie\x0cb.mie\n").unwrap(),
        vec![PathBuf::from("a.mie\x0cb.mie")]
    );
    // ... and so are VT and (as UTF-8) U+0085 / U+2028.
    assert_eq!(
        read(b"a.mie\x0bb.mie\n").unwrap(),
        vec![PathBuf::from("a.mie\x0bb.mie")]
    );
    assert_eq!(
        read("a.mie\u{85}b.mie\n".as_bytes()).unwrap(),
        vec![PathBuf::from("a.mie\u{85}b.mie")]
    );

    // One trailing CR is the CRLF terminator; an interior CR is a filename.
    assert_eq!(
        read(b"a.mie\r\nb.mie\r\n").unwrap(),
        vec![PathBuf::from("a.mie"), PathBuf::from("b.mie")]
    );
    assert_eq!(
        read(b"a\rb.mie\n").unwrap(),
        vec![PathBuf::from("a\rb.mie")]
    );
    // ... and the LAST line counts as a line even without its terminator. This
    // reader used `str::lines()`, which strips the CR only when an `\n`
    // actually followed it, so `"\r"` was a one-character path here and no path
    // at all in Python and C++.
    assert_eq!(
        read(b"a.mie\r\nb.mie\r").unwrap(),
        vec![PathBuf::from("a.mie"), PathBuf::from("b.mie")]
    );
    assert_eq!(read(b"\r").unwrap(), Vec::<PathBuf>::new());

    // ASCII blanks are trimmed; Unicode spaces are part of the name.
    assert_eq!(
        read(b" \ta.mie\t \n").unwrap(),
        vec![PathBuf::from("a.mie")]
    );
    assert_eq!(
        read("\u{a0}a.mie\n".as_bytes()).unwrap(),
        vec![PathBuf::from("\u{a0}a.mie")]
    );

    // A manifest is a text file: ill-formed UTF-8 is refused, not decoded.
    assert!(read(b"\xff\xfe\na.mie\n").is_err());
}

/// The glob-pattern alphabet the merge fuzz harness draws from, shared with
/// `python/tests/fuzz_support.py` and `cpp/tests/test_fuzz.cpp`.
///
/// A pattern built by lossily UTF-8-decoding random bytes would be the obvious
/// thing, and it is wrong twice over. Random bytes almost never contain `*` or
/// `?`, so the matcher's interesting branches are never reached; and the three
/// languages' lossy decoders do not agree character-for-character on how many
/// U+FFFD an invalid sequence produces, so the counters could diverge without
/// the glob matchers disagreeing about anything.
///
/// Drawing from an alphabet fixes both. The last two entries are deliberately
/// non-ASCII: Rust and Python match over scalar values and the C++ matcher
/// advances `?` by a whole UTF-8 character, and this is the surface where that
/// agreement is either real or it is not.
///
/// `*` and `?` are weighted (three and two slots) because the first version of
/// this harness drew uniformly from a 15-character alphabet over patterns up to
/// 95 characters long and matched a probe **zero** times in 512 iterations —
/// which is to say it fuzzed the matcher's reject path and nothing else. Short,
/// wildcard-heavy patterns are what reach the interesting branches.
///
/// `/` is there so `expand_glob` sees a directory part at all: without it every
/// pattern was a bare filename, and neither the textual split at the last
/// separator nor the directory-wildcard refusal (L2-MRG-001) was ever reached.
/// `/` and not `\`: a forward slash is a separator on every platform, while a
/// backslash is one only on Windows, so it would split the same pattern
/// differently on the two CI hosts and the counters would legitimately differ.
const GLOB_ALPHABET: [&str; 16] = [
    "*", "*", "*", "?", "?", ".", "a", "b", "m", "i", "e", "-", "x", "\u{e9}", "\u{4e2d}", "/",
];

/// Probe names the generated patterns are matched against. ASCII, Latin-1 and
/// CJK, counted separately so a divergence says which one broke.
const GLOB_PROBES: [&str; 3] = ["some.name.mie", "caf\u{e9}.mie", "\u{4e2d}\u{6587}.mie"];

/// L1-ROB-001 for the merge input-resolution surface: a manifest of arbitrary
/// bytes, and an arbitrary glob pattern driven through the matcher and the
/// directory expansion, must never panic — only return Ok/Err (or a bool).
///
/// `expand_glob` is called for crash-safety only and its result is deliberately
/// NOT counted: it reads the working directory, so what it returns depends on
/// where the suite ran, and a summary field has to mean the same thing in every
/// implementation on every host.
/// Requirements: L1-ROB-001, L2-MRG-001
#[test]
fn merge_input_resolution_tolerates_arbitrary_bytes() {
    fuzz_configure_logging();

    let mut state = FUZZ_SEED;
    // 512 by default rather than the reader harness's 256: each iteration is
    // cheap (no decode, no mmap) and the glob matcher has more branches than
    // this many inputs comfortably cover. The shared knob still overrides.
    let iterations = fuzz_iterations_or(512);

    let mut counts = MergeFuzzCounts::default();

    for i in 0..iterations {
        let size = usize::try_from(fuzz_xorshift64(&mut state) % 96).unwrap_or(0);
        let bytes = fuzz_bytes(&mut state, size);
        counts.total_bytes += u64::try_from(size).unwrap_or(0);

        // The pattern is drawn separately from the manifest bytes, and short:
        // the two surfaces want different input shapes, and deriving one from
        // the other means neither gets the shape it needs.
        let pattern_len = usize::try_from(fuzz_xorshift64(&mut state) % 12).unwrap_or(0);
        let pattern_bytes = fuzz_bytes(&mut state, pattern_len);

        let f = TempFile::new(&bytes);
        let result = std::panic::catch_unwind(|| {
            // read_manifest: Ok (parsed lines) or Err (non-UTF8) — never panic.
            let manifest = aero1553::merge::read_manifest(f.path());
            let (ok, errs, paths) = match &manifest {
                Ok(list) => (1u64, 0u64, u64::try_from(list.len()).unwrap_or(0)),
                Err(_) => (0, 1, 0),
            };

            let pattern: String = pattern_bytes
                .iter()
                .map(|b| GLOB_ALPHABET[usize::from(*b) % GLOB_ALPHABET.len()])
                .collect();
            let mut hits = [0u64; GLOB_PROBES.len()];
            for (slot, probe) in hits.iter_mut().zip(GLOB_PROBES) {
                *slot = u64::from(aero1553::merge::glob_match(&pattern, probe));
            }
            // What it returns is crash-safety only (see the doc comment), but
            // WHETHER it refuses the pattern as a directory wildcard is decided
            // before any I/O, from the pattern's text alone -- so that is
            // comparable across implementations and hosts.
            let refused = u64::from(matches!(
                aero1553::merge::expand_glob(&pattern),
                Err(e) if e.kind() == std::io::ErrorKind::InvalidInput
            ));
            (ok, errs, paths, hits, refused)
        });

        match result {
            Ok((ok, errs, paths, hits, refused)) => {
                counts.manifest_ok += ok;
                counts.manifest_errors += errs;
                counts.manifest_paths += paths;
                counts.glob_refused += refused;
                for (total, hit) in counts.glob_hits.iter_mut().zip(hits) {
                    *total += hit;
                }
            }
            Err(_) => {
                merge_fuzz_summary(iterations, &counts, "panic");
                panic!(
                    "merge input resolution panicked on random input \
                     (seed=0x{FUZZ_SEED:X}, iter={i}, size={size}). First 32 bytes: {:02X?}",
                    &bytes[..bytes.len().min(32)]
                );
            }
        }
    }

    merge_fuzz_summary(iterations, &counts, "ok");
}

/// The merge harness's running counters, so the success and panic paths hand
/// the summary one value rather than a parameter per field.
#[derive(Default)]
struct MergeFuzzCounts {
    total_bytes: u64,
    manifest_ok: u64,
    manifest_errors: u64,
    manifest_paths: u64,
    glob_hits: [u64; GLOB_PROBES.len()],
    glob_refused: u64,
}

/// Formats the merge harness's summary. Split out so the success and panic
/// paths cannot drift apart in field order or spelling.
fn merge_fuzz_summary(iterations: usize, c: &MergeFuzzCounts, outcome: &str) {
    fuzz_summary(
        "merge",
        iterations,
        &format!(
            "bytes={} manifest_ok={} manifest_errors={} manifest_paths={} \
             glob_ascii={} glob_latin1={} glob_cjk={} glob_refused={} outcome={outcome}",
            c.total_bytes,
            c.manifest_ok,
            c.manifest_errors,
            c.manifest_paths,
            c.glob_hits[0],
            c.glob_hits[1],
            c.glob_hits[2],
            c.glob_refused,
        ),
    );
}

/// L2-MRG-004 / L1-EXIT-004: with --allow-partial, a merge whose input hits an
/// unrecoverable sync loss truncates that file, completes from the rest, and
/// the writer commits the combined output as `.partial`. (Forcing an
/// unrecoverable loss needs >64 KB of non-resyncing garbage, so this is a
/// library test rather than a small conformance hex fixture.)
/// Requirements: L2-MRG-004, L1-EXIT-004
#[test]
fn merge_allow_partial_writes_partial_on_file_failure() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::writer::{WriteOptions, write_csv};

    // File A: good records at 100µs, 300µs. File B: good records at 200µs,
    // 400µs, then 70 KB of 0xFF → recover_sync exhausts the 64 KB window
    // (unrecoverable) after B yields its first record.
    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let mut b = [
        rt15_record_at(192, 15, 54, 50, 200, false),
        rt15_record_at(192, 15, 54, 50, 400, false),
    ]
    .concat();
    b.extend(vec![0xFFu8; 70_000]);
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];

    let merged = MergedRecordIter::new(&readers, None, true, false).unwrap();
    let out = TempFile::new(b"");
    let opts = WriteOptions {
        input_path: None,
        no_clobber: false,
        allow_partial: true,
        time_render: TimeRender::doy(),
    };
    let outcome = write_csv(merged, Some(out.path()), opts).unwrap();
    assert!(
        outcome.partial.is_some(),
        "--allow-partial should commit a .partial on the file failure"
    );
    // A's 100 + B's 200 + A's 300 + the record immediately before B's loss.
    // That last one used to be discarded because its *successor* boundary was
    // corrupt; continuous validation no longer looks ahead (L2-SYN-005), so a
    // well-formed record is no longer lost to its neighbour's damage. Mirrors
    // `test_merge_allow_partial_writes_partial_on_file_failure` in Python.
    assert_eq!(outcome.normal_count, 4);
    let partial = std::path::PathBuf::from(format!("{}.partial", out.path().display()));
    assert!(partial.exists(), "the .partial output file should exist");
    let _ = std::fs::remove_file(&partial);
}

/// L2-MRG-004: a *priming-time* failure — an input whose **first** record is
/// unreadable / non-MIE (here 4 KB of 0xFF) — under `--allow-partial` must arm
/// the deferred terminal so the writer commits a `.partial`, exactly like a
/// mid-file failure. Regression: pre-fix the priming failure was skipped
/// silently and a plain `.csv` was written with `outcome.partial == None`.
/// Requirements: L2-MRG-004
#[test]
fn merge_allow_partial_writes_partial_on_priming_failure() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::writer::{WriteOptions, write_csv};

    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&vec![0xFFu8; 4096]); // no valid first record
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];

    let merged = MergedRecordIter::new(&readers, None, true, false).unwrap();
    let out = TempFile::new(b"");
    let opts = WriteOptions {
        input_path: None,
        no_clobber: false,
        allow_partial: true,
        time_render: TimeRender::doy(),
    };
    let outcome = write_csv(merged, Some(out.path()), opts).unwrap();
    assert!(
        outcome.partial.is_some(),
        "--allow-partial must commit a .partial when an input fails to prime"
    );
    assert_eq!(
        outcome.normal_count, 2,
        "A's two good records reach the writer"
    );
    let partial = std::path::PathBuf::from(format!("{}.partial", out.path().display()));
    assert!(partial.exists(), "the .partial output file should exist");
    let _ = std::fs::remove_file(&partial);
}

/// L2-MRG-004: without `--allow-partial`, a priming-time failure fails the batch
/// (the error surfaces from `new()`); no `.partial` is produced.
/// Requirements: L2-MRG-004
#[test]
fn merge_no_allow_partial_priming_failure_fails_batch() {
    use aero1553::merge::MergedRecordIter;

    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&vec![0xFFu8; 4096]);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let result = MergedRecordIter::new(&readers, None, false, false);
    assert!(
        result.is_err(),
        "a bad input fails the batch without --allow-partial"
    );
}

/// L2-MRG-004: with `--allow-partial`, a merge in which **every** input fails to
/// prime still completes and commits an (empty) `.partial`.
/// Requirements: L2-MRG-004
#[test]
fn merge_allow_partial_all_inputs_bad() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::writer::{WriteOptions, write_csv};

    let fa = TempFile::new(&vec![0xFFu8; 4096]);
    let fb = TempFile::new(&vec![0xFFu8; 4096]);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged = MergedRecordIter::new(&readers, None, true, false).unwrap();
    let out = TempFile::new(b"");
    let opts = WriteOptions {
        input_path: None,
        no_clobber: false,
        allow_partial: true,
        time_render: TimeRender::doy(),
    };
    let outcome = write_csv(merged, Some(out.path()), opts).unwrap();
    assert!(
        outcome.partial.is_some(),
        "an all-bad merge still commits a .partial under --allow-partial"
    );
    assert_eq!(outcome.normal_count, 0, "no good rows survived");
    let partial = std::path::PathBuf::from(format!("{}.partial", out.path().display()));
    assert!(partial.exists());
    let _ = std::fs::remove_file(&partial);
}

/// L2-MRG-004: the priming-time terminal survives the drain even when a later
/// input is good — a bad **first** input (file index 0) plus a good second
/// input still yields a `.partial` carrying the good rows.
/// Requirements: L2-MRG-004
#[test]
fn merge_allow_partial_bad_input_then_good() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::writer::{WriteOptions, write_csv};

    let good = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 300, false),
    ]
    .concat();
    let fa = TempFile::new(&vec![0xFFu8; 4096]); // index 0: bad
    let fb = TempFile::new(&good); // index 1: good
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged = MergedRecordIter::new(&readers, None, true, false).unwrap();
    let out = TempFile::new(b"");
    let opts = WriteOptions {
        input_path: None,
        no_clobber: false,
        allow_partial: true,
        time_render: TimeRender::doy(),
    };
    let outcome = write_csv(merged, Some(out.path()), opts).unwrap();
    assert!(
        outcome.partial.is_some(),
        "a bad first input still forces a .partial"
    );
    assert_eq!(outcome.normal_count, 2, "the good input's rows are kept");
    let partial = std::path::PathBuf::from(format!("{}.partial", out.path().display()));
    assert!(partial.exists());
    let _ = std::fs::remove_file(&partial);
}

/// L2-MRG-006: an input whose records step backward in time (not internally
/// time-sorted) is a data-quality anomaly. In lenient mode the merge WARNs and
/// still emits every record (never re-sorts), so all records survive.
/// Requirements: L2-MRG-006
#[test]
fn merge_warns_on_within_file_backward_step() {
    use aero1553::merge::MergedRecordIter;

    // One file whose microsecond keys step 100 → 200 → 150 (the third record
    // is older than the second): a within-file backward step.
    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 200, false),
        rt15_record_at(192, 15, 54, 50, 150, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let readers = vec![MieFileReader::new(fa.path()).unwrap()];

    let merged = MergedRecordIter::new(&readers, None, false, false).unwrap();
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    // Lenient: advisory only — every record is still emitted (no failure).
    assert_eq!(
        msgs.len(),
        3,
        "lenient mode keeps all records despite the WARN"
    );
}

/// L2-MRG-006: in strict mode the same within-file backward step is a record
/// error that fails the batch (exit-1 class), mirroring how strict already
/// treats per-record / structural-invariant failures.
/// Requirements: L2-MRG-006
#[test]
fn merge_strict_fails_on_within_file_backward_step() {
    use aero1553::error::MieErrorKind;
    use aero1553::merge::MergedRecordIter;

    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 200, false),
        rt15_record_at(192, 15, 54, 50, 150, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let readers = vec![MieFileReader::new(fa.path()).unwrap()];

    // strict = true. The first two records pop cleanly; pulling the backward
    // third record arms a pending error that surfaces as a terminal Err.
    let merged = MergedRecordIter::new(&readers, None, false, true).unwrap();
    let mut saw_err = false;
    for item in merged {
        if let Err(e) = item {
            assert_eq!(e.kind(), MieErrorKind::NonMonotonicInput);
            saw_err = true;
            break;
        }
    }
    assert!(
        saw_err,
        "strict mode should surface a NonMonotonicInput error"
    );
}

/// L2-MRG-007: the same bus transaction witnessed by two recorders — identical
/// wire content at the same microsecond in two *different* input files —
/// collapses to a single row under `--collapse-duplicates`, and the suppressed
/// count is reported.
/// Requirements: L1-MRG-003, L2-MRG-007, L3-RS-015
#[test]
fn merge_collapse_cross_recorder_duplicate() {
    use aero1553::merge::MergedRecordIter;
    use std::sync::atomic::Ordering;

    let rec = rt15_record_at(192, 15, 54, 50, 100, false);
    let fa = TempFile::new(&rec);
    let fb = TempFile::new(&rec); // identical content + timestamp, different file
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collapse(true, 0);
    let collapsed = merged.collapsed_handle();
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(
        msgs.len(),
        1,
        "the second recorder's duplicate is collapsed"
    );
    assert_eq!(collapsed.load(Ordering::Relaxed), 1);
}

/// L2-MRG-007: identical content at *different* timestamps (beyond the window)
/// is real periodic traffic, not a cross-recorder duplicate — both rows survive.
/// Requirements: L2-MRG-007
#[test]
fn merge_collapse_keeps_different_time() {
    use aero1553::merge::MergedRecordIter;

    let fa = TempFile::new(&rt15_record_at(192, 15, 54, 50, 100, false));
    let fb = TempFile::new(&rt15_record_at(192, 15, 54, 50, 300, false));
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collapse(true, 0);
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 2, "distinct timestamps are distinct events");
}

/// L2-MRG-007: identical records from the *same* recorder (same input file) are
/// never collapsed — collapsing is strictly cross-recorder.
/// Requirements: L2-MRG-007
#[test]
fn merge_collapse_same_file_not_collapsed() {
    use aero1553::merge::MergedRecordIter;

    let rec = rt15_record_at(192, 15, 54, 50, 100, false);
    let body = [rec.clone(), rec].concat(); // two identical records, one file
    let fa = TempFile::new(&body);
    let readers = vec![MieFileReader::new(fa.path()).unwrap()];
    let merged = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collapse(true, 0);
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 2, "same-recorder duplicates are kept");
}

/// L2-MRG-007: with a non-zero window, near-simultaneous identical content from
/// two recorders whose clocks differ slightly collapses (3µs skew, 5µs window).
/// Requirements: L2-MRG-007
#[test]
fn merge_collapse_within_window() {
    use aero1553::merge::MergedRecordIter;

    let fa = TempFile::new(&rt15_record_at(192, 15, 54, 50, 100, false));
    let fb = TempFile::new(&rt15_record_at(192, 15, 54, 50, 103, false));
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collapse(true, 5);
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(msgs.len(), 1, "within-window clock skew collapses");
}

/// L2-MRG-006 + L2-MRG-007: collapsing must not panic on a lenient non-monotonic
/// input. A within-file backward timestamp step makes the merged stream step
/// backward, so the dedup window's eviction must not underflow `us - buf_us`.
/// Regression for the debug-build "attempt to subtract with overflow" panic.
/// Requirements: L2-MRG-006, L2-MRG-007
#[test]
fn merge_collapse_survives_lenient_non_monotonic() {
    use aero1553::merge::MergedRecordIter;

    // One file whose microsecond keys step 100 → 200 → 150 (backward at the
    // third record), with collapsing enabled.
    let a = [
        rt15_record_at(192, 15, 54, 50, 100, false),
        rt15_record_at(192, 15, 54, 50, 200, false),
        rt15_record_at(192, 15, 54, 50, 150, false),
    ]
    .concat();
    let fa = TempFile::new(&a);
    let readers = vec![MieFileReader::new(fa.path()).unwrap()];

    // Pre-fix this panicked computing 150 - 200 in the window eviction.
    let merged = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collapse(true, 0);
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    // Single file → nothing is cross-recorder → every record survives.
    assert_eq!(
        msgs.len(),
        3,
        "lenient non-monotonic + collapse keeps all rows without panicking"
    );
}

/// L2-MRG-006 + L2-MRG-007: after a backward timestamp step the stream is no
/// longer monotonic, so a record must only collapse against a survivor within
/// the window in ABSOLUTE time — a one-sided `us - buf_us` would match a
/// survivor far outside `collapse_window_us`. Regression for the over-collapse
/// (and the underflow panic) on non-monotonic input.
/// Requirements: L2-MRG-006, L2-MRG-007
#[test]
fn merge_collapse_no_over_collapse_after_backward_step() {
    use aero1553::merge::MergedRecordIter;
    use std::sync::atomic::Ordering;

    // File A: one record at 1000µs. File B non-monotonic: 1002µs then 10µs.
    // Merged order by absolute time: A@1000, B@1002, B@10 (backward at the end).
    let fa = TempFile::new(&rt15_record_at(192, 15, 54, 50, 1000, false));
    let b = [
        rt15_record_at(192, 15, 54, 50, 1002, false),
        rt15_record_at(192, 15, 54, 50, 10, false),
    ]
    .concat();
    let fb = TempFile::new(&b);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];

    // window = 5µs: B@1002 is a genuine cross-recorder duplicate of A@1000
    // (2µs apart) and collapses; B@10 is 990µs from A@1000 — far outside the
    // window — so it must be KEPT, not collapsed against it.
    let merged = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .collapse(true, 5);
    let collapsed = merged.collapsed_handle();
    let msgs: Vec<_> = merged.collect::<Result<_, _>>().unwrap();
    assert_eq!(
        msgs.len(),
        2,
        "the far backward record is kept, not over-collapsed"
    );
    assert_eq!(
        collapsed.load(Ordering::Relaxed),
        1,
        "only the in-window duplicate (B@1002) collapses"
    );
}

// ── L1-OUT-003 / L2-WRT-021 / L2-WRT-022: canonical row order ──────────────

/// A receive (Type Word 0x02 = `BC_TO_RT`) record for `rt`/`sa` at a chosen
/// microsecond within a fixed day/hour/minute/second, built by patching the
/// canonical fixture's timestamp triple and Command Word. The data-word count
/// stays at the fixture's 30 so the record layout is untouched.
///
/// Type 0x02 requires `Direction::Receive` (a decode.rs structural invariant),
/// so this helper is the "R" side; `xmt_record_at` is the "T" side.
fn rcv_record_at(rt: u8, sa: u8, micro: u32) -> Vec<u8> {
    let mut rec = record_rt15_sa11_rcv();
    patch_irig_micro(&mut rec, micro);
    patch_cmd(&mut rec, rt, sa, false);
    rec
}

/// A transmit (Type Word 0x04 = `RT_TO_BC`) record for `rt`/`sa` at `micro`.
/// Type 0x04 requires `Direction::Transmit`.
fn xmt_record_at(rt: u8, sa: u8, micro: u32) -> Vec<u8> {
    let mut rec = record_rt15_sa22_xmt();
    patch_irig_micro(&mut rec, micro);
    patch_cmd(&mut rec, rt, sa, true);
    rec
}

/// Overwrite the IRIG timestamp triple (bytes 2..8) with day 192, 15:54:50 and
/// the given microsecond, so records built by different helpers can be placed
/// at the same instant.
#[allow(
    clippy::decimal_bitwise_operands,
    reason = "packs wire fields whose values are semantic, not masks: `192` is the day-of-year, `15` the hour of 15:54:50, `30` the documented data-word count. Hex would obscure them, and the lint is inconsistent here anyway -- it flags the hour but not the minute and second in the same expression, because those sit inside shifts."
)]
fn patch_irig_micro(rec: &mut [u8], micro: u32) {
    let upper: u16 = ((192u16 & 0x1FF) << 5) | 15;
    let middle: u16 = (54u16 << 10) | (50u16 << 4) | ((micro >> 16) as u16 & 0xF);
    let lower: u16 = (micro & 0xFFFF) as u16;
    rec[2..4].copy_from_slice(&upper.to_le_bytes());
    rec[4..6].copy_from_slice(&middle.to_le_bytes());
    rec[6..8].copy_from_slice(&lower.to_le_bytes());
}

/// Overwrite the Command Word (bytes 8..10), keeping the fixture's 30-data-word
/// count: `RT<<11 | dir<<10 | SA<<5 | 30`.
#[allow(
    clippy::decimal_bitwise_operands,
    reason = "packs wire fields whose values are semantic, not masks: `192` is the day-of-year, `15` the hour of 15:54:50, `30` the documented data-word count. Hex would obscure them, and the lint is inconsistent here anyway -- it flags the hour but not the minute and second in the same expression, because those sit inside shifts."
)]
fn patch_cmd(rec: &mut [u8], rt: u8, sa: u8, transmit: bool) {
    let cmd: u16 = (u16::from(rt & 0x1F) << 11)
        | (u16::from(transmit) << 10)
        | (u16::from(sa & 0x1F) << 5)
        | 30;
    rec[8..10].copy_from_slice(&cmd.to_le_bytes());
}

/// `(RT, MSG)` pairs as the writer renders them, for order assertions.
fn rt_msg_pairs(msgs: &[aero1553::models::MieMessage]) -> Vec<(Option<u8>, String)> {
    msgs.iter().map(|m| (m.rt(), m.msg_label())).collect()
}

/// All three key levels plus R-before-T, from a deliberately wrong input order,
/// through the library pipeline.
///
/// Requirements: L1-OUT-003, L2-WRT-021
#[test]
fn canonical_order_sorts_tied_rows_by_rt_then_msg() {
    use aero1553::order::OrderIterExt;

    // One instant, four records, input order chosen to violate every key level.
    let bytes = [
        rcv_record_at(21, 3, 500), // highest RT first
        xmt_record_at(3, 11, 500), // T before R at the same SA
        rcv_record_at(3, 11, 500),
        xmt_record_at(3, 2, 500), // SA 2 last, though it sorts before SA 11
    ]
    .concat();
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader
        .iter()
        .order_rows(aero1553::order::DEFAULT_MAX_SORT_GROUP)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    assert_eq!(
        rt_msg_pairs(&msgs),
        vec![
            (Some(3), "2T".to_string()),
            (Some(3), "11R".to_string()),
            (Some(3), "11T".to_string()),
            (Some(21), "3R".to_string()),
        ],
        "rows must order by TIME_STAMP, then RT, then SA, then R before T"
    );
}

/// Ascending timestamps with descending RTs must pass through untouched — the
/// stage only permutes within one equal-timestamp run.
///
/// Requirements: L1-OUT-003, L2-WRT-021
#[test]
fn canonical_order_never_reorders_across_timestamps() {
    use aero1553::order::OrderIterExt;

    let bytes = [
        rcv_record_at(21, 3, 100),
        rcv_record_at(15, 3, 200),
        rcv_record_at(3, 3, 300),
    ]
    .concat();
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader
        .iter()
        .order_rows(aero1553::order::DEFAULT_MAX_SORT_GROUP)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    assert_eq!(
        msgs.iter().map(|m| m.rt().unwrap()).collect::<Vec<_>>(),
        vec![21, 15, 3],
        "differing timestamps must never be reordered"
    );
}

/// The stage composes with the merge: a tie spanning two inputs orders by
/// RT/MSG, not by which file was listed first.
///
/// Requirements: L1-OUT-003, L2-WRT-021, L2-MRG-002, L3-RS-016
#[test]
fn canonical_order_applies_to_merged_stream() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::order::OrderIterExt;

    // File A holds the HIGHER RT, so the heap's (file_index, seq) tiebreak alone
    // would emit RT 20 before RT 4. Canonical order must invert that.
    let a = rcv_record_at(20, 5, 700);
    let b = rcv_record_at(4, 5, 700);
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let msgs: Vec<_> = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .order_rows(aero1553::order::DEFAULT_MAX_SORT_GROUP)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    assert_eq!(
        msgs.iter().map(|m| m.rt().unwrap()).collect::<Vec<_>>(),
        vec![4, 20],
        "a merged tie must order by RT, not by input position"
    );
}

/// DELTA is per-RT/MSG key, and two records in one run that share a key also
/// share a timestamp, so the reorder cannot change any DELTA value.
///
/// Requirements: L1-OUT-003, L2-WRT-021, L1-DLT-001
#[test]
fn canonical_order_leaves_delta_unchanged() {
    use aero1553::order::OrderIterExt;

    let bytes = [
        rcv_record_at(21, 3, 500),
        rcv_record_at(3, 3, 500),
        rcv_record_at(21, 3, 900),
        rcv_record_at(3, 3, 900),
    ]
    .concat();
    let f = TempFile::new(&bytes);

    let reader = MieFileReader::new(f.path()).unwrap();
    let unordered: Vec<_> = reader.iter().collect::<Result<Vec<_>, _>>().unwrap();
    let reader2 = MieFileReader::new(f.path()).unwrap();
    let ordered: Vec<_> = reader2
        .iter()
        .order_rows(aero1553::order::DEFAULT_MAX_SORT_GROUP)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    // Same multiset of (RT, DELTA) pairs before and after reordering.
    let key = |ms: &[aero1553::models::MieMessage]| {
        let mut v: Vec<(u8, String)> = ms
            .iter()
            .map(|m| (m.rt().unwrap(), format!("{:?}", m.delta)))
            .collect();
        v.sort();
        v
    };
    assert_eq!(
        key(&unordered),
        key(&ordered),
        "reordering an equal-timestamp run must not change any DELTA"
    );
}

/// `max_sort_group = 1` restores raw capture order — L2-WRT-022's documented
/// "off" value.
///
/// Requirements: L2-WRT-022
#[test]
fn canonical_order_cap_of_one_restores_capture_order() {
    use aero1553::order::OrderIterExt;

    let bytes = [rcv_record_at(21, 3, 500), rcv_record_at(3, 3, 500)].concat();
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let msgs: Vec<_> = reader
        .iter()
        .order_rows(aero1553::order::MAX_SORT_GROUP_MIN)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    assert_eq!(
        msgs.iter().map(|m| m.rt().unwrap()).collect::<Vec<_>>(),
        vec![21, 3],
        "a cap of 1 must leave capture order untouched"
    );
}

/// The written CSV is itself in canonical order — the guarantee is observable in
/// the artifact, not only in the iterator.
///
/// Requirements: L1-OUT-003, L2-WRT-021
#[test]
fn canonical_order_is_visible_in_written_csv() {
    use aero1553::order::OrderIterExt;
    use aero1553::writer::{WriteOptions, write_csv};

    let bytes = [
        rcv_record_at(21, 3, 500),
        xmt_record_at(3, 11, 500),
        rcv_record_at(3, 11, 500),
    ]
    .concat();
    let f = TempFile::new(&bytes);
    let reader = MieFileReader::new(f.path()).unwrap();
    let out = std::env::temp_dir().join(format!("mie-order-csv-{}.csv", std::process::id()));
    write_csv(
        reader
            .iter()
            .order_rows(aero1553::order::DEFAULT_MAX_SORT_GROUP),
        Some(out.as_path()),
        WriteOptions::default(),
    )
    .unwrap();
    let text = std::fs::read_to_string(&out).unwrap();
    let _ = std::fs::remove_file(&out);

    // Columns 2 and 3 of each data row are RT and MSG.
    let rows: Vec<(String, String)> = text
        .lines()
        .skip(1)
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let cols: Vec<&str> = l.split(',').collect();
            (cols[1].to_string(), cols[2].to_string())
        })
        .collect();
    assert_eq!(
        rows,
        vec![
            ("3".to_string(), "11R".to_string()),
            ("3".to_string(), "11T".to_string()),
            ("21".to_string(), "3R".to_string()),
        ]
    );
}

// ── L2-MRG-005: DELTA scope ───────────────────────────────────────────────

/// File A carries a key unique to it plus one shared with B; B carries only the
/// shared key, offset in time. The shared key is where the two scopes differ.
fn two_files_sharing_a_key() -> (Vec<u8>, Vec<u8>) {
    let a = [
        rcv_record_at(15, 11, 100_000),
        rcv_record_at(20, 5, 100_000),
        rcv_record_at(15, 11, 300_000),
        rcv_record_at(20, 5, 300_000),
    ]
    .concat();
    let b = [rcv_record_at(20, 5, 200_000), rcv_record_at(20, 5, 400_000)].concat();
    (a, b)
}

/// The guarantee that makes `per-file` the default: every merged record's DELTA
/// equals the value that record gets when its own file is decoded alone.
///
/// Requirements: L2-MRG-005, L3-WRT-004
#[test]
fn per_file_delta_matches_single_file_decode() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::models::DeltaScope;

    let (a, b) = two_files_sharing_a_key();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);

    // Decode each file on its own: (file_offset -> delta).
    let mut alone: Vec<(u64, Option<f64>)> = Vec::new();
    for path in [fa.path(), fb.path()] {
        let r = MieFileReader::new(path).unwrap();
        for m in &r {
            let m = m.unwrap();
            alone.push((m.file_offset, m.delta));
        }
    }

    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged: Vec<_> = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .delta_scope(DeltaScope::PerFile)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(merged.len(), 6);

    // Each single-file (offset, delta) pair must appear in the merged output.
    for (offset, delta) in alone {
        assert!(
            merged
                .iter()
                .any(|m| m.file_offset == offset && m.delta == delta),
            "offset {offset:#X} delta {delta:?} missing from the merged stream"
        );
    }
}

/// `global` compresses the shared key's gaps while leaving a file-unique key
/// untouched.
///
/// Requirements: L2-MRG-005
#[test]
fn global_scope_measures_across_the_merged_timeline() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::models::DeltaScope;

    let (a, b) = two_files_sharing_a_key();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);
    let readers = vec![
        MieFileReader::new(fa.path()).unwrap(),
        MieFileReader::new(fb.path()).unwrap(),
    ];
    let merged: Vec<_> = MergedRecordIter::new(&readers, None, false, false)
        .unwrap()
        .delta_scope(DeltaScope::Global)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    let shared: Vec<Option<f64>> = merged
        .iter()
        .filter(|m| m.delta_key() == "20:5R")
        .map(|m| m.delta)
        .collect();
    let unique: Vec<Option<f64>> = merged
        .iter()
        .filter(|m| m.delta_key() == "15:11R")
        .map(|m| m.delta)
        .collect();
    assert_eq!(
        shared,
        vec![Some(0.0), Some(0.1), Some(0.1), Some(0.1)],
        "shared key compresses under global scope"
    );
    assert_eq!(
        unique,
        vec![Some(0.0), Some(0.2)],
        "a key unique to one file is unaffected by scope"
    );
}

/// Requirements: L2-MRG-005
#[test]
fn per_file_is_the_default_scope() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::models::DeltaScope;

    let (a, b) = two_files_sharing_a_key();
    let fa = TempFile::new(&a);
    let fb = TempFile::new(&b);
    let deltas = |scope: Option<DeltaScope>| -> Vec<Option<f64>> {
        let readers = vec![
            MieFileReader::new(fa.path()).unwrap(),
            MieFileReader::new(fb.path()).unwrap(),
        ];
        let it = MergedRecordIter::new(&readers, None, false, false).unwrap();
        let it = match scope {
            Some(s) => it.delta_scope(s),
            None => it,
        };
        it.map(|m| m.unwrap().delta).collect()
    };
    assert_eq!(deltas(None), deltas(Some(DeltaScope::PerFile)));
}

/// With one file the two scopes are the same computation by definition.
///
/// Requirements: L2-MRG-005
#[test]
fn scope_does_not_affect_a_single_input() {
    use aero1553::merge::MergedRecordIter;
    use aero1553::models::DeltaScope;

    let (a, _b) = two_files_sharing_a_key();
    let fa = TempFile::new(&a);
    let deltas = |scope: DeltaScope| -> Vec<Option<f64>> {
        let readers = vec![MieFileReader::new(fa.path()).unwrap()];
        MergedRecordIter::new(&readers, None, false, false)
            .unwrap()
            .delta_scope(scope)
            .map(|m| m.unwrap().delta)
            .collect()
    };
    assert_eq!(deltas(DeltaScope::PerFile), deltas(DeltaScope::Global));
}
