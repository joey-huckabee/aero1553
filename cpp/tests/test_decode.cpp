// SPDX-License-Identifier: Apache-2.0
//
// Tests for the pure binary-to-struct decoders.
//
// Bit positions and thresholds asserted here are wire format (docs/MIE-FORMAT.md)
// and are shared with the Rust and Python implementations. The raw words used as
// fixtures are the same ones those implementations' unit tests use, so a
// divergence shows up here rather than in a conformance diff.

#include "mie/decode.hpp"
#include "record_fixtures.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <vector>

namespace {

namespace dec = mie::decode;

using mie_test::le_bytes;

}  // namespace

// ---------------------------------------------------------------------------
// Primitive readers
// ---------------------------------------------------------------------------

TEST_CASE("read_u16 decodes little-endian", "[decode][L3-CPP-004]") {
    const uint8_t data[] = {0x02, 0x24, 0x7E, 0x79};
    uint16_t value = 0;

    REQUIRE(dec::read_u16(data, sizeof(data), 0, value));
    CHECK(value == 0x2402);
    REQUIRE(dec::read_u16(data, sizeof(data), 2, value));
    CHECK(value == 0x797E);
}

TEST_CASE("read_u16 refuses to read past the end", "[decode][L1-ROB-001]") {
    // The mapping's length is the only thing between a corrupt word count and a
    // read past the end of the file, so these are safety checks, not tidiness.
    const uint8_t data[] = {0x01, 0x02, 0x03, 0x04};
    uint16_t value = 0;

    CHECK_FALSE(dec::read_u16(data, sizeof(data), 3, value));  // one byte short
    CHECK_FALSE(dec::read_u16(data, sizeof(data), 4, value));  // exactly at end
    CHECK_FALSE(dec::read_u16(data, sizeof(data), 99, value));

    SECTION("an offset near the maximum cannot wrap into a valid range") {
        // `offset + 2 > size` wraps for an offset near SIZE_MAX and would admit
        // the read. A corrupt word count is exactly how such an offset arises,
        // so the bounds test is written to avoid the addition entirely.
        const std::size_t huge = static_cast<std::size_t>(-1);
        CHECK_FALSE(dec::read_u16(data, sizeof(data), huge, value));
        CHECK_FALSE(dec::read_u16(data, sizeof(data), huge - 1, value));
    }
}

TEST_CASE("read_u16_array fills only on a fully in-bounds range", "[decode][L1-ROB-001]") {
    const uint16_t words[] = {0x1111, 0x2222, 0x3333};
    const std::vector<uint8_t> data = le_bytes(words, 3);
    uint16_t out[3] = {0, 0, 0};

    REQUIRE(dec::read_u16_array(&data[0], data.size(), 0, 3, out));
    CHECK(out[0] == 0x1111);
    CHECK(out[2] == 0x3333);

    SECTION("a partially out-of-range request is refused outright") {
        uint16_t partial[3] = {0xAAAA, 0xAAAA, 0xAAAA};
        CHECK_FALSE(dec::read_u16_array(&data[0], data.size(), 2, 3, partial));
        // Untouched: a caller that ignored the return value must not find a
        // half-filled buffer that looks like real data.
        CHECK(partial[0] == 0xAAAA);
    }

    SECTION("a zero-length read succeeds trivially") {
        CHECK(dec::read_u16_array(&data[0], data.size(), 0, 0, out));
    }

    SECTION("an absurd count cannot overflow the byte calculation") {
        const std::size_t huge = static_cast<std::size_t>(-1) / 2;
        CHECK_FALSE(dec::read_u16_array(&data[0], data.size(), 0, huge, out));
    }
}

// ---------------------------------------------------------------------------
// Field decoders
// ---------------------------------------------------------------------------

