// SPDX-License-Identifier: Apache-2.0
//
// Dump tests: the report's shape, and the three ways a scan stops.
//
// The dump has no CSV oracle behind it -- it writes a human-facing report to
// stdout, and the shared conformance manifest has no `dump` mode. So unlike
// every other module in this tree, what is asserted here IS the contract rather
// than a second opinion about it. The report was separately diffed against the
// Rust implementation across a dozen fixtures and every flag combination while
// this was written; these tests are what keeps it that way.
//
// Everything here runs on deliberately broken input, because that is what the
// dump is FOR. A test suite that only fed it well-formed recordings would
// exercise none of the paths an operator actually reaches it through.

#include "mie/dump.hpp"

#include <catch2/catch.hpp>

#include <cstdio>
#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/error.hpp"
#include "record_fixtures.hpp"
#include "temp_path.hpp"

namespace {

using mie_test::TempFile;
using mie_test::TempPath;

/// Owns an output FILE for the duration of a capture.
///
/// RAII rather than a try/catch pair around each dump call. The dump raises on
/// a rejected input, and a hand-written close in the catch block would have to
/// either discard its result -- which the lint gate rightly objects to -- or
/// assert during stack unwinding, which is worse.
class OpenFile {
  public:
    explicit OpenFile(const std::string& path) : handle_(std::fopen(path.c_str(), "wb")) {}

    ~OpenFile() {
        if (handle_ != NULL) {
            // Only reached when a dump threw. The run is already failing and
            // there is nothing useful to do with a close failure here.
            (void)std::fclose(handle_);  // NOLINT(cert-err33-c)
        }
    }

    std::FILE* get() const { return handle_; }

    /// Close early, reporting success. A caller about to READ the file needs to
    /// insist the bytes actually landed -- a silent close failure would present
    /// as a dump that produced nothing.
    bool close() {
        if (handle_ == NULL) {
            return true;
        }
        const bool ok = std::fclose(handle_) == 0;
        handle_ = NULL;
        return ok;
    }

  private:
    OpenFile(const OpenFile&);
    OpenFile& operator=(const OpenFile&);

    std::FILE* handle_;
};

/// Run the record view into a temporary file and return everything it wrote.
std::string capture_records(const std::string& path, const mie::Optional<uint64_t>& records,
                            std::size_t offset) {
    const TempPath out("dump-out");
    OpenFile file(out.str());
    REQUIRE(file.get() != NULL);
    mie::dump::hex_dump_records(path, records, offset, file.get());
    REQUIRE(file.close());
    std::string text;
    REQUIRE(mie_test::read_file(out.str(), text));
    return text;
}

std::string capture_raw(const std::string& path, std::size_t offset,
                        const mie::Optional<std::size_t>& length) {
    const TempPath out("dump-raw-out");
    OpenFile file(out.str());
    REQUIRE(file.get() != NULL);
    mie::dump::hex_dump_raw(path, offset, length, file.get());
    REQUIRE(file.close());
    std::string text;
    REQUIRE(mie_test::read_file(out.str(), text));
    return text;
}

std::size_t count_occurrences(const std::string& haystack, const std::string& needle) {
    std::size_t count = 0;
    std::size_t at = haystack.find(needle);
    while (at != std::string::npos) {
        count += 1;
        at = haystack.find(needle, at + needle.size());
    }
    return count;
}

std::vector<uint8_t> two_records() {
    std::vector<uint16_t> words;
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 100));
    mie_test::append(words, mie_test::rt_to_bc(9, 4, 3, 200));
    return mie_test::finish(words);
}

}  // namespace

TEST_CASE("the record view annotates each record before its bytes", "[dump][L3-CPP-025]") {
    const TempFile input("mie-dump-basic.mie", two_records());
    const std::string report = capture_records(input.str(), mie::Optional<uint64_t>(), 0);

    SECTION("the header names the file and where the scan began") {
        REQUIRE(report.find("File: ") == 0);
        REQUIRE(report.find(" bytes)") != std::string::npos);
        REQUIRE(report.find("Record dump starting at offset 0x00000000") != std::string::npos);
    }

    SECTION("each record gets a decoded annotation") {
        REQUIRE(report.find("Record #0  @  0x00000000") != std::string::npos);
        REQUIRE(report.find("Record #1") != std::string::npos);
        // The arrow form, not the enum spelling: this view is read by a person
        // staring at a broken file.
        REQUIRE(report.find("BC->RT (Receive)") != std::string::npos);
        REQUIRE(report.find("RT->BC (Transmit)") != std::string::npos);
        REQUIRE(report.find("Bus A") != std::string::npos);
        REQUIRE(report.find("error flag (bit 14): clear") != std::string::npos);
        // SCREAMING_SNAKE, matching the other implementations' reports -- NOT
        // the CamelCase `models::message_format_name` used elsewhere.
        REQUIRE(report.find("Format: RECEIVE") != std::string::npos);
        REQUIRE(report.find("Format: TRANSMIT") != std::string::npos);
        REQUIRE(report.find("Cmd:    0x") != std::string::npos);
        REQUIRE(report.find("RT15 SA11 R") != std::string::npos);
        REQUIRE(report.find("RT9 SA4 T") != std::string::npos);
    }

    SECTION("it ends with a count") {
        REQUIRE(report.find("2 records dumped.") != std::string::npos);
    }

    SECTION("a clean record shows no Error line") {
        REQUIRE(report.find("  Error:  ") == std::string::npos);
    }
}

