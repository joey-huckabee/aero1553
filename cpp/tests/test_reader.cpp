// SPDX-License-Identifier: Apache-2.0
//
// Tests for the memory-mapped reader.
//
// The reader is where the format stops being bits and starts being decisions,
// so most of what is worth pinning here is a decision rather than an arithmetic
// result:
//
//   * a null Type Word at offset 0 is an EMPTY RECORDING (exit 0, header-only
//     CSV), while the same file with any other lead word is a wrong-file
//     rejection. One byte separates the two, and they have different exit codes
//   * the 0x2000 vs 0x2001 spurious classification depends on the PREVIOUS
//     record, which is why it can only happen here
//   * mid-stream validation runs at depth 1 while entry and recovery run at the
//     configured look-ahead. Using the configured depth mid-stream silently
//     drops the last good record before every corrupt region
//   * DELTA is ABSENT, not zero, when the clock went backwards -- and absent on
//     an uncalibrated Standard counter, which has no seconds to report
//   * an error is terminal: after it is thrown the walk stays closed
//
// Fixtures are built field by field through named helpers rather than by
// hand-shifted literals. That is a lesson from the sync suite: a literal like
// `0x7160 | 30` looks like it sets a data word count and does not, because
// those bits were already set, and the test then asserts the wrong thing while
// passing.

#include "mie/reader.hpp"

#include <catch2/catch.hpp>

#include <cstdio>
#include <cstdlib>
#include <sstream>
#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/log.hpp"
#include "mie/platform.hpp"
#include "record_fixtures.hpp"
#include "temp_path.hpp"

namespace {

using mie_test::LogCapture;

// ---------------------------------------------------------------------------
// Wire-format builders
// ---------------------------------------------------------------------------
//
// The primitives live in record_fixtures.hpp, shared with the other suites that
// build records on the wire. Named individually rather than pulled in wholesale
// so this list says exactly what is shared -- and so a name added to the header
// cannot quietly shadow something local here.
//
// The builders BELOW are the ones only this suite needs: an RT-to-RT
// transaction, a SPURIOUS_DATA record, an errored record with its Error Word,
// and the Standard-timestamp variant. They stay local because a fixture with
// one caller is not shared code, it is this suite's own setup.

using mie_test::bc_to_rt;
using mie_test::command_word;
using mie_test::le_bytes;
using mie_test::payload_base;
using mie_test::push_irig;
using mie_test::push_standard;
using mie_test::rt_to_bc;
using mie_test::status_word;
using mie_test::type_word;

/// RT-to-RT: Type, IRIG, Cmd1(Transmit), Cmd2(Receive), TxStatus, data...,
/// RxStatus.
std::vector<uint16_t> rt_to_rt(uint8_t tx_rt, uint8_t rx_rt, uint8_t data_words,
                               uint32_t microsecond = 0) {
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_RT_TO_RT, static_cast<uint16_t>(8 + data_words)));
    push_irig(words, microsecond);
    words.push_back(command_word(tx_rt, mie::DIRECTION_TRANSMIT, 5, data_words));
    words.push_back(command_word(rx_rt, mie::DIRECTION_RECEIVE, 7, data_words));
    words.push_back(status_word(tx_rt));
    for (uint8_t i = 0; i < data_words; ++i) {
        words.push_back(static_cast<uint16_t>(payload_base(0x3300, microsecond) + i));
    }
    words.push_back(status_word(rx_rt));
    return words;
}

/// SPURIOUS_DATA: Type, IRIG, and `payload` raw words. No Command Word at all,
/// which is exactly why it has no RT, no MSG and no DELTA.
std::vector<uint16_t> spurious(uint8_t payload, uint32_t microsecond = 0) {
    const uint16_t fill = payload_base(0xAA00, microsecond);
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_SPURIOUS_DATA, static_cast<uint16_t>(4 + payload)));
    push_irig(words, microsecond);
    for (uint8_t i = 0; i < payload; ++i) {
        words.push_back(static_cast<uint16_t>(fill + i));
    }
    return words;
}

/// An errored record: Type(bit 14), IRIG, Cmd, truncated payload, Error Word.
std::vector<uint16_t> errored(uint8_t rt, uint8_t subaddress, uint8_t payload, uint16_t code,
                              uint32_t microsecond = 0) {
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, static_cast<uint16_t>(6 + payload),
                              /*error=*/true));
    push_irig(words, microsecond);
    words.push_back(command_word(rt, mie::DIRECTION_RECEIVE, subaddress, 4));
    for (uint8_t i = 0; i < payload; ++i) {
        words.push_back(static_cast<uint16_t>(payload_base(0xBB00, microsecond) + i));
    }
    words.push_back(code);
    return words;
}

/// An errored record that ends AT its Command Word: Type(bit 14), timestamp,
/// Cmd, and `tail` (nothing, or an Error Word alone). With no tail it has no
/// Error Word, and the record's last word is the Command Word.
///
/// The Command Word is 0x0120 -- RT0 SA9 R, and also the known DDC code
/// ERROR_NO_RESPONSE -- so reading it as the Error Word passes the known-code
/// check silently, in strict mode too. That is what kept the defect hidden.
std::vector<uint16_t> errored_ending_at_command(bool standard, uint32_t at,
                                                const std::vector<uint16_t>& tail) {
    const uint16_t ts_words = standard ? 2 : 3;
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT,
                              static_cast<uint16_t>(1 + ts_words + 1 + tail.size()),
                              /*error=*/true));
    if (standard) {
        push_standard(words, at);
    } else {
        push_irig(words, at);
    }
    words.push_back(0x0120);
    words.insert(words.end(), tail.begin(), tail.end());
    return words;
}

/// A Standard-timestamp BC-to-RT record: Type, 2 counter words, Cmd, data,
/// Status.
std::vector<uint16_t> bc_to_rt_standard(uint8_t rt, uint8_t subaddress, uint8_t data_words,
                                        uint32_t ticks) {
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, static_cast<uint16_t>(5 + data_words)));
    push_standard(words, ticks);
    words.push_back(command_word(rt, mie::DIRECTION_RECEIVE, subaddress, data_words));
    for (uint8_t i = 0; i < data_words; ++i) {
        words.push_back(static_cast<uint16_t>(payload_base(0x4400, ticks) + i));
    }
    words.push_back(status_word(rt));
    return words;
}

std::vector<uint16_t>& operator+=(std::vector<uint16_t>& a, const std::vector<uint16_t>& b) {
    a.insert(a.end(), b.begin(), b.end());
    return a;
}

// ---------------------------------------------------------------------------
// On-disk fixtures
// ---------------------------------------------------------------------------

/// Everything one walk produced: the messages, and how it ended.
struct Walk {
    std::vector<mie::MieMessage> messages;
    bool threw;
    mie::MieErrorKind kind;
    std::string error_message;
    uint64_t sync_losses;
    bool empty_recording;
    /// Whether next() answered false after the throw, which is the property
    /// that makes an error terminal rather than merely reported.
    bool closed_after_throw;

    Walk()
        : messages(),
          threw(false),
          kind(mie::KIND_FILE_IO),
          error_message(),
          sync_losses(0),
          empty_recording(false),
          closed_after_throw(false) {}
};

Walk walk_file(const std::string& path, const mie::ReaderOptions& options) {
    Walk result;
    mie::MieFileReader reader;
    reader.open(path, options);

    mie::RecordIter it = reader.iter();
    mie::MieMessage message;
    try {
        while (it.next(message)) {
            result.messages.push_back(message);
        }
    } catch (const mie::MieError& error) {
        result.threw = true;
        result.kind = error.kind();
        result.error_message = error.message();
        result.closed_after_throw = !it.next(message);
    }
    result.sync_losses = reader.sync_losses();
    result.empty_recording = reader.empty_recording();
    return result;
}

Walk walk_words(const std::vector<uint16_t>& words, const mie::ReaderOptions& options,
                const std::string& name = "a.b.c.d.MUX0.mie") {
    const mie_test::TempFile fixture(name, le_bytes(words));
    return walk_file(fixture.str(), options);
}

Walk walk_words(const std::vector<uint16_t>& words) {
    return walk_words(words, mie::ReaderOptions());
}

/// Strict mode with the timestamp format PINNED.
///
/// Pinning matters: under AUTO, strict mode also rejects an ambiguous
/// auto-detection (L2-DEC-016), and a short fixture built to exercise some
/// other rule scores badly enough to trip that instead. The test would then
/// fail for a reason it was not written to check, or -- worse -- pass while
/// exercising the wrong branch. Ambiguity under strict gets its own case below.
mie::ReaderOptions strict_options() {
    mie::ReaderOptions options;
    options.strict = true;
    options.input_time_format = mie::TIMESTAMP_IRIG;
    return options;
}

/// Opening throws for the whole-file rejections, so those cases need their own
/// probe rather than walk_file's.
mie::MieErrorKind open_error_kind(const std::string& path, const mie::ReaderOptions& options) {
    mie::MieFileReader reader;
    try {
        reader.open(path, options);
    } catch (const mie::MieError& error) {
        return error.kind();
    }
    return mie::KIND_FILE_IO;
}

}  // namespace

