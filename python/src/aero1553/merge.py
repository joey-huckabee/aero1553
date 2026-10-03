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

The merge is ``rust/src/merge.rs``, run through the package's compiled
extension, so a merged decode from Python is the CLI's merged decode (L3-PY-014).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aero1553 import _native
from aero1553.models import DeltaScope

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aero1553.models import MieMessage
    from aero1553.reader import MieFileReader

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
    return list(_native.read_manifest(Path(path)))


def glob_match(pattern: str, name: str) -> bool:
    """Whole-string wildcard match: ``*`` matches any run (incl. empty), ``?``
    matches exactly one character; no other metacharacters are special.

    Iterative backtracking matcher with identical semantics to the Rust
    implementation (L3-RS-014).

    Returns:
        ``True`` when the pattern matches the whole string.
    """
    return _native.glob_match(pattern, name)


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

    Raises:
        ValueError: the directory part holds a wildcard (``captures/**/*.mie``,
            ``capt*/a.mie``); refused before the filesystem is touched.
        OSError: the directory could not be enumerated.
    """
    return list(_native.expand_glob(pattern))


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
    return _native.merge_readers(
        # The compiled reader behind each MieFileReader. A package-internal
        # hand-off between two of this package's own modules; exposing it as a
        # public property would make it API for this one call.
        [reader._native for reader in readers],  # noqa: SLF001  # pylint: disable=protected-access
        standard_tick_rate_hz=standard_tick_rate_hz,
        allow_partial=allow_partial,
        strict=strict,
        collapse_duplicates=collapse_duplicates,
        collapse_window_us=collapse_window_us,
        max_collapse_survivors=max_collapse_survivors,
        delta_scope=int(delta_scope),
    )
