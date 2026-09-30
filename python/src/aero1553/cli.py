"""Command-line interface for Aero1553.

Provides the ``aero1553`` CLI command for decoding DDC MIL-STD-1553
MIE binary recording files into CSV format, and for hex-dumping raw
binary content with record boundary awareness.

The command line is the Rust implementation's, run in-process through the
compiled extension (``aero1553._native``): flags, help text, log lines, exit
codes and CSV bytes are those of the ``aero1553`` Rust binary by construction,
not by keeping a second parser in step with it.

Configuration is loaded from an optional TOML file and merged with
CLI arguments. CLI arguments always take precedence.

Usage::

    # Decode to stdout
    aero1553 decode recording.mie

    # Decode with config file (--config is global: before the subcommand)
    aero1553 --config my-config.toml decode recording.mie

    # Decode excluding spurious data and mode codes
    aero1553 decode recording.mie --exclude-types SPURIOUS_DATA,MODE_COMMAND

    # Decode only RT 15 (include filter), excluding Bus B
    aero1553 decode recording.mie --include-rts 15 --exclude-buses B

    # Hex dump
    aero1553 dump recording.mie --records 10

From Python, :func:`main` runs one command line and returns its exit status::

    from aero1553.cli import EXIT_OK, main

    assert main(["decode", "recording.mie", "-o", "decoded.csv"]) == EXIT_OK

The CLI writes to the process's stdout and stderr *file descriptors*, as the
binary does, so its output is not routed through ``sys.stdout`` /
``sys.stderr``. It reaches a terminal, a pipe or a redirect exactly as the
binary's would; to capture it in-process, capture at the descriptor level
(pytest's ``capfd``, not ``capsys``).
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading

from aero1553 import _native

# Process exit codes -- the normative contract pinned by L2-CLI-011 /
# L1-EXIT-002..009. They are the values the Rust CLI returns (its
# `cli::exit_code` module); tests/test_cli.py pins each one against it.
EXIT_OK = 0  # complete / recovered / --allow-partial partial
EXIT_RUNTIME = 1  # runtime / decode error (I/O, writer, strict record failures)
EXIT_NO_RECORDS = 2  # input is not an MIE recording
EXIT_SYNC_LOSS = 3  # unrecoverable mid-file sync loss without --allow-partial
EXIT_USAGE = 4  # CLI usage error (bad/unknown/missing flag or argument)
EXIT_CONFIG = 5  # configuration error (missing/malformed/invalid config)
EXIT_MERGE_INCOMPATIBLE = 6  # merge inputs cannot share an absolute timeline (L1-EXIT-009)

# argv[0] as the Rust CLI sees it. It only ever skips it, but a real program
# name keeps any future use of it honest.
_PROG = "aero1553"


def main(argv: list[str] | None = None) -> int:
    """Run one ``aero1553`` command line in-process.

    Args:
        argv: Command-line arguments, without the program name. If ``None``,
            uses ``sys.argv[1:]``.

    Returns:
        Process exit code per L2-CLI-011: 0 success; 1 runtime/decode
        error; 2 no valid records; 3 unrecoverable sync loss; 4 CLI usage
        error; 5 configuration error; 6 incompatible merge inputs
        (L1-EXIT-009).
    """
    if argv is None:
        argv = sys.argv[1:]
    # Python's own buffered text goes out first: the CLI writes to the same
    # descriptors directly, and anything still sitting in sys.stdout would
    # otherwise surface AFTER output that was produced later.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            stream.flush()
    return _native.run_cli([_PROG, *argv])


def _neutralise_dead_stdout() -> None:
    """Point fd 1 at the null device once stdout is known to be unwritable.

    CPython flushes ``sys.stdout`` during interpreter shutdown. If the pipe is
    already gone that flush raises, CPython prints "Exception ignored while
    flushing sys.stdout" and — the part that actually matters — **overrides the
    process exit status with 120**. So a decode that correctly returned 0 after
    a broken pipe still exited non-zero, violating L2-WRT-018 (observed on
    Python 3.14 / Linux; earlier versions happened to leave an empty buffer and
    so escaped it).

    Repointing the file descriptor makes the shutdown flush a silent no-op.
    This is deliberately *not* done inside :func:`main`: it is fd-level surgery
    that would corrupt the output capture of any in-process caller (pytest, an
    embedding application). It belongs at the real process boundary, which is
    what :func:`main_cli` is.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:  # pragma: no cover - os.devnull is always openable
        return
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):  # pragma: no cover
        pass
    finally:
        os.close(devnull)


def main_cli(argv: list[str] | None = None) -> int:
    """Console-script / ``python -m`` entry point.

    Two things only a real process boundary may do, which is why they are here
    and not in :func:`main`:

    * **Ctrl-C.** While the Rust CLI runs, the interpreter is not executing
      Python bytecode, so Python's SIGINT handler -- which only sets a flag for
      the interpreter to act on -- would leave Ctrl-C waiting until a
      multi-second decode finished. Restoring the default disposition makes
      Ctrl-C end the process at once, exactly as it ends the Rust binary. An
      embedding application keeps its own handler, because :func:`main` never
      touches it.
    * **A dead stdout** must not turn a clean exit code into CPython's
      shutdown-failure 120; see :func:`_neutralise_dead_stdout`.

    Returns:
        The process exit code, with a dead stdout already handled so it cannot
        become CPython's shutdown-failure 120.
    """
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    code = main(argv)
    try:
        sys.stdout.flush()
    except OSError:
        # BrokenPipeError is the case this exists for, but it is a subclass of
        # OSError. Catching OSError also covers the disk-full / closed-handle
        # variants, which need the same treatment: neutralise stdout so
        # CPython's shutdown flush cannot turn a clean exit code into 120.
        _neutralise_dead_stdout()
    return code