// ---------------------------------------------------------------------------
// Opening
// ---------------------------------------------------------------------------

TEST_CASE("a missing path is FileNotFound", "[reader][L2-RDR-005]") {
    const std::string missing = mie_test::TempPath("does-not-exist.mie").str();
    mie::MieFileReader reader;
    REQUIRE_THROWS_AS(reader.open(missing, mie::ReaderOptions()), mie::MieError);
    CHECK(open_error_kind(missing, mie::ReaderOptions()) == mie::KIND_FILE_NOT_FOUND);
}

TEST_CASE("a zero-byte file is FileEmpty, not a decode failure", "[reader][L2-RDR-006]") {
    const mie_test::TempFile fixture("empty.mie", std::vector<uint8_t>());
    CHECK(open_error_kind(fixture.str(), mie::ReaderOptions()) == mie::KIND_FILE_EMPTY);
}

TEST_CASE("a directory reports I/O, not FileEmpty", "[reader][L2-RDR-006]") {
    // The Windows trap: a directory reports a zero size through the metadata
    // call, so a size-first check would call it an empty RECORDING and send the
    // operator looking for a capture problem instead of a typo. On POSIX the
    // mapping fails for its own reasons and lands in the same place.
    const mie::MieErrorKind kind = open_error_kind(mie_test::temp_root(), mie::ReaderOptions());
    CHECK(kind == mie::KIND_FILE_IO);
    CHECK(kind != mie::KIND_FILE_EMPTY);
}

TEST_CASE("an opened reader reports its path and size", "[reader]") {
    const std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    const mie_test::TempFile fixture("sized.mie", le_bytes(words));

    mie::MieFileReader reader;
    reader.open(fixture.str(), mie::ReaderOptions());
    CHECK(reader.path() == fixture.str());
    CHECK(reader.file_size() == words.size() * 2);
    CHECK(reader.sync_losses() == 0);
    CHECK_FALSE(reader.empty_recording());
}

// ---------------------------------------------------------------------------
// Finding the first record
// ---------------------------------------------------------------------------

TEST_CASE("records starting at offset 0 need no header skip", "[reader][L2-SYN-006]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words += bc_to_rt(3, 5, 2, 1000);

    const Walk walk = walk_words(words);
    CHECK_FALSE(walk.threw);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].file_offset == 0);
    // Each record is 8 words, so the second starts 16 bytes in.
    CHECK(walk.messages[1].file_offset == 16);
}

TEST_CASE("leading bytes before the first record are skipped", "[reader][L2-SYN-006][L2-SYN-012]") {
    // Four words of junk that cannot start a record, then two real ones. The
    // scan walks a 2-byte grid, so the recovered offset is exact.
    std::vector<uint16_t> words;
    words.push_back(0x0099);
    words.push_back(0x0099);
    words.push_back(0x0099);
    words.push_back(0x0099);
    const std::size_t header_words = words.size();
    words += bc_to_rt(9, 1, 3);
    words += bc_to_rt(9, 1, 3, 500);

    const Walk walk = walk_words(words);
    CHECK_FALSE(walk.threw);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].file_offset == header_words * 2);
}

TEST_CASE("a stream that opens on the terminator is an empty recording",
          "[reader][L1-EXIT-010][L2-RDR-021]") {
    // One byte of difference from the wrong-file case below, and a different
    // exit code: this is a real MIE recording that captured nothing, so the
    // CLI writes a header-only CSV and exits 0.
    std::vector<uint16_t> words;
    words.push_back(0x0000);
    words.push_back(0x0000);
    words.push_back(0x0000);
    words.push_back(0x0000);
    words.push_back(0x0000);
    words.push_back(0x0000);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words);
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.empty());
    CHECK(walk.empty_recording);
    CHECK(capture.contains("empty capture"));
}

TEST_CASE("a file with no MIE records at all is rejected", "[reader][L1-EXIT-002][L2-SYN-008]") {
    std::vector<uint8_t> bytes;
    for (std::size_t i = 0; i < 400; ++i) {
        bytes.push_back(static_cast<uint8_t>(0x99));
        bytes.push_back(static_cast<uint8_t>(0x77));
    }
    const mie_test::TempFile fixture("wrong.jpg", bytes);

    const Walk walk = walk_file(fixture.str(), mie::ReaderOptions());
    CHECK(walk.threw);
    CHECK(walk.kind == mie::KIND_NO_VALID_RECORDS);
    CHECK_FALSE(walk.empty_recording);
    // A rejection, not an empty stream: a caller that only looked at the
    // message count would write an empty CSV and exit 0 for a JPEG.
    CHECK(walk.messages.empty());
}

TEST_CASE("the construction-time rejection closes the walk after one throw",
          "[reader][L3-CPP-013]") {
    std::vector<uint8_t> bytes;
    for (std::size_t i = 0; i < 400; ++i) {
        bytes.push_back(static_cast<uint8_t>(0x99));
        bytes.push_back(static_cast<uint8_t>(0x77));
    }
    const mie_test::TempFile fixture("wrong.bin", bytes);

    const Walk walk = walk_file(fixture.str(), mie::ReaderOptions());
    REQUIRE(walk.threw);
    CHECK(walk.closed_after_throw);
}

TEST_CASE("a truncated first record is a rejection in both modes, with distinct classes",
          "[reader][L2-RDR-004]") {
    // A valid Type Word declaring 8 words, with only 5 words of file behind it.
    // The distinction matters to an operator: this means the recording was cut
    // short, where NoValidRecords means the wrong file was passed.
    std::vector<uint16_t> words;
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
    push_irig(words);
    words.push_back(command_word(4, mie::DIRECTION_RECEIVE, 2, 1));

    SECTION("strict rejects") {
        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_FIRST_RECORD_TRUNCATED);
    }

    SECTION("lenient rejects as no valid records, naming the truncation") {
        // Not a clean stop. That was the v2.12.0 route by which a non-MIE input
        // exited 0 with a header-only CSV, and this section once pinned it.
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, mie::ReaderOptions());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_NO_VALID_RECORDS);
        CHECK(walk.messages.empty());
        CHECK_FALSE(walk.empty_recording);
        CHECK(capture.contains("truncated"));
    }
}

TEST_CASE("a single-byte fill pattern is rejected as a homogeneous payload",
          "[reader][L2-SYN-018]") {
    // 0x20 fill is the motivating case: `0x20 0x20` parses as a valid
    // SPURIOUS_DATA Type Word, and look-ahead alone admits the whole stream
    // happily, because every "record" is as valid as the one before it.
    const std::vector<uint8_t> bytes(1024, 0x20);
    const mie_test::TempFile fixture("pad.mie", bytes);

    const LogCapture capture(mie::log::LEVEL_ERROR);
    const Walk walk = walk_file(fixture.str(), mie::ReaderOptions());
    CHECK(walk.threw);
    CHECK(walk.kind == mie::KIND_HOMOGENEOUS_PAYLOAD);
    CHECK(capture.contains("homogeneous-payload"));
}

// ---------------------------------------------------------------------------
// Timestamp format resolution
// ---------------------------------------------------------------------------

TEST_CASE("IRIG is auto-detected from the leading records", "[reader][L2-DEC-015]") {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 6; ++i) {
        words += bc_to_rt(3, 5, 2, i * 1000);
    }

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 6);
    CHECK(walk.messages[0].timestamp.is_irig());
    CHECK(walk.messages[0].timestamp.irig.day == 10);
    CHECK(walk.messages[0].timestamp.irig.hour == 15);
    CHECK(walk.messages[0].timestamp.irig.minute == 54);
    CHECK(walk.messages[0].timestamp.irig.second == 50);
    CHECK(walk.messages[3].timestamp.irig.microsecond == 3000);
}

TEST_CASE("a forced format is honoured", "[reader][L2-DEC-013]") {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 5; ++i) {
        words += bc_to_rt_standard(6, 4, 2, 1000 + i * 10);
    }

    mie::ReaderOptions options;
    options.input_time_format = mie::TIMESTAMP_STANDARD;
    const Walk walk = walk_words(words, options);
    REQUIRE(walk.messages.size() == 5);
    CHECK(walk.messages[0].timestamp.is_standard());
    CHECK(walk.messages[0].timestamp.standard.raw_ticks() == 1000);
    CHECK(walk.messages[4].timestamp.standard.raw_ticks() == 1040);
}

TEST_CASE("a forced format that the recording contradicts is kept, with a warning",
          "[reader][L2-DEC-013]") {
    // Eight clean IRIG records make detection decisive for IRIG. Forcing
    // Standard is then a contradiction -- and the forced choice still wins,
    // because the operator asked for it. Only strict mode refuses.
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 8; ++i) {
        words += bc_to_rt(3, 5, 4, i * 1000);
    }

    mie::ReaderOptions options;
    options.input_time_format = mie::TIMESTAMP_STANDARD;

    SECTION("lenient decodes with the forced format") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, options);
        CHECK_FALSE(walk.threw);
        if (!walk.messages.empty()) {
            CHECK(walk.messages[0].timestamp.is_standard());
        }
        CHECK(capture.contains("contradicts the recording"));
    }

    SECTION("strict rejects the mismatch") {
        options.strict = true;
        const Walk walk = walk_words(words, options);
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_TIMESTAMP_FORMAT_MISMATCH);
    }
}

