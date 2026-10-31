"""Every public Python function that takes a path accepts one longer than the
Windows legacy limit (260), with ``LongPathsEnabled`` on or off.

Rust's standard library adds the ``\\\\?\\`` extended-length prefix itself, so
reading, writing, merging and dumping -- all Rust since v4.0.0 -- reach a deep
directory whatever the machine's settings. Python's own file calls do not: on
Windows they honour long paths only when the registry switch is on. So any
path a Python function touches *itself* before handing it to Rust is where a
long path can fail. Windows CI runs this file with the switch forced off
(``ci.yml``), the configuration in which that shows.

Each case runs the same call on a short path and on a deep one, and requires
the same result. On POSIX a long path is unremarkable; the tests run there too
and prove only that.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from aero1553 import MieFileReader
from aero1553.config import load_config
from aero1553.dump import hex_dump_raw, hex_dump_records
from aero1553.merge import expand_glob, merge_readers, read_manifest
from aero1553.writer import WriteOptions, write_csv, write_csv_split
from tests.conftest import conformance_input

#: The deep directory's full path is at least this long: past both Windows
#: limits, 260 for a file and 248 for a directory.
MIN_PATH_CHARS = 320
SEGMENT = "d" * 60

ROOT = Path(__file__).resolve().parents[2]


def _native(path: Path) -> str:
    """``path`` as THIS test must spell it for its own file operations. Without
    the registry switch, Python on Windows cannot create or read a deep path
    through an ordinary spelling either. The functions under test always get
    the ordinary spelling, which is what is being tested."""
    if sys.platform == "win32":
        return "\\\\?\\" + str(path)
    return str(path)


def _write(path: Path, data: bytes) -> Path:
    with open(_native(path), "wb") as handle:  # noqa: PTH123 -- prefixed str, not a Path
        handle.write(data)
    return path


def _read(path: Path) -> bytes:
    with open(_native(path), "rb") as handle:  # noqa: PTH123
        return handle.read()


def _exists(path: Path) -> bool:
    return os.path.exists(_native(path))  # noqa: PTH110


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A short directory and a deep one, both existing."""
    base = tmp_path.resolve()
    short = base / "s"
    short.mkdir()
    deep = base
    while len(str(deep)) < MIN_PATH_CHARS:
        deep /= SEGMENT
    os.makedirs(_native(deep))  # noqa: PTH103 -- prefixed str, not a Path
    assert len(str(deep / "rec.mie")) > 260
    return short, deep


def _both(dirs: tuple[Path, Path], name: str, data: bytes) -> tuple[Path, Path]:
    short, deep = dirs
    return _write(short / name, data), _write(deep / name, data)


@pytest.mark.requirement("L2-CLI-024")
def test_reader_reads_a_long_path(dirs: tuple[Path, Path]) -> None:
    short, deep = _both(dirs, "rec.mie", conformance_input("basic-multi-record"))
    assert [m.to_dict() for m in MieFileReader(deep)] == [m.to_dict() for m in MieFileReader(short)]


@pytest.mark.requirement("L2-CLI-024")
def test_write_csv_to_a_long_path(dirs: tuple[Path, Path]) -> None:
    data = conformance_input("basic-multi-record")
    outputs = []
    for d in dirs:
        source = _write(d / "rec.mie", data)
        out = d / "out.csv"
        write_csv(MieFileReader(source), out, WriteOptions(input_path=source, no_clobber=True))
        outputs.append(_read(out))
    assert outputs[0] == outputs[1]
    assert outputs[0].startswith(b"TIME_STAMP,")


@pytest.mark.requirement("L2-CLI-024")
def test_write_csv_split_to_a_long_path(dirs: tuple[Path, Path]) -> None:
    data = conformance_input("errors-inline")
    results = []
    for d in dirs:
        source = _write(d / "rec.mie", data)
        write_csv_split(MieFileReader(source), d / "out.csv", WriteOptions(input_path=source))
        assert _exists(d / "out_errors.csv"), "the lazily created errors file"
        results.append((_read(d / "out.csv"), _read(d / "out_errors.csv")))
    assert results[0] == results[1]


@pytest.mark.requirement("L2-CLI-024")
def test_load_config_from_a_long_path(dirs: tuple[Path, Path]) -> None:
    text = (ROOT / "config" / "default.toml").read_bytes()
    short, deep = _both(dirs, "site.toml", text)
    assert load_config(deep) == load_config(short)


@pytest.mark.requirement("L2-CLI-024")
def test_load_config_missing_long_path_is_still_not_found(dirs: tuple[Path, Path]) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(dirs[1] / "absent.toml")


@pytest.mark.requirement("L2-CLI-024")
def test_manifest_glob_and_merge_with_long_paths(dirs: tuple[Path, Path]) -> None:
    merged = []
    for d in dirs:
        a = _write(d / "a.mie", conformance_input("merge-a"))
        b = _write(d / "b.mie", conformance_input("merge-b"))
        manifest = _write(d / "list.txt", f"{a}\n{b}\n".encode())
        assert read_manifest(manifest) == [a, b]
        assert sorted(expand_glob(str(d / "*.mie"))) == [a, b]
        merged.append([m.to_dict() for m in merge_readers([MieFileReader(a), MieFileReader(b)])])
    assert merged[0] == merged[1]
    assert merged[0]


@pytest.mark.requirement("L2-CLI-024")
def test_dumps_of_a_long_path(dirs: tuple[Path, Path]) -> None:
    paths = _both(dirs, "rec.mie", conformance_input("basic-multi-record"))
    for dump in (hex_dump_raw, hex_dump_records):
        texts = []
        for path in paths:
            stream = io.StringIO()
            dump(path, stream=stream)
            texts.append(stream.getvalue().replace(str(path), "<PATH>"))
        assert texts[0] == texts[1], dump.__name__
