"""Meta-tests that the cross-implementation conformance suite is wired up.

L2-CONF-003 ("each implementation's output SHALL match the checked-in CSV
oracle") is verified by `tests/conformance/run.py`, which is invoked by
the CI ``conformance`` job. The runner is not itself a pytest test and
therefore can't carry a ``@pytest.mark.requirement`` marker; these
pytest meta-tests assert that the conformance contract is present,
discoverable, and exercises at least one fixture so the trace matrix
credits L2-CONF-003 with a verifiable artifact.

A failing run of the conformance runner in CI is the authoritative
test; these meta-tests guarantee the runner stays wired up.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFORMANCE_DIR = _REPO_ROOT / "tests" / "conformance"


@pytest.mark.requirement("L2-CONF-003")
def test_conformance_runner_exists() -> None:
    """The cross-implementation runner script SHALL exist."""
    runner = _CONFORMANCE_DIR / "run.py"
    assert runner.is_file(), f"conformance runner missing at {runner}"


def _load_runner():
    """Import `tests/conformance/run.py` as a module.

    Imported rather than read as text. This test used to assert that three
    literal call expressions appeared in the source, which pinned the runner's
    *spelling* rather than its behaviour: it passed for any file containing
    those characters, and failed the moment the same behaviour was expressed
    differently — which is exactly what happened when the runner grew from a
    hard-wired Rust/Python pair into an implementation registry.
    """
    sys.path.insert(0, str(_CONFORMANCE_DIR))
    try:
        runner = _CONFORMANCE_DIR / "run.py"
        spec = importlib.util.spec_from_file_location("mie_conformance_run", runner)
        # Split, not `a is not None and b is not None`: these are two distinct
        # failures -- no spec at all versus a spec that cannot be executed --
        # and a composite assertion cannot say which happened.
        assert spec is not None, f"no module spec could be built for {runner}"
        assert spec.loader is not None, f"the module spec for {runner} has no loader"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_CONFORMANCE_DIR))


@pytest.mark.requirement("L2-CONF-002")
def test_conformance_runner_registers_both_clis() -> None:
    """The runner SHALL be able to execute both implementations.

    Asserted through the registry the runner actually dispatches on, so this
    cannot pass while the runner has lost the ability to drive one of them.
    """
    runner = _load_runner()
    for name in ("rust", "python"):
        assert name in runner.IMPLS, f"conformance runner does not register {name!r}"

    args = argparse.Namespace(rust_bin=Path("rust-cli"), python_bin=Path("py"))
    case = {"name": "meta", "input": "inputs/meta.hex", "expected": "expected/meta.csv"}
    sources = [Path("meta.mie")]

    for name in ("rust", "python"):
        command = runner.IMPLS[name].command(args, case, sources, Path("out.csv"))
        # The decode invocation carries the input and the destination. A
        # registry entry that built a command missing either would run, exit 0
        # and compare nothing.
        assert "decode" in command, f"{name} decode command lacks the subcommand: {command}"
        assert str(sources[0]) in command, f"{name} decode command lacks the input: {command}"
        assert "-o" in command, f"{name} decode command lacks an output flag: {command}"

        counting = runner.IMPLS[name].command(
            args, {"name": "meta", "mode": "count", "input": "inputs/meta.hex"}, sources, None
        )
        assert "count" in counting, f"{name} count command lacks the subcommand: {counting}"


@pytest.mark.requirement("L2-CONF-002")
def test_conformance_runner_comparison_rejects_a_difference() -> None:
    """The runner's byte comparison SHALL fail on differing output.

    The registry above proves the runner can drive each implementation; this
    proves the thing it does with their output actually distinguishes them. A
    comparison helper that silently accepted everything would leave every
    conformance case passing vacuously.
    """
    runner = _load_runner()
    runner.require_equal(b"same\n", b"same\n", "expected", "actual")
    with pytest.raises(AssertionError):
        runner.require_equal(b"expected\n", b"actual\n", "expected", "actual")


@pytest.mark.requirement("L2-CONF-003")
def test_conformance_manifest_has_cases_with_oracles() -> None:
    """The manifest SHALL list at least one case, each with both a
    hexadecimal input fixture and a checked-in CSV oracle path."""
    manifest_path = _CONFORMANCE_DIR / "manifest.json"
    assert manifest_path.is_file(), f"manifest missing at {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest["cases"] if isinstance(manifest, dict) else manifest
    assert cases, "manifest must contain at least one conformance case"
    inputs_dir = _CONFORMANCE_DIR / "inputs"
    expected_dir = _CONFORMANCE_DIR / "expected"
    for case in cases:
        name = case.get("name", "<unnamed>")
        # A case has either a single 'input' or a multi-file merge 'inputs'.
        specs = case.get("inputs") or ([case["input"]] if "input" in case else None)
        assert specs, f"case {name!r} missing 'input' or 'inputs'"
        # Negative cases (expected_exit != 0) may not have an oracle. Positive
        # cases carry one of three:
        #   'expected'                 — the main output, byte-exact;
        #   'expected_partial'         — an --allow-partial case, whose output
        #                                lands at <output>.partial;
        #   'compare_stdout'           — stdout, compared across the
        #                                implementations; for a case ending in
        #                                help, whose text cli_message_parity
        #                                already pins.
        if case.get("expected_exit", 0) == 0 and not case.get("compare_stdout"):
            oracle_key = "expected" if "expected" in case else "expected_partial"
            assert oracle_key in case, f"case {name!r} missing 'expected'/'expected_partial'"
            oracle = expected_dir / Path(case[oracle_key]).name
            assert oracle.is_file(), f"oracle for case {name!r} missing at {oracle}"
        for spec in specs:
            # `<missing>` names an input the runner deliberately never
            # creates (a merge input that cannot be opened, L2-MRG-004).
            if spec == "<missing>":
                continue
            fixture = inputs_dir / Path(spec).name
            assert fixture.is_file(), f"input fixture for case {name!r} missing at {fixture}"


@pytest.mark.requirement("L2-CONF-005")
def test_conformance_job_present_in_ci() -> None:
    """The CI workflow SHALL contain a job that invokes the conformance
    runner, so L2-CONF-005 ("CI runs the conformance suite on every push
    and pull request") has live evidence beyond spec review."""
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file(), f"CI workflow missing at {ci_path}"
    body = ci_path.read_text(encoding="utf-8")
    assert "tests/conformance/run.py" in body, (
        "CI workflow does not appear to invoke the conformance runner"
    )


@pytest.mark.requirement("L3-RS-020")
def test_windows_binary_is_static_checked_and_conformance_tested_in_ci() -> None:
    """The Windows x64 binary SHALL link the C runtime statically, and CI SHALL
    reject one that imports a C runtime DLL, check its version, run the
    conformance suite against it, and upload it under its release name."""
    config = (_REPO_ROOT / "rust" / ".cargo" / "config.toml").read_text(encoding="utf-8")
    static = (
        r"\[target\.x86_64-pc-windows-msvc\]\s*\n"
        r'rustflags = \["-C", "target-feature=\+crt-static"\]'
    )
    assert re.search(static, config), "the Windows C runtime is not linked statically"
    body = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python scripts/pe-imports.py rust/target/release/aero1553.exe" in body, (
        "no C runtime import check on the Windows release binary"
    )
    assert "--rust-bin rust/target/release/aero1553.exe" in body, (
        "CI does not run the conformance suite against the Windows release binary"
    )
    assert "name: aero1553-x86_64-windows.exe" in body, "the Windows binary is not uploaded"


