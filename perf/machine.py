"""What machine a performance report came from.

Gate ratios differ by machine type: on one CI runner a Python/Rust gate read
1.226 where every other runner read 0.95-1.07, consistently across its rounds.
Without the processor model in the report there is no telling whether a failed
gate is a regression or a machine, so every report records it.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


def cpu_model() -> str:
    """The processor's model name. Gate ratios differ by machine type -- one
    runner put a Python/Rust gate at 1.226 where others read 0.95-1.07 -- so a
    report must say which machine produced it. ``platform.processor()`` gives
    the model on Windows but only ``x86_64`` on Linux, where it is read from
    ``/proc/cpuinfo`` instead."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        fields: dict[str, str] = {}
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            fields.setdefault(key.strip(), value.strip())
        for key in ("model name", "Model", "Hardware"):
            if fields.get(key):
                return fields[key]
        # Arm Linux names no model; the implementer and part codes identify it.
        if fields.get("CPU part"):
            return f"Arm implementer {fields.get('CPU implementer', '?')} part {fields['CPU part']}"
    if sys.platform == "darwin":
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if out:
            return out
    return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "") or "unknown"
