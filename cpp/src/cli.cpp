// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::cli"

#include "mie/cli.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <memory>

#include "mie/config.hpp"
#include "mie/dump.hpp"
#include "mie/error.hpp"
#include "mie/filter.hpp"
#include "mie/log.hpp"
#include "mie/merge.hpp"
#include "mie/order.hpp"
#include "mie/platform.hpp"
#include "mie/reader.hpp"
#include "mie/text.hpp"
#include "mie/writer.hpp"

namespace mie {
namespace cli {

namespace {

const char* const kVersion = "4.0.0";

// The help text, character for character the Rust implementation's `HELP`
// in rust/src/cli.rs, which is the canonical one: help is part of what
// every implementation must print alike (L2-CLI-022), and
// tests/conformance/cli_message_parity.py compares the two byte for byte.
// Edit both together. This one was its own 75-line text until then.
const char* const kHelp =
    "aero1553 -- DDC MIL-STD-1553 MIE binary decoder\n"
    "\n"
    "USAGE:\n"
    "  aero1553 [--log-level L] [--config PATH] <command> [options]\n"
    "\n"
    "COMMANDS:\n"
    "  decode <INPUT>... Decode MIE file(s) to CSV (2+ inputs -> time-sorted merge)\n"
    "  count  <INPUT>    Print message count (no CSV)\n"
    "  dump   <INPUT>    Hex dump (raw or record-aware)\n"
    "\n"
    "GLOBAL OPTIONS:\n"
    "  --log-level LEVEL                     DEBUG|INFO|WARNING|WARN|ERROR|\n"
    "                                        CRITICAL|OFF (default WARNING;\n"
    "                                        case-insensitive; CRITICAL/OFF silence)\n"
    "  --config PATH                         TOML configuration file\n"
    "  --no-irig-day-advisory                Never emit the one-time IRIG\n"
    "                                        day-of-year advisory. It is logged at\n"
    "                                        INFO, so it is already silent at the\n"
    "                                        default level; this suppresses it at\n"
    "                                        INFO/DEBUG too (L2-LOG-001)\n"
    "  -V, -v, --version                     Print version and exit\n"
    "  -h, --help                            Print this help and exit\n"
    "\n"
    "DECODE OPTIONS:\n"
    "  -o, --output PATH                     Output CSV (default stdout)\n"
    "  --manifest PATH                       Read input paths from a file (one per\n"
    "                                        line; blank/#-comment lines ignored).\n"
    "                                        Mutually exclusive with positionals /\n"
    "                                        --glob (L2-MRG-001)\n"
    "  --glob PATTERN                        Expand a single-directory *|? filename\n"
    "                                        glob (no recursion). Mutually exclusive\n"
    "                                        with positionals / --manifest\n"
    "  --separate-errors                     Route errored/spurious records to a\n"
    "                                        separate <stem>_errors.csv. Default:\n"
    "                                        every record inline in the main CSV\n"
    "                                        with ERROR/ERROR_CODE populated\n"
    "  --no-clobber                          Refuse to overwrite an existing\n"
    "                                        output file (L2-WRT-017)\n"
    "  --allow-partial                       On unrecoverable mid-file sync\n"
    "                                        loss, write a <output>.partial\n"
    "                                        file and exit 0 instead of 3\n"
    "                                        (L1-EXIT-004)\n"
    "  --input-time-format auto|irig|standard\n"
    "                                        How timestamps are PARSED from the\n"
    "                                        file. Default auto (case-insensitive)\n"
    "  --output-time-format doy|iso|dom      How TIME_STAMP is WRITTEN to the CSV.\n"
    "                                        doy (default) DAY:HH:MM:SS.uuuuuu, the\n"
    "                                        vendor rendering; iso\n"
    "                                        YYYY-MM-DDTHH:MM:SS.uuuuuu plus zone;\n"
    "                                        dom DD:HH:MM:SS.uuuuuu. iso and dom\n"
    "                                        need a year. L2-WRT-025\n"
    "  --year YYYY                           Calendar year used to resolve the IRIG\n"
    "                                        day-of-year field (range 1..=9999). An\n"
    "                                        MIE file carries no year, so iso and\n"
    "                                        dom require one; doy ignores it.\n"
    "                                        L2-WRT-026\n"
    "  --utc-offset Z|+HH:MM|-HH:MM          Zone designator for the iso rendering\n"
    "                                        (default Z, meaning UTC). IRIG-B\n"
    "                                        carries no timezone, so this states\n"
    "                                        what the recording could not. L2-WRT-025\n"
    "  --detect-records N                    Records probed by timestamp-\n"
    "                                        format auto-detection (1..=32,\n"
    "                                        default 8). L2-DEC-015.\n"
    "  --lookahead-records N                 Total records checked by sync\n"
    "                                        validation per call (1 candidate\n"
    "                                        + N-1 look-ahead, range 1..=32,\n"
    "                                        default 2). L2-SYN-026.\n"
    "  --standard-tick-rate-hz HZ            Standard-counter frequency in Hz.\n"
    "                                        When set, Standard timestamps are\n"
    "                                        converted to microseconds and join\n"
    "                                        DELTA tracking. Must be > 0\n"
    "                                        (default: unset -> empty DELTA for\n"
    "                                        Standard). L2-DEC-017.\n"
    "  --strict                              Raise on invalid records\n"
    "  --format csv                          Output format (csv only at present)\n"
    "  --no-mux                              Leave the MUX column empty\n"
    "                                        (vendor-exact). Default: MUX is\n"
    "                                        derived from the file name (L2-WRT-020)\n"
    "  --mux-delimiter D                     MUX field separator (default '.')\n"
    "  --mux-field N                         0-based MUX field index; negative\n"
    "                                        counts from the end (default 4)\n"
    "  --collapse-duplicates                 Collapse the same bus transaction seen\n"
    "                                        by multiple recorders into one row\n"
    "                                        (multi-file merge only). Default: off\n"
    "  --collapse-window-us N                Timestamp tolerance in microseconds for\n"
    "                                        collapsing (default 0 = exact match)\n"
    "  --delta-scope per-file|global         Scope DELTA is measured over in a\n"
    "                                        multi-file merge (default per-file:\n"
    "                                        each gap is to the previous same-key\n"
    "                                        record from its OWN file, matching a\n"
    "                                        single-file decode). global measures\n"
    "                                        across the merged timeline. No effect\n"
    "                                        on a single input. L2-MRG-005.\n"
    "  --max-sort-group N                    Max consecutive same-TIME_STAMP records\n"
    "                                        buffered to order rows by RT then MSG\n"
    "                                        (range 1..=1048576, default 65536). Use 1\n"
    "                                        to disable reordering and emit raw\n"
    "                                        capture order. L2-WRT-022.\n"
    "  --max-collapse-survivors N            Max records the --collapse-duplicates\n"
    "                                        window retains at once (range\n"
    "                                        1..=1048576, default 4096). Bounds the\n"
    "                                        set by COUNT where the window bounds it\n"
    "                                        by TIME. Past the cap collapsing is\n"
    "                                        best-effort, with one WARN. L2-MRG-008.\n"
    "  --exclude-types VAL                   Comma-separated names or 0xNN\n"
    "  --exclude-rts VAL                     Comma-separated RT addresses\n"
    "  --exclude-buses VAL                   Comma-separated A|B\n"
    "  --exclude-subaddresses VAL            Comma-separated subaddresses\n"
    "  --include-types VAL                   (same syntax as --exclude-types)\n"
    "  --include-rts VAL\n"
    "  --include-buses VAL\n"
    "  --include-subaddresses VAL\n"
    "\n"
    "  Filter flags accept ONE value (comma-separable). Repeat the flag to\n"
    "  accumulate. `--include-rts 15,31` and `--include-rts 15 --include-rts 31`\n"
    "  are equivalent. Appending `=value` (e.g. `--include-rts=15`) also works.\n"
    "\n"
    "DUMP OPTIONS:\n"
    "  --raw                                 Raw hex dump (no record parsing)\n"
    "  --offset N                            Start offset (decimal or 0xHEX)\n"
    "  --length N                            Bytes to dump, raw mode (decimal or 0xHEX)\n"
    "  --records N                           Max records, record mode (decimal or 0xHEX)\n"
    "\n"
    "EXAMPLES:\n"
    "  aero1553 decode rec.mie -o out.csv\n"
    "  aero1553 decode rec.mie --separate-errors --include-rts 15\n"
    "  aero1553 decode a.mie b.mie c.mie -o merged.csv   # time-sorted merge\n"
    "  aero1553 decode --glob 'recordings/*.mie' -o merged.csv\n"
    "  aero1553 count rec.mie\n"
    "  aero1553 dump rec.mie --records 10\n";

/// The line every usage error ends with, in place of the help text that
/// used to follow one and scroll the error itself off the screen. Rust's
/// `USAGE_HINT` (L2-CLI-022).
const char* const kUsageHint = "Run 'aero1553 --help' for usage.";

/// A failure carrying the exit code it maps to, so a config problem (5) is not
/// flattened into a generic runtime error (1).
struct CliError {
    int code;
    std::string message;

