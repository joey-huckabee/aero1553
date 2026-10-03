// SPDX-License-Identifier: Apache-2.0
//
// Core data structures for decoded MIL-STD-1553 MIE binary records.
//
// Mirrors `rust/src/models.rs` and `python/src/aero1553/models.py`. The three
// are kept deliberately similar in shape -- same field names, same enum
// discriminants, same helper names -- because the shared conformance oracles
// can only hold them to account if a reader can put them side by side.
//
// Enum discriminants are explicit and are the values that appear ON THE WIRE or
// in the CSV. They are format, not implementation detail: changing one is a
// change to what the decoder reads, not a refactor.

#ifndef MIE_MODELS_HPP
#define MIE_MODELS_HPP

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

#include "mie/optional.hpp"

namespace mie {

// --- Enums ----------------------------------------------------------------

/// MIL-STD-1553 redundant bus identifier.
enum Bus { BUS_A = 0, BUS_B = 1 };

const char* bus_name(Bus bus);

/// Message transfer direction, from the RT's perspective.
///
/// The numeric values are load-bearing beyond identification: the canonical row
/// order sorts by (RT, subaddress, direction), and RECEIVE = 0 before
/// TRANSMIT = 1 is what makes "R before T" fall out of the comparison rather
/// than needing a special case. The Rust and Python implementations rely on the
/// same trick (L2-WRT-021); do not renumber.
enum Direction { DIRECTION_RECEIVE = 0, DIRECTION_TRANSMIT = 1 };

/// "Receive" / "Transmit", as the Rust implementation's Debug output spells
/// them. Diagnostic text only -- the CSV uses the single letters R and T, which
/// `MieMessage::msg_label` produces.
const char* direction_name(Direction direction);

/// DDC MIE Type Word message type code (bits 0-6).
enum MessageType {
    MESSAGE_TYPE_MODE_COMMAND = 0x01,
    MESSAGE_TYPE_BC_TO_RT = 0x02,
    MESSAGE_TYPE_RT_TO_BC = 0x04,
    MESSAGE_TYPE_RT_TO_RT = 0x08,
    MESSAGE_TYPE_BROADCAST_BC_TO_RT = 0x10,
    MESSAGE_TYPE_BROADCAST_RT_TO_RT = 0x18,
    MESSAGE_TYPE_SPURIOUS_DATA = 0x20
};

/// True when `code` is one of the seven known type codes.
///
/// Hot: `sync` calls it for every candidate record offset while scanning, so it
/// is a switch rather than a container lookup.
bool is_valid_message_type(uint8_t code);

/// Canonical name, matching the Python enum member and the names the CLI
/// filters accept (`--exclude-types MODE_COMMAND`). Empty for an unknown code.
const char* message_type_name(uint8_t code);

/// Resolve a CLI/config type name to its code, case-insensitively.
/// Returns false for an unrecognised name; the caller formats its own error.
bool message_type_from_name(const std::string& name, uint8_t& out_code);

/// Classified message format, which determines the payload layout.
enum MessageFormat {
    FORMAT_RECEIVE = 1,
    FORMAT_TRANSMIT = 2,
    FORMAT_RT_TO_RT = 3,
    FORMAT_RECEIVE_BROADCAST = 4,
    FORMAT_RT_TO_RT_BROADCAST = 5,
    FORMAT_MODE_CODE_TX_DATA = 6,
    FORMAT_MODE_CODE_RX_DATA = 7,
    FORMAT_MODE_CODE_NO_DATA = 8,
    FORMAT_MODE_CODE_BCAST_NO_DATA = 9,
    FORMAT_MODE_CODE_BCAST_DATA = 10,
    FORMAT_SPURIOUS_DATA = 11
};

/// CamelCase name, matching the Rust variant spelling ("RtToRt",
/// "ModeCodeBcastNoData"). Diagnostic text; empty for an unrecognised value.
const char* message_format_name(MessageFormat format);

/// Timestamp encoding used in the MIE binary file.
enum TimestampFormat { TIMESTAMP_AUTO = 0, TIMESTAMP_IRIG = 1, TIMESTAMP_STANDARD = 2 };

/// "Auto" / "Irig" / "Standard", matching the Rust Debug spelling. Note the
/// asymmetry with `timestamp_format_from_name`, which accepts the lowercase CLI
/// spellings: this one is for log lines, that one is for input.
const char* timestamp_format_name(TimestampFormat fmt);

/// Parse `auto` / `irig` / `standard` case-insensitively.
///
/// One source of truth shared by the CLI (`--input-time-format`) and the config
/// loader (`decode.input_time_format`), so the two cannot drift on which spellings
/// they accept. Returns false for an unrecognised name.
bool timestamp_format_from_name(const std::string& name, TimestampFormat& out);

/// Number of 16-bit words each timestamp format consumes. Zero for AUTO, which
/// is a request to probe rather than a layout.
uint16_t timestamp_word_count(TimestampFormat fmt);

/// How errored messages are routed in CSV output.
enum ErrorMode {
    /// Errored and spurious records go to a separate `<stem>_errors.csv`.
    ERROR_MODE_SEPARATE = 0,
    /// Default. Everything in one CSV, with ERROR / ERROR_CODE populated.
    ERROR_MODE_INLINE = 1
};

/// Scope over which DELTA is measured in a multi-file merge (L2-MRG-005).
/// Meaningful only with more than one input.
enum DeltaScope { DELTA_SCOPE_PER_FILE = 0, DELTA_SCOPE_GLOBAL = 1 };

bool delta_scope_from_name(const std::string& name, DeltaScope& out);
const char* delta_scope_name(DeltaScope scope);

// --- DDC and decoder-assigned error codes ---------------------------------
//
// The 0x01xx codes come from the DDC card. The 0x20xx codes are assigned by
// this decoder and have no hardware counterpart -- they classify a
// SPURIOUS_DATA record by whether it follows an errored one. See
// docs/ERROR-CATALOG.md for the operator-facing table.

const uint16_t ERROR_MANCHESTER_PARITY = 0x011E;
const uint16_t ERROR_NO_RESPONSE = 0x0120;
const uint16_t ERROR_INVERTED_SYNC = 0x0136;
const uint16_t ERROR_TOO_MANY_WORDS = 0x0140;
const uint16_t ERROR_UNKNOWN_DDC = 0x0150;

const uint16_t ERROR_SPURIOUS_CONTINUATION = 0x2000;
const uint16_t ERROR_SPURIOUS_STANDALONE = 0x2001;

bool is_known_ddc_error_code(uint16_t code);
bool is_known_custom_error_code(uint16_t code);
bool is_known_error_code(uint16_t code);

/// Description for a known code, or an empty string.
const char* ddc_error_description(uint16_t code);

/// Description for a known code, or "unknown DDC error code".
///
/// Prefer this anywhere the result reaches a human: the empty-string form
/// renders as an uninformative `code=0x0199 ()`, and the Python implementation
/// always carries a fallback, so using it keeps the operator-facing text
/// aligned across implementations.
const char* ddc_error_description_or_unknown(uint16_t code);

// --- Output timestamp rendering (L2-WRT-025) ------------------------------

/// Rendering selected for the `TIME_STAMP` CSV column (L2-WRT-025).
///
/// The *output* half of what used to be one `input_time_format` setting.
/// `TimestampFormat` decides how the bytes on disk are parsed; this decides how
/// the resulting instant is written down. The two are independent, subject to
/// the calendar preconditions of L2-WRT-026.
enum OutputTimeFormat {
    /// `DAY:HH:MM:SS.uuuuuu` -- the DDC vendor rendering, and the default.
    /// Byte-identical to what every version before v3.0.0 emitted.
    OUTPUT_TIME_DOY = 0,
    /// `YYYY-MM-DDTHH:MM:SS.uuuuuu` plus a zone designator.
    OUTPUT_TIME_ISO = 1,
    /// `DD:HH:MM:SS.uuuuuu` -- day of month, month deliberately absent.
    OUTPUT_TIME_DOM = 2
};

/// "doy" / "iso" / "dom" -- the canonical lowercase spelling, for diagnostics.
const char* output_time_format_name(OutputTimeFormat fmt);

/// Parse `doy` / `iso` / `dom` case-insensitively.
///
/// Shared by the CLI (`--output-time-format`) and the config loader
/// (`output.output_time_format`), mirroring `timestamp_format_from_name`.
/// Returns false for an unrecognised name; each caller formats its own error.
bool output_time_format_from_name(const std::string& name, OutputTimeFormat& out);

/// Whether a rendering resolves day-of-year to a calendar date, and so carries
/// the L2-WRT-026 preconditions (a year is required; the recording must be
/// calendar-locked).
bool output_time_format_needs_calendar(OutputTimeFormat fmt);

/// Inclusive bounds on a configured calendar year (L2-WRT-026 clause 1).
///
/// The upper bound is four digits because the `iso` rendering formats the year
/// as exactly `YYYY`; a five-digit year would widen column 1 without warning.
/// The lower bound is 1 because year 0 does not exist in the proleptic
/// Gregorian numbering this decoder uses.
const int YEAR_MIN = 1;
const int YEAR_MAX = 9999;

/// Everything the `TIME_STAMP` formatter needs beyond the timestamp itself
/// (L2-WRT-025). Resolved once per decode from the merged configuration.
///
/// Written with a constructor rather than default member initialisers: this
/// tree is C++11 as accepted by GCC 4.8.5, where a class with an NSDMI is not
/// an aggregate and cannot be brace-initialised (ADR-0001).
struct TimeRender {
    /// Which of the three renderings to produce.
    OutputTimeFormat format;
    /// Calendar year for the day-of-year resolution. Required by ISO and DOM,
    /// ignored by DOY (L2-WRT-026 clause 5). Absent means no year was
    /// configured, which the CLI refuses up front for a calendar rendering.
    Optional<int> year;
    /// Offset from UTC in minutes, used only by ISO. Zero renders as `Z`.
    int utc_offset_minutes;

