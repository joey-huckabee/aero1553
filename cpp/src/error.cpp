// SPDX-License-Identifier: Apache-2.0
//
// Message wording here is CONTRACT, not prose. Conformance cases compare
// stderr across implementations, so each string below is a transcription of the
// corresponding `Display` arm in rust/src/error.rs. Rewording one without
// changing the other two breaks a gate, which is the intended outcome.

#include "mie/error.hpp"

#include "mie/text.hpp"

#if defined(_WIN32)
// ERROR_BROKEN_PIPE / ERROR_NO_DATA, spelled numerically so this file does not
// have to include <windows.h> and trip the platform-confinement gate. Windows
// reports a closed pipe as 109 when writing to a pipe whose read end is gone,
// and as 232 when the pipe is being closed.
#else
#include <cerrno>
#endif

// Unconditional: std::make_shared is used below on every platform.
#include <memory>

namespace mie {

namespace {

/// `Record error at offset 0xNNNN: ` -- the shared prefix every record-level
/// message carries. Uppercase hex with no zero padding, matching Rust's
/// `{offset:X}` and the DDC convention of citing offsets in hex.
std::string record_prefix(uint64_t offset) {
    return "Record error at offset 0x" + text::hex_upper(offset, 1) + ": ";
}

bool os_code_is_broken_pipe(int os_code) {
#if defined(_WIN32)
    const int kErrorBrokenPipe = 109;
    const int kErrorNoData = 232;
    return os_code == kErrorBrokenPipe || os_code == kErrorNoData;
#else
    return os_code == EPIPE;
#endif
}

}  // namespace

const char* error_kind_name(MieErrorKind kind) {
    switch (kind) {
        case KIND_FILE_NOT_FOUND: return "FileNotFound";
        case KIND_FILE_EMPTY: return "FileEmpty";
        case KIND_FILE_IO: return "FileIo";
        case KIND_INVALID_TYPE_WORD: return "InvalidTypeWord";
        case KIND_UNKNOWN_TYPE_WORD: return "UnknownTypeWord";
        case KIND_RECORD_TRUNCATED: return "RecordTruncated";
        case KIND_FIRST_RECORD_TRUNCATED: return "FirstRecordTruncated";
        case KIND_PAYLOAD_ERROR: return "PayloadError";
        case KIND_UNKNOWN_ERROR_CODE: return "UnknownErrorCode";
        case KIND_NO_VALID_RECORDS: return "NoValidRecords";
        case KIND_HOMOGENEOUS_PAYLOAD: return "HomogeneousPayload";
        case KIND_WRITER_ERROR: return "WriterError";
        case KIND_INPUT_OUTPUT_COLLISION: return "InputOutputCollision";
        case KIND_CLOBBER_REFUSED: return "ClobberRefused";
        case KIND_UNRECOVERABLE_SYNC_LOSS: return "UnrecoverableSyncLoss";
        case KIND_MERGE_INPUTS_DROPPED: return "MergeInputsDropped";
        case KIND_TIMESTAMP_FORMAT_MISMATCH: return "TimestampFormatMismatch";
        case KIND_CALENDAR_UNAVAILABLE: return "CalendarUnavailable";
        case KIND_INCOMPATIBLE_MERGE_INPUTS: return "IncompatibleMergeInputs";
        case KIND_NON_MONOTONIC_INPUT: return "NonMonotonicInput";
        default: return "Unknown";
    }
}

MieError::MieError(MieErrorKind kind, const std::string& message)
    : kind_(kind), message_(std::make_shared<std::string>(message)), broken_pipe_(false) {}

// --- File-level -----------------------------------------------------------

MieError MieError::file_not_found(const std::string& path) {
    return MieError(KIND_FILE_NOT_FOUND, "MIE file not found: " + path);
}

MieError MieError::file_empty(const std::string& path) {
    return MieError(KIND_FILE_EMPTY, "MIE file is empty (0 bytes): " + path);
}

MieError MieError::file_io(const std::string& path, const std::string& os_message, int os_code) {
    MieError e(KIND_FILE_IO, "I/O error on " + path + ": " + os_message);
    e.broken_pipe_ = os_code_is_broken_pipe(os_code);
    return e;
}

MieError MieError::no_valid_records(const std::string& path, uint64_t scan_bytes) {
    return MieError(KIND_NO_VALID_RECORDS,
                    "No valid MIE records found in " + path + " (scanned first " +
                        text::decimal(scan_bytes) +
                        " bytes). The file may not be an MIE recording, or the records may "
                        "begin past the scan window.");
}

MieError MieError::homogeneous_payload(const std::string& path, uint64_t offset,
                                       uint32_t sample_records) {
    MieError e(KIND_HOMOGENEOUS_PAYLOAD,
               "Pathological homogeneous-payload input rejected (" + path + "): the first " +
                   text::decimal(sample_records) + " candidate records starting at offset 0x" +
                   text::hex_upper(offset, 1) +
                   " are byte-identical in non-timestamp positions. The file is most likely a "
                   "single-byte pad (e.g. 0x20-fill), not an MIE recording.");
    // Cites an offset but rejects the whole file, so it is deliberately NOT a
    // record error -- see the predicates below.
    e.offset_ = offset;
    return e;
}

MieError MieError::timestamp_format_mismatch(uint64_t offset, int32_t irig_score, int32_t std_score,
                                             uint32_t records_probed) {
    MieError e(
        KIND_TIMESTAMP_FORMAT_MISMATCH,
        "Timestamp-format auto-detection is ambiguous starting at offset 0x" +
            text::hex_upper(offset, 1) + " (IRIG score: " + text::decimal_signed(irig_score) +
            ", Standard score: " + text::decimal_signed(std_score) + " over " +
            text::decimal(records_probed) +
            " record(s) probed). Pass --input-time-format irig or --input-time-format standard to "
            "force the choice, or verify the file is actually an MIE recording.");
    e.offset_ = offset;
    return e;
}

MieError MieError::calendar_unavailable(const std::string& detail) {
    return MieError(KIND_CALENDAR_UNAVAILABLE,
                    "Cannot render a calendar timestamp: " + detail +
                        ". IRIG-B carries no year and no timezone, so a calendar rendering is "
                        "refused rather than guessed at. Use --output-time-format doy to write "
                        "the day-of-year form instead.");
}

// --- Record-level ---------------------------------------------------------

MieError MieError::invalid_type_word(uint64_t offset, uint16_t raw_type_word, uint16_t word_count) {
    MieError e(KIND_INVALID_TYPE_WORD,
               record_prefix(offset) + "Invalid Type Word 0x" + text::hex_upper(raw_type_word, 4) +
                   " with word_count=" + text::decimal(word_count) + " (minimum is 5)");
    e.offset_ = offset;
    return e;
}

MieError MieError::unknown_type_word(uint64_t offset, uint16_t raw_type_word,
                                     uint8_t message_type) {
    MieError e(KIND_UNKNOWN_TYPE_WORD,
               record_prefix(offset) + "Unknown message type 0x" +
                   text::hex_upper(message_type, 2) + " in Type Word 0x" +
                   text::hex_upper(raw_type_word, 4) +
                   ". Known types: 0x01, 0x02, 0x04, 0x08, 0x10, 0x18, 0x20.");
    e.offset_ = offset;
    return e;
}

MieError MieError::record_truncated(uint64_t offset, uint64_t record_bytes,
                                    uint64_t available_bytes) {
    MieError e(KIND_RECORD_TRUNCATED, record_prefix(offset) + "Record requires " +
                                          text::decimal(record_bytes) + " bytes but only " +
                                          text::decimal(available_bytes) + " bytes remain in file");
    e.offset_ = offset;
    return e;
}

MieError MieError::first_record_truncated(uint64_t offset, uint64_t record_bytes,
                                          uint64_t available_bytes) {
    // The em dash is U+2014, written as explicit UTF-8 bytes so the source
    // stays ASCII and cannot be re-encoded by an editor or a compiler flag.
    // Rust emits the same character here, and the two strings are compared.
    MieError e(KIND_FIRST_RECORD_TRUNCATED,
               record_prefix(offset) +
                   "First record after header detection is truncated -- Type Word "
                   "declares " +
                   text::decimal(record_bytes) + " bytes but only " +
                   text::decimal(available_bytes) + " bytes remain in file");
    e.offset_ = offset;
    return e;
}

MieError MieError::payload_error(uint64_t offset, const std::string& detail) {
    MieError e(KIND_PAYLOAD_ERROR, record_prefix(offset) + detail);
    e.offset_ = offset;
    return e;
}

MieError MieError::unknown_error_code(uint64_t offset, uint16_t error_code) {
    MieError e(KIND_UNKNOWN_ERROR_CODE,
               record_prefix(offset) + "Unknown error code 0x" + text::hex_upper(error_code, 4) +
                   ". Known DDC codes: 0x011E, 0x0120, 0x0136, 0x0140, 0x0150. "
                   "Known decoder codes: 0x2000, 0x2001.");
    e.offset_ = offset;
    return e;
}

MieError MieError::unrecoverable_sync_loss(uint64_t offset, uint64_t sync_losses) {
    MieError e(KIND_UNRECOVERABLE_SYNC_LOSS,
               "Unrecoverable mid-file sync loss at offset 0x" + text::hex_upper(offset, 1) +
                   " after " + text::decimal(sync_losses) +
                   " recovery attempt(s); the decoder could not reacquire sync within the scan "
                   "window. Pass --allow-partial with an output file (-o) to keep what was "
                   "decoded as a .partial file.");
    e.offset_ = offset;
    e.sync_losses_ = sync_losses;
    return e;
}

MieError MieError::merge_inputs_dropped(uint64_t left_out, uint64_t truncated, uint64_t total) {
    std::string summary = text::decimal(left_out) + " of " + text::decimal(total) +
                          " merge inputs could not be read and " +
                          (left_out == 1 ? "was" : "were") + " left out";
    if (truncated > 0) {
        summary += ", and " + text::decimal(truncated) + " " + (truncated == 1 ? "was" : "were") +
                   " truncated by an unrecoverable sync loss";
    }
    summary += " (see the per-input warnings above)";
    return MieError(KIND_MERGE_INPUTS_DROPPED, summary);
}

// --- Output ---------------------------------------------------------------

MieError MieError::writer_error(const std::string& destination, const std::string& os_message,
                                int os_code) {
    MieError e(KIND_WRITER_ERROR, "Failed to write to " + destination + ": " + os_message);
    // The one field that must survive as data rather than text: L2-WRT-018
    // turns a broken pipe into exit 0, and `decode x.mie | head` has to be a
    // normal thing to type rather than a failure.
    e.broken_pipe_ = os_code_is_broken_pipe(os_code);
    return e;
}

MieError MieError::input_output_collision(const std::string& path) {
    return MieError(KIND_INPUT_OUTPUT_COLLISION,
                    "Output path resolves to the same file as the input (" + path +
                        "); decoding in-place is unsafe with a memory-mapped reader. Choose a "
                        "different output path.");
}

MieError MieError::clobber_refused(const std::string& path) {
    return MieError(KIND_CLOBBER_REFUSED,
                    "Refusing to overwrite existing file " + path +
                        " (--no-clobber or output.no_clobber is set). Remove the file or unset "
                        "the flag to proceed.");
}

// --- Merge ----------------------------------------------------------------

MieError MieError::incompatible_merge_inputs(std::size_t file_index, const std::string& path,
                                             const std::string& detail) {
    return MieError(KIND_INCOMPATIBLE_MERGE_INPUTS,
                    "Cannot time-merge input #" + text::decimal(file_index) + " (" + path +
                        "): " + detail +
                        ". Multi-file merge requires every input to be calendar-locked IRIG "
                        "(Standard-format, freerun IRIG, and mixed-format sets cannot be ordered "
                        "on a common absolute timeline).");
}

MieError MieError::non_monotonic_input(std::size_t file_index, const std::string& path,
                                       uint64_t prev_us, uint64_t curr_us) {
    return MieError(KIND_NON_MONOTONIC_INPUT,
                    "merge: input #" + text::decimal(file_index) + " (" + path +
                        ") is not internally time-sorted: timestamp stepped backward (prev_us=" +
                        text::decimal(prev_us) + " curr_us=" + text::decimal(curr_us) +
                        "). The time-merge assumes each input is in chronological capture order.");
}

// --- Classification -------------------------------------------------------

bool MieError::is_file_error() const {
    switch (kind_) {
        case KIND_FILE_NOT_FOUND:
        case KIND_FILE_EMPTY:
        case KIND_FILE_IO: return true;
        default: return false;
    }
}

bool MieError::is_record_error() const {
    switch (kind_) {
        case KIND_INVALID_TYPE_WORD:
        case KIND_UNKNOWN_TYPE_WORD:
        case KIND_RECORD_TRUNCATED:
        case KIND_FIRST_RECORD_TRUNCATED:
        case KIND_PAYLOAD_ERROR:
        case KIND_UNKNOWN_ERROR_CODE:
        case KIND_UNRECOVERABLE_SYNC_LOSS:
        case KIND_MERGE_INPUTS_DROPPED: return true;
        default: return false;
    }
}

}  // namespace mie
