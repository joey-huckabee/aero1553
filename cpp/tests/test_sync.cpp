// SPDX-License-Identifier: Apache-2.0
//
// Tests for record alignment, validation and sync recovery.
//
// The subtle behaviours here are the ones a reviewer is least likely to
// reconstruct from the code, so each has its own case with the reason stated:
//
//   * the terminator is honoured on trusted boundaries, and during recovery
//     only as the last word of the file
//   * an unknown timestamp format uses the permissive (Standard) floor
//   * freerun suppresses the day-range check and nothing else
//   * the look-ahead advances by each record's DECLARED length, not by 2 bytes
//   * EOF ends a look-ahead walk without rejecting the candidate

#include "mie/sync.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <vector>

#include "mie/decode.hpp"
#include "record_fixtures.hpp"

namespace {

namespace sy = mie::sync;

// The wire-format primitives are shared with the other suites that build
// records; the helpers below are the ones only this suite needs.
using mie_test::le_bytes;
using mie_test::type_word;

/// A type code that is not one of the known ones.
///
/// 0x7F, not 0x99. Both are unknown codes, but 0x99 has BIT 7 set -- and bit 7
/// of a Type Word is the BUS, not part of the type. The local builder this
/// suite used to carry masked nothing, so 0x99 quietly produced "unknown type,
/// bus B" while the test said only "unknown type". The shared builder takes the
/// bus as its own argument and masks the type to seven bits, which is correct
/// and which is why the value had to change with it.
const uint8_t kUnknownMessageType = 0x7F;

/// The three IRIG timestamp words for a valid in-range time.
void push_valid_irig(std::vector<uint16_t>& words) {
    words.push_back(static_cast<uint16_t>((10 << 5) | 15));          // day 10, hour 15
    words.push_back(static_cast<uint16_t>((54 << 10) | (50 << 4)));  // minute 54, second 50
    words.push_back(0x0000);                                         // microsecond low
}

/// One well-formed IRIG BC-to-RT record of `word_count` words.
std::vector<uint16_t> irig_record(uint16_t word_count) {
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, word_count));
    push_valid_irig(words);
    while (words.size() < word_count) {
        words.push_back(0x1234);
    }
    return words;
}

std::vector<uint16_t> concat(const std::vector<uint16_t>& a, const std::vector<uint16_t>& b) {
    std::vector<uint16_t> out(a);
    out.insert(out.end(), b.begin(), b.end());
    return out;
}

// Functions rather than namespace-scope constants. An object with static
// storage duration whose constructor could throw is a cert-err58-cpp finding:
// the exception would escape before main() and could not be caught. These
// particular constructions cannot throw, but clang-tidy cannot prove that
// through Optional's non-noexcept constructor, and returning by value sidesteps
// the question entirely for the cost of a few parentheses.
mie::Optional<mie::TimestampFormat> irig_fmt() {
    return mie::Optional<mie::TimestampFormat>(mie::TIMESTAMP_IRIG);
}
mie::Optional<mie::TimestampFormat> standard_fmt() {
    return mie::Optional<mie::TimestampFormat>(mie::TIMESTAMP_STANDARD);
}
/// "Format not yet determined" -- which selects the permissive Standard floor.
mie::Optional<mie::TimestampFormat> unknown_fmt() { return mie::Optional<mie::TimestampFormat>(); }

}  // namespace

// ---------------------------------------------------------------------------
// The validation checks, in order
// ---------------------------------------------------------------------------

TEST_CASE("a well-formed record validates", "[sync][L3-CPP-004]") {
    const std::vector<uint8_t> data = le_bytes(concat(irig_record(8), irig_record(8)));
    CHECK(sy::validate_record(&data[0], data.size(), 0, data.size(), irig_fmt(), 2));
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 2) ==
          sy::VALIDATION_OK);
}

TEST_CASE("an unreadable Type Word is reported as such", "[sync]") {
    const std::vector<uint8_t> data(1, 0x02);
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 2) ==
          sy::VALIDATION_TYPE_WORD_UNREADABLE);
}

