//! Configuration loader and a hand-rolled TOML parser for our small schema.
//!
//! The parser supports exactly what `config/default.toml` needs:
//!   - `[section]` headers
//!   - `key = value` pairs
//!   - Quoted strings (`"..."`), integers, booleans (`true`/`false`)
//!   - Primitive arrays (`[1, 2, 3]` or `["a", "b"]`)
//!   - `#` line comments and trailing comments on value lines
//!   - Whitespace-insensitive
//!
//! Anything outside this subset is rejected with a line number. A duplicated
//! `(section, key)`, or a `[section]` header declared more than once, is
//! rejected too, matching Python's `tomllib` (the TOML spec forbids
//! redefinition); see `docs/CONFIG-REFERENCE.md` for the accepted subset.
//!
//! Precedence: CLI args > config file > built-in defaults
//! (implemented via [`DecoderConfig::with_overrides`]).

use std::fs;
use std::path::Path;

use crate::decode::{
    DEFAULT_DETECT_RECORDS, DEFAULT_MUX_DELIMITER, DEFAULT_MUX_ENABLED, DEFAULT_MUX_FIELD,
};
use crate::filter::FilterConfig;
use crate::merge::{
    DEFAULT_MAX_COLLAPSE_SURVIVORS, MAX_COLLAPSE_SURVIVORS_MAX, MAX_COLLAPSE_SURVIVORS_MIN,
};
use crate::models::{
    Bus, DeltaScope, ErrorMode, MessageType, OutputTimeFormat, TimestampFormat, YEAR_MAX, YEAR_MIN,
};
use crate::order::{DEFAULT_MAX_SORT_GROUP, MAX_SORT_GROUP_MAX, MAX_SORT_GROUP_MIN};
use crate::sync::DEFAULT_LOOKAHEAD_RECORDS;

/// L2-DEC-015 valid range for `decode.detect_records`. Values outside
/// this range are rejected at config-load time with a clear error.
pub const DETECT_RECORDS_MIN: usize = 1;
pub const DETECT_RECORDS_MAX: usize = 32;

/// L2-SYN-026 valid range for `decode.lookahead_records`. Same shape as
/// `DETECT_RECORDS_MIN`/_MAX — the two configurable record-count knobs
/// share their valid range for consistency.
pub const LOOKAHEAD_RECORDS_MIN: usize = 1;
pub const LOOKAHEAD_RECORDS_MAX: usize = 32;

/// Internal config state, assembled from the TOML loader and CLI overrides and
/// consumed within the crate / binary. **Not** part of the crate's stable public
/// API — that surface is the `pub use` re-exports in `lib.rs` — so it is
/// `#[doc(hidden)]` and excluded from SemVer checks (its field set grows as
/// decode options are added; see the `cargo-semver-checks` note in Cargo.toml).
#[doc(hidden)]
#[derive(Debug, Clone)]
pub struct DecoderConfig {
    pub log_level: String,
    /// L2-LOG-001: emit the one-time IRIG day-of-year advisory. Enabled by
    /// default, but logged at INFO, so at the default `WARNING` level it is
    /// already silent -- this key exists so a site that has validated its card
    /// model against vendor CSV can also keep it out of a `--log-level INFO`
    /// troubleshooting run. Set via `logging.irig_day_advisory = false` in TOML
    /// or `--no-irig-day-advisory` on the CLI.
    pub irig_day_advisory: bool,
    pub input_time_format: TimestampFormat,
    pub strict: bool,
    pub error_mode: ErrorMode,
    pub filters: FilterConfig,
    pub output_format: String,
    /// L2-WRT-017: refuse to overwrite an existing destination. Defaults
    /// to `false` (overwrite is permitted) to preserve historical
    /// behavior. Set via `output.no_clobber = true` in TOML or
    /// `--no-clobber` on the CLI.
    pub no_clobber: bool,
    /// L2-WRT-025: which rendering the `TIME_STAMP` column uses. Defaults to
    /// `doy` -- the DDC vendor rendering -- so a decode that selects nothing
    /// stays byte-comparable against vendor CSV. Set via
    /// `output.output_time_format` in TOML or `--output-time-format` on the CLI.
    pub output_time_format: OutputTimeFormat,
    /// L2-WRT-026: calendar year used to resolve the IRIG day-of-year field.
    /// Required by the `iso` and `dom` renderings, inert under `doy`. An MIE
    /// file carries no year, so `None` -- "not supplied" -- is the only honest
    /// default, and a calendar rendering with `None` here is refused rather
    /// than guessed at. Set via `output.year` or `--year`.
    pub year: Option<u16>,
    /// L2-WRT-025: offset from UTC in minutes, applied to the `iso` rendering's
    /// zone designator. Zero renders as `Z`. Set via `output.utc_offset` or
    /// `--utc-offset`.
    pub utc_offset_minutes: i16,
    /// L1-EXIT-004: on unrecoverable mid-file sync loss, commit the rows
    /// decoded so far as `<destination>.partial` and exit 0, rather
    /// than unlinking and exiting 3. Set via `decode.allow_partial =
    /// true` in TOML or `--allow-partial` on the CLI.
    pub allow_partial: bool,
    /// L2-DEC-015: number of records the timestamp-format auto-detect
    /// probe walks before committing to IRIG vs Standard. Default
    /// `DEFAULT_DETECT_RECORDS` (`8`). Set via
    /// `decode.detect_records = N` in TOML or `--detect-records N` on
    /// the CLI. Validated against `[DETECT_RECORDS_MIN,
    /// DETECT_RECORDS_MAX]` at load time.
    pub detect_records: usize,
    /// L2-SYN-026: total number of records `sync::validate_record`
    /// checks per call (1 candidate + N-1 look-ahead). Default
    /// `DEFAULT_LOOKAHEAD_RECORDS` (`2`), preserving the historical
    /// two-record look-ahead from L2-SYN-005. Set via
    /// `decode.lookahead_records = N` in TOML or
    /// `--lookahead-records N` on the CLI. Validated against
    /// `[LOOKAHEAD_RECORDS_MIN, LOOKAHEAD_RECORDS_MAX]` at load time.
    pub lookahead_records: usize,
    /// L2-DEC-017: optional Standard-counter tick rate in Hz. `None`
    /// (the default) keeps the historical empty-`DELTA` behavior for
    /// Standard-timestamp records. When set to a finite, strictly-positive
    /// value, Standard ticks are converted to microseconds and the records
    /// participate in DELTA tracking like IRIG. Set via
    /// `decode.standard_tick_rate_hz = <hz>` in TOML or
    /// `--standard-tick-rate-hz <hz>` on the CLI. Validated as finite and
    /// `> 0` at load time.
    pub standard_tick_rate_hz: Option<f64>,
    /// L2-WRT-020: populate the MUX column from a field of the input file name.
    /// Enabled by default (`[mux] enabled = false` / `--no-mux` disables it for
    /// vendor-exact output). The file name is split on `mux_delimiter` and the
    /// `mux_field`-th field (0-based; negative counts from the end) becomes MUX.
    pub mux_enabled: bool,
    pub mux_delimiter: String,
    pub mux_field: i64,
    /// L2-MRG-007: collapse the same bus transaction witnessed by multiple
    /// recorders into one row, in a multi-file merge. Off by default (loss-free);
    /// `[merge] collapse_duplicates = true` / `--collapse-duplicates` enables it.
    pub collapse_duplicates: bool,
    /// Timestamp tolerance in microseconds for collapsing (0 = exact-µs match).
    /// Widen it for recorders whose IRIG clocks differ slightly.
    pub collapse_window_us: u64,
    /// L2-MRG-005: scope over which DELTA is measured in a multi-file merge.
    /// Default `PerFile` — each gap is to the previous same-key record from the
    /// record's own file, matching a single-file decode and the vendor tool.
    /// `Global` measures across the merged timeline instead. Set via
    /// `[merge] delta_scope` in TOML or `--delta-scope` on the CLI.
    pub delta_scope: DeltaScope,
    /// L2-WRT-022: cap on the number of consecutive equal-`TIME_STAMP` records
    /// the canonical-order stage (L2-WRT-021) buffers at once. Default
    /// `DEFAULT_MAX_SORT_GROUP` (`65536`). Set via `output.max_sort_group = N` in
    /// TOML or `--max-sort-group N` on the CLI. Validated against
    /// `[MAX_SORT_GROUP_MIN, MAX_SORT_GROUP_MAX]` at load time. `1` disables
    /// reordering, restoring raw DDC capture order.
    pub max_sort_group: usize,
    /// L2-MRG-008: cap on the number of survivors the cross-recorder
    /// de-duplication window (L2-MRG-007) retains at once. Default
    /// `DEFAULT_MAX_COLLAPSE_SURVIVORS` (`4096`). Set via
    /// `merge.max_collapse_survivors = N` in TOML or `--max-collapse-survivors N`
    /// on the CLI. Validated against
    /// `[MAX_COLLAPSE_SURVIVORS_MIN, MAX_COLLAPSE_SURVIVORS_MAX]` at load time.
    /// The collapse window bounds retention in time; this bounds it in count, so
    /// input whose timestamps all decode alike cannot grow the set without limit.
    pub max_collapse_survivors: usize,
}

impl Default for DecoderConfig {
    fn default() -> Self {
        Self {
            log_level: "WARNING".to_string(),
            irig_day_advisory: true,
            input_time_format: TimestampFormat::Auto,
            strict: false,
            error_mode: ErrorMode::Inline,
            filters: FilterConfig::default(),
            output_format: "csv".to_string(),
            no_clobber: false,
            output_time_format: OutputTimeFormat::Doy,
            year: None,
            utc_offset_minutes: 0,
            allow_partial: false,
            detect_records: DEFAULT_DETECT_RECORDS,
            lookahead_records: DEFAULT_LOOKAHEAD_RECORDS,
            standard_tick_rate_hz: None,
            mux_enabled: DEFAULT_MUX_ENABLED,
            mux_delimiter: DEFAULT_MUX_DELIMITER.to_string(),
            mux_field: DEFAULT_MUX_FIELD,
            collapse_duplicates: false,
            collapse_window_us: 0,
            delta_scope: DeltaScope::PerFile,
            max_sort_group: DEFAULT_MAX_SORT_GROUP,
            max_collapse_survivors: DEFAULT_MAX_COLLAPSE_SURVIVORS,
        }
    }
}

