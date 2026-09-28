"""Canonical CSV row ordering (L1-OUT-003, L2-WRT-021, L2-WRT-022).

Rows leave the decoder in a canonical order: ascending ``TIME_STAMP``, then
ascending ``RT``, then ``MSG`` — subaddress ascending and, within one
subaddress, receive (``R``) before transmit (``T``). The upstream stages already
deliver ascending timestamps (a single file is in chronological capture order; a
merge is heap-ordered by absolute IRIG microseconds), so this stage is only
responsible for the **tie**: it buffers each run of consecutive records sharing
one timestamp and stable-sorts that run by ``(rt, subaddress, direction)``.

Two properties make this safe over a streaming pipeline:

- It **never reorders across a timestamp boundary**. Only a run of consecutive
  equal timestamps is permuted, so a non-monotonic input (L2-MRG-006, which
  forbids re-sorting) is left exactly as it arrived: the stream ``T, T, U, T``
  sorts the leading pair and leaves the trailing ``T`` where it is.
- Buffering is capped by ``max_group`` (L2-WRT-022). A corrupt recording whose
  timestamps all decode to one value would otherwise buffer the whole file; at
  the cap the run is emitted in arrival order with one WARN and decoding
  continues.

``SPURIOUS_DATA`` records carry no Command Word, so they have no ``RT``/``MSG``
to sort on. They are **pinned**: excluded from the permutation and kept
immediately after the record they followed on input. That preserves the
adjacency the ``0x2000`` "continuation of a preceding error" code is defined in
terms of (L2-ERR-005) — separating a spurious record from its parent error would
leave that code describing nothing.

DELTA needs no recomputation downstream of this stage: it is tracked per
``RT``/``MSG`` key, and two records in one run that share a key also share a
timestamp, so their gap is zero regardless of their relative order. The reorder
is DELTA-invariant by construction.

Mirrors ``rust/src/order.rs`` (L3-PY-016 / L3-RS-016).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from aero1553.exceptions import Aero1553Error
from aero1553.models import IrigTimestamp, MieMessage

logger = logging.getLogger(__name__)

#: Default cap on one buffered equal-timestamp run (L2-WRT-022). Far above any
#: real tie — a 1553 bus carries one transaction at a time, so genuine ties come
#: from the two concurrent buses or from overlapping recorders in a merge —
#: while bounding worst-case buffering. Shared in value with Rust (L3-WRT-003).
DEFAULT_MAX_SORT_GROUP = 4096

#: Valid range for ``output.max_sort_group`` / ``--max-sort-group``
#: (L3-WRT-003). ``1`` disables reordering entirely (every run is already at the
#: cap), the supported way to restore raw DDC capture order for a vendor diff.
MAX_SORT_GROUP_MIN = 1
MAX_SORT_GROUP_MAX = 1_048_576

#: A chunk is one sortable "anchor" record plus the pinned (Command-Word-less)
#: records trailing it, keyed by the anchor's sort key (None for leading pins).
_Chunk = tuple[tuple[int, int, int] | None, list[MieMessage]]

#: Sort sentinel for a run's leading pinned records, which have no anchor to
#: travel with. RT, subaddress, and direction are all non-negative, so this sorts
#: ahead of every real key and keeps such records at the front of the run.
_LEADING_PIN_KEY = (-1, -1, -1)


def _sort_key(msg: MieMessage) -> tuple[int, int, int] | None:
    """``(rt, subaddress, direction)``, or None for a record with no Command
    Word (``SPURIOUS_DATA``), which is pinned rather than sorted.

    Keying on the decoded fields — not on the rendered ``MSG`` string — is
    deliberate: ``"11R"`` sorts before ``"2R"`` lexicographically, which is not
    the required order. :class:`~aero1553.models.Direction` is an
    ``IntEnum`` with ``RECEIVE = 0`` and ``TRANSMIT = 1``, so R-before-T falls
    out of the values and cannot drift from Rust's ``#[repr(u8)]``.

    Returns:
        ``(rt, subaddress, direction)`` for a record with a Command Word, or
        ``None`` for one without, which marks it as pinned rather than sorted.
    """
    cw = msg.command_word
    if cw is None:
        return None
    return (cw.rt, cw.subaddress, int(cw.direction))


def _group_value(msg: MieMessage) -> tuple[int, int]:
    """Timestamp identity for grouping: ``(variant_tag, value)``.

    The variant is part of the key so a mixed IRIG/Standard stream never
    compares equal. That cannot happen today — the format is resolved once per
    file and a merge rejects mixed sets — but comparing the value alone would be
    silently wrong if it ever could.

    Returns:
        ``(variant_tag, value)`` -- ``0`` and absolute microseconds for IRIG,
        ``1`` and the raw counter for Standard. Two records group together only
        if this compares equal.
    """
    ts = msg.timestamp
    if isinstance(ts, IrigTimestamp):
        return (0, ts.to_total_microseconds())
    # ``Timestamp`` is exactly ``IrigTimestamp | StandardTimestamp``, so this is
    # the Standard branch — no unreachable third case to leave untested.
    return (1, ts.raw_value)


def _sort_run(buf: list[MieMessage]) -> list[MieMessage]:
    """Stable-sort one buffered run, keeping each Command-Word-less record
    attached to the record it followed on input.

    The run is split into chunks — one anchor record plus any pinned records
    trailing it — and the *chunks* are sorted, so a pin travels with its anchor.
    Preserving a pin's index instead would let sorting move a different record
    in front of it, breaking exactly the ``0x2000`` error-continuation adjacency
    the pin exists to protect. Pins arriving before any anchor form a leading
    chunk keyed ``None``, which sorts ahead of every real key and so stays at the
    front of the run where it arrived.

    Returns:
        The run's messages in canonical order, with every pinned record still
        immediately behind the record it followed on input.
    """
    chunks: list[_Chunk] = []
    for msg in buf:
        key = _sort_key(msg)
        if key is not None:
            chunks.append((key, [msg]))
        elif chunks:
            chunks[-1][1].append(msg)
        else:
            chunks.append((None, [msg]))
    if len(chunks) > 1:
        # list.sort is stable, so chunks with a fully equal key keep their
        # relative input order (L1-OUT-003). A leading-pin chunk (key None) maps
        # to a sentinel below every real key — RT/SA/direction are all >= 0 — so
        # it stays at the front of the run. Mapping to a uniform tuple (rather
        # than a `(is_none, key)` pair) keeps the sort key type homogeneous for
        # the CI-gated strict mypy run.
        chunks.sort(key=lambda c: c[0] if c[0] is not None else _LEADING_PIN_KEY)
    return [msg for _, group in chunks for msg in group]


def order_rows(
    messages: Iterable[MieMessage],
    max_group: int = DEFAULT_MAX_SORT_GROUP,
) -> Iterator[MieMessage]:
    """Yield ``messages`` in canonical row order (L2-WRT-021).

    Wired as the outermost stage of the decode pipeline — after the merge and
    after filtering, immediately before the writer — so the ordering guarantee
    holds over exactly the rows that reach the CSV.

    A mid-stream decoder failure arrives as a **raised** exception rather than as
    an error value, so the buffered run is flushed from the ``except`` handler
    before the exception is re-raised: an ``--allow-partial`` run must still
    commit those rows to its ``.partial`` (L2-MRG-004). The flush deliberately
    does **not** live in a ``finally`` block — on an early consumer close the
    generator receives ``GeneratorExit``, and yielding while that propagates
    raises ``RuntimeError``; a consumer that closed early wants no further rows,
    so ``GeneratorExit`` simply discards the buffer.

    Args:
        messages: Decoded message stream, in ascending timestamp order.
        max_group: L2-WRT-022 cap on one buffered equal-timestamp run. Clamped
            up to ``MAX_SORT_GROUP_MIN`` defensively; config validation already
            rejects anything below it.

    Yields:
        The same messages, with each equal-timestamp run in canonical order.

    Raises:
        Aero1553Error: re-raised unchanged from the upstream stream, after the
            buffered run has been flushed so those rows still reach the writer.
            This generator raises nothing of its own.
    """
    cap = max(max_group, MAX_SORT_GROUP_MIN)
    buf: list[MieMessage] = []
    group: tuple[int, int] | None = None

    try:
        for msg in messages:
            value = _group_value(msg)
            if buf and value != group:
                yield from _sort_run(buf)
                buf = []
            if not buf:
                group = value
            buf.append(msg)
            if len(buf) >= cap:
                # L2-WRT-022: degrade to arrival order rather than buffer past
                # the cap, so a corrupt all-one-timestamp file stays decodable.
                logger.warning(
                    "equal-timestamp run at %s reached the %d-record max_sort_group cap; "
                    "emitting this run in arrival order (raise [output] max_sort_group / "
                    "--max-sort-group to restore canonical RT/MSG order for it)",
                    buf[0].timestamp.format(),
                    cap,
                )
                yield from buf
                buf = []
    except Aero1553Error:
        # Flush before re-raising so the rows already decoded reach the writer
        # ahead of the failure (an --allow-partial run commits them).
        if buf:
            yield from _sort_run(buf)
        raise

    if buf:
        yield from _sort_run(buf)
