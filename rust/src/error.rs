//! Error types for the Aero1553 library.
//!
//! All fallible APIs return `Result<T, MieError>`. The single enum replaces
//! the Python class hierarchy; the `kind()` method returns a `MieErrorKind`
//! discriminant for callers that need to branch on the failure mode.

use std::fmt;
use std::io;
use std::path::PathBuf;

/// Single error type returned by all decoder operations.
#[derive(Debug)]
pub enum MieError {
    /// Specified MIE binary file does not exist.
    FileNotFound { path: PathBuf },

    /// Specified MIE binary file exists but is zero bytes.
    FileEmpty { path: PathBuf },

    /// File-level I/O failure (mmap, open, read).
    FileIo { path: PathBuf, source: io::Error },

    /// Type Word produced an invalid or zero word count.
    InvalidTypeWord {
        offset: u64,
        raw_type_word: u16,
        word_count: u16,
    },

    /// Type Word's message type code is not in the known set.
    UnknownTypeWord {
        offset: u64,
        raw_type_word: u16,
        message_type: u8,
    },

    /// Record extends beyond the end of the file.
    RecordTruncated {
        offset: u64,
        record_bytes: u64,
        available_bytes: u64,
    },

    /// The first valid Type Word found after header detection has a
    /// declared extent that runs past EOF. Per L2-RDR-004 this is a
    /// distinct error class from [`Self::RecordTruncated`]: strict mode
    /// surfaces it; lenient mode emits zero records and yields
    /// [`Self::NoValidRecords`] instead (exit 2), with a WARN naming the
    /// truncation. It is never a clean end of stream: a file that decodes
    /// to no rows is not a successful decode.
    ///
    /// Operationally this signals that the recording was aborted before
    /// the first complete record was written — distinct from a
    /// mid-stream truncation after at least one valid record.
    FirstRecordTruncated {
        offset: u64,
        record_bytes: u64,
        available_bytes: u64,
    },

    /// Record's payload is inconsistent with Type Word / Command Word.
    PayloadError { offset: u64, detail: String },

    /// Errored record contains an unrecognized error code.
    UnknownErrorCode { offset: u64, error_code: u16 },

    /// File exists and is non-empty but contains no decodable MIE records
    /// within the initial 64 KB scan window. Typically means the file
    /// isn't an MIE recording at all (e.g., a TOML file mistakenly passed
    /// as input).
    NoValidRecords { path: PathBuf, scan_bytes: u64 },

    /// L2-SYN-018: the input file appears to be a pathological single-
    /// byte pad rather than an MIE recording. After header detection
    /// finds a candidate record, the reader compares the first N
    /// consecutive candidate-sized chunks; if they are byte-identical
    /// in every position except the timestamp triple, the file is
    /// rejected. Defends against 0x20-fill (where `0x20 0x20` parses
    /// as a valid `SPURIOUS_DATA` Type Word and look-ahead alone admits
    /// the stream) and similar single-byte pads.
    HomogeneousPayload {
        path: PathBuf,
        offset: u64,
        sample_records: u32,
    },

    /// Output writer failed (CSV row, flush, etc).
    WriterError {
        destination: String,
        source: io::Error,
    },

    /// Output path resolves to the same file as the input. Per L2-WRT-014,
    /// decoding in-place is unsafe (the input is mmap-backed) and is
    /// rejected before any output file is opened.
    InputOutputCollision { path: PathBuf },

    /// `--no-clobber` was set (or `output.no_clobber = true` in config)
    /// and the output destination already exists. Per L2-WRT-017 the
    /// implementation refuses to overwrite rather than silently replacing.
    ClobberRefused { path: PathBuf },

    /// Mid-file sync loss in lenient mode that `recover_sync` could not
    /// reacquire within the scan window. Per L1-EXIT-004 this maps to CLI
    /// exit code `3` by default, or to a `.partial` commit + exit `0`
    /// when `--allow-partial` is set. `sync_losses` is the cumulative
    /// recovery-attempt count for the decode invocation.
    UnrecoverableSyncLoss { offset: u64, sync_losses: u64 },

    /// L2-MRG-004: a merge under `--allow-partial` left out `left_out` of its
    /// `total` inputs because they could not be opened or did not begin with a
    /// readable record, and truncated a further `truncated` at a mid-file
    /// unrecoverable sync loss. It ends the stream exactly as
    /// [`Self::UnrecoverableSyncLoss`] does -- the writer commits a `.partial`
    /// -- but names what happened rather than reporting a sync loss at offset 0
    /// that never occurred. Raised only when `left_out > 0`; a merge whose only
    /// failure is a mid-file sync loss reports that error itself.
    MergeInputsDropped {
        left_out: u64,
        truncated: u64,
        total: u64,
    },