TEST_CASE("an ambiguous auto-detection is rejected in strict mode and guessed in lenient",
          "[reader][L2-DEC-016]") {
    // Three RT-to-RT records: too few, and too unlike the shapes the probe
    // scores confidently, for either format to clear the floor by the required
    // margin. Lenient mode takes the best guess and says so in a WARN; strict
    // refuses to guess, because a wrong guess reinterprets every timestamp in
    // the file rather than failing visibly.
    std::vector<uint16_t> words = rt_to_rt(4, 11, 2, 0);
    words += rt_to_rt(4, 11, 2, 100);
    words += rt_to_rt(4, 11, 2, 200);

    SECTION("strict rejects") {
        mie::ReaderOptions options;
        options.strict = true;  // input_time_format stays AUTO, which is the point
        const Walk walk = walk_words(words, options);
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_TIMESTAMP_FORMAT_MISMATCH);
    }

    SECTION("lenient proceeds with a warning") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, mie::ReaderOptions());
        CHECK_FALSE(walk.threw);
        CHECK(walk.messages.size() == 3);
        CHECK(capture.contains("Ambiguous"));
    }
}

// ---------------------------------------------------------------------------
// Normal record decoding, per format
// ---------------------------------------------------------------------------

TEST_CASE("a BC-to-RT record decodes payload then status", "[reader][L2-RDR-007]") {
    std::vector<uint16_t> words = bc_to_rt(9, 12, 3, 0);
    words += bc_to_rt(9, 12, 3, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    const mie::MieMessage& m = walk.messages[0];

    CHECK(m.message_format == mie::FORMAT_RECEIVE);
    REQUIRE(m.command_word.has_value());
    CHECK(m.command_word.value().rt == 9);
    CHECK(m.command_word.value().subaddress == 12);
    CHECK(m.command_word.value().direction == mie::DIRECTION_RECEIVE);
    CHECK(m.command_word.value().data_word_count == 3);
    REQUIRE(m.data_words.size() == 3);
    CHECK(m.data_words[0] == 0x1100);
    CHECK(m.data_words[2] == 0x1102);
    REQUIRE(m.status_word.has_value());
    CHECK(m.status_word.value() == status_word(9));
    CHECK_FALSE(m.status_word_2.has_value());
    CHECK_FALSE(m.command_word_2.has_value());
    CHECK_FALSE(m.error_word.has_value());
    CHECK_FALSE(m.is_error());
    CHECK_FALSE(m.is_spurious());
    CHECK(m.msg_label() == "12R");
}

TEST_CASE("an RT-to-BC record decodes status before payload", "[reader][L2-RDR-008]") {
    // Same word count, different order. Getting this backwards produces a
    // status word that looks like data and a first data word that looks like a
    // status -- both plausible, neither flagged by anything downstream.
    std::vector<uint16_t> words = rt_to_bc(7, 3, 2, 0);
    words += rt_to_bc(7, 3, 2, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    const mie::MieMessage& m = walk.messages[0];

    CHECK(m.message_format == mie::FORMAT_TRANSMIT);
    REQUIRE(m.status_word.has_value());
    CHECK(m.status_word.value() == status_word(7));
    REQUIRE(m.data_words.size() == 2);
    CHECK(m.data_words[0] == 0x2200);
    CHECK(m.data_words[1] == 0x2201);
    CHECK(m.msg_label() == "3T");
}

TEST_CASE("an RT-to-RT record decodes both commands and both status words",
          "[reader][L2-MSG-001]") {
    std::vector<uint16_t> words = rt_to_rt(4, 11, 2);
    words += rt_to_rt(4, 11, 2, 200);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    const mie::MieMessage& m = walk.messages[0];

    CHECK(m.message_format == mie::FORMAT_RT_TO_RT);
    REQUIRE(m.command_word.has_value());
    CHECK(m.command_word.value().rt == 4);
    CHECK(m.command_word.value().direction == mie::DIRECTION_TRANSMIT);
    REQUIRE(m.command_word_2.has_value());
    CHECK(m.command_word_2.value().rt == 11);
    CHECK(m.command_word_2.value().direction == mie::DIRECTION_RECEIVE);
    REQUIRE(m.status_word.has_value());
    CHECK(m.status_word.value() == status_word(4));
    REQUIRE(m.status_word_2.has_value());
    CHECK(m.status_word_2.value() == status_word(11));
    REQUIRE(m.data_words.size() == 2);
    CHECK(m.data_words[0] == 0x3300);
}

TEST_CASE("a mode-code record with no data word carries only a status", "[reader][L2-MSG-004]") {
    // Type + IRIG + ModeCmd + Status = 6 words, which is one short of the
    // with-data shape and therefore classifies as ModeCodeNoData.
    std::vector<uint16_t> record;
    record.push_back(type_word(mie::MESSAGE_TYPE_MODE_COMMAND, 6));
    push_irig(record);
    record.push_back(command_word(8, mie::DIRECTION_TRANSMIT, 0, 2));
    record.push_back(status_word(8));

    std::vector<uint16_t> words = record;
    words += record;

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].message_format == mie::FORMAT_MODE_CODE_NO_DATA);
    REQUIRE(walk.messages[0].status_word.has_value());
    CHECK(walk.messages[0].status_word.value() == status_word(8));
    CHECK(walk.messages[0].data_words.empty());
}

TEST_CASE("a transmit mode-code record with a data word puts status first",
          "[reader][L2-MSG-004]") {
    std::vector<uint16_t> record;
    record.push_back(type_word(mie::MESSAGE_TYPE_MODE_COMMAND, 7));
    push_irig(record);
    record.push_back(command_word(8, mie::DIRECTION_TRANSMIT, 31, 2));
    record.push_back(status_word(8));
    record.push_back(0xC0DE);

    std::vector<uint16_t> words = record;
    words += record;

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].message_format == mie::FORMAT_MODE_CODE_TX_DATA);
    REQUIRE(walk.messages[0].status_word.has_value());
    CHECK(walk.messages[0].status_word.value() == status_word(8));
    REQUIRE(walk.messages[0].data_words.size() == 1);
    CHECK(walk.messages[0].data_words[0] == 0xC0DE);
}

TEST_CASE("a receive mode-code record puts the data word first", "[reader][L2-MSG-004]") {
    // The mirror image of the case above, and the reason both exist: the two
    // differ only in the direction bit, and swapping the two payload words
    // would produce a status that parses and a data word that looks fine.
    std::vector<uint16_t> record;
    record.push_back(type_word(mie::MESSAGE_TYPE_MODE_COMMAND, 7));
    push_irig(record);
    record.push_back(command_word(8, mie::DIRECTION_RECEIVE, 31, 2));
    record.push_back(0xD00D);
    record.push_back(status_word(8));

    std::vector<uint16_t> words = record;
    words += record;

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].message_format == mie::FORMAT_MODE_CODE_RX_DATA);
    REQUIRE(walk.messages[0].data_words.size() == 1);
    CHECK(walk.messages[0].data_words[0] == 0xD00D);
    REQUIRE(walk.messages[0].status_word.has_value());
    CHECK(walk.messages[0].status_word.value() == status_word(8));
}

TEST_CASE("a broadcast receive record carries data and no status", "[reader][L2-MSG-001]") {
    std::vector<uint16_t> record;
    record.push_back(type_word(mie::MESSAGE_TYPE_BROADCAST_BC_TO_RT, 7));
    push_irig(record);
    record.push_back(command_word(31, mie::DIRECTION_RECEIVE, 6, 2));
    record.push_back(0x5A5A);
    record.push_back(0x6B6B);

    std::vector<uint16_t> words = record;
    words += record;

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].message_format == mie::FORMAT_RECEIVE_BROADCAST);
    REQUIRE(walk.messages[0].data_words.size() == 2);
    CHECK(walk.messages[0].data_words[1] == 0x6B6B);
    CHECK_FALSE(walk.messages[0].status_word.has_value());
}

TEST_CASE("a 32-data-word record decodes at the bus standard's cap", "[reader][L2-DEC-004]") {
    // The Command Word's count field is five bits and a raw 0 means 32, so this
    // is both the largest legal payload and the encoding most likely to be read
    // as "no data words".
    std::vector<uint16_t> words = bc_to_rt(2, 1, 32, 0);
    words += bc_to_rt(2, 1, 32, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    REQUIRE(walk.messages[0].command_word.has_value());
    CHECK(walk.messages[0].command_word.value().data_word_count == 32);
    REQUIRE(walk.messages[0].data_words.size() == 32);
    CHECK(walk.messages[0].data_words[31] == 0x111F);
}

TEST_CASE("bus B is decoded from Type Word bit 7", "[reader][L2-DEC-001]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words[0] = type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8, false, mie::BUS_B);
    words += bc_to_rt(3, 5, 2, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].bus() == mie::BUS_B);
    CHECK(walk.messages[1].bus() == mie::BUS_A);
}

