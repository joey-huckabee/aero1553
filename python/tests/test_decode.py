"""Timestamp-format selection, through the public reader and CLI.

The word decoders themselves are the Rust crate's (``rust/src/decode.rs``),
pinned there and by the exhaustive digest in ``rust/examples/decode_digest.rs``
and the C++ suite. All expected values are derived from empirically validated
binary data cross-referenced against vendor-generated CSV output.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestDetectTimestampFormat:
    """Tests for auto-detection of timestamp format."""

    @pytest.mark.requirement("L2-DEC-013")
    def test_forced_irig(self, tmp_mie_file: Path) -> None:
        """Forcing IRIG should still decode correctly."""
        from aero1553.models import TimestampFormat
        from aero1553.reader import MieFileReader

        reader = MieFileReader(tmp_mie_file, input_time_format=TimestampFormat.IRIG)
        messages = list(reader)
        assert len(messages) == 3
        assert messages[0].timestamp.format() == "192:15:54:50.456225"

    @pytest.mark.requirement("L2-DEC-013")
    def test_cli_input_time_format_irig(self, tmp_mie_file: Path, tmp_path: Path) -> None:
        """CLI --input-time-format irig should work."""
        from aero1553.cli import main

        out = tmp_path / "irig.csv"
        rc = main(["decode", str(tmp_mie_file), "-o", str(out), "--input-time-format", "irig"])
        assert rc == 0
        assert out.exists()