    /// The default rendering: day-of-year, no calendar resolution. What `dump`
    /// uses unconditionally, and what every pre-v3.0.0 decode produced.
    TimeRender();
};

/// Why a calendar rendering could not be produced (L2-WRT-026).
///
/// Two of the preconditions are checked before the first row is written -- a
/// missing year at CLI parse time, a Standard recording once the format
/// resolves -- so the case that normally reaches a formatter is a day-of-year
/// the configured year does not have.
enum CalendarError {
    CALENDAR_OK = 0,
    /// A calendar rendering was selected with no year resolved.
    CALENDAR_MISSING_YEAR = 1,
    /// The day-of-year does not exist in the configured year.
    CALENDAR_NO_SUCH_DAY = 2,
    /// The recording uses the Standard encoding: a free-running counter with
    /// no epoch and no calendar meaning, whatever year is configured.
    CALENDAR_NOT_LOCKED = 3,
    /// The IRIG timestamp is freerun -- the card had no valid IRIG-B lock, so
    /// its fields are relative. They would render as a plausible date that
    /// means nothing.
    CALENDAR_FREERUN = 4
};

/// Proleptic Gregorian leap-year test (L2-WRT-025): divisible by 4, except
/// centuries, which must also be divisible by 400.
///
/// Hand-rolled rather than delegated to a date library so that all three
/// implementations compute it identically -- C++11 has no date type at all, and
/// a shared rule stated once is easier to hold aligned than three library
/// behaviours that merely agree today.
bool is_leap_year(int year);

/// Resolve a 1-based day-of-year to `(month, day_of_month)`, both 1-based.
///
/// Returns false when the day does not exist in that year: day 366 of a common
/// year, day 0, or anything above 366. That is the L2-WRT-026 clause 3
/// condition, which the caller turns into a refusal rather than rolling
/// forward into the next January.
bool day_of_year_to_month_day(int year, int day_of_year, int& month, int& day_of_month);

/// Render a UTC offset in minutes as an ISO-8601 designator: `Z` at zero,
/// otherwise `+HH:MM` / `-HH:MM`.
std::string format_utc_offset(int minutes);

// --- Timestamps -----------------------------------------------------------

/// IRIG timestamp, decoded from a 3-word binary field.
struct IrigTimestamp {
    uint16_t day;
    uint8_t hour;
    uint8_t minute;
    uint8_t second;
    uint32_t microsecond;
    bool freerun;

