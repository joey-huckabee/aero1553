// SPDX-License-Identifier: Apache-2.0

#include "mie/toml.hpp"

#include <cstdlib>

#include "mie/text.hpp"

namespace mie {
namespace toml {

// ---------------------------------------------------------------------------
// Value
// ---------------------------------------------------------------------------

Value::Value() : kind_(VALUE_STRING), integer_(0), float_(0.0), boolean_(false) {}

Value Value::of_string(const std::string& value) {
    Value v;
    v.kind_ = VALUE_STRING;
    v.string_ = value;
    return v;
}

Value Value::of_integer(int64_t value) {
    Value v;
    v.kind_ = VALUE_INTEGER;
    v.integer_ = value;
    return v;
}

Value Value::of_float(double value) {
    Value v;
    v.kind_ = VALUE_FLOAT;
    v.float_ = value;
    return v;
}

Value Value::of_boolean(bool value) {
    Value v;
    v.kind_ = VALUE_BOOLEAN;
    v.boolean_ = value;
    return v;
}

Value Value::of_array(const std::vector<Value>& items) {
    Value v;
    v.kind_ = VALUE_ARRAY;
    v.array_ = items;
    return v;
}

const char* Value::kind_name() const {
    switch (kind_) {
        case VALUE_STRING: return "string";
        case VALUE_INTEGER: return "integer";
        case VALUE_FLOAT: return "float";
        case VALUE_BOOLEAN: return "boolean";
        case VALUE_ARRAY: return "array";
    }
    return "unknown";
}

// ---------------------------------------------------------------------------
// ParseError, Entry, Document
// ---------------------------------------------------------------------------

ParseError::ParseError() : line(0) {}

std::string ParseError::format() const {
    if (line == 0) {
        return message;
    }
    return "line " + text::decimal(static_cast<uint64_t>(line)) + ": " + message;
}

Entry::Entry() : line(0) {}

Document::Document() = default;

void Document::add(const Entry& entry) { entries_.push_back(entry); }

bool Document::get(const std::string& section, const std::string& key, Value& out) const {
    for (std::size_t i = 0; i < entries_.size(); ++i) {
        if (entries_[i].section == section && entries_[i].key == key) {
            out = entries_[i].value;
            return true;
        }
    }
    return false;
}

bool Document::contains(const std::string& section, const std::string& key) const {
    Value ignored;
    return get(section, key, ignored);
}

// ---------------------------------------------------------------------------
// Grammar helpers
// ---------------------------------------------------------------------------

bool is_plain_identifier(const std::string& name) {
    if (name.empty()) {
        return false;
    }
    for (std::size_t i = 0; i < name.size(); ++i) {
        const char c = name[i];
        // Explicit ranges, never <cctype>: those read the locale table, and
        // under tr_TR the case mapping of 'I' changes. See the header note.
        //
        // NO DASH. The alphabet is exactly Python's `_IDENT_RE` (`^\w+$` under
        // re.ASCII) and Rust's `is_ascii_alphanumeric() || '_'`. Allowing `-`
        // here would make `[with-dash]` a section this parser accepts and the
        // other two refuse -- a divergence in what a shared config file means.
        if (!text::is_ascii_alnum(c) && c != '_') {
            return false;
        }
    }
    return true;
}

namespace {

/// Consume ASCII digits from `at`, returning the index after them.
std::size_t scan_digits(const std::string& s, std::size_t at) {
    while (at < s.size() && text::is_ascii_digit(s[at])) {
        at += 1;
    }
    return at;
}

/// Consume an optional leading sign.
std::size_t scan_sign(const std::string& s, std::size_t at) {
    if (at < s.size() && (s[at] == '+' || s[at] == '-')) {
        return at + 1;
    }
    return at;
}

/// Integer part: a lone `0`, or a non-zero digit followed by more digits.
///
/// A leading zero consumes exactly ONE byte, so `01` leaves the `1`
/// unconsumed and the literal is rejected as a whole -- TOML forbids leading
/// zeros, and a native parse would silently accept `08` as 8.
bool scan_integer_part(const std::string& s, std::size_t at, std::size_t& end) {
    if (at >= s.size()) {
        return false;
    }
    if (s[at] == '0') {
        end = at + 1;
        return true;
    }
    if (!text::is_ascii_digit(s[at])) {
        return false;
    }
    end = scan_digits(s, at);
    return true;
}

/// Optional fraction. A `.` MUST be followed by at least one digit, so a bare
/// trailing dot (`1.`) fails -- another form a native parse accepts.
bool scan_fraction(const std::string& s, std::size_t at, std::size_t& end) {
    if (at >= s.size() || s[at] != '.') {
        end = at;
        return true;
    }
    const std::size_t start = at + 1;
    const std::size_t stop = scan_digits(s, start);
    if (stop == start) {
        return false;
    }
    end = stop;
    return true;
}

/// Optional exponent `[eE] [+-]? [0-9]+`. A marker with no digits fails.
bool scan_exponent(const std::string& s, std::size_t at, std::size_t& end) {
    if (at >= s.size() || (s[at] != 'e' && s[at] != 'E')) {
        end = at;
        return true;
    }
    const std::size_t start = scan_sign(s, at + 1);
    const std::size_t stop = scan_digits(s, start);
    if (stop == start) {
        return false;
    }
    end = stop;
    return true;
}

}  // namespace

bool is_number_literal(const std::string& literal) {
    // Written as the grammar's own productions -- sign, integer part, optional
    // fraction, optional exponent -- so each rule is named and independently
    // testable. The literal is valid only if they together consume ALL of it,
    // which is what rejects `1_000`, `0x10` and trailing junk.
    std::size_t at = scan_sign(literal, 0);
    if (!scan_integer_part(literal, at, at)) {
        return false;
    }
    if (!scan_fraction(literal, at, at)) {
        return false;
    }
    if (!scan_exponent(literal, at, at)) {
        return false;
    }
    return at == literal.size();
}

namespace {

std::string trim(const std::string& s) { return text::trim_ascii_blank(s); }

bool fail(ParseError& error, std::size_t line, const std::string& message) {
    error.line = line;
    error.message = message;
    return false;
}

/// A value rendered for a diagnostic, quoted the way the other implementations
/// quote it so the three read alike.
std::string quoted(const std::string& s) { return "\"" + s + "\""; }

/// Strip a trailing `# comment`, preserving a `#` inside a quoted string.
std::string strip_comment(const std::string& line) {
    bool in_quote = false;
    bool prev_backslash = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (in_quote) {
            if (c == '\\' && !prev_backslash) {
                prev_backslash = true;
                continue;
            }
            if (c == '"' && !prev_backslash) {
                in_quote = false;
            }
            prev_backslash = false;
        } else if (c == '"') {
            in_quote = true;
        } else if (c == '#') {
            return line.substr(0, i);
        }
    }
    return line;
}

/// `"..."` with the four escapes the schema needs: `\"`, `\\`, `\n`, `\t`.
///
/// Deliberately not the full TOML escape set. `\uXXXX` would mean carrying a
/// UTF-8 encoder for a schema whose only string values are enum spellings and a
/// one-character delimiter; an unknown escape is rejected rather than passed
/// through, so adding one later is a widening rather than a behaviour change.
bool parse_string(const std::string& s, std::size_t line, std::string& out, ParseError& error) {
    if (s.size() < 2 || s[0] != '"' || s[s.size() - 1] != '"') {
        return fail(error, line, "malformed string " + quoted(s));
    }
    const std::string inner = s.substr(1, s.size() - 2);
    std::string result;
    result.reserve(inner.size());
    for (std::size_t i = 0; i < inner.size(); ++i) {
        const char c = inner[i];
        if (c == '\\') {
            if (i + 1 >= inner.size()) {
                return fail(error, line, "trailing backslash");
            }
            const char next = inner[i + 1];
            i += 1;
            if (next == '"') {
                result += '"';
            } else if (next == '\\') {
                result += '\\';
            } else if (next == 'n') {
                result += '\n';
            } else if (next == 't') {
                result += '\t';
            } else {
                return fail(error, line, std::string("bad escape \\") + next);
            }
        } else if (c == '"') {
            return fail(error, line, "unescaped quote in string");
        } else {
            result += c;
        }
    }
    out = result;
    return true;
}

/// Split array items on commas, respecting quoted strings.
std::vector<std::string> split_items(const std::string& s) {
    std::vector<std::string> out;
    std::string current;
    bool in_quote = false;
    bool prev_backslash = false;
    for (std::size_t i = 0; i < s.size(); ++i) {
        const char c = s[i];
        if (in_quote) {
            current += c;
            if (c == '\\' && !prev_backslash) {
                prev_backslash = true;
                continue;
            }
            if (c == '"' && !prev_backslash) {
                in_quote = false;
            }
            prev_backslash = false;
        } else if (c == ',') {
            out.push_back(trim(current));
            current.clear();
        } else {
            if (c == '"') {
                in_quote = true;
            }
            current += c;
        }
    }
    if (!trim(current).empty()) {
        out.push_back(trim(current));
    }
    return out;
}

bool parse_scalar(const std::string& text_in, std::size_t line, Value& out, ParseError& error);

bool parse_array(const std::string& s, std::size_t line, Value& out, ParseError& error) {
    if (s.size() < 2 || s[0] != '[' || s[s.size() - 1] != ']') {
        return fail(error, line, "malformed array " + quoted(s));
    }
    const std::string inner = trim(s.substr(1, s.size() - 2));
    if (inner.empty()) {
        out = Value::of_array(std::vector<Value>());
        return true;
    }
    std::vector<Value> items;
    const std::vector<std::string> pieces = split_items(inner);
    for (std::size_t i = 0; i < pieces.size(); ++i) {
        const std::string piece = trim(pieces[i]);
        if (!piece.empty() && piece[0] == '[') {
            // The flat schema has no key whose value is an array of arrays, so
            // accepting one would store a shape nothing reads. Checked HERE
            // rather than after parsing the item, which is what lets the item
            // parser be scalar-only and keeps these two functions free of
            // mutual recursion.
            return fail(error, line, "nested arrays not supported");
        }
        Value item;
        if (!parse_scalar(piece, line, item, error)) {
            return false;
        }
        items.push_back(item);
    }
    out = Value::of_array(items);
    return true;
}

bool parse_number(const std::string& s, std::size_t line, Value& out, ParseError& error) {
    // Validated against TOML's grammar BEFORE conversion. strtoll/strtod are
    // more permissive than TOML -- they take `08`, `1.`, `0x10` and leading
    // whitespace -- so delegating the decision to them would silently diverge
    // from Python's strict tomllib.
    if (!is_number_literal(s)) {
        return fail(error, line, "invalid number literal " + quoted(s));
    }
    const bool is_float = s.find('.') != std::string::npos || s.find('e') != std::string::npos ||
                          s.find('E') != std::string::npos;
    if (is_float) {
        out = Value::of_float(std::strtod(s.c_str(), nullptr));
        return true;
    }
    // An overflow is refused, not saturated: a 20-digit literal is an error, as
    // it is in Rust and Python. (`strtoll`, which this replaced, saturated and
    // reported the overflow only through `errno`, which went unchecked.)
    int64_t value = 0;
    if (!text::parse_int64(s, value)) {
        return fail(error, line, "invalid integer " + quoted(s));
    }
    out = Value::of_integer(value);
    return true;
}

/// A value that is NOT an array: string, number, or boolean.
///
/// Split from `parse_value` so the two are not mutually recursive. An array
/// holds only scalars by rule, so the item parser never needs to reach back up
/// -- and expressing that in the call graph is stronger than checking for it
/// after the fact.
bool parse_scalar(const std::string& text_in, std::size_t line, Value& out, ParseError& error) {
    const std::string s = trim(text_in);
    if (s.empty()) {
        return fail(error, line, "empty value");
    }
    const char first = s[0];
    if (first == '"') {
        std::string parsed;
        if (!parse_string(s, line, parsed, error)) {
            return false;
        }
        out = Value::of_string(parsed);
        return true;
    }
    if (s == "true") {
        out = Value::of_boolean(true);
        return true;
    }
    if (s == "false") {
        out = Value::of_boolean(false);
        return true;
    }
    if (first == '-' || first == '+' || text::is_ascii_digit(first)) {
        return parse_number(s, line, out, error);
    }
    // Everything else, including `{ a = 1 }`: an inline table is valid TOML the
    // flat schema cannot model, so it is refused rather than dropped.
    return fail(error, line, "cannot parse value " + quoted(s));
}

/// Any value: a scalar, or a single-line array of scalars.
bool parse_value(const std::string& text_in, std::size_t line, Value& out, ParseError& error) {
    const std::string s = trim(text_in);
    if (!s.empty() && s[0] == '[') {
        return parse_array(s, line, out, error);
    }
    return parse_scalar(s, line, out, error);
}

bool parse_section_header(const std::string& stripped, std::size_t line,
                          std::vector<std::string>& seen, std::string& out, ParseError& error) {
    // `[[section]]` -- an array of tables. Rejected rather than misread as a
    // section literally named `[section]`.
    if (!stripped.empty() && stripped[0] == '[') {
        return fail(error, line, "array-of-tables headers ([[...]]) are not supported");
    }
    if (stripped.empty() || stripped[stripped.size() - 1] != ']') {
        return fail(error, line, "unterminated section header");
    }
    const std::string section = trim(stripped.substr(0, stripped.size() - 1));
    if (section.empty()) {
        return fail(error, line, "empty section name");
    }
    // A dotted header nests a table, which the flat schema does not model --
    // storing it verbatim would silently ignore every key under it.
    if (section.find('.') != std::string::npos) {
        return fail(error, line,
                    "dotted section headers ([a.b]) are not supported; use a flat "
                    "[section] header");
    }
    if (!is_plain_identifier(section)) {
        return fail(error, line,
                    "unsupported section header [" + section +
                        "]; use a flat [section] name (letters, digits, underscore)");
    }
    // The TOML spec forbids defining a table twice. Without this the second
    // block silently merged into the first.
    for (std::size_t i = 0; i < seen.size(); ++i) {
        if (seen[i] == section) {
            return fail(error, line, "section [" + section + "] declared more than once");
        }
    }
    seen.push_back(section);
    out = section;
    return true;
}

bool parse_key_value(const std::string& line_text, std::size_t line, std::string& key, Value& value,
                     ParseError& error) {
    const std::string::size_type eq = line_text.find('=');
    if (eq == std::string::npos) {
        return fail(error, line, "expected '=' in " + quoted(line_text));
    }
    key = trim(line_text.substr(0, eq));
    const std::string value_text = trim(line_text.substr(eq + 1));
    if (key.empty()) {
        return fail(error, line, "empty key");
    }
    // A dotted key would be NESTED by tomllib, honouring the value; dropping it
    // here would silently ignore a safety option such as `output.no_clobber`.
    if (key[0] != '"' && key.find('.') != std::string::npos) {
        return fail(error, line,
                    "dotted keys (a.b = ...) are not supported; use a [section] header");
    }
    // A quoted key is honoured by tomllib with the quotes stripped, but would
    // be stored here with them attached -- the same key under two names.
    if (!is_plain_identifier(key)) {
        return fail(error, line,
                    "unsupported key " + quoted(key) + "; keys must be simple identifiers");
    }
    return parse_value(value_text, line, value, error);
}

}  // namespace

bool parse(const std::string& text_in, Document& out, ParseError& error) {
    out = Document();
    error = ParseError();

    std::string section;
    std::vector<std::string> seen_sections;

    std::size_t line_number = 0;
    std::size_t at = 0;
    while (at <= text_in.size()) {
        const std::string::size_type newline = text_in.find('\n', at);
        const bool last = newline == std::string::npos;
        std::string raw = last ? text_in.substr(at) : text_in.substr(at, newline - at);
        at = last ? text_in.size() + 1 : newline + 1;
        line_number += 1;

        // A trailing CR from a CRLF file is whitespace, not content. Config
        // files get edited on Windows and read on Linux.
        if (!raw.empty() && raw[raw.size() - 1] == '\r') {
            raw.erase(raw.size() - 1);
        }

        const std::string line = trim(strip_comment(raw));
        if (line.empty()) {
            continue;
        }

        if (line[0] == '[') {
            if (!parse_section_header(line.substr(1), line_number, seen_sections, section, error)) {
                return false;
            }
            continue;
        }

        Entry entry;
        entry.section = section;
        entry.line = line_number;
        if (!parse_key_value(line, line_number, entry.key, entry.value, error)) {
            return false;
        }
        if (out.contains(entry.section, entry.key)) {
            // tomllib raises on a repeated key, per the spec. Without this the
            // first value silently won, so a repeated key decoded differently
            // on each implementation.
            const std::string where =
                entry.section.empty() ? std::string() : " in section '[" + entry.section + "]'";
            return fail(error, line_number, "duplicate key '" + entry.key + "'" + where);
        }
        out.add(entry);
    }

    return true;
}

}  // namespace toml
}  // namespace mie