    /// L2-DEC-016: the L2-DEC-015 multi-record probe completed with a
    /// confidence below the configured floor — either the winning
    /// aggregate score is too low or the margin between the two
    /// candidate format scores is too narrow to make a confident call.
    /// Raised only in strict mode; lenient mode logs a WARN and uses
    /// the chosen format anyway (back-compat for borderline files
    /// that decoded acceptably before L2-DEC-015 / L2-DEC-016 landed).
    /// Maps to exit class `2` (the "wrong file type" class shared with
    /// `NoValidRecords` and `HomogeneousPayload`).
    TimestampFormatMismatch {
        offset: u64,
        irig_score: i32,
        std_score: i32,
        records_probed: u32,
    },

    /// L2-WRT-026: a calendar rendering (`iso` / `dom`) was requested but the
    /// recording cannot supply a calendar date -- it is Standard-format or
    /// freerun IRIG (clause 2), or a record's day-of-year does not exist in the
    /// configured year (clause 3). Raised before any row is written in the
    /// first case and at the offending record in the second; `detail` names the
    /// specific reason. Maps to exit class `2`, the "the file cannot satisfy
    /// this request" class it shares with `TimestampFormatMismatch`.
    CalendarUnavailable { detail: String },

    /// L1-EXIT-009: a multi-file merge cannot order its inputs on a common
    /// absolute timeline — one input is Standard-format, leads with a
    /// freerun IRIG record, or the set mixes timestamp formats. Raised
    /// before any output is written; maps to CLI exit code `6`. `detail`
    /// names the specific reason. See L2-MRG-003.
    IncompatibleMergeInputs {
        file_index: usize,
        path: PathBuf,
        detail: String,
    },

    /// L2-MRG-006: during a multi-file merge, one input's records are not in
    /// chronological capture order — a record's absolute IRIG microsecond key
    /// stepped backward relative to the previous record pulled from the *same*
    /// file. The time-merge assumes each input is internally time-sorted; a
    /// backward step (sync-loss recovery or a day/year rollover) means the
    /// merged output may be out of order for that input. Strict mode surfaces
    /// this (record-error class, exit `1`); lenient mode only WARNs and keeps
    /// going.
    NonMonotonicInput {
        file_index: usize,
        path: PathBuf,
        prev_us: u64,
        curr_us: u64,
    },
}

/// Discriminant identifying which variant of [`MieError`] occurred.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MieErrorKind {
    FileNotFound,
    FileEmpty,
    FileIo,
    InvalidTypeWord,
    UnknownTypeWord,
    RecordTruncated,
    FirstRecordTruncated,
    PayloadError,
    UnknownErrorCode,
    NoValidRecords,
    HomogeneousPayload,
    WriterError,
    InputOutputCollision,
    ClobberRefused,
    UnrecoverableSyncLoss,
    MergeInputsDropped,
    TimestampFormatMismatch,
    CalendarUnavailable,
    IncompatibleMergeInputs,
    NonMonotonicInput,
}

impl MieError {
    #[must_use]
    pub fn kind(&self) -> MieErrorKind {
        match self {
            Self::FileNotFound { .. } => MieErrorKind::FileNotFound,
            Self::FileEmpty { .. } => MieErrorKind::FileEmpty,
            Self::FileIo { .. } => MieErrorKind::FileIo,
            Self::InvalidTypeWord { .. } => MieErrorKind::InvalidTypeWord,
            Self::UnknownTypeWord { .. } => MieErrorKind::UnknownTypeWord,
            Self::RecordTruncated { .. } => MieErrorKind::RecordTruncated,
            Self::FirstRecordTruncated { .. } => MieErrorKind::FirstRecordTruncated,
            Self::PayloadError { .. } => MieErrorKind::PayloadError,
            Self::UnknownErrorCode { .. } => MieErrorKind::UnknownErrorCode,
            Self::NoValidRecords { .. } => MieErrorKind::NoValidRecords,
            Self::HomogeneousPayload { .. } => MieErrorKind::HomogeneousPayload,
            Self::WriterError { .. } => MieErrorKind::WriterError,
            Self::InputOutputCollision { .. } => MieErrorKind::InputOutputCollision,
            Self::ClobberRefused { .. } => MieErrorKind::ClobberRefused,
            Self::UnrecoverableSyncLoss { .. } => MieErrorKind::UnrecoverableSyncLoss,
            Self::MergeInputsDropped { .. } => MieErrorKind::MergeInputsDropped,
            Self::TimestampFormatMismatch { .. } => MieErrorKind::TimestampFormatMismatch,
            Self::CalendarUnavailable { .. } => MieErrorKind::CalendarUnavailable,
            Self::IncompatibleMergeInputs { .. } => MieErrorKind::IncompatibleMergeInputs,
            Self::NonMonotonicInput { .. } => MieErrorKind::NonMonotonicInput,
        }
    }

