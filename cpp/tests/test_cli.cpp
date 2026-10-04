// SPDX-License-Identifier: Apache-2.0
//
// CLI tests: argument parsing, precedence, and the exit-code contract.
//
// The shared conformance oracles already prove the CSV bytes are right. What
// they cannot reach is everything that happens BEFORE a decode starts -- a
// mistyped flag, a value out of range, a flag belonging to an unported module
// -- because a case that never produces a CSV has no oracle to compare against.
// Those failures are exactly the ones an operator hits first, so they are
// tested here, where `run()` is callable in-process and the exit code is an
// ordinary return value.

#include "mie/cli.hpp"

#include <catch2/catch.hpp>

#include <cstdio>
#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/log.hpp"
#include "mie/merge.hpp"
#include "mie/platform.hpp"
#include "mie/text.hpp"
#include "mie/writer.hpp"
#include "record_fixtures.hpp"
#include "temp_path.hpp"

namespace {

using mie_test::TempFile;
using mie_test::TempPath;

typedef std::vector<std::string> Args;

/// Build an argument vector without C++11 initializer-list-into-vector, which
/// reads badly at every call site once there are six of them.
Args args(const std::string& a0, const std::string& a1 = std::string(),
          const std::string& a2 = std::string(), const std::string& a3 = std::string(),
          const std::string& a4 = std::string(), const std::string& a5 = std::string(),
          const std::string& a6 = std::string(), const std::string& a7 = std::string()) {
    Args out;
    out.push_back(a0);
    // An empty trailing argument is treated as absent. That is fine for a
    // command line -- no flag or path here is the empty string -- and a case
    // that genuinely needs one builds its vector directly.
    const std::string rest[7] = {a1, a2, a3, a4, a5, a6, a7};
    for (std::size_t i = 0; i < 7; ++i) {
        if (!rest[i].empty()) {
            out.push_back(rest[i]);
        }
    }
    return out;
}

/// A minimal well-formed recording: two BC-to-RT records and the terminator.
///
/// Two, not one: entry validation confirms a candidate by looking ahead, so a
/// single-record file exercises a different path than the one these tests are
/// about.
std::vector<uint8_t> valid_recording() {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < 2; ++i) {
        mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 100 + i * 100));
    }
    return mie_test::finish(words);
}

/// Run the CLI with its reporting streams pointed at temporary files, and
/// return what it wrote to each.
///
/// No file-descriptor juggling: `run()` takes the streams it reports on, which
/// is the whole reason that seam exists.
int run_capturing(const Args& argv, std::string& out, std::string& err) {
    const TempPath out_path("mie-cli-out");
    const TempPath err_path("mie-cli-err");
    int code = 0;
    {
        std::FILE* out_handle = std::fopen(out_path.str().c_str(), "wb");
        std::FILE* err_handle = std::fopen(err_path.str().c_str(), "wb");
        REQUIRE(out_handle != NULL);
        REQUIRE(err_handle != NULL);
        code = mie::cli::run(argv, mie::cli::Streams(out_handle, err_handle));
        // Checked, not voided: a failed close can mean the bytes never
        // reached the file, and a test that then read an empty capture would
        // report a CLI bug that does not exist.
        REQUIRE(std::fclose(out_handle) == 0);
        REQUIRE(std::fclose(err_handle) == 0);
    }
    REQUIRE(mie_test::read_file(out_path.str(), out));
    REQUIRE(mie_test::read_file(err_path.str(), err));
    return code;
}

int run_capturing_stdout(const Args& argv, std::string& out) {
    std::string ignored;
    return run_capturing(argv, out, ignored);
}

}  // namespace

TEST_CASE("version and help are answered before anything else", "[cli][L3-CPP-014][L2-CLI-017]") {
    SECTION("every accepted spelling exits 0") {
        // Note the asymmetry: version is case-insensitive, help is not.
        // That is not a tidiness question -- Rust and Python both reject
        // `--HELP`, and accepting it made C++ the only one that did.
        const char* const spellings[] = {"--version", "-V", "-v", "--VERSION", "--help", "-h"};
        for (std::size_t i = 0; i < sizeof(spellings) / sizeof(spellings[0]); ++i) {
            std::string out;
            INFO(spellings[i]);
            REQUIRE(run_capturing_stdout(args(spellings[i]), out) == mie::cli::EXIT_OK);
            REQUIRE_FALSE(out.empty());
        }
    }

    SECTION("help is case-sensitive, unlike version") {
        const char* const rejected[] = {"--HELP", "--Help", "--hELP"};
        for (std::size_t i = 0; i < sizeof(rejected) / sizeof(rejected[0]); ++i) {
            INFO(rejected[i]);
            REQUIRE(mie::cli::run(args(rejected[i])) == mie::cli::EXIT_USAGE);
        }
    }

    SECTION("help wins over an otherwise broken invocation") {
        // An operator whose command line is wrong is the one most likely to ask
        // for help. Validating first would answer the question they did not ask.
        std::string out;
        REQUIRE(run_capturing_stdout(args("decode", "--nonsense", "--help"), out) ==
                mie::cli::EXIT_OK);
        REQUIRE(out.find("USAGE") != std::string::npos);
    }

    SECTION("the help text names every implemented subcommand") {
        const std::string help = mie::cli::help_text();
        REQUIRE(help.find("decode") != std::string::npos);
        REQUIRE(help.find("count") != std::string::npos);
    }
}

// Help text is prose on a stream, and L2-CLI-014 binds prose to the same ASCII
// rule as payload. It did not always: the rule exempted stderr prose, and this
// banner opened with a U+2014 em dash spelled "\xE2\x80\x94" -- ASCII in the
// source, three bytes on the wire. A stock Windows console runs at the OEM code
// page rather than UTF-8 and drew them as three unrelated glyphs, which was
// reported as memory corruption.
//
// scripts/assert-ascii-output.py enforces this at the source across all three
// implementations; this is the runtime half, on the bytes actually written.
// Rust and Python carry the same test against their own help text.
TEST_CASE("the help text is pure ASCII", "[cli][L2-CLI-014]") {
    const std::string help = mie::cli::help_text();
    REQUIRE_FALSE(help.empty());
    for (std::string::const_iterator it = help.begin(); it != help.end(); ++it) {
        const unsigned char byte = static_cast<unsigned char>(*it);
        INFO("byte 0x" << mie::text::hex_upper(byte, 2) << " at offset " << (it - help.begin()));
        REQUIRE(byte < 0x80);
    }
}

