"""Tests for MUX-from-filename population (L2-WRT-020).

Mirrors the Rust `mux_from_filename` unit tests and writer test so both
implementations agree on the extraction rule and the MUX cell output.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aero1553.decode import mux_from_filename
from aero1553.reader import MieFileReader
from aero1553.writer import message_to_row, write_csv
from tests.conftest import RECORD_RT15_SA11_RCV, replace

_OP_NAME = "full_loadout.draw.data.1553.aa.unused.mie_irig"


@pytest.mark.requirement("L2-WRT-020")
def test_mux_from_filename() -> None:
    name = _OP_NAME
    # Default field 4 → recorder identity; other operator files match.
    assert mux_from_filename(name, ".", 4) == "aa"
    assert mux_from_filename("full_loadout.draw.data.1553.bb.unused.mie_irig", ".", 4) == "bb"
    # Negative index counts from the end (-3 == index 4 here).
    assert mux_from_filename(name, ".", -3) == "aa"
    assert mux_from_filename(name, ".", 0) == "full_loadout"
    assert mux_from_filename(name, ".", -1) == "mie_irig"
    # Out-of-range → None (empty MUX).
    assert mux_from_filename(name, ".", 99) is None
    assert mux_from_filename(name, ".", -99) is None
    # Other delimiters; empty delimiter / empty field / missing delimiter.
    assert mux_from_filename("a_b_c", "_", 1) == "b"
    assert mux_from_filename(name, "", 4) is None
    assert mux_from_filename("a..b", ".", 1) is None
    assert mux_from_filename("plain", ".", 4) is None
    assert mux_from_filename("plain", ".", 0) == "plain"


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
