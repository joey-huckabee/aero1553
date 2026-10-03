// SPDX-License-Identifier: Apache-2.0
//
// Canonical row order (L1-OUT-003, L2-WRT-021, L2-WRT-022).
//
// Mirrors the Rust unit tests in `rust/src/order.rs` and the Python ones in
// `python/tests/test_order.py` case for case.
//
// THE PINNING CASES ARE THE POINT. A record with no Command Word has no sort
// key, and its position is defined relative to its PREDECESSOR — the card
// writes the leftover words of an errored transaction as a SPURIOUS record
// immediately after it, and `0x2000` means "continues the record before me".
// Holding a pin at a preserved INDEX while sorting looks equivalent and is not:
// it leaves the pin trailing whichever record the sort moved into that slot.
// `a pin travels with the record it followed` is the case that tells the two
// implementations apart, and it fails against the index-preserving version.

#include "mie/order.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/error.hpp"
#include "mie/log.hpp"
#include "mie/models.hpp"

namespace {

using mie_test::LogCapture;

/// A record at `micros` with a Command Word, so it sorts.
mie::MieMessage at(uint32_t micros, uint8_t rt, uint8_t subaddress, bool transmit) {
    mie::MieMessage message;
    message.timestamp =
        mie::Timestamp::from_irig(mie::IrigTimestamp(192, 15, 54, 50, micros, false));
    message.type_word = mie::TypeWord(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 8, false, 0x0802);
    message.message_format = mie::FORMAT_RECEIVE;
    message.command_word = mie::CommandWord(
        rt, transmit ? mie::DIRECTION_TRANSMIT : mie::DIRECTION_RECEIVE, subaddress, 2, 0);
    // The offset is the identity a test asserts on -- it survives sorting and
    // says which input record this is.
    message.file_offset = 0;
    return message;
}

/// A SPURIOUS record at `micros`: no Command Word, so it pins.
mie::MieMessage pin(uint32_t micros) {
    mie::MieMessage message;
    message.timestamp =
        mie::Timestamp::from_irig(mie::IrigTimestamp(192, 15, 54, 50, micros, false));
    message.type_word =
        mie::TypeWord(mie::MESSAGE_TYPE_SPURIOUS_DATA, mie::BUS_A, 6, false, 0x0620);
    message.message_format = mie::FORMAT_SPURIOUS_DATA;
    message.error_word = mie::ERROR_SPURIOUS_CONTINUATION;
    return message;
}

/// A Standard-timestamp record, for the mixed-variant case.
mie::MieMessage standard_at(uint32_t ticks, uint8_t rt) {
    mie::MieMessage message;
    message.timestamp = mie::Timestamp::from_standard(
        mie::StandardTimestamp(ticks, static_cast<uint16_t>((ticks >> 16) & 0xFFFF),
                               static_cast<uint16_t>(ticks & 0xFFFF)));
    message.type_word = mie::TypeWord(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 7, false, 0x0702);
    message.message_format = mie::FORMAT_RECEIVE;
    message.command_word = mie::CommandWord(rt, mie::DIRECTION_RECEIVE, 1, 2, 0);
    return message;
}

/// Tag each record so the assertions can name it after sorting.
std::vector<mie::MieMessage> tagged(const std::vector<mie::MieMessage>& records) {
    std::vector<mie::MieMessage> out(records);
    for (std::size_t i = 0; i < out.size(); ++i) {
        out[i].file_offset = i;
    }
    return out;
}

class VectorSource : public mie::MessageSource {
  public:
    explicit VectorSource(const std::vector<mie::MieMessage>& records)
        : records_(records),
          index_(0),
          throws_(false),
          error_(mie::MieError::payload_error(0, "x")) {}

    void throw_at_end(const mie::MieError& error) {
        throws_ = true;
        error_ = error;
    }

    bool next(mie::MieMessage& out) override {
        if (index_ < records_.size()) {
            out = records_[index_++];
            return true;
        }
        if (throws_) {
            throws_ = false;
            throw error_;
        }
        return false;
    }

