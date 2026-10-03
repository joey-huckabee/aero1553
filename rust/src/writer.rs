//! Streaming CSV writer.
//!
//! Rows are written directly to a `Write` impl as they are produced — no
//! intermediate `Vec<Row>` or `DataFrame` buffering. Memory usage is constant
//! regardless of file size.
//!
//! Column order matches the DDC vendor CSV byte-for-byte. `TERM_NAME`,
//! `IM_GAP`, `RCV_GAP`, and `XMT_GAP` are emitted as empty strings — they exist
//! for layout compatibility, not because we populate them. `MUX` is the
//! exception: it carries a value derived from the input file name by default
//! (L2-WRT-020), and is empty only when that is disabled (`--no-mux`) or the
//! configured field is absent.

use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::error::{MieError, MieResult};
use crate::models::{CalendarError, MAX_DATA_WORDS, MieMessage, TimeRender};
use crate::{log_info, log_warn};

// ── Path identity (L2-WRT-014) ────────────────────────────────────────

/// Test whether `input` and `output` resolve to the same file.
///
/// Handles the common case where `output` does not yet exist by
/// canonicalizing the output's parent directory and joining the
/// filename. Returns `Ok(false)` whenever either path or its parent
/// cannot be canonicalized — collision is only positive when both
/// resolve to the same identity.
///
/// This is intentionally symlink-safe (via `fs::canonicalize`) so that
/// `/tmp/in.mie` aliasing `/var/foo/in.mie` is detected.
///
/// # Errors
///
/// Returns the [`io::Error`] from canonicalizing the **input**, which must
/// exist. A destination that cannot be canonicalized is not an error — it
/// cannot collide, so the answer is `Ok(false)`.
pub fn paths_refer_to_same_file(input: &Path, output: &Path) -> io::Result<bool> {
    let input_canon = std::fs::canonicalize(input)?;

    // Direct path: both files exist on disk.
    if let Ok(out_canon) = std::fs::canonicalize(output) {
        return Ok(input_canon == out_canon);
    }

    // Output doesn't exist yet (the common case). Canonicalize the
    // parent and join the filename. If the parent itself doesn't exist,
    // there can be no collision because the output isn't reachable.
    let Some(parent) = output.parent() else {
        return Ok(false);
    };
    let parent = if parent.as_os_str().is_empty() {
        Path::new(".")
    } else {
        parent
    };
    let Ok(parent_canon) = std::fs::canonicalize(parent) else {
        return Ok(false);
    };
    let Some(filename) = output.file_name() else {
        return Ok(false);
    };
    Ok(input_canon == parent_canon.join(filename))
}

// ── Commit modes (L2-WRT-015, L2-WRT-016, L2-WRT-023) ─────────────────

/// How a finished temp file is moved onto its destination.
///
/// The distinction is the whole of L2-WRT-023. `Replace` is the shipped
/// default: overwriting an existing destination is what an operator re-running
/// a batch expects. `NoReplace` is what `--no-clobber` selects, and it has to
/// be enforced *by the commit itself* — an `exists()` test before the file is
/// opened answers a question about the past, and between that answer and the
/// rename any other process may create the destination. The rename then
/// destroys it, which is the exact outcome the flag exists to prevent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommitMode {
    /// `rename(2)` / `MoveFileEx(MOVEFILE_REPLACE_EXISTING)`: an existing
    /// destination is replaced.
    Replace,
    /// The destination is claimed atomically, and an existing one is refused
    /// rather than overwritten.
    NoReplace,
}

/// What a `NoReplace` commit did.
enum NoReplaceOutcome {
    /// The destination now holds the temp file's contents.
    Committed,
    /// The destination already existed. Nothing was written to it.
    Exists,
    /// The commit failed for a reason other than the destination existing.
    Failed(io::Error),
}

/// Move `temp` onto `dest` without ever replacing an existing file.
///
/// Two mechanisms, in order of preference:
///
/// 1. **`hard_link` + unlink.** `link(2)` — and `CreateHardLinkW`, which is
///    what `std::fs::hard_link` calls on Windows — fails with `EEXIST` /
///    `ERROR_ALREADY_EXISTS` when the destination exists, and otherwise
///    publishes the *complete* file under its final name in one atomic step.
///    A concurrent reader watching for the destination therefore never sees a
///    partial or empty file, exactly as with the replacing rename.
/// 2. **Exclusive-create reservation, then a replacing rename.** Hard links do
///    not exist on FAT/exFAT and are refused by some network filesystems, so
///    the link can fail for reasons that have nothing to do with the
///    destination. `create_new` claims the name atomically — one of two racing
///    processes wins it — and the rename that follows overwrites only *our own*
///    zero-byte reservation. The narrow cost is that the destination is briefly
///    an empty file, which is why this is the fallback and not the primary.
///
/// Note that `std::fs::rename` cannot implement this on its own in either
/// direction: it replaces on POSIX and on Windows alike, and the no-replace
/// syscalls that do exist (`renameat2(RENAME_NOREPLACE)`, `MoveFileExW` with no
/// flags) are reachable only through a `libc`/`windows-sys` dependency this
/// crate does not have and will not add.
fn commit_no_replace(temp: &Path, dest: &Path) -> NoReplaceOutcome {
    match std::fs::hard_link(temp, dest) {
        Ok(()) => NoReplaceOutcome::Committed,
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => NoReplaceOutcome::Exists,
        Err(_) => reserve_then_rename(temp, dest),
    }
}

/// The fallback half of [`commit_no_replace`], split out so the primary path
/// reads as three lines rather than three lines wrapped around a nested match.
fn reserve_then_rename(temp: &Path, dest: &Path) -> NoReplaceOutcome {
    match OpenOptions::new().write(true).create_new(true).open(dest) {
        Ok(reservation) => {
            drop(reservation);
            match std::fs::rename(temp, dest) {
                Ok(()) => NoReplaceOutcome::Committed,
                Err(e) => {
                    // Take the reservation back out. Leaving it would hand the
                    // operator an empty CSV where the failure message says
                    // nothing was written.
                    let _ = std::fs::remove_file(dest);
                    NoReplaceOutcome::Failed(e)
                }
            }
        }
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => NoReplaceOutcome::Exists,
        Err(e) => NoReplaceOutcome::Failed(e),
    }
}

// ── AtomicCsvFile (L2-WRT-015, L2-WRT-016) ────────────────────────────

/// Write a CSV to a temp file in the destination's directory, then
/// `rename()` atomically over the destination on successful commit.
///
/// On Drop without commit (i.e., decode failed or was interrupted),
/// the temp file is unlinked. The destination file — if it already
/// existed — is never touched on the failure path.
///
/// Rename is atomic on POSIX (`rename(2)`) and on NTFS within the
/// same volume (`MoveFileEx` with replace). Keeping the temp file
/// in the destination's parent guarantees same-volume placement.
///
/// Under [`AtomicCsvFile::no_clobber`] every commit — over the destination and
/// onto `<destination>.partial` alike — refuses an existing target instead of
/// replacing it (L2-WRT-023). That refusal is the guarantee; the pre-flight
/// `exists()` test is only an early, friendlier report of the same condition.
pub struct AtomicCsvFile {
    final_path: PathBuf,
    temp_path: PathBuf,
    /// Owned via `Option` so `commit()` can move out the writer and
    /// run `BufWriter::into_inner()` without partially-moving `self`
    /// (which Drop would object to).
    writer: Option<BufWriter<File>>,
    committed: bool,
    /// Selects the commit mode. `Replace` unless the caller opts in through
    /// [`AtomicCsvFile::no_clobber`], so the default stays L2-WRT-017's
    /// "overwrite succeeds by default".
    mode: CommitMode,
}

impl AtomicCsvFile {
    /// # Errors
    ///
    /// Returns [`MieError::WriterError`] if the temp file beside the
    /// destination cannot be created — no write permission on the directory, or
    /// every candidate temp name already taken.
    pub fn create(final_path: PathBuf) -> MieResult<Self> {
        let (temp_path, file) = create_exclusive_temp(&final_path, || make_temp_path(&final_path))
            .map_err(|(path, source)| MieError::WriterError {
                destination: path.display().to_string(),
                source,
            })?;
        Ok(Self {
            final_path,
            temp_path,
            writer: Some(BufWriter::new(file)),
            committed: false,
            mode: CommitMode::Replace,
        })
    }

    /// Select L2-WRT-023's no-replace commit for **every** target this writer
    /// can produce — the destination itself and `<destination>.partial` alike.
    ///
    /// A builder rather than a `create` parameter: `create` is public API, and
    /// growing its signature is a break the semver gate would be right to
    /// reject.
    #[must_use]
    pub fn no_clobber(mut self, yes: bool) -> Self {
        self.mode = if yes {
            CommitMode::NoReplace
        } else {
            CommitMode::Replace
        };
        self
    }

    /// Flush, close the temp file, and atomically rename it over the
    /// final destination. After a successful commit the temp file no
    /// longer exists so Drop's cleanup becomes a no-op.
    ///
    /// # Errors
    ///
    /// Returns [`MieError::WriterError`] if the final flush fails, the rename
    /// over the destination fails, or `commit` is called twice. Under
    /// [`AtomicCsvFile::no_clobber`], returns [`MieError::ClobberRefused`] when
    /// the destination exists at the moment of the commit (L2-WRT-023).
    pub fn commit(mut self) -> MieResult<()> {
        let destination = self.final_path.clone();
        self.commit_onto(&destination)
    }

    /// Flush, close the temp file, and atomically rename it to
    /// `<final_path>.partial` rather than over the final destination.
    /// Used by L2-WRT-016's `--allow-partial` branch: the original
    /// destination (if it existed) remains untouched, and the operator
    /// gets the decoded-so-far rows in the .partial file. Returns the
    /// path written so callers can log it.
    ///
    /// # Errors
    ///
    /// As [`AtomicCsvFile::commit`], but the rename targets
    /// `<destination>.partial`; the destination itself is left untouched. Under
    /// [`AtomicCsvFile::no_clobber`] an existing `.partial` is refused rather
    /// than replaced: it is an actual commit target, so L2-WRT-023 covers it
    /// like any other.
    pub fn commit_partial(mut self) -> MieResult<PathBuf> {
        // `<dest>.partial` lives in the destination directory by
        // construction (final_path itself does), so the rename stays on
        // one filesystem and is atomic. The name comes from
        // `partial_path_for` rather than being built here, so the path
        // this commits to and the path `commit_targets` pre-flights are
        // the same derivation and cannot drift (L2-WRT-014).
        let partial = partial_path_for(&self.final_path);
        self.commit_onto(&partial)?;
        Ok(partial)
    }

