// SPDX-License-Identifier: Apache-2.0
//
// EXHAUSTIVE verification of the model types' formatting and arithmetic.
//
// Where test_models.cpp checks the values a human thought to check, this sweeps
// each field's full encoded range and compares against an INDEPENDENT oracle --
// snprintf for the formatters, 64-bit arithmetic written a different way for
// the conversions. An independent oracle matters: comparing a hand-rolled
// formatter against itself proves only that it is self-consistent.
//
// The heavy sweeps assert only on mismatch rather than once per iteration. A
// million passing CHECKs would make the suite slow and its assertion count
// meaningless; a single REQUIRE on the first divergence, with the input in the
// message, is what a failure actually needs.

#include "mie/models.hpp"
#include "mie/text.hpp"

#include <catch2/catch.hpp>

#include <algorithm>
#include <cstdio>
#include <limits>
#include <string>
#include <vector>

namespace {

/// snprintf-based oracle. Independent of the hand-rolled formatters under test
/// -- a different implementation, not a different spelling of the same one.
std::string sprintf_fmt(const char* format, unsigned long long value) {
    char buffer[64];
    const int n = std::snprintf(buffer, sizeof(buffer), format, value);
    REQUIRE(n > 0);
    REQUIRE(static_cast<std::size_t>(n) < sizeof(buffer));
    return std::string(buffer, static_cast<std::size_t>(n));
}

/// snprintf into a fixed buffer with the result CHECKED, for the multi-field
/// formats. Truncation here would be worse than a failing test: it would make
/// the oracle itself wrong, silently weakening every comparison that uses it.
std::string irig_oracle(unsigned day, unsigned hour, unsigned minute, unsigned second,
                        unsigned microsecond) {
    char buffer[64];
    const int n = std::snprintf(buffer, sizeof(buffer), "%u:%02u:%02u:%02u.%06u", day, hour, minute,
                                second, microsecond);
    REQUIRE(n > 0);
    REQUIRE(static_cast<std::size_t>(n) < sizeof(buffer));
    return std::string(buffer, static_cast<std::size_t>(n));
}

std::string standard_oracle(uint32_t raw) {
    char buffer[32];
    const int n = std::snprintf(buffer, sizeof(buffer), "0x%08X", raw);
    REQUIRE(n > 0);
    REQUIRE(static_cast<std::size_t>(n) < sizeof(buffer));
    return std::string(buffer, static_cast<std::size_t>(n));
}

std::string msg_oracle(unsigned subaddress, char suffix) {
    char buffer[16];
    const int n = std::snprintf(buffer, sizeof(buffer), "%u%c", subaddress, suffix);
    REQUIRE(n > 0);
    REQUIRE(static_cast<std::size_t>(n) < sizeof(buffer));
    return std::string(buffer, static_cast<std::size_t>(n));
}

std::string key_oracle(unsigned rt, unsigned subaddress, char suffix) {
    char buffer[24];
    const int n = std::snprintf(buffer, sizeof(buffer), "%u:%u%c", rt, subaddress, suffix);
    REQUIRE(n > 0);
    REQUIRE(static_cast<std::size_t>(n) < sizeof(buffer));
    return std::string(buffer, static_cast<std::size_t>(n));
}

}  // namespace

// ---------------------------------------------------------------------------
// text: integer formatting against snprintf
// ---------------------------------------------------------------------------

TEST_CASE("hex_upper matches snprintf across the whole 16-bit range", "[text][exhaustive]") {
    for (uint32_t v = 0; v <= 0xFFFF; ++v) {
        const std::string got = mie::text::hex_upper(v, 4);
        const std::string want = sprintf_fmt("%04llX", v);
        if (got != want) {
            INFO("value = " << v);
            REQUIRE(got == want);
        }
    }
    SUCCEED("all 65536 four-digit hex renderings match snprintf");
}

TEST_CASE("hex_upper matches snprintf at eight digits too", "[text][exhaustive]") {
    // Sweeps the low 16 bits and the high 16 bits separately -- 2^32 renderings
    // would take minutes for no extra coverage of the digit logic.
    for (uint32_t low = 0; low <= 0xFFFF; ++low) {
        const std::string got = mie::text::hex_upper(low, 8);
        if (got != sprintf_fmt("%08llX", low)) {
            INFO("low = " << low);
            REQUIRE(got == sprintf_fmt("%08llX", low));
        }
    }
    for (uint32_t high = 0; high <= 0xFFFF; ++high) {
        const uint64_t v = static_cast<uint64_t>(high) << 16;
        const std::string got = mie::text::hex_upper(v, 8);
        if (got != sprintf_fmt("%08llX", v)) {
            INFO("high = " << high);
            REQUIRE(got == sprintf_fmt("%08llX", v));
        }
    }
    SUCCEED("eight-digit hex matches snprintf across both halves");
}

