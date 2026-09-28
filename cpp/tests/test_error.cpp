// SPDX-License-Identifier: Apache-2.0
//
// Tests for the error type (L3-CPP-012).
//
// Two things are pinned here, and both are cross-implementation contracts
// rather than local choices:
//
//   1. THE CLASSIFICATION. is_file_error() and is_record_error() must select
//      exactly the same variants as their Rust counterparts, and
//      is_record_error() must match Python's MieRecordError exactly. The table
//      below is exhaustive over every kind, and a companion case fails if a new
//      kind is added without a row -- which is the whole point. Rust and Python
//      both pin their classification the same way, so a variant cannot be added
//      on one side and left unclassified on another.
//
//   2. THE MESSAGE WORDING. Conformance cases compare stderr text across
//      implementations, so each message is a transcription of the matching
//      Display arm in rust/src/error.rs. These assertions are what make a
//      reworded message a build failure rather than a silent divergence.

#include "mie/error.hpp"

#include <catch2/catch.hpp>

#include <string>
#include <type_traits>
#include <vector>

namespace {

/// One row of the classification contract.
struct Classification {
    mie::MieErrorKind kind;
    bool is_file;
    bool is_record;
};

/// EVERY kind, with its expected classification.
///
/// Note the two rows that look wrong and are not: HomogeneousPayload and
/// TimestampFormatMismatch both carry a byte offset, yet neither is a record
/// error. They cite where the problem was noticed while rejecting the file as a
/// whole, and Python classes them under MieFileError for that reason. Carrying
/// an offset is not what makes something a record error.
///
/// Note also that is_file is NARROWER than Python's MieFileError: the
/// whole-file rejections and the destination guards answer false to BOTH
/// predicates here. That asymmetry is deliberate and documented in
/// docs/ERROR-CATALOG.md; match on kind() for those.
const Classification kContract[] = {
    {mie::KIND_FILE_NOT_FOUND, true, false},
    {mie::KIND_FILE_EMPTY, true, false},
    {mie::KIND_FILE_IO, true, false},

    {mie::KIND_INVALID_TYPE_WORD, false, true},
    {mie::KIND_UNKNOWN_TYPE_WORD, false, true},
    {mie::KIND_RECORD_TRUNCATED, false, true},
    {mie::KIND_FIRST_RECORD_TRUNCATED, false, true},
    {mie::KIND_PAYLOAD_ERROR, false, true},
    {mie::KIND_UNKNOWN_ERROR_CODE, false, true},
    {mie::KIND_UNRECOVERABLE_SYNC_LOSS, false, true},

    {mie::KIND_NO_VALID_RECORDS, false, false},
    {mie::KIND_HOMOGENEOUS_PAYLOAD, false, false},
    {mie::KIND_TIMESTAMP_FORMAT_MISMATCH, false, false},
    {mie::KIND_CALENDAR_UNAVAILABLE, false, false},
    {mie::KIND_WRITER_ERROR, false, false},
    {mie::KIND_INPUT_OUTPUT_COLLISION, false, false},
    {mie::KIND_CLOBBER_REFUSED, false, false},
    {mie::KIND_INCOMPATIBLE_MERGE_INPUTS, false, false},
    {mie::KIND_NON_MONOTONIC_INPUT, false, false},
};

const std::size_t kContractRows = sizeof(kContract) / sizeof(kContract[0]);

/// One constructed error per kind, so the predicates are exercised against real
/// instances rather than against a bare enum value.
std::vector<mie::MieError> every_error() {
    std::vector<mie::MieError> all;
    all.push_back(mie::MieError::file_not_found("/tmp/x.mie"));
    all.push_back(mie::MieError::file_empty("/tmp/x.mie"));
    all.push_back(mie::MieError::file_io("/tmp/x.mie", "Permission denied", 13));
    all.push_back(mie::MieError::invalid_type_word(0x1234, 0x2402, 3));
    all.push_back(mie::MieError::unknown_type_word(0x1234, 0x2499, 0x99));
    all.push_back(mie::MieError::record_truncated(0x1234, 80, 12));
    all.push_back(mie::MieError::first_record_truncated(0x40, 80, 12));
    all.push_back(mie::MieError::payload_error(0x1234, "IRIG day out of range"));
    all.push_back(mie::MieError::unknown_error_code(0x1234, 0x0199));
    all.push_back(mie::MieError::unrecoverable_sync_loss(0x1234, 3));
    all.push_back(mie::MieError::no_valid_records("/tmp/x.mie", 65536));
    all.push_back(mie::MieError::homogeneous_payload("/tmp/x.mie", 0x40, 4));
    all.push_back(mie::MieError::calendar_unavailable("no year was configured"));
    all.push_back(mie::MieError::timestamp_format_mismatch(0x40, 3, 2, 8));
    all.push_back(mie::MieError::writer_error("out.csv", "No space left on device", 28));
    all.push_back(mie::MieError::input_output_collision("/tmp/x.mie"));
    all.push_back(mie::MieError::clobber_refused("out.csv"));
    all.push_back(
        mie::MieError::incompatible_merge_inputs(1, "/tmp/b.mie", "resolves to Standard"));
    all.push_back(mie::MieError::non_monotonic_input(1, "/tmp/b.mie", 500, 400));
    return all;
}

/// Find a kind's expected classification. Fails the calling test if absent,
/// which is how a newly added kind with no contract row is caught.
const Classification* expected_for(mie::MieErrorKind kind) {
    for (std::size_t i = 0; i < kContractRows; ++i) {
        if (kContract[i].kind == kind) {
            return &kContract[i];
        }
    }
    return 0;
}

}  // namespace