TEST_CASE("Type Word bit layout", "[decode][typeword]") {
    // 0x2402: type 0x02 (BC to RT), bus A, word count 36, no error.
    const mie::TypeWord tw = dec::decode_type_word(0x2402);
    CHECK(tw.message_type == 0x02);
    CHECK(tw.bus == mie::BUS_A);
    CHECK(tw.word_count == 36);
    CHECK_FALSE(tw.error);
    CHECK(tw.raw == 0x2402);

    SECTION("bit 7 selects the bus") { CHECK(dec::decode_type_word(0x2482).bus == mie::BUS_B); }

    SECTION("bit 14 is the error flag") {
        const mie::TypeWord errored = dec::decode_type_word(0x6402);
        CHECK(errored.error);
        // The flag must not bleed into the word count -- they are adjacent
        // fields, and a mask that included bit 14 would inflate every errored
        // record's length.
        CHECK(errored.word_count == 36);
        CHECK(errored.message_type == 0x02);
    }

    SECTION("the word count is six bits, not eight") {
        // Bits 8-13. Bit 14 is the error flag and bit 15 is reserved, so a
        // 0xFF00 raw must yield 63, not 255.
        CHECK(dec::decode_type_word(0xFF00).word_count == 63);
    }

    SECTION("the message type is seven bits") {
        CHECK(dec::decode_type_word(0x00FF).message_type == 0x7F);
    }
}

TEST_CASE("IRIG timestamp bit layout", "[decode][timestamp]") {
    // day 10, hour 15, minute 54, second 50, microsecond 456225.
    const uint16_t upper = static_cast<uint16_t>((10 << 5) | 15);
    const uint16_t middle = static_cast<uint16_t>((54 << 10) | (50 << 4) | ((456225 >> 16) & 0xF));
    const uint16_t lower = static_cast<uint16_t>(456225 & 0xFFFF);

    const mie::IrigTimestamp ts = dec::decode_irig_timestamp(upper, middle, lower);
    CHECK(ts.day == 10);
    CHECK(ts.hour == 15);
    CHECK(ts.minute == 54);
    CHECK(ts.second == 50);
    CHECK(ts.microsecond == 456225);
    CHECK_FALSE(ts.freerun);
    CHECK(ts.format() == "10:15:54:50.456225");

    SECTION("bit 15 of the upper word is the freerun flag") {
        const mie::IrigTimestamp freerun =
            dec::decode_irig_timestamp(static_cast<uint16_t>(upper | 0x8000), middle, lower);
        CHECK(freerun.freerun);
        // Setting it must not corrupt the day, which occupies the adjacent bits.
        CHECK(freerun.day == 10);
    }

    SECTION("the microsecond spans two words") {
        // The high nibble lives in the middle word and the rest in the lower
        // word. A decoder that read only the lower word would cap at 65535 and
        // silently truncate the top of every microsecond above that.
        const mie::IrigTimestamp big = dec::decode_irig_timestamp(0, 0x000F, 0xFFFF);
        CHECK(big.microsecond == 0xFFFFF);
        CHECK(big.microsecond > 65535);
    }

    SECTION("the day field is nine bits, enough for a leap year") {
        const mie::IrigTimestamp late =
            dec::decode_irig_timestamp(static_cast<uint16_t>((366 << 5) | 23), 0, 0);
        CHECK(late.day == 366);
    }
}

TEST_CASE("Standard timestamp is a 32-bit counter, upper word first", "[decode][timestamp]") {
    const mie::StandardTimestamp ts = dec::decode_standard_timestamp(0x0012, 0xABCD);
    CHECK(ts.raw_value == 0x0012ABCD);
    CHECK(ts.upper_word == 0x0012);
    CHECK(ts.lower_word == 0xABCD);
    CHECK(ts.format() == "0x0012ABCD");

    SECTION("the full range survives without sign extension") {
        const mie::StandardTimestamp max = dec::decode_standard_timestamp(0xFFFF, 0xFFFF);
        CHECK(max.raw_value == 0xFFFFFFFFu);
    }
}

