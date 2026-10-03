// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::merge"

#include "mie/merge.hpp"

#include <algorithm>
#include <cstdio>

#include "mie/config.hpp"
#include "mie/error.hpp"
#include "mie/log.hpp"
#include "mie/text.hpp"

namespace mie {
namespace merge {

namespace {

/// Byte length of the UTF-8 sequence starting at `lead`.
///
/// Used so `?` consumes one character rather than one byte. A malformed or
/// unexpected continuation byte reports 1, which degrades to byte-wise matching
/// for that position rather than running off the end of the string -- a
/// filename is not required to be valid UTF-8 on either platform, and a matcher
/// is the wrong place to reject one.
std::size_t utf8_sequence_length(unsigned char lead) {
    if ((lead & 0x80) == 0x00) {
        return 1;
    }
    if ((lead & 0xE0) == 0xC0) {
        return 2;
    }
    if ((lead & 0xF0) == 0xE0) {
        return 3;
    }
    if ((lead & 0xF8) == 0xF0) {
        return 4;
    }
    return 1;
}

/// Advance `at` by one whole character, never past `size`.
std::size_t next_char(const std::string& text, std::size_t at) {
    const std::size_t width = utf8_sequence_length(static_cast<unsigned char>(text[at]));
    const std::size_t stepped = at + width;
    return stepped > text.size() ? text.size() : stepped;
}

/// Whether the directory part of a glob holds a `*` or `?`.
///
/// On Windows the extended-length prefix `\\?\` is skipped: its `?` is syntax,
/// not a wildcard, and refusing it would refuse every long-path pattern.
bool directory_has_wildcard(const std::string& directory) {
#if defined(_WIN32)
    const std::size_t from = directory.compare(0, 4, "\\\\?\\") == 0 ? 4 : 0;
#else
    const std::size_t from = 0;
#endif
    return directory.find_first_of("*?", from) != std::string::npos;
}

/// The microsecond value a message merges on.
///
/// Every input is validated as calendar-locked IRIG before any of this runs, so
/// the "no value" branch is unreachable for a real merge; 0 is a defined answer
/// rather than an assertion so a future format cannot turn a merge into a
/// crash.
uint64_t micros_of(const MieMessage& message, const Optional<double>& tick) {
    uint64_t out = 0;
    if (!message.timestamp.to_microseconds(tick, out)) {
        return 0;
    }
    return out;
}

/// Reject an input whose leading record cannot anchor an absolute timeline.
void check_mergeable(const MieMessage& message, std::size_t file_index, const std::string& path) {
    if (message.timestamp.is_standard()) {
        throw MieError::incompatible_merge_inputs(file_index, path,
                                                  "resolves to the Standard timestamp format");
    }
    if (message.timestamp.is_irig() && message.timestamp.irig.freerun) {
        throw MieError::incompatible_merge_inputs(
            file_index, path, "leads with a freerun IRIG record (no calendar time)");
    }
}

/// Take the error out of `slot`, clearing it.
///
/// Clearing BEFORE the throw is the point: a caller that catches and calls
/// `next()` again must get a clean end of stream, not the same failure a second
/// time. Returning by value also means the throw expression is a temporary,
/// which is what keeps the thrown object independent of the slot it came from.
MieError take(PendingError& slot) {
    const MieError error = *slot;
    slot.reset();
    return error;
}

/// Absolute distance between two microsecond stamps.
///
/// Order-independent on purpose. A lenient non-monotonic input (L2-MRG-006) can
/// step backward, and a one-sided subtraction would wrap to an enormous
/// unsigned value -- which compares as "outside the window" only by luck.
uint64_t abs_diff(uint64_t a, uint64_t b) { return a > b ? a - b : b - a; }

}  // namespace

// ---------------------------------------------------------------------------
// Input resolution
// ---------------------------------------------------------------------------

bool read_manifest(const std::string& path, std::vector<std::string>& out, platform::OsError& err) {
    err.clear();
    out.clear();
    // Through the platform layer: a manifest may sit at a non-ASCII path, and
    // std::fopen would fail to find it on Windows.
    std::vector<uint8_t> raw;
    if (!platform::read_file(path, raw, err)) {
        return false;
    }
    const std::string body(raw.begin(), raw.end());

    // A manifest is a TEXT file, and the other two implementations say so by
    // construction: `fs::read_to_string` and `Path.read_text(encoding="utf-8")`
    // both refuse ill-formed input. A std::string does not, so this
    // implementation used to accept arbitrary bytes as if they were paths --
    // the same manifest was rejected by two implementations and decoded by the
    // third. Found by the merge fuzz harness (L2-MRG-001).
    if (!text::is_valid_utf8(body)) {
        err.code = -1;
        err.message = "manifest is not valid UTF-8";
        return false;
    }

    std::string line;
    for (std::size_t i = 0; i <= body.size(); ++i) {
        if (i == body.size() || body[i] == '\n') {
            // Strip at most ONE trailing carriage return, which is what a CRLF
            // line ending is. Dropping every `\r` in the line -- as this did
            // until v2.16.0 -- also silently edits a filename that legitimately
            // contains one, and Rust's `str::lines()` and the fixed Python
            // reader both strip only the terminator.
            if (!line.empty() && line[line.size() - 1] == '\r') {
                line.erase(line.size() - 1);
            }
            const std::string trimmed = text::trim_ascii_blank(line);
            // A comment or a blank line is not an empty path -- treating it as
            // one would put "" into the merge and fail on a file nobody named.
            if (!trimmed.empty() && trimmed[0] != '#') {
                out.push_back(trimmed);
            }
            line.clear();
        } else {
            line += body[i];
        }
    }
    return true;
}

bool glob_match(const std::string& pattern, const std::string& name) {
    std::size_t p = 0;
    std::size_t t = 0;
    bool have_star = false;
    std::size_t star_at = 0;
    std::size_t mark = 0;

    while (t < name.size()) {
        if (p < pattern.size() && pattern[p] == '?') {
            p += 1;
            t = next_char(name, t);
        } else if (p < pattern.size() && pattern[p] != '*' && pattern[p] == name[t]) {
            p += 1;
            t += 1;
        } else if (p < pattern.size() && pattern[p] == '*') {
            have_star = true;
            star_at = p;
            mark = t;
            p += 1;
        } else if (have_star) {
            // Backtrack: let the last `*` swallow one more character and retry.
            // Stepping `mark` by a whole character keeps a multi-byte name from
            // being split mid-sequence, which would then compare against
            // pattern bytes that can never match.
            p = star_at + 1;
            mark = next_char(name, mark);
            t = mark;
        } else {
            return false;
        }
    }
    while (p < pattern.size() && pattern[p] == '*') {
        p += 1;
    }
    return p == pattern.size();
}

bool expand_glob(const std::string& pattern, std::vector<std::string>& out,
                 platform::OsError& err) {
    err.clear();
    out.clear();

    // Split on the LAST separator, and let the platform layer say what a
    // separator IS. This used to spell the set out as "/\\" on both platforms,
    // which is right on Windows and wrong on POSIX, where a backslash is an
    // ordinary filename character: `--glob 'odd\name*.mie'` was one pattern in
    // the current directory to Rust and Python (whose Path types agree with the
    // platform) and a `name*.mie` pattern inside a directory called `odd` here.
    // Identical expansion is the requirement (L2-MRG-001), and
    // `platform::is_separator` is where that question is already answered once.
    std::size_t slash = std::string::npos;
    for (std::size_t i = pattern.size(); i > 0; --i) {
        if (platform::is_separator(pattern[i - 1])) {
            slash = i - 1;
            break;
        }
    }
    std::string directory;
    std::string name_pattern;
    // Everything up to AND INCLUDING the separator, reused verbatim when
    // rebuilding each match. Rebuilding with a hardcoded '/' instead produced
    // `C:\dir/file.mie` for a Windows pattern -- which Windows happily OPENS,
    // so the decode still worked and only the paths the tool reported back were
    // mangled. A defect that survives every functional check and shows up in
    // log lines and error messages is worth more care, not less.
    std::string prefix;
    if (slash == std::string::npos) {
        directory = ".";
        name_pattern = pattern;
    } else {
        directory = pattern.substr(0, slash);
        name_pattern = pattern.substr(slash + 1);
        prefix = pattern.substr(0, slash + 1);
        if (directory.empty()) {
            // Rooted at the separator itself, e.g. "/recordings.mie".
            directory = pattern.substr(0, 1);
        }
    }

    // Wildcards apply to the filename only (L2-MRG-001 clause 2). Read
    // literally, `captures/**` is a directory named `**`, and the failure to
    // open it reported "No such file or directory" -- a missing folder, when
    // the mistake is the pattern.
    if (directory_has_wildcard(prefix)) {
        err.code = GLOB_INVALID_PATTERN;
        err.message =
            "wildcards are only supported in the filename, not in the directory part "
            "(no recursive **)";
        return false;
    }

    std::vector<std::string> names;
    if (!platform::list_directory(directory, names, err)) {
        return false;
    }

    for (std::size_t i = 0; i < names.size(); ++i) {
        // Name test before the stat: a directory of thousands of entries should
        // cost one stat per MATCH, not one per entry.
        if (!glob_match(name_pattern, names[i])) {
            continue;
        }
        const std::string candidate = prefix + names[i];
        // A glob resolves to FILES. `list_directory` returns every entry --
        // subdirectories, fifos, devices -- and matching them all is what made a
        // directory named `archive.mie` an input here while Rust and Python
        // silently skipped it: this implementation then failed to map it, and a
        // batch the other two decoded completely came back short (L2-MRG-001).
        //
        // `file_metadata` stats through symlinks, so a recording reached through
        // a link is a recording, and a dangling link answers false and is
        // skipped -- both matching the other two. An entry that cannot be
        // stat'ed at all is skipped rather than raised: it is a directory
        // listing racing a deletion, not a reason to fail the whole run.
        uint64_t size = 0;
        bool is_regular = false;
        platform::OsError probe;
        if (!platform::file_metadata(candidate, size, is_regular, probe) || !is_regular) {
            continue;
        }
        out.push_back(candidate);
    }
    // Lexicographic, so two hosts enumerating the same directory merge in the
    // same order. Directory order is whatever the filesystem feels like, which
    // would make the output depend on the machine that produced it.
    std::sort(out.begin(), out.end());
    return true;
}

// ---------------------------------------------------------------------------
// Options and dedup
// ---------------------------------------------------------------------------

MergeOptions::MergeOptions()
    : allow_partial(false),
      strict(false),
      collapse_duplicates(false),
      collapse_window_us(0),
      max_collapse_survivors(DEFAULT_MAX_COLLAPSE_SURVIVORS),
      delta_scope(DELTA_SCOPE_PER_FILE) {}

DedupKey::DedupKey() = default;

DedupKey DedupKey::of(const MieMessage& message) {
    DedupKey key;
    key.type_word = message.type_word;
    key.command_word = message.command_word;
    key.command_word_2 = message.command_word_2;
    key.status_word = message.status_word;
    key.status_word_2 = message.status_word_2;
    key.error_word = message.error_word;
    key.data_words = message.data_words;
    return key;
}

bool DedupKey::operator==(const DedupKey& other) const {
    return type_word.raw == other.type_word.raw && command_word == other.command_word &&
           command_word_2 == other.command_word_2 && status_word == other.status_word &&
           status_word_2 == other.status_word_2 && error_word == other.error_word &&
           data_words == other.data_words;
}

DedupWindow::Survivor::Survivor(uint64_t us_, std::size_t file_index_, const DedupKey& key_)
    : us(us_), file_index(file_index_), key(key_) {}

DedupWindow::DedupWindow(uint64_t window_us, std::size_t max_survivors)
    : window_us_(window_us),
      max_survivors_(max_survivors < MAX_COLLAPSE_SURVIVORS_MIN ? MAX_COLLAPSE_SURVIVORS_MIN
                                                                : max_survivors),
      capped_warned_(false) {}

bool DedupWindow::is_duplicate(uint64_t us, std::size_t file_index, const MieMessage& message) {
    // Evict on absolute distance, over the whole set rather than the front.
    // This costs one pass, which is what the match scan below already costs, so
    // the sorted-stream case is no slower than the front-only eviction it
    // replaces -- and the unsorted case is now bounded at all.
    //
    // `survivors_` is non-const here, so `begin()`/`end()` and the `remove_if`
    // result are all plain `iterator` -- which is what `erase` needs. GCC 4.8's
    // libstdc++ predates the C++11 erase overloads taking const iterators, so
    // handing one to a container mutator is banned in this tree; nothing here
    // can produce one.
    {
        const uint64_t window_for_evict = window_us_;
        const uint64_t now = us;
        const auto kept_end =
            std::remove_if(survivors_.begin(), survivors_.end(), [&](const Survivor& survivor) {
                return abs_diff(survivor.us, now) > window_for_evict;
            });
        survivors_.erase(kept_end, survivors_.end());
    }

    const DedupKey key = DedupKey::of(message);
    // A survivor matches only if it came from a DIFFERENT input and lies within
    // the window in ABSOLUTE time -- the merged stream may step backward, so
    // the distance has to be order-independent rather than a one-sided
    // subtraction.
    const bool matched =
        std::any_of(survivors_.begin(), survivors_.end(), [&](const Survivor& survivor) {
            return survivor.file_index != file_index && survivor.key == key;
        });
    if (matched) {
        return true;
    }

    // L2-MRG-008: make room rather than grow. Dropping the oldest ARRIVAL is the
    // honest choice once the window itself is over-full -- there is no "least
    // useful" survivor to pick, and arrival order is the one ordering that is
    // meaningful on input this badly behaved. Records are never dropped from the
    // OUTPUT; only the ability to recognise a later duplicate of this one is
    // given up.
    while (survivors_.size() >= max_survivors_) {
        survivors_.pop_front();
        if (!capped_warned_) {
            capped_warned_ = true;
            MIE_LOG_WARN("de-duplication survivor set hit the " +
                         text::decimal(static_cast<uint64_t>(max_survivors_)) +
                         "-record max_collapse_survivors cap; collapsing is best-effort past "
                         "this point (raise [merge] max_collapse_survivors / "
                         "--max-collapse-survivors, or narrow --collapse-window-us)");
        }
    }
    survivors_.emplace_back(us, file_index, key);
    return false;
}

// ---------------------------------------------------------------------------
// MergedSource
// ---------------------------------------------------------------------------

MergedSource::Entry::Entry() : us(0), file_index(0), seq(0) {}

bool MergedSource::Entry::operator<(const Entry& other) const {
    // Reversed: std::priority_queue is a max-heap and the merge wants the
    // smallest key first.
    if (us != other.us) {
        return us > other.us;
    }
    if (file_index != other.file_index) {
        return file_index > other.file_index;
    }
    return seq > other.seq;
}

MergedSource::MergedSource(const std::vector<MieFileReader*>& readers, const MergeOptions& options)
    : readers_(readers),
      next_seq_(readers.size(), 0),
      prev_us_(readers.size(), 0),
      has_prev_(readers.size(), false),
      warned_backward_(readers.size(), false),
      options_(options),
      delta_tracker_(options.standard_tick_rate_hz),
      dedup_(options.collapse_window_us, options.max_collapse_survivors),
      collapsed_(0) {
    iters_.reserve(readers_.size());
    paths_.reserve(readers_.size());
    for (std::size_t i = 0; i < readers_.size(); ++i) {
        paths_.push_back(readers_[i]->path());
        iters_.push_back(readers_[i]->iter());
    }

    for (std::size_t i = 0; i < iters_.size(); ++i) {
        MieMessage first;
        bool got = false;
        try {
            got = iters_[i].next(first);
        } catch (const MieError& error) {
            if (!options_.allow_partial) {
                throw;
            }
            // L2-MRG-004: drop the input and keep going. The file contributed
            // nothing (it failed at offset 0), and arming the terminal is what
            // makes the writer commit a `.partial` rather than a clean CSV that
            // silently omits an entire recording.
            MIE_LOG_WARN("merge: input #" + text::decimal(i) + " (" + paths_[i] +
                         ") could not be read; truncating it from the merge "
                         "(--allow-partial): " +
                         error.message());
            pending_terminal_.reset(new MieError(MieError::unrecoverable_sync_loss(0, 0)));
            continue;
        }
        if (!got) {
            continue;  // valid but empty recording; contributes nothing
        }
        check_mergeable(first, i, paths_[i]);

        Entry entry;
        entry.us = micros_of(first, options_.standard_tick_rate_hz);
        entry.file_index = i;
        entry.seq = 0;
        entry.message = first;
        prev_us_[i] = entry.us;
        has_prev_[i] = true;
        next_seq_[i] = 1;
        heap_.push(entry);
    }
}

uint64_t MergedSource::merge_micros(const MieMessage& message) const {
    return micros_of(message, options_.standard_tick_rate_hz);
}

void MergedSource::apply_global_delta(MieMessage& message) {
    if (options_.delta_scope == DELTA_SCOPE_PER_FILE) {
        // The default, and a deliberate no-op: it keeps the value the record's
        // own reader computed, which is what makes a merged DELTA identical to
        // a single-file one.
        return;
    }
    const CommandWord* command =
        message.command_word.has_value() ? &message.command_word.value() : nullptr;
    // No WARN here. A backward step on the merged timeline means some input was
    // not internally sorted, which `advance` already reports once per file
    // (L2-MRG-006); repeating it per record would bury that one message.
    message.delta = delta_tracker_.observe(command, message.timestamp).value();
}

void MergedSource::advance(std::size_t file_index) {
    MieMessage message;
    bool got = false;
    try {
        got = iters_[file_index].next(message);
    } catch (const MieError& error) {
        if (options_.allow_partial && error.kind() == KIND_UNRECOVERABLE_SYNC_LOSS) {
            MIE_LOG_WARN("merge: input #" + text::decimal(file_index) +
                         " truncated at its failure point: " + error.message());
            // Deferred until the heap drains, so every good record from every
            // input is written before the failure surfaces.
            pending_terminal_.reset(new MieError(error));
        } else {
            // Surfaced on the NEXT call, after the record already popped.
            pending_error_.reset(new MieError(error));
        }
        return;
    }
    if (!got) {
        return;  // this input is exhausted
    }

    const uint64_t us = merge_micros(message);
    // L2-MRG-006: each input is assumed internally time-sorted, because capture
    // order is chronological. A backward step means the merged output may be
    // out of order for this input.
    if (has_prev_[file_index] && us < prev_us_[file_index]) {
        if (options_.strict) {
            pending_error_.reset(new MieError(MieError::non_monotonic_input(
                file_index, paths_[file_index], prev_us_[file_index], us)));
        } else if (!warned_backward_[file_index]) {
            warned_backward_[file_index] = true;
            MIE_LOG_WARN("merge: input #" + text::decimal(file_index) + " (" + paths_[file_index] +
                         ") is not internally time-sorted: timestamp stepped backward (prev_us=" +
                         text::decimal(prev_us_[file_index]) + " curr_us=" + text::decimal(us) +
                         ") -- merged output may be out of order for this input (further "
                         "occurrences suppressed)");
        }
    }
    prev_us_[file_index] = us;
    has_prev_[file_index] = true;

    Entry entry;
    entry.us = us;
    entry.file_index = file_index;
    entry.seq = next_seq_[file_index];
    entry.message = message;
    next_seq_[file_index] = entry.seq + 1;
    heap_.push(entry);
}

bool MergedSource::next(MieMessage& out) {
    for (;;) {
        if (pending_error_) {
            throw take(pending_error_);
        }
        if (heap_.empty()) {
            if (pending_terminal_) {
                throw take(pending_terminal_);
            }
            return false;
        }

        const Entry entry = heap_.top();
        heap_.pop();
        const std::size_t file_index = entry.file_index;

        // Collapse BEFORE the global-DELTA stage (L2-MRG-007). A suppressed
        // duplicate must not advance the per-key DELTA cursor, or DELTA would
        // be measured across a timeline that includes rows nobody can see.
        if (options_.collapse_duplicates &&
            dedup_.is_duplicate(entry.us, file_index, entry.message)) {
            collapsed_ += 1;
            advance(file_index);
            continue;
        }

        out = entry.message;
        apply_global_delta(out);
        advance(file_index);
        return true;
    }
}

}  // namespace merge
}  // namespace mie