// `decode --nonsense --help` exits through the CliError catch, which sees a
// pending help flag -- so it never reaches the `throw HelpRequested()` inside
// the subcommand parsers. Those are reached only by a help flag on an OTHERWISE
// VALID subcommand line, which is the plainest way an operator asks for help
// about one command, and which nothing exercised until this case.
TEST_CASE("help asked for on a valid subcommand line prints help",
          "[cli][L3-CPP-014][L2-CLI-017]") {
    const char* const commands[] = {"decode", "count", "dump"};
    for (std::size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        std::string out;
        INFO(commands[i]);
        REQUIRE(run_capturing_stdout(args(commands[i], "--help"), out) == mie::cli::EXIT_OK);
        REQUIRE(out.find("USAGE") != std::string::npos);

        // And with the input present, so the flag is reached mid-parse rather
        // than as the parser's first token.
        std::string with_input;
        REQUIRE(run_capturing_stdout(args(commands[i], "rec.mie", "--help"), with_input) ==
                mie::cli::EXIT_OK);
        REQUIRE(with_input.find("USAGE") != std::string::npos);
    }
}

// A SUBCOMMAND flag whose value looks like an option. The global flags take a
// different path, so `--log-level -h` (covered above) does not reach this one:
// the diagnostic here is the ArgReader's, and it names the joined spelling as
// the way to pass the value deliberately.
TEST_CASE("a subcommand flag refuses an option-like value", "[cli][L2-CLI-015]") {
    std::string out;
    std::string err;
    REQUIRE(run_capturing(args("decode", "--mux-delimiter", "-h", "rec.mie"), out, err) ==
            mie::cli::EXIT_USAGE);
    INFO(err);
    REQUIRE(err.find("--mux-delimiter") != std::string::npos);
    REQUIRE(err.find("is an option") != std::string::npos);
    // The diagnostic points at the joined spelling, which remains legal.
    REQUIRE(err.find("--mux-delimiter=-h") != std::string::npos);
}

// `count` on a valid but EMPTY recording: the count goes to stdout, and the
// human sentence naming the empty capture goes to stderr. The sentence is one
// of the messages that carried a non-ASCII em dash (L2-CLI-014).
TEST_CASE("count names an empty recording on stderr", "[cli][L1-EXIT-010][L2-CLI-014]") {
    std::vector<uint8_t> terminator_only;
    terminator_only.push_back(0x00);
    terminator_only.push_back(0x00);
    const TempFile input("mie-cli-empty.mie", terminator_only);

    std::string out;
    std::string err;
    REQUIRE(run_capturing(args("count", input.str()), out, err) == mie::cli::EXIT_OK);
    REQUIRE(out == "0\n");
    INFO(err);
    REQUIRE(err.find("empty recording") != std::string::npos);
    for (std::string::const_iterator it = err.begin(); it != err.end(); ++it) {
        REQUIRE(static_cast<unsigned char>(*it) < 0x80);
    }
}

TEST_CASE("a bare invocation is a usage error, not a success", "[cli][L3-CPP-014]") {
    // Exit 0 here would let a script that forgot to pass its arguments look
    // like it had done its job.
    REQUIRE(mie::cli::run(Args()) == mie::cli::EXIT_USAGE);
}

TEST_CASE("unknown commands and options are usage errors", "[cli][L3-CPP-014]") {
    REQUIRE(mie::cli::run(args("frobnicate")) == mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("decode", "--frobnicate", "x.mie")) == mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("count", "--frobnicate", "x.mie")) == mie::cli::EXIT_USAGE);
    // No input at all.
    REQUIRE(mie::cli::run(args("decode")) == mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("count")) == mie::cli::EXIT_USAGE);
}

TEST_CASE("a valued flag given no value is a usage error", "[cli][L3-CPP-014]") {
    // The trailing-flag case: `--output` as the last token has nothing after
    // it. Reading past the end would be a crash; treating the absence as an
    // empty value would write to a file named "".
    const char* const flags[] = {"--output",
                                 "--config",
                                 "--log-level",
                                 "--input-time-format",
                                 "--format",
                                 "--detect-records",
                                 "--lookahead-records",
                                 "--standard-tick-rate-hz",
                                 "--max-sort-group",
                                 "--mux-delimiter",
                                 "--mux-field",
                                 "--exclude-types",
                                 "--include-types",
                                 "--exclude-rts",
                                 "--include-rts",
                                 "--exclude-buses",
                                 "--include-buses",
                                 "--exclude-subaddresses",
                                 "--include-subaddresses"};
    for (std::size_t i = 0; i < sizeof(flags) / sizeof(flags[0]); ++i) {
        INFO(flags[i]);
        REQUIRE(mie::cli::run(args("decode", "in.mie", flags[i])) == mie::cli::EXIT_USAGE);
    }
    REQUIRE(mie::cli::run(args("decode", "in.mie", "-o")) == mie::cli::EXIT_USAGE);
}

