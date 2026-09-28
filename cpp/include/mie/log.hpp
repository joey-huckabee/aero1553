// SPDX-License-Identifier: Apache-2.0
//
// The decoder's logger: one global level, four macros, stderr.
//
// Mirrors `rust/src/log.rs`. There is no facade, no handler hierarchy and no
// dependency, for the same reason the CSV writer and the TOML parser are
// hand-rolled: the whole requirement is "put a severity-filtered line on
// stderr", and every logging library available on a SLES 12 build host would be
// a new thing to qualify in exchange for that.
//
// LINE FORMAT is `LEVEL [module] message`, matching the Rust implementation
// word for word. The Python implementation prefixes an ISO timestamp instead
// (`logger.py`), so the three have never been byte-identical here and the
// conformance cases assert stderr by SUBSTRING rather than by equality --
// which is why aligning with Rust costs nothing and reads better side by side.
//
// WHY MACROS AND NOT FUNCTIONS. The message argument is a std::string built by
// concatenation at the call site, so calling a function would build it whether
// or not the level passes. The macros test the level first and never evaluate
// the argument when it fails, which is what keeps a DEBUG line inside the
// per-record loop free at the default WARN level. This is the same reason
// `log_debug!` in Rust is a macro over `format_args!`.
//
// LOCALE. Messages are assembled from `mie::text`'s formatters, never from an
// ostringstream: a stream formats through its imbued locale, and the whole
// point of `scripts/assert-locale-free.sh` is that no number this program
// prints may depend on the host's locale. `text::decimal` and
// `text::hex_upper` are the locale-free spellings, and they return std::string
// so `+` composes them.

#ifndef MIE_LOG_HPP
#define MIE_LOG_HPP

#include <cstddef>
#include <string>

namespace mie {
namespace log {

/// Severity. Higher value = more important, so the filter is a single `>=`.
/// The numbering matches `rust/src/log.rs`.
enum Level {
    LEVEL_DEBUG = 0,
    LEVEL_INFO = 1,
    LEVEL_WARN = 2,
    LEVEL_ERROR = 3,
    /// Silence everything. `CRITICAL` is accepted as a spelling of this
    /// because the Python implementation maps it the same way -- the decoder
    /// emits nothing at CRITICAL, so the two are indistinguishable in effect.
    LEVEL_OFF = 4
};

/// Parse a level name case-insensitively: DEBUG, INFO, WARNING (alias WARN),
/// ERROR, CRITICAL, OFF. False for anything else; the caller words its own
/// error, because the CLI and the config loader phrase it differently.
bool level_from_name(const std::string& name, Level& out);

/// The label that appears in a log line. "WARN", not "WARNING" -- matching
/// Rust.
const char* level_label(Level level);

/// Default is WARN, matching both other implementations.
void set_level(Level level);
Level current_level();

/// True when a message at `level` would be emitted. Exposed because a caller
/// occasionally has to do real work to build a diagnostic (the reader's hex
/// context dump) and needs to skip that work, not just the formatting.
bool enabled(Level level);

/// Enable or disable the one-time IRIG day-of-year advisory (L2-LOG-001).
/// Enabled by default.
///
/// This lives beside the level rather than in `ReaderOptions` because it is a
/// diagnostics switch, not a decode parameter: it is applied where
/// `--log-level` is applied, so it covers `decode`, `count` and `dump` from one
/// place instead of each command threading it through.
void set_irig_day_advisory(bool enabled);

/// Whether the IRIG day-of-year advisory may be emitted. The level filter still
/// applies on top of this: the advisory is logged at INFO, so at the default
/// WARNING level it stays silent even when this is true.
bool irig_day_advisory();

/// Emit one line. Prefer the macros: this does NOT test the level, so calling
/// it directly emits unconditionally.
void emit(Level level, const char* module_name, const std::string& message);

/// Where a line goes. Null (the default) means stderr.
///
/// A plain function pointer rather than std::function: this is set by the test
/// suite and by nothing else, and a captureless pointer keeps the type
/// trivially copyable and the assignment free of allocation.
using SinkFn = void (*)(const char*, std::size_t);

/// Redirect output. Passing null restores stderr.
///
/// Exists for the tests, which is worth stating plainly: the alternative was
/// to assert log behaviour by reading the process's stderr, and every
/// interesting property here -- that a DEBUG line is suppressed at WARN, that
/// the freerun advisory fires once per record and the day-of-year advisory
/// once per file -- is a property of the emitting code, not of the terminal.
void set_sink(SinkFn sink);

}  // namespace log
}  // namespace mie

// Each translation unit that logs defines MIE_LOG_MODULE before its first use,
// e.g. `#define MIE_LOG_MODULE "aero1553::reader"`. Deliberately not given
// a default here: a missing definition is a compile error naming the file that
// forgot, which is better than a log line that silently says "mie".
//
// The `do { } while (0)` wrapper makes each macro a single statement, so
// `if (x) MIE_LOG_WARN(...); else ...` parses the way it looks.

#define MIE_LOG_AT(level_, message_)                                \
    do {                                                            \
        if (::mie::log::enabled(level_)) {                          \
            ::mie::log::emit((level_), MIE_LOG_MODULE, (message_)); \
        }                                                           \
    } while (0)

#define MIE_LOG_DEBUG(message_) MIE_LOG_AT(::mie::log::LEVEL_DEBUG, message_)
#define MIE_LOG_INFO(message_) MIE_LOG_AT(::mie::log::LEVEL_INFO, message_)
#define MIE_LOG_WARN(message_) MIE_LOG_AT(::mie::log::LEVEL_WARN, message_)
#define MIE_LOG_ERROR(message_) MIE_LOG_AT(::mie::log::LEVEL_ERROR, message_)

#endif  // MIE_LOG_HPP
