"""Shared machinery for the L1-ROB-001 fuzz harnesses.

Every fuzz harness in this tree draws from the same generator as the Rust and
C++ ones -- same xorshift64, same seed, same draw order -- and reports through
the same ``FUZZ-SUMMARY`` line. That is the whole point: three implementations
proving "I did not crash" in three different shapes cannot be compared, and a
shared generator is wasted if the harnesses do not also agree on what they
count.

It lives in its own module rather than on a test class because the harnesses are
spread across files: the reader and dump harnesses are in ``test_e2e.py``, the
merge input-resolution harness is in ``test_merge.py``, and duplicating the
generator between them is exactly how the merge harness came to use
``random.Random`` and see different inputs from every other harness in the
project.

See ``docs/FUZZING.md`` for the map of what is fuzzed and what is not.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

FUZZ_SEED = 0x0DDC_D1EC_DDC0_DEC0
"""The seed every fuzz harness in every implementation starts from."""

_U64 = 0xFFFFFFFFFFFFFFFF

GLOB_ALPHABET = (
    "*",
    "*",
    "*",
    "?",
    "?",
    ".",
    "a",
    "b",
    "m",
    "i",
    "e",
    "-",
    "x",
    "é",
    "中",
    "/",
)
"""The glob-pattern alphabet the merge fuzz harness draws from.

Shared verbatim with ``rust/tests/integration.rs`` and ``cpp/tests/test_fuzz.cpp``.

A pattern built by lossily UTF-8-decoding random bytes would be the obvious
thing, and it is wrong twice over. Random bytes almost never contain ``*`` or
``?``, so the matcher's interesting branches are never reached; and the three
languages' lossy decoders do not agree character-for-character on how many
U+FFFD an invalid sequence produces, so the counters could diverge without the
glob matchers disagreeing about anything.

``*`` and ``?`` are weighted (three and two slots) because the first version of
this harness drew uniformly over patterns up to 95 characters long and matched a
probe *zero* times in 512 iterations -- it fuzzed the reject path and nothing
else. The last two entries are deliberately non-ASCII: Rust and Python match
over scalar values and the C++ matcher advances ``?`` by a whole UTF-8
character, and this is the surface where that agreement is either real or it is
not.

``/`` is there so ``expand_glob`` sees a directory part at all: without it every
pattern was a bare filename, and neither the textual split at the last separator
nor the directory-wildcard refusal (L2-MRG-001) was ever reached. ``/`` and not
a backslash: a forward slash is a separator on every platform, a backslash only
on Windows, so it would split the same pattern differently on the two CI hosts
and the counters would legitimately differ.
"""

GLOB_PROBES = ("some.name.mie", "café.mie", "中文.mie")
"""Names the generated patterns are matched against: ASCII, Latin-1, CJK.

