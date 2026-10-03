// SPDX-License-Identifier: Apache-2.0
//
// Tests for the wire-format model types.
//
// These pin values that are FORMAT rather than implementation choice: enum
// discriminants that appear on the wire, the CSV spellings of timestamps and
// message labels, and the error-code tables. A change here is a change to what
// the decoder reads or writes, not a refactor -- which is why several cases
// assert on literal numbers that look arbitrary.

#include "mie/models.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <vector>

TEST_CASE("bus and direction discriminants are wire values", "[models]") {
    CHECK(static_cast<int>(mie::BUS_A) == 0);
    CHECK(static_cast<int>(mie::BUS_B) == 1);
    CHECK(std::string(mie::bus_name(mie::BUS_A)) == "A");
    CHECK(std::string(mie::bus_name(mie::BUS_B)) == "B");

    SECTION("RECEIVE sorts before TRANSMIT") {
        // Load-bearing, not incidental: the canonical row order sorts by
        // (RT, subaddress, direction), and R-before-T falls out of these
        // numbers rather than needing a special case (L2-WRT-021). All three
        // implementations depend on it.
        CHECK(static_cast<int>(mie::DIRECTION_RECEIVE) == 0);
        CHECK(static_cast<int>(mie::DIRECTION_TRANSMIT) == 1);
        CHECK(mie::DIRECTION_RECEIVE < mie::DIRECTION_TRANSMIT);
    }
}

TEST_CASE("the known message-type set is exactly seven codes", "[models]") {
    const uint8_t known[] = {0x01, 0x02, 0x04, 0x08, 0x10, 0x18, 0x20};
    for (std::size_t i = 0; i < sizeof(known) / sizeof(known[0]); ++i) {
        CHECK(mie::is_valid_message_type(known[i]));
    }

    SECTION("everything else is rejected") {
        // Exhaustive over the byte, because sync uses this predicate to decide
        // whether a candidate offset holds a record. A code wrongly accepted
        // here becomes a false-positive resync on corrupt data.
        int accepted = 0;
        for (int code = 0; code <= 0xFF; ++code) {
            if (mie::is_valid_message_type(static_cast<uint8_t>(code))) {
                ++accepted;
            }
        }
        CHECK(accepted == 7);
    }
}

TEST_CASE("message type names round-trip through the CLI spelling", "[models]") {
    const uint8_t known[] = {0x01, 0x02, 0x04, 0x08, 0x10, 0x18, 0x20};
    for (std::size_t i = 0; i < sizeof(known) / sizeof(known[0]); ++i) {
        const std::string name = mie::message_type_name(known[i]);
        REQUIRE_FALSE(name.empty());
        uint8_t back = 0;
        REQUIRE(mie::message_type_from_name(name, back));
        CHECK(back == known[i]);
    }

    SECTION("lookup is case-insensitive, matching the other implementations") {
        uint8_t code = 0;
        CHECK(mie::message_type_from_name("bc_to_rt", code));
        CHECK(code == 0x02);
        CHECK(mie::message_type_from_name("Spurious_Data", code));
        CHECK(code == 0x20);
    }

    SECTION("an unknown name is declined rather than guessed") {
        uint8_t code = 0xEE;
        CHECK_FALSE(mie::message_type_from_name("BC_TO_RT_MAYBE", code));
        CHECK_FALSE(mie::message_type_from_name("", code));
    }

    CHECK(std::string(mie::message_type_name(0x99)).empty());
}

TEST_CASE("timestamp format names and word counts", "[models]") {
    mie::TimestampFormat fmt = mie::TIMESTAMP_AUTO;
    REQUIRE(mie::timestamp_format_from_name("IRIG", fmt));
    CHECK(fmt == mie::TIMESTAMP_IRIG);
    REQUIRE(mie::timestamp_format_from_name("standard", fmt));
    CHECK(fmt == mie::TIMESTAMP_STANDARD);
    REQUIRE(mie::timestamp_format_from_name("Auto", fmt));
    CHECK(fmt == mie::TIMESTAMP_AUTO);
    CHECK_FALSE(mie::timestamp_format_from_name("iri", fmt));

    // Wire layout: IRIG occupies three 16-bit words, Standard two. AUTO is a
    // request to probe, not a layout, so it consumes nothing.
    CHECK(mie::timestamp_word_count(mie::TIMESTAMP_IRIG) == 3);
    CHECK(mie::timestamp_word_count(mie::TIMESTAMP_STANDARD) == 2);
    CHECK(mie::timestamp_word_count(mie::TIMESTAMP_AUTO) == 0);
}

