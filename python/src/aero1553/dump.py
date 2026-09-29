"""Hex dump utility for MIE binary recording files.

Provides a command-line hex dump with MIE record boundary awareness.
Records are visually separated and annotated with decoded Type Word
fields, making it easy to identify record boundaries, message types,
and byte-level content.

The dump is ``rust/src/dump.rs``, run through the package's compiled
extension, so a dump from Python is byte-for-byte ``aero1553 dump``.
Anomalies that stop the record scan are logged on the ``aero1553.dump``
logger (L2-CLI-013).

Usage via CLI::

    aero1553 dump recording.mie
    aero1553 dump recording.mie --offset 0x48 --length 256
    aero1553 dump recording.mie --records 10
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from aero1553 import _native


def _count(name: str, value: int) -> int:
    """A caller's offset/length/count as the native dump takes it: a negative
    refused, and anything past the largest file a process can map clamped (it
    means "to the end" either way).

    Returns:
        ``value``, clamped to ``sys.maxsize``.

    Raises:
        ValueError: if ``value`` is negative.
    """
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return min(value, sys.maxsize)


def hex_dump_raw(
    path: str | Path,
    start_offset: int = 0,
    length: int | None = None,
    stream: TextIO | None = None,
) -> None:
    """Print a raw hex dump of a binary file.

    Both bounds are clamped to the file: an offset past the end prints an
    empty range rather than one running backwards.

    Args:
        path: Path to the binary file.
        start_offset: Byte offset to begin the dump.
        length: Number of bytes to dump. None means dump to end of file.
        stream: Output stream. Defaults to sys.stdout.

    Raises:
        MieFileNotFoundError: if ``path`` does not exist.
        MieFileEmptyError: if the file exists but holds no bytes.
        MieFileIoError: if the file exists but cannot be read.
        ValueError: if ``start_offset`` or ``length`` is negative.
    """
    _native.hex_dump_raw(
        Path(path),
        _count("start_offset", start_offset),
        None if length is None else _count("length", length),
        stream if stream is not None else sys.stdout,
    )


def hex_dump_records(
    path: str | Path,
    max_records: int | None = None,
    start_offset: int = 0,
    stream: TextIO | None = None,
) -> None:
    """Print a record-aware hex dump of an MIE binary file.

    Each record is displayed with a header showing the decoded Type Word
    fields (message type, bus, word count, error flag), timestamp, and
    command word summary. A malformed record is not an error: the scan writes
    an inline ``!!`` note, logs it, and stops.

    Args:
        path: Path to the MIE binary file.
        max_records: Maximum number of records to dump. None for all.
        start_offset: Byte offset to begin scanning for records.
        stream: Output stream. Defaults to sys.stdout.

    Raises:
        MieFileNotFoundError: if ``path`` does not exist.
        MieFileEmptyError: if the file exists but holds no bytes.
        MieFileIoError: if the file exists but cannot be read.
        ValueError: if ``max_records`` or ``start_offset`` is negative.
    """
    _native.hex_dump_records(
        Path(path),
        None if max_records is None else _count("max_records", max_records),
        _count("start_offset", start_offset),
        stream if stream is not None else sys.stdout,
    )
