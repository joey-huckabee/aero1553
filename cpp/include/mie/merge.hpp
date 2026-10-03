// SPDX-License-Identifier: Apache-2.0
//
// Multi-file, time-sorted streaming k-way merge (L1-MRG-*, L2-MRG-*).
//
// Mirrors `rust/src/merge.rs` and `python/src/aero1553/merge.py`.
//
// Takes several decoded recordings and yields one stream of records in global
// time order, holding at most ONE record per open file in a min-heap. Resident
// memory is O(number of files) and independent of the record count
// (L2-MRG-002), which is the same constant-memory property the single-file path
// has and the reason this is a merge rather than a concatenate-then-sort.
//
// The merged stream is a `MessageSource`, so it drops into the same pipeline
// the single-file path uses -- filter, order, writer, all unchanged. That is
// the whole payoff of the Decorator arrangement in `source.hpp`.
//
// EVERY INPUT MUST BE CALENDAR-LOCKED IRIG. A Standard-format input, one that
// leads with a freerun IRIG record, or a set that mixes the two is rejected up
// front (`incompatible_merge_inputs`, exit 6 -- L2-MRG-003). None of those can
// be placed on a common absolute timeline, and merging them would silently
// interleave records by a number that means something different in each file.
//
// DELTA IS PER-FILE BY DEFAULT (L2-MRG-005). Each reader already computed it
// while walking its own file, so the default scope leaves it alone -- which is
// what makes a merged record's DELTA identical to the value the same record
// gets from a single-file decode, by construction rather than by agreement.
// `--delta-scope global` recomputes across the merged timeline instead, using
// the SAME `DeltaTracker` the reader uses. Rust and Python each reached this
// second caller with a second tracker keyed differently from the reader's, and
// nothing asserted the two agreed on what "the same RT/MSG" meant; C++ has one
// definition because `delta.hpp` landed before this module did.
//
// NO NEW DEPENDENCY: the heap is `std::priority_queue` and the `--glob` matcher
// is hand-rolled, preserving the no-runtime-dependency rule (L3-CPP-002).

#ifndef MIE_MERGE_HPP
#define MIE_MERGE_HPP

#include <cstddef>
#include <deque>
#include <queue>
#include <string>
#include <vector>

#include "mie/delta.hpp"
#include "mie/models.hpp"
#include "mie/optional.hpp"
#include "mie/platform.hpp"
#include "mie/reader.hpp"
#include "mie/source.hpp"

namespace mie {
namespace merge {

/// Maximum inputs one merge may process.
///
/// Bounds open mappings and file descriptors so resource use is predictable;
/// exceeding it is a usage error (exit 4), not a resource exhaustion at record
/// 200 of 4 million. Shared in value with the other implementations
/// (L3-RS-014 / L3-PY-014).
const std::size_t MAX_MERGE_FILES = 256;

// ---------------------------------------------------------------------------
// Input resolution
// ---------------------------------------------------------------------------

/// Read a manifest into a list of paths: one per line, in order.
///
/// Blank lines and lines whose first non-blank character is `#` are ignored;
/// surrounding blanks are trimmed (L2-MRG-001). Order is the file's order and
/// is NOT sorted -- a manifest is how an operator states an order explicitly.
///
/// False on an I/O failure, with `err` set.
bool read_manifest(const std::string& path, std::vector<std::string>& out, platform::OsError& err);

/// Whole-string wildcard match: `*` matches any run including empty, `?`
/// matches exactly one character. Nothing else is special -- no character
/// classes, no brace expansion, no `**`.
///
/// Iterative with backtracking, not recursive: a pattern of many `*` against a
/// long name is exponential in a naive recursive matcher, and the input here is
/// an operator-supplied string.
///
/// `?` consumes one whole UTF-8 CHARACTER, not one byte. The other two
/// implementations match over Unicode scalar values, and a byte-wise `?` would
/// quietly disagree with them on any non-ASCII filename -- matching `a?c`
/// against a two-byte character where they would not.
bool glob_match(const std::string& pattern, const std::string& name);

/// Expand a single-directory glob `DIR/PATTERN`, or `PATTERN` in the working
/// directory. Wildcards apply to the FILENAME only -- there is no recursive
/// descent. Returns matching regular files sorted lexicographically by path, so
/// the merge order is deterministic and identical across implementations
/// (L2-MRG-001).
///
/// False on an I/O failure, with `err` set. A glob that matches nothing is not
/// an I/O failure: it returns true with an empty list, and the caller decides
/// what to say about it.
///
/// Also false, with `err.code == GLOB_INVALID_PATTERN` and no I/O attempted,
/// when the directory part holds a `*` or `?` (`captures/**/*.mie`): wildcards
/// apply to the filename only, and the CLI reports this as a usage error.
bool expand_glob(const std::string& pattern, std::vector<std::string>& out, platform::OsError& err);

/// The `OsError::code` `expand_glob` sets for a wildcard in the directory part.
/// Negative so it can never collide with an errno or a Win32 error code.
const int GLOB_INVALID_PATTERN = -1;

// ---------------------------------------------------------------------------
// The merge
// ---------------------------------------------------------------------------

/// How the merge behaves. A struct rather than the builder chain the Rust
/// implementation uses, matching `ReaderOptions` and `WriteOptions` in this
/// tree.
struct MergeOptions {
    /// L2-DEC-017 Standard-counter calibration, passed through to the DELTA
    /// tracker. Merge inputs are IRIG by definition, so this only matters to
    /// global-scope DELTA.
    Optional<double> standard_tick_rate_hz;
    /// L2-MRG-004: a file that cannot be read is dropped from the merge with a
    /// WARN, and the run commits what it decoded, rather than failing.
    bool allow_partial;
    /// L2-MRG-006: in strict mode a within-file backward timestamp step fails
    /// the batch; lenient mode WARNs once per file.
    bool strict;
    /// L2-MRG-007: suppress a record already seen from a DIFFERENT input within
    /// `collapse_window_us`.
    bool collapse_duplicates;
    uint64_t collapse_window_us;
    /// L2-MRG-008: cap on the retained survivor set. The window bounds
    /// retention in TIME; this bounds it in COUNT.
    std::size_t max_collapse_survivors;
    /// L2-MRG-005.
    DeltaScope delta_scope;

