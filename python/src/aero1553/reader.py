"""Sequential reader for DDC MIE binary recording files.

This module provides :class:`MieFileReader`, an iterator that reads an
MIE binary file and yields fully decoded :class:`~aero1553.models.MieMessage`
instances in file order. It handles:

- **Header detection**: Automatically finds the first valid record,
  skipping any proprietary file headers.
- **Continuous sync validation**: Each record boundary is verified
  before decoding using a configurable N-record look-ahead (default 2).
- **Sync recovery**: If a record fails validation mid-file, scans
  forward in word-aligned steps to find the next valid record.
- **Timestamp format detection**: Auto-detects IRIG vs Standard.
- **All 10 MIL-STD-1553 message formats** plus SPURIOUS_DATA.
- **Error record handling**: Truncated payloads with Error Words.
- **SPURIOUS_DATA continuation detection**.
- **Per-RT/MSG DELTA** calculation.
- **Memory-efficient mmap I/O**.

Sync Recovery:

    The reader maintains sync through a validate-then-decode approach.
    At each record boundary, :func:`~aero1553.sync.validate_record`
    confirms the Type Word is valid, the word count is plausible, and
    the next record's Type Word also looks valid (look-ahead).

    If validation fails, the reader calls
    :func:`~aero1553.sync.recover_sync` which scans forward in
    2-byte steps until it finds a valid record. If recovery fails
    (no valid record within the scan window), iteration stops.

    Error records (bit 14 set) and their SPURIOUS_DATA continuations
    are valid records that pass sync validation normally. Sync loss
    only occurs when the DDC card writes truly corrupt data (e.g.,
    truncated mid-word, power loss during recording).
"""

from __future__ import annotations

import logging
import mmap
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from aero1553.decode import (
    DEFAULT_DETECT_RECORDS,
    DEFAULT_MUX_DELIMITER,
    DEFAULT_MUX_ENABLED,
    DEFAULT_MUX_FIELD,
    MIN_RECORD_BYTES_STANDARD,
    DetectionConfidence,
    classify_message_format,
    decode_command_word,
    decode_irig_timestamp,
    decode_standard_timestamp,
    decode_type_word,
    detect_record_anomalies,
    is_terminator_type_word,
    mux_from_filename,
    probe_timestamp_format,
    read_u16,
    read_u16_array,
    validate_post_extract_invariants,
    validate_structural_invariants,
)
from aero1553.delta import DeltaKind, DeltaTracker
from aero1553.exceptions import (
    MieFileEmptyError,
    MieFileIoError,
    MieFileNotFoundError,
    MieFirstRecordTruncatedError,
    MieHomogeneousPayloadError,
    MieInvalidTypeWordError,
    MieNoValidRecordsError,
    MiePayloadError,
    MieRecordTruncatedError,
    MieTimestampFormatMismatchError,
    MieUnknownErrorCodeError,
    MieUnknownTypeWordError,
    MieUnrecoverableSyncLossError,
)
from aero1553.logger import irig_day_advisory
from aero1553.models import (
    ERROR_SPURIOUS_CONTINUATION,
    ERROR_SPURIOUS_STANDALONE,
    KNOWN_DDC_ERROR_CODES,
    TIMESTAMP_WORD_COUNTS,
    CommandWord,
    MessageFormat,
    MessageType,
    MieMessage,
    Timestamp,
    TimestampFormat,
    TypeWord,
)
from aero1553.sync import (
    DEFAULT_LOOKAHEAD_RECORDS,
    ValidationFailure,
    find_first_record,
    recover_sync,
    validate_record_detailed,
)

logger = logging.getLogger(__name__)


@dataclass
class _DecodeState:
    """Mutable per-iteration decode state threaded through the record helpers.

    ``__iter__`` owns one instance for the duration of a decode; the
    ``_decode_one`` / ``_build_*`` helpers read and update it in place so the
    generator's ``yield`` sites can stay in ``__iter__`` while the per-record
    logic lives in named methods.
    """

    prev_was_error: bool
    warned_irig_day: bool
    #: Per-RT/MSG DELTA state (L2-RDR-009/010/017/018/019). Owned by
    #: :mod:`aero1553.delta`, which is also what the merge uses for
    #: ``--delta-scope global`` -- before that extraction the key was spelled
    #: three different ways across two modules.
    delta_tracker: DeltaTracker
    msg_count: int
    #: L2-WRT-026 clause 4: highest day-of-year seen so far from a
    #: calendar-locked record, for the rollover watch. None until the first.
    last_irig_day: int | None = None
    #: Whether the clause 4 rollover warning has fired for this input. One
    #: per decode, like every other observational warning here.
    warned_day_rollover: bool = False


@dataclass
class _Step:
    """Outcome of decoding a single record.

    ``message`` is the :class:`MieMessage` to yield (``None`` for a
    skipped/recovered/terminated record), ``next_offset`` is where the loop
    resumes, and ``stop`` ends iteration cleanly (terminator or lenient EOF).
    """

    message: MieMessage | None
    next_offset: int
    stop: bool = False


