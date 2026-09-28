// SPDX-License-Identifier: Apache-2.0
//
// Tests for the logger.
//
// Two properties carry real weight and the rest is bookkeeping:
//
//   * the level filter must SUPPRESS THE ARGUMENT, not just the output. The
//     macros exist so a DEBUG line inside the per-record loop costs nothing at
//     the default level, and a filter that still evaluated its argument would
//     silently give that up while every assertion about visible output still
//     passed.
//   * OFF must silence everything, including ERROR. It is the spelling an
//     operator reaches for when the decoder's stderr is being piped somewhere
//     that must stay clean.

#include "mie/log.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <vector>

#include "log_capture.hpp"

namespace {

namespace lg = mie::log;

using mie_test::captured_lines;
using mie_test::LogCapture;

}  // namespace

#define MIE_LOG_MODULE "aero1553::test_log"

// ---------------------------------------------------------------------------
// Level names
// ---------------------------------------------------------------------------

TEST_CASE("level names parse case-insensitively", "[log][L2-CLI-004]") {
    lg::Level level = lg::LEVEL_OFF;

    CHECK(lg::level_from_name("DEBUG", level));
    CHECK(level == lg::LEVEL_DEBUG);
    CHECK(lg::level_from_name("debug", level));
    CHECK(level == lg::LEVEL_DEBUG);
    CHECK(lg::level_from_name("Info", level));
    CHECK(level == lg::LEVEL_INFO);
    CHECK(lg::level_from_name("ERROR", level));
    CHECK(level == lg::LEVEL_ERROR);

    SECTION("WARN is an alias for WARNING") {
        CHECK(lg::level_from_name("WARNING", level));
        CHECK(level == lg::LEVEL_WARN);
        CHECK(lg::level_from_name("warn", level));
        CHECK(level == lg::LEVEL_WARN);
    }

    SECTION("CRITICAL and OFF both mean silence") {
        // The decoder emits nothing at CRITICAL, so the two are
        // indistinguishable in effect -- and the Python implementation maps
        // CRITICAL to a numeric level above every message it emits for the same
        // reason. Accepting both spellings is what keeps `--log-level CRITICAL`
        // portable across the three.
        CHECK(lg::level_from_name("CRITICAL", level));
        CHECK(level == lg::LEVEL_OFF);
        CHECK(lg::level_from_name("off", level));
        CHECK(level == lg::LEVEL_OFF);
    }
}

TEST_CASE("an unrecognised level name is declined", "[log]") {
    lg::Level level = lg::LEVEL_DEBUG;
    CHECK_FALSE(lg::level_from_name("nope", level));
    CHECK_FALSE(lg::level_from_name("", level));
    CHECK_FALSE(lg::level_from_name("TRACE", level));
    CHECK_FALSE(lg::level_from_name("WARNINGS", level));
    // Declined means untouched, so a caller can keep its default in place.
    CHECK(level == lg::LEVEL_DEBUG);
}

TEST_CASE("labels are the short Rust spellings", "[log]") {
    // WARN, not WARNING: the label appears in every emitted line and the Rust
    // implementation writes it this way.
    CHECK(std::string(lg::level_label(lg::LEVEL_DEBUG)) == "DEBUG");
    CHECK(std::string(lg::level_label(lg::LEVEL_INFO)) == "INFO");
    CHECK(std::string(lg::level_label(lg::LEVEL_WARN)) == "WARN");
    CHECK(std::string(lg::level_label(lg::LEVEL_ERROR)) == "ERROR");
    CHECK(std::string(lg::level_label(lg::LEVEL_OFF)) == "OFF");
}

TEST_CASE("levels are ordered by severity", "[log][L1-LOG-001]") {
    CHECK(lg::LEVEL_DEBUG < lg::LEVEL_INFO);
    CHECK(lg::LEVEL_INFO < lg::LEVEL_WARN);
    CHECK(lg::LEVEL_WARN < lg::LEVEL_ERROR);
    CHECK(lg::LEVEL_ERROR < lg::LEVEL_OFF);
}

// ---------------------------------------------------------------------------
// The level filter
// ---------------------------------------------------------------------------

TEST_CASE("the default level is WARN", "[log]") {
    // Not set by this test: read as the process starts it. Every other case in
    // the suite restores whatever it found, so this holds wherever it runs in
    // the ordering.
    CHECK(lg::current_level() == lg::LEVEL_WARN);
}

