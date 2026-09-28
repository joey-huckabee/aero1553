"""Custom exceptions for the Aero1553 package.

All exceptions inherit from :class:`Aero1553Error`, allowing callers
to catch the full family with a single ``except`` clause while still
being able to discriminate specific failure modes.

Exception hierarchy::

    Aero1553Error
    ├── MieFileError
    │   ├── MieFileNotFoundError
    │   ├── MieFileEmptyError
    │   ├── MieNoValidRecordsError
    │   ├── MieInputOutputCollisionError
    │   ├── MieClobberRefusedError
    │   ├── MieIncompatibleMergeInputsError
    │   ├── MieHomogeneousPayloadError
    │   ├── MieTimestampFormatMismatchError
    │   └── MieCalendarUnavailableError
    ├── MieRecordError
    │   ├── MieInvalidTypeWordError
    │   ├── MieUnknownTypeWordError
    │   ├── MieRecordTruncatedError
    │   ├── MieFirstRecordTruncatedError
    │   ├── MiePayloadError
    │   ├── MieUnknownErrorCodeError
    │   └── MieUnrecoverableSyncLossError
    ├── MieNonMonotonicInputError
    └── MieWriterError
"""

from __future__ import annotations


class Aero1553Error(Exception):
    """Base exception for every error the decoder *converts*.

    Catch it to handle any decoder-level failure with a single handler::

        try:
            for msg in MieFileReader("recording.mie"):
                process(msg)
        except Aero1553Error as exc:
            logger.error("Decoder failure: %s", exc)

    Input-side failures are all converted, including the ones raised by the
    open/mmap itself — permission denied, a path that is not a regular file, a
    device I/O error — which become :class:`MieFileIoError` (v2.12.0; they
    previously escaped as a bare ``OSError`` and slipped past the handler
    above). Every variant of Rust's ``MieError`` now has a class here, and the
    two sets correspond one to one.

    Output-side failures still reach you as ``OSError`` where the standard
    library raises them outside a converted path, so a caller writing to an
    arbitrary sink may still want ``(Aero1553Error, OSError)`` — which is
    what the CLI uses, chiefly so a broken pipe on stdout stays a clean exit.
    """


class MieFileError(Aero1553Error):
    """Base exception for file-level errors.

    Never raised directly — it exists to be caught, and every file-level
    failure is one of its subclasses: the file is missing, empty, or
    unreadable; it holds no decodable records, is a single-byte pad, or has an
    undecidable timestamp format; it cannot join a merge; or the destination is
    refused. Corresponds to Rust's `is_file_error()` predicate **plus** the
    whole-file rejections and destination guards that predicate excludes; see
    `docs/ERROR-CATALOG.md` §2.
    """


class MieFileNotFoundError(MieFileError):
    """Raised when the specified MIE binary file does not exist.

    Attributes:
        path: The file path that was not found.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"MIE file not found: {path}")


class MieNoValidRecordsError(MieFileError):
    """Raised when the input file contains no decodable MIE records.

    Per L1-EXIT-002 the CLI maps this to exit code ``2``. Typically means the
    file is not an MIE recording at all (e.g., a TOML file passed as
    input by mistake) or the records begin past the 64 KB header scan
    window.

    Attributes:
        path: Path that was scanned.
        scan_bytes: Number of bytes scanned before giving up.
    """

    def __init__(self, path: str, scan_bytes: int) -> None:
        self.path = path
        self.scan_bytes = scan_bytes
        super().__init__(
            f"No valid MIE records found in {path} "
            f"(scanned first {scan_bytes} bytes). "
            f"The file may not be an MIE recording, or the records may "
            f"begin past the scan window."
        )


class MieFileEmptyError(MieFileError):
    """Raised when the specified MIE binary file exists but is zero bytes.

    An empty file cannot contain any valid records and indicates either
    a recording that was started but never received data, or a file
    transfer error.

    Attributes:
        path: The file path that was empty.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"MIE file is empty (0 bytes): {path}")