TEST_CASE("every error kind is classified exactly as the contract says", "[error][L3-CPP-012]") {
    // Looked up by kind rather than paired by index: the table is grouped by
    // classification because that is how it reads as a contract, and the enum
    // interleaves those groups. Pairing positionally would force one of the two
    // orderings to be arbitrary, and an arbitrary ordering is one nobody
    // maintains correctly.
    const std::vector<mie::MieError> all = every_error();

    for (std::size_t i = 0; i < all.size(); ++i) {
        INFO("kind = " << mie::error_kind_name(all[i].kind()));
        const Classification* expected = expected_for(all[i].kind());
        REQUIRE(expected != 0);
        CHECK(all[i].is_file_error() == expected->is_file);
        CHECK(all[i].is_record_error() == expected->is_record);
    }
}

TEST_CASE("the contract table covers every kind exactly once", "[error][L3-CPP-012]") {
    // The guard that gives the table its teeth. A kind added without a row
    // would otherwise default to "neither predicate" silently -- and a new
    // RECORD error that answered false to is_record_error() would escape the
    // lenient/strict handling that predicate drives, turning a skippable bad
    // record into a hard failure or vice versa.
    const std::size_t kind_count = static_cast<std::size_t>(mie::KIND_NON_MONOTONIC_INPUT) + 1;
    CHECK(kContractRows == kind_count);

    for (std::size_t value = 0; value < kind_count; ++value) {
        const mie::MieErrorKind kind = static_cast<mie::MieErrorKind>(value);
        INFO("kind = " << mie::error_kind_name(kind));
        int rows = 0;
        for (std::size_t i = 0; i < kContractRows; ++i) {
            if (kContract[i].kind == kind) {
                ++rows;
            }
        }
        CHECK(rows == 1);
    }
}

