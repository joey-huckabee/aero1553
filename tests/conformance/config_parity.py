"""Differential config-parser parity check across the Rust and Python CLIs.

The two implementations parse TOML with structurally different parsers — Python
uses the full-TOML ``tomllib``; Rust uses a minimal hand-rolled parser for the
flat ``[section]`` + ``key = value`` schema. Aligning them one divergent form at
a time (a blacklist) does not converge, so this module drives a fixed corpus of
config snippets through *both* CLIs and asserts they land in the same class —
either both **accept** (exit 0) or both **reject** (non-zero config/usage error)
— and that the class matches the schema's intent.

Run automatically by ``run.py`` when both implementations are under test. A
divergence here is the systematic signal the manual per-bug conformance cases
were catching reactively.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from differential import classify, describe_agreement, describe_divergence

# ``accept`` = a valid config for the flat schema (both CLIs decode, exit 0).
# ``reject`` = outside the flat schema (both CLIs must refuse with a config or
# usage error). Every ``reject`` snippet is a full-TOML form ``tomllib`` accepts
# but the schema does not — the class this corpus exists to keep aligned.
CORPUS: list[tuple[str, str, str]] = [
    # ── valid flat forms (accept) ──────────────────────────────────────────
    ("flat-strict", "[decode]\nstrict = true\n", "accept"),
    ("comment-only", "# just a comment\n", "accept"),
    ("empty-file", "", "accept"),
    ("string-value", '[decode]\ninput_time_format = "irig"\n', "accept"),
    ("int-value", "[decode]\ndetect_records = 8\n", "accept"),
    ("float-value", "[decode]\nstandard_tick_rate_hz = 1000000.0\n", "accept"),
    ("bool-value", "[output]\nno_clobber = true\n", "accept"),
    # L2-LOG-001: the day-of-year advisory switch. A bool key in [logging],
    # a section that until now held nothing but a string -- exactly the shape
    # that has drifted between the loaders before.
    ("irig-day-advisory-false", "[logging]\nirig_day_advisory = false\n", "accept"),
    ("irig-day-advisory-true", "[logging]\nirig_day_advisory = true\n", "accept"),
    ("irig-day-advisory-int", "[logging]\nirig_day_advisory = 0\n", "reject"),
    ("irig-day-advisory-string", '[logging]\nirig_day_advisory = "false"\n', "reject"),
    ("int-array", "[filter]\nexclude_rts = [0, 31]\n", "accept"),
    ("string-array", '[filter]\nexclude_buses = ["A", "B"]\n', "accept"),
    ("trailing-comment", "[decode]\nstrict = true  # yes\n", "accept"),
    ("extra-whitespace", "[decode]\n  strict   =   true  \n", "accept"),
    ("blank-lines", "[decode]\n\n\nstrict = true\n", "accept"),
    ("negative-mux-field", "[mux]\nfield = -1\n", "accept"),
    # L2-WRT-022: the canonical-order run cap. In range on both, out of range on
    # both — a range check is exactly the kind of validation that has drifted
    # between the two loaders before.
    # L2-MRG-005: the DELTA scope selector. Both names accepted on both sides,
    # anything else rejected on both.
    ("delta-scope-per-file", '[merge]\ndelta_scope = "per-file"\n', "accept"),
    ("delta-scope-global", '[merge]\ndelta_scope = "global"\n', "accept"),
    ("delta-scope-mixed-case", '[merge]\ndelta_scope = "Per-File"\n', "accept"),
    ("delta-scope-unknown", '[merge]\ndelta_scope = "whole"\n', "reject"),
    ("delta-scope-non-string", "[merge]\ndelta_scope = 1\n", "reject"),
    ("max-sort-group-valid", "[output]\nmax_sort_group = 64\n", "accept"),
    ("max-sort-group-min", "[output]\nmax_sort_group = 1\n", "accept"),
    ("max-sort-group-max", "[output]\nmax_sort_group = 1048576\n", "accept"),
    ("max-sort-group-zero", "[output]\nmax_sort_group = 0\n", "reject"),
    ("max-sort-group-over", "[output]\nmax_sort_group = 1048577\n", "reject"),
    ("max-sort-group-negative", "[output]\nmax_sort_group = -1\n", "reject"),
    ("max-sort-group-bool", "[output]\nmax_sort_group = true\n", "reject"),
    ("max-sort-group-string", '[output]\nmax_sort_group = "64"\n', "reject"),
    # L2-MRG-008. The same probes its sibling cap gets: a range validated
    # in two implementations and clamped in the third is a divergence that
    # only shows up on the value nobody tries by hand.
    ("max-collapse-survivors-valid", "[merge]\nmax_collapse_survivors = 64\n", "accept"),
    ("max-collapse-survivors-min", "[merge]\nmax_collapse_survivors = 1\n", "accept"),
    ("max-collapse-survivors-max", "[merge]\nmax_collapse_survivors = 1048576\n", "accept"),
    ("max-collapse-survivors-zero", "[merge]\nmax_collapse_survivors = 0\n", "reject"),
    ("max-collapse-survivors-over", "[merge]\nmax_collapse_survivors = 1048577\n", "reject"),
    ("max-collapse-survivors-negative", "[merge]\nmax_collapse_survivors = -1\n", "reject"),
    ("max-collapse-survivors-bool", "[merge]\nmax_collapse_survivors = true\n", "reject"),
    ("max-collapse-survivors-string", '[merge]\nmax_collapse_survivors = "64"\n', "reject"),
    # ── outside the flat schema (reject on both) ───────────────────────────
    ("inline-table", "[decode]\nx = { a = 1 }\n", "reject"),
    ("multiline-array", "[filter]\nexclude_rts = [\n  1,\n]\n", "reject"),
    ("int-underscores", "[decode]\ndetect_records = 1_0\n", "reject"),
    ("hex-int", "[decode]\ndetect_records = 0x08\n", "reject"),
    ("octal-int", "[decode]\ndetect_records = 0o10\n", "reject"),
    ("binary-int", "[decode]\ndetect_records = 0b1000\n", "reject"),
    ("datetime-value", "[decode]\nx = 1979-05-27\n", "reject"),
    # `1e6` is a plain float both parsers accept — kept as an `accept` guard that
    # the whitelist must not over-reject scientific notation.
    ("exponent-float", "[decode]\nstandard_tick_rate_hz = 1e6\n", "accept"),
    ("dotted-key", "decode.strict = true\n", "reject"),
    ("dotted-key-in-section", "[decode]\nfoo.bar = 1\n", "reject"),
    ("dotted-header", "[output.no_clobber]\nenabled = true\n", "reject"),
    ("array-of-tables", "[[decode]]\nstrict = true\n", "reject"),
    ("duplicate-key", "[decode]\nstrict = true\nstrict = false\n", "reject"),
    (
        "duplicate-section",
        "[decode]\nstrict = true\n[decode]\nallow_partial = true\n",
        "reject",
    ),
    ("section-as-scalar", "decode = true\n", "reject"),
    ("non-string-enum", "[decode]\ninput_time_format = 1\n", "reject"),
    # L2-CFG-012: the pre-v3.0.0 spelling. Rejected by NAME rather than left to
    # the unknown-key WARN, because silently discarding a forced format would
    # revert the decode to auto-detection without saying so. Renaming the key
    # above without adding this would have quietly turned `non-string-enum`
    # into a no-op unknown-key case.
    ("retired-time-format-key", '[decode]\ntime_format = "irig"\n', "reject"),
    # The three v3.0.0 rendering keys (L2-CFG-012).
    # A calendar rendering needs a year, so `iso` ALONE is a refusal, not an
    # acceptance (L2-WRT-026 clause 1). Both spellings are pinned here because
    # the pair is the interesting part: the key is valid, the configuration is
    # not, and the two implementations must agree on which is which.
    ("output-time-format", '[output]\noutput_time_format = "iso"\nyear = 2026\n', "accept"),
    ("output-time-format-without-year", '[output]\noutput_time_format = "iso"\n', "reject"),
    ("output-time-format-doy", '[output]\noutput_time_format = "doy"\n', "accept"),
    ("output-time-format-bad", '[output]\noutput_time_format = "elapsed"\n', "reject"),
    ("output-year", "[output]\nyear = 2026\n", "accept"),
    ("output-year-out-of-range", "[output]\nyear = 10000\n", "reject"),
    ("output-year-non-int", '[output]\nyear = "2026"\n', "reject"),
    ("output-utc-offset", '[output]\nutc_offset = "-05:00"\n', "accept"),
    ("output-utc-offset-bad", '[output]\nutc_offset = "+24:00"\n', "reject"),
    ("quoted-key", '[decode]\n"stri.ct" = true\n', "reject"),
    ("trailing-after-header", "[decode] junk\nstrict = true\n", "reject"),
    ("unterminated-section", "[decode\nstrict = true\n", "reject"),
    ("empty-section-name", "[]\nstrict = true\n", "reject"),
    ("hyphen-section-name", "[bad-section]\nstrict = true\n", "reject"),
    ("quoted-section-name", '["bad"]\nstrict = true\n', "reject"),
    ("space-in-section-name", "[bad section]\nstrict = true\n", "reject"),
    # ── numeric literals Rust's native i64/f64 accept but TOML rejects ──────
    ("leading-zero-int", "[decode]\ndetect_records = 08\n", "reject"),
    ("bare-trailing-dot", "[decode]\nstandard_tick_rate_hz = 1.\n", "reject"),
    ("leading-zero-in-array", "[filter]\nexclude_rts = [01]\n", "reject"),
    ("zero-then-zero", "[decode]\ndetect_records = 00\n", "reject"),
    # ── string escapes: only \" \\ \n \t are supported on both ─────────────
    ("escaped-quote-string", '[mux]\ndelimiter = "\\""\n', "accept"),
    ("carriage-return-escape", '[mux]\ndelimiter = "\\r"\n', "reject"),
    ("unicode-escape", '[mux]\ndelimiter = "\\u002C"\n', "reject"),
    # An array string containing an escaped quote AND a comma: the comma is
    # inside the string, so the array has one element — the splitter must not
    # break on it. Under an unknown key both just warn-and-accept.
    ("array-escaped-quote", '[bogus]\nunknown_key = ["a\\", b"]\n', "accept"),
]


def check_config_parser_parity(
    invocations: dict[str, list[str]], root: Path, input_mie: Path, temp: Path
) -> None:
    """Drive ``CORPUS`` through every implementation; raise on any divergence or
    mismatch against the expected verdict.

    ``invocations`` maps an implementation name to its CLI prefix. Taking the
    whole mapping rather than two binaries is what lets a third implementation
    join without touching this function -- and what makes the comparison
    all-pairs by construction rather than by a chain of ``!=``.

    ``input_mie`` is a materialized, valid single-record recording so an accepted
    config decodes to exit 0. Only the config differs between snippets.
    """
    failures: list[str] = []
    for name, toml, expect in CORPUS:
        cfg = temp / f"parity-{name}.toml"
        cfg.write_text(toml, encoding="utf-8")
        classes: dict[str, str] = {}
        codes: dict[str, int] = {}
        for impl, prefix in invocations.items():
            out = temp / f"parity-{name}-{impl}.csv"
            command = [
                *prefix,
                "--config",
                str(cfg),
                "decode",
                str(input_mie),
                "-o",
                str(out),
            ]
            result = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            codes[impl] = result.returncode
            classes[impl] = classify(result.returncode)

        divergence = describe_divergence(classes, codes)
        if divergence is not None:
            failures.append(f"{name}: DIVERGENT - {divergence}")
        elif next(iter(classes.values())) != expect:
            # Unanimous, and unanimously wrong. Worth reporting separately from
            # a divergence: every parser agreeing on the wrong answer is a
            # corpus or specification problem, not an implementation one.
            failures.append(f"{name}: {describe_agreement(classes, codes)}, expected {expect}")
    if failures:
        raise AssertionError("config-parser parity failures:\n  " + "\n  ".join(failures))
    print(f"PASS config-parser-parity ({len(CORPUS)} snippets across {', '.join(invocations)})")
