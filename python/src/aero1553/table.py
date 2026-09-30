"""Decoded records as columns, for NumPy and pandas.

:func:`columns` turns a whole record stream -- a reader, a filtered or ordered
stream, or a merge -- into one typed buffer per field, built in Rust with no
Python object per record. NumPy and pandas wrap each buffer without copying::

    import numpy as np
    import pandas as pd
    from aero1553 import MieFileReader, columns

    cols = columns(MieFileReader("recording.mie"))
    rt = np.asarray(cols["rt"])                      # int8
    words = np.asarray(cols["data_words"])           # (n, 32) uint16
    df = pd.DataFrame({k: v for k, v in cols.items() if k != "data_words"})

For one record at a time, :meth:`MieMessage.to_dict` returns the same fields
as plain Python values, for dataclasses or JSON.

Absent values
-------------
A numeric buffer has no ``None``, so a column follows NumPy's conventions: an
absent integer is ``-1``, an absent float is ``NaN``, and an absent
``datetime`` is NumPy's ``NaT``. ``to_dict`` says ``None`` for all of them.

Time
----
Time follows the decoder's existing rules rather than new ones:

``timestamp``
    The ``TIME_STAMP`` text of the CSV (``DAY:HH:MM:SS.uuuuuu``).
``time_us``
    Microseconds by the DELTA rule (L2-DEC-017): always for an IRIG record
    (counted from day 0 of the IRIG day-of-year clock, so day 1 is
    86 400 000 000); for a Standard record only when
    ``standard_tick_rate_hz`` is given, since the file does not record the
    counter's rate.
``ticks``
    The raw Standard counter; absent for IRIG.
``freerun``, ``day_of_year``, ``time_of_day_us``
    The IRIG clock reading as stored; absent for Standard. ``freerun`` means
    the recorder's clock was not locked to a time source.
``datetime``
    Present only when ``year`` is given -- the file stores the day of the year
    but not the year, and without the year even the month is unknown (day 60
    is 1 March in 2026 and 29 February in 2024). The column holds
    microseconds since 1970-01-01T00:00Z; view it as
    ``np.asarray(cols["datetime"]).view("datetime64[us]")``. ``to_dict`` gives
    a timezone-aware :class:`datetime.datetime`. ``utc_offset_minutes`` is the
    recorder clock's offset from UTC, as ``--utc-offset`` is for the CLI.
    A record with no date is ``NaT``: a freerun or Standard record, or day 366
    of a common year. These are the calendar rules of the CLI's ISO rendering
    (L2-WRT-026), which refuses the whole CSV where a column marks the record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aero1553 import _native

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from aero1553.models import MieMessage

#: Every field :func:`columns` and :meth:`MieMessage.to_dict` can return, in
#: column order. ``datetime`` is returned only when a year is given.
FIELDS: tuple[str, ...] = tuple(_native.table_fields())


def columns(
    messages: Iterable[MieMessage],
    *,
    fields: Sequence[str] | None = None,
    standard_tick_rate_hz: float | None = None,
    year: int | None = None,
    utc_offset_minutes: int = 0,
) -> dict[str, Any]:
    """Every record of ``messages`` as one buffer per field.

    Args:
        messages: A :class:`~aero1553.MieFileReader`, the stream of
            :func:`~aero1553.filters.apply_filters`,
            :func:`~aero1553.order.order_rows` or
            :func:`~aero1553.merge.merge_readers`, or any iterable of
            :class:`~aero1553.MieMessage`. It is consumed.
        fields: The fields to return, from :data:`FIELDS`. By default every
            field, less ``datetime`` when no ``year`` is given. The text
            fields (``timestamp``, ``msg_label``, ``mux``) are the only ones
            that create a Python object per record; leave them out for speed.
        standard_tick_rate_hz: The Standard counter's rate, which gives
            Standard records a ``time_us`` (the same setting as the reader's).
        year: The calendar year of the recording; adds ``datetime``.
        utc_offset_minutes: The recorder clock's offset from UTC, in minutes.

    Returns:
        A dict, in :data:`FIELDS` order, of a typed :class:`memoryview` per
        numeric field and a :class:`list` per text field. ``data_words`` is
        ``(n, 32)``, zero-padded (``data_word_count`` says how many of each
        row are real); for an empty stream it is an empty 1-D buffer, since a
        :class:`memoryview` cannot have a zero in its shape --
        ``np.asarray(cols["data_words"]).reshape(-1, 32)`` is ``(n, 32)``
        either way.

    Raises:
        ValueError: an unknown field, ``datetime`` asked for without a year,
            or an option out of range.
    """
    return dict(
        _native.columns(
            iter(messages),
            fields=fields,
            standard_tick_rate_hz=standard_tick_rate_hz,
            year=year,
            utc_offset_minutes=utc_offset_minutes,
        )
    )