TEST_CASE("delta scope names round-trip", "[models]") {
    mie::DeltaScope scope = mie::DELTA_SCOPE_GLOBAL;
    REQUIRE(mie::delta_scope_from_name("per-file", scope));
    CHECK(scope == mie::DELTA_SCOPE_PER_FILE);
    REQUIRE(mie::delta_scope_from_name("GLOBAL", scope));
    CHECK(scope == mie::DELTA_SCOPE_GLOBAL);
    CHECK_FALSE(mie::delta_scope_from_name("perfile", scope));

    CHECK(std::string(mie::delta_scope_name(mie::DELTA_SCOPE_PER_FILE)) == "per-file");
    CHECK(std::string(mie::delta_scope_name(mie::DELTA_SCOPE_GLOBAL)) == "global");
}

// ---------------------------------------------------------------------------
// Error codes
// ---------------------------------------------------------------------------

TEST_CASE("the DDC and decoder-assigned code sets stay separate", "[models][errors]") {
    // The 0x01xx codes come from the card; the 0x20xx codes are assigned by
    // this decoder and have no hardware counterpart. Conflating them would
    // report a decoder classification as a bus fault.
    CHECK(mie::is_known_ddc_error_code(mie::ERROR_MANCHESTER_PARITY));
    CHECK(mie::is_known_ddc_error_code(mie::ERROR_NO_RESPONSE));
    CHECK(mie::is_known_ddc_error_code(mie::ERROR_INVERTED_SYNC));
    CHECK(mie::is_known_ddc_error_code(mie::ERROR_TOO_MANY_WORDS));
    CHECK(mie::is_known_ddc_error_code(mie::ERROR_UNKNOWN_DDC));
    CHECK_FALSE(mie::is_known_ddc_error_code(mie::ERROR_SPURIOUS_CONTINUATION));

    CHECK(mie::is_known_custom_error_code(mie::ERROR_SPURIOUS_CONTINUATION));
    CHECK(mie::is_known_custom_error_code(mie::ERROR_SPURIOUS_STANDALONE));
    CHECK_FALSE(mie::is_known_custom_error_code(mie::ERROR_NO_RESPONSE));

    CHECK(mie::is_known_error_code(mie::ERROR_NO_RESPONSE));
    CHECK(mie::is_known_error_code(mie::ERROR_SPURIOUS_STANDALONE));
    CHECK_FALSE(mie::is_known_error_code(0x0199));
}

TEST_CASE("code values match docs/ERROR-CATALOG.md", "[models][errors]") {
    // Spelled out rather than compared to themselves: these appear in operator
    // documentation and in the CSV, so a typo in the constant is a
    // documentation divergence, not just a wrong number.
    CHECK(mie::ERROR_MANCHESTER_PARITY == 0x011E);
    CHECK(mie::ERROR_NO_RESPONSE == 0x0120);
    CHECK(mie::ERROR_INVERTED_SYNC == 0x0136);
    CHECK(mie::ERROR_TOO_MANY_WORDS == 0x0140);
    CHECK(mie::ERROR_UNKNOWN_DDC == 0x0150);
    CHECK(mie::ERROR_SPURIOUS_CONTINUATION == 0x2000);
    CHECK(mie::ERROR_SPURIOUS_STANDALONE == 0x2001);
}

TEST_CASE("an unknown code gets a fallback description for humans", "[models][errors]") {
    CHECK(std::string(mie::ddc_error_description(mie::ERROR_NO_RESPONSE)) ==
          "No Status Response or Too Few Data Words");

    // The empty-string form renders as an uninformative "code=0x0199 ()", which
    // is why anything operator-facing uses the _or_unknown variant.
    CHECK(std::string(mie::ddc_error_description(0x0199)).empty());
    CHECK(std::string(mie::ddc_error_description_or_unknown(0x0199)) == "unknown DDC error code");
    CHECK(std::string(mie::ddc_error_description_or_unknown(mie::ERROR_NO_RESPONSE)) ==
          "No Status Response or Too Few Data Words");
}

// ---------------------------------------------------------------------------
// Timestamps
// ---------------------------------------------------------------------------

TEST_CASE("IRIG formats as DAY:HH:MM:SS.uuuuuu", "[models][timestamp][L2-DEC-014]") {
    const mie::IrigTimestamp t(10, 15, 54, 50, 456225, false);
    CHECK(t.format() == "10:15:54:50.456225");

    SECTION("fields are zero-padded but the day is not") {
        const mie::IrigTimestamp early(5, 1, 2, 3, 7, false);
        CHECK(early.format() == "5:01:02:03.000007");
    }

    SECTION("microseconds are always six digits") {
        const mie::IrigTimestamp zero(0, 0, 0, 0, 0, false);
        CHECK(zero.format() == "0:00:00:00.000000");
    }
}

