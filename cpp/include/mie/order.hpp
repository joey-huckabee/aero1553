// SPDX-License-Identifier: Apache-2.0
//
// Canonical row order for equal-timestamp ties (L1-OUT-003, L2-WRT-021).
//
// Mirrors `rust/src/order.rs` and `python/src/aero1553/order.py`.
//
// THE STAGE IS RUN-SCOPED, AND THAT IS THE DESIGN POINT. It permutes only a run
// of *consecutive* records that share a `TIME_STAMP`, never the file. A
// whole-file sort would break the constant-memory guarantee every other stage
// upholds, and would violate L2-MRG-006's "never re-sort" rule for a merged
// stream. In practice a run is one or two records: a 1553 bus carries one
// transaction at a time, so real ties come from the two concurrent buses or
// from overlapping recorders in a merge.
//
// THE RUN IS CAPPED (L2-WRT-022). The pathological case is real -- a corrupt
// recording whose timestamps all decode to the same value would otherwise
// buffer the whole file -- so `max_sort_group` turns the one data-dependent
// buffer in the pipeline back into a constant. At the cap the run is emitted in
// arrival order with one WARN. Removing the cap silently reintroduces an
// unbounded allocation.
//
// PINNING IS THE SUBTLE PART. A record with no Command Word (SPURIOUS_DATA) has
// no sort key, and its position is defined RELATIVE TO ITS PREDECESSOR: the
// card writes the leftover words of an errored transaction as a SPURIOUS record
// immediately after it, and the `0x2000` continuation code means "continues the
// record before me". So a pin must travel WITH the record it followed, not sit
// at a preserved index. Sorting by index and holding pins in place looks
// equivalent and is not -- it leaves the pin trailing whichever record the sort
// happened to move into that slot, which breaks exactly the adjacency the
// `0x2000` code depends on.
//
// A pin protects its predecessor only within one run, and a SPURIOUS record
// carries its own timestamp. One stamped LATER opens the next run and follows
// whatever this run ends with -- so when sorting would move the record it
// actually followed away from that end, the run is emitted in arrival order
// instead (L2-WRT-021, review finding M3).

#ifndef MIE_ORDER_HPP
#define MIE_ORDER_HPP

#include <cstddef>
#include <exception>
#include <vector>

#include "mie/models.hpp"
#include "mie/source.hpp"

namespace mie {

/// L2-WRT-022 default cap on one buffered equal-timestamp run.
extern const std::size_t ORDER_DEFAULT_MAX_SORT_GROUP;

/// A `MessageSource` that imposes canonical row order on equal-timestamp runs.
///
/// Positioned as the LAST stage before the writer, on both the single-file and
/// merge paths. Anything downstream of it would be reordering rows the operator
/// is about to read.
class OrderedSource : public MessageSource {
  public:
    /// `max_sort_group` is clamped to at least 1; `1` disables reordering and
    /// restores raw DDC capture order. `inner` must outlive this.
    OrderedSource(MessageSource& inner, std::size_t max_sort_group);

    /// Next record in canonical order.
    ///
    /// A throw from the inner source is deferred until the buffered run has
    /// been emitted. That is not politeness: under `--allow-partial` the rows
    /// this stage is holding must reach the committed `.partial`, and letting
    /// the throw through first would drop a whole equal-timestamp group from
    /// the operator's only record of what was decoded.
    bool next(MieMessage& out) override;

  private:
    /// Move the buffered run into the emission queue, sorting it first.
    ///
    /// `keep_tail`: the run is being closed by a record with no Command Word at
    /// a different timestamp, which will follow whatever this run ends with. If
    /// sorting would move the record that arrived last away from the end, the
    /// run is emitted in arrival order instead (L2-WRT-021).
    void flush(bool keep_tail);
    /// Emit a capped run in arrival order, with one WARN.
    void flush_capped();
    /// Add one record, flushing first if it opens a new run.
    void accept(const MieMessage& message);

    MessageSource* inner_;
    /// The run of consecutive equal-timestamp records being accumulated.
    std::vector<MieMessage> buffer_;
    /// Records sorted and awaiting emission, held REVERSED so the back is the
    /// front of the queue and emission is a pop rather than an erase-from-front.
    std::vector<MieMessage> pending_;
    std::size_t max_group_;
    bool done_;
    /// A throw seen while a run was buffered, re-raised after the flush.
    std::exception_ptr deferred_;
};

}  // namespace mie

#endif  // MIE_ORDER_HPP
