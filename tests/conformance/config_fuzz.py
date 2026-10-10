"""Differential *fuzzer* for the Rust and Python config parsers.

The static ``config_parity.py`` corpus only tests forms a human enumerated —
which is exactly why config divergences kept being found one at a time. This
module instead *generates* many small TOML-ish config documents and drives each
through both CLIs, asserting they agree on accept vs reject and write the same diagnostics. It
searches the
edges (odd numeric literals, string escapes, exotic structures) so CI finds a
divergence before a reviewer does.

Deterministic by default (fixed seed + iteration count) so CI is reproducible;
override with the ``MIE_CONFIG_FUZZ_SEED`` / ``MIE_CONFIG_FUZZ_ITERS`` environment variables to
explore further locally. On a divergence it prints the exact config that
disagreed, ready to paste into ``config_parity.py`` as a pinned regression.
"""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

from differential import (
    classify,
    describe_divergence,
    describe_stderr_problems,
    normalized_stderr,
)

_DEFAULT_SEED = 20260711  # fixed → reproducible CI runs
# Each iteration spawns both CLIs, so keep the default modest for CI wall-clock;
# a fixed seed makes it a deterministic generated corpus. Bump MIE_CONFIG_FUZZ_ITERS
# locally to explore further.
_DEFAULT_ITERS = 100

# Section names: mostly real, sometimes junk / dotted / array-table forms.
_SECTIONS = [
    "decode",
    "output",
    "mux",
    "merge",
    "filter",
    "logging",
    "bogus",
    "output.no_clobber",  # dotted header
    "decode.foo",
    "bad-section",  # non-identifier: hyphen
    '"bad"',  # non-identifier: quoted
    "bad section",  # non-identifier: space
    "sectión",  # non-identifier: non-ASCII letter (see _KEYS note)
]
# Keys: real identifiers plus a few that stress the key grammar.
_KEYS = [
    "strict",
    "input_time_format",
    # The pre-v3.0.0 spelling, kept in the palette deliberately: it is a
    # retired key the loaders must REJECT by name (L2-CFG-012), not an unknown
    # key they warn about, and the fuzzer should be generating documents that
    # exercise that distinction.
    "time_format",
    "output_time_format",
    "year",
    "utc_offset",
    "error_mode",
    "detect_records",
    "lookahead_records",
    "standard_tick_rate_hz",
    "no_clobber",
    "max_sort_group",
    "max_collapse_survivors",
    "collapse_window_us",
    "delta_scope",
    "enabled",
    "delimiter",
    "field",
    "exclude_rts",
    "level",
    "irig_day_advisory",
    "unknown_key",
    "decode.strict",  # dotted key
    '"strict"',  # quoted key
    # Non-ASCII identifiers. Rust gates keys on `is_ascii_alphanumeric`, so
    # these must be rejected on BOTH sides. Python's `\w` is Unicode-aware by
    # default and would accept them, so `_IDENT_RE` is compiled with `re.ASCII`
    # — these entries are what keeps that flag from being silently dropped.
    "stricté",  # Latin-1 letter
    "中文",  # CJK
]
# Values: valid forms mixed heavily with edge cases that have historically
# diverged (leading zeros, bare trailing dot, hex/oct/bin, underscores, string
# escapes, inline tables, datetimes, multi-line-array openers).
_VALUES = [
    '"per-file"',  # a valid delta_scope name; nonsense for every other key
    # Non-ASCII digits: Rust's `is_ascii_digit` rejects them, and Python's
    # `_NUMBER_RE` only agrees because it is compiled with `re.ASCII`. Bare
    # `\d` would match these and accept a literal Rust refuses.
    "٤٢",  # Arabic-Indic 42
    "true",
    "false",
    "8",
    "-1",
    "0",
    "1000000.0",
    "1e6",
    '"auto"',
    '"irig"',
    # v3.0.0 rendering values: valid names for `output_time_format`, and
    # nonsense for every other key, which is the point of a shared palette.
    '"doy"',
    '"iso"',
    '"dom"',
    # UTC-offset shapes, including the near-misses the grammar must refuse.
    '"Z"',
    '"-05:00"',
    '"+24:00"',
    '"+5:00"',
    "2026",
    '"."',
    "[0, 31]",
    '["A", "B"]',
    "[]",
    # edge numerics
    "08",
    "01",
    "1.",
    "0x08",
    "0o10",
    "0b1000",
    "1_000",
    "+.5",
    "00",
    # 64-bit boundaries. The C++ loader parsed integers with `strtoll`, which
    # saturates on overflow, and accepted these on the keys with no upper bound
    # (`collapse_window_us`, `field`) as 9223372036854775807; Rust and Python
    # refuse them. The palette had no literal past 2^63, so it never noticed.
    "99999999999999999999",
    "9223372036854775808",
    "9223372036854775807",
    "-9223372036854775809",
    # edge strings
    '"\\""',
    '"\\r"',
    '"\\u002C"',
    '"\\t"',
    '"unterminated',
    # Expansion / traversal syntax as plain data. A config value is TOML data:
    # neither parser may expand, execute or resolve any of these, so whatever key
    # a form lands on, both must reach the same accept/reject class. Pins the
    # "TOML data, never interpolated" half of the CONFIG-REFERENCE.md trust
    # boundary across two parsers that share no code.
    '"$(whoami)"',
    '"${HOME}"',
    '"`id`"',
    '"%PATH%"',
    '"../../../etc/passwd"',
    '"\\\\?\\C:\\Windows\\system32"',
    # exotic structures
    "{ a = 1 }",
    "1979-05-27",
    "[01]",
    "[\n1,\n]",
    # arrays whose string elements contain an escaped quote and/or a comma,
    # stressing the quote-aware, backslash-tracking array splitter.
    '["a\\", b"]',
    '["x,y", "z"]',
    '["p\\"q"]',
    # L7: forms whose verdict or message differed between Rust and C++ once
    # the messages were compared.
    "tru",  # starts like a boolean
    "1e400",  # overflows to infinity
    "inf",
    "-1.5",
    "[[1, 2]]",  # a nested array that the splitter cuts in two
    '"\\\u00e9"',  # a bad escape whose byte is not ASCII
    '"\u0131nfo"',  # a dotless i, which Unicode upper-casing maps to I
    '["0x+1"]',  # a signed hex type code
    '["0x1FF", "0xZZ"]',
    '["\u017fpurious_data"]',  # a long s, which upper-cases to S
    "true\u00a0",  # a no-break space after the value
]


