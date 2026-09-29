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

The decoding is the ``aero1553`` Rust crate's, running in-process through the
package's compiled extension; this class is its Python face. Its log lines
reach Python's :mod:`logging` under the ``aero1553.reader`` logger, filtered
by that logger's effective level as it stands when the reader is created and
when each pass over the file starts.

Sync Recovery:
    The reader maintains sync through a validate-then-decode approach.
    At each record boundary it confirms the Type Word is valid, the word
    count is plausible, and the next record's Type Word also looks valid
    (look-ahead). If validation fails it scans forward in 2-byte steps
    until it finds a valid record. If recovery fails (no valid record
    within the scan window), iteration stops.

    Error records (bit 14 set) and their SPURIOUS_DATA continuations
    are valid records that pass sync validation normally. Sync loss
    only occurs when the DDC card writes truly corrupt data (e.g.,
    truncated mid-word, power loss during recording).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aero1553._native import (
    DEFAULT_DETECT_RECORDS,
    DEFAULT_LOOKAHEAD_RECORDS,
    DEFAULT_MUX_DELIMITER,
    DEFAULT_MUX_ENABLED,
    DEFAULT_MUX_FIELD,
    NativeReader,
)
from aero1553.models import TimestampFormat

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aero1553.models import MieMessage


class MieFileReader:
    """Memory-mapped sequential reader for MIE binary files.

    Reads a DDC MIE binary recording file and yields decoded
    :class:`~aero1553.models.MieMessage` instances. Uses a read-only
    memory map for efficient access to large files without loading
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

    Each ``iter(reader)`` is a fresh pass from the first record, and the
    iterator it returns may outlive the reader.

    Args:
        path: Path to the MIE binary file.
        strict: If ``True``, raise exceptions on sync loss, invalid
            records, and unknown error codes instead of recovering.
        input_time_format: Timestamp format. ``AUTO`` (default) detects from
            the first record.
        detect_records: Records probed by timestamp auto-detection (L2-DEC-015);
            values below 1 are treated as 1.
        lookahead_records: Validation look-ahead depth (L2-SYN-026); values
            below 1 are treated as 1.
        standard_tick_rate_hz: Standard-counter tick rate (L2-DEC-017). ``None``
            keeps Standard records out of DELTA tracking.
        mux_enabled: Populate MUX from the file name (L2-WRT-020).
        mux_delimiter: Delimiter the file name is split on for MUX.
        mux_field: Which split field is the MUX value (negative counts from
            the end).
        calendar_year: The calendar year a calendar rendering will use, or
            ``None`` under ``doy`` (L2-WRT-026 clause 4 / L2-LOG-002).

    Raises:
        MieFileNotFoundError: If the file does not exist.
        MieFileEmptyError: If the file is zero bytes.
        MieFileIoError: If the file exists but cannot be opened or mapped.
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
        self._native = NativeReader(
            self._path,
            strict=strict,
            input_time_format=int(input_time_format),
            # Clamped here, as the pure-Python reader clamped them, so a library
            # caller cannot break the >= 1 invariant by passing 0 or less.
            detect_records=max(1, detect_records),
            lookahead_records=max(1, lookahead_records),
            standard_tick_rate_hz=standard_tick_rate_hz,
            mux_enabled=mux_enabled,
            mux_delimiter=mux_delimiter,
            mux_field=mux_field,
            calendar_year=calendar_year,
        )

    @property
    def path(self) -> Path:
        """Path to the source MIE binary file."""
        return self._path

    @property
    def file_size(self) -> int:
        """Size of the source file in bytes."""
        return int(self._native.file_size)

    @property
    def sync_losses(self) -> int:
        """Cumulative sync-recovery count from the most recent
        ``__iter__`` call. Reset to 0 each iteration.

        Used by the CLI's L1-EXIT-005 exit-class summary to distinguish
        Complete (sync_losses == 0) from PartialRecovered (sync_losses
        > 0 with a successful full decode).
        """
        return int(self._native.sync_losses)

    @property
    def empty_recording(self) -> bool:
        """Whether the most recent ``__iter__`` classified the input as a valid
        but empty recording (record stream opens on the end-of-records
        terminator; zero records, but not a wrong-file rejection). Reset each
        ``__iter__`` call. Per L1-EXIT-010 the CLI uses this to emit the
        ``empty-recording`` exit class and write a header-only CSV at exit 0.
        """
        return bool(self._native.empty_recording)

    def __iter__(self) -> Iterator[MieMessage]:
        """Iterate over all decoded messages in file order.

        Returns the compiled iterator itself, so no Python code runs per
        record.

        Returns:
            An iterator of decoded MieMessage instances, one per binary record.

        Raises:
            MieFileIoError: if the file cannot be memory-mapped or read.
        """
        return self._native.records()
