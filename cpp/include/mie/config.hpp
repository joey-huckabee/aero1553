// SPDX-License-Identifier: Apache-2.0
//
// The decoder's configuration schema.
//
// This is the layer ABOVE `mie/toml.hpp`, and the division is deliberate:
//
//   toml.hpp    what the file SAYS   -- sections, keys, values, line numbers
//   config.hpp  what it MEANS        -- which keys exist, their types, their
//                                       ranges, their enum spellings, defaults
//
// Everything MIE-specific is here. The parser knows nothing about
// `detect_records` having a range of [1, 32], and it should not: that is what
// makes it liftable into another project, and what keeps this file readable as
// a statement of the schema rather than a mix of grammar and policy.
//
// Mirrors `rust/src/config.rs` and `python/src/aero1553/config.py`.
// `docs/CONFIG-REFERENCE.md` is the normative description of every key.
//
// PRECEDENCE is CLI > config file > default, applied by `with_overrides`.
//
// VALIDATION HAPPENS AT LOAD TIME (L2-CFG-010), not at use. An out-of-range
// `detect_records` must name the config file while the operator is looking at
// it, rather than surfacing later as a clamped value nobody asked for.

#ifndef MIE_CONFIG_HPP
#define MIE_CONFIG_HPP

#include <cstddef>
#include <cstdint>
#include <exception>
#include <memory>
#include <string>

#include "mie/decode.hpp"
#include "mie/filter.hpp"
#include "mie/models.hpp"
#include "mie/optional.hpp"
#include "mie/sync.hpp"
#include "mie/toml.hpp"

namespace mie {

// --- Validated ranges (L2-CFG-010) ---------------------------------------

/// L2-DEC-015: how many records the timestamp-format probe may walk.
const std::size_t DETECT_RECORDS_MIN = 1;
const std::size_t DETECT_RECORDS_MAX = 32;

/// L2-SYN-026: look-ahead depth. Same range as the probe size, deliberately --
/// the two record-count knobs are the same kind of quantity.
const std::size_t LOOKAHEAD_RECORDS_MIN = 1;
const std::size_t LOOKAHEAD_RECORDS_MAX = 32;

/// L2-WRT-022: cap on one buffered equal-timestamp run. `1` disables
/// reordering and restores raw DDC capture order.
const std::size_t MAX_SORT_GROUP_MIN = 1;
const std::size_t MAX_SORT_GROUP_MAX = 1048576;
const std::size_t DEFAULT_MAX_SORT_GROUP = 4096;

/// L2-MRG-008: bounds on `merge.max_collapse_survivors`, the cap on the
/// de-duplication survivor set. The collapse window bounds retention in TIME;
/// this bounds it in COUNT, so input whose timestamps all decode alike cannot
/// grow the set without limit. The default matches DEFAULT_MAX_SORT_GROUP
/// deliberately: the two caps guard the same class of pathological input.
const std::size_t MAX_COLLAPSE_SURVIVORS_MIN = 1;
const std::size_t MAX_COLLAPSE_SURVIVORS_MAX = 1048576;
const std::size_t DEFAULT_MAX_COLLAPSE_SURVIVORS = 4096;

/// A problem with the configuration.
///
/// A separate type from `MieError`, mirroring Rust's `ConfigError`, and the
/// reason is worth stating: `MieErrorKind` is an exhaustive table pinned by
/// test and mapped to exit codes, and a malformed config file is not a decode
/// failure. Folding it in would mean either a kind that never reaches the
/// decoder or a decode error that never comes from a record.
class ConfigError : public std::exception {
  public:
    explicit ConfigError(const std::string& message);
    ~ConfigError() throw() override;

    const std::string& message() const { return *message_; }
    const char* what() const throw() override;