/// Override container: every field is optional and only applied if `Some`.
/// Filter overrides MERGE into the existing set rather than replacing it,
/// matching the Python `with_overrides` semantics. Internal plumbing — not part
/// of the stable public API; `#[doc(hidden)]` and excluded from SemVer checks.
#[doc(hidden)]
#[derive(Debug, Default, Clone)]
pub struct ConfigOverrides {
    pub log_level: Option<String>,
    pub irig_day_advisory: Option<bool>,
    pub input_time_format: Option<TimestampFormat>,
    pub strict: Option<bool>,
    pub error_mode: Option<ErrorMode>,
    pub output_format: Option<String>,
    pub no_clobber: Option<bool>,
    pub output_time_format: Option<OutputTimeFormat>,
    pub year: Option<u16>,
    pub utc_offset_minutes: Option<i16>,
    pub allow_partial: Option<bool>,
    pub detect_records: Option<usize>,
    pub lookahead_records: Option<usize>,
    pub standard_tick_rate_hz: Option<f64>,
    pub mux_enabled: Option<bool>,
    pub mux_delimiter: Option<String>,
    pub mux_field: Option<i64>,
    pub collapse_duplicates: Option<bool>,
    pub collapse_window_us: Option<i64>,
    pub delta_scope: Option<DeltaScope>,
    pub max_sort_group: Option<usize>,
    pub max_collapse_survivors: Option<usize>,

    pub exclude_types: Vec<u8>,
    pub exclude_rts: Vec<u8>,
    pub exclude_buses: Vec<Bus>,
    pub exclude_subaddresses: Vec<u8>,

    pub include_types: Vec<u8>,
    pub include_rts: Vec<u8>,
    pub include_buses: Vec<Bus>,
    pub include_subaddresses: Vec<u8>,
}

/// Apply each present (`Some`) override onto the config field of the same name.
///
/// Used by [`DecoderConfig::with_overrides`] for the fields whose rule is simply
/// "take the override if it was supplied". Fields needing anything more (a
/// re-wrap, a clamp) are written out longhand at the call site.
macro_rules! apply_plain_overrides {
    ($cfg:ident, $ov:ident, $($field:ident),+ $(,)?) => {
        $(
            if let Some(v) = $ov.$field {
                $cfg.$field = v;
            }
        )+
    };
}

impl DecoderConfig {
    #[must_use]
    pub fn with_overrides(mut self, ov: ConfigOverrides) -> Self {
        // The plain fields — "if the override is present, take it" — are applied
        // from a declarative name list rather than as fourteen near-identical
        // `if let` blocks. Adding a decode option means adding its name here, and
        // the two fields that need more than a straight copy stay spelled out
        // below, where they are visible instead of buried in the repetition.
        apply_plain_overrides!(
            self,
            ov,
            log_level,
            irig_day_advisory,
            input_time_format,
            strict,
            error_mode,
            output_format,
            no_clobber,
            output_time_format,
            utc_offset_minutes,
            allow_partial,
            detect_records,
            lookahead_records,
            mux_enabled,
            mux_delimiter,
            mux_field,
            collapse_duplicates,
            max_sort_group,
            max_collapse_survivors,
            delta_scope,
        );

        // Stored as `Option<f64>`, so the value is re-wrapped rather than copied.
        if let Some(v) = ov.standard_tick_rate_hz {
            self.standard_tick_rate_hz = Some(v);
        }
        // Likewise `Option<u16>`: "no year supplied" has to stay expressible,
        // so an absent override must not overwrite a configured year with
        // `None` (L2-WRT-026 clause 1).
        if let Some(v) = ov.year {
            self.year = Some(v);
        }
        if let Some(v) = ov.collapse_window_us {
            // CLI / config-load validation already rejects negatives; the
            // clamp makes that defensive rather than assumed, and `try_from`
            // then cannot fail -- which is the point of writing it this way
            // instead of `as`: the guarantee is in the types, not a comment.
            self.collapse_window_us = u64::try_from(v.max(0)).unwrap_or(0);
        }

        merge_unique(&mut self.filters.exclude_types, ov.exclude_types);
        merge_unique(&mut self.filters.exclude_rts, ov.exclude_rts);
        merge_unique(&mut self.filters.exclude_buses, ov.exclude_buses);
        merge_unique(
            &mut self.filters.exclude_subaddresses,
            ov.exclude_subaddresses,
        );

        merge_unique(&mut self.filters.include_types, ov.include_types);
        merge_unique(&mut self.filters.include_rts, ov.include_rts);
        merge_unique(&mut self.filters.include_buses, ov.include_buses);
        merge_unique(
            &mut self.filters.include_subaddresses,
            ov.include_subaddresses,
        );

        self
    }
}

fn merge_unique<T: PartialEq>(target: &mut Vec<T>, source: Vec<T>) {
    for v in source {
        if !target.contains(&v) {
            target.push(v);
        }
    }
}

// ── Public loader ─────────────────────────────────────────────────────

#[derive(Debug)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ConfigError {}

/// # Errors
///
/// Returns [`ConfigError`] when the path does not exist, names something that
/// is not a regular file (a directory, FIFO or character device — `--config
/// /dev/zero` would otherwise read forever), cannot be read, or holds TOML this
/// loader rejects. `None` is not an error: it yields the defaults.
pub fn load_config(path: Option<&Path>) -> Result<DecoderConfig, ConfigError> {
    let Some(path) = path else {
        return Ok(DecoderConfig::default());
    };
    // Validate the operator-supplied path BEFORE reading it. `exists()` alone
    // is not a sufficient guard: it is true for directories, FIFOs, and
    // character devices, so `--config <dir>` produces a confusing OS error and
    // `--config /dev/zero` reads forever. Requiring a *regular file* is the
    // actual precondition. Mirrors the Python loader's check and message
    // (`pythonsecurity:S8707` there); both are held to the same wording so a
    // bad `--config` fails identically on either implementation.
    if !path.exists() {
        return Err(ConfigError(format!(
            "Config file not found: {}",
            path.display()
        )));
    }
    if !path.is_file() {
        return Err(ConfigError(format!(
            "Config path is not a regular file: {}",
            path.display()
        )));
    }
    let text = fs::read_to_string(path)
        .map_err(|e| ConfigError(format!("Reading {}: {}", path.display(), e)))?;
    parse_into_config(&text)
}

/// # Errors
///
/// Returns [`ConfigError`] for TOML this parser rejects (see [`parse_toml`]), a
/// section used as a scalar, or any key whose value fails its schema check —
/// wrong type, or a number outside the range `L2-CFG-010` pins for it. Every
/// value is validated at load time, so a bad key fails here rather than at the
/// point of use.
pub fn parse_into_config(text: &str) -> Result<DecoderConfig, ConfigError> {
    let toml = parse_toml(text).map_err(ConfigError)?;
    reject_section_used_as_scalar(&toml)?;
    let mut cfg = DecoderConfig::default();
    // Each `[section]` is applied by its own helper (all validate at load time
    // per L2-CFG-010) so no single function carries the whole schema.
    apply_logging_section(&toml, &mut cfg)?;
    apply_decode_section(&toml, &mut cfg)?;
    apply_output_section(&toml, &mut cfg)?;
    apply_mux_section(&toml, &mut cfg)?;
    apply_merge_section(&toml, &mut cfg)?;
    apply_filter_sections(&toml, &mut cfg)?;
    warn_unknown_keys(&toml);
    Ok(cfg)
}

/// `[logging]`: validate at load time so the error points at the config file
/// rather than surfacing later as a silent no-op.
fn apply_logging_section(toml: &TomlDoc, cfg: &mut DecoderConfig) -> Result<(), ConfigError> {
    if let Some(level) = toml.get_string("logging", "level")? {
        let upper = level.to_uppercase();
        if crate::log::Level::parse(&upper).is_none() {
            return Err(ConfigError(format!(
                "Invalid logging.level: {level:?}. \
                 Valid: DEBUG, INFO, WARNING, WARN, ERROR, CRITICAL, OFF"
            )));
        }
        cfg.log_level = upper;
    }
    if let Some(b) = toml.get_bool("logging", "irig_day_advisory")? {
        cfg.irig_day_advisory = b;
    }
    Ok(())
}

/// `[decode]`: timestamp format, strict/allow-partial flags, detection and
/// look-ahead ranges, and the Standard tick-rate calibration.
fn apply_decode_section(toml: &TomlDoc, cfg: &mut DecoderConfig) -> Result<(), ConfigError> {
    // L2-CFG-012: `decode.time_format` was renamed in v3.0.0. Left to the
    // generic unknown-key rule it would only WARN (L2-CFG-009) -- and a WARN is
    // the wrong answer for a rename, because the operator's explicit choice
    // would be discarded while the run reported success, silently reverting a
    // forced format to auto-detection. Rejected by name, with a pointer.
    //
    // Presence is tested through `get` rather than `get_string` so that a
    // wrong-typed value (`time_format = 1`) reports the rename too, instead of
    // a type error about a key that no longer exists.
    if toml.get("decode", "time_format").is_some() {
        return Err(ConfigError(
            "decode.time_format was renamed in v3.0.0. Use decode.input_time_format to \
             choose how timestamps are PARSED (auto, irig, standard); use \
             output.output_time_format to choose how they are WRITTEN (doy, iso, dom)."
                .to_string(),
        ));
    }
    if let Some(tf) = toml.get_string("decode", "input_time_format")? {
        cfg.input_time_format = parse_time_format(tf)?;
    }
    if let Some(b) = toml.get_bool("decode", "strict")? {
        cfg.strict = b;
    }
    if let Some(em) = toml.get_string("decode", "error_mode")? {
        cfg.error_mode = parse_error_mode(em)?;
    }
    if let Some(b) = toml.get_bool("decode", "allow_partial")? {
        cfg.allow_partial = b;
    }
    if let Some(n) = toml.get_int("decode", "detect_records")? {
        // L2-DEC-015: validate range [1, 32] at load time per L2-CFG-010.
        cfg.detect_records = require_int_range(
            n,
            "decode.detect_records",
            DETECT_RECORDS_MIN,
            DETECT_RECORDS_MAX,
        )?;
    }
    if let Some(n) = toml.get_int("decode", "lookahead_records")? {
        // L2-SYN-026: validate range [1, 32] at load time per L2-CFG-010.
        cfg.lookahead_records = require_int_range(
            n,
            "decode.lookahead_records",
            LOOKAHEAD_RECORDS_MIN,
            LOOKAHEAD_RECORDS_MAX,
        )?;
    }
    if let Some(hz) = toml.get_float("decode", "standard_tick_rate_hz")? {
        // L2-DEC-017: the tick rate must be a real, strictly-positive frequency.
        cfg.standard_tick_rate_hz =
            Some(require_positive_finite(hz, "decode.standard_tick_rate_hz")?);
    }
    Ok(())
}

