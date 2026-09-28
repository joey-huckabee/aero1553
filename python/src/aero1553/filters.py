"""Message filtering for decoded MIE messages.

Provides a generator wrapper that filters decoded messages based on
:class:`~aero1553.config.FilterConfig` criteria. Filtering is
applied after decoding and before CSV output, so filtered messages
do not appear in the output and are not counted.

Usage::

    from aero1553.config import FilterConfig
    from aero1553.filters import apply_filters
    from aero1553.reader import MieFileReader

    config = FilterConfig(exclude_types={0x20})  # drop spurious data
    reader = MieFileReader("recording.mie")
    for msg in apply_filters(reader, config):
        print(msg.timestamp.format())
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from aero1553.config import FilterConfig
from aero1553.models import MieMessage

logger = logging.getLogger(__name__)


def apply_filters(
    messages: Iterable[MieMessage],
    filters: FilterConfig,
) -> Iterator[MieMessage]:
    """Apply exclusion filters to a stream of decoded messages.

    This is a generator wrapper that yields only messages not matching
    any exclusion criterion. If no filters are active, all messages
    pass through with zero overhead.

    Args:
        messages: Iterable of decoded MieMessage instances (typically
            from :class:`~aero1553.reader.MieFileReader`).
        filters: Filter configuration specifying which messages to
            exclude.

    Yields:
        MieMessage instances that do not match any exclusion criterion.
    """
    if not filters.is_active:
        logger.debug("No filters active, passing all messages through")
        yield from messages
        return

    _log_active_filters(filters)
    excluded_count = 0
    passed_count = 0

    for msg in messages:
        rt, subaddress = _rt_and_subaddress(msg)
        if filters.should_exclude(
            message_type=msg.type_word.message_type,
            rt=rt,
            bus=msg.type_word.bus,
            subaddress=subaddress,
        ):
            excluded_count += 1
            _log_filtered_out(msg, rt, subaddress)
            continue

        passed_count += 1
        yield msg

    logger.info(
        "Filter results: %d passed, %d excluded",
        passed_count,
        excluded_count,
    )


def _rt_and_subaddress(msg: MieMessage) -> tuple[int | None, int | None]:
    """Extract ``(rt, subaddress)`` from a message's Command Word, or
    ``(None, None)`` for SPURIOUS_DATA (no Command Word) so only type/bus
    filters can match it (mirrors the Rust filter) and no AttributeError.

    Returns:
        ``(rt, subaddress)`` read from the record's Command Word, or
        ``(None, None)`` when the record has no Command Word.
    """
    cw = msg.command_word
    if cw is None:
        return None, None
    return cw.rt, cw.subaddress


def _show_filter_set(values: object) -> str:
    """Render a filter set as a sorted ``[a, b]`` list, or ``none`` when empty.

    Sorted because these are Python ``set``s, whose iteration order is not
    guaranteed — an unsorted render makes the log line unstable between runs.
    Buses print as their names (``A`` / ``B``) rather than ``<Bus.A: 0>``, so
    the line matches the Rust `log_active_filters` output exactly.

    Returns:
        The members as ``[a, b]`` in sorted order, or the literal ``none`` when
        the set is empty.
    """
    items = list(values) if values else []  # type: ignore[call-overload]
    if not items:
        return "none"
    rendered = sorted(getattr(v, "name", None) or str(v) for v in items)
    return "[" + ", ".join(rendered) + "]"


def _log_active_filters(filters: FilterConfig) -> None:
    """Emit the one-time INFO summary of the active exclude/include sets."""
    logger.info(
        "Filtering active: exclude_types=%s exclude_rts=%s "
        "exclude_buses=%s exclude_subaddresses=%s "
        "include_types=%s include_rts=%s "
        "include_buses=%s include_subaddresses=%s",
        _show_filter_set(filters.exclude_types),
        _show_filter_set(filters.exclude_rts),
        _show_filter_set(filters.exclude_buses),
        _show_filter_set(filters.exclude_subaddresses),
        _show_filter_set(filters.include_types),
        _show_filter_set(filters.include_rts),
        _show_filter_set(filters.include_buses),
        _show_filter_set(filters.include_subaddresses),
    )


def _log_filtered_out(msg: MieMessage, rt: int | None, subaddress: int | None) -> None:
    """DEBUG line for a message dropped by the filters."""
    logger.debug(
        "Filtered out: offset=0x%X type=0x%02X RT%s SA%s Bus %s",
        msg.file_offset,
        msg.type_word.message_type,
        rt if rt is not None else "-",
        subaddress if subaddress is not None else "-",
        msg.type_word.bus.name,
    )