TEST_CASE("a terminator ends the stream cleanly", "[reader][L2-RDR-021]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words += bc_to_rt(3, 5, 2, 100);
    words.push_back(0x0000);
    // Words after the terminator must not be decoded, which is what makes this
    // different from simply running out of file.
    words += bc_to_rt(3, 5, 2, 200);

    const Walk walk = walk_words(words);
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 2);
    CHECK_FALSE(walk.empty_recording);
}

// ---------------------------------------------------------------------------
// Errored records
// ---------------------------------------------------------------------------

TEST_CASE("an errored record carries its DDC code and truncated payload",
          "[reader][L2-ERR-001][L2-ERR-002]") {
    std::vector<uint16_t> words = errored(6, 2, 2, mie::ERROR_MANCHESTER_PARITY);
    words += bc_to_rt(6, 2, 2, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    const mie::MieMessage& m = walk.messages[0];

    CHECK(m.is_error());
    CHECK(m.type_word.error);
    REQUIRE(m.error_word.has_value());
    CHECK(m.error_word.value() == mie::ERROR_MANCHESTER_PARITY);
    // Payload = word_count - Type - timestamp - Cmd - ErrorWord.
    REQUIRE(m.data_words.size() == 2);
    CHECK(m.data_words[0] == 0xBB00);
    CHECK(m.error_label() == std::string("ERROR"));
}

TEST_CASE("an unknown DDC error code warns in lenient mode and rejects in strict",
          "[reader][L2-ERR-003][L2-ERR-004]") {
    std::vector<uint16_t> words = errored(6, 2, 2, 0x0199);
    words += bc_to_rt(6, 2, 2, 100);

    SECTION("lenient keeps the record and warns") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, mie::ReaderOptions());
        CHECK_FALSE(walk.threw);
        REQUIRE(walk.messages.size() == 2);
        REQUIRE(walk.messages[0].error_word.has_value());
        // The code is preserved verbatim. Substituting a known code, or
        // dropping it, would lose the only evidence of what the card actually
        // reported.
        CHECK(walk.messages[0].error_word.value() == 0x0199);
        CHECK(capture.contains("unknown DDC error code"));
    }

    SECTION("strict rejects") {
        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_UNKNOWN_ERROR_CODE);
        CHECK(walk.closed_after_throw);
    }
}

TEST_CASE("an errored record with no payload words still decodes", "[reader][L2-ERR-002]") {
    std::vector<uint16_t> words = errored(6, 2, 0, mie::ERROR_NO_RESPONSE);
    words += bc_to_rt(6, 2, 2, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 2);
    CHECK(walk.messages[0].data_words.empty());
    REQUIRE(walk.messages[0].error_word.has_value());
    CHECK(walk.messages[0].error_word.value() == mie::ERROR_NO_RESPONSE);
}

TEST_CASE("an errored record with no room for an Error Word is malformed", "[reader][L2-ERR-002]") {
    // Sync validation admits a record that ends at its Command Word; bit 14
    // does not raise that minimum. Reading such a record's "last word" as the
    // Error Word read the Command Word. Both formats, because the minimum is
    // format-dependent: 5 words for IRIG, 4 for Standard.
    const bool standard = GENERATE(false, true);
    INFO((standard ? "Standard" : "IRIG"));

    mie::ReaderOptions lenient;
    lenient.input_time_format = standard ? mie::TIMESTAMP_STANDARD : mie::TIMESTAMP_IRIG;
    mie::ReaderOptions strict = lenient;
    strict.strict = true;

    const std::vector<uint16_t> no_tail;
    std::vector<uint16_t> words = standard ? bc_to_rt_standard(6, 2, 2, 0) : bc_to_rt(6, 2, 2, 0);
    std::vector<uint16_t> with_error_word = words;
    words += errored_ending_at_command(standard, 100, no_tail);
    with_error_word += errored_ending_at_command(
        standard, 100, std::vector<uint16_t>(1, mie::ERROR_MANCHESTER_PARITY));
    const std::vector<uint16_t> clean =
        standard ? bc_to_rt_standard(6, 2, 2, 200) : bc_to_rt(6, 2, 2, 200);
    words += clean;
    with_error_word += clean;

    SECTION("strict rejects it as a payload error naming the missing word") {
        const Walk walk = walk_words(words, strict);
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_PAYLOAD_ERROR);
        CHECK(walk.error_message.find("errored record has no Error Word") != std::string::npos);
        // The leading clean record came out first, so the failure is this
        // record's, not a sync rejection of the file.
        CHECK(walk.messages.size() == 1);
        CHECK(walk.closed_after_throw);
    }

    SECTION("lenient skips it with a warning and keeps decoding") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, lenient);
        CHECK_FALSE(walk.threw);
        REQUIRE(walk.messages.size() == 2);
        for (std::size_t i = 0; i < walk.messages.size(); ++i) {
            CHECK_FALSE(walk.messages[i].is_error());
            CHECK_FALSE(walk.messages[i].error_word.has_value());
        }
        CHECK(capture.contains("errored record has no Error Word"));
    }

    SECTION("one word more holds the Error Word, and decodes with its real code") {
        const Walk walk = walk_words(with_error_word, strict);
        CHECK_FALSE(walk.threw);
        REQUIRE(walk.messages.size() == 3);
        CHECK(walk.messages[1].is_error());
        REQUIRE(walk.messages[1].error_word.has_value());
        CHECK(walk.messages[1].error_word.value() == mie::ERROR_MANCHESTER_PARITY);
        CHECK(walk.messages[1].data_words.empty());
    }
}

TEST_CASE("a skipped errored record leaves no parent for a spurious one",
          "[reader][L2-ERR-002][L2-ERR-006]") {
    // Well-formed error, then one with no Error Word, then SPURIOUS_DATA. The
    // skip must clear the errored-predecessor state, or the spurious record
    // becomes a 0x2000 continuation of the error two records back.
    const std::vector<uint16_t> no_tail;
    std::vector<uint16_t> words = bc_to_rt(6, 2, 2, 0);
    words += errored(6, 2, 0, mie::ERROR_MANCHESTER_PARITY, 100);
    words += errored_ending_at_command(false, 200, no_tail);
    words += spurious(2, 300);
    words += bc_to_rt(6, 2, 2, 400);

    mie::ReaderOptions lenient;
    lenient.input_time_format = mie::TIMESTAMP_IRIG;
    const Walk walk = walk_words(words, lenient);
    CHECK_FALSE(walk.threw);
    REQUIRE(walk.messages.size() == 4);
    REQUIRE(walk.messages[1].error_word.has_value());
    CHECK(walk.messages[1].error_word.value() == mie::ERROR_MANCHESTER_PARITY);
    REQUIRE(walk.messages[2].error_word.has_value());
    CHECK(walk.messages[2].error_word.value() == mie::ERROR_SPURIOUS_STANDALONE);
}

// ---------------------------------------------------------------------------
// SPURIOUS_DATA classification -- the reason this lives in the reader
// ---------------------------------------------------------------------------

TEST_CASE("a spurious record after an errored one is a continuation",
          "[reader][L2-ERR-005][L2-SYN-017]") {
    std::vector<uint16_t> words = errored(6, 2, 1, mie::ERROR_TOO_MANY_WORDS);
    words += spurious(3);
    words += bc_to_rt(6, 2, 2, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    const mie::MieMessage& sp = walk.messages[1];

    CHECK(sp.is_spurious());
    CHECK(sp.message_format == mie::FORMAT_SPURIOUS_DATA);
    REQUIRE(sp.error_word.has_value());
    CHECK(sp.error_word.value() == mie::ERROR_SPURIOUS_CONTINUATION);
    CHECK_FALSE(sp.command_word.has_value());
    CHECK(sp.msg_label().empty());
    REQUIRE(sp.data_words.size() == 3);
    CHECK(sp.data_words[0] == 0xAA00);
    CHECK(sp.error_label() == std::string("SPURIOUS"));
}

TEST_CASE("a continuation takes its errored parent's timestamp", "[reader][L2-ERR-005]") {
    // The card stamped the leftover words 300 us after the error. The
    // continuation is part of the error's transaction, so it is reported at the
    // error's time -- which is what keeps the two rows adjacent through the
    // order stage and a merge. The record after it keeps its own time.
    std::vector<uint16_t> words = errored(6, 2, 1, mie::ERROR_TOO_MANY_WORDS, 100);
    words += spurious(3, 400);
    words += bc_to_rt(6, 2, 2, 900);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    REQUIRE(walk.messages[1].error_word.has_value());
    CHECK(walk.messages[1].error_word.value() == mie::ERROR_SPURIOUS_CONTINUATION);
    CHECK(walk.messages[1].timestamp.irig.microsecond == 100);
    CHECK(walk.messages[0].timestamp.irig.microsecond == 100);
    CHECK(walk.messages[2].timestamp.irig.microsecond == 900);
}

TEST_CASE("a standalone spurious record keeps its own timestamp", "[reader][L2-ERR-006]") {
    std::vector<uint16_t> words = bc_to_rt(6, 2, 2, 100);
    words += spurious(2, 400);
    words += bc_to_rt(6, 2, 2, 900);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    CHECK(walk.messages[1].error_word.value() == mie::ERROR_SPURIOUS_STANDALONE);
    CHECK(walk.messages[1].timestamp.irig.microsecond == 400);
}

TEST_CASE("only the continuation is re-timed, not a second spurious record",
          "[reader][L2-ERR-005][L2-ERR-006]") {
    std::vector<uint16_t> words = errored(6, 2, 1, mie::ERROR_INVERTED_SYNC, 100);
    words += spurious(2, 400);
    words += spurious(2, 700);
    words += bc_to_rt(6, 2, 2, 900);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 4);
    CHECK(walk.messages[1].timestamp.irig.microsecond == 100);  // continuation
    CHECK(walk.messages[2].error_word.value() == mie::ERROR_SPURIOUS_STANDALONE);
    CHECK(walk.messages[2].timestamp.irig.microsecond == 700);  // its own time
}

