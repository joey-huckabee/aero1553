"""Hex dump utility for MIE binary recording files.

Provides a command-line hex dump with MIE record boundary awareness.
Records are visually separated and annotated with decoded Type Word
fields, making it easy to identify record boundaries, message types,
and byte-level content.

Usage via CLI::

    aero1553 dump recording.mie
    aero1553 dump recording.mie --offset 0x48 --length 256
    aero1553 dump recording.mie --records 10
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TextIO

from aero1553.decode import (
    MIN_RECORD_BYTES,
    MIN_RECORD_WORDS,
    classify_message_format,
    decode_command_word,
    decode_irig_timestamp,
    decode_type_word,
    read_u16,
)
from aero1553.exceptions import MieFileEmptyError, MieFileIoError, MieFileNotFoundError
from aero1553.models import DDC_ERROR_DESCRIPTIONS, MessageType, TypeWord

logger = logging.getLogger(__name__)


#: Map of known type codes to human-readable names.
_TYPE_NAMES: dict[int, str] = {
    MessageType.MODE_COMMAND: "Mode Command",
    MessageType.BC_TO_RT: "BC->RT (Receive)",
    MessageType.RT_TO_BC: "RT->BC (Transmit)",
    MessageType.RT_TO_RT: "RT->RT",
    MessageType.BROADCAST_BC_TO_RT: "Broadcast BC->RT",
    MessageType.BROADCAST_RT_TO_RT: "Broadcast RT->RT",
    MessageType.SPURIOUS_DATA: "Spurious Data",
}


def hex_dump_raw(
    path: str | Path,
    start_offset: int = 0,
    length: int | None = None,
    stream: TextIO | None = None,
) -> None:
    """Print a raw hex dump of a binary file.

    Args:
        path: Path to the binary file.
        start_offset: Byte offset to begin the dump.
        length: Number of bytes to dump. None means dump to end of file.
        stream: Output stream. Defaults to sys.stdout.

    Raises:
        MieFileNotFoundError: if ``path`` does not exist.
        MieFileEmptyError: if the file exists but holds no bytes.
        MieFileIoError: if the file exists but cannot be read.
    """
    fpath = Path(path)
    if not fpath.exists():
        raise MieFileNotFoundError(str(fpath))
    # Read before the emptiness check, and convert the failure, so an
    # unopenable path reports I/O rather than "empty" — matching `dump_hex` /
    # `dump_records` in `rust/src/dump.rs`.
    try:
        data = fpath.read_bytes()
    except OSError as exc:
        raise MieFileIoError(str(fpath), exc) from exc
    if len(data) == 0:
        raise MieFileEmptyError(str(fpath))

    out = stream if stream is not None else sys.stdout

    # Both bounds are clamped to the file, and the start is clamped FIRST. An
    # offset past the end previously printed a range running backwards --
    # `Range: 0x000186A0-0x000000B2` -- because only the end was bounded. The
    # bytes shown were right either way (the slice is empty), but the header
    # said something impossible, and it disagreed with the Rust and C++
    # reports, which clamp both.
    start = min(start_offset, len(data))
    end = len(data) if length is None else min(start_offset + length, len(data))
    end = max(end, start)
    chunk = data[start:end]

    print(f"File: {fpath.name} ({len(data)} bytes)", file=out)
    print(f"Range: 0x{start:08X}-0x{end:08X}\n", file=out)

    for i in range(0, len(chunk), 16):
        addr = start + i
        hex_part = " ".join(f"{b:02X}" for b in chunk[i : i + 16])
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk[i : i + 16])
        print(
            f"  {addr:08X}  {hex_part:<48s}  |{ascii_part}|",
            file=out,
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
    command word summary.

    Args:
        path: Path to the MIE binary file.
        max_records: Maximum number of records to dump. None for all.
        start_offset: Byte offset to begin scanning for records.
        stream: Output stream. Defaults to sys.stdout.

    Raises:
        MieFileNotFoundError: if ``path`` does not exist.
        MieFileEmptyError: if the file exists but holds no bytes.
        MieFileIoError: if the file exists but cannot be read.
    """
    fpath = Path(path)
    if not fpath.exists():
        raise MieFileNotFoundError(str(fpath))
    # Read before the emptiness check, and convert the failure, so an
    # unopenable path reports I/O rather than "empty" — matching `dump_hex` /
    # `dump_records` in `rust/src/dump.rs`.
    try:
        data = fpath.read_bytes()
    except OSError as exc:
        raise MieFileIoError(str(fpath), exc) from exc
    if len(data) == 0:
        raise MieFileEmptyError(str(fpath))

    out = stream if stream is not None else sys.stdout
    file_len = len(data)
    offset = start_offset
    record_num = 0

    print(
        f"File: {fpath.name} ({file_len} bytes)",
        file=out,
    )
    print(
        f"Record dump starting at offset 0x{start_offset:08X}\n",
        file=out,
    )

    while offset + MIN_RECORD_BYTES <= file_len:
        if max_records is not None and record_num >= max_records:
            break

        type_raw = read_u16(data, offset)
        tw = decode_type_word(type_raw)

        # Validate the record's extent; a stop reason is written inline and
        # logged (L2-CLI-013) inside the helper, so here we just break.
        record_end = _dump_record_extent(tw, offset, file_len, out)
        if record_end is None:
            break
        record_bytes = record_end - offset

        _write_record_annotation(out, data, tw, offset, record_bytes, record_num)
        _write_record_hex_payload(out, data[offset:record_end], offset)

        print(file=out)
        offset = record_end
        record_num += 1

    print(
        f"{'-' * 72}\n{record_num} records dumped.",
        file=out,
    )


