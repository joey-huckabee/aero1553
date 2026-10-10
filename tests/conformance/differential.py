"""Shared machinery for the differential config checks.

Three checks — a curated corpus (`config_parity`), a generated fuzzer
(`config_fuzz`), and the config *path* (`config_path_parity`) — all do the same
thing: run one input through every implementation and compare the verdicts. They
each had their own copy of "compare Rust to Python", written when there were
exactly two implementations and the comparison was a single `!=`.

A third implementation makes the comparison a real operation rather than an
inline test, and this is where it lives.

ALL-PAIRS, NOT MAJORITY. Every implementation must agree with every other; any
disagreement fails. The alternatives were considered and rejected:

  * *Majority* — two implementations sharing a bug outvote the correct one, and
    the gate reports success. It also destroys the finding: you learn "the
    minority is wrong" rather than "these two disagree", which is the opposite
    of what a differential check is for.

  * *Reference implementation* — nominating one as the oracle makes its quirks
    normative. A bug in the reference becomes a requirement the others must
    reproduce.

Because agreement here is plain equality, all-pairs is *equivalent* to "every
implementation returned the same verdict". The reason to think of it as pairs is
the failure message: "3 implementations disagreed" is not actionable, and
"accept: Rust, Python | reject: C++" is — it says which side to look at first
without claiming which side is right.

WHAT IS COMPARED depends on the check, so these helpers are generic over the
compared value:

  * `config_parity` and `config_fuzz` compare the accept/reject VERDICT, and
    the DIAGNOSTICS: every line written to stderr, with run-specific paths
    replaced by placeholders (L2-CLI-022). A rejection may be exit 5 (config
    error) or exit 4 (usage) depending on where the value was caught, so the
    verdict rather than the code is what must agree. Exit codes are still
    carried into the failure text, because they are usually the fastest clue
    to why a divergence happened.

  * `config_path_parity` and `cli_message_parity` compare the EXACT exit
    code, because their cases pin specific codes rather than mere
    admissibility, and the diagnostics; `cli_message_parity` compares stdout
    too, which is where help and version go.

The diagnostics were not compared until L2-CLI-022: the rule had been that
wording may drift. That let the config errors of two implementations differ in
60 of 79 corpus snippets, hid a C++ message that printed an infinite value as
an empty string, and left the usage layer agreeing on the exit code of every
case and on the wording of almost none.
"""

from __future__ import annotations

from collections.abc import Mapping


def classify(returncode: int) -> str:
    """The verdict a run represents: it either took the config or it did not."""
    return "accept" if returncode == 0 else "reject"


def group_by_value(values: Mapping[str, str]) -> dict[str, list[str]]:
    """Implementation names grouped by the value they produced.

    Insertion-ordered so the report reads in a stable order across runs rather
    than in whatever order a set iterated.
    """
    groups: dict[str, list[str]] = {}
    for impl, value in values.items():
        groups.setdefault(value, []).append(impl)
    return groups


def describe_divergence(
    values: Mapping[str, str], codes: Mapping[str, int] | None = None
) -> str | None:
    """``None`` when every implementation agreed; otherwise the split.

    The description names each verdict and who returned it — deliberately
    without nominating a winner. Which side is *correct* is a judgement the
    maintainer makes from the snippet; what this can state as fact is that the
    implementations disagree, and that alone is the bug.
    """
    if len(set(values.values())) <= 1:
        return None

    parts: list[str] = []
    for value, impls in group_by_value(values).items():
        if codes is not None:
            named = ", ".join(f"{impl} (exit {codes[impl]})" for impl in impls)
        else:
            named = ", ".join(impls)
        parts.append(f"{value}: {named}")
    return " | ".join(parts)


def normalized_stderr(stderr: bytes, paths: Mapping[str, str]) -> str:
    """``stderr`` with each run-specific path replaced by its placeholder.

    ``paths`` maps a path as it was passed on the command line to the
    placeholder that stands for it (``{"/tmp/x/parity-3.toml": "<CONFIG>"}``).
    Each implementation prints the path it was given, so the paths are the only
    part of a diagnostic that legitimately differs between two runs -- and the
    output path differs per implementation by construction.

    Bytes that are not UTF-8 survive as lone surrogates, so they still compare
    and still fail `describe_stderr_problems`'s ASCII check.

    Line endings ARE compared. On Windows the C++ binary once wrote stderr in
    text mode, ending every line CRLF where Rust and Python write LF; it now
    opens stderr in binary mode, as it already did stdout.
    """
    text = stderr.decode("utf-8", "surrogateescape")
    for path, placeholder in sorted(paths.items(), key=lambda item: -len(item[0])):
        text = text.replace(path, placeholder)
    return text


def _shown(text: str) -> str:
    """`text` printable on any console: non-ASCII as Python escapes, and a
    carriage return made visible, so two texts that differ only in their line
    endings do not print identically."""
    return text.encode("ascii", "backslashreplace").decode("ascii").replace("\r", "\\r")


def describe_stderr_problems(stderrs: Mapping[str, str], *, compare: bool = True) -> str | None:
    """``None`` when every implementation wrote the same diagnostics, all of
    them ASCII (L2-CLI-022, L2-CLI-014); otherwise what is wrong, and whose.

    The ASCII half is checked here rather than left to the comparison because
    implementations can agree on a non-ASCII byte: both once repeated a config
    key such as ``stricte-acute`` to the console exactly as written.

    ``compare=False`` checks only the ASCII half, for an outcome whose text
    another check owns.
    """
    problems = [
        f"{impl} wrote non-ASCII: {_shown(line)}"
        for impl, text in stderrs.items()
        for line in text.split("\n")
        if not line.isascii()
    ]
    groups = group_by_value(stderrs)
    if compare and len(groups) > 1:
        parts = []
        for text, impls in groups.items():
            lines = text.split("\n")
            body = "\n".join(f"        | {_shown(line)}" for line in lines) or "        | (empty)"
            parts.append(f"      {', '.join(impls)}:\n{body}")
        problems.append("stderr differs:\n" + "\n".join(parts))
    return "; ".join(problems) if problems else None


def describe_agreement(classes: Mapping[str, str], codes: Mapping[str, int]) -> str:
    """How a unanimous verdict was reached, for a message about the *expected*
    result being wrong rather than about a divergence."""
    verdict = next(iter(classes.values()))
    detail = ", ".join(f"{impl} exit {codes[impl]}" for impl in sorted(classes))
    return f"all {verdict} ({detail})"