    /// True if this error wraps an `io::Error` whose kind is `BrokenPipe`.
    /// Per L2-WRT-018 a broken-pipe condition on stdout output SHALL
    /// exit `0` with no error; the CLI driver uses this predicate.
    #[must_use]
    pub fn is_broken_pipe(&self) -> bool {
        matches!(
            self,
            Self::WriterError { source, .. } if source.kind() == io::ErrorKind::BrokenPipe
        )
    }

    /// True if this error originated in **file I/O** — the input could not be
    /// opened, was empty, or the read itself failed.
    ///
    /// Deliberately narrower than Python's `MieFileError`, which additionally
    /// covers the file-shape rejections (`NoValidRecords`, `HomogeneousPayload`,
    /// `TimestampFormatMismatch`, `IncompatibleMergeInputs`) and the
    /// destination guards (`InputOutputCollision`, `ClobberRefused`). Those are
    /// not I/O failures on the input, so folding them in would make the
    /// predicate mean less. Match on [`MieError::kind`] when you need the wider
    /// set. See `docs/ERROR-CATALOG.md` §2 for the full mapping.
    #[must_use]
    pub fn is_file_error(&self) -> bool {
        matches!(
            self.kind(),
            MieErrorKind::FileNotFound | MieErrorKind::FileEmpty | MieErrorKind::FileIo
        )
    }

    /// True if this error is tied to a specific record byte offset — the
    /// analogue of Python's `MieRecordError`, which this set matches exactly.
    ///
    /// Note that carrying an `offset` field is *not* on its own sufficient:
    /// `HomogeneousPayload` and `TimestampFormatMismatch` both cite an offset
    /// but reject the file as a whole, and Python classes them under
    /// `MieFileError` accordingly. Conversely `MergeInputsDropped` cites none
    /// yet is record-class: it replaces the `UnrecoverableSyncLoss` a partial
    /// merge used to end with, and keeps that class so code catching it does
    /// not break.
    #[must_use]
    pub fn is_record_error(&self) -> bool {
        matches!(
            self.kind(),
            MieErrorKind::InvalidTypeWord
                | MieErrorKind::UnknownTypeWord
                | MieErrorKind::RecordTruncated
                | MieErrorKind::FirstRecordTruncated
                | MieErrorKind::PayloadError
                | MieErrorKind::UnknownErrorCode
                | MieErrorKind::UnrecoverableSyncLoss
                | MieErrorKind::MergeInputsDropped
        )
    }
}