/// Validate a TOML integer within `[lo, hi]` at load time (L2-CFG-010). `key`
/// names the offending key for the error message.
fn require_int_range(n: i64, key: &str, lo: usize, hi: usize) -> Result<usize, ConfigError> {
    // `try_from` carries the range check and the conversion together, so a
    // negative or oversized value cannot reach the `usize` at all. Comparing
    // `n as i64` against the bounds and then casting back did the same job in
    // two steps, each of which the compiler had to take on trust.
    match usize::try_from(n) {
        Ok(v) if (lo..=hi).contains(&v) => Ok(v),
        _ => Err(ConfigError(format!(
            "Invalid {key}: {n}. Valid range: [{lo}, {hi}]"
        ))),
    }
}

/// Validate a Standard tick rate: finite and strictly positive (L2-DEC-017).
fn require_positive_finite(hz: f64, key: &str) -> Result<f64, ConfigError> {
    if !hz.is_finite() || hz <= 0.0 {
        return Err(ConfigError(format!(
            "Invalid {key}: {hz}. Must be a finite value greater than 0"
        )));
    }
    Ok(hz)
}

/// `[output]`: output format (only `csv` today, L2-CFG-010), no-clobber, and the
/// canonical-order run cap (L2-WRT-022).
fn apply_output_section(toml: &TomlDoc, cfg: &mut DecoderConfig) -> Result<(), ConfigError> {
    if let Some(fmt) = toml.get_string("output", "format")? {
        if fmt != "csv" {
            return Err(ConfigError(format!(
                "Invalid output.format: {fmt:?}. Valid: csv"
            )));
        }
        cfg.output_format = fmt.to_string();
    }
    if let Some(b) = toml.get_bool("output", "no_clobber")? {
        cfg.no_clobber = b;
    }
    // L2-CFG-012 / L2-WRT-025: which rendering the TIME_STAMP column uses.
    if let Some(otf) = toml.get_string("output", "output_time_format")? {
        cfg.output_time_format = parse_output_time_format(otf)?;
    }
    // L2-WRT-026 clause 1: the year is validated here but NOT required here --
    // a config may legitimately set a year it never uses, and a calendar
    // rendering may legitimately take its year from the CLI instead. Whether
    // one is actually needed is a question about the resolved pair, so it is
    // asked once both sources have been merged (see `cli::resolve_time_render`).
    if let Some(n) = toml.get_int("output", "year")? {
        let year = crate::models::check_year(n).map_err(|_| {
            ConfigError(format!(
                "Invalid output.year: {n}. Valid range: [{YEAR_MIN}, {YEAR_MAX}]"
            ))
        })?;
        cfg.year = Some(year);
    }
    if let Some(offset) = toml.get_string("output", "utc_offset")? {
        cfg.utc_offset_minutes = parse_utc_offset(offset)?;
    }
    // L2-WRT-022: cap on one buffered equal-timestamp run. Range-checked here so
    // a bad value fails at load time rather than silently clamping later; the
    // message text matches Python's `_load_max_sort_group` (L3-WRT-003).
    if let Some(n) = toml.get_int("output", "max_sort_group")? {
        match usize::try_from(n) {
            Ok(v) if (MAX_SORT_GROUP_MIN..=MAX_SORT_GROUP_MAX).contains(&v) => {
                cfg.max_sort_group = v;
            }
            _ => {
                return Err(ConfigError(format!(
                    "Invalid output.max_sort_group: {n}. Valid range: [{MAX_SORT_GROUP_MIN}, {MAX_SORT_GROUP_MAX}]"
                )));
            }
        }
    }
    Ok(())
}

/// `[mux]`: MUX-from-filename configuration (L2-WRT-020).
fn apply_mux_section(toml: &TomlDoc, cfg: &mut DecoderConfig) -> Result<(), ConfigError> {
    if let Some(b) = toml.get_bool("mux", "enabled")? {
        cfg.mux_enabled = b;
    }
    if let Some(d) = toml.get_string("mux", "delimiter")? {
        if d.is_empty() {
            return Err(ConfigError(
                "Invalid mux.delimiter: must be a non-empty string".to_string(),
            ));
        }
        cfg.mux_delimiter = d.to_string();
    }
    if let Some(n) = toml.get_int("mux", "field")? {
        cfg.mux_field = n;
    }
    Ok(())
}

/// `[merge]`: cross-recorder duplicate collapsing (L2-MRG-007). Mirrors the
/// Python `_load_merge_section` so a `[merge]` block in a shared config file
/// behaves identically on both implementations (the CLI flags
/// `--collapse-duplicates` / `--collapse-window-us` still override it).
fn apply_merge_section(toml: &TomlDoc, cfg: &mut DecoderConfig) -> Result<(), ConfigError> {
    if let Some(b) = toml.get_bool("merge", "collapse_duplicates")? {
        cfg.collapse_duplicates = b;
    }
    if let Some(name) = toml.get_string("merge", "delta_scope")? {
        cfg.delta_scope = DeltaScope::from_name_ci(name).ok_or_else(|| {
            ConfigError(format!(
                "Invalid merge.delta_scope: {name:?}. Valid: per-file, global"
            ))
        })?;
    }
    if let Some(n) = toml.get_int("merge", "collapse_window_us")? {
        if n < 0 {
            return Err(ConfigError(format!(
                "Invalid merge.collapse_window_us: {n}; must be a non-negative integer"
            )));
        }
        cfg.collapse_window_us = u64::try_from(n).unwrap_or(0);
    }
    // L2-MRG-008: cap on the retained survivor set. Range-checked here so a bad
    // value fails at load time rather than silently clamping later; the message
    // text matches Python's loader and the C++ one (L3-WRT-003).
    if let Some(n) = toml.get_int("merge", "max_collapse_survivors")? {
        match usize::try_from(n) {
            Ok(v) if (MAX_COLLAPSE_SURVIVORS_MIN..=MAX_COLLAPSE_SURVIVORS_MAX).contains(&v) => {
                cfg.max_collapse_survivors = v;
            }
            _ => {
                return Err(ConfigError(format!(
                    "Invalid merge.max_collapse_survivors: {n}. Valid range: [{MAX_COLLAPSE_SURVIVORS_MIN}, {MAX_COLLAPSE_SURVIVORS_MAX}]"
                )));
            }
        }
    }
    Ok(())
}

/// `[filter]`: the four exclude-array keys, each element validated on push.
fn apply_filter_sections(toml: &TomlDoc, cfg: &mut DecoderConfig) -> Result<(), ConfigError> {
    if let Some(types) = toml.get_array("filter", "exclude_types")? {
        for v in types {
            cfg.filters.exclude_types.push(parse_type_value(v)?);
        }
    }
    if let Some(rts) = toml.get_array("filter", "exclude_rts")? {
        for v in rts {
            cfg.filters
                .exclude_rts
                .push(parse_int_rt_sa(v, "exclude_rts")?);
        }
    }
    if let Some(buses) = toml.get_array("filter", "exclude_buses")? {
        for v in buses {
            cfg.filters.exclude_buses.push(parse_bus_value(v)?);
        }
    }
    if let Some(sas) = toml.get_array("filter", "exclude_subaddresses")? {
        for v in sas {
            cfg.filters
                .exclude_subaddresses
                .push(parse_int_rt_sa(v, "exclude_subaddresses")?);
        }
    }
    Ok(())
}

/// L2-CFG-009: WARN on unknown `[section] key` entries so typos in a config
/// file (e.g., `exclude_subdresses`) surface to the operator instead of being
/// silently dropped. Non-fatal so forward-compatible additions don't break
/// older configs.
fn warn_unknown_keys(toml: &TomlDoc) {
    for (section, key, _) in &toml.entries {
        if !is_known_shared_key(section.as_str(), key.as_str()) {
            crate::log_warn!("unknown TOML key: [{section}] {key}");
        }
    }
}

/// A known section name written as a root-level scalar (e.g. `decode = true`
/// instead of a `[decode]` header) is a config error, matching the Python
/// loader. Otherwise the intended section is silently dropped — the same
/// silent-ignore class of bug as a section the loader never reads. A
/// truly-unknown root-level key (not a section name) stays a non-fatal
/// unknown-key WARN (L2-CFG-009).
fn reject_section_used_as_scalar(toml: &TomlDoc) -> Result<(), ConfigError> {
    for (section, key, _) in &toml.entries {
        if section.is_empty() && is_known_section(key) {
            return Err(ConfigError(format!(
                "Invalid [{key}]: expected a table, not a value"
            )));
        }
    }
    Ok(())
}

/// The six `[section]` names the schema defines.
fn is_known_section(name: &str) -> bool {
    matches!(
        name,
        "logging" | "decode" | "output" | "mux" | "merge" | "filter"
    )
}

/// Shared schema membership check used by L2-CFG-009. Any
/// `(section, key)` pair not in this list triggers an unknown-key WARN
/// at load time.
#[allow(
    clippy::unnested_or_patterns,
    reason = "one `(section, key)` pair per line is a table mirroring CONFIG-REFERENCE.md; nesting groups by section, which makes adding a key edit an existing line instead of adding one"
)]
fn is_known_shared_key(section: &str, key: &str) -> bool {
    matches!(
        (section, key),
        ("logging", "level")
            | ("logging", "irig_day_advisory")
            | ("decode", "input_time_format")
            | ("decode", "strict")
            | ("decode", "error_mode")
            | ("decode", "allow_partial")
            | ("decode", "detect_records")
            | ("decode", "lookahead_records")
            | ("decode", "standard_tick_rate_hz")
            | ("output", "format")
            | ("output", "no_clobber")
            | ("output", "output_time_format")
            | ("output", "year")
            | ("output", "utc_offset")
            | ("output", "max_sort_group")
            | ("mux", "enabled")
            | ("mux", "delimiter")
            | ("mux", "field")
            | ("merge", "collapse_duplicates")
            | ("merge", "collapse_window_us")
            | ("merge", "max_collapse_survivors")
            | ("merge", "delta_scope")
            | ("filter", "exclude_types")
            | ("filter", "exclude_rts")
            | ("filter", "exclude_buses")
            | ("filter", "exclude_subaddresses")
    )
}