TEST_CASE("Command Word bit layout", "[decode][commandword]") {
    // 0x797E: RT 15, RECEIVE, subaddress 11, 30 data words. The same raw word
    // the Rust and Python unit tests use, and the one whose CSV row reads
    // "15,11R" -- that trailing R is the direction, which makes this fixture's
    // meaning cross-checkable rather than asserted from memory. The first draft
    // of this test called it transmit and was wrong.
    const mie::CommandWord cmd = dec::decode_command_word(0x797E);
    CHECK(cmd.rt == 15);
    CHECK(cmd.direction == mie::DIRECTION_RECEIVE);
    CHECK(cmd.subaddress == 11);
    CHECK(cmd.data_word_count == 30);
    CHECK(cmd.raw == 0x797E);

    SECTION("a raw data-word-count of zero means thirty-two") {
        // The five-bit field cannot encode 32, so the bus standard reuses 0.
        // Read literally, every full-length transaction would decode as empty.
        const mie::CommandWord full = dec::decode_command_word(0x7960);
        CHECK(full.data_word_count == 32);
    }

    SECTION("bit 10 is the direction") {
        // Setting bit 10 on the same word flips it to transmit and leaves every
        // other field alone.
        const mie::CommandWord transmit = dec::decode_command_word(0x7D7E);
        CHECK(transmit.direction == mie::DIRECTION_TRANSMIT);
        CHECK(transmit.rt == 15);
        CHECK(transmit.subaddress == 11);
        CHECK(transmit.data_word_count == 30);
    }

    SECTION("RT 31 is broadcast and subaddress 0 or 31 is a mode code") {
        const mie::CommandWord broadcast = dec::decode_command_word(0xFC01);
        CHECK(broadcast.rt == 31);
        CHECK(broadcast.is_broadcast());

        CHECK(dec::decode_command_word(0x0801).is_mode_code());        // subaddress 0
        CHECK(dec::decode_command_word(0x0BE1).is_mode_code());        // subaddress 31
        CHECK_FALSE(dec::decode_command_word(0x0961).is_mode_code());  // subaddress 11
    }
}

// ---------------------------------------------------------------------------
// Terminator
// ---------------------------------------------------------------------------

TEST_CASE("a null Type Word terminates the record stream", "[decode][L2-RDR-021]") {
    CHECK(dec::is_terminator_type_word(0x0000));
    CHECK_FALSE(dec::is_terminator_type_word(0x2402));

    SECTION("it is unambiguous because no record can be zero words long") {
        // The word-count field of 0x0000 is zero, and the minimum valid record
        // is four words -- so the terminator can never also be a record.
        CHECK(dec::decode_type_word(dec::TERMINATOR_TYPE_WORD).word_count <
              dec::MIN_RECORD_WORDS_STANDARD);
    }
}

TEST_CASE("the two timestamp formats have different record floors", "[decode]") {
    // Using the wrong floor is not an off-by-one: validating a Standard file
    // against the IRIG floor rejects every record in it.
    CHECK(dec::MIN_RECORD_BYTES == 10);
    CHECK(dec::MIN_RECORD_BYTES_STANDARD == 8);
    CHECK(dec::MIN_RECORD_WORDS == 5);
    CHECK(dec::MIN_RECORD_WORDS_STANDARD == 4);
}

// ---------------------------------------------------------------------------
// MUX from the file name (L2-WRT-020)
// ---------------------------------------------------------------------------