    MergeOptions();
};

/// Content identity of a record, for cross-recorder de-duplication.
///
/// The bits a recorder reads off the wire: Type Word, Command and Status Words,
/// Error Word, and the valid data words. Timestamp, file offset, MUX and DELTA
/// are deliberately EXCLUDED -- two recorders witnessing one bus transaction
/// disagree on all four, and including any of them would make the collapse
/// never fire. The timestamp drives the window instead of the equality.
struct DedupKey {
    TypeWord type_word;
    Optional<CommandWord> command_word;
    Optional<CommandWord> command_word_2;
    Optional<uint16_t> status_word;
    Optional<uint16_t> status_word_2;
    Optional<uint16_t> error_word;
    /// The payload as the decoder holds it. `DataWords` already compares only
    /// its live prefix, which is the comparison this key wants, and keeping the
    /// inline buffer means a dedup window of survivors allocates nothing per
    /// entry -- the bounded-memory claim above stays true by construction.
    DataWords data_words;

    DedupKey();
    static DedupKey of(const MieMessage& message);
    bool operator==(const DedupKey& other) const;
};

/// Sliding time-window de-duplicator over the merged stream (L2-MRG-007).
///
/// Holds only the survivors emitted within `window_us`, so resident memory is
/// bounded by the window rather than the record count. In a sorted stream an
/// older survivor can never match a later record, which is what makes the
/// eviction safe.
/// Sliding time-window de-duplicator over the merged stream (L2-MRG-007), with
/// the survivor-set cap of L2-MRG-008.
///
/// Retention is defined on the ABSOLUTE time distance to the current record and
/// is therefore independent of the order survivors were appended in: a survivor
/// is kept iff `|survivor_us - us| <= window_us`. That order independence is the
/// requirement, not an optimisation. This used to evict only from the FRONT,
/// testing the one-sided `us - front_us`, which is correct only while the stream
/// is sorted. After a lenient backward step (L2-MRG-006) the front can hold a
/// timestamp in the FUTURE of the current record, the one-sided test is then
/// never true, and the front never leaves -- blocking eviction of everything
/// behind it. An alternating 1000us / 0us probe with a zero-width window
/// retained all 10 000 records and ran quadratically (2x the records, 4x the
/// time), contradicting the bounded-memory guarantee L2-MRG-007 makes and
/// L2-MRG-002 depends on.
///
/// The window bounds retention in TIME; it does not bound it in COUNT. A corrupt
/// recording whose timestamps all decode to one value, or a wide operator-set
/// `collapse_window_us` on a dense bus, puts arbitrarily many records inside one
/// window. `max_survivors` is the second, independent bound that makes the
/// guarantee unconditional -- the same reasoning, and the same default, as
/// `max_sort_group` (L2-WRT-022) applies to the reorder stage.
class DedupWindow {
  public:
    DedupWindow(uint64_t window_us, std::size_t max_survivors);

    /// True when `message` duplicates a recent survivor from a DIFFERENT input
    /// within the window. Same-file identical content is never a duplicate --
    /// a bus really can carry the same transaction twice, and dropping the
    /// second would be losing data rather than de-duplicating it.
    ///
    /// A non-duplicate is recorded as a survivor.
    bool is_duplicate(uint64_t us, std::size_t file_index, const MieMessage& message);