Counted separately so a divergence says which one broke.
"""


def glob_pattern(payload: bytes) -> str:
    """Map bytes onto :data:`GLOB_ALPHABET` to build a glob pattern.

    Args:
        payload: Bytes drawn from the shared generator.

    Returns:
        The pattern, one alphabet entry per input byte.
    """
    return "".join(GLOB_ALPHABET[b % len(GLOB_ALPHABET)] for b in payload)


def xorshift64(state: int) -> tuple[int, int]:
    """One step of the shared PRNG.

    Not a good PRNG; a *reproducible* one, which is the property that matters.
    Byte-for-byte the generator in ``rust/tests/integration.rs`` and
    ``cpp/tests/test_fuzz.cpp``.

    Args:
        state: The current 64-bit state.

    Returns:
        ``(new_state, output)`` -- both are the same value, as in the Rust and
        C++ spellings.
    """
    x = state & _U64
    x ^= (x << 13) & _U64
    x ^= (x >> 7) & _U64
    x ^= (x << 17) & _U64
    x &= _U64
    return x, x


def fill(state: int, size: int) -> tuple[int, bytes]:
    """Draw ``size`` bytes: eight at a time little-endian, then the tail.

    The draw *order* is as much a part of the contract as the PRNG -- Rust and
    C++ consume the stream identically, which is what makes iteration N the same
    bytes in all three implementations.

    Args:
        state: The current 64-bit PRNG state.
        size: How many bytes to draw.

    Returns:
        ``(new_state, payload)``.
    """
    payload = bytearray(size)
    j = 0
    while j + 8 <= size:
        state, r = xorshift64(state)
        payload[j : j + 8] = r.to_bytes(8, "little")
        j += 8
    while j < size:
        state, r = xorshift64(state)
        payload[j] = r & 0xFF
        j += 1
    return state, bytes(payload)


def iterations(default: int = 256) -> int:
    """``MIE_FUZZ_ITERATIONS``, or ``default``.

    A value that does not parse, or parses to zero, falls back rather than
    guessing -- the same rule in all three implementations.

    Args:
        default: What to use when the variable is absent or unusable.

    Returns:
        The iteration count to run.
    """
    override = os.environ.get("MIE_FUZZ_ITERATIONS")
    if override and override.isdigit() and int(override) > 0:
        return int(override)
    return default


def stream_logs() -> bool:
    """Whether ``MIE_FUZZ_STREAM_LOGS`` asks for the decoder's diagnostics.

    Returns:
        True when the variable is set to ``1`` / ``true`` / ``TRUE``.
    """
    return os.environ.get("MIE_FUZZ_STREAM_LOGS") in {"1", "true", "TRUE"}


@contextlib.contextmanager
def fuzz_logging() -> Iterator[None]:
    """Silence the ``aero1553`` logger unless ``MIE_FUZZ_STREAM_LOGS`` asks.

    The decoder logs through the ``aero1553`` logger; with no handler
    configured (the fuzz tests call the library directly, not the CLI) pytest's
    log capture swallows the records -- but the package still *formats* every
    one of them. Setting the level to ``OFF`` skips the formatting entirely,
    which is what the Rust and C++ harnesses do with ``Level::Off`` /
    ``LEVEL_OFF``.

    When the knob is set, the package's own stderr handler is installed at
    WARNING so the diagnostics stream under ``pytest -s``. The package logger is
    restored afterward so no handler is left bound to this test's soon-stale
    captured stream.

    Yields:
        None. The block runs with the logger configured.
    """
    from aero1553.logger import configure_logging

    mie_log = logging.getLogger("aero1553")
    saved_handlers = mie_log.handlers[:]
    saved_level = mie_log.level
    saved_propagate = mie_log.propagate
    configure_logging("WARNING" if stream_logs() else "OFF")
    try:
        yield
    finally:
        for h in mie_log.handlers[:]:
            mie_log.removeHandler(h)
        for h in saved_handlers:
            mie_log.addHandler(h)
        mie_log.setLevel(saved_level)
        mie_log.propagate = saved_propagate


def summary(harness: str, count: int, fields: str) -> None:
    """Emit one harness's ``FUZZ-SUMMARY`` line.

    The line has the same shape in all three implementations, so the burn-in
    jobs produce artifacts that can be *diffed* rather than three differently
    shaped pass messages. On identical inputs the counters should be identical
    too; ``scripts/compare-fuzz-summaries.py`` fails the run when they are not.

    Every field must be **path-independent**. The three implementations name
    their temp files differently, so anything derived from a path measures the
    harness rather than the decoder -- which is why the dump harness counts
    output lines and not bytes.

    Written to ``MIE_FUZZ_SUMMARY`` when that names a file -- a file survives
    both pytest's capture and a job log's truncation -- and to stderr, where it
    is visible under ``pytest -s``.

    Args:
        harness: Harness name, matching the other implementations' spelling.
        count: The iteration count this run used.
        fields: Space-separated ``key=value`` pairs, harness-specific.
    """
    line = (
        f"FUZZ-SUMMARY impl=python harness={harness} "
        f"seed=0x{FUZZ_SEED:016X} iterations={count} {fields}"
    )
    sys.stderr.write(line + "\n")
    target = os.environ.get("MIE_FUZZ_SUMMARY")
    if target:
        with Path(target).open("a", encoding="ascii") as handle:
            handle.write(line + "\n")