    /// The one commit sequence, shared by [`AtomicCsvFile::commit`] and
    /// [`AtomicCsvFile::commit_partial`] so the two cannot drift in how they
    /// flush, close, or honour [`CommitMode`]. Each used to spell the sequence
    /// out for itself, which is how a rule can end up applying to one commit
    /// target and not the other.
    ///
    /// Takes `&mut self` rather than `self` because both callers own the value
    /// and want it dropped — with `committed` settled — on the way out.
    fn commit_onto(&mut self, destination: &Path) -> MieResult<()> {
        let Some(writer) = self.writer.take() else {
            return Err(MieError::WriterError {
                destination: self.final_path.display().to_string(),
                source: io::Error::other("AtomicCsvFile committed without an active writer"),
            });
        };
        let temp_for_err = self.temp_path.display().to_string();
        let file = writer.into_inner().map_err(|e| MieError::WriterError {
            destination: temp_for_err,
            source: e.into_error(),
        })?;
        // Closing the File before rename matters on Windows: NTFS will
        // not rename a file that has an open handle. POSIX is fine
        // either way, but explicit close keeps platforms aligned.
        drop(file);

        match self.mode {
            CommitMode::Replace => {
                std::fs::rename(&self.temp_path, destination).map_err(|source| {
                    MieError::WriterError {
                        destination: destination.display().to_string(),
                        source,
                    }
                })?;
                self.committed = true;
            }
            CommitMode::NoReplace => match commit_no_replace(&self.temp_path, destination) {
                NoReplaceOutcome::Committed => {
                    // The link mechanism leaves the temp behind on purpose: it
                    // published a second name for the same bytes rather than
                    // moving them. Unlinking is best effort — the destination
                    // is committed either way, and leaving `committed` false
                    // when the unlink fails just gives Drop a second attempt.
                    self.committed = std::fs::remove_file(&self.temp_path).is_ok();
                }
                NoReplaceOutcome::Exists => {
                    return Err(MieError::ClobberRefused {
                        path: destination.to_path_buf(),
                    });
                }
                NoReplaceOutcome::Failed(source) => {
                    return Err(MieError::WriterError {
                        destination: destination.display().to_string(),
                        source,
                    });
                }
            },
        }
        Ok(())
    }

    #[must_use]
    pub fn final_path(&self) -> &Path {
        &self.final_path
    }
}

impl Write for AtomicCsvFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self.writer.as_mut() {
            Some(writer) => writer.write(buf),
            None => Err(io::Error::other("AtomicCsvFile::write after commit")),
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        match self.writer.as_mut() {
            Some(writer) => writer.flush(),
            None => Err(io::Error::other("AtomicCsvFile::flush after commit")),
        }
    }
}

impl Drop for AtomicCsvFile {
    fn drop(&mut self) {
        // Take and drop the writer explicitly so the file handle is
        // closed before we try to unlink (matters on Windows).
        let _ = self.writer.take();
        if !self.committed {
            // Best-effort cleanup. If the temp file is already gone
            // (e.g. commit succeeded part-way then failed in rename
            // and we're cleaning up here defensively), this is a no-op.
            let _ = std::fs::remove_file(&self.temp_path);
        }
    }
}

/// Exclusively create (`create_new` == `O_CREAT|O_EXCL`) a temp file, retrying
/// with a fresh name from `next_temp` on a name clash. Never opens an existing
/// file, so two writers targeting the same destination cannot collide and
/// clobber each other. Returns the created path + open file, or the
/// (path-for-error, io error) on a non-clash failure or after exhausting
/// retries. `next_temp` is injected so the retry/exhaustion paths are testable.
fn create_exclusive_temp(
    final_path: &Path,
    mut next_temp: impl FnMut() -> PathBuf,
) -> Result<(PathBuf, File), (PathBuf, io::Error)> {
    let mut last = final_path.to_path_buf();
    for _ in 0..TEMP_CREATE_MAX_ATTEMPTS {
        let temp_path = next_temp();
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp_path)
        {
            Ok(file) => return Ok((temp_path, file)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                last = temp_path;
            }
            Err(source) => return Err((temp_path, source)),
        }
    }
    Err((
        last,
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not create a unique temp file",
        ),
    ))
}

/// Process-global monotonic counter feeding the temp-file salt, so two writers
/// created in one process never derive the same name. Mirrors the Python
/// `_temp_counter`.
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Bound on exclusive-create retries before giving up (a reused PID may leave a
/// stale temp; each retry advances the counter so it converges immediately).
const TEMP_CREATE_MAX_ATTEMPTS: u32 = 128;

/// Construct a temp file path:
/// `<destination>.aero1553.tmp.<pid>.<counter>.<nanos>` in the destination's
/// parent directory. Same-directory placement guarantees the subsequent
/// `rename()` lives on one filesystem and is therefore atomic; the per-process
/// counter plus wall-clock nanoseconds make each call unique and hard to
/// predict, so two writers targeting the same destination cannot derive the
/// same name. The caller still creates it with `create_new` (`O_EXCL`) as the
/// guarantee.
fn make_temp_path(final_path: &Path) -> PathBuf {
    let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let mut name = final_path
        .file_name()
        .map(std::ffi::OsStr::to_os_string)
        .unwrap_or_default();
    name.push(format!(
        ".aero1553.tmp.{}.{counter}.{nanos}",
        std::process::id()
    ));
    match final_path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.join(name),
        _ => PathBuf::from(name),
    }
}

/// Number of leading columns that make up the DDC vendor layout (L1-OUT-001).
/// Everything past this index is a decoder addition appended at the tail.
/// Shared in value with the Python implementation's `VENDOR_COLUMN_COUNT`.
pub const VENDOR_COLUMN_COUNT: usize = 44;

/// Header row written before the first data row. Public so callers can
/// embed it elsewhere if needed.
///
/// Two blocks, in this order (L2-WRT-001): the 44-column DDC vendor block
/// (`TIME_STAMP` through `XMT_GAP`), whose order is dictated by the vendor CSV,
/// then the decoder-added `ERROR` / `ERROR_CODE`, which have no vendor
/// counterpart. Keeping additions at the tail is what makes column *N* of a
/// decoded CSV the same field as column *N* of a vendor CSV for all 44.
pub const CSV_HEADER: &str = concat!(
    "TIME_STAMP,RT,MSG,",
    "WD01,WD02,WD03,WD04,WD05,WD06,WD07,WD08,WD09,WD10,",
    "WD11,WD12,WD13,WD14,WD15,WD16,WD17,WD18,WD19,WD20,",
    "WD21,WD22,WD23,WD24,WD25,WD26,WD27,WD28,WD29,WD30,",
    "WD31,WD32,",
    "STAT,CMD,MUX,TERM_NAME,BUS,DELTA,IM_GAP,RCV_GAP,XMT_GAP,ERROR,ERROR_CODE\n",
);

/// Streaming CSV row writer.
pub struct CsvWriter<W: Write> {
    out: W,
    rows_written: u64,
    destination: String,
    /// L2-WRT-025: how the `TIME_STAMP` column is rendered. Defaults to
    /// day-of-year; set with [`CsvWriter::with_time_render`].
    time_render: TimeRender,
}

impl<W: Write> CsvWriter<W> {
    /// Create a writer and emit the header row immediately.
    ///
    /// # Errors
    ///
    /// Returns [`MieError::WriterError`] if the header row cannot be written.
    pub fn new(out: W, destination: impl Into<String>) -> MieResult<Self> {
        let mut w = Self {
            out,
            rows_written: 0,
            destination: destination.into(),
            time_render: TimeRender::doy(),
        };
        w.write_str(CSV_HEADER)?;
        Ok(w)
    }

    /// Select the `TIME_STAMP` rendering (L2-WRT-025).
    ///
    /// A builder rather than a `new` parameter so that every existing caller
    /// keeps producing the vendor-compatible day-of-year form untouched --
    /// which is the same reason `doy` is the CLI default (L1-OUT-004).
    #[must_use]
    pub fn with_time_render(mut self, render: TimeRender) -> Self {
        self.time_render = render;
        self
    }

    /// # Errors
    ///
    /// Returns [`MieError::WriterError`] if the row cannot be written. A closed
    /// downstream pipe arrives here with its kind preserved, so the CLI can
    /// treat it as a clean exit (L2-WRT-018).
    pub fn write_message(&mut self, msg: &MieMessage) -> MieResult<()> {
        // L2-WRT-025: the timestamp is rendered BEFORE any byte of the row is
        // written, because a calendar rendering can fail where `doy` cannot --
        // a day-of-year the configured year does not have (L2-WRT-026 clause
        // 3). Resolving it first keeps that refusal from leaving a half-written
        // line in the output.
        let timestamp = msg.timestamp.format_with(self.time_render).map_err(|e| {
            MieError::CalendarUnavailable {
                detail: describe_calendar_error(e, msg.file_offset),
            }
        })?;
        write_row(&mut self.out, msg, &timestamp).map_err(|source| MieError::WriterError {
            destination: self.destination.clone(),
            source,
        })?;
        self.rows_written += 1;
        Ok(())
    }

    /// # Errors
    ///
    /// Returns [`MieError::WriterError`] if the final flush fails. Flushing is
    /// part of the contract, not a courtesy: rows buffered and never written
    /// would otherwise be reported as a successful decode.
    pub fn finish(mut self) -> MieResult<u64> {
        self.out.flush().map_err(|source| MieError::WriterError {
            destination: self.destination.clone(),
            source,
        })?;
        Ok(self.rows_written)
    }

    pub fn rows_written(&self) -> u64 {
        self.rows_written
    }

    fn write_str(&mut self, s: &str) -> MieResult<()> {
        self.out
            .write_all(s.as_bytes())
            .map_err(|source| MieError::WriterError {
                destination: self.destination.clone(),
                source,
            })
    }
}

/// Write a CSV field with RFC4180 minimal quoting, matching Python's
/// `csv.QUOTE_MINIMAL`: a value containing the delimiter (`,`), a double quote,
/// or a line break is wrapped in double quotes with internal quotes doubled;
/// plain values are written verbatim. Only the MUX cell can carry such a value,
/// so this keeps MUX output byte-identical across implementations.
fn write_csv_field<W: Write>(out: &mut W, value: &str) -> std::io::Result<()> {
    if value.contains([',', '"', '\n', '\r']) {
        out.write_all(b"\"")?;
        let mut buf = [0u8; 4];
        for ch in value.chars() {
            if ch == '"' {
                out.write_all(b"\"\"")?;
            } else {
                out.write_all(ch.encode_utf8(&mut buf).as_bytes())?;
            }
        }
        out.write_all(b"\"")?;
    } else {
        out.write_all(value.as_bytes())?;
    }
    Ok(())
}