TEST_CASE("enabled() answers for every level at every setting", "[log]") {
    const lg::Level previous = lg::current_level();

    lg::set_level(lg::LEVEL_DEBUG);
    CHECK(lg::enabled(lg::LEVEL_DEBUG));
    CHECK(lg::enabled(lg::LEVEL_ERROR));

    lg::set_level(lg::LEVEL_WARN);
    CHECK_FALSE(lg::enabled(lg::LEVEL_DEBUG));
    CHECK_FALSE(lg::enabled(lg::LEVEL_INFO));
    CHECK(lg::enabled(lg::LEVEL_WARN));
    CHECK(lg::enabled(lg::LEVEL_ERROR));

    lg::set_level(lg::LEVEL_OFF);
    CHECK_FALSE(lg::enabled(lg::LEVEL_DEBUG));
    CHECK_FALSE(lg::enabled(lg::LEVEL_INFO));
    CHECK_FALSE(lg::enabled(lg::LEVEL_WARN));
    CHECK_FALSE(lg::enabled(lg::LEVEL_ERROR));

    lg::set_level(previous);
}

TEST_CASE("OFF silences every level including ERROR", "[log]") {
    const LogCapture capture(lg::LEVEL_OFF);
    MIE_LOG_DEBUG(std::string("d"));
    MIE_LOG_INFO(std::string("i"));
    MIE_LOG_WARN(std::string("w"));
    MIE_LOG_ERROR(std::string("e"));
    CHECK(capture.count() == 0);
}

TEST_CASE("a suppressed macro does not evaluate its argument", "[log]") {
    // The whole reason these are macros. A function taking std::string would
    // build the message whether or not it was going to be used, and the
    // per-record DEBUG lines in the reader would cost an allocation each at the
    // default level. Nothing about the emitted output can detect that, so it is
    // asserted directly with a side effect.
    const LogCapture capture(lg::LEVEL_WARN);

    int evaluations = 0;
    auto build = [&evaluations]() -> std::string {
        evaluations += 1;
        return std::string("built");
    };

    MIE_LOG_DEBUG(build());
    MIE_LOG_INFO(build());
    CHECK(evaluations == 0);

    MIE_LOG_WARN(build());
    CHECK(evaluations == 1);
    CHECK(capture.count() == 1);
}

// ---------------------------------------------------------------------------
// Line format
// ---------------------------------------------------------------------------

TEST_CASE("a line is LEVEL [module] message with one trailing newline", "[log]") {
    const LogCapture capture(lg::LEVEL_DEBUG);
    MIE_LOG_WARN(std::string("something happened"));

    REQUIRE(capture.count() == 1);
    CHECK(capture.lines()[0] == "WARN [aero1553::test_log] something happened\n");
}

TEST_CASE("emit() writes unconditionally", "[log]") {
    // The macros filter; emit does not. Documented, and worth pinning: a caller
    // that has already tested `enabled()` for its own reasons must not have the
    // level applied a second time.
    const LogCapture capture(lg::LEVEL_OFF);
    lg::emit(lg::LEVEL_DEBUG, "somewhere", "forced");
    REQUIRE(capture.count() == 1);
    CHECK(capture.lines()[0] == "DEBUG [somewhere] forced\n");
}

TEST_CASE("an empty message still produces a well-formed line", "[log]") {
    const LogCapture capture(lg::LEVEL_DEBUG);
    MIE_LOG_INFO(std::string());
    REQUIRE(capture.count() == 1);
    CHECK(capture.lines()[0] == "INFO [aero1553::test_log] \n");
}

TEST_CASE("a message containing newlines is passed through verbatim", "[log]") {
    // No escaping and no re-prefixing of continuation lines. Multi-line
    // diagnostics are rare, and rewriting them would make a hex dump unreadable
    // in exchange for tidier grep output.
    const LogCapture capture(lg::LEVEL_DEBUG);
    MIE_LOG_ERROR(std::string("first\nsecond"));
    REQUIRE(capture.count() == 1);
    CHECK(capture.lines()[0] == "ERROR [aero1553::test_log] first\nsecond\n");
}

TEST_CASE("each emission is delivered as exactly one write", "[log]") {
    // The sink receives whole lines, never a prefix and a body separately.
    // Splitting them would let two writers interleave inside one line, which is
    // the failure mode that makes a concurrent log unreadable.
    const LogCapture capture(lg::LEVEL_DEBUG);
    MIE_LOG_WARN(std::string("a"));
    MIE_LOG_WARN(std::string("b"));
    MIE_LOG_WARN(std::string("c"));
    CHECK(capture.count() == 3);
}

TEST_CASE("the sink can be removed again", "[log]") {
    {
        const LogCapture capture(lg::LEVEL_DEBUG);
        MIE_LOG_WARN(std::string("captured"));
        CHECK(capture.count() == 1);
    }
    // Out of scope: the sink is back to stderr, so this line goes there and the
    // buffer keeps the single entry from before. Restoring matters because a
    // leaked sink would silently redirect every later test's logging into a
    // buffer nobody reads.
    lg::set_level(lg::LEVEL_DEBUG);
    MIE_LOG_WARN(std::string("to stderr"));
    CHECK(captured_lines().size() == 1);
    lg::set_level(lg::LEVEL_WARN);
}
