// SPDX-License-Identifier: Apache-2.0
//
// Bit positions, thresholds and score weights in this file are WIRE FORMAT and
// cross-implementation contract, transcribed from rust/src/decode.rs. They are
// documented in docs/MIE-FORMAT.md. Changing one changes what the decoder
// reads, and the shared conformance oracles will say so.

#include "mie/decode.hpp"

#include <cstdlib>

#include "mie/text.hpp"

namespace mie {
namespace decode {

const char* const DEFAULT_MUX_DELIMITER = ".";

bool is_terminator_type_word(uint16_t raw) { return raw == TERMINATOR_TYPE_WORD; }

// --- Primitive readers ----------------------------------------------------

bool read_u16(const uint8_t* data, std::size_t size, std::size_t offset, uint16_t& out) {
    // The addition is checked against the size BEFORE being performed in a way
    // that could wrap: `offset + 2 > size` would wrap to a small number for an
    // offset near SIZE_MAX and admit an out-of-bounds read. A corrupt word
    // count is exactly how such an offset arises.
    if (offset > size || size - offset < 2) {
        return false;
    }
    // Little-endian, assembled by hand rather than by casting to uint16_t*:
    // the mapped file has no alignment guarantee, and a misaligned load is
    // undefined behaviour that UBSan flags and some architectures fault on.
    out = static_cast<uint16_t>(static_cast<uint16_t>(data[offset]) |
                                (static_cast<uint16_t>(data[offset + 1]) << 8));
    return true;
}

bool read_u16_array(const uint8_t* data, std::size_t size, std::size_t offset, std::size_t count,
                    uint16_t* out) {
    if (count == 0) {
        return true;
    }
    // Overflow-safe: count is bounded by the record's word count, but that
    // comes off the wire and cannot be trusted.
    if (count > size / 2) {
        return false;
    }
    const std::size_t needed = count * 2;
    if (offset > size || size - offset < needed) {
        return false;
    }
    for (std::size_t i = 0; i < count; ++i) {
        out[i] = static_cast<uint16_t>(static_cast<uint16_t>(data[offset + i * 2]) |
                                       (static_cast<uint16_t>(data[offset + i * 2 + 1]) << 8));
    }
    return true;
}

// --- Field decoders -------------------------------------------------------

TypeWord decode_type_word(uint16_t raw) {
    const auto message_type = static_cast<uint8_t>(raw & 0x7F);
    const Bus bus = ((raw >> 7) & 1) == 0 ? BUS_A : BUS_B;
    const auto word_count = static_cast<uint16_t>((raw >> 8) & 0x3F);
    const bool error = ((raw >> 14) & 1) != 0;
    return TypeWord(message_type, bus, word_count, error, raw);
}

IrigTimestamp decode_irig_timestamp(uint16_t upper, uint16_t middle, uint16_t lower) {
    const bool freerun = ((upper >> 15) & 1) != 0;
    const auto day = static_cast<uint16_t>((upper >> 5) & 0x01FF);
    const auto hour = static_cast<uint8_t>(upper & 0x1F);
    const auto minute = static_cast<uint8_t>((middle >> 10) & 0x3F);
    const auto second = static_cast<uint8_t>((middle >> 4) & 0x3F);
    // The microsecond spans two words: the low nibble of the middle word holds
    // its high bits, the whole lower word holds the rest.
    const auto us_hi = static_cast<uint32_t>(middle & 0xF);
    const auto us_lo = static_cast<uint32_t>(lower);
    const uint32_t microsecond = (us_hi << 16) | us_lo;
    return IrigTimestamp(day, hour, minute, second, microsecond, freerun);
}

StandardTimestamp decode_standard_timestamp(uint16_t upper, uint16_t lower) {
    const uint32_t raw_value = (static_cast<uint32_t>(upper) << 16) | static_cast<uint32_t>(lower);
    return StandardTimestamp(raw_value, upper, lower);
}

CommandWord decode_command_word(uint16_t raw) {
    const auto rt = static_cast<uint8_t>((raw >> 11) & 0x1F);
    const Direction direction = ((raw >> 10) & 1) == 0 ? DIRECTION_RECEIVE : DIRECTION_TRANSMIT;
    const auto subaddress = static_cast<uint8_t>((raw >> 5) & 0x1F);
    auto data_word_count = static_cast<uint8_t>(raw & 0x1F);
    // A raw field of 0 means 32 data words, not zero. The five-bit field cannot
    // encode 32, so the bus standard reuses 0 for it -- read literally, every
    // full-length transaction would decode as empty.
    if (data_word_count == 0) {
        data_word_count = 32;
    }
    return CommandWord(rt, direction, subaddress, data_word_count, raw);
}

// --- MUX from the file name -----------------------------------------------

bool mux_from_filename(const std::string& file_name, const std::string& delimiter, int64_t field,
                       std::string& out) {
    out.clear();
    if (delimiter.empty()) {
        return false;
    }

    // Split on the whole delimiter string, not on any of its characters.
    std::vector<std::string> parts;
    std::size_t start = 0;
    for (;;) {
        const std::size_t hit = file_name.find(delimiter, start);
        if (hit == std::string::npos) {
            parts.push_back(file_name.substr(start));
            break;
        }
        parts.push_back(file_name.substr(start, hit - start));
        start = hit + delimiter.size();
    }

    const auto count = static_cast<int64_t>(parts.size());
    // A negative index counts from the end, so -1 selects the last field.
    const int64_t index = field < 0 ? count + field : field;
    if (index < 0 || index >= count) {
        return false;
    }

    const std::string value = text::trim_ascii_blank(parts[static_cast<std::size_t>(index)]);
    if (value.empty()) {
        // An empty field is treated as absent rather than as an empty MUX
        // value, so the column renders empty either way and a recorder that
        // emits `name..part` does not produce a blank-but-present cell.
        return false;
    }
    out = value;
    return true;
}

// --- Message format classification ----------------------------------------

namespace {

/// Mode-code shape classification (L2-MSG-004).
///
/// The thresholds are relative to the record's timestamp word count, NOT
/// absolute: a Standard record is one word shorter than the IRIG equivalent, so
/// absolute thresholds would misclassify every Standard mode code.
MessageFormat classify_mode_code(const CommandWord& cmd, uint16_t word_count,
                                 uint16_t timestamp_words) {
    const bool is_broadcast = cmd.rt == 31;
    if (is_broadcast) {
        // Broadcast mode codes carry no Status Word.
        //   with data: Type + TS + ModeCmd + Data = timestamp_words + 3
        //   no data:   Type + TS + ModeCmd        = timestamp_words + 2
        return word_count >= timestamp_words + 3 ? FORMAT_MODE_CODE_BCAST_DATA
                                                 : FORMAT_MODE_CODE_BCAST_NO_DATA;
    }

    // Non-broadcast mode codes always carry a Status Word; a data word appears
    // only on the longer records (mode codes 0-15 carry none, 16-31 carry one).
    //   with data: Type + TS + ModeCmd + Data + Status = timestamp_words + 4
    //   no data:   Type + TS + ModeCmd        + Status = timestamp_words + 3
    //
    // A mode code without a data word classifies the same either way, because
    // its wire shape is ModeCmd + Status regardless of direction; the CMD
    // column still preserves the direction for the operator.
    if (word_count >= timestamp_words + 4) {
        return cmd.direction == DIRECTION_TRANSMIT ? FORMAT_MODE_CODE_TX_DATA
                                                   : FORMAT_MODE_CODE_RX_DATA;
    }
    return FORMAT_MODE_CODE_NO_DATA;
}

/// Minimum payload words for a format, from the primary Command Word's declared
/// data word count. Drives the L2-SYN-022 capacity check.
///
/// SPURIOUS_DATA returns 0 because its size is variable -- there is no capacity
/// to check against, and inventing one would reject valid continuations.
///
/// For the RT-to-RT formats the second Command Word has not been extracted yet,
/// so Cmd1's count stands in. The bus requires the two to agree, and
/// L2-SYN-027 checks that separately once Cmd2 exists.
uint16_t min_payload_words(MessageFormat fmt, const CommandWord& cmd) {
    const uint16_t dwc = cmd.data_word_count;
    switch (fmt) {
        case FORMAT_RECEIVE:
        case FORMAT_TRANSMIT: return static_cast<uint16_t>(dwc + 1);  // data + status
        case FORMAT_RT_TO_RT:
            return static_cast<uint16_t>(dwc + 3);  // cmd2 + tx status + data + rx status
        case FORMAT_RECEIVE_BROADCAST: return dwc;  // data only, no status
        case FORMAT_RT_TO_RT_BROADCAST:
            return static_cast<uint16_t>(dwc + 2);  // cmd2 + tx status + data
        case FORMAT_MODE_CODE_TX_DATA:
        case FORMAT_MODE_CODE_RX_DATA:
            return 2;  // status + data
        // The next two return the same number for DIFFERENT reasons, and are
        // kept apart deliberately. A non-broadcast mode code with no data word
        // still carries a Status Word; a broadcast mode code WITH a data word
        // carries no status. One payload word either way -- a coincidence of
        // the wire format, not a shared meaning.
        //
        // clang-tidy sees identical branches and suggests merging. Merging
        // would assert that the two formats are one case, and the next change
        // to either would have to split them again with the reason lost. The
        // Rust implementation keeps them separate too, and keeping the two
        // readable against each other is what the conformance work depends on.
        // NOLINTNEXTLINE(bugprone-branch-clone)
        case FORMAT_MODE_CODE_NO_DATA: return 1;     // status only
        case FORMAT_MODE_CODE_BCAST_DATA: return 1;  // data only, no status
        case FORMAT_MODE_CODE_BCAST_NO_DATA:
        case FORMAT_SPURIOUS_DATA:
        default: return 0;
    }
}

}  // namespace

bool classify_message_format(uint8_t message_type, const CommandWord& command_word,
                             uint16_t word_count, uint16_t timestamp_words, MessageFormat& out) {
    switch (message_type) {
        case MESSAGE_TYPE_BC_TO_RT: out = FORMAT_RECEIVE; return true;
        case MESSAGE_TYPE_RT_TO_BC: out = FORMAT_TRANSMIT; return true;
        case MESSAGE_TYPE_RT_TO_RT: out = FORMAT_RT_TO_RT; return true;
        case MESSAGE_TYPE_BROADCAST_BC_TO_RT: out = FORMAT_RECEIVE_BROADCAST; return true;
        case MESSAGE_TYPE_BROADCAST_RT_TO_RT: out = FORMAT_RT_TO_RT_BROADCAST; return true;
        case MESSAGE_TYPE_MODE_COMMAND:
            out = classify_mode_code(command_word, word_count, timestamp_words);
            return true;
        case MESSAGE_TYPE_SPURIOUS_DATA: out = FORMAT_SPURIOUS_DATA; return true;
        default:
            // Declined rather than raised: this function does not know the byte
            // offset, and an error citing offset 0 would be actively
            // misleading. The reader raises UnknownTypeWord with the offset it
            // does know.
            return false;
    }
}

// --- Structural invariants ------------------------------------------------

InvariantViolation::InvariantViolation()
    : kind(INVARIANT_DIRECTION_BC_TO_RT), severity(SEVERITY_REJECT) {}

InvariantViolation::InvariantViolation(WhichInvariant kind_in, InvariantSeverity severity_in,
                                       const std::string& detail_in)
    : kind(kind_in), severity(severity_in), detail(detail_in) {}

bool validate_structural_invariants(const TypeWord& tw, const CommandWord& cmd,
                                    MessageFormat msg_fmt, uint16_t ts_words,
                                    InvariantViolation& out) {
    // L2-SYN-020: BC-to-RT must be a Receive command.
    if (tw.message_type == MESSAGE_TYPE_BC_TO_RT && cmd.direction != DIRECTION_RECEIVE) {
        out = InvariantViolation(INVARIANT_DIRECTION_BC_TO_RT, SEVERITY_REJECT,
                                 "Type 0x02 (BC->RT) requires Cmd direction = Receive; "
                                 "got Transmit (raw Cmd = 0x" +
                                     text::hex_upper(cmd.raw, 4) + ")");
        return false;
    }

    // L2-SYN-021: RT-to-BC must be a Transmit command.
    if (tw.message_type == MESSAGE_TYPE_RT_TO_BC && cmd.direction != DIRECTION_TRANSMIT) {
        out = InvariantViolation(INVARIANT_DIRECTION_RT_TO_BC, SEVERITY_REJECT,
                                 "Type 0x04 (RT->BC) requires Cmd direction = Transmit; "
                                 "got Receive (raw Cmd = 0x" +
                                     text::hex_upper(cmd.raw, 4) + ")");
        return false;
    }

    // L2-SYN-022: the declared word count must be able to hold the payload the
    // Command Word promises. Overhead is Type(1) + timestamp + Cmd(1).
    const auto min_wc = static_cast<uint16_t>(1 + ts_words + 1 + min_payload_words(msg_fmt, cmd));
    if (tw.word_count < min_wc) {
        out = InvariantViolation(
            INVARIANT_WORD_COUNT_CAPACITY, SEVERITY_REJECT,
            "TW.word_count = " + text::decimal(tw.word_count) +
                " is too small for declared payload (need at least " + text::decimal(min_wc) +
                " for " + message_format_name(msg_fmt) +
                " with data_word_count = " + text::decimal(cmd.data_word_count) + ")");
        return false;
    }

    return true;
}

bool validate_post_extract_invariants(MessageFormat msg_fmt, const CommandWord& cmd,
                                      const Optional<CommandWord>& cmd2, InvariantViolation& out) {
    const bool is_rt_to_rt = msg_fmt == FORMAT_RT_TO_RT || msg_fmt == FORMAT_RT_TO_RT_BROADCAST;
    if (!is_rt_to_rt || !cmd2.has_value()) {
        return true;
    }
    const CommandWord& c2 = cmd2.value();

    // L2-SYN-023.
    if (c2.direction != DIRECTION_RECEIVE) {
        out = InvariantViolation(INVARIANT_DIRECTION_RT_TO_RT_CMD2, SEVERITY_REJECT,
                                 "RT-to-RT Cmd2 requires direction = Receive; got Transmit "
                                 "(raw Cmd2 = 0x" +
                                     text::hex_upper(c2.raw, 4) + ")");
        return false;
    }

    // L2-SYN-027. Worth more than it looks: the capacity check above only ever
    // sees Cmd1, so a Cmd2 that over-claims its data word count would otherwise
    // pass unnoticed.
    if (cmd.data_word_count != c2.data_word_count) {
        out = InvariantViolation(INVARIANT_DATA_WORD_COUNT_MISMATCH, SEVERITY_REJECT,
                                 "RT-to-RT Cmd1/Cmd2 data_word_count mismatch: Cmd1 = " +
                                     text::decimal(cmd.data_word_count) +
                                     ", Cmd2 = " + text::decimal(c2.data_word_count) +
                                     " (raw Cmd1 = 0x" + text::hex_upper(cmd.raw, 4) +
                                     ", Cmd2 = 0x" + text::hex_upper(c2.raw, 4) + ")");
        return false;
    }

    return true;
}

std::vector<InvariantViolation> detect_record_anomalies(const TypeWord& tw, const CommandWord& cmd,
                                                        const Optional<uint16_t>& status_word) {
    std::vector<InvariantViolation> out;

    // L2-SYN-024: the Status Word's RT field should echo the Command Word's.
    // AnomalyWarn, not Reject: bus interference produces this on real
    // recordings, and rejecting would drop valid records.
    if (status_word.has_value()) {
        const uint16_t status_raw = status_word.value();
        const auto status_rt = static_cast<uint8_t>((status_raw >> 11) & 0x1F);
        if (status_rt != cmd.rt) {
            out.emplace_back(INVARIANT_STATUS_RT_MISMATCH, SEVERITY_ANOMALY_WARN,
                             "Status RT = " + text::decimal(status_rt) +
                                 " does not match Cmd RT = " + text::decimal(cmd.rt) +
                                 " (raw Status = 0x" + text::hex_upper(status_raw, 4) +
                                 "); possible bus interference");
        }
    }

    // L2-SYN-025: bit 15 is reserved. AnomalyWarn because a set bit may be an
    // undocumented vendor extension rather than corruption.
    if (((tw.raw >> 15) & 1) != 0) {
        out.emplace_back(INVARIANT_TYPE_WORD_RESERVED_BIT, SEVERITY_ANOMALY_WARN,
                         "Type Word bit 15 (reserved) is set in raw 0x" +
                             text::hex_upper(tw.raw, 4) +
                             "; possible undocumented vendor extension");
    }

    return out;
}

// --- Timestamp-format auto-detection --------------------------------------

DetectionOutcome::DetectionOutcome()
    : format(TIMESTAMP_IRIG),
      irig_score(0),
      std_score(0),
      records_probed(0),
      confidence(CONFIDENCE_AMBIGUOUS) {}

namespace {

/// T/R consistency: BC-to-RT expects Receive, RT-to-BC expects Transmit. Other
/// message types never match, so they contribute nothing to either score.
bool tr_direction_matches(const TypeWord& tw, const CommandWord& cmd) {
    return (tw.message_type == MESSAGE_TYPE_BC_TO_RT && cmd.direction == DIRECTION_RECEIVE) ||
           (tw.message_type == MESSAGE_TYPE_RT_TO_BC && cmd.direction == DIRECTION_TRANSMIT);
}

/// IRIG candidate: the Command Word would sit at offset+8 (Type + 3 timestamp
/// words). Up to +5 -- T/R 2, word-count plausibility 2, field-range validity 1.
int32_t score_irig_candidate(const uint8_t* data, std::size_t size, std::size_t offset,
                             const TypeWord& tw) {
    uint16_t cmd_raw = 0;
    if (!read_u16(data, size, offset + 8, cmd_raw)) {
        return 0;
    }
    const CommandWord cmd = decode_command_word(cmd_raw);
    int32_t score = 0;
    if (tr_direction_matches(tw, cmd)) {
        score += 2;
    }
    // IRIG overhead = Type(1) + TS(3) + Cmd(1) + Status(1) = 6.
    if (static_cast<int32_t>(tw.word_count) - 6 == static_cast<int32_t>(cmd.data_word_count)) {
        score += 2;
    }
    uint16_t ts_upper = 0;
    uint16_t ts_middle = 0;
    if (read_u16(data, size, offset + 2, ts_upper) && read_u16(data, size, offset + 4, ts_middle)) {
        // Only fields with a real semantic range are scored. The microsecond
        // high nibble is always below 16 by construction, so testing it would
        // award a point to every candidate and separate nothing.
        const auto hour = static_cast<uint16_t>(ts_upper & 0x1F);
        const auto minute = static_cast<uint16_t>((ts_middle >> 10) & 0x3F);
        const auto second = static_cast<uint16_t>((ts_middle >> 4) & 0x3F);
        if (hour < 24 && minute < 60 && second < 60) {
            score += 1;
        }
    }
    return score;
}

/// Standard candidate: the Command Word would sit at offset+6 (Type + 2
/// timestamp words). Up to +4 -- T/R 2, word-count plausibility 2. No
/// range bonus: a raw 32-bit counter has no field bounds to check.
int32_t score_standard_candidate(const uint8_t* data, std::size_t size, std::size_t offset,
                                 const TypeWord& tw) {
    uint16_t cmd_raw = 0;
    if (!read_u16(data, size, offset + 6, cmd_raw)) {
        return 0;
    }
    const CommandWord cmd = decode_command_word(cmd_raw);
    int32_t score = 0;
    if (tr_direction_matches(tw, cmd)) {
        score += 2;
    }
    // Standard overhead = Type(1) + TS(2) + Cmd(1) + Status(1) = 5.
    if (static_cast<int32_t>(tw.word_count) - 5 == static_cast<int32_t>(cmd.data_word_count)) {
        score += 2;
    }
    return score;
}

DetectionConfidence classify_confidence(int32_t max_score, int32_t margin) {
    if (max_score < CONFIDENCE_FLOOR || margin < MIN_MARGIN) {
        return CONFIDENCE_AMBIGUOUS;
    }
    if (max_score >= DECISIVE_FLOOR && margin >= DECISIVE_MARGIN) {
        return CONFIDENCE_DECISIVE;
    }
    return CONFIDENCE_MARGINAL;
}

}  // namespace

DetectionOutcome probe_timestamp_format(const uint8_t* data, std::size_t size,
                                        std::size_t first_offset, std::size_t max_records) {
    const std::size_t limit = max_records < 1 ? 1 : max_records;
    int32_t irig_score = 0;
    int32_t std_score = 0;
    std::size_t records_probed = 0;
    std::size_t offset = first_offset;

    for (std::size_t i = 0; i < limit; ++i) {
        // The Standard floor is used deliberately: it is the smaller of the
        // two, so scoring is not refused for a record that would be valid under
        // the format the probe might be about to choose.
        if (offset > size || size - offset < MIN_RECORD_BYTES_STANDARD) {
            break;
        }
        uint16_t tw_raw = 0;
        if (!read_u16(data, size, offset, tw_raw)) {
            break;
        }
        const TypeWord tw = decode_type_word(tw_raw);
        // Structurally impossible records are skipped rather than scored: the
        // reader would reject them too, and scoring them would skew the result
        // with data neither format can explain.
        if (tw.word_count < MIN_RECORD_WORDS_STANDARD) {
            break;
        }

        irig_score += score_irig_candidate(data, size, offset, tw);
        std_score += score_standard_candidate(data, size, offset, tw);
        ++records_probed;

        const std::size_t record_bytes = static_cast<std::size_t>(tw.word_count) * 2;
        if (record_bytes == 0) {
            break;
        }
        const std::size_t next_offset = offset + record_bytes;
        // Guards both a wrap and a non-advancing step; either would spin here
        // forever on corrupt input.
        if (next_offset <= offset || next_offset > size) {
            break;
        }
        offset = next_offset;
    }

    DetectionOutcome outcome;
    // IRIG wins ties (L2-DEC-012).
    outcome.format = irig_score >= std_score ? TIMESTAMP_IRIG : TIMESTAMP_STANDARD;
    outcome.irig_score = irig_score;
    outcome.std_score = std_score;
    outcome.records_probed = records_probed;

    const int32_t max_score = irig_score > std_score ? irig_score : std_score;
    const int32_t margin = irig_score > std_score ? irig_score - std_score : std_score - irig_score;
    outcome.confidence = classify_confidence(max_score, margin);
    return outcome;
}

}  // namespace decode
}  // namespace mie
