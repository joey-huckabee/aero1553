//! CLI acceptance tests: spawn the actual built `aero1553` binary
//! as a subprocess and assert on exit code, stdout, stderr, and
//! filesystem effects.
//!
//! Sits one level above the cross-implementation conformance suite
//! (`tests/conformance/`). Conformance proves the Rust and Python
//! CLIs produce byte-identical CSV; this file covers Rust-only CLI
//! behaviors that conformance can't assert (`--no-clobber`,
//! input/output collision rejection, exit-class taxonomy, `--help` /
//! `--version`) plus a smoke-level happy-path decode to confirm the
//! binary is wired together end-to-end.
//!
//! Runs on every platform `cargo test --all-targets` runs on — Cargo
//! exposes the built binary path via `env!("CARGO_BIN_EXE_<name>")`
//! and appends `.exe` on Windows automatically, so no per-OS code
//! paths are needed here.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

const BIN: &str = env!("CARGO_BIN_EXE_aero1553");

// ── Fixtures ─────────────────────────────────────────────────────────

/// One valid 72-byte RT15 SA11 receive record. Byte-exact with the
/// `record_rt15_sa11_rcv` fixture in `rust/tests/integration.rs` —
/// duplicated here so the CLI suite has no link-time dependency on
/// the integration suite.
fn one_valid_record() -> Vec<u8> {
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

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

/// One errored RT15 SA11 record: Type Word with bit 14 set (error),
/// IRIG timestamp byte-identical with `one_valid_record`, Cmd Word
/// 0x797E, two zero data words, and a trailing Error Word of 0x011E
/// (Manchester/Parity). 16 bytes total. Mirrors the
/// `errored_record_rt15_sa11_us(...)` builder in
/// `python/tests/conftest.py`.
fn errored_record() -> Vec<u8> {
    let mut s = String::new();
    s.push_str("02480F1826DB21F6"); // Type 0x4802 (err bit, wc=8) + IRIG TS
    s.push_str("7E79"); // Cmd Word 0x797E
    s.push_str("00000000"); // 2 zero data words
    s.push_str("1E01"); // Error Word 0x011E
    hex(&s)
}

/// `one_valid_record` with the IRIG freerun bit (bit 15 of the upper
/// timestamp word) set — a record with no calendar anchor. A merge rejects a
/// freerun-leading input because it can't share an absolute timeline.
fn freerun_record() -> Vec<u8> {
    let mut r = one_valid_record();
    r[3] |= 0x80; // set bit 15 of the little-endian upper timestamp word
    r
}

// ── Scratch directory helper ─────────────────────────────────────────

/// Per-test scratch directory, removed on drop. Tests work inside one
/// of these instead of inventing unique names per artifact — paths
/// can be plain `dir/input.mie`, `dir/output.csv`.
struct TempDir(PathBuf);
impl TempDir {
    fn new() -> Self {
        static C: AtomicU64 = AtomicU64::new(0);
        let n = C.fetch_add(1, Ordering::Relaxed);
        let pid = std::process::id();
        let p = std::env::temp_dir().join(format!("mie-cli-{pid}-{n}"));
        std::fs::create_dir_all(&p).unwrap();
        Self(p)
    }
    fn path(&self) -> &Path {
        &self.0
    }
    fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let p = self.0.join(name);
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(bytes).unwrap();
        f.flush().unwrap();
        p
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

// ── Subprocess helper ────────────────────────────────────────────────

fn run<I, S>(args: I) -> Output
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    let out = Command::new(BIN)
        .args(args)
        .output()
        .expect("failed to spawn aero1553 binary");
    if !out.stderr.is_empty() {
        // Surface stderr in test output so a Windows CI failure can
        // be triaged from the runner logs without re-running locally.
        eprintln!(
            "--- aero1553 stderr ---\n{}\n--------------------------",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    out
}

fn exit_code(o: &Output) -> i32 {
    o.status
        .code()
        .expect("process exited via signal, not a code")
}

// ── Tests ────────────────────────────────────────────────────────────

/// Requirements: L2-CLI-001, L2-CLI-008, L2-CLI-009
#[test]
fn help_exits_zero_and_lists_all_subcommands() {
    let out = run(["--help"]);
    assert_eq!(exit_code(&out), 0, "--help must exit 0");
    let stdout = String::from_utf8_lossy(&out.stdout);
    for sub in ["decode", "count", "dump"] {
        assert!(
            stdout.contains(sub),
            "--help output missing subcommand '{sub}'\n--- stdout ---\n{stdout}"
        );
    }
}

/// Requirements: L2-CLI-005
#[test]
fn version_prints_crate_version() {
    let out = run(["--version"]);
    assert_eq!(exit_code(&out), 0, "--version must exit 0");
    let stdout = String::from_utf8_lossy(&out.stdout);
    let expected = env!("CARGO_PKG_VERSION");
    assert!(
        stdout.contains(expected),
        "--version output missing crate version '{expected}'\n--- stdout ---\n{stdout}"
    );
}

/// Every accepted spelling of the version flag — both short forms (`-V`/`-v`)
/// and the long form in any letter case — prints the version and exits 0, so
/// the Rust and Python CLIs agree on every spelling a user might type.
/// Requirements: L2-CLI-005
#[test]
fn version_flag_accepts_all_spellings() {
    let expected = env!("CARGO_PKG_VERSION");
    for flag in [
        "-V",
        "-v",
        "--version",
        "--VERSION",
        "--Version",
        "--vErSiOn",
    ] {
        let out = run([flag]);
        assert_eq!(exit_code(&out), 0, "{flag} must exit 0");
        let stdout = String::from_utf8_lossy(&out.stdout);
        assert!(
            stdout.contains(expected),
            "{flag} output missing crate version '{expected}'\n--- stdout ---\n{stdout}"
        );
    }
}

/// Requirements: L2-CLI-002
#[test]
fn output_dash_is_a_filename_not_stdout() {
    // `-o -` writes a file CALLED `-`. stdout is selected by omitting the flag,
    // and that is the only way to select it.
    //
    // Pinned in all three implementations because this is a rule they once
    // disagreed on: the C++ build treated `-` as stdout for a while, and the
    // CLI-surface-parity gate could not see it -- that gate compares flag
    // names, not what their values mean.
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let dash = tmp.path().join("-");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        dash.as_os_str(),
    ]);
    assert_eq!(
        exit_code(&out),
        0,
        "writing to a file named `-` must succeed"
    );
    assert!(
        out.stdout.is_empty(),
        "`-o -` must not write to stdout; got {} bytes",
        out.stdout.len()
    );

    let csv = std::fs::read_to_string(&dash).expect("no file named `-` was created");
    assert!(
        csv.contains("MSG"),
        "the file named `-` should hold the CSV"
    );
}

/// Requirements: L2-CLI-001, L2-CLI-002, L2-WRT-001
#[test]
fn decode_happy_path_writes_csv_with_header_and_one_row() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0, "decode happy path must exit 0");

    let csv = std::fs::read_to_string(&output).expect("output CSV not created");
    // Don't assert the full header byte-for-byte — that's the
    // conformance suite's job. Just confirm the writer ran and
    // produced a header line plus the one data row.
    assert!(
        csv.contains("MSG"),
        "CSV missing MSG header column\n--- csv ---\n{csv}"
    );
    assert!(
        csv.lines().count() >= 2,
        "CSV has fewer than 2 lines (header + data)\n--- csv ---\n{csv}"
    );
}

/// L1-EXIT-010: decoding a valid but empty recording (the record stream is
/// just the `00 00` end-of-records terminator) SHALL exit 0, write a
/// header-only CSV, and report the `empty-recording` exit class — NOT the
/// `NoValidRecords` wrong-file rejection (exit 2). Models an unused
/// MIL-STD-1553 channel that captured no traffic.
/// Requirements: L1-EXIT-010, L2-RDR-021
#[test]
fn decode_empty_recording_exits_0_with_header_only_csv() {
    let tmp = TempDir::new();
    let input = tmp.write("empty.mie", &[0x00, 0x00]);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("--log-level"),
        std::ffi::OsStr::new("info"),
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0, "empty recording must exit 0");
    let csv = std::fs::read_to_string(&output).expect("header-only CSV must be created");
    assert_eq!(
        csv.lines().count(),
        1,
        "empty recording produces a header-only CSV (no data rows)\n--- csv ---\n{csv}"
    );
    assert!(csv.contains("MSG"), "header row must be present");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("empty-recording"),
        "stderr must name the empty-recording exit class\n--- stderr ---\n{stderr}"
    );
}

