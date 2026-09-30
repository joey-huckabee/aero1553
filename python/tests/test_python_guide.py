"""Run every example in the Python library guide, and check what it prints.

``docs/PYTHON-GUIDE.md`` is executable documentation. Each ```python block
runs on its own, in a fresh namespace, in a directory holding the golden
recordings under the names the guide uses (``flight.mie`` and friends). When
a ```text block follows it, that is the block's expected output, and what the
example prints -- captured at the file-descriptor level, so output written by
the Rust CLI counts too -- must match it exactly. So the guide cannot show an
API that does not exist, or a number the library does not produce.

Examples that import NumPy or pandas are skipped when those are not
installed; they are in the dev environment, so CI runs them all.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "PYTHON-GUIDE.md"
_GOLDEN_PY = Path(__file__).resolve().parents[2] / "tests" / "golden" / "golden.py"

#: The guide's file names for the golden recordings.
FILES = {
    "flight.mie": "a-small",
    "flight-2.mie": "b-small",
    "counter.mie": "standard-small",
    "freerun.mie": "freerun-small",
}


@dataclass(frozen=True)
class Example:
    line: int
    code: str
    expected: str | None


def examples(text: str) -> list[Example]:
    """Every ```python block of ``text``, with the ```text block after it."""
    fence = re.compile(r"^```(\w*)\n(.*?)^```\n", re.S | re.M)
    blocks = list(fence.finditer(text))
    out = []
    for i, block in enumerate(blocks):
        if block.group(1) != "python":
            continue
        expected = None
        after = blocks[i + 1] if i + 1 < len(blocks) else None
        # An output block counts only when nothing but whitespace separates it
        # from the example.
        if after and after.group(1) == "text" and not text[block.end() : after.start()].strip():
            expected = after.group(2)
        line = text.count("\n", 0, block.start()) + 1
        out.append(Example(line, block.group(2), expected))
    return out


EXAMPLES = examples(GUIDE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recordings(tmp_path_factory: pytest.TempPathFactory) -> Path:
    spec = importlib.util.spec_from_file_location("golden", _GOLDEN_PY)
    assert spec is not None
    assert spec.loader is not None
    golden = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(golden)
    source = tmp_path_factory.mktemp("golden")
    for name, recording in FILES.items():
        written = golden.write(recording, source)
        shutil.copyfile(written, source / name)
    return source


@pytest.mark.requirement("L3-PY-007")
@pytest.mark.requirement("L3-PY-020")
def test_the_guide_has_examples_with_output() -> None:
    assert len(EXAMPLES) >= 30, "the guide's examples were not found"
    assert sum(e.expected is not None for e in EXAMPLES) >= 20, "most examples show their output"


@pytest.mark.requirement("L3-PY-007")
@pytest.mark.requirement("L3-PY-020")
@pytest.mark.parametrize("example", EXAMPLES, ids=[f"line-{e.line}" for e in EXAMPLES])
def test_guide_example(
    example: Example,
    recordings: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    for lib in ("numpy", "pandas"):
        if re.search(rf"^\s*import {lib}\b", example.code, re.M):
            pytest.importorskip(lib)
    for name in FILES:
        shutil.copyfile(recordings / name, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    # A real, registered module, as a script would run in: @dataclass looks
    # its defining module up in sys.modules.
    module = types.ModuleType("__guide__")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    capfd.readouterr()
    exec(compile(example.code, f"{GUIDE.name}:{example.line}", "exec"), module.__dict__)
    printed = capfd.readouterr().out
    if example.expected is not None:
        assert printed == example.expected, f"{GUIDE.name} line {example.line}"