/// Turn a [`CalendarError`] into the operator-facing `detail` of a
/// [`MieError::CalendarUnavailable`], naming the record it came from.
fn describe_calendar_error(err: CalendarError, file_offset: u64) -> String {
    match err {
        CalendarError::MissingYear => "no year was configured; set [output] year \
             or pass --year YYYY"
            .to_string(),
        CalendarError::NoSuchDay { day, year } => format!(
            "the record at offset 0x{file_offset:X} carries day-of-year {day}, which \
             does not exist in {year} ({year} is not a leap year). The configured \
             year is wrong for this recording"
        ),
        CalendarError::NotCalendarLocked => "this recording uses the Standard \
             timestamp encoding, a free-running counter with no epoch. No year \
             places it on a calendar"
            .to_string(),
        CalendarError::FreerunNotAnchored => format!(
            "the record at offset 0x{file_offset:X} carries a freerun IRIG \
             timestamp -- the card had no valid IRIG-B lock, so its day and time \
             fields are relative rather than calendar-anchored"
        ),
    }
}

/// Write one CSV row. `timestamp` is rendered by the caller (see
/// [`CsvWriter::write_message`]) so that a failed calendar resolution cannot
/// leave a partly-written row behind.
fn write_row<W: Write>(out: &mut W, msg: &MieMessage, timestamp: &str) -> std::io::Result<()> {
    // TIME_STAMP
    out.write_all(timestamp.as_bytes())?;
    out.write_all(b",")?;

    // RT
    if let Some(rt) = msg.rt() {
        write!(out, "{rt}")?;
    }
    out.write_all(b",")?;

    // MSG
    out.write_all(msg.msg_label().as_bytes())?;
    out.write_all(b",")?;

    // WD01..WD32
    for i in 0..MAX_DATA_WORDS {
        if let Some(&w) = msg.data_words.as_slice().get(i) {
            write!(out, "{w:04X}")?;
        }
        out.write_all(b",")?;
    }

    // STAT
    if let Some(s) = msg.status_word {
        write!(out, "{s:04X}")?;
    }
    out.write_all(b",")?;

    // CMD
    if let Some(cw) = msg.command_word {
        write!(out, "{:04X}", cw.raw)?;
    }
    out.write_all(b",")?;

    // MUX (L2-WRT-020: derived from the file name; empty when disabled or the
    // configured field is absent), then TERM_NAME (always empty).
    if let Some(mux) = &msg.mux {
        write_csv_field(out, mux)?;
    }
    out.write_all(b",,")?;

    // BUS
    out.write_all(msg.bus().as_str().as_bytes())?;
    out.write_all(b",")?;

    // DELTA — empty cell when delta is None (SPURIOUS_DATA, uncalibrated
    // Standard timestamps, or non-monotonic timestamps).
    if let Some(d) = msg.delta {
        write!(out, "{d:.6}")?;
    }
    out.write_all(b",")?;

    // IM_GAP, RCV_GAP, XMT_GAP (always empty) — the tail of the 44-column
    // vendor block (L2-WRT-001).
    out.write_all(b",,,")?;

    // ERROR, then ERROR_CODE — decoder-added columns with no vendor
    // counterpart, appended AFTER the vendor block so column N of a decoded
    // CSV is column N of the vendor CSV for all 44 (L1-OUT-001).
    out.write_all(msg.error_label().as_bytes())?;
    out.write_all(b",")?;

    if let Some(c) = msg.error_word {
        write!(out, "{c:04X}")?;
    }
    out.write_all(b"\n")?;

    Ok(())
}

// ── Top-level entry points matching the Python API ────────────────────

/// Output-side options controlling safety checks enforced by `write_csv`
/// and `write_csv_split`. Default is "no checks" so library callers that
/// want raw behavior still get it.
#[derive(Debug, Clone, Default)]
pub struct WriteOptions {
    /// Input path used for the L2-WRT-014 same-file collision check.
    /// `None` skips the check (typically when the caller has already
    /// validated, or there is no associated input file context).
    pub input_path: Option<PathBuf>,
    /// L2-WRT-017: refuse to overwrite an existing destination.
    pub no_clobber: bool,
    /// L1-EXIT-004 / L2-WRT-016: when the decode hits an unrecoverable mid-
    /// file sync loss, commit the rows decoded so far as
    /// `<destination>.partial` and treat the run as successful (exit 0)
    /// rather than unlinking the temp + propagating the error (exit 3).
    pub allow_partial: bool,
    /// L2-WRT-025: how the `TIME_STAMP` column is rendered. Defaults to the
    /// day-of-year form, so a caller that does not set it gets exactly the
    /// vendor-compatible output every version before v3.0.0 produced.
    pub time_render: TimeRender,
}

/// Outcome of a successful CSV write. `partial` is `Some(_)` when
/// `WriteOptions.allow_partial` was set and the decode hit an
/// `UnrecoverableSyncLoss` — the rows decoded so far have been
/// committed to the `.partial` path captured in `PartialCommit`.
/// `partial` is `None` for a complete (or completely-recovered)
/// decode; the CLI distinguishes Complete from `PartialRecovered` by
/// querying `MieFileReader::sync_losses()` post-iteration.
#[derive(Debug)]
pub struct WriteOutcome {
    pub normal_count: u64,
    pub error_count: u64,
    pub partial: Option<PartialCommit>,
}

/// Records where the partial output landed when `allow_partial`
/// converted an `UnrecoverableSyncLoss` into a successful exit.
/// `errors_path` is `Some(_)` only when split-mode produced any
/// errored/spurious rows before the sync loss.
#[derive(Debug)]
pub struct PartialCommit {
    pub main_path: PathBuf,
    pub errors_path: Option<PathBuf>,
    pub offset: u64,
    pub sync_losses: u64,
}

/// Pre-flight checks shared by file-output entry points. Runs the
/// L2-WRT-014 input/output identity test and the L2-WRT-017 no-clobber
/// gate, in that order. No filesystem state is mutated; this only
/// produces an error before any output file is opened.
///
/// `split_errors` says whether the caller is `write_csv_split`, which decides
/// both which paths can be committed (L2-WRT-014, via [`commit_targets`]) and
/// whether the derived errors destination gets its own no-clobber check.
///
/// The collision test covers **every** commit target and **is** the guarantee:
/// L2-WRT-014 is a pre-open rule, and once the mapping is live there is nothing
/// left to refuse.
///
/// The no-clobber test here is **not** the guarantee — that lives in the commit
/// (L2-WRT-023, [`CommitMode::NoReplace`]). `exists()` answers a question about
/// the past, and between the answer and the rename any other process may create
/// the destination. What this test buys is an *early* refusal, before a temp
/// file exists and before a whole file is decoded, with the destination named.
/// It covers only the two paths a run definitely creates: `.partial` targets are
/// deliberately left to the commit, so a stale `<dest>.partial` lying around
/// does not refuse a run that was never going to write one.
fn preflight_output(output: &Path, split_errors: bool, opts: &WriteOptions) -> MieResult<()> {
    // L2-WRT-014, over every path this run could commit -- not just `output`.
    if let Some(input) = &opts.input_path {
        for target in commit_targets(output, split_errors, opts.allow_partial) {
            if paths_refer_to_same_file(input, &target).unwrap_or(false) {
                return Err(MieError::InputOutputCollision { path: target });
            }
        }
    }
    // L2-WRT-017, over the destinations that get created.
    if opts.no_clobber {
        if output.exists() {
            return Err(MieError::ClobberRefused {
                path: output.to_path_buf(),
            });
        }
        if split_errors {
            let error_path = error_path_for(output);
            if error_path.exists() {
                return Err(MieError::ClobberRefused { path: error_path });
            }
        }
    }
    Ok(())
}

/// Stream `messages` to a single CSV. Errors and spurious records are
/// included with their ERROR / `ERROR_CODE` columns populated (INLINE mode,
/// or stdout where splitting is not possible).
///
/// `output` may be `None` for stdout; stdout output skips the
/// pre-flight checks because it has no filesystem identity and ignores
/// `allow_partial` (a partial stdout stream is what the consumer
/// would have seen anyway).
/// # Errors
///
/// Returns [`MieError::InputOutputCollision`] if the destination resolves to
/// the input, [`MieError::ClobberRefused`] under `no_clobber` when it already
/// exists, [`MieError::WriterError`] for any write or rename failure, and
/// whatever the message stream itself raised — a strict-mode rejection or an
/// unrecoverable sync loss.
///
/// Under `allow_partial` an [`MieError::UnrecoverableSyncLoss`] from the stream
/// is **not** returned: the rows decoded so far are committed as
/// `<destination>.partial` and the call succeeds (L2-WRT-016).
pub fn write_csv<I>(
    messages: I,
    output: Option<&Path>,
    opts: WriteOptions,
) -> MieResult<WriteOutcome>
where
    I: IntoIterator<Item = MieResult<MieMessage>>,
{
    match output {
        Some(path) => {
            preflight_output(path, false, &opts)?;
            let mut atomic = AtomicCsvFile::create(path.to_path_buf())?.no_clobber(opts.no_clobber);

            let (count, partial_info) = {
                let mut writer = CsvWriter::new(&mut atomic, path.display().to_string())?
                    .with_time_render(opts.time_render);
                let mut partial_info: Option<PartialStop> = None;
                for item in messages {
                    match item {
                        Ok(msg) => writer.write_message(&msg)?,
                        Err(e) => match PartialStop::of(&e) {
                            Some(stop) if opts.allow_partial => {
                                partial_info = Some(stop);
                                break;
                            }
                            _ => return Err(e),
                        },
                    }
                }
                let n = writer.finish()?;
                (n, partial_info)
            };

            match partial_info {
                None => {
                    atomic.commit()?;
                    log_info!("wrote {} rows to {}", count, path.display());
                    Ok(WriteOutcome {
                        normal_count: count,
                        error_count: 0,
                        partial: None,
                    })
                }
                Some(stop) => {
                    let partial_path = atomic.commit_partial()?;
                    log_warn!(
                        "{}; wrote {} rows to {} (--allow-partial)",
                        stop.reason,
                        count,
                        partial_path.display()
                    );
                    let PartialStop {
                        offset,
                        sync_losses,
                        ..
                    } = stop;
                    Ok(WriteOutcome {
                        normal_count: count,
                        error_count: 0,
                        partial: Some(PartialCommit {
                            main_path: partial_path,
                            errors_path: None,
                            offset,
                            sync_losses,
                        }),
                    })
                }
            }
        }
        None => {
            let stdout = std::io::stdout();
            let buf = BufWriter::new(stdout.lock());
            let mut writer =
                CsvWriter::new(buf, "stdout".to_string())?.with_time_render(opts.time_render);
            stream_into(&mut writer, messages)?;
            let n = writer.finish()?;
            log_info!("wrote {} rows to stdout", n);
            Ok(WriteOutcome {
                normal_count: n,
                error_count: 0,
                partial: None,
            })
        }
    }
}