    CliError(int code_, const std::string& message_) : code(code_), message(message_) {}
};

CliError usage_error(const std::string& message) { return CliError(EXIT_USAGE, message); }

/// An option the subcommand does not have, as typed -- `--no-mux=true`
/// reports itself in full -- with any byte outside printable ASCII escaped
/// (L2-CLI-014). Rust's `unknown_option`; C++ said `unknown option X` for two
/// of the three subcommands and named the subcommand for the third.
std::string unknown_option(const char* command, const std::string& token) {
    return std::string("unknown ") + command + " option: " + text::escape_bytes(token);
}

/// Thrown by a subcommand parser when it sees `-h`/`--help`.
///
/// Help is a SUCCESSFUL outcome, not an error, so it cannot travel as a
/// CliError -- run() prints "Error: ..." for those and would turn help into
/// a diagnostic. Mirrors Rust's `ParseError::HelpRequested`.
struct HelpRequested {};

// Defined here rather than beside run(): the subcommand parsers need
// is_help_flag, and in C++ a name must be declared before it is used.
bool is_version_flag(const std::string& arg) {
    return arg == "-V" || arg == "-v" || text::equals_ignoring_ascii_case(arg, "--version");
}

/// Note the asymmetry with `is_version_flag`, which IS case-insensitive:
/// `--VERSION` is a version request in all three implementations, `--HELP`
/// is not a help request in any of them. This accepted `--HELP`, which made
/// C++ the only implementation that did.
bool is_help_flag(const std::string& arg) { return arg == "-h" || arg == "--help"; }

CliError config_error(const std::string& message) { return CliError(EXIT_CONFIG, message); }
CliError runtime_error_(const std::string& message) { return CliError(EXIT_RUNTIME, message); }

/// Map a decode-time MieError to its exit class.
///
/// Shared by `decode` and `count` so the two agree: a wrong-file rejection is
/// exit 2 on both. Rust records that `count` once flattened every reader error
/// to exit 1, which made a script's "is this an MIE file?" check depend on
/// which subcommand it happened to use.
int exit_code_for(const MieError& error) {
    switch (error.kind()) {
        case KIND_NO_VALID_RECORDS:
        case KIND_HOMOGENEOUS_PAYLOAD:
        case KIND_TIMESTAMP_FORMAT_MISMATCH:
        case KIND_CALENDAR_UNAVAILABLE: return EXIT_NO_RECORDS;
        default: return EXIT_RUNTIME;
    }
}

bool write_out(std::FILE* stream, const std::string& text) {
    if (stream == nullptr) {
        return false;
    }
    const bool ok = std::fwrite(text.data(), 1, text.size(), stream) == text.size();
    // Flushed here rather than at exit: `count` writes its integer to `out` and
    // its sentence to `err`, and a caller reading both wants them complete the
    // moment run() returns, not whenever the CRT gets around to it.
    (void)std::fflush(stream);
    return ok;
}

// ---------------------------------------------------------------------------
// Argument cursor
// ---------------------------------------------------------------------------

/// A cursor over the argument list that understands `--flag value` and
/// `--flag=value` as one thing.
///
/// The Rust parser spells both forms out at every flag, which is twenty-odd
/// near-identical match arms and twenty-odd chances for one of them to drift.
/// Handling it once here is the same behaviour with one place to be wrong.
/// Does this token begin like a number? Python 3.14 argparse's `-\.?\d`,
/// applied as a PREFIX test the way re.match applies it -- a dash, an optional
/// decimal point, then a digit. Hand-rolled because <regex> is banned outright
/// here (libstdc++ had none until GCC 4.9, see ADR-0001).
///
/// This deliberately accepts more than "is a number": "-5e3", "-0x5" and "-1a"
/// all begin like one and are therefore values. That is the point -- such a
/// token is far likelier to be a mistyped number than a flag, and letting it
/// through means the flag's OWN validator reports it, so `--mux-field -1a`
/// says `invalid --mux-field: "-1a"; must be an integer` rather than the less helpful "the
/// next argument is an option".
///
/// THIS RULE IS VERSION-DEPENDENT IN PYTHON AND WE PIN THE NEWER ONE. Through
/// 3.13 argparse used an anchored full match, `^-\d+$|^-\d*\.\d+$`, so only
/// plain decimals were exempt; 3.14 replaced it with the prefix test above.
/// This project supports Python 3.10 through 3.14, so argparse does not agree
/// with itself across the supported range and no choice here can match all of
/// them. We follow 3.14: simpler, where Python is going, and the more
/// permissive of the two, so adopting it cannot newly reject an invocation
/// that used to work. The shapes it disagrees with 3.10-3.13 about are
/// consequently outside the specified contract (L2-CLI-015) and are kept out
/// of the conformance suite.
bool starts_like_a_number(const std::string& token) {
    if (token.size() < 2 || token[0] != '-') {
        return false;
    }
    // The decimal point is optional; a digit after it is not.
    const std::string::size_type at = (token[1] == '.') ? 2 : 1;
    return at < token.size() && token[at] >= '0' && token[at] <= '9';
}

/// Does this token look like an option rather than a value?
///
/// Decides whether the SEPARATED form may consume the next token. Without
/// this, `--mux-delimiter --no-mux` set the delimiter to the string
/// "--no-mux" and the `--no-mux` flag silently never ran -- a wrong decode
/// that exited 0. Refusing turns that into a usage error, which is the point:
/// the failure was silent, not merely inconsistent.
///
/// The rule is argparse's, deliberately, because Python has always behaved
/// this way and the alternative was to make two implementations agree with
/// each other and disagree with the third. Its three exemptions each keep a
/// real invocation working:
///
///   - a lone "-" is a value, so `-o -` writes a file named "-" (L2-CLI-005);
///   - anything beginning like a number is a value (see
///     starts_like_a_number), so `--mux-field -1` (a documented feature:
///     negative indices count from the end) still parses, and
///     `--collapse-window-us -5` still reaches its own validator to be
///     refused for being negative rather than for looking like a flag;
///   - a token containing a space is a value, since no option is spelled so.
///
/// The JOINED form is unaffected: `--mux-delimiter=--no-mux` is unambiguous
/// and stays legal, which is why it is the documented way to pass a value
/// that looks like a flag.
bool looks_like_option(const std::string& token) {
    if (token.size() < 2 || token[0] != '-') {
        return false;
    }
    if (token.find(' ') != std::string::npos) {
        return false;
    }
    return !starts_like_a_number(token);
}

class ArgReader {
  public:
    explicit ArgReader(const std::vector<std::string>& args) : args_(args), at_(0) {}

    bool at_end() const { return at_ >= args_.size(); }
    const std::string& peek() const { return args_[at_]; }
    void advance() { at_ += 1; }

    /// Abandon every remaining argument.
    ///
    /// Used when a flag cannot take its value. From that point the command
    /// line is uninterpretable -- nothing downstream can be attributed to
    /// the right flag -- so nothing in it may be honoured, including a `-h`.
    /// `help_pending` then answers false, which is what keeps
    /// `--log-level -h` a usage error.
    void abandon_rest() { at_ = args_.size(); }