TEST_CASE("every kind has a constructor exercised by these tests", "[error][L3-CPP-012]") {
    // Coverage of the CONSTRUCTORS, not just the table. A kind could otherwise
    // be listed in the contract and never actually built, leaving its message
    // formatting and field retention untested.
    const std::vector<mie::MieError> all = every_error();
    const std::size_t kind_count = static_cast<std::size_t>(mie::KIND_NON_MONOTONIC_INPUT) + 1;
    REQUIRE(all.size() == kind_count);

    for (std::size_t value = 0; value < kind_count; ++value) {
        const mie::MieErrorKind kind = static_cast<mie::MieErrorKind>(value);
        INFO("kind = " << mie::error_kind_name(kind));
        int built = 0;
        for (std::size_t i = 0; i < all.size(); ++i) {
            if (all[i].kind() == kind) {
                ++built;
            }
        }
        CHECK(built == 1);
    }
}

TEST_CASE("no kind is both a file error and a record error", "[error][L3-CPP-012]") {
    const std::vector<mie::MieError> all = every_error();
    for (std::size_t i = 0; i < all.size(); ++i) {
        INFO("kind = " << mie::error_kind_name(all[i].kind()));
        // Parenthesised because Catch2 decomposes the expression and cannot
        // handle a bare && inside an assertion -- it static_asserts rather than
        // miscompiling, so this is a build error, not a silent wrong result.
        CHECK_FALSE((all[i].is_file_error() && all[i].is_record_error()));
    }
}

TEST_CASE("carrying an offset does not make an error a record error", "[error][L3-CPP-012]") {
    // The specific confusion this guards against. Both of these cite where the
    // problem was noticed and then reject the whole file.
    const mie::MieError homogeneous = mie::MieError::homogeneous_payload("/tmp/x.mie", 0x40, 4);
    REQUIRE(homogeneous.offset().has_value());
    CHECK(homogeneous.offset().value() == 0x40);
    CHECK_FALSE(homogeneous.is_record_error());

    const mie::MieError ambiguous = mie::MieError::timestamp_format_mismatch(0x40, 3, 2, 8);
    REQUIRE(ambiguous.offset().has_value());
    CHECK_FALSE(ambiguous.is_record_error());
}

TEST_CASE("record errors carry the offset; file errors do not", "[error]") {
    CHECK(mie::MieError::payload_error(0x2A, "bad").offset().has_value());
    CHECK(mie::MieError::record_truncated(0x2A, 1, 0).offset().has_value());
    CHECK_FALSE(mie::MieError::file_not_found("/tmp/x").offset().has_value());
    CHECK_FALSE(mie::MieError::clobber_refused("out.csv").offset().has_value());
}

TEST_CASE("sync losses are retained only where they mean something", "[error]") {
    // The --allow-partial summary reports the recovery-attempt count, so it has
    // to survive as data rather than only as message text.
    const mie::MieError loss = mie::MieError::unrecoverable_sync_loss(0x100, 7);
    REQUIRE(loss.sync_losses().has_value());
    CHECK(loss.sync_losses().value() == 7);

    CHECK_FALSE(mie::MieError::payload_error(0x100, "x").sync_losses().has_value());
}

TEST_CASE("a broken pipe is recognised from the OS code", "[error][L2-WRT-018]") {
    // `aero1553 decode x.mie | head` is a normal thing to type, and
    // L2-WRT-018 makes it exit 0. The OS code is the only reliable signal --
    // the message text is localised by the OS and cannot be matched on.
#if defined(_WIN32)
    const int broken = 109;  // ERROR_BROKEN_PIPE
    const int other = 5;     // ERROR_ACCESS_DENIED
#else
    const int broken = EPIPE;
    const int other = EACCES;
#endif
    CHECK(mie::MieError::writer_error("stdout", "Broken pipe", broken).is_broken_pipe());
    CHECK_FALSE(
        mie::MieError::writer_error("out.csv", "Permission denied", other).is_broken_pipe());

    SECTION("an input-side broken pipe is recognised too") {
        CHECK(mie::MieError::file_io("/dev/stdin", "Broken pipe", broken).is_broken_pipe());
    }

    SECTION("errors with no OS code behind them are never broken pipes") {
        CHECK_FALSE(mie::MieError::file_not_found("/tmp/x").is_broken_pipe());
        CHECK_FALSE(mie::MieError::payload_error(0, "x").is_broken_pipe());
    }
}

