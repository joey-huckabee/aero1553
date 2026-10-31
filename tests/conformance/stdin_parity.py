"""Standard input decodes exactly as the same bytes in a file (L2-CLI-023).

Every manifest case that reads one recording is run twice per implementation:
once with the recording as a file, as the case is written, and once with the
same bytes piped to standard input as ``-``. The two runs must agree on

  * the exit code,
  * every file the run commits -- the CSV, the errors file, a ``.partial`` --
    and stdout, byte for byte,
  * stderr at WARN and above, once the input's name is replaced: a file run
    names its path, a stdin run names ``<stdin>``. INFO and DEBUG lines are
    left out, as in the stderr oracles (L2-CLI-022): the "opened" line says the
    file's size, which a stream does not know.

So the whole corpus -- headers, resync, truncated tails, strict mode, empty
recordings, filters, configs, every output mode -- doubles as the stdin test,
and a case added later is covered without being written twice.

Cases left out, and why (each is counted in the summary line):

  * several inputs (a merge) -- refused for stdin by design;
  * ``input_names`` -- a file-name-derived ``MUX``, which a stream has no name for;
  * ``output_name`` -- the destination beside the input, a path-collision case;
  * ``dump`` -- refused for stdin by design;
  * ``--`` in the subcommand arguments -- after it, ``-`` is a file name.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Free-text log levels, left out of the comparison (see the module docstring).
_FREE_LOG_LEVELS = ("INFO [", "DEBUG [")

STDIN_NAME = "<stdin>"


def _why_not(case: dict[str, Any]) -> str | None:
    if case.get("inputs"):
        return "several inputs"
    if case.get("input_names"):
        return "file-name MUX"
    if case.get("output_name"):
        return "output beside the input"
    if case.get("mode") == "dump":
        return "dump"
    if "--" in case.get("subcommand_args", []):
        return "end of options"
    return None


def _comparable(stderr: bytes, replace: dict[str, str]) -> str:
    text = stderr.decode("utf-8", "surrogateescape")
    for old, new in sorted(replace.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    return "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith(_FREE_LOG_LEVELS)
    )


def _committed(output: Path | None) -> dict[str, bytes]:
    """Every file a run left beside its destination, by name."""
    if output is None or not output.parent.exists():
        return {}
    return {p.name: p.read_bytes() for p in sorted(output.parent.iterdir()) if p.is_file()}


def check_stdin_parity(
    invocations: dict[str, list[str]],
    root: Path,
    cases: list[dict[str, Any]],
    recording: Callable[[str], bytes],
    build: Callable[[list[str], dict[str, Any], list[Path], Path | None], list[str]],
    stdout_modes: tuple[str, ...],
    temp: Path,
) -> None:
    """Run each eligible case from a file and from stdin; require agreement.

    ``recording`` turns a case's ``input`` into its bytes, and ``build`` is the
    runner's own command builder, so the commands are the ones the suite runs.

    Raises:
        AssertionError: naming the case, the implementation and what differed.
    """
    skipped: dict[str, int] = {}
    compared = 0
    for case in cases:
        reason = _why_not(case)
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        name = case["name"]
        data = recording(case["input"])
        mode = case.get("mode", "decode")
        for label, prefix in invocations.items():
            runs: dict[str, tuple[int, dict[str, bytes], bytes, str]] = {}
            for how in ("file", "stdin"):
                work = temp / "stdin-parity" / label / name / how
                work.mkdir(parents=True, exist_ok=True)
                source = work / "in" / "rec.mie"
                source.parent.mkdir(exist_ok=True)
                source.write_bytes(data)
                output = None if mode in stdout_modes else work / "out" / "out.csv"
                if output is not None:
                    output.parent.mkdir(exist_ok=True)
                    if (pre := case.get("pre_existing_output")) is not None:
                        output.write_text(pre, encoding="utf-8")
                command = build(list(prefix), case, [source], output)
                if how == "stdin":
                    command = ["-" if arg == str(source) else arg for arg in command]
                result = subprocess.run(
                    command,
                    cwd=root,
                    input=data if how == "stdin" else None,
                    stdin=None if how == "stdin" else subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                replace = {str(root): "<ROOT>", str(work): "<WORK>"}
                replace[str(source) if how == "file" else STDIN_NAME] = "<INPUT>"
                runs[how] = (
                    result.returncode,
                    _committed(output),
                    result.stdout,
                    _comparable(result.stderr, replace),
                )
            file_run, stdin_run = runs["file"], runs["stdin"]
            for index, what in enumerate(("exit code", "committed files", "stdout", "stderr")):
                if file_run[index] != stdin_run[index]:
                    raise AssertionError(
                        f"stdin-parity {name} [{label}]: {what} differs between a file "
                        f"and stdin\n--- file ---\n{file_run[index]!r}\n"
                        f"--- stdin ---\n{stdin_run[index]!r}"
                    )
        compared += 1
    left_out = ", ".join(f"{n} {why}" for why, n in sorted(skipped.items()))
    print(
        f"PASS stdin-parity: {compared} cases identical from a file and from stdin "
        f"(not applicable: {left_out})"
    )
