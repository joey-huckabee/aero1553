// SPDX-License-Identifier: Apache-2.0
//
// Tests for the locale-free formatting primitives (L3-CPP-007).
//
// The fixed6 cases carry the most weight: DELTA is the one column whose
// rendering can silently differ from the Rust and Python implementations on a
// host configured for a comma decimal separator, and a wrong separator produces
// a CSV that still parses.

#include "mie/text.hpp"

#include <catch2/catch.hpp>

#include <clocale>
#include <limits>
#include <string>

namespace {

namespace txt = mie::text;

}  // namespace

TEST_CASE("ASCII classification does not consult the locale", "[text][L3-CPP-007]") {
    CHECK(txt::is_ascii_digit('0'));
    CHECK(txt::is_ascii_digit('9'));
    CHECK_FALSE(txt::is_ascii_digit('/'));
    CHECK_FALSE(txt::is_ascii_digit(':'));

    CHECK(txt::is_ascii_alpha('a'));
    CHECK(txt::is_ascii_alpha('Z'));
    CHECK_FALSE(txt::is_ascii_alpha('_'));

    SECTION("blank is space and tab only, never a newline") {
        // A "whitespace" predicate that quietly included '\n' would let a
        // line-oriented parser join two config lines into one.
        CHECK(txt::is_ascii_blank(' '));
        CHECK(txt::is_ascii_blank('\t'));
        CHECK_FALSE(txt::is_ascii_blank('\n'));
        CHECK_FALSE(txt::is_ascii_blank('\r'));
    }

    SECTION("high bytes are not letters") {
        // Under a single-byte locale these can classify as alphabetic, which is
        // how a locale-aware parser starts accepting keys it should reject.
        CHECK_FALSE(txt::is_ascii_alpha(static_cast<char>(0xC3)));
        CHECK_FALSE(txt::is_ascii_digit(static_cast<char>(0xB1)));
    }
}

TEST_CASE("case folding is ASCII-only", "[text][L3-CPP-007]") {
    CHECK(txt::ascii_lower('I') == 'i');
    CHECK(txt::ascii_upper('i') == 'I');
    CHECK(txt::to_ascii_lower("MODE_COMMAND") == "mode_command");
    CHECK(txt::equals_ignoring_ascii_case("--VERSION", "--version"));
    CHECK(txt::equals_ignoring_ascii_case("Irig", "IRIG"));
    CHECK_FALSE(txt::equals_ignoring_ascii_case("irig", "irigg"));
    CHECK_FALSE(txt::equals_ignoring_ascii_case("", "x"));
    CHECK(txt::equals_ignoring_ascii_case("", ""));
}

TEST_CASE("hex digit values cover both cases and reject non-digits", "[text]") {
    CHECK(txt::ascii_hex_value('0') == 0);
    CHECK(txt::ascii_hex_value('9') == 9);
    CHECK(txt::ascii_hex_value('a') == 10);
    CHECK(txt::ascii_hex_value('F') == 15);
    CHECK(txt::ascii_hex_value('g') == -1);
    CHECK(txt::ascii_hex_value(' ') == -1);
}

TEST_CASE("trim removes blanks from both ends only", "[text]") {
    CHECK(txt::trim_ascii_blank("  key = value  ") == "key = value");
    CHECK(txt::trim_ascii_blank("\t\tx") == "x");
    CHECK(txt::trim_ascii_blank("") == "");
    CHECK(txt::trim_ascii_blank("   ") == "");
}

TEST_CASE("quote writes every byte as printable ASCII", "[text][L2-CLI-014][L2-CLI-022]") {
    CHECK(txt::quote("per-file") == "\"per-file\"");
    CHECK(txt::quote("") == "\"\"");
    CHECK(txt::quote("a\"b\\c") == "\"a\\\"b\\\\c\"");
    CHECK(txt::quote("a\tb\r\n") == "\"a\\x09b\\x0D\\x0A\"");
    CHECK(txt::quote("~ \x7f") == "\"~ \\x7F\"");
    // Byte by byte, upper-case hex: the spelling Rust's escape_bytes produces.
    CHECK(txt::quote("\xC3\xA9") == "\"\\xC3\\xA9\"");
    CHECK(txt::quote("\xEF\xBB\xBF") == "\"\\xEF\\xBB\\xBF\"");
    CHECK(txt::escape_bytes(std::string(1, '\xFF') + "a") == "\\xFFa");
    for (int b = 0; b < 256; ++b) {
        const std::string escaped = txt::escape_bytes(std::string(1, static_cast<char>(b)));
        for (std::size_t i = 0; i < escaped.size(); ++i) {
            CHECK(escaped[i] >= 0x20);
            CHECK(escaped[i] <= 0x7E);
        }
    }
}

