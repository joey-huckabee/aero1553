// SPDX-License-Identifier: Apache-2.0
//
// Per-RT/MSG DELTA tracking -- the one definition of "the same message", and
// the one place the inter-arrival arithmetic lives.
//
// WHY THIS IS ITS OWN MODULE
//
// In the Rust and Python implementations DELTA was tracked twice, keyed two
// different ways: a packed integer inside the reader and the
// `"<rt>:<sa><T|R>"` display string inside the merge. Python was worse again --
// the reader spelled the key a third way, hand-building the string on the
// errored-record path because the message does not exist yet there. Nothing
// anywhere asserted that the spellings agreed on what "the same RT/MSG" means.
// They did agree, by care rather than by construction.
//
// C++ reaches this point with `merge` still unwritten, which is the whole
// reason to land the shared type now: the second caller can be built on it
// instead of reproducing the split and then having it extracted back out.
//
// THIS MODULE DOES NOT LOG
//
// `observe()` returns a DeltaOutcome describing what happened, and the caller
// decides whether to say anything. Same rule as `sync`, same reason: a tracker
// cannot know whether a backward step deserves a WARN (single-file decode: yes,
// once per key) or is already reported at file granularity (a merge naming its
// unsorted inputs, L2-MRG-006).
//
// The "have I already mentioned this key" bookkeeping IS kept here, so the
// once-per-key promise has one owner rather than a set in each caller.
//
// Mirrors `rust/src/delta.rs` and `python/src/aero1553/delta.py`.

#ifndef MIE_DELTA_HPP
#define MIE_DELTA_HPP

#include <cstdint>
#include <map>
#include <set>

#include "mie/models.hpp"
#include "mie/optional.hpp"

namespace mie {

/// Pack `(rt, subaddress, direction)` into one key.
///
/// **This is the definition of "the same message" for DELTA purposes.** The
/// three fields are 5, 5 and 1 bits on the wire, so the packing is lossless
/// with room to spare, and an integer key keeps the per-record path free of the
/// allocation a formatted string would cost.
///
/// `MieMessage::delta_key()` is the display spelling of this same tuple.
/// `test_delta.cpp` asserts the two partition the `(rt, subaddress, direction)`
/// space identically -- the check that could not exist while the two
/// representations lived in different modules.
uint32_t delta_key(uint8_t rt, uint8_t subaddress, bool transmit);

/// Which of the five things one observation turned out to be.
enum DeltaKind {
    /// First sighting of this key. DELTA is `0.000000` (L2-RDR-010).
    DELTA_FIRST,
    /// A non-negative gap, carried in `DeltaOutcome::seconds`.
    DELTA_ELAPSED,
    /// The clock went backwards for this key (L2-RDR-017).
    DELTA_BACKWARD,
    /// Standard timestamp with no configured tick rate (L2-RDR-019).
    DELTA_UNCALIBRATED,
    /// No RT/MSG key at all -- SPURIOUS_DATA (L2-RDR-018).
    DELTA_NO_KEY
};

/// What one observation meant.
///
/// Deliberately richer than the `Optional<double>` the CSV eventually needs,
/// because the column cannot distinguish "no gap yet" from "no honest gap" from
/// "no key at all" -- and the caller has to, in order to narrate correctly.
struct DeltaOutcome {
    DeltaKind kind;
    /// Set for DELTA_ELAPSED.
    double seconds;
    /// Set for DELTA_BACKWARD.
    uint64_t prev_us;
    /// Set for DELTA_BACKWARD.
    uint64_t curr_us;
    /// Set for DELTA_BACKWARD, so a caller can name the key in a diagnostic
    /// without re-deriving it.
    uint32_t key;
    /// True exactly once per key per tracker, so a caller that warns on a
    /// backward step gets one line per key rather than one per record.
    bool first_for_key;

    DeltaOutcome();

    /// The value the `DELTA` column takes -- where four of the five outcomes
    /// collapse into an empty cell.
    Optional<double> value() const;
};

/// Last-seen timestamp per RT/MSG key, and the gap arithmetic over it.
///
/// One instance per DELTA scope: the reader makes one per file, and the merge
/// will make one for the whole merged timeline under `--delta-scope global`
/// (L2-MRG-005).
class DeltaTracker {
  public:
    DeltaTracker();
    /// `tick_rate_hz` is the L2-DEC-017 Standard-counter calibration. Absent
    /// keeps Standard records out of tracking entirely.
    explicit DeltaTracker(const Optional<double>& tick_rate_hz);

    /// Record one message and report the gap since the previous one sharing its
    /// RT/MSG key.
    ///
    /// `command_word` is null for SPURIOUS_DATA, which has no key. Taking the
    /// Command Word rather than a whole MieMessage is what lets the reader call
    /// this *before* the message exists -- on the errored-record path the DELTA
    /// is computed and then handed to the constructor.
    DeltaOutcome observe(const CommandWord* command_word, const Timestamp& timestamp);

  private:
    std::map<uint32_t, uint64_t> last_us_;
    /// Keys that have already produced a backward-step report.
    std::set<uint32_t> warned_keys_;
    Optional<double> tick_rate_hz_;
};

}  // namespace mie

#endif  // MIE_DELTA_HPP