impl fmt::Display for MieError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::FileNotFound { path } => {
                write!(f, "MIE file not found: {}", path.display())
            }
            Self::FileEmpty { path } => {
                write!(f, "MIE file is empty (0 bytes): {}", path.display())
            }
            Self::FileIo { path, source } => {
                write!(f, "I/O error on {}: {}", path.display(), source)
            }
            Self::InvalidTypeWord {
                offset,
                raw_type_word,
                word_count,
            } => write!(
                f,
                "Record error at offset 0x{offset:X}: \
                 Invalid Type Word 0x{raw_type_word:04X} with word_count={word_count} (minimum is 5)"
            ),
            Self::UnknownTypeWord {
                offset,
                raw_type_word,
                message_type,
            } => write!(
                f,
                "Record error at offset 0x{offset:X}: \
                 Unknown message type 0x{message_type:02X} in Type Word 0x{raw_type_word:04X}. \
                 Known types: 0x01, 0x02, 0x04, 0x08, 0x10, 0x18, 0x20."
            ),
            Self::RecordTruncated {
                offset,
                record_bytes,
                available_bytes,
            } => write!(
                f,
                "Record error at offset 0x{offset:X}: \
                 Record requires {record_bytes} bytes but only {available_bytes} bytes remain in file"
            ),
            Self::FirstRecordTruncated {
                offset,
                record_bytes,
                available_bytes,
            } => write!(
                f,
                "Record error at offset 0x{offset:X}: \
                 First record after header detection is truncated -- \
                 Type Word declares {record_bytes} bytes but only \
                 {available_bytes} bytes remain in file"
            ),
            Self::PayloadError { offset, detail } => {
                write!(f, "Record error at offset 0x{offset:X}: {detail}")
            }
            Self::UnknownErrorCode { offset, error_code } => write!(
                f,
                "Record error at offset 0x{offset:X}: \
                 Unknown error code 0x{error_code:04X}. \
                 Known DDC codes: 0x011E, 0x0120, 0x0136, 0x0140, 0x0150. \
                 Known decoder codes: 0x2000, 0x2001."
            ),
            Self::NoValidRecords { path, scan_bytes } => write!(
                f,
                "No valid MIE records found in {} (scanned first {scan_bytes} bytes). \
                 The file may not be an MIE recording, or the records may begin past the scan window.",
                path.display()
            ),
            Self::HomogeneousPayload {
                path,
                offset,
                sample_records,
            } => write!(
                f,
                "Pathological homogeneous-payload input rejected ({}): \
                 the first {sample_records} candidate records starting at \
                 offset 0x{offset:X} are byte-identical in non-timestamp \
                 positions. The file is most likely a single-byte pad \
                 (e.g. 0x20-fill), not an MIE recording.",
                path.display()
            ),
            Self::WriterError {
                destination,
                source,
            } => write!(f, "Failed to write to {destination}: {source}"),
            Self::InputOutputCollision { path } => write!(
                f,
                "Output path resolves to the same file as the input ({}); \
                 decoding in-place is unsafe with a memory-mapped reader. \
                 Choose a different output path.",
                path.display()
            ),
            Self::ClobberRefused { path } => write!(
                f,
                "Refusing to overwrite existing file {} \
                 (--no-clobber or output.no_clobber is set). \
                 Remove the file or unset the flag to proceed.",
                path.display()
            ),
            Self::UnrecoverableSyncLoss {
                offset,
                sync_losses,
            } => write!(
                f,
                "Unrecoverable mid-file sync loss at offset 0x{offset:X} \
                 after {sync_losses} recovery attempt(s); the decoder could \
                 not reacquire sync within the scan window. \
                 Pass --allow-partial to keep what was decoded as a .partial file."
            ),
            Self::MergeInputsDropped {
                left_out,
                truncated,
                total,
            } => {
                let verb = |n: u64| if n == 1 { "was" } else { "were" };
                write!(
                    f,
                    "{left_out} of {total} merge inputs could not be read and {} left out",
                    verb(*left_out)
                )?;
                if *truncated > 0 {
                    write!(
                        f,
                        ", and {truncated} {} truncated by an unrecoverable sync loss",
                        verb(*truncated)
                    )?;
                }
                write!(f, " (see the per-input warnings above)")
            }
            Self::TimestampFormatMismatch {
                offset,
                irig_score,
                std_score,
                records_probed,
            } => write!(
                f,
                "Timestamp-format auto-detection is ambiguous starting at offset 0x{offset:X} \
                 (IRIG score: {irig_score}, Standard score: {std_score} over {records_probed} \
                 record(s) probed). Pass --input-time-format irig or --input-time-format standard to \
                 force the choice, or verify the file is actually an MIE recording."
            ),
            Self::CalendarUnavailable { detail } => write!(
                f,
                "Cannot render a calendar timestamp: {detail}. IRIG-B carries no year and \
                 no timezone, so a calendar rendering is refused rather than guessed at. \
                 Use --output-time-format doy to write the day-of-year form instead."
            ),
            Self::IncompatibleMergeInputs {
                file_index,
                path,
                detail,
            } => write!(
                f,
                "Cannot time-merge input #{file_index} ({}): {detail}. \
                 Multi-file merge requires every input to be calendar-locked IRIG \
                 (Standard-format, freerun IRIG, and mixed-format sets cannot be \
                 ordered on a common absolute timeline).",
                path.display()
            ),
            Self::NonMonotonicInput {
                file_index,
                path,
                prev_us,
                curr_us,
            } => write!(
                f,
                "merge: input #{file_index} ({}) is not internally time-sorted: \
                 timestamp stepped backward (prev_us={prev_us} curr_us={curr_us}). \
                 The time-merge assumes each input is in chronological capture order.",
                path.display()
            ),
        }
    }
}