TEST_CASE("decimal and decimal_padded match snprintf", "[text][exhaustive]") {
    // 0..99999 covers every digit-count transition that matters for the CSV
    // fields, including the leading-zero boundaries at 10, 100, 1000, ...
    for (uint32_t v = 0; v <= 99999; ++v) {
        if (mie::text::decimal(v) != sprintf_fmt("%llu", v)) {
            INFO("value = " << v);
            REQUIRE(mie::text::decimal(v) == sprintf_fmt("%llu", v));
        }
        if (mie::text::decimal_padded(v, 6) != sprintf_fmt("%06llu", v)) {
            INFO("value = " << v);
            REQUIRE(mie::text::decimal_padded(v, 6) == sprintf_fmt("%06llu", v));
        }
        if (mie::text::decimal_padded(v, 2) != sprintf_fmt("%02llu", v)) {
            INFO("value = " << v);
            REQUIRE(mie::text::decimal_padded(v, 2) == sprintf_fmt("%02llu", v));
        }
    }
    SUCCEED("decimal rendering matches snprintf for 0..99999 at three widths");
}

TEST_CASE("decimal handles the 64-bit boundaries", "[text][exhaustive]") {
    // The values a 32-bit intermediate would truncate, checked explicitly
    // because a sweep cannot reach them.
    const uint64_t boundaries[] = {0,
                                   9,
                                   10,
                                   99,
                                   100,
                                   65535,
                                   65536,
                                   4294967295ull,
                                   4294967296ull,
                                   9223372036854775807ull,
                                   9223372036854775808ull,
                                   18446744073709551615ull};
    for (std::size_t i = 0; i < sizeof(boundaries) / sizeof(boundaries[0]); ++i) {
        CHECK(mie::text::decimal(boundaries[i]) == sprintf_fmt("%llu", boundaries[i]));
    }
}

// ---------------------------------------------------------------------------
// IrigTimestamp::format against snprintf
// ---------------------------------------------------------------------------

TEST_CASE("IRIG formatting matches snprintf over every day value",
          "[models][exhaustive][L2-DEC-014]") {
    // The day field is nine bits, so 0..511 is its full encoded range -- wider
    // than the calendar-valid 1..366, which is deliberate: the formatter's job
    // is to render what the bits say, and validation rejects the rest.
    for (uint32_t day = 0; day <= 511; ++day) {
        const mie::IrigTimestamp ts(static_cast<uint16_t>(day), 15, 54, 50, 456225, false);
        const std::string want = irig_oracle(day, 15, 54, 50, 456225);
        if (ts.format() != want) {
            INFO("day = " << day);
            REQUIRE(ts.format() == want);
        }
    }
    SUCCEED("all 512 encodable day values render as snprintf does");
}

TEST_CASE("IRIG formatting matches snprintf over every hour, minute and second",
          "[models][exhaustive][L2-DEC-014]") {
    // Hour is five bits: 0..31.
    for (uint32_t hour = 0; hour <= 31; ++hour) {
        const mie::IrigTimestamp ts(10, static_cast<uint8_t>(hour), 54, 50, 456225, false);
        const std::string want = irig_oracle(10, hour, 54, 50, 456225);
        if (ts.format() != want) {
            INFO("hour = " << hour);
            REQUIRE(ts.format() == want);
        }
    }

    // Minute and second are six bits each: 0..63.
    for (uint32_t minute = 0; minute <= 63; ++minute) {
        const mie::IrigTimestamp ts(10, 15, static_cast<uint8_t>(minute), 50, 456225, false);
        const std::string want = irig_oracle(10, 15, minute, 50, 456225);
        if (ts.format() != want) {
            INFO("minute = " << minute);
            REQUIRE(ts.format() == want);
        }
    }
    for (uint32_t second = 0; second <= 63; ++second) {
        const mie::IrigTimestamp ts(10, 15, 54, static_cast<uint8_t>(second), 456225, false);
        const std::string want = irig_oracle(10, 15, 54, second, 456225);
        if (ts.format() != want) {
            INFO("second = " << second);
            REQUIRE(ts.format() == want);
        }
    }
    SUCCEED("every encodable hour, minute and second renders as snprintf does");
}

