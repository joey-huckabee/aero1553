// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::reader"

#include "mie/reader.hpp"

#include <algorithm>
#include <utility>
#include <vector>

#include "mie/log.hpp"
#include "mie/text.hpp"

namespace mie {

namespace {

/// `0x` + uppercase hex, unpadded -- the `0x{:X}` spelling used throughout the
/// Rust reader's diagnostics.
std::string hex(uint64_t value) { return "0x" + text::hex_upper(value, 1); }

/// `0x` + hex zero-padded to `width`, for the fields whose width is meaningful
/// (a Type Word is always four digits, a message type always two).
std::string hexw(uint64_t value, std::size_t width) { return "0x" + text::hex_upper(value, width); }

std::string dec(uint64_t value) { return text::decimal(value); }

/// DEBUG-only hex dump of the bytes around a validation failure.
///
/// Guarded on the level before doing any of the work, not just before emitting
/// it: building the dump is a loop and an allocation, and a sync loss inside a
/// badly corrupted file can happen thousands of times.
void log_validation_context(const uint8_t* data, std::size_t size, std::size_t offset) {
    if (!log::enabled(log::LEVEL_DEBUG)) {
        return;
    }
    const std::size_t start = offset > 16 ? offset - 16 : 0;
    std::size_t end = start + 32;
    if (end > size) {
        end = size;
    }
    std::string dump;
    dump.reserve((end - start) * 3);
    for (std::size_t i = start; i < end; ++i) {
        if (i != start) {
            dump += " ";
        }
        dump += text::hex_upper(data[i], 2);
    }
    MIE_LOG_DEBUG("validation context at " + hex(offset) + " (bytes " + hex(start) + ".." +
                  hex(end) + ", max 32): " + dump);
}

/// Read `count` data words at `at`, capped at the bus standard's 32.
///
/// A read that does not fit leaves the payload EMPTY rather than partially
/// filled. That is the deliberate choice: a half-read payload is
/// indistinguishable downstream from a genuinely short one, so it would present
/// corruption as data.
void read_data_words(const uint8_t* data, std::size_t size, std::size_t at, std::size_t count,
                     uint16_t* buf, DataWords& out) {
    const std::size_t capped = count < MAX_DATA_WORDS ? count : MAX_DATA_WORDS;
    if (decode::read_u16_array(data, size, at, capped, buf)) {
        out = DataWords::from_words(buf, capped);
    }
}

/// Per-format payload extraction: the second Command Word (RT-to-RT only),
/// the primary and secondary Status Words, and the data words.
///
/// `size` bounds every read to the CURRENT RECORD, not to the file. An
/// over-claiming Command Word would otherwise read words belonging to the next
/// record and present them as this one's payload -- which decodes without
/// complaint and is wrong in a way no downstream check would catch.
void extract_payload(const uint8_t* data, std::size_t size, std::size_t p, MessageFormat fmt,
                     const CommandWord& cmd, Optional<CommandWord>& out_cmd2,
                     Optional<uint16_t>& out_status, Optional<uint16_t>& out_status2,
                     DataWords& out_words) {
    out_cmd2 = none();
    out_status = none();
    out_status2 = none();
    out_words.clear();

    uint16_t buf[MAX_DATA_WORDS];
    uint16_t word = 0;

    switch (fmt) {
        case FORMAT_RECEIVE: {
            const std::size_t n = cmd.data_word_count;
            read_data_words(data, size, p, n, buf, out_words);
            if (decode::read_u16(data, size, p + n * 2, word)) {
                out_status = word;
            }
            break;
        }
        case FORMAT_TRANSMIT: {
            if (decode::read_u16(data, size, p, word)) {
                out_status = word;
            }
            read_data_words(data, size, p + 2, cmd.data_word_count, buf, out_words);
            break;
        }
        case FORMAT_RT_TO_RT: {
            uint16_t cmd2_raw = 0;
            static_cast<void>(decode::read_u16(data, size, p, cmd2_raw));
            const CommandWord cmd2 = decode::decode_command_word(cmd2_raw);
            out_cmd2 = cmd2;
            if (decode::read_u16(data, size, p + 2, word)) {
                out_status = word;
            }
            const std::size_t n = cmd2.data_word_count;
            read_data_words(data, size, p + 4, n, buf, out_words);
            if (decode::read_u16(data, size, p + 4 + n * 2, word)) {
                out_status2 = word;
            }
            break;
        }
        case FORMAT_RECEIVE_BROADCAST:
            read_data_words(data, size, p, cmd.data_word_count, buf, out_words);
            break;
        case FORMAT_RT_TO_RT_BROADCAST: {
            uint16_t cmd2_raw = 0;
            static_cast<void>(decode::read_u16(data, size, p, cmd2_raw));
            const CommandWord cmd2 = decode::decode_command_word(cmd2_raw);
            out_cmd2 = cmd2;
            if (decode::read_u16(data, size, p + 2, word)) {
                out_status = word;
            }
            read_data_words(data, size, p + 4, cmd2.data_word_count, buf, out_words);
            break;
        }
        case FORMAT_MODE_CODE_TX_DATA: {
            if (decode::read_u16(data, size, p, word)) {
                out_status = word;
            }
            if (decode::read_u16(data, size, p + 2, word)) {
                out_words = DataWords::from_words(&word, 1);
            }
            break;
        }
        case FORMAT_MODE_CODE_RX_DATA: {
            if (decode::read_u16(data, size, p, word)) {
                out_words = DataWords::from_words(&word, 1);
            }
            if (decode::read_u16(data, size, p + 2, word)) {
                out_status = word;
            }
            break;
        }
        case FORMAT_MODE_CODE_NO_DATA:
            if (decode::read_u16(data, size, p, word)) {
                out_status = word;
            }
            break;
        case FORMAT_MODE_CODE_BCAST_NO_DATA: break;
        case FORMAT_MODE_CODE_BCAST_DATA:
            if (decode::read_u16(data, size, p, word)) {
                out_words = DataWords::from_words(&word, 1);
            }
            break;
        case FORMAT_SPURIOUS_DATA: break;
    }
}

}  // namespace

// ---------------------------------------------------------------------------
// ReaderOptions
// ---------------------------------------------------------------------------

ReaderOptions::ReaderOptions()
    : strict(false),
      input_time_format(TIMESTAMP_AUTO),
      detect_records(decode::DEFAULT_DETECT_RECORDS),
      lookahead_records(sync::DEFAULT_LOOKAHEAD_RECORDS),
      mux_enabled(decode::DEFAULT_MUX_ENABLED),
      mux_delimiter(decode::DEFAULT_MUX_DELIMITER),
      mux_field(decode::DEFAULT_MUX_FIELD),
      calendar_year() {}

// ---------------------------------------------------------------------------
// MieFileReader
// ---------------------------------------------------------------------------

MieFileReader::MieFileReader()
    : file_size_(0),
      strict_(false),
      input_time_format_(TIMESTAMP_AUTO),
      detect_records_(decode::DEFAULT_DETECT_RECORDS),
      lookahead_records_(sync::DEFAULT_LOOKAHEAD_RECORDS),
      sync_losses_(0),
      empty_recording_(false) {}

MieFileReader::~MieFileReader() = default;

void MieFileReader::open(const std::string& path, const ReaderOptions& options) {
    path_ = path;

    if (!platform::path_exists(path)) {
        throw MieError::file_not_found(path);
    }

    platform::OsError os_err;
    uint64_t size = 0;
    bool is_regular = false;
    if (!platform::file_metadata(path, size, is_regular, os_err)) {
        throw MieError::file_io(path, os_err.message, os_err.code);
    }
    // L2-RDR-006. The `is_regular` guard is what keeps a DIRECTORY out of this
    // branch: on Windows a directory reports a zero size, and reporting "empty
    // recording" for one would send the operator looking for a capture problem
    // instead of a typo. A non-regular path falls through and the mapping
    // reports the I/O failure it really is.
    if (is_regular && size == 0) {
        throw MieError::file_empty(path);
    }

    if (!mapping_.open(path, os_err)) {
        throw MieError::file_io(path, os_err.message, os_err.code);
    }
    file_size_ = mapping_.size();

    strict_ = options.strict;
    input_time_format_ = options.input_time_format;
    detect_records_ = options.detect_records > 0 ? options.detect_records : 1;
    lookahead_records_ = options.lookahead_records > 0 ? options.lookahead_records : 1;
    standard_tick_rate_hz_ = options.standard_tick_rate_hz;
    calendar_year_ = options.calendar_year;

    MIE_LOG_DEBUG("reader opened " + path_ + " (" + dec(file_size_) +
                  " bytes, strict=" + (strict_ ? "true" : "false") +
                  ", input_time_format=" + timestamp_format_name(input_time_format_) +
                  ", detect_records=" + dec(detect_records_) + ")");

    // L2-WRT-020: resolve MUX once, from the file NAME. Once per file, not once
    // per record -- the value cannot change mid-file, and every message then
    // shares the one string by pointer.
    mux_.reset();
    if (options.mux_enabled) {
        std::string mux_value;
        if (decode::mux_from_filename(platform::path_filename(path_), options.mux_delimiter,
                                      options.mux_field, mux_value)) {
            mux_.reset(new std::string(mux_value));
        }
    }
}

TimestampFormat MieFileReader::default_resolved_format() const {
    return input_time_format_ == TIMESTAMP_AUTO ? TIMESTAMP_IRIG : input_time_format_;
}

TimestampFormat MieFileReader::resolve_format_for_hit(const sync::ScanHit& hit,
                                                      PendingError& error) const {
    const uint8_t* data = mapping_.data();
    const auto size = static_cast<std::size_t>(file_size_);

    uint16_t candidate_type_raw = 0;
    static_cast<void>(decode::read_u16(data, size, hit.offset, candidate_type_raw));
    const TypeWord candidate_tw = decode::decode_type_word(candidate_type_raw);
    const std::size_t candidate_record_bytes =
        static_cast<std::size_t>(candidate_tw.word_count) * 2;

    if (sync::is_homogeneous_payload(data, size, hit.offset, candidate_record_bytes)) {
        MIE_LOG_ERROR("pathological homogeneous-payload input at offset " + hex(hit.offset) +
                      " in " + path_ + ": " + dec(sync::HOMOGENEITY_SAMPLE_RECORDS) +
                      " consecutive candidate records are byte-identical");
        error.reset(new MieError(MieError::homogeneous_payload(
            path_, static_cast<uint64_t>(hit.offset),
            static_cast<uint32_t>(sync::HOMOGENEITY_SAMPLE_RECORDS))));
        return default_resolved_format();
    }

    if (input_time_format_ == TIMESTAMP_AUTO) {
        return resolve_auto_format(hit, error);
    }
    return check_forced_format(hit, error);
}

TimestampFormat MieFileReader::resolve_auto_format(const sync::ScanHit& hit,
                                                   PendingError& error) const {
    const decode::DetectionOutcome outcome = decode::probe_timestamp_format(
        mapping_.data(), static_cast<std::size_t>(file_size_), hit.offset, detect_records_);

    const std::string scores = "IRIG=" + text::decimal_signed(outcome.irig_score) +
                               " STD=" + text::decimal_signed(outcome.std_score) + " over " +
                               dec(outcome.records_probed) + " record(s)";

    switch (outcome.confidence) {
        case decode::CONFIDENCE_DECISIVE:
            MIE_LOG_INFO(std::string("auto-detected timestamp format: ") +
                         timestamp_format_name(outcome.format) + " (Decisive: " + scores + ")");
            break;
        case decode::CONFIDENCE_MARGINAL:
            MIE_LOG_INFO(std::string("auto-detected timestamp format: ") +
                         timestamp_format_name(outcome.format) + " (Marginal: " + scores +
                         ") -- pass --input-time-format to force the choice if this is wrong");
            break;
        case decode::CONFIDENCE_AMBIGUOUS:
            if (strict_) {
                MIE_LOG_ERROR("timestamp-format auto-detection is ambiguous in " + path_ +
                              " starting at offset " + hex(hit.offset) + ": " + scores +
                              " -- strict mode rejects ambiguous files; pass "
                              "--input-time-format to force the choice");
                error.reset(new MieError(MieError::timestamp_format_mismatch(
                    static_cast<uint64_t>(hit.offset), outcome.irig_score, outcome.std_score,
                    static_cast<uint32_t>(outcome.records_probed))));
            } else {
                MIE_LOG_WARN(std::string("auto-detected timestamp format: ") +
                             timestamp_format_name(outcome.format) + " (Ambiguous: " + scores +
                             ") -- using best guess; pass --input-time-format to force the "
                             "choice or --strict to reject ambiguous files");
            }
            break;
    }
    return outcome.format;
}

TimestampFormat MieFileReader::check_forced_format(const sync::ScanHit& hit,
                                                   PendingError& error) const {
    const decode::DetectionOutcome outcome = decode::probe_timestamp_format(
        mapping_.data(), static_cast<std::size_t>(file_size_), hit.offset, detect_records_);

    // L2-DEC-013: only a DECISIVE probe for the other format counts as a
    // contradiction. A marginal or ambiguous disagreement is exactly the case
    // the operator passed --input-time-format to settle, so overriding them there
    // would defeat the flag.
    const bool contradicts =
        outcome.confidence == decode::CONFIDENCE_DECISIVE && outcome.format != input_time_format_;
    if (!contradicts) {
        return input_time_format_;
    }

    const std::string scores = "IRIG=" + text::decimal_signed(outcome.irig_score) +
                               " STD=" + text::decimal_signed(outcome.std_score) + " over " +
                               dec(outcome.records_probed) + " record(s)";

    if (strict_) {
        MIE_LOG_ERROR(std::string("forced timestamp format ") +
                      timestamp_format_name(input_time_format_) + " contradicts the recording in " +
                      path_ + " at offset " + hex(hit.offset) + ": detection is decisive for " +
                      timestamp_format_name(outcome.format) + " (" + scores +
                      ") -- strict mode rejects the mismatch; drop --input-time-format to "
                      "auto-detect");
        error.reset(new MieError(MieError::timestamp_format_mismatch(
            static_cast<uint64_t>(hit.offset), outcome.irig_score, outcome.std_score,
            static_cast<uint32_t>(outcome.records_probed))));
    } else {
        MIE_LOG_WARN(
            std::string("forced timestamp format ") + timestamp_format_name(input_time_format_) +
            " contradicts the recording at offset " + hex(hit.offset) +
            ": detection is decisive for " + timestamp_format_name(outcome.format) + " (" + scores +
            ") -- decoding with the forced format anyway; drop --input-time-format to "
            "auto-detect or pass --strict to reject the mismatch");
    }

    // The forced format is kept either way. In strict mode the error stops
    // iteration before it matters; in lenient mode the operator asked for this
    // format and gets it.
    return input_time_format_;
}

bool MieFileReader::diagnose_no_records(const Optional<TimestampFormat>& format_hint,
                                        PendingError& error) {
    const uint8_t* data = mapping_.data();
    const auto file_len = static_cast<std::size_t>(file_size_);

    // L1-EXIT-010 / L2-RDR-021: a valid but EMPTY recording opens directly on
    // the end-of-records terminator. Zero records, exit 0, header-only CSV --
    // not the wrong-file rejection below. A non-null lead word still falls
    // through, so the guard against "you passed a JPEG" is untouched.
    uint16_t lead = 0;
    if (decode::read_u16(data, file_len, 0, lead) && decode::is_terminator_type_word(lead)) {
        MIE_LOG_WARN(path_ +
                     ": recording contains no records -- the stream opens on the "
                     "end-of-records terminator (empty capture); writing header-only output");
        empty_recording_ = true;
        return true;
    }

    // L2-RDR-004: separate "no MIE record here at all" from "a structurally
    // valid Type Word whose declared extent runs past EOF". The second means
    // the recording was cut short; the first means the wrong file was passed.
    // Either way, outside strict mode the outcome is the no-valid-records class.
    const auto scan_bytes =
        static_cast<uint64_t>(file_len < sync::MAX_SCAN_BYTES ? file_len : sync::MAX_SCAN_BYTES);
    std::size_t trunc_offset = 0;
    std::size_t record_bytes = 0;
    std::size_t available = 0;
    if (sync::diagnose_header_scan_failure(data, file_len, file_len, format_hint,
                                           sync::MAX_SCAN_BYTES, trunc_offset, record_bytes,
                                           available)) {
        if (strict_) {
            MIE_LOG_ERROR("first record after header detection is truncated at " +
                          hex(trunc_offset) + ": declared " + dec(record_bytes) + " bytes, only " +
                          dec(available) + " available");
            error.reset(new MieError(MieError::first_record_truncated(
                static_cast<uint64_t>(trunc_offset), static_cast<uint64_t>(record_bytes),
                static_cast<uint64_t>(available))));
            return false;
        }
        // Lenient: the WARN keeps the specific diagnosis, but the outcome is the
        // no-valid-records class (exit 2). This branch once returned a clean end
        // of stream, so a file that decodes to no rows -- a text file, say,
        // whose bytes happen to form one plausible Type Word -- exited 0 with a
        // header-only CSV, indistinguishable from success.
        MIE_LOG_WARN("first record after header detection is truncated at " + hex(trunc_offset) +
                     ": declared " + dec(record_bytes) + " bytes, only " + dec(available) +
                     " available -- no records decoded");
        error.reset(new MieError(MieError::no_valid_records(path_, scan_bytes)));
        return false;
    }

    MIE_LOG_ERROR("no valid records found in first " + dec(scan_bytes) + " bytes of " + path_);
    error.reset(new MieError(MieError::no_valid_records(path_, scan_bytes)));
    return false;
}

RecordIter MieFileReader::iter() {
    // Reset the per-walk counters, so a second walk over the same handle does
    // not report the first one's totals.
    sync_losses_ = 0;
    empty_recording_ = false;

    RecordIter it(*this);

    // Absent tells the sync helpers to scan format-agnostically; present pins
    // the expected layout.
    Optional<TimestampFormat> format_hint;
    if (input_time_format_ != TIMESTAMP_AUTO) {
        format_hint = input_time_format_;
    }

    const uint8_t* data = mapping_.data();
    const auto file_len = static_cast<std::size_t>(file_size_);

    sync::ScanHit hit;
    const bool found = sync::find_first_record(data, file_len, file_len, format_hint,
                                               sync::MAX_SCAN_BYTES, lookahead_records_, hit);

    it.resolved_format_ = default_resolved_format();
    if (found) {
        if (hit.offset == 0) {
            MIE_LOG_DEBUG("first record at offset 0 (no header)");
        } else {
            MIE_LOG_INFO("file header detected: " + dec(hit.offset) +
                         " bytes before first record at " + hex(hit.offset));
        }
        it.resolved_format_ = resolve_format_for_hit(hit, it.pending_error_);
        it.offset_ = hit.offset;
    } else {
        it.done_ = diagnose_no_records(format_hint, it.pending_error_);
        it.offset_ = file_len;
    }

    MIE_LOG_INFO("beginning decode of " + path_);
    return it;
}

// ---------------------------------------------------------------------------
// RecordIter
// ---------------------------------------------------------------------------

RecordIter::RecordIter(MieFileReader& owner)
    : owner_(&owner),
      data_(owner.mapping_.data()),
      file_len_(static_cast<std::size_t>(owner.file_size_)),
      offset_(0),
      done_(false),
      strict_(owner.strict_),
      resolved_format_(TIMESTAMP_IRIG),
      lookahead_records_(owner.lookahead_records_),
      prev_was_error_(false),
      delta_tracker_(owner.standard_tick_rate_hz_),
      warned_irig_day_(false),
      calendar_year_(owner.calendar_year_),
      last_irig_day_(),
      warned_day_rollover_(false),
      msg_count_(0),
      sync_losses_(0),
      mux_(owner.mux_) {}

RecordIter::RecordIter(RecordIter&& other) noexcept
    : owner_(other.owner_),
      data_(other.data_),
      file_len_(other.file_len_),
      offset_(other.offset_),
      done_(other.done_),
      pending_error_(std::move(other.pending_error_)),
      strict_(other.strict_),
      resolved_format_(other.resolved_format_),
      lookahead_records_(other.lookahead_records_),
      prev_was_error_(other.prev_was_error_),
      delta_tracker_(std::move(other.delta_tracker_)),
      warned_irig_day_(other.warned_irig_day_),
      calendar_year_(other.calendar_year_),
      last_irig_day_(other.last_irig_day_),
      warned_day_rollover_(other.warned_day_rollover_),
      msg_count_(other.msg_count_),
      sync_losses_(other.sync_losses_),
      mux_(std::move(other.mux_)) {
    // The moved-from iterator must not also walk the stream: `iter()` returns
    // by value, and a source left live would keep a second view onto the same
    // reader. The pending error needs no separate reset -- a moved-from
    // shared_ptr is already null.
    other.done_ = true;
}

MIE_NORETURN void RecordIter::fail(const MieError& error) {
    done_ = true;
    throw error;
}

bool RecordIter::next(MieMessage& out) {
    if (done_) {
        return false;
    }

    // A construction-time failure surfaces here, exactly once. Throwing it from
    // `iter()` instead would be tempting, but the reader is a stream: "this
    // file has no valid records" is a fact about the stream, and the caller's
    // loop is where it belongs. Silently yielding nothing was the other option
    // and it is the wrong one -- an empty CSV and exit 0 for a JPEG.
    if (pending_error_) {
        const MieError error = *pending_error_;
        pending_error_.reset();
        fail(error);
    }

    for (;;) {
        const Step step = decode_one(out);
        if (step == STEP_CONTINUE) {
            continue;
        }
        if (step == STEP_YIELD) {
            return true;
        }
        return false;
    }
}

RecordIter::Step RecordIter::decode_one(MieMessage& out) {
    // A Type Word plus the smallest possible payload has to fit.
    if (offset_ + decode::MIN_RECORD_BYTES_STANDARD > file_len_) {
        done_ = true;
        log_complete();
        return STEP_STOP;
    }

    uint16_t type_raw = 0;
    if (!decode::read_u16(data_, file_len_, offset_, type_raw)) {
        done_ = true;
        return STEP_STOP;
    }

    // L2-RDR-021: a null Type Word at a record boundary is the end-of-records
    // terminator. A normal end of stream, not a sync loss -- treating it as one
    // produced a spurious recovery scan at the end of every well-formed file.
    if (decode::is_terminator_type_word(type_raw)) {
        MIE_LOG_DEBUG("end-of-records terminator at " + hex(offset_) + "; decode complete");
        done_ = true;
        log_complete();
        return STEP_STOP;
    }

    const TypeWord tw = decode::decode_type_word(type_raw);
    const TimestampFormat resolved = resolved_format_;
    const uint16_t ts_words = timestamp_word_count(resolved);
    const std::size_t record_bytes = static_cast<std::size_t>(tw.word_count) * 2;

    // Validate through the shared sync path, so a corrupt-but-plausible record
    // cannot slip through here after being rejected everywhere else.
    //
    // DEPTH 1 -- no look-ahead -- and that is deliberate. It is the one call
    // site that differs from `find_first_record` and `recover_sync`, which keep
    // the configured depth (L2-SYN-026). Those answer "is this the start of a
    // record stream?", where a wrong answer costs a resumption point. This one
    // walks a chain already locked on, and a rejection here DISCARDS a record.
    //
    // With look-ahead, a well-formed record sitting immediately before a
    // corrupt region was dropped because its SUCCESSOR failed: one good record
    // lost per corruption site at the default depth, N-1 at depth N. A record
    // that passes on its own is complete and in bounds; whether the next
    // boundary is corrupt is the next iteration's problem, and recovery already
    // handles it (L2-SYN-005).
    const sync::ValidationFailure failure =
        sync::validate_record_detailed(data_, file_len_, offset_, file_len_, resolved, 1);
    if (failure != sync::VALIDATION_OK) {
        return handle_sync_loss(failure, type_raw, tw, record_bytes);
    }

    Timestamp timestamp;
    if (!decode_timestamp_at(resolved, timestamp)) {
        // An out-of-bounds timestamp read stops the walk without latching
        // `done_` or logging completion, matching the Rust reader. Validation
        // above already proved the record fits, so reaching here means the
        // mapping shrank underneath us -- there is nothing honest to report.
        return STEP_STOP;
    }

    const std::size_t cmd_byte_offset = offset_ + 2 + static_cast<std::size_t>(ts_words) * 2;

    // SPURIOUS_DATA has no Command Word at all.
    if (tw.message_type == static_cast<uint8_t>(MESSAGE_TYPE_SPURIOUS_DATA)) {
        spurious_message(tw, timestamp, ts_words, cmd_byte_offset, record_bytes, out);
        return STEP_YIELD;
    }

    uint16_t cmd_raw = 0;
    if (!decode::read_u16(data_, file_len_, cmd_byte_offset, cmd_raw)) {
        done_ = true;
        return STEP_STOP;
    }
    const CommandWord cmd = decode::decode_command_word(cmd_raw);

    // Errored record: Type Word bit 14.
    if (tw.error) {
        const Optional<double> delta = delta_for(cmd, timestamp);
        // A failure inside decode_error_record (a strict-mode UnknownErrorCode,
        // or an Error Word past the end) throws, and `fail` has already latched
        // the walk closed -- terminal, like every other error this iterator
        // raises.
        decode_error_record(tw, timestamp, cmd, cmd_byte_offset, ts_words, delta, out);
        advance_after_yield(record_bytes);
        prev_was_error_ = true;
        return STEP_YIELD;
    }

    return decode_normal_record(tw, cmd, timestamp, ts_words, cmd_byte_offset, record_bytes, out);
}

RecordIter::Step RecordIter::handle_sync_loss(sync::ValidationFailure failure, uint16_t type_raw,
                                              const TypeWord& tw, std::size_t record_bytes) {
    sync_losses_ += 1;
    owner_->sync_losses_ += 1;
    log_validation_context(data_, file_len_, offset_);

    if (strict_) {
        // Latch before throwing: a caller that catches and calls next() again
        // must get a clean end of stream, not a second attempt at the record
        // that just failed.
        done_ = true;
        switch (failure) {
            case sync::VALIDATION_UNKNOWN_MESSAGE_TYPE:
                throw MieError::unknown_type_word(static_cast<uint64_t>(offset_), type_raw,
                                                  tw.message_type);
            case sync::VALIDATION_INVALID_WORD_COUNT:
                throw MieError::invalid_type_word(static_cast<uint64_t>(offset_), type_raw,
                                                  tw.word_count);
            case sync::VALIDATION_RECORD_TRUNCATED:
                throw MieError::record_truncated(
                    static_cast<uint64_t>(offset_), static_cast<uint64_t>(record_bytes),
                    static_cast<uint64_t>(file_len_ > offset_ ? file_len_ - offset_ : 0));
            default:
                // Every other failure is an IRIG field out of range or a
                // look-ahead rejection. There is no dedicated variant for those,
                // and inventing seven would give the CLI seven exit codes for
                // one condition.
                break;
        }
        throw MieError::payload_error(static_cast<uint64_t>(offset_),
                                      std::string(sync::validation_failure_text(failure)) +
                                          " (raw_type=" + hexw(type_raw, 4) + ")");
    }

    MIE_LOG_WARN("sync lost at " + hex(offset_) + " (type=" + hexw(tw.message_type, 2) +
                 " wc=" + dec(tw.word_count) + "); scanning forward");

    sync::ScanHit hit;
    if (sync::recover_sync(data_, file_len_, offset_, file_len_, resolved_format_,
                           sync::MAX_SCAN_BYTES, lookahead_records_, hit)) {
        MIE_LOG_INFO("sync recovered at " + hex(hit.offset) + " (skipped " + dec(hit.skipped) +
                     " bytes from " + hex(offset_) + ")");
        offset_ = hit.offset;
        prev_was_error_ = false;
        return STEP_CONTINUE;
    }

    // Recovery found nothing. Separate running out of FILE from genuine
    // mid-file corruption: if fewer bytes remain than the scan window, the scan
    // was cut short by EOF rather than by failing to find a record, and that is
    // a truncated recording -- a clean stop, not an error.
    const std::size_t bytes_remaining = file_len_ > offset_ ? file_len_ - offset_ : 0;
    if (bytes_remaining < sync::MAX_SCAN_BYTES) {
        MIE_LOG_INFO("lenient mode: scan exhausted at EOF (offset " + hex(offset_) + ", " +
                     dec(bytes_remaining) + " bytes remain < " + dec(sync::MAX_SCAN_BYTES) +
                     " scan window); treating as truncation");
        done_ = true;
        log_complete();
        return STEP_STOP;
    }

    MIE_LOG_ERROR("unrecoverable sync loss at " + hex(offset_) + " after " + dec(msg_count_) +
                  " messages");
    log_complete();
    fail(MieError::unrecoverable_sync_loss(static_cast<uint64_t>(offset_), sync_losses_));
}

bool RecordIter::decode_timestamp_at(TimestampFormat resolved, Timestamp& out) {
    if (resolved == TIMESTAMP_STANDARD) {
        uint16_t upper = 0;
        uint16_t lower = 0;
        if (!decode::read_u16(data_, file_len_, offset_ + 2, upper) ||
            !decode::read_u16(data_, file_len_, offset_ + 4, lower)) {
            return false;
        }
        out = Timestamp::from_standard(decode::decode_standard_timestamp(upper, lower));
        return true;
    }

    // IRIG -- and AUTO, which `iter()` already resolved to a concrete format
    // before the first record. A stray AUTO here therefore decodes as IRIG, the
    // same fallback `default_resolved_format` applies, which keeps the reader
    // total instead of asserting (L1-ROB-001).
    uint16_t upper = 0;
    uint16_t middle = 0;
    uint16_t lower = 0;
    if (!decode::read_u16(data_, file_len_, offset_ + 2, upper) ||
        !decode::read_u16(data_, file_len_, offset_ + 4, middle) ||
        !decode::read_u16(data_, file_len_, offset_ + 6, lower)) {
        return false;
    }
    const IrigTimestamp irig = decode::decode_irig_timestamp(upper, middle, lower);

    // L2-WRT-026 clause 4: under a calendar rendering, a backward day step
    // means the recording crosses New Year while the whole decode uses one
    // configured year, so every row after the wrap is dated a year early. WARN
    // once and carry on -- the decoder deliberately does NOT auto-increment,
    // because sync-loss corruption produces this same signal, and advancing the
    // year on corrupt data would be a worse error than the one it fixed.
    if (calendar_year_.has_value() && !irig.freerun) {
        const auto day = static_cast<int>(irig.day);
        if (last_irig_day_.has_value() && day < last_irig_day_.value() && !warned_day_rollover_) {
            warned_day_rollover_ = true;
            MIE_LOG_WARN(
                "day-of-year stepped back from " +
                text::decimal(static_cast<uint64_t>(last_irig_day_.value())) + " to " +
                text::decimal(static_cast<uint64_t>(day)) + " at " + hex(offset_) +
                ": this recording appears to cross a year boundary, but every row is dated " +
                text::decimal(static_cast<uint64_t>(calendar_year_.value())) +
                ". Rows after the wrap are a year early. Split the recording at the boundary "
                "and decode each part with its own --year");
        }
        last_irig_day_ = day;
    }

    if (irig.freerun) {
        MIE_LOG_WARN("freerun timestamp at " + hex(offset_));
    } else if (!warned_irig_day_ && log::irig_day_advisory()) {
        // PRA-9: one-time day-of-year advisory, on the first calendar-locked
        // record. Once per decode, not once per record -- it is a property of
        // the card's firmware, and repeating it per record would bury every
        // other warning in the file.
        //
        // INFO, not WARN (L2-LOG-001). For the same reason it is once-per-file
        // rather than once-per-record, only stronger: it is a standing
        // disclaimer about card firmware, not an observation about this
        // recording -- nothing here compares the decoded day against anything,
        // so it fires on every calendar-locked IRIG file from every card. At
        // WARN it sat in the default output of every run and crowded out the
        // warnings that ARE observations.
        //
        // L2-LOG-002: the same disclaimer, at a level that follows what the
        // operator asked for. Under `doy` a skewed day is one field a human
        // reads as "day 192" and can re-examine. Under a calendar rendering it
        // is resolved into 2026-07-11 -- something that looks like a fact, gets
        // pasted into reports, and is correlated against systems with no way to
        // know it is suspect. Choosing a calendar rendering IS the operator
        // saying this field is load-bearing, which is when a standing caveat
        // about it earns the default level's attention.
        warned_irig_day_ = true;
        if (calendar_year_.has_value()) {
            MIE_LOG_WARN(
                "IRIG day-of-year is being resolved into calendar dates, and that field has a "
                "known firmware-dependent discrepancy on some DDC cards "
                "(hour/minute/second/microsecond are unaffected). Verify your card model "
                "against vendor CSV before relying on these dates -- see "
                "docs/VENDOR-CSV-DIFFS.md section 5");
        } else {
            MIE_LOG_INFO(
                "IRIG day-of-year decoded for this recording; the day-of-year field has a known "
                "firmware-dependent discrepancy on some DDC cards (hour/minute/second/microsecond "
                "are unaffected) -- see docs/VENDOR-CSV-DIFFS.md section 5");
        }
    }
    out = Timestamp::from_irig(irig);
    return true;
}

void RecordIter::spurious_message(const TypeWord& tw, const Timestamp& timestamp, uint16_t ts_words,
                                  std::size_t cmd_byte_offset, std::size_t record_bytes,
                                  MieMessage& out) {
    const int32_t raw_word_count =
        static_cast<int32_t>(tw.word_count) - 1 - static_cast<int32_t>(ts_words);

    out = MieMessage();
    out.timestamp = timestamp;
    out.type_word = tw;
    out.message_format = FORMAT_SPURIOUS_DATA;
    out.file_offset = static_cast<uint64_t>(offset_);
    out.mux = mux_;

    if (raw_word_count > 0) {
        const auto n = static_cast<std::size_t>(raw_word_count);
        const std::size_t capped = n < MAX_DATA_WORDS ? n : MAX_DATA_WORDS;
        // Bound the read to THIS record. The clamp to file_len_ is not
        // decoration: `size` is what read_u16_array bounds against, so handing
        // it a record end past the mapping would authorise the very read the
        // bounds check exists to prevent.
        std::size_t record_end = offset_ + record_bytes;
        if (record_end > file_len_) {
            record_end = file_len_;
        }
        uint16_t buf[MAX_DATA_WORDS];
        if (decode::read_u16_array(data_, record_end, cmd_byte_offset, capped, buf)) {
            out.data_words = DataWords::from_words(buf, capped);
        }
    }

    // The 0x2000 / 0x2001 distinction, and the reason it can only be made here:
    // the card writes leftover words from an errored transaction as a separate
    // SPURIOUS_DATA record immediately after. Whether this one continues an
    // error is a fact about the PREVIOUS record, which sync and decode never
    // see.
    // The cast is not redundant: the conditional operator promotes two uint16_t
    // operands to int, and handing that to Optional<uint16_t> is a narrowing
    // conversion. GCC is quiet about it here; MSVC compiles at /W4 /WX, where
    // C4244 makes it an error -- green on Linux, broken on the Windows build.
    out.error_word = static_cast<uint16_t>(prev_was_error_ ? ERROR_SPURIOUS_CONTINUATION
                                                           : ERROR_SPURIOUS_STANDALONE);

    MIE_LOG_DEBUG("SPURIOUS_DATA at " + hex(offset_) + ": " +
                  dec(raw_word_count > 0 ? static_cast<uint64_t>(raw_word_count) : 0) +
                  " raw words, " + (prev_was_error_ ? "continuation" : "standalone"));

    advance_after_yield(record_bytes);
    prev_was_error_ = false;
}

RecordIter::Step RecordIter::decode_normal_record(const TypeWord& tw, const CommandWord& cmd,
                                                  const Timestamp& timestamp, uint16_t ts_words,
                                                  std::size_t cmd_byte_offset,
                                                  std::size_t record_bytes, MieMessage& out) {
    MessageFormat msg_fmt = FORMAT_RECEIVE;
    if (!decode::classify_message_format(tw.message_type, cmd, tw.word_count, ts_words, msg_fmt)) {
        MIE_LOG_WARN("cannot classify record at " + hex(offset_) +
                     " (type=" + hexw(tw.message_type, 2) + "); skipping");
        offset_ += record_bytes;
        prev_was_error_ = false;
        return STEP_CONTINUE;
    }

    MIE_LOG_DEBUG("record at " + hex(offset_) + ": type=" + hexw(tw.message_type, 2) + " fmt=" +
                  message_format_name(msg_fmt) + " RT" + dec(cmd.rt) + " SA" + dec(cmd.subaddress));

    // L2-SYN-020..022 pre-extract invariants.
    decode::InvariantViolation violation;
    if (!decode::validate_structural_invariants(tw, cmd, msg_fmt, ts_words, violation)) {
        if (strict_) {
            fail(MieError::payload_error(
                static_cast<uint64_t>(offset_),
                "L2-SYN structural invariant violation: " + violation.detail));
        }
        MIE_LOG_WARN("L2-SYN structural invariant violation at " + hex(offset_) + ": " +
                     violation.detail + "; skipping record");
        offset_ += record_bytes;
        prev_was_error_ = false;
        return STEP_CONTINUE;
    }

    // Bound every payload read to this record's bytes, so an over-claiming
    // Command Word cannot reach into the next record.
    std::size_t record_end = offset_ + record_bytes;
    if (record_end > file_len_) {
        record_end = file_len_;
    }
    const std::size_t payload_offset = cmd_byte_offset + 2;

    Optional<CommandWord> cmd2;
    Optional<uint16_t> status;
    Optional<uint16_t> status2;
    DataWords data_words;
    extract_payload(data_, record_end, payload_offset, msg_fmt, cmd, cmd2, status, status2,
                    data_words);

    // L2-SYN-023 / L2-SYN-027 post-extract invariants. Separate from the
    // pre-extract pass because Cmd2 lives inside the payload and does not exist
    // until it has been read.
    if (!decode::validate_post_extract_invariants(msg_fmt, cmd, cmd2, violation)) {
        if (strict_) {
            fail(MieError::payload_error(
                static_cast<uint64_t>(offset_),
                "L2-SYN structural invariant violation: " + violation.detail));
        }
        MIE_LOG_WARN("L2-SYN structural invariant violation at " + hex(offset_) + ": " +
                     violation.detail + "; skipping record");
        offset_ += record_bytes;
        prev_was_error_ = false;
        return STEP_CONTINUE;
    }

    // L2-SYN-024 / L2-SYN-025 anomalies. WARN-class: the record decodes
    // correctly and is kept. Rejecting on these would produce false negatives
    // on real recordings, where bus interference and undocumented vendor
    // extensions both occur.
    const std::vector<decode::InvariantViolation> anomalies =
        decode::detect_record_anomalies(tw, cmd, status);
    for (std::size_t i = 0; i < anomalies.size(); ++i) {
        MIE_LOG_WARN("L2-SYN anomaly at " + hex(offset_) + ": " + anomalies[i].detail);
    }

    const Optional<double> delta = delta_for(cmd, timestamp);

    out = MieMessage();
    out.timestamp = timestamp;
    out.type_word = tw;
    out.message_format = msg_fmt;
    out.command_word = cmd;
    out.command_word_2 = cmd2;
    out.status_word = status;
    out.status_word_2 = status2;
    out.data_words = data_words;
    out.delta = delta;
    out.file_offset = static_cast<uint64_t>(offset_);
    out.mux = mux_;

    advance_after_yield(record_bytes);
    prev_was_error_ = false;

    if (msg_count_ > 0 && msg_count_ % 100000 == 0) {
        MIE_LOG_INFO("decoded " + dec(msg_count_) + " messages (" + hex(offset_) + " / " +
                     hex(file_len_) + ")");
    }

    return STEP_YIELD;
}

void RecordIter::decode_error_record(const TypeWord& tw, const Timestamp& timestamp,
                                     const CommandWord& cmd, std::size_t cmd_byte_offset,
                                     uint16_t ts_words, const Optional<double>& delta,
                                     MieMessage& out) {
    // The card truncates the payload on a bus error and appends the code as the
    // record's LAST word.
    const std::size_t error_word_offset =
        offset_ + (static_cast<std::size_t>(tw.word_count) - 1) * 2;
    uint16_t error_code = 0;
    if (!decode::read_u16(data_, file_len_, error_word_offset, error_code)) {
        // log_complete before the throw, matching the Rust reader: this is the
        // end of the decode, and the summary line is what tells an operator how
        // far it got. The strict-mode paths in handle_sync_loss and
        // decode_normal_record deliberately do NOT log it -- there the
        // conversation continues in the CLI's own error report.
        log_complete();
        fail(MieError::payload_error(static_cast<uint64_t>(offset_), "error word out of bounds"));
    }

    if (!is_known_ddc_error_code(error_code)) {
        if (strict_) {
            log_complete();
            fail(MieError::unknown_error_code(static_cast<uint64_t>(offset_), error_code));
        }
        MIE_LOG_WARN("unknown DDC error code " + hexw(error_code, 4) + " at " + hex(offset_));
    }

    // Payload words = total - Type(1) - timestamp - Cmd(1) - ErrorWord(1).
    const int32_t payload_words =
        static_cast<int32_t>(tw.word_count) - 1 - static_cast<int32_t>(ts_words) - 1 - 1;

    out = MieMessage();
    out.timestamp = timestamp;
    out.type_word = tw;
    out.command_word = cmd;
    out.error_word = error_code;
    out.delta = delta;
    out.file_offset = static_cast<uint64_t>(offset_);
    out.mux = mux_;

    if (payload_words > 0) {
        const auto n = static_cast<std::size_t>(payload_words);
        const std::size_t capped = n < MAX_DATA_WORDS ? n : MAX_DATA_WORDS;
        std::size_t record_end = offset_ + static_cast<std::size_t>(tw.word_count) * 2;
        if (record_end > file_len_) {
            record_end = file_len_;
        }
        uint16_t buf[MAX_DATA_WORDS];
        if (decode::read_u16_array(data_, record_end, cmd_byte_offset + 2, capped, buf)) {
            out.data_words = DataWords::from_words(buf, capped);
        }
    }

    // Classification can legitimately fail on a truncated error record; RECEIVE
    // is the fallback, matching Rust. The format only selects a payload layout,
    // and the payload was already read above from the Type Word's own count.
    MessageFormat msg_fmt = FORMAT_RECEIVE;
    static_cast<void>(
        decode::classify_message_format(tw.message_type, cmd, tw.word_count, ts_words, msg_fmt));
    out.message_format = msg_fmt;

    MIE_LOG_INFO(
        "error record at " + hex(offset_) + ": RT" + dec(cmd.rt) + " SA" + dec(cmd.subaddress) +
        " " + direction_name(cmd.direction) + ", code=" + hexw(error_code, 4) + " (" +
        ddc_error_description_or_unknown(error_code) + "), " +
        dec(payload_words > 0 ? static_cast<uint64_t>(payload_words) : 0) + " payload words");
}

Optional<double> RecordIter::delta_for(const CommandWord& cmd, const Timestamp& timestamp) {
    const DeltaOutcome outcome = delta_tracker_.observe(&cmd, timestamp);
    // The tracker deliberately does not log: it cannot know that a backward
    // step is worth a line in a single-file decode and merely noise in a merge
    // that already reports unsorted inputs at file granularity (L2-MRG-006).
    // Narration is the reader's job and nobody else's.
    if (outcome.kind == DELTA_BACKWARD && outcome.first_for_key) {
        MIE_LOG_WARN("non-monotonic timestamp at " + hex(offset_) + " for RT/MSG key " +
                     hexw(outcome.key, 8) + ": prev_us=" + dec(outcome.prev_us) +
                     " curr_us=" + dec(outcome.curr_us) +
                     " (further out-of-order occurrences for this key suppressed)");
    }
    return outcome.value();
}

void RecordIter::advance_after_yield(std::size_t record_bytes) {
    offset_ += record_bytes;
    msg_count_ += 1;
}

void RecordIter::log_complete() const {
    MIE_LOG_INFO("decode complete: " + dec(msg_count_) + " messages, " + dec(sync_losses_) +
                 " sync recoveries, format=" + timestamp_format_name(resolved_format_) +
                 ", file=" + owner_->path_);
}

}  // namespace mie
