// SPDX-License-Identifier: Apache-2.0

#include "mie/text.hpp"

#include <cmath>
#include <cstdio>
#include <limits>

namespace mie {
namespace text {

namespace {

/// The ASCII character for one digit, 0..15, upper-case past 9.
///
/// Computed rather than looked up in a "0123456789ABCDEF" table: `d` derives
/// from recording bytes, and an index into a table is a bound a reader (and a
/// taint analyser) has to prove from the caller's `base`. Arithmetic has no
/// bound to prove.
char digit_char(unsigned d) {
    return d < 10 ? static_cast<char>('0' + d) : static_cast<char>('A' + (d - 10));
}

/// Render `value` into `out` (most-significant digit first), padding with
/// leading zeros to at least `width` characters.
///
/// Shared by decimal_padded and hex_upper because the two differ only in base,
/// and a second hand-rolled digit loop is a second place to get the zero case
/// wrong.
std::string render_unsigned(uint64_t value, unsigned base, std::size_t width) {
    // 64 binary digits is the widest any supported base can produce, so no
    // input can overflow this buffer.
    char digits[64];
    std::size_t n = 0;
    if (value == 0) {
        digits[n++] = '0';
    }
    while (value > 0) {
        digits[n++] = digit_char(static_cast<unsigned>(value % base));
        value /= base;
    }

    std::string out;
    // Widen rather than truncate when the value needs more room than `width`.
    // A truncated timestamp or word count reads as a plausible wrong value; an
    // over-wide one reads as obviously anomalous, which is what an operator
    // staring at a corrupt recording needs.
    if (n < width) {
        out.assign(width - n, '0');
    }
    out.reserve(out.size() + n);
    while (n > 0) {
        out.push_back(digits[--n]);
    }
    return out;
}

}  // namespace

std::string to_ascii_lower(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (std::size_t i = 0; i < s.size(); ++i) {
        out.push_back(ascii_lower(s[i]));
    }
    return out;
}

bool equals_ignoring_ascii_case(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) {
        return false;
    }
    for (std::size_t i = 0; i < a.size(); ++i) {
        if (ascii_lower(a[i]) != ascii_lower(b[i])) {
            return false;
        }
    }
    return true;
}

std::string trim_ascii_blank(const std::string& s) {
    std::size_t begin = 0;
    while (begin < s.size() && is_ascii_blank(s[begin])) {
        ++begin;
    }
    std::size_t end = s.size();
    while (end > begin && is_ascii_blank(s[end - 1])) {
        --end;
    }
    return s.substr(begin, end - begin);
}

std::string trim_ascii_whitespace(const std::string& s) {
    std::size_t begin = 0;
    while (begin < s.size() && is_ascii_whitespace(s[begin])) {
        ++begin;
    }
    std::size_t end = s.size();
    while (end > begin && is_ascii_whitespace(s[end - 1])) {
        --end;
    }
    return s.substr(begin, end - begin);
}

bool is_valid_utf8(const std::string& s) {
    std::size_t i = 0;
    while (i < s.size()) {
        const auto lead = static_cast<unsigned char>(s[i]);
        std::size_t width = 0;
        uint32_t code = 0;
        if (lead < 0x80) {
            i += 1;
            continue;
        } else if ((lead & 0xE0) == 0xC0) {
            width = 2;
            code = lead & 0x1FU;
        } else if ((lead & 0xF0) == 0xE0) {
            width = 3;
            code = lead & 0x0FU;
        } else if ((lead & 0xF8) == 0xF0) {
            width = 4;
            code = lead & 0x07U;
        } else {
            return false;  // continuation byte in lead position, or 0xF8..0xFF
        }
        if (i + width > s.size()) {
            return false;  // truncated sequence
        }
        for (std::size_t k = 1; k < width; ++k) {
            const auto cont = static_cast<unsigned char>(s[i + k]);
            if ((cont & 0xC0) != 0x80) {
                return false;
            }
            code = (code << 6) | (cont & 0x3FU);
        }
        // Overlong encodings, surrogate halves and out-of-range code points are
        // each well-formed byte patterns for a decoder that only counts
        // continuation bytes, and each is rejected by the Rust and Python
        // readers this has to agree with.
        if (width == 2 && code < 0x80) {
            return false;
        }
        if (width == 3 && code < 0x800) {
            return false;
        }
        if (width == 4 && code < 0x10000) {
            return false;
        }
        if (code > 0x10FFFF) {
            return false;
        }
        if (code >= 0xD800 && code <= 0xDFFF) {
            return false;
        }
        i += width;
    }
    return true;
}

namespace {

/// Accumulate the ASCII digits of `s` from `at` into `out`, refusing a value
/// above `limit`. False on an empty digit run or a non-digit.
bool accumulate_digits(const std::string& s, std::size_t at, uint64_t limit, uint64_t& out) {
    if (at >= s.size()) {
        return false;
    }
    uint64_t value = 0;
    for (std::size_t i = at; i < s.size(); ++i) {
        if (!is_ascii_digit(s[i])) {
            return false;
        }
        const uint64_t digit = static_cast<uint64_t>(s[i] - '0');
        // value * 10 + digit <= limit, without computing anything that wraps.
        if (value > (limit - digit) / 10) {
            return false;
        }
        value = value * 10 + digit;
    }
    out = value;
    return true;
}

}  // namespace

namespace {

/// Append `cp` (at most U+10FFFF) in UTF-8 form. Surrogate code points are
/// encoded like any other three-byte value, which is what makes this WTF-8.
void append_utf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
        out += static_cast<char>(cp);
    } else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

bool is_high_surrogate(uint32_t u) { return u >= 0xD800 && u <= 0xDBFF; }
bool is_low_surrogate(uint32_t u) { return u >= 0xDC00 && u <= 0xDFFF; }

/// One WTF-8 sequence at `at`: its code point and length, or false when the
/// bytes there are not a well-formed sequence.
bool decode_wtf8_at(const std::string& s, std::size_t at, uint32_t& cp, std::size_t& len) {
    const uint32_t lead = static_cast<unsigned char>(s[at]);
    if (lead < 0x80) {
        cp = lead;
        len = 1;
        return true;
    }
    uint32_t min = 0;
    if (lead >= 0xC2 && lead <= 0xDF) {
        cp = lead & 0x1F;
        len = 2;
        min = 0x80;
    } else if (lead >= 0xE0 && lead <= 0xEF) {
        cp = lead & 0x0F;
        len = 3;
        min = 0x800;
    } else if (lead >= 0xF0 && lead <= 0xF4) {
        cp = lead & 0x07;
        len = 4;
        min = 0x10000;
    } else {
        return false;
    }
    if (at + len > s.size()) {
        return false;
    }
    for (std::size_t k = 1; k < len; ++k) {
        const uint32_t b = static_cast<unsigned char>(s[at + k]);
        if ((b & 0xC0) != 0x80) {
            return false;
        }
        cp = (cp << 6) | (b & 0x3F);
    }
    return cp >= min && cp <= 0x10FFFF;
}

}  // namespace