TEST_CASE("what() exposes the same text as message()", "[error]") {
    // MieError is thrown as well as returned, so a handler that only knows
    // std::exception must still get the operator-facing wording.
    const mie::MieError e = mie::MieError::file_empty("/tmp/x.mie");
    CHECK(std::string(e.what()) == e.message());
    CHECK(std::string(e.what()) == "MIE file is empty (0 bytes): /tmp/x.mie");
}

TEST_CASE("copying an error cannot throw", "[error][L3-CPP-012]") {
    // CERT-ERR60-CPP. MieError is thrown, and copying an exception can happen
    // during propagation -- so a copy constructor that allocates can throw
    // mid-unwind, which is std::terminate. Holding the message behind a
    // shared_ptr is what makes this true; a plain std::string member would not.
    //
    // Asserted here rather than left to the linter because the linter only runs
    // on one tier, and a future field added to MieError would break the
    // property silently everywhere else.
    CHECK(std::is_nothrow_copy_constructible<mie::MieError>::value);

    SECTION("and the copy still carries everything") {
        const mie::MieError original = mie::MieError::unrecoverable_sync_loss(0x1234, 7);
        // The copy is the subject, not an accident: this asserts that the
        // shared_ptr-held message and the trivially-copyable fields all survive
        // a copy intact. Eliding it, as the check suggests, would delete the
        // test.
        // NOLINTNEXTLINE(performance-unnecessary-copy-initialization)
        const mie::MieError copy(original);
        CHECK(copy.kind() == original.kind());
        CHECK(copy.message() == original.message());
        REQUIRE(copy.offset().has_value());
        CHECK(copy.offset().value() == 0x1234);
        REQUIRE(copy.sync_losses().has_value());
        CHECK(copy.sync_losses().value() == 7);
    }
}

TEST_CASE("it can be thrown and caught as a std::exception", "[error]") {
    bool caught = false;
    try {
        throw mie::MieError::clobber_refused("out.csv");
    } catch (const std::exception& e) {
        caught = true;
        CHECK(std::string(e.what()).find("Refusing to overwrite") == 0);
    }
    CHECK(caught);
}

// ---------------------------------------------------------------------------
// Message wording -- transcriptions of rust/src/error.rs Display arms
// ---------------------------------------------------------------------------

TEST_CASE("record-level messages match the Rust wording", "[error][message]") {
    CHECK(mie::MieError::invalid_type_word(0x1234, 0x2402, 3).message() ==
          "Record error at offset 0x1234: Invalid Type Word 0x2402 with word_count=3 "
          "(minimum is 5)");

    CHECK(mie::MieError::unknown_type_word(0x1234, 0x2499, 0x99).message() ==
          "Record error at offset 0x1234: Unknown message type 0x99 in Type Word 0x2499. "
          "Known types: 0x01, 0x02, 0x04, 0x08, 0x10, 0x18, 0x20.");

    CHECK(mie::MieError::record_truncated(0x1234, 80, 12).message() ==
          "Record error at offset 0x1234: Record requires 80 bytes but only 12 bytes remain "
          "in file");

    CHECK(mie::MieError::payload_error(0xAB, "IRIG day out of range").message() ==
          "Record error at offset 0xAB: IRIG day out of range");

    CHECK(mie::MieError::unknown_error_code(0x1234, 0x0199).message() ==
          "Record error at offset 0x1234: Unknown error code 0x0199. "
          "Known DDC codes: 0x011E, 0x0120, 0x0136, 0x0140, 0x0150. "
          "Known decoder codes: 0x2000, 0x2001.");
}

TEST_CASE("the offset is uppercase hex with no padding", "[error][message]") {
    // Matches Rust's {offset:X}: hex without zero padding, and uppercase, which
    // is the DDC convention the vendor tooling also uses.
    CHECK(mie::MieError::payload_error(0, "x").message() == "Record error at offset 0x0: x");
    CHECK(mie::MieError::payload_error(255, "x").message() == "Record error at offset 0xFF: x");
    CHECK(mie::MieError::payload_error(0xABCDEF, "x").message() ==
          "Record error at offset 0xABCDEF: x");
}

