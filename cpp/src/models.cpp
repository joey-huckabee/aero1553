// SPDX-License-Identifier: Apache-2.0

#include "mie/models.hpp"

#include <cmath>
#include <cstring>

#include "mie/text.hpp"

namespace mie {

// --- Enums ----------------------------------------------------------------

const char* bus_name(Bus bus) { return bus == BUS_B ? "B" : "A"; }

const char* direction_name(Direction direction) {
    return direction == DIRECTION_TRANSMIT ? "Transmit" : "Receive";
}

const char* message_format_name(MessageFormat format) {
    switch (format) {
        case FORMAT_RECEIVE: return "Receive";
        case FORMAT_TRANSMIT: return "Transmit";
        case FORMAT_RT_TO_RT: return "RtToRt";
        case FORMAT_RECEIVE_BROADCAST: return "ReceiveBroadcast";
        case FORMAT_RT_TO_RT_BROADCAST: return "RtToRtBroadcast";
        case FORMAT_MODE_CODE_TX_DATA: return "ModeCodeTxData";
        case FORMAT_MODE_CODE_RX_DATA: return "ModeCodeRxData";
        case FORMAT_MODE_CODE_NO_DATA: return "ModeCodeNoData";
        case FORMAT_MODE_CODE_BCAST_NO_DATA: return "ModeCodeBcastNoData";
        case FORMAT_MODE_CODE_BCAST_DATA: return "ModeCodeBcastData";
        case FORMAT_SPURIOUS_DATA: return "SpuriousData";
    }
    return "";
}

const char* timestamp_format_name(TimestampFormat fmt) {
    switch (fmt) {
        case TIMESTAMP_IRIG: return "Irig";
        case TIMESTAMP_STANDARD: return "Standard";
        case TIMESTAMP_AUTO: return "Auto";
    }
    return "Auto";
}

const char* output_time_format_name(OutputTimeFormat fmt) {
    switch (fmt) {
        case OUTPUT_TIME_ISO: return "iso";
        case OUTPUT_TIME_DOM: return "dom";
        case OUTPUT_TIME_DOY: return "doy";
    }
    return "doy";
}

bool output_time_format_from_name(const std::string& name, OutputTimeFormat& out) {
    if (text::equals_ignoring_ascii_case(name, "doy")) {
        out = OUTPUT_TIME_DOY;
        return true;
    }
    if (text::equals_ignoring_ascii_case(name, "iso")) {
        out = OUTPUT_TIME_ISO;
        return true;
    }
    if (text::equals_ignoring_ascii_case(name, "dom")) {
        out = OUTPUT_TIME_DOM;
        return true;
    }
    return false;
}

bool output_time_format_needs_calendar(OutputTimeFormat fmt) {
    return fmt == OUTPUT_TIME_ISO || fmt == OUTPUT_TIME_DOM;
}

TimeRender::TimeRender() : format(OUTPUT_TIME_DOY), year(), utc_offset_minutes(0) {}

bool is_leap_year(int year) { return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0); }

namespace {

/// Days in each month of a common year, January first.
const int kCommonYearMonthLengths[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};

}  // namespace

bool day_of_year_to_month_day(int year, int day_of_year, int& month, int& day_of_month) {
    const bool leap = is_leap_year(year);
    const int year_length = leap ? 366 : 365;
    if (day_of_year <= 0 || day_of_year > year_length) {
        return false;
    }

    int remaining = day_of_year;
    for (int index = 0; index < 12; ++index) {
        // February is the only month whose length depends on the year.
        const int length = kCommonYearMonthLengths[index] + ((index == 1 && leap) ? 1 : 0);
        if (remaining <= length) {
            month = index + 1;
            day_of_month = remaining;
            return true;
        }
        remaining -= length;
    }
    // Unreachable: the month lengths sum to year_length, which bounds
    // day_of_year above.
    return false;
}

std::string format_utc_offset(int minutes) {
    if (minutes == 0) {
        return "Z";
    }
    const bool negative = minutes < 0;
    const int magnitude = negative ? -minutes : minutes;
    std::string out;
    out.reserve(6);
    out += negative ? '-' : '+';
    out += text::decimal_padded(static_cast<uint32_t>(magnitude / 60), 2);
    out += ':';
    out += text::decimal_padded(static_cast<uint32_t>(magnitude % 60), 2);
    return out;
}