std::string utf16_to_wtf8(const std::u16string& units) {
    std::string out;
    out.reserve(units.size());
    for (std::size_t i = 0; i < units.size(); ++i) {
        uint32_t cp = units[i];
        if (is_high_surrogate(cp) && i + 1 < units.size() && is_low_surrogate(units[i + 1])) {
            cp = 0x10000 + ((cp - 0xD800) << 10) + (static_cast<uint32_t>(units[i + 1]) - 0xDC00);
            ++i;
        }
        append_utf8(out, cp);
    }
    return out;
}

std::u16string wtf8_to_utf16(const std::string& wtf8) {
    std::u16string out;
    out.reserve(wtf8.size());
    std::size_t at = 0;
    while (at < wtf8.size()) {
        uint32_t cp = 0;
        std::size_t len = 0;
        if (!decode_wtf8_at(wtf8, at, cp, len)) {
            out += static_cast<char16_t>(0xFFFD);
            at += 1;
            continue;
        }
        if (cp >= 0x10000) {
            cp -= 0x10000;
            out += static_cast<char16_t>(0xD800 + (cp >> 10));
            out += static_cast<char16_t>(0xDC00 + (cp & 0x3FF));
        } else {
            out += static_cast<char16_t>(cp);
        }
        at += len;
    }
    return out;
}

bool parse_int64(const std::string& s, int64_t& out) {
    const bool negative = !s.empty() && s[0] == '-';
    const std::size_t start = (!s.empty() && (s[0] == '-' || s[0] == '+')) ? 1 : 0;
    // The magnitude of INT64_MIN is one more than INT64_MAX.
    const uint64_t max_positive = static_cast<uint64_t>(std::numeric_limits<int64_t>::max());
    const uint64_t limit = negative ? max_positive + 1 : max_positive;
    uint64_t magnitude = 0;
    if (!accumulate_digits(s, start, limit, magnitude)) {
        return false;
    }
    if (!negative) {
        out = static_cast<int64_t>(magnitude);
    } else if (magnitude == max_positive + 1) {
        out = std::numeric_limits<int64_t>::min();
    } else {
        out = -static_cast<int64_t>(magnitude);
    }
    return true;
}

namespace {

/// Case-insensitive ASCII equality of `s[at, at + word.size())` with `word`
/// (lower-case), and nothing after it.
bool ends_with_word(const std::string& s, std::size_t at, const char* word) {
    std::size_t i = 0;
    for (; word[i] != '\0'; ++i) {
        if (at + i >= s.size() || ascii_lower(s[at + i]) != word[i]) {
            return false;
        }
    }
    return at + i == s.size();
}

}  // namespace

