"""Check the checks: prove each generated check can fail, then sweep seeds.

    python tests/conformance/selftest.py [--rust-bin PATH] [--seeds N]

Run from the repository root in the Python package's environment, after
``cargo build`` in ``rust/`` (the daily fuzz workflow runs it). Two parts:

1. **Every check bites.** ``broken_cli.py`` wraps the real CLI and breaks one
   rule per mode. Each check is run against the mode that breaks its rule and
   must FAIL, naming that rule. A check that passes against a CLI breaking its
   rule has stopped checking -- after a refactor of the harness, say -- and the
   normal suite cannot tell: it only ever sees a correct CLI, where a working
   check and a broken one both pass. ``refuse-all``, a CLI that refuses every
   run, must trip flag_pairs' coverage floors, since every comparison between
   two refusals passes.

2. **A seed sweep.** ``flag_pairs.py`` over ``--seeds`` seeds (default 20)
   against every implementation: different seeds build different
   combinations, so the sweep reaches pairs-of-values in more company than the
   one seed CI runs on every push.

Exit 0 when every check bit and the sweep passed.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

SUITE = Path(__file__).resolve().parent
ROOT = SUITE.parents[1]
sys.path.insert(0, str(SUITE))

import run  # noqa: E402  (the runner's helpers, so the commands are the suite's own)
from flag_pairs import check_flag_pairs  # noqa: E402
from stdin_parity import check_stdin_parity  # noqa: E402

BROKEN = SUITE / "broken_cli.py"

#: Mode of broken_cli.py -> text the failing check's report must contain.
FLAG_PAIRS_BITES = {
    "threads": "thread count is invisible",
    "split": "--separate-errors does not partition",
    "filter": "the filter ADDED",
    "stdin": "standard input differs from the file",
    "destination": "destination is invisible",
    "panic": "undocumented exit code 101",
    "impl": "differ in stdout",
    "refuse-all": "coverage:",
}


def broken(mode: str, real: Path) -> list[str]:
    return [sys.executable, str(BROKEN), mode, str(real)]


def expect_failure(name: str, needle: str, check: Callable[[], None]) -> str | None:
    """Run ``check``; it must raise AssertionError mentioning ``needle``.
    Returns a problem, or None when the check bit as it should."""
    try:
        check()
    except AssertionError as exc:
        if needle in str(exc):
            print(f"PASS bites: {name} (failed as it should: {needle!r})")
            return None
        return f"{name}: failed, but not on {needle!r}:\n{str(exc)[:800]}"
    return f"{name}: PASSED against a CLI that breaks its rule -- the check is not checking"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    exe = ".exe" if sys.platform == "win32" else ""
    parser.add_argument(
        "--rust-bin", type=Path, default=ROOT / "rust" / "target" / "debug" / f"aero1553{exe}"
    )
    parser.add_argument("--seeds", type=int, default=20, help="Seeds in the sweep (default 20).")
    args = parser.parse_args()
    real = args.rust_bin.resolve()
    if not real.exists():
        print(f"no Rust CLI at {real}; run `cargo build` in rust/ first", file=sys.stderr)
        return 2
    problems: list[str] = []

    with tempfile.TemporaryDirectory(prefix="mie-selftest-") as tmp:
        temp = Path(tmp)

        def scratch() -> Path:
            return Path(tempfile.mkdtemp(dir=temp))

        # 1. Every check bites.
        os.environ.pop("MIE_FLAG_PAIRS_SEEDS", None)
        for mode, needle in FLAG_PAIRS_BITES.items():
            if mode == "impl":
                invocations = {"Rust": broken("none", real), "Second": broken("impl", real)}
            else:
                invocations = {"Rust": broken(mode, real)}
            problem = expect_failure(
                f"flag-pairs vs broken_cli {mode}",
                needle,
                lambda inv=invocations: check_flag_pairs(inv, ROOT, SUITE, scratch()),
            )
            if problem:
                problems.append(problem)

        manifest = run.json.loads(run.MANIFEST.read_text(encoding="utf-8"))["cases"]
        problem = expect_failure(
            "stdin-parity vs broken_cli stdin",
            "stdin-parity",
            lambda: check_stdin_parity(
                {"Rust": broken("stdin", real)},
                ROOT,
                manifest,
                lambda spec: run.read_hex(SUITE / spec),
                run._build_command,  # noqa: SLF001 -- the suite's own command builder
                tuple(run._STDOUT_MODES),  # noqa: SLF001
                scratch(),
            ),
        )
        if problem:
            problems.append(problem)

        # 2. The seed sweep, against the real implementations.
        invocations = {"Rust": [str(real)], "Python": [sys.executable, "-m", "aero1553"]}
        os.environ["MIE_FLAG_PAIRS_SEEDS"] = str(args.seeds)
        try:
            check_flag_pairs(invocations, ROOT, SUITE, scratch())
        except AssertionError as exc:
            problems.append(f"seed sweep: {exc}")
        finally:
            os.environ.pop("MIE_FLAG_PAIRS_SEEDS", None)

    if problems:
        print("\nFAIL\n\n" + "\n\n".join(problems), file=sys.stderr)
        return 1
    print(f"selftest: every check bit; {args.seeds}-seed flag-pairs sweep passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