bool is_valid_message_type(uint8_t code) {
    switch (code) {
        case MESSAGE_TYPE_MODE_COMMAND:
        case MESSAGE_TYPE_BC_TO_RT:
        case MESSAGE_TYPE_RT_TO_BC:
        case MESSAGE_TYPE_RT_TO_RT:
        case MESSAGE_TYPE_BROADCAST_BC_TO_RT:
        case MESSAGE_TYPE_BROADCAST_RT_TO_RT:
        case MESSAGE_TYPE_SPURIOUS_DATA: return true;
        default: return false;
    }
}

const char* message_type_name(uint8_t code) {
    switch (code) {
        case MESSAGE_TYPE_MODE_COMMAND: return "MODE_COMMAND";
        case MESSAGE_TYPE_BC_TO_RT: return "BC_TO_RT";
        case MESSAGE_TYPE_RT_TO_BC: return "RT_TO_BC";
        case MESSAGE_TYPE_RT_TO_RT: return "RT_TO_RT";
        case MESSAGE_TYPE_BROADCAST_BC_TO_RT: return "BROADCAST_BC_TO_RT";
        case MESSAGE_TYPE_BROADCAST_RT_TO_RT: return "BROADCAST_RT_TO_RT";
        case MESSAGE_TYPE_SPURIOUS_DATA: return "SPURIOUS_DATA";
        default: return "";
    }
}

bool message_type_from_name(const std::string& name, uint8_t& out_code) {
    // Linear scan over seven entries, driven by the same table that renders the
    // names. A second hard-coded list here is a second place for a name to be
    // spelled differently from what the CLI help advertises.
    static const uint8_t kCodes[] = {
        MESSAGE_TYPE_MODE_COMMAND, MESSAGE_TYPE_BC_TO_RT,           MESSAGE_TYPE_RT_TO_BC,
        MESSAGE_TYPE_RT_TO_RT,     MESSAGE_TYPE_BROADCAST_BC_TO_RT, MESSAGE_TYPE_BROADCAST_RT_TO_RT,
        MESSAGE_TYPE_SPURIOUS_DATA};
    for (std::size_t i = 0; i < sizeof(kCodes) / sizeof(kCodes[0]); ++i) {
        if (text::equals_ignoring_ascii_case(name, message_type_name(kCodes[i]))) {
            out_code = kCodes[i];
            return true;
        }
    }
    return false;
}

bool timestamp_format_from_name(const std::string& name, TimestampFormat& out) {
    if (text::equals_ignoring_ascii_case(name, "auto")) {
        out = TIMESTAMP_AUTO;
        return true;
    }
    if (text::equals_ignoring_ascii_case(name, "irig")) {
        out = TIMESTAMP_IRIG;
        return true;
    }
    if (text::equals_ignoring_ascii_case(name, "standard")) {
        out = TIMESTAMP_STANDARD;
        return true;
    }
    return false;
}

uint16_t timestamp_word_count(TimestampFormat fmt) {
    switch (fmt) {
        case TIMESTAMP_IRIG: return 3;
        case TIMESTAMP_STANDARD: return 2;
        case TIMESTAMP_AUTO:
        default: return 0;
    }
}

bool delta_scope_from_name(const std::string& name, DeltaScope& out) {
    if (text::equals_ignoring_ascii_case(name, "per-file")) {
        out = DELTA_SCOPE_PER_FILE;
        return true;
    }
    if (text::equals_ignoring_ascii_case(name, "global")) {
        out = DELTA_SCOPE_GLOBAL;
        return true;
    }
    return false;
}

const char* delta_scope_name(DeltaScope scope) {
    return scope == DELTA_SCOPE_GLOBAL ? "global" : "per-file";
}

// --- Error codes ----------------------------------------------------------

bool is_known_ddc_error_code(uint16_t code) {
    return code == ERROR_MANCHESTER_PARITY || code == ERROR_NO_RESPONSE ||
           code == ERROR_INVERTED_SYNC || code == ERROR_TOO_MANY_WORDS || code == ERROR_UNKNOWN_DDC;
}

bool is_known_custom_error_code(uint16_t code) {
    return code == ERROR_SPURIOUS_CONTINUATION || code == ERROR_SPURIOUS_STANDALONE;
}

bool is_known_error_code(uint16_t code) {
    return is_known_ddc_error_code(code) || is_known_custom_error_code(code);
}