class MieFileReader:
    """Memory-mapped sequential reader for MIE binary files.

    Reads a DDC MIE binary recording file and yields decoded
    :class:`~aero1553.models.MieMessage` instances. Uses
    ``mmap`` for efficient access to large files without loading
    the entire file into memory.

    The reader automatically handles:

    - **File headers**: Scans from offset 0 to find the first valid
      record. Files that start directly with records (offset 0) and
      files with proprietary headers (e.g., embedded equipment names)
      are both supported transparently.

    - **Sync loss and recovery**: If the reader encounters an invalid
      record mid-file (corruption, unexpected padding, partial writes),
      it scans forward in 2-byte steps to find the next valid record.
      In strict mode, sync loss raises an exception instead.

    - **Error records**: Records with bit 14 set contain a truncated
      payload plus an appended Error Word. These are valid records
      that maintain sync — the word count correctly describes the
      record length including the Error Word.

    - **SPURIOUS_DATA continuations**: After an error record, the
      remaining words from the interrupted transaction may appear as
      a SPURIOUS_DATA (0x20) record. The reader tracks the error→
      spurious linkage and assigns appropriate custom error codes
      (0x2000 for continuation, 0x2001 for standalone).

    Args:
        path: Path to the MIE binary file.
        strict: If ``True``, raise exceptions on sync loss, invalid
            records, and unknown error codes instead of recovering.
        input_time_format: Timestamp format. ``AUTO`` (default) detects from
            the first record.

    Raises:
        MieFileNotFoundError: If the file does not exist.
        MieFileEmptyError: If the file is zero bytes.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        strict: bool = False,
        input_time_format: TimestampFormat = TimestampFormat.AUTO,
        detect_records: int = DEFAULT_DETECT_RECORDS,
        lookahead_records: int = DEFAULT_LOOKAHEAD_RECORDS,
        standard_tick_rate_hz: float | None = None,
        mux_enabled: bool = DEFAULT_MUX_ENABLED,
        mux_delimiter: str = DEFAULT_MUX_DELIMITER,
        mux_field: int = DEFAULT_MUX_FIELD,
        calendar_year: int | None = None,
    ) -> None:
        self._path = Path(path)
        self._strict = strict
        # L2-WRT-026 clause 4 / L2-LOG-002: the calendar year a calendar
        # rendering will resolve day-of-year against, or None under `doy`.
        # The reader has no rendering concern of its own; it takes this
        # because two of its diagnostics change meaning when the day-of-year
        # field becomes load-bearing.
        self._calendar_year = calendar_year
        self._input_time_format = input_time_format
        # L2-DEC-015: probe size for auto-detection. Clamped to >= 1
        # (a no-probe call is nonsensical). The CLI / config layer
        # validates the upper bound; we mirror the clamp here so a
        # library caller can't break the invariant by passing 0.
        self._detect_records = max(1, detect_records)
        # L2-SYN-026: validate_record look-ahead depth. Clamped to
        # >= 1 with the same library-caller-friendly invariant as
        # detect_records.
        self._lookahead_records = max(1, lookahead_records)
        # L2-DEC-017: optional Standard-counter tick rate in Hz. None
        # (the default) keeps the historical empty-DELTA behavior for
        # Standard records; a finite, strictly-positive value enables
        # tick->microsecond conversion and DELTA participation. The CLI /
        # config layer validates the value; handed to the DeltaTracker
        # this reader builds for each iteration.
        self._standard_tick_rate_hz = standard_tick_rate_hz
        # Cumulative sync-recovery count from the most recent __iter__
        # call. Reset to 0 on each iteration so the CLI can read it
        # after the loop completes (for L1-EXIT-003 / L1-EXIT-005 exit-class
        # summary). Mirrors `MieFileReader::sync_losses` in Rust.
        self._sync_losses: int = 0
        # L1-EXIT-010 / L2-RDR-021: set True during __iter__ when the input is a
        # valid but *empty* recording — its record stream opens directly on the
        # end-of-records terminator (a null Type Word), so zero records are
        # yielded but the file is a legitimate MIE recording (not a wrong-file
        # NoValidRecords rejection). The CLI queries this after a successful
        # zero-record decode to report the empty-recording exit class at exit 0.
        # Reset at the start of each __iter__ call. Mirrors Rust's
        # ``MieFileReader::empty_recording``.
        self._empty_recording: bool = False
        # L2-WRT-020: resolve the per-file MUX value once, from the file name.
        # Shared by reference across every message this reader yields.
        self._mux: str | None = (
            mux_from_filename(self._path.name, mux_delimiter, mux_field) if mux_enabled else None
        )
        if not self._path.exists():
            raise MieFileNotFoundError(str(self._path))
        # Order mirrors `MieFileReader::new` in Rust: open first, size second.
        # Checking the size first would mis-report an unopenable path as empty,
        # because a directory stats as zero bytes on Windows. Taking the size
        # from the open handle also closes the stat/open race.
        try:
            with open(self._path, "rb") as probe:
                self._file_size = os.fstat(probe.fileno()).st_size
        except OSError as exc:
            raise MieFileIoError(str(self._path), exc) from exc
        if self._file_size == 0:
            raise MieFileEmptyError(str(self._path))
        logger.debug(
            "Initialized reader for %s (%d bytes, strict=%s, "
            "input_time_format=%s, detect_records=%d)",
            self._path,
            self._file_size,
            self._strict,
            self._input_time_format.name,
            self._detect_records,
        )

    @property
    def path(self) -> Path:
        """Path to the source MIE binary file."""
        return self._path

    @property
    def file_size(self) -> int:
        """Size of the source file in bytes."""
        return self._file_size

    @property
    def sync_losses(self) -> int:
        """Cumulative sync-recovery count from the most recent
        ``__iter__`` call. Reset to 0 each iteration.

        Used by the CLI's L1-EXIT-005 exit-class summary to distinguish
        Complete (sync_losses == 0) from PartialRecovered (sync_losses
        > 0 with a successful full decode).
        """
        return self._sync_losses

    @property
    def empty_recording(self) -> bool:
        """Whether the most recent ``__iter__`` classified the input as a valid
        but empty recording (record stream opens on the end-of-records
        terminator; zero records, but not a wrong-file rejection). Reset each
        ``__iter__`` call. Per L1-EXIT-010 the CLI uses this to emit the
        ``empty-recording`` exit class and write a header-only CSV at exit 0.
        """
        return self._empty_recording

    def __iter__(self) -> Iterator[MieMessage]:
        """Iterate over all decoded messages in file order.

        The iterator performs the following steps for each record:

        1. **Validate** the current offset with :func:`validate_record`.
        2. If invalid, **recover sync** by scanning forward.
        3. **Decode** the Type Word, timestamp, command word(s),
           status word(s), data words, and error word (if applicable).
        4. **Yield** the decoded :class:`MieMessage`.
        5. **Advance** to the next record boundary.

        Yields:
            Decoded MieMessage instances, one per binary record.

        Raises:
            MieFileIoError: if the file cannot be memory-mapped or read.
            MieRecordError: any per-record failure the decode loop escalates --
                in strict mode these abort the walk, in lenient mode most are
                logged and skipped. See :meth:`_decode_one`.
        """
        state = _DecodeState(
            prev_was_error=False,
            warned_irig_day=False,
            delta_tracker=DeltaTracker(self._standard_tick_rate_hz),
            msg_count=0,
        )
        # Reset the externally-visible flags at the start of each iteration so
        # successive __iter__ calls on the same reader handle don't accumulate
        # stale counts.
        self._sync_losses = 0
        self._empty_recording = False

        resolved_format: TimestampFormat | None = None
        if self._input_time_format != TimestampFormat.AUTO:
            resolved_format = self._input_time_format

        logger.info("Beginning decode of %s", self._path.name)

        # Acquisition is wrapped, the decode loop below is not: an OSError from
        # open/mmap is a file-level failure (MieFileIoError, mirroring Rust's
        # FileIo), while one raised further down belongs to whatever raised it.
        try:
            fh = open(self._path, "rb")
        except OSError as exc:
            raise MieFileIoError(str(self._path), exc) from exc
        with fh:
            fd = fh.fileno()
            try:
                mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
            except OSError as exc:
                raise MieFileIoError(str(self._path), exc) from exc
            with mm:
                file_len = len(mm)

                setup = self._detect_format_and_setup(mm, file_len, resolved_format)
                if setup is None:
                    return
                offset, resolved_format = setup

                loop_min = MIN_RECORD_BYTES_STANDARD
                while offset + loop_min <= file_len:
                    step = self._decode_one(mm, offset, file_len, resolved_format, state)
                    offset = step.next_offset
                    if step.message is not None:
                        yield step.message
                        state.msg_count += 1
                        if state.msg_count % 100_000 == 0:
                            logger.info(
                                "Decoded %d messages (0x%X / 0x%X)",
                                state.msg_count,
                                offset,
                                file_len,
                            )
                    if step.stop:
                        break

        logger.info(
            "Decode complete: %d messages, %d sync recoveries, format=%s, file=%s",
            state.msg_count,
            self._sync_losses,
            resolved_format.name if resolved_format else "unknown",
            self._path.name,
        )

    # ── Setup: locate first record + finalize timestamp format ──────────────

    def _detect_format_and_setup(
        self,
        mm: mmap.mmap,
        file_len: int,
        resolved_format: TimestampFormat | None,
    ) -> tuple[int, TimestampFormat] | None:
        """Locate the first record and finalize the timestamp format.

        Runs once before the decode loop. Returns ``(start_offset,
        resolved_format)`` to begin iteration, or ``None`` when the record
        stream is legitimately empty or (in lenient mode) ends before the
        first record — the caller yields zero records. Raises the appropriate
        ``Mie*`` error for wrong-file / strict-mode conditions.

        Returns:
            ``(start_offset, resolved_format)`` to begin iteration, or ``None``
            when the record stream is legitimately empty or (in lenient mode)
            ends before the first record -- the caller then yields zero records.
        """
        # ── Find first record (header detection) ───────
        start_offset = find_first_record(
            mm,
            file_len,
            ts_format=resolved_format,
            lookahead_records=self._lookahead_records,
        )
        if start_offset is None:
            self._handle_missing_first_record(mm, file_len, resolved_format)
            return None

        # L2-SYN-012: report the header size at INFO. Emitted here rather than
        # inside `find_first_record` so the sync helpers stay pure and both
        # implementations narrate this from the reader (mirrors `iter()` in
        # `rust/src/reader.rs`).
        if start_offset > 0:
            logger.info(
                "File header detected: %d bytes before first record at 0x%X",
                start_offset,
                start_offset,
            )
        else:
            logger.debug("First record at offset 0 (no header)")

        self._reject_homogeneous_payload(mm, start_offset)
        resolved_format = self._confirm_timestamp_format(mm, start_offset, resolved_format)
        return start_offset, resolved_format

    def _handle_missing_first_record(
        self,
        mm: mmap.mmap,
        file_len: int,
        resolved_format: TimestampFormat | None,
    ) -> None:
        """Diagnose a failed first-record scan.

        Returns normally when the caller should yield zero records (an empty
        recording, or lenient-mode first-record truncation); raises otherwise
        (wrong file, or strict-mode truncation).

        Raises:
            MieNoValidRecordsError: if no record was found in the scanned span,
                which means this is not an MIE recording.
            MieFirstRecordTruncatedError: in strict mode, if a record starts but
                is cut off by EOF. Lenient mode returns normally instead.
        """
        # L1-EXIT-010 / L2-RDR-021: a valid but *empty* recording opens
        # directly on the end-of-records terminator (a null Type Word). The
        # record stream is legitimately empty — this is NOT a wrong-file
        # NoValidRecordsError. Yield zero records cleanly (the writer emits a
        # header-only CSV) and flag the condition so the CLI can report the
        # empty-recording exit class at exit 0. An unrecognized non-null lead
        # word still falls through to the wrong-file diagnosis below,
        # preserving the guard against genuinely non-MIE inputs.
        if file_len >= 2 and is_terminator_type_word(read_u16(mm, 0)):
            logger.warning(
                "%s: recording contains no records -- the stream "
                "opens on the end-of-records terminator (empty "
                "capture); writing header-only output",
                self._path.name,
            )
            self._empty_recording = True
            return

        # L2-RDR-004: distinguish "no Type Word at all" from
        # "found a structurally-valid Type Word but its
        # declared extent runs past EOF". The latter gets
        # MieFirstRecordTruncatedError in strict mode and
        # terminates cleanly with zero records in lenient.
        from aero1553.sync import (
            MAX_SCAN_BYTES,
            diagnose_header_scan_failure,
        )

        truncated = diagnose_header_scan_failure(
            mm,
            file_len,
            ts_format=resolved_format,
        )
        if truncated is not None:
            offset_t, record_bytes, available = truncated
            if self._strict:
                logger.error(
                    "First record after header detection is "
                    "truncated at 0x%X: declared %d bytes, "
                    "only %d available",
                    offset_t,
                    record_bytes,
                    available,
                )
                raise MieFirstRecordTruncatedError(offset_t, record_bytes, available)
            logger.warning(
                "First record after header detection is "
                "truncated at 0x%X: declared %d bytes, "
                "only %d available -- lenient mode terminates "
                "cleanly with zero records",
                offset_t,
                record_bytes,
                available,
            )
            return

        # L1-EXIT-002: surface as an exception so the CLI maps
        # to exit 2 (and so library callers can react)
        # rather than silently yielding zero messages.
        scan_bytes = min(file_len, MAX_SCAN_BYTES)
        logger.error(
            "No valid records found in first %d bytes of %s",
            scan_bytes,
            self._path.name,
        )
        raise MieNoValidRecordsError(str(self._path), scan_bytes)

    def _reject_homogeneous_payload(self, mm: mmap.mmap, start_offset: int) -> None:
        """L2-SYN-018: reject pathological homogeneous-payload inputs.

        (e.g. 0x20-padded files where every "record" parses as a synthetic
        SPURIOUS_DATA frame). The candidate's Type Word tells us the record
        size; we compare the next N candidate-sized chunks for byte identity
        in non-timestamp positions.

        Raises:
            MieHomogeneousPayloadError: if the sampled chunks are byte-identical
                outside the timestamp triple, which means the input is padding
                rather than a recording.
        """
        from aero1553.sync import (
            HOMOGENEITY_SAMPLE_RECORDS,
            is_homogeneous_payload,
        )

        candidate_tw = decode_type_word(read_u16(mm, start_offset))
        candidate_record_bytes = candidate_tw.word_count * 2
        if is_homogeneous_payload(mm, start_offset, candidate_record_bytes):
            logger.error(
                "Pathological homogeneous-payload input at "
                "offset 0x%X in %s: %d consecutive candidate "
                "records are byte-identical",
                start_offset,
                self._path.name,
                HOMOGENEITY_SAMPLE_RECORDS,
            )
            raise MieHomogeneousPayloadError(
                str(self._path),
                start_offset,
                HOMOGENEITY_SAMPLE_RECORDS,
            )

    def _confirm_timestamp_format(
        self,
        mm: mmap.mmap,
        start_offset: int,
        resolved_format: TimestampFormat | None,
    ) -> TimestampFormat:
        """Resolve the final timestamp format via the multi-record probe.

        Auto-detects when unset (L2-DEC-015); otherwise sanity-checks the
        forced format against the probe (L2-DEC-013) and returns it unchanged.

        Returns:
            The format every record will be decoded with. Final for the file --
            there is no per-record re-detection (L2-DEC-011).
        """
        if resolved_format is None:
            return self._auto_detect_timestamp_format(mm, start_offset)
        self._sanity_check_forced_format(mm, start_offset, resolved_format)
        return resolved_format

    def _auto_detect_timestamp_format(
        self,
        mm: mmap.mmap,
        start_offset: int,
    ) -> TimestampFormat:
        """L2-DEC-015: multi-record probe to disambiguate IRIG vs Standard
        BEFORE iteration begins. The chosen format is final per L2-DEC-011 —
        no per-record re-detection.

        Returns:
            The detected :class:`TimestampFormat`.

        Raises:
            MieTimestampFormatMismatchError: if the probe cannot tell the two
                formats apart well enough to choose.
        """
        outcome = probe_timestamp_format(
            mm,
            start_offset,
            self._detect_records,
        )
        if outcome.confidence == DetectionConfidence.DECISIVE:
            logger.info(
                "Auto-detected timestamp format: %s (Decisive: IRIG=%d STD=%d over %d record(s))",
                outcome.format.name,
                outcome.irig_score,
                outcome.std_score,
                outcome.records_probed,
            )
        elif outcome.confidence == DetectionConfidence.MARGINAL:
            logger.info(
                "Auto-detected timestamp format: %s "
                "(Marginal: IRIG=%d STD=%d over %d "
                "record(s)) -- pass --input-time-format to force "
                "the choice if this is wrong",
                outcome.format.name,
                outcome.irig_score,
                outcome.std_score,
                outcome.records_probed,
            )
        else:
            # L2-DEC-016 ambiguous case. Strict mode
            # rejects; lenient logs WARN and uses the
            # chosen format anyway (back-compat for
            # borderline files that decoded acceptably
            # under the old single-record detector).
            if self._strict:
                logger.error(
                    "Timestamp-format auto-detection is "
                    "ambiguous in %s starting at offset "
                    "0x%X: IRIG=%d STD=%d over %d record(s) -- "
                    "strict mode rejects ambiguous files; "
                    "pass --input-time-format to force the choice",
                    self._path.name,
                    start_offset,
                    outcome.irig_score,
                    outcome.std_score,
                    outcome.records_probed,
                )
                raise MieTimestampFormatMismatchError(
                    start_offset,
                    outcome.irig_score,
                    outcome.std_score,
                    outcome.records_probed,
                )
            logger.warning(
                "Auto-detected timestamp format: %s "
                "(Ambiguous: IRIG=%d STD=%d over %d "
                "record(s)) -- using best guess; pass "
                "--input-time-format to force the choice or "
                "--strict to reject ambiguous files",
                outcome.format.name,
                outcome.irig_score,
                outcome.std_score,
                outcome.records_probed,
            )
        return outcome.format

    def _sanity_check_forced_format(
        self,
        mm: mmap.mmap,
        start_offset: int,
        resolved_format: TimestampFormat,
    ) -> None:
        """L2-DEC-013: the format was forced via --input-time-format /
        decode.input_time_format. Sanity-check it against the same detection probe:
        if the probe is *Decisive* about the OTHER format, the forced
        selection is obviously wrong (e.g. --input-time-format standard on an IRIG
        file), which would otherwise emit garbage timestamps for the whole
        file. Marginal/Ambiguous probes are NOT flagged — those are exactly
        the cases where forcing is the legitimate override. resolved_format
        stays the forced format.

        Raises:
            MieTimestampFormatMismatchError: only when the probe is *Decisive*
                about the OTHER format. Marginal and Ambiguous probes pass,
                because those are exactly the cases forcing exists for.
        """
        outcome = probe_timestamp_format(
            mm,
            start_offset,
            self._detect_records,
        )
        if outcome.confidence == DetectionConfidence.DECISIVE and outcome.format != resolved_format:
            if self._strict:
                logger.error(
                    "Forced timestamp format %s contradicts the "
                    "recording in %s at offset 0x%X: detection is "
                    "decisive for %s (IRIG=%d STD=%d over %d "
                    "record(s)) -- strict mode rejects the mismatch; "
                    "drop --input-time-format to auto-detect",
                    resolved_format.name,
                    self._path.name,
                    start_offset,
                    outcome.format.name,
                    outcome.irig_score,
                    outcome.std_score,
                    outcome.records_probed,
                )
                raise MieTimestampFormatMismatchError(
                    start_offset,
                    outcome.irig_score,
                    outcome.std_score,
                    outcome.records_probed,
                )
            logger.warning(
                "Forced timestamp format %s contradicts the "
                "recording at offset 0x%X: detection is decisive "
                "for %s (IRIG=%d STD=%d over %d record(s)) -- "
                "decoding with the forced format anyway; drop "
                "--input-time-format to auto-detect or pass --strict to "
                "reject the mismatch",
                resolved_format.name,
                start_offset,
                outcome.format.name,
                outcome.irig_score,
                outcome.std_score,
                outcome.records_probed,
            )

    # ── Per-record decode: one loop iteration ───────────────────────────────

    def _decode_one(
        self,
        mm: mmap.mmap,
        offset: int,
        file_len: int,
        fmt: TimestampFormat,
        state: _DecodeState,
    ) -> _Step:
        """Decode the record at ``offset`` and describe what the loop does next.

        Reads and validates the Type Word, recovers from sync loss, then
        dispatches to the SPURIOUS / error / normal builder. Returns a
        :class:`_Step`; the ``yield`` itself stays in ``__iter__``.

        Returns:
            A :class:`_Step` saying what to emit and where to go next: a
            message plus the following offset, a recovery hop with no message,
            or a stop.
        """
        # ── Read and validate Type Word ────────────
        type_raw = read_u16(mm, offset)

        # L2-RDR-021: a null Type Word (0x0000) at a record boundary is the
        # recorder's end-of-records terminator. Stop cleanly — a normal end of
        # stream, not a sync loss. (When the terminator sits within
        # MIN_RECORD_BYTES of EOF the loop guard already ended iteration; this
        # covers a terminator followed by trailing padding.) The last real
        # record was already confirmed by the L2-SYN-028 look-ahead and emitted
        # before we reach here.
        if is_terminator_type_word(type_raw):
            logger.debug(
                "end-of-records terminator at 0x%X; decode complete",
                offset,
            )
            return _Step(message=None, next_offset=offset, stop=True)

        tw = decode_type_word(type_raw)
        ts_words = TIMESTAMP_WORD_COUNTS[fmt]
        record_bytes = tw.word_count * 2

        # ── Validate current record ────────────────
        # Depth 1 — no look-ahead — is deliberate here, and is the one place it
        # differs from `find_first_record` / `recover_sync`, which keep the
        # configured `lookahead_records` (L2-SYN-026). Those two answer "is this
        # the start of a record stream?", where a wrong answer costs nothing but
        # a resumption point. This call site walks a chain that is *already*
        # locked on, and rejecting here discards the record.
        #
        # With look-ahead, a well-formed record sitting immediately before a
        # corrupt region was dropped because its *successor* failed — one valid
        # record lost per corruption site at the default depth of 2, and N-1 at
        # depth N. A record that passes checks 1-5 is complete and in-bounds;
        # whether the *next* boundary is corrupt is the next iteration's problem,
        # and sync recovery already handles it (L2-SYN-005). Mirrors the same
        # call site in `rust/src/reader.rs`.
        validation_failure = validate_record_detailed(
            mm,
            offset,
            file_len,
            ts_format=fmt,
            lookahead_records=1,
        )
        if validation_failure is not None:
            return self._handle_validation_failure(
                mm, offset, file_len, fmt, tw, type_raw, record_bytes, validation_failure, state
            )

        # ── Decode timestamp ───────────────────────
        timestamp = self._decode_record_timestamp(mm, offset, fmt, state)
        cmd_byte_offset = offset + 2 + ts_words * 2

        # ── SPURIOUS_DATA: no Command Word ────────
        if tw.message_type == MessageType.SPURIOUS_DATA:
            return self._build_spurious_step(
                mm, offset, tw, timestamp, cmd_byte_offset, ts_words, record_bytes, state
            )

        # ── Decode Command Word ────────────────────
        cmd = decode_command_word(read_u16(mm, cmd_byte_offset))

        # ── Errored record (bit 14 set) ───────────
        if tw.error:
            return self._build_error_step(
                mm, offset, tw, timestamp, cmd, cmd_byte_offset, ts_words, record_bytes, state
            )

        # ── Normal record ──────────────────────────
        return self._build_normal_step(
            mm, offset, tw, timestamp, cmd, cmd_byte_offset, ts_words, record_bytes, state
        )

    def _handle_validation_failure(
        self,
        mm: mmap.mmap,
        offset: int,
        file_len: int,
        fmt: TimestampFormat,
        tw: TypeWord,
        type_raw: int,
        record_bytes: int,
        validation_failure: ValidationFailure,
        state: _DecodeState,
    ) -> _Step:
        """Sync loss: raise in strict mode, else attempt recovery.

        Returns a recovering :class:`_Step` (no message) on success, a stopping
        step on lenient-mode EOF truncation, or raises on strict/corruption.

        Returns:
            A recovering :class:`_Step` (no message) pointing at the resynced
            offset, or a stopping step on lenient-mode EOF truncation.

        Raises:
            MieInvalidTypeWordError: strict mode, Type Word fields impossible.
            MieRecordTruncatedError: strict mode, record runs past EOF.
            MieUnknownTypeWordError: strict mode, unrecognised message type.
            MiePayloadError: strict mode, any other validation failure.
            MieUnrecoverableSyncLossError: recovery scanned its budget without
                finding a valid record. This one fires in lenient mode too.
        """
        # ── Sync loss — attempt recovery ───────
        self._sync_losses += 1
        _log_validation_context(mm, offset)

        if self._strict:
            if validation_failure == ValidationFailure.INVALID_WORD_COUNT:
                raise MieInvalidTypeWordError(offset, type_raw, tw.word_count)
            if validation_failure == ValidationFailure.RECORD_TRUNCATED:
                raise MieRecordTruncatedError(offset, record_bytes, file_len - offset)
            if validation_failure == ValidationFailure.UNKNOWN_MESSAGE_TYPE:
                raise MieUnknownTypeWordError(offset, type_raw, tw.message_type)
            raise MiePayloadError(
                offset,
                f"{validation_failure} (raw_type=0x{type_raw:04X})",
            )

        # L2-SYN-013: sync loss at WARNING, successful recovery at INFO. Both
        # are emitted here (not inside `recover_sync`) so the sync helpers stay
        # pure, and carry the same detail as the Rust reader's equivalents.
        logger.warning(
            "Sync lost at 0x%X (type=0x%02X wc=%d); scanning forward",
            offset,
            tw.message_type,
            tw.word_count,
        )
        recovered = recover_sync(
            mm,
            offset,
            file_len,
            ts_format=fmt,
            lookahead_records=self._lookahead_records,
        )
        if recovered is not None:
            logger.info(
                "Sync recovered at 0x%X (skipped %d bytes from 0x%X)",
                recovered,
                recovered - offset,
                offset,
            )
        if recovered is None:
            # Distinguish truncation (file ended before the 64 KB scan window
            # exhausted) from genuine mid-file corruption.
            #
            # - Truncation → L1-DEC-005 / L2-RDR-002: lenient mode stops
            #   cleanly.
            # - Corruption → L1-EXIT-004: raise so the CLI maps to exit 3 (or
            #   `.partial` + exit 0 with --allow-partial).
            from aero1553.sync import MAX_SCAN_BYTES

            bytes_remaining = file_len - offset
            if bytes_remaining < MAX_SCAN_BYTES:
                logger.info(
                    "Lenient mode: scan exhausted at EOF "
                    "(offset 0x%X, %d bytes remain < %d "
                    "scan window); treating as truncation",
                    offset,
                    bytes_remaining,
                    MAX_SCAN_BYTES,
                )
                return _Step(message=None, next_offset=offset, stop=True)
            logger.error(
                "Unrecoverable sync loss at 0x%X after %d messages",
                offset,
                state.msg_count,
            )
            raise MieUnrecoverableSyncLossError(offset, self._sync_losses)

        state.prev_was_error = False
        return _Step(message=None, next_offset=recovered, stop=False)

    def _decode_record_timestamp(
        self,
        mm: mmap.mmap,
        offset: int,
        fmt: TimestampFormat,
        state: _DecodeState,
    ) -> Timestamp:
        """Decode the record timestamp and emit the freerun / IRIG day-of-year
        advisories (the latter one-time per decode).

        Returns:
            The decoded :class:`IrigTimestamp` or :class:`StandardTimestamp`,
            per the format resolved for the file.
        """
        if fmt == TimestampFormat.IRIG:
            # decode_irig_timestamp always returns a concrete IrigTimestamp, so
            # the freerun / day-of-year fields are read directly (no isinstance
            # guard — it would be gratuitously always-true here).
            irig = decode_irig_timestamp(
                read_u16(mm, offset + 2),
                read_u16(mm, offset + 4),
                read_u16(mm, offset + 6),
            )
            # L2-WRT-026 clause 4: under a calendar rendering, a backward day
            # step means the recording crosses New Year while the whole decode
            # uses one configured year, so every row after the wrap is dated a
            # year early. Warn once and carry on -- the decoder deliberately
            # does NOT auto-increment, because sync-loss corruption produces
            # this same signal, and advancing the year on corrupt data would be
            # a worse error than the one it fixed.
            if self._calendar_year is not None and not irig.freerun:
                if (
                    state.last_irig_day is not None
                    and irig.day < state.last_irig_day
                    and not state.warned_day_rollover
                ):
                    state.warned_day_rollover = True
                    logger.warning(
                        "Day-of-year stepped back from %d to %d at 0x%X: this "
                        "recording appears to cross a year boundary, but every row "
                        "is dated %d. Rows after the wrap are a year early. Split "
                        "the recording at the boundary and decode each part with "
                        "its own --year",
                        state.last_irig_day,
                        irig.day,
                        offset,
                        self._calendar_year,
                    )
                state.last_irig_day = irig.day

            if irig.freerun:
                logger.warning("Freerun timestamp at 0x%X", offset)
            elif not state.warned_irig_day and irig_day_advisory():
                # PRA-9: the IRIG day-of-year field has a known
                # firmware-dependent decode discrepancy on some
                # DDC cards; time-of-day fields are unaffected.
                # Emit a one-time advisory (not a decode failure).
                #
                # INFO, not WARNING (L2-LOG-001). It is a standing
                # disclaimer about card firmware, not an observation
                # about this recording -- nothing here compares the
                # decoded day against anything, so it fires on every
                # calendar-locked IRIG file from every card. At WARNING
                # it sat in the default output of every run and crowded
                # out the warnings that ARE observations (sync
                # recovery, non-monotonic DELTA, a hit sort cap).
                #
                # L2-LOG-002: the same disclaimer, at a level that follows what
                # the operator asked for. Under `doy` a skewed day is one field
                # a human reads as "day 192" and can re-examine. Under a
                # calendar rendering it is resolved into 2026-07-11 --
                # something that looks like a fact, gets pasted into reports,
                # and is correlated against systems with no way to know it is
                # suspect. Choosing a calendar rendering IS the operator saying
                # this field is load-bearing, which is when a standing caveat
                # about it earns the default level's attention.
                state.warned_irig_day = True
                if self._calendar_year is not None:
                    logger.warning(
                        "IRIG day-of-year is being resolved into calendar dates, "
                        "and that field has a known firmware-dependent discrepancy "
                        "on some DDC cards (hour/minute/second/microsecond are "
                        "unaffected). Verify your card model against vendor CSV "
                        "before relying on these dates -- see "
                        "docs/VENDOR-CSV-DIFFS.md section 5"
                    )
                else:
                    logger.info(
                        "IRIG day-of-year decoded for this recording; the "
                        "day-of-year field has a known firmware-dependent "
                        "discrepancy on some DDC cards (hour/minute/second/"
                        "microsecond are unaffected) -- see "
                        "docs/VENDOR-CSV-DIFFS.md section 5"
                    )
            return irig
        return decode_standard_timestamp(
            read_u16(mm, offset + 2),
            read_u16(mm, offset + 4),
        )

    def _build_spurious_step(
        self,
        mm: mmap.mmap,
        offset: int,
        tw: TypeWord,
        timestamp: Timestamp,
        cmd_byte_offset: int,
        ts_words: int,
        record_bytes: int,
        state: _DecodeState,
    ) -> _Step:
        """Build a SPURIOUS_DATA message (no Command Word). Classified as a
        continuation of a preceding error or a standalone frame.

        Returns:
            A :class:`_Step` carrying the SPURIOUS_DATA message, with
            ``error_word`` set to ``0x2000`` when it continues a preceding
            errored record and ``0x2001`` when it stands alone.
        """
        raw_word_count = tw.word_count - 1 - ts_words
        data_words: tuple[int, ...] = ()
        if raw_word_count > 0:
            data_words = read_u16_array(mm, cmd_byte_offset, raw_word_count)

        error_code = (
            ERROR_SPURIOUS_CONTINUATION if state.prev_was_error else ERROR_SPURIOUS_STANDALONE
        )

        logger.debug(
            "SPURIOUS_DATA at 0x%X: %d raw words, %s",
            offset,
            raw_word_count,
            "continuation" if state.prev_was_error else "standalone",
        )

        # SPURIOUS_DATA has no RT/MSG key. DELTA is None
        # so the CSV writer emits an empty cell.
        msg = MieMessage(
            timestamp=timestamp,
            type_word=tw,
            message_format=MessageFormat.SPURIOUS_DATA,
            command_word=None,
            command_word_2=None,
            status_word=None,
            status_word_2=None,
            data_words=data_words,
            error_word=error_code,
            delta=None,
            file_offset=offset,
            mux=self._mux,
        )
        state.prev_was_error = False
        return _Step(message=msg, next_offset=offset + record_bytes, stop=False)

    def _build_error_step(
        self,
        mm: mmap.mmap,
        offset: int,
        tw: TypeWord,
        timestamp: Timestamp,
        cmd: CommandWord,
        cmd_byte_offset: int,
        ts_words: int,
        record_bytes: int,
        state: _DecodeState,
    ) -> _Step:
        """Build an errored record (Type Word bit 14 set), with DELTA tracking.

        Returns:
            A :class:`_Step` carrying the errored message and the next offset.
        """
        msg = _decode_error_record(
            mm,
            offset,
            tw,
            timestamp,
            cmd,
            cmd_byte_offset,
            ts_words,
            self._strict,
        )
        # Error records participate in DELTA tracking under
        # the shared contract: the diagnostic value of
        # knowing inter-arrival gaps to a flaky RT/MSG is
        # higher than the cost of including anomaly rows.
        delta = _narrate_delta(state.delta_tracker, cmd, timestamp, offset)
        msg = MieMessage(
            timestamp=msg.timestamp,
            type_word=msg.type_word,
            message_format=msg.message_format,
            command_word=msg.command_word,
            command_word_2=msg.command_word_2,
            status_word=msg.status_word,
            status_word_2=msg.status_word_2,
            data_words=msg.data_words,
            error_word=msg.error_word,
            delta=delta,
            file_offset=msg.file_offset,
            mux=self._mux,
        )
        state.prev_was_error = True
        return _Step(message=msg, next_offset=offset + record_bytes, stop=False)

    def _build_normal_step(
        self,
        mm: mmap.mmap,
        offset: int,
        tw: TypeWord,
        timestamp: Timestamp,
        cmd: CommandWord,
        cmd_byte_offset: int,
        ts_words: int,
        record_bytes: int,
        state: _DecodeState,
    ) -> _Step:
        """Build a normal (non-error, non-spurious) record.

        Applies the L2-SYN structural invariants (strict aborts, lenient skips)
        around the payload extraction, then emits anomaly warnings and DELTA.

        Returns:
            A :class:`_Step` carrying the decoded message, or a skipping step
            with no message when lenient mode drops a record that violated a
            structural invariant.

        Raises:
            MiePayloadError: in strict mode, if a L2-SYN structural invariant
                fails either before or after payload extraction.
        """
        skip = _Step(message=None, next_offset=offset + record_bytes, stop=False)
        try:
            msg_fmt = classify_message_format(tw.message_type, cmd, tw.word_count, ts_words)
        except ValueError as exc:
            logger.warning(
                "Cannot classify at 0x%X: %s -- skipping",
                offset,
                exc,
            )
            state.prev_was_error = False
            return skip

        # L2-SYN-020..025: structural invariants. Strict mode
        # aborts via MiePayloadError; lenient mode WARNs and
        # skips the record.
        inv = validate_structural_invariants(
            tw,
            cmd,
            msg_fmt,
            ts_words,
        )
        if inv is not None:
            if self._strict:
                raise MiePayloadError(
                    offset,
                    f"L2-SYN structural invariant violation: {inv.detail}",
                )
            logger.warning(
                "L2-SYN structural invariant violation at 0x%X: %s; skipping record",
                offset,
                inv.detail,
            )
            state.prev_was_error = False
            return skip

        logger.debug(
            "Record at 0x%X: type=0x%02X fmt=%s RT%d SA%d %s",
            offset,
            tw.message_type,
            msg_fmt.name,
            cmd.rt,
            cmd.subaddress,
            cmd.direction.name,
        )

        # Bound payload reads to this record's byte range so a
        # Command Word that *claims* a larger data_word_count than
        # the record can hold cannot read into the following record
        # or past EOF. The Type Word's word_count defines the record
        # length (already validated to fit the file); over-claims
        # yield empty/partial data instead of raising. Mirrors the
        # Rust reader's record-bounded extract_payload.
        record_end = offset + record_bytes
        payload_byte_offset = cmd_byte_offset + 2
        cmd2, status, status2, data_words = _extract_payload(
            mm,
            payload_byte_offset,
            record_end,
            msg_fmt,
            cmd,
        )

        # L2-SYN-023 / L2-SYN-027: post-extract Reject-class checks.
        # Same strict/lenient policy as the pre-extract checks.
        post_inv = validate_post_extract_invariants(msg_fmt, cmd, cmd2)
        if post_inv is not None:
            if self._strict:
                raise MiePayloadError(
                    offset,
                    f"L2-SYN structural invariant violation: {post_inv.detail}",
                )
            logger.warning(
                "L2-SYN structural invariant violation at 0x%X: %s; skipping record",
                offset,
                post_inv.detail,
            )
            state.prev_was_error = False
            return skip

        # L2-SYN-024 / L2-SYN-025: AnomalyWarn-class.
        # Both modes log a WARN and continue emitting.
        for anomaly in detect_record_anomalies(tw, cmd, status):
            logger.warning(
                "L2-SYN anomaly at 0x%X: %s",
                offset,
                anomaly.detail,
            )

        delta = _narrate_delta(state.delta_tracker, cmd, timestamp, offset)

        msg = MieMessage(
            timestamp=timestamp,
            type_word=tw,
            message_format=msg_fmt,
            command_word=cmd,
            command_word_2=cmd2,
            status_word=status,
            status_word_2=status2,
            data_words=data_words,
            error_word=None,
            delta=delta,
            file_offset=offset,
            mux=self._mux,
        )
        state.prev_was_error = False
        return _Step(message=msg, next_offset=offset + record_bytes, stop=False)


def _decode_error_record(
    mm: mmap.mmap,
    offset: int,
    tw: TypeWord,
    timestamp: Timestamp,
    cmd: CommandWord,
    cmd_byte_offset: int,
    ts_words: int,
    strict: bool,
) -> MieMessage:
    """Decode a record with the error flag (bit 14) set.

    Error Word is the last word of the record. Payload between
    Command Word and Error Word = truncated data words.

    Returns:
        The decoded message, with ``error_word`` carrying the DDC code.

    Raises:
        MieUnknownErrorCodeError: in strict mode, if the Error Word is not a
            known DDC code. Lenient mode keeps the record and logs a WARN.
    """
    error_word_offset = offset + (tw.word_count - 1) * 2
    error_code = read_u16(mm, error_word_offset)

    if error_code not in KNOWN_DDC_ERROR_CODES:
        if strict:
            raise MieUnknownErrorCodeError(offset, error_code)
        logger.warning(
            "Unknown DDC error code 0x%04X at offset 0x%X",
            error_code,
            offset,
        )

    payload_words = tw.word_count - 1 - ts_words - 1 - 1
    data_words: tuple[int, ...] = ()
    if payload_words > 0:
        data_words = read_u16_array(mm, cmd_byte_offset + 2, payload_words)

    try:
        msg_fmt = classify_message_format(tw.message_type, cmd, tw.word_count, ts_words)
    except ValueError:
        msg_fmt = MessageFormat.RECEIVE

    from aero1553.models import DDC_ERROR_DESCRIPTIONS

    desc = DDC_ERROR_DESCRIPTIONS.get(error_code, "Unknown")
    logger.info(
        "Error record at 0x%X: RT%d SA%d %s, code=0x%04X (%s), %d payload words",
        offset,
        cmd.rt,
        cmd.subaddress,
        cmd.direction.name,
        error_code,
        desc,
        payload_words,
    )

    return MieMessage(
        timestamp=timestamp,
        type_word=tw,
        message_format=msg_fmt,
        command_word=cmd,
        command_word_2=None,
        status_word=None,
        status_word_2=None,
        data_words=data_words,
        error_word=error_code,
        delta=None,
        file_offset=offset,
    )


def _narrate_delta(
    tracker: DeltaTracker,
    command_word: CommandWord,
    timestamp: Timestamp,
    offset: int,
) -> float | None:
    """Return this record's DELTA, emitting the WARN that sometimes goes with it.

    The arithmetic and the per-key state belong to
    :class:`~aero1553.delta.DeltaTracker`; what is left here is narration,
    which is the reader's job and nobody else's. The tracker deliberately does
    not log: it cannot know that a backward step is worth a line in a
    single-file decode and merely noise in a merge that already reports
    unsorted inputs at file granularity (L2-MRG-006).

    Args:
        tracker: The per-file DELTA state.
        command_word: The record's Command Word.
        timestamp: The record's timestamp.
        offset: Byte offset, for the diagnostic.

    Returns:
        Seconds since the previous record with the same RT/MSG key, or ``None``
        where no honest gap exists.
    """
    outcome = tracker.observe(command_word, timestamp)
    if outcome.kind is DeltaKind.BACKWARD and outcome.first_for_key:
        logger.warning(
            "Non-monotonic timestamp at 0x%X for RT/MSG key 0x%08X: "
            "prev_us=%d curr_us=%d (further out-of-order occurrences "
            "for this key suppressed)",
            offset,
            outcome.key,
            outcome.prev_us,
            outcome.curr_us,
        )
    return outcome.value


# Payload-extraction result: (command_word_2, status_word, status_word_2, data_words).
_PayloadResult = tuple[CommandWord | None, int | None, int | None, tuple[int, ...]]
# Record-bounded readers passed into the per-family extractors: ``_r16`` returns
# ``None`` and ``_read_n`` returns ``()`` on an out-of-bounds read.
_R16 = Callable[[int], "int | None"]
_ReadN = Callable[[int, int], "tuple[int, ...]"]

# RT-to-RT formats take their data-word count from Cmd2 (read from the payload),
# not Cmd1. The mode-code formats carry at most a single data word.
_RT_TO_RT_FORMATS = frozenset({MessageFormat.RT_TO_RT, MessageFormat.RT_TO_RT_BROADCAST})
_MODE_CODE_FORMATS = frozenset(
    {
        MessageFormat.MODE_CODE_TX_DATA,
        MessageFormat.MODE_CODE_RX_DATA,
        MessageFormat.MODE_CODE_NO_DATA,
        MessageFormat.MODE_CODE_BCAST_NO_DATA,
        MessageFormat.MODE_CODE_BCAST_DATA,
    }
)


def _extract_payload(
    mm: mmap.mmap,
    p: int,
    record_end: int,
    fmt: MessageFormat,
    cmd: CommandWord,
) -> _PayloadResult:
    """Extract command words, status words, and data words per format.

    All reads are bounded to ``record_end`` (the byte just past this
    record, ``offset + word_count * 2``, already validated to fit the
    file). RT-to-RT formats take their data-word count from Cmd2, which
    the L2-SYN-022 capacity check — computed from Cmd1 — does not bound;
    a Cmd2 that over-claims must yield empty/partial data rather than
    reading into the following record or raising ``struct.error``. The
    local ``_r16``/``_read_n`` helpers return ``None``/``()`` on an
    out-of-bounds read, mirroring the Rust reader's record-bounded
    ``extract_payload`` (``read_u16`` → ``Option``, ``read_n`` → empty).

    Dispatch is split by family (RT-to-RT, mode-code, direct) so each
    per-format block stays flat and independently testable.

    Returns:
        ``(command_word_2, status_word, status_word_2, data_words)`` for the
        format. Any field the format does not carry is ``None``, and an
        out-of-bounds read yields ``None`` or an empty tuple rather than
        raising.
    """

    def _r16(off: int) -> int | None:
        if off + 2 > record_end:
            return None
        return read_u16(mm, off)

    def _read_n(start: int, n: int) -> tuple[int, ...]:
        if start + n * 2 > record_end:
            return ()
        return read_u16_array(mm, start, n)

    if fmt in _RT_TO_RT_FORMATS:
        return _extract_rt_to_rt(p, fmt, _r16, _read_n)
    if fmt in _MODE_CODE_FORMATS:
        return _extract_mode_code(p, fmt, _r16)
    return _extract_direct(p, cmd, fmt, _r16, _read_n)


def _extract_direct(
    p: int,
    cmd: CommandWord,
    fmt: MessageFormat,
    r16: _R16,
    read_n: _ReadN,
) -> _PayloadResult:
    """RT-addressed BC↔RT transfers whose data-word count comes from Cmd1.

    Returns:
        ``(None, status_word, None, data_words)`` -- these formats carry no
        second Command Word and no second Status Word.

    Raises:
        ValueError: if ``fmt`` is not one of the direct formats. Unreachable
            via the dispatcher, which routes by family.
    """
    if fmt == MessageFormat.RECEIVE:
        n = cmd.data_word_count
        data_words = read_n(p, n)
        status = r16(p + n * 2)
        return None, status, None, data_words

    if fmt == MessageFormat.TRANSMIT:
        status = r16(p)
        n = cmd.data_word_count
        data_words = read_n(p + 2, n)
        return None, status, None, data_words

    if fmt == MessageFormat.RECEIVE_BROADCAST:
        n = cmd.data_word_count
        data_words = read_n(p, n)
        return None, None, None, data_words

    raise ValueError(f"Unhandled message format: {fmt}")


def _extract_rt_to_rt(
    p: int,
    fmt: MessageFormat,
    r16: _R16,
    read_n: _ReadN,
) -> _PayloadResult:
    """RT-to-RT transfers: a second Command Word supplies the data-word count.

    The broadcast variant has no receiving-RT Status Word (and so does not
    read one past the data words).

    Returns:
        ``(cmd2, tx_status, rx_status, data_words)``. ``rx_status`` is ``None``
        for the broadcast variant, which has no receiving-RT Status Word.
    """
    cmd2 = decode_command_word(r16(p) or 0)
    tx_status = r16(p + 2)
    n = cmd2.data_word_count
    data_words = read_n(p + 4, n)
    if fmt == MessageFormat.RT_TO_RT_BROADCAST:
        return cmd2, tx_status, None, data_words
    rx_status = r16(p + 4 + n * 2)
    return cmd2, tx_status, rx_status, data_words


def _extract_mode_code(
    p: int,
    fmt: MessageFormat,
    r16: _R16,
) -> _PayloadResult:
    """Mode-code commands: a Status Word and at most a single data word.

    Returns:
        ``(None, status_word, None, data_words)`` with at most one data word.
        Broadcast mode codes carry no Status Word, so it is ``None`` there.
    """
    if fmt == MessageFormat.MODE_CODE_TX_DATA:
        status = r16(p)
        w = r16(p + 2)
        return None, status, None, () if w is None else (w,)

    if fmt == MessageFormat.MODE_CODE_RX_DATA:
        w = r16(p)
        status = r16(p + 2)
        return None, status, None, () if w is None else (w,)

    if fmt == MessageFormat.MODE_CODE_NO_DATA:
        status = r16(p)
        return None, status, None, ()

    if fmt == MessageFormat.MODE_CODE_BCAST_NO_DATA:
        return None, None, None, ()

    # MODE_CODE_BCAST_DATA — the only remaining member of _MODE_CODE_FORMATS.
    w = r16(p)
    return None, None, None, () if w is None else (w,)


def _log_validation_context(mm: mmap.mmap, offset: int) -> None:
    """Emit at most 32 bytes around a validation failure at DEBUG."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    ctx_start = max(0, offset - 16)
    ctx_end = min(len(mm), ctx_start + 32)
    hex_part = " ".join(f"{byte:02X}" for byte in mm[ctx_start:ctx_end])
    logger.debug(
        "validation context at 0x%X (bytes 0x%X..0x%X, max 32): %s",
        offset,
        ctx_start,
        ctx_end,
        hex_part,
    )