TEST_CASE("a continuation takes its parent's Standard timestamp too", "[reader][L2-ERR-005]") {
    // Standard records carry a two-word counter instead of three IRIG words.
    std::vector<uint16_t> words = bc_to_rt_standard(6, 4, 2, 1000);
    // Errored: Type(bit 14), counter (2), Cmd, one payload word, Error Word = 6.
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 6, /*error=*/true));
    push_standard(words, 2000);
    words.push_back(command_word(6, mie::DIRECTION_RECEIVE, 4, 4));
    words.push_back(0xBB00);
    words.push_back(mie::ERROR_TOO_MANY_WORDS);
    // SPURIOUS_DATA: Type, counter (2), two leftover words = 5 -- stamped later.
    words.push_back(type_word(mie::MESSAGE_TYPE_SPURIOUS_DATA, 5));
    push_standard(words, 2500);
    words.push_back(0xAA00);
    words.push_back(0xAA01);
    words += bc_to_rt_standard(6, 4, 2, 3000);

    mie::ReaderOptions options;
    options.input_time_format = mie::TIMESTAMP_STANDARD;
    const Walk walk = walk_words(words, options);
    REQUIRE(walk.messages.size() == 4);
    REQUIRE(walk.messages[2].error_word.has_value());
    CHECK(walk.messages[2].error_word.value() == mie::ERROR_SPURIOUS_CONTINUATION);
    CHECK(walk.messages[2].timestamp.is_standard());
    CHECK(walk.messages[2].timestamp.standard.raw_ticks() == 2000);
    CHECK(walk.messages[3].timestamp.standard.raw_ticks() == 3000);
}

TEST_CASE("a spurious record with no errored predecessor is standalone", "[reader][L2-ERR-006]") {
    std::vector<uint16_t> words = bc_to_rt(6, 2, 2);
    words += spurious(2);
    words += bc_to_rt(6, 2, 2, 100);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    REQUIRE(walk.messages[1].error_word.has_value());
    CHECK(walk.messages[1].error_word.value() == mie::ERROR_SPURIOUS_STANDALONE);
}

TEST_CASE("only the FIRST spurious record after an error is a continuation",
          "[reader][L2-ERR-005][L2-ERR-006]") {
    // The flag is cleared by the spurious record that consumes it. A second one
    // continues nothing -- the card writes one continuation per errored
    // transaction, so a second would be a separate event.
    std::vector<uint16_t> words = errored(6, 2, 1, mie::ERROR_INVERTED_SYNC);
    words += spurious(2);
    words += spurious(2, 100);
    words += bc_to_rt(6, 2, 2, 200);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 4);
    REQUIRE(walk.messages[1].error_word.has_value());
    REQUIRE(walk.messages[2].error_word.has_value());
    CHECK(walk.messages[1].error_word.value() == mie::ERROR_SPURIOUS_CONTINUATION);
    CHECK(walk.messages[2].error_word.value() == mie::ERROR_SPURIOUS_STANDALONE);
}

TEST_CASE("a clean record between an error and a spurious one breaks the continuation",
          "[reader][L2-ERR-005][L2-ERR-006]") {
    std::vector<uint16_t> words = errored(6, 2, 1, mie::ERROR_NO_RESPONSE);
    words += bc_to_rt(6, 2, 2, 50);
    words += spurious(2, 100);
    words += bc_to_rt(6, 2, 2, 200);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 4);
    REQUIRE(walk.messages[2].error_word.has_value());
    CHECK(walk.messages[2].error_word.value() == mie::ERROR_SPURIOUS_STANDALONE);
}

TEST_CASE("a spurious record never carries DELTA", "[reader][L2-RDR-018]") {
    // It has no Command Word, so it has no RT/MSG key -- there is nothing for a
    // gap to be measured against.
    std::vector<uint16_t> words = spurious(2);
    words += spurious(2, 100);
    words += bc_to_rt(6, 2, 2, 200);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    CHECK_FALSE(walk.messages[0].delta.has_value());
    CHECK_FALSE(walk.messages[1].delta.has_value());
    CHECK(walk.messages[2].delta.has_value());
}

// ---------------------------------------------------------------------------
// DELTA
// ---------------------------------------------------------------------------

TEST_CASE("DELTA is zero on the first record of a key and the gap thereafter",
          "[reader][L2-RDR-009][L2-RDR-010]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 250000);
    words += bc_to_rt(3, 5, 2, 950000);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    REQUIRE(walk.messages[0].delta.has_value());
    CHECK(walk.messages[0].delta.value() == Approx(0.0));
    REQUIRE(walk.messages[1].delta.has_value());
    CHECK(walk.messages[1].delta.value() == Approx(0.25));
    REQUIRE(walk.messages[2].delta.has_value());
    CHECK(walk.messages[2].delta.value() == Approx(0.7));
}

TEST_CASE("DELTA is tracked per RT/MSG key, not globally", "[reader][L2-RDR-009]") {
    // Interleaved traffic from two terminals. A single global previous-time
    // would make every gap the inter-record spacing rather than the per-key
    // period, which reads as plausible and is wrong.
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(9, 1, 2, 100000);
    words += bc_to_rt(3, 5, 2, 200000);
    words += bc_to_rt(9, 1, 2, 300000);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 4);
    CHECK(walk.messages[0].delta.value() == Approx(0.0));
    CHECK(walk.messages[1].delta.value() == Approx(0.0));
    CHECK(walk.messages[2].delta.value() == Approx(0.2));
    CHECK(walk.messages[3].delta.value() == Approx(0.2));
}

TEST_CASE("direction is part of the DELTA key", "[reader][L2-RDR-009]") {
    // Same RT, same subaddress, opposite direction: two different messages on
    // the bus, and therefore two independent periods.
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += rt_to_bc(3, 5, 2, 100000);
    words += bc_to_rt(3, 5, 2, 400000);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    CHECK(walk.messages[0].delta.value() == Approx(0.0));
    CHECK(walk.messages[1].delta.value() == Approx(0.0));
    CHECK(walk.messages[2].delta.value() == Approx(0.4));
}

TEST_CASE("a backwards clock leaves DELTA absent and warns once per key", "[reader][L2-RDR-017]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 500000);
    words += bc_to_rt(3, 5, 2, 100000);
    words += bc_to_rt(3, 5, 2, 50000);
    words += bc_to_rt(3, 5, 2, 600000);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 4);

    CHECK(walk.messages[0].delta.value() == Approx(0.0));
    // Absent, not negative and not clamped to zero: there is no honest gap to
    // report, and a zero would be indistinguishable from a first sighting.
    CHECK_FALSE(walk.messages[1].delta.has_value());
    CHECK_FALSE(walk.messages[2].delta.has_value());
    // The tracker still advanced, so the recovery measures from the last seen
    // time rather than from the high-water mark.
    REQUIRE(walk.messages[3].delta.has_value());
    CHECK(walk.messages[3].delta.value() == Approx(0.55));

    // Two out-of-order records, one warning: a chronically unsorted file would
    // otherwise emit a line per record and bury everything else.
    CHECK(capture.count_containing("non-monotonic timestamp") == 1);

    // The whole line, not a keyword: the text is shared with Rust (L2-RDR-017),
    // and a keyword check passed while Rust's copy carried two 18-space runs
    // from a literal whose line continuations were lost.
    uint64_t prev_us = 0;
    uint64_t curr_us = 0;
    REQUIRE(walk.messages[0].timestamp.to_microseconds(mie::none(), prev_us));
    REQUIRE(walk.messages[1].timestamp.to_microseconds(mie::none(), curr_us));
    std::ostringstream expected;
    expected << "non-monotonic timestamp at 0x" << std::uppercase << std::hex
             << bc_to_rt(3, 5, 2, 500000).size() * 2
             << " for RT/MSG key 0x00030500: prev_us=" << std::dec << prev_us
             << " curr_us=" << curr_us
             << " (further out-of-order occurrences for this key suppressed)";
    CHECK(capture.contains(expected.str()));
}

TEST_CASE("each key gets its own non-monotonic warning", "[reader][L2-RDR-017]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 500000);
    words += bc_to_rt(9, 1, 2, 500000);
    words += bc_to_rt(3, 5, 2, 100000);
    words += bc_to_rt(9, 1, 2, 100000);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 4);
    CHECK(capture.count_containing("non-monotonic timestamp") == 2);
}