const char* ddc_error_description(uint16_t code) {
    switch (code) {
        case ERROR_MANCHESTER_PARITY: return "Manchester/Parity Error or Bit Count Error";
        case ERROR_NO_RESPONSE: return "No Status Response or Too Few Data Words";
        case ERROR_INVERTED_SYNC: return "Inverted Sync on Data Word";
        case ERROR_TOO_MANY_WORDS: return "Too Many Data Words";
        case ERROR_UNKNOWN_DDC: return "Unknown DDC Error";
        default: return "";
    }
}

const char* ddc_error_description_or_unknown(uint16_t code) {
    const char* desc = ddc_error_description(code);
    return desc[0] == '\0' ? "unknown DDC error code" : desc;
}

// --- IrigTimestamp --------------------------------------------------------

IrigTimestamp::IrigTimestamp()
    : day(0), hour(0), minute(0), second(0), microsecond(0), freerun(false) {}

IrigTimestamp::IrigTimestamp(uint16_t day_in, uint8_t hour_in, uint8_t minute_in, uint8_t second_in,
                             uint32_t microsecond_in, bool freerun_in)
    : day(day_in),
      hour(hour_in),
      minute(minute_in),
      second(second_in),
      microsecond(microsecond_in),
      freerun(freerun_in) {}

uint64_t IrigTimestamp::to_total_microseconds() const {
    const uint64_t d = day;
    const uint64_t h = hour;
    const uint64_t m = minute;
    const uint64_t s = second;
    return (d * 86400u + h * 3600u + m * 60u + s) * 1000000u + microsecond;
}

std::string IrigTimestamp::format() const {
    std::string out;
    out.reserve(24);
    out += text::decimal(day);
    out += ':';
    out += text::decimal_padded(hour, 2);
    out += ':';
    out += text::decimal_padded(minute, 2);
    out += ':';
    out += text::decimal_padded(second, 2);
    out += '.';
    out += text::decimal_padded(microsecond % 1000000u, 6);
    return out;
}

CalendarError IrigTimestamp::format_with(const TimeRender& render, std::string& out) const {
    if (render.format == OUTPUT_TIME_DOY) {
        out = format();
        return CALENDAR_OK;
    }

    // L2-WRT-026 clause 2: a freerun record's fields are relative, so resolving
    // them against a year would produce a date that looks entirely ordinary and
    // means nothing.
    if (freerun) {
        return CALENDAR_FREERUN;
    }
    if (!render.year.has_value()) {
        return CALENDAR_MISSING_YEAR;
    }

    const int year = render.year.value();
    int month = 0;
    int day_of_month = 0;
    if (!day_of_year_to_month_day(year, static_cast<int>(day), month, day_of_month)) {
        return CALENDAR_NO_SUCH_DAY;
    }

    std::string time_part;
    time_part.reserve(16);
    time_part += text::decimal_padded(hour, 2);
    time_part += ':';
    time_part += text::decimal_padded(minute, 2);
    time_part += ':';
    time_part += text::decimal_padded(second, 2);
    time_part += '.';
    time_part += text::decimal_padded(microsecond % 1000000u, 6);

    std::string rendered;
    rendered.reserve(32);
    if (render.format == OUTPUT_TIME_ISO) {
        rendered += text::decimal_padded(static_cast<uint32_t>(year), 4);
        rendered += '-';
        rendered += text::decimal_padded(static_cast<uint32_t>(month), 2);
        rendered += '-';
        rendered += text::decimal_padded(static_cast<uint32_t>(day_of_month), 2);
        rendered += 'T';
        rendered += time_part;
        rendered += format_utc_offset(render.utc_offset_minutes);
    } else {
        rendered += text::decimal_padded(static_cast<uint32_t>(day_of_month), 2);
        rendered += ':';
        rendered += time_part;
    }
    out = rendered;
    return CALENDAR_OK;
}

bool IrigTimestamp::operator==(const IrigTimestamp& other) const {
    return day == other.day && hour == other.hour && minute == other.minute &&
           second == other.second && microsecond == other.microsecond && freerun == other.freerun;
}

// --- StandardTimestamp ----------------------------------------------------

StandardTimestamp::StandardTimestamp() : raw_value(0), upper_word(0), lower_word(0) {}

StandardTimestamp::StandardTimestamp(uint32_t raw_value_in, uint16_t upper_word_in,
                                     uint16_t lower_word_in)
    : raw_value(raw_value_in), upper_word(upper_word_in), lower_word(lower_word_in) {}

