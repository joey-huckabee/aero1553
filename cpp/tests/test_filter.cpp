// SPDX-License-Identifier: Apache-2.0
//
// Message filtering (L2-FLT-001).
//
// Mirrors `rust/src/filter.rs` and `python/src/aero1553/filters.py`.
//
// THE ASYMMETRY IS THE SEMANTICS. Exclude and include are not opposites:
//
//   * An EMPTY include set is no constraint at all. Reading it as "include
//     nothing" would make a config that sets only `exclude_rts` drop every
//     record in the file — a whole-output-lost bug from a one-line config.
//   * A record with no Command Word cannot SATISFY an RT or subaddress include
//     filter, so an active one drops it. The same record is unaffected by an RT
//     or subaddress EXCLUDE filter, which can only match a value it does not
//     have.
//
// Those two rules pull in opposite directions on the same record, which is why
// each has its own case here rather than being covered incidentally.

#include "mie/filter.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/log.hpp"
#include "mie/models.hpp"

namespace {

using mie_test::LogCapture;

/// A normal record: BC-to-RT, bus A, RT 5, subaddress 1, Receive.
mie::MieMessage record(uint8_t rt = 5, uint8_t subaddress = 1, mie::Bus bus = mie::BUS_A,
                       uint8_t message_type = mie::MESSAGE_TYPE_BC_TO_RT) {
    mie::MieMessage message;
    message.timestamp = mie::Timestamp::from_irig(mie::IrigTimestamp(192, 15, 54, 50, 100, false));
    message.type_word = mie::TypeWord(message_type, bus, 8, false, 0);
    message.message_format = mie::FORMAT_RECEIVE;
    message.command_word = mie::CommandWord(rt, mie::DIRECTION_RECEIVE, subaddress, 2, 0);
    return message;
}

/// SPURIOUS_DATA: no Command Word, so no RT and no subaddress.
mie::MieMessage spurious(mie::Bus bus = mie::BUS_A) {
    mie::MieMessage message;
    message.timestamp = mie::Timestamp::from_irig(mie::IrigTimestamp(192, 15, 54, 50, 200, false));
    message.type_word = mie::TypeWord(mie::MESSAGE_TYPE_SPURIOUS_DATA, bus, 6, false, 0);
    message.message_format = mie::FORMAT_SPURIOUS_DATA;
    message.error_word = mie::ERROR_SPURIOUS_STANDALONE;
    return message;
}

class VectorSource : public mie::MessageSource {
  public:
    explicit VectorSource(const std::vector<mie::MieMessage>& records)
        : records_(records), index_(0) {}

    bool next(mie::MieMessage& out) override {
        if (index_ >= records_.size()) {
            return false;
        }
        out = records_[index_++];
        return true;
    }

  private:
    std::vector<mie::MieMessage> records_;
    std::size_t index_;
};

/// Drain a FilteredSource, returning how many records survived.
std::size_t survivors(const std::vector<mie::MieMessage>& input, const mie::FilterConfig& filters) {
    VectorSource source(input);
    mie::FilteredSource filtered(source, filters);
    std::size_t count = 0;
    mie::MieMessage message;
    while (filtered.next(message)) {
        count += 1;
    }
    return count;
}

}  // namespace

// ---------------------------------------------------------------------------
// is_active
// ---------------------------------------------------------------------------

TEST_CASE("an empty filter set is inactive", "[filter][L2-FLT-001]") {
    // So a caller can skip the stage entirely rather than run a predicate that
    // cannot reject anything.
    CHECK_FALSE(mie::FilterConfig().is_active());
}

TEST_CASE("any populated set makes the config active", "[filter][L2-FLT-001]") {
    // Every set, individually: a new one added to the struct and forgotten here
    // would be a filter that silently never engages the stage.
    {
        mie::FilterConfig f;
        f.exclude_types.push_back(mie::MESSAGE_TYPE_BC_TO_RT);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.exclude_rts.push_back(5);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.exclude_buses.push_back(mie::BUS_A);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.exclude_subaddresses.push_back(1);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.include_types.push_back(mie::MESSAGE_TYPE_BC_TO_RT);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.include_rts.push_back(5);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.include_buses.push_back(mie::BUS_A);
        CHECK(f.is_active());
    }
    {
        mie::FilterConfig f;
        f.include_subaddresses.push_back(1);
        CHECK(f.is_active());
    }
}

