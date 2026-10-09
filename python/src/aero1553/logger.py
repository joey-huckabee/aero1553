"""Centralized logging configuration for Aero1553.

Provides a single function to configure the ``aero1553`` logger
hierarchy. All modules in the package obtain their loggers via
``logging.getLogger(__name__)``, which places them under the
``aero1553`` namespace and inherits the configuration set here.

Log Levels:
    DEBUG:
        Per-record decode details (type word, RT, SA, direction, word
        count), truncation events, parsed CLI arguments.
    INFO:
        File open/close with sizes, decode start/complete with counts
        and elapsed time, CSV write row counts, progress checkpoints
        every 100,000 messages.
    WARNING:
        Invalid records encountered during decode (non-fatal), freerun
        timestamps detected (external IRIG source unavailable).
    ERROR:
        File not found, empty file, write failures, unrecoverable
        record corruption.
    CRITICAL / OFF:
        Suppress all decoder output. The decoder emits no CRITICAL-level
        messages, so selecting ``CRITICAL`` is effectively silent;
        ``OFF`` is the explicit "silence everything" spelling. Both match
        the Rust logger's ``Level::Off``.

``WARN`` is accepted as a case-insensitive alias for ``WARNING``.

Usage::

    from aero1553.logger import configure_logging

    configure_logging("DEBUG")  # Enable all log output
    configure_logging("INFO")   # Standard operational logging
    configure_logging("WARNING") # Only warnings and errors (default)
    configure_logging("OFF")     # Silence all output
"""

from __future__ import annotations

import logging
import sys

from aero1553 import _native

#: Name of the root logger for the Aero1553 package.
LOGGER_NAME: str = "aero1553"

#: Default log format string.
LOG_FORMAT: str = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"

#: Default timestamp format for log records.
LOG_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"


def set_irig_day_advisory(enabled: bool) -> None:
    """Enable or disable the one-time IRIG day-of-year advisory.

    The level filter still applies on top of this: the advisory is logged at
    ``INFO``, so at the default ``WARNING`` level it is silent regardless.
    Disabling it also removes it from a ``--log-level INFO`` run.

    Args:
        enabled: ``False`` to suppress the advisory at every level.

    It sets the decoder's own switch -- there is no Python copy of it -- so
    it applies from the next record, including in a reader already open. A
    command line run with ``aero1553.cli.main`` has a switch of its own
    (``--no-irig-day-advisory``) and neither changes nor sees this one
    (L2-LOG-003).
    """
    _native.set_irig_day_advisory(enabled)


def irig_day_advisory() -> bool:
    """Whether the IRIG day-of-year advisory may be emitted.

    Returns:
        ``True`` unless suppressed via ``--no-irig-day-advisory`` or
        ``[logging] irig_day_advisory = false``.
    """
    return _native.irig_day_advisory()


def configure_logging(
    level: str = "WARNING",
    stream: object | None = None,
) -> None:
    """Configure the ``aero1553`` logger hierarchy.

    Sets up a :class:`logging.StreamHandler` on the package root logger
    with a structured format. Safe to call multiple times; subsequent
    calls replace the existing handler.

    Args:
        level: Log level name. One of ``DEBUG``, ``INFO``, ``WARNING``
            (alias ``WARN``), ``ERROR``, ``CRITICAL``, or ``OFF``
            (silence all output). Case-insensitive.
        stream: Output stream for log messages. Defaults to
            ``sys.stderr`` if ``None``.

    Raises:
        ValueError: If ``level`` is not a recognized log level name.
    """
    # The decoder's own parser, so this accepts exactly the names
    # `--log-level` and `[logging] level` do. CRITICAL and OFF both silence the
    # decoder: they map one above CRITICAL, since `logging` has no OFF level.
    numeric_level = _native.log_level_threshold(level)
    if numeric_level is None:
        raise ValueError(f"Invalid log level: {level!r}")

    target_stream = stream if stream is not None else sys.stderr

    root_logger = logging.getLogger(LOGGER_NAME)

    # Remove existing handlers to avoid duplicate output on repeated calls
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(target_stream)  # type: ignore[arg-type]
    handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    root_logger.setLevel(numeric_level)
    root_logger.addHandler(handler)