/// Split-output streaming: normal records to `output`, errored / spurious
/// to `<stem>_errors<ext>`. Only opens the error temp file lazily on the
/// first error row, so files with no errors don't produce an empty
/// `_errors.csv` (and don't even create a temp).
///
/// Both files use the `AtomicCsvFile` pattern — temp + atomic rename.
/// When `opts.allow_partial` and the iterator yields
/// `UnrecoverableSyncLoss`, both files (if any) are committed as
/// `.partial` and the function returns Ok with `PartialCommit` info.
/// # Errors
///
/// The same set as [`write_csv`], plus [`MieError::ClobberRefused`] for the
/// derived `<stem>_errors<suffix>` path, which gets its own check. The main CSV
/// is committed **before** the errors file (L2-WRT-019), so a failure writing
/// the latter leaves the former in place.
pub fn write_csv_split<I>(messages: I, output: &Path, opts: WriteOptions) -> MieResult<WriteOutcome>
where
    I: IntoIterator<Item = MieResult<MieMessage>>,
{
    preflight_output(output, true, &opts)?;

    // Both the errors destination and every `.partial` variant were pre-flighted
    // by `preflight_output` above -- collision against the input, and
    // no-clobber for the two paths a run creates. This used to reason that the
    // errors path needed no collision check because it "is derived from output,
    // which was already checked", which does not follow: a derived path is an
    // ordinary path that can name a *different* input, and `capture_errors.mie`
    // is a plausible recording name (L2-WRT-014).
    let error_path = error_path_for(output);

    let mut main_atomic = AtomicCsvFile::create(output.to_path_buf())?.no_clobber(opts.no_clobber);
    let mut errors_atomic: Option<AtomicCsvFile> = None;

    let (normal_count, error_count, partial_info) = {
        let mut main = CsvWriter::new(&mut main_atomic, output.display().to_string())?
            .with_time_render(opts.time_render);
        // The error writer is created lazily on first error row so a
        // clean file doesn't leave an empty errors CSV behind.
        let mut error_writer: Option<CsvWriter<&mut AtomicCsvFile>> = None;
        let mut partial_info: Option<PartialStop> = None;

        for item in messages {
            let msg = match item {
                Ok(m) => m,
                Err(e) => match PartialStop::of(&e) {
                    Some(stop) if opts.allow_partial => {
                        partial_info = Some(stop);
                        break;
                    }
                    _ => return Err(e),
                },
            };
            if msg.error_label().is_empty() {
                main.write_message(&msg)?;
            } else {
                if error_writer.is_none() {
                    errors_atomic = Some(
                        AtomicCsvFile::create(error_path.clone())?.no_clobber(opts.no_clobber),
                    );
                    let Some(inner) = errors_atomic.as_mut() else {
                        return Err(MieError::WriterError {
                            destination: error_path.display().to_string(),
                            source: io::Error::other("error output writer was not initialized"),
                        });
                    };
                    error_writer = Some(
                        CsvWriter::new(inner, error_path.display().to_string())?
                            .with_time_render(opts.time_render),
                    );
                }
                let Some(writer) = error_writer.as_mut() else {
                    return Err(MieError::WriterError {
                        destination: error_path.display().to_string(),
                        source: io::Error::other("error CSV writer was not initialized"),
                    });
                };
                writer.write_message(&msg)?;
            }
        }

        let n_errors = match error_writer.as_ref() {
            Some(w) => w.rows_written(),
            None => 0,
        };
        let n_main = main.rows_written();

        // Flush both CsvWriters before commit (drops them, which
        // releases the borrows on the AtomicCsvFiles).
        if let Some(w) = error_writer {
            w.finish()?;
        }
        main.finish()?;

        (n_main, n_errors, partial_info)
    };

    commit_split_outputs(
        SplitCommit {
            main_atomic,
            errors_atomic,
            output,
            error_path: &error_path,
            normal_count,
            error_count,
        },
        partial_info,
    )
}

/// The two atomic files plus their row counts, threaded into
/// [`commit_split_outputs`]. Grouped into a struct to keep the commit helper's
/// signature small (and to satisfy the arg-count lint).
struct SplitCommit<'a> {
    main_atomic: AtomicCsvFile,
    errors_atomic: Option<AtomicCsvFile>,
    output: &'a Path,
    error_path: &'a Path,
    normal_count: u64,
    error_count: u64,
}

/// What ended an `--allow-partial` write early, kept so the final WARN can say
/// what happened. A mid-file [`MieError::UnrecoverableSyncLoss`] reports its
/// offset and recovery count, exactly as before; a merge that left inputs out
/// ([`MieError::MergeInputsDropped`], L2-MRG-004) reports that instead of a sync
/// loss at offset 0 that never occurred. `offset` and `sync_losses` feed
/// [`PartialCommit`] unchanged (both 0 for left-out inputs, as they always were).
struct PartialStop {
    offset: u64,
    sync_losses: u64,
    reason: String,
}

impl PartialStop {
    /// The stop an `--allow-partial` run commits a `.partial` for, or `None`
    /// for an error that fails the run outright.
    fn of(e: &MieError) -> Option<Self> {
        match e {
            MieError::UnrecoverableSyncLoss {
                offset,
                sync_losses,
            } => Some(Self {
                offset: *offset,
                sync_losses: *sync_losses,
                reason: format!(
                    "unrecoverable sync loss at 0x{offset:X} after {sync_losses} recovery attempt(s)"
                ),
            }),
            MieError::MergeInputsDropped { .. } => Some(Self {
                offset: 0,
                sync_losses: 0,
                reason: e.to_string(),
            }),
            _ => None,
        }
    }
}

/// Commit the split outputs. `partial_info = None` is the normal path (atomic
/// rename over each destination, MAIN first per L2-WRT-019 so a failed errors
/// commit never leaves an orphan errors file); `Some(stop)` is the
/// `--allow-partial` path (rename each temp to its `.partial`).
fn commit_split_outputs(
    files: SplitCommit<'_>,
    partial_info: Option<PartialStop>,
) -> MieResult<WriteOutcome> {
    let SplitCommit {
        main_atomic,
        errors_atomic,
        output,
        error_path,
        normal_count,
        error_count,
    } = files;

    let Some(stop) = partial_info else {
        // Normal path. The two commits are sequential (no cross-file
        // atomicity): MAIN first so that if the errors commit fails, the file
        // left behind is the primary artifact, never an orphan errors file. If
        // the main commit fails, the errors temp is still un-renamed (unlinked
        // on Drop), so neither file appears. See ARCHITECTURE.md §8.
        main_atomic.commit()?;
        log_info!("wrote {} normal rows to {}", normal_count, output.display());
        if normal_count == 0 {
            log_warn!("main CSV is empty (header only)");
        }
        if let Some(ea) = errors_atomic {
            ea.commit()?;
            log_info!(
                "wrote {} error/spurious rows to {}",
                error_count,
                error_path.display()
            );
        } else {
            log_info!("no error/spurious records -- error file not created");
        }
        return Ok(WriteOutcome {
            normal_count,
            error_count,
            partial: None,
        });
    };

    // Partial path. Rename each temp to its `.partial` counterpart so the
    // operator can inspect what was decoded before the corruption.
    //
    // MAIN FIRST, for the same reason as the normal path above and under the
    // same rule (L2-WRT-019). This is the branch that used to run the other way
    // round in all three implementations: an errors `.partial` that committed
    // before a main `.partial` whose rename then failed left the operator an
    // orphan `<dest>_errors.csv.partial` next to no main output at all -- the
    // precise residue the main-first order exists to make impossible. That the
    // normal path got it right and the failure path did not is what a rule
    // stated over "the commit" rather than over "every commit" buys you.
    let main_partial_path = main_atomic.commit_partial()?;
    let errors_partial_path = match errors_atomic {
        Some(ea) => Some(ea.commit_partial()?),
        None => None,
    };
    log_warn!(
        "{}; wrote {} normal + {} error rows as partial to {} (--allow-partial)",
        stop.reason,
        normal_count,
        error_count,
        main_partial_path.display()
    );

    Ok(WriteOutcome {
        normal_count,
        error_count,
        partial: Some(PartialCommit {
            main_path: main_partial_path,
            errors_path: errors_partial_path,
            offset: stop.offset,
            sync_losses: stop.sync_losses,
        }),
    })
}

fn stream_into<W, I>(writer: &mut CsvWriter<W>, messages: I) -> MieResult<()>
where
    W: Write,
    I: IntoIterator<Item = MieResult<MieMessage>>,
{
    for item in messages {
        let msg = item?;
        writer.write_message(&msg)?;
    }
    Ok(())
}

/// `<destination>.partial` — where an `--allow-partial` run commits the rows
/// decoded before an unrecoverable sync loss (L2-WRT-016).
///
/// Kept beside `error_path_for` and used by **both** `AtomicCsvFile::commit_partial`
/// (which renames onto it) and `commit_targets` (which pre-flights it). A second
/// spelling of this derivation is how a guard comes to check a path the writer
/// does not actually write.
fn partial_path_for(destination: &Path) -> PathBuf {
    let mut name = destination
        .file_name()
        .map(std::ffi::OsStr::to_os_string)
        .unwrap_or_default();
    name.push(".partial");
    match destination.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.join(&name),
        _ => PathBuf::from(name),
    }
}

/// Every path a decode run could commit, given its destination and mode.
///
/// The L2-WRT-014 collision guard has to test **all** of them, not just the
/// destination the operator named. The derived paths are ordinary paths that
/// can name an ordinary file, and "it was derived from a path we already
/// checked" says nothing about whether it collides with a *different* input:
/// `-o capture.mie --separate-errors` derives `capture_errors.mie`, which is a
/// perfectly plausible name for one of the recordings being decoded. Both
/// destructive cases were live until this existed — the errors file and the
/// `.partial` file each committed straight over an input, and the run exited 0.
///
/// `.partial` targets are enumerated even though a clean decode never writes
/// one: the guard runs before the output is opened, which is the only point at
/// which refusing is still safe, and by then nobody knows whether the decode
/// will lose sync. Refusing a run that *might* have destroyed an input is the
/// conservative direction.
///
/// Ordering is main, errors, then their `.partial` variants, so the error names
/// the most direct collision when more than one target matches.
#[must_use]
pub fn commit_targets(output: &Path, split_errors: bool, allow_partial: bool) -> Vec<PathBuf> {
    let mut targets = vec![output.to_path_buf()];
    if split_errors {
        targets.push(error_path_for(output));
    }
    if allow_partial {
        // Indexing over a snapshot of the length: the partial of the errors
        // file is a real commit target in split mode (`write_csv_split`
        // commits both as `.partial`), and it is the one an audit forgets.
        let committed = targets.len();
        for i in 0..committed {
            targets.push(partial_path_for(&targets[i]));
        }
    }
    targets
}

