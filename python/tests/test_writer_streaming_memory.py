"""Constant-memory verification for the streaming writer (L3-PY-012).

Proves Python decode memory is O(1) in the record count — the property
PY-streaming exists to deliver — by measuring the ``tracemalloc`` peak
while writing a small vs. a large record stream and asserting it stays
essentially flat. Under the old full-``DataFrame`` writer the peak grew
linearly (~100x more records → ~100x peak); a regression that
reintroduces row buffering would blow the ratio bound and fail here.

The input is a lazy generator (not a materialized list, which would
itself be O(record_count) and mask the writer's behavior), and the
destination is a real file (a ``StringIO`` would accumulate the whole
CSV in memory and defeat the measurement).
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from aero1553.models import MieMessage
from aero1553.reader import MieFileReader
from aero1553.writer import write_csv
from tests.conftest import RECORD_RT15_SA11_RCV


def _sample_message(tmp_path: Path) -> MieMessage:
    fpath = tmp_path / "one.mie"
    fpath.write_bytes(RECORD_RT15_SA11_RCV)
    return next(iter(MieFileReader(fpath)))


def _peak_bytes_writing(n: int, msg: MieMessage, out: Path) -> int:
    """Peak Python heap while streaming ``n`` copies of ``msg`` to ``out``."""

    def stream() -> Iterator[MieMessage]:
        for _ in range(n):
            yield msg

    gc.collect()
    tracemalloc.start()
    try:
        outcome = write_csv(stream(), output=out)
        assert outcome.normal_count == n
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


@pytest.mark.requirement("L3-PY-012")
def test_write_csv_memory_is_constant_in_record_count(tmp_path: Path) -> None:
    """The write-side peak for 100x more records stays within a small
    constant factor — O(1), not O(record_count)."""
    msg = _sample_message(tmp_path)

    small = 300
    large = 10_000  # ~33x more records
    peak_small = _peak_bytes_writing(small, msg, tmp_path / "small.csv")
    peak_large = _peak_bytes_writing(large, msg, tmp_path / "large.csv")

    # O(record_count) would put this ratio near 100; O(1) keeps it near 1.
    # A factor of 5 cleanly separates the two while tolerating allocator
    # and tracer noise.
    assert peak_large < peak_small * 5, (
        f"write peak grew with record count: {peak_small} -> {peak_large} bytes "
        f"({large // small}x more records); the writer is buffering rows"
    )
