// SPDX-License-Identifier: Apache-2.0
//
// Pure binary-to-struct conversion. No I/O, no logging, no allocation beyond
// the strings in a violation's detail text.
//
// Mirrors `rust/src/decode.rs` (which the Python package also runs). Every
// bit position and threshold here is wire format, documented in
// docs/MIE-FORMAT.md; a change is a change to what the decoder reads.
//
// The functions divide into four groups:
//
//   1. primitive little-endian readers, bounds-checked
//   2. field decoders  (Type Word, timestamps, Command Word)
//   3. classification  (message format, including the mode-code shapes)
//   4. structural invariants and the timestamp-format probe
//
// Group 4 is where the two severity classes live, and the distinction matters
// operationally: a Reject violation means the record is corrupt, while an
// AnomalyWarn means the record decodes fine but something on the bus looked
// odd. Treating the second as the first produces false negatives on real
// recordings (L2-SYN-024 fires on ordinary bus interference), which is why the
// severity travels with the violation rather than being decided by the caller.

#ifndef MIE_DECODE_HPP
#define MIE_DECODE_HPP

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "mie/models.hpp"
#include "mie/optional.hpp"

namespace mie {
namespace decode {

// --- Record size floors ---------------------------------------------------
//
// The two timestamp formats give a record two different minimum sizes, and
// using the wrong one is a real bug rather than an off-by-one: validating a
// Standard-format file against the IRIG floor rejects every record in it.

/// IRIG: Type(2) + timestamp(6) + Cmd(2).
const std::size_t MIN_RECORD_BYTES = 10;
/// Standard: Type(2) + timestamp(4) + Cmd(2).
const std::size_t MIN_RECORD_BYTES_STANDARD = 8;
/// IRIG: Type(1) + timestamp(3) + Cmd(1).
const uint16_t MIN_RECORD_WORDS = 5;
/// Standard: Type(1) + timestamp(2) + Cmd(1).
const uint16_t MIN_RECORD_WORDS_STANDARD = 4;

// --- Terminator -----------------------------------------------------------

/// A null Type Word ends the record stream (L2-RDR-021 / L2-SYN-028).
///
/// MIE files have no header and no footer: records start at byte 0, and a
/// 0x0000 Type Word caps the stream. It is unambiguous because the word-count
/// field (bits 8-13) is zero, and the minimum valid record is four words -- so
/// this value can never be a record. A file consisting solely of this word is
/// an empty recording: a channel that captured no traffic, which is a normal
/// outcome and not an error.
const uint16_t TERMINATOR_TYPE_WORD = 0x0000;

bool is_terminator_type_word(uint16_t raw);

// --- Primitive readers ----------------------------------------------------
//
// Bounds-checked and total: on an out-of-range read they report failure rather
// than reading past the mapping. The reader runs over a memory-mapped file
// whose length is the only thing standing between a corrupt word count and a
// segfault, so "returns false" is the whole safety story here (L1-ROB-001).

/// Read one little-endian uint16 at `offset`. False when out of range.
bool read_u16(const uint8_t* data, std::size_t size, std::size_t offset, uint16_t& out);

/// Read `count` little-endian uint16s starting at `offset` into `out`.
/// False when the range is out of bounds; `out` is untouched in that case.
bool read_u16_array(const uint8_t* data, std::size_t size, std::size_t offset, std::size_t count,
                    uint16_t* out);

// --- Field decoders -------------------------------------------------------

/// Type Word bit layout: type in 0-6, bus in 7, word count in 8-13,
/// error in 14, reserved in 15.
TypeWord decode_type_word(uint16_t raw);

/// IRIG timestamp from its three words. Bit layout in docs/MIE-FORMAT.md:
/// freerun in upper 15, day in upper 5-13, hour in upper 0-4, minute in
/// middle 10-15, second in middle 4-9, and the microsecond split across
/// middle 0-3 (high) and the whole lower word.
IrigTimestamp decode_irig_timestamp(uint16_t upper, uint16_t middle, uint16_t lower);

/// Standard timestamp: a 32-bit free-running counter, upper word first.
StandardTimestamp decode_standard_timestamp(uint16_t upper, uint16_t lower);

/// Command Word: RT in 11-15, direction in 10, subaddress in 5-9,
/// data word count in 0-4 -- where a raw 0 means 32, not zero.
CommandWord decode_command_word(uint16_t raw);

// --- MUX from the file name (L2-WRT-020) ----------------------------------

const bool DEFAULT_MUX_ENABLED = true;
extern const char* const DEFAULT_MUX_DELIMITER;

/// The 5th delimiter-separated field, matching the
/// `name.part.part.part.<MUX>.part.ext` recorder convention.
const int64_t DEFAULT_MUX_FIELD = 4;

/// Extract the MUX value from a file NAME (a basename, never a path).
///
/// Splits on `delimiter` and returns the `field`-th part, trimmed. A negative
/// `field` counts from the end, so -1 is the last part. Declines -- yielding an
/// empty MUX column -- when the index is out of range, the selected field is
/// empty after trimming, or the delimiter is empty.
bool mux_from_filename(const std::string& file_name, const std::string& delimiter, int64_t field,
                       std::string& out);

// --- Message format classification ----------------------------------------

/// Classify the payload layout from the type code and Command Word.
///
/// False for a type code outside the known set; the caller raises
/// UnknownTypeWord with the offset it knows and this function does not.
bool classify_message_format(uint8_t message_type, const CommandWord& command_word,
                             uint16_t word_count, uint16_t timestamp_words, MessageFormat& out);

// --- Structural invariants ------------------------------------------------

/// Which invariant a record violated, so the reader can phrase a precise
/// diagnostic instead of collapsing everything into one PayloadError.
enum WhichInvariant {
    /// L2-SYN-020: type 0x02 (BC to RT) requires Cmd direction = Receive.
    INVARIANT_DIRECTION_BC_TO_RT,
    /// L2-SYN-021: type 0x04 (RT to BC) requires Cmd direction = Transmit.
    INVARIANT_DIRECTION_RT_TO_BC,
    /// L2-SYN-022: Type Word word count too small for the declared payload.
    INVARIANT_WORD_COUNT_CAPACITY,
    /// L2-SYN-023: RT-to-RT Cmd2 direction must be Receive.
    INVARIANT_DIRECTION_RT_TO_RT_CMD2,
    /// L2-SYN-024: Status Word RT does not match Command Word RT.
    INVARIANT_STATUS_RT_MISMATCH,
    /// L2-SYN-025: Type Word bit 15 (reserved) is set.
    INVARIANT_TYPE_WORD_RESERVED_BIT,
    /// L2-SYN-027: RT-to-RT Cmd1 and Cmd2 disagree on data word count.
    INVARIANT_DATA_WORD_COUNT_MISMATCH
};

/// What a violation means for the record.
///
/// The two classes are not interchangeable. REJECT means the record is corrupt:
/// strict mode aborts, lenient mode skips it. ANOMALY_WARN means the record
/// decodes correctly but something looked unusual -- and both modes emit it,
/// because rejecting on these would produce false negatives on real recordings,
/// where bus interference and undocumented vendor extensions both occur.
enum InvariantSeverity { SEVERITY_REJECT, SEVERITY_ANOMALY_WARN };

struct InvariantViolation {
    WhichInvariant kind;
    InvariantSeverity severity;
    std::string detail;