TEST_CASE("--records stops the scan early", "[dump][L3-CPP-025]") {
    const TempFile input("mie-dump-limit.mie", two_records());

    const std::string one = capture_records(input.str(), mie::Optional<uint64_t>(1), 0);
    REQUIRE(one.find("Record #0") != std::string::npos);
    REQUIRE(one.find("Record #1") == std::string::npos);
    REQUIRE(one.find("1 records dumped.") != std::string::npos);

    SECTION("zero records is a legal request, not an absent one") {
        // The distinction an Optional exists to make: `--records 0` asks for
        // nothing, which is different from not passing the flag at all.
        const std::string none = capture_records(input.str(), mie::Optional<uint64_t>(0), 0);
        REQUIRE(none.find("Record #") == std::string::npos);
        REQUIRE(none.find("0 records dumped.") != std::string::npos);
    }
}

TEST_CASE("an errored record shows the Error Word and its meaning", "[dump][L3-CPP-025]") {
    // The Error Word is the last word of an errored record. Printing its DDC
    // description here is what makes the reason legible without a trip to the
    // error catalogue -- which is most of the value of the record view.
    std::vector<uint16_t> words;
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 7, /*error=*/true));
    mie_test::push_irig(words, 100);
    words.push_back(mie_test::command_word(15, mie::DIRECTION_RECEIVE, 11, 2));
    words.push_back(0x0000);
    words.push_back(0x011E);  // Manchester/Parity error
    const TempFile input("mie-dump-error.mie", mie_test::finish(words));

    const std::string report = capture_records(input.str(), mie::Optional<uint64_t>(), 0);
    REQUIRE(report.find("error flag (bit 14): SET") != std::string::npos);
    REQUIRE(report.find("Error:  0x011E") != std::string::npos);
    REQUIRE(report.find("Manchester") != std::string::npos);
}

TEST_CASE("a spurious record shows no Command Word or Error Word", "[dump][L3-CPP-025]") {
    // Everything after a SPURIOUS_DATA record's timestamp is leftover bus
    // words, bit 14 or not. Reading the first as a Command Word printed an RT
    // and subaddress that were not there, and with bit 14 set the last was
    // read as a "DDC error".
    std::vector<uint16_t> words;
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_SPURIOUS_DATA, 6, /*error=*/true));
    mie_test::push_irig(words, 100);
    words.push_back(0xAAAA);
    words.push_back(0xBBBB);
    const TempFile input("mie-dump-spurious.mie", mie_test::finish(words));

    const std::string report = capture_records(input.str(), mie::Optional<uint64_t>(), 0);
    REQUIRE(report.find("error flag (bit 14): SET") != std::string::npos);
    REQUIRE(report.find("Data:   2 leftover word(s); SPURIOUS_DATA carries no Command Word") !=
            std::string::npos);
    REQUIRE(report.find("Cmd:") == std::string::npos);
    REQUIRE(report.find("Error:") == std::string::npos);
}

