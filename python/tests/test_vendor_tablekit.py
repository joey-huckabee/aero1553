"""``scripts/vendor-tablekit.py check`` (L3-RS-002): the vendored tablekit is exactly the
tag its ``SHA256SUMS`` names. The check runs in repo-hygiene and so in CI; these tests prove
it passes on the repository and fails on each way the copy can drift."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vendor-tablekit.py"
_spec = importlib.util.spec_from_file_location("vendor_tablekit", _SCRIPT)
assert _spec is not None and _spec.loader is not None
vendor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vendor)


@pytest.fixture
def copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of third_party/tablekit that the check is pointed at."""
    dest = tmp_path / "tablekit"
    shutil.copytree(vendor.DEST, dest)
    monkeypatch.setattr(vendor, "DEST", dest)
    monkeypatch.setattr(vendor, "SUMS", dest / "SHA256SUMS")
    return dest


@pytest.mark.requirement("L3-RS-002")
def test_the_repository_copy_matches() -> None:
    assert vendor.check() == 0


@pytest.mark.requirement("L3-RS-002")
def test_names_the_tag_and_commit() -> None:
    header = vendor.SUMS.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("# tablekit v") and "(commit " in header


@pytest.mark.requirement("L3-RS-002")
def test_an_edited_file_fails(copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lib = copy / "crates" / "tablekit-csv" / "src" / "lib.rs"
    lib.write_text(lib.read_text(encoding="utf-8") + "\n// local edit\n", encoding="utf-8")
    assert vendor.check() == 1
    assert "changed: crates/tablekit-csv/src/lib.rs" in capsys.readouterr().err


@pytest.mark.requirement("L3-RS-002")
def test_a_missing_file_fails(copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (copy / "crates" / "tablekit" / "src" / "lib.rs").unlink()
    assert vendor.check() == 1
    assert "missing: crates/tablekit/src/lib.rs" in capsys.readouterr().err


@pytest.mark.requirement("L3-RS-002")
def test_an_added_file_fails(copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (copy / "crates" / "tablekit-csv" / "src" / "extra.rs").write_text("", encoding="utf-8")
    assert vendor.check() == 1
    assert "not vendored from tablekit: crates/tablekit-csv/src/extra.rs" in capsys.readouterr().err


@pytest.mark.requirement("L3-RS-002")
def test_only_the_csv_crates_are_vendored() -> None:
    crates = sorted(p.name for p in (vendor.DEST / "crates").iterdir())
    assert crates == ["tablekit", "tablekit-csv"]
    manifest = (vendor.DEST / "Cargo.toml").read_text(encoding="utf-8")
    assert "parquet" not in manifest and "tablekit-profile" not in manifest