bool StandardTimestamp::to_microseconds(double tick_rate_hz, uint64_t& out) const {
    // Decline rather than produce a number. An uncalibrated rate would yield a
    // DELTA that looks like real timing and is not, which is worse than an
    // empty column.
    if (!std::isfinite(tick_rate_hz) || tick_rate_hz <= 0.0) {
        return false;
    }
    const double micros = static_cast<double>(raw_value) * 1000000.0 / tick_rate_hz;
    // Half-away-from-zero (L2-DEC-017), as Rust's f64::round. Not
    // floor(micros + 0.5): the addition rounds before floor sees it, so that
    // form returns 1 for 0.49999999999999994 and, where doubles are spaced 1
    // apart (2^52 to 2^53, reached at sub-1 Hz rates), adds 1 to every odd
    // count. std::round rounds the exact value.
    const double rounded = std::round(micros);

    // Decline a result that cannot be a microsecond count. This is reachable
    // from ordinary operator input: `--standard-tick-rate-hz 1e-300` is finite
    // and positive, passes the guard above, and drives `micros` far past what a
    // uint64_t holds. Converting an out-of-range double with static_cast is
    // UNDEFINED BEHAVIOUR in C++ -- not a saturating clamp -- so the check has
    // to happen before the conversion, not be inferred from it.
    //
    // 2^64 exactly, which a double represents without rounding. Comparing
    // against UINT64_MAX converted to double would be wrong: that value is not
    // representable and rounds UP, admitting a double the cast cannot take.
    const double kTwoPow64 = 18446744073709551616.0;
    if (!(rounded >= 0.0) || rounded >= kTwoPow64) {
        // Written `!(rounded >= 0.0)` so a NaN -- which compares false against
        // everything -- is rejected here rather than reaching the cast.
        return false;
    }
    out = static_cast<uint64_t>(rounded);
    return true;
}

std::string StandardTimestamp::format() const { return "0x" + text::hex_upper(raw_value, 8); }

CalendarError StandardTimestamp::format_with(const TimeRender& render, std::string& out) const {
    if (output_time_format_needs_calendar(render.format)) {
        return CALENDAR_NOT_LOCKED;
    }
    out = format();
    return CALENDAR_OK;
}

bool StandardTimestamp::operator==(const StandardTimestamp& other) const {
    return raw_value == other.raw_value && upper_word == other.upper_word &&
           lower_word == other.lower_word;
}

// --- Timestamp ------------------------------------------------------------

Timestamp::Timestamp() : format_kind(TIMESTAMP_AUTO) {}

Timestamp Timestamp::from_irig(const IrigTimestamp& value) {
    Timestamp t;
    t.format_kind = TIMESTAMP_IRIG;
    t.irig = value;
    return t;
}

Timestamp Timestamp::from_standard(const StandardTimestamp& value) {
    Timestamp t;
    t.format_kind = TIMESTAMP_STANDARD;
    t.standard = value;
    return t;
}

bool Timestamp::to_microseconds(const Optional<double>& tick_rate_hz, uint64_t& out) const {
    if (format_kind == TIMESTAMP_IRIG) {
        out = irig.to_total_microseconds();
        return true;
    }
    if (format_kind == TIMESTAMP_STANDARD) {
        if (!tick_rate_hz.has_value()) {
            return false;
        }
        return standard.to_microseconds(tick_rate_hz.value(), out);
    }
    return false;
}

std::string Timestamp::format() const {
    if (format_kind == TIMESTAMP_IRIG) {
        return irig.format();
    }
    if (format_kind == TIMESTAMP_STANDARD) {
        return standard.format();
    }
    return std::string();
}

CalendarError Timestamp::format_with(const TimeRender& render, std::string& out) const {
    if (format_kind == TIMESTAMP_IRIG) {
        return irig.format_with(render, out);
    }
    if (format_kind == TIMESTAMP_STANDARD) {
        return standard.format_with(render, out);
    }
    // TIMESTAMP_AUTO is not a layout, so there is nothing to render. `format()`
    // returns an empty string here for the same reason.
    out = std::string();
    return CALENDAR_OK;
}

bool Timestamp::operator==(const Timestamp& other) const {
    if (format_kind != other.format_kind) {
        return false;
    }
    if (format_kind == TIMESTAMP_IRIG) {
        return irig == other.irig;
    }
    if (format_kind == TIMESTAMP_STANDARD) {
        return standard == other.standard;
    }
    return true;
}