class MieFileIoError(MieFileError):
    """Raised when opening, stat-ing, reading or memory-mapping the input fails.

    Covers the failures that are neither "missing" nor "empty": permission
    denied, a path that is not a regular file, a device-level read error. The
    underlying ``OSError`` is preserved as ``source`` and chained via
    ``raise ... from``, so nothing is lost by the conversion.

    Mirrors Rust's ``MieError::FileIo``, including the ordering: the input is
    opened *before* its size is checked, so an unopenable path reports an I/O
    failure rather than being mis-reported as empty (a directory stats as zero
    bytes on Windows).

    Attributes:
        path: The file path whose I/O failed.
        source: The originating ``OSError``.
    """

    def __init__(self, path: str, source: OSError) -> None:
        self.path = path
        self.source = source
        super().__init__(f"I/O error on {path}: {source}")


class MieInputOutputCollisionError(MieFileError):
    """Raised when the output path resolves to the same file as the input.

    Per L2-WRT-014, decoding in-place is unsafe with a memory-mapped
    reader. The implementation rejects this configuration before any
    output file is opened.

    Attributes:
        path: The colliding path (both input and output resolve here).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"Output path resolves to the same file as the input ({path}); "
            f"decoding in-place is unsafe with a memory-mapped reader. "
            f"Choose a different output path."
        )


class MieClobberRefusedError(MieFileError):
    """Raised when ``--no-clobber`` is set and the destination exists.

    Per L2-WRT-017, the implementation refuses to overwrite an existing
    file when the no-clobber flag is in effect (CLI ``--no-clobber`` or
    config ``output.no_clobber = true``).

    Attributes:
        path: The destination path that already exists.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"Refusing to overwrite existing file {path} "
            f"(--no-clobber or output.no_clobber is set). "
            f"Remove the file or unset the flag to proceed."
        )


class MieIncompatibleMergeInputsError(MieFileError):
    """Raised when a multi-file merge cannot order its inputs on a common
    absolute timeline.

    Per L1-EXIT-009 / L2-MRG-003: time-sorted merge requires every input to
    be calendar-locked IRIG. A Standard-format input, a freerun-leading
    input, or a set that mixes timestamp formats is rejected before any
    output is written. Maps to CLI exit code 6.

    Attributes:
        file_index: Index of the offending input in resolved order.
        path: Path of the offending input.
        detail: The specific reason it cannot be merged.
    """

    def __init__(self, file_index: int, path: str, detail: str) -> None:
        self.file_index = file_index
        self.path = path
        self.detail = detail
        super().__init__(
            f"Cannot time-merge input #{file_index} ({path}): {detail}. "
            f"Multi-file merge requires every input to be calendar-locked "
            f"IRIG (Standard-format, freerun IRIG, and mixed-format sets "
            f"cannot be ordered on a common absolute timeline)."
        )


class MieNonMonotonicInputError(Aero1553Error):
    """Raised in strict mode when a merge input is not internally time-sorted.

    Per L2-MRG-006 the time-merge assumes each input file's records are in
    chronological capture order. A backward microsecond step within one file
    (sync-loss recovery or a day/year rollover) means the merged output may be
    out of order for that input. Strict mode surfaces this as a record error
    (the CLI maps it to exit code ``1``); lenient mode only logs a WARN and
    keeps going.

    Attributes:
        file_index: Index of the offending input in resolved order.
        path: Path of the offending input.
        prev_us: Microsecond key of the previous record from that file.
        curr_us: Microsecond key of the backward-stepping record.
    """

    def __init__(self, file_index: int, path: str, prev_us: int, curr_us: int) -> None:
        self.file_index = file_index
        self.path = path
        self.prev_us = prev_us
        self.curr_us = curr_us
        super().__init__(
            f"merge: input #{file_index} ({path}) is not internally "
            f"time-sorted: timestamp stepped backward "
            f"(prev_us={prev_us} curr_us={curr_us}). The time-merge assumes "
            f"each input is in chronological capture order."
        )