/// L1-EXIT-010: `count` on an empty recording prints `0` and exits 0.
/// Requirements: L1-EXIT-010
#[test]
fn count_empty_recording_prints_zero_exits_0() {
    let tmp = TempDir::new();
    let input = tmp.write("empty.mie", &[0x00, 0x00]);
    let out = run([std::ffi::OsStr::new("count"), input.as_os_str()]);
    assert_eq!(
        exit_code(&out),
        0,
        "count of an empty recording must exit 0"
    );
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        "0",
        "count must print 0 for an empty recording"
    );
}

/// L2-CLI-011: `count` on a wrong-file (no valid records) input SHALL exit 2,
/// matching `decode`. Regression: `count` previously flattened the
/// `NoValidRecords` error to the generic runtime exit 1.
/// Requirements: L2-CLI-011, L1-EXIT-002
#[test]
fn count_no_valid_records_exits_2() {
    let tmp = TempDir::new();
    let input = tmp.write("junk.mie", &vec![0xFFu8; 256]);
    let out = run([std::ffi::OsStr::new("count"), input.as_os_str()]);
    assert_eq!(
        exit_code(&out),
        2,
        "count on a wrong-file input must exit 2 (aligned with decode)"
    );
}

/// A multi-file merge whose inputs can't share an absolute timeline (a
/// freerun-leading file here) is rejected before any output, exit 6.
/// Requirements: L1-EXIT-009, L2-MRG-003, L2-CLI-011
#[test]
fn merge_incompatible_inputs_exit_6() {
    let tmp = TempDir::new();
    let mut good = one_valid_record();
    good.extend(one_valid_record());
    let mut freerun = freerun_record();
    freerun.extend(freerun_record());
    let g = tmp.write("good.mie", &good);
    let f = tmp.write("freerun.mie", &freerun);
    let output = tmp.path().join("merged.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        g.as_os_str(),
        f.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(
        exit_code(&out),
        6,
        "incompatible (freerun) merge inputs must exit 6"
    );
    assert!(
        !output.exists(),
        "no output file should be created when the merge is rejected"
    );
}

/// Requirements: L2-MRG-004
///
/// A merge `decode` where one input fails at *priming* (a non-MIE first record)
/// under `--allow-partial` writes the combined output as `<out>.partial`, leaves
/// the plain `<out>` absent, and exits 0. Regression for the reported symptom
/// (pre-fix: a plain `out.csv` + exit 0, no `.partial`).
#[test]
fn merge_allow_partial_priming_writes_dot_partial() {
    let tmp = TempDir::new();
    let mut good = one_valid_record();
    good.extend(one_valid_record());
    let g = tmp.write("good.mie", &good);
    let b = tmp.write("bad.mie", &vec![0xFFu8; 4096]); // non-MIE first record
    let output = tmp.path().join("merged.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        g.as_os_str(),
        b.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
        std::ffi::OsStr::new("--allow-partial"),
    ]);
    assert_eq!(
        exit_code(&out),
        0,
        "--allow-partial downgrades the priming failure to exit 0"
    );
    let partial = PathBuf::from(format!("{}.partial", output.display()));
    assert!(
        partial.exists(),
        "the combined output must be committed as .partial"
    );
    assert!(!output.exists(), "the plain output must NOT be written");
}

/// Requirements: L2-MRG-004
///
/// A merge where one input fails at *open* (an empty 0-byte file) under
/// `--allow-partial` likewise writes a `.partial` and exits 0 — the per-file
/// failure is tolerated whether it occurs at open, priming, or mid-file.
#[test]
fn merge_allow_partial_open_failure_writes_dot_partial() {
    let tmp = TempDir::new();
    let mut good = one_valid_record();
    good.extend(one_valid_record());
    let g = tmp.write("good.mie", &good);
    let e = tmp.write("empty.mie", b""); // 0-byte → fails at open
    let output = tmp.path().join("merged.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        g.as_os_str(),
        e.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
        std::ffi::OsStr::new("--allow-partial"),
    ]);
    assert_eq!(exit_code(&out), 0);
    let partial = PathBuf::from(format!("{}.partial", output.display()));
    assert!(
        partial.exists(),
        "an open-failure merge must commit a .partial"
    );
    assert!(!output.exists());
    // L2-MRG-004: the WARN names what happened -- not a sync loss at offset 0.
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("1 of 2 merge inputs could not be read and was left out"),
        "{stderr}"
    );
    assert!(!stderr.contains("sync loss"), "{stderr}");
}

/// Requirements: L2-MRG-004
///
/// When every merge input fails, `--allow-partial` has nothing to keep: the run
/// fails with the FIRST input's own error and writes nothing -- whether the
/// inputs fail at open (missing, exit 1) or a missing input precedes one that
/// opens but is not a recording (still exit 1, the first input's code).
#[test]
fn merge_allow_partial_every_input_failing_reports_the_first() {
    let tmp = TempDir::new();
    let junk = tmp.write("junk.mie", &vec![0xFFu8; 4096]);
    let missing = tmp.path().join("missing.mie");
    let output = tmp.path().join("merged.csv");
    let partial = PathBuf::from(format!("{}.partial", output.display()));

    for inputs in [[&missing, &missing], [&missing, &junk]] {
        let out = run([
            std::ffi::OsStr::new("decode"),
            inputs[0].as_os_str(),
            inputs[1].as_os_str(),
            std::ffi::OsStr::new("-o"),
            output.as_os_str(),
            std::ffi::OsStr::new("--allow-partial"),
        ]);
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(exit_code(&out), 1, "{stderr}");
        assert!(stderr.contains("MIE file not found"), "{stderr}");
        assert!(!output.exists() && !partial.exists());
    }
}

/// Requirements: L2-MRG-004
///
/// On stdout a partial merge cannot commit a `.partial`, so a left-out input
/// fails the run with the sync-loss exit class -- naming what happened.
#[test]
fn merge_allow_partial_to_stdout_with_a_left_out_input_exits_3() {
    let tmp = TempDir::new();
    let mut good = one_valid_record();
    good.extend(one_valid_record());
    let g = tmp.write("good.mie", &good);
    let missing = tmp.path().join("missing.mie");

    let out = run([
        std::ffi::OsStr::new("decode"),
        missing.as_os_str(),
        g.as_os_str(),
        std::ffi::OsStr::new("--allow-partial"),
    ]);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(exit_code(&out), 3, "{stderr}");
    assert!(
        stderr.contains("Error: 1 of 2 merge inputs could not be read and was left out"),
        "{stderr}"
    );
}

/// Requirements: L1-EXIT-004
///
/// Stdout cannot hold a `.partial`, so a sync loss there exits 3 with the rows
/// before it printed, `--allow-partial` or not -- and the advice must not tell
/// the operator to pass the flag they already passed: it names the output file
/// that was missing.
#[test]
fn sync_loss_to_stdout_with_allow_partial_exits_3_and_asks_for_a_file() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(one_valid_record());
    bytes.extend(vec![0xFFu8; 70_000]); // >64 KB that never resyncs -> unrecoverable
    let input = tmp.write("cut.mie", &bytes);

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--allow-partial"),
    ]);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(exit_code(&out), 3, "{stderr}");
    assert_eq!(
        out.stdout
            .split(|&b| b == b'\n')
            .filter(|l| !l.is_empty())
            .count(),
        3
    );
    assert!(
        stderr.contains("Pass --allow-partial with an output file (-o) to keep what was decoded"),
        "{stderr}"
    );
}

/// Requirements: L2-WRT-014
#[test]
fn no_clobber_refuses_to_overwrite_existing_output() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let sentinel = b"SENTINEL CONTENT FROM PREVIOUS RUN\n";
    let output = tmp.write("out.csv", sentinel);

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--no-clobber"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_ne!(
        exit_code(&out),
        0,
        "--no-clobber must NOT exit 0 when target already exists"
    );

    let preserved = std::fs::read(&output).expect("output file disappeared");
    assert_eq!(
        preserved, sentinel,
        "--no-clobber must not modify the existing file"
    );
}

/// Requirements: L2-WRT-017
///
/// `--no-clobber` checks the files the options could write, not the ones this
/// run will: with `--allow-partial` a stale `out.csv.partial` refuses a clean
/// run before it decodes, and without `--allow-partial` it is not looked at.
#[test]
fn no_clobber_with_allow_partial_refuses_a_stale_partial() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let partial = tmp.write("out.csv.partial", b"STALE");
    let output = tmp.path().join("out.csv");

    let refused = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--no-clobber"),
        std::ffi::OsStr::new("--allow-partial"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert_eq!(exit_code(&refused), 1, "{stderr}");
    assert!(stderr.contains("out.csv.partial"), "{stderr}");
    assert!(!output.exists());
    assert_eq!(std::fs::read(&partial).unwrap(), b"STALE");

    let accepted = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--no-clobber"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&accepted), 0);
    assert!(output.exists());
}

