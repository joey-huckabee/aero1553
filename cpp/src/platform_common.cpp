// SPDX-License-Identifier: Apache-2.0
//
// The portable half of the platform layer: everything that is decided by the
// C++ standard library or by string manipulation rather than by an OS call.
//
// It lives beside platform_posix.cpp and platform_win32.cpp -- and is named to
// match their `platform_*` pattern -- so the one confinement rule
// (scripts/assert-platform-confined.sh) reads as "platform_* may include OS
// headers, nothing else may". This file happens to include none, which is the
// point: anything that can be written once is written once here, and the two
// backends carry only the calls that genuinely differ.

#include "mie/platform.hpp"

#include <chrono>
#include <cstdio>
#include <string>

namespace mie {
namespace platform {

// 64 KiB. Large enough that a CSV row -- at most a few hundred bytes -- never
// costs a syscall on its own, small enough to stay irrelevant against the
// constant-memory design point the writer is held to.
// The multiplication is done in std::size_t rather than in int and widened
// afterwards. At this value the difference is invisible, but the pattern is not:
// the same expression with a larger constant overflows int silently and yields a
// buffer size nobody asked for.
const std::size_t kWriteBufferSize = std::size_t(64) * 1024;

// Portable, so defined once here rather than in each backend: every successful
// move ends the same way whichever OS call made it.
void AtomicFile::mark_committed() {
    committed_ = true;
    temp_path_.clear();
}

namespace {

// Per-process temp-name counter. L3-WRT-001 requires the name be unique per
// writer instance, and two writers created inside the same nanosecond tick
// would otherwise collide -- coarse clocks make that a real case, not a
// theoretical one.
//
// Deliberately NOT std::atomic: the decoder is single-threaded by design, and
// libstdc++ 4.8's <atomic> pulls in a -latomic dependency for some types on the
// SLES 12 toolchain. If a thread is ever introduced this becomes a real atomic
// and the link line grows -- until then this is one fewer thing the fidelity
// tier has to prove.
unsigned long long g_temp_counter = 0;

const char kTempInfix[] = ".aero1553.tmp.";

std::string to_decimal(unsigned long long value) {
    // Hand-rolled rather than std::to_string(unsigned long long), which is
    // present in libstdc++ 4.8 but routes through vsnprintf and therefore
    // through the locale. Every other integer this program prints goes through
    // an explicit formatter for the same reason; see the locale-free gate.
    if (value == 0) {
        return std::string("0");
    }
    char digits[24];
    std::size_t n = 0;
    while (value > 0) {
        digits[n++] = static_cast<char>('0' + static_cast<int>(value % 10));
        value /= 10;
    }
    std::string out;
    out.reserve(n);
    while (n > 0) {
        out.push_back(digits[--n]);
    }
    return out;
}

}  // namespace

std::string make_temp_name(const std::string& final_path) {
    // Shape and field order are pinned to the Rust and Python implementations:
    //   <destination-filename>.aero1553.tmp.<pid>.<counter>.<nanos>
    // placed in the destination's own directory. Same-directory placement is
    // what makes the later rename a single-filesystem operation, and therefore
    // atomic. Uniqueness is belt-and-braces -- the caller still creates the
    // file with exclusive-create, which is the actual guarantee.
    const std::string dir = path_parent(final_path);
    const std::string name = path_filename(final_path);

    std::string temp = name;
    temp += kTempInfix;
    temp += to_decimal(process_id());
    temp += '.';
    temp += to_decimal(g_temp_counter++);
    temp += '.';
    temp += to_decimal(wall_clock_nanos());

    if (dir.empty()) {
        return temp;
    }
    return path_join(dir, temp);
}

uint64_t wall_clock_nanos() {
    using Clock = std::chrono::system_clock;
    const Clock::time_point now = Clock::now();
    const std::chrono::nanoseconds since_epoch =
        std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch());
    const auto count = static_cast<long long>(since_epoch.count());
    // A pre-1970 clock would make this negative. Clamp rather than wrap: the
    // value is only ever a uniqueness salt, so a misconfigured clock must not
    // be able to produce a huge unsigned number that reads like a real stamp.
    return count < 0 ? 0u : static_cast<uint64_t>(count);
}

bool is_separator(char c) {
#if defined(_WIN32)
    return c == '/' || c == '\\';
#else
    // On POSIX a backslash is an ordinary filename character. Treating it as a
    // separator here would silently truncate any path that legitimately
    // contains one -- a bug that cannot reproduce on Windows, which is the
    // worst kind to carry in shared code.
    return c == '/';
#endif
}

std::string path_parent(const std::string& utf8_path) {
    for (std::size_t i = utf8_path.size(); i > 0; --i) {
        if (is_separator(utf8_path[i - 1])) {
            // Keep the root separator itself: the parent of "/x" is "/", not "".
            if (i == 1) {
                return utf8_path.substr(0, 1);
            }
#if defined(_WIN32)
            // Likewise the parent of "C:\x" is "C:\", the drive's root -- not
            // "C:", which Windows reads as the CURRENT DIRECTORY on drive C.
            // Returning "C:" made the input/output identity check resolve an
            // output at a drive root against the working directory, and refuse
            // `-o C:\a.mie` as "the same file as the input" whenever the input
            // was a.mie in the working directory on that drive.
            const char drive = utf8_path[0];
            const bool drive_letter =
                (drive >= 'A' && drive <= 'Z') || (drive >= 'a' && drive <= 'z');
            if (i == 3 && utf8_path[1] == ':' && drive_letter) {
                return utf8_path.substr(0, 3);
            }
#endif
            return utf8_path.substr(0, i - 1);
        }
    }
    return std::string();
}

std::string path_filename(const std::string& utf8_path) {
    for (std::size_t i = utf8_path.size(); i > 0; --i) {
        if (is_separator(utf8_path[i - 1])) {
            return utf8_path.substr(i);
        }
    }
    return utf8_path;
}

std::string path_join(const std::string& dir, const std::string& name) {
    if (dir.empty()) {
        return name;
    }
    if (name.empty()) {
        return dir;
    }
#if defined(_WIN32)
    const char preferred = '\\';
#else
    const char preferred = '/';
#endif
    std::string out = dir;
    const bool dir_ends = is_separator(out[out.size() - 1]);
    const bool name_starts = is_separator(name[0]);
    if (dir_ends && name_starts) {
        out.append(name, 1, name.size() - 1);
    } else if (!dir_ends && !name_starts) {
        out.push_back(preferred);
        out += name;
    } else {
        out += name;
    }
    return out;
}

bool read_file(const std::string& utf8_path, std::vector<uint8_t>& bytes, OsError& err) {
    err.clear();
    bytes.clear();
    std::FILE* handle = open_read(utf8_path, err);
    if (handle == nullptr) {
        return false;
    }

    uint8_t buffer[8192];
    bool ok = true;
    for (;;) {
        const std::size_t got = std::fread(buffer, 1, sizeof(buffer), handle);
        bytes.insert(bytes.end(), buffer, buffer + got);
        if (got < sizeof(buffer)) {
            // A short read is the end of the file or a real error, and fread
            // alone does not say which. Asking again would be a read in the EOF
            // state, and after a FAILED read the position is indeterminate, so
            // a retry could silently duplicate or skip bytes.
            if (std::ferror(handle) != 0) {
                capture_stream_error(err);
                ok = false;
            }
            break;
        }
    }
    if (std::fclose(handle) != 0 && ok) {
        capture_stream_error(err);
        ok = false;
    }
    return ok;
}

bool path_exists(const std::string& utf8_path) {
    uint64_t size = 0;
    bool is_regular = false;
    OsError err;
    return file_metadata(utf8_path, size, is_regular, err);
}

bool paths_same_file(const std::string& utf8_input, const std::string& utf8_output, bool& same,
                     OsError& err) {
    // Mirrors paths_refer_to_same_file in rust/src/writer.rs. A collision is
    // only ever *positive* when both sides resolve; an output path that does
    // not exist yet cannot be the input, so the answer there is "no", not an
    // error. Failing to canonicalize the INPUT is a real failure, though --
    // that path is about to be opened.
    same = false;

    std::string input_canon;
    if (!canonical_path(utf8_input, input_canon, err)) {
        return false;
    }

    std::string output_canon;
    OsError ignored;
    if (canonical_path(utf8_output, output_canon, ignored)) {
        same = (input_canon == output_canon);
        return true;
    }

    // The destination does not exist yet. Canonicalize its directory instead
    // and compare the resulting full path, so that a symlinked output
    // directory still resolves onto the input when it should.
    const std::string parent = path_parent(utf8_output);
    const std::string leaf = path_filename(utf8_output);
    if (leaf.empty()) {
        return true;
    }

    std::string parent_canon;
    const std::string parent_to_resolve = parent.empty() ? std::string(".") : parent;
    if (!canonical_path(parent_to_resolve, parent_canon, ignored)) {
        // No resolvable parent directory means no collision is provable. The
        // subsequent open will produce the real, more specific error.
        return true;
    }

    same = (input_canon == path_join(parent_canon, leaf));
    return true;
}

}  // namespace platform
}  // namespace mie
