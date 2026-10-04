"""A sync loss on a stream destination is raised, ``allow_partial`` or not (L3-PY-022).

``allow_partial`` downgrades an unrecoverable sync loss only by keeping a
``.partial`` file (L1-EXIT-004), and a stream cannot hold one. A stream write
with ``allow_partial`` used to stop at the loss and return the ``WriteOutcome``
of a complete decode, so the caller could not tell the recording had been cut
short; the CLIs' stdout output exits 3 in the same situation.

The rows that reach the stream before the error are pinned against the
``.partial`` file the same decode commits to a file destination, so "the rows
decoded before the loss" means the same thing on both.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aero1553.exceptions import MieMergeInputsDroppedError, MieUnrecoverableSyncLossError
from aero1553.merge import merge_readers
from aero1553.reader import MieFileReader
from aero1553.writer import WriteOptions, write_csv
from tests.conftest import conformance_input
from tests.test_merge import rt15_record_at


def _sync_loss_recording(tmp_path: Path) -> Path:
    path = tmp_path / "partial.mie"
    path.write_bytes(conformance_input("partial-unrecoverable"))
    return path


def _partial_file_rows(tmp_path: Path, recording: Path) -> str:
    """The rows a file destination keeps for ``recording`` under allow_partial."""
    out = tmp_path / "file.csv"
    outcome = write_csv(MieFileReader(recording), out, WriteOptions(allow_partial=True))
    assert outcome.partial is not None
    kept = outcome.partial.main_path.read_text(encoding="utf-8")
    assert len(kept.splitlines()) > 1, "the fixture must decode rows before the loss"
    return kept


@pytest.mark.requirement("L3-PY-022")
@pytest.mark.parametrize("allow_partial", [True, False])
def test_a_stream_raises_the_sync_loss_after_the_rows_before_it(
    tmp_path: Path, allow_partial: bool
) -> None:
    recording = _sync_loss_recording(tmp_path)
    expected = _partial_file_rows(tmp_path, recording)

    sink = io.StringIO()
    with pytest.raises(MieUnrecoverableSyncLossError):
        write_csv(MieFileReader(recording), sink, WriteOptions(allow_partial=allow_partial))

    assert sink.getvalue() == expected


@pytest.mark.requirement("L3-PY-022")
def test_the_sync_loss_advice_names_the_output_file(tmp_path: Path) -> None:
    """The advice holds whether or not ``allow_partial`` was given: a stream
    needs a file destination for a ``.partial``, not the flag it already has."""
    sink = io.StringIO()
    with pytest.raises(MieUnrecoverableSyncLossError) as lost:
        write_csv(
            MieFileReader(_sync_loss_recording(tmp_path)),
            sink,
            WriteOptions(allow_partial=True),
        )
    assert str(lost.value).endswith(
        "Pass --allow-partial with an output file (-o) to keep what was decoded as a .partial file."
    )


@pytest.mark.requirement("L3-PY-022")
def test_a_stream_raises_a_merge_that_left_inputs_out(tmp_path: Path) -> None:
    """The other partial stop on the same path, raised all along -- pinned so the
    two cannot drift apart again."""
    left_out = tmp_path / "a.mie"
    good = tmp_path / "b.mie"
    left_out.write_bytes(b"\xff" * 4096)  # no valid first record
    good.write_bytes(rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300))

    merged = merge_readers([MieFileReader(left_out), MieFileReader(good)], allow_partial=True)
    sink = io.StringIO()
    with pytest.raises(MieMergeInputsDroppedError):
        write_csv(merged, sink, WriteOptions(allow_partial=True))
    assert len(sink.getvalue().splitlines()) == 3  # header + the good input's two rows