TEST_CASE("decimal formatting handles zero and the boundaries", "[text]") {
    CHECK(txt::decimal(0) == "0");
    CHECK(txt::decimal(1) == "1");
    CHECK(txt::decimal(4294967295u) == "4294967295");
    // A uint64 at full width -- the case a 32-bit intermediate would truncate.
    CHECK(txt::decimal(18446744073709551615ull) == "18446744073709551615");
}

TEST_CASE("WTF-8 round-trips every UTF-16 sequence, paired or not", "[text][L2-CLI-021]") {
    // A Windows file name is any sequence of UTF-16 units. Each of these must
    // come back unchanged -- including the unpaired surrogates the Win32 UTF-8
    // conversion used to replace with U+FFFD.
    const char16_t ascii[] = {u'a', u'.', u'm', u'i', u'e'};
    const char16_t accented[] = {u'a', 0x00F1, u'o'};  // a, n with tilde, o
    const char16_t pair[] = {0xD83D, 0xDE00};          // one supplementary character
    const char16_t lone_high[] = {u'x', 0xD800, u'.'};
    const char16_t lone_low[] = {0xDC00, u'x'};
    const char16_t reversed[] = {0xDC00, 0xD800};  // a low then a high: two lone units
    const std::u16string cases[] = {std::u16string(),
                                    std::u16string(ascii, ascii + 5),
                                    std::u16string(accented, accented + 3),
                                    std::u16string(pair, pair + 2),
                                    std::u16string(lone_high, lone_high + 3),
                                    std::u16string(lone_low, lone_low + 2),
                                    std::u16string(reversed, reversed + 2)};
    for (std::size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        INFO("case " << i);
        CHECK(mie::text::wtf8_to_utf16(mie::text::utf16_to_wtf8(cases[i])) == cases[i]);
    }
    // The encodings themselves: a pair is the 4-byte UTF-8 form, a lone
    // surrogate the 3-byte form of its code point.
    CHECK(mie::text::utf16_to_wtf8(std::u16string(pair, pair + 2)) == "\xF0\x9F\x98\x80");
    CHECK(mie::text::utf16_to_wtf8(std::u16string(1, char16_t(0xD800))) == "\xED\xA0\x80");
    CHECK(mie::text::utf16_to_wtf8(std::u16string(accented, accented + 3)) == "a\xC3\xB1o");
}

TEST_CASE("WTF-8 decoding turns malformed input into U+FFFD", "[text][L2-CLI-021]") {
    const std::u16string replacement(1, char16_t(0xFFFD));
    CHECK(mie::text::wtf8_to_utf16("\x80") == replacement);                    // stray continuation
    CHECK(mie::text::wtf8_to_utf16("\xC0\xAF") == replacement + replacement);  // overlong
    CHECK(mie::text::wtf8_to_utf16("\xE2\x82") == replacement + replacement);  // truncated
    CHECK(mie::text::wtf8_to_utf16("\xF4\x90\x80\x80").size() == 4u);          // above U+10FFFF
    CHECK(mie::text::wtf8_to_utf16("\xFF") == replacement);
}

TEST_CASE("integer parsing refuses overflow rather than saturating", "[text][L2-CLI-020]") {
    // The grammar and range of Rust's i64 / u64 from_str: [+-]?[0-9]+, nothing
    // else, and a value outside the type is an error -- never the nearest
    // bound, which is what strtoll returned.
    int64_t s = 7;
    CHECK(mie::text::parse_int64("0", s));
    CHECK(s == 0);
    CHECK(mie::text::parse_int64("+42", s));
    CHECK(s == 42);
    CHECK(mie::text::parse_int64("-42", s));
    CHECK(s == -42);
    CHECK(mie::text::parse_int64("9223372036854775807", s));
    CHECK(s == std::numeric_limits<int64_t>::max());
    CHECK(mie::text::parse_int64("-9223372036854775808", s));
    CHECK(s == std::numeric_limits<int64_t>::min());
    s = 7;
    CHECK_FALSE(mie::text::parse_int64("9223372036854775808", s));
    CHECK_FALSE(mie::text::parse_int64("-9223372036854775809", s));
    CHECK_FALSE(mie::text::parse_int64("99999999999999999999", s));
    CHECK(s == 7);  // untouched on failure

    const char* const malformed[] = {"", "+", "-", " 5", "5 ", "0x10", "1_000", "1e3", "--5"};
    for (std::size_t i = 0; i < sizeof(malformed) / sizeof(malformed[0]); ++i) {
        INFO(malformed[i]);
        CHECK_FALSE(mie::text::parse_int64(malformed[i], s));
    }
    // Arabic-Indic four: a digit to a Unicode-aware classifier, not to ASCII.
    CHECK_FALSE(mie::text::parse_int64("\xD9\xA4", s));

    uint64_t u = 7;
    CHECK(mie::text::parse_uint64("18446744073709551615", u));
    CHECK(u == std::numeric_limits<uint64_t>::max());
    CHECK(mie::text::parse_uint64("+1", u));
    CHECK(u == 1);
    u = 7;
    CHECK_FALSE(mie::text::parse_uint64("18446744073709551616", u));
    CHECK_FALSE(mie::text::parse_uint64("-0", u));
    CHECK_FALSE(mie::text::parse_uint64("-1", u));
    CHECK(u == 7);
}