    IrigTimestamp();
    IrigTimestamp(uint16_t day, uint8_t hour, uint8_t minute, uint8_t second, uint32_t microsecond,
                  bool freerun);

    /// Absolute microseconds from the start of the year.
    uint64_t to_total_microseconds() const;

    /// `DAY:HH:MM:SS.uuuuuu`, matching the DDC vendor CSV layout.
    ///
    /// The microsecond field is exactly six digits (L2-DEC-014). Validation
    /// rejects a microsecond >= 1000000 (L2-SYN-004), so the modulo here is
    /// defensive: a caller that builds an IrigTimestamp directly, bypassing
    /// validation, still gets a well-formed string rather than a seven-digit
    /// field that would shift every subsequent CSV column.
    std::string format() const;

    /// Format under the selected rendering (L2-WRT-025).
    ///
    /// DOY is infallible and byte-identical to `format()`. The calendar
    /// renderings resolve `day` against `render.year` and refuse rather than
    /// approximate when that resolution has no answer -- day 366 of a common
    /// year does not become January 1st (L2-WRT-026 clause 3).
    ///
    /// Every rendering emits exactly six microsecond digits; L2-DEC-014 is not
    /// relaxed by the wider cell.
    ///
    /// Returns CALENDAR_OK and fills `out` on success; otherwise returns the
    /// reason and leaves `out` untouched.
    CalendarError format_with(const TimeRender& render, std::string& out) const;

