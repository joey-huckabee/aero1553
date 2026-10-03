// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::filter"

#include "mie/filter.hpp"

#include <algorithm>

#include "mie/log.hpp"
#include "mie/text.hpp"

namespace mie {

namespace {

template <typename T>
bool contains(const std::vector<T>& values, T wanted) {
    return std::find(values.begin(), values.end(), wanted) != values.end();
}

/// A set rendered for the one-time INFO line: `none`, or `[a, b, c]`.
///
/// SORTED, and that is not cosmetic. Python holds these as sets, whose
/// iteration order is not guaranteed; sorting is what makes the three
/// implementations emit the same line for the same configuration rather than
/// the same line most of the time.
template <typename T>
std::string show(const std::vector<T>& values) {
    if (values.empty()) {
        return "none";
    }
    std::vector<T> sorted(values);
    std::sort(sorted.begin(), sorted.end());
    std::string out = "[";
    for (std::size_t i = 0; i < sorted.size(); ++i) {
        if (i != 0) {
            out += ", ";
        }
        out += text::decimal(static_cast<uint64_t>(sorted[i]));
    }
    out += "]";
    return out;
}

/// Buses render as A / B rather than 0 / 1, matching the CSV column and the
/// other two implementations.
std::string show_buses(const std::vector<Bus>& values) {
    if (values.empty()) {
        return "none";
    }
    std::vector<Bus> sorted(values);
    std::sort(sorted.begin(), sorted.end());
    std::string out = "[";
    for (std::size_t i = 0; i < sorted.size(); ++i) {
        if (i != 0) {
            out += ", ";
        }
        out += bus_name(sorted[i]);
    }
    out += "]";
    return out;
}

void log_active_filters(const FilterConfig& filters) {
    MIE_LOG_INFO("Filtering active: exclude_types=" + show(filters.exclude_types) +
                 " exclude_rts=" + show(filters.exclude_rts) +
                 " exclude_buses=" + show_buses(filters.exclude_buses) +
                 " exclude_subaddresses=" + show(filters.exclude_subaddresses) + " include_types=" +
                 show(filters.include_types) + " include_rts=" + show(filters.include_rts) +
                 " include_buses=" + show_buses(filters.include_buses) +
                 " include_subaddresses=" + show(filters.include_subaddresses));
}

void log_filtered_out(const MieMessage& message) {
    const std::string rt = message.command_word.has_value()
                               ? text::decimal(message.command_word.value().rt)
                               : std::string("-");
    const std::string subaddress = message.command_word.has_value()
                                       ? text::decimal(message.command_word.value().subaddress)
                                       : std::string("-");
    MIE_LOG_DEBUG("Filtered out: offset=0x" + text::hex_upper(message.file_offset, 1) + " type=0x" +
                  text::hex_upper(message.type_word.message_type, 2) + " RT" + rt + " SA" +
                  subaddress + " Bus " + bus_name(message.bus()));
}

}  // namespace

bool FilterConfig::should_exclude(const MieMessage& message) const {
    return excluded(message, true);
}

bool FilterConfig::excludes_as_parent(const MieMessage& parent) const {
    return excluded(parent, false);
}

bool FilterConfig::excludes_type_of(const MieMessage& message) const {
    const uint8_t message_type = message.type_word.message_type;
    return contains(exclude_types, message_type) ||
           (!include_types.empty() && !contains(include_types, message_type));
}

bool FilterConfig::excluded(const MieMessage& message, bool with_types) const {
    const uint8_t message_type = message.type_word.message_type;
    const Bus bus = message.type_word.bus;
    const bool has_command = message.command_word.has_value();
    const uint8_t rt = has_command ? message.command_word.value().rt : 0;
    const uint8_t subaddress = has_command ? message.command_word.value().subaddress : 0;

    // Exclusion first. An RT or subaddress exclusion can only match a value the
    // record actually has, so a Command-Word-less record passes them by.
    if (contains(exclude_types, message_type)) {
        return true;
    }
    if (has_command && contains(exclude_rts, rt)) {
        return true;
    }
    if (contains(exclude_buses, bus)) {
        return true;
    }
    if (has_command && contains(exclude_subaddresses, subaddress)) {
        return true;
    }

    // Inclusion. An EMPTY set is no constraint at all -- reading it as "include
    // nothing" would make a config that sets only exclude_rts drop every record
    // in the file.
    if (with_types && !include_types.empty() && !contains(include_types, message_type)) {
        return true;
    }
    if (!include_buses.empty() && !contains(include_buses, bus)) {
        return true;
    }
    // A record with no Command Word cannot satisfy an RT or subaddress include
    // filter: an operator narrowing to RT 5 is not asking to keep records that
    // have no RT at all.
    if (!include_rts.empty() && !(has_command && contains(include_rts, rt))) {
        return true;
    }
    if (!include_subaddresses.empty() &&
        !(has_command && contains(include_subaddresses, subaddress))) {
        return true;
    }

    return false;
}

FilteredSource::FilteredSource(MessageSource& inner, const FilterConfig& filters)
    : inner_(&inner), filters_(filters), passed_(0), excluded_(0), parent_kept_() {
    if (filters_.is_active()) {
        log_active_filters(filters_);
    }
}

FilteredSource::~FilteredSource() {
    if (!filters_.is_active()) {
        return;
    }
    // A destructor that throws during unwinding is std::terminate, and building
    // this message allocates. The tally is a courtesy; losing it must never
    // turn a decode failure into a crash.
    try {
        MIE_LOG_INFO("Filter results: " + text::decimal(passed_) + " passed, " +
                     text::decimal(excluded_) + " excluded");
    } catch (...) {  // NOLINT(bugprone-empty-catch)
    }
}

bool FilteredSource::next(MieMessage& out) {
    // Loops rather than recursing: a long excluded run must not consume stack
    // proportional to its length.
    for (;;) {
        if (!inner_->next(out)) {
            return false;
        }
        if (!drops(out)) {
            passed_ += 1;
            return true;
        }
        excluded_ += 1;
        log_filtered_out(out);
    }
}

bool FilteredSource::drops(const MieMessage& message) {
    // A 0x2000 continuation is the second half of its parent's transaction, so
    // it shares the parent's fate (L2-FLT-003): kept exactly when the parent
    // passes the RT, subaddress, bus and type-exclusion filters, and its own
    // type passes any type filter.
    const bool drop = (parent_kept_.has_value() && message.is_continuation())
                          ? (!parent_kept_.value() || filters_.excludes_type_of(message))
                          : filters_.should_exclude(message);
    if (message.is_error() && !message.is_spurious()) {
        parent_kept_.set(!filters_.excludes_as_parent(message));
    } else {
        parent_kept_.reset();
    }
    return drop;
}

}  // namespace mie
