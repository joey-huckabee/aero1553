"""Per-RT/MSG ``DELTA`` tracking — the one definition of "the same message".

Why this is its own module
--------------------------

``DELTA`` used to be computed in three places with the key spelled three ways.
:mod:`aero1553.reader` called ``_compute_delta`` twice: once with
``msg.delta_key`` and once with a hand-built ``f"{cmd.rt}:{cmd.subaddress}{c}"``,
because on the errored-record path the message does not exist yet.
:mod:`aero1553.merge` kept its own tracker, also keyed by ``msg.delta_key``.
Three spellings of one concept, and nothing asserted that the hand-built string
matched the property it was standing in for — a mismatch would have made
``DELTA`` mean one thing for clean records and another for errored ones, in the
same file, silently.

The arithmetic was duplicated with it, and had already drifted: the reader warns
once per key when a clock steps backwards, and the merge path did the same
computation silently.

This module does not log
-----------------------

:meth:`DeltaTracker.observe` returns a :class:`DeltaOutcome` describing what
happened and the caller decides whether to say anything — the same rule
:mod:`aero1553.sync` follows, for the same reason. A tracker cannot know
whether a backward step is worth a WARN (single-file decode: yes, once per key)
or is already reported at file granularity (a merge naming its unsorted inputs,
L2-MRG-006).

The "have I already mentioned this key" bookkeeping *is* kept here, so the
once-per-key promise has one owner rather than a set in each caller.

Mirrors ``rust/src/delta.rs`` and ``cpp/src/delta.cpp``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from aero1553 import _native
from aero1553.models import CommandWord, Timestamp

__all__ = ["DeltaKind", "DeltaOutcome", "DeltaTracker", "delta_key"]


def delta_key(rt: int, subaddress: int, transmit: bool) -> int:
    """Pack ``(rt, subaddress, direction)`` into one integer key.

    **This is the definition of "the same message" for DELTA purposes.** The
    three fields are 5, 5 and 1 bits on the wire, so the packing is lossless
    with room to spare.

    :attr:`~aero1553.models.MieMessage.delta_key` is the display spelling of
    this same tuple. ``test_delta.py`` asserts the two partition the
    ``(rt, subaddress, direction)`` space identically — the check that could not
    exist while the representations lived in different modules.

    Returns:
        The three fields packed into one ``int``: ``rt`` at bit 16,
        ``subaddress`` at bit 8, and the transmit flag in bit 0.
    """
    return _native.delta_key(rt, subaddress, transmit)


class DeltaKind(IntEnum):
    """Which of the five things one observation turned out to be."""

    #: First sighting of this key. DELTA is ``0.000000`` (L2-RDR-010).
    FIRST = 0
    #: A non-negative gap, carried in :attr:`DeltaOutcome.seconds`.
    ELAPSED = 1
    #: The clock went backwards for this key (L2-RDR-017).
    BACKWARD = 2
    #: Standard timestamp with no configured tick rate (L2-RDR-019).
    UNCALIBRATED = 3
    #: No RT/MSG key at all — SPURIOUS_DATA (L2-RDR-018).
    NO_KEY = 4


@dataclass(frozen=True)
class DeltaOutcome:
    """What one observation meant.

    Deliberately richer than the ``float | None`` the CSV eventually needs,
    because the column cannot distinguish "no gap yet" from "no honest gap" from
    "no key at all" — and the caller has to, in order to narrate correctly.

    Attributes:
        kind: Which outcome this is.
        seconds: The gap, for :attr:`DeltaKind.ELAPSED` only.
        prev_us: Previous timestamp, for :attr:`DeltaKind.BACKWARD` only.
        curr_us: Current timestamp, for :attr:`DeltaKind.BACKWARD` only.
        key: The packed key, for :attr:`DeltaKind.BACKWARD` only — so a caller
            can name it in a diagnostic without re-deriving it.
        first_for_key: True exactly once per key per tracker, so a caller that
            warns on a backward step gets one line per key rather than one per
            record.
    """

    kind: DeltaKind
    seconds: float | None = None
    prev_us: int | None = None
    curr_us: int | None = None
    key: int | None = None
    first_for_key: bool = False

    @property
    def value(self) -> float | None:
        """The value the ``DELTA`` column takes.

        Where four of the five outcomes collapse into an empty cell.
        """
        if self.kind is DeltaKind.FIRST:
            return 0.0
        if self.kind is DeltaKind.ELAPSED:
            return self.seconds
        return None


class DeltaTracker:
    """Last-seen timestamp per RT/MSG key, and the gap arithmetic over it.

    One instance per DELTA scope: the reader makes one per file, and the merge
    makes one for the whole merged timeline under ``--delta-scope global``
    (L2-MRG-005).
    """

    __slots__ = ("_native",)

    def __init__(self, tick_rate_hz: float | None = None) -> None:
        """Create an empty tracker.

        Args:
            tick_rate_hz: L2-DEC-017 Standard-counter calibration. ``None``
                keeps Standard records out of tracking entirely.
        """
        self._native = _native.DeltaTracker(tick_rate_hz)

    def observe(self, command_word: CommandWord | None, timestamp: Timestamp) -> DeltaOutcome:
        """Record one message and report the gap since the previous one with its key.

        ``command_word`` is ``None`` for SPURIOUS_DATA, which has no key. Taking
        the Command Word rather than a whole message is what lets the reader call
        this *before* the message exists — on the errored-record path the DELTA
        is computed and then handed to the constructor. That is exactly where the
        hand-built key string used to come from.

        Args:
            command_word: The record's Command Word, or ``None``.
            timestamp: The record's timestamp.

        Returns:
            What the observation meant; see :class:`DeltaOutcome`.
        """
        kind, seconds, prev_us, curr_us, key, first_for_key = self._native.observe(
            command_word, timestamp
        )
        return DeltaOutcome(
            kind=DeltaKind(kind),
            seconds=seconds,
            prev_us=prev_us,
            curr_us=curr_us,
            key=key,
            first_for_key=first_for_key,
        )
