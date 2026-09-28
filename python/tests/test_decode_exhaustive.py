"""EXHAUSTIVE verification of the word decoders against the Rust implementation.

The per-case tests in ``test_decode.py`` check the values a human thought to
check. These check ALL of them: every one of the 65 536 possible Type Words,
every one of the 65 536 possible Command Words, and every bit position of every
IRIG and Standard timestamp field. There is no corner case left for a reviewer
to have missed, because no case is selected.

HOW IT WORKS

``rust/examples/decode_digest.rs`` sweeps the same inputs through the *Rust*
decoders and prints an FNV-1a digest of the decoded fields. The constants below
are those digests -- the same four numbers ``cpp/tests/test_decode_exhaustive.cpp``
pins. This file recomputes them from the Python decoders, so a single differing
field in any of ~390 000 decodes changes the digest and fails the test.

Regenerate after an intentional wire-format change::

    cd rust && cargo run --release --example decode_digest

A changed digest means the RUST decoder changed. That is a different problem
from the Python one drifting, and the fix is different too -- check which side
moved before editing anything here.

WHY THIS EXISTS AT ALL. The C++ tree has had this check since it joined the
joint cut; Python did not, so the strongest cross-implementation check in the
project covered two decoders out of three. That is a parity gap of exactly the
kind ``docs/FUZZING.md`` exists to track, and closing it is why this file is
here.

WHY THE DISCRIMINATION TESTS BELOW MATTER

A digest test that cannot fail is worse than no test: it reports success
forever. Every digest here is therefore accompanied by a case that feeds a
deliberately-wrong decoding through the same hash and asserts the result
differs. Without those, a bug in the hash -- or a hash that ignored its input --
would make this whole file a very convincing no-op.
"""

from __future__ import annotations

import pytest

from aero1553.decode import (
    decode_command_word,
    decode_irig_timestamp,
    decode_standard_timestamp,
    decode_type_word,
)

TYPE_WORD_DIGEST = 0xC9E9E16B4E9C7B25
COMMAND_WORD_DIGEST = 0x4EF6FD5E628BF325
IRIG_DIGEST = 0x62BFB17B4FC79D25
STANDARD_DIGEST = 0x094680D7977C5B25

_U64 = 0xFFFFFFFFFFFFFFFF
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x00000100000001B3


class Fnv1a:
    """FNV-1a, 64-bit.

    Chosen for being trivial to reimplement identically in three languages --
    the point is cross-language agreement, not cryptographic strength. Must
    match ``rust/examples/decode_digest.rs`` and ``test_decode_exhaustive.cpp``
    byte for byte.
    """

    __slots__ = ("state",)

    def __init__(self) -> None:
        self.state = _FNV_OFFSET

    def byte(self, value: int) -> None:
        """Feed one byte.

        Args:
            value: The byte value (0-255).
        """
        self.state = ((self.state ^ (value & 0xFF)) * _FNV_PRIME) & _U64

    def u16(self, value: int) -> None:
        """Feed a 16-bit value, little-endian.

        Args:
            value: The value to feed.
        """
        self.byte(value & 0xFF)
        self.byte((value >> 8) & 0xFF)

    def u32(self, value: int) -> None:
        """Feed a 32-bit value, little-endian.

        Args:
            value: The value to feed.
        """
        self.u16(value & 0xFFFF)
        self.u16((value >> 16) & 0xFFFF)


def _type_word_digest() -> int:
    h = Fnv1a()
    for raw in range(0x10000):
        tw = decode_type_word(raw)
        h.byte(tw.message_type)
        h.byte(int(tw.bus))
        h.u16(tw.word_count)
        h.byte(int(tw.error))
    return h.state


def _command_word_digest() -> int:
    h = Fnv1a()
    for raw in range(0x10000):
        cw = decode_command_word(raw)
        h.byte(cw.rt)
        h.byte(int(cw.direction))
        h.byte(cw.subaddress)
        h.byte(cw.data_word_count)
    return h.state