class MieRecordError(Aero1553Error):
    """Base exception for record-level decoding errors.

    Raised when an individual binary record within a valid file cannot
    be decoded. Includes the byte offset of the problematic record for
    diagnostic purposes.

    Attributes:
        offset: Byte offset of the record in the source file.
        detail: Human-readable description of the error.
    """

    def __init__(self, offset: int, detail: str) -> None:
        self.offset = offset
        self.detail = detail
        super().__init__(f"Record error at offset 0x{offset:X}: {detail}")


class MieInvalidTypeWordError(MieRecordError):
    """Raised when a Type Word produces an invalid or zero word count.

    A word count below the minimum record size (5 words = Type Word +
    3 Timestamp words + Command Word) indicates either file corruption
    or an unsupported record type.

    Attributes:
        offset: Byte offset of the record.
        raw_type_word: The raw 16-bit Type Word value.
        word_count: The decoded word count that was invalid.
    """

    def __init__(self, offset: int, raw_type_word: int, word_count: int) -> None:
        self.raw_type_word = raw_type_word
        self.word_count = word_count
        super().__init__(
            offset,
            f"Invalid Type Word 0x{raw_type_word:04X} with word_count={word_count} (minimum is 5)",
        )


class MieUnknownTypeWordError(MieRecordError):
    """Raised when a Type Word contains an unrecognized message type code.

    The DDC MIE format defines seven message type codes in bits 0–6 of
    the Type Word: 0x01 (Mode Command), 0x02 (BC→RT), 0x04 (RT→BC),
    0x08 (RT→RT), 0x10 (Broadcast BC→RT), 0x18 (Broadcast RT→RT), and
    0x20 (Spurious Data). Any other value indicates either file
    corruption, a firmware version producing an unknown format, or an
    undocumented DDC extension.

    When encountered, the decoder prints diagnostic information to the
    console including the byte offset, raw Type Word value, and
    surrounding context to assist in reverse engineering the unknown
    format.

    Attributes:
        offset: Byte offset of the record in the source file.
        raw_type_word: The raw 16-bit Type Word value.
        message_type: The unrecognized message type code (bits 0–6).
    """

    def __init__(self, offset: int, raw_type_word: int, message_type: int) -> None:
        self.raw_type_word = raw_type_word
        self.message_type = message_type
        super().__init__(
            offset,
            f"Unknown message type 0x{message_type:02X} in Type Word "
            f"0x{raw_type_word:04X}. Known types: 0x01 (Mode Command), "
            f"0x02 (BC->RT), 0x04 (RT->BC), 0x08 (RT->RT), "
            f"0x10 (Broadcast BC->RT), 0x18 (Broadcast RT->RT), "
            f"0x20 (Spurious Data).",
        )


class MieRecordTruncatedError(MieRecordError):
    """Raised when a record extends beyond the end of the file.

    This typically occurs at the end of a recording that was terminated
    mid-write. The reader can optionally skip truncated final records
    rather than raising this exception.

    Attributes:
        offset: Byte offset of the record start.
        record_bytes: Expected record size in bytes.
        available_bytes: Actual bytes remaining in the file.
    """

    def __init__(self, offset: int, record_bytes: int, available_bytes: int) -> None:
        self.record_bytes = record_bytes
        self.available_bytes = available_bytes
        super().__init__(
            offset,
            f"Record requires {record_bytes} bytes but only {available_bytes} bytes remain in file",
        )