TEST_CASE("an uncalibrated Standard counter has no DELTA", "[reader][L2-RDR-019]") {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 5; ++i) {
        words += bc_to_rt_standard(6, 4, 2, 1000 + i * 500);
    }

    mie::ReaderOptions options;
    options.input_time_format = mie::TIMESTAMP_STANDARD;
    const Walk walk = walk_words(words, options);
    REQUIRE(walk.messages.size() == 5);
    for (std::size_t i = 0; i < walk.messages.size(); ++i) {
        // Raw counter ticks have no known rate or epoch, so a gap in SECONDS
        // cannot be computed truthfully. An empty cell is the honest answer.
        CHECK_FALSE(walk.messages[i].delta.has_value());
    }
}

TEST_CASE("a calibrated Standard counter participates in DELTA",
          "[reader][L2-DEC-017][L2-RDR-019]") {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 4; ++i) {
        words += bc_to_rt_standard(6, 4, 2, 1000000 * i);
    }

    mie::ReaderOptions options;
    options.input_time_format = mie::TIMESTAMP_STANDARD;
    options.standard_tick_rate_hz = 1000000.0;  // one tick per microsecond
    const Walk walk = walk_words(words, options);
    REQUIRE(walk.messages.size() == 4);
    CHECK(walk.messages[0].delta.value() == Approx(0.0));
    CHECK(walk.messages[1].delta.value() == Approx(1.0));
    CHECK(walk.messages[3].delta.value() == Approx(1.0));
}

TEST_CASE("errored records take part in DELTA tracking", "[reader][L2-RDR-016]") {
    // They are real traffic on the bus. Excluding them would make the gap after
    // an error look like the gap across it, which is precisely the interval an
    // analyst investigating the error wants to see.
    std::vector<uint16_t> words = bc_to_rt(6, 2, 2, 0);
    words += errored(6, 2, 1, mie::ERROR_NO_RESPONSE, 300000);
    words += bc_to_rt(6, 2, 2, 500000);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    REQUIRE(walk.messages[1].delta.has_value());
    CHECK(walk.messages[1].delta.value() == Approx(0.3));
    // The errored record advanced the cursor, so the next gap is measured from
    // IT and not from the last clean record.
    REQUIRE(walk.messages[2].delta.has_value());
    CHECK(walk.messages[2].delta.value() == Approx(0.2));
}

// ---------------------------------------------------------------------------
// Structural invariants
// ---------------------------------------------------------------------------

TEST_CASE("a BC-to-RT record with a Transmit command is rejected or skipped",
          "[reader][L2-SYN-020]") {
    std::vector<uint16_t> bad = bc_to_rt(5, 4, 2);
    bad[4] = command_word(5, mie::DIRECTION_TRANSMIT, 4, 2);

    std::vector<uint16_t> words = bad;
    words += bc_to_rt(5, 4, 2, 100);
    words += bc_to_rt(5, 4, 2, 200);

    SECTION("lenient skips the record and keeps going") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, mie::ReaderOptions());
        CHECK_FALSE(walk.threw);
        CHECK(walk.messages.size() == 2);
        CHECK(capture.contains("structural invariant violation"));
    }

    SECTION("strict rejects with a payload error") {
        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_PAYLOAD_ERROR);
        CHECK(walk.closed_after_throw);
    }
}

TEST_CASE("an RT-to-RT record whose second command transmits is rejected", "[reader][L2-SYN-023]") {
    // Post-extract, because Cmd2 lives inside the payload and does not exist
    // until it has been read.
    std::vector<uint16_t> bad = rt_to_rt(4, 11, 2);
    bad[5] = command_word(11, mie::DIRECTION_TRANSMIT, 7, 2);

    std::vector<uint16_t> words = bad;
    words += rt_to_rt(4, 11, 2, 100);
    words += rt_to_rt(4, 11, 2, 200);

    SECTION("lenient skips it") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, mie::ReaderOptions());
        CHECK(walk.messages.size() == 2);
        CHECK(capture.contains("Cmd2 requires direction = Receive"));
    }

    SECTION("strict rejects") {
        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_PAYLOAD_ERROR);
    }
}

TEST_CASE("an RT-to-RT data-word-count disagreement is caught", "[reader][L2-SYN-027]") {
    // The capacity check only ever sees Cmd1, so a Cmd2 that over-claims would
    // otherwise pass unnoticed.
    std::vector<uint16_t> bad = rt_to_rt(4, 11, 2);
    bad[5] = command_word(11, mie::DIRECTION_RECEIVE, 7, 5);

    std::vector<uint16_t> words = bad;
    words += rt_to_rt(4, 11, 2, 100);
    words += rt_to_rt(4, 11, 2, 200);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK(walk.messages.size() == 2);
    CHECK(capture.contains("data_word_count mismatch"));
}

TEST_CASE("a Status Word RT mismatch is an anomaly, not a rejection", "[reader][L2-SYN-024]") {
    // Warned about and KEPT. Rejecting would produce false negatives on real
    // recordings, where bus interference and undocumented vendor extensions
    // both occur.
    std::vector<uint16_t> odd = bc_to_rt(5, 4, 2);
    odd[odd.size() - 1] = status_word(0);

    std::vector<uint16_t> words = odd;
    words += bc_to_rt(5, 4, 2, 100);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 2);
    CHECK(capture.contains("Status RT = 0 does not match Cmd RT = 5"));
}

TEST_CASE("an anomaly is still an anomaly in strict mode", "[reader][L2-SYN-024]") {
    std::vector<uint16_t> odd = bc_to_rt(5, 4, 2);
    odd[odd.size() - 1] = status_word(0);

    std::vector<uint16_t> words = odd;
    words += bc_to_rt(5, 4, 2, 100);

    const Walk walk = walk_words(words, strict_options());
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 2);
}

TEST_CASE("an RT-to-BC record with a Receive command is rejected or skipped",
          "[reader][L2-SYN-021]") {
    std::vector<uint16_t> bad = rt_to_bc(5, 4, 2);
    bad[4] = command_word(5, mie::DIRECTION_RECEIVE, 4, 2);

    std::vector<uint16_t> words = bad;
    words += rt_to_bc(5, 4, 2, 100);
    words += rt_to_bc(5, 4, 2, 200);

    SECTION("lenient skips it") {
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk walk = walk_words(words, mie::ReaderOptions());
        CHECK(walk.messages.size() == 2);
        CHECK(capture.contains("requires Cmd direction = Transmit"));
    }

    SECTION("strict rejects") {
        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_PAYLOAD_ERROR);
    }
}

TEST_CASE("a word count too small for the declared payload is rejected", "[reader][L2-SYN-022]") {
    // The Type Word says eight words; the Command Word promises ten data words
    // plus a status. Both are individually plausible, and only comparing them
    // catches it -- which is the entire point of the capacity check.
    std::vector<uint16_t> bad;
    bad.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
    push_irig(bad, 0);
    bad.push_back(command_word(5, mie::DIRECTION_RECEIVE, 4, 10));
    bad.push_back(0x0101);
    bad.push_back(0x0102);
    bad.push_back(0x0103);

    std::vector<uint16_t> words = bad;
    words += bc_to_rt(5, 4, 2, 100);
    words += bc_to_rt(5, 4, 2, 200);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK(walk.messages.size() == 2);
    CHECK(capture.contains("is too small for declared payload"));
}

TEST_CASE("a set reserved Type Word bit is an anomaly, not a rejection", "[reader][L2-SYN-025]") {
    std::vector<uint16_t> odd = bc_to_rt(5, 4, 2);
    odd[0] = static_cast<uint16_t>(odd[0] | 0x8000);

    std::vector<uint16_t> words = odd;
    words += bc_to_rt(5, 4, 2, 100);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK_FALSE(walk.threw);
    // Kept, because an undocumented vendor extension is a likelier explanation
    // than corruption, and discarding the record would lose real traffic.
    CHECK(walk.messages.size() == 2);
    CHECK(capture.contains("reserved"));
}

// ---------------------------------------------------------------------------
// Sync loss and recovery
// ---------------------------------------------------------------------------

TEST_CASE("a corrupt region mid-file is recovered from in lenient mode",
          "[reader][L2-SYN-009][L2-SYN-013][L2-SYN-015]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    for (int i = 0; i < 6; ++i) {
        words.push_back(0x0099);
    }
    words += bc_to_rt(3, 5, 2, 200);
    words += bc_to_rt(3, 5, 2, 300);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK_FALSE(walk.threw);
    // The two records before the corruption AND the two after it. Losing the
    // second one would be the look-ahead bug: mid-stream validation runs at
    // depth 1 precisely so a good record is not discarded because its successor
    // is corrupt.
    CHECK(walk.messages.size() == 4);
    CHECK(walk.sync_losses == 1);
    CHECK(capture.contains("scanning forward"));
}