TEST_CASE("an out-of-range microsecond cannot widen the field", "[models][timestamp]") {
    // Validation rejects microsecond >= 1000000 (L2-SYN-004), so this is
    // defensive. It matters because a seven-digit field would shift every
    // subsequent CSV column on that row -- corrupting the file rather than one
    // cell -- if a caller built the struct directly.
    const mie::IrigTimestamp t(1, 0, 0, 0, 1234567, false);
    const std::string s = t.format();
    CHECK(s == "1:00:00:00.234567");
    CHECK(s.size() == 17);
}

TEST_CASE("IRIG converts to absolute microseconds from the year start", "[models][timestamp]") {
    const mie::IrigTimestamp t(10, 15, 54, 50, 456225, false);
    // Every term is widened before multiplying. Casting only the first one
    // leaves `15 * 3600` and friends evaluated in int, which is what the
    // implementation must NOT do -- so an expectation computed that way would
    // agree with a buggy implementation on small inputs and disagree on large.
    const uint64_t seconds = static_cast<uint64_t>(10) * 86400u +
                             static_cast<uint64_t>(15) * 3600u + static_cast<uint64_t>(54) * 60u +
                             50u;
    const uint64_t expected = seconds * 1000000u + 456225u;
    CHECK(t.to_total_microseconds() == expected);

    SECTION("a late-year day does not overflow 32 bits") {
        // Day 365 alone is ~3.15e13 microseconds, well past 2^32. A 32-bit
        // intermediate would wrap and produce a timestamp earlier than the
        // start of the year.
        const mie::IrigTimestamp late(365, 23, 59, 59, 999999, false);
        CHECK(late.to_total_microseconds() > 31000000000000ull);
    }
}

TEST_CASE("Standard timestamps format as 0x-prefixed 8-digit hex", "[models][timestamp]") {
    const mie::StandardTimestamp t(0x0012ABCD, 0x0012, 0xABCD);
    CHECK(t.format() == "0x0012ABCD");
    CHECK(t.raw_ticks() == 0x0012ABCD);

    const mie::StandardTimestamp zero(0, 0, 0);
    CHECK(zero.format() == "0x00000000");
}

TEST_CASE("Standard ticks convert only with a usable rate", "[models][timestamp][L2-DEC-017]") {
    const mie::StandardTimestamp t(1000000, 0x000F, 0x4240);
    uint64_t micros = 0;

    SECTION("a 1 MHz counter yields one second") {
        REQUIRE(t.to_microseconds(1000000.0, micros));
        CHECK(micros == 1000000);
    }

    SECTION("an unusable rate is declined rather than guessed") {
        // A produced number here would look like real timing and would not be.
        // An empty DELTA column is the honest output.
        CHECK_FALSE(t.to_microseconds(0.0, micros));
        CHECK_FALSE(t.to_microseconds(-1.0, micros));
    }

    SECTION("rounding is half-away-from-zero, matching Python's int(x + 0.5)") {
        const mie::StandardTimestamp odd(3, 0, 3);
        REQUIRE(odd.to_microseconds(2000000.0, micros));
        // 3 ticks / 2 MHz = 1.5 us -> 2, not 1.
        CHECK(micros == 2);
    }
}

TEST_CASE("the Timestamp union dispatches on its discriminant", "[models][timestamp]") {
    const mie::Timestamp irig = mie::Timestamp::from_irig(mie::IrigTimestamp(1, 2, 3, 4, 5, false));
    CHECK(irig.is_irig());
    CHECK_FALSE(irig.is_standard());
    CHECK(irig.format() == "1:02:03:04.000005");

    const mie::Timestamp std_ts =
        mie::Timestamp::from_standard(mie::StandardTimestamp(0xFF, 0, 0xFF));
    CHECK(std_ts.is_standard());
    CHECK(std_ts.format() == "0x000000FF");

    SECTION("IRIG needs no tick rate; Standard does") {
        uint64_t micros = 0;
        CHECK(irig.to_microseconds(mie::none(), micros));

        CHECK_FALSE(std_ts.to_microseconds(mie::none(), micros));
        CHECK(std_ts.to_microseconds(mie::Optional<double>(1000000.0), micros));
    }

    SECTION("two formats are never equal even with identical raw bits") { CHECK(irig != std_ts); }
}