TEST_CASE("IRIG microseconds render as six digits across the full range",
          "[models][exhaustive][L2-DEC-014]") {
    // Every legal microsecond value, 0..999999. This is the field where a
    // width bug shifts every subsequent CSV column on the row, so it is swept
    // in full rather than sampled.
    for (uint32_t us = 0; us < 1000000; ++us) {
        const mie::IrigTimestamp ts(10, 15, 54, 50, us, false);
        const std::string want = irig_oracle(10, 15, 54, 50, us);
        if (ts.format() != want) {
            INFO("microsecond = " << us);
            REQUIRE(ts.format() == want);
        }
    }
    SUCCEED("all 1000000 microsecond values render as exactly six digits");
}

TEST_CASE("an out-of-range microsecond is reduced, never widened", "[models][exhaustive]") {
    // Validation rejects these (L2-SYN-004), so this is the defensive path. A
    // seven-digit field would shift every later column on the row, so the
    // property checked is the WIDTH, over the whole encodable range: the field
    // is 20 bits, so values up to 0xFFFFF can reach the formatter.
    for (uint32_t us = 1000000; us <= 0xFFFFF; ++us) {
        const mie::IrigTimestamp ts(1, 0, 0, 0, us, false);
        const std::string got = ts.format();
        if (got.size() != 17) {
            INFO("microsecond = " << us << " rendered as " << got);
            REQUIRE(got.size() == 17);
        }
    }
    SUCCEED("every out-of-range microsecond still renders in six digits");
}

TEST_CASE("IRIG absolute microseconds agree with an independent computation",
          "[models][exhaustive]") {
    // The oracle multiplies out in a different order from the implementation,
    // so a lost widening cast shows up as a mismatch rather than as two copies
    // of the same mistake.
    for (uint32_t day = 0; day <= 511; day += 7) {
        for (uint32_t hour = 0; hour <= 31; hour += 3) {
            for (uint32_t minute = 0; minute <= 63; minute += 7) {
                for (uint32_t second = 0; second <= 63; second += 11) {
                    const uint32_t us = 456225;
                    const mie::IrigTimestamp ts(
                        static_cast<uint16_t>(day), static_cast<uint8_t>(hour),
                        static_cast<uint8_t>(minute), static_cast<uint8_t>(second), us, false);
                    const uint64_t want = static_cast<uint64_t>(day) * 86400000000ull +
                                          static_cast<uint64_t>(hour) * 3600000000ull +
                                          static_cast<uint64_t>(minute) * 60000000ull +
                                          static_cast<uint64_t>(second) * 1000000ull +
                                          static_cast<uint64_t>(us);
                    if (ts.to_total_microseconds() != want) {
                        INFO("day=" << day << " hour=" << hour << " min=" << minute
                                    << " sec=" << second);
                        REQUIRE(ts.to_total_microseconds() == want);
                    }
                }
            }
        }
    }

    SECTION("the largest encodable timestamp does not overflow 64 bits") {
        const mie::IrigTimestamp max(511, 31, 63, 63, 0xFFFFF, false);
        const uint64_t want = 511ull * 86400000000ull + 31ull * 3600000000ull +
                              63ull * 60000000ull + 63ull * 1000000ull + 0xFFFFFull;
        CHECK(max.to_total_microseconds() == want);
        // Comfortably past 2^32, which is what a 32-bit intermediate would wrap.
        CHECK(max.to_total_microseconds() > 4294967296ull);
    }
}

// ---------------------------------------------------------------------------
// StandardTimestamp conversion
// ---------------------------------------------------------------------------

TEST_CASE("Standard tick conversion rounds half away from zero",
          "[models][exhaustive][L2-DEC-017]") {
    // Swept over every half-tick boundary a 2 MHz counter can
    // produce in its first thousand ticks -- the region where a floor-instead-
    // of-round bug is invisible in a spot check.
    for (uint32_t ticks = 0; ticks < 1000; ++ticks) {
        const mie::StandardTimestamp ts(ticks, 0, 0);
        uint64_t got = 0;
        REQUIRE(ts.to_microseconds(2000000.0, got));
        // 2 MHz: each tick is 0.5 us, so odd tick counts land exactly on .5 and
        // must round UP.
        const uint64_t want = (static_cast<uint64_t>(ticks) + 1) / 2;
        if (got != want) {
            INFO("ticks = " << ticks);
            REQUIRE(got == want);
        }
    }
    SUCCEED("every half-microsecond boundary rounds away from zero");
}