  private:
    std::vector<mie::MieMessage> records_;
    std::size_t index_;
    bool throws_;
    mie::MieError error_;
};

/// Drain an OrderedSource, returning the file_offset of each record in the
/// order emitted.
std::vector<uint64_t> order_of(const std::vector<mie::MieMessage>& input,
                               std::size_t max_group = 4096) {
    VectorSource source(tagged(input));
    mie::OrderedSource ordered(source, max_group);
    std::vector<uint64_t> out;
    mie::MieMessage message;
    while (ordered.next(message)) {
        out.push_back(message.file_offset);
    }
    return out;
}

std::vector<uint64_t> seq(uint64_t a) { return std::vector<uint64_t>(1, a); }

std::vector<uint64_t> seq(uint64_t a, uint64_t b) {
    std::vector<uint64_t> out;
    out.push_back(a);
    out.push_back(b);
    return out;
}

std::vector<uint64_t> seq(uint64_t a, uint64_t b, uint64_t c) {
    std::vector<uint64_t> out = seq(a, b);
    out.push_back(c);
    return out;
}

std::vector<uint64_t> seq(uint64_t a, uint64_t b, uint64_t c, uint64_t d) {
    std::vector<uint64_t> out = seq(a, b, c);
    out.push_back(d);
    return out;
}

}  // namespace

// ---------------------------------------------------------------------------
// Degenerate cases
// ---------------------------------------------------------------------------

TEST_CASE("an empty stream yields nothing", "[order][L2-WRT-021]") {
    CHECK(order_of(std::vector<mie::MieMessage>()).empty());
}

TEST_CASE("a single record passes through", "[order][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 5, 1, false));
    CHECK(order_of(input) == seq(0));
}

TEST_CASE("records with differing timestamps are never reordered",
          "[order][L2-WRT-021][L2-MRG-006]") {
    // The stage permutes only WITHIN a run of equal timestamps. Reordering
    // across them would be a whole-file sort, which breaks the constant-memory
    // guarantee and the merge's never-re-sort rule.
    std::vector<mie::MieMessage> input;
    input.push_back(at(300, 9, 1, false));  // out of RT order on purpose
    input.push_back(at(200, 5, 1, false));
    input.push_back(at(100, 1, 1, false));
    CHECK(order_of(input) == seq(0, 1, 2));
}

// ---------------------------------------------------------------------------
// The sort key
// ---------------------------------------------------------------------------

TEST_CASE("a tied run sorts by RT", "[order][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(at(100, 2, 1, false));
    input.push_back(at(100, 5, 1, false));
    CHECK(order_of(input) == seq(1, 2, 0));
}

TEST_CASE("a tied run sorts by subaddress within an RT", "[order][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 5, 11, false));
    input.push_back(at(100, 5, 2, false));
    CHECK(order_of(input) == seq(1, 0));
}

TEST_CASE("Receive orders before Transmit at an equal subaddress", "[order][L2-WRT-021]") {
    // Falls out of the discriminants -- RECEIVE is 0, TRANSMIT is 1 -- rather
    // than needing a special case, in all three implementations.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 5, 1, true));
    input.push_back(at(100, 5, 1, false));
    CHECK(order_of(input) == seq(1, 0));
}

TEST_CASE("the key is the decoded fields, not the rendered MSG string", "[order][L2-WRT-021]") {
    // "11R" sorts before "2R" lexicographically, which is NOT the required
    // order. A string key is the shortcut that looks right and is wrong.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 5, 11, false));
    input.push_back(at(100, 5, 2, false));
    const std::vector<uint64_t> emitted = order_of(input);
    CHECK(emitted == seq(1, 0));  // subaddress 2 first, numerically
}

TEST_CASE("records with a fully equal key keep arrival order", "[order][L1-OUT-003]") {
    // A stable sort, not merely a sort: two records that tie on every key
    // component must not swap.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 5, 1, false));
    input.push_back(at(100, 5, 1, false));
    input.push_back(at(100, 5, 1, false));
    CHECK(order_of(input) == seq(0, 1, 2));
}

TEST_CASE("timestamp grouping compares the variant as well as the value", "[order][L2-WRT-021]") {
    // An IRIG microsecond count and a Standard tick count could compare equal
    // as numbers while meaning entirely different times. Impossible today --
    // the format is resolved once per file -- but comparing values alone would
    // be silently wrong the day it is not.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(standard_at(100, 2));
    CHECK(order_of(input) == seq(0, 1));
}

// ---------------------------------------------------------------------------
// Pinning
// ---------------------------------------------------------------------------

