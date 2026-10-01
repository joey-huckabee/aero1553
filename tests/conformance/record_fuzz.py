"""Differential *fuzzer* for the record-stream decoders.

WHAT THIS IS FOR. The three in-process L1-ROB-001 harnesses
(`rust/tests/integration.rs`, `python/tests/test_e2e.py`,
`cpp/tests/test_fuzz.cpp`) share a generator, so iteration N is the same bytes
everywhere -- but each one only ever proves a negative about its own process:
*it did not crash*. Their ``FUZZ-SUMMARY`` lines compare run totals, which
catches a disagreement about how many records a file yields but not two
implementations decoding the same record to different values.

This module closes that gap. It generates one input, drives it through **every**
implementation's CLI, and compares the exit-code class **and the CSV bytes**
all-pairs. It is the record-stream twin of ``config_fuzz.py``, which has done
the same thing for the config parsers since v2.11.0, and it uses the same
comparator for the same reasons -- see ``differential.py``: no majority rule
(two implementations sharing a bug would outvote the correct one) and no
reference implementation (that would make one tree's quirks normative).

STRUCTURE-AWARE, NOT UNIFORM NOISE. The in-process harnesses feed uniform random
bytes, which reach the *recovery* paths densely and the *valid record* paths
almost never -- across a 25 000-iteration burn-in they never once produced
enough consecutive equal-timestamp records to reach the canonical-order stage's
cap branch, which all three harnesses' comments claimed they reached "often".
This generator instead starts from the committed conformance fixtures -- every
valid record shape the project knows about -- concatenates a few, and then
*damages* them. That reaches decode, ordering, DELTA and error-classification
paths that noise cannot, while still producing inputs no human wrote.

REPRODUCIBILITY. Deterministic by default (fixed seed, fixed iteration count),
overridable with ``MIE_RECORD_FUZZ_SEED`` / ``MIE_RECORD_FUZZ_ITERS``. On a
divergence the generated input is printed as a **hex fixture**, ready to paste
into ``tests/conformance/inputs/`` as a permanent case -- because a seed stops
reproducing anything the moment the generator changes.
"""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

from differential import classify, describe_divergence

_DEFAULT_SEED = 20260826
# Each iteration spawns one subprocess per implementation, so this is the
# expensive kind of fuzzing. The default keeps the differential job to a few
# seconds; the burn-in raises it.
_DEFAULT_ITERS = 60

# Inputs that exist to be pathological at the FILE level rather than the record
# level. Splicing them into a stream tells us nothing a targeted conformance
# case does not already pin, and the very large one makes every iteration slow.
_TEMPLATE_SKIP = frozenset({"partial-unrecoverable.hex"})


def _load_templates(suite: Path) -> list[bytes]:
    """Every committed hex fixture, as bytes, sorted by name for determinism."""
    from run import read_hex  # local import: run.py imports this module

    templates: list[bytes] = []
    for path in sorted((suite / "inputs").glob("*.hex")):
        if path.name in _TEMPLATE_SKIP:
            continue
        data = read_hex(path)
        if data:
            templates.append(data)
    return templates


def _mutate(rng: random.Random, data: bytearray) -> str:
    """Apply one damage operation in place; return a short label for the report.

    Each operation is chosen to break a *different* layer: a bit flip inside a
    Type Word changes a record's shape, truncation cuts a record in half, a
    spliced run of noise forces a sync recovery, and a duplicated slice produces
    the repeated timestamps the canonical-order stage exists to handle.
    """
    if not data:
        data.extend(rng.randbytes(8))
        return "seed-empty"

    kind = rng.choice(
        ["bitflip", "setbyte", "truncate", "append", "zero-word", "duplicate", "splice"]
    )
    if kind == "bitflip":
        at = rng.randrange(len(data))
        data[at] ^= 1 << rng.randrange(8)
    elif kind == "setbyte":
        data[rng.randrange(len(data))] = rng.randrange(256)
    elif kind == "truncate":
        del data[rng.randrange(len(data)) :]
    elif kind == "append":
        data.extend(rng.randbytes(rng.randrange(1, 33)))
    elif kind == "zero-word":
        at = rng.randrange(len(data)) & ~1
        data[at : at + 2] = b"\x00\x00"
    elif kind == "duplicate":
        start = rng.randrange(len(data))
        end = min(len(data), start + rng.randrange(2, 80))
        data[start:start] = data[start:end]
    else:  # splice
        at = rng.randrange(len(data) + 1)
        data[at:at] = rng.randbytes(rng.randrange(1, 17))
    return kind


