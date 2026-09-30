"""Aero1553: DDC MIL-STD-1553 MIE binary recording file decoder.

This package provides a decoder for proprietary binary files produced by
DDC (Data Device Corporation) MIL-STD-1553 PCI recording cards. These
files use the ``.mie`` or ``.mie_alta`` extension and contain timestamped
1553 bus monitor captures.

The binary format is a bare stream of **variable-length** records — there
is no file header, and a null Type Word terminates the stream. Each record
opens with a Type Word that determines its length and shape, followed by a
timestamp, a MIL-STD-1553 command word, an optional status word, and up to
32 data words captured from the bus. Two timestamp formats are supported,
**IRIG** and **Standard**; the format is auto-detected per file by probing
the Command Word at both candidate offsets and scoring the results.

Typical usage::

    from aero1553 import MieFileReader

    reader = MieFileReader("recording.mie")
    for message in reader:
        print(message.timestamp, message.rt, message.subaddress)

The decoder entry point ``MieFileReader``, the ``MieMessage`` records it
yields, and ``columns`` (a whole stream as NumPy/pandas-ready buffers; see
:mod:`aero1553.table`) are importable directly from the package root.

The release history lives in ``CHANGELOG.md``; the installed version is
available as ``aero1553.__version__``.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Public package-root API (L3-PY-007). The decoder entry point —
# ``MieFileReader``, a typed callable: ``MieFileReader(path)`` constructs a
# reader that decodes the file lazily into ``MieMessage`` records — and that
# record type are re-exported here so library consumers can write
# ``from aero1553 import MieFileReader`` without reaching into submodules.
# (The submodule paths remain importable and unchanged.)
from aero1553.models import MieMessage
from aero1553.reader import MieFileReader
from aero1553.table import columns

try:
    __version__ = _pkg_version("aero1553")
except PackageNotFoundError:
    # Source-tree fallback: the package isn't installed (e.g. imported
    # directly from a clone before `pip install -e ./python` or
    # `uv sync` has run). All standard usage paths install the
    # package first, so this branch is rarely hit; the sentinel value
    # makes it obvious that the version came from this fallback rather
    # than from real package metadata.
    __version__ = "0.0.0+source"

del PackageNotFoundError, _pkg_version

__all__ = ["MieFileReader", "MieMessage", "__version__", "columns"]
