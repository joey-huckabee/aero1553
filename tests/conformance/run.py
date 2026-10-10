#!/usr/bin/env python3
"""Compare shared MIE decoding behavior across every implementation."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from broken_pipe import check_broken_pipe
from cli_message_parity import check_cli_message_parity
from config_fuzz import check_config_parser_fuzz
from config_parity import check_config_parser_parity
from config_path_parity import check_config_path_parity
from differential import describe_stderr_problems, normalized_stderr
from glob_parity import check_glob_parity
from long_paths import check_long_paths
from output_mode import check_output_mode
from record_fuzz import check_record_stream_fuzz
from symlinks import check_symlinks

ROOT = Path(__file__).resolve().parents[2]
SUITE = Path(__file__).resolve().parent
MANIFEST = SUITE / "manifest.json"

# Closed schema for case objects. ``tests/conformance/README.md``
# specifies that unknown fields SHALL be rejected by the runner so a
# typo (e.g. ``arg`` instead of ``args``) cannot silently disable a
# per-case override. ``FIELD_TYPES`` doubles as the allowed-field set;
# keep it in lockstep with the schema table in the README.
#
# The Rust and Python CLIs share one argument surface, so a single
# ``args`` vector is passed verbatim to both — there is no per-impl
# argument translation.
FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "input": str,
    "inputs": list,  # multi-file merge: list of hex input paths
    "input_names": list,  # override materialized temp file name(s) (L2-WRT-020)
    # Override the destination's file NAME, placing it in the same directory as
    # the materialized inputs. Without it the runner names the output itself and
    # puts it elsewhere, so a case cannot express a destination whose DERIVED
    # paths -- `<stem>_errors<ext>`, `<dest>.partial` -- land on an input
    # (L2-WRT-014). Each implementation gets its own subdirectory, so one that
    # regressed and wrote cannot corrupt the fixture the next one reads.
    "output_name": str,
    "expected": str,
    "expected_errors": str,
    "expected_partial": str,  # oracle for the <output>.partial (allow-partial)
    "config": str,
    "mode": str,
    "args": list,
    "expected_stderr_contains": str,
    # The payload is STDOUT, not the CSV: it must be non-empty and the same
    # bytes in every implementation. For a case that ends in help, whose text
    # `cli_message_parity` already pins; it stands in for "expected", which
    # such a case has no use for. It replaced a substring check
    # ("stdout contains 'decode'") written while the help texts differed --
    # Python's per subcommand, Rust's one screen, C++'s its own -- and so could
    # not be compared. Since v4.0.0 Python runs Rust's CLI, and since L7 C++
    # prints Rust's help, so the whole text can be.
    "compare_stdout": bool,
    "expected_exit": int,
    # Content written to the destination BEFORE the run, so the overwrite
    # contract can be pinned: `no_clobber` is off by default, so a decode must
    # replace an unrelated existing file rather than refuse it.
    "pre_existing_output": str,
    # Arguments placed BEFORE the subcommand. `args` goes after it, so a
    # global flag -- `--log-level`, `--config` -- cannot be exercised through
    # it. Rust parses globals in a separate loop from subcommand flags, so
    # without this the `--flag=value` spelling was only ever covered on one of
    # the two code paths.
    "global_args": list,
    # Arguments placed immediately AFTER the subcommand and BEFORE the input
    # paths. `args` goes after the paths (and after `-o`), which is too late
    # to exercise anything positional -- `--` in particular.
    "subcommand_args": list,
}
ALLOWED_MODES: frozenset[str] = frozenset({"decode", "count", "dump"})
# Modes whose payload is stdout rather than a written file. `dump` joined them
# once its report was made identical across the implementations: until then the
# three differed in their rule and arrow characters, and Python emitted CRLF on
# Windows, so there was no shared artifact to oracle against.
_STDOUT_MODES: frozenset[str] = frozenset({"count", "dump"})


def validate_case_schema(case: Any, index: int) -> None:
    """Reject malformed manifest cases with a clear, actionable error.

    Two failure classes are caught here so neither becomes a silent
    no-op at run time:

    1. **Unknown field name** — a misspelled key (e.g. ``arg``
       for ``args``) is otherwise ignored by ``case.get(...,
       default)`` and the case runs with default behavior.
    2. **Wrong field type** — e.g. ``"args": "single string"``
       (should be a list) would propagate downstream as a
       ``subprocess`` argument-list shape error far from the
       manifest entry that caused it.

    Fails fast on the first malformed case; rerun after fixing to
    see any subsequent ones.

    Raises:
        RuntimeError: if the case is not an object, lacks ``name``, has
            neither or both of ``input`` / ``inputs``, names an unknown
            field, holds a field of the wrong type (or a non-string list
            element), or gives a ``mode`` outside ``ALLOWED_MODES``.
    """
    if not isinstance(case, dict):
        raise RuntimeError(
            f"manifest case at index {index}: expected an object, got {type(case).__name__}"
        )
    name = case.get("name") if isinstance(case.get("name"), str) else None
    label = repr(name) if name else f"at index {index}"
    if "name" not in case:
        raise RuntimeError(f"manifest case {label}: missing required 'name' field")
    # Exactly one of 'input' (single file) or 'inputs' (multi-file merge).
    has_input = "input" in case
    has_inputs = "inputs" in case
    if not (has_input or has_inputs):
        raise RuntimeError(f"manifest case {label}: missing required 'input' or 'inputs' field")
    if has_input and has_inputs:
        raise RuntimeError(
            f"manifest case {label}: specify exactly one of 'input' or 'inputs', not both"
        )

    unknown = sorted(set(case) - set(FIELD_TYPES))
    if unknown:
        raise RuntimeError(
            f"manifest case {label}: unknown field(s) {unknown}. "
            f"Allowed fields are {sorted(FIELD_TYPES)}. "
            "Check tests/conformance/README.md for the schema."
        )

    for field, expected_type in FIELD_TYPES.items():
        if field not in case:
            continue
        if not isinstance(case[field], expected_type):
            type_name = (
                expected_type.__name__
                if isinstance(expected_type, type)
                else " or ".join(t.__name__ for t in expected_type)
            )
            raise RuntimeError(
                f"manifest case {label}: field {field!r} must be {type_name}, "
                f"got {type(case[field]).__name__}"
            )
        # list-typed fields must hold strings only — ``args`` ends up as
        # a CLI argument vector, where a non-string element would raise
        # far from the manifest entry.
        if expected_type is list:
            for i, item in enumerate(case[field]):
                if not isinstance(item, str):
                    raise RuntimeError(
                        f"manifest case {label}: field {field!r}[{i}] must be str, "
                        f"got {type(item).__name__}"
                    )

    if "mode" in case and case["mode"] not in ALLOWED_MODES:
        raise RuntimeError(
            f"manifest case {label}: mode {case['mode']!r} is not one of {sorted(ALLOWED_MODES)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rust-bin",
        type=Path,
        help="Use this Rust binary instead of rust/target/debug/aero1553.",
    )
    parser.add_argument(
        "--python-bin",
        type=Path,
        help="Use this Python interpreter for the Python CLI instead of the "
        "one running this script (sys.executable). Needed when the runner is "
        "not itself launched from an interpreter that has aero1553.",
    )
    parser.add_argument(
        "--cpp-bin",
        type=Path,
        help="Use this C++ binary instead of asking cpp/Makefile where it put "
        "one (`make -s print-decoder-bin`). The build directory name encodes "
        "the toolchain, so the Makefile is asked rather than re-derived here.",
    )
    parser.add_argument(
        "--update-expected",
        action="store_true",
        help="Update CSV oracles, but only when every implementation agrees.",
    )
    impl_group = parser.add_mutually_exclusive_group()
    impl_group.add_argument(
        "--only",
        metavar="IMPLS",
        help=(
            "Comma-separated implementations to run, e.g. `--only cpp` or "
            "`--only rust,python`. Validates just those against the committed "
            "expected/ oracles, so a host missing a toolchain can still check "
            "its own side."
        ),
    )
    impl_group.add_argument(
        "--skip",
        metavar="IMPLS",
        help=(
            "Comma-separated implementations to leave out; the rest run. "
            "`--skip cpp` is the Rust/Python pairing this suite ran before the "
            "C++ implementation existed."
        ),
    )
    impl_group.add_argument(
        "--python-only",
        action="store_true",
        help="Deprecated alias for `--only python`.",
    )
    impl_group.add_argument(
        "--rust-only",
        action="store_true",
        help="Deprecated alias for `--only rust`.",
    )
    parser.add_argument(
        "--temp-root",
        type=Path,
        help="Create temporary files under this directory.",
    )
    return parser.parse_args()


def select_impls(args: argparse.Namespace) -> list[ImplSpec]:
    """Resolve which implementations this run covers.

    The default is EVERY registered implementation, and opting out is
    explicit. The alternative -- quietly running whichever binaries happen to
    be present -- would let a job whose build step silently failed still report
    a full pass, which is the failure mode where a green gate proves nothing.

    Raises:
        ValueError: if ``--only`` / ``--skip`` names no implementation or an
            unknown one, or ``--skip`` leaves none to run.
    """
    known = list(IMPLS)
    if args.rust_only:
        args.only = "rust"
    if args.python_only:
        args.only = "python"

    def parse_list(value: str, flag: str) -> list[str]:
        names = [piece.strip() for piece in value.split(",") if piece.strip()]
        if not names:
            raise ValueError(f"{flag} needs at least one implementation name")
        for name in names:
            if name not in IMPLS:
                raise ValueError(
                    f"{flag}: unknown implementation {name!r}; "
                    f"known implementations are {', '.join(known)}"
                )
        return names

    if args.only:
        chosen = parse_list(args.only, "--only")
    elif args.skip:
        skipped = parse_list(args.skip, "--skip")
        chosen = [name for name in known if name not in skipped]
        if not chosen:
            raise ValueError("--skip left no implementations to run")
    else:
        chosen = known
    return [IMPLS[name] for name in chosen]


def read_hex(path: Path) -> bytes:
    chunks = [line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()]
    return bytes.fromhex("".join(chunks))


#: An ``inputs`` entry naming a file that must NOT exist: the input is given a
#: path but nothing is written there, so a case can pin what an implementation
#: does with an input it cannot open (L2-MRG-004).
MISSING_INPUT = "<missing>"


def materialize(
    into: Path, input_specs: list[str], input_names: list[str] | None, case_name: str
) -> list[Path]:
    """A case's hex fixtures written as .mie files under `into`.

    Each file is named from ``input_names`` when the case gives them, else
    ``<case_name>-in<i>.mie``. A :data:`MISSING_INPUT` entry gets a name and no
    file. Module-level, with every input passed in, so it cannot read a
    previous case's names through a closure over the case loop.
    """
    into.mkdir(parents=True, exist_ok=True)
    written = []
    for i, spec in enumerate(input_specs):
        fname = input_names[i] if input_names else f"{case_name}-in{i}.mie"
        src = into / fname
        if spec == MISSING_INPUT:
            src.unlink(missing_ok=True)
        else:
            src.write_bytes(read_hex(SUITE / spec))
        written.append(src)
    return written


def _readable(stream: bytes | str | None) -> str:
    """A captured stream as text for a failure message."""
    if stream is None:
        return ""
    if isinstance(stream, str):
        return stream
    return stream.decode("utf-8", "replace")


# Log lines below WARN are left out of the stderr oracle: L2-CLI-022 leaves the
# text of the decoder's INFO and DEBUG lines free. A case whose point is that
# such a line appears asserts it with ``expected_stderr_contains``.
_FREE_LOG_LEVELS = ("INFO [", "DEBUG [")


def stderr_oracle_path(case_name: str) -> Path:
    """Where a case's stderr oracle lives. No file means stderr must be empty."""
    return SUITE / "expected" / f"{case_name}.stderr"