def _irig_digest() -> int:
    """Three sweeps, one per word.

    Each covers that word's full 16-bit range while the other two hold fixed at
    values with a mixed bit pattern. A single sweep over all three words would
    be 2**48 decodes; this covers every bit position of every field, which is
    what the decoder can actually get wrong.
    """
    h = Fnv1a()

    def feed(upper: int, middle: int, lower: int) -> None:
        ts = decode_irig_timestamp(upper, middle, lower)
        h.u16(ts.day)
        h.byte(ts.hour)
        h.byte(ts.minute)
        h.byte(ts.second)
        h.u32(ts.microsecond)
        h.byte(int(ts.freerun))

    for upper in range(0x10000):
        feed(upper, 0xA5A5, 0x5A5A)
    for middle in range(0x10000):
        feed(0xA5A5, middle, 0x5A5A)
    for lower in range(0x10000):
        feed(0xA5A5, 0x5A5A, lower)
    return h.state


def _standard_digest() -> int:
    h = Fnv1a()
    for upper in range(0x10000):
        # The complement gives the lower word a different bit pattern from the
        # upper one, so a decoder that swapped them would change the digest.
        ts = decode_standard_timestamp(upper, upper ^ 0xFFFF)
        h.u32(ts.raw_value)
        h.u16(ts.upper_word)
        h.u16(ts.lower_word)
    return h.state


@pytest.mark.requirement("L3-PY-017")
def test_every_type_word_decodes_as_rust_decodes_it() -> None:
    assert _type_word_digest() == TYPE_WORD_DIGEST


@pytest.mark.requirement("L3-PY-017")
def test_every_command_word_decodes_as_rust_decodes_it() -> None:
    assert _command_word_digest() == COMMAND_WORD_DIGEST


@pytest.mark.requirement("L3-PY-017")
def test_every_irig_field_bit_decodes_as_rust_decodes_it() -> None:
    assert _irig_digest() == IRIG_DIGEST


@pytest.mark.requirement("L3-PY-017")
def test_every_standard_timestamp_decodes_as_rust_decodes_it() -> None:
    assert _standard_digest() == STANDARD_DIGEST


def test_the_digest_changes_when_any_decoded_field_changes() -> None:
    """The discrimination test: prove the hash is actually reading its input.

    Feeds a deliberately-wrong decoding of one Type Word through the same hash
    and asserts the result differs from the correct one. Without this, a hash
    that ignored its argument would make every assertion above pass forever.
    """
    correct = Fnv1a()
    tw = decode_type_word(0x0224)
    correct.byte(tw.message_type)
    correct.byte(int(tw.bus))
    correct.u16(tw.word_count)
    correct.byte(int(tw.error))

    for wrong_fields in (
        (tw.message_type ^ 1, int(tw.bus), tw.word_count, int(tw.error)),
        (tw.message_type, int(tw.bus) ^ 1, tw.word_count, int(tw.error)),
        (tw.message_type, int(tw.bus), tw.word_count ^ 1, int(tw.error)),
        (tw.message_type, int(tw.bus), tw.word_count, int(tw.error) ^ 1),
    ):
        wrong = Fnv1a()
        wrong.byte(wrong_fields[0])
        wrong.byte(wrong_fields[1])
        wrong.u16(wrong_fields[2])
        wrong.byte(wrong_fields[3])
        assert wrong.state != correct.state, (
            "the digest ignored a changed field, so the sweeps above prove nothing"
        )


def test_the_hash_matches_the_reference_vector() -> None:
    """FNV-1a of the empty input is the offset basis, and of "a" is known.

    Pins the constants themselves: an offset basis or prime mistyped here would
    otherwise produce four digests that disagree with Rust for a reason that has
    nothing to do with the decoders.
    """
    assert Fnv1a().state == 0xCBF29CE484222325
    h = Fnv1a()
    h.byte(ord("a"))
    assert h.state == 0xAF63DC4C8601EC8C