/// Requirements: L2-WRT-016
#[test]
fn rejects_input_equal_to_output_path() {
    let tmp = TempDir::new();
    let original = one_valid_record();
    let same = tmp.write("rec.mie", &original);

    let out = run([
        std::ffi::OsStr::new("decode"),
        same.as_os_str(),
        std::ffi::OsStr::new("-o"),
        same.as_os_str(),
    ]);
    assert_ne!(
        exit_code(&out),
        0,
        "decode must reject identical input and output paths"
    );

    // Belt-and-braces: even if the collision check were to fail
    // open, the input file must still be intact. Operator data
    // doesn't get destroyed by a CLI typo.
    let preserved = std::fs::read(&same).expect("input file disappeared");
    assert_eq!(
        preserved, original,
        "input file was modified despite the collision rejection"
    );
}

/// Requirements: L2-WRT-014, L2-MRG-001
///
/// A MERGE must check every path it could commit against every input, not just
/// the destination against the destination. The writer cannot do this one: on
/// the merge path it is handed `input_path: None` precisely because it is given
/// one stream and never learns how many files fed it, so the CLI guard is the
/// only thing standing between `capture_errors.mie` and being overwritten by
/// the errors file derived from `-o capture.mie`.
///
/// Asserted on the artifact, not the message: exit non-zero, every input byte
/// identical, and no output file created at all.
#[test]
fn merge_rejects_an_input_that_a_derived_output_path_would_overwrite() {
    let tmp = TempDir::new();
    let original = one_valid_record();
    let first = tmp.write("recording.mie", &original);
    // Plausible name for a second recorder's file -- and exactly what
    // `-o capture.mie --separate-errors` derives.
    let victim = tmp.write("capture_errors.mie", &original);

    let out = run([
        std::ffi::OsStr::new("decode"),
        first.as_os_str(),
        victim.as_os_str(),
        std::ffi::OsStr::new("-o"),
        tmp.0.join("capture.mie").as_os_str(),
        std::ffi::OsStr::new("--separate-errors"),
    ]);
    assert_ne!(
        exit_code(&out),
        0,
        "a merge whose errors file would land on an input must be rejected"
    );

    assert_eq!(
        std::fs::read(&victim).expect("input file disappeared"),
        original,
        "input file was modified despite the collision rejection"
    );
    assert!(
        !tmp.0.join("capture.mie").exists(),
        "no output may be created once the run is refused"
    );
}

/// Requirements: L2-CLI-008, L3-RS-008
///
/// Two-channel output contract:
/// - stdout: ONLY the integer record count (machine-parseable for
///   pipelines: `n=$(aero1553 count rec.mie)`).
/// - stderr: human-readable status line including the input path
///   so an interactive operator still sees context. Always emitted
///   (not gated by --log-level).
#[test]
fn count_subcommand_emits_integer_to_stdout_and_status_to_stderr() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());

    let out = run([std::ffi::OsStr::new("count"), input.as_os_str()]);
    assert_eq!(exit_code(&out), 0, "count must exit 0 on valid input");

    let stdout = String::from_utf8_lossy(&out.stdout);
    assert_eq!(
        stdout.trim(),
        "1",
        "stdout must contain only the integer count (got: {stdout:?})"
    );

    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("counted 1 messages"),
        "stderr must include the human-readable status line (got: {stderr:?})"
    );
}

/// Requirements: L2-CLI-005, L1-EXIT-007
#[test]
fn no_args_invocation_is_usage_error_exit_4() {
    // No subcommand is a usage error (L1-EXIT-007), not a no-valid-records
    // condition (L1-EXIT-002) — it must exit with the usage code 4, not
    // merely some non-zero code.
    let out = run(Vec::<&str>::new());
    assert_eq!(
        exit_code(&out),
        4,
        "invoking the binary with no subcommand must be a usage error (exit 4)"
    );
}

// ── Filter behavior (Rust-only include side per L3-RS-010) ───────────

/// Helper: count data rows in a CSV (lines minus the one-line header).
fn data_row_count(csv: &str) -> usize {
    csv.lines().count().saturating_sub(1)
}

/// Requirements: L2-FLT-001, L3-RS-010
#[test]
fn include_rts_filter_keeps_only_matching_records() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());

    // `--include-rts 15` matches the fixture's RT15: row retained.
    let kept_out = tmp.path().join("kept.csv");
    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--include-rts"),
        std::ffi::OsStr::new("15"),
        std::ffi::OsStr::new("-o"),
        kept_out.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);
    let csv = std::fs::read_to_string(&kept_out).unwrap();
    assert_eq!(
        data_row_count(&csv),
        1,
        "RT15 record should be kept by --include-rts 15\n--- csv ---\n{csv}"
    );

    // `--include-rts 7` excludes RT15 (no match): zero data rows.
    let dropped_out = tmp.path().join("dropped.csv");
    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--include-rts"),
        std::ffi::OsStr::new("7"),
        std::ffi::OsStr::new("-o"),
        dropped_out.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);
    let csv = std::fs::read_to_string(&dropped_out).unwrap();
    assert_eq!(
        data_row_count(&csv),
        0,
        "RT15 record should NOT pass --include-rts 7\n--- csv ---\n{csv}"
    );
}

/// Requirements: L2-FLT-001
#[test]
fn exclude_rts_filter_drops_matching_records() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--exclude-rts"),
        std::ffi::OsStr::new("15"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);
    let csv = std::fs::read_to_string(&output).unwrap();
    assert_eq!(
        data_row_count(&csv),
        0,
        "RT15 record should be dropped by --exclude-rts 15\n--- csv ---\n{csv}"
    );
}

// ── Exit-class summary line (L1-EXIT-005) ────────────────────────────

/// Requirements: L1-EXIT-005, L2-CLI-006
///
/// The exit-class summary line is emitted via `log_info!` so it
/// only surfaces at INFO level or below. Default is WARN, so the
/// test explicitly raises the level. This exercises both the
/// log-level CLI flag and the summary-line format.
///
/// L2-CLI-006 cited because the summary line satisfies the
/// "human-readable diagnostics on stderr" obligation; tagging it
/// here lets the trace matrix attribute the test (the matrix's L1
/// section displays only L2 children + rolled-up status, not
/// direct L1 test markers, so the L1-EXIT-005 tag alone is
/// invisible in the rendered matrix).
#[test]
fn decode_emits_exit_class_summary_at_info_level() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("--log-level"),
        std::ffi::OsStr::new("info"),
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);

    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("decode exit class: complete"),
        "stderr must include the L1-EXIT-005 summary line on clean decode\n--- stderr ---\n{stderr}"
    );
}

/// The non-monotonic WARN reads exactly as the C++ one does (L2-RDR-017).
///
/// Checked as a whole line, not a keyword: through v4.0.0 the Rust literal had
/// lost its `\` line continuations in a refactor, so the message carried two
/// runs of 18 spaces, and every test that looked for "non-monotonic" passed.
/// Same two records as `tests/conformance/inputs/single-file-non-monotonic.hex`:
/// RT15 SA11 R at 50.500000, then at 50.250000.
///
/// Requirements: L2-RDR-017
#[test]
fn non_monotonic_warn_is_one_well_formed_line() {
    let mut data = one_valid_record();
    data[4..8].copy_from_slice(&hex("27DB20A1"));
    let mut earlier = one_valid_record();
    earlier[4..8].copy_from_slice(&hex("23DB90D0"));
    data.extend(earlier);

    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &data);
    let output = tmp.path().join("out.csv");
    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);

    let stderr = String::from_utf8_lossy(&out.stderr);
    let expected = "non-monotonic timestamp at 0x48 for RT/MSG key 0x000F0B00: \
                    prev_us=16646090500000 curr_us=16646090250000 \
                    (further out-of-order occurrences for this key suppressed)";
    let line = stderr
        .lines()
        .find(|l| l.contains("non-monotonic timestamp"))
        .unwrap_or_else(|| panic!("no non-monotonic WARN\n--- stderr ---\n{stderr}"));
    assert!(
        line.ends_with(expected),
        "WARN text drifted\n  got:  {line}\n  want: ...{expected}"
    );
    assert_eq!(stderr.matches("non-monotonic timestamp").count(), 1);
}

// ── dump subcommand (L2-CLI-009) ─────────────────────────────────────