def path_placeholders(
    case: dict[str, Any],
    sources: list[Path],
    output: Path | None,
    temp: Path,
) -> dict[str, str]:
    """Each run-specific path one implementation was given, mapped to the
    placeholder that stands for it in the stderr oracle.

    Every implementation repeats a path as it was given (L2-CLI-022), so these
    are the only parts of the diagnostics that legitimately differ between
    runs, implementations and hosts. Each implementation writes into a
    directory of its own under one file name, so a path derived from the
    destination -- the errors file, a ``.partial`` -- normalises too.
    """
    paths = {str(ROOT): "<ROOT>", str(temp): "<TEMP>"}
    if output is not None:
        paths[str(output.parent)] = "<OUTDIR>"
        paths[str(output)] = "<OUTPUT>"
        paths[str(_errors_path(output))] = "<ERRORS>"
    for i, src in enumerate(sources):
        paths[str(src)] = f"<INPUT{i}>"
    if config := case.get("config"):
        paths[str((SUITE / config).resolve())] = "<CONFIG>"
    return paths


def comparable_stderr(stderr: bytes, placeholders: dict[str, str]) -> str:
    """``stderr`` as its oracle holds it: paths replaced by placeholders, and
    the INFO and DEBUG lines left out. Line endings are kept."""
    text = normalized_stderr(stderr, placeholders)
    return "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith(_FREE_LOG_LEVELS)
    )


