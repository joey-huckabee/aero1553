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
  timestamps all decode to one value would otherwise buffer the whole file;
  each time the cap is reached the buffered records are emitted in arrival
  order with one WARN, the rest of the run is gathered (and sorted) as a new
  group, and decoding continues. A cap of ``1`` is the "off" switch and is
  silent.

``SPURIOUS_DATA`` records carry no Command Word, so they have no ``RT``/``MSG``
to sort on. They are **pinned**: excluded from the permutation and kept
immediately after the record they followed on input. That preserves the
adjacency the ``0x2000`` "continuation of a preceding error" code is defined in
terms of (L2-ERR-005) — separating a spurious record from its parent error would
leave that code describing nothing. A spurious record stamped *later* than the
record it followed opens the next run, so when sorting the current run would
move that record away from its end, the run is emitted in arrival order instead
(L2-WRT-021).

DELTA needs no recomputation downstream of this stage: it is tracked per
``RT``/``MSG`` key, and two records in one run that share a key also share a
timestamp, so their gap is zero regardless of their relative order. The reorder
is DELTA-invariant by construction.

The stage is ``rust/src/order.rs``, run through the package's compiled
extension (L3-PY-016 / L3-RS-016).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aero1553 import _native

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from aero1553.models import MieMessage

#: Default cap on one buffered equal-timestamp run (L2-WRT-022). Far above any
#: real tie — a 1553 bus carries one transaction at a time, so genuine ties come
#: from the two concurrent buses or from overlapping recorders in a merge —
#: while bounding worst-case buffering to about 10 MB. Only a broken clock
#: reaches it. Shared in value with Rust and C++ (L3-WRT-003).
DEFAULT_MAX_SORT_GROUP = 65_536

#: Valid range for ``output.max_sort_group`` / ``--max-sort-group``
#: (L3-WRT-003). ``1`` disables reordering entirely (every run is already at the
#: cap), the supported way to restore raw DDC capture order for a vendor diff.
MAX_SORT_GROUP_MIN = 1
MAX_SORT_GROUP_MAX = 1_048_576


def order_rows(
    messages: Iterable[MieMessage],
    max_group: int = DEFAULT_MAX_SORT_GROUP,
) -> Iterator[MieMessage]:
    """Yield ``messages`` in canonical row order (L2-WRT-021).

    Wired as the outermost stage of the decode pipeline — after the merge and
    after filtering, immediately before the writer — so the ordering guarantee
    holds over exactly the rows that reach the CSV.

    A mid-stream decoder failure is raised only after the buffered run has been
    flushed, so an ``--allow-partial`` run still commits those rows to its
    ``.partial`` (L2-MRG-004).

    Given the iterator of a reader or of another stage, the ordering runs
    entirely in the compiled decoder; any other iterable of
    :class:`~aero1553.models.MieMessage` works too, record by record. Either way
    the input is consumed.

    Args:
        messages: Decoded message stream, in ascending timestamp order.
        max_group: L2-WRT-022 cap on one buffered equal-timestamp run. Clamped
            up to ``MAX_SORT_GROUP_MIN`` defensively; config validation already
            rejects anything below it.

    Returns:
        An iterator of the same messages, with each equal-timestamp run in
        canonical order.
    """
    return _native.order_rows(messages, max(max_group, MAX_SORT_GROUP_MIN))