    /// Is a help request still pending in the unconsumed arguments?
    ///
    /// Bounded by `--`: after the end-of-options separator a `-h` is a path
    /// and not a request (L2-CLI-016).
    bool help_pending() const {
        for (std::size_t i = at_; i < args_.size(); ++i) {
            if (args_[i] == "--") {
                return false;
            }
            if (is_help_flag(args_[i])) {
                return true;
            }
        }
        return false;
    }

    /// Consume the current token if it is exactly `name`.
    bool take_flag(const char* name) {
        if (at_end() || args_[at_] != name) {
            return false;
        }
        at_ += 1;
        return true;
    }

    /// Consume `name value` or `name=value`, putting the value in `out`.
    ///
    /// Throws when the separated form runs off the end: a trailing
    /// `--output` with nothing after it is a usage error, not an empty path.
    bool take_value(const char* name, std::string& out) {
        if (at_end()) {
            return false;
        }
        const std::string& token = args_[at_];
        const std::string prefix = std::string(name) + "=";
        // `>=`, not `>`: `--flag=` is the flag carrying an EMPTY value, not an
        // unknown option. Rust and Python both hand the empty string to the
        // flag's own validator and let it decide -- `--exclude-rts=` is an
        // empty filter and decodes fine, while `--mux-delimiter=` is rejected
        // as non-empty-required. With `>` this branch could never yield an
        // empty value, so C++ answered "unknown option" (exit 4) where the
        // other two exited 0. `compare` clamps its length, so a token shorter
        // than the prefix still compares unequal.
        if (token.size() >= prefix.size() && token.compare(0, prefix.size(), prefix) == 0) {
            out = token.substr(prefix.size());
            at_ += 1;
            return true;
        }
        if (token != name) {
            return false;
        }
        at_ += 1;
        if (at_end()) {
            abandon_rest();
            throw usage_error(std::string(name) + " requires a value");
        }
        if (looks_like_option(args_[at_])) {
            // Hard stop: see abandon_rest(). A later `-h` must not rescue this.
            const std::string offender = text::escape_bytes(args_[at_]);
            abandon_rest();
            throw usage_error(std::string(name) + " requires a value, but the next argument is" +
                              " an option: " + offender + "; to pass it as a value, write " + name +
                              "=" + offender);
        }
        out = args_[at_];
        at_ += 1;
        return true;
    }

    /// `take_value` for a flag with a short alias, e.g. `-o` / `--output`.
    bool take_value(const char* short_name, const char* name, std::string& out) {
        if (!at_end() && args_[at_] == short_name) {
            at_ += 1;
            if (at_end()) {
                abandon_rest();
                throw usage_error(std::string(name) + " requires a value");
            }
            if (looks_like_option(args_[at_])) {
                // Hard stop: see abandon_rest().
                const std::string offender = text::escape_bytes(args_[at_]);
                abandon_rest();
                throw usage_error(std::string(name) + " requires a value, but the next argument" +
                                  " is an option: " + offender + "; to pass it as a value," +
                                  " write " + name + "=" + offender);
            }
            out = args_[at_];
            at_ += 1;
            return true;
        }
        return take_value(name, out);
    }

  private:
    std::vector<std::string> args_;
    std::size_t at_;
};

// ---------------------------------------------------------------------------
// Value parsers
// ---------------------------------------------------------------------------

/// An integer flag value. Surrounding ASCII whitespace is ignored, on every
/// numeric flag and in both implementations (Rust trims with `trim_ascii`).
/// Rejects trailing junk, which `atoi` accepts, and a value outside int64_t,
/// which `strtoll` saturated to the nearest bound and accepted.
///
/// The messages are Rust's, word for word (L2-CLI-022): `invalid FLAG: "x";
/// must be an integer`, and `invalid FLAG: N; valid range: [LO, HI]` from
/// `parse_ranged`. C++ had its own for each, and an empty value had a third.
int64_t parse_integer(const std::string& text, const char* flag) {
    int64_t value = 0;
    if (!text::parse_int64(text::trim_ascii_whitespace(text), value)) {
        throw usage_error(std::string("invalid ") + flag + ": " + text::quote(text) +
                          "; must be an integer");
    }
    return value;
}

std::size_t parse_ranged(const std::string& text, const char* flag, std::size_t lo,
                         std::size_t hi) {
    const int64_t value = parse_integer(text, flag);
    if (value < static_cast<int64_t>(lo) || value > static_cast<int64_t>(hi)) {
        throw usage_error(std::string("invalid ") + flag + ": " + text::decimal_signed(value) +
                          "; valid range: [" + text::decimal(static_cast<uint64_t>(lo)) + ", " +
                          text::decimal(static_cast<uint64_t>(hi)) + "]");
    }
    return static_cast<std::size_t>(value);
}

/// L2-CLI-019: what `--time-format` says now that it has been split in two.
///
/// The message names both replacements and states which concern each covers,
/// because the operator's intent is still expressible and the only open
/// question is which of the two they meant -- exactly what a generic "unknown
/// option" cannot answer.
const char* const kRetiredTimeFormatMessage =
    "--time-format was split in v3.0.0 and is no longer accepted. Use "
    "--input-time-format auto|irig|standard to choose how timestamps are PARSED "
    "from the file, or --output-time-format doy|iso|dom to choose how they are "
    "WRITTEN to the CSV.";

/// L2-CLI-018: `--year YYYY`, range-checked at parse time so a bad value is a
/// usage error rather than a malformed cell far into the output.
int parse_year_argument(const std::string& text) {
    return static_cast<int>(parse_ranged(text, "--year", static_cast<std::size_t>(YEAR_MIN),
                                         static_cast<std::size_t>(YEAR_MAX)));
}

double parse_tick_rate(const std::string& text) {
    // Rust's f64 grammar first: strtod alone also takes hexadecimal floats
    // (`0x10`, `0x1p4`) that Rust refuses.
    const std::string trimmed = text::trim_ascii_whitespace(text);
    if (!text::is_rust_float_literal(trimmed)) {
        throw usage_error("invalid --standard-tick-rate-hz: " + text::quote(text) +
                          "; must be a number");
    }
    const double value = std::strtod(trimmed.c_str(), nullptr);
    // Finite AND positive, as L2-CLI-012 requires. `inf` and `1e400` (which
    // overflows to infinity) used to pass the positivity test alone; NaN
    // fails it, since every comparison with NaN is false.
    // Repeated as written, as the config loader does (L2-CLI-022).
    if (!(value > 0.0) || !std::isfinite(value)) {
        throw usage_error("invalid --standard-tick-rate-hz: " + trimmed +
                          "; must be a finite value greater than 0");
    }
    return value;
}

/// Split a comma-separated filter list, dropping empty pieces so a trailing
/// comma is tolerated rather than producing an unnamed element.
std::vector<std::string> split_csv(const std::string& text) {
    std::vector<std::string> out;
    std::string current;
    for (std::size_t i = 0; i < text.size(); ++i) {
        if (text[i] == ',') {
            const std::string piece = text::trim_ascii_whitespace(current);
            if (!piece.empty()) {
                out.push_back(piece);
            }
            current.clear();
        } else {
            current += text[i];
        }
    }
    const std::string piece = text::trim_ascii_whitespace(current);
    if (!piece.empty()) {
        out.push_back(piece);
    }
    return out;
}

/// Parse a filter-list element with a parser shared with the config loader,
/// reporting failure as a USAGE error that names the flag.
///
/// `parse_type_name` / `parse_bus_name` signal with ConfigError, which is exit
/// 5. Reached through a CLI flag the same bad value is exit 4 -- the operator
/// mistyped an argument, not their config file -- and the message should say
/// which flag rather than leaving them to guess. Without this the ConfigError
/// also escaped every handler in `run()` and aborted the process.
uint8_t parse_type_flag(const std::string& text, const char* flag) {
    try {
        return parse_type_name(text);
    } catch (const ConfigError& error) {
        throw usage_error(std::string(flag) + ": " + error.message());
    }
}

Bus parse_bus_flag(const std::string& text, const char* flag) {
    try {
        return parse_bus_name(text);
    } catch (const ConfigError& error) {
        throw usage_error(std::string(flag) + ": " + error.message());
    }
}

/// A flag value that must be a non-negative integer, in decimal or as `0x...`.
///
/// Unbounded above but for the type: a byte offset into a recording and a
/// record count are both as large as the file allows, so there is no ceiling to
/// impose that would not be arbitrary. The type is uint64_t, as Rust's is, so
/// everything up to 2^64 - 1 is accepted and anything beyond it refused. The
/// hexadecimal form is for offsets read off a hex dump (L2-CLI-020).
uint64_t parse_non_negative(const std::string& text_value, const char* flag) {
    const std::string trimmed = text::trim_ascii_whitespace(text_value);
    uint64_t value = 0;
    if (text::parse_uint64(trimmed, value) || text::parse_hex_uint64(trimmed, value)) {
        return value;
    }
    // Not an unsigned number: a negative one gets the specific complaint.
    const int64_t signed_value = parse_integer(text_value, flag);
    throw usage_error(std::string("invalid ") + flag + ": " + text::decimal_signed(signed_value) +
                      "; must be a non-negative integer");
}

/// A filter list element that must be a 0-31 wire field, in decimal or as
/// `0x...` -- Rust has always taken both here (L2-CLI-020).
uint8_t parse_small(const std::string& text, const char* flag) {
    const uint64_t value = parse_non_negative(text, flag);
    if (value > 31) {
        throw usage_error(std::string("invalid ") + flag + ": " + text::decimal(value) +
                          "; valid range: [0, 31]");
    }
    return static_cast<uint8_t>(value);
}

// ---------------------------------------------------------------------------
// Parsed arguments
// ---------------------------------------------------------------------------

struct GlobalArgs {
    Optional<std::string> config;
    Optional<std::string> log_level;
    /// `--no-irig-day-advisory`. Absent leaves the config-file value (or the
    /// default) in place, per L2-CFG-003 precedence.
    Optional<bool> irig_day_advisory;