class MieHomogeneousPayloadError(MieFileError):
    """Raised when the input file looks like a pathological single-byte
    pad rather than an MIE recording.

    Per L2-SYN-018 the reader applies an additional defense beyond the
    Type Word / look-ahead validation: after a candidate record is
    accepted, the first N=4 consecutive candidate records are compared
    in their non-timestamp byte positions. If all N records are
    byte-identical outside the timestamp triple, the file is rejected
    with this error class.

    The motivating case is a 0x20-padded file, where ``0x20 0x20``
    parses as a valid SPURIOUS_DATA Type Word and the two-record
    look-ahead heuristic alone admits the stream — extracting it
    would emit millions of synthetic SPURIOUS_DATA records.

    Both strict and lenient mode reject. This is a "wrong-file-shape"
    error analogous to :class:`MieNoValidRecordsError`, not a
    per-record failure that lenient mode might skip.

    Attributes:
        path: The file path that was rejected.
        offset: Byte offset of the homogeneous run start.
        sample_records: Number of consecutive identical records sampled.
    """

    def __init__(self, path: str, offset: int, sample_records: int) -> None:
        self.path = path
        self.offset = offset
        self.sample_records = sample_records
        super().__init__(
            f"Pathological homogeneous-payload input rejected ({path}): "
            f"the first {sample_records} candidate records starting at "
            f"offset 0x{offset:X} are byte-identical in non-timestamp "
            f"positions. The file is most likely a single-byte pad "
            f"(e.g. 0x20-fill), not an MIE recording."
        )


class MieTimestampFormatMismatchError(MieFileError):
    """Raised when L2-DEC-015 timestamp-format auto-detection completes
    with a confidence below the L2-DEC-016 floor.

    The L2-DEC-015 probe walks the first N records (default 8) and
    aggregates per-record IRIG vs Standard scoring. When the winning
    aggregate score is below the confidence floor, OR when the margin
    between the two candidate scores is below the minimum-margin
    threshold, the call is classified as ``AMBIGUOUS`` and — in
    strict mode — raised as this exception. Lenient mode logs a WARN
    instead and uses the chosen format anyway.

    Maps to exit class 2 in the CLI per L1-EXIT-002 — the file is
    semantically a "wrong file type" case, alongside
    :class:`MieNoValidRecordsError` and
    :class:`MieHomogeneousPayloadError`.

    Attributes:
        offset: Byte offset where the probe started.
        irig_score: Aggregated IRIG score across the probe set.
        std_score: Aggregated Standard score across the probe set.
        records_probed: Number of records actually probed.
    """

    def __init__(
        self,
        offset: int,
        irig_score: int,
        std_score: int,
        records_probed: int,
    ) -> None:
        self.offset = offset
        self.irig_score = irig_score
        self.std_score = std_score
        self.records_probed = records_probed
        super().__init__(
            f"Timestamp-format auto-detection is ambiguous starting at "
            f"offset 0x{offset:X} (IRIG score: {irig_score}, Standard "
            f"score: {std_score} over {records_probed} record(s) "
            f"probed). Pass --input-time-format irig or --input-time-format "
            f"standard to force the choice, or verify the file is "
            f"actually an MIE recording."
        )


