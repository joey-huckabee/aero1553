// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::order"

#include "mie/order.hpp"

#include <algorithm>

#include "mie/log.hpp"
#include "mie/text.hpp"

namespace mie {

const std::size_t ORDER_DEFAULT_MAX_SORT_GROUP = 65536;

namespace {

/// The sort key: RT, then subaddress, then direction.
///
/// Keyed on the DECODED FIELDS, never on the rendered `MSG` string. `"11R"`
/// sorts before `"2R"` lexicographically, which is not the required order --
/// and a string key would be an easy, wrong-looking-right shortcut.
///
/// `DIRECTION_RECEIVE` is 0 and `DIRECTION_TRANSMIT` is 1, so R-before-T falls
/// out of the discriminants rather than needing a special case. Those values
/// are pinned by the wire format in all three implementations; do not renumber.
struct SortKey {
    uint8_t rt;
    uint8_t subaddress;
    uint8_t direction;

    SortKey() : rt(0), subaddress(0), direction(0) {}

    bool operator<(const SortKey& other) const {
        if (rt != other.rt) {
            return rt < other.rt;
        }
        if (subaddress != other.subaddress) {
            return subaddress < other.subaddress;
        }
        return direction < other.direction;
    }
};

/// One sortable "anchor" record plus any pinned records trailing it.
///
/// Chunks -- not individual records -- are what get sorted, which is how a pin
/// travels with the record it followed. `has_key` false marks a LEADING chunk:
/// pins that arrived before any anchor, which sort ahead of everything and so
/// stay at the front of the run where they arrived.
struct Chunk {
    bool has_key;
    SortKey key;
    /// Position in arrival order, so a sort can be undone exactly.
    std::size_t arrival;
    std::vector<MieMessage> records;

    Chunk() : has_key(false), arrival(0) {}