TEST_CASE("MUX takes the configured field from the file name", "[decode][mux][L2-WRT-020]") {
    std::string mux;
    REQUIRE(dec::mux_from_filename("rec.2026.01.02.BUS7.001.mie", ".", 4, mux));
    CHECK(mux == "BUS7");

    SECTION("a negative field counts from the end") {
        REQUIRE(dec::mux_from_filename("a.b.c.d", ".", -1, mux));
        CHECK(mux == "d");
        REQUIRE(dec::mux_from_filename("a.b.c.d", ".", -4, mux));
        CHECK(mux == "a");
    }

    SECTION("an out-of-range index yields no MUX rather than an error") {
        CHECK_FALSE(dec::mux_from_filename("a.b", ".", 9, mux));
        CHECK(mux.empty());
        CHECK_FALSE(dec::mux_from_filename("a.b", ".", -9, mux));
    }

    SECTION("an empty field is absent, not an empty value") {
        // A recorder emitting `name..part` should leave the column empty rather
        // than producing a blank-but-present cell.
        CHECK_FALSE(dec::mux_from_filename("a..c", ".", 1, mux));
        CHECK_FALSE(dec::mux_from_filename("a.   .c", ".", 1, mux));
    }

    SECTION("the field is trimmed of spaces and tabs only") {
        REQUIRE(dec::mux_from_filename("a. \tB7\t .c", ".", 1, mux));
        CHECK(mux == "B7");
        CHECK_FALSE(dec::mux_from_filename("a. \t .c", ".", 1, mux));
        // Unicode whitespace and the other ASCII whitespace bytes are part of
        // the name -- the same rule as a --manifest line. Rust once trimmed
        // them here, so a no-break space gave two different MUX values.
        const std::string nbsp = "\xC2\xA0";             // U+00A0
        const std::string ideographic = "\xE3\x80\x80";  // U+3000
        REQUIRE(dec::mux_from_filename("a." + nbsp + "B7" + ideographic + ".c", ".", 1, mux));
        CHECK(mux == nbsp + "B7" + ideographic);
        REQUIRE(dec::mux_from_filename("a." + nbsp + ".c", ".", 1, mux));
        CHECK(mux == nbsp);
        REQUIRE(dec::mux_from_filename("a.\rB7\v.c", ".", 1, mux));
        CHECK(mux == "\rB7\v");
    }

    SECTION("an empty delimiter declines rather than splitting per character") {
        CHECK_FALSE(dec::mux_from_filename("a.b.c", "", 0, mux));
    }

    SECTION("a multi-character delimiter splits on the whole string") {
        REQUIRE(dec::mux_from_filename("a--b--c", "--", 1, mux));
        CHECK(mux == "b");
    }

    SECTION("a name with no delimiter is one field") {
        REQUIRE(dec::mux_from_filename("recording", ".", 0, mux));
        CHECK(mux == "recording");
        CHECK_FALSE(dec::mux_from_filename("recording", ".", 1, mux));
    }
}

// ---------------------------------------------------------------------------
// Message format classification
// ---------------------------------------------------------------------------

TEST_CASE("each known message type maps to a format", "[decode][format]") {
    const mie::CommandWord cmd = dec::decode_command_word(0x797E);
    mie::MessageFormat fmt = mie::FORMAT_SPURIOUS_DATA;

    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_BC_TO_RT, cmd, 36, 3, fmt));
    CHECK(fmt == mie::FORMAT_RECEIVE);
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_RT_TO_BC, cmd, 36, 3, fmt));
    CHECK(fmt == mie::FORMAT_TRANSMIT);
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_RT_TO_RT, cmd, 36, 3, fmt));
    CHECK(fmt == mie::FORMAT_RT_TO_RT);
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_BROADCAST_BC_TO_RT, cmd, 36, 3, fmt));
    CHECK(fmt == mie::FORMAT_RECEIVE_BROADCAST);
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_BROADCAST_RT_TO_RT, cmd, 36, 3, fmt));
    CHECK(fmt == mie::FORMAT_RT_TO_RT_BROADCAST);
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_SPURIOUS_DATA, cmd, 36, 3, fmt));
    CHECK(fmt == mie::FORMAT_SPURIOUS_DATA);
}

TEST_CASE("an unknown type is declined without inventing an offset", "[decode][format]") {
    // This function does not know the byte offset, so raising here would
    // produce an error citing offset 0 -- actively misleading. The reader
    // raises UnknownTypeWord with the offset it does know.
    const mie::CommandWord cmd = dec::decode_command_word(0x797E);
    mie::MessageFormat fmt = mie::FORMAT_RECEIVE;
    CHECK_FALSE(dec::classify_message_format(0x99, cmd, 36, 3, fmt));
    CHECK_FALSE(dec::classify_message_format(0x00, cmd, 36, 3, fmt));
}

