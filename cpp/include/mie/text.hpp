// SPDX-License-Identifier: Apache-2.0
//
// Locale-free text formatting and ASCII classification.
//
// Every number this program prints and every character it classifies goes
// through here, so that the "never locale-sensitive" rule (L3-CPP-007) has one
// place to be true rather than N places to be checked.
//
// WHY THIS EXISTS AT ALL. The obvious implementations are all locale-sensitive:
//
//   * std::to_string routes through vsnprintf, so its decimal separator is
//     whatever LC_NUMERIC says.
//   * printf("%.6f") likewise -- a de_DE host emits "1,234500" where the Rust
//     and Python implementations emit "1.234500", and every byte-exact CSV
//     oracle fails on that host alone.
//   * std::isdigit / std::toupper read the locale's character table. Under
//     tr_TR the uppercase of 'i' is a dotted capital I, so a case-insensitive
//     comparison of a config key or a CLI flag silently stops matching.
//   * std::ostringstream carries a locale too, and a default-constructed one
//     picks up the global locale rather than the classic one.
//
// The program never calls setlocale, and `scripts/assert-locale-free.sh` fails
// the build if it starts to. But that guarantee only covers this program: as a
// library, the decoder can be linked into a host that has already called
// setlocale before main, and then the initial "C" locale no longer holds. So
// fixed6() below does not merely assume the C locale, it normalises the result.
// The cheap defence is worth having because the expensive failure -- a wrong
// decimal separator in a CSV that still parses -- is silent.

#ifndef MIE_TEXT_HPP
#define MIE_TEXT_HPP

#include <cstddef>
#include <cstdint>
#include <string>