def _make_input(rng: random.Random, templates: list[bytes]) -> tuple[bytes, str]:
    """One generated recording, plus a description of how it was built."""
    picks = rng.sample(templates, k=min(len(templates), rng.randrange(1, 4)))
    data = bytearray()
    for chunk in picks:
        data.extend(chunk)

    labels = [_mutate(rng, data) for _ in range(rng.randrange(0, 7))]
    return bytes(data), f"{len(picks)} fixture(s), mutations: {labels or ['none']}"


def _as_hex_fixture(data: bytes, recipe: str) -> str:
    """The input rendered as a committable ``inputs/*.hex`` file.

    A seed does not survive a change to the generator; a hex dump does. This is
    the same reasoning ``config_fuzz.py`` applies to the documents it prints.
    """
    lines = [f"# Generated by record_fuzz.py -- {recipe}", f"# {len(data)} bytes"]
    lines.extend(data[offset : offset + 16].hex() for offset in range(0, len(data), 16))
    return "\n".join(lines)


def _first_difference(a: bytes, b: bytes) -> str:
    """A one-line description of where two CSV outputs first differ."""
    a_lines = a.split(b"\n")
    b_lines = b.split(b"\n")
    for index in range(max(len(a_lines), len(b_lines))):
        left = a_lines[index] if index < len(a_lines) else b"<no line>"
        right = b_lines[index] if index < len(b_lines) else b"<no line>"
        if left != right:
            return (
                f"first differing CSV line is {index + 1}:\n"
                f"          {left.decode('utf-8', 'replace')}\n"
                f"          {right.decode('utf-8', 'replace')}"
            )
    return "CSV outputs are identical"


def check_record_stream_fuzz(
    invocations: dict[str, list[str]], root: Path, suite: Path, temp: Path
) -> None:
    """Fuzz every implementation's record decoder; raise on the first batch of
    divergences.

    ``invocations`` maps an implementation name to its CLI prefix, so a fourth
    decoder joins the sweep without changing the comparison -- which is
    all-pairs: any two disagreeing is a finding, regardless of which is right.

    Raises:
        AssertionError: if any two implementations disagree on any input.
    """
    seed = int(os.environ.get("MIE_RECORD_FUZZ_SEED", _DEFAULT_SEED))
    iters = int(os.environ.get("MIE_RECORD_FUZZ_ITERS", _DEFAULT_ITERS))
    rng = random.Random(seed)
    templates = _load_templates(suite)
    if not templates:
        raise AssertionError(f"record-stream fuzz found no hex fixtures under {suite / 'inputs'}")

    divergences: list[str] = []
    for i in range(iters):
        data, recipe = _make_input(rng, templates)
        # One input file shared by every implementation. That matters beyond
        # tidiness: MUX is populated from the input FILE NAME by default
        # (L2-WRT-020), so per-implementation copies would put a different value
        # in every CSV and the comparison would fail on the harness rather than
        # on the decoders.
        source = temp / f"recfuzz-{i}.mie"
        source.write_bytes(data)

        verdicts: dict[str, str] = {}
        codes: dict[str, int] = {}
        outputs: dict[str, bytes] = {}
        for impl, prefix in invocations.items():
            out = temp / f"recfuzz-{i}-{impl}.csv"
            result = subprocess.run(
                [*prefix, "decode", str(source), "-o", str(out)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            payload = out.read_bytes() if out.exists() else b""
            codes[impl] = result.returncode
            outputs[impl] = payload
            # Exit CLASS, not the exact code: the implementations may
            # legitimately reach the same refusal by different routes. The CSV
            # bytes are compared exactly, which is where a real decode
            # divergence shows up.
            verdicts[impl] = f"{classify(result.returncode)} csv={len(payload)}B"

        divergence = describe_divergence(verdicts, codes)
        if divergence is None:
            # Same verdict and same output length is not the same output.
            distinct = dict(outputs)
            first = next(iter(distinct.values()))
            if all(payload == first for payload in distinct.values()):
                continue
            names = sorted(distinct)
            divergence = (
                f"identical exit class but differing CSV bytes; "
                f"{_first_difference(distinct[names[0]], distinct[names[1]])}"
            )

        divergences.append(
            f"{divergence}\n"
            f"      iteration {i} ({recipe})\n"
            f"      paste this into tests/conformance/inputs/ to pin it:\n"
            + "\n".join("        " + line for line in _as_hex_fixture(data, recipe).splitlines())
        )
        if len(divergences) >= 5:
            break

    if divergences:
        raise AssertionError(
            f"record-stream fuzz found {len(divergences)} divergence(s) "
            f"(seed={seed}, {iters} iterations):\n\n" + "\n\n".join(divergences)
        )
    print(f"PASS record-stream-fuzz ({iters} generated recordings, seed {seed})")