// --- TypeWord / CommandWord -----------------------------------------------

TypeWord::TypeWord() : message_type(0), bus(BUS_A), word_count(0), error(false), raw(0) {}

TypeWord::TypeWord(uint8_t message_type_in, Bus bus_in, uint16_t word_count_in, bool error_in,
                   uint16_t raw_in)
    : message_type(message_type_in),
      bus(bus_in),
      word_count(word_count_in),
      error(error_in),
      raw(raw_in) {}

bool TypeWord::operator==(const TypeWord& other) const {
    return message_type == other.message_type && bus == other.bus &&
           word_count == other.word_count && error == other.error && raw == other.raw;
}

CommandWord::CommandWord()
    : rt(0), direction(DIRECTION_RECEIVE), subaddress(0), data_word_count(0), raw(0) {}

CommandWord::CommandWord(uint8_t rt_in, Direction direction_in, uint8_t subaddress_in,
                         uint8_t data_word_count_in, uint16_t raw_in)
    : rt(rt_in),
      direction(direction_in),
      subaddress(subaddress_in),
      data_word_count(data_word_count_in),
      raw(raw_in) {}

bool CommandWord::operator==(const CommandWord& other) const {
    return rt == other.rt && direction == other.direction && subaddress == other.subaddress &&
           data_word_count == other.data_word_count && raw == other.raw;
}

// --- DataWords ------------------------------------------------------------

DataWords::DataWords() : len_(0) {
    // Zeroed on construction so that a comparison or a hex dump of an
    // over-declared buffer cannot read whatever was previously on the stack.
    std::memset(buf_, 0, sizeof(buf_));
}

DataWords DataWords::from_words(const uint16_t* words, std::size_t count) {
    DataWords out;
    const std::size_t n = count < MAX_DATA_WORDS ? count : MAX_DATA_WORDS;
    for (std::size_t i = 0; i < n; ++i) {
        out.buf_[i] = words[i];
    }
    out.len_ = static_cast<uint8_t>(n);
    return out;
}

bool DataWords::try_push(uint16_t word) {
    if (len_ >= MAX_DATA_WORDS) {
        return false;
    }
    buf_[len_] = word;
    ++len_;
    return true;
}

bool DataWords::operator==(const DataWords& other) const {
    if (len_ != other.len_) {
        return false;
    }
    // Compares only the live prefix. The tail is zeroed, so a memcmp of the
    // whole buffer would agree today -- but it would start disagreeing the
    // moment a caller pushes and then clears, which is exactly the kind of
    // latent difference that shows up as one failing conformance case months
    // later.
    for (std::size_t i = 0; i < len_; ++i) {
        if (buf_[i] != other.buf_[i]) {
            return false;
        }
    }
    return true;
}

// --- MieMessage -----------------------------------------------------------

MieMessage::MieMessage() : message_format(FORMAT_SPURIOUS_DATA), file_offset(0) {}

Optional<uint8_t> MieMessage::rt() const {
    if (!command_word.has_value()) {
        return none();
    }
    return command_word.value().rt;
}

Optional<uint8_t> MieMessage::subaddress() const {
    if (!command_word.has_value()) {
        return none();
    }
    return command_word.value().subaddress;
}

std::string MieMessage::msg_label() const {
    if (!command_word.has_value()) {
        return std::string();
    }
    const CommandWord& cw = command_word.value();
    std::string out = text::decimal(cw.subaddress);
    out += (cw.direction == DIRECTION_TRANSMIT) ? 'T' : 'R';
    return out;
}

std::string MieMessage::delta_key() const {
    if (!command_word.has_value()) {
        return std::string();
    }
    const CommandWord& cw = command_word.value();
    std::string out = text::decimal(cw.rt);
    out += ':';
    out += text::decimal(cw.subaddress);
    out += (cw.direction == DIRECTION_TRANSMIT) ? 'T' : 'R';
    return out;
}

const char* MieMessage::error_label() const {
    // The message type decides first: a SPURIOUS_DATA record is SPURIOUS even
    // with Type Word bit 14 set. It is decoded, coded (0x2000/0x2001) and timed
    // as spurious, and an ERROR row promises a DDC code read from an Error
    // Word, which it does not carry. The raw bit stays in type_word.error.
    if (is_spurious()) {
        return "SPURIOUS";
    }
    if (type_word.error) {
        return "ERROR";
    }
    return "";
}

}  // namespace mie