namespace mie {
namespace text {

// --- ASCII classification -------------------------------------------------
//
// Explicit ranges, never <cctype>. Declared inline because they sit in the
// per-character path of the TOML and CLI parsers.

inline bool is_ascii_digit(char c) { return c >= '0' && c <= '9'; }
inline bool is_ascii_upper(char c) { return c >= 'A' && c <= 'Z'; }
inline bool is_ascii_lower(char c) { return c >= 'a' && c <= 'z'; }
inline bool is_ascii_alpha(char c) { return is_ascii_upper(c) || is_ascii_lower(c); }
inline bool is_ascii_alnum(char c) { return is_ascii_alpha(c) || is_ascii_digit(c); }

/// Space and tab only -- NOT newline. Line-oriented parsers decide for
/// themselves what ends a line, and a "whitespace" predicate that quietly
/// includes '\n' turns a missing terminator into a silently joined line.
inline bool is_ascii_blank(char c) { return c == ' ' || c == '\t'; }

/// The six ASCII whitespace bytes: space, tab, newline, vertical tab, form
/// feed and carriage return -- exactly what Rust's `str::trim_ascii` strips.
inline bool is_ascii_whitespace(char c) { return c == ' ' || (c >= '\t' && c <= '\r'); }

inline char ascii_lower(char c) { return is_ascii_upper(c) ? static_cast<char>(c - 'A' + 'a') : c; }
inline char ascii_upper(char c) { return is_ascii_lower(c) ? static_cast<char>(c - 'a' + 'A') : c; }

/// Hex digit value, or -1 when `c` is not one.
inline int ascii_hex_value(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

std::string to_ascii_lower(const std::string& s);
bool equals_ignoring_ascii_case(const std::string& a, const std::string& b);

/// Remove leading and trailing spaces and tabs -- nothing else.
///
/// The one trim for text taken from a file name: a --manifest line
/// (L2-MRG-001 rule 4) and the MUX field (L2-WRT-020) both call it, as they
/// call `text::trim_ascii_blank` in rust/src/text.rs. A no-break space or other
/// Unicode whitespace at either end is part of the name and is kept.
std::string trim_ascii_blank(const std::string& s);

/// Trim ASCII whitespace (is_ascii_whitespace) from both ends: the rule for a
/// command-line value, matching Rust's `str::trim_ascii`. Unicode spaces such
/// as U+00A0 are NOT whitespace here, in either implementation.
std::string trim_ascii_whitespace(const std::string& s);

/// Whether `s` is well-formed UTF-8.
///
/// Exists because a `std::string` is a byte sequence and the other two
/// implementations' strings are not: `fs::read_to_string` and
/// `Path.read_text(encoding="utf-8")` both REJECT ill-formed input, so a
/// manifest of arbitrary bytes was accepted here and refused there. The merge
/// fuzz harness found it -- 512 generated manifests, 498 rejected by Rust and
/// Python and 0 by this implementation.
///
/// Rejects the things a naive length-driven decoder accepts: overlong
/// encodings, surrogate halves (U+D800..U+DFFF), and anything above U+10FFFF.
/// A path that is not valid UTF-8 cannot round-trip to Windows, so accepting
/// one here would only defer the failure.
bool is_valid_utf8(const std::string& s);

// --- UTF-16 <-> WTF-8 -----------------------------------------------------
//
// WTF-8 is UTF-8 extended to carry an UNPAIRED surrogate, encoded exactly as
// UTF-8 would encode its code point (three bytes, 0xED 0xA0..0xBF ...). A
// Windows file name is any sequence of UTF-16 units, paired or not; WTF-8 is
// how such a name travels through this program's narrow strings and comes back
// out unchanged. It is also what Rust's OsString holds on Windows, so the two
// implementations agree on which file a name refers to (L2-CLI-021).

/// Encode UTF-16 units as WTF-8: a valid surrogate pair as its 4-byte UTF-8
/// code point, an unpaired surrogate as 3 bytes, everything else as UTF-8.
std::string utf16_to_wtf8(const std::u16string& units);

/// Decode WTF-8 back to UTF-16 units, the exact inverse of utf16_to_wtf8. A
/// byte sequence that is not well-formed WTF-8 (a stray continuation byte, an
/// overlong form, a truncated sequence, a value above U+10FFFF) decodes to
/// U+FFFD, one per offending lead byte -- as the Win32 conversion this replaced
/// did with invalid input.
std::u16string wtf8_to_utf16(const std::string& wtf8);

// --- Integer parsing ------------------------------------------------------

/// Parse exactly `[+-]?[0-9]+` -- ASCII digits only, no whitespace, no prefix,
/// no digit separators -- into `out`. False on anything else, AND on a value
/// outside int64_t: an overflow is refused, never saturated.
///
/// Written out rather than delegated to `strtoll`, which saturates on overflow
/// (reporting it only through `errno`, which both callers ignored), skips
/// leading whitespace, and is locale-aware about what counts as a space. The
/// grammar and the range are those of Rust's `i64::from_str`, which is what
/// the other two implementations parse with.
bool parse_int64(const std::string& s, int64_t& out);

/// As parse_int64 for `[+]?[0-9]+` into uint64_t: a `-` sign is refused, as
/// Rust's `u64::from_str` refuses it, and so is a value above 2^64 - 1.
bool parse_uint64(const std::string& s, uint64_t& out);

/// `0x` or `0X` followed by one or more hex digits -- no sign anywhere --
/// into uint64_t, refusing a value above 2^64 - 1. The hexadecimal form the
/// dump offsets and the RT/subaddress filters accept (L2-CLI-020).
bool parse_hex_uint64(const std::string& s, uint64_t& out);

/// True when `s` is a float literal in the grammar of Rust's `f64::from_str`:
///
///     Float  ::= Sign? ( "inf" | "infinity" | "nan" | Number )   -- letters case-insensitive
///     Number ::= ( Digit+ | Digit+ "." Digit* | Digit* "." Digit+ ) Exp?
///     Exp    ::= ("e" | "E") Sign? Digit+
///
/// The gate in front of `strtod`, which ALSO accepts hexadecimal floats
/// (`0x10`, `0x1p4`), `nan(...)` payloads and leading whitespace -- forms Rust
/// refuses. A literal that passes converts through `strtod` to the value Rust
/// computes: both round correctly, and the program never calls `setlocale`, so
/// the decimal separator is `.`.
bool is_rust_float_literal(const std::string& s);

// --- Integer formatting ---------------------------------------------------

/// Unsigned decimal, no padding.
std::string decimal(uint64_t value);

/// Signed decimal, no padding.
std::string decimal_signed(int64_t value);

/// Unsigned decimal, zero-padded to at least `width` digits. A value too large
/// for `width` is NOT truncated -- it widens. Truncating a timestamp field to
/// make it fit would produce a plausible wrong time rather than an obvious one.
std::string decimal_padded(uint64_t value, std::size_t width);

/// Uppercase hexadecimal, zero-padded to at least `width` digits, with no `0x`
/// prefix. The CSV columns (`STAT`, `CMD`, `WD01`..`WD32`) are bare 4-digit
/// uppercase hex, matching DDC vendor output.
std::string hex_upper(uint64_t value, std::size_t width);

// --- Floating-point formatting --------------------------------------------

/// Format as exactly six digits after the decimal point, with `.` as the
/// separator regardless of the host's locale.
///
/// This is the `DELTA` column. Rust writes it with `{:.6}`, Python with
/// `f"{d:.6f}"`, and both round the exact binary value half-to-even -- which is
/// also what a conforming `printf("%.6f")` does, so the three agree bit for bit
/// as long as the separator is a dot.
///
/// The separator is normalised rather than assumed: see the header comment.
/// Non-finite input yields an empty string, because there is no sensible CSV
/// spelling of NaN or infinity here and an empty cell is the honest one.
std::string fixed6(double value);

}  // namespace text
}  // namespace mie

#endif  // MIE_TEXT_HPP
