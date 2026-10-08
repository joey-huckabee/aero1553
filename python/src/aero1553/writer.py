"""CSV output writer for decoded MIE messages.

Streams rows straight to the output handle -- no DataFrame or full-file
buffering, so decode memory is O(1) in the record count. The writing is the
``aero1553`` Rust crate's writer, run through the package's compiled extension:
the same bytes, the same atomic commit and ``.partial`` handling, as the CLI.

Produces CSV output matching the column layout used by DDC's recording
software, enabling direct comparison between Aero1553 output and
vendor-generated CSV files.

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

import errno
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO, TypedDict

from aero1553 import _native
from aero1553.models import DOY_RENDER, MAX_DATA_WORDS, MieMessage, TimeRender

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
    return _native.paths_refer_to_same_file(input_path, output_path)


# ── WriteOptions and results (L2-WRT-014, L2-WRT-017) ─────────────────


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

    ``_errors`` goes in front of the file name's final ``.`` (L2-ERR-008):
    ``out.csv`` -> ``out_errors.csv``, ``out`` -> ``out_errors``, and ``o.`` ->
    ``o_errors.``.

    Taken from the writer's own :func:`commit_targets`, so the path reported
    here is the path written. It used to be ``pathlib``'s ``stem`` +
    ``suffix``, which split a name ending in ``.`` differently from the writer
    (``o._errors`` against ``o_errors``) and changed its own answer in Python
    3.14.

    Args:
        output: The destination the operator named.

    Returns:
        The sibling path errored and spurious rows are written to.
    """
    return Path(_native.commit_targets(output, True, False)[1])


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
    return list(_native.commit_targets(output, split_errors, allow_partial))


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
    cells = _native.message_to_row(msg, **_render_kwargs(render))
    return dict(zip(CSV_HEADER, cells, strict=True))


class _RenderArgs(TypedDict):
    """The rendering, as the compiled writer's keyword arguments."""

    time_format: int
    year: int | None
    utc_offset_minutes: int


class _OptionArgs(_RenderArgs):
    """``WriteOptions``, as the compiled writer's keyword arguments."""

    input_path: Path | None
    no_clobber: bool
    allow_partial: bool


def _render_kwargs(render: TimeRender) -> _RenderArgs:
    return {
        "time_format": int(render.format),
        "year": render.year,
        "utc_offset_minutes": render.utc_offset_minutes,
    }


def _option_kwargs(opts: WriteOptions) -> _OptionArgs:
    return {
        "input_path": opts.input_path,
        "no_clobber": opts.no_clobber,
        "allow_partial": opts.allow_partial,
        **_render_kwargs(opts.time_render),
    }


def _outcome(raw: tuple[int, int, tuple[Path, Path | None, int, int] | None]) -> WriteOutcome:
    normal, errors, partial = raw
    commit = None
    if partial is not None:
        main_path, errors_path, offset, sync_losses = partial
        commit = PartialCommit(
            main_path=Path(main_path),
            errors_path=None if errors_path is None else Path(errors_path),
            offset=offset,
            sync_losses=sync_losses,
        )
    return WriteOutcome(normal_count=normal, error_count=errors, partial=commit)


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
            destinations ignore these (no on-disk identity, no partial): on
            a stream a sync loss raises after the rows decoded before it
            have been written, whatever ``allow_partial`` says.

    Returns:
        A WriteOutcome capturing counts and optional PartialCommit info.

    Raises:
        MieInputOutputCollisionError: Output path resolves to the same file as the input.
        MieClobberRefusedError: Output exists and ``opts.no_clobber`` is True.
        MieUnrecoverableSyncLossError: Lenient-mode mid-file sync loss
            exhausted recovery, and either ``opts.allow_partial`` is False or
            ``output`` is a stream.
        MieWriterError: If an I/O error occurs during writing.
    """
    if opts is None:
        opts = WriteOptions()
    if isinstance(output, (str, Path)):
        destination = str(output)
        target: str | Path | TextIO = Path(output)
    else:
        # ``None`` is ``sys.stdout`` as it stands now -- the Python object, so a
        # redirected or captured stdout receives the rows.
        destination = "stdout" if output is None else "<stream>"
        target = output if output is not None else sys.stdout
    return _outcome(
        _native.write_csv(messages, target, destination=destination, **_option_kwargs(opts))
    )


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
    return _outcome(_native.write_csv_split(messages, Path(output), **_option_kwargs(opts)))