def _make_document(rng: random.Random) -> str:
    lines: list[str] = []
    for _ in range(rng.randint(0, 3)):
        if rng.random() < 0.08:
            lines.append(f"[[{rng.choice(_SECTIONS)}]]")  # array-of-tables
        else:
            lines.append(f"[{rng.choice(_SECTIONS)}]")
        for _ in range(rng.randint(0, 4)):
            key = rng.choice(_KEYS)
            val = rng.choice(_VALUES)
            # A tab and a no-break space (L2-CFG-010: only spaces and tabs are blanks).
            sep = rng.choice(["=", " = ", "  =  ", "\t=\t", "\u00a0= "])
            trailing = rng.choice(["", "  # comment", " "])
            lines.append(f"{key}{sep}{val}{trailing}")
        if rng.random() < 0.15:
            lines.append(rng.choice(["", "# a comment", "   "]))
    return "\n".join(lines) + "\n"


def check_config_parser_fuzz(
    invocations: dict[str, list[str]], root: Path, input_mie: Path, temp: Path
) -> None:
    """Fuzz every implementation's config parser; raise on the first batch of
    divergences.

    ``invocations`` maps an implementation name to its CLI prefix, so a third
    parser joins the sweep without changing the comparison -- which is
    all-pairs: any two disagreeing is a finding, regardless of which is right.

    Raises:
        AssertionError: if any two implementations disagree on any generated
            config (reporting up to the first ten divergences).
    """
    seed = int(os.environ.get("MIE_CONFIG_FUZZ_SEED", _DEFAULT_SEED))
    iters = int(os.environ.get("MIE_CONFIG_FUZZ_ITERS", _DEFAULT_ITERS))
    rng = random.Random(seed)
    divergences: list[str] = []
    for i in range(iters):
        doc = _make_document(rng)
        cfg = temp / f"fuzz-{i}.toml"
        cfg.write_text(doc, encoding="utf-8")
        classes: dict[str, str] = {}
        codes: dict[str, int] = {}
        stderrs: dict[str, str] = {}
        for impl, prefix in invocations.items():
            out = temp / f"fuzz-{i}-{impl}.csv"
            result = subprocess.run(
                [*prefix, "--config", str(cfg), "decode", str(input_mie), "-o", str(out)],
                cwd=root,
                capture_output=True,
                check=False,
                timeout=30,
            )
            codes[impl] = result.returncode
            classes[impl] = classify(result.returncode)
            stderrs[impl] = normalized_stderr(
                result.stderr,
                {str(cfg): "<CONFIG>", str(out): "<OUTPUT>", str(input_mie): "<INPUT>"},
            )
        divergence = describe_divergence(classes, codes)
        if divergence is None:
            # L2-CLI-022 / L2-CLI-014: the diagnostics too, ASCII and alike --
            # except that after a usage error only the ASCII rule is checked
            # here, the text being the usage layer's contract (differential.py).
            usage = set(codes.values()) == {4}
            divergence = describe_stderr_problems(stderrs, compare=not usage)
        if divergence is not None:
            # The generating document is reproduced verbatim and indented: a
            # fuzz finding is only actionable if it can be pasted straight into
            # a file, and the seed alone does not survive a change to the
            # generator.
            divergences.append(divergence + " for config:\n" + "    " + doc.replace("\n", "\n    "))
            if len(divergences) >= 10:
                break
    if divergences:
        raise AssertionError(
            f"config-parser fuzz found {len(divergences)} divergence(s) "
            f"(seed={seed}, {iters} iterations):\n\n" + "\n".join(divergences)
        )
    print(f"PASS config-parser-fuzz ({iters} generated configs, seed {seed})")