fn error_path_for(output: &Path) -> std::path::PathBuf {
    let stem = output
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let ext = output.extension().map(|e| e.to_string_lossy().into_owned());
    let parent = output.parent().unwrap_or_else(|| Path::new(""));
    let name = match ext {
        Some(e) if !e.is_empty() => format!("{stem}_errors.{e}"),
        _ => format!("{stem}_errors"),
    };
    parent.join(name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;

    /// Requirements: L2-WRT-020
    #[test]
    fn mux_value_written_with_quoting() {
        use std::sync::Arc;
        let row = |mux: Option<Arc<str>>| {
            let msg = MieMessage {
                mux,
                ..sample_msg()
            };
            let mut buf = Vec::new();
            write_row(&mut buf, &msg, &msg.timestamp.format()).unwrap();
            String::from_utf8(buf).unwrap()
        };
        // Plain value appears verbatim in the MUX cell (after CMD 797E).
        assert!(row(Some(Arc::from("aa"))).contains(",797E,aa,,"));
        // A value containing the delimiter is RFC4180-quoted (matches Python).
        assert!(row(Some(Arc::from("a,b"))).contains(",797E,\"a,b\",,"));
        // None → empty MUX (and empty TERM_NAME).
        assert!(row(None).contains(",797E,,,"));
    }

    #[allow(
        clippy::unreadable_literal,
        reason = "the fixture value is asserted verbatim as CSV text below (`,A,0.123456,`); digit separators here would break that correspondence"
    )]
    fn sample_msg() -> MieMessage {
        MieMessage {
            timestamp: Timestamp::Irig(IrigTimestamp {
                day: 10,
                hour: 15,
                minute: 54,
                second: 50,
                microsecond: 456_225,
                freerun: false,
            }),
            type_word: TypeWord {
                message_type: 0x02,
                bus: Bus::A,
                word_count: 36,
                error: false,
                raw: 0x2402,
            },
            message_format: MessageFormat::Receive,
            command_word: Some(CommandWord {
                rt: 15,
                direction: Direction::Receive,
                subaddress: 11,
                data_word_count: 30,
                raw: 0x797E,
            }),
            command_word_2: None,
            status_word: Some(0x7800),
            status_word_2: None,
            data_words: DataWords::from_slice(&[0x0400, 0x0000, 0x0000, 0x002F]),
            error_word: None,
            delta: Some(0.123456),
            file_offset: 0,
            mux: None,
        }
    }

    /// A message that routes to the errors file (`error_label() == "ERROR"`).
    fn error_msg() -> MieMessage {
        let mut m = sample_msg();
        m.type_word.error = true;
        m.type_word.raw |= 1 << 14;
        m.error_word = None;
        m
    }

    /// Requirements: L2-WRT-001
    #[test]
    fn header_present() {
        let mut buf = Vec::new();
        let writer = CsvWriter::new(&mut buf, "memory").unwrap();
        writer.finish().unwrap();
        let s = String::from_utf8(buf).unwrap();
        assert!(s.starts_with("TIME_STAMP,RT,MSG,WD01"));
        assert!(s.trim_end().ends_with("XMT_GAP,ERROR,ERROR_CODE"));
    }

    /// The 44-column DDC vendor block comes first, in vendor order, and the
    /// decoder's own columns are appended after it — so column N of a decoded
    /// CSV is column N of a vendor CSV for every N in 1..=44 (L1-OUT-001).
    ///
    /// Requirements: L1-OUT-001, L2-WRT-001
    #[test]
    fn vendor_block_precedes_decoder_added_columns() {
        let cols: Vec<&str> = CSV_HEADER.trim_end().split(',').collect();
        assert_eq!(cols.len(), 46, "46 columns total");
        assert_eq!(cols.len() - VENDOR_COLUMN_COUNT, 2, "two decoder additions");

        // The vendor block ends at XMT_GAP...
        assert_eq!(cols[VENDOR_COLUMN_COUNT - 1], "XMT_GAP");
        // ...and the gap columns sit at their vendor indices (1-based 42/43/44),
        // which is exactly what the pre-v2.10.0 interleaved layout got wrong.
        assert_eq!(cols[41], "IM_GAP");
        assert_eq!(cols[42], "RCV_GAP");
        assert_eq!(cols[43], "XMT_GAP");
        // Decoder additions are strictly at the tail.
        assert_eq!(&cols[VENDOR_COLUMN_COUNT..], &["ERROR", "ERROR_CODE"]);
        // No decoder-added column may appear inside the vendor block.
        assert!(
            !cols[..VENDOR_COLUMN_COUNT].contains(&"ERROR"),
            "ERROR must not be inside the vendor block"
        );
        assert!(
            !cols[..VENDOR_COLUMN_COUNT].contains(&"ERROR_CODE"),
            "ERROR_CODE must not be inside the vendor block"
        );
    }

    /// Requirements: L2-WRT-001, L2-WRT-003
    #[test]
    fn row_format_matches_python_layout() {
        let mut buf = Vec::new();
        let mut w = CsvWriter::new(&mut buf, "memory").unwrap();
        w.write_message(&sample_msg()).unwrap();
        let n = w.finish().unwrap();
        assert_eq!(n, 1);

        let s = String::from_utf8(buf).unwrap();
        let mut lines = s.lines();
        let _header = lines.next().unwrap();
        let row = lines.next().unwrap();

        // Spot-check critical fields
        assert!(row.starts_with("10:15:54:50.456225,15,11R,"));
        assert!(row.contains("0400,0000,0000,002F,"));
        // Status, Cmd
        assert!(row.contains(",7800,797E,"));
        // Bus, Delta
        assert!(row.contains(",A,0.123456,"));
        // After DELTA the row is all-empty for a clean message: the three gap
        // columns, then the two appended decoder columns — five empty cells,
        // so five trailing commas.
        assert!(
            row.ends_with(",0.123456,,,,,"),
            "expected DELTA followed by 5 empty cells, got: {row}"
        );
    }

    /// Requirements: L2-WRT-002
    #[test]
    fn data_words_padded_to_32() {
        let mut buf = Vec::new();
        let mut w = CsvWriter::new(&mut buf, "memory").unwrap();
        w.write_message(&sample_msg()).unwrap();
        w.finish().unwrap();
        let s = String::from_utf8(buf).unwrap();
        let row = s.lines().nth(1).unwrap();
        // Count commas before STAT (should be: 3 fixed + 32 WD + 1 = 36 commas before STAT value)
        let commas: usize = row.matches(',').count();
        // Total fields in row = 3 + 32 + 11 = 46; commas = 45
        assert_eq!(commas, 45);
    }

    /// Requirements: L2-ERR-008
    #[test]
    fn error_path_naming() {
        assert_eq!(
            error_path_for(Path::new("out.csv")),
            Path::new("out_errors.csv")
        );
        assert_eq!(
            error_path_for(Path::new("data/x/out.csv")),
            Path::new("data/x/out_errors.csv")
        );
        assert_eq!(error_path_for(Path::new("out")), Path::new("out_errors"));
    }

    // ── AtomicCsvFile and path identity ──────────────────────────────
    //
    // Tests pinning L2-WRT-014 (input/output collision), L2-WRT-015
    // (atomic rename), L2-WRT-016 default-cleanup, and L2-WRT-017
    // (--no-clobber refusal). The .partial-rename branch of L2-WRT-016
    // depends on the --allow-partial CLI flag and is covered with
    // Phase 3.

    use std::sync::atomic::{AtomicU64, Ordering};

    fn unique_path(suffix: &str) -> std::path::PathBuf {
        static C: AtomicU64 = AtomicU64::new(0);
        let n = C.fetch_add(1, Ordering::Relaxed);
        let pid = std::process::id();
        std::env::temp_dir().join(format!("mie-atomic-test-{pid}-{n}{suffix}"))
    }

    /// Requirements: L3-WRT-001
    #[test]
    fn make_temp_path_lives_next_to_destination() {
        let dest = std::env::temp_dir().join("out.csv");
        let a = make_temp_path(&dest);
        let b = make_temp_path(&dest);
        assert_eq!(a.parent(), dest.parent());
        let name = a.file_name().unwrap().to_string_lossy().into_owned();
        assert!(name.starts_with("out.csv.aero1553.tmp."));
        assert!(name.contains(&std::process::id().to_string()));
        // Each call yields a distinct, unique temp path.
        assert_ne!(a, b);
    }

    /// Two writers targeting one destination must get distinct temp files
    /// (same-process concurrency safety) — exclusive create prevents a clash.
    /// Requirements: L2-WRT-015
    #[test]
    fn two_writers_same_destination_get_distinct_temps() {
        let dest = std::env::temp_dir().join("mie-concurrent-out.csv");
        let a = AtomicCsvFile::create(dest.clone()).unwrap();
        let b = AtomicCsvFile::create(dest.clone()).unwrap();
        assert_ne!(a.temp_path, b.temp_path);
        assert!(a.temp_path.exists() && b.temp_path.exists());
        // Uncommitted: Drop unlinks both temps, leaving the destination absent.
        drop(a);
        drop(b);
    }

    /// A persistent name clash exhausts the retries and surfaces an error rather
    /// than looping forever or overwriting.
    /// Requirements: L2-WRT-015
    #[test]
    fn create_exclusive_temp_fails_after_persistent_clash() {
        let clash = std::env::temp_dir().join("mie-persistent-clash.tmp");
        std::fs::write(&clash, b"x").unwrap(); // exists → create_new always clashes
        let dest = std::env::temp_dir().join("out.csv");
        let result = create_exclusive_temp(&dest, || clash.clone());
        let (_, err) = result.expect_err("a persistent clash must exhaust retries");
        assert_eq!(err.kind(), io::ErrorKind::AlreadyExists);
        let _ = std::fs::remove_file(&clash);
    }

    /// A non-clash create failure (e.g. a missing parent directory) is returned
    /// immediately, not retried.
    /// Requirements: L2-WRT-015
    #[test]
    fn create_exclusive_temp_surfaces_non_clash_errors() {
        let dest = std::env::temp_dir().join("mie-no-such-dir").join("out.csv");
        let temp = dest.clone();
        let result = create_exclusive_temp(&dest, || temp.clone());
        let (_, err) = result.expect_err("a missing parent dir must fail");
        assert_ne!(err.kind(), io::ErrorKind::AlreadyExists);
    }

    /// `create` wraps a temp-creation failure as `MieError::WriterError` rather
    /// than leaking the raw io error.
    /// Requirements: L2-WRT-015
    #[test]
    fn create_wraps_temp_creation_failure() {
        // Parent directory does not exist, so the temp file cannot be created.
        let dest = std::env::temp_dir()
            .join("mie-absent-parent-dir")
            .join("out.csv");
        assert!(AtomicCsvFile::create(dest).is_err());
    }

    /// Requirements: L2-WRT-015
    #[test]
    fn atomic_commit_renames_temp_over_destination() {
        let dest = unique_path(".csv");
        {
            let mut atomic = AtomicCsvFile::create(dest.clone()).unwrap();
            atomic.write_all(b"hello\n").unwrap();
            atomic.commit().unwrap();
        }
        let content = std::fs::read_to_string(&dest).unwrap();
        assert_eq!(content, "hello\n");
        // Temp file must be gone after commit.
        let tmp = make_temp_path(&dest);
        assert!(!tmp.exists(), "temp file still present after commit");
        let _ = std::fs::remove_file(&dest);
    }

    /// Requirements: L2-WRT-015, L2-WRT-016
    #[test]
    fn atomic_drop_without_commit_unlinks_temp_and_leaves_destination() {
        let dest = unique_path(".csv");
        // Pre-create destination so we can verify it isn't touched.
        std::fs::write(&dest, b"original\n").unwrap();
        let tmp = make_temp_path(&dest);
        {
            let mut atomic = AtomicCsvFile::create(dest.clone()).unwrap();
            atomic.write_all(b"discarded\n").unwrap();
            // Drop without commit — simulates a decode failure.
        }
        // Temp must be unlinked; destination must be unchanged.
        assert!(!tmp.exists(), "temp file should be cleaned up on Drop");
        let content = std::fs::read_to_string(&dest).unwrap();
        assert_eq!(content, "original\n");
        let _ = std::fs::remove_file(&dest);
    }

    // ── L2-WRT-023: the no-replace commit ─────────────────────────────
    //
    // These drive `AtomicCsvFile` directly rather than going through
    // `write_csv`, because the whole point is the window the pre-flight cannot
    // see: the destination is created AFTER the writer opened its temp, which is
    // exactly where a second process lands. Every one of them passes on the old
    // code if you only check the pre-flight, and fails on it here.

    /// A destination that appears after the writer opened its temp is refused,
    /// not overwritten. This is the race `exists()` cannot close.
    /// Requirements: L2-WRT-017, L2-WRT-023
    #[test]
    fn no_clobber_commit_refuses_a_destination_created_after_the_preflight() {
        let dest = unique_path(".csv");
        let atomic = {
            let mut a = AtomicCsvFile::create(dest.clone())
                .unwrap()
                .no_clobber(true);
            a.write_all(b"ours\n").unwrap();
            a
        };
        // The other process wins the race.
        std::fs::write(&dest, b"theirs\n").unwrap();

        let temp = atomic.temp_path.clone();
        match atomic.commit() {
            Err(MieError::ClobberRefused { path }) => assert_eq!(path, dest),
            other => panic!("expected ClobberRefused, got {other:?}"),
        }
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "theirs\n");
        assert!(!temp.exists(), "a refused commit must leave no temp behind");
        let _ = std::fs::remove_file(&dest);
    }

    /// The `.partial` target gets the same rule. It is never pre-flighted, so
    /// before L2-WRT-023 it was overwritten unconditionally under --no-clobber.
    /// Requirements: L2-WRT-016, L2-WRT-023, L3-WRT-005
    #[test]
    fn no_clobber_commit_partial_refuses_an_existing_partial() {
        let dest = unique_path(".csv");
        let partial = partial_path_for(&dest);
        std::fs::write(&partial, b"earlier forensics\n").unwrap();

        let mut atomic = AtomicCsvFile::create(dest.clone())
            .unwrap()
            .no_clobber(true);
        atomic.write_all(b"ours\n").unwrap();
        let temp = atomic.temp_path.clone();
        match atomic.commit_partial() {
            Err(MieError::ClobberRefused { path }) => assert_eq!(path, partial),
            other => panic!("expected ClobberRefused on the .partial, got {other:?}"),
        }
        assert_eq!(
            std::fs::read_to_string(&partial).unwrap(),
            "earlier forensics\n"
        );
        assert!(!temp.exists(), "a refused commit must leave no temp behind");
        let _ = std::fs::remove_file(&partial);
    }

    /// A no-replace commit onto a free name is an ordinary success, and leaves
    /// no temp behind -- the link mechanism has to unlink it explicitly.
    /// Requirements: L2-WRT-023
    #[test]
    fn no_clobber_commit_writes_normally_when_the_destination_is_free() {
        let dest = unique_path(".csv");
        let mut atomic = AtomicCsvFile::create(dest.clone())
            .unwrap()
            .no_clobber(true);
        atomic.write_all(b"rows\n").unwrap();
        let temp = atomic.temp_path.clone();
        atomic.commit().unwrap();

        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "rows\n");
        assert!(!temp.exists(), "the temp must not survive a commit");
        let _ = std::fs::remove_file(&dest);
    }

    /// The default is unchanged: without --no-clobber an existing destination is
    /// still replaced (L2-WRT-017's "overwrite succeeds by default").
    /// Requirements: L2-WRT-017
    #[test]
    fn default_commit_still_replaces_an_existing_destination() {
        let dest = unique_path(".csv");
        std::fs::write(&dest, b"stale\n").unwrap();
        let mut atomic = AtomicCsvFile::create(dest.clone()).unwrap();
        atomic.write_all(b"fresh\n").unwrap();
        atomic.commit().unwrap();
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "fresh\n");
        let _ = std::fs::remove_file(&dest);
    }

    /// The `.partial` half of the default, for the same reason.
    /// Requirements: L2-WRT-016
    #[test]
    fn default_commit_partial_still_replaces_an_existing_partial() {
        let dest = unique_path(".csv");
        let partial = partial_path_for(&dest);
        std::fs::write(&partial, b"stale\n").unwrap();
        let mut atomic = AtomicCsvFile::create(dest.clone()).unwrap();
        atomic.write_all(b"fresh\n").unwrap();
        assert_eq!(atomic.commit_partial().unwrap(), partial);
        assert_eq!(std::fs::read_to_string(&partial).unwrap(), "fresh\n");
        let _ = std::fs::remove_file(&partial);
    }

    /// The fallback path, exercised directly. On a filesystem with no hard links
    /// `commit_no_replace` reserves the name with `create_new` instead, and must
    /// still refuse a taken one -- so both arms of the fallback are pinned, not
    /// just the one this machine's filesystem happens to reach.
    /// Requirements: L2-WRT-023, L3-RS-017
    #[test]
    fn reserve_then_rename_commits_and_refuses_like_the_link_path() {
        let free = unique_path(".csv");
        let temp = unique_path(".tmp");
        std::fs::write(&temp, b"rows\n").unwrap();
        match reserve_then_rename(&temp, &free) {
            NoReplaceOutcome::Committed => {}
            _ => panic!("a free destination must commit"),
        }
        assert_eq!(std::fs::read_to_string(&free).unwrap(), "rows\n");

        let temp2 = unique_path(".tmp");
        std::fs::write(&temp2, b"other\n").unwrap();
        match reserve_then_rename(&temp2, &free) {
            NoReplaceOutcome::Exists => {}
            _ => panic!("a taken destination must be refused"),
        }
        // Refused, so the destination still holds the first writer's bytes and
        // no zero-byte reservation was left in its place.
        assert_eq!(std::fs::read_to_string(&free).unwrap(), "rows\n");
        let _ = std::fs::remove_file(&free);
        let _ = std::fs::remove_file(&temp2);
    }

    /// Requirements: L2-WRT-014
    #[test]
    fn paths_refer_to_same_file_existing() {
        let p = unique_path(".dat");
        std::fs::write(&p, b"x").unwrap();
        assert!(paths_refer_to_same_file(&p, &p).unwrap());
        let _ = std::fs::remove_file(&p);
    }

    /// Requirements: L2-WRT-014
    #[test]
    fn paths_refer_to_same_file_nonexistent_output_under_same_parent() {
        // Input exists; output names the same path but doesn't exist yet
        // (because we removed it). The check should still detect collision
        // via parent canonicalize + filename match.
        let p = unique_path(".dat");
        std::fs::write(&p, b"x").unwrap();
        let same_name_missing_file = p.clone();
        std::fs::remove_file(&same_name_missing_file).unwrap();
        // Re-create input so canonicalize works on input.
        std::fs::write(&p, b"x").unwrap();
        // Different output path that doesn't exist — must NOT be a collision.
        let different = unique_path(".csv");
        assert!(!paths_refer_to_same_file(&p, &different).unwrap());
        let _ = std::fs::remove_file(&p);
    }

    /// Requirements: L2-WRT-014
    #[test]
    fn write_csv_rejects_input_output_collision() {
        let p = unique_path(".csv");
        std::fs::write(&p, b"existing\n").unwrap();
        let opts = WriteOptions {
            input_path: Some(p.clone()),
            no_clobber: false,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        // Empty iterator — should never reach the write because
        // preflight_output fails first.
        let result = write_csv(std::iter::empty(), Some(&p), opts);
        match result {
            Err(MieError::InputOutputCollision { path }) => assert_eq!(path, p),
            other => panic!("expected InputOutputCollision, got {other:?}"),
        }
        // File must be unchanged.
        let content = std::fs::read_to_string(&p).unwrap();
        assert_eq!(content, "existing\n");
        let _ = std::fs::remove_file(&p);
    }

    /// Requirements: L2-WRT-017
    #[test]
    fn write_csv_rejects_clobber_when_no_clobber_set() {
        let p = unique_path(".csv");
        std::fs::write(&p, b"existing\n").unwrap();
        let opts = WriteOptions {
            input_path: None,
            no_clobber: true,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        let result = write_csv(std::iter::empty(), Some(&p), opts);
        match result {
            Err(MieError::ClobberRefused { path }) => assert_eq!(path, p),
            other => panic!("expected ClobberRefused, got {other:?}"),
        }
        // File must be unchanged.
        let content = std::fs::read_to_string(&p).unwrap();
        assert_eq!(content, "existing\n");
        let _ = std::fs::remove_file(&p);
    }

    /// Requirements: L2-WRT-017
    #[test]
    fn write_csv_overwrites_by_default() {
        let p = unique_path(".csv");
        std::fs::write(&p, b"existing\n").unwrap();
        let result = write_csv(std::iter::empty(), Some(&p), WriteOptions::default());
        result.unwrap();
        // File should now contain just the CSV header.
        let content = std::fs::read_to_string(&p).unwrap();
        assert!(content.starts_with("TIME_STAMP,RT,MSG,"));
        let _ = std::fs::remove_file(&p);
    }

    /// Requirements: L2-WRT-014
    ///
    /// Every path a run could commit is a collision candidate -- not just the
    /// destination the operator named. The derived errors path is an ordinary
    /// path that can name a *different* input: `-o capture.mie
    /// --separate-errors` derives `capture_errors.mie`, a plausible recording
    /// name. Before this, the errors file committed straight over that input
    /// and the run exited 0.
    #[test]
    fn write_csv_split_rejects_collision_on_the_derived_errors_path() {
        let dest = unique_path(".mie");
        let victim = error_path_for(&dest);
        std::fs::write(&victim, b"the recording being decoded").unwrap();

        let opts = WriteOptions {
            input_path: Some(victim.clone()),
            no_clobber: false,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        let result = write_csv_split(std::iter::empty(), &dest, opts);
        match result {
            Err(MieError::InputOutputCollision { path }) => assert_eq!(path, victim),
            other => panic!("expected InputOutputCollision on the errors path, got {other:?}"),
        }

        // The whole point: the input survives, and nothing was created.
        assert_eq!(
            std::fs::read(&victim).unwrap(),
            b"the recording being decoded",
            "input file was modified despite the collision rejection"
        );
        assert!(!dest.exists(), "main destination must not be created");
        let _ = std::fs::remove_file(&victim);
    }

    /// Requirements: L2-WRT-014, L2-WRT-016
    ///
    /// `<destination>.partial` is a commit target under `--allow-partial`, so
    /// an input named `out.csv.partial` collides with `-o out.csv`. It is
    /// enumerated even though a clean decode never writes one: the guard runs
    /// before the output is opened, which is the only point at which refusing
    /// is still safe, and by then nobody knows whether the decode will lose
    /// sync.
    #[test]
    fn write_csv_rejects_collision_on_the_partial_path() {
        let dest = unique_path(".csv");
        let victim = partial_path_for(&dest);
        std::fs::write(&victim, b"the recording being decoded").unwrap();

        let collide = |allow_partial| {
            write_csv(
                std::iter::empty(),
                Some(&dest),
                WriteOptions {
                    input_path: Some(victim.clone()),
                    no_clobber: false,
                    allow_partial,
                    time_render: TimeRender::doy(),
                },
            )
        };

        // Without --allow-partial there is no `.partial` target, so the same
        // pair of paths is a perfectly ordinary decode.
        collide(false).expect("no .partial target without allow_partial");
        assert!(dest.exists());
        std::fs::remove_file(&dest).unwrap();

        match collide(true) {
            Err(MieError::InputOutputCollision { path }) => assert_eq!(path, victim),
            other => panic!("expected InputOutputCollision on the .partial path, got {other:?}"),
        }
        assert_eq!(
            std::fs::read(&victim).unwrap(),
            b"the recording being decoded",
            "input file was modified despite the collision rejection"
        );
        assert!(!dest.exists(), "main destination must not be created");
        let _ = std::fs::remove_file(&victim);
    }

    /// Requirements: L2-WRT-014, L2-WRT-016, L2-WRT-019
    ///
    /// The FOURTH commit target: the errors file's own `.partial`, written when
    /// split mode meets an `--allow-partial` sync loss. It had only the
    /// enumeration test below covering it -- that it appears in a list -- and
    /// nothing exercising it end to end. "The other three are checked and this
    /// one is in the same list" is a composition argument, and a composition
    /// argument ("derived from output, which was already checked") is what put
    /// the hole here in the first place.
    ///
    /// Deliberately isolated: with `-o capture.mie` none of the other three
    /// targets matches this input, so only the target under test can make it
    /// pass.
    #[test]
    fn write_csv_split_rejects_collision_on_the_errors_partial_path() {
        let dest = unique_path(".mie");
        let victim = partial_path_for(&error_path_for(&dest));
        std::fs::write(&victim, b"the recording being decoded").unwrap();

        let opts = |allow_partial| WriteOptions {
            input_path: Some(victim.clone()),
            no_clobber: false,
            allow_partial,
            time_render: TimeRender::doy(),
        };

        // Confirm the isolation rather than trusting it: the other three
        // targets must not name this file, or the test proves nothing.
        for target in commit_targets(&dest, true, true) {
            if target != victim {
                assert_ne!(target, victim);
            }
        }

        // Without allow_partial the errors `.partial` is not a target at all.
        write_csv_split(std::iter::empty(), &dest, opts(false))
            .expect("no errors-partial target without allow_partial");
        assert!(dest.exists());
        std::fs::remove_file(&dest).unwrap();

        match write_csv_split(std::iter::empty(), &dest, opts(true)) {
            Err(MieError::InputOutputCollision { path }) => assert_eq!(path, victim),
            other => panic!("expected InputOutputCollision on the errors .partial, got {other:?}"),
        }
        assert_eq!(
            std::fs::read(&victim).unwrap(),
            b"the recording being decoded",
            "input file was modified despite the collision rejection"
        );
        assert!(!dest.exists(), "main destination must not be created");
        let _ = std::fs::remove_file(&victim);
    }

    /// Requirements: L2-WRT-014
    ///
    /// The enumeration itself, pinned: main, errors, then their `.partial`
    /// variants. The errors file's own `.partial` is the one an audit forgets,
    /// and split mode commits it.
    #[test]
    fn commit_targets_enumerates_every_committable_path() {
        let out = Path::new("dir").join("capture.csv");
        let name = |p: &Path| p.file_name().unwrap().to_string_lossy().into_owned();

        assert_eq!(
            commit_targets(&out, false, false)
                .iter()
                .map(|p| name(p))
                .collect::<Vec<_>>(),
            vec!["capture.csv"]
        );
        assert_eq!(
            commit_targets(&out, true, false)
                .iter()
                .map(|p| name(p))
                .collect::<Vec<_>>(),
            vec!["capture.csv", "capture_errors.csv"]
        );
        assert_eq!(
            commit_targets(&out, false, true)
                .iter()
                .map(|p| name(p))
                .collect::<Vec<_>>(),
            vec!["capture.csv", "capture.csv.partial"]
        );
        assert_eq!(
            commit_targets(&out, true, true)
                .iter()
                .map(|p| name(p))
                .collect::<Vec<_>>(),
            vec![
                "capture.csv",
                "capture_errors.csv",
                "capture.csv.partial",
                "capture_errors.csv.partial",
            ]
        );
        // Every target stays beside the destination, so each rename is
        // same-filesystem and stays atomic (L2-WRT-015).
        for target in commit_targets(&out, true, true) {
            assert_eq!(target.parent(), out.parent());
        }
    }

    /// Requirements: L2-WRT-014
    #[test]
    fn write_csv_split_rejects_input_output_collision() {
        let p = unique_path(".csv");
        std::fs::write(&p, b"existing\n").unwrap();
        let opts = WriteOptions {
            input_path: Some(p.clone()),
            no_clobber: false,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        let result = write_csv_split(std::iter::empty(), &p, opts);
        match result {
            Err(MieError::InputOutputCollision { path }) => assert_eq!(path, p),
            other => panic!("expected InputOutputCollision, got {other:?}"),
        }
        let _ = std::fs::remove_file(&p);
    }

    /// Requirements: L2-WRT-017
    #[test]
    fn write_csv_split_no_clobber_checks_errors_file_too() {
        // No-clobber should reject if the *errors* file exists, even
        // when the main destination is fresh.
        let dest = unique_path(".csv");
        let err_dest = error_path_for(&dest);
        std::fs::write(&err_dest, b"old errors\n").unwrap();
        let opts = WriteOptions {
            input_path: None,
            no_clobber: true,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        let result = write_csv_split(std::iter::empty(), &dest, opts);
        match result {
            Err(MieError::ClobberRefused { path }) => assert_eq!(path, err_dest),
            other => panic!("expected ClobberRefused on errors path, got {other:?}"),
        }
        // Main dest must not have been created.
        assert!(!dest.exists());
        let _ = std::fs::remove_file(&err_dest);
    }

    /// Requirements: L2-WRT-019, L2-WRT-015
    ///
    /// Separate mode commits main before errors. We force the *second*
    /// (errors) commit to fail by making the errors destination a
    /// directory — renaming a file over a directory fails on both POSIX
    /// (EISDIR) and Windows (`MoveFileEx`). The already-committed main CSV
    /// must remain (the primary artifact is the residue), and the errors
    /// temp must be unlinked — never an orphan errors file.
    #[test]
    fn split_errors_commit_failure_leaves_main_not_orphan_errors() {
        let dest = unique_path(".csv");
        let err_dest = error_path_for(&dest);
        std::fs::create_dir(&err_dest).unwrap();

        let messages: Vec<MieResult<MieMessage>> = vec![Ok(sample_msg()), Ok(error_msg())];
        let result = write_csv_split(messages, &dest, WriteOptions::default());

        // The errors commit fails, so the overall call surfaces the error.
        assert!(
            result.is_err(),
            "errors-commit failure should surface as Err"
        );
        // ...but main was committed first, so the primary file is complete.
        let main = std::fs::read_to_string(&dest).unwrap();
        assert!(main.starts_with("TIME_STAMP,RT,MSG,"));
        assert!(main.contains("10:15:54:50.456225,15,11R,"));
        // No orphan errors *file*: the destination is still the directory.
        assert!(
            err_dest.is_dir(),
            "errors destination should be untouched (still a dir)"
        );
        // The errors temp must have been unlinked on Drop.
        assert!(
            !make_temp_path(&err_dest).exists(),
            "errors temp leaked after failed commit"
        );

        let _ = std::fs::remove_file(&dest);
        let _ = std::fs::remove_dir(&err_dest);
    }

    /// Requirements: L2-WRT-019, L2-WRT-015
    ///
    /// When the *first* (main) commit fails, neither output file appears:
    /// the errors commit is never reached, and both temps are unlinked on
    /// Drop. We force the main commit to fail by making the main
    /// destination a directory.
    #[test]
    fn split_main_commit_failure_leaves_neither_file() {
        let dest = unique_path(".csv");
        let err_dest = error_path_for(&dest);
        std::fs::create_dir(&dest).unwrap();

        let messages: Vec<MieResult<MieMessage>> = vec![Ok(sample_msg()), Ok(error_msg())];
        let result = write_csv_split(messages, &dest, WriteOptions::default());

        assert!(result.is_err(), "main-commit failure should surface as Err");
        // No errors file is produced — main is committed first and failed.
        assert!(
            !err_dest.exists(),
            "errors file must not appear when the main commit fails first"
        );
        // Both temps cleaned up on Drop.
        assert!(!make_temp_path(&dest).exists(), "main temp leaked");
        assert!(!make_temp_path(&err_dest).exists(), "errors temp leaked");
        // The main destination is still just the directory we created.
        assert!(dest.is_dir());

        let _ = std::fs::remove_dir(&dest);
    }

    /// Requirements: L2-WRT-016, L2-WRT-019
    ///
    /// The `--allow-partial` half of `split_main_commit_failure_leaves_neither_file`,
    /// and the direct regression for the bug that branch carried. Force the MAIN
    /// `.partial` rename to fail (a directory sits on it) and assert that no
    /// errors `.partial` appears. Before the fix this branch committed errors
    /// first, so the errors `.partial` was already on disk by the time the main
    /// one failed -- an orphan forensic artifact with no main output beside it.
    #[test]
    fn split_partial_main_commit_failure_leaves_no_orphan_errors_partial() {
        let dest = unique_path(".csv");
        let err_dest = error_path_for(&dest);
        let main_partial = partial_path_for(&dest);
        let errors_partial = partial_path_for(&err_dest);
        std::fs::create_dir(&main_partial).unwrap();

        let messages: Vec<MieResult<MieMessage>> = vec![
            Ok(sample_msg()),
            Ok(error_msg()),
            Err(MieError::UnrecoverableSyncLoss {
                offset: 0x99,
                sync_losses: 2,
            }),
        ];
        let opts = WriteOptions {
            input_path: None,
            no_clobber: false,
            allow_partial: true,
            time_render: TimeRender::doy(),
        };
        let result = write_csv_split(messages, &dest, opts);

        assert!(
            result.is_err(),
            "a failed main .partial commit should surface as Err"
        );
        assert!(
            !errors_partial.exists(),
            "errors .partial must not appear when the main .partial commit fails first"
        );
        assert!(!dest.exists(), "the destination itself is never written");
        assert!(!err_dest.exists());
        assert!(!make_temp_path(&dest).exists(), "main temp leaked");
        assert!(!make_temp_path(&err_dest).exists(), "errors temp leaked");

        let _ = std::fs::remove_dir(&main_partial);
    }

    /// Requirements: L2-WRT-016, L2-WRT-019
    ///
    /// The mirror image: the MAIN `.partial` commits, then the errors one
    /// fails. The residue is the primary artifact, which is the whole point of
    /// the order.
    #[test]
    fn split_partial_errors_commit_failure_leaves_the_main_partial() {
        let dest = unique_path(".csv");
        let err_dest = error_path_for(&dest);
        let main_partial = partial_path_for(&dest);
        let errors_partial = partial_path_for(&err_dest);
        std::fs::create_dir(&errors_partial).unwrap();

        let messages: Vec<MieResult<MieMessage>> = vec![
            Ok(sample_msg()),
            Ok(error_msg()),
            Err(MieError::UnrecoverableSyncLoss {
                offset: 0x99,
                sync_losses: 2,
            }),
        ];
        let opts = WriteOptions {
            input_path: None,
            no_clobber: false,
            allow_partial: true,
            time_render: TimeRender::doy(),
        };
        let result = write_csv_split(messages, &dest, opts);

        assert!(
            result.is_err(),
            "a failed errors .partial commit should surface as Err"
        );
        let body = std::fs::read_to_string(&main_partial)
            .expect("the main .partial must survive an errors-commit failure");
        assert!(body.starts_with("TIME_STAMP,RT,MSG,"));
        assert!(
            errors_partial.is_dir(),
            "the errors .partial target should be untouched (still a dir)"
        );

        let _ = std::fs::remove_file(&main_partial);
        let _ = std::fs::remove_dir(&errors_partial);
    }

    /// The wiring, end to end: `WriteOptions::no_clobber` has to reach the
    /// commit, not just the pre-flight. The destination is created after
    /// `write_csv` is already streaming -- which the pre-flight cannot see --
    /// by handing it a message iterator that writes the file as it is consumed.
    /// Requirements: L2-WRT-017, L2-WRT-023
    #[test]
    fn write_csv_no_clobber_refuses_a_destination_that_appears_mid_decode() {
        let dest = unique_path(".csv");
        let racer = dest.clone();
        // The "other process": it runs between the pre-flight (already done) and
        // the commit (not yet reached), because it runs while the stream drains.
        let messages = std::iter::once(Ok(sample_msg())).inspect(move |_| {
            std::fs::write(&racer, b"theirs\n").unwrap();
        });
        let opts = WriteOptions {
            input_path: None,
            no_clobber: true,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        match write_csv(messages, Some(&dest), opts) {
            Err(MieError::ClobberRefused { path }) => assert_eq!(path, dest),
            other => panic!("expected ClobberRefused, got {other:?}"),
        }
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "theirs\n");
        assert!(!make_temp_path(&dest).exists(), "temp leaked after refusal");
        let _ = std::fs::remove_file(&dest);
    }

    /// Requirements: L3-WRT-002
    #[test]
    fn atomic_commit_partial_writes_dot_partial_and_leaves_destination() {
        let dest = unique_path(".csv");
        // Pre-create destination so we can verify it stays untouched.
        std::fs::write(&dest, b"original\n").unwrap();
        let partial_path = {
            let mut atomic = AtomicCsvFile::create(dest.clone()).unwrap();
            atomic.write_all(b"partial decode\n").unwrap();
            atomic.commit_partial().unwrap()
        };
        // The committed-partial path must be <dest>.partial.
        let expected_partial = {
            let mut name = dest.file_name().unwrap().to_os_string();
            name.push(".partial");
            dest.parent().unwrap().join(name)
        };
        assert_eq!(partial_path, expected_partial);
        assert_eq!(
            std::fs::read_to_string(&partial_path).unwrap(),
            "partial decode\n"
        );
        // Original destination must be unchanged.
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "original\n");
        // Temp must be gone.
        let tmp = make_temp_path(&dest);
        assert!(
            !tmp.exists(),
            "temp file should be gone after commit_partial"
        );
        let _ = std::fs::remove_file(&dest);
        let _ = std::fs::remove_file(&partial_path);
    }

    /// Requirements: L2-WRT-016, L1-EXIT-004
    #[test]
    fn write_csv_with_allow_partial_commits_on_unrecoverable() {
        let dest = unique_path(".csv");
        // Synthetic iterator: one good message, then UnrecoverableSyncLoss.
        let messages: Vec<MieResult<MieMessage>> = vec![
            Ok(sample_msg()),
            Err(MieError::UnrecoverableSyncLoss {
                offset: 0x1234,
                sync_losses: 1,
            }),
        ];
        let opts = WriteOptions {
            input_path: None,
            no_clobber: false,
            allow_partial: true,
            time_render: TimeRender::doy(),
        };
        let outcome = write_csv(messages, Some(&dest), opts).unwrap();
        let partial = outcome.partial.expect("partial commit info");
        assert_eq!(partial.offset, 0x1234);
        assert_eq!(partial.sync_losses, 1);
        // Main destination must NOT exist; only the .partial does.
        assert!(!dest.exists(), "destination should not exist on partial");
        assert!(partial.main_path.exists(), "partial file must exist");
        let body = std::fs::read_to_string(&partial.main_path).unwrap();
        assert!(body.starts_with("TIME_STAMP,RT,MSG,"));
        assert!(body.contains("11R")); // sample_msg is SA 11 R
        let _ = std::fs::remove_file(&partial.main_path);
    }

    /// Requirements: L2-WRT-016, L1-EXIT-004
    #[test]
    fn write_csv_without_allow_partial_propagates_unrecoverable() {
        let dest = unique_path(".csv");
        let messages: Vec<MieResult<MieMessage>> = vec![
            Ok(sample_msg()),
            Err(MieError::UnrecoverableSyncLoss {
                offset: 0x42,
                sync_losses: 1,
            }),
        ];
        let opts = WriteOptions {
            input_path: None,
            no_clobber: false,
            allow_partial: false,
            time_render: TimeRender::doy(),
        };
        let err = write_csv(messages, Some(&dest), opts).unwrap_err();
        match err {
            MieError::UnrecoverableSyncLoss {
                offset,
                sync_losses,
            } => {
                assert_eq!(offset, 0x42);
                assert_eq!(sync_losses, 1);
            }
            other => panic!("expected UnrecoverableSyncLoss, got {other:?}"),
        }
        // Both destination and .partial must be absent — Drop unlinked
        // the temp because allow_partial was false.
        assert!(!dest.exists());
        let mut partial_name = dest.file_name().unwrap().to_os_string();
        partial_name.push(".partial");
        let partial = dest.parent().unwrap().join(partial_name);
        assert!(!partial.exists());
    }

    /// Every stage of a commit -- the flush, the close, the move -- reports as
    /// `WriterError` and leaves the destination untouched with no temp behind
    /// (L2-WRT-024). The failure is provoked at the move, which is the stage a
    /// test can actually reach: renaming a file onto a directory fails on POSIX
    /// (EISDIR) and on Windows alike. The flush and close stages are covered by
    /// construction here rather than by injection -- `BufWriter::into_inner`
    /// hands their error back as a value this function already maps, so there is
    /// no path by which one could escape unclassified. Python had no such
    /// guarantee, which is why it needed its own injected-stream test.
    /// Requirements: L2-WRT-024
    #[test]
    fn a_failed_commit_reports_a_writer_error_and_leaves_nothing_behind() {
        let dest = unique_path(".csv");
        std::fs::create_dir(&dest).unwrap();

        let mut atomic = AtomicCsvFile::create(dest.clone()).unwrap();
        let temp = atomic.temp_path.clone();
        atomic.write_all(b"rows\n").unwrap();
        match atomic.commit() {
            Err(MieError::WriterError { destination, .. }) => {
                assert_eq!(destination, dest.display().to_string());
            }
            other => panic!("expected WriterError, got {other:?}"),
        }
        assert!(!temp.exists(), "the temp must not survive a failed commit");
        assert!(dest.is_dir(), "the destination is untouched");
        let _ = std::fs::remove_dir(&dest);
    }

    /// Requirements: L2-WRT-018
    #[test]
    fn is_broken_pipe_predicate() {
        let e = MieError::WriterError {
            destination: "stdout".to_string(),
            source: io::Error::new(io::ErrorKind::BrokenPipe, "pipe closed"),
        };
        assert!(e.is_broken_pipe());

        let other = MieError::WriterError {
            destination: "stdout".to_string(),
            source: io::Error::other("nope"),
        };
        assert!(!other.is_broken_pipe());

        let non_writer = MieError::FileEmpty {
            path: std::path::PathBuf::from("/x"),
        };
        assert!(!non_writer.is_broken_pipe());
    }
}