// ---------------------------------------------------------------------------
// DataWords
// ---------------------------------------------------------------------------

TEST_CASE("DataWords holds up to the bus-standard maximum", "[models][datawords]") {
    CHECK(mie::MAX_DATA_WORDS == 32);

    mie::DataWords w;
    CHECK(w.empty());
    for (std::size_t i = 0; i < mie::MAX_DATA_WORDS; ++i) {
        REQUIRE(w.try_push(static_cast<uint16_t>(i)));
    }
    CHECK(w.size() == 32);
    CHECK(w[0] == 0);
    CHECK(w[31] == 31);

    SECTION("a 33rd word is refused, not silently dropped or overflowing") {
        CHECK_FALSE(w.try_push(0xFFFF));
        CHECK(w.size() == 32);
        CHECK(w[31] == 31);
    }
}

TEST_CASE("DataWords truncates an over-long input rather than failing",
          "[models][datawords][L1-ROB-001]") {
    // Only reachable on corrupt input, since a conforming transaction cannot
    // exceed the cap. Staying total matters more than being strict there: the
    // reader's job on corrupt data is to keep going.
    uint16_t source[40];
    for (std::size_t i = 0; i < 40; ++i) {
        source[i] = static_cast<uint16_t>(i + 100);
    }
    const mie::DataWords w = mie::DataWords::from_words(source, 40);
    CHECK(w.size() == 32);
    CHECK(w[0] == 100);
    CHECK(w[31] == 131);
}

TEST_CASE("DataWords is range-for iterable over its live prefix", "[models][datawords]") {
    const uint16_t source[4] = {0x1111, 0x2222, 0x3333, 0x4444};
    const mie::DataWords w = mie::DataWords::from_words(source, 3);

    std::vector<uint16_t> seen;
    for (const uint16_t word : w) {
        // The raw loop is the point of the test: std::copy would exercise
        // begin()/end() without proving the range-for syntax itself compiles.
        // cppcheck-suppress useStlAlgorithm
        seen.push_back(word);
    }

    // Three, not four: the fourth word is past the live prefix.
    REQUIRE(seen.size() == 3u);
    CHECK(seen[0] == 0x1111);
    CHECK(seen[1] == 0x2222);
    CHECK(seen[2] == 0x3333);

    const mie::DataWords empty;
    CHECK(empty.begin() == empty.end());
}

TEST_CASE("DataWords equality compares only the live prefix", "[models][datawords]") {
    const uint16_t a[] = {1, 2, 3};
    const uint16_t b[] = {1, 2, 3, 4};

    const mie::DataWords wa = mie::DataWords::from_words(a, 3);
    const mie::DataWords wb = mie::DataWords::from_words(b, 4);
    CHECK(wa != wb);
    CHECK(wa == mie::DataWords::from_words(a, 3));

    SECTION("a pushed-then-cleared buffer equals a fresh one") {
        // The tail is zeroed at construction, so a whole-buffer memcmp would
        // agree here today. It would stop agreeing the moment the tail held
        // stale words, which is the latent difference this pins.
        mie::DataWords dirty;
        REQUIRE(dirty.try_push(0xDEAD));
        REQUIRE(dirty.try_push(0xBEEF));
        dirty.clear();
        CHECK(dirty == mie::DataWords());
        CHECK(dirty.empty());
    }

    SECTION("an empty buffer has no words") {
        const mie::DataWords empty = mie::DataWords::from_words(a, 0);
        CHECK(empty.empty());
        CHECK(empty == mie::DataWords());
    }
}

// ---------------------------------------------------------------------------
// MieMessage
// ---------------------------------------------------------------------------

namespace {

/// A minimal well-formed message, so each test below varies one thing.
mie::MieMessage sample_message() {
    mie::MieMessage m;
    m.timestamp = mie::Timestamp::from_irig(mie::IrigTimestamp(10, 15, 54, 50, 456225, false));
    m.type_word = mie::TypeWord(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 36, false, 0x2402);
    m.message_format = mie::FORMAT_RECEIVE;
    m.command_word = mie::CommandWord(15, mie::DIRECTION_RECEIVE, 11, 30, 0x797E);
    return m;
}

}  // namespace

TEST_CASE("MSG label is subaddress plus direction letter", "[models][message]") {
    mie::MieMessage m = sample_message();
    CHECK(m.msg_label() == "11R");

    m.command_word = mie::CommandWord(15, mie::DIRECTION_TRANSMIT, 11, 30, 0x797E);
    CHECK(m.msg_label() == "11T");

    SECTION("a record with no Command Word has no label") {
        // SPURIOUS_DATA carries no Command Word, so the MSG cell is empty --
        // and the record is pinned rather than sorted by the ordering stage.
        m.command_word = mie::none();
        CHECK(m.msg_label().empty());
        CHECK(m.delta_key().empty());
        CHECK_FALSE(m.rt().has_value());
        CHECK_FALSE(m.subaddress().has_value());
    }
}