/// Requirements: L2-CLI-009
#[test]
fn dump_records_outputs_hex_to_stdout() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());

    let out = run([
        std::ffi::OsStr::new("dump"),
        input.as_os_str(),
        std::ffi::OsStr::new("--records"),
        std::ffi::OsStr::new("1"),
    ]);
    assert_eq!(exit_code(&out), 0, "dump --records 1 must exit 0");

    let stdout = String::from_utf8_lossy(&out.stdout);
    // dump output includes a Cmd Word and at least one hex word from
    // the fixture's payload. Check coarse invariants only — the exact
    // dump format is not part of the cross-impl contract.
    assert!(
        stdout.contains("797E") || stdout.contains("7e79") || stdout.contains("0x797E"),
        "dump output should include the fixture's Cmd Word 0x797E in some form\n--- stdout ---\n{stdout}"
    );
}

// ── Inline error output (L2-ERR-010, L2-ERR-011) ─────────────────────

/// Requirements: L2-ERR-010, L2-ERR-011, L3-RS-009
///
/// Inline is the default: with no flag, errored records stay in the main CSV
/// with the ERROR and `ERROR_CODE` columns populated and no `_errors.csv` is
/// produced. Routing them to a separate file is now the opt-in
/// (`--separate-errors`).
/// Requirements: L2-ERR-010, L2-ERR-011, L3-RS-009
#[test]
fn inline_is_default_and_populates_error_code_column() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(errored_record());
    let input = tmp.write("rec.mie", &bytes);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);

    let csv = std::fs::read_to_string(&output).expect("output CSV not created");
    assert!(
        data_row_count(&csv) >= 2,
        "inline mode should keep both records in one file (got {} data rows)\n--- csv ---\n{csv}",
        data_row_count(&csv)
    );
    assert!(
        csv.contains("011E"),
        "the default (inline) must populate ERROR_CODE with the DDC code (0x011E)\n--- csv ---\n{csv}"
    );

    // The separate `_errors.csv` file must NOT have been created
    // when inline mode is active (L2-ERR-011).
    let errors_csv = tmp.path().join("out_errors.csv");
    assert!(
        !errors_csv.exists(),
        "the default (inline) must not produce a separate _errors.csv (found: {})",
        errors_csv.display()
    );
}

/// `--separate-errors` opts back into the split layout: clean records in the
/// main CSV, errored/spurious in `<stem>_errors.csv`.
/// Requirements: L2-ERR-008, L3-RS-009
#[test]
fn separate_errors_flag_opts_into_split_output() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(errored_record());
    let input = tmp.write("rec.mie", &bytes);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
        std::ffi::OsStr::new("--separate-errors"),
    ]);
    assert_eq!(exit_code(&out), 0);

    let main_csv = std::fs::read_to_string(&output).expect("main CSV not created");
    let errors_path = tmp.path().join("out_errors.csv");
    let errors_csv = std::fs::read_to_string(&errors_path).expect("errors CSV not created");

    assert_eq!(
        data_row_count(&main_csv),
        1,
        "the clean record belongs in the main CSV\n--- csv ---\n{main_csv}"
    );
    assert!(
        !main_csv.contains("011E"),
        "the errored record must not remain in the main CSV\n--- csv ---\n{main_csv}"
    );
    assert!(
        errors_csv.contains("011E"),
        "the errored record belongs in the errors CSV\n--- csv ---\n{errors_csv}"
    );
}

/// `--inline-errors` was removed when inline became the default. It must fail
/// loudly as a usage error (exit 4) rather than be quietly accepted, so a script
/// still carrying it gets corrected instead of silently relying on behaviour
/// that is now the default anyway.
/// Requirements: L2-CLI-011, L3-RS-009
#[test]
fn removed_inline_errors_flag_is_a_usage_error() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
        std::ffi::OsStr::new("--inline-errors"),
    ]);
    assert_eq!(exit_code(&out), 4, "the removed flag must be a usage error");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("--inline-errors"),
        "the error should name the offending flag\n--- stderr ---\n{stderr}"
    );
    assert!(
        !output.exists(),
        "no output should be written on a usage error"
    );
}

/// Requirements: L2-ERR-011, L3-RS-009
#[test]
fn stdout_output_forces_inline_error_mode() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(errored_record());
    let input = tmp.write("rec.mie", &bytes);

    let out = run([std::ffi::OsStr::new("decode"), input.as_os_str()]);
    assert_eq!(exit_code(&out), 0);

    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("011E"),
        "stdout output must inline errored records\n--- stdout ---\n{stdout}"
    );
}

/// L2-SYN-012 had no Rust verification at all — the trace matrix listed only a
/// Python test, and that test asserted against `find_first_record` rather than
/// the reader that actually emits the line. The Rust reader has always logged
/// it; this pins the behavior on this side too.
/// Filter diagnostics match the Python implementation: an INFO summary of the
/// active sets on construction and an INFO passed/excluded tally when the
/// stream finishes. The sets render sorted so the line is stable regardless of
/// the order values were parsed in (Python holds them as unordered sets).
/// Requirements: L2-FLT-001
#[test]
fn active_filters_and_tally_are_logged_at_info() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(one_valid_record());
    let input = tmp.write("rec.mie", &bytes);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("--log-level"),
        std::ffi::OsStr::new("info"),
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
        std::ffi::OsStr::new("--exclude-rts"),
        std::ffi::OsStr::new("31,15,0"),
    ]);
    assert_eq!(exit_code(&out), 0);

    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("Filtering active:") && stderr.contains("exclude_rts=[0, 15, 31]"),
        "expected a sorted active-filter summary
--- stderr ---
{stderr}"
    );
    assert!(
        stderr.contains("Filter results: 0 passed, 2 excluded"),
        "expected the passed/excluded tally
--- stderr ---
{stderr}"
    );
}

/// Requirements: L2-SYN-012
#[test]
fn header_detection_logs_size_at_info() {
    let tmp = TempDir::new();
    let mut bytes = b"DDC-HEADER-1234\n".to_vec(); // 16-byte proprietary header
    bytes.extend(one_valid_record());
    bytes.extend(one_valid_record());
    let input = tmp.write("headered.mie", &bytes);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("--log-level"),
        std::ffi::OsStr::new("info"),
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);

    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("file header detected") && stderr.contains("16 bytes"),
        "header detection must report the skipped byte count at INFO\
         \n--- stderr ---\n{stderr}"
    );
}

/// Requirements: L2-SYN-013
#[test]
fn debug_sync_failure_includes_bounded_validation_context() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(one_valid_record());
    bytes.extend([0x03, 0x00].repeat(5));
    let input = tmp.write("corrupt.mie", &bytes);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("--log-level"),
        std::ffi::OsStr::new("debug"),
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--strict"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_ne!(exit_code(&out), 0);

    let stderr = String::from_utf8_lossy(&out.stderr);
    // The precise reason names the *offending* position. Before v2.12.0 this
    // read "look-ahead message type is unknown", because the last good record
    // was rejected on account of its corrupt successor — which also discarded
    // that valid record. Continuous validation no longer looks ahead, so the
    // garbage is reported where it actually is, and the good record is kept.
    assert!(
        stderr.contains("Unknown message type"),
        "strict failure should name the precise validation reason\n--- stderr ---\n{stderr}"
    );
    assert!(
        stderr.contains("validation context") && stderr.contains("max 32"),
        "DEBUG failure should include a bounded context dump\n--- stderr ---\n{stderr}"
    );
}

/// Requirements: L2-SYN-004, L2-SYN-016
#[test]
fn strict_irig_failure_names_precise_validation_reason() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    let mut invalid_day = one_valid_record();
    invalid_day[2..4].copy_from_slice(&0x000Fu16.to_le_bytes());
    bytes.extend(invalid_day);
    let input = tmp.write("bad-irig.mie", &bytes);
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--strict"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_ne!(exit_code(&out), 0);

    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("IRIG day-of-year is out of range"),
        "strict failure should name the precise IRIG field\n--- stderr ---\n{stderr}"
    );
}

// -- IRIG day-of-year advisory (L2-LOG-001) --------------------------

/// Requirements: L2-LOG-001
///
/// The advisory is a standing disclaimer about card firmware, not an
/// observation about this recording, so it is logged at INFO and the default
/// WARNING level never shows it. Before v2.18.0 it was a WARN and appeared in
/// the default output of every decode of a calendar-locked IRIG file.
#[test]
fn irig_day_advisory_is_silent_at_the_default_level() {
    let d = TempDir::new();
    let input = d.write("in.mie", &one_valid_record());
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
    ]);
    assert_eq!(exit_code(&o), 0);
    let stderr = String::from_utf8_lossy(&o.stderr);
    assert!(
        !stderr.contains("day-of-year"),
        "the advisory must not appear at the default level
--- stderr ---
{stderr}"
    );
}