TEST_CASE("hex integers take a 0x prefix and hex digits only", "[text][L2-CLI-020]") {
    uint64_t v = 7;
    CHECK(mie::text::parse_hex_uint64("0x10", v));
    CHECK(v == 16);
    CHECK(mie::text::parse_hex_uint64("0XfF", v));
    CHECK(v == 255);
    CHECK(mie::text::parse_hex_uint64("0xFFFFFFFFFFFFFFFF", v));
    CHECK(v == std::numeric_limits<uint64_t>::max());
    v = 7;
    // No digits, a sign after the prefix (which Rust's from_str_radix once
    // let through), a non-hex digit, a missing prefix, and one digit too many.
    const char* const bad[] = {
        "0x", "0X", "0x+1", "0x-1", "-0x1", "0xg", "10", "x10", "0x10000000000000000"};
    for (std::size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        INFO(bad[i]);
        CHECK_FALSE(mie::text::parse_hex_uint64(bad[i], v));
    }
    CHECK(v == 7);
}

TEST_CASE("float literals follow Rust's f64 grammar", "[text][L2-CLI-020]") {
    // The gate in front of strtod, which also takes hexadecimal floats that
    // Rust refuses. inf / infinity / nan are lexically valid in both (and then
    // refused for not being finite by the caller that needs a finite value).
    const char* const good[] = {"1",    "1.",   ".5",   "1.5", "+1",  "-1",        "1e3",  "1E3",
                                "1e+3", "1e-3", ".5e1", "inf", "INF", "+Infinity", "-nan", "NaN"};
    for (std::size_t i = 0; i < sizeof(good) / sizeof(good[0]); ++i) {
        INFO(good[i]);
        CHECK(mie::text::is_rust_float_literal(good[i]));
    }
    const char* const bad[] = {"",     "+",      ".",   "e3",    "1e",    "1e+",
                               "0x10", "0x1p4",  "1_0", "1.2.3", " 1",    "1 ",
                               "infx", "nan(1)", "in",  "--1",   "1e3.5", "1,5"};
    for (std::size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        INFO(bad[i]);
        CHECK_FALSE(mie::text::is_rust_float_literal(bad[i]));
    }
}

TEST_CASE("signed decimal negates without undefined behaviour", "[text]") {
    CHECK(txt::decimal_signed(0) == "0");
    CHECK(txt::decimal_signed(-1) == "-1");
    CHECK(txt::decimal_signed(42) == "42");
    // INT64_MIN has no positive counterpart, so negating it directly is
    // undefined behaviour -- which the UBSan CI tier would catch.
    CHECK(txt::decimal_signed(-9223372036854775807LL - 1) == "-9223372036854775808");
}

TEST_CASE("zero padding widens rather than truncates", "[text]") {
    CHECK(txt::decimal_padded(7, 2) == "07");
    CHECK(txt::decimal_padded(0, 6) == "000000");
    CHECK(txt::decimal_padded(456225, 6) == "456225");

    SECTION("a value too wide for the field is not cut") {
        // Truncating would turn microsecond 1234567 into a plausible "234567"
        // and shift nothing visibly. Widening makes the anomaly obvious.
        CHECK(txt::decimal_padded(1234567, 6) == "1234567");
    }
}

TEST_CASE("hex is uppercase, unprefixed and zero-padded", "[text]") {
    CHECK(txt::hex_upper(0, 4) == "0000");
    CHECK(txt::hex_upper(0x2402, 4) == "2402");
    CHECK(txt::hex_upper(0x797E, 4) == "797E");
    CHECK(txt::hex_upper(0xDEADBEEFu, 8) == "DEADBEEF");
    CHECK(txt::hex_upper(0xABCDEF, 4) == "ABCDEF");
}

// ---------------------------------------------------------------------------
// fixed6 -- the DELTA column
// ---------------------------------------------------------------------------

TEST_CASE("fixed6 renders exactly six decimals", "[text][delta][L3-CPP-007]") {
    CHECK(txt::fixed6(0.0) == "0.000000");
    CHECK(txt::fixed6(1.0) == "1.000000");
    CHECK(txt::fixed6(0.5) == "0.500000");
    CHECK(txt::fixed6(1.2345) == "1.234500");
    CHECK(txt::fixed6(0.000001) == "0.000001");
}