// ---------------------------------------------------------------------------
// Exclusion
// ---------------------------------------------------------------------------

TEST_CASE("an inactive config excludes nothing", "[filter][L2-FLT-001]") {
    const mie::FilterConfig f;
    CHECK_FALSE(f.should_exclude(record()));
    CHECK_FALSE(f.should_exclude(spurious()));
}

TEST_CASE("each exclude set drops its own match", "[filter][L2-FLT-001]") {
    {
        mie::FilterConfig f;
        f.exclude_types.push_back(mie::MESSAGE_TYPE_BC_TO_RT);
        CHECK(f.should_exclude(record()));
        CHECK_FALSE(f.should_exclude(record(5, 1, mie::BUS_A, mie::MESSAGE_TYPE_RT_TO_BC)));
    }
    {
        mie::FilterConfig f;
        f.exclude_rts.push_back(5);
        CHECK(f.should_exclude(record(5)));
        CHECK_FALSE(f.should_exclude(record(6)));
    }
    {
        mie::FilterConfig f;
        f.exclude_buses.push_back(mie::BUS_B);
        CHECK(f.should_exclude(record(5, 1, mie::BUS_B)));
        CHECK_FALSE(f.should_exclude(record(5, 1, mie::BUS_A)));
    }
    {
        mie::FilterConfig f;
        f.exclude_subaddresses.push_back(11);
        CHECK(f.should_exclude(record(5, 11)));
        CHECK_FALSE(f.should_exclude(record(5, 12)));
    }
}

TEST_CASE("an RT or subaddress exclusion cannot match a record with no Command Word",
          "[filter][L2-FLT-001]") {
    // A SPURIOUS record has no RT to exclude. Treating its absence as a match
    // would silently drop every continuation record whenever any RT filter was
    // set, breaking the 0x2000 adjacency for reasons the operator never asked
    // for.
    mie::FilterConfig f;
    f.exclude_rts.push_back(5);
    f.exclude_subaddresses.push_back(1);
    CHECK_FALSE(f.should_exclude(spurious()));
}

TEST_CASE("a spurious record is still subject to type and bus exclusion", "[filter][L2-FLT-001]") {
    // Both values it DOES have.
    {
        mie::FilterConfig f;
        f.exclude_types.push_back(mie::MESSAGE_TYPE_SPURIOUS_DATA);
        CHECK(f.should_exclude(spurious()));
    }
    {
        mie::FilterConfig f;
        f.exclude_buses.push_back(mie::BUS_B);
        CHECK(f.should_exclude(spurious(mie::BUS_B)));
        CHECK_FALSE(f.should_exclude(spurious(mie::BUS_A)));
    }
}

// ---------------------------------------------------------------------------
// Inclusion
// ---------------------------------------------------------------------------

TEST_CASE("an empty include set is no constraint", "[filter][L2-FLT-001]") {
    // THE case that matters most. Reading empty as "include nothing" would make
    // a config that sets only exclude_rts drop the entire file.
    mie::FilterConfig f;
    f.exclude_rts.push_back(9);
    CHECK_FALSE(f.should_exclude(record(5)));
    CHECK_FALSE(f.should_exclude(spurious()));
}