TEST_CASE("an unknown message type is rejected before anything else", "[sync][L2-SYN-001]") {
    std::vector<uint16_t> words = irig_record(8);
    words[0] = type_word(kUnknownMessageType, 8);
    const std::vector<uint8_t> data = le_bytes(words);
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 1) ==
          sy::VALIDATION_UNKNOWN_MESSAGE_TYPE);
}

TEST_CASE("the word-count floor depends on the timestamp format", "[sync][L2-SYN-002]") {
    // IRIG needs Type + 3 timestamp + Cmd = 5 words; Standard needs 4. A record
    // of exactly 4 words is therefore valid under Standard and too short under
    // IRIG -- which is why using the wrong floor rejects every record in a file.
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 4));
    words.push_back(0x0000);
    words.push_back(0x0000);
    words.push_back(0x0000);
    const std::vector<uint8_t> data = le_bytes(words);

    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 1) ==
          sy::VALIDATION_INVALID_WORD_COUNT);
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), standard_fmt(), 1) ==
          sy::VALIDATION_OK);

    SECTION("an unknown format uses the permissive floor") {
        // The format has not been probed yet at this point. Using the IRIG
        // floor would reject every record of a Standard-format file before the
        // probe ever ran.
        CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), unknown_fmt(),
                                           1) == sy::VALIDATION_OK);
    }
}

TEST_CASE("a record extending past the end of the file is rejected", "[sync][L2-RDR-002]") {
    // Declares 20 words but only 8 are present.
    std::vector<uint16_t> words = irig_record(8);
    words[0] = type_word(mie::MESSAGE_TYPE_BC_TO_RT, 20);
    const std::vector<uint8_t> data = le_bytes(words);
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 1) ==
          sy::VALIDATION_RECORD_TRUNCATED);
}

// ---------------------------------------------------------------------------
// IRIG field ranges (L2-SYN-004 / L2-SYN-019)
// ---------------------------------------------------------------------------

namespace {

/// The IRIG upper word: freerun in bit 15, day in bits 5-13, hour in bits 0-4.
///
/// A builder rather than a hand-written shift at each call site. Writing
/// `(0 << 5) | 15` to show the layout is a redundant bitmask that cppcheck
/// rightly objects to, and spelling the layout once is clearer than spelling it
/// eleven times anyway.
///
/// Bit 14 is deliberately absent: it is reserved and belongs to no field.
uint16_t irig_upper(uint32_t day, uint32_t hour, bool freerun = false) {
    uint16_t v = static_cast<uint16_t>(hour & 0x1F);
    v = static_cast<uint16_t>(v | ((day & 0x1FF) << 5));
    if (freerun) {
        v = static_cast<uint16_t>(v | 0x8000);
    }
    return v;
}

/// The IRIG middle word: minute in bits 10-15, second in bits 4-9, and the
/// microsecond's high nibble in bits 0-3.
uint16_t irig_middle(uint32_t minute, uint32_t second, uint32_t microsecond = 0) {
    uint16_t v = static_cast<uint16_t>((microsecond >> 16) & 0xF);
    v = static_cast<uint16_t>(v | ((second & 0x3F) << 4));
    v = static_cast<uint16_t>(v | ((minute & 0x3F) << 10));
    return v;
}

/// An eight-word IRIG record with the timestamp words replaced wholesale, so a
/// test can put any field value on the wire regardless of whether it is legal.
std::vector<uint8_t> irig_with_timestamp(uint16_t upper, uint16_t middle, uint16_t lower) {
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
    words.push_back(upper);
    words.push_back(middle);
    words.push_back(lower);
    for (int i = 0; i < 4; ++i) {
        words.push_back(0x1234);
    }
    return le_bytes(words);
}

}  // namespace