    GlobalArgs() = default;
};

struct DecodeArgs {
    /// Positional inputs. Mutually exclusive with `manifest` and `glob`
    /// (L2-MRG-001): each is a complete way of naming the input set, and
    /// combining two would leave the ORDER of the result undefined.
    std::vector<std::string> inputs;
    Optional<std::string> manifest;
    Optional<std::string> glob;
    Optional<std::string> output;
    ConfigOverrides overrides;

    DecodeArgs() = default;
};

/// Parse everything after `decode`.
DecodeArgs parse_decode(ArgReader& reader) {
    DecodeArgs args;
    std::string value;
    // POSIX end-of-options: the FIRST `--` is dropped and everything after
    // it is a path, however it is spelled. A later `--` is an ordinary
    // positional, which is the only way to name a file called `--`.
    bool end_of_options = false;

    while (!reader.at_end()) {
        const std::string token = reader.peek();

        if (end_of_options) {
            args.inputs.push_back(token);
            reader.advance();
            continue;
        }
        if (token == "--") {
            end_of_options = true;
            reader.advance();
            continue;
        }
        if (is_help_flag(token)) {
            throw HelpRequested();
        }

        if (reader.take_value("-o", "--output", value)) {
            args.output = value;
        } else if (reader.take_flag("--separate-errors")) {
            // Presence-only. Absence must NOT contribute `inline`, or it would
            // clobber a `separate` the operator set in their config file.
            args.overrides.error_mode = ERROR_MODE_SEPARATE;
        } else if (reader.take_flag("--no-clobber")) {
            args.overrides.no_clobber = true;
        } else if (reader.take_flag("--allow-partial")) {
            args.overrides.allow_partial = true;
        } else if (reader.take_flag("--strict")) {
            args.overrides.strict = true;
        } else if (reader.take_flag("--no-mux")) {
            args.overrides.mux_enabled = false;
        } else if (reader.take_value("--manifest", value)) {
            args.manifest = value;
        } else if (reader.take_value("--glob", value)) {
            args.glob = value;
        } else if (reader.take_flag("--collapse-duplicates")) {
            args.overrides.collapse_duplicates = true;
        } else if (reader.take_value("--collapse-window-us", value)) {
            // Unbounded above: the tolerance is a physical property of how far
            // apart two recorders' clocks can be, not a resource limit.
            const int64_t window = parse_integer(value, "--collapse-window-us");
            if (window < 0) {
                throw usage_error("invalid --collapse-window-us: " + text::decimal_signed(window) +
                                  "; must be a non-negative integer");
            }
            args.overrides.collapse_window_us = static_cast<uint64_t>(window);
        } else if (reader.take_value("--delta-scope", value)) {
            DeltaScope scope = DELTA_SCOPE_PER_FILE;
            if (!delta_scope_from_name(value, scope)) {
                throw usage_error("invalid --delta-scope: " + text::quote(value) +
                                  "; valid: per-file, global");
            }
            args.overrides.delta_scope = scope;
        } else if (reader.take_value("--input-time-format", value)) {
            TimestampFormat format = TIMESTAMP_AUTO;
            if (!timestamp_format_from_name(value, format)) {
                throw usage_error("invalid --input-time-format: " + text::quote(value) +
                                  "; valid: auto, irig, standard");
            }
            args.overrides.input_time_format = format;
        } else if (reader.take_value("--output-time-format", value)) {
            OutputTimeFormat rendering = OUTPUT_TIME_DOY;
            if (!output_time_format_from_name(value, rendering)) {
                throw usage_error("invalid --output-time-format: " + text::quote(value) +
                                  "; valid: doy, iso, dom");
            }
            args.overrides.output_time_format = rendering;
        } else if (reader.take_value("--year", value)) {
            args.overrides.year = parse_year_argument(value);
        } else if (reader.take_value("--utc-offset", value)) {
            int minutes = 0;
            if (!parse_utc_offset(value, minutes)) {
                throw usage_error("invalid --utc-offset: " + text::quote(value) +
                                  "; valid: Z, or +HH:MM / -HH:MM with HH in [0, 23] and "
                                  "MM in [0, 59]");
            }
            args.overrides.utc_offset_minutes = minutes;
        } else if (reader.take_value("--time-format", value)) {
            // L2-CLI-019: `--time-format` was split in v3.0.0. This arm exists
            // so the diagnostic can name both replacements -- the generic
            // unknown-option path below would report only that the flag is
            // unrecognised, which is the one thing an operator hitting this
            // already knows.
            throw usage_error(kRetiredTimeFormatMessage);
        } else if (reader.take_value("--format", value)) {
            // L1-EXIT-007: validated here, with every other flag value, so an
            // unsupported format is a USAGE error (exit 4). It used to be
            // checked after the config merge, which made it a runtime error
            // (exit 1) and contradicted the requirement. The config-file
            // spelling stays a load-time error (exit 5, L2-CFG-010).
            if (value != "csv") {
                throw usage_error("invalid --format: " + text::quote(value) + "; valid: csv");
            }
            args.overrides.output_format = value;
        } else if (reader.take_value("--detect-records", value)) {
            args.overrides.detect_records =
                parse_ranged(value, "--detect-records", DETECT_RECORDS_MIN, DETECT_RECORDS_MAX);
        } else if (reader.take_value("--lookahead-records", value)) {
            args.overrides.lookahead_records = parse_ranged(
                value, "--lookahead-records", LOOKAHEAD_RECORDS_MIN, LOOKAHEAD_RECORDS_MAX);
        } else if (reader.take_value("--standard-tick-rate-hz", value)) {
            args.overrides.standard_tick_rate_hz = parse_tick_rate(value);
        } else if (reader.take_value("--max-sort-group", value)) {
            args.overrides.max_sort_group =
                parse_ranged(value, "--max-sort-group", MAX_SORT_GROUP_MIN, MAX_SORT_GROUP_MAX);
        } else if (reader.take_value("--max-collapse-survivors", value)) {
            args.overrides.max_collapse_survivors =
                parse_ranged(value, "--max-collapse-survivors", MAX_COLLAPSE_SURVIVORS_MIN,
                             MAX_COLLAPSE_SURVIVORS_MAX);
        } else if (reader.take_value("--mux-delimiter", value)) {
            if (value.empty()) {
                throw usage_error("invalid --mux-delimiter: must be a non-empty string");
            }
            args.overrides.mux_delimiter = value;
        } else if (reader.take_value("--mux-field", value)) {
            args.overrides.mux_field = parse_integer(value, "--mux-field");
        } else if (reader.take_value("--exclude-types", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.exclude_types.push_back(
                    parse_type_flag(items[i], "--exclude-types"));
            }
        } else if (reader.take_value("--include-types", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.include_types.push_back(
                    parse_type_flag(items[i], "--include-types"));
            }
        } else if (reader.take_value("--exclude-buses", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.exclude_buses.push_back(
                    parse_bus_flag(items[i], "--exclude-buses"));
            }
        } else if (reader.take_value("--include-buses", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.include_buses.push_back(
                    parse_bus_flag(items[i], "--include-buses"));
            }
        } else if (reader.take_value("--exclude-rts", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.exclude_rts.push_back(
                    parse_small(items[i], "--exclude-rts"));
            }
        } else if (reader.take_value("--include-rts", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.include_rts.push_back(
                    parse_small(items[i], "--include-rts"));
            }
        } else if (reader.take_value("--exclude-subaddresses", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.exclude_subaddresses.push_back(
                    parse_small(items[i], "--exclude-subaddresses"));
            }
        } else if (reader.take_value("--include-subaddresses", value)) {
            const std::vector<std::string> items = split_csv(value);
            for (std::size_t i = 0; i < items.size(); ++i) {
                args.overrides.filters.include_subaddresses.push_back(
                    parse_small(items[i], "--include-subaddresses"));
            }
        } else if (!token.empty() && token[0] == '-' && token != "-") {
            throw usage_error(unknown_option("decode", token));
        } else {
            args.inputs.push_back(token);
            reader.advance();
        }
    }

    // Exactly one input method. Checked here rather than at resolution so the
    // message names the combination the operator actually typed.
    int methods = 0;
    if (!args.inputs.empty()) {
        methods += 1;
    }
    if (args.manifest.has_value()) {
        methods += 1;
    }
    if (args.glob.has_value()) {
        methods += 1;
    }
    if (methods == 0) {
        throw usage_error("decode requires an input file (positional, --manifest, or --glob)");
    }
    if (methods > 1) {
        throw usage_error(
            "decode accepts only one input method: positional paths, --manifest, or --glob -- "
            "not a combination");
    }
    return args;
}

/// Everything after `dump`.
struct DumpArgs {
    std::string input;
    bool raw;
    std::size_t offset;
    Optional<std::size_t> length;
    Optional<uint64_t> records;