bool is_rust_float_literal(const std::string& s) {
    std::size_t at = (!s.empty() && (s[0] == '+' || s[0] == '-')) ? 1 : 0;
    if (ends_with_word(s, at, "inf") || ends_with_word(s, at, "infinity") ||
        ends_with_word(s, at, "nan")) {
        return true;
    }
    std::size_t int_digits = 0;
    while (at < s.size() && is_ascii_digit(s[at])) {
        ++at;
        ++int_digits;
    }
    std::size_t frac_digits = 0;
    if (at < s.size() && s[at] == '.') {
        ++at;
        while (at < s.size() && is_ascii_digit(s[at])) {
            ++at;
            ++frac_digits;
        }
    }
    if (int_digits == 0 && frac_digits == 0) {
        return false;  // no digits at all: "", "+", ".", "e3"
    }
    if (at < s.size() && (s[at] == 'e' || s[at] == 'E')) {
        ++at;
        if (at < s.size() && (s[at] == '+' || s[at] == '-')) {
            ++at;
        }
        const std::size_t exp_start = at;
        while (at < s.size() && is_ascii_digit(s[at])) {
            ++at;
        }
        if (at == exp_start) {
            return false;  // "1e", "1e+"
        }
    }
    return at == s.size();
}

bool parse_hex_uint64(const std::string& s, uint64_t& out) {
    if (s.size() < 3 || s[0] != '0' || (s[1] != 'x' && s[1] != 'X')) {
        return false;
    }
    uint64_t value = 0;
    for (std::size_t i = 2; i < s.size(); ++i) {
        const int digit = ascii_hex_value(s[i]);
        if (digit < 0) {
            return false;
        }
        if (value > (std::numeric_limits<uint64_t>::max() >> 4)) {
            return false;
        }
        value = (value << 4) | static_cast<uint64_t>(digit);
    }
    out = value;
    return true;
}

bool parse_uint64(const std::string& s, uint64_t& out) {
    const std::size_t start = (!s.empty() && s[0] == '+') ? 1 : 0;
    return accumulate_digits(s, start, std::numeric_limits<uint64_t>::max(), out);
}

std::string decimal(uint64_t value) { return render_unsigned(value, 10, 0); }

std::string decimal_signed(int64_t value) {
    if (value >= 0) {
        return render_unsigned(static_cast<uint64_t>(value), 10, 0);
    }
    // Negate in the unsigned domain. `-value` on INT64_MIN is undefined
    // behaviour, and UBSan is switched on in one of the CI tiers, so this is a
    // real failure rather than a theoretical one.
    const uint64_t magnitude = ~static_cast<uint64_t>(value) + 1u;
    return "-" + render_unsigned(magnitude, 10, 0);
}

std::string decimal_padded(uint64_t value, std::size_t width) {
    return render_unsigned(value, 10, width);
}

std::string hex_upper(uint64_t value, std::size_t width) {
    return render_unsigned(value, 16, width);
}

std::string fixed6(double value) {
    // No sensible CSV spelling exists for NaN or infinity, and inventing one
    // would put a token in the column that neither of the other two
    // implementations emits. An empty cell is the honest answer.
    //
    // std::isfinite rather than a self-comparison trick: it is C++11 and it
    // says what it means.
    if (!std::isfinite(value)) {
        return std::string();
    }

    // A double's magnitude tops out around 1.8e308, so "%.6f" can produce ~310
    // integer digits plus a sign, a point and six decimals. 512 is comfortably
    // clear of that, and the result is checked rather than assumed.
    char buffer[512];
    const int written = std::snprintf(buffer, sizeof(buffer), "%.6f", value);
    if (written <= 0 || static_cast<std::size_t>(written) >= sizeof(buffer)) {
        return std::string();
    }

    // Normalise the decimal separator.
    //
    // The program never calls setlocale, so in its own binary this loop finds a
    // '.' and changes nothing. It matters when the decoder is linked into a
    // host that HAS called setlocale: the C locale is only the *initial* one,
    // not a permanent property, and a comma here would corrupt every DELTA cell
    // while leaving the CSV superficially well-formed.
    //
    // "%.6f" applies no thousands grouping (that needs the ' flag), so there is
    // exactly one non-digit character to find besides a leading sign. Replacing
    // the first such character is therefore complete, not a heuristic.
    std::string out(buffer, static_cast<std::size_t>(written));
    for (std::size_t i = 0; i < out.size(); ++i) {
        const char c = out[i];
        if (c == '-' || c == '+' || is_ascii_digit(c)) {
            continue;
        }
        out[i] = '.';
        break;
    }
    return out;
}

}  // namespace text
}  // namespace mie