// ── Helpers for value coercion ────────────────────────────────────────

fn parse_time_format(s: &str) -> Result<TimestampFormat, ConfigError> {
    TimestampFormat::from_name_ci(s).ok_or_else(|| {
        ConfigError(format!(
            "Invalid input_time_format: {s:?}. Valid: auto, irig, standard"
        ))
    })
}

fn parse_output_time_format(s: &str) -> Result<OutputTimeFormat, ConfigError> {
    OutputTimeFormat::from_name_ci(s).ok_or_else(|| {
        ConfigError(format!(
            "Invalid output_time_format: {s:?}. Valid: doy, iso, dom"
        ))
    })
}

/// Parse a UTC offset designator into minutes east of UTC (L2-CFG-012).
///
/// Accepts `Z` (case-insensitively) for zero, or a signed `+HH:MM` / `-HH:MM`.
/// The shape is checked exactly rather than leniently: `+5:00`, `+0500` and a
/// trailing-space variant are all rejected, because a zone that parsed loosely
/// here would silently shift every ISO timestamp in the file.
///
/// # Errors
///
/// Returns [`ConfigError`] when `s` does not match the grammar, or its hours
/// or minutes are out of range.
pub fn parse_utc_offset(s: &str) -> Result<i16, ConfigError> {
    let invalid = || {
        ConfigError(format!(
            "Invalid output.utc_offset: {s:?}. Valid: Z, or +HH:MM / -HH:MM \
             with HH in [0, 23] and MM in [0, 59]"
        ))
    };

    if s.eq_ignore_ascii_case("z") {
        return Ok(0);
    }

    let bytes = s.as_bytes();
    if bytes.len() != 6 || bytes[3] != b':' {
        return Err(invalid());
    }
    let sign: i16 = match bytes[0] {
        b'+' => 1,
        b'-' => -1,
        _ => return Err(invalid()),
    };

    let two_digits = |range: std::ops::Range<usize>| -> Option<i16> {
        let text = s.get(range)?;
        if !text.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        text.parse::<i16>().ok()
    };
    let (Some(hours), Some(minutes)) = (two_digits(1..3), two_digits(4..6)) else {
        return Err(invalid());
    };
    if hours > 23 || minutes > 59 {
        return Err(invalid());
    }

    Ok(sign * (hours * 60 + minutes))
}

fn parse_error_mode(s: &str) -> Result<ErrorMode, ConfigError> {
    match s.to_ascii_lowercase().as_str() {
        "separate" => Ok(ErrorMode::Separate),
        "inline" => Ok(ErrorMode::Inline),
        other => Err(ConfigError(format!(
            "Invalid error_mode: {other:?}. Valid: separate, inline"
        ))),
    }
}

/// # Errors
///
/// Returns [`ConfigError`] if the value is neither a string nor an integer, if
/// an integer is outside `u8`, or if a string names no known message type.
pub fn parse_type_value(v: &TomlValue) -> Result<u8, ConfigError> {
    match v {
        TomlValue::String(s) => parse_type_name(s),
        TomlValue::Int(i) => {
            u8::try_from(*i).map_err(|_| ConfigError(format!("Type code out of range: {i}")))
        }
        _ => Err(ConfigError(
            "exclude_types entries must be strings or integers".into(),
        )),
    }
}

/// Parse a message-type identifier: name (e.g. "`BC_TO_RT`") or hex (e.g. "0x02").
///
/// # Errors
///
/// Returns [`ConfigError`] if the name matches no known message type and is not
/// a valid `0x`-prefixed hex byte.
pub fn parse_type_name(s: &str) -> Result<u8, ConfigError> {
    let upper = s.trim().to_uppercase();
    let by_name: &[(&str, u8)] = &[
        ("MODE_COMMAND", MessageType::ModeCommand as u8),
        ("BC_TO_RT", MessageType::BcToRt as u8),
        ("RT_TO_BC", MessageType::RtToBc as u8),
        ("RT_TO_RT", MessageType::RtToRt as u8),
        ("BROADCAST_BC_TO_RT", MessageType::BroadcastBcToRt as u8),
        ("BROADCAST_RT_TO_RT", MessageType::BroadcastRtToRt as u8),
        ("SPURIOUS_DATA", MessageType::SpuriousData as u8),
    ];
    for (name, code) in by_name {
        if upper == *name {
            return Ok(*code);
        }
    }
    if let Some(rest) = upper.strip_prefix("0X") {
        return u8::from_str_radix(rest, 16)
            .map_err(|_| ConfigError(format!("Invalid hex type code: {s:?}")));
    }
    Err(ConfigError(format!(
        "Unknown message type: {s:?}. \
         Valid: MODE_COMMAND, BC_TO_RT, RT_TO_BC, RT_TO_RT, \
         BROADCAST_BC_TO_RT, BROADCAST_RT_TO_RT, SPURIOUS_DATA"
    )))
}

/// # Errors
///
/// Returns [`ConfigError`] if the value is not a string, or names a bus other
/// than `A` or `B`.
pub fn parse_bus_value(v: &TomlValue) -> Result<Bus, ConfigError> {
    if let TomlValue::String(s) = v {
        parse_bus_name(s)
    } else {
        Err(ConfigError("exclude_buses entries must be strings".into()))
    }
}

/// # Errors
///
/// Returns [`ConfigError`] for anything other than `A` or `B`, case-insensitive.
pub fn parse_bus_name(s: &str) -> Result<Bus, ConfigError> {
    match s.trim().to_ascii_uppercase().as_str() {
        "A" => Ok(Bus::A),
        "B" => Ok(Bus::B),
        other => Err(ConfigError(format!("Invalid bus: {other:?}. Valid: A, B"))),
    }
}

/// Parse a MIL-STD-1553 RT address or subaddress: integer in [0, 31].
/// Per the L2-CFG schema reference, values outside this range are
/// rejected at load time because they could never match a real record.
fn parse_int_rt_sa(v: &TomlValue, field: &str) -> Result<u8, ConfigError> {
    match v {
        TomlValue::Int(i) => {
            // One expression carries both bounds: `try_from` rejects negatives
            // and anything above 255, the range check rejects the rest. The
            // previous form checked the range and then cast, so nothing in the
            // types stopped the cast from being moved away from its guard.
            match u8::try_from(*i) {
                Ok(v) if v <= 31 => Ok(v),
                _ => Err(ConfigError(format!(
                    "{field} value out of MIL-STD-1553 range [0, 31]: {i}"
                ))),
            }
        }
        _ => Err(ConfigError(format!("{field} entries must be integers"))),
    }
}

// ── TOML parser ───────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub enum TomlValue {
    String(String),
    Int(i64),
    Float(f64),
    Bool(bool),
    Array(Vec<TomlValue>),
}

#[derive(Debug, Default)]
pub struct TomlDoc {
    /// Indexed by `(section, key)` → value. Order-insensitive.
    entries: Vec<(String, String, TomlValue)>,
}

impl TomlDoc {
    #[must_use]
    pub fn get(&self, section: &str, key: &str) -> Option<&TomlValue> {
        self.entries
            .iter()
            .find(|(s, k, _)| s == section && k == key)
            .map(|(_, _, v)| v)
    }
    /// # Errors
    ///
    /// Returns [`ConfigError`] if the key is present but is not a string. A
    /// missing key is `Ok(None)`, not an error.
    pub fn get_string(&self, section: &str, key: &str) -> Result<Option<&str>, ConfigError> {
        match self.get(section, key) {
            None => Ok(None),
            Some(TomlValue::String(s)) => Ok(Some(s)),
            Some(_) => Err(ConfigError(format!("[{section}] {key} must be a string"))),
        }
    }
    /// # Errors
    ///
    /// Returns [`ConfigError`] if the key is present but is not a boolean. A
    /// missing key is `Ok(None)`, not an error.
    pub fn get_bool(&self, section: &str, key: &str) -> Result<Option<bool>, ConfigError> {
        match self.get(section, key) {
            None => Ok(None),
            Some(TomlValue::Bool(b)) => Ok(Some(*b)),
            Some(_) => Err(ConfigError(format!("[{section}] {key} must be a boolean"))),
        }
    }
    /// # Errors
    ///
    /// Returns [`ConfigError`] if the key is present but is not an array. A
    /// missing key is `Ok(None)`, not an error.
    pub fn get_array(&self, section: &str, key: &str) -> Result<Option<&[TomlValue]>, ConfigError> {
        match self.get(section, key) {
            None => Ok(None),
            Some(TomlValue::Array(a)) => Ok(Some(a)),
            Some(_) => Err(ConfigError(format!("[{section}] {key} must be an array"))),
        }
    }
    /// # Errors
    ///
    /// Returns [`ConfigError`] if the key is present but is not an integer. A
    /// missing key is `Ok(None)`, not an error.
    pub fn get_int(&self, section: &str, key: &str) -> Result<Option<i64>, ConfigError> {
        match self.get(section, key) {
            None => Ok(None),
            Some(TomlValue::Int(i)) => Ok(Some(*i)),
            Some(_) => Err(ConfigError(format!("[{section}] {key} must be an integer"))),
        }
    }
    /// Read a float value. Accepts a TOML integer as well so operators can
    /// write either `1000000` or `1000000.0` for a rate-style key.
    /// # Errors
    ///
    /// Returns [`ConfigError`] if the key is present but is neither a float
    /// nor an integer. A missing key is `Ok(None)`, not an error.
    pub fn get_float(&self, section: &str, key: &str) -> Result<Option<f64>, ConfigError> {
        match self.get(section, key) {
            None => Ok(None),
            Some(TomlValue::Float(f)) => Ok(Some(*f)),
            #[allow(clippy::cast_precision_loss)]
            Some(TomlValue::Int(i)) => Ok(Some(*i as f64)),
            Some(_) => Err(ConfigError(format!("[{section}] {key} must be a number"))),
        }
    }
}