impl std::error::Error for MieError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::FileIo { source, .. } | Self::WriterError { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// Result type alias for decoder operations.
pub type MieResult<T> = std::result::Result<T, MieError>;

#[cfg(test)]
mod tests {
    use super::*;

    /// Requirements: L3-RS-006
    #[test]
    fn display_includes_offset_in_hex() {
        let err = MieError::InvalidTypeWord {
            offset: 0xABCD,
            raw_type_word: 0x1234,
            word_count: 0,
        };
        let s = err.to_string();
        assert!(s.contains("0xABCD"));
        assert!(s.contains("0x1234"));
    }

    /// Requirements: L3-RS-006
    #[test]
    fn kind_classification() {
        let e = MieError::FileEmpty {
            path: PathBuf::from("/x"),
        };
        assert!(e.is_file_error());
        assert!(!e.is_record_error());

        let e = MieError::PayloadError {
            offset: 0,
            detail: "x".into(),
        };
        assert!(!e.is_file_error());
        assert!(e.is_record_error());

        // The sync-loss terminal is record-class: it names the offset it gave
        // up at, and Python's MieUnrecoverableSyncLossError extends
        // MieRecordError. It was omitted from the predicate until v2.12.0.
        let e = MieError::UnrecoverableSyncLoss {
            offset: 0x40,
            sync_losses: 2,
        };
        assert!(e.is_record_error(), "sync loss is a record-class failure");
        assert!(!e.is_file_error());
    }

    /// Every `MieErrorKind` must be deliberately classified as file-class,
    /// record-class, or neither — adding a variant without deciding fails here.
    ///
    /// This is the mechanical form of the "add it to `is_record_error()` or
    /// `is_file_error()`" step in MAINTAINER-GUIDE.md §"Adding an error
    /// variant". The predicates had drifted out of step with the Python
    /// hierarchy precisely because nothing pinned the whole boundary — only two
    /// spot-check assertions existed. Mirrored by
    /// `test_every_exception_class_is_classified` on the Python side.
    ///
    /// Requirements: L3-RS-006
    #[test]
    fn every_error_kind_is_deliberately_classified() {
        use MieErrorKind as K;

        // Record-class: tied to one record's byte offset. Matches the Python
        // classes extending MieRecordError, one for one.
        const RECORD: &[K] = &[
            K::InvalidTypeWord,
            K::UnknownTypeWord,
            K::RecordTruncated,
            K::FirstRecordTruncated,
            K::PayloadError,
            K::UnknownErrorCode,
            K::UnrecoverableSyncLoss,
            // Not tied to one offset, but it replaces the UnrecoverableSyncLoss
            // a partial merge used to end with, so it keeps that class: Python
            // code catching MieRecordError around such a merge still works.
            K::MergeInputsDropped,
        ];
        // File-class: an I/O failure on the input itself.
        const FILE: &[K] = &[K::FileNotFound, K::FileEmpty, K::FileIo];
        // Neither predicate, by design. Python groups the first four under
        // MieFileError (whole-file rejections and destination guards) and
        // leaves the last two directly under Aero1553Error; Rust's narrower
        // is_file_error() covers I/O only, so these answer false to both.
        const NEITHER: &[K] = &[
            K::NoValidRecords,
            K::HomogeneousPayload,
            K::TimestampFormatMismatch,
            K::CalendarUnavailable,
            K::IncompatibleMergeInputs,
            K::InputOutputCollision,
            K::ClobberRefused,
            K::WriterError,
            K::NonMonotonicInput,
        ];

        // Exhaustiveness: a new variant added to MieErrorKind but to none of the
        // three lists trips this match, which has no wildcard arm.
        for kind in RECORD.iter().chain(FILE).chain(NEITHER) {
            match kind {
                K::FileNotFound
                | K::FileEmpty
                | K::FileIo
                | K::InvalidTypeWord
                | K::UnknownTypeWord
                | K::RecordTruncated
                | K::FirstRecordTruncated
                | K::PayloadError
                | K::UnknownErrorCode
                | K::NoValidRecords
                | K::HomogeneousPayload
                | K::WriterError
                | K::InputOutputCollision
                | K::ClobberRefused
                | K::UnrecoverableSyncLoss
                | K::MergeInputsDropped
                | K::TimestampFormatMismatch
                | K::CalendarUnavailable
                | K::IncompatibleMergeInputs
                | K::NonMonotonicInput => {}
            }
        }
        let listed = RECORD.len() + FILE.len() + NEITHER.len();
        assert_eq!(
            listed, 20,
            "every MieErrorKind variant must appear in exactly one list; \
             add the new variant to RECORD, FILE or NEITHER (and to the \
             matching Python base class)"
        );

        for k in RECORD {
            assert!(sample(*k).is_record_error(), "{k:?} should be record-class");
            assert!(!sample(*k).is_file_error(), "{k:?} must not be file-class");
        }
        for k in FILE {
            assert!(sample(*k).is_file_error(), "{k:?} should be file-class");
            assert!(
                !sample(*k).is_record_error(),
                "{k:?} must not be record-class"
            );
        }
        for k in NEITHER {
            assert!(!sample(*k).is_file_error(), "{k:?} is not file-class");
            assert!(!sample(*k).is_record_error(), "{k:?} is not record-class");
        }
    }