/// Requirements: L2-LOG-001
///
/// It is still emitted -- once per decode, not once per record -- for anyone
/// who asks for INFO. Two records in, one line out.
#[test]
fn irig_day_advisory_fires_once_at_info() {
    let d = TempDir::new();
    let bytes = [one_valid_record(), one_valid_record()].concat();
    let input = d.write("in.mie", &bytes);
    let out = d.path().join("out.csv");
    let o = run([
        "--log-level",
        "INFO",
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
    ]);
    assert_eq!(exit_code(&o), 0);
    let stderr = String::from_utf8_lossy(&o.stderr);
    // Count LINES, not substring hits: the message itself says "day-of-year"
    // twice in one sentence, so `matches()` would report two for one emission.
    assert_eq!(
        stderr.lines().filter(|l| l.contains("day-of-year")).count(),
        1,
        "the advisory fires once per decode
--- stderr ---
{stderr}"
    );
}

/// Requirements: L2-LOG-001, L2-CFG-003
///
/// `--no-irig-day-advisory` suppresses it even at INFO -- the case for a site
/// that has diffed its card model against vendor CSV and wants a verbose
/// troubleshooting run without the known-noise line. The config key does the
/// same thing, and the CLI flag wins over a config that leaves it enabled.
#[test]
fn irig_day_advisory_can_be_disabled() {
    let d = TempDir::new();
    let input = d.write("in.mie", &one_valid_record());

    let out1 = d.path().join("out1.csv");
    let o1 = run([
        "--log-level",
        "INFO",
        "--no-irig-day-advisory",
        "decode",
        input.to_str().unwrap(),
        "-o",
        out1.to_str().unwrap(),
    ]);
    assert_eq!(exit_code(&o1), 0);
    let e1 = String::from_utf8_lossy(&o1.stderr);
    assert!(
        !e1.contains("day-of-year"),
        "--no-irig-day-advisory should suppress it at INFO
--- stderr ---
{e1}"
    );

    let cfg = d.write(
        "cfg.toml",
        b"[logging]
irig_day_advisory = false
",
    );
    let out2 = d.path().join("out2.csv");
    let o2 = run([
        "--log-level",
        "INFO",
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        input.to_str().unwrap(),
        "-o",
        out2.to_str().unwrap(),
    ]);
    assert_eq!(exit_code(&o2), 0);
    let e2 = String::from_utf8_lossy(&o2.stderr);
    assert!(
        !e2.contains("day-of-year"),
        "[logging] irig_day_advisory = false should suppress it
--- stderr ---
{e2}"
    );
}

/// Requirements: L2-LOG-001, L2-CLI-002
///
/// A value-less flag must decline the `=value` spelling rather than accept it
/// and discard the value -- the same rule `--no-mux` follows.
#[test]
fn irig_day_advisory_flag_rejects_a_joined_value() {
    let d = TempDir::new();
    let input = d.write("in.mie", &one_valid_record());
    let o = run([
        "--no-irig-day-advisory=true",
        "decode",
        input.to_str().unwrap(),
    ]);
    assert_ne!(
        exit_code(&o),
        0,
        "--no-irig-day-advisory=true should be a usage error"
    );
}

// ── Timestamp-format auto-detect (L2-DEC-015) ────────────────────────

/// Requirements: L2-DEC-015
///
/// `--detect-records N` is accepted and the decode completes
/// normally. The probe at N=2 sees the single-record fixture as a
/// 1-record probe (the second record doesn't exist), scores it
/// decisively IRIG, and decodes. No strict-mode assertion here —
/// that path needs an ambiguous fixture, which is task #104's
/// territory.
#[test]
fn detect_records_flag_accepts_valid_size() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--detect-records"),
        std::ffi::OsStr::new("2"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(
        exit_code(&out),
        0,
        "decode with --detect-records 2 must exit 0 on a valid fixture"
    );
    assert!(output.exists(), "output CSV must be created");
}

/// Requirements: L2-SYN-026
///
/// `--lookahead-records N` is accepted in range and the decode
/// completes normally. Default N=2 (`DEFAULT_LOOKAHEAD_RECORDS`)
/// preserves historical behavior; any value in [1, 32] is valid.
#[test]
fn lookahead_records_flag_accepts_valid_size() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--lookahead-records"),
        std::ffi::OsStr::new("4"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(
        exit_code(&out),
        0,
        "decode with --lookahead-records 4 must exit 0 on a valid fixture"
    );
    assert!(output.exists(), "output CSV must be created");
}

/// Requirements: L2-SYN-026
#[test]
fn lookahead_records_flag_rejects_out_of_range() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--lookahead-records"),
        std::ffi::OsStr::new("999"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_ne!(
        exit_code(&out),
        0,
        "--lookahead-records 999 must fail (above the max of 32)"
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("--lookahead-records") && stderr.contains("999"),
        "stderr should name the offending flag and value (got: {stderr:?})"
    );
}

/// Requirements: L2-DEC-015
///
/// Out-of-range `--detect-records` is rejected at parse time with a
/// non-zero exit and the valid range in the error message.
#[test]
fn detect_records_flag_rejects_out_of_range() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    // Above the max of 32.
    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("--detect-records"),
        std::ffi::OsStr::new("999"),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_ne!(
        exit_code(&out),
        0,
        "--detect-records 999 must fail (above the max of 32)"
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("--detect-records") && stderr.contains("999"),
        "stderr should name the offending flag and value (got: {stderr:?})"
    );
}

// ── L1-OUT-003 / L2-WRT-021 / L2-WRT-022: canonical row order via the CLI ───

/// A valid receive record (Type 0x02, RT/SA patched) placed at `micro` within a
/// fixed day 192 15:54:50, so several records can share one `TIME_STAMP`. Keeps
/// the fixture's 30-data-word count so the record layout is untouched.
#[allow(
    clippy::decimal_bitwise_operands,
    reason = "packs wire fields whose values are semantic, not masks: `192` is the day-of-year, `15` the hour of 15:54:50, `30` the documented data-word count. Hex would obscure them, and the lint is inconsistent here anyway -- it flags the hour but not the minute and second in the same expression, because those sit inside shifts."
)]
fn record_at(rt: u8, sa: u8, micro: u32) -> Vec<u8> {
    let mut rec = one_valid_record();
    let upper: u16 = ((192u16 & 0x1FF) << 5) | 15;
    let middle: u16 = (54u16 << 10) | (50u16 << 4) | ((micro >> 16) as u16 & 0xF);
    let lower: u16 = (micro & 0xFFFF) as u16;
    rec[2..4].copy_from_slice(&upper.to_le_bytes());
    rec[4..6].copy_from_slice(&middle.to_le_bytes());
    rec[6..8].copy_from_slice(&lower.to_le_bytes());
    let cmd: u16 = (u16::from(rt & 0x1F) << 11) | (u16::from(sa & 0x1F) << 5) | 30;
    rec[8..10].copy_from_slice(&cmd.to_le_bytes());
    rec
}

/// `(RT, MSG)` columns of each data row of a decoded CSV.
fn csv_rt_msg(text: &str) -> Vec<(String, String)> {
    text.lines()
        .skip(1)
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let c: Vec<&str> = l.split(',').collect();
            (c[1].to_string(), c[2].to_string())
        })
        .collect()
}

/// Requirements: L1-OUT-003, L2-WRT-021
#[test]
fn decode_writes_rows_in_canonical_order() {
    let d = TempDir::new();
    // Three records at one instant, RTs deliberately descending on input.
    let bytes = [
        record_at(21, 3, 500),
        record_at(15, 7, 500),
        record_at(3, 11, 500),
    ]
    .concat();
    let input = d.write("in.mie", &bytes);
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
    ]);
    assert_eq!(exit_code(&o), 0);
    let text = std::fs::read_to_string(&out).unwrap();
    assert_eq!(
        csv_rt_msg(&text),
        vec![
            ("3".to_string(), "11R".to_string()),
            ("15".to_string(), "7R".to_string()),
            ("21".to_string(), "3R".to_string()),
        ],
        "decode must write equal-timestamp rows in ascending RT order"
    );
}

/// `--max-sort-group 1` is the documented escape hatch back to raw DDC capture
/// order for a vendor-CSV diff.
///
/// Requirements: L2-WRT-022, L3-WRT-003
#[test]
fn max_sort_group_one_restores_capture_order() {
    let d = TempDir::new();
    let bytes = [record_at(21, 3, 500), record_at(3, 11, 500)].concat();
    let input = d.write("in.mie", &bytes);
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--max-sort-group",
        "1",
    ]);
    assert_eq!(exit_code(&o), 0);
    let text = std::fs::read_to_string(&out).unwrap();
    assert_eq!(
        csv_rt_msg(&text),
        vec![
            ("21".to_string(), "3R".to_string()),
            ("3".to_string(), "11R".to_string()),
        ],
        "--max-sort-group 1 must leave capture order untouched"
    );
}

