// SPDX-License-Identifier: Apache-2.0
//
// The decoder's single error type.
//
// Mirrors `rust/src/error.rs`: one type with a kind discriminant, rather than
// the Python implementation's class hierarchy. That choice is pinned by
// L3-CPP-012, and the reason to follow Rust here rather than Python is that
// C++ exception hierarchies invite `catch` by base class, which is exactly the
// classification the two predicates below are meant to make explicit.
//
// WHY NOT A TAGGED UNION OF EIGHTEEN FIELD SETS
//
// The Rust enum carries per-variant payloads (`path`, `raw_type_word`,
// `record_bytes`, `irig_score`, ...). Reproducing that in C++11 -- no
// std::variant, no pattern matching -- means eighteen structs and a manual
// discriminated union, and nothing would read those fields back.
//
// Every payload field has exactly one consumer: the formatted message. So the
// message is formatted at construction, by a named constructor per variant that
// takes precisely that variant's fields, and only the three values with a
// SECOND consumer are also stored:
//
//   * `offset`       -- diagnostics and DEBUG logging
//   * `sync_losses`  -- the --allow-partial summary line
//   * broken pipe    -- L2-WRT-018 routes it to exit 0, so the CLI must be able
//                       to ask, and the OS code it depends on is not
//                       recoverable from the message text
//
// The upshot is that the named constructors are the schema. Adding a variant
// means adding one constructor and one kind, and the compiler will not let a
// caller build the error without supplying that variant's fields.

#ifndef MIE_ERROR_HPP
#define MIE_ERROR_HPP

#include <cstdint>
#include <exception>
#include <memory>
#include <string>

#include "mie/optional.hpp"

namespace mie {

/// Which failure occurred. The CLI maps these to exit codes (L2-CLI-011); see
/// docs/ERROR-CATALOG.md for the operator-facing table.
enum MieErrorKind {
    KIND_FILE_NOT_FOUND,
    KIND_FILE_EMPTY,
    KIND_FILE_IO,
    KIND_INVALID_TYPE_WORD,
    KIND_UNKNOWN_TYPE_WORD,
    KIND_RECORD_TRUNCATED,
    KIND_FIRST_RECORD_TRUNCATED,
    KIND_PAYLOAD_ERROR,
    KIND_UNKNOWN_ERROR_CODE,
    KIND_NO_VALID_RECORDS,
    KIND_HOMOGENEOUS_PAYLOAD,
    KIND_WRITER_ERROR,
    KIND_INPUT_OUTPUT_COLLISION,
    KIND_CLOBBER_REFUSED,
    KIND_UNRECOVERABLE_SYNC_LOSS,
    KIND_MERGE_INPUTS_DROPPED,
    KIND_TIMESTAMP_FORMAT_MISMATCH,
    KIND_CALENDAR_UNAVAILABLE,
    KIND_INCOMPATIBLE_MERGE_INPUTS,
    KIND_NON_MONOTONIC_INPUT
};

/// Name of a kind, for diagnostics and for the test that pins the
/// classification. Not operator-facing text -- that is `what()`.
const char* error_kind_name(MieErrorKind kind);

class MieError : public std::exception {
  public:
    // --- File-level failures ---------------------------------------------

    static MieError file_not_found(const std::string& path);
    static MieError file_empty(const std::string& path);

    /// `os_code` is errno or GetLastError; it is retained only so that
    /// `is_broken_pipe()` can answer without re-parsing the message.
    static MieError file_io(const std::string& path, const std::string& os_message, int os_code);

    static MieError no_valid_records(const std::string& path, uint64_t scan_bytes);
    static MieError homogeneous_payload(const std::string& path, uint64_t offset,
                                        uint32_t sample_records);
    static MieError timestamp_format_mismatch(uint64_t offset, int32_t irig_score,
                                              int32_t std_score, uint32_t records_probed);

    /// L2-WRT-026: a calendar rendering (`iso` / `dom`) was requested but the
    /// recording cannot supply a calendar date -- it is Standard-format or
    /// freerun IRIG (clause 2), or a record's day-of-year does not exist in the
    /// configured year (clause 3). `detail` names the specific reason.
    ///
    /// Exit class 2, the "the file cannot satisfy this request" class it shares
    /// with `timestamp_format_mismatch`.
    static MieError calendar_unavailable(const std::string& detail);

    // --- Record-level failures -------------------------------------------