    bool operator==(const IrigTimestamp& other) const;
    bool operator!=(const IrigTimestamp& other) const { return !(*this == other); }
};

/// Standard timestamp: a 2-word free-running counter.
struct StandardTimestamp {
    uint32_t raw_value;
    uint16_t upper_word;
    uint16_t lower_word;

    StandardTimestamp();
    StandardTimestamp(uint32_t raw_value, uint16_t upper_word, uint16_t lower_word);

    /// Raw counter ticks. The tick rate is card-dependent and is NOT encoded in
    /// the file, so this cannot be converted to time without external
    /// calibration -- which is why `to_microseconds` takes the rate as an
    /// argument and can decline.
    uint32_t raw_ticks() const { return raw_value; }

    /// Convert ticks to microseconds using an externally supplied rate.
    ///
    /// Declines (returns false) unless the rate is finite and strictly
    /// positive, so an uncalibrated or nonsensical rate can never be mistaken
    /// for real timing. Rounding is half-away-from-zero; ticks are
    /// non-negative, so this matches Python's `int(x + 0.5)` exactly
    /// (L2-DEC-017).
    bool to_microseconds(double tick_rate_hz, uint64_t& out) const;

    /// `0xNNNNNNNN`.
    std::string format() const;

    /// Under DOY this is `format()`. Under a calendar rendering it refuses
    /// (L2-WRT-026 clause 2): a free-running counter has no epoch, so no year
    /// can place it on a calendar, and quietly emitting raw hex into a column
    /// the operator asked to be ISO-8601 would be its own kind of lie.
    CalendarError format_with(const TimeRender& render, std::string& out) const;