/// The `--flag=value` spelling is accepted, like every other valued flag.
///
/// Requirements: L3-WRT-003
#[test]
fn max_sort_group_accepts_equals_spelling() {
    let d = TempDir::new();
    let bytes = [record_at(21, 3, 500), record_at(3, 11, 500)].concat();
    let input = d.write("in.mie", &bytes);
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--max-sort-group=1",
    ]);
    assert_eq!(exit_code(&o), 0);
    let text = std::fs::read_to_string(&out).unwrap();
    assert_eq!(csv_rt_msg(&text).len(), 2);
}

/// Out-of-range and non-integer values are usage errors (exit 4), matching
/// `--detect-records`.
///
/// Requirements: L2-WRT-022, L3-WRT-003
#[test]
fn max_sort_group_rejects_invalid_values() {
    let d = TempDir::new();
    let input = d.write("in.mie", &one_valid_record());
    for bad in ["0", "1048577", "abc", "-1"] {
        let out = d.path().join(format!("out-{bad}.csv"));
        let o = run([
            "decode",
            input.to_str().unwrap(),
            "-o",
            out.to_str().unwrap(),
            "--max-sort-group",
            bad,
        ]);
        assert_eq!(
            exit_code(&o),
            4,
            "--max-sort-group {bad} must be a usage error"
        );
        let stderr = String::from_utf8_lossy(&o.stderr);
        assert!(
            stderr.contains("max-sort-group"),
            "error text must name the flag, got: {stderr}"
        );
    }
}

/// The `[output] max_sort_group` TOML key drives the same behavior, and a CLI
/// flag overrides it (CLI > config > default).
///
/// Requirements: L2-WRT-022, L3-WRT-003, L1-CFG-001
#[test]
fn max_sort_group_config_key_and_cli_precedence() {
    let d = TempDir::new();
    let bytes = [record_at(21, 3, 500), record_at(3, 11, 500)].concat();
    let input = d.write("in.mie", &bytes);
    let cfg = d.write("cfg.toml", b"[output]\nmax_sort_group = 1\n");

    // Config alone: reordering disabled, so capture order survives.
    let out1 = d.path().join("out1.csv");
    let o1 = run([
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        input.to_str().unwrap(),
        "-o",
        out1.to_str().unwrap(),
    ]);
    assert_eq!(exit_code(&o1), 0);
    assert_eq!(
        csv_rt_msg(&std::fs::read_to_string(&out1).unwrap())[0].0,
        "21",
        "config max_sort_group = 1 must disable reordering"
    );

    // CLI overrides the config back to a real cap, restoring canonical order.
    let out2 = d.path().join("out2.csv");
    let o2 = run([
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        input.to_str().unwrap(),
        "-o",
        out2.to_str().unwrap(),
        "--max-sort-group",
        "4096",
    ]);
    assert_eq!(exit_code(&o2), 0);
    assert_eq!(
        csv_rt_msg(&std::fs::read_to_string(&out2).unwrap())[0].0,
        "3",
        "--max-sort-group must override the config value"
    );
}

/// An out-of-range value in TOML is a config error, not a silent clamp.
///
/// Requirements: L2-WRT-022, L3-WRT-003
#[test]
fn max_sort_group_toml_out_of_range_is_a_config_error() {
    let d = TempDir::new();
    let input = d.write("in.mie", &one_valid_record());
    let cfg = d.write("cfg.toml", b"[output]\nmax_sort_group = 0\n");
    let out = d.path().join("out.csv");
    let o = run([
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
    ]);
    assert_ne!(exit_code(&o), 0, "an invalid config value must not succeed");
    let stderr = String::from_utf8_lossy(&o.stderr);
    assert!(
        stderr.contains("max_sort_group"),
        "error text must name the key, got: {stderr}"
    );
}

/// A capped run degrades to arrival order with one WARN, rather than failing or
/// dropping rows.
///
/// Requirements: L2-WRT-022
#[test]
fn capped_run_warns_and_keeps_every_row() {
    let d = TempDir::new();
    // Four records at one instant with a cap of 2.
    let bytes = [
        record_at(21, 3, 500),
        record_at(15, 3, 500),
        record_at(9, 3, 500),
        record_at(3, 3, 500),
    ]
    .concat();
    let input = d.write("in.mie", &bytes);
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--max-sort-group",
        "2",
    ]);
    assert_eq!(exit_code(&o), 0);
    let text = std::fs::read_to_string(&out).unwrap();
    assert_eq!(
        csv_rt_msg(&text).len(),
        4,
        "no row may be dropped at the cap"
    );
    // Two cap-sized chunks of one run: one WARN each, naming the cap.
    let stderr = String::from_utf8_lossy(&o.stderr);
    assert_eq!(
        stderr.matches("max_sort_group cap").count(),
        2,
        "one WARN per chunk that reached the cap, got: {stderr}"
    );

    // A cap of 1 is the "off" switch, not an overflow: rows in capture order,
    // and no WARN at all.
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--max-sort-group",
        "1",
    ]);
    assert_eq!(exit_code(&o), 0);
    let stderr = String::from_utf8_lossy(&o.stderr);
    assert!(
        !stderr.contains("max_sort_group"),
        "a cap of 1 must not WARN, got: {stderr}"
    );
}

/// In separate error mode each output file is independently in canonical order.
///
/// Requirements: L1-OUT-003, L2-WRT-021
#[test]
fn canonical_order_holds_in_both_separate_mode_files() {
    let d = TempDir::new();
    // Clean rows out of order, plus an errored row, all at one instant.
    let bytes = [
        record_at(21, 3, 500),
        record_at(3, 11, 500),
        errored_record(),
    ]
    .concat();
    let input = d.write("in.mie", &bytes);
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--separate-errors",
    ]);
    assert_eq!(exit_code(&o), 0);
    let main = std::fs::read_to_string(&out).unwrap();
    assert_eq!(
        csv_rt_msg(&main),
        vec![
            ("3".to_string(), "11R".to_string()),
            ("21".to_string(), "3R".to_string()),
        ],
        "the main CSV must be in canonical order"
    );
}

/// `--help` advertises the flag, which is what the conformance suite's
/// `check_cli_surface` gate reads to compare the two CLIs' flag sets.
///
/// Requirements: L3-WRT-003
#[test]
fn decode_help_advertises_max_sort_group() {
    let out = run(["decode", "--help"]);
    assert_eq!(exit_code(&out), 0);
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("--max-sort-group"),
        "decode --help must advertise --max-sort-group\n{stdout}"
    );
}

/// Requirements: L2-MRG-005, L3-WRT-004
#[test]
fn delta_scope_flag_and_config_key() {
    let d = TempDir::new();
    // Two files sharing RT20/SA5; A also holds RT15/SA11.
    let a = [
        record_at(15, 11, 100_000),
        record_at(20, 5, 100_000),
        record_at(15, 11, 300_000),
        record_at(20, 5, 300_000),
    ]
    .concat();
    let b = [record_at(20, 5, 200_000), record_at(20, 5, 400_000)].concat();
    let fa = d.write("a.mie", &a);
    let fb = d.write("b.mie", &b);

    // DELTA column of each row for the shared key, in output order.
    let shared_deltas = |csv: &str| -> Vec<String> {
        csv.lines()
            .skip(1)
            .filter(|l| !l.trim().is_empty())
            .map(|l| l.split(',').map(str::to_string).collect::<Vec<_>>())
            .filter(|c| c[1] == "20" && c[2] == "5R")
            .map(|c| c[40].clone())
            .collect()
    };

    // Default (per-file): each file keeps its own 0.2s cadence.
    let out = d.path().join("default.csv");
    let o = run([
        "decode",
        fa.to_str().unwrap(),
        fb.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--no-mux",
    ]);
    assert_eq!(exit_code(&o), 0);
    let got = shared_deltas(&std::fs::read_to_string(&out).unwrap());
    assert_eq!(
        got,
        vec!["0.000000", "0.000000", "0.200000", "0.200000"],
        "default must be per-file"
    );

    // --delta-scope global: gaps compress across the merged timeline.
    let out2 = d.path().join("global.csv");
    let o = run([
        "decode",
        fa.to_str().unwrap(),
        fb.to_str().unwrap(),
        "-o",
        out2.to_str().unwrap(),
        "--no-mux",
        "--delta-scope",
        "global",
    ]);
    assert_eq!(exit_code(&o), 0);
    assert_eq!(
        shared_deltas(&std::fs::read_to_string(&out2).unwrap()),
        vec!["0.000000", "0.100000", "0.100000", "0.100000"]
    );

    // The config key drives it, and the CLI flag overrides the config.
    let cfg = d.write("cfg.toml", b"[merge]\ndelta_scope = \"global\"\n");
    let out3 = d.path().join("cfgglobal.csv");
    let o = run([
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        fa.to_str().unwrap(),
        fb.to_str().unwrap(),
        "-o",
        out3.to_str().unwrap(),
        "--no-mux",
    ]);
    assert_eq!(exit_code(&o), 0);
    assert_eq!(
        shared_deltas(&std::fs::read_to_string(&out3).unwrap())[1],
        "0.100000",
        "config delta_scope = global must take effect"
    );

    let out4 = d.path().join("cliwins.csv");
    let o = run([
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        fa.to_str().unwrap(),
        fb.to_str().unwrap(),
        "-o",
        out4.to_str().unwrap(),
        "--no-mux",
        "--delta-scope",
        "per-file",
    ]);
    assert_eq!(exit_code(&o), 0);
    assert_eq!(
        shared_deltas(&std::fs::read_to_string(&out4).unwrap())[1],
        "0.000000",
        "--delta-scope must override the config value"
    );
}