TEST_CASE("--flag=value and --flag value are the same thing", "[cli][L3-CPP-014][L2-CLI-015]") {
    // Both spellings go through one cursor rather than being written out per
    // flag, so this sweep is what proves the cursor -- not twenty near-copies.
    const TempFile input("mie-cli-forms.mie", valid_recording());

    struct Case {
        const char* flag;
        const char* value;
    };
    const Case cases[] = {
        {"--input-time-format", "irig"},
        {"--format", "csv"},
        {"--detect-records", "4"},
        {"--lookahead-records", "2"},
        {"--standard-tick-rate-hz", "1000000"},
        {"--max-sort-group", "64"},
        {"--mux-delimiter", "_"},
        {"--mux-field", "0"},
        {"--exclude-rts", "31"},
        {"--include-rts", "15"},
        {"--exclude-buses", "B"},
        {"--include-buses", "A"},
        {"--exclude-subaddresses", "30"},
        {"--include-subaddresses", "11"},
        {"--exclude-types", "RT_TO_RT"},
        {"--include-types", "BC_TO_RT"},
    };

    for (std::size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        const TempPath separated("mie-cli-sep.csv");
        const TempPath joined("mie-cli-join.csv");
        INFO(cases[i].flag);

        const Args split_form =
            args("decode", input.str(), "-o", separated.str(), cases[i].flag, cases[i].value);
        const Args joined_form = args("decode", input.str(), "-o", joined.str(),
                                      std::string(cases[i].flag) + "=" + cases[i].value);

        REQUIRE(mie::cli::run(split_form) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(joined_form) == mie::cli::EXIT_OK);

        // Same bytes, not merely the same exit code: a form that parsed but
        // dropped the value would still exit 0.
        std::string a;
        std::string b;
        REQUIRE(mie_test::read_file(separated.str(), a));
        REQUIRE(mie_test::read_file(joined.str(), b));
        REQUIRE(a == b);
    }
}