TEST_CASE("the first-record message is pure ASCII", "[error][message][L2-CLI-014]") {
    // This message used to carry U+2014, written as explicit UTF-8 bytes, and
    // this test used to assert that it did. It now asserts the opposite.
    //
    // A Windows console runs at the OEM code page, not UTF-8, so those three
    // bytes reached an operator as three unrelated CP437 glyphs -- reported,
    // reasonably, as "garbage characters" that looked like memory corruption.
    // L2-CLI-014 now binds stderr prose to the same ASCII rule as stdout
    // payload; scripts/assert-ascii-output.py enforces it across all three
    // implementations. Rust and Python emit the identical wording.
    const std::string msg = mie::MieError::first_record_truncated(0x40, 80, 12).message();
    CHECK(msg ==
          "Record error at offset 0x40: First record after header detection is truncated "
          "-- Type Word declares 80 bytes but only 12 bytes remain in file");
    for (std::string::const_iterator it = msg.begin(); it != msg.end(); ++it) {
        CHECK(static_cast<unsigned char>(*it) < 0x80);
    }
}

TEST_CASE("file-level messages match the Rust wording", "[error][message]") {
    CHECK(mie::MieError::file_not_found("/tmp/x.mie").message() ==
          "MIE file not found: /tmp/x.mie");
    CHECK(mie::MieError::file_empty("/tmp/x.mie").message() ==
          "MIE file is empty (0 bytes): /tmp/x.mie");
    CHECK(mie::MieError::file_io("/tmp/x.mie", "Permission denied", 13).message() ==
          "I/O error on /tmp/x.mie: Permission denied");
    CHECK(mie::MieError::writer_error("out.csv", "No space left on device", 28).message() ==
          "Failed to write to out.csv: No space left on device");
}

TEST_CASE("the sync-loss message names the escape hatch", "[error][message]") {
    // An operator hitting this needs to be told about --allow-partial in the
    // message itself; sending them to the documentation is a worse experience
    // and the other two implementations do not.
    const std::string msg = mie::MieError::unrecoverable_sync_loss(0x1234, 3).message();
    CHECK(msg ==
          "Unrecoverable mid-file sync loss at offset 0x1234 after 3 recovery attempt(s); "
          "the decoder could not reacquire sync within the scan window. Pass --allow-partial "
          "to keep what was decoded as a .partial file.");
}

TEST_CASE("the ambiguity message reports both scores", "[error][message]") {
    // Both scores and the probe count, so an operator can see how close the
    // call was rather than only that it failed.
    CHECK(mie::MieError::timestamp_format_mismatch(0x40, 3, 2, 8).message() ==
          "Timestamp-format auto-detection is ambiguous starting at offset 0x40 "
          "(IRIG score: 3, Standard score: 2 over 8 record(s) probed). "
          "Pass --input-time-format irig or --input-time-format standard to force the choice, "
          "or verify the file is actually an MIE recording.");

    SECTION("a negative score is rendered with its sign") {
        const std::string msg = mie::MieError::timestamp_format_mismatch(0, -4, 2, 8).message();
        CHECK(msg.find("IRIG score: -4") != std::string::npos);
    }
}

TEST_CASE("kind names are distinct and non-empty", "[error]") {
    // Diagnostics only, but a duplicated or empty name makes a failure report
    // ambiguous exactly when it is being read under pressure.
    for (std::size_t i = 0; i < kContractRows; ++i) {
        const std::string name = mie::error_kind_name(kContract[i].kind);
        CHECK_FALSE(name.empty());
        for (std::size_t j = i + 1; j < kContractRows; ++j) {
            CHECK(name != std::string(mie::error_kind_name(kContract[j].kind)));
        }
    }
}