def check_stderr(
    name: str,
    stderrs: dict[str, str],
    update: bool,
) -> None:
    """Hold every implementation's stderr to the others' and to the oracle.

    Agreement is checked first, so a divergence is reported as two actual
    outputs side by side, the way the CSV is; then the oracle, which is what
    catches a line every implementation lost or changed at once.

    Raises:
        AssertionError: on a divergence, a non-ASCII byte, or a mismatch
            with the oracle.
    """
    problem = describe_stderr_problems(stderrs)
    if problem:
        raise AssertionError(f"{name}: {problem}")
    oracle = stderr_oracle_path(name)
    agreed = next(iter(stderrs.values()))
    if update:
        if agreed:
            oracle.write_bytes(agreed.encode("utf-8", "surrogateescape"))
            print(f"UPDATED {oracle.relative_to(ROOT)}")
        elif oracle.exists():
            oracle.unlink()
            print(f"REMOVED {oracle.relative_to(ROOT)}")
    expected = oracle.read_bytes().decode("utf-8", "surrogateescape") if oracle.exists() else ""
    problem = describe_stderr_problems({f"oracle {oracle.name}": expected, **stderrs})
    if problem:
        raise AssertionError(f"{name}: {problem}")


def check_no_stray_stderr_oracles(case_names: set[str]) -> None:
    """Fail on a stderr oracle whose case is gone or renamed. A missing oracle
    means "stderr is empty", so one left behind under an old name would
    otherwise just stop being read.

    Raises:
        RuntimeError: naming every oracle with no case in the manifest.
    """
    stray = sorted(
        path.name
        for path in (SUITE / "expected").glob("*.stderr")
        if path.name[: -len(".stderr")] not in case_names
    )
    if stray:
        raise RuntimeError(f"stderr oracles with no manifest case: {stray}")