TEST_CASE("mode-code thresholds are relative to the timestamp length",
          "[decode][format][L2-MSG-004]") {
    // THE reason these are not absolute: a Standard record is one word shorter
    // than the IRIG equivalent, so absolute thresholds misclassify every
    // Standard mode code.
    const mie::CommandWord tx_mode = dec::decode_command_word(0x0801 | 0x0400);  // sub 0, transmit
    mie::MessageFormat irig_fmt = mie::FORMAT_RECEIVE;
    mie::MessageFormat std_fmt = mie::FORMAT_RECEIVE;

    // Word count 7: with data under IRIG (3 + 4), and beyond it under Standard.
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_MODE_COMMAND, tx_mode, 7, 3, irig_fmt));
    REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_MODE_COMMAND, tx_mode, 6, 2, std_fmt));
    CHECK(irig_fmt == mie::FORMAT_MODE_CODE_TX_DATA);
    CHECK(std_fmt == mie::FORMAT_MODE_CODE_TX_DATA);

    SECTION("one word short of the threshold means no data word") {
        mie::MessageFormat fmt = mie::FORMAT_RECEIVE;
        REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_MODE_COMMAND, tx_mode, 6, 3, fmt));
        CHECK(fmt == mie::FORMAT_MODE_CODE_NO_DATA);
    }

    SECTION("direction selects between the two data-carrying shapes") {
        const mie::CommandWord rx_mode = dec::decode_command_word(0x0801);  // sub 0, receive
        mie::MessageFormat fmt = mie::FORMAT_RECEIVE;
        REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_MODE_COMMAND, rx_mode, 7, 3, fmt));
        CHECK(fmt == mie::FORMAT_MODE_CODE_RX_DATA);
    }

    SECTION("broadcast mode codes carry no status word, so the shapes differ") {
        // RT 31, subaddress 0.
        const mie::CommandWord bcast = dec::decode_command_word(0xF801);
        REQUIRE(bcast.is_broadcast());
        mie::MessageFormat fmt = mie::FORMAT_RECEIVE;

        REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_MODE_COMMAND, bcast, 6, 3, fmt));
        CHECK(fmt == mie::FORMAT_MODE_CODE_BCAST_DATA);
        REQUIRE(dec::classify_message_format(mie::MESSAGE_TYPE_MODE_COMMAND, bcast, 5, 3, fmt));
        CHECK(fmt == mie::FORMAT_MODE_CODE_BCAST_NO_DATA);
    }
}

// ---------------------------------------------------------------------------
// Structural invariants
// ---------------------------------------------------------------------------

TEST_CASE("per-type direction invariants reject a mismatched command", "[decode][invariant]") {
    dec::InvariantViolation v;

    SECTION("L2-SYN-020: BC-to-RT must be a Receive command") {
        const mie::TypeWord tw(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 36, false, 0x2402);
        const mie::CommandWord transmit = dec::decode_command_word(0x7D7E);
        REQUIRE(transmit.direction == mie::DIRECTION_TRANSMIT);

        CHECK_FALSE(dec::validate_structural_invariants(tw, transmit, mie::FORMAT_RECEIVE, 3, v));
        CHECK(v.kind == dec::INVARIANT_DIRECTION_BC_TO_RT);
        CHECK(v.severity == dec::SEVERITY_REJECT);
        // The raw word is quoted in the diagnostic so an operator can find the
        // offending record without re-deriving it.
        CHECK(v.detail.find("0x7D7E") != std::string::npos);
    }

    SECTION("L2-SYN-021: RT-to-BC must be a Transmit command") {
        const mie::TypeWord tw(mie::MESSAGE_TYPE_RT_TO_BC, mie::BUS_A, 36, false, 0x2404);
        const mie::CommandWord receive = dec::decode_command_word(0x797E);
        REQUIRE(receive.direction == mie::DIRECTION_RECEIVE);

        CHECK_FALSE(dec::validate_structural_invariants(tw, receive, mie::FORMAT_TRANSMIT, 3, v));
        CHECK(v.kind == dec::INVARIANT_DIRECTION_RT_TO_BC);
    }
}

