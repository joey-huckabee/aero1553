"""Ctrl-C during a long native call stops it, and commits nothing (L3-PY-021).

``write_csv`` and ``write_csv_split`` run in Rust with the GIL released, and
CPython runs signal handlers only between bytecodes, so Ctrl-C used to wait for
the whole write -- which had already renamed its temp file over the destination
by the time ``KeyboardInterrupt`` surfaced. A cancelled run left a complete-
looking output behind.

Each test starts a write over a recording large enough to take a while, waits
in a background thread until the writer's temp file exists (so the interrupt
lands mid-write, not before the call or after it), then simulates Ctrl-C with
``_thread.interrupt_main``. The source is a native reader -- the path the fix
covers; a Python-level source raised ``KeyboardInterrupt`` from its own code
all along.
"""

from __future__ import annotations

import _thread
import signal
import threading
import time
from pathlib import Path

import pytest

from aero1553.reader import MieFileReader
from aero1553.writer import write_csv, write_csv_split
from tests.conftest import conformance_input

# 3 records per copy; 100 000 copies is 300 000 records, a write long enough to
# be interrupted in the middle on any machine this suite runs on.
_COPIES = 100_000


def _big_recording(path: Path) -> Path:
    path.write_bytes(conformance_input("basic-multi-record") * _COPIES)
    return path


@pytest.fixture(autouse=True)
def _python_handles_sigint() -> None:
    """Python's own Ctrl-C handler, the condition these tests are about.

    ``_thread.interrupt_main`` does nothing unless Python handles SIGINT, and a
    process entry point (``cli.main_cli``) may have handed it back to the OS.
    The conftest fixture restores whatever was there afterwards.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)


def _interrupt_once_writing(directory: Path) -> threading.Thread:
    """Simulate Ctrl-C as soon as a writer temp file appears in ``directory``."""

    def watch() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(directory.glob("*.aero1553.tmp.*")):
                _thread.interrupt_main(signal.SIGINT)
                return
            time.sleep(0.001)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


@pytest.mark.requirement("L3-PY-021")
def test_ctrl_c_stops_write_csv_and_commits_nothing(tmp_path: Path) -> None:
    source = _big_recording(tmp_path / "rec.mie")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    destination = out_dir / "decoded.csv"

    watcher = _interrupt_once_writing(out_dir)
    with pytest.raises(KeyboardInterrupt):
        write_csv(MieFileReader(source), destination)
    watcher.join(timeout=5)

    assert not destination.exists(), "an interrupted write must not be committed"
    assert not list(out_dir.iterdir()), "and must leave no temp file behind"


@pytest.mark.requirement("L3-PY-021")
def test_ctrl_c_stops_write_csv_split_and_commits_nothing(tmp_path: Path) -> None:
    source = _big_recording(tmp_path / "rec.mie")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    destination = out_dir / "decoded.csv"

    watcher = _interrupt_once_writing(out_dir)
    with pytest.raises(KeyboardInterrupt):
        write_csv_split(MieFileReader(source), destination)
    watcher.join(timeout=5)

    assert not list(out_dir.iterdir()), "neither file may be committed, nor a temp left"