TEST_CASE("Standard conversion declines every unusable tick rate",
          "[models][exhaustive][L2-DEC-017]") {
    // A produced number here would look like real timing and would not be, so
    // the refusal is the feature. Covers the whole class of unusable rates
    // rather than one example.
    const mie::StandardTimestamp ts(1000000, 0x000F, 0x4240);
    uint64_t out = 0;

    CHECK_FALSE(ts.to_microseconds(0.0, out));
    CHECK_FALSE(ts.to_microseconds(-0.0, out));
    CHECK_FALSE(ts.to_microseconds(-1.0, out));
    CHECK_FALSE(ts.to_microseconds(-1e9, out));

    SECTION("non-finite rates are refused") {
        const double inf = std::numeric_limits<double>::infinity();
        CHECK_FALSE(ts.to_microseconds(inf, out));
        CHECK_FALSE(ts.to_microseconds(-inf, out));
        CHECK_FALSE(ts.to_microseconds(std::numeric_limits<double>::quiet_NaN(), out));
    }

    SECTION("the smallest positive rate is accepted, not treated as zero") {
        // Denormal-adjacent but strictly positive: the RATE predicate is "> 0",
        // not "not close to 0", and a decoder that rounded it away would
        // silently drop a legitimate if eccentric calibration.
        //
        // Checked against a zero tick count, so the question asked is purely
        // "is this rate usable" -- the answer for a NON-zero count is the
        // separate section below, and conflating the two is what let this case
        // assert undefined behaviour.
        const mie::StandardTimestamp zero_ticks(0, 0, 0);
        CHECK(zero_ticks.to_microseconds(std::numeric_limits<double>::min(), out));
        CHECK(out == 0u);
    }

    SECTION("a result too large to represent is declined, not truncated") {
        // Reachable from ordinary input: `--standard-tick-rate-hz 1e-300` is
        // finite and positive, passes the rate guard, and drives the result far
        // past what a uint64_t holds. Converting an out-of-range double with
        // static_cast is UNDEFINED BEHAVIOUR in C++ -- so before this check
        // existed, this suite asserted the success of an operation the standard
        // gives no meaning to, and passed because the UB happened to produce a
        // number.
        //
        // Rust saturated (defined, but a fabricated timestamp) and Python
        // returned an integer the other two cannot hold. All three now decline.
        CHECK_FALSE(ts.to_microseconds(1e-300, out));
        CHECK_FALSE(ts.to_microseconds(std::numeric_limits<double>::min(), out));
    }
}

TEST_CASE("Standard raw values survive the full 32-bit range", "[models][exhaustive]") {
    // Swept per 16-bit half rather than over all 2^32, which would add minutes
    // for no additional coverage of the assembly logic.
    for (uint32_t low = 0; low <= 0xFFFF; ++low) {
        const mie::StandardTimestamp ts(low, 0, static_cast<uint16_t>(low));
        if (ts.raw_ticks() != low) {
            INFO("low = " << low);
            REQUIRE(ts.raw_ticks() == low);
        }
    }
    for (uint32_t high = 0; high <= 0xFFFF; ++high) {
        const uint32_t v = high << 16;
        const mie::StandardTimestamp ts(v, static_cast<uint16_t>(high), 0);
        if (ts.raw_ticks() != v) {
            INFO("high = " << high);
            REQUIRE(ts.raw_ticks() == v);
        }
    }
    SUCCEED("Standard raw values round-trip across both 16-bit halves");
}

TEST_CASE("Standard formatting matches snprintf across both halves", "[models][exhaustive]") {
    for (uint32_t low = 0; low <= 0xFFFF; ++low) {
        const mie::StandardTimestamp ts(low, 0, static_cast<uint16_t>(low));
        const std::string want = standard_oracle(low);
        if (ts.format() != want) {
            INFO("low = " << low);
            REQUIRE(ts.format() == want);
        }
    }
    for (uint32_t high = 0; high <= 0xFFFF; ++high) {
        const uint32_t v = high << 16;
        const mie::StandardTimestamp ts(v, static_cast<uint16_t>(high), 0);
        const std::string want = standard_oracle(v);
        if (ts.format() != want) {
            INFO("high = " << high);
            REQUIRE(ts.format() == want);
        }
    }
    SUCCEED("Standard formatting matches snprintf across both halves");
}

