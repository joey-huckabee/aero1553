"""Stream output is the CLI's bytes, whatever the stream's settings (L3-PY-023).

The CSV writer and the hex dumps hand a text stream with a binary layer under
it (``sys.stdout``, any ``open(path, "w")`` file) the bytes themselves,
through that layer. Handing it ``str`` let a Windows text stream turn every
``\\n`` into ``\\r\\n``, against the LF rule of L2-WRT-012 -- ``write_csv`` to
stdout and the dumps both did, while the CLI wrote LF.

``io.TextIOWrapper(io.BytesIO(), newline="\\r\\n")`` is a Windows text stream
on every platform, so these fail without the fix on Linux too, where the real
stdout never shows the bug.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pytest

from aero1553.dump import hex_dump_raw, hex_dump_records
from aero1553.reader import MieFileReader
from aero1553.writer import write_csv
from tests.conftest import conformance_input


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    path = tmp_path / "rec.mie"
    path.write_bytes(conformance_input("basic-multi-record"))
    return path


def _csv_to(recording: Path) -> Callable[[object], object]:
    return lambda stream: write_csv(MieFileReader(recording), stream)


def _raw_to(recording: Path) -> Callable[[object], object]:
    return lambda stream: hex_dump_raw(recording, stream=stream)


def _records_to(recording: Path) -> Callable[[object], object]:
    return lambda stream: hex_dump_records(recording, stream=stream)


WRITERS = {"write_csv": _csv_to, "hex_dump_raw": _raw_to, "hex_dump_records": _records_to}


def _reference(write: Callable[[object], object]) -> bytes:
    """The bytes with no text layer in the way: a StringIO does not translate."""
    text = io.StringIO()
    write(text)
    return text.getvalue().encode("utf-8")


@pytest.mark.requirement("L3-PY-023")
def test_write_csv_reference_is_the_file_destination(recording: Path, tmp_path: Path) -> None:
    """The reference used below is the file writer's bytes, LF only."""
    out = tmp_path / "out.csv"
    write_csv(MieFileReader(recording), out)
    assert _reference(_csv_to(recording)) == out.read_bytes()
    assert b"\r" not in out.read_bytes()


@pytest.mark.requirement("L3-PY-023")
@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_crlf_text_stream_receives_lf(recording: Path, name: str) -> None:
    write = WRITERS[name](recording)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n")
    write(stream)
    stream.flush()
    assert raw.getvalue() == _reference(write)
    assert b"\r" not in raw.getvalue()


@pytest.mark.requirement("L3-PY-023")
@pytest.mark.parametrize("name", sorted(WRITERS))
def test_text_around_the_call_keeps_its_place(recording: Path, name: str) -> None:
    write = WRITERS[name](recording)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
    stream.write("before\n")
    write(stream)
    stream.write("after\n")
    stream.flush()
    assert raw.getvalue() == b"before\n" + _reference(write) + b"after\n"


@pytest.mark.requirement("L3-PY-023")
def test_the_stream_encoding_does_not_change_the_bytes(recording: Path) -> None:
    write = _csv_to(recording)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-16", newline="\r\n")
    write(stream)
    stream.flush()
    assert raw.getvalue() == _reference(write)


@pytest.mark.requirement("L3-PY-023")
def test_a_partial_raw_write_is_completed(recording: Path) -> None:
    """A raw layer (``python -u``'s stdout) may take only part of a write."""

    class Trickle(io.RawIOBase):
        def __init__(self) -> None:
            self.data = bytearray()

        def writable(self) -> bool:
            return True

        def write(self, b: object) -> int:
            chunk = bytes(b)[:7]  # type: ignore[call-overload]
            self.data += chunk
            return len(chunk)

    raw = Trickle()
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n")  # type: ignore[type-var]
    write = _csv_to(recording)
    write(stream)
    assert bytes(raw.data) == _reference(write)


@pytest.mark.requirement("L3-PY-023")
def test_a_stream_without_a_binary_layer_is_given_text(recording: Path) -> None:
    """Anything with a ``write`` method still works -- including one whose
    ``buffer`` attribute is not a binary stream."""

    class Collector:
        buffer = "not a binary stream"

        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, text: str) -> int:
            self.parts.append(text)
            return len(text)

    sink = Collector()
    write = _csv_to(recording)
    write(sink)
    assert "".join(sink.parts).encode("utf-8") == _reference(write)


@pytest.mark.requirement("L3-PY-023")
def test_a_broken_binary_layer_is_still_a_clean_stop_for_write_csv(recording: Path) -> None:
    class Closed(io.RawIOBase):
        def writable(self) -> bool:
            return True

        def write(self, b: object) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    stream = io.TextIOWrapper(Closed(), encoding="utf-8")  # type: ignore[type-var]
    outcome = write_csv(MieFileReader(recording), stream)  # L2-WRT-018: no raise
    assert outcome.partial is None


@pytest.mark.requirement("L3-PY-023")
def test_a_broken_binary_layer_propagates_from_a_dump(recording: Path) -> None:
    class Closed(io.RawIOBase):
        def writable(self) -> bool:
            return True

        def write(self, b: object) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    stream = io.TextIOWrapper(Closed(), encoding="utf-8")  # type: ignore[type-var]
    with pytest.raises(BrokenPipeError):
        hex_dump_records(recording, stream=stream)