  private:
    /// Behind a shared_ptr for the same reason MieError's message is: this type
    /// is thrown, copying an exception can happen mid-unwind, and a copy
    /// constructor that allocates can throw there -- which is std::terminate.
    std::shared_ptr<const std::string> message_;
};

/// Resolved configuration.
///
/// Every field has a default that is the documented behaviour with no config
/// file present, so a default-constructed instance is a valid configuration
/// rather than a placeholder.
struct DecoderConfig {
    std::string log_level;
    /// L2-LOG-001. Emit the one-time IRIG day-of-year advisory. True by
    /// default, but the advisory is logged at INFO, so at the default WARNING
    /// level it is already silent -- this exists so a site that has validated
    /// its card model against vendor CSV can also keep it out of a
    /// `--log-level INFO` run.
    bool irig_day_advisory;
    TimestampFormat input_time_format;
    bool strict;
    ErrorMode error_mode;
    FilterConfig filters;
    std::string output_format;
    /// L2-WRT-017. Defaults to false (overwrite permitted).
    bool no_clobber;
    /// L2-WRT-025: which rendering the TIME_STAMP column uses. Defaults to
    /// `doy` -- the DDC vendor rendering -- so a decode that selects nothing
    /// stays byte-comparable against vendor CSV.
    OutputTimeFormat output_time_format;
    /// L2-WRT-026: calendar year used to resolve the IRIG day-of-year field.
    /// Required by `iso` / `dom`, inert under `doy`. An MIE file carries no
    /// year, so "absent" is the only honest default, and a calendar rendering
    /// with no year is refused rather than guessed at.
    Optional<int> year;
    /// L2-WRT-025: offset from UTC in minutes for the `iso` zone designator.
    /// Zero renders as `Z`.
    int utc_offset_minutes;
    /// L1-EXIT-004: commit `<destination>.partial` and exit 0 rather than
    /// unlinking and failing, on an unrecoverable mid-file sync loss.
    bool allow_partial;
    std::size_t detect_records;
    std::size_t lookahead_records;
    /// L2-DEC-017. Absent keeps Standard records out of DELTA tracking.
    Optional<double> standard_tick_rate_hz;
    /// L2-WRT-020 MUX population from the input file name.
    bool mux_enabled;
    std::string mux_delimiter;
    int64_t mux_field;
    /// L2-MRG-007 cross-recorder duplicate collapsing, off by default.
    bool collapse_duplicates;
    uint64_t collapse_window_us;
    /// L2-MRG-005 DELTA scope for a multi-file merge.
    DeltaScope delta_scope;
    /// L2-WRT-022 canonical-order run cap.
    std::size_t max_sort_group;
    std::size_t max_collapse_survivors;

    DecoderConfig();
};

/// Parse a UTC offset designator into minutes east of UTC (L2-CFG-012).
///
/// Accepts `Z` (case-insensitively), or a signed `+HH:MM` / `-HH:MM`. Shared by
/// the config loader (`output.utc_offset`) and the CLI (`--utc-offset`) so the
/// two spellings cannot drift. Returns false for anything else.
bool parse_utc_offset(const std::string& text, int& out);

/// CLI-supplied overrides. Absent means "not supplied", which is why every
/// field is an Optional rather than carrying a sentinel: `--strict=false` and
/// "no --strict flag" are different instructions.
struct ConfigOverrides {
    Optional<std::string> log_level;
    Optional<bool> irig_day_advisory;
    Optional<TimestampFormat> input_time_format;
    Optional<bool> strict;
    Optional<ErrorMode> error_mode;
    Optional<std::string> output_format;
    Optional<bool> no_clobber;
    Optional<OutputTimeFormat> output_time_format;
    Optional<int> year;
    Optional<int> utc_offset_minutes;
    Optional<bool> allow_partial;
    Optional<std::size_t> detect_records;
    Optional<std::size_t> lookahead_records;
    Optional<double> standard_tick_rate_hz;
    Optional<bool> mux_enabled;
    Optional<std::string> mux_delimiter;
    Optional<int64_t> mux_field;
    Optional<bool> collapse_duplicates;
    Optional<uint64_t> collapse_window_us;
    Optional<DeltaScope> delta_scope;
    Optional<std::size_t> max_sort_group;
    Optional<std::size_t> max_collapse_survivors;

    /// Filter overrides MERGE into the loaded sets rather than replacing them,
    /// matching the Python semantics: `--exclude-rt 5` on top of a config file
    /// that already excludes RT 9 excludes both.
    FilterConfig filters;

    ConfigOverrides();
};

/// Apply `overrides` onto `base`, returning the result. CLI beats file.
DecoderConfig with_overrides(const DecoderConfig& base, const ConfigOverrides& overrides);

/// Parse TOML text into a configuration. Throws ConfigError.
DecoderConfig parse_into_config(const std::string& text);

/// Load from `path`, or return the defaults when absent. Throws ConfigError
/// for a missing or unreadable file as well as for a schema problem -- an
/// operator who named a config file expects it to be used.
DecoderConfig load_config(const Optional<std::string>& path);

/// True when `name` is one of the six sections the schema defines.
bool is_known_section(const std::string& name);

/// True when `(section, key)` is in the schema. Drives the L2-CFG-009
/// unknown-key WARN, which is non-fatal so a config written for a newer
/// version still loads on an older one.
bool is_known_key(const std::string& section, const std::string& key);

/// Resolve a message-type name (`BC_TO_RT`) or hex code (`0x02`) to its code.
/// Throws ConfigError on an unrecognised name.
uint8_t parse_type_name(const std::string& name);

/// Resolve `A` / `B`, case-insensitively. Throws ConfigError otherwise.
Bus parse_bus_name(const std::string& name);

}  // namespace mie

#endif  // MIE_CONFIG_HPP