    bool operator<(const Chunk& other) const {
        if (has_key != other.has_key) {
            // Keyless (leading pins) first.
            return !has_key;
        }
        if (!has_key) {
            return false;
        }
        return key < other.key;
    }
};

bool key_of(const MieMessage& message, SortKey& out) {
    if (!message.command_word.has_value()) {
        return false;
    }
    const CommandWord& command = message.command_word.value();
    out.rt = command.rt;
    out.subaddress = command.subaddress;
    out.direction = command.direction == DIRECTION_TRANSMIT ? 1 : 0;
    return true;
}

/// True when two timestamps belong to the same ordering group.
///
/// Compares the VARIANT as well as the value. A mixed IRIG/Standard stream is
/// impossible today -- the format is resolved once per file and a merge rejects
/// mixed inputs -- so an equality test on the value alone would be silently
/// wrong if that ever changed, comparing a counter tick against a microsecond.
bool same_group(const Timestamp& a, const Timestamp& b) {
    if (a.format_kind != b.format_kind) {
        return false;
    }
    if (a.is_irig()) {
        return a.irig.to_total_microseconds() == b.irig.to_total_microseconds();
    }
    if (a.is_standard()) {
        return a.standard.raw_value == b.standard.raw_value;
    }
    return false;
}

/// Orders chunks by arrival, to undo a sort.
bool earlier_arrival(const Chunk& a, const Chunk& b) { return a.arrival < b.arrival; }

/// Stable-sort one buffered run, keeping each Command-Word-less record attached
/// to the record it followed on input.
///
/// `keep_tail` is set when the run is being closed by a pinned record at a
/// DIFFERENT timestamp (L2-WRT-021). That record will follow whatever this run
/// ends with, so if sorting would move the chunk that arrived last away from the
/// end, the run is returned in arrival order instead: canonical tie order is
/// given up for this one run so that the pin still follows its predecessor and
/// timestamps still ascend. `fell_back` reports that.
std::vector<MieMessage> sort_run(const std::vector<MieMessage>& run, bool keep_tail,
                                 bool& fell_back) {
    fell_back = false;
    std::vector<Chunk> chunks;
    for (std::size_t i = 0; i < run.size(); ++i) {
        SortKey key;
        if (key_of(run[i], key)) {
            Chunk chunk;
            chunk.has_key = true;
            chunk.key = key;
            chunk.arrival = chunks.size();
            chunk.records.push_back(run[i]);
            chunks.push_back(chunk);
        } else if (!chunks.empty()) {
            // A pin joins the chunk of the record it followed, so the sort
            // moves the two together. Preserving its INDEX instead would leave
            // it trailing whichever record the sort happened to put in that
            // slot -- which is precisely the 0x2000 continuation adjacency the
            // pin exists to protect.
            chunks.back().records.push_back(run[i]);
        } else {
            Chunk chunk;  // leading pins: no anchor yet
            chunk.records.push_back(run[i]);
            chunks.push_back(chunk);
        }
    }

    if (chunks.size() > 1) {
        const std::size_t last_arrival = chunks.size() - 1;
        // stable_sort, not sort: chunks with a fully equal key must keep their
        // relative input order (L1-OUT-003).
        std::stable_sort(chunks.begin(), chunks.end());
        if (keep_tail && chunks.back().arrival != last_arrival) {
            // Undo the sort: arrival positions are unique, so this restores
            // input order exactly.
            std::sort(chunks.begin(), chunks.end(), earlier_arrival);
            fell_back = true;
        }
    }

    std::vector<MieMessage> out;
    out.reserve(run.size());
    for (std::size_t i = 0; i < chunks.size(); ++i) {
        for (std::size_t j = 0; j < chunks[i].records.size(); ++j) {
            out.push_back(chunks[i].records[j]);
        }
    }
    return out;
}

}  // namespace

OrderedSource::OrderedSource(MessageSource& inner, std::size_t max_sort_group)
    : inner_(&inner), max_group_(max_sort_group > 0 ? max_sort_group : 1), done_(false) {}

void OrderedSource::flush(bool keep_tail) {
    if (buffer_.empty()) {
        return;
    }
    bool fell_back = false;
    const std::vector<MieMessage> sorted = sort_run(buffer_, keep_tail, fell_back);
    if (fell_back) {
        MIE_LOG_DEBUG("equal-timestamp run at " + buffer_.front().timestamp.format() +
                      " emitted in arrival order: canonical order would separate its last "
                      "record from the following record that has no Command Word");
    }
    buffer_.clear();
    // Reversed, so emission pops the back rather than erasing the front.
    for (std::size_t i = sorted.size(); i > 0; --i) {
        pending_.push_back(sorted[i - 1]);
    }
}

void OrderedSource::flush_capped() {
    // A cap of 1 is the documented "off" switch, not an overflow: it flushes
    // silently. A WARN per record was a flood, and a measured 40x slowdown.
    if (max_group_ > 1) {
        MIE_LOG_WARN("equal-timestamp run at " +
                     (buffer_.empty() ? std::string() : buffer_.front().timestamp.format()) +
                     " reached the " + text::decimal(max_group_) +
                     "-record max_sort_group cap; emitting these records in arrival order "
                     "and gathering the rest of the run afresh (raise [output] "
                     "max_sort_group / --max-sort-group to restore canonical RT/MSG order "
                     "for it)");
    }
    for (std::size_t i = buffer_.size(); i > 0; --i) {
        pending_.push_back(buffer_[i - 1]);
    }
    buffer_.clear();
}

void OrderedSource::accept(const MieMessage& message) {
    const bool starts_new_run =
        !buffer_.empty() && !same_group(buffer_.front().timestamp, message.timestamp);
    if (starts_new_run) {
        // A record with no Command Word is pinned to whatever this run ends
        // with, so that end must be the record it actually followed.
        flush(!message.command_word.has_value());
    }
    buffer_.push_back(message);
    if (buffer_.size() >= max_group_) {
        flush_capped();
    }
}

bool OrderedSource::next(MieMessage& out) {
    for (;;) {
        // Drain what is already ordered before touching the inner stream, so a
        // consumer that stops early still receives everything this stage has
        // committed to emitting.
        if (!pending_.empty()) {
            out = pending_.back();
            pending_.pop_back();
            return true;
        }
        if (deferred_) {
            // Re-raised only now that the buffered run has been emitted. Under
            // --allow-partial those rows belong in the committed .partial.
            const std::exception_ptr raised = deferred_;
            deferred_ = std::exception_ptr();
            std::rethrow_exception(raised);
        }
        if (done_) {
            return false;
        }

        MieMessage message;
        bool got = false;
        try {
            got = inner_->next(message);
        } catch (...) {
            // Flush FIRST, defer the throw. Letting it through here would drop
            // a whole equal-timestamp group from the operator's only record of
            // what was decoded.
            deferred_ = std::current_exception();
            flush(false);
            continue;
        }

        if (!got) {
            done_ = true;
            flush(false);
            continue;
        }
        accept(message);
    }
}

}  // namespace mie