TEST_CASE("the capacity invariant rejects a word count that cannot hold the payload",
          "[decode][invariant][L2-SYN-022]") {
    // Receive needs Type(1) + TS(3) + Cmd(1) + data + status.
    // Built field by field rather than as a magic constant: OR-ing a count into
    // a word that already has those bits set does not produce that count, which
    // is how the first draft of this fixture ended up meaning something other
    // than its comment claimed.
    const mie::CommandWord cmd =
        dec::decode_command_word(static_cast<uint16_t>((14 << 11) | (11 << 5) | 30));
    dec::InvariantViolation v;

    const mie::TypeWord too_small(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 10, false, 0x0A02);
    CHECK_FALSE(dec::validate_structural_invariants(too_small, cmd, mie::FORMAT_RECEIVE, 3, v));
    CHECK(v.kind == dec::INVARIANT_WORD_COUNT_CAPACITY);
    CHECK(v.severity == dec::SEVERITY_REJECT);

    SECTION("a sufficient word count passes") {
        const uint16_t needed = static_cast<uint16_t>(1 + 3 + 1 + cmd.data_word_count + 1);
        const mie::TypeWord ok(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, needed, false, 0);
        CHECK(dec::validate_structural_invariants(ok, cmd, mie::FORMAT_RECEIVE, 3, v));
    }

    SECTION("SPURIOUS_DATA has no capacity to check against") {
        // Variable size by nature; inventing a floor would reject valid
        // continuations of an errored transaction.
        const mie::TypeWord spurious(mie::MESSAGE_TYPE_SPURIOUS_DATA, mie::BUS_A, 5, false, 0x0520);
        CHECK(dec::validate_structural_invariants(spurious, cmd, mie::FORMAT_SPURIOUS_DATA, 3, v));
    }
}

TEST_CASE("RT-to-RT post-extract invariants need Cmd2", "[decode][invariant]") {
    const mie::CommandWord cmd1 = dec::decode_command_word(0x797E);  // receive, 30 words
    dec::InvariantViolation v;

    SECTION("a no-op for non-RT-to-RT formats") {
        CHECK(dec::validate_post_extract_invariants(mie::FORMAT_RECEIVE, cmd1, mie::none(), v));
    }

    SECTION("a no-op when Cmd2 has not been extracted") {
        CHECK(dec::validate_post_extract_invariants(mie::FORMAT_RT_TO_RT, cmd1, mie::none(), v));
    }

    SECTION("L2-SYN-023: Cmd2 must be a Receive command") {
        const mie::CommandWord cmd2_tx = dec::decode_command_word(0x7D7E);
        REQUIRE(cmd2_tx.direction == mie::DIRECTION_TRANSMIT);
        CHECK_FALSE(dec::validate_post_extract_invariants(mie::FORMAT_RT_TO_RT, cmd1, cmd2_tx, v));
        CHECK(v.kind == dec::INVARIANT_DIRECTION_RT_TO_RT_CMD2);
    }

    SECTION("L2-SYN-027: the two commands must agree on the data word count") {
        // The capacity check only ever sees Cmd1, so a Cmd2 that over-claims
        // would otherwise pass unnoticed -- which is the point of this check.
        const mie::CommandWord cmd2_receive_29 =
            dec::decode_command_word(static_cast<uint16_t>((15 << 11) | (11 << 5) | 29));
        REQUIRE(cmd2_receive_29.direction == mie::DIRECTION_RECEIVE);
        REQUIRE(cmd2_receive_29.data_word_count != cmd1.data_word_count);

        CHECK_FALSE(
            dec::validate_post_extract_invariants(mie::FORMAT_RT_TO_RT, cmd1, cmd2_receive_29, v));
        CHECK(v.kind == dec::INVARIANT_DATA_WORD_COUNT_MISMATCH);
    }

    SECTION("agreeing commands pass") {
        const mie::CommandWord cmd2_ok =
            dec::decode_command_word(static_cast<uint16_t>((15 << 11) | (11 << 5) | 30));
        REQUIRE(cmd2_ok.data_word_count == cmd1.data_word_count);
        CHECK(dec::validate_post_extract_invariants(mie::FORMAT_RT_TO_RT, cmd1, cmd2_ok, v));
    }
}