TEST_CASE("strict mode surfaces the specific validation failure",
          "[reader][L2-SYN-001][L2-SYN-016]") {
    // TWO good records lead, deliberately. Entry validation applies the
    // configured look-ahead, so a file whose SECOND record is corrupt does not
    // start at offset 0 at all -- the scan simply picks up after the damage and
    // the walk never sees the failure. The look-ahead is only skipped
    // mid-stream (depth 1), which is the path these sections exercise.
    SECTION("an unknown message type") {
        std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
        words += bc_to_rt(3, 5, 2, 100);
        std::vector<uint16_t> bad = bc_to_rt(3, 5, 2, 200);
        bad[0] = type_word(0x7F, 8);
        words += bad;
        words += bc_to_rt(3, 5, 2, 300);

        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_UNKNOWN_TYPE_WORD);
        // Records were yielded before the failure, so this is a rejection
        // mid-stream rather than at construction.
        CHECK(walk.messages.size() == 2);
    }

    SECTION("an impossible word count") {
        std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
        words += bc_to_rt(3, 5, 2, 100);
        std::vector<uint16_t> bad = bc_to_rt(3, 5, 2, 200);
        bad[0] = type_word(mie::MESSAGE_TYPE_BC_TO_RT, 2);
        words += bad;
        words += bc_to_rt(3, 5, 2, 300);

        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_INVALID_TYPE_WORD);
    }

    SECTION("an IRIG field out of range") {
        std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
        std::vector<uint16_t> bad;
        bad.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
        push_irig(bad, 0, 10, 24);  // hour 24 is out of range
        bad.push_back(command_word(3, mie::DIRECTION_RECEIVE, 5, 2));
        bad.push_back(0x1111);
        bad.push_back(0x1112);
        bad.push_back(status_word(3));
        words += bad;
        words += bc_to_rt(3, 5, 2, 200);

        // There is no dedicated error variant for an out-of-range IRIG field,
        // and inventing seven would give the CLI seven exit codes for one
        // condition. PayloadError carries the specific text instead.
        const Walk walk = walk_words(words, strict_options());
        CHECK(walk.threw);
        CHECK(walk.kind == mie::KIND_PAYLOAD_ERROR);
        CHECK(walk.error_message.find("hour") != std::string::npos);
    }
}

TEST_CASE("a truncated tail ends the walk cleanly rather than as an error",
          "[reader][L2-RDR-002]") {
    // A record header promising more than the file holds, with less than the
    // 64 KB scan window behind it: the scan was cut short by EOF, not by
    // failing to find a record, and that is a truncated recording.
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 40));
    push_irig(words);
    words.push_back(command_word(3, mie::DIRECTION_RECEIVE, 5, 2));

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 2);
    // A truncated final record, named as one -- not a sync loss (L2-RDR-002).
    CHECK(walk.sync_losses == 0);
    CHECK(capture.count_containing("do not hold a complete record") == 1);
    CHECK(capture.count_containing("sync lost") == 0);
}

TEST_CASE("corruption that runs past EOF before a whole record is still a sync loss",
          "[reader][L2-RDR-002][L2-SYN-010]") {
    // Lenient mode scans before it narrates. A Type Word declaring 126 bytes
    // with a whole 16-byte record behind it runs past EOF, yet it is not the
    // recording's final record: the scan recovers the record behind it.
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 63));
    words += bc_to_rt(3, 5, 2, 200);

    mie::ReaderOptions options;
    options.input_time_format = mie::TIMESTAMP_IRIG;
    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words, options);
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 3);
    CHECK(walk.sync_losses == 1);
    CHECK(capture.count_containing("sync lost") == 1);
    CHECK(capture.count_containing("do not hold a complete record") == 0);
}

TEST_CASE("a truncated final record is an error in strict mode", "[reader][L2-RDR-003]") {
    // Same fixture as the lenient case above, opposite policy: strict mode does
    // not silently drop the tail of a recording, because "the file ended early"
    // and "the decoder stopped early" look identical in the output otherwise.
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    words.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 40));
    push_irig(words, 200);
    words.push_back(command_word(3, mie::DIRECTION_RECEIVE, 5, 2));

    const Walk walk = walk_words(words, strict_options());
    CHECK(walk.threw);
    CHECK(walk.kind == mie::KIND_RECORD_TRUNCATED);
    CHECK(walk.messages.size() == 2);
}

TEST_CASE("a truncated tail of two to seven bytes is reported in strict mode",
          "[reader][L2-RDR-002][L2-RDR-003]") {
    // Fewer than 8 bytes cannot hold a record, but 2-7 still hold a readable
    // Type Word, so strict mode reports the tail like any longer one: a
    // plausible Type Word is RecordTruncated, an implausible one gets the
    // Type-Word classes. A null Type Word is the terminator and one byte holds
    // no Type Word; both end cleanly. Lenient mode keeps the whole records and
    // says what it dropped, once.
    struct Case {
        std::vector<uint8_t> tail;
        bool reported;
        mie::MieErrorKind kind;
    };
    std::vector<Case> cases;
    const uint8_t two[] = {0x02, 0x24};
    const uint8_t four[] = {0x02, 0x24, 0xAA, 0xAA};
    const uint8_t seven[] = {0x02, 0x24, 1, 2, 3, 4, 5};
    const uint8_t bad_type[] = {0x7F, 0x24, 0xAA, 0xAA};
    const uint8_t bad_count[] = {0x02, 0x01, 0xAA, 0xAA};
    const uint8_t terminator[] = {0x00, 0x00};
    const uint8_t one[] = {0x02};
    cases.push_back(Case{std::vector<uint8_t>(two, two + 2), true, mie::KIND_RECORD_TRUNCATED});
    cases.push_back(Case{std::vector<uint8_t>(four, four + 4), true, mie::KIND_RECORD_TRUNCATED});
    cases.push_back(Case{std::vector<uint8_t>(seven, seven + 7), true, mie::KIND_RECORD_TRUNCATED});
    cases.push_back(
        Case{std::vector<uint8_t>(bad_type, bad_type + 4), true, mie::KIND_UNKNOWN_TYPE_WORD});
    cases.push_back(
        Case{std::vector<uint8_t>(bad_count, bad_count + 4), true, mie::KIND_INVALID_TYPE_WORD});
    cases.push_back(
        Case{std::vector<uint8_t>(terminator, terminator + 2), false, mie::KIND_FILE_IO});
    cases.push_back(Case{std::vector<uint8_t>(one, one + 1), false, mie::KIND_FILE_IO});

    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    for (std::size_t i = 0; i < cases.size(); ++i) {
        INFO("case " << i);
        std::vector<uint8_t> bytes = le_bytes(words);
        bytes.insert(bytes.end(), cases[i].tail.begin(), cases[i].tail.end());
        const mie_test::TempFile fixture("short-tail.mie", bytes);

        const Walk strict = walk_file(fixture.str(), strict_options());
        CHECK(strict.messages.size() == 2);
        CHECK(strict.threw == cases[i].reported);
        if (cases[i].reported) {
            CHECK(strict.kind == cases[i].kind);
        }

        mie::ReaderOptions lenient;
        lenient.input_time_format = mie::TIMESTAMP_IRIG;
        const LogCapture capture(mie::log::LEVEL_WARN);
        const Walk loose = walk_file(fixture.str(), lenient);
        CHECK_FALSE(loose.threw);
        CHECK(loose.messages.size() == 2);
        CHECK(capture.count_containing("do not hold a complete record") ==
              (cases[i].reported ? 1u : 0u));
    }
}

TEST_CASE("a corrupt region larger than the scan window is unrecoverable",
          "[reader][L1-EXIT-004][L2-SYN-010][L2-SYN-011]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    // More than MAX_SCAN_BYTES (64 KB) of junk, so the scan exhausts its window
    // with plenty of file left -- genuine mid-file corruption rather than a
    // truncation.
    for (std::size_t i = 0; i < 40000; ++i) {
        words.push_back(0x0099);
    }
    words += bc_to_rt(3, 5, 2, 200);

    const LogCapture capture(mie::log::LEVEL_ERROR);
    const Walk walk = walk_words(words, mie::ReaderOptions());
    CHECK(walk.threw);
    CHECK(walk.kind == mie::KIND_UNRECOVERABLE_SYNC_LOSS);
    CHECK(walk.messages.size() == 2);
    CHECK(walk.sync_losses >= 1);
    CHECK(walk.closed_after_throw);
    CHECK(capture.contains("unrecoverable sync loss"));
}

TEST_CASE("sync losses are reported per walk and reset between walks", "[reader]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);
    for (int i = 0; i < 6; ++i) {
        words.push_back(0x0099);
    }
    words += bc_to_rt(3, 5, 2, 200);
    words += bc_to_rt(3, 5, 2, 300);

    const mie_test::TempFile fixture("twice.mie", le_bytes(words));
    mie::MieFileReader reader;
    reader.open(fixture.str(), mie::ReaderOptions());

    mie::MieMessage message;
    std::size_t first_count = 0;
    {
        mie::RecordIter it = reader.iter();
        while (it.next(message)) {
            first_count += 1;
        }
    }
    const uint64_t first_losses = reader.sync_losses();

    std::size_t second_count = 0;
    {
        mie::RecordIter it = reader.iter();
        while (it.next(message)) {
            second_count += 1;
        }
    }

    CHECK(first_count == 4);
    CHECK(second_count == 4);
    CHECK(first_losses == 1);
    // Reset, not accumulated: a second walk that reported 2 would make the
    // CLI's partial-vs-clean exit class depend on how many times the file had
    // been read.
    CHECK(reader.sync_losses() == 1);
}

