// SPDX-License-Identifier: Apache-2.0
//
// Transcribed from rust/src/sync.rs. Every threshold and ordering here is
// shared behaviour, and the shared conformance oracles will say so if one
// moves.
//
// No logging and no I/O in this file. See the header for why that is a rule
// rather than an accident.

#include "mie/sync.hpp"

#include <cstring>

#include "mie/decode.hpp"

namespace mie {
namespace sync {

ScanHit::ScanHit() : offset(0), skipped(0) {}

ScanHit::ScanHit(std::size_t offset_in, std::size_t skipped_in)
    : offset(offset_in), skipped(skipped_in) {}

const char* validation_failure_text(ValidationFailure failure) {
    switch (failure) {
        case VALIDATION_OK: return "valid";
        case VALIDATION_TYPE_WORD_UNREADABLE: return "Type Word is not readable";
        case VALIDATION_UNKNOWN_MESSAGE_TYPE: return "message type is unknown";
        case VALIDATION_INVALID_WORD_COUNT: return "word count is outside the valid range";
        case VALIDATION_RECORD_TRUNCATED: return "record extends beyond end of file";
        case VALIDATION_IRIG_HOUR_OUT_OF_RANGE: return "IRIG hour is out of range";
        case VALIDATION_IRIG_MINUTE_OUT_OF_RANGE: return "IRIG minute is out of range";
        case VALIDATION_IRIG_SECOND_OUT_OF_RANGE: return "IRIG second is out of range";
        case VALIDATION_IRIG_MICROSECOND_OUT_OF_RANGE: return "IRIG microsecond is out of range";
        case VALIDATION_IRIG_DAY_OUT_OF_RANGE: return "IRIG day-of-year is out of range";
        case VALIDATION_LOOKAHEAD_UNKNOWN_MESSAGE_TYPE: return "look-ahead message type is unknown";
        case VALIDATION_LOOKAHEAD_INVALID_WORD_COUNT:
            return "look-ahead word count is outside the valid range";
        default: return "unknown validation failure";
    }
}

namespace {

/// Minimum word count for a record under `ts_format`.
///
/// An unknown format falls back to the SMALLER (Standard) minimum, which is
/// deliberately permissive: the format has not been probed yet at that point,
/// and using the IRIG floor would reject every record in a Standard-format file
/// before the probe ever got to run.
uint16_t min_word_count(const Optional<TimestampFormat>& ts_format) {
    if (ts_format.has_value()) {
        const TimestampFormat fmt = ts_format.value();
        if (fmt == TIMESTAMP_IRIG || fmt == TIMESTAMP_STANDARD) {
            return static_cast<uint16_t>(1 + timestamp_word_count(fmt) + 1);
        }
    }
    return decode::MIN_RECORD_WORDS_STANDARD;
}

/// Check 5: IRIG field ranges (L2-SYN-004 / L2-SYN-019).
///
/// A no-op when all three timestamp words are not readable -- near EOF the
/// caller's in-bounds checks remain authoritative, and failing here would
/// reject a record for a reason that is really "the file ended".
ValidationFailure validate_irig_fields(const uint8_t* data, std::size_t size, std::size_t offset,
                                       std::size_t file_len) {
    if (offset > file_len || file_len - offset < 8) {
        return VALIDATION_OK;
    }
    uint16_t ts_upper = 0;
    uint16_t ts_middle = 0;
    uint16_t ts_lower = 0;
    if (!decode::read_u16(data, size, offset + 2, ts_upper) ||
        !decode::read_u16(data, size, offset + 4, ts_middle) ||
        !decode::read_u16(data, size, offset + 6, ts_lower)) {
        return VALIDATION_OK;
    }

    const bool freerun = ((ts_upper >> 15) & 1) == 1;
    const auto day = static_cast<uint16_t>((ts_upper >> 5) & 0x1FF);
    const auto hour = static_cast<uint16_t>(ts_upper & 0x1F);
    const auto minute = static_cast<uint16_t>((ts_middle >> 10) & 0x3F);
    const auto second = static_cast<uint16_t>((ts_middle >> 4) & 0x3F);
    const uint32_t microsecond =
        (static_cast<uint32_t>(ts_middle & 0xF) << 16) | static_cast<uint32_t>(ts_lower);

    if (hour >= 24) {
        return VALIDATION_IRIG_HOUR_OUT_OF_RANGE;
    }
    if (minute >= 60) {
        return VALIDATION_IRIG_MINUTE_OUT_OF_RANGE;
    }
    if (second >= 60) {
        return VALIDATION_IRIG_SECOND_OUT_OF_RANGE;
    }
    if (microsecond > 999999) {
        return VALIDATION_IRIG_MICROSECOND_OUT_OF_RANGE;
    }
    // L2-SYN-019: the day range is NOT checked when freerun is set. A
    // free-running oscillator is not calendar-locked, so its day field may
    // carry any value. Hour, minute, second and microsecond still apply --
    // those are a function of the counter modulus and stay in range regardless.
    if (!freerun && (day < 1 || day > 366)) {
        return VALIDATION_IRIG_DAY_OUT_OF_RANGE;
    }
    return VALIDATION_OK;
}

/// Check 6: the N-record look-ahead (L2-SYN-005 / L2-SYN-026 / L2-SYN-028).
///
/// Walks up to `lookahead_records - 1` followers, checking each on the same
/// Type Word fields as the candidate, advancing by each one's DECLARED length
/// so the next iteration lands on the record after it rather than two bytes on.
///
/// EOF ends the walk gracefully: the records that would have been checked do
/// not exist, which is not a reason to reject the candidate.
ValidationFailure validate_lookahead_records(const uint8_t* data, std::size_t size,
                                             std::size_t offset, std::size_t record_bytes,
                                             std::size_t file_len, uint16_t min_wc,
                                             std::size_t lookahead_records, bool honor_terminator) {
    const std::size_t n = lookahead_records < 1 ? 1 : lookahead_records;
    if (offset > (static_cast<std::size_t>(-1) - record_bytes)) {
        return VALIDATION_RECORD_TRUNCATED;
    }
    std::size_t next_offset = offset + record_bytes;

    for (std::size_t i = 1; i < n; ++i) {
        if (next_offset > file_len || file_len - next_offset < 2) {
            break;
        }
        uint16_t next_raw = 0;
        if (!decode::read_u16(data, size, next_offset, next_raw)) {
            break;
        }
        // L2-SYN-028: a null Type Word is the end-of-records terminator, not an
        // invalid follower -- treat it like EOF so the LAST real record of a
        // recording is confirmed rather than dropped.
        //
        // On the trusted-boundary path anywhere. In recovery (false) only as the
        // LAST word of the file: mid-file, a mis-aligned candidate could land
        // its declared boundary on a stray zero DATA word and validate as a
        // bogus "last record", but a zero in the final two bytes ends the
        // candidate exactly where EOF would, and EOF already confirms (above).
        // Without that, a recovered final record was dropped from every file
        // that carried the terminator.
        if (decode::is_terminator_type_word(next_raw) &&
            (honor_terminator || file_len - next_offset == 2)) {
            break;
        }
        const TypeWord next_tw = decode::decode_type_word(next_raw);
        if (!is_valid_message_type(next_tw.message_type)) {
            return VALIDATION_LOOKAHEAD_UNKNOWN_MESSAGE_TYPE;
        }
        if (next_tw.word_count < min_wc || next_tw.word_count > 63) {
            return VALIDATION_LOOKAHEAD_INVALID_WORD_COUNT;
        }
        const std::size_t next_record_bytes = static_cast<std::size_t>(next_tw.word_count) * 2;
        if (next_record_bytes == 0) {
            // Unreachable -- a zero word count is already below the min_wc
            // floor above. Guarded anyway, because the alternative to breaking
            // is looping forever.
            break;
        }
        if (next_offset > (static_cast<std::size_t>(-1) - next_record_bytes)) {
            break;
        }
        next_offset += next_record_bytes;
    }

    return VALIDATION_OK;
}

/// The core validator. `honor_terminator` selects the trusted-boundary
/// behaviour described on `validate_record_detailed`.
ValidationFailure validate_record_impl(const uint8_t* data, std::size_t size, std::size_t offset,
                                       std::size_t file_len,
                                       const Optional<TimestampFormat>& ts_format,
                                       std::size_t lookahead_records, bool honor_terminator) {
    // Check 1: the Type Word is readable, and within the declared file length.
    // Both are needed: `size` is the mapping and `file_len` is what the caller
    // considers the file to be, and a probe near the end can satisfy one
    // without the other.
    uint16_t type_raw = 0;
    if (!decode::read_u16(data, size, offset, type_raw)) {
        return VALIDATION_TYPE_WORD_UNREADABLE;
    }
    if (offset > file_len || file_len - offset < 2) {
        return VALIDATION_TYPE_WORD_UNREADABLE;
    }
    const TypeWord tw = decode::decode_type_word(type_raw);

    // Check 2: a known message type.
    if (!is_valid_message_type(tw.message_type)) {
        return VALIDATION_UNKNOWN_MESSAGE_TYPE;
    }

    // Check 3: a plausible word count.
    const uint16_t min_wc = min_word_count(ts_format);
    if (tw.word_count < min_wc || tw.word_count > 63) {
        return VALIDATION_INVALID_WORD_COUNT;
    }

    // Check 4: the record fits in the file.
    const std::size_t record_bytes = static_cast<std::size_t>(tw.word_count) * 2;
    if (offset > file_len || file_len - offset < record_bytes) {
        return VALIDATION_RECORD_TRUNCATED;
    }

    // Check 5: IRIG field ranges, when the format is known to be IRIG.
    if (ts_format.has_value() && ts_format.value() == TIMESTAMP_IRIG) {
        const ValidationFailure irig = validate_irig_fields(data, size, offset, file_len);
        if (irig != VALIDATION_OK) {
            return irig;
        }
    }

    // Check 6: the look-ahead.
    return validate_lookahead_records(data, size, offset, record_bytes, file_len, min_wc,
                                      lookahead_records, honor_terminator);
}

}  // namespace

bool validate_record(const uint8_t* data, std::size_t size, std::size_t offset,
                     std::size_t file_len, const Optional<TimestampFormat>& ts_format,
                     std::size_t lookahead_records) {
    return validate_record_detailed(data, size, offset, file_len, ts_format, lookahead_records) ==
           VALIDATION_OK;
}

ValidationFailure validate_record_detailed(const uint8_t* data, std::size_t size,
                                           std::size_t offset, std::size_t file_len,
                                           const Optional<TimestampFormat>& ts_format,
                                           std::size_t lookahead_records) {
    return validate_record_impl(data, size, offset, file_len, ts_format, lookahead_records, true);
}

bool find_first_record(const uint8_t* data, std::size_t size, std::size_t file_len,
                       const Optional<TimestampFormat>& ts_format, std::size_t max_scan,
                       std::size_t lookahead_records, ScanHit& out) {
    const std::size_t scan_end = file_len < max_scan ? file_len : max_scan;
    for (std::size_t offset = 0; offset < scan_end; offset += 2) {
        if (validate_record(data, size, offset, file_len, ts_format, lookahead_records)) {
            out = ScanHit(offset, offset);
            return true;
        }
    }
    return false;
}

bool is_homogeneous_payload(const uint8_t* data, std::size_t size, std::size_t offset,
                            std::size_t record_bytes) {
    if (record_bytes == 0) {
        return false;
    }
    // Overflow-safe: record_bytes comes from a wire word count.
    if (record_bytes > (static_cast<std::size_t>(-1) / HOMOGENEITY_SAMPLE_RECORDS)) {
        return false;
    }
    const std::size_t total = HOMOGENEITY_SAMPLE_RECORDS * record_bytes;
    if (offset > size || size - offset < total) {
        return false;
    }

    const uint8_t* first = data + offset;
    for (std::size_t i = 1; i < HOMOGENEITY_SAMPLE_RECORDS; ++i) {
        const uint8_t* other = data + offset + i * record_bytes;
        // Compare the Type Word and everything from byte 8 onward, skipping
        // bytes 2..8 -- the IRIG timestamp triple, which legitimately differs
        // between records and would defeat the comparison.
        //
        // For a Standard-format record the timestamp is only four bytes, so
        // this skips two extra bytes of Command Word. That is conservative: it
        // can only make the check less likely to reject, never more.
        if (std::memcmp(first, other, 2) != 0) {
            return false;
        }
        if (record_bytes > 8 && std::memcmp(first + 8, other + 8, record_bytes - 8) != 0) {
            return false;
        }
    }
    return true;
}

bool diagnose_header_scan_failure(const uint8_t* data, std::size_t size, std::size_t file_len,
                                  const Optional<TimestampFormat>& ts_format, std::size_t max_scan,
                                  std::size_t& out_offset, std::size_t& out_record_bytes,
                                  std::size_t& out_available) {
    const std::size_t scan_end = file_len < max_scan ? file_len : max_scan;
    // IRIG is assumed when the format is unknown, which is the stricter floor
    // here -- this function is looking for a Type Word whose ONLY problem is
    // length, so a stricter word-count floor narrows the search rather than
    // widening it.
    const TimestampFormat resolved = ts_format.has_value() ? ts_format.value() : TIMESTAMP_IRIG;
    const auto min_wc = static_cast<uint16_t>(1 + timestamp_word_count(resolved) + 1);

    for (std::size_t offset = 0; offset + 2 <= scan_end; offset += 2) {
        uint16_t type_raw = 0;
        if (!decode::read_u16(data, size, offset, type_raw)) {
            break;
        }
        const TypeWord tw = decode::decode_type_word(type_raw);
        if (!is_valid_message_type(tw.message_type)) {
            continue;
        }
        if (tw.word_count < min_wc || tw.word_count > 63) {
            continue;
        }
        const std::size_t record_bytes = static_cast<std::size_t>(tw.word_count) * 2;
        if (offset > file_len || file_len - offset < record_bytes) {
            out_offset = offset;
            out_record_bytes = record_bytes;
            out_available = file_len - offset;
            return true;
        }
        // This Type Word looks valid AND fits, so find_first_record would
        // already have returned it unless the IRIG range check or the
        // look-ahead rejected it. Either way the failure is not length-driven;
        // keep looking for one that is.
    }
    return false;
}

bool recover_sync(const uint8_t* data, std::size_t size, std::size_t offset, std::size_t file_len,
                  const Optional<TimestampFormat>& ts_format, std::size_t max_scan,
                  std::size_t lookahead_records, ScanHit& out) {
    const auto max_size = static_cast<std::size_t>(-1);
    const std::size_t scan_start = offset > max_size - 2 ? max_size : offset + 2;
    const std::size_t window_end = offset > max_size - max_scan ? max_size : offset + max_scan;
    const std::size_t scan_end = file_len < window_end ? file_len : window_end;

    for (std::size_t candidate = scan_start; candidate < scan_end; candidate += 2) {
        // honor_terminator = false. A mis-aligned candidate whose declared
        // length happens to land its boundary on a zero data word must NOT
        // validate as a bogus "last record before the terminator". Recovery
        // demands a real follower, EOF, or the terminator as the file's last
        // word -- which ends the candidate where EOF would.
        if (validate_record_impl(data, size, candidate, file_len, ts_format, lookahead_records,
                                 false) == VALIDATION_OK) {
            out = ScanHit(candidate, candidate - offset);
            return true;
        }
    }
    return false;
}

}  // namespace sync
}  // namespace mie
