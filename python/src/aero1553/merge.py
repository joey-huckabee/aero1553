"""Multi-file, time-sorted streaming k-way merge (L1-MRG-*, L2-MRG-*).

Accepts several decoded recordings and yields one stream of ``MieMessage``s in
global time order, holding at most one record per open file in a min-heap
(resident memory O(number of files), independent of total record count —
L2-MRG-002). The merged stream feeds the existing ``write_csv`` /
``write_csv_split`` unchanged.

Merge requires every input to be calendar-locked IRIG; Standard-format,
freerun-leading, or mixed-format inputs are rejected up front
(:class:`MieIncompatibleMergeInputsError`, CLI exit 6 — L2-MRG-003). DELTA is
measured **per input file** by default — each reader already computed it for its
own file, so this module leaves it alone and a merged record's value equals its
single-file value by construction. ``--delta-scope global`` recomputes it across
the merged timeline instead (L2-MRG-005).

Mirrors ``rust/src/merge.rs``; the heap is the standard-library :mod:`heapq` and the
``--glob`` matcher is hand-rolled to the same single-directory ``*``/``?``
semantics as Rust (L3-PY-014) — no new dependency.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import os
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from aero1553.delta import DeltaTracker
from aero1553.exceptions import (
    Aero1553Error,
    MieIncompatibleMergeInputsError,
    MieNonMonotonicInputError,
    MieUnrecoverableSyncLossError,
)
from aero1553.models import DeltaScope, IrigTimestamp, MieMessage, StandardTimestamp
from aero1553.reader import MieFileReader

logger = logging.getLogger(__name__)

#: Maximum number of input files a single merge invocation may process. Bounds
#: open mappings / file descriptors; exceeding it is a usage error (the CLI
#: maps it to exit 4). Shared in value with the Rust constant (L3-PY-014).
MAX_MERGE_FILES = 256

MAX_COLLAPSE_SURVIVORS_MIN = 1
"""Smallest accepted ``merge.max_collapse_survivors`` (L2-MRG-008)."""

MAX_COLLAPSE_SURVIVORS_MAX = 1048576
"""Largest accepted ``merge.max_collapse_survivors`` (L2-MRG-008)."""

DEFAULT_MAX_COLLAPSE_SURVIVORS = 4096
"""Default cap on the de-duplication survivor set (L2-MRG-008).