TEST_CASE("anomalies are reported without rejecting the record",
          "[decode][invariant][L2-SYN-024]") {
    // AnomalyWarn, not Reject: bus interference produces a Status/Cmd RT
    // mismatch on real recordings, and a set reserved bit may be an
    // undocumented vendor extension. Rejecting either would drop valid records.
    const mie::CommandWord cmd = dec::decode_command_word(0x797E);  // RT 15
    const mie::TypeWord clean(mie::MESSAGE_TYPE_RT_TO_BC, mie::BUS_A, 36, false, 0x2404);

    SECTION("a matching Status RT produces nothing") {
        const uint16_t status_rt15 = static_cast<uint16_t>(15 << 11);
        CHECK(dec::detect_record_anomalies(clean, cmd, status_rt15).empty());
    }

    SECTION("a mismatched Status RT is a warning") {
        const uint16_t status_rt7 = static_cast<uint16_t>(7 << 11);
        const std::vector<dec::InvariantViolation> found =
            dec::detect_record_anomalies(clean, cmd, status_rt7);
        REQUIRE(found.size() == 1);
        CHECK(found[0].kind == dec::INVARIANT_STATUS_RT_MISMATCH);
        CHECK(found[0].severity == dec::SEVERITY_ANOMALY_WARN);
    }

    SECTION("no status word means no status check") {
        CHECK(dec::detect_record_anomalies(clean, cmd, mie::none()).empty());
    }

    SECTION("a set reserved bit is a warning") {
        const mie::TypeWord reserved(mie::MESSAGE_TYPE_RT_TO_BC, mie::BUS_A, 36, false, 0xA404);
        const std::vector<dec::InvariantViolation> found =
            dec::detect_record_anomalies(reserved, cmd, mie::none());
        REQUIRE(found.size() == 1);
        CHECK(found[0].kind == dec::INVARIANT_TYPE_WORD_RESERVED_BIT);
    }

    SECTION("both anomalies on one record are both reported") {
        // Reporting only the first would hide the other from an operator trying
        // to characterise a noisy bus.
        const mie::TypeWord reserved(mie::MESSAGE_TYPE_RT_TO_BC, mie::BUS_A, 36, false, 0xA404);
        const uint16_t status_rt7 = static_cast<uint16_t>(7 << 11);
        CHECK(dec::detect_record_anomalies(reserved, cmd, status_rt7).size() == 2);
    }
}

// ---------------------------------------------------------------------------
// Timestamp-format auto-detection
// ---------------------------------------------------------------------------

namespace {

/// A well-formed IRIG BC-to-RT record: Type, 3 timestamp words, Cmd, Status,
/// then `data_words` data words. Word count is set so the IRIG scorer's
/// plausibility test matches.
std::vector<uint8_t> irig_record(uint8_t data_words) {
    // Built from the shared wire-format helpers rather than by hand. The
    // hand-rolled version packed the same fields at the same offsets -- and
    // packed the Type Word WITHOUT masking, so an out-of-range argument would
    // have spilled into the bus and error bits rather than being caught.
    const uint16_t word_count = static_cast<uint16_t>(6 + data_words);
    std::vector<uint16_t> words;
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, word_count));
    // push_irig's defaults are day 10, hour 15, minute 54, second 50 -- exactly
    // the time this fixture used to spell out.
    mie_test::push_irig(words);
    words.push_back(mie_test::command_word(5, mie::DIRECTION_RECEIVE, 11, data_words));
    words.push_back(mie_test::status_word(5));
    for (uint8_t i = 0; i < data_words; ++i) {
        words.push_back(static_cast<uint16_t>(0x1000 + i));
    }
    return le_bytes(words);
}

}  // namespace