    bool operator==(const StandardTimestamp& other) const;
    bool operator!=(const StandardTimestamp& other) const { return !(*this == other); }
};

/// Tagged union of the two timestamp encodings.
///
/// A struct with a discriminant rather than a real union: both alternatives are
/// small trivially-copyable aggregates, so holding both costs a dozen bytes and
/// removes every question about which member is live.
struct Timestamp {
    TimestampFormat format_kind;
    IrigTimestamp irig;
    StandardTimestamp standard;

    Timestamp();
    static Timestamp from_irig(const IrigTimestamp& value);
    static Timestamp from_standard(const StandardTimestamp& value);

    bool is_irig() const { return format_kind == TIMESTAMP_IRIG; }
    bool is_standard() const { return format_kind == TIMESTAMP_STANDARD; }

    /// Absolute microseconds from a known epoch, when one exists.
    ///
    /// Always available for IRIG (microseconds from the start of the year).
    /// For Standard it depends on calibration: available only when
    /// `tick_rate_hz` holds a finite, strictly-positive frequency, because raw
    /// counter ticks have no known rate or epoch and a DELTA in seconds cannot
    /// be computed truthfully without one. IRIG ignores `tick_rate_hz`.
    /// See L2-DEC-017.
    bool to_microseconds(const Optional<double>& tick_rate_hz, uint64_t& out) const;

    std::string format() const;

    /// Format under the selected rendering (L2-WRT-025), dispatching on the
    /// discriminant. This is what the CSV writer calls; `format()` remains the
    /// day-of-year rendering that L2-WRT-011 pins and that `dump` uses.
    CalendarError format_with(const TimeRender& render, std::string& out) const;

    bool operator==(const Timestamp& other) const;
    bool operator!=(const Timestamp& other) const { return !(*this == other); }
};

// --- Type Word and Command Word -------------------------------------------

struct TypeWord {
    uint8_t message_type;
    Bus bus;
    uint16_t word_count;
    /// Bit 14. When set, the card truncated the payload and appended an Error
    /// Word; see the error-handling model in docs/MIE-FORMAT.md.
    bool error;
    uint16_t raw;

    TypeWord();
    TypeWord(uint8_t message_type, Bus bus, uint16_t word_count, bool error, uint16_t raw);

    bool operator==(const TypeWord& other) const;
    bool operator!=(const TypeWord& other) const { return !(*this == other); }
};

struct CommandWord {
    /// Remote Terminal address, 0-30; 31 means broadcast.
    uint8_t rt;
    Direction direction;
    /// Subaddress, 0-31; 0 and 31 designate mode codes.
    uint8_t subaddress;
    /// Data word count, 1-32. A raw field of 0 means 32.
    uint8_t data_word_count;
    uint16_t raw;

    CommandWord();
    CommandWord(uint8_t rt, Direction direction, uint8_t subaddress, uint8_t data_word_count,
                uint16_t raw);

    bool is_broadcast() const { return rt == 31; }
    bool is_mode_code() const { return subaddress == 0 || subaddress == 31; }

    bool operator==(const CommandWord& other) const;
    bool operator!=(const CommandWord& other) const { return !(*this == other); }
};

// --- DataWords ------------------------------------------------------------

/// MIL-STD-1553B caps a single transaction at 32 data words.
const std::size_t MAX_DATA_WORDS = 32;

/// Fixed-capacity 16-bit word buffer.
///
/// Deliberately NOT a std::vector. The cap is a property of the bus standard,
/// not a tuning choice, and an inline buffer keeps the per-record path free of
/// heap allocation -- which is what makes the O(1)-memory claim (L3-CPP-011)
/// hold for a multi-gigabyte recording. The Rust implementation uses `[u16; 32]`
/// for the same reason; do not "generalise" this to a dynamic container.
class DataWords {
  public:
    DataWords();

    /// Build from a raw pointer and length, keeping at most MAX_DATA_WORDS.
    ///
    /// An over-long input is TRUNCATED rather than rejected or fatal. A
    /// standard-conforming transaction cannot exceed the cap, so this only
    /// engages on corrupt input -- where staying total matters more than being
    /// strict, because the reader's job there is to keep going (L1-ROB-001).
    static DataWords from_words(const uint16_t* words, std::size_t count);

