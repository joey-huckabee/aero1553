// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::config"

#include "mie/config.hpp"

#include <cmath>
#include <cstdio>
#include <memory>
#include <vector>

#include "mie/log.hpp"
#include "mie/platform.hpp"
#include "mie/text.hpp"

namespace mie {

FilterConfig::FilterConfig() = default;

bool FilterConfig::is_active() const {
    return !exclude_types.empty() || !exclude_rts.empty() || !exclude_buses.empty() ||
           !exclude_subaddresses.empty() || !include_types.empty() || !include_rts.empty() ||
           !include_buses.empty() || !include_subaddresses.empty();
}

ConfigError::ConfigError(const std::string& message)
    : message_(std::make_shared<std::string>(message)) {}

// NOT `= default`: see the note on MieError's destructor in mie/error.hpp --
// cppcheck 2.13 reports internalAstError on `throw() = default`.
ConfigError::~ConfigError() throw() {}

const char* ConfigError::what() const throw() { return message_->c_str(); }

DecoderConfig::DecoderConfig()
    : log_level("WARNING"),
      irig_day_advisory(true),
      input_time_format(TIMESTAMP_AUTO),
      strict(false),
      error_mode(ERROR_MODE_INLINE),
      output_format("csv"),
      no_clobber(false),
      output_time_format(OUTPUT_TIME_DOY),
      year(),
      utc_offset_minutes(0),
      allow_partial(false),
      detect_records(decode::DEFAULT_DETECT_RECORDS),
      lookahead_records(sync::DEFAULT_LOOKAHEAD_RECORDS),
      mux_enabled(decode::DEFAULT_MUX_ENABLED),
      mux_delimiter(decode::DEFAULT_MUX_DELIMITER),
      mux_field(decode::DEFAULT_MUX_FIELD),
      collapse_duplicates(false),
      collapse_window_us(0),
      delta_scope(DELTA_SCOPE_PER_FILE),
      max_sort_group(DEFAULT_MAX_SORT_GROUP),
      max_collapse_survivors(DEFAULT_MAX_COLLAPSE_SURVIVORS) {}

ConfigOverrides::ConfigOverrides() = default;

// ---------------------------------------------------------------------------
// Schema membership
// ---------------------------------------------------------------------------

bool is_known_section(const std::string& name) {
    return name == "logging" || name == "decode" || name == "output" || name == "mux" ||
           name == "merge" || name == "filter";
}

bool is_known_key(const std::string& section, const std::string& key) {
    // Written out rather than derived from the loaders, because this list has a
    // second job: it decides what gets a WARN. A key that a loader silently
    // ignores must still appear here, or a typo in it goes unreported.
    struct Pair {
        const char* section;
        const char* key;
    };
    static const Pair known[] = {
        {"logging", "level"},
        {"logging", "irig_day_advisory"},
        {"decode", "input_time_format"},
        {"decode", "strict"},
        {"decode", "error_mode"},
        {"decode", "allow_partial"},
        {"decode", "detect_records"},
        {"decode", "lookahead_records"},
        {"decode", "standard_tick_rate_hz"},
        {"output", "format"},
        {"output", "no_clobber"},
        {"output", "max_sort_group"},
        {"output", "output_time_format"},
        {"output", "year"},
        {"output", "utc_offset"},
        {"mux", "enabled"},
        {"mux", "delimiter"},
        {"mux", "field"},
        {"merge", "collapse_duplicates"},
        {"merge", "collapse_window_us"},
        {"merge", "max_collapse_survivors"},
        {"merge", "delta_scope"},
        {"filter", "exclude_types"},
        {"filter", "exclude_rts"},
        {"filter", "exclude_buses"},
        {"filter", "exclude_subaddresses"},
    };
    for (std::size_t i = 0; i < sizeof(known) / sizeof(known[0]); ++i) {
        if (section == known[i].section && key == known[i].key) {
            return true;
        }
    }
    return false;
}

// ---------------------------------------------------------------------------
// Value coercion
// ---------------------------------------------------------------------------

namespace {

/// A value repeated in a diagnostic, by the rule every implementation shares
/// (`text::quote`, L2-CLI-022).
std::string quoted(const std::string& s) { return text::quote(s); }

/// A key's fully-qualified name, for a diagnostic that names the offender.
std::string qualified(const std::string& section, const std::string& key) {
    return section + "." + key;
}

/// Typed lookups. Absent is not an error; present-with-the-wrong-type is.
///
/// These live HERE rather than on `toml::Document` on purpose: "this key must
/// be a string" is a schema statement, and the parser has no schema. Putting
/// them on the document would be the first crack in the separation the two
/// files exist to maintain.
bool lookup(const toml::Document& doc, const std::string& section, const std::string& key,
            toml::ValueKind wanted, toml::Value& out) {
    toml::Value found;
    if (!doc.get(section, key, found)) {
        return false;
    }
    if (found.kind() != wanted) {
        static const char* const article[] = {"a string", "an integer", "a float", "a boolean",
                                              "an array"};
        throw ConfigError("[" + section + "] " + key + " must be " + article[wanted]);
    }
    out = found;
    return true;
}

bool get_string(const toml::Document& doc, const std::string& section, const std::string& key,
                std::string& out) {
    toml::Value v;
    if (!lookup(doc, section, key, toml::VALUE_STRING, v)) {
        return false;
    }
    out = v.as_string();
    return true;
}

bool get_bool(const toml::Document& doc, const std::string& section, const std::string& key,
              bool& out) {
    toml::Value v;
    if (!lookup(doc, section, key, toml::VALUE_BOOLEAN, v)) {
        return false;
    }
    out = v.as_boolean();
    return true;
}

bool get_int(const toml::Document& doc, const std::string& section, const std::string& key,
             int64_t& out) {
    toml::Value v;
    if (!lookup(doc, section, key, toml::VALUE_INTEGER, v)) {
        return false;
    }
    out = v.as_integer();
    return true;
}

/// A float key also accepts an integer, so an operator can write `1000000` or
/// `1000000.0` for a rate. Deliberately asymmetric: an integer key does NOT
/// accept a float, because `detect_records = 8.0` is a mistake worth naming.
bool get_number(const toml::Document& doc, const std::string& section, const std::string& key,
                double& out, std::string& written) {
    toml::Entry entry;
    if (!doc.get_entry(section, key, entry)) {
        return false;
    }
    written = entry.written;
    if (entry.value.is_float()) {
        out = entry.value.as_float();
        return true;
    }
    if (entry.value.is_integer()) {
        out = static_cast<double>(entry.value.as_integer());
        return true;
    }
    throw ConfigError("[" + section + "] " + key + " must be a number");
}

bool get_array(const toml::Document& doc, const std::string& section, const std::string& key,
               std::vector<toml::Value>& out) {
    toml::Value v;
    if (!lookup(doc, section, key, toml::VALUE_ARRAY, v)) {
        return false;
    }
    out = v.as_array();
    return true;
}

std::size_t require_int_range(int64_t value, const std::string& key, std::size_t lo,
                              std::size_t hi) {
    if (value < static_cast<int64_t>(lo) || value > static_cast<int64_t>(hi)) {
        throw ConfigError("Invalid " + key + ": " + text::decimal_signed(value) +
                          ". Valid range: [" + text::decimal(static_cast<uint64_t>(lo)) + ", " +
                          text::decimal(static_cast<uint64_t>(hi)) + "]");
    }
    return static_cast<std::size_t>(value);
}

/// The message repeats the value as `written` in the file. It once rendered the
/// parsed double with `fixed6`, which printed `0.000000` where Rust printed `0`
/// -- and printed `1e400`, which overflows to infinity, as an empty string.
double require_positive_finite(double value, const std::string& written, const std::string& key) {
    // Both halves matter. A non-finite rate would make every converted
    // timestamp a NaN, and a zero or negative one would invert the timeline.
#if defined(_WIN32)
    const bool finite = _finite(value) != 0;
#else
    const bool finite = std::isfinite(value);
#endif
    if (!finite || value <= 0.0) {
        throw ConfigError("Invalid " + key + ": " + written +
                          ". Must be a finite value greater than 0");
    }
    return value;
}

}  // namespace

/// Parse a UTC offset designator into minutes east of UTC (L2-CFG-012).
///
/// Accepts `Z` (case-insensitively) for zero, or a signed `+HH:MM` / `-HH:MM`.
/// The shape is checked exactly rather than leniently: `+5:00`, `+0500` and a
/// trailing-space variant are all rejected, because a zone that parsed loosely
/// here would silently shift every ISO timestamp in the file.
///
/// Digits are classified with an explicit ASCII range rather than `<cctype>`:
/// this tree is never locale-sensitive, and `scripts/assert-locale-free.sh`
/// enforces it.
bool parse_utc_offset(const std::string& text, int& out) {
    if (text::equals_ignoring_ascii_case(text, "z")) {
        out = 0;
        return true;
    }
    if (text.size() != 6 || text[3] != ':') {
        return false;
    }
    if (text[0] != '+' && text[0] != '-') {
        return false;
    }
    const std::size_t digit_positions[4] = {1, 2, 4, 5};
    for (std::size_t i = 0; i < 4; ++i) {
        const char c = text[digit_positions[i]];
        if (c < '0' || c > '9') {
            return false;
        }
    }
    const int hours = (text[1] - '0') * 10 + (text[2] - '0');
    const int minutes = (text[4] - '0') * 10 + (text[5] - '0');
    if (hours > 23 || minutes > 59) {
        return false;
    }
    out = (text[0] == '-' ? -1 : 1) * (hours * 60 + minutes);
    return true;
}

uint8_t parse_type_name(const std::string& name) {
    const std::string trimmed = text::trim_ascii_blank(name);
    struct Named {
        const char* name;
        uint8_t code;
    };
    static const Named names[] = {
        {"MODE_COMMAND", MESSAGE_TYPE_MODE_COMMAND},
        {"BC_TO_RT", MESSAGE_TYPE_BC_TO_RT},
        {"RT_TO_BC", MESSAGE_TYPE_RT_TO_BC},
        {"RT_TO_RT", MESSAGE_TYPE_RT_TO_RT},
        {"BROADCAST_BC_TO_RT", MESSAGE_TYPE_BROADCAST_BC_TO_RT},
        {"BROADCAST_RT_TO_RT", MESSAGE_TYPE_BROADCAST_RT_TO_RT},
        {"SPURIOUS_DATA", MESSAGE_TYPE_SPURIOUS_DATA},
    };
    for (std::size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        if (text::equals_ignoring_ascii_case(trimmed, names[i].name)) {
            return names[i].code;
        }
    }

    // A hex code, so a recording carrying a type this build does not name can
    // still be filtered.
    if (trimmed.size() > 2 && trimmed[0] == '0' && (trimmed[1] == 'x' || trimmed[1] == 'X')) {
        // Every digit is checked before any range verdict, so `0x1FFZ` is an
        // invalid type rather than an out-of-range one, as in Rust.
        bool all_hex = true;
        for (std::size_t i = 2; i < trimmed.size(); ++i) {
            all_hex = all_hex && text::ascii_hex_value(trimmed[i]) >= 0;
        }
        if (all_hex) {
            uint32_t value = 0;
            for (std::size_t i = 2; i < trimmed.size(); ++i) {
                value = value * 16 + static_cast<uint32_t>(text::ascii_hex_value(trimmed[i]));
                if (value > 0xFF) {
                    throw ConfigError("Type code out of range: " + quoted(name));
                }
            }
            return static_cast<uint8_t>(value);
        }
    }

    throw ConfigError("Invalid message type: " + quoted(name) +
                      ". Valid: MODE_COMMAND, BC_TO_RT, RT_TO_BC, RT_TO_RT, "
                      "BROADCAST_BC_TO_RT, BROADCAST_RT_TO_RT, SPURIOUS_DATA, or 0xNN");
}

Bus parse_bus_name(const std::string& name) {
    const std::string trimmed = text::trim_ascii_blank(name);
    if (text::equals_ignoring_ascii_case(trimmed, "A")) {
        return BUS_A;
    }
    if (text::equals_ignoring_ascii_case(trimmed, "B")) {
        return BUS_B;
    }
    throw ConfigError("Invalid bus: " + quoted(name) + ". Valid: A, B");
}

namespace {

uint8_t parse_type_value(const toml::Value& value) {
    if (value.is_string()) {
        return parse_type_name(value.as_string());
    }
    if (value.is_integer()) {
        const int64_t code = value.as_integer();
        if (code < 0 || code > 0xFF) {
            throw ConfigError("Type code out of range: " + text::decimal_signed(code));
        }
        return static_cast<uint8_t>(code);
    }
    throw ConfigError("exclude_types entries must be strings or integers");
}

Bus parse_bus_value(const toml::Value& value) {
    if (!value.is_string()) {
        throw ConfigError("exclude_buses entries must be strings");
    }
    return parse_bus_name(value.as_string());
}

/// A `[filter]` entry that must be a small non-negative integer -- an RT
/// address or a subaddress, both 0-31 on the wire.
uint8_t parse_small_int(const toml::Value& value, const char* key) {
    if (!value.is_integer()) {
        throw ConfigError(std::string(key) + " entries must be integers");
    }
    const int64_t n = value.as_integer();
    if (n < 0 || n > 31) {
        throw ConfigError(std::string(key) + " entries must be in [0, 31]; got " +
                          text::decimal_signed(n));
    }
    return static_cast<uint8_t>(n);
}

// --- Section loaders. One per [section], so no single function carries the
// --- whole schema and each can be read against its CONFIG-REFERENCE entry.

// Each loader checks its keys in the order Rust's does, because the first bad
// key is the one reported: C++ once checked `irig_day_advisory` before `level`
// and `max_sort_group` before `year`, so a file with two mistakes was reported
// differently by each implementation (L2-CLI-022).

void apply_logging(const toml::Document& doc, DecoderConfig& config) {
    std::string level;
    if (get_string(doc, "logging", "level", level)) {
        log::Level parsed = log::LEVEL_WARN;
        if (!log::level_from_name(level, parsed)) {
            throw ConfigError("Invalid logging.level: " + quoted(level) +
                              ". Valid: " + log::LEVEL_NAMES);
        }
        // Stored uppercased, matching the other implementations, so a later
        // comparison does not have to be case-insensitive too.
        std::string upper;
        for (std::size_t i = 0; i < level.size(); ++i) {
            upper += text::ascii_upper(level[i]);
        }
        config.log_level = upper;
    }

    bool advisory = false;
    if (get_bool(doc, "logging", "irig_day_advisory", advisory)) {
        config.irig_day_advisory = advisory;
    }
}

void apply_decode(const toml::Document& doc, DecoderConfig& config) {
    // L2-CFG-012: `decode.time_format` was renamed in v3.0.0. Left to the
    // generic unknown-key rule it would only WARN (L2-CFG-009) -- and a WARN is
    // the wrong answer for a rename, because the operator's explicit choice
    // would be discarded while the run reported success, silently reverting a
    // forced format to auto-detection. Tested by presence rather than by type,
    // so a wrong-typed value reports the rename too.
    if (doc.contains("decode", "time_format")) {
        throw ConfigError(
            "decode.time_format was renamed in v3.0.0. Use decode.input_time_format to "
            "choose how timestamps are PARSED (auto, irig, standard); use "
            "output.output_time_format to choose how they are WRITTEN (doy, iso, dom).");
    }
    std::string text_value;
    if (get_string(doc, "decode", "input_time_format", text_value)) {
        TimestampFormat format = TIMESTAMP_AUTO;
        if (!timestamp_format_from_name(text_value, format)) {
            throw ConfigError("Invalid input_time_format: " + quoted(text_value) +
                              ". Valid: auto, irig, standard");
        }
        config.input_time_format = format;
    }
    bool flag = false;
    if (get_bool(doc, "decode", "strict", flag)) {
        config.strict = flag;
    }
    if (get_string(doc, "decode", "error_mode", text_value)) {
        if (text::equals_ignoring_ascii_case(text_value, "separate")) {
            config.error_mode = ERROR_MODE_SEPARATE;
        } else if (text::equals_ignoring_ascii_case(text_value, "inline")) {
            config.error_mode = ERROR_MODE_INLINE;
        } else {
            throw ConfigError("Invalid error_mode: " + quoted(text_value) +
                              ". Valid: separate, inline");
        }
    }
    if (get_bool(doc, "decode", "allow_partial", flag)) {
        config.allow_partial = flag;
    }
    int64_t number = 0;
    if (get_int(doc, "decode", "detect_records", number)) {
        config.detect_records = require_int_range(number, "decode.detect_records",
                                                  DETECT_RECORDS_MIN, DETECT_RECORDS_MAX);
    }
    if (get_int(doc, "decode", "lookahead_records", number)) {
        config.lookahead_records = require_int_range(number, "decode.lookahead_records",
                                                     LOOKAHEAD_RECORDS_MIN, LOOKAHEAD_RECORDS_MAX);
    }
    double rate = 0.0;
    std::string written;
    if (get_number(doc, "decode", "standard_tick_rate_hz", rate, written)) {
        config.standard_tick_rate_hz =
            require_positive_finite(rate, written, "decode.standard_tick_rate_hz");
    }
}

void apply_output(const toml::Document& doc, DecoderConfig& config) {
    std::string format;
    if (get_string(doc, "output", "format", format)) {
        if (format != "csv") {
            throw ConfigError("Invalid output.format: " + quoted(format) + ". Valid: csv");
        }
        config.output_format = format;
    }
    bool flag = false;
    if (get_bool(doc, "output", "no_clobber", flag)) {
        config.no_clobber = flag;
    }
    // L2-CFG-012 / L2-WRT-025: which rendering the TIME_STAMP column uses.
    std::string rendering;
    if (get_string(doc, "output", "output_time_format", rendering)) {
        OutputTimeFormat selected = OUTPUT_TIME_DOY;
        if (!output_time_format_from_name(rendering, selected)) {
            throw ConfigError("Invalid output_time_format: " + quoted(rendering) +
                              ". Valid: doy, iso, dom");
        }
        config.output_time_format = selected;
    }
    // L2-WRT-026 clause 1: the year is validated here but NOT required here --
    // a config may set a year it never uses, and a calendar rendering may take
    // its year from the CLI instead. Whether one is needed is a question about
    // the resolved pair, asked once both sources have merged.
    int64_t year_value = 0;
    if (get_int(doc, "output", "year", year_value)) {
        config.year =
            static_cast<int>(require_int_range(year_value, "output.year", YEAR_MIN, YEAR_MAX));
    }
    std::string offset_text;
    if (get_string(doc, "output", "utc_offset", offset_text)) {
        int minutes = 0;
        if (!parse_utc_offset(offset_text, minutes)) {
            throw ConfigError("Invalid output.utc_offset: " + quoted(offset_text) +
                              ". Valid: Z, or +HH:MM / -HH:MM with HH in [0, 23] and "
                              "MM in [0, 59]");
        }
        config.utc_offset_minutes = minutes;
    }
    int64_t number = 0;
    if (get_int(doc, "output", "max_sort_group", number)) {
        config.max_sort_group = require_int_range(number, "output.max_sort_group",
                                                  MAX_SORT_GROUP_MIN, MAX_SORT_GROUP_MAX);
    }
}

void apply_mux(const toml::Document& doc, DecoderConfig& config) {
    bool flag = false;
    if (get_bool(doc, "mux", "enabled", flag)) {
        config.mux_enabled = flag;
    }
    std::string delimiter;
    if (get_string(doc, "mux", "delimiter", delimiter)) {
        if (delimiter.empty()) {
            throw ConfigError("Invalid mux.delimiter: must be a non-empty string");
        }
        config.mux_delimiter = delimiter;
    }
    int64_t number = 0;
    if (get_int(doc, "mux", "field", number)) {
        // Not range-checked: negative counts from the end, and an index past
        // either end simply yields an empty MUX column (L2-WRT-020) rather
        // than being an error.
        config.mux_field = number;
    }
}

void apply_merge(const toml::Document& doc, DecoderConfig& config) {
    bool flag = false;
    if (get_bool(doc, "merge", "collapse_duplicates", flag)) {
        config.collapse_duplicates = flag;
    }
    std::string scope;
    if (get_string(doc, "merge", "delta_scope", scope)) {
        DeltaScope parsed = DELTA_SCOPE_PER_FILE;
        if (!delta_scope_from_name(scope, parsed)) {
            throw ConfigError("Invalid merge.delta_scope: " + quoted(scope) +
                              ". Valid: per-file, global");
        }
        config.delta_scope = parsed;
    }
    int64_t number = 0;
    if (get_int(doc, "merge", "collapse_window_us", number)) {
        if (number < 0) {
            throw ConfigError("Invalid merge.collapse_window_us: " + text::decimal_signed(number) +
                              "; must be a non-negative integer");
        }
        config.collapse_window_us = static_cast<uint64_t>(number);
    }
    // L2-MRG-008: cap on the retained survivor set. Range-checked here so a bad
    // value fails at load time rather than silently clamping later; the message
    // text matches the Rust and Python loaders (L3-WRT-003).
    if (get_int(doc, "merge", "max_collapse_survivors", number)) {
        config.max_collapse_survivors =
            require_int_range(number, "merge.max_collapse_survivors", MAX_COLLAPSE_SURVIVORS_MIN,
                              MAX_COLLAPSE_SURVIVORS_MAX);
    }
}

void apply_filter(const toml::Document& doc, DecoderConfig& config) {
    std::vector<toml::Value> items;
    if (get_array(doc, "filter", "exclude_types", items)) {
        for (std::size_t i = 0; i < items.size(); ++i) {
            config.filters.exclude_types.push_back(parse_type_value(items[i]));
        }
    }
    if (get_array(doc, "filter", "exclude_rts", items)) {
        for (std::size_t i = 0; i < items.size(); ++i) {
            config.filters.exclude_rts.push_back(parse_small_int(items[i], "exclude_rts"));
        }
    }
    if (get_array(doc, "filter", "exclude_buses", items)) {
        for (std::size_t i = 0; i < items.size(); ++i) {
            config.filters.exclude_buses.push_back(parse_bus_value(items[i]));
        }
    }
    if (get_array(doc, "filter", "exclude_subaddresses", items)) {
        for (std::size_t i = 0; i < items.size(); ++i) {
            config.filters.exclude_subaddresses.push_back(
                parse_small_int(items[i], "exclude_subaddresses"));
        }
    }
}

/// A known section written as a root-level scalar -- `decode = true` instead of
/// a `[decode]` header -- is an error rather than an unknown key.
///
/// Without this the intended section is silently dropped, which is the same
/// failure mode as a typo but harder to spot: the operator can see the word
/// `decode` in their file.
void reject_section_used_as_scalar(const toml::Document& doc) {
    for (std::size_t i = 0; i < doc.size(); ++i) {
        const toml::Entry& entry = doc.at(i);
        if (entry.section.empty() && is_known_section(entry.key)) {
            throw ConfigError("Invalid [" + entry.key + "]: expected a table, not a value");
        }
    }
}

/// L2-CFG-009: WARN on a key the schema does not define, so a typo surfaces.
///
/// Non-fatal by design. A config written for a newer build must still load on
/// an older one, or a shared site config becomes un-upgradable.
void warn_unknown_keys(const toml::Document& doc) {
    for (std::size_t i = 0; i < doc.size(); ++i) {
        const toml::Entry& entry = doc.at(i);
        if (is_known_key(entry.section, entry.key)) {
            continue;
        }
        const std::string where =
            entry.section.empty() ? entry.key : qualified(entry.section, entry.key);
        MIE_LOG_WARN("unknown config key '" + where + "' at line " +
                     text::decimal(static_cast<uint64_t>(entry.line)) + "; ignored");
    }
}

}  // namespace

DecoderConfig parse_into_config(const std::string& text_in) {
    toml::Document doc;
    toml::ParseError error;
    if (!toml::parse(text_in, doc, error)) {
        // The one place a parser failure becomes a schema failure. Below this
        // line nothing knows about TOML; above it nothing knows about MIE.
        throw ConfigError(error.format());
    }

    reject_section_used_as_scalar(doc);

    DecoderConfig config;
    apply_logging(doc, config);
    apply_decode(doc, config);
    apply_output(doc, config);
    apply_mux(doc, config);
    apply_merge(doc, config);
    apply_filter(doc, config);
    warn_unknown_keys(doc);
    return config;
}

DecoderConfig load_config(const Optional<std::string>& path) {
    if (!path.has_value()) {
        return DecoderConfig();
    }
    const std::string& file = path.value();

    // The three ways a config path fails are distinguished, because they have
    // three different remedies: the file is not there, the path names something
    // that is not a regular file, or it exists and could not be read. Rust and
    // Python both say which; C++ collapsed all three into one message until a
    // differential check compared them.
    if (!platform::path_exists(file)) {
        throw ConfigError("Config file not found: " + file);
    }
    platform::OsError err;
    uint64_t size = 0;
    bool is_regular = false;
    if (platform::file_metadata(file, size, is_regular, err) && !is_regular) {
        // A directory, a device, a pipe. Reading it would either fail opaquely
        // or -- worse, for a character device -- block forever.
        throw ConfigError("Config path is not a regular file: " + file);
    }

    // Through the platform layer, not std::fopen. On Windows the CRT reads a
    // narrow path in the ANSI codepage, so a config at a path containing any
    // non-ASCII byte could not be opened at all -- while Rust and Python opened
    // it happily, and every Linux test passed.
    std::vector<uint8_t> raw;
    if (!platform::read_file(file, raw, err)) {
        throw ConfigError("Cannot read config file: " + file + ": " + err.message);
    }
    const std::string text_in(raw.begin(), raw.end());
    // TOML is UTF-8 (TOML 1.0). This loader read the bytes and accepted a file
    // Rust and Python could not even read, so the three disagreed on whether
    // it was a config at all.
    if (!text::is_valid_utf8(text_in)) {
        throw ConfigError(file + ": not valid UTF-8");
    }

    try {
        return parse_into_config(text_in);
    } catch (const ConfigError& error) {
        // Re-thrown with the path attached. A schema message names the key; an
        // operator with several config files needs to know which one.
        throw ConfigError(file + ": " + error.message());
    }
}

// ---------------------------------------------------------------------------
// Precedence
// ---------------------------------------------------------------------------

namespace {

/// Append `extra` onto `into`, for the filter sets that MERGE rather than
/// replace.
template <typename T>
void extend(std::vector<T>& into, const std::vector<T>& extra) {
    into.insert(into.end(), extra.begin(), extra.end());
}

}  // namespace

DecoderConfig with_overrides(const DecoderConfig& base, const ConfigOverrides& overrides) {
    DecoderConfig out = base;

    if (overrides.log_level.has_value()) {
        out.log_level = overrides.log_level.value();
    }
    if (overrides.irig_day_advisory.has_value()) {
        out.irig_day_advisory = overrides.irig_day_advisory.value();
    }
    if (overrides.input_time_format.has_value()) {
        out.input_time_format = overrides.input_time_format.value();
    }
    if (overrides.strict.has_value()) {
        out.strict = overrides.strict.value();
    }
    if (overrides.error_mode.has_value()) {
        out.error_mode = overrides.error_mode.value();
    }
    if (overrides.output_format.has_value()) {
        out.output_format = overrides.output_format.value();
    }
    if (overrides.output_time_format.has_value()) {
        out.output_time_format = overrides.output_time_format.value();
    }
    // Stored as an Optional: "no year supplied" has to stay expressible, so an
    // absent override must not overwrite a configured year (L2-WRT-026).
    if (overrides.year.has_value()) {
        out.year = overrides.year.value();
    }
    if (overrides.utc_offset_minutes.has_value()) {
        out.utc_offset_minutes = overrides.utc_offset_minutes.value();
    }
    if (overrides.no_clobber.has_value()) {
        out.no_clobber = overrides.no_clobber.value();
    }
    if (overrides.allow_partial.has_value()) {
        out.allow_partial = overrides.allow_partial.value();
    }
    if (overrides.detect_records.has_value()) {
        out.detect_records = overrides.detect_records.value();
    }
    if (overrides.lookahead_records.has_value()) {
        out.lookahead_records = overrides.lookahead_records.value();
    }
    if (overrides.standard_tick_rate_hz.has_value()) {
        out.standard_tick_rate_hz = overrides.standard_tick_rate_hz.value();
    }
    if (overrides.mux_enabled.has_value()) {
        out.mux_enabled = overrides.mux_enabled.value();
    }
    if (overrides.mux_delimiter.has_value()) {
        out.mux_delimiter = overrides.mux_delimiter.value();
    }
    if (overrides.mux_field.has_value()) {
        out.mux_field = overrides.mux_field.value();
    }
    if (overrides.collapse_duplicates.has_value()) {
        out.collapse_duplicates = overrides.collapse_duplicates.value();
    }
    if (overrides.collapse_window_us.has_value()) {
        out.collapse_window_us = overrides.collapse_window_us.value();
    }
    if (overrides.delta_scope.has_value()) {
        out.delta_scope = overrides.delta_scope.value();
    }
    if (overrides.max_sort_group.has_value()) {
        out.max_sort_group = overrides.max_sort_group.value();
    }
    if (overrides.max_collapse_survivors.has_value()) {
        out.max_collapse_survivors = overrides.max_collapse_survivors.value();
    }

    // Filters MERGE. `--exclude-rt 5` on top of a config that already excludes
    // RT 9 excludes both -- replacing would silently discard the file's rule,
    // and an operator adding one flag does not expect to lose their config.
    extend(out.filters.exclude_types, overrides.filters.exclude_types);
    extend(out.filters.exclude_rts, overrides.filters.exclude_rts);
    extend(out.filters.exclude_buses, overrides.filters.exclude_buses);
    extend(out.filters.exclude_subaddresses, overrides.filters.exclude_subaddresses);
    extend(out.filters.include_types, overrides.filters.include_types);
    extend(out.filters.include_rts, overrides.filters.include_rts);
    extend(out.filters.include_buses, overrides.filters.include_buses);
    extend(out.filters.include_subaddresses, overrides.filters.include_subaddresses);

    return out;
}

}  // namespace mie