TEST_CASE("fixed6 declines to invent a spelling for non-finite values", "[text][delta]") {
    // Neither of the other implementations emits a token here, so emitting
    // "nan" or "inf" would be a divergence that only shows up on pathological
    // input -- exactly where an operator is least able to spot it.
    //
    // Built from <limits> rather than by dividing by a zero constant: MSVC
    // constant-folds `1.0 / zero` and rejects it outright as C2124 "divide or
    // mod by zero", where GCC and Clang quietly produce the IEEE infinity. That
    // is a compile-time divergence between the tiers, not a runtime one, so it
    // fails the Windows build rather than a test.
    CHECK(txt::fixed6(std::numeric_limits<double>::infinity()).empty());
    CHECK(txt::fixed6(-std::numeric_limits<double>::infinity()).empty());
    CHECK(txt::fixed6(std::numeric_limits<double>::quiet_NaN()).empty());
}

TEST_CASE("fixed6 emits a dot even under a comma-separator locale", "[text][delta][L3-CPP-007]") {
    // THE case this function exists for. The decoder never calls setlocale, but
    // as a library it can be linked into a host that already has -- and a comma
    // here corrupts every DELTA cell while leaving the CSV parseable.
    //
    // The locale is restored before the assertion is evaluated, so a Catch2
    // failure message cannot itself be formatted under the altered locale.
    const char* previous = std::setlocale(LC_NUMERIC, 0);
    const std::string saved = previous != 0 ? std::string(previous) : std::string("C");

    const char* applied = std::setlocale(LC_NUMERIC, "de_DE.UTF-8");
    if (applied == 0) {
        applied = std::setlocale(LC_NUMERIC, "de_DE");
    }
    const bool locale_available = applied != 0;

    const std::string rendered = txt::fixed6(1.2345);
    // Restoring the locale is best-effort teardown: if it fails there is
    // nothing useful to do about it here, and failing the test would report a
    // teardown problem as a formatting problem.
    (void)std::setlocale(LC_NUMERIC, saved.c_str());

    CHECK(rendered == "1.234500");
    CHECK(rendered.find(',') == std::string::npos);

    if (!locale_available) {
        // Reported rather than silently skipped: a gate that cannot run is not
        // a gate that passed, and on a runner without the German locale this
        // case proves only that the C-locale path works.
        WARN(
            "de_DE locale unavailable; fixed6 separator normalisation was not "
            "exercised against a comma locale on this host");
    }
}

TEST_CASE("fixed6 rounds the way the other implementations round", "[text][delta]") {
    // Rust's {:.6}, Python's f"{d:.6f}" and C's %.6f all round the EXACT BINARY
    // VALUE, and that is the subtlety worth pinning: none of the literals below
    // is a true tie, because none is exactly representable as a double. Which
    // way each one goes is decided by whether the nearest double sits above or
    // below the decimal it was written as -- not by a tie-breaking rule.
    //
    // 0.0000005 is stored slightly BELOW five ten-millionths, so it rounds down.
    // 2.0000005 is stored slightly ABOVE, so it rounds up. Reasoning about these
    // as "ties to even" predicts the wrong answer for the second one; the first
    // draft of this test did exactly that.
    //
    // Every expectation here was taken from the two reference implementations
    // rather than derived: Rust and Python were both run on these inputs and
    // agree with all of them. The conformance oracles remain the real proof.
    CHECK(txt::fixed6(0.0000005) == "0.000000");
    CHECK(txt::fixed6(0.0000015) == "0.000002");
    CHECK(txt::fixed6(2.0000005) == "2.000001");
    CHECK(txt::fixed6(-0.5) == "-0.500000");
    CHECK(txt::fixed6(0.5) == "0.500000");
}

TEST_CASE("fixed6 handles a very large magnitude without truncating", "[text][delta]") {
    // A double's exact decimal expansion runs to ~310 integer digits, and the
    // internal buffer has to clear that. A silent truncation would emit a
    // shortened number rather than nothing -- a wrong value that still looks
    // like one.
    //
    // The expected length and leading digits were taken from Rust and Python,
    // which produce byte-identical output for this input. Digits past the first
    // seventeen are not "precision": they are the exact value of the nearest
    // double, and all three implementations print it in full rather than
    // zero-filling, so a divergence here would be a real one.
    const std::string big = txt::fixed6(1e300);
    REQUIRE_FALSE(big.empty());
    CHECK(big.size() == 308);
    CHECK(big.compare(0, 40, "1000000000000000052504760255204420248704") == 0);
    CHECK(big.substr(big.size() - 10) == "160.000000");
}
