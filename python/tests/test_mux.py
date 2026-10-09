"""Tests for MUX-from-filename population (L2-WRT-020).

The extraction rule is the Rust reader's; these pin it as the Python API
exposes it, and the MUX cell the writer emits.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aero1553.reader import MieFileReader
from aero1553.writer import message_to_row, write_csv
from tests.conftest import RECORD_RT15_SA11_RCV, replace

_OP_NAME = "full_loadout.draw.data.1553.aa.unused.mie_irig"


_ALT_NAME = "full_loadout.draw.data.1553.bb.unused.mie_irig"


@pytest.mark.requirement("L2-WRT-020")
@pytest.mark.parametrize(
    ("name", "delimiter", "field", "expected"),
    [
        # Default field 4 -> recorder identity; other operator files match.
        (_OP_NAME, ".", 4, "aa"),
        (_ALT_NAME, ".", 4, "bb"),
        # Negative index counts from the end (-3 == index 4 here).
        (_OP_NAME, ".", -3, "aa"),
        (_OP_NAME, ".", 0, "full_loadout"),
        (_OP_NAME, ".", -1, "mie_irig"),
        # Out of range -> no MUX.
        (_OP_NAME, ".", 99, None),
        (_OP_NAME, ".", -99, None),
        # Other delimiters; empty delimiter / empty field / missing delimiter.
        ("a_b_c", "_", 1, "b"),
        (_OP_NAME, "", 4, None),
        ("a..b", ".", 1, None),
        ("plain", ".", 4, None),
        ("plain", ".", 0, "plain"),
        # Spaces are trimmed; Unicode whitespace is part of the name, as on a
        # --manifest line (no tab case: Windows forbids one in a file name).
        ("a. B7 .c", ".", 1, "B7"),
        ("a.   .c", ".", 1, None),
        ("a.\u00a0B7\u3000.c", ".", 1, "\u00a0B7\u3000"),
        ("a.\u00a0.c", ".", 1, "\u00a0"),
    ],
)
def test_mux_from_filename(
    tmp_path: Path, name: str, delimiter: str, field: int, expected: str | None
) -> None:
    """The extraction rule, applied by the reader to the file it opens."""
    fpath = tmp_path / name
    fpath.write_bytes(RECORD_RT15_SA11_RCV)
    msgs = list(MieFileReader(fpath, mux_delimiter=delimiter, mux_field=field))
    assert msgs, "fixture decoded no messages"
    assert all(m.mux == expected for m in msgs)


@pytest.mark.requirement("L2-WRT-020")
def test_reader_attaches_mux_from_filename(tmp_path: Path) -> None:
    fpath = tmp_path / _OP_NAME
    fpath.write_bytes(RECORD_RT15_SA11_RCV)
    # Default (enabled): every record carries the field-4 value.
    msgs = list(MieFileReader(fpath))
    assert msgs, "fixture decoded no messages"
    assert all(m.mux == "aa" for m in msgs)
    # Disabled → no MUX.
    msgs = list(MieFileReader(fpath, mux_enabled=False))
    assert all(m.mux is None for m in msgs)
    # Custom field override.
    msgs = list(MieFileReader(fpath, mux_field=0))
    assert all(m.mux == "full_loadout" for m in msgs)


@pytest.mark.requirement("L2-WRT-020")
def test_writer_emits_mux_and_quotes(tmp_path: Path) -> None:
    fpath = tmp_path / _OP_NAME
    fpath.write_bytes(RECORD_RT15_SA11_RCV)
    msg = next(iter(MieFileReader(fpath)))
    assert message_to_row(msg)["MUX"] == "aa"

    # A MUX value containing the delimiter is RFC4180-quoted by the csv module.
    buf = io.StringIO()
    write_csv([replace(msg, mux="a,b")], output=buf)
    assert '"a,b"' in buf.getvalue()