TEST_CASE("a scan stop is written inline AND logged", "[dump][L3-CPP-026]") {
    // L2-CLI-013. Both, deliberately: the report may be piped somewhere the log
    // is not, and the log may be watched by someone who never sees the report.
    SECTION("a truncated record") {
        std::vector<uint16_t> words;
        // Declares 36 words but the file stops well short of that.
        words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 36));
        mie_test::push_irig(words, 100);
        words.push_back(mie_test::command_word(15, mie::DIRECTION_RECEIVE, 11, 2));
        const TempFile input("mie-dump-truncated.mie", mie_test::le_bytes(words));

        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        const std::string report = capture_records(input.str(), mie::Optional<uint64_t>(), 0);
        REQUIRE(report.find("!! Truncated record at 0x00000000") != std::string::npos);
        REQUIRE(report.find("bytes needed") != std::string::npos);
        REQUIRE(report.find("0 records dumped.") != std::string::npos);

        bool logged = false;
        const std::vector<std::string>& lines = mie_test::captured_lines();
        for (std::size_t i = 0; i < lines.size(); ++i) {
            if (lines[i].find("truncated record") != std::string::npos) {
                logged = true;
            }
        }
        REQUIRE(logged);
    }

    SECTION("an invalid word count") {
        // A word count below the structural minimum cannot describe a record,
        // so there is no extent to advance by and the scan has to stop.
        std::vector<uint16_t> words;
        words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 1));
        mie_test::push_irig(words, 100);
        words.push_back(mie_test::command_word(15, mie::DIRECTION_RECEIVE, 11, 2));
        words.push_back(0x0000);
        words.push_back(0x0000);
        const TempFile input("mie-dump-badwc.mie", mie_test::le_bytes(words));

        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        const std::string report = capture_records(input.str(), mie::Optional<uint64_t>(), 0);
        REQUIRE(report.find("!! Invalid word_count=1 at 0x00000000") != std::string::npos);
        REQUIRE(report.find("0 records dumped.") != std::string::npos);

        bool logged = false;
        const std::vector<std::string>& lines = mie_test::captured_lines();
        for (std::size_t i = 0; i < lines.size(); ++i) {
            if (lines[i].find("invalid word_count") != std::string::npos) {
                logged = true;
            }
        }
        REQUIRE(logged);
    }
}

TEST_CASE("the scan stops at damage rather than resynchronising", "[dump][L3-CPP-026]") {
    // Deliberate. `decode`'s recovery walk is the tool for reading past
    // corruption; a dump that skipped ahead would misrepresent where the damage
    // begins, which is the one thing this view exists to show.
    std::vector<uint16_t> words;
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 100));
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 2));  // impossible
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 300));
    const TempFile input("mie-dump-stop.mie", mie_test::le_bytes(words));

    const std::string report = capture_records(input.str(), mie::Optional<uint64_t>(), 0);
    REQUIRE(report.find("Record #0") != std::string::npos);
    REQUIRE(report.find("!! Invalid word_count=2") != std::string::npos);
    // One record, then the stop -- the good record after the damage is NOT
    // reached.
    REQUIRE(report.find("1 records dumped.") != std::string::npos);
    REQUIRE(count_occurrences(report, "Record #") == 1u);
}

TEST_CASE("the raw view parses nothing and clamps its range", "[dump][L3-CPP-027]") {
    const TempFile input("mie-dump-raw.mie", two_records());

    SECTION("the whole file by default") {
        const std::string report = capture_raw(input.str(), 0, mie::Optional<std::size_t>());
        REQUIRE(report.find("File: ") == 0);
        REQUIRE(report.find("Range: 0x00000000-") != std::string::npos);
        // No decoding at all: the raw view is what an operator reaches for when
        // the decoder has already refused the file.
        REQUIRE(report.find("Record #") == std::string::npos);
        REQUIRE(report.find("Type:") == std::string::npos);
        // The hex+ASCII shape.
        REQUIRE(report.find("  00000000  ") != std::string::npos);
        REQUIRE(report.find('|') != std::string::npos);
    }

    SECTION("a window that fits is honoured exactly") {
        // Chosen to sit inside the fixture. A window running past the end is
        // the clamping case, covered separately below -- conflating the two
        // would leave neither actually asserted.
        const std::string report = capture_raw(input.str(), 8, mie::Optional<std::size_t>(16));
        REQUIRE(report.find("Range: 0x00000008-0x00000018") != std::string::npos);
    }

    SECTION("a window running past the end clamps there") {
        const std::string report = capture_raw(input.str(), 8, mie::Optional<std::size_t>(9999));
        const std::string whole = capture_raw(input.str(), 8, mie::Optional<std::size_t>());
        REQUIRE(report == whole);
    }

    SECTION("an offset past the end yields an empty range, not an error") {
        // The operator is exploring. "Your offset is past the end" is exactly
        // what the empty output already says, so refusing would add nothing.
        const std::string report = capture_raw(input.str(), 1000000, mie::Optional<std::size_t>());
        REQUIRE(report.find("Range: ") != std::string::npos);
        REQUIRE(report.find("  0000") == std::string::npos);
    }

    SECTION("a length that would overflow clamps to the end of the file") {
        // `--offset 8 --length <huge>` must mean "to the end", never wrap around
        // to a range starting before the offset.
        const std::string huge =
            capture_raw(input.str(), 8, mie::Optional<std::size_t>(static_cast<std::size_t>(-1)));
        const std::string rest = capture_raw(input.str(), 8, mie::Optional<std::size_t>());
        REQUIRE(huge == rest);
    }

    SECTION("a zero length yields an empty range") {
        const std::string report = capture_raw(input.str(), 8, mie::Optional<std::size_t>(0));
        REQUIRE(report.find("Range: 0x00000008-0x00000008") != std::string::npos);
    }
}

