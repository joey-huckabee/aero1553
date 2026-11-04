"""``scripts/pe-imports.py`` reads a PE import table and fails on a C runtime DLL
(L3-RS-020). It is what proves, on a disconnected Windows machine with no
Visual Studio tools, that a self-built exe needs no Visual C++ Redistributable;
CI uses it on every Windows build. The images here are built in memory, so the
check is proven to find the C runtime, not only to pass a clean exe."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pe-imports.py"
_spec = importlib.util.spec_from_file_location("pe_imports", _SCRIPT)
assert _spec is not None and _spec.loader is not None
pe_imports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe_imports)


def _pe(dlls: list[str], magic: int = 0x20B) -> bytes:
    """A minimal PE image: one section at RVA 0x1000 (file offset 0x200) holding
    the import descriptors and the DLL names they point to."""
    fixed = {0x10B: 96, 0x20B: 112}[magic]
    optional_size = fixed + 16 * 8
    image = bytearray(0x400)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x40)
    image[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 0x44, 0x8664, 1, 0, 0, 0, optional_size, 0x22)
    optional = 0x58
    struct.pack_into("<H", image, optional, magic)
    struct.pack_into("<II", image, optional + fixed + 8, 0x1000, 20 * (len(dlls) + 1))
    section = optional + optional_size
    image[section : section + 8] = b".idata\0\0"
    struct.pack_into("<IIII", image, section + 8, 0x200, 0x1000, 0x200, 0x200)
    names_rva = 0x1000 + 20 * (len(dlls) + 1)
    for i, dll in enumerate(dlls):
        struct.pack_into("<5I", image, 0x200 + 20 * i, 0, 0, 0, names_rva, 0)
        start = 0x200 + names_rva - 0x1000
        image[start : start + len(dll) + 1] = dll.encode() + b"\0"
        names_rva += len(dll) + 1
    return bytes(image)


CLEAN = ["bcryptprimitives.dll", "api-ms-win-core-synch-l1-2-0.dll", "KERNEL32.dll", "ntdll.dll"]


@pytest.mark.requirement("L3-RS-020")
@pytest.mark.parametrize("magic", [0x20B, 0x10B], ids=["PE32+", "PE32"])
def test_reads_every_imported_dll(magic: int) -> None:
    assert pe_imports.imported_dlls(_pe(CLEAN, magic)) == CLEAN


@pytest.mark.requirement("L3-RS-020")
@pytest.mark.parametrize(
    "runtime",
    ["VCRUNTIME140.dll", "MSVCP140.dll", "ucrtbase.dll", "api-ms-win-crt-heap-l1-1-0.dll"],
)
def test_fails_on_a_c_runtime_dll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    exe = tmp_path / "dynamic.exe"
    exe.write_bytes(_pe([*CLEAN, runtime]))
    monkeypatch.setattr("sys.argv", ["pe-imports.py", str(exe)])
    assert pe_imports.main() == 1


@pytest.mark.requirement("L3-RS-020")
def test_passes_an_exe_with_only_windows_dlls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "static.exe"
    exe.write_bytes(_pe(CLEAN))
    monkeypatch.setattr("sys.argv", ["pe-imports.py", str(exe)])
    assert pe_imports.main() == 0


@pytest.mark.requirement("L3-RS-020")
def test_refuses_a_file_that_is_not_pe() -> None:
    with pytest.raises(ValueError, match="no MZ header"):
        pe_imports.imported_dlls(b"\x7fELF" + bytes(64))