// ---------------------------------------------------------------------------
// MieMessage labels, exhaustive over the RT and subaddress space
// ---------------------------------------------------------------------------

TEST_CASE("MSG and DELTA labels are correct for every RT and subaddress", "[models][exhaustive]") {
    // 32 RTs x 32 subaddresses x 2 directions -- the complete space a Command
    // Word can encode. These strings key DELTA tracking and appear in the CSV,
    // so an off-by-one in either would silently mis-group records.
    for (uint32_t rt = 0; rt < 32; ++rt) {
        for (uint32_t sa = 0; sa < 32; ++sa) {
            for (int d = 0; d < 2; ++d) {
                const mie::Direction dir =
                    d == 0 ? mie::DIRECTION_RECEIVE : mie::DIRECTION_TRANSMIT;
                const char suffix = d == 0 ? 'R' : 'T';

                mie::MieMessage m;
                m.command_word =
                    mie::CommandWord(static_cast<uint8_t>(rt), dir, static_cast<uint8_t>(sa), 1, 0);

                const std::string want_msg = msg_oracle(sa, suffix);
                const std::string want_key = key_oracle(rt, sa, suffix);

                if (m.msg_label() != want_msg || m.delta_key() != want_key) {
                    INFO("rt=" << rt << " sa=" << sa << " dir=" << d);
                    REQUIRE(m.msg_label() == want_msg);
                    REQUIRE(m.delta_key() == want_key);
                }

                REQUIRE(m.rt().has_value());
                REQUIRE(m.rt().value() == rt);
                REQUIRE(m.subaddress().value() == sa);
            }
        }
    }
    SUCCEED("all 2048 RT/subaddress/direction combinations label correctly");
}

TEST_CASE("DELTA keys are unique across the whole command space", "[models][exhaustive]") {
    // Two distinct RT/subaddress/direction triples must never produce the same
    // key: a collision would merge two message streams' DELTA tracking, and the
    // resulting gaps would be wrong in a way no single-record test could see.
    std::vector<std::string> keys;
    keys.reserve(2048);
    for (uint32_t rt = 0; rt < 32; ++rt) {
        for (uint32_t sa = 0; sa < 32; ++sa) {
            for (int d = 0; d < 2; ++d) {
                mie::MieMessage m;
                m.command_word =
                    mie::CommandWord(static_cast<uint8_t>(rt),
                                     d == 0 ? mie::DIRECTION_RECEIVE : mie::DIRECTION_TRANSMIT,
                                     static_cast<uint8_t>(sa), 1, 0);
                keys.push_back(m.delta_key());
            }
        }
    }
    REQUIRE(keys.size() == 2048);
    std::sort(keys.begin(), keys.end());
    CHECK(std::adjacent_find(keys.begin(), keys.end()) == keys.end());
}

TEST_CASE("the ERROR label covers every error/spurious combination", "[models][exhaustive]") {
    for (int errored = 0; errored < 2; ++errored) {
        for (int spurious = 0; spurious < 2; ++spurious) {
            mie::MieMessage m;
            m.type_word.error = errored != 0;
            m.message_format = spurious != 0 ? mie::FORMAT_SPURIOUS_DATA : mie::FORMAT_RECEIVE;

            const char* want = spurious != 0 ? "SPURIOUS" : (errored != 0 ? "ERROR" : "");
            INFO("errored=" << errored << " spurious=" << spurious);
            CHECK(std::string(m.error_label()) == want);
        }
    }
}

// ---------------------------------------------------------------------------
// DataWords, exhaustive over every length
// ---------------------------------------------------------------------------

TEST_CASE("DataWords behaves correctly at every length from 0 to 32",
          "[models][exhaustive][datawords]") {
    for (std::size_t n = 0; n <= mie::MAX_DATA_WORDS; ++n) {
        uint16_t source[mie::MAX_DATA_WORDS];
        for (std::size_t i = 0; i < n; ++i) {
            source[i] = static_cast<uint16_t>(0xC000 + i);
        }
        const mie::DataWords w = mie::DataWords::from_words(source, n);

        INFO("length = " << n);
        REQUIRE(w.size() == n);
        REQUIRE(w.empty() == (n == 0));
        for (std::size_t i = 0; i < n; ++i) {
            REQUIRE(w[i] == static_cast<uint16_t>(0xC000 + i));
        }

        // Equality must be sensitive to length, not only to content: a buffer
        // of the same words but one shorter is a different payload.
        if (n > 0) {
            const mie::DataWords shorter = mie::DataWords::from_words(source, n - 1);
            REQUIRE(w != shorter);
        }
    }
    SUCCEED("every DataWords length from 0 to 32 behaves correctly");
}