TEST_CASE("--flag= is the flag with an empty value, not an unknown option",
          "[cli][L3-CPP-014][L2-CLI-015]") {
    // The sweep above only ever passes NON-EMPTY values, so it cannot reach
    // this boundary. `take_value` matched on `token.size() > prefix.size()`,
    // one character too strict: `--exclude-rts=` fell past the `=` branch and
    // past the exact-name check and was reported as an unknown option, exit 4,
    // where Rust and Python both exited 0 having applied an empty filter.
    //
    // The contract is that `--flag=` hands the empty string to the flag's own
    // validator and lets it decide -- which is why the two halves below differ.
    const TempFile input("mie-cli-empty-eq.mie", valid_recording());

    SECTION("a validator that accepts empty: an empty filter decodes") {
        const TempPath joined("mie-cli-empty-join.csv");
        const TempPath omitted("mie-cli-empty-omit.csv");

        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", joined.str(), "--exclude-rts=")) ==
                mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", omitted.str())) ==
                mie::cli::EXIT_OK);

        // An empty exclusion excludes nothing, so the bytes must match the run
        // that never passed the flag at all.
        std::string a;
        std::string b;
        REQUIRE(mie_test::read_file(joined.str(), a));
        REQUIRE(mie_test::read_file(omitted.str(), b));
        REQUIRE(a == b);
    }

    SECTION("a validator that rejects empty still rejects it") {
        // These already returned EXIT_USAGE before the fix -- but as "unknown
        // option", never reaching the validator. Same code, different reason,
        // which is why the exit code alone could not have caught the bug.
        REQUIRE(mie::cli::run(args("decode", input.str(), "--mux-delimiter=")) ==
                mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("decode", input.str(), "--max-sort-group=")) ==
                mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("the separated form refuses a value that looks like an option",
          "[cli][L3-CPP-014][L2-CLI-015]") {
    // The last cross-implementation divergence in flag parsing.
    // `--mux-delimiter --no-mux` set the delimiter to the string "--no-mux"
    // here and in Rust -- so `--no-mux` silently never ran and the decode
    // succeeded with a wrong MUX column -- while Python refused it. A silent
    // wrong answer is the worst of the three possible behaviours, so both
    // moved to Python's rule.
    const TempFile input("mie-cli-optlike.mie", valid_recording());

    SECTION("option-like tokens are refused") {
        // Only shapes every supported Python version agrees on. "-5e3",
        // "-0x5" and "-1a" begin like numbers under the 3.14 matcher and are
        // values; 3.10-3.13 called them options, so they are outside the
        // contract and not asserted here.
        const char* const tokens[] = {"--no-mux", "--foo", "-o", "-x", "-abc", "--1"};
        for (std::size_t i = 0; i < sizeof(tokens) / sizeof(tokens[0]); ++i) {
            INFO(tokens[i]);
            REQUIRE(mie::cli::run(args("decode", input.str(), "--mux-delimiter", tokens[i])) ==
                    mie::cli::EXIT_USAGE);
        }
    }

    SECTION("the joined form is unambiguous and still accepts them") {
        const TempPath out("mie-cli-optlike-join.csv");
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(),
                                   "--mux-delimiter=--no-mux")) == mie::cli::EXIT_OK);
    }

    SECTION("the exemptions are values, not options") {
        // A lone "-" is a path: `-o -` writes a file named "-" (L2-CLI-005).
        // A negative number is a value: `--mux-field -1` counts from the end.
        // A token with a space is a value; no option is spelled that way.
        const TempPath out("mie-cli-optlike-ok.csv");
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(), "--mux-field", "-1")) ==
                mie::cli::EXIT_OK);
        const TempPath out2("mie-cli-optlike-ok2.csv");
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out2.str(), "--mux-delimiter",
                                   "- x")) == mie::cli::EXIT_OK);

        // A negative number still reaches its OWN validator, so a flag that
        // forbids negatives refuses it for that reason -- not for looking
        // like an option.
        REQUIRE(mie::cli::run(args("decode", input.str(), "--collapse-window-us", "-5")) ==
                mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("`--` ends option parsing", "[cli][L3-CPP-014][L2-CLI-016]") {
    // POSIX end-of-options. After `--` every token is a path, however it is
    // spelled -- which is the only way to decode a file whose name begins with
    // a dash. Python has always honoured it; Rust and C++ used to report `--`
    // itself as an unknown option.
    const TempFile input("mie-cli-eoo.mie", valid_recording());

    SECTION("before the subcommand, the next token is the subcommand name") {
        const TempPath out("mie-cli-eoo-a.csv");
        REQUIRE(mie::cli::run(args("--", "decode", input.str(), "-o", out.str())) ==
                mie::cli::EXIT_OK);

        // Scoped to that loop: the subcommand parser gets a fresh scan, so a
        // flag after the subcommand still acts as a flag.
        const TempPath out2("mie-cli-eoo-b.csv");
        REQUIRE(mie::cli::run(args("--", "decode", input.str(), "-o", out2.str(), "--no-mux")) ==
                mie::cli::EXIT_OK);
    }

    SECTION("it suppresses the global flags rather than being one") {
        // `-- --version` asks for a SUBCOMMAND called "--version".
        REQUIRE(mie::cli::run(args("--", "--version")) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("--", "-h")) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("--", "--help")) == mie::cli::EXIT_USAGE);
        // And on its own there is still no subcommand.
        REQUIRE(mie::cli::run(args("--")) == mie::cli::EXIT_USAGE);
    }

    SECTION("after it, a flag spelling is a path") {
        // `--no-mux` becomes a second input, so this fails opening it rather
        // than quietly disabling the MUX column.
        const TempPath out("mie-cli-eoo-c.csv");
        REQUIRE(mie::cli::run(args("decode", input.str(), "--", "--no-mux", "-o", out.str())) !=
                mie::cli::EXIT_OK);
    }

    SECTION("only the first marker is consumed") {
        // A second `--` is an ordinary positional, which is how a file
        // actually named `--` is named.
        const TempPath out("mie-cli-eoo-d.csv");
        REQUIRE(mie::cli::run(args("decode", "--", "--", "-o", out.str())) != mie::cli::EXIT_OK);
    }

    SECTION("count and dump honour it too") {
        const TempPath out("mie-cli-eoo-e.csv");
        REQUIRE(mie::cli::run(args("--", "count", input.str())) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("--", "dump", input.str())) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("count", "--", input.str())) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("dump", "--", input.str())) == mie::cli::EXIT_OK);
        // One positional each: after `--`, a flag spelling becomes a second.
        REQUIRE(mie::cli::run(args("dump", "--", input.str(), "--raw")) == mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("help outranks a deferred diagnostic but not a missing value",
          "[cli][L3-CPP-014][L2-CLI-017]") {
    // This used to be answered by a scan over the whole argument vector, which
    // could not tell a help token apart from a token being consumed as some
    // flag's VALUE, and did not know where the subcommand began. Both mistakes
    // made C++ the only implementation doing them.
    const TempFile input("mie-cli-help.mie", valid_recording());

    SECTION("a deferred diagnostic yields to a pending help request") {
        // An unrecognised option, and a value rejected AFTER it was taken:
        // both are deferred, so help wins.
        std::string out;
        REQUIRE(run_capturing_stdout(args("decode", input.str(), "--nonsense", "--help"), out) ==
                mie::cli::EXIT_OK);
        REQUIRE(out.find("USAGE") != std::string::npos);

        REQUIRE(mie::cli::run(args("decode", input.str(), "--input-time-format", "bad",
                                   "--help")) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("count", input.str(), "--nonsense", "--help")) ==
                mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--nonsense", "--help")) ==
                mie::cli::EXIT_OK);
    }

    SECTION("a failed value consumption does not yield to it") {
        // `take_value` abandons the rest of the line here, so nothing is
        // pending. Without that these all printed help and exited 0, where
        // Rust and Python reported the usage error.
        REQUIRE(mie::cli::run(args("--log-level", "-h", "decode", input.str())) ==
                mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("--config", "--help", "decode", input.str())) ==
                mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("decode", input.str(), "--mux-delimiter", "-h")) ==
                mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("decode", input.str(), "--mux-delimiter", "-h", "--help")) ==
                mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("--log-level", "--nonsense", "--help")) == mie::cli::EXIT_USAGE);
    }

    SECTION("version is global-only") {
        // After the subcommand it belongs to that subcommand, and every one of
        // them calls it an unknown option. The old scan answered it anywhere.
        REQUIRE(mie::cli::run(args("decode", input.str(), "--version")) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("count", input.str(), "--version")) == mie::cli::EXIT_USAGE);
        std::string out;
        REQUIRE(run_capturing_stdout(args("--version"), out) == mie::cli::EXIT_OK);
        REQUIRE(out.find("aero1553") != std::string::npos);
    }

    SECTION("help after `--` is a path, so it rescues nothing") {
        REQUIRE(mie::cli::run(args("decode", "--nonsense", "--", "--help")) ==
                mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("numeric flags reject trailing junk", "[cli][L3-CPP-014]") {
    // `strtoll` stops at the first non-digit and reports success, so "4x" would
    // silently become 4. A typo must be refused, not rounded off.
    // Whitespace INSIDE the number, or a non-ASCII space around it, is junk too.
    const char* const bad[] = {"4x", "", "  ", "0x4", "4.5", "--", "1e3", "4 4", "4\xC2\xA0"};
    for (std::size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        INFO(bad[i]);
        REQUIRE(mie::cli::run(args("decode", "in.mie", "--detect-records", bad[i])) ==
                mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("numeric flags ignore surrounding ASCII whitespace", "[cli][L2-CLI-020]") {
    // One rule on every numeric flag, in both implementations: surrounding
    // ASCII whitespace is trimmed (Rust's `str::trim_ascii`). C++ used to skip
    // only LEADING blanks, as strtoll did, and Rust's --year trimmed nothing.
    // Only parsing is in question here, so the missing input is the failure
    // that should remain: exit 1, not the usage error a bad value gives.
    const char* const padded[] = {" 4", "4 ", "\t4\t", "\r\n4"};
    for (std::size_t i = 0; i < sizeof(padded) / sizeof(padded[0]); ++i) {
        INFO(padded[i]);
        CHECK(mie::cli::run(args("decode", "no-such.mie", "--detect-records", padded[i])) ==
              mie::cli::EXIT_RUNTIME);
        CHECK(mie::cli::run(args("decode", "no-such.mie", "--year", padded[i])) ==
              mie::cli::EXIT_RUNTIME);
    }
}

TEST_CASE("bounded flags reject out-of-range values", "[cli][L3-CPP-014]") {
    static const char* const out_of_range[][2] = {
        {"--detect-records", "0"},     {"--detect-records", "33"}, {"--lookahead-records", "0"},
        {"--lookahead-records", "33"}, {"--max-sort-group", "0"},  {"--max-sort-group", "1048577"},
        {"--exclude-rts", "32"},       {"--include-rts", "-1"},    {"--exclude-subaddresses", "99"},
    };
    for (std::size_t i = 0; i < sizeof(out_of_range) / sizeof(out_of_range[0]); ++i) {
        INFO(out_of_range[i][0] << " " << out_of_range[i][1]);
        REQUIRE(mie::cli::run(args("decode", "in.mie", out_of_range[i][0], out_of_range[i][1])) ==
                mie::cli::EXIT_USAGE);
    }

    SECTION("the boundaries themselves are accepted") {
        const TempFile input("mie-cli-bounds.mie", valid_recording());
        static const char* const ok[][2] = {
            {"--detect-records", "1"},    {"--detect-records", "32"},
            {"--lookahead-records", "1"}, {"--lookahead-records", "32"},
            {"--max-sort-group", "1"},    {"--max-sort-group", "1048576"},
        };
        for (std::size_t i = 0; i < sizeof(ok) / sizeof(ok[0]); ++i) {
            const TempPath out("mie-cli-bound.csv");
            INFO(ok[i][0] << " " << ok[i][1]);
            REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(), ok[i][0],
                                       ok[i][1])) == mie::cli::EXIT_OK);
        }
    }
}

TEST_CASE("--standard-tick-rate-hz must be a positive number", "[cli][L3-CPP-014]") {
    // A zero or negative rate would divide by zero or run the clock backwards;
    // NaN compares false against every bound, so it has to be rejected by the
    // positive test rather than by a range check.
    const char* const bad[] = {"0", "-1", "-0.5", "abc", "", "nan", "1.0.0"};
    for (std::size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        INFO(bad[i]);
        REQUIRE(mie::cli::run(args("decode", "in.mie", "--standard-tick-rate-hz", bad[i])) ==
                mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("--mux-delimiter rejects an empty value", "[cli][L3-CPP-014]") {
    // Splitting on "" has no meaningful answer; accepting it would produce a
    // MUX column derived from nothing.
    REQUIRE(mie::cli::run(args("decode", "in.mie", "--mux-delimiter=")) == mie::cli::EXIT_USAGE);
}

TEST_CASE("--mux-field accepts a negative index", "[cli][L3-CPP-014]") {
    // Unlike the other integer flags this one is deliberately unbounded below:
    // a negative index counts back from the end of the filename.
    const TempFile input("mie-cli-mux.mie", valid_recording());
    const TempPath out("mie-cli-mux.csv");
    REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(), "--mux-field", "-1")) ==
            mie::cli::EXIT_OK);
}

TEST_CASE("a bad filter value is a usage error, not a config error", "[cli][L3-CPP-014]") {
    // Regression pin. `parse_type_name` and `parse_bus_name` are shared with
    // the config loader and signal with ConfigError; reached through a CLI flag
    // that must become exit 4, not exit 5, and above all not an uncaught
    // exception -- which is what it was, aborting the process.
    REQUIRE(mie::cli::run(args("decode", "in.mie", "--exclude-types", "0x100")) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("decode", "in.mie", "--include-types", "NOT_A_TYPE")) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("decode", "in.mie", "--exclude-buses", "C")) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("decode", "in.mie", "--include-buses", "")) == mie::cli::EXIT_USAGE);
}