/// Parse the supported TOML subset. Returns a flat key/section map.
/// True when `name` is a plain identifier (letters, digits, underscore),
/// matching Python's `_IDENT_RE`. Shared by the section-header and key checks
/// so the two implementations cannot drift on what an identifier is.
fn is_plain_identifier(name: &str) -> bool {
    name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Parse and validate a `[section]` header, returning the section name.
///
/// `stripped` is the line with its leading `[` already removed. Every rejection
/// here exists to match Python's `tomllib` + loader behavior (L2-CFG-010): the
/// forms below all *parse* on Python and are then refused by the loader, so
/// accepting them on Rust would be a silent cross-implementation divergence.
fn parse_section_header(
    stripped: &str,
    lineno: usize,
    seen_sections: &mut Vec<String>,
) -> Result<String, String> {
    // `[[section]]` array-of-tables — rejected rather than misread as a section
    // literally named `[decode]`.
    if stripped.starts_with('[') {
        return Err(format!(
            "line {lineno}: array-of-tables headers ([[...]]) are not supported"
        ));
    }
    let inner = stripped
        .strip_suffix(']')
        .ok_or_else(|| format!("line {lineno}: unterminated section header"))?;
    let section = inner.trim().to_string();
    if section.is_empty() {
        return Err(format!("line {lineno}: empty section name"));
    }
    // A dotted header (`[output.no_clobber]`) nests a table, which the flat
    // schema does not model — storing it verbatim would silently ignore its keys.
    if section.contains('.') {
        return Err(format!(
            "line {lineno}: dotted section headers ([a.b]) are not supported; \
             use a flat [section] header"
        ));
    }
    // `[bad-section]` / `["bad"]` / `[bad section]` would otherwise be stored as
    // oddly-named sections on Rust while Python rejects them.
    if !is_plain_identifier(&section) {
        return Err(format!(
            "line {lineno}: unsupported section header [{section}]; use a flat \
             [section] name (letters, digits, underscore)"
        ));
    }
    // The TOML spec forbids defining a table twice. Without this the parser
    // silently merged the second block into the first.
    if seen_sections.iter().any(|s| s == &section) {
        return Err(format!(
            "line {lineno}: section [{section}] declared more than once"
        ));
    }
    seen_sections.push(section.clone());
    Ok(section)
}

/// Parse and validate a `key = value` line.
///
/// As with headers, each rejection mirrors a form Python's `tomllib` accepts
/// syntactically but the loader refuses, so that both implementations agree
/// (L2-CFG-010).
fn parse_key_value(line: &str, lineno: usize) -> Result<(String, TomlValue), String> {
    let eq = line
        .find('=')
        .ok_or_else(|| format!("line {lineno}: expected '=' in {line:?}"))?;
    let key = line[..eq].trim().to_string();
    let value_text = line[eq + 1..].trim();
    if key.is_empty() {
        return Err(format!("line {lineno}: empty key"));
    }
    // Dotted keys (`decode.strict = true`) would be nested by tomllib (honoring
    // the value); dropping them here would silently ignore a safety option such
    // as `output.no_clobber`.
    if !key.starts_with('"') && key.contains('.') {
        return Err(format!(
            "line {lineno}: dotted keys (a.b = ...) are not supported; use a [section] header"
        ));
    }
    // A quoted key (`"strict" = true`) is honored by tomllib (quotes stripped)
    // but would be stored literally on Rust.
    if !is_plain_identifier(&key) {
        return Err(format!(
            "line {lineno}: unsupported key {key:?}; keys must be simple identifiers"
        ));
    }
    let value = parse_value(value_text, lineno)?;
    Ok((key, value))
}

/// Message for a repeated `(section, key)`, which Python's `tomllib` raises on
/// per the TOML spec. Without the check the parser silently kept the FIRST
/// value (`TomlDoc::get` finds the head), so a repeated key decoded differently
/// on each implementation.
fn duplicate_key_error(section: &str, key: &str, lineno: usize) -> String {
    let where_ = if section.is_empty() {
        String::new()
    } else {
        format!(" in section '[{section}]'")
    };
    format!("line {lineno}: duplicate key '{key}'{where_}")
}

/// # Errors
///
/// Returns the diagnostic, with its line number, for anything outside this
/// loader's deliberately flat subset of TOML: a malformed section header or
/// key/value line, a duplicate key, a re-opened section, a dotted key or
/// section header, an array-of-tables header, or an unterminated string or
/// array. The subset is held identical to the Python and C++ loaders by
/// `tests/conformance/config_parity.py`.
pub fn parse_toml(text: &str) -> Result<TomlDoc, String> {
    let mut doc = TomlDoc::default();
    let mut section = String::new();
    let mut seen_sections: Vec<String> = Vec::new();

    for (index, raw) in text.lines().enumerate() {
        let line = strip_comment(raw).trim();
        if line.is_empty() {
            continue;
        }
        let lineno = index + 1;

        if let Some(stripped) = line.strip_prefix('[') {
            section = parse_section_header(stripped, lineno, &mut seen_sections)?;
            continue;
        }

        let (key, value) = parse_key_value(line, lineno)?;
        if doc
            .entries
            .iter()
            .any(|(s, k, _)| s == &section && k == &key)
        {
            return Err(duplicate_key_error(&section, &key, lineno));
        }
        doc.entries.push((section.clone(), key, value));
    }

    Ok(doc)
}

/// Strip a trailing `# comment`, but preserve `#` inside double-quoted strings.
fn strip_comment(line: &str) -> &str {
    let bytes = line.as_bytes();
    let mut in_quote = false;
    let mut prev_backslash = false;
    for (i, &b) in bytes.iter().enumerate() {
        if in_quote {
            if b == b'\\' && !prev_backslash {
                prev_backslash = true;
                continue;
            }
            if b == b'"' && !prev_backslash {
                in_quote = false;
            }
            prev_backslash = false;
        } else if b == b'"' {
            in_quote = true;
        } else if b == b'#' {
            return &line[..i];
        }
    }
    line
}

fn parse_value(s: &str, lineno: usize) -> Result<TomlValue, String> {
    let s = s.trim();
    if s.is_empty() {
        return Err(format!("line {lineno}: empty value"));
    }
    match s.as_bytes()[0] {
        b'"' => parse_string(s, lineno).map(TomlValue::String),
        b'[' => parse_array(s, lineno),
        b't' | b'f' => parse_bool(s, lineno).map(TomlValue::Bool),
        // A numeric literal containing a decimal point or exponent is a
        // float; otherwise an integer. Validate against the TOML number grammar
        // FIRST — `i64`/`f64::from_str` are more permissive than TOML (they
        // accept leading zeros like `08`, a bare trailing dot like `1.`, etc.),
        // which would silently diverge from Python's strict `tomllib`.
        b'-' | b'+' | b'0'..=b'9' => {
            if !is_toml_number_literal(s) {
                return Err(format!("line {lineno}: invalid number literal {s:?}"));
            }
            if s.contains('.') || s.contains('e') || s.contains('E') {
                parse_float(s, lineno).map(TomlValue::Float)
            } else {
                parse_int(s, lineno).map(TomlValue::Int)
            }
        }
        _ => Err(format!("line {lineno}: cannot parse value {s:?}")),
    }
}

fn parse_string(s: &str, lineno: usize) -> Result<String, String> {
    if !s.starts_with('"') || !s.ends_with('"') || s.len() < 2 {
        return Err(format!("line {lineno}: malformed string {s:?}"));
    }
    let inner = &s[1..s.len() - 1];
    // Minimal escape handling: \", \\, \n, \t
    let mut out = String::with_capacity(inner.len());
    let mut chars = inner.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some(o) => return Err(format!("line {lineno}: bad escape \\{o}")),
                None => return Err(format!("line {lineno}: trailing backslash")),
            }
        } else if c == '"' {
            return Err(format!("line {lineno}: unescaped quote in string"));
        } else {
            out.push(c);
        }
    }
    Ok(out)
}

fn parse_bool(s: &str, lineno: usize) -> Result<bool, String> {
    match s {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(format!("line {lineno}: expected boolean, got {s:?}")),
    }
}

/// Validate a numeric literal against the flat schema's TOML number grammar:
/// `[+-]? ( 0 | [1-9][0-9]* ) ( . [0-9]+ )? ( [eE] [+-]? [0-9]+ )?`. Rejects the
/// non-TOML forms Rust's native `i64`/`f64` parsing would otherwise accept —
/// leading zeros (`08`, `01`), a bare trailing dot (`1.`), and `0x`/`0o`/`0b` /
/// underscore forms — keeping the two implementations aligned. Hand-rolled (no
/// regex dependency).
/// Consume a run of ASCII digits starting at `i`, returning the index after it
/// (which is `i` itself when there are none).
fn scan_digits(b: &[u8], mut i: usize) -> usize {
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    i
}

/// Consume an optional leading `+` or `-`.
fn scan_sign(b: &[u8], i: usize) -> usize {
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i + 1
    } else {
        i
    }
}

/// Integer part: a lone `0`, or a non-zero digit followed by more digits.
/// `None` when absent or malformed.
///
/// A leading zero consumes exactly one byte, so `01` leaves the `1` unconsumed
/// and the whole literal is ultimately rejected — TOML forbids leading zeros.
fn scan_integer_part(b: &[u8], i: usize) -> Option<usize> {
    match b.get(i) {
        None => None,
        Some(b'0') => Some(i + 1),
        Some(&c) if c.is_ascii_digit() => Some(scan_digits(b, i)),
        Some(_) => None,
    }
}

/// Optional fractional part. A `.` MUST be followed by at least one digit, so a
/// bare trailing dot yields `None`.
fn scan_fraction(b: &[u8], i: usize) -> Option<usize> {
    if i >= b.len() || b[i] != b'.' {
        return Some(i);
    }
    let start = i + 1;
    let end = scan_digits(b, start);
    (end > start).then_some(end)
}

/// Optional exponent `[eE] [+-]? [0-9]+`. An exponent marker with no digits
/// after it yields `None`.
fn scan_exponent(b: &[u8], i: usize) -> Option<usize> {
    if i >= b.len() || (b[i] != b'e' && b[i] != b'E') {
        return Some(i);
    }
    let start = scan_sign(b, i + 1);
    let end = scan_digits(b, start);
    (end > start).then_some(end)
}

