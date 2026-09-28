"""CSV output writer for decoded MIE messages.

Streams rows straight to the output handle through the standard-library
``csv`` module — no DataFrame or full-file buffering, so decode memory is
O(1) in the record count (L3-PY-012). Produces CSV output matching the
column layout used by DDC's recording software, enabling direct
comparison between Aero1553 output and vendor-generated CSV files.

Output Column Definitions:

    TIME_STAMP
        IRIG-format timestamp of the first word of this message on the
        1553 bus, formatted as ``DAY:HH:MM:SS.uuuuuu``. The DAY field
        is the day-of-year (1–366). Hours, minutes, and seconds are
        zero-padded to two digits. Microseconds are zero-padded to six
        digits, giving microsecond-level resolution.

    RT
        Remote Terminal address (0–30). Identifies which RT participated
        in this bus transaction. Address 31 is reserved for broadcast.

    MSG
        Message identifier combining the subaddress and transfer
        direction in the format ``<Subaddress><T|R>``. For example,
        ``11R`` means Subaddress 11, Receive (BC→RT); ``22T`` means
        Subaddress 22, Transmit (RT→BC). Subaddresses 0 and 31 denote
        mode code messages per MIL-STD-1553B.

    WD01 through WD32
        Raw 16-bit data words in uppercase hexadecimal (e.g., ``0400``,
        ``CA22``). Words are in bus wire order. Columns beyond the
        actual data word count for this message are empty strings.
        The maximum is 32 data words per MIL-STD-1553B.

    STAT
        Raw 16-bit MIL-STD-1553 Status Word in uppercase hexadecimal.
        Returned by the RT to indicate message acceptance, busy status,
        subsystem flag, etc. Bits 15–11 echo the RT address.

    CMD
        Raw 16-bit MIL-STD-1553 Command Word in uppercase hexadecimal.
        Sent by the Bus Controller to initiate the transaction. Contains
        the RT address, T/R bit, subaddress, and word count.

    MUX
        Multiplexer label / source identifier. Not decoded from the binary
        record: by default it is derived from a field of the input **file
        name** (L2-WRT-020) so a decoded CSV carries the recorder identity
        encoded in the name. Emitted empty when MUX population is disabled
        (``--no-mux`` / ``[mux] enabled = false``, which restores the
        vendor-exact layout) or when the configured field is absent.

    TERM_NAME
        Terminal or equipment name associated with the RT/SA combination.
        Derived from external configuration; not decoded, so emitted as
        an empty column to preserve the vendor CSV layout (L2-WRT-013).

    BUS
        Redundant bus identifier: ``A`` or ``B``. MIL-STD-1553 defines
        two redundant buses for fault tolerance; this field indicates
        which bus the message was captured on.

    DELTA
        Inter-arrival time in seconds (six decimal places) between this
        message and the most recent prior message sharing the same
        Remote Terminal address (RT) and message identifier (MSG).

        The MSG identifier is the combination of Subaddress and Direction
        (e.g., ``11T`` for Subaddress 11, Transmit; ``22R`` for
        Subaddress 22, Receive). Messages are grouped by the composite
        key ``<RT>:<MSG>`` — for example, all messages to RT 15 SA 11
        Receive are tracked independently from RT 15 SA 11 Transmit,
        and independently from RT 30 SA 11 Receive.

        For the first occurrence of any RT/MSG combination in a
        recording file, DELTA is ``0.000000``.

        This metric directly reveals the Bus Controller's scheduling
        rate for each unique message type. A consistent DELTA of
        approximately 0.016 seconds indicates the BC is polling that
        message at a 60 Hz minor frame rate. A consistent DELTA of
        approximately 0.033 seconds indicates a 30 Hz rate. Jitter or
        drift in DELTA values across a recording can indicate bus
        loading anomalies, missed scheduling cycles, BC priority
        changes, or intermittent RT response failures.

    IM_GAP
        Inter-message gap. Not decoded from the binary record; emitted as
        an empty column to preserve the vendor CSV layout (L2-WRT-013).

    RCV_GAP
        Receive gap. Not decoded from the binary record; emitted as an
        empty column to preserve the vendor CSV layout (L2-WRT-013).

    XMT_GAP
        Transmit gap. Not decoded from the binary record; emitted as an
        empty column to preserve the vendor CSV layout (L2-WRT-013).
"""

from __future__ import annotations

import contextlib
import csv
import errno
import itertools
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from aero1553.exceptions import (
    MieCalendarUnavailableError,
    MieClobberRefusedError,
    MieInputOutputCollisionError,
    MieUnrecoverableSyncLossError,
    MieWriterError,
)
from aero1553.models import (
    DOY_RENDER,
    MAX_DATA_WORDS,
    CalendarUnavailableError,
    MieMessage,
    TimeRender,
)

logger = logging.getLogger(__name__)


# ── Broken-pipe classification (L2-WRT-018) ────────────────────────────

#: ``errno`` values that mean "the downstream consumer closed the pipe" on
#: Windows. POSIX surfaces this as ``EPIPE``, which CPython raises as a
#: ``BrokenPipeError``; Windows does **not** — writing to a pipe whose read end
#: has closed comes out of the text layer as a plain ``OSError`` with ``EINVAL``
#: (22), occasionally ``EPIPE`` (32). A bare ``except BrokenPipeError`` therefore
#: never fires there, which made ``aero1553 decode rec.mie | head`` exit 1
#: with an error on Windows while the Rust CLI exited 0.
_WINDOWS_BROKEN_PIPE_ERRNOS: Final[frozenset[int]] = frozenset({errno.EINVAL, errno.EPIPE})