TEST_CASE("the probe picks IRIG for IRIG-shaped records", "[decode][probe][L2-DEC-015]") {
    const std::vector<uint8_t> data = irig_record(4);
    const dec::DetectionOutcome outcome = dec::probe_timestamp_format(&data[0], data.size(), 0, 8);

    CHECK(outcome.format == mie::TIMESTAMP_IRIG);
    CHECK(outcome.records_probed == 1);
    CHECK(outcome.irig_score > outcome.std_score);
}

TEST_CASE("IRIG wins a tie", "[decode][probe][L2-DEC-012]") {
    // Buffer of zeros: both candidates score nothing, so the scores tie at 0.
    // The tie-break must be IRIG, and the outcome must be reported as
    // ambiguous rather than as a confident IRIG call.
    const std::vector<uint8_t> zeros(64, 0);
    const dec::DetectionOutcome outcome =
        dec::probe_timestamp_format(&zeros[0], zeros.size(), 0, 8);

    CHECK(outcome.format == mie::TIMESTAMP_IRIG);
    CHECK(outcome.irig_score == outcome.std_score);
    CHECK(outcome.confidence == dec::CONFIDENCE_AMBIGUOUS);
}

TEST_CASE("confidence separates a decisive call from a marginal one",
          "[decode][probe][L2-DEC-016]") {
    SECTION("one perfect IRIG record clears the floor but is not decisive") {
        // The floor of 4 exists so a single decisive record passes; one perfect
        // IRIG record scores 5, which clears the floor without reaching the
        // decisive threshold of 8.
        const std::vector<uint8_t> data = irig_record(4);
        const dec::DetectionOutcome outcome =
            dec::probe_timestamp_format(&data[0], data.size(), 0, 1);
        CHECK(outcome.irig_score >= dec::CONFIDENCE_FLOOR);
        CHECK(outcome.confidence != dec::CONFIDENCE_AMBIGUOUS);
    }

    SECTION("a truncated buffer is probed as far as it goes") {
        // EOF stops the walk rather than failing it: a short file still gets
        // whatever verdict its records support.
        const std::vector<uint8_t> tiny(4, 0);
        const dec::DetectionOutcome outcome =
            dec::probe_timestamp_format(&tiny[0], tiny.size(), 0, 8);
        CHECK(outcome.records_probed == 0);
        CHECK(outcome.confidence == dec::CONFIDENCE_AMBIGUOUS);
    }
}

TEST_CASE("the probe cannot loop forever on corrupt input", "[decode][probe][L1-ROB-001]") {
    // A record whose declared word count is zero would advance the walk by zero
    // bytes. Without the non-advancing guard this spins until the record limit
    // -- and with a large --detect-records that is a hang, not a slow decode.
    std::vector<uint16_t> words;
    words.push_back(static_cast<uint16_t>(mie::MESSAGE_TYPE_BC_TO_RT));  // word count 0
    for (int i = 0; i < 16; ++i) {
        words.push_back(0x0000);
    }
    const std::vector<uint8_t> data = le_bytes(&words[0], words.size());

    const dec::DetectionOutcome outcome =
        dec::probe_timestamp_format(&data[0], data.size(), 0, 1000);
    CHECK(outcome.records_probed == 0);
}

TEST_CASE("a zero record limit is clamped rather than skipping the probe", "[decode][probe]") {
    const std::vector<uint8_t> data = irig_record(4);
    const dec::DetectionOutcome outcome = dec::probe_timestamp_format(&data[0], data.size(), 0, 0);
    CHECK(outcome.records_probed == 1);
}