/// True when `s` is a well-formed TOML number literal.
///
/// Written as the grammar's own productions — sign, integer part, optional
/// fraction, optional exponent — so each rule is named, independently testable,
/// and readable on its own. The literal is valid only if the productions
/// together consume the entire string.
fn is_toml_number_literal(s: &str) -> bool {
    let b = s.as_bytes();
    let i = scan_sign(b, 0);
    let Some(i) = scan_integer_part(b, i) else {
        return false;
    };
    let Some(i) = scan_fraction(b, i) else {
        return false;
    };
    let Some(i) = scan_exponent(b, i) else {
        return false;
    };
    i == b.len()
}

fn parse_int(s: &str, lineno: usize) -> Result<i64, String> {
    s.parse::<i64>()
        .map_err(|_| format!("line {lineno}: invalid integer {s:?}"))
}

fn parse_float(s: &str, lineno: usize) -> Result<f64, String> {
    s.parse::<f64>()
        .map_err(|_| format!("line {lineno}: invalid float {s:?}"))
}

fn parse_array(s: &str, lineno: usize) -> Result<TomlValue, String> {
    if !s.starts_with('[') || !s.ends_with(']') {
        return Err(format!("line {lineno}: malformed array {s:?}"));
    }
    let inner = s[1..s.len() - 1].trim();
    if inner.is_empty() {
        return Ok(TomlValue::Array(Vec::new()));
    }
    let mut items = Vec::new();
    for piece in split_array_items(inner) {
        let v = parse_value(piece.trim(), lineno)?;
        if matches!(v, TomlValue::Array(_)) {
            return Err(format!("line {lineno}: nested arrays not supported"));
        }
        items.push(v);
    }
    Ok(TomlValue::Array(items))
}

/// Split on commas, respecting double-quoted strings.
fn split_array_items(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_quote = false;
    let mut prev_backslash = false;
    for c in s.chars() {
        if in_quote {
            (in_quote, prev_backslash) = push_quoted_char(c, &mut cur, prev_backslash);
        } else if c == ',' {
            out.push(cur.trim().to_string());
            cur.clear();
        } else {
            if c == '"' {
                in_quote = true;
            }
            cur.push(c);
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur.trim().to_string());
    }
    out
}