    /// One representative `MieError` per kind, for the classification sweep.
    fn sample(kind: MieErrorKind) -> MieError {
        let p = || PathBuf::from("/x");
        match kind {
            MieErrorKind::FileNotFound => MieError::FileNotFound { path: p() },
            MieErrorKind::FileEmpty => MieError::FileEmpty { path: p() },
            MieErrorKind::FileIo => MieError::FileIo {
                path: p(),
                source: io::Error::other("x"),
            },
            MieErrorKind::InvalidTypeWord => MieError::InvalidTypeWord {
                offset: 0,
                raw_type_word: 0,
                word_count: 0,
            },
            MieErrorKind::UnknownTypeWord => MieError::UnknownTypeWord {
                offset: 0,
                raw_type_word: 0,
                message_type: 0,
            },
            MieErrorKind::RecordTruncated => MieError::RecordTruncated {
                offset: 0,
                record_bytes: 0,
                available_bytes: 0,
            },
            MieErrorKind::FirstRecordTruncated => MieError::FirstRecordTruncated {
                offset: 0,
                record_bytes: 0,
                available_bytes: 0,
            },
            MieErrorKind::PayloadError => MieError::PayloadError {
                offset: 0,
                detail: "x".into(),
            },
            MieErrorKind::UnknownErrorCode => MieError::UnknownErrorCode {
                offset: 0,
                error_code: 0,
            },
            MieErrorKind::NoValidRecords => MieError::NoValidRecords {
                path: p(),
                scan_bytes: 0,
            },
            MieErrorKind::HomogeneousPayload => MieError::HomogeneousPayload {
                path: p(),
                offset: 0,
                sample_records: 0,
            },
            MieErrorKind::WriterError => MieError::WriterError {
                destination: "x".into(),
                source: io::Error::other("x"),
            },
            MieErrorKind::InputOutputCollision => MieError::InputOutputCollision { path: p() },
            MieErrorKind::ClobberRefused => MieError::ClobberRefused { path: p() },
            MieErrorKind::UnrecoverableSyncLoss => MieError::UnrecoverableSyncLoss {
                offset: 0,
                sync_losses: 0,
            },
            MieErrorKind::MergeInputsDropped => MieError::MergeInputsDropped {
                left_out: 1,
                truncated: 0,
                total: 2,
            },
            MieErrorKind::TimestampFormatMismatch => MieError::TimestampFormatMismatch {
                offset: 0,
                irig_score: 0,
                std_score: 0,
                records_probed: 0,
            },
            MieErrorKind::IncompatibleMergeInputs => MieError::IncompatibleMergeInputs {
                file_index: 0,
                path: p(),
                detail: "x".into(),
            },
            MieErrorKind::NonMonotonicInput => MieError::NonMonotonicInput {
                file_index: 0,
                path: p(),
                prev_us: 0,
                curr_us: 0,
            },
            MieErrorKind::CalendarUnavailable => {
                MieError::CalendarUnavailable { detail: "x".into() }
            }
        }
    }

    /// Requirements: L3-RS-006
    #[test]
    fn source_chain_for_io_errors() {
        let io_err = io::Error::new(io::ErrorKind::PermissionDenied, "nope");
        let e = MieError::FileIo {
            path: PathBuf::from("/x"),
            source: io_err,
        };
        assert!(std::error::Error::source(&e).is_some());
    }
}