@pytest.mark.requirement("L3-RS-018")
def test_glibc_2_22_binary_is_built_checked_and_conformance_tested_in_ci() -> None:
    """CI SHALL build the Rust binary for SLES 12 SP5 (glibc 2.22, kernel
    4.12) with a pinned cargo-libc, reject a symbol newer than GLIBC_2.22, run
    the tests under the simulated kernel, and run the conformance suite
    against that binary rather than a dev build."""
    body = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"CARGO_LIBC_VERSION: \d+\.\d+\.\d+\n", body), "cargo-libc is not pinned"
    assert "cargo libc build --distro sles-12-sp5" in body, "no SLES 12 SP5 build in CI"
    assert "--max-glibc 2.22" in body, "CI does not check the binary's glibc symbol versions"
    assert "cargo libc test --distro sles-12-sp5 --runner local" in body, (
        "CI does not run the tests under the simulated SLES 12 SP5 kernel"
    )
    pinned = r"SLES_IMAGE: registry\.suse\.com/suse/sles12sp5@sha256:[0-9a-f]{64}\n"
    assert re.search(pinned, body), "the SLES 12 SP5 userland image is not pinned by digest"
    assert '--target-libgcc "$sles/lib64/libgcc_s.so.1"' in body, (
        "CI does not check the binary's libgcc_s needs against SUSE's libgcc_s.so.1"
    )
    assert '--rust-bin "$RUNNER_TEMP/on-sles"' in body, (
        "CI does not run the conformance suite on SUSE's glibc and libgcc_s"
    )
    assert "--rust-bin rust/target/x86_64-unknown-linux-gnu/release/aero1553" in body, (
        "CI does not run the conformance suite against the glibc 2.22 binary"
    )


@pytest.mark.requirement("L3-RS-021")
def test_offline_builds_are_proven_in_ci() -> None:
    """CI SHALL gather a bundle and build from it offline on both platforms, with the
    network cut on Linux and the bundled Rust installer on both, running the scripts
    docs/BUILDING.md tells an operator to run."""
    body = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python3 scripts/offline-bundle.py" in body, "no Linux bundle in CI"
    assert "python scripts/offline-bundle.py" in body, "no Windows bundle in CI"
    assert "sudo unshare -n --" in body, "the Linux offline build does not cut the network"
    assert "PATH=/usr/bin:/bin" in body, "the Linux offline build can see the runner's Rust"
    assert 'bundle/source/scripts/offline-build.sh"' in body
    assert "bundle\\source\\scripts\\offline-build.ps1" in body
    assert "msiexec.exe" in body, "the Windows offline build does not use the bundled installer"
    guide = (_REPO_ROOT / "docs" / "BUILDING.md").read_text(encoding="utf-8")
    for script in ("offline-bundle.py", "offline-build.sh", "offline-build.ps1", "pe-imports.py"):
        assert script in guide, f"docs/BUILDING.md does not explain {script}"