TEST_CASE("each IRIG field range is checked separately", "[sync][L2-SYN-004]") {
    // Distinct failures rather than one generic reject, so a DEBUG line can
    // tell an operator which field was wrong.
    SECTION("hour") {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(10, 24), irig_middle(54, 50), 0);
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_IRIG_HOUR_OUT_OF_RANGE);
    }
    SECTION("minute") {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(10, 15), irig_middle(60, 50), 0);
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_IRIG_MINUTE_OUT_OF_RANGE);
    }
    SECTION("second") {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(10, 15), irig_middle(54, 60), 0);
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_IRIG_SECOND_OUT_OF_RANGE);
    }
    SECTION("microsecond") {
        // 1000000 needs the middle word's low nibble as well as the low word.
        // One past the legal maximum. The value spans both words, so it needs
        // the middle word's low nibble as well as the whole lower word -- a
        // decoder reading only the lower word would see 16960 and accept it.
        const uint32_t us = 1000000;
        const std::vector<uint8_t> d = irig_with_timestamp(
            irig_upper(10, 15), irig_middle(54, 50, us), static_cast<uint16_t>(us & 0xFFFF));
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_IRIG_MICROSECOND_OUT_OF_RANGE);
    }
    SECTION("day") {
        // Day 0. Encodable, but outside the calendar range 1..366.
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(0, 15), irig_middle(54, 50), 0);
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_IRIG_DAY_OUT_OF_RANGE);
    }
}

TEST_CASE("freerun suppresses the day check and nothing else", "[sync][L2-SYN-019]") {
    // A free-running oscillator is not calendar-locked, so its day field may
    // carry any value. Hour/minute/second/microsecond are a function of the
    // counter modulus and stay in range regardless -- suppressing those too
    // would let genuinely corrupt records through.

    SECTION("an out-of-range day is accepted when freerun is set") {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(500, 15, true), irig_middle(54, 50), 0);
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_OK);
    }

    SECTION("an out-of-range hour is still rejected when freerun is set") {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(500, 24, true), irig_middle(54, 50), 0);
        CHECK(sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
              sy::VALIDATION_IRIG_HOUR_OUT_OF_RANGE);
    }
}

TEST_CASE("every valid day is accepted and every invalid one rejected",
          "[sync][exhaustive][L2-SYN-004]") {
    // The nine-bit day field, swept in full. Days 1..366 are the calendar
    // range; 0 and 367..511 are encodable and must be rejected.
    for (uint32_t day = 0; day <= 511; ++day) {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(day, 15), irig_middle(54, 50), 0);
        const sy::ValidationFailure got =
            sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1);
        const bool want_ok = day >= 1 && day <= 366;
        if ((got == sy::VALIDATION_OK) != want_ok) {
            INFO("day = " << day << " got " << sy::validation_failure_text(got));
            REQUIRE((got == sy::VALIDATION_OK) == want_ok);
        }
    }
    SUCCEED("all 512 encodable day values classify correctly");
}

TEST_CASE("every hour, minute and second boundary is checked", "[sync][exhaustive][L2-SYN-004]") {
    for (uint32_t hour = 0; hour <= 31; ++hour) {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(10, hour), irig_middle(54, 50), 0);
        const bool ok = sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
                        sy::VALIDATION_OK;
        if (ok != (hour < 24)) {
            INFO("hour = " << hour);
            REQUIRE(ok == (hour < 24));
        }
    }
    for (uint32_t minute = 0; minute <= 63; ++minute) {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(10, 15), irig_middle(minute, 50), 0);
        const bool ok = sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
                        sy::VALIDATION_OK;
        if (ok != (minute < 60)) {
            INFO("minute = " << minute);
            REQUIRE(ok == (minute < 60));
        }
    }
    for (uint32_t second = 0; second <= 63; ++second) {
        const std::vector<uint8_t> d =
            irig_with_timestamp(irig_upper(10, 15), irig_middle(54, second), 0);
        const bool ok = sy::validate_record_detailed(&d[0], d.size(), 0, d.size(), irig_fmt(), 1) ==
                        sy::VALIDATION_OK;
        if (ok != (second < 60)) {
            INFO("second = " << second);
            REQUIRE(ok == (second < 60));
        }
    }
    SUCCEED("every encodable hour, minute and second classifies correctly");
}