TEST_CASE("the hex line renders non-printable bytes as dots", "[dump][L3-CPP-027]") {
    // Explicit ASCII range, never <cctype>: this tree is locale-free by rule,
    // and `isprint` under a non-C locale would render high bytes differently on
    // different hosts -- making a diagnostic report depend on the environment
    // that produced it.
    std::vector<uint8_t> bytes;
    bytes.push_back(0x00);
    bytes.push_back(0x1F);  // just below printable
    bytes.push_back(0x20);  // space, the first printable
    bytes.push_back('A');
    bytes.push_back(0x7E);  // tilde, the last printable
    bytes.push_back(0x7F);  // DEL, just above
    bytes.push_back(0xFF);
    const TempFile input("mie-dump-ascii.mie", bytes);

    const std::string report = capture_raw(input.str(), 0, mie::Optional<std::size_t>());
    REQUIRE(report.find("|.. A~..|") != std::string::npos);
}

TEST_CASE("a missing or empty input is refused", "[dump][L3-CPP-027]") {
    SECTION("missing") {
        const TempPath absent("mie-dump-absent.mie");
        try {
            capture_records(absent.str(), mie::Optional<uint64_t>(), 0);
            FAIL("expected a file-not-found rejection");
        } catch (const mie::MieError& error) {
            CHECK(error.kind() == mie::KIND_FILE_NOT_FOUND);
        }
    }

    SECTION("empty") {
        // Zero bytes is not a recording, and a dump of nothing would be an
        // empty report that looks like a successful read of a valid file.
        const TempFile empty("mie-dump-empty.mie", std::string());
        try {
            capture_raw(empty.str(), 0, mie::Optional<std::size_t>());
            FAIL("expected a file-empty rejection");
        } catch (const mie::MieError& error) {
            CHECK(error.kind() == mie::KIND_FILE_EMPTY);
        }
    }
}

TEST_CASE("the report is pure ASCII with LF endings", "[dump][L2-CLI-014]") {
    // The report is a stdout PAYLOAD -- piped, redirected, and diffed against
    // the Rust and Python reports -- so it carries no byte above 0x7F and no
    // CR. Python's report used box-drawing, arrow and en-dash characters until
    // this was written down; those could not be encoded on a redirected Windows
    // stdout at cp1252, and made 11 of 34 lines differ from this one.
    //
    // Both views, and a record carrying an anomaly note, because the rule, the
    // arrow and the `!!` note are the three places a non-ASCII character would
    // plausibly be reintroduced.
    std::vector<uint16_t> words;
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 100));
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 2));  // anomaly
    // Padding so the scan can still read a minimum-sized record at the bad Type
    // Word's offset. Without it the loop stops for lack of bytes BEFORE
    // validating the word count, and the note this case is checking for is
    // never written -- the test would then pass over a report that never
    // reached the path it means to cover.
    for (int i = 0; i < 6; ++i) {
        words.push_back(0x0000);
    }
    const TempFile input("mie-dump-ascii-contract.mie", mie_test::le_bytes(words));

    std::string reports[2];
    reports[0] = capture_records(input.str(), mie::Optional<uint64_t>(), 0);
    reports[1] = capture_raw(input.str(), 0, mie::Optional<std::size_t>());

    for (std::size_t which = 0; which < 2; ++which) {
        const std::string& report = reports[which];
        INFO((which == 0 ? "record view" : "raw view"));
        REQUIRE_FALSE(report.empty());
        for (std::size_t i = 0; i < report.size(); ++i) {
            const unsigned char byte = static_cast<unsigned char>(report[i]);
            INFO("byte " << i << " = 0x" << std::hex << static_cast<unsigned>(byte));
            REQUIRE(byte < 0x80);
            REQUIRE(byte != '\r');
        }
    }
    // The anomaly note really is present, so the sweep above covered it rather
    // than passing over a report that never reached the interesting path.
    REQUIRE(reports[0].find("!! Invalid word_count") != std::string::npos);
}