/// An unrecognized scope is a usage error from the CLI and a config error from
/// TOML, matching how the other named-enum options behave.
///
/// Requirements: L2-MRG-005, L3-WRT-004
#[test]
fn delta_scope_rejects_unknown_names() {
    let d = TempDir::new();
    let input = d.write("in.mie", &one_valid_record());
    let out = d.path().join("out.csv");
    let o = run([
        "decode",
        input.to_str().unwrap(),
        "-o",
        out.to_str().unwrap(),
        "--delta-scope",
        "per_file",
    ]);
    assert_eq!(
        exit_code(&o),
        4,
        "an unknown --delta-scope is a usage error"
    );
    let stderr = String::from_utf8_lossy(&o.stderr);
    assert!(stderr.contains("delta-scope"), "got: {stderr}");

    let cfg = d.write("bad.toml", b"[merge]\ndelta_scope = \"whole\"\n");
    let out2 = d.path().join("out2.csv");
    let o = run([
        "--config",
        cfg.to_str().unwrap(),
        "decode",
        input.to_str().unwrap(),
        "-o",
        out2.to_str().unwrap(),
    ]);
    assert_ne!(exit_code(&o), 0);
    let stderr = String::from_utf8_lossy(&o.stderr);
    assert!(stderr.contains("delta_scope"), "got: {stderr}");
}

/// `--help` advertises the flag, which the conformance `check_cli_surface` gate
/// compares against the Python CLI.
///
/// Requirements: L3-WRT-004
#[test]
fn decode_help_advertises_delta_scope() {
    let out = run(["decode", "--help"]);
    assert_eq!(exit_code(&out), 0);
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(stdout.contains("--delta-scope"), "{stdout}");
}

// -- v3.0.0 timestamp rendering (L2-WRT-025 / L2-WRT-026 / L2-CLI-018/019) --

/// The default decode must be byte-identical to what pre-v3.0.0 emitted --
/// that is the whole point of `doy` being the default (L1-OUT-004), and it is
/// what keeps a no-flag decode diffable against vendor CSV.
///
/// Requirements: L2-WRT-011, L2-WRT-025
#[test]
fn default_rendering_is_unchanged_day_of_year() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let out = run(["decode", input.to_str().unwrap()]);
    assert_eq!(exit_code(&out), 0);
    let stdout = String::from_utf8_lossy(&out.stdout);
    let row = stdout.lines().nth(1).expect("a data row");
    assert!(
        row.starts_with("192:15:54:50.456225,"),
        "default rendering changed: {row}"
    );
}

/// The same instant renders one day apart either side of a leap day, which is
/// the whole reason a calendar rendering needs a year.
///
/// Requirements: L2-WRT-025, L2-WRT-026
#[test]
fn calendar_renderings_resolve_day_of_year_against_the_given_year() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let path = input.to_str().unwrap();

    let cases = [
        ("iso", "2026", None, "2026-07-11T15:54:50.456225Z"),
        ("iso", "2024", None, "2024-07-10T15:54:50.456225Z"),
        ("dom", "2026", None, "11:15:54:50.456225"),
        ("dom", "2024", None, "10:15:54:50.456225"),
        (
            "iso",
            "2026",
            Some("--utc-offset=-05:00"),
            "2026-07-11T15:54:50.456225-05:00",
        ),
        (
            "iso",
            "2026",
            Some("--utc-offset=+05:30"),
            "2026-07-11T15:54:50.456225+05:30",
        ),
    ];

    for (format, year, extra, expected) in cases {
        let mut args = vec![
            "decode",
            path,
            "--output-time-format",
            format,
            "--year",
            year,
        ];
        if let Some(flag) = extra {
            args.push(flag);
        }
        let out = run(&args);
        assert_eq!(exit_code(&out), 0, "{format} {year} {extra:?}");
        let stdout = String::from_utf8_lossy(&out.stdout);
        let row = stdout.lines().nth(1).expect("a data row");
        assert!(
            row.starts_with(&format!("{expected},")),
            "{format} {year} {extra:?} rendered {row}"
        );
    }
}

/// A calendar rendering with no year from either source is refused before the
/// output is opened, and the diagnostic names BOTH ways to supply one
/// (L2-WRT-026 clause 1).
///
/// Requirements: L2-WRT-026, L2-CLI-018
#[test]
fn calendar_rendering_without_a_year_is_a_usage_error() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());

    for format in ["iso", "dom"] {
        let output = tmp.path().join(format!("out-{format}.csv"));
        let out = run([
            std::ffi::OsStr::new("decode"),
            input.as_os_str(),
            std::ffi::OsStr::new("-o"),
            output.as_os_str(),
            std::ffi::OsStr::new("--output-time-format"),
            std::ffi::OsStr::new(format),
        ]);
        assert_eq!(exit_code(&out), 4, "{format} without a year must be usage");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(stderr.contains("--year"), "must name the flag\n{stderr}");
        assert!(
            stderr.contains("[output] year"),
            "must name the config key too\n{stderr}"
        );
        assert!(
            !output.exists(),
            "nothing may be written when the year is missing"
        );
    }
}

/// `doy` never needs a year, so nothing an operator runs today starts asking
/// for one, even with a year sitting in a site config (L2-WRT-026 clause 5).
///
/// Requirements: L2-WRT-026
#[test]
fn doy_ignores_year_and_offset_rather_than_rejecting_them() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let out = run([
        "decode",
        input.to_str().unwrap(),
        "--output-time-format",
        "doy",
        "--year",
        "2024",
        "--utc-offset=-05:00",
    ]);
    assert_eq!(exit_code(&out), 0);
    let stdout = String::from_utf8_lossy(&out.stdout);
    let row = stdout.lines().nth(1).expect("a data row");
    assert!(
        row.starts_with("192:15:54:50.456225,"),
        "an inert year/offset must not change doy: {row}"
    );
}

/// Both new value-taking flags are range-checked at parse time, so a bad value
/// is a usage error rather than a malformed cell far into the output.
///
/// Requirements: L2-CLI-018
#[test]
fn new_flag_values_are_validated_at_parse_time() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let path = input.to_str().unwrap();

    let bad = [
        ("--output-time-format", "elapsed"),
        ("--output-time-format", ""),
        ("--year", "0"),
        ("--year", "10000"),
        ("--year", "-1"),
        ("--year", "twenty"),
        ("--utc-offset", "+24:00"),
        ("--utc-offset", "+05:60"),
        ("--utc-offset", "+5:00"),
        ("--utc-offset", "0500"),
        ("--utc-offset", "PST"),
    ];

    for (flag, value) in bad {
        let out = run(["decode", path, flag, value]);
        assert_eq!(exit_code(&out), 4, "{flag} {value:?} should be rejected");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains(flag),
            "the diagnostic should name {flag}\n{stderr}"
        );
    }
}