def is_broken_pipe(exc: BaseException) -> bool:
    """Whether ``exc`` is a downstream-consumer-closed-the-pipe condition.

    The Python analogue of Rust's ``MieError::is_broken_pipe`` (``rust/src/error.rs``),
    which tests ``io::ErrorKind::BrokenPipe`` — a kind the Rust standard library
    already normalizes across platforms. Python has no such normalization, so the
    Windows ``errno`` values are matched explicitly (see
    :data:`_WINDOWS_BROKEN_PIPE_ERRNOS`).

    The extra ``errno`` matching is deliberately scoped to Windows: on POSIX,
    ``EINVAL`` from a write is a genuine failure and must stay one, so widening
    the match there would silently turn real write errors into clean exits.

    Returns:
        ``True`` if the consumer closed the pipe, which callers treat as a
        clean stop rather than a failure.
    """
    if isinstance(exc, BrokenPipeError):
        return True
    if sys.platform == "win32" and isinstance(exc, OSError):
        return exc.errno in _WINDOWS_BROKEN_PIPE_ERRNOS
    return False


# NOTE: no "silence stdout after the pipe breaks" step is performed here.
# The usual recipe for that (`os.dup2(devnull, sys.stdout.fileno())`, from
# CPython's own SIGPIPE note) is written for a process about to call
# `sys.exit`, and is wrong for this module: `cli.main()` is an ordinary
# function that tests and embedders call in-process, and rebinding file
# descriptor 1 underneath them corrupts the caller's own output capture.
# The observable contract of L2-WRT-018 — exit 0, and no error text from us —
# holds without it.


# ── Path identity check (L2-WRT-014) ───────────────────────────────────


