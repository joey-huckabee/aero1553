"""The config number grammar is TOML 1.0's, judged by ``tomllib`` (L2-CFG-014).

The CLIs share one parser (Rust's, which this package runs through its
extension), so comparing them cannot catch a grammar mistake: both would make
it. Python's standard ``tomllib`` is an independent parser of the same
grammar, so it referees instead. Each literal is put in a config file as
``[mux] field`` -- the one key that takes any 64-bit integer -- and the
loader's verdict must match ``tomllib``'s:

- an integer ``tomllib`` reads and that fits in 64 bits loads as that value;
- one that does not fit is refused as an ``invalid integer``;
- a float is a number the key then refuses (``must be an integer``), and a
  positive one loads as ``standard_tick_rate_hz`` with ``tomllib``'s value;
- anything ``tomllib`` refuses, or reads as something other than a number
  (``1979-05-27`` is a date), is refused by the grammar.

``inf`` and ``nan`` are the stated exception: TOML numbers, refused by
design because no key accepts a non-finite value.

``tomllib`` arrived in Python 3.11; on 3.10 the module is skipped, and CI's
3.11 to 3.14 jobs run it on Linux and Windows.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from aero1553 import _native

tomllib = pytest.importorskip("tomllib")

#: Refused by design though TOML reads them (see the module docstring).
NON_FINITE = {"inf", "+inf", "-inf", "nan", "+nan", "-nan"}

I64_MIN, I64_MAX = -(2**63), 2**63 - 1

HAND_PICKED = [
    # The forms the change adds, and their neighbours that stay refused.
    "1_000_000", "1_0", "1__0", "_1", "1_", "0_1", "1_000_000.0", "1.000_1",
    "1_.0", "1._0", "1e1_0", "1e_1", "1_e1", "1e06",
    "0x08", "0xFF", "0xff", "0xE", "0x1e3", "0xdead_beef", "0x0008", "0x",
    "0x_8", "0x8_", "0X8", "-0x1", "+0x1", "0xG", "0x1.0",
    "0o10", "0o7_7", "0o8", "0O7", "0b1000", "0b1_0", "0b2", "0B1", "0b1e1",
    # Plain decimals, before and after.
    "0", "-0", "+0", "8", "-1", "+8", "08", "00", "1.", ".5", "1.e3", "1..2",
    "1.5", "0.5", "1e6", "1E6", "1.5e-3", "0e0", "1e", "1e+", "-", "+",
    # The edges of 64 bits, in every radix.
    "9223372036854775807", "9223372036854775808", "-9223372036854775808",
    "-9223372036854775809", "0x7FFF_FFFF_FFFF_FFFF", "0x8000_0000_0000_0000",
    "0o777777777777777777777", "0o1000000000000000000000",
    "0b" + "1" * 63, "0b1" + "0" * 63,
    # Not numbers at all.
    "1979-05-27", "1979-05-27T07:32:00", "true",
]  # fmt: skip

ALPHABET = "0123456789abcdefABCDEFxob_+-.eE"


def _generated(seed: int, count: int) -> list[str]:
    """Literals near the grammar: random strings over its alphabet, and valid
    forms with one character changed, so both sides of each rule are hit."""
    rng = random.Random(seed)
    out: list[str] = []
    for _ in range(count):
        roll = rng.random()
        if roll < 0.3:
            out.append("".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 10))))
            continue
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 20)))
        if roll < 0.5:
            radix = rng.choice(["0x", "0o", "0b"])
            pool = {"0x": "0123456789abcdefABCDEF", "0o": "01234567", "0b": "01"}[radix]
            body = "".join(rng.choice(pool) for _ in range(rng.randint(1, 22)))
            text = radix + body
        elif roll < 0.75:
            text = rng.choice(["", "-", "+"]) + digits
        else:
            text = rng.choice(["", "-", "+"]) + digits + "." + digits[::-1]
            if rng.random() < 0.5:
                text += rng.choice("eE") + rng.choice(["", "-", "+"]) + str(rng.randint(0, 400))
        # Separators, valid and not.
        for _ in range(rng.randint(0, 3)):
            at = rng.randint(0, len(text))
            text = text[:at] + "_" + text[at:]
        if rng.random() < 0.15 and text:
            at = rng.randrange(len(text))
            text = text[:at] + rng.choice(ALPHABET) + text[at + 1 :]
        out.append(text)
    return out


def _rust_verdict(tmp: Path, literal: str) -> tuple[str, object]:
    """``("int", value)``, ``("float", value)``, ``("too-big", None)``,
    ``("refused", message)`` -- what the shared loader made of the literal."""
    path = tmp / "n.toml"
    path.write_text(f"[mux]\nfield = {literal}\n", encoding="utf-8")
    try:
        return "int", _native.load_config(path)["mux_field"]
    except ValueError as exc:
        message = str(exc)
    if "invalid integer" in message:
        return "too-big", None
    if "must be an integer" not in message:
        return "refused", message
    path.write_text(f"[decode]\nstandard_tick_rate_hz = {literal}\n", encoding="utf-8")
    try:
        return "float", _native.load_config(path)["standard_tick_rate_hz"]
    except ValueError as exc:
        if "must be a number" in str(exc):
            return "refused", str(exc)  # a boolean: a value, but not a number
        return "float", None  # not positive, or not finite: the range, not the grammar


def _toml_verdict(literal: str) -> tuple[str, object]:
    try:
        value = tomllib.loads(f"x = {literal}\n")["x"]
    except tomllib.TOMLDecodeError:
        return "refused", None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "refused", None  # a date, a boolean: not a number
    if isinstance(value, int):
        return ("int", value) if I64_MIN <= value <= I64_MAX else ("too-big", None)
    return "float", value


Verdict = Callable[[Path, str], tuple[str, object]]


def _disagreements(tmp: Path, literals: list[str], rust: Verdict) -> list[str]:
    problems = []
    for literal in literals:
        if literal.lower().lstrip("+-") in {"inf", "nan"}:
            continue
        want, value = _toml_verdict(literal)
        got, got_value = rust(tmp, literal)
        if got != want:
            problems.append(f"{literal!r}: tomllib {want} {value!r}, loader {got} {got_value!r}")
        elif want == "int" and got_value != value:
            problems.append(f"{literal!r}: tomllib {value}, loader {got_value}")
        elif want == "float" and got_value is not None and got_value != value:
            problems.append(f"{literal!r}: tomllib {value!r}, loader {got_value!r}")
    return problems


@pytest.mark.requirement("L2-CFG-014")
def test_number_grammar_matches_tomllib(tmp_path: Path) -> None:
    literals = HAND_PICKED + _generated(seed=20261101, count=4000)
    problems = _disagreements(tmp_path, literals, _rust_verdict)
    assert not problems, f"{len(problems)} disagreement(s):\n" + "\n".join(problems[:40])
    # The generator must reach every outcome, or the comparison above is
    # quietly about fewer rules than it claims (docs/FUZZING.md).
    reached = Counter(_toml_verdict(lit)[0] for lit in literals)
    prefixed = sum(1 for lit in literals if _toml_verdict(lit)[0] == "int" and lit[1:2] in "xob")
    separated = sum(1 for lit in literals if "_" in lit and _toml_verdict(lit)[0] != "refused")
    # About half the least each reached over 30 seeds (827, 495, 84, 2491;
    # 372 prefixed, 788 separated), so a seed change cannot trip them.
    floors = {"int": 400, "float": 250, "too-big": 40, "refused": 1200}
    short = {k: reached[k] for k, n in floors.items() if reached[k] < n}
    assert not short, f"generator reached too few: {short} (floors {floors})"
    assert prefixed >= 180, prefixed
    assert separated >= 400, separated


@pytest.mark.requirement("L2-CFG-014")
def test_non_finite_numbers_are_refused(tmp_path: Path) -> None:
    for literal in sorted(NON_FINITE):
        assert tomllib.loads(f"x = {literal}\n")["x"] is not None
        assert _rust_verdict(tmp_path, literal)[0] == "refused", literal


@pytest.mark.requirement("L2-CFG-014")
@pytest.mark.parametrize(
    ("mistake", "literal"),
    [
        ("accepts a doubled separator", "1__0"),
        ("accepts a signed prefix", "-0x1"),
        ("reads 0xE as an exponent", "0xE"),
        ("drops the radix", "0o10"),
    ],
)
def test_the_referee_catches_a_broken_parser(tmp_path: Path, mistake: str, literal: str) -> None:
    """A comparison that passes against a parser with a known mistake is not
    comparing anything; each mistake here must be reported."""

    def broken(tmp: Path, text: str) -> tuple[str, object]:
        if text != literal:
            return _rust_verdict(tmp, text)
        if mistake == "accepts a doubled separator":
            return "int", 10
        if mistake == "accepts a signed prefix":
            return "int", -1
        if mistake == "reads 0xE as an exponent":
            return "refused", "invalid number literal"
        return "int", 10

    problems = _disagreements(tmp_path, HAND_PICKED, broken)
    assert any(repr(literal) in p for p in problems), mistake