    InvariantViolation();
    InvariantViolation(WhichInvariant kind, InvariantSeverity severity, const std::string& detail);
};

/// Pre-extract checks: per-type direction (L2-SYN-020/021) and the word-count
/// capacity check (L2-SYN-022). True when the record is structurally sound.
bool validate_structural_invariants(const TypeWord& tw, const CommandWord& cmd,
                                    MessageFormat msg_fmt, uint16_t ts_words,
                                    InvariantViolation& out);

/// Post-extract checks for the RT-to-RT formats (L2-SYN-023, L2-SYN-027).
///
/// Separate from the pre-extract pass because Cmd2 lives inside the payload and
/// does not exist until it has been extracted. A no-op for other formats, and
/// when cmd2 is absent.
///
/// L2-SYN-027 matters more than it looks: the capacity check only ever sees
/// Cmd1, so a Cmd2 that over-claims its data word count would otherwise go
/// unnoticed.
bool validate_post_extract_invariants(MessageFormat msg_fmt, const CommandWord& cmd,
                                      const Optional<CommandWord>& cmd2, InvariantViolation& out);

/// AnomalyWarn-class observations (L2-SYN-024, L2-SYN-025).
///
/// Returns every anomaly found rather than the first: a single record can trip
/// both, and reporting only one would hide the other from an operator trying to
/// characterise a noisy bus.
std::vector<InvariantViolation> detect_record_anomalies(const TypeWord& tw, const CommandWord& cmd,
                                                        const Optional<uint16_t>& status_word);

// --- Timestamp-format auto-detection (L2-DEC-015 / L2-DEC-016) ------------

/// How strong the probe's conclusion was.
enum DetectionConfidence {
    /// High absolute score and a wide margin.
    CONFIDENCE_DECISIVE,
    /// Cleared the floor without reaching decisive. Used, and logged at INFO.
    CONFIDENCE_MARGINAL,
    /// Both candidates scored too low, or too close to separate. Strict mode
    /// raises TimestampFormatMismatch; lenient mode WARNs and proceeds.
    CONFIDENCE_AMBIGUOUS
};

struct DetectionOutcome {
    /// The chosen format. IRIG wins ties (L2-DEC-012).
    TimestampFormat format;
    int32_t irig_score;
    int32_t std_score;
    /// How many records were actually scored -- fewer than requested when EOF
    /// arrived first or a record's declared length was impossible.
    std::size_t records_probed;
    DetectionConfidence confidence;

    DetectionOutcome();
};

// L2-DEC-016 thresholds, deliberately conservative: they fire only when the
// probe genuinely could not distinguish, not merely because a small probe set
// produced a low absolute score. The floor of 4 lets a single decisive record
// through -- one perfect IRIG record scores 5, one perfect Standard record 4.
const int32_t CONFIDENCE_FLOOR = 4;
const int32_t MIN_MARGIN = 3;
const int32_t DECISIVE_FLOOR = 8;
const int32_t DECISIVE_MARGIN = 6;

/// Default probe size, overridable by `decode.detect_records` / --detect-records.
const std::size_t DEFAULT_DETECT_RECORDS = 8;

/// Walk up to `max_records` from `first_offset`, scoring IRIG against Standard.
///
/// `max_records` is clamped to at least 1. The walk is bounded by the file
/// length and stops early at EOF or on a structurally impossible record length.
DetectionOutcome probe_timestamp_format(const uint8_t* data, std::size_t size,
                                        std::size_t first_offset, std::size_t max_records);

}  // namespace decode
}  // namespace mie

#endif  // MIE_DECODE_HPP