class MieCalendarUnavailableError(MieFileError):
    """Raised when a calendar rendering cannot be produced (L2-WRT-026).

    A calendar rendering (``iso`` / ``dom``) was requested but the recording
    cannot supply a calendar date: it is Standard-format or freerun IRIG
    (clause 2), or a record's day-of-year does not exist in the configured year
    (clause 3). Raised before any row is written in the first case and at the
    offending record in the second.

    Maps to exit class 2 in the CLI -- "the file cannot satisfy this request",
    the class it shares with :class:`MieTimestampFormatMismatchError`. Day 366
    against a common year lands here rather than being a per-record error
    because it says the *configured year* is wrong, which makes every date in
    the run suspect, not just that one row.

    Attributes:
        detail: The specific reason, named for the operator.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(
            f"Cannot render a calendar timestamp: {detail}. IRIG-B carries no year "
            f"and no timezone, so a calendar rendering is refused rather than guessed "
            f"at. Use --output-time-format doy to write the day-of-year form instead."
        )


class MieFirstRecordTruncatedError(MieRecordError):
    """Raised when the FIRST record after header detection is truncated.

    Per L2-RDR-004 this is the post-header counterpart to L2-RDR-002 /
    L2-RDR-003: the file contains a structurally-valid Type Word (known
    message type, plausible word count, optional valid IRIG range) at
    or after the header, but the Type Word's declared extent runs past
    end-of-file. Strict mode surfaces this as a distinct error class
    (separate from generic :class:`MieRecordTruncatedError`); lenient
    mode terminates cleanly with zero records emitted.

    The distinction matters operationally: a generic
    :class:`MieRecordTruncatedError` usually means a mid-stream cut
    after at least one valid record; a
    :class:`MieFirstRecordTruncatedError` means the recording was
    aborted before the first complete record was written.

    Attributes:
        offset: Byte offset of the structurally-valid Type Word.
        record_bytes: Number of bytes the Type Word declares.
        available_bytes: Bytes remaining in the file from ``offset``.
    """

    def __init__(self, offset: int, record_bytes: int, available_bytes: int) -> None:
        self.record_bytes = record_bytes
        self.available_bytes = available_bytes
        super().__init__(
            offset,
            f"First record after header detection is truncated: "
            f"Type Word declares {record_bytes} bytes but only "
            f"{available_bytes} bytes remain in file",
        )


class MiePayloadError(MieRecordError):
    """Raised when a record's payload cannot be extracted.

    Indicates that the data words, status word, or command word within
    a record are inconsistent with the Type Word's declared word count
    or the Command Word's declared data word count.

    Attributes:
        offset: Byte offset of the record.
    """


class MieWriterError(Aero1553Error):
    """Raised when CSV or other output writing fails.

    Wraps underlying I/O errors with decoder-specific context.

    Attributes:
        destination: Description of the output destination (path or "stdout").
    """

    def __init__(self, destination: str, cause: Exception) -> None:
        self.destination = destination
        self.cause = cause
        super().__init__(f"Failed to write to {destination}: {cause}")


class MieUnrecoverableSyncLossError(MieRecordError):
    """Raised in lenient mode when sync recovery exhausts mid-file.

    Per L1-EXIT-004 the CLI maps this to exit code ``3`` by default, or to a
    ``.partial`` commit + exit ``0`` when ``--allow-partial`` is set.
    Carries the cumulative recovery-attempt count for the decode
    invocation so the operator can correlate with sync-loss WARN logs.

    Attributes:
        offset: Byte offset of the record that triggered the
            unrecoverable loss.
        sync_losses: Cumulative recovery attempts when the loss became
            unrecoverable.
    """

    def __init__(self, offset: int, sync_losses: int) -> None:
        self.sync_losses = sync_losses
        super().__init__(
            offset,
            f"Unrecoverable mid-file sync loss after {sync_losses} "
            f"recovery attempt(s); the decoder could not reacquire sync "
            f"within the scan window. Pass --allow-partial to keep what "
            f"was decoded as a .partial file.",
        )


class MieUnknownErrorCodeError(MieRecordError):
    """Raised when an errored record contains an unrecognized error code.

    DDC hardware error codes occupy the 0x01xx range. Aero1553 custom
    codes occupy the 0x20xx range. Any error code outside these known
    sets indicates either an undocumented DDC firmware version or file
    corruption.

    Attributes:
        offset: Byte offset of the record in the source file.
        error_code: The unrecognized 16-bit error code value.
    """

    def __init__(self, offset: int, error_code: int) -> None:
        self.error_code = error_code
        super().__init__(
            offset,
            f"Unknown error code 0x{error_code:04X}. "
            f"Known DDC codes: 0x011E, 0x0120, 0x0136, 0x0140, 0x0150. "
            f"Known decoder codes: 0x2000, 0x2001.",
        )