TEST_CASE("filter lists tolerate whitespace and a trailing comma", "[cli][L3-CPP-014]") {
    const TempFile input("mie-cli-list.mie", valid_recording());
    const TempPath tidy("mie-cli-tidy.csv");
    const TempPath messy("mie-cli-messy.csv");

    REQUIRE(mie::cli::run(args("decode", input.str(), "-o", tidy.str(), "--exclude-rts",
                               "1,2,3")) == mie::cli::EXIT_OK);
    REQUIRE(mie::cli::run(args("decode", input.str(), "-o", messy.str(), "--exclude-rts",
                               " 1 , 2 ,3 ,")) == mie::cli::EXIT_OK);

    std::string a;
    std::string b;
    REQUIRE(mie_test::read_file(tidy.str(), a));
    REQUIRE(mie_test::read_file(messy.str(), b));
    REQUIRE(a == b);
}

TEST_CASE("an invalid --log-level is refused rather than ignored", "[cli][L3-CPP-014]") {
    // Silently falling back to the default would leave an operator who asked
    // for DEBUG staring at a quiet run and concluding nothing happened.
    REQUIRE(mie::cli::run(args("--log-level", "LOUD", "count", "in.mie")) == mie::cli::EXIT_USAGE);

    const char* const accepted[] = {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL", "OFF"};
    const TempFile input("mie-cli-level.mie", valid_recording());
    for (std::size_t i = 0; i < sizeof(accepted) / sizeof(accepted[0]); ++i) {
        std::string out;
        INFO(accepted[i]);
        REQUIRE(run_capturing_stdout(args("--log-level", accepted[i], "count", input.str()), out) ==
                mie::cli::EXIT_OK);
    }
    mie::log::set_level(mie::log::LEVEL_OFF);
}

TEST_CASE("dump parses its own flag set", "[cli][L3-CPP-028]") {
    const TempFile input("mie-cli-dump.mie", valid_recording());

    SECTION("both views run") {
        REQUIRE(mie::cli::run(args("dump", input.str())) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--raw")) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--records", "1")) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--raw", "--offset", "4", "--length",
                                   "8")) == mie::cli::EXIT_OK);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--offset=2")) == mie::cli::EXIT_OK);
    }

    SECTION("a missing input is a runtime error, not a usage one") {
        // The command line was fine; the file was not. Collapsing the two would
        // send an operator looking for a typo that is not there.
        const TempPath absent("mie-cli-dump-absent.mie");
        REQUIRE(mie::cli::run(args("dump", absent.str())) == mie::cli::EXIT_RUNTIME);
    }

    SECTION("bad flags and arguments are usage errors") {
        REQUIRE(mie::cli::run(args("dump")) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("dump", "--frobnicate", input.str())) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--offset")) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--offset", "-1")) == mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--records", "abc")) ==
                mie::cli::EXIT_USAGE);
        REQUIRE(mie::cli::run(args("dump", input.str(), "--length", "4x")) == mie::cli::EXIT_USAGE);
    }

    SECTION("a second input is refused rather than merged") {
        // dump is a view of ONE byte range. There is no sensible way to show
        // two files at once, so a second path is a mistake -- not, as it is for
        // decode, an invitation to merge.
        REQUIRE(mie::cli::run(args("dump", input.str(), input.str())) == mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("count still takes exactly one input", "[cli][L3-CPP-015]") {
    // The merge is a decode-only capability.
    REQUIRE(mie::cli::run(args("count", "a.mie", "b.mie")) == mie::cli::EXIT_USAGE);
}

TEST_CASE("the three input methods are mutually exclusive", "[cli][L3-CPP-024]") {
    // L2-MRG-001. Each names the input set completely, so combining two leaves
    // the ORDER of the result undefined -- and order is the whole point of a
    // time-sorted merge.
    REQUIRE(mie::cli::run(args("decode", "a.mie", "--manifest", "m.txt")) == mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("decode", "a.mie", "--glob", "*.mie")) == mie::cli::EXIT_USAGE);
    REQUIRE(mie::cli::run(args("decode", "--manifest", "m.txt", "--glob", "*.mie")) ==
            mie::cli::EXIT_USAGE);
}

TEST_CASE("an input set that resolves to nothing names which method was empty",
          "[cli][L3-CPP-024]") {
    // "the manifest was empty", "the glob matched nothing" and "you gave me no
    // arguments" are three different mistakes, and an operator debugging a
    // batch script needs to know which one they made.
    const TempFile empty_manifest("mie-cli-empty-manifest.txt",
                                  std::string("# nothing but a comment\n"));
    std::string out;
    std::string err;

    REQUIRE(run_capturing(args("decode", "--manifest", empty_manifest.str()), out, err) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(err.find("contains no input paths") != std::string::npos);

    const TempPath anchor("cli-glob-none");
    REQUIRE(run_capturing(args("decode", "--glob", anchor.str() + "-*.mie"), out, err) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(err.find("matched no files") != std::string::npos);

    // A wildcard in the directory part is a malformed pattern, not a missing
    // folder: a usage error, refused before the filesystem is touched.
    REQUIRE(run_capturing(args("decode", "--glob", "captures/**/*.mie"), out, err) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(err.find("directory part") != std::string::npos);
}

TEST_CASE("a merge refuses to write over one of its own inputs", "[cli][L3-CPP-029]") {
    // Regression pin, and a data-loss one. The writer's own input/output guard
    // takes a single path and is deliberately given none on the merge path --
    // it is handed one stream and cannot know how many files fed it -- so for a
    // merge the CLI's check is the only thing standing between
    // `decode a.mie b.mie -o a.mie` and a truncated input being read as it is
    // overwritten. Rust and Python both guard this; C++ shipped without it.
    const TempFile first("mie-cli-collide-a.mie", valid_recording());
    const TempFile second("mie-cli-collide-b.mie", valid_recording());

    std::string out;
    std::string err;

    SECTION("the output naming the first input") {
        REQUIRE(run_capturing(args("decode", first.str(), second.str(), "-o", first.str()), out,
                              err) == mie::cli::EXIT_RUNTIME);
        REQUIRE(err.find("resolves to merge input") != std::string::npos);
    }

    SECTION("the output naming a later input") {
        // Every input is checked, not just the first.
        REQUIRE(run_capturing(args("decode", first.str(), second.str(), "-o", second.str()), out,
                              err) == mie::cli::EXIT_RUNTIME);
        REQUIRE(err.find("resolves to merge input") != std::string::npos);
    }

    SECTION("an input a DERIVED output path would overwrite") {
        // The destination itself collides with nothing; the errors file derived
        // FROM it is the input. `-o capture.mie --separate-errors` derives
        // `capture_errors.mie`, a plausible name for a second recorder's file.
        // Before this the guard checked only the destination, the errors file
        // committed straight over the input, and the run exited 0.
        //
        // The victim path is DERIVED, not named: TempFile picks a unique
        // suffix of its own, so a separately-named fixture would never be what
        // `error_path_for` computes and the test would pass for the wrong
        // reason. (It did, first time round.)
        TempPath destination("mie-cli-collide.mie");
        const std::vector<uint8_t> recording = valid_recording();
        const std::string before(recording.begin(), recording.end());
        const std::string victim = destination.also_remove(mie::error_path_for(destination.str()));
        REQUIRE(mie_test::write_file(victim, before));

        REQUIRE(run_capturing(args("decode", first.str(), victim, "-o", destination.str(),
                                   "--separate-errors"),
                              out, err) == mie::cli::EXIT_RUNTIME);
        REQUIRE(err.find("resolves to merge input") != std::string::npos);

        // Asserted on the artifact, not just the message.
        std::string after;
        REQUIRE(mie_test::read_file(victim, after));
        CHECK(after == before);
        std::string unused;
        CHECK_FALSE(mie_test::read_file(destination.str(), unused));
    }

    SECTION("a distinct output is allowed") {
        const TempPath destination("mie-cli-collide-out.csv");
        REQUIRE(mie::cli::run(args("decode", first.str(), second.str(), "-o", destination.str())) ==
                mie::cli::EXIT_OK);
    }

    SECTION("the single-input case is still guarded by the writer") {
        // Unchanged behaviour, asserted here so a future refactor cannot move
        // the merge guard in and drop the single-file one on the way past.
        REQUIRE(mie::cli::run(args("decode", first.str(), "-o", first.str())) != mie::cli::EXIT_OK);
    }
}

TEST_CASE("more inputs than the cap is refused up front", "[cli][L3-CPP-024]") {
    // Refused before anything is opened. The cap exists to keep resource use
    // predictable, so discovering it at file 257 would already have consumed
    // the descriptors it is meant to bound.
    std::string manifest_body;
    for (std::size_t i = 0; i <= mie::merge::MAX_MERGE_FILES; ++i) {
        manifest_body += "f" + mie::text::decimal(i) + ".mie\n";
    }
    const TempFile manifest("mie-cli-over-cap.txt", manifest_body);

    std::string out;
    std::string err;
    REQUIRE(run_capturing(args("decode", "--manifest", manifest.str()), out, err) ==
            mie::cli::EXIT_USAGE);
    REQUIRE(err.find("too many input files") != std::string::npos);
}

TEST_CASE("a partial merge of inputs that all fail reports the first", "[cli][L2-MRG-004]") {
    // --allow-partial keeps what could be decoded. When nothing could be, the
    // run fails with the FIRST input's own error and writes nothing -- whether
    // every input fails at open, or a missing input precedes one that opens but
    // is not a recording (the first input's error, not the second's).
    const TempPath missing("mie-cli-partial-missing.mie");
    const TempFile junk("mie-cli-partial-junk.mie", std::vector<uint8_t>(4096, 0xFF));
    TempPath output("mie-cli-partial-out.csv");
    const std::string partial = output.sibling(".partial");

    std::string out;
    std::string err;
    SECTION("every input missing") {
        REQUIRE(run_capturing(args("decode", missing.str(), missing.str(), "-o", output.str(),
                                   "--allow-partial"),
                              out, err) == mie::cli::EXIT_RUNTIME);
    }
    SECTION("a missing input before one that is not a recording") {
        REQUIRE(run_capturing(args("decode", missing.str(), junk.str(), "-o", output.str(),
                                   "--allow-partial"),
                              out, err) == mie::cli::EXIT_RUNTIME);
    }
    REQUIRE(err.find("MIE file not found") != std::string::npos);
    REQUIRE_FALSE(mie::platform::path_exists(output.str()));
    REQUIRE_FALSE(mie::platform::path_exists(partial));
}

TEST_CASE("a partial merge names the input it left out", "[cli][L2-MRG-004]") {
    // A missing input beside a good one: the run commits a .partial, and the
    // closing WARN says an input was left out rather than reporting a sync loss
    // that never occurred. On stdout there is no .partial to commit, so the
    // same run fails with the sync-loss exit class and the same words.
    const TempPath missing("mie-cli-leftout-missing.mie");
    const TempFile good("mie-cli-leftout-good.mie", valid_recording());
    const std::string said = "1 of 2 merge inputs could not be read and was left out";

    std::string out;
    std::string err;
    SECTION("to a file") {
        TempPath output("mie-cli-leftout-out.csv");
        const std::string partial = output.sibling(".partial");
        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        REQUIRE(run_capturing(args("decode", missing.str(), good.str(), "-o", output.str(),
                                   "--allow-partial", "--input-time-format", "irig"),
                              out, err) == mie::cli::EXIT_OK);
        REQUIRE(mie::platform::path_exists(partial));
        REQUIRE(capture.count_containing(said) == 1u);
        REQUIRE(capture.count_containing("sync loss") == 0u);
    }
    SECTION("to stdout") {
        REQUIRE(run_capturing(args("decode", missing.str(), good.str(), "--allow-partial",
                                   "--input-time-format", "irig"),
                              out, err) == mie::cli::EXIT_SYNC_LOSS);
        REQUIRE(err.find("Error: " + said) != std::string::npos);
    }
}

TEST_CASE("exit codes classify the failure", "[cli][L3-CPP-016]") {
    SECTION("a missing input is a runtime error") {
        const TempPath absent("mie-cli-absent.mie");
        REQUIRE(mie::cli::run(args("decode", absent.str(), "-o", "out.csv")) ==
                mie::cli::EXIT_RUNTIME);
        // count agrees with decode: a script's "is this readable?" check must
        // not depend on which subcommand it happened to use.
        REQUIRE(mie::cli::run(args("count", absent.str())) == mie::cli::EXIT_RUNTIME);
    }

    SECTION("a file that is not a recording is exit 2 on both subcommands") {
        std::vector<uint8_t> junk;
        junk.reserve(512);
        for (std::size_t i = 0; i < 512; ++i) {
            junk.push_back(static_cast<uint8_t>(0xA5));
        }
        const TempFile input("mie-cli-junk.mie", junk);
        const TempPath out("mie-cli-junk.csv");
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str())) ==
                mie::cli::EXIT_NO_RECORDS);
        REQUIRE(mie::cli::run(args("count", input.str())) == mie::cli::EXIT_NO_RECORDS);
    }

    SECTION("a missing config file is exit 5") {
        const TempPath absent("mie-cli-absent.toml");
        REQUIRE(mie::cli::run(args("--config", absent.str(), "count", "in.mie")) ==
                mie::cli::EXIT_CONFIG);
    }

    SECTION("malformed config is exit 5") {
        const TempFile config("mie-cli-bad.toml", std::string("[decode\nstrict = true\n"));
        REQUIRE(mie::cli::run(args("--config", config.str(), "count", "in.mie")) ==
                mie::cli::EXIT_CONFIG);
    }

    // L1-EXIT-007: an unsupported --format is an invalid FLAG VALUE, so it is
    // a USAGE error. It was EXIT_RUNTIME through v2.14.0 because the check ran
    // after the config merge rather than at parse time -- which put one
    // mistake on three different exit codes depending on where it was written
    // (4 for --input-time-format, 1 for --format, 5 for the same value in a config
    // file). The config-file spelling is still 5; only this one moved.
    SECTION("an unsupported output format is a usage error") {
        const TempFile input("mie-cli-fmt.mie", valid_recording());
        const TempPath out("mie-cli-fmt.csv");
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(), "--format",
                                   "parquet")) == mie::cli::EXIT_USAGE);
        // Both spellings, since the joined form takes a different parse path.
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(), "--format=parquet")) ==
                mie::cli::EXIT_USAGE);
        // csv is still accepted.
        REQUIRE(mie::cli::run(args("decode", input.str(), "-o", out.str(), "--format", "csv")) ==
                mie::cli::EXIT_OK);
    }
}