    DumpArgs() : raw(false), offset(0) {}
};

DumpArgs parse_dump(ArgReader& reader) {
    DumpArgs args;
    bool input_seen = false;
    bool end_of_options = false;
    std::string value;

    while (!reader.at_end()) {
        const std::string token = reader.peek();
        if (end_of_options) {
            if (input_seen) {
                throw usage_error("unexpected positional argument: " + token);
            }
            args.input = token;
            input_seen = true;
            reader.advance();
            continue;
        }
        if (token == "--") {
            end_of_options = true;
            reader.advance();
            continue;
        }
        if (is_help_flag(token)) {
            throw HelpRequested();
        }
        if (reader.take_flag("--raw")) {
            args.raw = true;
        } else if (reader.take_value("--offset", value)) {
            args.offset = static_cast<std::size_t>(parse_non_negative(value, "--offset"));
        } else if (reader.take_value("--length", value)) {
            args.length = static_cast<std::size_t>(parse_non_negative(value, "--length"));
        } else if (reader.take_value("--records", value)) {
            args.records = parse_non_negative(value, "--records");
        } else if (!token.empty() && token[0] == '-' && token != "-") {
            throw usage_error(unknown_option("dump", token));
        } else if (input_seen) {
            // dump reads ONE file. It is a diagnostic view of a specific byte
            // range, and there is no sensible way to show two at once -- so a
            // second path is a mistake, not an invitation to merge.
            throw usage_error("unexpected positional argument: " + token);
        } else {
            args.input = token;
            input_seen = true;
            reader.advance();
        }
    }

    if (!input_seen) {
        throw usage_error("dump requires an input file");
    }
    return args;
}

std::string parse_count(ArgReader& reader) {
    Optional<std::string> input;
    bool end_of_options = false;
    while (!reader.at_end()) {
        const std::string token = reader.peek();
        if (!end_of_options) {
            // See parse_decode for the end-of-options rule.
            if (token == "--") {
                end_of_options = true;
                reader.advance();
                continue;
            }
            if (is_help_flag(token)) {
                throw HelpRequested();
            }
            if (!token.empty() && token[0] == '-' && token != "-") {
                throw usage_error(unknown_option("count", token));
            }
        }
        // A second path is reported where it stands, as in Rust and as `dump`
        // does: collecting every path first and complaining afterwards let a
        // later unknown option be reported instead (L2-CLI-022).
        if (input.has_value()) {
            throw usage_error("unexpected positional argument: " + token);
        }
        input = token;
        reader.advance();
    }
    if (!input.has_value()) {
        throw usage_error("count requires an input file");
    }
    return input.value();
}

// ---------------------------------------------------------------------------
// Runners
// ---------------------------------------------------------------------------

void apply_log_level(const char* source, const std::string& value) {
    log::Level level = log::LEVEL_WARN;
    if (!log::level_from_name(value, level)) {
        throw usage_error(std::string("invalid ") + source + ": " + text::quote(value) +
                          "; valid: " + log::LEVEL_NAMES);
    }
    log::set_level(level);
}

DecoderConfig resolve_config(const GlobalArgs& globals) {
    DecoderConfig config;
    try {
        config = load_config(globals.config);
    } catch (const ConfigError& error) {
        throw config_error(error.message());
    }
    // The config file's level was validated at load time, so this cannot fail
    // for a file-sourced value.
    apply_log_level("[logging].level", config.log_level);
    // The CLI wins, and is applied last for that reason.
    if (globals.log_level.has_value()) {
        apply_log_level("--log-level", globals.log_level.value());
    }

    // L2-LOG-001, same precedence as the level above: config file first, CLI on
    // top. Applied here rather than through ReaderOptions so it covers decode,
    // count and dump from one place -- every subcommand reaches the reader
    // through this function.
    log::set_irig_day_advisory(globals.irig_day_advisory.has_value()
                                   ? globals.irig_day_advisory.value()
                                   : config.irig_day_advisory);
    return config;
}

ReaderOptions reader_options(const DecoderConfig& config) {
    ReaderOptions options;
    options.strict = config.strict;
    options.input_time_format = config.input_time_format;
    options.detect_records = config.detect_records;
    options.lookahead_records = config.lookahead_records;
    options.standard_tick_rate_hz = config.standard_tick_rate_hz;
    options.mux_enabled = config.mux_enabled;
    options.mux_delimiter = config.mux_delimiter;
    options.mux_field = config.mux_field;
    // Present only when a calendar rendering is actually in force, so a year
    // left in a site config does not change the reader's diagnostics for a
    // `doy` run (L2-WRT-026 clause 5).
    if (output_time_format_needs_calendar(config.output_time_format)) {
        options.calendar_year = config.year;
    }
    return options;
}

/// Resolve the merged configuration into a `TimeRender`, enforcing the
/// L2-WRT-026 clause 1 precondition.
///
/// The check lives here rather than in either loader because neither can answer
/// it alone: a config file may set the rendering and the CLI supply the year, or
/// the reverse. It runs before the output is opened, so a run that cannot
/// produce the requested dates writes nothing at all.
TimeRender resolve_time_render(const DecoderConfig& config) {
    if (output_time_format_needs_calendar(config.output_time_format) && !config.year.has_value()) {
        throw usage_error(
            std::string("--output-time-format ") +
            output_time_format_name(config.output_time_format) +
            " needs a calendar year, and an MIE recording does not carry one: IRIG-B encodes "
            "day-of-year but not the year. Set [output] year = YYYY in a config file or pass "
            "--year YYYY. (--output-time-format doy needs no year.)");
    }
    TimeRender render;
    render.format = config.output_time_format;
    render.year = config.year;
    render.utc_offset_minutes = config.utc_offset_minutes;
    return render;
}

/// Sync recoveries across every input.
///
/// Summed, not taken from the first: a merge that recovered in file three is
/// just as `partial-recovered` as one that recovered in file one, and reporting
/// only the first reader's count would call it clean.
uint64_t total_sync_losses(const std::vector<MieFileReader*>& readers) {
    uint64_t total = 0;
    for (std::size_t i = 0; i < readers.size(); ++i) {
        total += readers[i]->sync_losses();
    }
    return total;
}

/// True when EVERY input was a valid but empty recording.
///
/// All of them, not any: a merge of one empty file and one full file produced
/// records, and calling that an empty recording would report exit class
/// `empty-recording` over a CSV with rows in it.
bool all_empty_recordings(const std::vector<MieFileReader*>& readers) {
    if (readers.empty()) {
        return false;
    }
    for (std::size_t i = 0; i < readers.size(); ++i) {
        if (!readers[i]->empty_recording()) {
            return false;
        }
    }
    return true;
}

/// Resolve the input set from whichever method was given (L2-MRG-001).
std::vector<std::string> resolve_inputs(const DecodeArgs& args) {
    std::vector<std::string> paths;
    platform::OsError err;

    if (args.manifest.has_value()) {
        if (!merge::read_manifest(args.manifest.value(), paths, err)) {
            throw runtime_error_("failed to read manifest " + args.manifest.value() + ": " +
                                 err.message);
        }
    } else if (args.glob.has_value()) {
        if (!merge::expand_glob(args.glob.value(), paths, err)) {
            // A wildcard in the directory part is a malformed pattern, refused
            // before any I/O: the command line is wrong, so exit 4, not 1.
            if (err.code == merge::GLOB_INVALID_PATTERN) {
                throw usage_error("--glob \"" + args.glob.value() + "\": " + err.message);
            }
            throw runtime_error_("failed to expand --glob \"" + args.glob.value() +
                                 "\": " + err.message);
        }
    } else {
        paths = args.inputs;
    }

    if (paths.empty()) {
        // Named separately: "the manifest was empty" and "the glob matched
        // nothing" are different mistakes from "you gave me no arguments", and
        // an operator debugging a batch script needs to know which.
        if (args.manifest.has_value()) {
            throw usage_error("manifest " + args.manifest.value() + " contains no input paths");
        }
        if (args.glob.has_value()) {
            throw usage_error("--glob \"" + args.glob.value() + "\" matched no files");
        }
        throw usage_error("decode requires at least one input file");
    }
    if (paths.size() > merge::MAX_MERGE_FILES) {
        // Refused up front rather than discovered as an open failure partway
        // through: the cap exists to keep resource use predictable, and finding
        // out at file 257 would already have consumed the descriptors.
        throw usage_error("too many input files: " + text::decimal(paths.size()) + " (maximum is " +
                          text::decimal(merge::MAX_MERGE_FILES) +
                          "); split the set into smaller batches");
    }
    return paths;
}

/// Refuse a merge whose output resolves to one of its own inputs (L2-WRT-014
/// across the input set).
///
/// The writer has its own input/output guard, but it takes a single path and is
/// deliberately given none on the merge path -- it is handed one stream and
/// cannot know how many files fed it. So for a merge this is the ONLY guard,
/// and without it `decode a.mie b.mie -o a.mie` would truncate an input while
/// still reading it.
///
/// Gated on whether a merge was REQUESTED, not on how many readers survived.
/// `--allow-partial` can drop a multi-input merge to a single open reader, and
/// the writer's guard is off for the whole run either way -- so keying off the
/// surviving count would leave exactly that case unguarded.
///
/// Checks EVERY path the run could commit, not just the destination the
/// operator named: `writer::commit_targets` enumerates the derived errors file
/// and the `.partial` variants alongside it. The writer runs the same
/// enumeration for a single-input decode, where it knows the one input; on the
/// merge path it is given no `input_path` and this is the only guard that sees
/// the input set at all.
void check_merge_output_collision(const std::string& output, const std::vector<std::string>& inputs,
                                  bool split_errors, bool allow_partial) {
    const std::vector<std::string> targets = commit_targets(output, split_errors, allow_partial);
    for (std::size_t t = 0; t < targets.size(); ++t) {
        for (std::size_t i = 0; i < inputs.size(); ++i) {
            bool same = false;
            platform::OsError err;
            if (platform::paths_same_file(inputs[i], targets[t], same, err) && same) {
                // Naming which target collided matters: told only that the
                // output collides, an operator looks at `-o` and sees a name
                // that is plainly different from every input.
                const std::string role =
                    targets[t] == output ? "output path " : "derived output path ";
                throw runtime_error_(role + targets[t] + " resolves to merge input " + inputs[i] +
                                     "; choose a different output path");
            }
        }
    }
}

/// Adapts a RecordIter to the pipeline's MessageSource contract.
///
/// Lives here rather than in `reader.hpp` because it is the CLI that knows both
/// types: making the reader implement MessageSource would give it a vtable and
/// a dependency on the pipeline contract for the benefit of one caller.
class ReaderSource : public MessageSource {
  public:
    explicit ReaderSource(RecordIter& iter) : iter_(&iter) {}
    bool next(MieMessage& out) override { return iter_->next(out); }