TEST_CASE("DataWords refuses every push past the cap", "[models][exhaustive][datawords]") {
    mie::DataWords w;
    for (std::size_t i = 0; i < mie::MAX_DATA_WORDS; ++i) {
        REQUIRE(w.try_push(static_cast<uint16_t>(i)));
    }
    // Many attempts, not one: a decoder that allowed a single extra push would
    // corrupt adjacent memory, and ASan would only catch it if the test tried.
    for (int extra = 0; extra < 64; ++extra) {
        REQUIRE_FALSE(w.try_push(0xFFFF));
        REQUIRE(w.size() == mie::MAX_DATA_WORDS);
    }
    // Contents unchanged by the refused pushes.
    for (std::size_t i = 0; i < mie::MAX_DATA_WORDS; ++i) {
        REQUIRE(w[i] == static_cast<uint16_t>(i));
    }
}

TEST_CASE("DataWords truncates every over-long input to the cap",
          "[models][exhaustive][datawords][L1-ROB-001]") {
    uint16_t source[128];
    for (std::size_t i = 0; i < 128; ++i) {
        source[i] = static_cast<uint16_t>(i);
    }
    for (std::size_t n = mie::MAX_DATA_WORDS; n <= 128; ++n) {
        const mie::DataWords w = mie::DataWords::from_words(source, n);
        INFO("requested = " << n);
        REQUIRE(w.size() == mie::MAX_DATA_WORDS);
        REQUIRE(w[0] == 0);
        REQUIRE(w[mie::MAX_DATA_WORDS - 1] == mie::MAX_DATA_WORDS - 1);
    }
    SUCCEED("every over-long payload truncates to the bus-standard cap");
}

// ---------------------------------------------------------------------------
// Enum name round-trips
// ---------------------------------------------------------------------------

TEST_CASE("message type names round-trip under every case permutation", "[models][exhaustive]") {
    // Case-insensitive lookup is the contract; this exercises it over every
    // upper/lower combination of a name rather than one hand-picked mixed-case
    // spelling.
    const std::string name = "BC_TO_RT";
    // The shift is done in size_t, not in unsigned int. MSVC warns (C4334)
    // that a 32-bit shift is being widened to 64 bits and GCC does not, so
    // this is a portability difference the Windows tier catches and the
    // Linux tiers cannot.
    const std::size_t permutations = static_cast<std::size_t>(1) << name.size();
    for (std::size_t mask = 0; mask < permutations; ++mask) {
        std::string spelled = name;
        for (std::size_t bit = 0; bit < name.size(); ++bit) {
            if ((mask >> bit) & 1u) {
                spelled[bit] = mie::text::ascii_lower(spelled[bit]);
            }
        }
        uint8_t code = 0;
        if (!mie::message_type_from_name(spelled, code) || code != mie::MESSAGE_TYPE_BC_TO_RT) {
            INFO("spelling = " << spelled);
            REQUIRE(mie::message_type_from_name(spelled, code));
            REQUIRE(code == mie::MESSAGE_TYPE_BC_TO_RT);
        }
    }
    SUCCEED("all 256 case permutations of BC_TO_RT resolve");
}

TEST_CASE("every byte value is classified consistently by the type predicates",
          "[models][exhaustive]") {
    // is_valid_message_type and message_type_name must agree for every byte:
    // a code the predicate accepts but the namer cannot spell would produce an
    // empty name in a filter diagnostic.
    for (int code = 0; code <= 0xFF; ++code) {
        const uint8_t c = static_cast<uint8_t>(code);
        const bool valid = mie::is_valid_message_type(c);
        const bool named = mie::message_type_name(c)[0] != '\0';
        if (valid != named) {
            INFO("code = 0x" << std::hex << code);
            REQUIRE(valid == named);
        }
    }
    SUCCEED("the type predicate and the namer agree on all 256 byte values");
}
