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

    /// True when `message` should be dropped from the output.
    bool should_exclude(const MieMessage& message) const;
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
    MessageSource* inner_;
    FilterConfig filters_;
    uint64_t passed_;
    uint64_t excluded_;
};

}  // namespace mie

#endif  // MIE_FILTER_HPP
