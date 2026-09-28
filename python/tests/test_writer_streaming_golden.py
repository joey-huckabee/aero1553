"""Byte-exact characterization guard for the CSV writer (PY-streaming).

These tests freeze the writer's current observable output as golden
fixtures so the PY-streaming refactor — which replaces the full-pandas-
DataFrame buffering with a constant-memory streaming writer — cannot
change a single output byte. They are deliberately end-to-end (real
``MieFileReader`` → real writer) and compare raw bytes, not parsed CSV,
so quoting, line terminators, padding, and trailing empty cells are all
pinned.

The golden fixtures under ``tests/golden/py_streaming/`` were generated
from the pre-refactor (pandas) writer. If a future change legitimately
alters output, regenerate them deliberately and review the diff against
the Rust writer and the cross-impl conformance oracles — never silently.

The representative stream exercises every distinct row shape the writer
emits:

* normal records with a full 30-data-word payload,
* DELTA progression (first occurrence ``0.000000`` → steady ``0.016000``),
* an errored record (Type Word bit 14: truncated payload, ``ERROR``/code),
* a SPURIOUS_DATA continuation (``SPURIOUS``/``2000`` — proving the
  reader's ``prev_was_error`` classification survives into the CSV),
* and the split-mode partition (normal rows vs errored/spurious rows).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aero1553.reader import MieFileReader
from aero1553.writer import write_csv, write_csv_split
from tests.conftest import (
    errored_record_rt15_sa11_us,
    normal_record_rt15_sa11_us,
    spurious_record_us,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "py_streaming"


def _build_stream() -> bytes:
    """Representative MIE byte stream.

    MUST stay in sync with ``tests/golden/py_streaming/*`` — the golden
    fixtures were generated from exactly these records. The errored
    record sets the reader's ``prev_was_error`` flag, so the following
    spurious record is classified as a ``0x2000`` continuation.
    """
    return (
        normal_record_rt15_sa11_us(100)
        + normal_record_rt15_sa11_us(16100)
        + errored_record_rt15_sa11_us(32100)
        + spurious_record_us(32100, 0xBEEF)
        + normal_record_rt15_sa11_us(48100)
    )


@pytest.fixture
def stream_mie_file(tmp_path: Path) -> Path:
    fpath = tmp_path / "stream.mie"
    fpath.write_bytes(_build_stream())
    return fpath


@pytest.mark.requirement("L3-PY-012")
def test_inline_file_output_is_byte_exact(stream_mie_file: Path, tmp_path: Path) -> None:
    """``write_csv`` to a file path matches the golden bytes exactly."""
    out = tmp_path / "inline.csv"
    write_csv(MieFileReader(stream_mie_file), output=out)
    expected = (GOLDEN_DIR / "inline.csv").read_bytes()
    assert out.read_bytes() == expected


@pytest.mark.requirement("L3-PY-012")
def test_inline_stream_output_is_byte_exact(stream_mie_file: Path) -> None:
    """``write_csv`` to a text stream matches the golden bytes exactly.

    The stream path encodes to UTF-8 for comparison; the golden file is
    the LF-terminated byte image both the file and stream paths produce.
    """
    buf = io.StringIO()
    write_csv(MieFileReader(stream_mie_file), output=buf)
    expected = (GOLDEN_DIR / "inline.csv").read_bytes()
    assert buf.getvalue().encode("utf-8") == expected


@pytest.mark.requirement("L3-PY-012")
def test_split_output_is_byte_exact(stream_mie_file: Path, tmp_path: Path) -> None:
    """``write_csv_split`` main + errors files match the golden bytes."""
    main = tmp_path / "split.csv"
    write_csv_split(MieFileReader(stream_mie_file), output=main)
    errors = tmp_path / "split_errors.csv"

    assert main.read_bytes() == (GOLDEN_DIR / "split_main.csv").read_bytes()
    assert errors.exists(), "errors file should be created (stream has error rows)"
    assert errors.read_bytes() == (GOLDEN_DIR / "split_errors.csv").read_bytes()