def paths_refer_to_same_file(input_path: Path, output_path: Path) -> bool:
    """Test whether ``input_path`` and ``output_path`` resolve to the same file.

    Handles the common case where ``output_path`` does not yet exist by
    resolving the parent directory and comparing against the prospective
    full path. Symlink-safe via ``Path.resolve``.

    Returns:
        ``True`` if both paths name the same file. ``False`` when they differ,
        and also when either path cannot be resolved -- a destination that
        cannot be resolved cannot collide.
    """
    try:
        input_resolved = Path(input_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    # Direct path: both exist.
    try:
        output_resolved = Path(output_path).resolve(strict=True)
        return input_resolved == output_resolved
    except (OSError, RuntimeError):
        pass
    # Output doesn't exist; resolve its parent and join the filename.
    op = Path(output_path)
    parent = op.parent if str(op.parent) else Path()
    try:
        parent_resolved = parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if op.name == "":
        return False
    return input_resolved == parent_resolved / op.name


# ── WriteOptions and preflight (L2-WRT-014, L2-WRT-017) ────────────────


@dataclass(frozen=True)
class WriteOptions:
    """Output-side options for the L2-WRT-014/017 safety checks.

    Attributes:
        input_path: Input MIE file path. When set, ``write_csv`` and
            ``write_csv_split`` reject same-path output before opening
            any file.
        no_clobber: When True, refuse to overwrite an existing
            destination.
        allow_partial: When True, an unrecoverable mid-file sync loss
            commits the rows decoded so far as ``<destination>.partial``
            and returns success rather than propagating the error.
        time_render: L2-WRT-025. How the ``TIME_STAMP`` column is rendered.
            Defaults to the day-of-year form, so a caller that does not set it
            gets exactly the vendor-compatible output every version before
            v3.0.0 produced.
    """

    input_path: Path | None = None
    no_clobber: bool = False
    allow_partial: bool = False
    time_render: TimeRender = DOY_RENDER


@dataclass(frozen=True)
class PartialCommit:
    """Where the partial output landed when ``allow_partial`` converted
    an ``UnrecoverableSyncLoss`` into a successful exit.

    Attributes:
        main_path: Path of the committed main `.partial` file.
        errors_path: Path of the errors `.partial` file, if any
            errored/spurious rows were written before the sync loss.
        offset: Byte offset of the unrecoverable boundary.
        sync_losses: Cumulative recovery attempts when the loss
            became unrecoverable.
    """

    main_path: Path
    errors_path: Path | None
    offset: int
    sync_losses: int


@dataclass(frozen=True)
class WriteOutcome:
    """Result of a successful CSV write.

    ``partial`` is ``None`` for Complete / PartialRecovered decodes;
    the CLI distinguishes the two by querying
    ``MieFileReader.sync_losses`` post-iteration. ``partial`` is
    ``Some(PartialCommit)`` only when ``allow_partial`` fired.

    Attributes:
        normal_count: Number of normal messages written.
        error_count: Number of errored/spurious messages written.
        partial: Partial-commit info, if applicable.
    """

    normal_count: int
    error_count: int
    partial: PartialCommit | None


def error_path_for(output: Path) -> Path:
    """``<stem>_errors<suffix>`` -- where split mode sends errored rows.

    Used by both :func:`write_csv_split` (which writes it) and
    :func:`commit_targets` (which pre-flights it), so the path guarded and the
    path written are one derivation.

    Args:
        output: The destination the operator named.

    Returns:
        The sibling path errored and spurious rows are written to.
    """
    return output.with_name(f"{output.stem}_errors{output.suffix}")


def partial_path_for(destination: Path) -> Path:
    """``<destination>.partial`` -- where an ``--allow-partial`` run commits the
    rows decoded before an unrecoverable sync loss (L2-WRT-016).

    Used by both :meth:`_AtomicCsvFile.commit_partial` and
    :func:`commit_targets`, for the same reason as :func:`error_path_for`.

    Args:
        destination: The path a complete run would have committed.

    Returns:
        The sibling ``.partial`` path an interrupted run commits instead.
    """
    return destination.with_name(f"{destination.name}.partial")


def commit_targets(output: Path, split_errors: bool, allow_partial: bool) -> list[Path]:
    """Every path a decode run could commit, given its destination and mode.

    The L2-WRT-014 collision guard has to test *all* of them, not just the
    destination the operator named. A derived path is an ordinary path that can
    name an ordinary file, and "it was derived from a path we already checked"
    says nothing about whether it collides with a *different* input:
    ``-o capture.mie --separate-errors`` derives ``capture_errors.mie``, which
    is a perfectly plausible name for one of the recordings being decoded. Both
    destructive cases were live until this existed -- the errors file and the
    ``.partial`` file each committed straight over an input, and the run exited
    0.

    ``.partial`` targets are enumerated even though a clean decode never writes
    one: the guard runs before the output is opened, which is the only point at
    which refusing is still safe, and by then nobody knows whether the decode
    will lose sync. Refusing a run that *might* have destroyed an input is the
    conservative direction.

    Args:
        output: The destination the operator named.
        split_errors: True when errored rows go to their own file.
        allow_partial: True when a sync loss commits ``.partial`` output.

    Returns:
        Main, errors, then their ``.partial`` variants -- ordered so the error
        names the most direct collision when more than one target matches.
    """
    targets = [output]
    if split_errors:
        targets.append(error_path_for(output))
    if allow_partial:
        # A list comprehension, fully evaluated BEFORE the extend. A generator
        # would be consumed *while* ``targets`` grows, so it would read its own
        # output and never terminate. The partial of the *errors* file is a real
        # commit target in split mode, and it is the one an audit forgets.
        partials = [partial_path_for(target) for target in targets]
        targets.extend(partials)
    return targets


def _preflight_output(output: Path, split_errors: bool, opts: WriteOptions) -> None:
    """Raise per the L2-WRT-014 and L2-WRT-017 contracts.

    Runs before any output file is opened so existing destinations are
    never partially overwritten on a rejected configuration.

    The collision test covers **every** commit target and **is** the guarantee:
    L2-WRT-014 is a pre-open rule, and once the mapping is live there is nothing
    left to refuse.

    The no-clobber test here is **not** the guarantee -- that lives in the commit
    (L2-WRT-023, :meth:`_AtomicCsvFile._commit_no_replace`). ``exists()`` answers
    a question about the past, and between the answer and the rename any other
    process may create the destination. What this test buys is an *early*
    refusal, before a temp file exists and before a whole file is decoded, with
    the destination named. It covers only the two paths a run definitely creates:
    ``.partial`` targets are deliberately left to the commit, so a stale
    ``<dest>.partial`` lying around does not refuse a run that was never going to
    write one.

    Args:
        output: The destination the operator named.
        split_errors: True when the caller is :func:`write_csv_split`.
        opts: Output safety options.

    Raises:
        MieInputOutputCollisionError: if any path this run could commit is also
            an input (L2-WRT-014).
        MieClobberRefusedError: if a destination exists and ``no_clobber`` is
            set (L2-WRT-017).
    """
    if opts.input_path is not None:
        for target in commit_targets(output, split_errors, opts.allow_partial):
            if paths_refer_to_same_file(opts.input_path, target):
                raise MieInputOutputCollisionError(str(target))
    if opts.no_clobber:
        if output.exists():
            raise MieClobberRefusedError(str(output))
        if split_errors:
            error_path = error_path_for(output)
            if error_path.exists():
                raise MieClobberRefusedError(str(error_path))


# ── Atomic CSV write helper (L2-WRT-015, L2-WRT-016) ───────────────────


#: Process-global monotonic counter feeding the temp-file salt, so two writers
#: created in one process never derive the same name (``next()`` is atomic under
#: the GIL). Mirrors the Rust ``TEMP_COUNTER`` atomic.
_temp_counter = itertools.count()

#: Bound on exclusive-create retries before giving up (a reused PID may leave a
#: stale temp; each retry advances the counter so it converges immediately).
_TEMP_MAX_ATTEMPTS = 128


def _unique_temp_path(final_path: Path) -> Path:
    """A fresh, hard-to-predict temp path beside ``final_path``.

    Pattern: ``<destination>.aero1553.tmp.<pid>.<counter>.<nanos>``. Co-located
    so ``os.replace`` is atomic (same filesystem); the per-process counter plus
    wall-clock nanoseconds make each call unique (and unpredictable), so two
    writers targeting the same destination cannot derive the same name. The
    caller still creates it with exclusive-create (``O_EXCL``) as the guarantee.

    Returns:
        The candidate temp path. It is only a NAME -- the caller still has to
        create it exclusively.
    """
    salt = f"{os.getpid()}.{next(_temp_counter)}.{time.time_ns()}"
    return final_path.with_name(f"{final_path.name}.aero1553.tmp.{salt}")


#: CSV column definitions in output order. Each entry is (column_name, description).
#: The canonical column order is defined here and used by all output functions.
#:
#: Two blocks, in this order (L2-WRT-001):
#:
#: 1. The **44-column DDC vendor block**, ``TIME_STAMP`` through ``XMT_GAP``. Its
#:    order is dictated by the vendor CSV and must not change — column N here is
#:    column N of a vendor-produced CSV, which is what makes a positional
#:    (``awk $N``) comparison against vendor output correct.
#: 2. **Decoder-added columns**, appended after it. ``ERROR`` / ``ERROR_CODE``
#:    have no vendor counterpart: the vendor tool does not report bus errors as
#:    CSV fields at all. Any column added in future goes here too, never inside
#:    the vendor block.
CSV_COLUMNS: list[tuple[str, str]] = [
    # ── DDC vendor block (columns 1-44) ────────────────────────────────────
    ("TIME_STAMP", "IRIG timestamp DAY:HH:MM:SS.uuuuuu"),
    ("RT", "Remote Terminal address 0-30"),
    ("MSG", "Message identifier: <Subaddress><T|R>"),
    *[(f"WD{i:02d}", f"Data word {i} (hex)") for i in range(1, MAX_DATA_WORDS + 1)],
    ("STAT", "MIL-STD-1553 Status Word (hex)"),
    ("CMD", "MIL-STD-1553 Command Word (hex)"),
    ("MUX", "Source label from the input file name (L2-WRT-020; empty with --no-mux)"),
    ("TERM_NAME", "Terminal name (external config, empty by spec L2-WRT-013)"),
    ("BUS", "Bus identifier: A or B"),
    ("DELTA", "Seconds since prior message with same RT+MSG"),
    ("IM_GAP", "Inter-message gap (empty by spec L2-WRT-013)"),
    ("RCV_GAP", "Receive gap (empty by spec L2-WRT-013)"),
    ("XMT_GAP", "Transmit gap (empty by spec L2-WRT-013)"),
    # ── Decoder-added columns (45-46), no vendor counterpart ───────────────
    ("ERROR", "Error label: empty=normal, ERROR=bit14, SPURIOUS=type 0x20"),
    ("ERROR_CODE", "DDC error code (0x01xx) or decoder code (0x20xx)"),
]

#: Number of leading columns that make up the DDC vendor layout (L1-OUT-001).
#: Everything past this index is a decoder addition.
VENDOR_COLUMN_COUNT: int = 44

#: Ordered list of column names for CSV header row.
CSV_HEADER: list[str] = [name for name, _ in CSV_COLUMNS]


def message_to_row(msg: MieMessage, render: TimeRender = DOY_RENDER) -> dict[str, str]:
    """Convert a single decoded message to a dict of CSV field strings.

    Args:
        msg: A fully decoded MieMessage instance.
        render: L2-WRT-025 rendering for the ``TIME_STAMP`` column. Defaults to
            day-of-year, so existing callers are unaffected.

    Returns:
        A dict keyed by column name (matching :data:`CSV_HEADER`) with
        string values. Handles all message types including errored
        records and SPURIOUS_DATA (where command_word is None).

    Raises:
        MieCalendarUnavailableError: when a calendar rendering cannot be
            resolved for this record (L2-WRT-026).
    """
    try:
        timestamp = msg.timestamp.format_with(render)
    except CalendarUnavailableError as exc:
        raise MieCalendarUnavailableError(
            f"{exc} (record at offset 0x{msg.file_offset:X})"
        ) from exc
    row: dict[str, str] = {
        "TIME_STAMP": timestamp,
        "RT": str(msg.rt) if msg.rt is not None else "",
        "MSG": msg.msg_label,
        "STAT": f"{msg.status_word:04X}" if msg.status_word is not None else "",
        "CMD": f"{msg.command_word.raw:04X}" if msg.command_word is not None else "",
        "MUX": msg.mux or "",
        "TERM_NAME": "",
        "BUS": msg.bus.name,
        "DELTA": f"{msg.delta:.6f}" if msg.delta is not None else "",
        "ERROR": msg.error_label,
        "ERROR_CODE": f"{msg.error_word:04X}" if msg.error_word is not None else "",
        "IM_GAP": "",
        "RCV_GAP": "",
        "XMT_GAP": "",
    }

    for i in range(1, MAX_DATA_WORDS + 1):
        col = f"WD{i:02d}"
        idx = i - 1
        if idx < len(msg.data_words):
            row[col] = f"{msg.data_words[idx]:04X}"
        else:
            row[col] = ""

    return row


# ── Streaming primitives (PY-streaming, L3-PY-012) ─────────────────────
#
# These mirror the Rust writer's `AtomicCsvFile` and `CsvWriter` so both
# implementations stream rows straight to the output handle with no
# per-record buffering — memory is O(1) in the record count. The
# byte image they produce is pinned by the golden characterization
# tests (tests/test_writer_streaming_golden.py).


class _AtomicCsvFile:
    """Temp-file + ``os.replace`` atomic writer (L2-WRT-015/016).

    Mirrors the Rust ``AtomicCsvFile``. Opens a temp file beside the
    destination (same directory → ``os.replace`` is atomic on one
    filesystem). Callers write through :attr:`stream`. :meth:`commit`
    renames the temp over the destination; :meth:`commit_partial`
    renames it to ``<destination>.partial``. If the writer is closed
    without committing (decode failed or was interrupted), the temp
    file is unlinked and a pre-existing destination is left untouched.

    With ``no_clobber`` set, **every** commit this writer performs -- over the
    destination and onto ``<destination>.partial`` alike -- refuses an existing
    target instead of replacing it (L2-WRT-023). That refusal is the guarantee;
    the pre-flight ``exists()`` test only reports the same condition earlier.

    Usable as a context manager: an uncommitted writer is cleaned up on
    ``__exit__`` so the failure path leaves no temp behind.
    """

    def __init__(self, final_path: Path, no_clobber: bool = False) -> None:
        self._final = final_path
        self._no_clobber = no_clobber
        # Create a uniquely-named temp file beside the destination with
        # exclusive create (mode "x" == O_CREAT|O_EXCL): it never opens an
        # existing file, so two writers targeting the same destination in one
        # process cannot collide on a shared name and clobber each other. On the
        # (near-impossible) name clash we retry with the next unique name.
        # newline="" keeps the csv terminator LF-only; the stream is owned by
        # this object and closed in commit() / cleanup(), so a `with` block does
        # not fit its lifecycle.
        last_exc: OSError | None = None
        for _ in range(_TEMP_MAX_ATTEMPTS):
            temp = _unique_temp_path(final_path)
            try:
                self._stream: TextIO = open(  # pylint: disable=consider-using-with
                    temp, "x", newline="", encoding="utf-8"
                )
            except FileExistsError as exc:
                last_exc = exc
                continue
            except OSError as exc:
                raise MieWriterError(str(final_path), exc) from exc
            self._temp = temp
            self._committed = False
            return
        raise MieWriterError(
            str(final_path),
            last_exc or OSError("could not create a unique temp file"),
        )

    @property
    def stream(self) -> TextIO:
        """The underlying text stream (the open temp file)."""
        return self._stream

    def commit(self) -> None:
        """Flush, close, and atomically rename the temp over the destination.

        Raises:
            MieWriterError: if the flush, close or rename fails. The temp file
                is removed first, so a failure leaves nothing behind.
            MieClobberRefusedError: under ``no_clobber``, if the destination
                exists at the moment of the commit (L2-WRT-023).
        """
        self._commit_onto(self._final)

    def commit_partial(self) -> Path:
        """Rename the temp to ``<destination>.partial`` instead of over the
        destination (L2-WRT-016 ``--allow-partial`` branch). The original
        destination, if any, is left untouched.

        Returns:
            The ``.partial`` path actually written.

        Raises:
            MieWriterError: if the flush, close or rename fails.
            MieClobberRefusedError: under ``no_clobber``, if the ``.partial``
                path exists at the moment of the commit. It is an actual commit
                target, so L2-WRT-023 covers it like any other.
        """
        partial = partial_path_for(self._final)
        self._commit_onto(partial)
        return partial

    def _commit_onto(self, destination: Path) -> None:
        """The one commit sequence, shared by :meth:`commit` and
        :meth:`commit_partial` so the two cannot drift in how they flush, close,
        or honour ``no_clobber``. Each used to spell the sequence out for
        itself, which is how a rule can end up applying to one commit target and
        not the other.

        Raises:
            MieWriterError: on a flush, close or rename failure.
            MieClobberRefusedError: under ``no_clobber``, if ``destination``
                exists at the moment of the commit.
        """
        try:
            self._close_stream()
        except OSError as exc:
            # THE FINAL FLUSH IS PART OF THE COMMIT (L2-WRT-024). It is also the
            # single most likely place for a disk-full error to land, because it
            # is where the last buffered rows actually reach the filesystem --
            # every earlier `write` may have returned having only filled a
            # buffer. Closing outside this wrapper, which is what shipped, let
            # that failure escape as a raw OSError from a method documented to
            # raise MieWriterError, so the CLI classified a truncated CSV as an
            # unexpected crash rather than a write failure.
            self._cleanup_temp()
            raise MieWriterError(str(destination), exc) from exc

        if self._no_clobber:
            self._commit_no_replace(destination)
            return
        try:
            os.replace(self._temp, destination)
        except OSError as exc:
            self._cleanup_temp()
            raise MieWriterError(str(destination), exc) from exc
        self._committed = True

    def _commit_no_replace(self, destination: Path) -> None:
        """Move the temp onto ``destination`` without ever replacing an existing
        file (L2-WRT-023). Mirrors the Rust ``commit_no_replace``.

        Two mechanisms, in order of preference:

        1. ``os.link`` + unlink. ``link(2)`` -- and ``CreateHardLinkW``, which is
           what ``os.link`` calls on Windows -- raises ``FileExistsError`` when
           the destination exists, and otherwise publishes the *complete* file
           under its final name in one atomic step, so a concurrent reader never
           observes a partial or empty file.
        2. Exclusive-create reservation, then ``os.replace``. Hard links do not
           exist on FAT/exFAT and are refused by some network filesystems, so the
           link can fail for reasons that have nothing to do with the
           destination. Mode ``"x"`` claims the name atomically -- one of two
           racing processes wins it -- and the rename that follows overwrites
           only *our own* zero-byte reservation.

        ``os.replace`` cannot implement this on its own: it replaces on every
        platform, which is exactly why an ``exists()`` pre-flight paired with a
        replacing rename is not a no-clobber guarantee.

        Raises:
            MieWriterError: on a failure other than the destination existing.
            MieClobberRefusedError: if ``destination`` already exists.
        """
        try:
            os.link(self._temp, destination)
        except FileExistsError as exc:
            self._cleanup_temp()
            raise MieClobberRefusedError(str(destination)) from exc
        except OSError:
            self._reserve_then_replace(destination)
            return
        # The link published a second name for the same bytes rather than moving
        # them, so the temp is still there by design. Unlinking is best effort:
        # the destination is committed either way, and leaving ``_committed``
        # False when the unlink fails just gives ``close()`` a second attempt.
        try:
            self._temp.unlink()
            self._committed = True
        except OSError:
            pass

    def _reserve_then_replace(self, destination: Path) -> None:
        """Fallback half of :meth:`_commit_no_replace`, for filesystems with no
        hard links.

        Raises:
            MieWriterError: if the reservation or the rename fails.
            MieClobberRefusedError: if ``destination`` already exists.
        """
        try:
            # os.open, not open(dest, "x") with an empty body. The point here is
            # to CLAIM THE NAME, not to open a text file and write nothing to it,
            # and the descriptor form says so -- it is also the same
            # O_CREAT|O_EXCL the C++ POSIX backend reserves with, so the two
            # implementations read as one mechanism rather than two.
            os.close(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError as exc:
            self._cleanup_temp()
            raise MieClobberRefusedError(str(destination)) from exc
        except OSError as exc:
            self._cleanup_temp()
            raise MieWriterError(str(destination), exc) from exc
        try:
            os.replace(self._temp, destination)
        except OSError as exc:
            # Take the reservation back out. Leaving it would hand the operator
            # an empty CSV where the failure message says nothing was written.
            with contextlib.suppress(OSError):
                destination.unlink()
            self._cleanup_temp()
            raise MieWriterError(str(destination), exc) from exc
        self._committed = True

    def _close_stream(self) -> None:
        if not self._stream.closed:
            self._stream.flush()
            self._stream.close()

    def _cleanup_temp(self) -> None:
        try:
            if self._temp.exists():
                self._temp.unlink()
        except OSError:
            pass

    def close(self) -> None:
        """Close the stream; unlink the temp if it was never committed.

        The close is swallowed rather than propagated: this runs on the failure
        path (and from ``__exit__``), where its whole job is to leave no temp
        behind. A stream whose buffered data cannot be flushed raises again from
        ``close()``, and letting that through would skip the unlink and leak the
        very temp file this method exists to remove -- while replacing whatever
        error actually caused the failure. The commit path reports flush and
        close failures properly (L2-WRT-024); by the time we are here, someone
        already has the real error.
        """
        try:
            if not self._stream.closed:
                self._stream.close()
        except OSError:
            pass
        if not self._committed:
            self._cleanup_temp()

    def __enter__(self) -> _AtomicCsvFile:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _StreamingCsvRowWriter:
    """Streaming CSV row writer (mirrors the Rust ``CsvWriter``).

    Writes the header row on construction, then one CSV row per message
    via ``csv.DictWriter``. Retains no per-record buffer beyond the
    underlying stream's, so memory is O(1) in the record count
    (L3-PY-012 / L3-RS-012). ``lineterminator="\\n"`` keeps output
    byte-stable across platforms and aligned with the Rust writer.

    A broken pipe is allowed to propagate unchanged (the stdout consumer
    closed early); callers classify it with :func:`is_broken_pipe` and map it
    to a clean exit per L2-WRT-018. Other ``OSError``\\ s (disk full,
    permission) are wrapped as :class:`MieWriterError`.
    """

    def __init__(self, stream: TextIO, destination: str, render: TimeRender = DOY_RENDER) -> None:
        self._destination = destination
        self._render = render
        self._writer = csv.DictWriter(stream, fieldnames=CSV_HEADER, lineterminator="\n")
        self._rows_written = 0
        try:
            self._writer.writeheader()
        except OSError as exc:
            self._reraise_or_wrap(exc)

    def write_message(self, msg: MieMessage) -> None:
        """Write one decoded message as a CSV row."""
        try:
            # L2-WRT-025: the row is built -- and the timestamp resolved --
            # before anything is handed to the csv writer, so a calendar
            # refusal (L2-WRT-026 clause 3) cannot leave a half-written line.
            self._writer.writerow(message_to_row(msg, self._render))
        except OSError as exc:
            self._reraise_or_wrap(exc)
        self._rows_written += 1

    def _reraise_or_wrap(self, exc: OSError) -> None:
        """Let a broken pipe through untouched (L2-WRT-018 — the caller decides
        it is a clean stop); wrap every other OS error as a writer failure.

        Matching on :func:`is_broken_pipe` rather than the ``BrokenPipeError``
        type is what makes this correct on Windows, where the pipe-closed
        condition arrives as a bare ``OSError``.

        Raises:
            OSError: re-raised unchanged when it is a broken pipe, so the
                caller can treat it as a clean stop.
            MieWriterError: wrapping any other OS error.
        """
        if is_broken_pipe(exc):
            raise exc
        raise MieWriterError(self._destination, exc) from exc

    @property
    def rows_written(self) -> int:
        """Number of data rows written so far."""
        return self._rows_written


def write_csv(
    messages: Iterable[MieMessage],
    output: str | Path | TextIO | None = None,
    opts: WriteOptions | None = None,
) -> WriteOutcome:
    """Write all messages (normal + errored) to a single CSV.

    Used for INLINE error mode. ERROR and ERROR_CODE columns are
    populated for errored and spurious records.

    Args:
        messages: Iterable of decoded MieMessage instances.
        output: Destination for CSV output (file path, stream, or None for stdout).
        opts: Output safety options. When ``output`` is a file path, the
            L2-WRT-014 input/output collision check, L2-WRT-017 no-clobber
            check, and L1-EXIT-004 allow_partial handling are applied. Stream
            destinations ignore these (no on-disk identity, no partial).

    Returns:
        A WriteOutcome capturing counts and optional PartialCommit info.

    Raises:
        MieInputOutputCollisionError: Output path resolves to the same file as the input.
        MieClobberRefusedError: Output exists and ``opts.no_clobber`` is True.
        MieUnrecoverableSyncLossError: Lenient-mode mid-file sync loss
            exhausted recovery and ``opts.allow_partial`` is False.
        MieWriterError: If an I/O error occurs during writing.
    """
    if opts is None:
        opts = WriteOptions()

    # File-path destination vs. text-stream (TextIO or None → stdout): the two
    # differ enough (atomic temp + preflight + .partial vs. straight streaming)
    # to warrant separate helpers.
    if isinstance(output, (str, Path)):
        return _write_csv_to_file(messages, Path(output), opts)
    stream: TextIO = output if output is not None else sys.stdout
    dest_name = "stdout" if output is None else "<stream>"
    return _write_csv_to_stream(messages, stream, dest_name, opts)


def _write_csv_to_file(
    messages: Iterable[MieMessage], dest: Path, opts: WriteOptions
) -> WriteOutcome:
    """Stream rows into an atomic temp file (constant memory), then commit() over
    the destination — or commit_partial() to ``<dest>.partial`` on an
    allow_partial sync loss.

    Returns:
        The row counts, with ``partial`` set when the rows were committed to
        ``<dest>.partial`` instead of the destination.

    Raises:
        MieInputOutputCollisionError: destination is also an input.
        MieClobberRefusedError: destination exists and ``no_clobber`` is set.
        MieUnrecoverableSyncLossError: sync was lost and ``allow_partial`` is
            NOT set. With it set, the rows are committed and this returns
            normally.
        MieWriterError: on an I/O failure writing or renaming.
    """
    _preflight_output(dest, False, opts)
    partial_info: tuple[int, int] | None = None
    with _AtomicCsvFile(dest, no_clobber=opts.no_clobber) as atomic:
        writer = _StreamingCsvRowWriter(atomic.stream, str(dest), opts.time_render)
        try:
            for msg in messages:
                writer.write_message(msg)
        except MieUnrecoverableSyncLossError as exc:
            if not opts.allow_partial:
                raise
            partial_info = (exc.offset, exc.sync_losses)

        count = writer.rows_written
        if partial_info is None:
            atomic.commit()
            logger.info("wrote %d rows to %s", count, dest)
            return WriteOutcome(normal_count=count, error_count=0, partial=None)

        partial_path = atomic.commit_partial()
        offset, sync_losses = partial_info
        logger.warning(
            "Unrecoverable sync loss at 0x%X after %d recovery attempt(s); "
            "wrote %d rows to %s (--allow-partial)",
            offset,
            sync_losses,
            count,
            partial_path,
        )
        return WriteOutcome(
            normal_count=count,
            error_count=0,
            partial=PartialCommit(
                main_path=partial_path,
                errors_path=None,
                offset=offset,
                sync_losses=sync_losses,
            ),
        )


def _write_csv_to_stream(
    messages: Iterable[MieMessage], stream: TextIO, dest_name: str, opts: WriteOptions
) -> WriteOutcome:
    """Stream rows straight to a text sink. No on-disk identity, so no preflight,
    no atomic temp, and no ``.partial`` — rows already sent are what the consumer
    has seen.

    Returns:
        The row counts, always with ``partial`` unset -- a stream has no
        ``.partial`` to commit to.

    Raises:
        MieUnrecoverableSyncLossError: sync was lost. ``allow_partial`` cannot
            help here: the rows already sent are what the consumer has seen.
        OSError: re-raised unchanged when it is a broken pipe, so the caller
            can treat a closed consumer as a clean stop (L2-WRT-018).
        MieWriterError: on any other I/O failure.
    """
    writer = _StreamingCsvRowWriter(stream, dest_name, opts.time_render)
    try:
        for msg in messages:
            writer.write_message(msg)
    except OSError as exc:
        # L2-WRT-018: downstream consumer closed early. Treat as success. Any
        # other OSError is a real write failure and must keep propagating (the
        # row writer has already wrapped those as MieWriterError, so reaching
        # here with one is defensive).
        if not is_broken_pipe(exc):
            raise
        logger.info("Stdout consumer closed early (broken pipe) -- exit 0")
        return WriteOutcome(normal_count=writer.rows_written, error_count=0, partial=None)
    except MieUnrecoverableSyncLossError:
        if not opts.allow_partial:
            raise
        # Rows decoded so far are already in the stream; nothing to roll back.
        logger.debug("Unrecoverable sync loss on stream output (--allow-partial)")

    logger.info("wrote %d rows to %s", writer.rows_written, dest_name)
    return WriteOutcome(normal_count=writer.rows_written, error_count=0, partial=None)


def write_csv_split(
    messages: Iterable[MieMessage],
    output: str | Path,
    opts: WriteOptions | None = None,
) -> WriteOutcome:
    """Write normal messages to main CSV, errors to a separate file.

    Used for SEPARATE error mode (`--separate-errors`; INLINE is the
    default). Normal messages go to
    ``output``, errored and spurious records go to
    ``<output_stem>_errors<output_suffix>``.

    Both files are written via the atomic temp + ``os.replace`` pattern.
    If the errors-file write fails after the main file has been
    committed, the main file remains; we accept this trade-off because
    atomically committing two files together is not possible without
    cross-file rename support.

    Args:
        messages: Iterable of decoded MieMessage instances.
        output: Path for the main CSV output file.
        opts: Output safety options. Both the main destination AND the
            derived errors path are checked against ``opts.no_clobber``.
            The L2-WRT-014 collision check applies to the main path.

    Returns:
        The normal and error row counts.

    Raises:
        MieInputOutputCollisionError: Main output collides with input.
        MieClobberRefusedError: Main or errors destination exists and
            ``opts.no_clobber`` is True. The derived errors path gets its own
            check.
        MieUnrecoverableSyncLossError: sync was lost and ``allow_partial`` is
            not set.
        MieWriterError: If an I/O error occurs during writing. The main CSV is
            committed FIRST (L2-WRT-019), so a failure on the errors file
            leaves the main file in place.
    """
    if opts is None:
        opts = WriteOptions()
    output_path = Path(output)
    error_path = error_path_for(output_path)

    # Covers the errors destination and every ``.partial`` variant, not just
    # ``output_path``. This used to reason that the errors path needed no
    # collision check because it was "derived from output_path which was just
    # checked", which does not follow: a derived path is an ordinary path that
    # can name a *different* input, and ``capture_errors.mie`` is a plausible
    # recording name (L2-WRT-014).
    _preflight_output(output_path, True, opts)

    # Stream into the main temp file eagerly; the errors temp is created
    # lazily on the first error row so a clean decode never leaves an
    # empty errors CSV behind. Both stay O(1) in the record count.
    main_atomic = _AtomicCsvFile(output_path, no_clobber=opts.no_clobber)
    errors_atomic: _AtomicCsvFile | None = None
    partial_info: tuple[int, int] | None = None
    try:
        main_writer = _StreamingCsvRowWriter(main_atomic.stream, str(output_path), opts.time_render)
        error_writer: _StreamingCsvRowWriter | None = None

        try:
            for msg in messages:
                if msg.error_label:
                    if error_writer is None:
                        errors_atomic = _AtomicCsvFile(error_path, no_clobber=opts.no_clobber)
                        error_writer = _StreamingCsvRowWriter(
                            errors_atomic.stream, str(error_path), opts.time_render
                        )
                    error_writer.write_message(msg)
                else:
                    main_writer.write_message(msg)
        except MieUnrecoverableSyncLossError as exc:
            if not opts.allow_partial:
                raise
            partial_info = (exc.offset, exc.sync_losses)

        normal_count = main_writer.rows_written
        error_count = error_writer.rows_written if error_writer is not None else 0
        return _commit_split_outputs(
            main_atomic,
            errors_atomic,
            output_path,
            error_path,
            normal_count,
            error_count,
            partial_info,
        )
    finally:
        # Unlink any temp that was never committed (failure path). After a
        # successful commit/commit_partial these are no-ops.
        main_atomic.close()
        if errors_atomic is not None:
            errors_atomic.close()


def _commit_split_outputs(
    main_atomic: _AtomicCsvFile,
    errors_atomic: _AtomicCsvFile | None,
    output_path: Path,
    error_path: Path,
    normal_count: int,
    error_count: int,
    partial_info: tuple[int, int] | None,
) -> WriteOutcome:
    """Commit the split outputs. ``partial_info is None`` is the normal path
    (atomic rename over each destination, MAIN first per L2-WRT-019 so a failed
    errors commit never leaves an orphan errors file); a tuple is the
    ``--allow-partial`` path (rename each temp to its ``.partial``).

    Returns:
        The counts passed in, with ``partial`` populated on the
        ``--allow-partial`` path and ``None`` on the normal one.
    """
    if partial_info is None:
        main_atomic.commit()
        logger.info("wrote %d normal rows to %s", normal_count, output_path)
        if errors_atomic is not None:
            errors_atomic.commit()
            logger.info("wrote %d error/spurious rows to %s", error_count, error_path)
        else:
            logger.info("no error/spurious records -- error file not created")
        return WriteOutcome(normal_count=normal_count, error_count=error_count, partial=None)

    # Partial path: commit each file as .partial -- MAIN FIRST, for the same
    # reason as the normal path above and under the same rule (L2-WRT-019).
    # This is the branch that used to run the other way round in all three
    # implementations: an errors .partial that committed before a main .partial
    # whose rename then failed left the operator an orphan
    # <dest>_errors.csv.partial next to no main output at all -- the precise
    # residue the main-first order exists to make impossible. That the normal
    # path got it right and the failure path did not is what a rule stated over
    # "the commit" rather than over "every commit" buys you.
    main_partial = main_atomic.commit_partial()
    errors_partial: Path | None = None
    if errors_atomic is not None:
        errors_partial = errors_atomic.commit_partial()
    offset, sync_losses = partial_info
    logger.warning(
        "Unrecoverable sync loss at 0x%X after %d recovery attempt(s); "
        "wrote %d normal + %d error rows as partial to %s (--allow-partial)",
        offset,
        sync_losses,
        normal_count,
        error_count,
        main_partial,
    )
    return WriteOutcome(
        normal_count=normal_count,
        error_count=error_count,
        partial=PartialCommit(
            main_path=main_partial,
            errors_path=errors_partial,
            offset=offset,
            sync_losses=sync_losses,
        ),
    )