/// `--time-format` was split in v3.0.0. Unlike `--inline-errors` it has a
/// successor -- two of them -- so the diagnostic must say which is which. That
/// is the one question a generic "unknown option" cannot answer (L2-CLI-019).
///
/// Requirements: L2-CLI-019
#[test]
fn retired_time_format_flag_names_both_replacements() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let output = tmp.path().join("out.csv");

    // Both the space-separated and the joined spelling land on the arm.
    for spelling in ["--time-format", "--time-format=irig"] {
        let out = run([
            std::ffi::OsStr::new("decode"),
            input.as_os_str(),
            std::ffi::OsStr::new("-o"),
            output.as_os_str(),
            std::ffi::OsStr::new(spelling),
            std::ffi::OsStr::new("irig"),
        ]);
        assert_eq!(exit_code(&out), 4, "{spelling} must be a usage error");
        let stderr = String::from_utf8_lossy(&out.stderr);
        for expected in [
            "--time-format",
            "--input-time-format",
            "--output-time-format",
        ] {
            assert!(
                stderr.contains(expected),
                "{spelling}: diagnostic should mention {expected}\n{stderr}"
            );
        }
        assert!(!output.exists(), "no output on a retired-flag usage error");
    }
}

/// Every new flag must appear in `decode --help`: the cross-implementation
/// parity gate scrapes help text, so a flag the parser accepts but help does
/// not advertise fails the conformance run.
///
/// Requirements: L2-CLI-018
#[test]
fn decode_help_advertises_the_timestamp_flags() {
    let out = run(["decode", "--help"]);
    assert_eq!(exit_code(&out), 0);
    let stdout = String::from_utf8_lossy(&out.stdout);
    for flag in [
        "--input-time-format",
        "--output-time-format",
        "--year",
        "--utc-offset",
    ] {
        assert!(
            stdout.contains(flag),
            "help must advertise {flag}\n{stdout}"
        );
    }
}

/// The day-of-year advisory follows the rendering: `INFO` under `doy`, where a
/// skewed day is visibly a day number, and `WARNING` under a calendar
/// rendering, where the same skew is resolved into something that reads as a
/// fact. The opt-out silences it either way.
///
/// Requirements: L2-LOG-001, L2-LOG-002
#[test]
fn day_of_year_advisory_escalates_under_a_calendar_rendering() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let path = input.to_str().unwrap();

    // `doy` at the default level: silent (the advisory is INFO).
    let quiet = run(["decode", path]);
    let quiet_err = String::from_utf8_lossy(&quiet.stderr);
    assert!(
        !quiet_err.contains("day-of-year"),
        "the advisory must stay out of a default doy decode\n{quiet_err}"
    );

    // `doy` at INFO: present, and at INFO.
    let info = run(["--log-level", "INFO", "decode", path]);
    let info_err = String::from_utf8_lossy(&info.stderr);
    let advisory = info_err
        .lines()
        .find(|line| line.contains("day-of-year"))
        .unwrap_or_else(|| panic!("expected the advisory at INFO\n{info_err}"));
    assert!(advisory.contains("INFO"), "expected INFO, got: {advisory}");

    // A calendar rendering at the DEFAULT level: present, and at WARN.
    let calendar = run([
        "decode",
        path,
        "--output-time-format",
        "iso",
        "--year",
        "2026",
    ]);
    assert_eq!(exit_code(&calendar), 0);
    let calendar_err = String::from_utf8_lossy(&calendar.stderr);
    let escalated = calendar_err
        .lines()
        .find(|line| line.contains("day-of-year"))
        .unwrap_or_else(|| panic!("expected the advisory at the default level\n{calendar_err}"));
    assert!(
        escalated.contains("WARN"),
        "expected WARN under a calendar rendering, got: {escalated}"
    );

    // The opt-out still wins, under the calendar rendering too.
    let opted_out = run([
        "--no-irig-day-advisory",
        "decode",
        path,
        "--output-time-format",
        "iso",
        "--year",
        "2026",
    ]);
    let opted_out_err = String::from_utf8_lossy(&opted_out.stderr);
    assert!(
        !opted_out_err.contains("day-of-year"),
        "the opt-out must suppress it at every level and rendering\n{opted_out_err}"
    );
}

/// A file name that is not valid Unicode: a lone `0xFF` byte on POSIX, an
/// unpaired surrogate on Windows. Both are legal names on their platform, and
/// neither survives conversion to a Rust `String`.
fn non_utf8_name(stem: &str, ext: &str) -> std::ffi::OsString {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        let mut bytes = stem.as_bytes().to_vec();
        bytes.push(0xFF);
        bytes.extend_from_slice(ext.as_bytes());
        std::ffi::OsString::from_vec(bytes)
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt;
        let mut wide: Vec<u16> = stem.encode_utf16().collect();
        wide.push(0xD800);
        wide.extend(ext.encode_utf16());
        std::ffi::OsString::from_wide(&wide)
    }
}

/// Requirements: L2-CLI-021
///
/// A recording, an output and a config whose names are not UTF-8 are named
/// on the command line and used as given -- `decode`, `count` and `dump`,
/// with `--output` both space-separated and `=`-joined. `main` used to collect
/// `std::env::args()`, which panics (exit 101) on the first such argument; the
/// C++ CLI decoded the same file.
#[test]
fn non_utf8_path_arguments_are_used_as_given() {
    let tmp = TempDir::new();
    let mut bytes = one_valid_record();
    bytes.extend(one_valid_record());
    let input = tmp.path().join(non_utf8_name("in", ".mie"));
    std::fs::write(&input, &bytes).unwrap();
    let config = tmp.path().join(non_utf8_name("cfg", ".toml"));
    std::fs::write(&config, b"[decode]\nstrict = false\n").unwrap();

    let output = tmp.path().join(non_utf8_name("out", ".csv"));
    let out = run([
        std::ffi::OsStr::new("--config"),
        config.as_os_str(),
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        std::ffi::OsStr::new("-o"),
        output.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);
    let text = std::fs::read_to_string(&output).expect("the output is written under its own name");
    assert_eq!(csv_rt_msg(&text).len(), 2);

    let joined = tmp.path().join(non_utf8_name("joined", ".csv"));
    let mut flag = std::ffi::OsString::from("--output=");
    flag.push(joined.as_os_str());
    let out = run([
        std::ffi::OsStr::new("decode"),
        input.as_os_str(),
        flag.as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 0);
    assert!(
        joined.exists(),
        "the =-joined form keeps the name's bytes too"
    );

    let out = run([std::ffi::OsStr::new("count"), input.as_os_str()]);
    assert_eq!(exit_code(&out), 0);
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "2");

    let out = run([
        std::ffi::OsStr::new("dump"),
        input.as_os_str(),
        std::ffi::OsStr::new("--records"),
        std::ffi::OsStr::new("1"),
    ]);
    assert_eq!(exit_code(&out), 0);
    assert!(
        String::from_utf8_lossy(&out.stdout).contains("Record #0"),
        "dump reads the file it was given"
    );
}

/// Requirements: L2-CLI-014, L2-CLI-021
///
/// Only a path may be non-UTF-8. Any other value that is not is a usage error
/// -- not a panic, and not the escaped text silently used as a pattern or a
/// delimiter -- and every message that quotes such an argument is ASCII.
#[test]
fn non_utf8_non_path_values_are_usage_errors() {
    let tmp = TempDir::new();
    let input = tmp.write("rec.mie", &one_valid_record());
    let bad = non_utf8_name("x", "");
    for flag in ["--glob", "--mux-delimiter", "--year", "--log-level"] {
        let out = if flag == "--log-level" {
            run([
                std::ffi::OsStr::new(flag),
                bad.as_os_str(),
                std::ffi::OsStr::new("count"),
                input.as_os_str(),
            ])
        } else {
            run([
                std::ffi::OsStr::new("decode"),
                input.as_os_str(),
                std::ffi::OsStr::new(flag),
                bad.as_os_str(),
            ])
        };
        assert_eq!(exit_code(&out), 4, "{flag}");
        assert!(out.stderr.is_ascii(), "{flag}: stderr must be ASCII");
    }

    // A non-UTF-8 token where a subcommand is expected is quoted in its
    // escaped, ASCII form.
    let out = run([bad.as_os_str()]);
    assert_eq!(exit_code(&out), 4);
    assert!(out.stderr.is_ascii());
    let stderr = String::from_utf8_lossy(&out.stderr);
    #[cfg(unix)]
    assert!(stderr.contains("xFF"), "{stderr}");
    #[cfg(windows)]
    assert!(stderr.contains("u{d800}"), "{stderr}");

    // As a positional it is a path: a missing file is the ordinary runtime
    // failure (exit 1), not a panic.
    let out = run([
        std::ffi::OsStr::new("decode"),
        bad.as_os_str(),
        std::ffi::OsStr::new("-o"),
        tmp.path().join("out.csv").as_os_str(),
    ]);
    assert_eq!(exit_code(&out), 1);
}
