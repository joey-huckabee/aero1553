// SPDX-License-Identifier: Apache-2.0
//
// Message filter configuration.
//
// Mirrors `rust/src/filter.rs` and `python/src/aero1553/filters.py`.
//
// EXCLUDE AND INCLUDE ARE NOT OPPOSITES, and the asymmetry is the whole of the
// semantics. An empty include set means "no include constraint", never "include
// nothing" -- otherwise a config that set only `exclude_rts` would drop every
// record in the file. Exclusion is checked first and still applies afterwards,
// so the narrower rule always wins.
//
// A record with NO Command Word (SPURIOUS_DATA) has no RT and no subaddress. It
// therefore cannot satisfy an RT or subaddress INCLUDE filter, and is dropped
// when one is active -- an operator narrowing to RT 5 is not asking to keep
// records that have no RT at all. It is unaffected by RT or subaddress
// EXCLUDE filters, which can only match a value it does not have.

#ifndef MIE_FILTER_HPP
#define MIE_FILTER_HPP

#include <cstdint>
#include <vector>

#include "mie/models.hpp"
#include "mie/optional.hpp"
#include "mie/source.hpp"

namespace mie {

/// Which messages to keep.
///
/// EXCLUDE and INCLUDE are both supported, and they are not opposites. An
/// empty include set means "no include constraint", not "include nothing" --
/// otherwise a config that set only `exclude_rts` would drop every record.
/// Where both are present the include set is checked first and exclusion still
/// applies, so the narrower rule wins.
struct FilterConfig {
    std::vector<uint8_t> exclude_types;
    std::vector<uint8_t> exclude_rts;
    std::vector<Bus> exclude_buses;
    std::vector<uint8_t> exclude_subaddresses;

    std::vector<uint8_t> include_types;
    std::vector<uint8_t> include_rts;
    std::vector<Bus> include_buses;
    std::vector<uint8_t> include_subaddresses;

    FilterConfig();

    /// True when any set is populated, so the caller can skip the stage
    /// entirely rather than run a predicate that cannot reject anything.
    bool is_active() const;

    /// True when `message` should be dropped from the output, judged on its
    /// own. In a stream, `FilteredSource` judges a 0x2000 continuation through
    /// its parent instead (L2-FLT-003).
    bool should_exclude(const MieMessage& message) const;

    /// The parent half of the pairing rule (L2-FLT-003): would `parent`'s RT,
    /// subaddress, bus or type-exclusion filters drop it? `include_types` is
    /// left out on purpose -- a type SELECTION is judged on each record's own
    /// type, so `--include-types SPURIOUS_DATA` keeps the continuations it
    /// asked for even though it drops their parents.
    bool excludes_as_parent(const MieMessage& parent) const;

    /// The continuation half: is the record's own type excluded, or left out
    /// of an active type selection?
    bool excludes_type_of(const MieMessage& message) const;

  private:
    /// `should_exclude`, with the type selection optionally skipped.
    bool excluded(const MieMessage& message, bool with_types) const;
};

/// A `MessageSource` that drops what the filters exclude.
///
/// Wraps another source and presents the same interface, so it can be inserted
/// into the pipeline without any other stage knowing it is there.
class FilteredSource : public MessageSource {
  public:
    /// Logs the active sets once, at INFO. `inner` must outlive this.
    FilteredSource(MessageSource& inner, const FilterConfig& filters);

    /// Emits the passed/excluded tally.
    ///
    /// From the DESTRUCTOR rather than at end of stream, matching Rust's
    /// `Drop`: a consumer can stop early -- a broken pipe, `| head` -- and an
    /// end-of-stream hook would simply never run, silently losing the tally
    /// exactly when the operator most wants to know how much was dropped.
    ~FilteredSource() override;

    bool next(MieMessage& out) override;

    uint64_t passed() const { return passed_; }
    uint64_t excluded() const { return excluded_; }

  private:
    /// Whether to drop `message`, applying the continuation pairing rule.
    bool drops(const MieMessage& message);

    MessageSource* inner_;
    FilterConfig filters_;
    uint64_t passed_;
    uint64_t excluded_;
    /// For the record just seen, when it was an errored (non-spurious) one:
    /// whether it passed the parent-side filters. Absent after any other
    /// record, so a continuation with no errored record before it is judged on
    /// its own. A continuation always arrives directly after its parent -- the
    /// reader emits them back to back, they share one timestamp so a merge
    /// cannot separate them, and collapse keeps or drops them together -- so
    /// one remembered verdict is all the state this needs.
    Optional<bool> parent_kept_;
};

}  // namespace mie

#endif  // MIE_FILTER_HPP