TEST_CASE("IRIG range checks are skipped when the words are not all readable", "[sync]") {
    // Near EOF the caller's in-bounds checks are authoritative. Failing here
    // would reject a record for a reason that is really "the file ended".
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 5));
    words.push_back(static_cast<uint16_t>((10 << 5) | 15));
    const std::vector<uint8_t> data = le_bytes(words);
    // Declared 5 words but only 2 present, so this fails on length, not range.
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 1) ==
          sy::VALIDATION_RECORD_TRUNCATED);
}

// ---------------------------------------------------------------------------
// Look-ahead (L2-SYN-005 / L2-SYN-026 / L2-SYN-028)
// ---------------------------------------------------------------------------

TEST_CASE("look-ahead rejects a candidate whose follower is malformed", "[sync][L2-SYN-005]") {
    const std::vector<uint16_t> good = irig_record(8);
    std::vector<uint16_t> bad = irig_record(8);
    bad[0] = type_word(kUnknownMessageType, 8);  // unknown type
    const std::vector<uint8_t> data = le_bytes(concat(good, bad));

    // Depth 1 checks only the candidate, so it passes.
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 1) ==
          sy::VALIDATION_OK);
    // Depth 2 checks the follower, which is malformed.
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 2) ==
          sy::VALIDATION_LOOKAHEAD_UNKNOWN_MESSAGE_TYPE);
}

TEST_CASE("look-ahead advances by each record's declared length", "[sync][L2-SYN-026]") {
    // THE thing this must get right. Advancing two bytes at a time would land
    // the second check inside the first follower's payload, where arbitrary
    // data would be judged as a Type Word.
    //
    // Three records of different lengths: if the walk advances correctly it
    // reaches all three; if it advances by 2 it lands mid-payload on data
    // words that are not valid Type Words.
    std::vector<uint16_t> words = irig_record(6);
    const std::vector<uint16_t> second = irig_record(10);
    const std::vector<uint16_t> third = irig_record(8);
    words = concat(concat(words, second), third);
    const std::vector<uint8_t> data = le_bytes(words);

    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 3) ==
          sy::VALIDATION_OK);
}

TEST_CASE("EOF ends the look-ahead walk without rejecting the candidate", "[sync][L2-SYN-005]") {
    // The records that would have been checked do not exist. That is not a
    // reason to discard a well-formed record -- the last record of every file
    // would be dropped.
    const std::vector<uint8_t> data = le_bytes(irig_record(8));
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 8) ==
          sy::VALIDATION_OK);
}

TEST_CASE("a null Type Word confirms the last record on a trusted boundary",
          "[sync][L2-SYN-028][L2-RDR-021]") {
    // MIE files end with a 0x0000 Type Word. Treating it as an invalid follower
    // would drop the last real record of every recording.
    std::vector<uint16_t> words = irig_record(8);
    words.push_back(0x0000);
    const std::vector<uint8_t> data = le_bytes(words);
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 0, data.size(), irig_fmt(), 2) ==
          sy::VALIDATION_OK);
}

TEST_CASE("recovery honours the terminator as the last word of the file",
          "[sync][L2-SYN-011][L2-SYN-028]") {
    // Every DDC recording ends "...record, 0x0000". When recovery lands on the
    // final record, its follower is that terminator; rejecting it dropped the
    // record from every such file, while the same file without the terminator
    // kept it. A zero in the final two bytes ends the candidate exactly where
    // EOF would, and EOF already confirms.
    //
    //   [0] 0xFFFF                  bytes 0-1    garbage, forces recovery
    //   [1] Type(BC_TO_RT, wc=5)    bytes 2-3    the final record
    //   [2..4] IRIG timestamp       bytes 4-9
    //   [5] one payload word        bytes 10-11
    //   [6] 0x0000                  bytes 12-13  the terminator, then EOF
    std::vector<uint16_t> words;
    words.push_back(0xFFFF);
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 5));
    push_valid_irig(words);
    words.push_back(0x1234);
    words.push_back(0x0000);

    const std::vector<uint8_t> data = le_bytes(words);
    sy::ScanHit hit;
    REQUIRE(sy::recover_sync(&data[0], data.size(), 0, data.size(), irig_fmt(), sy::MAX_SCAN_BYTES,
                             2, hit));
    CHECK(hit.offset == 2);
}