    /// Append one word. Returns false when already full.
    bool try_push(uint16_t word);

    std::size_t size() const { return len_; }
    bool empty() const { return len_ == 0; }

    /// Unchecked; callers index within size().
    uint16_t operator[](std::size_t index) const { return buf_[index]; }

    const uint16_t* data() const { return buf_; }

    /// Range-for support over the live prefix.
    ///
    /// Present so `for (uint16_t w : words)` reads the same in all three
    /// implementations: Rust has `IntoIterator for &DataWords` and Python's
    /// `data_words` is a tuple. Without these, C++ callers had to spell out
    /// `data()`/`size()` and index by hand -- the one place the three diverged
    /// on how the payload is traversed.
    const uint16_t* begin() const { return buf_; }
    const uint16_t* end() const { return buf_ + len_; }

    void clear() { len_ = 0; }

    bool operator==(const DataWords& other) const;
    bool operator!=(const DataWords& other) const { return !(*this == other); }

  private:
    uint16_t buf_[MAX_DATA_WORDS];
    uint8_t len_;
};

// --- MieMessage -----------------------------------------------------------

/// One decoded record.
///
/// A record, not a service: the fields are public because this is a value that
/// gets copied, filtered, ordered and formatted, and the Rust and Python
/// counterparts are plain structs too. Keeping the three readable against each
/// other is what the conformance work depends on.
struct MieMessage {
    Timestamp timestamp;
    TypeWord type_word;
    MessageFormat message_format;
    Optional<CommandWord> command_word;
    Optional<CommandWord> command_word_2;
    Optional<uint16_t> status_word;
    Optional<uint16_t> status_word_2;
    DataWords data_words;
    Optional<uint16_t> error_word;

    /// Seconds since the previous record with the same RT+MSG key.
    ///
    /// Present and zero on the first occurrence of a key with a calibrated
    /// timestamp; present and non-negative for a gap. ABSENT wherever no DELTA
    /// is meaningful -- SPURIOUS_DATA has no RT/MSG key, an uncalibrated
    /// Standard timestamp has no tick rate, and a non-monotonic timestamp has
    /// no honest gap. Absent renders as an empty CSV cell, which is why this is
    /// an Optional and not a sentinel value.
    Optional<double> delta;

    uint64_t file_offset;

    /// MUX column, derived from the source file name (L2-WRT-020).
    ///
    /// Shared rather than copied: one string per input file, held by
    /// shared_ptr, so carrying it on every record stays O(1) in resident memory
    /// across a merge of many inputs. Null when MUX is disabled or the
    /// configured filename field is absent or empty.
    std::shared_ptr<const std::string> mux;

    MieMessage();

    /// RT address, absent for a record with no Command Word.
    Optional<uint8_t> rt() const;
    Optional<uint8_t> subaddress() const;
    Bus bus() const { return type_word.bus; }

    /// The `MSG` column: `<subaddress><T|R>`, empty for SPURIOUS_DATA.
    std::string msg_label() const;

    /// Key for per-RT/MSG DELTA tracking: `<rt>:<subaddress><T|R>`.
    /// Empty for SPURIOUS_DATA, which is therefore never DELTA-tracked.
    std::string delta_key() const;

    bool is_error() const { return type_word.error; }
    bool is_spurious() const { return message_format == FORMAT_SPURIOUS_DATA; }

    /// A SPURIOUS_DATA record continuing the errored record decoded just before
    /// it (decoder code 0x2000, L2-ERR-005). Filtering and duplicate collapse
    /// decide its fate through its parent rather than on its own.
    bool is_continuation() const {
        return is_spurious() && error_word.has_value() &&
               error_word.value() == ERROR_SPURIOUS_CONTINUATION;
    }

    /// The `ERROR` column: "", "ERROR", or "SPURIOUS".
    const char* error_label() const;
};

}  // namespace mie

#endif  // MIE_MODELS_HPP