def _dump_record_extent(tw: TypeWord, offset: int, file_len: int, out: TextIO) -> int | None:
    """Validate the record at ``offset`` for the dump scan. Returns ``record_end``
    to proceed, or ``None`` to stop scanning — writing the inline anomaly note to
    ``out`` and logging it (L2-CLI-013) on each stop path.

    Returns:
        The offset one past the validated record, or ``None`` to stop the scan.
        Every ``None`` path has already written its reason to ``out``, so the
        caller just breaks.
    """
    if tw.word_count < MIN_RECORD_WORDS:
        print(
            f"  !! Invalid word_count={tw.word_count} at 0x{offset:08X}, stopping",
            file=out,
        )
        logger.warning(
            "dump: invalid word_count=%d at 0x%X; stopping record scan",
            tw.word_count,
            offset,
        )
        return None

    record_bytes = tw.word_count * 2
    if offset + record_bytes > file_len:
        print(
            f"  !! Truncated record at 0x{offset:08X} "
            f"({record_bytes} bytes needed, {file_len - offset} available)",
            file=out,
        )
        logger.warning(
            "dump: truncated record at 0x%X (%d bytes needed, %d available); stopping record scan",
            offset,
            record_bytes,
            file_len - offset,
        )
        return None

    return offset + record_bytes


def _write_record_annotation(
    out: TextIO, data: bytes, tw: TypeWord, offset: int, record_bytes: int, record_num: int
) -> None:
    """Write the decoded-header annotation block (Type / Time / Cmd) for one record.
    The IRIG timestamp decode is a best-effort summary — for Standard-format files
    the raw bytes below remain authoritative."""
    type_name = _TYPE_NAMES.get(tw.message_type, f"UNKNOWN(0x{tw.message_type:02X})")
    ts_upper = read_u16(data, offset + 2)
    ts_middle = read_u16(data, offset + 4)
    ts_lower = read_u16(data, offset + 6)
    timestamp = decode_irig_timestamp(ts_upper, ts_middle, ts_lower)
    cmd = decode_command_word(read_u16(data, offset + 8))

    # Classified message format. dump runs on suspect files, so guard the
    # classifier — an unclassifiable record degrades to a label, never throws.
    # `3` = IRIG timestamp words (dump decodes the timestamp as IRIG above).
    try:
        fmt_name = classify_message_format(tw.message_type, cmd, tw.word_count, 3).name
    except ValueError:
        fmt_name = "(unclassifiable)"

    lines = [
        "-" * 72,
        f"  Record #{record_num}  @  0x{offset:08X}  ({record_bytes} bytes, {tw.word_count} words)",
        f"  Type:   0x{tw.raw:04X}  ->  {type_name}  Bus {'B' if tw.bus else 'A'}  "
        f"error flag (bit 14): {'SET' if tw.error else 'clear'}",
        f"  Format: {fmt_name}",
        f"  Time:   {timestamp.format()}{'  [FREERUN]' if timestamp.freerun else ''}",
        f"  Cmd:    0x{cmd.raw:04X}  ->  RT{cmd.rt} SA{cmd.subaddress} "
        f"{'T' if cmd.direction else 'R'} WC={cmd.data_word_count}",
    ]
    # For an errored record the Error Word is the last word of the record; show
    # its value and the DDC description so the reason is legible at a glance.
    if tw.error:
        err_code = read_u16(data, offset + (tw.word_count - 1) * 2)
        desc = DDC_ERROR_DESCRIPTIONS.get(err_code, "unknown DDC error code")
        lines.append(f"  Error:  0x{err_code:04X}  ->  {desc}")
    print("\n".join(lines), file=out)


def _write_record_hex_payload(out: TextIO, record_data: bytes, offset: int) -> None:
    """Hex-dump a record's raw bytes, 16 per line, each line offset-annotated."""
    for i in range(0, len(record_data), 16):
        addr = offset + i
        chunk = record_data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"    {addr:08X}  {hex_part:<48s}  |{ascii_part}|", file=out)