def run_command(
    command: list[str],
    output: Path | None,
    case_name: str,
    implementation: str,
    expected_exit: int = 0,
    read_path: Path | None = None,
    payload_is_stdout: bool = False,
) -> tuple[bytes | None, bytes]:
    """Run one implementation's CLI and assert its exit code matches.

    Returns ``(payload, stderr)`` where ``payload`` is:
      - the CSV bytes from ``output`` when ``output`` is a path and
        ``expected_exit == 0`` (the historic decode-mode behavior);
      - the captured stdout bytes when ``output is None`` (used by the
        ``count`` mode, where stdout *is* the data being compared);
      - ``None`` for negative cases (no payload expected).
    ``stderr`` is always returned, for the exact comparison against the
    case's stderr oracle.

    Both streams are captured as BYTES. Text mode would translate line endings
    on the way in, and both are compared with theirs: a run that ended its
    lines CRLF on Windows would have read exactly like one that ended them LF.

    Raises:
        RuntimeError: on an unexpected exit code, a command timeout, or a
            missing output file.
    """
    print(f"RUN  {case_name} ({implementation})", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        rendered = subprocess.list2cmdline(command)
        raise RuntimeError(
            f"{case_name}: {implementation} exceeded 30 seconds\n"
            f"command: {rendered}\n"
            f"stdout:\n{_readable(exc.stdout)}\n"
            f"stderr:\n{_readable(exc.stderr)}"
        ) from exc
    if result.returncode != expected_exit:
        rendered = subprocess.list2cmdline(command)
        raise RuntimeError(
            f"{case_name}: {implementation} exited {result.returncode}, expected {expected_exit}\n"
            f"command: {rendered}\n"
            f"stdout:\n{_readable(result.stdout)}\n"
            f"stderr:\n{_readable(result.stderr)}"
        )
    if expected_exit != 0:
        # Negative case — no payload expected; its stderr is what the case
        # is about.
        return None, result.stderr
    if payload_is_stdout:
        # The case asserts on stdout instead of a CSV -- see
        # ``compare_stdout``. `decode` still needs its `-o`, so the
        # destination exists and is simply not what is being compared; a run
        # that printed help never created it, and that is not a failure here.
        return result.stdout, result.stderr
    if output is None:
        # Stdout-comparison mode (e.g. `count`), where stdout is the data.
        return result.stdout, result.stderr
    # An --allow-partial decode lands its output at ``<output>.partial`` rather
    # than ``<output>``; ``read_path`` points the comparison at the real artifact.
    read_target = read_path if read_path is not None else output
    if not read_target.exists():
        raise RuntimeError(f"{case_name}: {implementation} did not create {read_target}")
    return read_target.read_bytes(), result.stderr


def _build_command(
    command: list[str],
    case: dict[str, Any],
    sources: list[Path],
    output: Path | None,
) -> list[str]:
    """Append the shared subcommand/flag tail to a CLI prefix.

    ``sources`` is one path for a single-input case, or several for a
    multi-file merge (``decode`` takes them as positionals). ``output`` is the
    per-case scratch CSV path for ``mode == "decode"``, or ``None`` for
    ``mode == "count"`` (stdout-comparison mode, no -o). ``--config`` is global
    (before the subcommand) for both CLIs.
    """
    if config := case.get("config"):
        command += ["--config", str((SUITE / config).resolve())]
    # Before the subcommand, which is what makes these GLOBAL.
    command += case.get("global_args", [])
    mode = case.get("mode", "decode")
    if mode in _STDOUT_MODES:
        # `count` and `dump` write their result to stdout and take exactly one
        # input, so there is no -o and no second positional.
        command += [mode, *case.get("subcommand_args", []), str(sources[0])]
    else:
        command += [
            "decode",
            *case.get("subcommand_args", []),
            *(str(s) for s in sources),
            "-o",
            str(output),
        ]
    command += case.get("args", [])
    return command


def prepare_rust_bin(args: argparse.Namespace) -> None:
    if args.rust_bin:
        args.rust_bin = args.rust_bin.resolve()
    else:
        suffix = ".exe" if sys.platform == "win32" else ""
        args.rust_bin = ROOT / "rust" / "target" / "debug" / f"aero1553{suffix}"

    if args.rust_bin.exists():
        return
    if shutil.which("cargo") is None:
        raise RuntimeError("cargo was not found; pass --rust-bin or install Rust")

    print("BUILD Rust CLI", flush=True)
    # 120s was too tight and flaked the windows-latest conformance job on a
    # cold cache: a from-scratch debug build plus link genuinely exceeds two
    # minutes on the slowest runner in the matrix.
    #
    # The timeout is kept -- it exists to stop a hung build from consuming the
    # whole job budget, and removing it would trade a visible flake for an
    # invisible stall. It is raised to a figure that a real build cannot reach
    # but a hang comfortably will.
    result = subprocess.run(
        ["cargo", "build", "--quiet", "--locked", "--bin", "aero1553"],
        cwd=ROOT / "rust",
        check=False,
        timeout=600,
    )
    if result.returncode != 0 or not args.rust_bin.exists():
        raise RuntimeError("failed to build the Rust CLI")


def prepare_python_bin(args: argparse.Namespace) -> None:
    """Resolve the Python interpreter that will run the Python aero1553 CLI.

    Default to :data:`sys.executable`. When the runner is invoked under
    ``uv --directory python run python ...``, the active interpreter is
    the project environment's and already has ``aero1553`` installed, so
    no second lookup of "the environment's interpreter" is needed -- one
    that could resolve to a different interpreter than the one the package
    was installed into. The interpreter is sanity-checked by importing ``aero1553``
    so the runner fails fast with a clear error rather than emitting a
    confusing ``No module named aero1553`` for every case.

    Raises:
        RuntimeError: if the interpreter does not exist or cannot import
            ``aero1553``.
    """
    # absolute(), NOT resolve(): a virtualenv's bin/python is a SYMLINK to the
    # base interpreter, and resolving it throws the venv away. `python3 -m venv
    # /tmp/v && /tmp/v/bin/pip install -e ./python` then failed with
    # "aero1553 is not importable from /usr/bin/python3.10" -- naming an
    # interpreter the caller never asked for, which reads as the package being
    # uninstalled rather than the path being rewritten. CI never hit it because
    # a Windows venv's python is a real file and CI's Linux job installs into
    # the system interpreter. absolute() still normalises a relative path.
    if args.python_bin:
        args.python_bin = args.python_bin.absolute()
    else:
        args.python_bin = Path(sys.executable).absolute()

    if not args.python_bin.exists():
        raise RuntimeError(f"Python interpreter was not found: {args.python_bin}")

    probe = subprocess.run(
        [str(args.python_bin), "-c", "import aero1553"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            f"aero1553 is not importable from {args.python_bin}. "
            "Either install the package into this interpreter (e.g. "
            "`uv --directory python sync`) or pass --python-bin pointing at "
            "an interpreter that has it.\n"
            f"stderr:\n{probe.stderr}"
        )


def prepare_cpp_bin(args: argparse.Namespace) -> None:
    """Resolve the C++ decoder binary.

    The Makefile is ASKED where the binary is rather than the path being
    re-derived here, because the build directory name encodes the target
    triple, the compiler version and the sanitizer tier -- a runner that
    guessed it would silently test yesterday's binary whenever any of those
    changed. On Windows, where the build is CMake/MSVC and there may be no
    make, the usual layouts are searched instead.

    The binary is never built here. Unlike `cargo build`, a C++ build needs a
    toolchain choice (make vs MSVC, which sanitizer tier), and guessing wrong
    would produce a confusing failure inside a build the caller did not ask
    for.

    Raises:
        RuntimeError: if ``--cpp-bin`` names a missing file, or no binary is
            found by asking the Makefile or searching the build layouts.
    """
    if args.cpp_bin:
        args.cpp_bin = args.cpp_bin.resolve()
        if not args.cpp_bin.exists():
            raise RuntimeError(f"--cpp-bin does not exist: {args.cpp_bin}")
        return

    cpp_dir = ROOT / "cpp"
    candidate: Path | None = None
    if shutil.which("make"):
        probe = subprocess.run(
            ["make", "-s", "print-decoder-bin"],
            cwd=cpp_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        reported = probe.stdout.strip()
        if probe.returncode == 0 and reported:
            candidate = (cpp_dir / reported).resolve()

    if candidate is None or not candidate.exists():
        suffix = ".exe" if sys.platform == "win32" else ""
        found = sorted(cpp_dir.glob(f"build*/**/aero1553{suffix}"))
        candidate = found[-1].resolve() if found else candidate

    if candidate is None or not candidate.exists():
        raise RuntimeError(
            "the C++ decoder binary was not found. Build it first "
            "(`cd cpp && make all`, or the CMake/MSVC build on Windows) or "
            "pass --cpp-bin. To run this suite without the C++ "
            "implementation, pass `--skip cpp`."
        )
    args.cpp_bin = candidate


class ImplSpec:
    """One implementation, and everything the runner needs to drive it.

    A registry entry rather than a hard-wired pair of code paths: the suite ran
    against exactly two implementations for its whole life, and every
    comparison, skip and oracle check was written as `rust`/`python` variables
    side by side. Adding a third that way would have meant tripling roughly two
    hundred lines and getting each one right; adding it here means one entry.
    """

    def __init__(
        self,
        name: str,
        label: str,
        prepare: Callable[[argparse.Namespace], None],
        prefix: Callable[[argparse.Namespace], list[str]],
        unsupported: Callable[[dict[str, Any]], str | None] | None = None,
        full_cli_surface: bool = True,
    ) -> None:
        self.name = name
        self.label = label
        self.prepare = prepare
        self.prefix = prefix
        self._unsupported = unsupported
        # Whether this implementation takes part in the all-pairs flag-surface
        # comparison. An implementation still being delivered in phases has a
        # deliberately smaller surface, and comparing it would fail by design.
        self.full_cli_surface = full_cli_surface

    def command(
        self,
        args: argparse.Namespace,
        case: dict[str, Any],
        sources: list[Path],
        output: Path | None,
    ) -> list[str]:
        return _build_command(self.prefix(args), case, sources, output)

    def unsupported(self, case: dict[str, Any]) -> str | None:
        """Why this implementation cannot run `case`, or None if it can.

        The phase limitation belongs to the IMPLEMENTATION, not to the case: a
        merge case is a perfectly good case, it is the C++ build that has no
        merge yet. Marking it on the case would mean editing the manifest twice
        -- once to exclude, once to put back -- and in between, the manifest
        would misdescribe the contract.
        """
        return self._unsupported(case) if self._unsupported else None


IMPLS: dict[str, ImplSpec] = {
    "rust": ImplSpec(
        name="rust",
        label="Rust",
        prepare=prepare_rust_bin,
        prefix=lambda args: [str(args.rust_bin)],
    ),
    "python": ImplSpec(
        name="python",
        label="Python",
        prepare=prepare_python_bin,
        # The Python and Rust CLIs share one argument surface -- global
        # --config before the subcommand, a count subcommand, and an identical
        # decode flag set -- so this differs only in the entrypoint prefix. The
        # per-case `args` are passed verbatim to every implementation.
        prefix=lambda args: [str(args.python_bin), "-m", "aero1553"],
    ),
    "cpp": ImplSpec(
        name="cpp",
        label="C++",
        prepare=prepare_cpp_bin,
        prefix=lambda args: [str(args.cpp_bin)],
    ),
}


def _errors_path(main_output: Path) -> Path:
    """Derive the split-mode errors path for a given main output path.

    The L2-ERR-008 rule, written out here rather than imported, so the runner
    stays an independent check on the writers: `_errors` goes in front of the
    file name's final `.`, unless that `.` leads the name -- `out.csv` →
    `out_errors.csv`, `out` → `out_errors`, `o.` → `o_errors.`.

    Not `pathlib`'s `stem` / `suffix`: those split a name ending in `.`
    differently from the rule, and differently again from Python 3.14 on.
    """
    name = main_output.name
    dot = name.rfind(".")
    if dot > 0:
        return main_output.with_name(f"{name[:dot]}_errors{name[dot:]}")
    return main_output.with_name(f"{name}_errors")


def diff_bytes(
    expected: bytes,
    actual: bytes,
    expected_name: str,
    actual_name: str,
) -> str:
    return "".join(
        difflib.unified_diff(
            expected.decode("utf-8").splitlines(keepends=True),
            actual.decode("utf-8").splitlines(keepends=True),
            fromfile=expected_name,
            tofile=actual_name,
        )
    )


def require_equal(
    expected: bytes,
    actual: bytes,
    expected_name: str,
    actual_name: str,
) -> None:
    if expected == actual:
        return
    raise AssertionError(
        f"{actual_name} does not match {expected_name}\n"
        f"{diff_bytes(expected, actual, expected_name, actual_name)}"
    )


# Long-option token, e.g. `--exclude-types`. The CLIs expose no flags with
# digits, but the pattern allows them defensively.
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
# Subcommands whose --help is scanned in addition to the top-level --help.
_HELP_SUBCOMMANDS = ("decode", "count", "dump")


def _help_flags(base_command: list[str]) -> set[str]:
    """Union of long-option flags across a CLI's top-level and per-subcommand
    ``--help`` output. The Rust help is one combined block (every flag appears
    regardless of subcommand), so its union comes from any single ``--help``;
    the Python argparse help is per-subcommand, so the union spans them all."""
    flags: set[str] = set()
    invocations = [[*base_command, "--help"]]
    invocations += [[*base_command, sub, "--help"] for sub in _HELP_SUBCOMMANDS]
    for inv in invocations:
        result = subprocess.run(inv, capture_output=True, text=True, check=False)
        flags |= set(_FLAG_RE.findall(result.stdout + result.stderr))
    return flags


def check_cli_surface(args: argparse.Namespace, impls: list[ImplSpec]) -> None:
    """Assert the CLIs expose an identical long-flag set across
    ``decode`` / ``count`` / ``dump`` (plus global options).

    The implementations are kept to one identical argument surface (L1-CLI-001
    only *requires* matching capabilities, but parity is maintained in
    practice). A flag added to one but not the others -- or a help text that
    stops advertising a flag the parser still accepts -- fails here, guarding
    cross-implementation parity against silent drift.

    Implementations still being delivered in phases are excluded by their
    registry entry: their surface is deliberately smaller, so comparing it
    would fail by design rather than on a regression. They are named in the
    output, so the exclusion cannot pass unnoticed.

    Raises:
        AssertionError: if any compared implementation's flag set differs
            from the first one's.
    """
    excluded = [impl.label for impl in impls if not impl.full_cli_surface]
    comparable = [impl for impl in impls if impl.full_cli_surface]
    if len(comparable) < 2:
        print(
            "SKIP cli-surface-parity "
            f"(needs two full-surface implementations; have {len(comparable)})"
        )
        return

    surfaces = {impl.label: _help_flags(impl.prefix(args)) for impl in comparable}
    reference_label, reference = next(iter(surfaces.items()))
    for label, flags in surfaces.items():
        if flags == reference:
            continue
        only_reference = sorted(reference - flags) or ["(none)"]
        only_other = sorted(flags - reference) or ["(none)"]
        raise AssertionError(
            "CLI flag surface diverged between implementations:\n"
            f"  only in {reference_label}: {', '.join(only_reference)}\n"
            f"  only in {label}: {', '.join(only_other)}"
        )
    note = f" (not compared: {', '.join(excluded)})" if excluded else ""
    print(f"PASS cli-surface-parity ({len(reference)} flags across {', '.join(surfaces)}){note}")


def main() -> int:
    args = parse_args()

    # Load + validate the manifest BEFORE preparing any toolchain so a
    # malformed manifest fails fast with a schema error instead of getting
    # masked behind a slow Rust build or an interpreter probe failure.
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for index, case in enumerate(manifest["cases"]):
        validate_case_schema(case, index)
    check_no_stray_stderr_oracles({case["name"] for case in manifest["cases"]})

    impls = select_impls(args)

    if args.update_expected and len(impls) != len(IMPLS):
        raise RuntimeError(
            "--update-expected rewrites the oracles only after confirming every "
            "implementation agrees, so it needs all of them; drop --only / --skip."
        )

    # Only prepare the toolchains we will actually run -- this is what lets a
    # single-implementation host (no cargo, no installed aero1553, no C++
    # build) still validate its side against the committed oracles.
    for impl in impls:
        impl.prepare(args)
    print(f"IMPLS {', '.join(impl.label for impl in impls)}")

    check_cli_surface(args, impls)

    passed = 0
    skipped = 0
    if args.temp_root:
        args.temp_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="mie-conformance-",
        dir=args.temp_root,
    ) as temp_dir:
        temp = Path(temp_dir)

        # Contract checks, not differential ones: each implementation is held to
        # the specification on its own, so they run at any implementation count.
        # The manifest cannot express any of them -- every case there drains the
        # output, the runner picks short paths, it compares bytes, never file
        # metadata, and it cannot make a symlink.
        contract = {impl.label: impl.prefix(args) for impl in impls}
        check_broken_pipe(contract, ROOT, temp)
        check_long_paths(contract, ROOT, temp)
        check_output_mode(contract, ROOT, temp)
        check_symlinks(contract, ROOT, temp)

        # Differential config-parser checks, over EVERY implementation under
        # test rather than a fixed pair. The comparison is all-pairs: any two
        # disagreeing is a finding, regardless of which is right. A majority
        # rule would let two implementations sharing a bug outvote the correct
        # one, and nominating a reference would make that reference's quirks
        # normative -- see tests/conformance/differential.py.
        #
        # These are the only checks that can catch a divergence BETWEEN
        # implementations' hand-rolled parsers. Each implementation's own unit
        # tests pin it against its own reading of the grammar, which is exactly
        # the thing in question when two readings differ.
        if len(impls) >= 2:
            parity_input = temp / "config-parity-in.mie"
            parity_input.write_bytes(read_hex(SUITE / "inputs" / "count-one.hex"))
            invocations = {impl.label: impl.prefix(args) for impl in impls}
            check_config_parser_parity(invocations, ROOT, parity_input, temp)
            check_config_parser_fuzz(invocations, ROOT, parity_input, temp)
            # The record-stream twin of the config fuzzer: the only check that
            # compares what the DECODERS produce on inputs nobody wrote. The
            # per-implementation fuzz harnesses each prove their own process did
            # not crash; this proves the three agree.
            check_record_stream_fuzz(invocations, ROOT, SUITE, temp)
            # Same idea one level up: the config *path*, not its contents.
            check_config_path_parity(invocations, ROOT, parity_input, temp)
            # And the input side of the same question: what `--glob` resolves
            # to. The wildcard matcher was pinned three times over; the
            # directory entries it was applied to were not, and that is where
            # the three implementations actually disagreed.
            (temp / "glob-parity-source.mie").write_bytes(parity_input.read_bytes())
            check_glob_parity(invocations, temp)
            # What the command line itself is told: usage errors, help and
            # version, compared byte for byte (L2-CLI-022).
            check_cli_message_parity(invocations, parity_input, temp)
        else:
            # A differential check needs something to differ from.
            print(
                "SKIP config-parser-parity / -fuzz / -path / glob-parity / cli-message-parity "
                f"(needs two or more implementations; have {len(impls)})"
            )

        for case in manifest["cases"]:
            name = case["name"]

            # Which implementations can run THIS case. An implementation still
            # being delivered in phases sits out the cases needing a module it
            # does not have; the reason is printed so the gap stays visible
            # rather than looking like coverage.
            running = []
            for impl in impls:
                reason = impl.unsupported(case)
                if reason is None:
                    running.append(impl)
                else:
                    print(f"SKIP {name} [{impl.label}] {reason}")
            if not running:
                skipped += 1
                continue

            # One 'input' or several 'inputs' (multi-file merge). Each hex
            # fixture is materialized to its own temp .mie and passed as a
            # positional to every CLI.
            input_specs = case.get("inputs") or [case["input"]]
            # L2-WRT-020: a case may override the materialized file name(s) so a
            # filename-derived feature (the MUX column) can be exercised. Names
            # are placed in a per-case subdirectory to avoid collisions.
            input_names = case.get("input_names")
            output_name = case.get("output_name")
            case_dir = temp / name if (input_names or output_name) else temp
            if case_dir is not temp:
                case_dir.mkdir(parents=True, exist_ok=True)

            # With `output_name` the destination sits BESIDE the inputs, so each
            # implementation needs its own directory: the point of such a case is
            # that nothing may be written, and sharing a directory would let a
            # regressed implementation corrupt the fixture the next one reads.
            per_impl_sources: dict[str, list[Path]] = {}
            if output_name:
                for impl in running:
                    per_impl_sources[impl.name] = materialize(
                        case_dir / impl.name, input_specs, input_names, name
                    )
                sources = per_impl_sources[running[0].name]
            else:
                sources = materialize(case_dir, input_specs, input_names, name)
            expected_exit = int(case.get("expected_exit", 0))
            mode = case.get("mode", "decode")

            # ``count`` mode compares stdout (the integer count) rather than a
            # CSV file, so the per-impl output paths are unused.
            outputs: dict[str, Path | None] = {}
            for impl in running:
                if mode in _STDOUT_MODES:
                    outputs[impl.name] = None
                elif output_name:
                    outputs[impl.name] = case_dir / impl.name / output_name
                else:
                    # One directory per implementation, one file name: a
                    # destination or a path derived from it then reads the same
                    # in every implementation's stderr once the directory is
                    # replaced by its placeholder.
                    (temp / impl.name).mkdir(exist_ok=True)
                    outputs[impl.name] = temp / impl.name / f"{name}.csv"

            # An --allow-partial case commits to ``<output>.partial``; read that
            # artifact for the comparison and oracle against ``expected_partial``.
            partial_oracle = case.get("expected_partial")

            # A case may ask for the destination to ALREADY EXIST, which is how
            # the overwrite contract gets pinned: `no_clobber` is off by
            # default, so a decode must replace an unrelated existing file
            # rather than refuse. The sentinel is deliberately not valid CSV --
            # if any of it survives into the comparison, the destination was
            # appended to rather than replaced, and the oracle diff says so.
            pre_existing = case.get("pre_existing_output")

            produced: dict[str, bytes | None] = {}
            captured_stderr: dict[str, bytes] = {}
            for impl in running:
                output = outputs[impl.name]
                if pre_existing is not None and output is not None:
                    output.write_text(pre_existing, encoding="utf-8")
                read_path = Path(f"{output}.partial") if partial_oracle and output else None
                produced[impl.name], captured_stderr[impl.name] = run_command(
                    impl.command(args, case, per_impl_sources.get(impl.name, sources), output),
                    output,
                    name,
                    impl.label,
                    expected_exit=expected_exit,
                    read_path=read_path,
                    payload_is_stdout=bool(case.get("compare_stdout")),
                )

            # L1-OUT-002 / L2-WRT-014, asserted on EVERY case rather than only
            # the ones about it: decoding never modifies an input, so a byte of
            # difference here means some destination -- the one named, or one
            # derived from it -- resolved onto a source file. That is exactly
            # the failure the derived-path collision guard exists to stop, and
            # it had been possible for the errors file and the `.partial` file
            # while every CSV oracle in this suite still matched.
            for impl in running:
                for i, src in enumerate(per_impl_sources.get(impl.name, sources)):
                    if input_specs[i] == MISSING_INPUT:
                        # Nothing may appear at a missing input's path either.
                        if src.exists():
                            raise AssertionError(
                                f"{name}: {impl.label} CREATED its missing input {src.name}"
                            )
                        continue
                    original = read_hex(SUITE / input_specs[i])
                    if src.read_bytes() != original:
                        raise AssertionError(
                            f"{name}: {impl.label} MODIFIED its input {src.name}; "
                            f"a decode must never write to a file it is reading"
                        )

            # Every case's stderr, exactly: the same in every implementation
            # and the same as the committed oracle, `expected/<case>.stderr`
            # (L2-CLI-022, L2-CONF-002). No oracle means nothing may be written.
            #
            # This runs BEFORE the negative-case short-circuit below. The
            # substring check it replaced once sat after it, so on any case with
            # a non-zero ``expected_exit`` the needle was never evaluated.
            #
            # It replaced a substring check because a substring cannot fail on
            # what it does not mention. Three cases in four had no stderr
            # assertion at all, so a new stray WARN went unnoticed in each of
            # them; and one case's needle stopped a word short of a divergence:
            # C++ said "for format 1" where Rust and Python said "for Receive".
            check_stderr(
                name,
                {
                    impl.label: comparable_stderr(
                        captured_stderr[impl.name],
                        path_placeholders(
                            case,
                            per_impl_sources.get(impl.name, sources),
                            outputs[impl.name],
                            temp,
                        ),
                    )
                    for impl in running
                },
                args.update_expected,
            )

            # A substring of the RAW stderr, for what the oracle leaves out:
            # a case whose point is that an INFO or DEBUG line appears.
            stderr_needle = case.get("expected_stderr_contains")
            if stderr_needle:
                for impl in running:
                    captured = _readable(captured_stderr[impl.name])
                    if stderr_needle not in captured:
                        raise AssertionError(
                            f"{name}: {impl.label} stderr does not contain "
                            f"{stderr_needle!r}\n--- stderr ---\n{captured}"
                        )

            if case.get("compare_stdout"):
                reference = running[0]
                if not produced[reference.name]:
                    raise AssertionError(f"{name}: {reference.label} wrote nothing to stdout")
                for impl in running[1:]:
                    require_equal(
                        produced[reference.name] or b"",
                        produced[impl.name] or b"",
                        f"{name} {reference.label} stdout",
                        f"{name} {impl.label} stdout",
                    )
                passed += 1
                print(f"PASS {name} (stdout identical)")
                continue

            if expected_exit != 0:
                # Negative case -- exit code alone is the assertion. No CSV
                # oracle is required (and the "expected" key may be omitted
                # from the manifest entry).
                passed += 1
                print(f"PASS {name} (expected_exit={expected_exit})")
                continue

            # Cross-implementation agreement, every pair, against the first that
            # ran. Each is also checked against the committed oracle below, which
            # is itself the byte-exact cross-impl contract -- but comparing them
            # to each other first gives a diff between two actual outputs, which
            # is the more useful failure message.
            reference = running[0]
            for impl in running[1:]:
                require_equal(
                    produced[reference.name],
                    produced[impl.name],
                    f"{name} {reference.label} output",
                    f"{name} {impl.label} output",
                )

            expected_path = SUITE / (case.get("expected_partial") or case["expected"])
            if args.update_expected:
                expected_path.parent.mkdir(parents=True, exist_ok=True)
                expected_path.write_bytes(produced[reference.name])
                print(f"UPDATED {expected_path.relative_to(ROOT)}")

            if not expected_path.exists():
                raise RuntimeError(f"{name}: expected output is missing: {expected_path}")
            expected = expected_path.read_bytes()
            for impl in running:
                require_equal(
                    expected,
                    produced[impl.name],
                    str(expected_path),
                    f"{name} {impl.label} output",
                )

            # Split-output cases (separate error mode) compare an additional
            # <output_stem>_errors.csv against the expected_errors oracle. Every
            # implementation derives the errors path the same way (see the
            # L2-ERR-008 stem/suffix definition), so the canonical naming is
            # reused here.
            expected_errors_rel = case.get("expected_errors")
            if expected_errors_rel:
                error_outputs: dict[str, bytes] = {}
                for impl in running:
                    errors_path = _errors_path(outputs[impl.name])
                    if not errors_path.exists():
                        raise RuntimeError(
                            f"{name}: {impl.label} did not create errors file {errors_path}"
                        )
                    error_outputs[impl.name] = errors_path.read_bytes()
                for impl in running[1:]:
                    require_equal(
                        error_outputs[reference.name],
                        error_outputs[impl.name],
                        f"{name} {reference.label} errors output",
                        f"{name} {impl.label} errors output",
                    )
                expected_errors_path = SUITE / expected_errors_rel
                if args.update_expected:
                    expected_errors_path.parent.mkdir(parents=True, exist_ok=True)
                    expected_errors_path.write_bytes(error_outputs[reference.name])
                    print(f"UPDATED {expected_errors_path.relative_to(ROOT)}")
                if not expected_errors_path.exists():
                    raise RuntimeError(
                        f"{name}: expected_errors oracle is missing: {expected_errors_path}"
                    )
                expected_errors = expected_errors_path.read_bytes()
                for impl in running:
                    require_equal(
                        expected_errors,
                        error_outputs[impl.name],
                        str(expected_errors_path),
                        f"{name} {impl.label} errors output",
                    )

            passed += 1
            print(f"PASS {name}")

    summary = f"{passed} conformance cases passed"
    if skipped:
        summary += f", {skipped} skipped (no implementation under test supports them)"
    print(summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