TEST_CASE("recovery does NOT honour a zero word mid-file", "[sync][L2-SYN-011][L2-SYN-028]") {
    // The asymmetry that matters. Recovery probes arbitrary un-aligned offsets,
    // so a mis-aligned candidate whose declared length happens to land its
    // boundary on a stray zero DATA word must not validate as a bogus "last
    // record before the terminator". It has to find a real follower, or EOF.
    //
    // Built so that a candidate at offset 2 would have a zero word at its
    // boundary while real data continues afterwards.
    // Word layout, and the arithmetic matters -- getting it wrong puts the zero
    // inside the record instead of at its boundary, which tests nothing:
    //
    //   [0] 0xFFFF                  bytes 0-1    garbage, forces recovery
    //   [1] Type(BC_TO_RT, wc=5)    bytes 2-3    the candidate
    //   [2..4] IRIG timestamp       bytes 4-9    |  5 words = 10 bytes,
    //   [5] one payload word        bytes 10-11  |  so the record is 2..11
    //   [6] 0x0000                  bytes 12-13  the candidate's BOUNDARY
    //   [7] 0xFFFF                  bytes 14-15  ...and the file continues
    std::vector<uint16_t> words;
    words.push_back(0xFFFF);
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 5));
    push_valid_irig(words);
    words.push_back(0x1234);  // fifth word of the record
    words.push_back(0x0000);  // zero word exactly at the boundary
    words.push_back(0xFFFF);  // the file continues, so this is not really the end

    const std::vector<uint8_t> data = le_bytes(words);

    // On the trusted path the zero would confirm the candidate.
    CHECK(sy::validate_record_detailed(&data[0], data.size(), 2, data.size(), irig_fmt(), 2) ==
          sy::VALIDATION_OK);

    // Recovery from offset 0 must NOT accept that same candidate at offset 2,
    // because the zero is a data word rather than a real terminator.
    sy::ScanHit hit;
    const bool found = sy::recover_sync(&data[0], data.size(), 0, data.size(), irig_fmt(),
                                        sy::MAX_SCAN_BYTES, 2, hit);
    if (found) {
        CHECK(hit.offset != 2);
    }
}

// ---------------------------------------------------------------------------
// find_first_record
// ---------------------------------------------------------------------------

TEST_CASE("a file with no header validates at offset zero", "[sync][L2-SYN-003]") {
    // MIE files have no header: records start at byte 0. The scan returns
    // immediately in that case rather than walking the whole window.
    const std::vector<uint8_t> data = le_bytes(concat(irig_record(8), irig_record(8)));
    sy::ScanHit hit;
    REQUIRE(sy::find_first_record(&data[0], data.size(), data.size(), irig_fmt(),
                                  sy::MAX_SCAN_BYTES, 2, hit));
    CHECK(hit.offset == 0);
    CHECK(hit.skipped == 0);
}

TEST_CASE("leading garbage is skipped and reported", "[sync][L2-SYN-003]") {
    std::vector<uint16_t> words;
    words.push_back(0xFFFF);
    words.push_back(0xFFFF);
    const std::vector<uint16_t> records = concat(irig_record(8), irig_record(8));
    words.insert(words.end(), records.begin(), records.end());
    const std::vector<uint8_t> data = le_bytes(words);

    sy::ScanHit hit;
    REQUIRE(sy::find_first_record(&data[0], data.size(), data.size(), irig_fmt(),
                                  sy::MAX_SCAN_BYTES, 2, hit));
    CHECK(hit.offset == 4);
    CHECK(hit.skipped == 4);
}

TEST_CASE("finding nothing is a result, not an error", "[sync]") {
    // The EXPECTED outcome for a valid empty recording. The reader decides what
    // that means; this function does not log and does not raise.
    const std::vector<uint8_t> data(64, 0xFF);
    sy::ScanHit hit;
    CHECK_FALSE(sy::find_first_record(&data[0], data.size(), data.size(), irig_fmt(),
                                      sy::MAX_SCAN_BYTES, 2, hit));
}