/// Consume one char while inside a quoted string, pushing it to `cur`. Returns
/// the updated `(in_quote, prev_backslash)` state: a backslash escapes the next
/// character (suppressing a closing quote), and an unescaped `"` closes the
/// quote.
fn push_quoted_char(c: char, cur: &mut String, prev_backslash: bool) -> (bool, bool) {
    cur.push(c);
    if c == '\\' && !prev_backslash {
        return (true, true); // still quoted; next char is escaped
    }
    let closing = c == '"' && !prev_backslash;
    (!closing, false)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Absolute path to the repo-root `config/default.toml`.
    ///
    /// Anchored to `CARGO_MANIFEST_DIR` (the directory holding `Cargo.toml`,
    /// i.e. `rust/`) rather than to the process working directory. Cargo runs
    /// test binaries from the package root, so a bare relative
    /// `"config/default.toml"` silently meant `rust/config/default.toml` --
    /// a path that has never existed.
    fn shared_default_toml() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../config/default.toml")
    }

    /// L2-SYN-026 / L2-DEC-015: the shipped *default values* of the two
    /// record-count knobs, pinned as values rather than as "whatever the
    /// constant says".
    ///
    /// Nothing asserted these before v2.15.0. Every look-ahead test passed an
    /// explicit depth, so the default could disagree with the specification
    /// and no test would notice -- which is exactly what happened:
    /// L2-SYN-026 read "default `8`" while all three implementations shipped
    /// `2`. Referencing the constant here would reproduce the blind spot, so
    /// the literals are deliberate. If you are changing one, change
    /// `docs/L2-REQ.md`, `docs/CONFIG-REFERENCE.md`, `docs/CLI-REFERENCE.md`
    /// and `config/default.toml` in the same commit.
    /// Requirements: L2-SYN-026
    #[test]
    fn shipped_defaults_for_the_record_count_knobs() {
        let cfg = DecoderConfig::default();
        assert_eq!(cfg.lookahead_records, 2, "L2-SYN-026 default");
        assert_eq!(cfg.detect_records, 8, "L2-DEC-015 default");
        assert_eq!(crate::sync::DEFAULT_LOOKAHEAD_RECORDS, 2);
    }

    /// Requirements: L2-CFG-001
    #[test]
    fn parse_minimal_doc() {
        let text = r#"
[logging]
level = "INFO"

[decode]
input_time_format = "irig"
strict = true
error_mode = "inline"

[filter]
exclude_types = ["SPURIOUS_DATA", "MODE_COMMAND"]
exclude_rts = [0, 31]
exclude_buses = ["B"]
exclude_subaddresses = []

[output]
format = "csv"
"#;
        let cfg = parse_into_config(text).unwrap();
        assert_eq!(cfg.log_level, "INFO");
        assert_eq!(cfg.input_time_format, TimestampFormat::Irig);
        assert!(cfg.strict);
        assert_eq!(cfg.error_mode, ErrorMode::Inline);
        assert!(cfg.filters.exclude_types.contains(&0x20));
        assert!(cfg.filters.exclude_types.contains(&0x01));
        assert!(cfg.filters.exclude_rts.contains(&31));
        assert!(cfg.filters.exclude_buses.contains(&Bus::B));
        assert_eq!(cfg.filters.exclude_subaddresses, Vec::<u8>::new());
    }

    /// Requirements: L2-CFG-001
    #[test]
    fn comments_are_stripped() {
        let text = r#"
# leading comment
[decode]
strict = true  # trailing comment
input_time_format = "auto"
"#;
        let cfg = parse_into_config(text).unwrap();
        assert!(cfg.strict);
        assert_eq!(cfg.input_time_format, TimestampFormat::Auto);
    }

    /// Requirements: L2-CFG-001
    #[test]
    fn input_time_format_is_case_insensitive() {
        for (spelling, expected) in [
            ("IRIG", TimestampFormat::Irig),
            ("Irig", TimestampFormat::Irig),
            ("AUTO", TimestampFormat::Auto),
            ("Standard", TimestampFormat::Standard),
        ] {
            let text = format!("[decode]\ninput_time_format = \"{spelling}\"\n");
            let cfg = parse_into_config(&text).unwrap();
            assert_eq!(cfg.input_time_format, expected, "spelling {spelling:?}");
        }
        // An unrecognized spelling is still rejected.
        let bad = "[decode]\ninput_time_format = \"bogus\"\n";
        assert!(parse_into_config(bad).is_err());
    }

    /// Requirements: L2-CFG-001
    #[test]
    fn hash_in_string_not_a_comment() {
        // Verify the parser preserves `#` inside a quoted string rather
        // than treating it as a comment delimiter. Tested at the
        // TOML-parser layer because the validator (Phase 5) now
        // restricts known string-valued keys to enum members, none of
        // which contain `#`.
        let text = r#"
[output]
format = "csv#weird"
"#;
        let doc = parse_toml(text).unwrap();
        match doc.get("output", "format") {
            Some(TomlValue::String(s)) => assert_eq!(s, "csv#weird"),
            other => panic!("expected String(\"csv#weird\"), got {other:?}"),
        }
    }

    /// A repeated `(section, key)` is rejected, matching Python's `tomllib`,
    /// rather than silently keeping the first value (L2-CFG-010).
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_duplicate_key() {
        let err = parse_toml("[decode]\nstrict = true\nstrict = false\n").unwrap_err();
        assert!(err.contains("duplicate key 'strict'"), "got {err:?}");
        assert!(err.contains("[decode]"), "got {err:?}");
        // The same key name in different sections is fine (distinct tables).
        assert!(parse_toml("[a]\nx = 1\n[b]\nx = 2\n").is_ok());
    }

    /// A re-declared `[section]` header is rejected, matching Python's `tomllib`
    /// (the TOML spec forbids defining a table twice) — even when the repeated
    /// block uses different keys, which the `(section, key)` dup-key guard would
    /// not catch.
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_duplicate_section_header() {
        let err = parse_toml("[decode]\nstrict = true\n[decode]\ninput_time_format = \"irig\"\n")
            .unwrap_err();
        assert!(err.contains("declared more than once"), "got {err:?}");
        assert!(err.contains("[decode]"), "got {err:?}");
        // Distinct section headers are fine.
        assert!(parse_toml("[decode]\nstrict = true\n[mux]\nenabled = false\n").is_ok());
    }

    /// TOML forms outside the flat `[section] key = value` schema are rejected,
    /// matching Python: array-of-tables headers (`[[decode]]`) and dotted keys
    /// (`a.b = ...`). Otherwise Rust would misread `[[decode]]` and silently
    /// drop `decode.strict` (including a safety option like `output.no_clobber`).
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_dotted_keys_and_array_tables() {
        let at = parse_toml("[[decode]]\nstrict = true\n").unwrap_err();
        assert!(at.contains("array-of-tables"), "got {at:?}");
        let dk = parse_toml("decode.strict = true\n").unwrap_err();
        assert!(dk.contains("dotted keys"), "got {dk:?}");
        // A dotted key inside a section is rejected too.
        assert!(parse_toml("[decode]\nfoo.bar = 1\n").is_err());
        // Normal flat forms still parse.
        assert!(parse_toml("[decode]\nstrict = true\n[mux]\nenabled = false\n").is_ok());
    }

    /// A dotted section header (`[output.no_clobber]`) nests a table, which the
    /// flat schema does not model; reject it on both implementations rather than
    /// storing a section literally named `output.no_clobber` and dropping its
    /// keys (which silently ignored `no_clobber` on Rust).
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_dotted_section_headers() {
        let err = parse_toml("[output.no_clobber]\nenabled = true\n").unwrap_err();
        assert!(err.contains("dotted section headers"), "got {err:?}");
        // A dotted header for a section with no typed key would otherwise slip
        // through on both implementations; it is rejected too.
        assert!(parse_toml("[decode.foo]\nx = 1\n").is_err());
        // Flat headers still parse.
        assert!(parse_toml("[output]\nno_clobber = true\n").is_ok());
    }

    /// A section name must be a simple identifier, matching Python's `_IDENT_RE`;
    /// a hyphen, space, or quote in the header is a config error rather than an
    /// oddly-named section stored on Rust only.
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_non_identifier_section_headers() {
        for bad in ["[bad-section]\n", "[\"bad\"]\n", "[bad section]\n"] {
            let err = parse_toml(bad).unwrap_err();
            assert!(err.contains("unsupported section header"), "got {err:?}");
        }
        // A valid-identifier (even if unknown) section still parses.
        assert!(parse_toml("[bogus]\nx = 1\n").is_ok());
        assert!(parse_toml("[decode]\nstrict = true\n").is_ok());
    }

    /// A non-identifier key (e.g. a quoted key) is rejected: tomllib would honor
    /// `"strict" = true` by stripping the quotes, but Rust stored it literally —
    /// a silent divergence. Both now reject non-identifier keys (L2-CFG-010).
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_non_identifier_keys() {
        let err = parse_toml("[decode]\n\"strict\" = true\n").unwrap_err();
        assert!(err.contains("simple identifiers"), "got {err:?}");
        // Plain identifier keys still parse.
        assert!(parse_toml("[decode]\nstandard_tick_rate_hz = 1.0\n").is_ok());
    }

    /// The number grammar accepts plain decimal integers/floats but rejects the
    /// non-TOML forms Rust's native `i64`/`f64` parsing would otherwise take
    /// (leading zeros, a bare trailing dot), keeping numeric acceptance aligned
    /// with Python's `tomllib`.
    /// Requirements: L2-CFG-010
    #[test]
    fn number_literal_grammar() {
        for ok in [
            "0",
            "8",
            "-1",
            "+8",
            "100",
            "1.5",
            "0.5",
            "1e6",
            "1000000.0",
            "1.5e-3",
        ] {
            assert!(is_toml_number_literal(ok), "{ok:?} should be accepted");
        }
        for bad in [
            "08", "01", "00", "1.", ".5", "1.e3", "0x08", "1_0", "", "+", "1..2",
        ] {
            assert!(!is_toml_number_literal(bad), "{bad:?} should be rejected");
        }
        // A leading-zero literal is rejected end-to-end, not just by the helper.
        assert!(parse_toml("[decode]\ndetect_records = 08\n").is_err());
        assert!(parse_toml("[filter]\nexclude_rts = [01]\n").is_err());
    }

    /// Schema-invalid but TOML-valid values are load-time config errors, aligned
    /// with the Python loader: a non-string for a string-typed key, and a known
    /// section name assigned a scalar (`decode = true`) rather than a `[table]`.
    /// Requirements: L2-CFG-010
    #[test]
    fn rejects_non_string_and_scalar_sections() {
        assert!(parse_into_config("[decode]\ninput_time_format = 1\n").is_err());
        assert!(parse_into_config("[decode]\nerror_mode = 1\n").is_err());
        for section in ["decode", "logging", "output", "mux", "merge", "filter"] {
            let err = parse_into_config(&format!("{section} = true\n")).unwrap_err();
            assert!(err.0.contains(&format!("[{section}]")), "got {:?}", err.0);
        }
        // A non-section unknown root-level key stays a tolerated unknown-key WARN.
        assert!(parse_into_config("bogus = true\n").is_ok());
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn unknown_input_time_format_rejected() {
        let text = r#"
[decode]
input_time_format = "potato"
"#;
        assert!(parse_into_config(text).is_err());
    }

    /// Requirements: L2-CFG-007
    #[test]
    fn unknown_type_name_rejected() {
        let text = r#"
[filter]
exclude_types = ["UNICORN"]
"#;
        assert!(parse_into_config(text).is_err());
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn unknown_log_level_rejected_at_parse_time() {
        // Regression: previously the config parser accepted any string
        // as logging.level and the bad value was silently dropped at
        // apply time. Now the parser fails fast with a value-level
        // diagnostic.
        let text = "[logging]\nlevel = \"NOPE\"\n";
        let err = parse_into_config(text).unwrap_err();
        assert!(
            err.0.contains("logging.level"),
            "error should mention the field: {}",
            err.0
        );
        assert!(err.0.contains("NOPE"));
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn known_log_levels_accepted_case_insensitively() {
        for level in [
            "DEBUG", "info", "Warning", "WARN", "error", "CRITICAL", "OFF", "off",
        ] {
            let text = format!("[logging]\nlevel = \"{level}\"\n");
            parse_into_config(&text)
                .unwrap_or_else(|e| panic!("expected {level:?} to parse, got: {}", e.0));
        }
    }

    // ── L2-CFG schema validations (Phase 5) ──────────────────────────

    /// Requirements: L2-CFG-010
    #[test]
    fn unknown_output_format_rejected() {
        let text = "[output]\nformat = \"json\"\n";
        let err = parse_into_config(text).unwrap_err();
        assert!(
            err.0.contains("output.format"),
            "error should name the field: {}",
            err.0
        );
        assert!(err.0.contains("json"));
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn output_format_csv_still_accepted() {
        let text = "[output]\nformat = \"csv\"\n";
        let cfg = parse_into_config(text).unwrap();
        assert_eq!(cfg.output_format, "csv");
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn exclude_rts_out_of_range_rejected() {
        // L2-CFG: RT must be in [0, 31]. 32 is out of range.
        let text = "[filter]\nexclude_rts = [32]\n";
        let err = parse_into_config(text).unwrap_err();
        assert!(
            err.0.contains("exclude_rts") && err.0.contains("[0, 31]"),
            "expected range error mentioning [0, 31]: {}",
            err.0
        );
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn exclude_subaddresses_negative_rejected() {
        let text = "[filter]\nexclude_subaddresses = [-1]\n";
        let err = parse_into_config(text).unwrap_err();
        assert!(
            err.0.contains("exclude_subaddresses") && err.0.contains("[0, 31]"),
            "expected range error mentioning [0, 31]: {}",
            err.0
        );
    }

    /// Requirements: L2-CFG-010
    #[test]
    fn exclude_rts_zero_and_thirty_one_accepted() {
        // Boundary values must still parse.
        let text = "[filter]\nexclude_rts = [0, 31]\n";
        let cfg = parse_into_config(text).unwrap();
        assert_eq!(cfg.filters.exclude_rts, vec![0, 31]);
    }

    /// Requirements: L2-CFG-009
    #[test]
    fn unknown_top_level_key_is_warned_not_rejected() {
        // L2-CFG-009: unknown keys WARN at load time but do not fail
        // the load — preserves forward compatibility.
        let text = "[output]\nformat = \"csv\"\nunknown_thing = true\n";
        let cfg = parse_into_config(text).expect("unknown key should warn, not fail");
        assert_eq!(cfg.output_format, "csv");
    }

    /// Requirements: L2-CFG-009
    #[test]
    fn unknown_filter_key_is_warned_not_rejected() {
        // Common typo: exclude_subdresses (missing 'ad').
        let text = "[filter]\nexclude_subdresses = [0]\n";
        let cfg = parse_into_config(text).expect("typo'd key should warn, not fail");
        // The misspelled key gets WARN'd; the correctly-spelled key
        // (had it been written) would not be filtered, so default empty.
        assert_eq!(cfg.filters.exclude_subaddresses, Vec::<u8>::new());
    }

    /// Requirements: L2-CFG-001
    #[test]
    fn missing_eq_returns_line_number() {
        let text = "[decode]\nstrict true\n";
        let err = parse_into_config(text).unwrap_err();
        assert!(err.0.contains("line 2"));
    }

    /// Requirements: L2-CFG-003
    #[test]
    fn defaults_when_no_path() {
        let cfg = load_config(None).unwrap();
        assert_eq!(cfg.log_level, "WARNING");
        assert_eq!(cfg.input_time_format, TimestampFormat::Auto);
        // Inline is the default error mode; separate is opt-in (L2-ERR-011).
        assert_eq!(cfg.error_mode, ErrorMode::Inline);
        assert!(!cfg.strict);
    }

    /// Requirements: L2-CFG-003, L2-CFG-004
    #[test]
    fn overrides_apply_and_filter_merge() {
        let cfg = DecoderConfig {
            filters: FilterConfig {
                exclude_rts: vec![31],
                ..Default::default()
            },
            ..Default::default()
        };
        let merged = cfg.with_overrides(ConfigOverrides {
            input_time_format: Some(TimestampFormat::Standard),
            exclude_rts: vec![0],
            ..Default::default()
        });
        assert_eq!(merged.input_time_format, TimestampFormat::Standard);
        assert_eq!(merged.filters.exclude_rts, vec![31, 0]);
    }

    /// Requirements: L2-CFG-007
    #[test]
    fn type_name_parsing() {
        assert_eq!(parse_type_name("BC_TO_RT").unwrap(), 0x02);
        assert_eq!(parse_type_name("0x20").unwrap(), 0x20);
        assert!(parse_type_name("nope").is_err());
    }

    /// Requirements: L2-CFG-008
    /// The shipped starter config must parse, and must parse to the defaults.
    ///
    /// Resolved from `CARGO_MANIFEST_DIR`, not from the process working
    /// directory. `Path::new("config/default.toml")` looked right but was
    /// relative to `rust/` -- cargo runs tests from the package root -- so it
    /// resolved to a `rust/config/` that has never existed. The body sat behind
    /// `if path.exists()`, so this test and its neighbour **passed without
    /// asserting anything** from the day they were written, while
    /// `docs/TRACE-MATRIX.md` reported L2-CFG-008 as verified on the strength
    /// of this one artifact. The guard is gone: an absent file now fails.
    /// Requirements: L2-CFG-008
    #[test]
    fn parses_default_toml_from_disk() {
        let path = shared_default_toml();
        let cfg = load_config(Some(&path)).unwrap();
        assert_eq!(cfg.output_format, "csv");
        // The four keys added to keep the starter file complete must
        // parse to their documented defaults (correct section + type).
        assert!(!cfg.allow_partial);
        assert!(!cfg.no_clobber);
        assert_eq!(cfg.detect_records, 8);
        assert_eq!(
            cfg.lookahead_records,
            crate::sync::DEFAULT_LOOKAHEAD_RECORDS
        );
    }

    /// `--config` is operator-supplied, so the path is validated before it is
    /// read. `exists()` alone is not enough — it is true for directories and
    /// device files. Mirrors `TestConfigPathValidation` in
    /// `python/tests/test_config.py`, including the message wording.
    /// Requirements: L2-CFG-001
    #[test]
    fn load_config_rejects_non_regular_file() {
        let missing = std::env::temp_dir().join("mie-cfg-does-not-exist-xyz.toml");
        let err = load_config(Some(&missing)).unwrap_err();
        assert!(err.0.contains("Config file not found"), "got {:?}", err.0);

        // A directory passes `exists()`; without the regular-file check this
        // surfaced as a raw OS error from the read instead of a config error.
        let dir = std::env::temp_dir().join(format!("mie-cfg-dir-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let err = load_config(Some(&dir)).unwrap_err();
        let _ = std::fs::remove_dir_all(&dir);
        assert!(
            err.0.contains("not a regular file"),
            "a directory must be rejected as a non-regular file, got {:?}",
            err.0
        );
    }

    /// Config values are TOML *data*. Shell, make and cmd expansion syntax is
    /// stored verbatim — loading never spawns a process, expands an environment
    /// variable, or resolves an include. Pins the `docs/CONFIG-REFERENCE.md`
    /// "Trust boundary" claim on the one key taking a free-form string; mirrors
    /// `test_string_values_are_data_never_interpolated` on the Python side.
    /// Requirements: L2-CFG-001
    #[test]
    fn string_values_are_data_never_interpolated() {
        let literal = "$(whoami)${HOME}`id`%PATH%";
        let cfg = parse_into_config(&format!("[mux]\ndelimiter = \"{literal}\"\n")).unwrap();
        assert_eq!(cfg.mux_delimiter, literal);
    }

    /// A path containing `..` is ordinary input, not an attack to block: the
    /// config location is unrestricted by design (see "Trust boundary"). If that
    /// is ever reversed, this test fails and forces the doc to change with it.
    /// Requirements: L2-CFG-001
    #[test]
    fn traversal_segments_resolve_and_load() {
        let base = std::env::temp_dir().join(format!("mie-cfg-trav-{}", std::process::id()));
        let nested = base.join("sub");
        std::fs::create_dir_all(&nested).unwrap();
        let cfg_path = nested.join("site.toml");
        std::fs::write(&cfg_path, b"[decode]\nstrict = true\n").unwrap();

        let traversed = nested.join("..").join("sub").join("site.toml");
        let loaded = load_config(Some(&traversed));
        let _ = std::fs::remove_dir_all(&base);
        assert!(
            loaded.unwrap().strict,
            "a traversing path must load normally"
        );
    }

    /// `[output] max_sort_group` drives the canonical-order run cap, and is a
    /// recognized schema key (no unknown-key WARN).
    /// Requirements: L2-WRT-022, L3-WRT-003, L2-CFG-001
    #[test]
    fn parses_max_sort_group() {
        let cfg = parse_into_config("[output]\nmax_sort_group = 64\n").unwrap();
        assert_eq!(cfg.max_sort_group, 64);
        assert!(is_known_shared_key("output", "max_sort_group"));
    }

    /// Absent key → the documented default.
    /// Requirements: L2-WRT-022
    #[test]
    fn max_sort_group_defaults_when_absent() {
        let cfg = parse_into_config("[output]\nno_clobber = true\n").unwrap();
        assert_eq!(cfg.max_sort_group, DEFAULT_MAX_SORT_GROUP);
    }

    /// The documented "off" value parses and is in range.
    /// Requirements: L2-WRT-022
    #[test]
    fn max_sort_group_accepts_one() {
        let cfg = parse_into_config("[output]\nmax_sort_group = 1\n").unwrap();
        assert_eq!(cfg.max_sort_group, MAX_SORT_GROUP_MIN);
    }

    /// Out-of-range on either side, and a non-integer, are load-time errors
    /// (L2-CFG-010) rather than a silent clamp — the message names the key so a
    /// shared config file fails identically on Python.
    /// Requirements: L2-WRT-022, L2-CFG-010, L3-WRT-003
    #[test]
    fn max_sort_group_rejects_out_of_range_and_non_integer() {
        for bad in ["0", "1048577", "-5"] {
            let err =
                parse_into_config(&format!("[output]\nmax_sort_group = {bad}\n")).unwrap_err();
            assert!(
                err.0.contains("max_sort_group"),
                "error must name the key, got {:?}",
                err.0
            );
        }
        let not_int = parse_into_config("[output]\nmax_sort_group = true\n").unwrap_err();
        assert!(not_int.0.contains("max_sort_group"), "got {:?}", not_int.0);
    }

    /// The upper bound itself is accepted (inclusive range).
    /// Requirements: L2-WRT-022
    #[test]
    fn max_sort_group_accepts_upper_bound() {
        let cfg = parse_into_config(&format!(
            "[output]\nmax_sort_group = {MAX_SORT_GROUP_MAX}\n"
        ))
        .unwrap();
        assert_eq!(cfg.max_sort_group, MAX_SORT_GROUP_MAX);
    }

    /// A `[merge]` section in a config file must drive the collapse settings,
    /// identically to the Python loader (L2-MRG-007) — previously the Rust
    /// loader ignored the section entirely and warned it was unknown.
    /// Requirements: L2-MRG-007, L2-CFG-001
    #[test]
    fn parses_merge_section() {
        let cfg =
            parse_into_config("[merge]\ncollapse_duplicates = true\ncollapse_window_us = 100\n")
                .unwrap();
        assert!(cfg.collapse_duplicates);
        assert_eq!(cfg.collapse_window_us, 100);
        // The keys are recognized, so they don't trip the unknown-key WARN.
        assert!(is_known_shared_key("merge", "collapse_duplicates"));
        assert!(is_known_shared_key("merge", "collapse_window_us"));
    }

    /// Defaults hold when the section (or a key) is absent.
    /// Requirements: L2-MRG-007
    #[test]
    fn merge_section_defaults_when_absent() {
        let cfg = parse_into_config("[merge]\ncollapse_duplicates = true\n").unwrap();
        assert!(cfg.collapse_duplicates);
        assert_eq!(cfg.collapse_window_us, 0);
    }

    /// A negative window is a load-time error (L2-CFG-010), and a non-integer
    /// window is rejected by the typed getter — matching the Python loader.
    /// Requirements: L2-MRG-007, L2-CFG-010
    #[test]
    fn merge_section_rejects_bad_window() {
        let neg = parse_into_config("[merge]\ncollapse_window_us = -1\n").unwrap_err();
        assert!(neg.0.contains("collapse_window_us"), "got {:?}", neg.0);
        let not_int = parse_into_config("[merge]\ncollapse_window_us = true\n").unwrap_err();
        assert!(
            not_int.0.contains("must be an integer"),
            "got {:?}",
            not_int.0
        );
    }

    /// The advertised "fully-commented starter file" must actually contain
    /// every key documented in `docs/CONFIG-REFERENCE.md` (active or as a
    /// commented example), so the reference config can't silently drift
    /// incomplete again.
    /// Requirements: L2-CFG-001
    #[test]
    fn default_toml_documents_every_schema_key() {
        let path = shared_default_toml();
        let text = std::fs::read_to_string(&path).unwrap();
        let documents = |key: &str| {
            text.lines().any(|line| {
                let line = line.trim_start_matches('#').trim_start();
                line.starts_with(&format!("{key} ")) || line.starts_with(&format!("{key}="))
            })
        };
        for key in [
            "level",
            "input_time_format",
            "strict",
            "error_mode",
            "allow_partial",
            "detect_records",
            "lookahead_records",
            "standard_tick_rate_hz",
            "format",
            "no_clobber",
            "max_sort_group",
            "collapse_duplicates",
            "collapse_window_us",
            "exclude_types",
            "exclude_rts",
            "exclude_buses",
            "exclude_subaddresses",
        ] {
            assert!(
                documents(key),
                "config/default.toml is missing documented key `{key}` \
                 (see docs/CONFIG-REFERENCE.md)"
            );
        }
    }

    /// Requirements: L2-CFG-011, L2-DEC-017
    #[test]
    fn standard_tick_rate_hz_default_is_none() {
        let cfg = parse_into_config("[decode]\ninput_time_format = \"standard\"\n").unwrap();
        assert_eq!(cfg.standard_tick_rate_hz, None);
    }

    /// Requirements: L2-CFG-011
    #[test]
    fn standard_tick_rate_hz_accepts_float_and_int() {
        let as_float = parse_into_config("[decode]\nstandard_tick_rate_hz = 1000000.0\n").unwrap();
        assert_eq!(as_float.standard_tick_rate_hz, Some(1_000_000.0));
        // An operator may write a bare integer; get_float coerces it.
        let as_int = parse_into_config("[decode]\nstandard_tick_rate_hz = 1000000\n").unwrap();
        assert_eq!(as_int.standard_tick_rate_hz, Some(1_000_000.0));
    }

    /// Requirements: L2-CFG-011, L2-CFG-010
    #[test]
    fn standard_tick_rate_hz_rejects_nonpositive() {
        for bad in ["0", "0.0", "-1.0"] {
            let text = format!("[decode]\nstandard_tick_rate_hz = {bad}\n");
            let err = parse_into_config(&text).unwrap_err();
            assert!(
                err.0.contains("standard_tick_rate_hz"),
                "error should name the field for {bad:?}: {}",
                err.0
            );
        }
    }

    /// Requirements: L2-CFG-003
    #[test]
    fn standard_tick_rate_hz_override_applies() {
        let merged = DecoderConfig::default().with_overrides(ConfigOverrides {
            standard_tick_rate_hz: Some(2_000_000.0),
            ..Default::default()
        });
        assert_eq!(merged.standard_tick_rate_hz, Some(2_000_000.0));
    }

    /// Requirements: L2-CFG-001
    #[test]
    #[allow(
        clippy::float_cmp,
        reason = "asserts the parser produced EXACTLY 1500000.0, a value representable in f64; an epsilon comparison would weaken the test"
    )]
    fn parses_float_value() {
        let doc = parse_toml("[decode]\nstandard_tick_rate_hz = 1.5e6\n").unwrap();
        match doc.get("decode", "standard_tick_rate_hz") {
            Some(TomlValue::Float(f)) => assert_eq!(*f, 1_500_000.0),
            other => panic!("expected Float(1500000.0), got {other:?}"),
        }
    }
}