TEST_CASE("an active include set keeps only what it names", "[filter][L2-FLT-001]") {
    {
        mie::FilterConfig f;
        f.include_rts.push_back(5);
        CHECK_FALSE(f.should_exclude(record(5)));
        CHECK(f.should_exclude(record(6)));
    }
    {
        mie::FilterConfig f;
        f.include_types.push_back(mie::MESSAGE_TYPE_BC_TO_RT);
        CHECK_FALSE(f.should_exclude(record()));
        CHECK(f.should_exclude(record(5, 1, mie::BUS_A, mie::MESSAGE_TYPE_RT_TO_BC)));
    }
    {
        mie::FilterConfig f;
        f.include_buses.push_back(mie::BUS_A);
        CHECK_FALSE(f.should_exclude(record(5, 1, mie::BUS_A)));
        CHECK(f.should_exclude(record(5, 1, mie::BUS_B)));
    }
    {
        mie::FilterConfig f;
        f.include_subaddresses.push_back(1);
        CHECK_FALSE(f.should_exclude(record(5, 1)));
        CHECK(f.should_exclude(record(5, 2)));
    }
}

TEST_CASE("a record with no Command Word cannot satisfy an RT or subaddress include",
          "[filter][L2-FLT-001]") {
    // An operator narrowing to RT 5 is not asking to keep records that have no
    // RT at all. This is the mirror of the exclusion case above, and the two
    // pull in opposite directions on the SAME record.
    {
        mie::FilterConfig f;
        f.include_rts.push_back(5);
        CHECK(f.should_exclude(spurious()));
    }
    {
        mie::FilterConfig f;
        f.include_subaddresses.push_back(1);
        CHECK(f.should_exclude(spurious()));
    }
    // A type or bus include it CAN satisfy still keeps it.
    {
        mie::FilterConfig f;
        f.include_types.push_back(mie::MESSAGE_TYPE_SPURIOUS_DATA);
        CHECK_FALSE(f.should_exclude(spurious()));
    }
}

TEST_CASE("exclusion still applies inside an include set", "[filter][L2-FLT-001]") {
    // The narrower rule wins: including RT 5 and excluding subaddress 2 keeps
    // RT 5 subaddress 1 and drops RT 5 subaddress 2.
    mie::FilterConfig f;
    f.include_rts.push_back(5);
    f.exclude_subaddresses.push_back(2);
    CHECK_FALSE(f.should_exclude(record(5, 1)));
    CHECK(f.should_exclude(record(5, 2)));
    CHECK(f.should_exclude(record(6, 1)));
}

TEST_CASE("include sets combine as an AND across dimensions", "[filter][L2-FLT-001]") {
    // Each active include is its own constraint; a record must satisfy all of
    // them, not any one.
    mie::FilterConfig f;
    f.include_rts.push_back(5);
    f.include_buses.push_back(mie::BUS_A);
    CHECK_FALSE(f.should_exclude(record(5, 1, mie::BUS_A)));
    CHECK(f.should_exclude(record(5, 1, mie::BUS_B)));
    CHECK(f.should_exclude(record(6, 1, mie::BUS_A)));
}

// ---------------------------------------------------------------------------
// The stage
// ---------------------------------------------------------------------------

TEST_CASE("the stage drops what the predicate excludes", "[filter][L2-FLT-001]") {
    std::vector<mie::MieMessage> input;
    input.push_back(record(5));
    input.push_back(record(9));
    input.push_back(record(5));

    mie::FilterConfig f;
    f.exclude_rts.push_back(9);
    CHECK(survivors(input, f) == 2);
}

TEST_CASE("an inactive stage passes everything through", "[filter][L2-FLT-001]") {
    std::vector<mie::MieMessage> input;
    input.push_back(record(5));
    input.push_back(spurious());
    CHECK(survivors(input, mie::FilterConfig()) == 2);
}

TEST_CASE("a long excluded run does not recurse", "[filter]") {
    // The stage loops rather than recursing: a file where the filter drops
    // thousands of consecutive records must not consume stack proportional to
    // the run length.
    std::vector<mie::MieMessage> input;
    input.reserve(5001);
    for (int i = 0; i < 5000; ++i) {
        input.push_back(record(9));
    }
    input.push_back(record(5));

    mie::FilterConfig f;
    f.exclude_rts.push_back(9);
    CHECK(survivors(input, f) == 1);
}

