// SPDX-License-Identifier: Apache-2.0

#include "mie/text.hpp"

#include <cmath>
#include <cstdio>

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