TEST_CASE("count writes the integer alone to stdout", "[cli][L3-CPP-017]") {
    // `n=$(aero1553 count x.mie)` must not need to strip prose, so the
    // human-readable sentence goes to stderr and stdout carries the number and
    // a newline -- nothing else.
    const TempFile input("mie-cli-count.mie", valid_recording());
    std::string out;
    REQUIRE(run_capturing_stdout(args("count", input.str()), out) == mie::cli::EXIT_OK);
    REQUIRE(out == "2\n");
}

TEST_CASE("config supplies defaults that the CLI overrides", "[cli][L3-CPP-018]") {
    const TempFile input("mie-cli-prec.mie", valid_recording());

    SECTION("a presence-only flag left off does not clobber the config value") {
        // The trap this pins: contributing `false` for an absent flag would make
        // `--no-clobber`'s absence mean "allow overwriting", silently undoing a
        // safety setting the operator had put in their config file.
        const TempFile config("mie-cli-clobber.toml", std::string("[output]\nno_clobber = true\n"));
        const TempPath out("mie-cli-clobber.csv");

        REQUIRE(mie::cli::run(args("--config", config.str(), "decode", input.str(), "-o",
                                   out.str())) == mie::cli::EXIT_OK);
        // Second run over the same destination: no_clobber from the config must
        // still be in force even though no flag was passed either time.
        REQUIRE(mie::cli::run(args("--config", config.str(), "decode", input.str(), "-o",
                                   out.str())) != mie::cli::EXIT_OK);
    }

    SECTION("a config value is used when no flag contradicts it") {
        const TempFile config(
            "mie-cli-detect.toml",
            std::string("[decode]\ndetect_records = 4\ninput_time_format = \"irig\"\n"));
        const TempPath out("mie-cli-detect.csv");
        REQUIRE(mie::cli::run(args("--config", config.str(), "decode", input.str(), "-o",
                                   out.str())) == mie::cli::EXIT_OK);
    }

    SECTION("an invalid config value is exit 5, and the CLI equivalent is exit 4") {
        // Same bad number, two sources, two different exit codes -- which is
        // the distinction that tells an operator where to go and fix it.
        const TempFile config("mie-cli-range.toml", std::string("[decode]\ndetect_records = 99\n"));
        REQUIRE(mie::cli::run(args("--config", config.str(), "count", input.str())) ==
                mie::cli::EXIT_CONFIG);
        REQUIRE(mie::cli::run(args("decode", input.str(), "--detect-records", "99")) ==
                mie::cli::EXIT_USAGE);
    }
}

