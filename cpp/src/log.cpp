// SPDX-License-Identifier: Apache-2.0

#include "mie/log.hpp"

#include <atomic>
#include <cstdio>

#include "mie/text.hpp"

namespace mie {
namespace log {

namespace {

/// The global level. `std::atomic<int>`'s constructor is constexpr, so this is
/// constant-initialised and cannot participate in a static-initialisation-order
/// problem -- which matters because a logging call can legitimately happen from
/// another translation unit's static constructor.
///
/// Atomic rather than a plain int for the same reason `rust/src/log.rs` uses an
/// AtomicU8: reading it is on the per-record path, and a torn read of a level
/// would be undefined behaviour that a race detector would (rightly) flag even
/// though the decoder is single-threaded today.
std::atomic<int> g_level(static_cast<int>(LEVEL_WARN));

/// Null means stderr. Not atomic: it is set by the test suite between test
/// cases, never concurrently with logging.
SinkFn g_sink = nullptr;

/// L2-LOG-001. Atomic for the same reason `g_level` is: it is read on the
/// per-record path.
std::atomic<bool> g_irig_day_advisory(true);

void write_out(const std::string& line) {
    if (g_sink != nullptr) {
        g_sink(line.c_str(), line.size());
        return;
    }
    // One fwrite for the whole line. Two calls could interleave with another
    // writer's output between the prefix and the message.
    //
    // The return value is deliberately ignored: a logger that reported its own
    // I/O failures would have nowhere to report them, and a full or closed
    // stderr must not turn a successful decode into a failure. The CSV
    // destination is the output that gets checked.
    static_cast<void>(std::fwrite(line.data(), 1, line.size(), stderr));
}

}  // namespace

const char* const LEVEL_NAMES = "DEBUG, INFO, WARNING, WARN, ERROR, CRITICAL, OFF";

bool level_from_name(const std::string& name, Level& out) {
    const std::string lower = text::to_ascii_lower(name);
    if (lower == "debug") {
        out = LEVEL_DEBUG;
    } else if (lower == "info") {
        out = LEVEL_INFO;
    } else if (lower == "warning" || lower == "warn") {
        out = LEVEL_WARN;
    } else if (lower == "error") {
        out = LEVEL_ERROR;
    } else if (lower == "critical" || lower == "off") {
        // Both silence the decoder. Nothing here is emitted at CRITICAL, so
        // selecting it is indistinguishable from OFF -- the Python
        // implementation maps CRITICAL to a numeric level above every message
        // it emits for exactly the same reason.
        out = LEVEL_OFF;
    } else {
        return false;
    }
    return true;
}

const char* level_label(Level level) {
    switch (level) {
        case LEVEL_DEBUG: return "DEBUG";
        case LEVEL_INFO: return "INFO";
        case LEVEL_WARN: return "WARN";
        case LEVEL_ERROR: return "ERROR";
        case LEVEL_OFF: return "OFF";
    }
    return "OFF";
}

void set_level(Level level) { g_level.store(static_cast<int>(level), std::memory_order_relaxed); }

Level current_level() {
    const int raw = g_level.load(std::memory_order_relaxed);
    switch (raw) {
        case 0: return LEVEL_DEBUG;
        case 1: return LEVEL_INFO;
        case 2: return LEVEL_WARN;
        case 3: return LEVEL_ERROR;
        default: return LEVEL_OFF;
    }
}

bool enabled(Level level) {
    return static_cast<int>(level) >= g_level.load(std::memory_order_relaxed);
}

void set_irig_day_advisory(bool enabled) {
    g_irig_day_advisory.store(enabled, std::memory_order_relaxed);
}

bool irig_day_advisory() { return g_irig_day_advisory.load(std::memory_order_relaxed); }

void emit(Level level, const char* module_name, const std::string& message) {
    std::string line;
    // Enough for the prefix and a typical diagnostic; longer messages simply
    // grow the string once more.
    line.reserve(message.size() + 32);
    line += level_label(level);
    line += " [";
    line += (module_name != nullptr ? module_name : "");
    line += "] ";
    line += message;
    line += "\n";
    write_out(line);
}

void set_sink(SinkFn sink) { g_sink = sink; }

}  // namespace log
}  // namespace mie
