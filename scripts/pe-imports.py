"""List the DLLs a Windows executable imports; fail on a C runtime DLL (L3-RS-020).

    python scripts/pe-imports.py aero1553.exe

Prints each DLL named in the executable's import table, and exits 1 if one is a C
runtime DLL (``vcruntime*``, ``msvcp*``, ``ucrtbase``, ``api-ms-win-crt-*``): an exe
that imports one does not start on a machine without the Visual C++ Redistributable.

Reads the PE file itself, with the standard library only, so it runs on any machine
with Python -- a disconnected one without Visual Studio's ``dumpbin`` included.
Delay-loaded imports are not listed; the Rust standard library uses none.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

C_RUNTIME = re.compile(r"^(vcruntime|msvcp|ucrtbase|api-ms-win-crt-)", re.IGNORECASE)


def imported_dlls(data: bytes) -> list[str]:
    """The DLL names in the import directory of a PE32 or PE32+ image.

    Raises:
        ValueError: if ``data`` is not a PE image, or its import table points
            outside every section.
    """
    if data[:2] != b"MZ":
        raise ValueError("not a PE file: no MZ header")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file: no PE signature")
    sections_count, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    # The data directories follow the fixed part of the optional header, whose
    # length differs between PE32 (0x10B) and PE32+ (0x20B); entry 1 is imports.
    directories = optional + {0x10B: 96, 0x20B: 112}[magic]
    import_rva = struct.unpack_from("<I", data, directories + 8)[0]
    sections = []
    for i in range(sections_count):
        base = optional + optional_size + 40 * i
        size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", data, base + 8)
        sections.append((rva, max(size, raw_size), raw_offset))

    def offset(rva: int) -> int:
        for start, size, raw in sections:
            if start <= rva < start + size:
                return raw + rva - start
        raise ValueError(f"RVA {rva:#x} is in no section")

    names = []
    entry = offset(import_rva)
    while True:
        descriptor = struct.unpack_from("<5I", data, entry)
        if not any(descriptor):  # the all-zero descriptor ends the table
            return names
        name = offset(descriptor[3])
        names.append(data[name : data.index(b"\0", name)].decode("ascii"))
        entry += 20


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    exe = Path(sys.argv[1])
    dlls = imported_dlls(exe.read_bytes())
    print(f"{exe} ({exe.stat().st_size:,} bytes) imports:")
    for dll in dlls:
        print(f"  {dll}")
    runtime = [dll for dll in dlls if C_RUNTIME.match(dll)]
    if runtime:
        print(f"FAIL: imports the C runtime: {', '.join(runtime)}", file=sys.stderr)
        return 1
    print("no C runtime DLL: runs without the Visual C++ Redistributable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
