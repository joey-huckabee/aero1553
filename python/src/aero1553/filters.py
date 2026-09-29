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

from typing import TYPE_CHECKING

from aero1553 import _native

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from aero1553.config import FilterConfig
    from aero1553.models import MieMessage


def apply_filters(
    messages: Iterable[MieMessage],
    filters: FilterConfig,
) -> Iterator[MieMessage]:
    """Apply the exclude/include filters to a stream of decoded messages.

    Yields only the messages that match no active ``exclude_*`` set and that
    every active ``include_*`` set admits (see
    :meth:`~aero1553.config.FilterConfig.should_exclude`). With no filter
    active every message passes.

    Given the iterator of a reader or of another stage, filtering runs entirely
    in the compiled decoder; any other iterable of
    :class:`~aero1553.models.MieMessage` works too, record by record. Either way
    the input is consumed.

    Args:
        messages: Iterable of decoded MieMessage instances (typically
            from :class:`~aero1553.reader.MieFileReader`).
        filters: Filter configuration specifying which messages to
            exclude.

    Returns:
        An iterator of the messages that pass the filters.
    """
    return _native.apply_filters(
        messages,
        exclude_types=sorted(filters.exclude_types),
        exclude_rts=sorted(filters.exclude_rts),
        exclude_buses=sorted(int(bus) for bus in filters.exclude_buses),
        exclude_subaddresses=sorted(filters.exclude_subaddresses),
        include_types=sorted(filters.include_types),
        include_rts=sorted(filters.include_rts),
        include_buses=sorted(int(bus) for bus in filters.include_buses),
        include_subaddresses=sorted(filters.include_subaddresses),
    )