TEST_CASE("the scan window bounds the search", "[sync]") {
    // A 64 KB cap keeps a runaway scan from walking a multi-gigabyte file.
    std::vector<uint16_t> words(64, 0xFFFF);
    const std::vector<uint16_t> rec = concat(irig_record(8), irig_record(8));
    words.insert(words.end(), rec.begin(), rec.end());
    const std::vector<uint8_t> data = le_bytes(words);

    sy::ScanHit hit;
    // A window too small to reach the record finds nothing...
    CHECK_FALSE(sy::find_first_record(&data[0], data.size(), data.size(), irig_fmt(), 16, 2, hit));
    // ...and the full window finds it.
    CHECK(sy::find_first_record(&data[0], data.size(), data.size(), irig_fmt(), sy::MAX_SCAN_BYTES,
                                2, hit));
}

// ---------------------------------------------------------------------------
// Homogeneous-payload defence (L2-SYN-018)
// ---------------------------------------------------------------------------

TEST_CASE("a single-byte pad is detected as homogeneous", "[sync][L2-SYN-018]") {
    // THE case this exists for: 0x20-fill, where `0x20 0x20` parses as a valid
    // SPURIOUS_DATA Type Word and look-ahead alone admits the whole stream.
    const std::vector<uint8_t> data(256, 0x20);
    CHECK(sy::is_homogeneous_payload(&data[0], data.size(), 0, 16));
}

TEST_CASE("real records differing outside the timestamp are not homogeneous",
          "[sync][L2-SYN-018]") {
    std::vector<uint16_t> words;
    for (std::size_t i = 0; i < sy::HOMOGENEITY_SAMPLE_RECORDS; ++i) {
        std::vector<uint16_t> rec = irig_record(8);
        rec[5] = static_cast<uint16_t>(0x1000 + i);  // payload differs per record
        words = concat(words, rec);
    }
    const std::vector<uint8_t> data = le_bytes(words);
    CHECK_FALSE(sy::is_homogeneous_payload(&data[0], data.size(), 0, 16));
}

TEST_CASE("records differing ONLY in the timestamp are homogeneous", "[sync][L2-SYN-018]") {
    // The timestamp triple is deliberately ignored: real records differ there
    // and a fill pattern does not, so comparing it would defeat the check.
    std::vector<uint16_t> words;
    for (std::size_t i = 0; i < sy::HOMOGENEITY_SAMPLE_RECORDS; ++i) {
        std::vector<uint16_t> rec = irig_record(8);
        rec[3] = static_cast<uint16_t>(i);  // microsecond low word only
        words = concat(words, rec);
    }
    const std::vector<uint8_t> data = le_bytes(words);
    CHECK(sy::is_homogeneous_payload(&data[0], data.size(), 0, 16));
}

TEST_CASE("too few records to sample is not a rejection", "[sync][L2-SYN-018]") {
    // A short file cannot be judged; saying "not homogeneous" lets it proceed
    // to the normal checks rather than rejecting it for being small.
    const std::vector<uint8_t> data(16, 0x20);
    CHECK_FALSE(sy::is_homogeneous_payload(&data[0], data.size(), 0, 16));
}

TEST_CASE("a zero record size cannot divide by zero or loop", "[sync][L1-ROB-001]") {
    const std::vector<uint8_t> data(64, 0x20);
    CHECK_FALSE(sy::is_homogeneous_payload(&data[0], data.size(), 0, 0));
}

// ---------------------------------------------------------------------------
// diagnose_header_scan_failure (L2-RDR-004)
// ---------------------------------------------------------------------------

TEST_CASE("a truncated first record is distinguished from a non-MIE file", "[sync][L2-RDR-004]") {
    // The distinction matters to an operator: one means the recording was cut
    // short, the other means they passed the wrong file.
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 40));  // declares 80 bytes
    push_valid_irig(words);
    const std::vector<uint8_t> data = le_bytes(words);  // only 8 bytes present

    std::size_t offset = 0;
    std::size_t record_bytes = 0;
    std::size_t available = 0;
    REQUIRE(sy::diagnose_header_scan_failure(&data[0], data.size(), data.size(), irig_fmt(),
                                             sy::MAX_SCAN_BYTES, offset, record_bytes, available));
    CHECK(offset == 0);
    CHECK(record_bytes == 80);
    CHECK(available == 8);
}