// ---------------------------------------------------------------------------
// MUX
// ---------------------------------------------------------------------------

TEST_CASE("MUX is taken from the configured field of the file name", "[reader][L2-WRT-020]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words += bc_to_rt(3, 5, 2, 100);

    const Walk walk = walk_words(words, mie::ReaderOptions(), "one.two.three.four.BUS7.mie");
    REQUIRE(walk.messages.size() == 2);
    REQUIRE(walk.messages[0].mux);
    // Field 4 of the DOT-separated name. The fixture helper prefixes "mie-", so
    // "one" and field 0 merge into "mie-one" and the default index of 4 lands on
    // "BUS7" -- which is the point: the index is counted over the whole file
    // name, not over some notion of the "interesting" part of it.
    CHECK(*walk.messages[0].mux == "BUS7");
    // One string per file, shared by pointer onto every record: carrying MUX
    // stays O(1) in resident memory however many records the file holds.
    CHECK(walk.messages[0].mux.get() == walk.messages[1].mux.get());
}

TEST_CASE("MUX can be disabled for vendor-exact output", "[reader][L2-WRT-020]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words += bc_to_rt(3, 5, 2, 100);

    mie::ReaderOptions options;
    options.mux_enabled = false;
    const Walk walk = walk_words(words, options, "one.two.three.four.BUS7.mie");
    REQUIRE(walk.messages.size() == 2);
    CHECK_FALSE(walk.messages[0].mux);
}

TEST_CASE("a negative MUX field counts from the end of the name", "[reader][L2-WRT-020]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words += bc_to_rt(3, 5, 2, 100);

    // -2, not -1: the fixture helper appends a pid-and-counter token, so the
    // LAST dotted field is unique per run and nothing deterministic can be
    // asserted about it. -2 still proves the thing that matters -- that a
    // negative index counts from the end rather than the start.
    mie::ReaderOptions options;
    options.mux_field = -2;
    const Walk walk = walk_words(words, options, "one.two.mie");
    REQUIRE(walk.messages.size() == 2);
    REQUIRE(walk.messages[0].mux);
    CHECK(*walk.messages[0].mux == "two");
}

TEST_CASE("an out-of-range MUX field leaves the column empty", "[reader][L2-WRT-020]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2);
    words += bc_to_rt(3, 5, 2, 100);

    mie::ReaderOptions options;
    options.mux_field = 99;
    const Walk walk = walk_words(words, options, "one.two.mie");
    REQUIRE(walk.messages.size() == 2);
    CHECK_FALSE(walk.messages[0].mux);
}

// ---------------------------------------------------------------------------
// Advisories
// ---------------------------------------------------------------------------

TEST_CASE("a freerun timestamp warns on every record", "[reader][L2-DEC-003][L2-SYN-019]") {
    // Per record, unlike the day-of-year advisory below: freerun means this
    // record's absolute time is unknown, so which records are affected is the
    // information the operator needs.
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 3; ++i) {
        std::vector<uint16_t> record;
        record.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
        push_irig(record, i * 100, 10, 15, 54, 50, /*freerun=*/true);
        record.push_back(command_word(3, mie::DIRECTION_RECEIVE, 5, 2));
        record.push_back(0x1111);
        record.push_back(0x1112);
        record.push_back(status_word(3));
        words += record;
    }

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    CHECK(walk.messages[0].timestamp.irig.freerun);
    CHECK(capture.count_containing("freerun timestamp") == 3);
}

TEST_CASE("the IRIG day-of-year advisory fires once per decode", "[reader][L2-LOG-001]") {
    // PRA-9. It is a property of the card's firmware, not of any one record, so
    // repeating it per record would bury every other warning in the file.
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 5; ++i) {
        words += bc_to_rt(3, 5, 2, i * 1000);
    }

    const LogCapture capture(mie::log::LEVEL_INFO);
    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 5);
    CHECK(capture.count_containing("day-of-year") == 1);
}

TEST_CASE("the IRIG day-of-year advisory is silent at the default level", "[reader][L2-LOG-001]") {
    // L2-LOG-001: INFO, not WARN. It is a standing disclaimer about card
    // firmware rather than an observation about this recording -- nothing here
    // compares the decoded day against anything -- so at WARN it appeared in
    // the default output of every decode of a calendar-locked IRIG file.
    std::vector<uint16_t> words;
    words += bc_to_rt(3, 5, 2, 1000);

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 1);
    CHECK(capture.count_containing("day-of-year") == 0);
}

TEST_CASE("the IRIG day-of-year advisory can be disabled outright", "[reader][L2-LOG-001]") {
    // L2-LOG-001: for a site that has diffed its card model against vendor CSV
    // and wants a verbose troubleshooting run without the known-noise line.
    std::vector<uint16_t> words;
    words += bc_to_rt(3, 5, 2, 1000);

    const LogCapture capture(mie::log::LEVEL_INFO);
    mie::log::set_irig_day_advisory(false);
    const Walk walk = walk_words(words);
    // Global state: restore it before the CHECK, so a failing expectation
    // cannot leave the advisory off for every later case in the binary.
    mie::log::set_irig_day_advisory(true);
    REQUIRE(walk.messages.size() == 1);
    CHECK(capture.count_containing("day-of-year") == 0);
}

TEST_CASE("a freerun record does not trigger the day-of-year advisory", "[reader]") {
    // A freerun timestamp has no calendar day, so there is no day-of-year
    // discrepancy to advise about.
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 3; ++i) {
        std::vector<uint16_t> record;
        record.push_back(type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
        push_irig(record, i * 100, 10, 15, 54, 50, /*freerun=*/true);
        record.push_back(command_word(3, mie::DIRECTION_RECEIVE, 5, 2));
        record.push_back(0x1111);
        record.push_back(0x1112);
        record.push_back(status_word(3));
        words += record;
    }

    const LogCapture capture(mie::log::LEVEL_WARN);
    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    CHECK(capture.count_containing("day-of-year") == 0);
}

// ---------------------------------------------------------------------------
// Iteration mechanics
// ---------------------------------------------------------------------------

TEST_CASE("file offsets advance by each record's declared length", "[reader][L2-DEC-010]") {
    // Records of different sizes, so a fixed stride would pass on a uniform
    // file and fail here.
    std::vector<uint16_t> words = bc_to_rt(3, 5, 1, 0);
    const std::size_t first_bytes = words.size() * 2;
    const std::vector<uint16_t> second = bc_to_rt(3, 5, 8, 100);
    const std::size_t second_bytes = second.size() * 2;
    words += second;
    words += bc_to_rt(3, 5, 2, 200);

    const Walk walk = walk_words(words);
    REQUIRE(walk.messages.size() == 3);
    CHECK(walk.messages[0].file_offset == 0);
    CHECK(walk.messages[1].file_offset == first_bytes);
    CHECK(walk.messages[2].file_offset == first_bytes + second_bytes);
}

TEST_CASE("a walk reports its own message count and resolved format", "[reader][L2-DEC-011]") {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 4; ++i) {
        words += bc_to_rt(3, 5, 2, i * 100);
    }

    const mie_test::TempFile fixture("counts.mie", le_bytes(words));
    mie::MieFileReader reader;
    reader.open(fixture.str(), mie::ReaderOptions());

    mie::RecordIter it = reader.iter();
    CHECK(it.resolved_format() == mie::TIMESTAMP_IRIG);

    mie::MieMessage message;
    while (it.next(message)) {
    }
    CHECK(it.message_count() == 4);
    CHECK(it.sync_losses() == 0);
}

TEST_CASE("next() keeps answering false after the stream ends", "[reader][L3-CPP-013]") {
    std::vector<uint16_t> words = bc_to_rt(3, 5, 2, 0);
    words += bc_to_rt(3, 5, 2, 100);

    const mie_test::TempFile fixture("exhausted.mie", le_bytes(words));
    mie::MieFileReader reader;
    reader.open(fixture.str(), mie::ReaderOptions());

    mie::RecordIter it = reader.iter();
    mie::MieMessage message;
    CHECK(it.next(message));
    CHECK(it.next(message));
    CHECK_FALSE(it.next(message));
    CHECK_FALSE(it.next(message));
}

TEST_CASE("look-ahead depth is configurable and does not change a clean decode",
          "[reader][L2-SYN-026]") {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 5; ++i) {
        words += bc_to_rt(3, 5, 2, i * 100);
    }

    mie::ReaderOptions options;
    options.lookahead_records = 4;
    const Walk walk = walk_words(words, options);
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 5);
}

TEST_CASE("a zero look-ahead or probe size is clamped rather than rejected", "[reader]") {
    // Zero would mean "check no records", which is not a policy the format
    // admits. Clamping keeps a bad config from turning into undefined
    // behaviour, and the CLI clamps to [1, 32] before it ever gets here.
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 3; ++i) {
        words += bc_to_rt(3, 5, 2, i * 100);
    }

    mie::ReaderOptions options;
    options.lookahead_records = 0;
    options.detect_records = 0;
    const Walk walk = walk_words(words, options);
    CHECK_FALSE(walk.threw);
    CHECK(walk.messages.size() == 3);
}