    /// How many survivors are currently retained.
    ///
    /// Exposed for the L2-MRG-007 / L2-MRG-008 bound tests: the guarantee they
    /// check is about resident SIZE, and asserting it through collapse
    /// behaviour alone would not distinguish "bounded" from "happens not to
    /// have grown yet".
    std::size_t survivor_count() const { return survivors_.size(); }

  private:
    struct Survivor {
        uint64_t us;
        std::size_t file_index;
        DedupKey key;
        Survivor(uint64_t us_, std::size_t file_index_, const DedupKey& key_);
    };

    uint64_t window_us_;
    std::size_t max_survivors_;
    std::deque<Survivor> survivors_;
    /// One WARN per merge, not per capped record: a pathological input hits the
    /// cap on nearly every record, and the cadence that matters to an operator
    /// is "this run stopped being exact", once. Mirrors the one-WARN-per-input
    /// cadence L2-MRG-006 uses for the same reason.
    bool capped_warned_;
};

/// Streaming k-way merge over per-file readers.
///
/// The readers are BORROWED, not owned: they must outlive this object. That
/// mirrors the Rust signature taking a slice, and it keeps this module free of
/// the question of how the CLI chose to store them.
class MergedSource : public MessageSource {
  public:
    /// Prime the merge: pull each file's first record and validate it is
    /// calendar-locked IRIG.
    ///
    /// Throws `incompatible_merge_inputs` for an input that cannot anchor an
    /// absolute timeline (L2-MRG-003). Under `allow_partial`, a file that fails
    /// to produce a first record is skipped with a WARN instead of failing the
    /// batch (L2-MRG-004).
    MergedSource(const std::vector<MieFileReader*>& readers, const MergeOptions& options);

    bool next(MieMessage& out) override;

    /// Records suppressed as cross-recorder duplicates, for the end-of-run
    /// summary. Read after the source is drained.
    uint64_t collapsed() const { return collapsed_; }

  private:
    /// One record at the front of an input, ordered by the merge key
    /// `(microseconds, file index, within-file sequence)`.
    ///
    /// The file index and sequence are what make the order TOTAL: without them
    /// two records sharing a timestamp would compare equal and the heap could
    /// return them in either order, so the same inputs could produce different
    /// bytes on different runs. This is the heap's INTERNAL order only -- the
    /// equal-timestamp order the CSV shows is `order.hpp`'s canonical RT/MSG
    /// order, which a heap key cannot produce because the heap never holds two
    /// same-timestamp records from one input at once.
    struct Entry {
        uint64_t us;
        std::size_t file_index;
        uint64_t seq;
        MieMessage message;

        Entry();
        /// Reversed so `std::priority_queue`, which is a MAX-heap, yields the
        /// smallest key. Spelling it here rather than passing `std::greater`
        /// keeps the reversal next to the reason for it.
        bool operator<(const Entry& other) const;
    };

    void advance(std::size_t file_index);
    void apply_global_delta(MieMessage& message);
    uint64_t merge_micros(const MieMessage& message) const;

    std::vector<MieFileReader*> readers_;
    std::vector<RecordIter> iters_;
    std::priority_queue<Entry> heap_;
    std::vector<uint64_t> next_seq_;
    /// Microsecond key of the previous record from each input, and whether one
    /// has been seen. Used to detect a within-file backward step (L2-MRG-006).
    std::vector<uint64_t> prev_us_;
    std::vector<bool> has_prev_;
    /// One-time-per-file guard so a non-monotonic input WARNs at most once,
    /// mirroring the single-file non-monotonic-DELTA WARN. Repeating it per
    /// record would bury the message in its own output.
    std::vector<bool> warned_backward_;
    /// Whether the previous record pulled from each input was collapsed as a
    /// duplicate. A 0x2000 continuation is the next record of its file after
    /// its parent, so this is its parent's fate, which it shares (L2-MRG-007).
    std::vector<bool> last_collapsed_;
    std::vector<std::string> paths_;

    MergeOptions options_;
    DeltaTracker delta_tracker_;
    DedupWindow dedup_;

    /// Raise on the NEXT call: a mid-stream failure without `--allow-partial`,
    /// which fails the batch but only after the record already popped has been
    /// handed over.
    PendingError pending_error_;
    /// Raise once the heap drains: an `--allow-partial` deferred unrecoverable
    /// loss. Deferring it is what lets every good record reach the writer first
    /// so it can commit a `.partial`.
    PendingError pending_terminal_;

    uint64_t collapsed_;
};

}  // namespace merge
}  // namespace mie

#endif  // MIE_MERGE_HPP