TEST_CASE("a pin travels with the record it followed", "[order][L2-WRT-021][L2-ERR-005]") {
    // THE case that distinguishes a correct implementation from an
    // index-preserving one. Input: RT 9 then its pin, then RT 2. Sorting moves
    // RT 2 to the front; the pin must follow RT 9 to its new position, NOT stay
    // at index 1 where it would trail RT 9's replacement.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));  // 0: anchor
    input.push_back(pin(100));              // 1: continues record 0
    input.push_back(at(100, 2, 1, false));  // 2: sorts ahead of RT 9

    // 2 (RT 2), then 0 (RT 9) with its pin 1 still immediately behind it.
    CHECK(order_of(input) == seq(2, 0, 1));
}

TEST_CASE("multiple pins all travel with their anchor", "[order][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(pin(100));
    input.push_back(pin(100));
    input.push_back(at(100, 2, 1, false));
    CHECK(order_of(input) == seq(3, 0, 1, 2));
}

TEST_CASE("a run opening with a pin keeps it at the front", "[order][L2-WRT-021]") {
    // A pin arriving before any anchor has nothing to travel with, so it stays
    // where it arrived rather than being attached to a record it never
    // followed.
    std::vector<mie::MieMessage> input;
    input.push_back(pin(100));
    input.push_back(at(100, 9, 1, false));
    input.push_back(at(100, 2, 1, false));
    CHECK(order_of(input) == seq(0, 2, 1));
}

TEST_CASE("a run of only pins is untouched", "[order][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(pin(100));
    input.push_back(pin(100));
    input.push_back(pin(100));
    CHECK(order_of(input) == seq(0, 1, 2));
}

TEST_CASE("a pin does not join a record from a different timestamp", "[order][L2-WRT-021]") {
    // Runs are bounded by the timestamp, so a pin cannot be dragged into the
    // previous group.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(pin(200));
    input.push_back(at(200, 2, 1, false));
    CHECK(order_of(input) == seq(0, 1, 2));
}

// ---------------------------------------------------------------------------
// The cap (L2-WRT-022)
// ---------------------------------------------------------------------------

TEST_CASE("a pin at a later timestamp keeps the run in arrival order",
          "[order][L1-OUT-003][L2-WRT-021][L2-ERR-005]") {
    // Review finding M3. The pin opens the next run, so it follows whatever
    // this run ends with. Sorting would put RT 3 last -- between RT 1 and the
    // pin that continues it -- so the run is emitted as it arrived.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 3, 1, false));  // 0
    input.push_back(at(100, 1, 1, false));  // 1: the pin's predecessor
    input.push_back(pin(101));              // 2: later timestamp
    CHECK(order_of(input) == seq(0, 1, 2));
}

TEST_CASE("a pin at a later timestamp keeps canonical order when the tail stays last",
          "[order][L1-OUT-003][L2-WRT-021]") {
    // The fallback is taken only when sorting would move the record that
    // arrived last; here it stays last, so the rest of the run is still sorted.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 20, 1, false));  // 0
    input.push_back(at(100, 3, 1, false));   // 1
    input.push_back(at(100, 25, 1, false));  // 2: arrived last, sorts last
    input.push_back(pin(101));               // 3
    CHECK(order_of(input) == seq(1, 0, 2, 3));
}

TEST_CASE("a later pin after a same-timestamp pin keeps the chain together",
          "[order][L1-OUT-003][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 20, 1, false));  // 0
    input.push_back(at(100, 3, 1, false));   // 1
    input.push_back(pin(100));               // 2: trails record 1 in its run
    input.push_back(pin(101));               // 3: trails record 2
    CHECK(order_of(input) == seq(0, 1, 2, 3));
}

TEST_CASE("a keyed record at a later timestamp still sorts the run", "[order][L2-WRT-021]") {
    // Only a record with no Command Word is pinned to the run's end.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 20, 1, false));  // 0
    input.push_back(at(100, 3, 1, false));   // 1
    input.push_back(at(101, 1, 1, false));   // 2
    CHECK(order_of(input) == seq(1, 0, 2));
}