Far above any genuine population of one collapse window -- a 1553 bus carries
one transaction at a time, so a window holds one record per recorder per
transaction -- while bounding worst-case retention to a few hundred kilobytes.
Matches ``DEFAULT_MAX_SORT_GROUP`` deliberately: the two caps guard the same
class of pathological input, and an operator who has reasoned about one should
not have to re-derive the other.
"""


# ── Input resolution helpers ──────────────────────────────────────────────


def read_manifest(path: str | Path) -> list[Path]:
    """Read a manifest into a list of paths: one path per line, in order.

    Blank lines and lines whose first non-whitespace character is ``#`` are
    ignored; surrounding whitespace is trimmed (L2-MRG-001).

    Returns:
        The listed paths in file order. An **empty list is not an error**: a
        manifest with no usable lines returns ``[]``, which is what lets the CLI
        distinguish "listed nothing" from "could not be read".
    """
    # `newline=""` disables universal-newline translation. Without it Python
    # rewrites a lone `\r` -- and `\r\n` -- to `\n` before this code sees the
    # text, so a filename containing a bare carriage return silently became two
    # paths here while Rust's `fs::read_to_string` and the C++ reader, neither
    # of which translates, kept it as one (L2-MRG-001).
    #
    # `Path.read_text` grew a `newline` parameter only in 3.13; this package
    # supports 3.10, so the stream is opened explicitly.
    with Path(path).open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    out: list[Path] = []
    # `split("\n")`, NOT `splitlines()`. `splitlines()` also breaks on vertical
    # tab, form feed, file/group/record separator, U+0085, U+2028 and U+2029 --
    # none of which terminates a line in any manifest anyone writes, and all of
    # which are legal in a POSIX filename. Rust's `str::lines()` and the C++
    # reader both split on `\n` alone, so a manifest naming one file containing
    # a form feed resolved to two nonexistent files here and one real file
    # there (L2-MRG-001).
    for raw in text.split("\n"):
        # Strip at most one trailing carriage return -- that is what a CRLF
        # line ending is -- then ASCII space and tab only. `str.strip()` removes
        # Unicode whitespace, which the locale-free C++ implementation cannot
        # do without embedding a table; see the note in `rust/src/merge.rs`.
        line = raw[:-1] if raw.endswith("\r") else raw
        trimmed = line.strip(" \t")
        if not trimmed or trimmed.startswith("#"):
            continue
        out.append(Path(trimmed))
    return out


def glob_match(pattern: str, name: str) -> bool:
    """Whole-string wildcard match: ``*`` matches any run (incl. empty), ``?``
    matches exactly one character; no other metacharacters are special.

    Iterative backtracking matcher with identical semantics to the Rust
    implementation (L3-RS-014).

    Returns:
        ``True`` when the pattern matches the whole string.
    """
    p = t = 0
    star: int | None = None
    mark = 0
    while t < len(name):
        if p < len(pattern) and pattern[p] in ("?", name[t]):
            p += 1
            t += 1
        elif p < len(pattern) and pattern[p] == "*":
            star = p
            mark = t
            p += 1
        elif star is not None:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def expand_glob(pattern: str) -> list[Path]:
    """Expand a single-directory glob ``DIR/PATTERN`` (or ``PATTERN`` for the
    current directory). Wildcards apply to the filename only — no recursive
    ``**``, no brace expansion.

    "Regular file" is decided **after following symlinks** (the default for
    ``DirEntry.is_file``), so a recording reached through a symlink is a
    recording. A dangling symlink answers ``False`` and is skipped, which is
    also what a broken link deserves: the merge would only fail to open it a
    moment later. Directories -- including one named ``archive.mie`` -- are never
    matched. All three implementations resolve the same set (L2-MRG-001).

    Returns:
        Matching regular files, sorted lexicographically so the order is
        deterministic across implementations (L2-MRG-001). An **empty list is
        not an error**: a pattern that matches nothing returns ``[]``, leaving
        "matched no files" distinguishable from "could not read the directory".
    """
    p = Path(pattern)
    name_pat = p.name
    directory = p.parent if str(p.parent) else Path()
    out: list[Path] = []
    with os.scandir(directory) as it:
        for entry in it:
            # Name test before the stat: a directory of thousands of entries
            # should cost one stat per *match*, not one per entry.
            if not glob_match(name_pat, entry.name):
                continue
            if not entry.is_file():
                continue
            out.append(Path(entry.path))
    out.sort(key=str)
    return out


# ── k-way merge ────────────────────────────────────────────────────────────


def _merge_micros(msg: MieMessage, tick: float | None) -> int:
    """Microsecond merge key. IRIG always yields an int; the 0 fallback is
    unreachable for a validated (IRIG-only) merge.

    Returns:
        The record's absolute microseconds, or ``0`` on the unreachable
        fallback.
    """
    us = msg.timestamp.to_microseconds(tick)
    return 0 if us is None else us


def _check_mergeable(msg: MieMessage, file_index: int, path: Path) -> None:
    """Reject an input whose leading record cannot anchor an absolute timeline.

    Raises:
        MieIncompatibleMergeInputsError: if the input resolves to the Standard
            timestamp format, or leads with a freerun IRIG record. Either way it
            has no calendar-locked timeline to merge against (L2-MRG-003).
    """
    ts = msg.timestamp
    if isinstance(ts, StandardTimestamp):
        raise MieIncompatibleMergeInputsError(
            file_index, str(path), "resolves to the Standard timestamp format"
        )
    if isinstance(ts, IrigTimestamp) and ts.freerun:
        raise MieIncompatibleMergeInputsError(
            file_index,
            str(path),
            "leads with a freerun IRIG record (no calendar time)",
        )


def _apply_global_delta(msg: MieMessage, tracker: DeltaTracker) -> MieMessage:
    """Recompute DELTA on the merged global timeline (L2-MRG-005).

    SPURIOUS_DATA (no Command Word), an uncalibrated Standard counter, and a
    backward step all resolve to an empty cell, and the tracker tells them apart
    so it can keep the right cursor for each.

    No WARN here: a backward step on the merged timeline means an input was not
    internally sorted, which the merge already reports once per file
    (L2-MRG-006). Repeating it per record would bury that.

    Args:
        msg: The record popped from the merge heap.
        tracker: Global-scope DELTA state, shared in kind with the reader's so
            the merged timeline and a single-file decode answer "the same
            RT/MSG" identically by construction.

    Returns:
        A copy of ``msg`` carrying the recomputed DELTA.
    """
    return msg.with_delta(tracker.observe(msg.command_word, msg.timestamp).value)


def _dedup_key(msg: MieMessage) -> tuple[object, ...]:
    """Content identity of a message for cross-recorder de-duplication
    (L2-MRG-007): the bits a recorder reads off the wire — Type Word, Command /
    Status Words, Error Word, and the data words. Timestamp, file offset, MUX,
    and DELTA are intentionally excluded — the timestamp drives the window (not
    equality), and the rest are per-recorder. Mirrors ``DedupKey`` in Rust.

    Returns:
        A hashable tuple of exactly the wire content. Two messages with equal
        keys are the same bus transaction, whatever their timestamps.
    """
    return (
        msg.type_word,
        msg.command_word,
        msg.command_word_2,
        msg.status_word,
        msg.status_word_2,
        msg.error_word,
        msg.data_words,
    )


class _DedupWindow:
    """Sliding time-window de-duplicator over the merged stream (L2-MRG-007),
    with the survivor-set cap of L2-MRG-008.

    Retention is defined on the **absolute** time distance to the current record
    and is therefore independent of the order survivors were appended in: a
    survivor is kept iff ``abs(survivor_us - us) <= window_us``. That order
    independence is the requirement, not an optimisation. This used to evict only
    from the FRONT, testing the one-sided ``us - front_us``, which is correct
    only while the stream is sorted. After a lenient backward step (L2-MRG-006)
    the front can hold a timestamp in the *future* of the current record, the
    one-sided test is then never true, and the front never leaves — blocking
    eviction of everything behind it. An alternating 1000us / 0us probe with a
    zero-width window retained all 10 000 records and ran quadratically (2x the
    records, 4x the time), contradicting the bounded-memory guarantee L2-MRG-007
    makes and L2-MRG-002 depends on.

    The window bounds retention in TIME; it does not bound it in COUNT. A corrupt
    recording whose timestamps all decode to one value, or a wide operator-set
    ``collapse_window_us`` on a dense bus, puts arbitrarily many records inside
    one window. ``max_survivors`` is the second, independent bound that makes the
    guarantee unconditional — the same reasoning, and the same default, as
    ``max_sort_group`` (L2-WRT-022) applies to the reorder stage.

    Mirrors ``DedupWindow`` in ``rust/src/merge.rs`` and ``cpp/src/merge.cpp``.
    """

    def __init__(self, window_us: int, max_survivors: int = DEFAULT_MAX_COLLAPSE_SURVIVORS) -> None:
        self._window_us = window_us
        self._max_survivors = max(max_survivors, MAX_COLLAPSE_SURVIVORS_MIN)
        self._survivors: deque[tuple[int, int, tuple[object, ...]]] = deque()
        #: One WARN per merge, not per capped record: a pathological input hits
        #: the cap on nearly every record, and the cadence that matters to an
        #: operator is "this run stopped being exact", once. Mirrors the
        #: one-WARN-per-input cadence L2-MRG-006 uses for the same reason.
        self._capped_warned = False

    def is_duplicate(self, us: int, file_index: int, msg: MieMessage) -> bool:
        """True if ``msg`` (at ``us``, from ``file_index``) duplicates a recent
        survivor from a *different* input within the window — the same bus
        transaction witnessed by another recorder. Same-file identical content is
        never a duplicate; a non-duplicate is recorded as a survivor.

        Returns:
            ``True`` if this message duplicates a recent survivor from another
            input and should be collapsed. ``False`` otherwise -- and in that
            case ``msg`` has been recorded as a survivor itself.
        """
        # Evict on absolute distance, over the whole set rather than the front.
        # This costs one pass, which is what the match scan below already costs,
        # so the sorted-stream case is no slower than the front-only eviction it
        # replaces — and the unsorted case is now bounded at all.
        window = self._window_us
        self._survivors = deque(s for s in self._survivors if abs(s[0] - us) <= window)

        key = _dedup_key(msg)
        # Every retained survivor is already within the window, so the match test
        # is content and provenance only.
        for _survivor_us, file_idx, survivor_key in self._survivors:
            if file_idx != file_index and survivor_key == key:
                return True

        # L2-MRG-008: make room rather than grow. Dropping the oldest ARRIVAL is
        # the honest choice once the window itself is over-full — there is no
        # "least useful" survivor to pick, and arrival order is the one ordering
        # that is meaningful on input this badly behaved. Records are never
        # dropped from the OUTPUT; only the ability to recognise a later
        # duplicate of this one is given up.
        while len(self._survivors) >= self._max_survivors:
            self._survivors.popleft()
            if not self._capped_warned:
                self._capped_warned = True
                logger.warning(
                    "de-duplication survivor set hit the %d-record "
                    "max_collapse_survivors cap; collapsing is best-effort past "
                    "this point (raise [merge] max_collapse_survivors / "
                    "--max-collapse-survivors, or narrow --collapse-window-us)",
                    self._max_survivors,
                )
        self._survivors.append((us, file_index, key))
        return False


def merge_readers(
    readers: list[MieFileReader],
    *,
    standard_tick_rate_hz: float | None = None,
    allow_partial: bool = False,
    strict: bool = False,
    collapse_duplicates: bool = False,
    collapse_window_us: int = 0,
    max_collapse_survivors: int = DEFAULT_MAX_COLLAPSE_SURVIVORS,
    delta_scope: DeltaScope = DeltaScope.PER_FILE,
) -> Iterator[MieMessage]:
    """Stream a time-sorted k-way merge over ``readers``.

    Validation of each input's leading record (L2-MRG-003) happens **eagerly**
    when this is called — so an incompatible set raises
    :class:`MieIncompatibleMergeInputsError` before any output is written,
    matching the Rust reader. The returned iterator then yields ``MieMessage``s
    in global time order so the existing writer consumes them unchanged. With
    ``allow_partial`` a file that fails is skipped / truncated with a WARN and
    the merge completes, deferring the terminal
    :class:`MieUnrecoverableSyncLossError` so the writer commits a ``.partial``
    (L2-MRG-004). The heap key ``(microseconds, file index, sequence)`` gives a
    deterministic total order (L2-MRG-002). A within-file backward timestamp
    step (L2-MRG-006) WARNs once per file in lenient mode and raises
    :class:`MieNonMonotonicInputError` in ``strict`` mode.

    Returns:
        An iterator over the merged stream in global time order.

    Raises:
        MieIncompatibleMergeInputsError: **eagerly, before any output**, if any
            input is not calendar-locked IRIG (exit 6).
        MieNonMonotonicInputError: in ``strict`` mode only, on a backward
            timestamp step within one input.
        MieUnrecoverableSyncLossError: from an input that loses sync. Under
            ``allow_partial`` this is deferred until the heap drains, so the
            writer still commits a ``.partial``.
        Aero1553Error: any other decoder failure from an underlying reader,
            propagated unchanged.
    """
    iters = [iter(r) for r in readers]
    seqs = [0] * len(readers)
    counter = itertools.count()
    # Microsecond key of the previous record pulled from each file, in capture
    # order, plus a one-time WARN guard — for L2-MRG-006 backward-step detection.
    prev_us: list[int | None] = [None] * len(readers)
    warned: list[bool] = [False] * len(readers)
    # Heap items: (us, file_index, seq, tiebreak_counter, msg). The unique
    # counter guarantees msg is never compared.
    heap: list[tuple[int, int, int, int, MieMessage]] = []
    # A priming-time failure under allow_partial arms this terminal so the writer
    # commits a `.partial` (L2-MRG-004), exactly like a mid-file failure. The file
    # contributed no records (truncated at offset 0).
    priming_terminal: MieUnrecoverableSyncLossError | None = None

    # Prime + validate eagerly (before any output).
    for idx, it in enumerate(iters):
        try:
            msg = next(it)
        except StopIteration:
            continue  # empty file contributes nothing
        except Aero1553Error as exc:
            if allow_partial:
                logger.warning(
                    "merge: input #%d (%s) could not be read; truncating it "
                    "from the merge (--allow-partial): %s",
                    idx,
                    readers[idx].path,
                    exc,
                )
                priming_terminal = MieUnrecoverableSyncLossError(0, 0)
                continue
            raise
        _check_mergeable(msg, idx, readers[idx].path)
        us = _merge_micros(msg, standard_tick_rate_hz)
        prev_us[idx] = us
        heapq.heappush(heap, (us, idx, 0, next(counter), msg))
        seqs[idx] = 1

    dedup = (
        _DedupWindow(collapse_window_us, max_collapse_survivors) if collapse_duplicates else None
    )
    return _merge_drain(
        readers,
        iters,
        seqs,
        counter,
        heap,
        standard_tick_rate_hz,
        allow_partial,
        strict,
        prev_us,
        warned,
        dedup,
        priming_terminal,
        delta_scope,
    )


def _resolve_emission(
    msg: MieMessage,
    us: int,
    idx: int,
    dedup: _DedupWindow | None,
    delta_scope: DeltaScope,
    tracker: DeltaTracker,
) -> MieMessage | None:
    """The record to emit for this heap pop, or ``None`` if it is a collapsed
    cross-recorder duplicate.

    De-duplication runs *before* the DELTA stage (L2-MRG-007) so a suppressed
    duplicate never advances the per-key tracker and gaps are measured across the
    surviving stream. Under ``PER_FILE`` (the default) the DELTA each reader
    already computed for its own file is left exactly as-is, which is what makes
    a merged record's value identical to a single-file decode (L2-MRG-005).

    Returns:
        The message to emit -- carrying a recomputed DELTA under ``GLOBAL``, or
        untouched under ``PER_FILE`` -- or ``None`` when it was collapsed as a
        cross-recorder duplicate and must not be emitted at all.
    """
    if dedup is not None and dedup.is_duplicate(us, idx, msg):
        return None
    if delta_scope == DeltaScope.GLOBAL:
        return _apply_global_delta(msg, tracker)
    return msg


def _pull_next(
    readers: list[MieFileReader],
    iters: list[Iterator[MieMessage]],
    idx: int,
    tick: float | None,
    prev_us: list[int | None],
    warned: list[bool],
    strict: bool,
    allow_partial: bool,
) -> tuple[MieMessage | None, int | None, MieUnrecoverableSyncLossError | None]:
    """Advance input ``idx`` by one record for the heap.

    Returns ``(record, microsecond key, deferred terminal)``. The record and key
    are both ``None`` when the input is exhausted or was truncated. A terminal is
    returned rather than raised only under ``--allow-partial``, so the caller can
    defer it until the heap drains and the writer still commits a `.partial`
    (L2-MRG-004); in strict mode it propagates.

    Monotonicity is checked here, against the caller's *previous* key, before the
    caller records the new one (L2-MRG-006).

    Returns:
        ``(record, microsecond key, deferred terminal)``. The first two are both
        ``None`` when the input is exhausted or truncated. The third is
        non-``None`` only under ``--allow-partial``.

    Raises:
        MieUnrecoverableSyncLossError: in strict mode, when this input loses
            sync. Under ``--allow-partial`` it is returned instead of raised.
    """
    try:
        nxt = next(iters[idx])
    except StopIteration:
        return None, None, None  # file exhausted
    except MieUnrecoverableSyncLossError as exc:
        if allow_partial:
            logger.warning("merge: input #%d truncated at its failure point: %s", idx, exc)
            return None, None, exc
        raise
    curr = _merge_micros(nxt, tick)
    _check_monotonic_input(readers, idx, prev_us[idx], curr, strict, warned)
    return nxt, curr, None


def _merge_drain(
    readers: list[MieFileReader],
    iters: list[Iterator[MieMessage]],
    seqs: list[int],
    counter: itertools.count[int],
    heap: list[tuple[int, int, int, int, MieMessage]],
    tick: float | None,
    allow_partial: bool,
    strict: bool,
    prev_us: list[int | None],
    warned: list[bool],
    dedup: _DedupWindow | None,
    pending_terminal: MieUnrecoverableSyncLossError | None = None,
    delta_scope: DeltaScope = DeltaScope.PER_FILE,
) -> Iterator[MieMessage]:
    """Drain the primed heap: pop the min, optionally collapse cross-recorder
    duplicates, recompute DELTA when ``delta_scope`` is ``global`` (per-file
    keeps each reader's own value), advance the file that record came from.

    ``pending_terminal`` carries a priming-time --allow-partial failure (set in
    ``merge_readers``); a mid-file failure may overwrite it. Either is raised
    after the heap drains so the writer commits a `.partial` (L2-MRG-004).

    Yields:
        Merged messages in global time order, one per heap pop that survives
        de-duplication.

    Raises:
        MieUnrecoverableSyncLossError: the deferred ``pending_terminal``, raised
            only once the heap has drained.
    """
    tracker = DeltaTracker(tick)
    collapsed = 0
    while heap:
        us, idx, _, _, msg = heapq.heappop(heap)
        emitted = _resolve_emission(msg, us, idx, dedup, delta_scope, tracker)
        if emitted is None:
            collapsed += 1
        else:
            yield emitted

        nxt, curr, deferred = _pull_next(
            readers, iters, idx, tick, prev_us, warned, strict, allow_partial
        )
        if deferred is not None:
            pending_terminal = deferred  # defer until the heap drains
            continue
        if nxt is None or curr is None:
            continue  # input exhausted; the two are set or unset together

        prev_us[idx] = curr
        seq = seqs[idx]
        seqs[idx] = seq + 1
        # next() on the infinite itertools.count() tiebreak never raises.
        tiebreak = next(counter)  # pylint: disable=stop-iteration-return
        heapq.heappush(heap, (curr, idx, seq, tiebreak, nxt))

    if collapsed:
        logger.info("merge: collapsed %d duplicate message(s) across recorders", collapsed)
    # An --allow-partial deferred failure surfaces here so the writer commits a
    # `.partial` after all good records are written.
    if pending_terminal is not None:
        raise pending_terminal


def _check_monotonic_input(
    readers: list[MieFileReader],
    idx: int,
    prev: int | None,
    curr: int,
    strict: bool,
    warned: list[bool],
) -> None:
    """L2-MRG-006: each input is assumed internally time-sorted (capture order is
    chronological). A backward step means the merged output may be out of order
    for this file — strict fails the batch, lenient WARNs once per file.

    Raises:
        MieNonMonotonicInputError: in ``strict`` mode only. Lenient mode logs one
            WARN per file and returns normally.
    """
    if prev is None or curr >= prev:
        return
    if strict:
        raise MieNonMonotonicInputError(idx, str(readers[idx].path), prev, curr)
    if not warned[idx]:
        warned[idx] = True
        logger.warning(
            "merge: input #%d (%s) is not internally time-sorted: "
            "timestamp stepped backward (prev_us=%d curr_us=%d) -- "
            "merged output may be out of order for this input "
            "(further occurrences suppressed)",
            idx,
            readers[idx].path,
            prev,
            curr,
        )