TEST_CASE("a file with no plausible Type Word yields no length diagnosis", "[sync][L2-RDR-004]") {
    const std::vector<uint8_t> data(64, 0xFF);
    std::size_t offset = 0;
    std::size_t record_bytes = 0;
    std::size_t available = 0;
    CHECK_FALSE(sy::diagnose_header_scan_failure(&data[0], data.size(), data.size(), irig_fmt(),
                                                 sy::MAX_SCAN_BYTES, offset, record_bytes,
                                                 available));
}

// ---------------------------------------------------------------------------
// recover_sync
// ---------------------------------------------------------------------------

TEST_CASE("recovery finds the next valid record after corruption", "[sync][L2-SYN-011]") {
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
    push_valid_irig(words);
    words.push_back(0xFFFF);
    words.push_back(0xFFFF);
    words.push_back(0xFFFF);
    words.push_back(0xFFFF);
    const std::vector<uint16_t> good = concat(irig_record(8), irig_record(8));
    words = concat(words, good);
    const std::vector<uint8_t> data = le_bytes(words);

    sy::ScanHit hit;
    REQUIRE(sy::recover_sync(&data[0], data.size(), 0, data.size(), irig_fmt(), sy::MAX_SCAN_BYTES,
                             2, hit));
    CHECK(hit.offset == 16);
    CHECK(hit.skipped == 16);
}

TEST_CASE("recovery starts past the failing offset, never at it", "[sync]") {
    // Returning the same offset would loop forever: the reader would re-decode
    // the record that just failed.
    const std::vector<uint8_t> data = le_bytes(concat(irig_record(8), irig_record(8)));
    sy::ScanHit hit;
    if (sy::recover_sync(&data[0], data.size(), 0, data.size(), irig_fmt(), sy::MAX_SCAN_BYTES, 2,
                         hit)) {
        CHECK(hit.offset >= 2);
    }
}

TEST_CASE("recovery gives up at the scan window", "[sync][L2-SYN-011]") {
    const std::vector<uint8_t> data(4096, 0xFF);
    sy::ScanHit hit;
    CHECK_FALSE(sy::recover_sync(&data[0], data.size(), 0, data.size(), irig_fmt(), 64, 2, hit));
}

TEST_CASE("every validation failure has distinct, non-empty text", "[sync]") {
    // These reach DEBUG output; a duplicated or empty phrase makes a diagnostic
    // ambiguous exactly when someone is reading it under pressure.
    const sy::ValidationFailure all[] = {sy::VALIDATION_OK,
                                         sy::VALIDATION_TYPE_WORD_UNREADABLE,
                                         sy::VALIDATION_UNKNOWN_MESSAGE_TYPE,
                                         sy::VALIDATION_INVALID_WORD_COUNT,
                                         sy::VALIDATION_RECORD_TRUNCATED,
                                         sy::VALIDATION_IRIG_HOUR_OUT_OF_RANGE,
                                         sy::VALIDATION_IRIG_MINUTE_OUT_OF_RANGE,
                                         sy::VALIDATION_IRIG_SECOND_OUT_OF_RANGE,
                                         sy::VALIDATION_IRIG_MICROSECOND_OUT_OF_RANGE,
                                         sy::VALIDATION_IRIG_DAY_OUT_OF_RANGE,
                                         sy::VALIDATION_LOOKAHEAD_UNKNOWN_MESSAGE_TYPE,
                                         sy::VALIDATION_LOOKAHEAD_INVALID_WORD_COUNT};
    const std::size_t count = sizeof(all) / sizeof(all[0]);
    for (std::size_t i = 0; i < count; ++i) {
        const std::string text = sy::validation_failure_text(all[i]);
        CHECK_FALSE(text.empty());
        for (std::size_t j = i + 1; j < count; ++j) {
            CHECK(text != std::string(sy::validation_failure_text(all[j])));
        }
    }
}