TEST_CASE("an arrival-order fallback is logged at DEBUG, not WARN", "[order][L2-WRT-021]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 3, 1, false));
    input.push_back(at(100, 1, 1, false));
    input.push_back(pin(101));

    const LogCapture capture(mie::log::LEVEL_DEBUG);
    CHECK(order_of(input) == seq(0, 1, 2));
    CHECK(capture.count_containing("DEBUG [aero1553::order] equal-timestamp run at") == 1);
    CHECK(capture.count_containing("emitted in arrival order") == 1);
    CHECK(capture.count_containing("WARN [") == 0);
}

TEST_CASE("a cap of one disables reordering", "[order][L2-WRT-022]") {
    // Restores raw DDC capture order, which is what an operator diffing
    // against the vendor tool's own ordering wants.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(at(100, 2, 1, false));
    CHECK(order_of(input, 1) == seq(0, 1));
}

TEST_CASE("a capped run is emitted in arrival order with one WARN", "[order][L2-WRT-022]") {
    const LogCapture capture(mie::log::LEVEL_WARN);
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(at(100, 5, 1, false));
    input.push_back(at(100, 2, 1, false));

    CHECK(order_of(input, 2) == seq(0, 1, 2));
    CHECK(capture.count_containing("max_sort_group cap") == 1);
}

TEST_CASE("a capped run keeps every row", "[order][L2-WRT-022]") {
    // The cap changes ORDER, never content. Dropping rows to stay under a
    // buffer limit would be a data-loss bug wearing a performance hat.
    std::vector<mie::MieMessage> input;
    input.reserve(10);
    for (int i = 0; i < 10; ++i) {
        input.push_back(at(100, static_cast<uint8_t>(10 - i), 1, false));
    }
    const LogCapture capture(mie::log::LEVEL_WARN);
    CHECK(order_of(input, 3).size() == 10);
}

TEST_CASE("a zero cap is clamped rather than stalling", "[order][L2-WRT-022]") {
    // Zero would mean "buffer nothing", which as a literal instruction is a
    // stage that can never emit. Clamped to 1, which is the nearest thing that
    // means something.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(at(100, 2, 1, false));
    CHECK(order_of(input, 0) == seq(0, 1));
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

TEST_CASE("a buffered run is flushed before an error surfaces",
          "[order][L1-EXIT-004][L2-WRT-021]") {
    // Under --allow-partial the rows this stage is holding must reach the
    // committed .partial. Letting the throw through first would drop a whole
    // equal-timestamp group from the operator's only record of what decoded.
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    input.push_back(at(100, 2, 1, false));
    VectorSource source(tagged(input));
    source.throw_at_end(mie::MieError::unrecoverable_sync_loss(0x40, 1));

    mie::OrderedSource ordered(source, 4096);
    std::vector<uint64_t> emitted;
    mie::MieMessage message;
    bool threw = false;
    try {
        while (ordered.next(message)) {
            emitted.push_back(message.file_offset);
        }
    } catch (const mie::MieError& error) {
        threw = true;
        CHECK(error.kind() == mie::KIND_UNRECOVERABLE_SYNC_LOSS);
    }

    CHECK(threw);
    // Both rows arrived, and in canonical order, BEFORE the failure.
    CHECK(emitted == seq(1, 0));
}

TEST_CASE("an error with an empty buffer passes straight through", "[order]") {
    // A named local: `VectorSource source(std::vector<T>())` declares a
    // function, not an object.
    const std::vector<mie::MieMessage> nothing;
    VectorSource source(nothing);
    source.throw_at_end(mie::MieError::payload_error(0x10, "planted"));
    mie::OrderedSource ordered(source, 4096);
    mie::MieMessage message;
    CHECK_THROWS_AS(ordered.next(message), mie::MieError);
}

TEST_CASE("the stream ends after the deferred error is raised", "[order]") {
    std::vector<mie::MieMessage> input;
    input.push_back(at(100, 9, 1, false));
    VectorSource source(tagged(input));
    source.throw_at_end(mie::MieError::payload_error(0x10, "planted"));

    mie::OrderedSource ordered(source, 4096);
    mie::MieMessage message;
    CHECK(ordered.next(message));  // the flushed row
    CHECK_THROWS_AS(ordered.next(message), mie::MieError);
    // The error is raised ONCE; a caller that catches and resumes sees the end
    // of the stream rather than the same failure forever.
    CHECK_FALSE(ordered.next(message));
}