TEST_CASE("the stage counts what passed and what was dropped", "[filter][L2-FLT-001]") {
    std::vector<mie::MieMessage> input;
    input.push_back(record(5));
    input.push_back(record(9));
    input.push_back(record(9));

    mie::FilterConfig f;
    f.exclude_rts.push_back(9);

    VectorSource source(input);
    mie::FilteredSource filtered(source, f);
    mie::MieMessage message;
    while (filtered.next(message)) {
    }
    CHECK(filtered.passed() == 1);
    CHECK(filtered.excluded() == 2);
}

TEST_CASE("the active sets are reported once, sorted", "[filter][L2-FLT-001]") {
    // Sorted so the line is identical across the three implementations: Python
    // holds these as sets, whose iteration order is not guaranteed.
    const LogCapture capture(mie::log::LEVEL_INFO);
    mie::FilterConfig f;
    f.exclude_rts.push_back(9);
    f.exclude_rts.push_back(2);
    f.exclude_buses.push_back(mie::BUS_B);

    const std::vector<mie::MieMessage> nothing;
    VectorSource source(nothing);
    {
        // Constructed for its side effect and destroyed immediately: the active
        // sets are reported once, at construction.
        const mie::FilteredSource filtered(source, f);
    }

    CHECK(capture.count_containing("Filtering active:") == 1);
    CHECK(capture.contains("exclude_rts=[2, 9]"));
    CHECK(capture.contains("exclude_buses=[B]"));
    CHECK(capture.contains("include_rts=none"));
}

TEST_CASE("the tally is emitted even when the consumer stops early", "[filter][L2-WRT-018]") {
    // From the DESTRUCTOR, matching Rust's Drop. A consumer can stop early --
    // a broken pipe, `| head` -- and an end-of-stream hook would simply never
    // run, losing the tally exactly when the operator most wants to know how
    // much was dropped.
    std::vector<mie::MieMessage> input;
    input.push_back(record(9));
    input.push_back(record(5));
    input.push_back(record(5));

    mie::FilterConfig f;
    f.exclude_rts.push_back(9);

    const LogCapture capture(mie::log::LEVEL_INFO);
    {
        VectorSource source(input);
        mie::FilteredSource filtered(source, f);
        mie::MieMessage message;
        // One record only, then abandon the stage mid-stream.
        CHECK(filtered.next(message));
    }
    CHECK(capture.contains("Filter results: 1 passed, 1 excluded"));
}

TEST_CASE("an inactive stage reports nothing at all", "[filter][L2-FLT-001]") {
    // No filters configured means no filter narration; the log belongs to
    // whoever did ask for filtering.
    const LogCapture capture(mie::log::LEVEL_INFO);
    {
        const std::vector<mie::MieMessage> nothing;
        VectorSource source(nothing);
        const mie::FilteredSource filtered(source, mie::FilterConfig());
    }
    CHECK(capture.count_containing("Filtering active:") == 0);
    CHECK(capture.count_containing("Filter results:") == 0);
}

TEST_CASE("a dropped record is named at DEBUG", "[filter][L2-FLT-001]") {
    const LogCapture capture(mie::log::LEVEL_DEBUG);
    std::vector<mie::MieMessage> input;
    input.push_back(record(9, 3));

    mie::FilterConfig f;
    f.exclude_rts.push_back(9);
    CHECK(survivors(input, f) == 0);
    CHECK(capture.contains("Filtered out:"));
    CHECK(capture.contains("RT9"));
    CHECK(capture.contains("SA3"));
}

TEST_CASE("a dropped record with no Command Word renders its RT as a dash",
          "[filter][L2-FLT-001]") {
    const LogCapture capture(mie::log::LEVEL_DEBUG);
    std::vector<mie::MieMessage> input;
    input.push_back(spurious());

    mie::FilterConfig f;
    f.exclude_types.push_back(mie::MESSAGE_TYPE_SPURIOUS_DATA);
    CHECK(survivors(input, f) == 0);
    CHECK(capture.contains("RT- SA-"));
}
