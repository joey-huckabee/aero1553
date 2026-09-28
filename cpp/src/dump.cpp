// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::dump"

#include "mie/dump.hpp"

#include <string>
#include <vector>

#include "mie/decode.hpp"
#include "mie/error.hpp"
#include "mie/log.hpp"
#include "mie/models.hpp"
#include "mie/platform.hpp"
#include "mie/text.hpp"

namespace mie {
namespace dump {

namespace {

const std::size_t kBytesPerLine = 16;
/// Width of the hex column: 16 bytes as "XX " minus the trailing space.
const std::size_t kHexColumnWidth = 48;
const std::size_t kRuleWidth = 72;

/// Human label for a message type.
///
/// Deliberately NOT `models::message_type_name`, which returns the enum-style
/// `BC_TO_RT` used in the CSV and in error text. This view is read by a person
/// staring at a broken file, and the arrow form says which way the traffic went
/// without them having to remember the convention.
std::string type_label(uint8_t code) {
    switch (code) {
        case MESSAGE_TYPE_MODE_COMMAND: return "Mode Command";
        case MESSAGE_TYPE_BC_TO_RT: return "BC->RT (Receive)";
        case MESSAGE_TYPE_RT_TO_BC: return "RT->BC (Transmit)";
        case MESSAGE_TYPE_RT_TO_RT: return "RT->RT";
        case MESSAGE_TYPE_BROADCAST_BC_TO_RT: return "Broadcast BC->RT";
        case MESSAGE_TYPE_BROADCAST_RT_TO_RT: return "Broadcast RT->RT";
        case MESSAGE_TYPE_SPURIOUS_DATA: return "Spurious Data";
        default:
            // Named, not hidden. An unknown type code is the most interesting
            // thing on the line when a scan has gone wrong.
            return "UNKNOWN(0x" + text::hex_upper(code, 2) + ")";
    }
}

/// Canonical uppercase name of a classified message format.
///
/// Deliberately NOT `models::message_format_name`, which returns the CamelCase
/// spelling used elsewhere. The dump report is diffed against the Rust and
/// Python dumps, both of which print the SCREAMING_SNAKE form here -- so this
/// is contract, not preference, and the two spellings must not be conflated.
const char* format_label(MessageFormat format) {
    switch (format) {
        case FORMAT_RECEIVE: return "RECEIVE";
        case FORMAT_TRANSMIT: return "TRANSMIT";
        case FORMAT_RT_TO_RT: return "RT_TO_RT";
        case FORMAT_RECEIVE_BROADCAST: return "RECEIVE_BROADCAST";
        case FORMAT_RT_TO_RT_BROADCAST: return "RT_TO_RT_BROADCAST";
        case FORMAT_MODE_CODE_TX_DATA: return "MODE_CODE_TX_DATA";
        case FORMAT_MODE_CODE_RX_DATA: return "MODE_CODE_RX_DATA";
        case FORMAT_MODE_CODE_NO_DATA: return "MODE_CODE_NO_DATA";
        case FORMAT_MODE_CODE_BCAST_NO_DATA: return "MODE_CODE_BCAST_NO_DATA";
        case FORMAT_MODE_CODE_BCAST_DATA: return "MODE_CODE_BCAST_DATA";
        case FORMAT_SPURIOUS_DATA: return "SPURIOUS_DATA";
    }
    return "";
}

std::string rule() { return std::string(kRuleWidth, '-'); }

/// The last path component, or the whole string when there is no separator.
std::string file_name_of(const std::string& path) {
    const std::size_t slash = path.find_last_of("/\\");
    return slash == std::string::npos ? path : path.substr(slash + 1);
}

/// Read the whole file.
///
/// Read rather than mapped: this view must work on files the reader rejects,
/// and the ranges asked for are small and arbitrary rather than a walk.
std::vector<uint8_t> read_whole_file(const std::string& path) {
    // Through the platform layer: a recording may sit at a non-ASCII path, and
    // std::fopen would fail to find it on Windows.
    std::vector<uint8_t> data;
    platform::OsError err;
    if (!platform::read_file(path, data, err)) {
        // "not found" and "could not be read" are different problems with
        // different remedies, and a directory stats as zero bytes on Windows --
        // so the existence test decides which this was rather than the size.
        if (!platform::path_exists(path)) {
            throw MieError::file_not_found(path);
        }
        throw MieError::file_io(path, err.message, err.code);
    }
    if (data.empty()) {
        throw MieError::file_empty(path);
    }
    return data;
}

/// Write `text`, raising a writer error on failure.
///
/// Every write is checked. `aero1553 dump x.mie | head` closes the pipe
/// mid-report, and a dump that ignored the failure would exit 0 having produced
/// a truncated view -- indistinguishable, to a script, from a short file.
void emit(std::FILE* out, const std::string& text) {
    if (std::fwrite(text.data(), 1, text.size(), out) != text.size()) {
        platform::OsError err;
        platform::capture_stream_error(err);
        throw MieError::writer_error("stdout", err.message, err.code);
    }
}

void emit_line(std::FILE* out, const std::string& text) { emit(out, text + "\n"); }

/// One hex+ASCII line: address, up to 16 bytes in hex, then the printable
/// rendering between pipes.
void write_hex_line(std::FILE* out, const char* indent, std::size_t address, const uint8_t* bytes,
                    std::size_t count) {
    std::string line = indent;
    line += text::hex_upper(address, 8);
    line += "  ";

    std::string hex;
    for (std::size_t i = 0; i < count; ++i) {
        if (i > 0) {
            hex += ' ';
        }
        hex += text::hex_upper(bytes[i], 2);
    }
    line += hex;
    // Padded so the ASCII column lines up even on a short final line.
    if (hex.size() < kHexColumnWidth) {
        line.append(kHexColumnWidth - hex.size(), ' ');
    }
    line += "  |";
    for (std::size_t i = 0; i < count; ++i) {
        // Explicit ASCII range, never <cctype>: this tree is locale-free by
        // rule (scripts/assert-locale-free.sh), and `isprint` under a non-C
        // locale would render high bytes differently on different hosts.
        const uint8_t byte = bytes[i];
        line += (byte >= 32 && byte < 127) ? static_cast<char>(byte) : '.';
    }
    line += "|";
    emit_line(out, line);
}

void write_hex_block(std::FILE* out, const char* indent, const uint8_t* data, std::size_t size,
                     std::size_t base_address) {
    for (std::size_t i = 0; i < size; i += kBytesPerLine) {
        const std::size_t count = (size - i < kBytesPerLine) ? (size - i) : kBytesPerLine;
        write_hex_line(out, indent, base_address + i, data + i, count);
    }
}

/// Validate the record at `offset`. False stops the scan, having written the
/// anomaly inline and logged it (L2-CLI-013).
bool record_extent(const TypeWord& type_word, std::size_t offset, std::size_t file_size,
                   std::FILE* out, std::size_t& record_end) {
    if (type_word.word_count < decode::MIN_RECORD_WORDS) {
        const std::string where = text::hex_upper(offset, 8);
        emit_line(out, "  !! Invalid word_count=" + text::decimal(type_word.word_count) + " at 0x" +
                           where + ", stopping");
        MIE_LOG_WARN("dump: invalid word_count=" + text::decimal(type_word.word_count) + " at 0x" +
                     text::hex_upper(offset, 1) + "; stopping record scan");
        return false;
    }

    const std::size_t record_bytes = static_cast<std::size_t>(type_word.word_count) * 2;
    // Belt and braces. `word_count` is six bits, so `record_bytes` cannot exceed
    // 126 and this cannot fire for an offset inside a real file -- but the
    // alternative to checking is silently wrapping to a small offset and
    // dumping the wrong bytes as though they were right.
    if (offset > static_cast<std::size_t>(-1) - record_bytes) {
        emit_line(out, "  !! Offset overflow at 0x" + text::hex_upper(offset, 8) +
                           " (record_bytes=" + text::decimal(record_bytes) + "), stopping");
        MIE_LOG_WARN("dump: offset overflow at 0x" + text::hex_upper(offset, 1) +
                     " (record_bytes=" + text::decimal(record_bytes) + "); stopping record scan");
        return false;
    }

    const std::size_t end = offset + record_bytes;
    if (end > file_size) {
        emit_line(out, "  !! Truncated record at 0x" + text::hex_upper(offset, 8) + " (" +
                           text::decimal(record_bytes) + " bytes needed, " +
                           text::decimal(file_size - offset) + " available)");
        MIE_LOG_WARN("dump: truncated record at 0x" + text::hex_upper(offset, 1) + " (" +
                     text::decimal(record_bytes) + " bytes needed, " +
                     text::decimal(file_size - offset) + " available); stopping record scan");
        return false;
    }
    record_end = end;
    return true;
}

uint16_t word_at(const std::vector<uint8_t>& data, std::size_t offset) {
    uint16_t value = 0;
    if (!decode::read_u16(data.empty() ? nullptr : &data[0], data.size(), offset, value)) {
        return 0;
    }
    return value;
}

/// The decoded-header block for one record.
void write_annotation(std::FILE* out, const std::vector<uint8_t>& data, const TypeWord& type_word,
                      std::size_t offset, std::size_t record_bytes, uint64_t record_number) {
    // The timestamp is decoded as IRIG unconditionally. For a Standard-format
    // file this line is wrong and the raw bytes below remain authoritative --
    // which is the honest trade for a view whose whole job is to run before the
    // format has been established.
    const IrigTimestamp timestamp = decode::decode_irig_timestamp(
        word_at(data, offset + 2), word_at(data, offset + 4), word_at(data, offset + 6));
    const CommandWord command = decode::decode_command_word(word_at(data, offset + 8));

    MessageFormat format = FORMAT_RECEIVE;
    // A record that cannot be classified gets a label, not an error: this runs
    // on suspect files by definition.
    const bool classified = decode::classify_message_format(type_word.message_type, command,
                                                            type_word.word_count, 3, format);

    emit_line(out, rule());
    emit_line(out, "  Record #" + text::decimal(record_number) + "  @  0x" +
                       text::hex_upper(offset, 8) + "  (" + text::decimal(record_bytes) +
                       " bytes, " + text::decimal(type_word.word_count) + " words)");
    emit_line(out, "  Type:   0x" + text::hex_upper(type_word.raw, 4) + "  ->  " +
                       type_label(type_word.message_type) + "  Bus " + bus_name(type_word.bus) +
                       "  error flag (bit 14): " + (type_word.error ? "SET" : "clear"));
    emit_line(out,
              std::string("  Format: ") + (classified ? format_label(format) : "(unclassifiable)"));
    emit_line(out, "  Time:   " + timestamp.format() + (timestamp.freerun ? "  [FREERUN]" : ""));
    emit_line(out, "  Cmd:    0x" + text::hex_upper(command.raw, 4) + "  ->  RT" +
                       text::decimal(command.rt) + " SA" + text::decimal(command.subaddress) + " " +
                       (command.direction == DIRECTION_TRANSMIT ? "T" : "R") +
                       " WC=" + text::decimal(command.data_word_count));

    if (type_word.error) {
        // The Error Word is the last word of an errored record. Showing its DDC
        // description here is what makes the reason legible without a trip to
        // the error catalogue.
        const uint16_t code =
            word_at(data, offset + (static_cast<std::size_t>(type_word.word_count) - 1) * 2);
        emit_line(out, "  Error:  0x" + text::hex_upper(code, 4) + "  ->  " +
                           ddc_error_description_or_unknown(code));
    }
}

}  // namespace

void hex_dump_raw(const std::string& path, std::size_t offset, const Optional<std::size_t>& length,
                  std::FILE* out) {
    const std::vector<uint8_t> data = read_whole_file(path);
    const std::size_t file_size = data.size();

    // Clamped, not validated. An operator exploring a damaged file should get
    // an empty range back rather than an argument error -- the empty output
    // already says the offset is past the end.
    const std::size_t start = offset < file_size ? offset : file_size;
    std::size_t end = file_size;
    if (length.has_value()) {
        const std::size_t requested = length.value();
        // Saturate rather than wrap: `--offset 4096 --length <huge>` must mean
        // "to the end", not "back to the beginning".
        end = (offset > static_cast<std::size_t>(-1) - requested) ? file_size : offset + requested;
        if (end > file_size) {
            end = file_size;
        }
    }
    if (end < start) {
        end = start;
    }

    emit_line(out, "File: " + file_name_of(path) + " (" + text::decimal(file_size) + " bytes)");
    emit_line(out, "Range: 0x" + text::hex_upper(start, 8) + "-0x" + text::hex_upper(end, 8));
    emit_line(out, "");

    write_hex_block(out, "  ", &data[0] + start, end - start, start);
    if (std::fflush(out) != 0) {
        platform::OsError err;
        platform::capture_stream_error(err);
        throw MieError::writer_error("stdout", err.message, err.code);
    }
}

void hex_dump_records(const std::string& path, const Optional<uint64_t>& max_records,
                      std::size_t offset, std::FILE* out) {
    const std::vector<uint8_t> data = read_whole_file(path);
    const std::size_t file_size = data.size();

    emit_line(out, "File: " + file_name_of(path) + " (" + text::decimal(file_size) + " bytes)");
    emit_line(out, "Record dump starting at offset 0x" + text::hex_upper(offset, 8));
    emit_line(out, "");

    std::size_t at = offset;
    uint64_t record_number = 0;
    for (;;) {
        if (at > static_cast<std::size_t>(-1) - decode::MIN_RECORD_BYTES) {
            break;
        }
        if (at + decode::MIN_RECORD_BYTES > file_size) {
            break;
        }
        if (max_records.has_value() && record_number >= max_records.value()) {
            break;
        }

        uint16_t type_raw = 0;
        if (!decode::read_u16(&data[0], file_size, at, type_raw)) {
            break;
        }
        const TypeWord type_word = decode::decode_type_word(type_raw);

        std::size_t record_end = 0;
        if (!record_extent(type_word, at, file_size, out, record_end)) {
            break;
        }

        write_annotation(out, data, type_word, at, record_end - at, record_number);
        write_hex_block(out, "    ", &data[0] + at, record_end - at, at);
        emit_line(out, "");

        at = record_end;
        record_number += 1;
    }

    emit_line(out, rule());
    emit_line(out, text::decimal(record_number) + " records dumped.");
    if (std::fflush(out) != 0) {
        platform::OsError err;
        platform::capture_stream_error(err);
        throw MieError::writer_error("stdout", err.message, err.code);
    }
}

}  // namespace dump
}  // namespace mie