TEST_CASE("--separate-errors on stdout forces inline, and says so", "[cli][L3-CPP-017]") {
    // You cannot split stdout, so the flag has to be ignored there. Ignoring it
    // SILENTLY is the problem: an operator who asked for a separate errors file
    // and got one combined stream would only discover it by reading the output.
    const TempFile input("mie-cli-split-stdout.mie", valid_recording());
    const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
    std::string out;
    std::string err;
    // No --output at all: that is how stdout is selected.
    REQUIRE(run_capturing(args("decode", input.str(), "--separate-errors"), out, err) ==
            mie::cli::EXIT_OK);
    REQUIRE(out.find("TIME_STAMP") == 0);

    bool warned = false;
    const std::vector<std::string>& lines = mie_test::captured_lines();
    for (std::size_t i = 0; i < lines.size(); ++i) {
        if (lines[i].find("forces inline error mode") != std::string::npos) {
            warned = true;
        }
    }
    REQUIRE(warned);
}

TEST_CASE("omitting --output writes stdout", "[cli][L2-CLI-002]") {
    // stdout is selected by leaving the flag off. That is the ONLY way to
    // select it, in all three implementations.
    const TempFile input("mie-cli-stdout-dest.mie", valid_recording());
    std::string out;
    REQUIRE(run_capturing_stdout(args("decode", input.str()), out) == mie::cli::EXIT_OK);
    REQUIRE(out.find("TIME_STAMP") == 0);
    // A single trailing newline, not a CRLF pair, on every host (L2-WRT-012).
    REQUIRE(out.find("\r\n") == std::string::npos);
}

TEST_CASE("--output takes a path, and no value of it is magic", "[cli][L2-CLI-002]") {
    // `-o -` writes a file CALLED `-`. It used to mean stdout in this build and
    // nowhere else, which the surface-parity gate could not catch: that gate
    // compares flag names, not what their values mean.
    //
    // The convention is real, but so is the trap -- and since omitting `-o`
    // already writes stdout, the special case only added a second spelling for
    // something that worked, at the cost of making one filename unreachable.
    const TempFile input("mie-cli-dash-dest.mie", valid_recording());

    // Written beside the input so the odd name is cleaned up with it.
    const std::string dash = input.str() + "-dash";
    TempPath destination("mie-cli-dash-holder");
    const std::string literal_dash = destination.also_remove(dash);

    std::string out;
    REQUIRE(run_capturing_stdout(args("decode", input.str(), "-o", literal_dash), out) ==
            mie::cli::EXIT_OK);
    // Nothing on stdout: it all went to the file.
    REQUIRE(out.empty());

    std::string written;
    REQUIRE(mie_test::read_file(literal_dash, written));
    REQUIRE(written.find("TIME_STAMP") == 0);
}