    static MieError invalid_type_word(uint64_t offset, uint16_t raw_type_word, uint16_t word_count);
    static MieError unknown_type_word(uint64_t offset, uint16_t raw_type_word,
                                      uint8_t message_type);
    static MieError record_truncated(uint64_t offset, uint64_t record_bytes,
                                     uint64_t available_bytes);
    static MieError first_record_truncated(uint64_t offset, uint64_t record_bytes,
                                           uint64_t available_bytes);
    static MieError payload_error(uint64_t offset, const std::string& detail);
    static MieError unknown_error_code(uint64_t offset, uint16_t error_code);
    static MieError unrecoverable_sync_loss(uint64_t offset, uint64_t sync_losses);

    /// How an `--allow-partial` merge ends when an input was left out at open
    /// or while priming (L2-MRG-004). Record-class although it cites no record:
    /// it replaces the `unrecoverable_sync_loss` such a merge used to end with,
    /// and keeps that class so code catching it does not break.
    static MieError merge_inputs_dropped(uint64_t left_out, uint64_t truncated, uint64_t total);

    // --- Destination guards and output ------------------------------------

    static MieError writer_error(const std::string& destination, const std::string& os_message,
                                 int os_code);
    static MieError input_output_collision(const std::string& path);
    static MieError clobber_refused(const std::string& path);

    // --- Merge ------------------------------------------------------------

    static MieError incompatible_merge_inputs(std::size_t file_index, const std::string& path,
                                              const std::string& detail);
    static MieError non_monotonic_input(std::size_t file_index, const std::string& path,
                                        uint64_t prev_us, uint64_t curr_us);

    // --- Accessors --------------------------------------------------------

    MieErrorKind kind() const { return kind_; }

    /// The operator-facing message. Identical in wording to the Rust
    /// implementation's Display output, because conformance cases compare
    /// stderr text across implementations.
    const std::string& message() const { return *message_; }

    const char* what() const throw() override { return message_->c_str(); }

    /// Byte offset of the failing record, when the failure has one.
    ///
    /// Carrying an offset is NOT the same as being a record error:
    /// HomogeneousPayload and TimestampFormatMismatch both cite one while
    /// rejecting the file as a whole, and both answer false below.
    Optional<uint64_t> offset() const { return offset_; }

    /// Cumulative recovery attempts, on UnrecoverableSyncLoss only.
    Optional<uint64_t> sync_losses() const { return sync_losses_; }

    /// True when this wraps a broken-pipe condition. L2-WRT-018 makes that
    /// exit 0 with no error -- `aero1553 decode x.mie | head` is a normal
    /// thing to type, not a failure.
    bool is_broken_pipe() const { return broken_pipe_; }

    /// Input I/O only, matching `MieError::is_file_error()` in Rust.
    ///
    /// Deliberately NARROWER than Python's `MieFileError`, which also covers
    /// the whole-file rejections and the destination guards. Those answer false
    /// to BOTH predicates here; match on kind() for them. The asymmetry is
    /// documented in docs/ERROR-CATALOG.md and pinned by test on all three
    /// implementations, so it cannot drift unnoticed.
    bool is_file_error() const;

    /// Tied to a specific record. Matches Python's `MieRecordError` exactly,
    /// and Rust's `is_record_error()`.
    bool is_record_error() const;

    // NOT `= default`: cppcheck 2.13 cannot parse a defaulted destructor that
    // carries a dynamic exception specification and reports internalAstError,
    // which degrades its analysis of the whole translation unit. The empty body
    // is identical in behaviour. (Sonar cpp:S3490 flags this; the trade is
    // deliberate -- a parser error costs more than a style finding.)
    ~MieError() throw() override {}

  private:
    MieError(MieErrorKind kind, const std::string& message);

    MieErrorKind kind_;

    /// The message is held behind a shared_ptr rather than as a std::string
    /// member, and that is not an optimisation.
    ///
    /// MieError is thrown. Copying an exception can happen during propagation,
    /// and a std::string member makes the copy constructor allocate -- so it can
    /// throw, mid-unwind, which is std::terminate. CERT-ERR60-CPP is the rule;
    /// clang-tidy caught this, and it was right to.
    ///
    /// The usual answers are both bad here: a fixed char buffer would truncate
    /// messages that are deliberately detailed (several name the flag that
    /// resolves the problem), and suppressing the check would leave the hazard
    /// in place. Copying a shared_ptr is noexcept, so this keeps the full
    /// message AND makes the whole type nothrow-copy-constructible -- every
    /// other member is trivially copyable, so the implicit copy constructor
    /// inherits that.
    ///
    /// Never null. Every constructor sets it, and what() dereferences it.
    std::shared_ptr<const std::string> message_;

    Optional<uint64_t> offset_;
    Optional<uint64_t> sync_losses_;
    bool broken_pipe_;
};

}  // namespace mie

#endif  // MIE_ERROR_HPP