  private:
    RecordIter* iter_;
};

/// The exit-class summary, and the code that goes with a successful write.
int classify_decode_exit(const WriteOutcome& outcome, uint64_t sync_losses, bool empty_recording) {
    // L1-EXIT-010: report `empty-recording` only when the decode really
    // produced nothing AND the reader saw the end-of-records terminator. A file
    // that yielded zero rows because a filter dropped them all is `complete`.
    const bool empty = empty_recording && !outcome.partial.has_value() &&
                       outcome.normal_count == 0 && outcome.error_count == 0;
    std::string classification;
    if (outcome.partial.has_value()) {
        classification = "partial-unrecoverable";
    } else if (empty) {
        classification = "empty-recording";
    } else if (sync_losses > 0) {
        classification = "partial-recovered";
    } else {
        classification = "complete";
    }
    MIE_LOG_INFO("decode exit class: " + classification +
                 " (sync_losses=" + text::decimal(sync_losses) + ")");
    return EXIT_OK;
}

/// Report a decode failure and choose its exit class.
int report_decode_failure(const Streams& streams, const MieError& error, uint64_t sync_losses) {
    if (error.is_broken_pipe()) {
        // L2-WRT-018: `aero1553 decode x.mie | head` is a normal thing to
        // type, not a failure.
        MIE_LOG_INFO("decode exit class: complete (broken-pipe on stdout)");
        return EXIT_OK;
    }

    MIE_LOG_ERROR(error.message());
    (void)write_out(streams.err, "Error: " + error.message() + "\n");

    const int code = exit_code_for(error);
    if (code == EXIT_NO_RECORDS) {
        MIE_LOG_INFO("decode exit class: no-records");
        return code;
    }
    if (error.kind() == KIND_UNRECOVERABLE_SYNC_LOSS) {
        MIE_LOG_INFO(
            "decode exit class: partial-unrecoverable (sync_losses=" + text::decimal(sync_losses) +
            "); pass --allow-partial with an output file (-o) to preserve the rows decoded so far");
        return EXIT_SYNC_LOSS;
    }
    if (error.kind() == KIND_MERGE_INPUTS_DROPPED) {
        // L2-MRG-004: a merge under --allow-partial left inputs out. With a
        // file destination the writer commits a `.partial` and this is never
        // reached; it is reached on stdout, which cannot hold one -- the same
        // exit class a sync loss gets there.
        MIE_LOG_INFO(
            "decode exit class: partial-unrecoverable (merge inputs left out); "
            "write to a file with -o to keep the rows as a .partial");
        return EXIT_SYNC_LOSS;
    }
    if (error.kind() == KIND_INCOMPATIBLE_MERGE_INPUTS) {
        MIE_LOG_INFO("decode exit class: merge-incompatible");
        return EXIT_MERGE_INCOMPATIBLE;
    }
    return EXIT_RUNTIME;
}

int run_decode(const Streams& streams, const GlobalArgs& globals, DecodeArgs& args) {
    const DecoderConfig base = resolve_config(globals);
    if (globals.log_level.has_value()) {
        args.overrides.log_level = globals.log_level.value();
    }
    const DecoderConfig config = with_overrides(base, args.overrides);

    // No post-merge output_format check: both ways of setting it are rejected
    // before this point -- the CLI value at parse time (exit 4) and the
    // config-file value at load time (exit 5, L2-CFG-010). A third check here
    // would be unreachable.

    const std::vector<std::string> inputs = resolve_inputs(args);
    const bool merging = inputs.size() > 1;

    // shared_ptr because MieFileReader owns a mapping and is deliberately
    // non-copyable, so it cannot live in a vector directly. The readers must
    // outlive every iterator the merge holds, which is what owning them here
    // -- one scope above the pipeline -- guarantees.
    std::vector<std::shared_ptr<MieFileReader>> owned;
    std::vector<MieFileReader*> readers;
    // L2-MRG-004: inputs left out because they could not be opened. The merge
    // counts them into the summary it ends with.
    std::size_t open_left_out = 0;
    // The first input that failed to open (under --allow-partial on a merge),
    // and the input-list positions of it and of the first input that opened:
    // when every input fails, the earlier of the two is reported (L2-MRG-004).
    std::shared_ptr<CliError> first_open_failure;
    std::size_t first_open_failure_at = 0;
    std::size_t first_opened_at = 0;
    owned.reserve(inputs.size());
    readers.reserve(inputs.size());
    for (std::size_t i = 0; i < inputs.size(); ++i) {
        const auto reader = std::make_shared<MieFileReader>();
        try {
            reader->open(inputs[i], reader_options(config));
        } catch (const MieError& error) {
            if (merging && config.allow_partial) {
                // L2-MRG-004: one unreadable input must not cost the operator
                // the other twenty.
                MIE_LOG_WARN("merge: input " + inputs[i] +
                             " could not be opened; truncating it from the merge "
                             "(--allow-partial): " +
                             error.message());
                open_left_out += 1;
                if (!first_open_failure) {
                    first_open_failure.reset(new CliError(exit_code_for(error), error.message()));
                    first_open_failure_at = i;
                }
                continue;
            }
            throw CliError(exit_code_for(error), error.message());
        }
        MIE_LOG_INFO("opened " + reader->path() + " (" + text::decimal(reader->file_size()) +
                     " bytes)");
        readers.push_back(reader.get());
        owned.push_back(reader);
        if (readers.size() == 1) {
            first_opened_at = i;
        }
    }
    // L2-MRG-004: when NO input could be opened there is nothing to keep, so
    // --allow-partial does not apply -- report the first input's own error,
    // exactly as without the flag.
    if (readers.empty() && first_open_failure) {
        throw CliError(first_open_failure->code, first_open_failure->message);
    }

    // `--output` is a PATH, and no value of it is special-cased -- `-o -`
    // writes a file called `-`, the same as Rust and Python. stdout is selected
    // by OMITTING the flag (L2-CLI-002), which every implementation already
    // did, so treating `-` as stdout only added a second way to say the same
    // thing plus a trap for anyone whose file really is named `-`.
    //
    // This build did special-case it, and nothing caught the divergence: the
    // surface-parity gate compares flag NAMES, not what their values mean.
    const Optional<std::string>& destination = args.output;

    WriteOptions write_options;
    write_options.no_clobber = config.no_clobber;
    write_options.allow_partial = config.allow_partial;
    // L2-WRT-026 clause 1: a calendar rendering needs a year, and whether one
    // was supplied is a question about the *resolved* pair -- either source may
    // have provided it -- so it is asked here, before the output is opened.
    write_options.time_render = resolve_time_render(config);
    // So `-o -` lands on the same stream everything else reports on.
    write_options.stdout_stream = streams.out;
    // The writer's own guard takes ONE input path, so it covers the single-input
    // case. A merge needs the whole set checked instead, which `run_decode` does
    // below -- the writer cannot, because it is handed one stream and never
    // learns how many files fed it.
    if (destination.has_value() && !merging) {
        write_options.input_path = inputs[0];
    } else if (destination.has_value()) {
        // The RESOLVED config decides the mode, not the flags: a site config
        // file can select separate-errors or allow-partial without either flag
        // appearing on the command line, and enumerating from the flags alone
        // would leave exactly those runs unguarded.
        check_merge_output_collision(destination.value(), inputs,
                                     config.error_mode == ERROR_MODE_SEPARATE,
                                     config.allow_partial);
    }

    if (!destination.has_value() && config.error_mode == ERROR_MODE_SEPARATE) {
        // L3-RS-009: separate mode needs a file path to derive the errors name
        // from, so stdout forces inline. Warned rather than done quietly --
        // an operator who asked for a split file and silently got one combined
        // stream would only find out by reading the output.
        MIE_LOG_WARN("stdout output forces inline error mode");
    }

    merge::MergeOptions merge_options;
    merge_options.standard_tick_rate_hz = config.standard_tick_rate_hz;
    merge_options.allow_partial = config.allow_partial;
    merge_options.strict = config.strict;
    merge_options.collapse_duplicates = config.collapse_duplicates;
    merge_options.collapse_window_us = config.collapse_window_us;
    merge_options.max_collapse_survivors = config.max_collapse_survivors;
    merge_options.delta_scope = config.delta_scope;
    merge_options.inputs_left_out_at_open = open_left_out;

    // The pipeline, assembled. The only difference a merge makes is which source
    // sits at the HEAD of it: filter, canonical order and the writer downstream
    // are the same code operating on the same contract.
    //
    // Held by pointer, not by value, for two reasons. MergedSource primes
    // itself in its constructor and that priming can throw (L2-MRG-003), so it
    // must be built only on the merge path. And `iter()` may be called on a
    // reader at most once at a time -- constructing a single-file walk that the
    // merge path then ignored would reset the very counters the merge is
    // reporting into.
    std::shared_ptr<RecordIter> single;
    std::shared_ptr<ReaderSource> from_reader;
    std::shared_ptr<merge::MergedSource> merged;
    MessageSource* head = nullptr;

    if (merging) {
        try {
            merged.reset(new merge::MergedSource(readers, merge_options));
        } catch (const MieError& error) {
            // Incompatible inputs (L2-MRG-003) and priming failures surface
            // here, before any output exists -- through the same classifier
            // that handles a mid-stream failure, so the exit codes agree.
            //
            // Under --allow-partial a priming failure reaches here only when
            // every opened input failed (L2-MRG-004). If an input that failed
            // to OPEN came earlier in the input list, every input has failed
            // and that one is the first: report it, exactly as without the flag.
            if (config.allow_partial && error.kind() != KIND_INCOMPATIBLE_MERGE_INPUTS &&
                first_open_failure && first_open_failure_at < first_opened_at) {
                throw CliError(first_open_failure->code, first_open_failure->message);
            }
            return report_decode_failure(streams, error, 0);
        }
        head = merged.get();
    } else {
        single.reset(new RecordIter(readers[0]->iter()));
        from_reader.reset(new ReaderSource(*single));
        head = from_reader.get();
    }

    FilteredSource filtered(*head, config.filters);
    OrderedSource ordered(filtered, config.max_sort_group);
    MessageSource* pipeline = &ordered;

    WriteOutcome outcome;
    try {
        if (!destination.has_value()) {
            outcome = write_csv(*pipeline, Optional<std::string>(), write_options);
        } else if (config.error_mode == ERROR_MODE_SEPARATE) {
            outcome = write_csv_split(*pipeline, destination.value(), write_options);
        } else {
            outcome = write_csv(*pipeline, destination, write_options);
        }
    } catch (const MieError& error) {
        return report_decode_failure(streams, error, total_sync_losses(readers));
    }

    if (merged.get() != nullptr && merged->collapsed() > 0) {
        MIE_LOG_INFO("merge: collapsed " + text::decimal(merged->collapsed()) +
                     " duplicate message(s) across recorders");
    }

    return classify_decode_exit(outcome, total_sync_losses(readers), all_empty_recordings(readers));
}

int run_dump(const Streams& streams, const GlobalArgs& globals, const DumpArgs& args) {
    // dump takes only the log level from configuration -- a hex view has no use
    // for a timestamp format, filters or an error mode. The config is still
    // LOADED so that a malformed one fails here exactly as it would for the
    // other subcommands, rather than being the one command that ignores it.
    (void)resolve_config(globals);

    try {
        if (args.raw) {
            dump::hex_dump_raw(args.input, args.offset, args.length, streams.out);
        } else {
            dump::hex_dump_records(args.input, args.records, args.offset, streams.out);
        }
    } catch (const MieError& error) {
        if (error.is_broken_pipe()) {
            // `dump x.mie | head` is a normal thing to type (L2-WRT-018).
            return EXIT_OK;
        }
        throw CliError(exit_code_for(error), error.message());
    }
    return EXIT_OK;
}

int run_count(const Streams& streams, const GlobalArgs& globals, const std::string& input) {
    const DecoderConfig config = resolve_config(globals);

    MieFileReader file_reader;
    try {
        file_reader.open(input, reader_options(config));
    } catch (const MieError& error) {
        throw CliError(exit_code_for(error), error.message());
    }

    uint64_t count = 0;
    {
        // The config's filters apply, matching decode: an operator who wants a
        // raw count omits [filter] from their config rather than needing a
        // second subcommand.
        RecordIter iter = file_reader.iter();
        ReaderSource from_reader(iter);
        FilteredSource filtered(from_reader, config.filters);
        MieMessage message;
        try {
            while (filtered.next(message)) {
                count += 1;
            }
        } catch (const MieError& error) {
            throw CliError(exit_code_for(error), error.message());
        }
    }

    // The integer alone on stdout -- that is the machine-readable answer, and
    // a script doing `n=$(aero1553 count x.mie)` must not have to strip
    // prose. The human sentence goes to stderr, ungated by --log-level so an
    // interactive operator sees it without opting into INFO.
    (void)write_out(streams.out, text::decimal(count) + "\n");
    if (file_reader.empty_recording()) {
        (void)write_out(streams.err, "no records in " + file_reader.path() +
                                         " (empty recording -- opens on the end-of-records "
                                         "terminator)\n");
    } else {
        (void)write_out(streams.err, "counted " + text::decimal(count) + " messages in " +
                                         file_reader.path() + "\n");
    }
    return EXIT_OK;
}

}  // namespace

const char* help_text() { return kHelp; }

std::string version_line() { return std::string("aero1553 ") + kVersion; }

Streams::Streams() : out(stdout), err(stderr) {}

Streams::Streams(std::FILE* out_, std::FILE* err_) : out(out_), err(err_) {}

int run(const std::vector<std::string>& args) { return run(args, Streams()); }

int run(const std::vector<std::string>& args, const Streams& streams) {
    if (args.empty()) {
        // Help, but a non-zero exit: a script that invokes the tool with no
        // arguments has a bug, and exiting 0 would hide it.
        (void)write_out(streams.err, kHelp);
        return EXIT_USAGE;
    }

    // Declared outside the `try` so the handler can ask whether a help request
    // is still pending at the point the parse gave up.
    ArgReader reader(args);
    try {
        GlobalArgs globals;
        std::string value;

        // Global flags, help and version, all resolved POSITIONALLY. This used
        // to be a scan over the whole argument vector, which was wrong in two
        // ways that made C++ the only implementation doing them: it answered
        // `--version` AFTER the subcommand, where the other two report an
        // unknown option, and it answered a help or version token that was
        // being consumed as some flag's VALUE, so `--log-level -h` printed
        // help instead of failing.
        //
        // Help remaining reachable from a BROKEN command line -- the case the
        // scan existed for -- is now the handler's job below, which asks
        // whether a help request is still pending rather than assuming one
        // anywhere on the line counts.
        while (!reader.at_end()) {
            const std::string token = reader.peek();
            if (is_help_flag(token)) {
                return write_out(streams.out, kHelp) ? EXIT_OK : EXIT_RUNTIME;
            }
            // Version is global-only: after the subcommand it belongs to that
            // subcommand, and every one of them calls it an unknown option.
            if (is_version_flag(token)) {
                return write_out(streams.out, version_line() + "\n") ? EXIT_OK : EXIT_RUNTIME;
            }
            if (token == "--") {
                // POSIX end-of-options, scoped to THIS loop: the subcommand
                // parser gets a fresh scan, so `-- decode rec.mie --no-mux`
                // still honours `--no-mux`. The next token is the subcommand
                // NAME, whatever it looks like.
                reader.advance();
                break;
            }
            if (reader.take_value("--config", value)) {
                globals.config = value;
            } else if (reader.take_value("--log-level", value)) {
                globals.log_level = value;
            } else if (reader.take_flag("--no-irig-day-advisory")) {
                // take_flag matches the whole token, so the `=value` spelling
                // does not match here and is reported as an unknown option
                // rather than silently accepted with its value discarded.
                globals.irig_day_advisory = false;
            } else {
                break;
            }
        }

        if (reader.at_end()) {
            throw usage_error("no command given; expected decode, count or dump");
        }
        const std::string command = reader.peek();
        reader.advance();

        // The whole command line is parsed BEFORE the level is applied, as in
        // Rust: so `--log-level NOPE count x --bogus` reports the unknown
        // option in both, and a parse error is logged at the default level
        // whatever `--log-level` asked for. C++ applied the level first, and
        // the two disagreed on both (L2-CLI-022).
        DecodeArgs decode_args;
        std::string count_input;
        DumpArgs dump_args;
        if (command == "decode") {
            decode_args = parse_decode(reader);
        } else if (command == "count") {
            count_input = parse_count(reader);
        } else if (command == "dump") {
            dump_args = parse_dump(reader);
        } else {
            throw usage_error("unknown command " + text::quote(command) +
                              "; expected decode, count or dump");
        }

        // Applied before anything runs, so the version banner and every later
        // diagnostic respect it. An invalid value fails here rather than being
        // silently ignored.
        if (globals.log_level.has_value()) {
            apply_log_level("--log-level", globals.log_level.value());
        } else {
            log::set_level(log::LEVEL_WARN);
        }
        MIE_LOG_INFO(version_line());

        if (command == "decode") {
            return run_decode(streams, globals, decode_args);
        }
        if (command == "count") {
            return run_count(streams, globals, count_input);
        }
        return run_dump(streams, globals, dump_args);
    } catch (const HelpRequested&) {
        return write_out(streams.out, kHelp) ? EXIT_OK : EXIT_RUNTIME;
    } catch (const CliError& error) {
        // A pending `-h` outranks a DEFERRED diagnostic -- an unrecognised
        // option, or a value rejected after it was taken. It does not outrank
        // a failed value CONSUMPTION, because `take_value` abandons the rest
        // of the line there, so nothing is pending. That split is argparse's
        // and is what the other two implementations do (L2-CLI-017).
        if (error.code == EXIT_USAGE && reader.help_pending()) {
            return write_out(streams.out, kHelp) ? EXIT_OK : EXIT_RUNTIME;
        }
        MIE_LOG_ERROR(error.message);
        (void)write_out(streams.err, "Error: " + error.message + "\n");
        if (error.code == EXIT_USAGE) {
            // One line, not the help: see kUsageHint.
            (void)write_out(streams.err, std::string(kUsageHint) + "\n");
        }
        return error.code;
    } catch (const MieError& error) {
        // Anything a runner did not already classify.
        MIE_LOG_ERROR(error.message());
        (void)write_out(streams.err, "Error: " + error.message() + "\n");
        return exit_code_for(error);
    } catch (const ConfigError& error) {
        MIE_LOG_ERROR(error.message());
        (void)write_out(streams.err, "Error: " + error.message() + "\n");
        return EXIT_CONFIG;
    } catch (const std::exception& error) {
        // The backstop. An exception escaping here would reach
        // std::terminate and abort, and a caller cannot tell an abort
        // apart from a signal -- every failure this tool has must arrive
        // as an exit code.
        (void)write_out(streams.err, std::string("Error: ") + error.what() + "\n");
        return EXIT_RUNTIME;
    }
}

}  // namespace cli
}  // namespace mie
