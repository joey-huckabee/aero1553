"""One rule for text the decoder prints but did not write (L2-CLI-025).

Printable ASCII stands for itself; every other character is ``\\u{hex}``, and a
byte of a file name that is not UTF-8 is ``\\xNN``. Exception messages and log
records follow it as the CLI does; attributes such as ``path`` keep the exact
value, for a program to use.
"""

from __future__ import annotations

import logging
import os
import pickle
import sys
from pathlib import Path

import pytest

from aero1553 import MieFileReader
from aero1553.exceptions import MieFileNotFoundError
from tests.conftest import RECORD_RT15_SA11_RCV

E_ACUTE = "é"


@pytest.mark.requirement("L2-CLI-025")
def test_exception_message_is_escaped_and_path_is_exact(tmp_path: Path) -> None:
    missing = tmp_path / f"caf{E_ACUTE}.mie"
    with pytest.raises(MieFileNotFoundError) as caught:
        MieFileReader(missing)
    message = str(caught.value)
    assert message.endswith("caf\\u{e9}.mie"), message
    assert message.isascii()
    assert caught.value.path == str(missing)


@pytest.mark.requirement("L2-CLI-025")
def test_a_newline_in_a_message_is_escaped_and_pickling_keeps_both() -> None:
    exc = MieFileNotFoundError(f"dir/a\nb{E_ACUTE}.mie")
    assert str(exc) == "MIE file not found: dir/a\\u{a}b\\u{e9}.mie"
    again = pickle.loads(pickle.dumps(exc))
    assert (str(again), again.path) == (str(exc), exc.path)


@pytest.mark.requirement("L2-CLI-025")
@pytest.mark.skipif(sys.platform == "win32", reason="a file name byte that is not UTF-8 is POSIX")
def test_a_file_name_byte_that_is_not_utf8_shows_as_itself(tmp_path: Path) -> None:
    name = os.fsdecode(b"bad\xffname.mie")
    with pytest.raises(MieFileNotFoundError) as caught:
        MieFileReader(tmp_path / name)
    assert str(caught.value).endswith("bad\\xFFname.mie")
    # The attribute is the real name: os.fsencode gets the byte back.
    assert os.fsencode(caught.value.path).endswith(b"bad\xffname.mie")


@pytest.mark.requirement("L2-CLI-025")
def test_log_records_follow_the_rule(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Data after the terminator: the reader WARNs, naming the file.
    path = tmp_path / f"joined {E_ACUTE}.mie"
    path.write_bytes(RECORD_RT15_SA11_RCV + b"\x00\x00AA")
    with caplog.at_level(logging.WARNING, logger="aero1553"):
        list(MieFileReader(path))
    warnings = [r.getMessage() for r in caplog.records if "terminator" in r.getMessage()]
    assert warnings, caplog.text
    assert "joined \\u{e9}.mie" in warnings[0]
    assert all(m.isascii() for m in warnings)