TEST_CASE("the DELTA key distinguishes RT, subaddress and direction", "[models][message]") {
    const mie::MieMessage m = sample_message();
    CHECK(m.delta_key() == "15:11R");

    mie::MieMessage other = sample_message();
    other.command_word = mie::CommandWord(15, mie::DIRECTION_TRANSMIT, 11, 30, 0x797E);
    // Same RT and subaddress, opposite direction: a distinct stream, so a
    // distinct key. Collapsing these would report a gap between a receive and
    // a transmit as though they were the same message.
    CHECK(other.delta_key() != m.delta_key());
}

TEST_CASE("the ERROR column distinguishes errored from spurious", "[models][message]") {
    mie::MieMessage m = sample_message();
    CHECK(std::string(m.error_label()).empty());
    CHECK_FALSE(m.is_error());
    CHECK_FALSE(m.is_spurious());

    SECTION("Type Word bit 14 makes it an error") {
        m.type_word.error = true;
        CHECK(m.is_error());
        CHECK(std::string(m.error_label()) == "ERROR");
    }

    SECTION("a spurious record without the error bit reads as SPURIOUS") {
        m.message_format = mie::FORMAT_SPURIOUS_DATA;
        CHECK(m.is_spurious());
        CHECK(std::string(m.error_label()) == "SPURIOUS");
    }

    SECTION("the message type wins over the error bit") {
        // A SPURIOUS_DATA record with bit 14 set is decoded, coded
        // (0x2000/0x2001) and timed as spurious, and an ERROR row promises a
        // DDC code read from an Error Word, which it does not carry -- so it
        // reads SPURIOUS (docs/DATAFLOW.md row S7). The raw bit stays visible.
        m.message_format = mie::FORMAT_SPURIOUS_DATA;
        m.type_word.error = true;
        CHECK(m.is_error());
        CHECK(std::string(m.error_label()) == "SPURIOUS");
    }
}

TEST_CASE("Command Word predicates match the bus standard", "[models][message]") {
    const mie::CommandWord broadcast(31, mie::DIRECTION_RECEIVE, 5, 1, 0);
    CHECK(broadcast.is_broadcast());
    CHECK_FALSE(broadcast.is_mode_code());

    const mie::CommandWord mode_low(3, mie::DIRECTION_TRANSMIT, 0, 1, 0);
    const mie::CommandWord mode_high(3, mie::DIRECTION_TRANSMIT, 31, 1, 0);
    CHECK(mode_low.is_mode_code());
    CHECK(mode_high.is_mode_code());

    const mie::CommandWord ordinary(3, mie::DIRECTION_TRANSMIT, 11, 1, 0);
    CHECK_FALSE(ordinary.is_mode_code());
    CHECK_FALSE(ordinary.is_broadcast());
}

TEST_CASE("MUX is shared rather than copied per record", "[models][message][L2-WRT-020]") {
    // One string per input file, not one per record. With a merge of many
    // large inputs, per-record copies would make resident memory scale with
    // the record count -- the opposite of the O(1) design point.
    mie::MieMessage a = sample_message();
    mie::MieMessage b = sample_message();
    const std::shared_ptr<const std::string> mux(new std::string("flight01"));
    a.mux = mux;
    b.mux = mux;

    REQUIRE(a.mux);
    CHECK(*a.mux == "flight01");
    CHECK(a.mux.get() == b.mux.get());

    SECTION("absent by default, which renders as an empty column") {
        const mie::MieMessage fresh;
        CHECK_FALSE(fresh.mux);
    }
}

TEST_CASE("DELTA absence is distinct from a DELTA of zero", "[models][message]") {
    // The distinction is the reason delta is an Optional. Absent means "no
    // meaningful gap" -- SPURIOUS_DATA, an uncalibrated Standard timestamp, a
    // clock that stepped backwards -- and renders as an empty cell. Zero means
    // "first occurrence of this key", and renders as 0.000000.
    mie::MieMessage m = sample_message();
    CHECK_FALSE(m.delta.has_value());

    m.delta = 0.0;
    REQUIRE(m.delta.has_value());
    CHECK(m.delta.value() == 0.0);

    m.delta.reset();
    CHECK_FALSE(m.delta.has_value());
}
