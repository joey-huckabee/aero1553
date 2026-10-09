//! Core data structures for decoded MIL-STD-1553 MIE binary records.
//!
//! Mirrors the Python `models.py`. All structs are plain (not `Copy`) values;
//! immutability is enforced by Rust's type system rather than by `frozen=True`.
//! `DataWords` replaces `tuple[int, ...]` with an inline-buffer container
//! capped at the MIL-STD-1553B maximum of 32 data words, avoiding per-message
//! heap allocation.

use core::fmt;

// ── Enums ─────────────────────────────────────────────────────────────

/// MIL-STD-1553 redundant bus identifier.
///
/// `Ord` is derived (A < B, by discriminant) so filter-set diagnostics can
/// render a stable, sorted list — see `filter::log_active_filters`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum Bus {
    A = 0,
    B = 1,
}

impl Bus {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::A => "A",
            Self::B => "B",
        }
    }
}

impl fmt::Display for Bus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// MIL-STD-1553 message transfer direction (from RT perspective).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Direction {
    /// BC sends data TO the RT.
    Receive = 0,
    /// RT sends data TO the BC.
    Transmit = 1,
}

/// DDC MIE Type Word message type code (bits 0–6).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum MessageType {
    ModeCommand = 0x01,
    BcToRt = 0x02,
    RtToBc = 0x04,
    RtToRt = 0x08,
    BroadcastBcToRt = 0x10,
    BroadcastRtToRt = 0x18,
    SpuriousData = 0x20,
}

impl MessageType {
    /// Convert raw 8-bit code to enum, or `None` if unknown.
    #[must_use]
    pub fn from_code(code: u8) -> Option<Self> {
        match code {
            0x01 => Some(Self::ModeCommand),
            0x02 => Some(Self::BcToRt),
            0x04 => Some(Self::RtToBc),
            0x08 => Some(Self::RtToRt),
            0x10 => Some(Self::BroadcastBcToRt),
            0x18 => Some(Self::BroadcastRtToRt),
            0x20 => Some(Self::SpuriousData),
            _ => None,
        }
    }

    /// CLI-friendly canonical name (matches Python enum name).
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::ModeCommand => "MODE_COMMAND",
            Self::BcToRt => "BC_TO_RT",
            Self::RtToBc => "RT_TO_BC",
            Self::RtToRt => "RT_TO_RT",
            Self::BroadcastBcToRt => "BROADCAST_BC_TO_RT",
            Self::BroadcastRtToRt => "BROADCAST_RT_TO_RT",
            Self::SpuriousData => "SPURIOUS_DATA",
        }
    }
}

/// O(1) check that a raw type code is in the known set.
#[inline]
#[must_use]
pub fn is_valid_message_type(code: u8) -> bool {
    matches!(code, 0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0x18 | 0x20)
}

/// Classified message format determining the payload layout.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum MessageFormat {
    Receive = 1,
    Transmit = 2,
    RtToRt = 3,
    ReceiveBroadcast = 4,
    RtToRtBroadcast = 5,
    ModeCodeTxData = 6,
    ModeCodeRxData = 7,
    ModeCodeNoData = 8,
    ModeCodeBcastNoData = 9,
    ModeCodeBcastData = 10,
    SpuriousData = 11,
}

/// Timestamp encoding format used in the MIE binary file.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum TimestampFormat {
    Auto = 0,
    Irig = 1,
    Standard = 2,
}

impl TimestampFormat {
    /// Parse an `input_time_format` name (`auto` / `irig` / `standard`)
    /// case-insensitively. The single source of truth shared by the CLI
    /// (`--input-time-format`) and the config loader
    /// (`decode.input_time_format`) so the two can never disagree on which
    /// spellings are accepted. Returns `None` for an unrecognized name; each
    /// caller formats its own error type.
    pub(crate) fn from_name_ci(name: &str) -> Option<Self> {
        match name.to_ascii_lowercase().as_str() {
            "auto" => Some(Self::Auto),
            "irig" => Some(Self::Irig),
            "standard" => Some(Self::Standard),
            _ => None,
        }
    }
}

/// Rendering selected for the `TIME_STAMP` CSV column (L2-WRT-025).
///
/// This is the *output* half of what used to be one `--time-format` flag.
/// [`TimestampFormat`] decides how the bytes on disk are parsed;
/// `OutputTimeFormat` decides how the resulting instant is written down. The
/// two are independent: any input encoding can feed any rendering, subject to
/// the calendar preconditions of L2-WRT-026.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
#[repr(u8)]
pub enum OutputTimeFormat {
    /// `DAY:HH:MM:SS.uuuuuu` -- the DDC vendor rendering, and the default.
    /// Byte-identical to what every version before v3.0.0 emitted.
    #[default]
    Doy = 0,
    /// `YYYY-MM-DDTHH:MM:SS.uuuuuu` plus a zone designator.
    Iso = 1,
    /// `DD:HH:MM:SS.uuuuuu` -- day of month, with the month deliberately
    /// absent from the cell.
    Dom = 2,
}

impl OutputTimeFormat {
    /// Parse an `output_time_format` name case-insensitively. Shared by the CLI
    /// (`--output-time-format`) and the config loader
    /// (`output.output_time_format`), mirroring [`TimestampFormat::from_name_ci`].
    pub(crate) fn from_name_ci(name: &str) -> Option<Self> {
        match name.to_ascii_lowercase().as_str() {
            "doy" => Some(Self::Doy),
            "iso" => Some(Self::Iso),
            "dom" => Some(Self::Dom),
            _ => None,
        }
    }

    /// The canonical lowercase spelling, for diagnostics and log lines.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Doy => "doy",
            Self::Iso => "iso",
            Self::Dom => "dom",
        }
    }

    /// Whether this rendering resolves day-of-year to a calendar date, and so
    /// carries the preconditions of L2-WRT-026 (a year is required; the
    /// recording must be calendar-locked).
    #[must_use]
    pub fn needs_calendar(self) -> bool {
        matches!(self, Self::Iso | Self::Dom)
    }
}

/// Everything the `TIME_STAMP` formatter needs beyond the timestamp itself
/// (L2-WRT-025). Resolved once per decode from the merged configuration and
/// carried unchanged through the writer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct TimeRender {
    /// Which of the three renderings to produce.
    pub format: OutputTimeFormat,
    /// Calendar year for the day-of-year resolution. Required by `Iso` and
    /// `Dom`; ignored by `Doy` (L2-WRT-026 clause 5). `None` means no year was
    /// configured, which the CLI refuses up front for a calendar rendering.
    pub year: Option<u16>,
    /// Offset from UTC in minutes, used only by `Iso`. Zero renders as `Z`.
    pub utc_offset_minutes: i16,
}

impl TimeRender {
    /// The default rendering: day-of-year, no calendar resolution. This is what
    /// `dump` uses unconditionally and what every pre-v3.0.0 decode produced.
    #[must_use]
    pub fn doy() -> Self {
        Self::default()
    }

    /// A rendering whose year and offset are known to be in range -- the checked
    /// way to build one from values nobody has validated yet.
    ///
    /// The fields stay public, so a literal can still hold anything; a value
    /// out of range there renders a five-digit year or an offset like `+83:20`.
    /// The CLI and config loader check as they parse, and the Python bindings
    /// build every rendering through this. The inputs are `i64` so a caller
    /// holding a wider integer -- a Python `int` -- gets a range error rather
    /// than having to narrow first and report an overflow instead.
    ///
    /// # Errors
    ///
    /// [`TimeRenderError`] when `year` is outside [`YEAR_MIN`]`..=`[`YEAR_MAX`]
    /// or `utc_offset_minutes` outside +/-[`MAX_UTC_OFFSET_MINUTES`].
    pub fn new(
        format: OutputTimeFormat,
        year: Option<i64>,
        utc_offset_minutes: i64,
    ) -> Result<Self, TimeRenderError> {
        Ok(Self {
            format,
            year: year.map(check_year).transpose()?,
            utc_offset_minutes: check_utc_offset(utc_offset_minutes)?,
        })
    }
}

/// A year or UTC offset outside the range a rendering can represent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeRenderError {
    /// The year is outside [`YEAR_MIN`]`..=`[`YEAR_MAX`] (L2-WRT-026 clause 1).
    YearOutOfRange(i64),
    /// The offset is outside +/-[`MAX_UTC_OFFSET_MINUTES`] (L2-CFG-012).
    UtcOffsetOutOfRange(i64),
}

impl std::fmt::Display for TimeRenderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::YearOutOfRange(y) => {
                write!(f, "year must be in [{YEAR_MIN}, {YEAR_MAX}], got {y}")
            }
            Self::UtcOffsetOutOfRange(m) => write!(
                f,
                "utc_offset_minutes must be in [-{MAX_UTC_OFFSET_MINUTES}, \
                 {MAX_UTC_OFFSET_MINUTES}], got {m}"
            ),
        }
    }
}

impl std::error::Error for TimeRenderError {}

/// The one year-range check (L2-WRT-026 clause 1). The CLI, the config loader
/// and the Python bindings all call it, each wording the failure its own way.
///
/// # Errors
///
/// [`TimeRenderError::YearOutOfRange`] outside [`YEAR_MIN`]`..=`[`YEAR_MAX`].
pub fn check_year(year: i64) -> Result<u16, TimeRenderError> {
    match u16::try_from(year) {
        Ok(y) if (YEAR_MIN..=YEAR_MAX).contains(&y) => Ok(y),
        _ => Err(TimeRenderError::YearOutOfRange(year)),
    }
}

/// The one UTC-offset range check. Compared in `i64`, never through `abs()`:
/// the `columns()` binding once tested `i16::abs(m) > 1439`, and `i16::MIN`
/// has no positive counterpart -- a release build wrapped it back to itself,
/// which is less than 1439, so -32768 passed.
///
/// # Errors
///
/// [`TimeRenderError::UtcOffsetOutOfRange`] outside +/-[`MAX_UTC_OFFSET_MINUTES`].
pub fn check_utc_offset(minutes: i64) -> Result<i16, TimeRenderError> {
    let limit = i64::from(MAX_UTC_OFFSET_MINUTES);
    if (-limit..=limit).contains(&minutes) {
        i16::try_from(minutes).map_err(|_| TimeRenderError::UtcOffsetOutOfRange(minutes))
    } else {
        Err(TimeRenderError::UtcOffsetOutOfRange(minutes))
    }
}

/// Why a calendar rendering could not be produced (L2-WRT-026).
///
/// Two of the three preconditions are checked before the first row is written
/// -- a missing year at CLI parse time, a non-calendar-locked recording once
/// the format resolves -- so the only variant that normally reaches a formatter
/// is [`Self::NoSuchDay`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CalendarError {
    /// A calendar rendering was selected with no year resolved from either the
    /// configuration or the CLI.
    MissingYear,
    /// The day-of-year does not exist in the configured year -- day 366 of a
    /// common year, or a day outside `1..=366` entirely.
    NoSuchDay { day: u16, year: u16 },
    /// The recording uses the Standard encoding: a free-running counter with
    /// no epoch and no calendar meaning, whatever year is configured.
    NotCalendarLocked,
    /// The IRIG timestamp is freerun -- the card had no valid IRIG-B lock, so
    /// its day/hour/minute/second fields are relative, not calendar-anchored.
    /// They would render as a plausible date that means nothing.
    FreerunNotAnchored,
}

/// Inclusive bounds on a configured calendar year (L2-WRT-026 clause 1).
///
/// The upper bound is four digits because the `iso` rendering formats the year
/// as exactly `YYYY`; a five-digit year would widen column 1 without warning.
/// The lower bound is 1 because year 0 does not exist in the proleptic
/// Gregorian numbering this decoder uses.
pub const YEAR_MIN: u16 = 1;
/// See [`YEAR_MIN`].
pub const YEAR_MAX: u16 = 9999;

/// The largest UTC offset magnitude, in minutes: `+23:59` / `-23:59`, the most
/// the `+HH:MM` grammar of L2-CFG-012 can spell.
pub const MAX_UTC_OFFSET_MINUTES: i16 = 23 * 60 + 59;

/// Days in each month of a common year, January first.
const COMMON_YEAR_MONTH_LENGTHS: [u16; 12] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/// Proleptic Gregorian leap-year test (L2-WRT-025): divisible by 4, except
/// centuries, which must also be divisible by 400.
///
/// Hand-rolled rather than delegated to a date library so that all three
/// implementations compute it identically -- C++11 has no date type at all, and
/// a shared rule stated once is easier to hold aligned than three library
/// behaviours that merely agree today.
#[must_use]
pub fn is_leap_year(year: u16) -> bool {
    year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400))
}

/// Resolve a 1-based day-of-year to `(month, day_of_month)`, both 1-based.
///
/// Returns `None` when the day does not exist in that year: day `366` of a
/// common year, day `0`, or anything above `366`. That is the L2-WRT-026
/// clause 3 condition, and the caller turns it into a refusal rather than
/// rolling forward into the next January.
#[must_use]
pub fn day_of_year_to_month_day(year: u16, day_of_year: u16) -> Option<(u8, u8)> {
    let leap = is_leap_year(year);
    let year_length = if leap { 366 } else { 365 };
    if day_of_year == 0 || day_of_year > year_length {
        return None;
    }

    let mut remaining = day_of_year;
    for (index, &base_length) in COMMON_YEAR_MONTH_LENGTHS.iter().enumerate() {
        // February is the only month whose length depends on the year.
        let length = if index == 1 && leap {
            base_length + 1
        } else {
            base_length
        };
        if remaining <= length {
            // Both fit in u8: month is 1..=12 and day is 1..=31.
            #[allow(clippy::cast_possible_truncation)]
            return Some(((index + 1) as u8, remaining as u8));
        }
        remaining -= length;
    }
    // Unreachable: the month lengths sum to `year_length`, which bounds
    // `day_of_year` above.
    None
}

/// How errored messages are routed in CSV output.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum ErrorMode {
    /// Errored + spurious go to a separate `<output>_errors.csv`.
    Separate = 0,
    /// Everything in one CSV with `ERROR/ERROR_CODE` columns populated.
    Inline = 1,
}

/// Scope over which DELTA is measured in a multi-file merge (L2-MRG-005).
///
/// Only meaningful when more than one input is decoded; with a single input the
/// two are the same computation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum DeltaScope {
    /// Default. Each record's gap is to the previous same-key record **from its
    /// own file**, so the value matches what that file would produce decoded on
    /// its own — and what the DDC vendor tool reports, since it has no merge.
    PerFile = 0,
    /// Each record's gap is to the previous same-key record from **any** input,
    /// measured across the merged timeline. Answers "how long since any recorder
    /// last saw this key"; compresses gaps whenever one key appears in several
    /// inputs.
    Global = 1,
}

impl DeltaScope {
    /// Parse a `delta_scope` name (`per-file` / `global`) case-insensitively.
    /// The single source of truth shared by the CLI (`--delta-scope`) and the
    /// config loader (`merge.delta_scope`) so the two cannot disagree on which
    /// spellings are accepted. Returns `None` for an unrecognized name; each
    /// caller formats its own error type.
    pub(crate) fn from_name_ci(name: &str) -> Option<Self> {
        match name.to_ascii_lowercase().as_str() {
            "per-file" => Some(Self::PerFile),
            "global" => Some(Self::Global),
            _ => None,
        }
    }

    /// Canonical spelling, for help text and error messages.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::PerFile => "per-file",
            Self::Global => "global",
        }
    }
}

// ── DDC + decoder error codes ─────────────────────────────────────────

/// Manchester encoding, parity, or bit count error.
pub const ERROR_MANCHESTER_PARITY: u16 = 0x011E;
/// No status word response, or too few data words.
pub const ERROR_NO_RESPONSE: u16 = 0x0120;
/// Inverted sync pattern detected on a data word.
pub const ERROR_INVERTED_SYNC: u16 = 0x0136;
/// More data words received than the Command Word specified.
pub const ERROR_TOO_MANY_WORDS: u16 = 0x0140;
/// Unknown / undocumented DDC error.
pub const ERROR_UNKNOWN_DDC: u16 = 0x0150;

/// `SPURIOUS_DATA` continuation of a preceding errored message (decoder-assigned).
pub const ERROR_SPURIOUS_CONTINUATION: u16 = 0x2000;
/// Standalone `SPURIOUS_DATA`, no preceding error record (decoder-assigned).
pub const ERROR_SPURIOUS_STANDALONE: u16 = 0x2001;

/// True if `code` is a known DDC hardware error code (0x01xx range).
#[inline]
#[must_use]
pub fn is_known_ddc_error_code(code: u16) -> bool {
    matches!(
        code,
        ERROR_MANCHESTER_PARITY
            | ERROR_NO_RESPONSE
            | ERROR_INVERTED_SYNC
            | ERROR_TOO_MANY_WORDS
            | ERROR_UNKNOWN_DDC
    )
}

/// True if `code` is a decoder-assigned spurious code (0x20xx range).
#[inline]
#[must_use]
pub fn is_known_custom_error_code(code: u16) -> bool {
    matches!(
        code,
        ERROR_SPURIOUS_CONTINUATION | ERROR_SPURIOUS_STANDALONE
    )
}

/// True if `code` is in either known set.
#[inline]
#[must_use]
pub fn is_known_error_code(code: u16) -> bool {
    is_known_ddc_error_code(code) || is_known_custom_error_code(code)
}

/// Human-readable description for a known error code, else the
/// `"unknown DDC error code"` fallback.
///
/// Prefer this over [`ddc_error_description`] anywhere the result is rendered
/// for a human: that function returns an empty string for an unrecognized
/// code, which formats as an uninformative `code=0x0199 ()`. Python's
/// equivalent lookups always carry a fallback, so this keeps the two
/// implementations' operator-facing text aligned.
#[must_use]
pub fn ddc_error_description_or_unknown(code: u16) -> &'static str {
    let desc = ddc_error_description(code);
    if desc.is_empty() {
        "unknown DDC error code"
    } else {
        desc
    }
}

/// Human-readable description for a known error code, else empty string.
#[must_use]
pub fn ddc_error_description(code: u16) -> &'static str {
    match code {
        ERROR_MANCHESTER_PARITY => "Manchester/Parity Error or Bit Count Error",
        ERROR_NO_RESPONSE => "No Status Response or Too Few Data Words",
        ERROR_INVERTED_SYNC => "Inverted Sync on Data Word",
        ERROR_TOO_MANY_WORDS => "Too Many Data Words",
        ERROR_UNKNOWN_DDC => "Unknown DDC Error",
        _ => "",
    }
}

// ── Timestamps ────────────────────────────────────────────────────────

/// IRIG-format timestamp decoded from a 3-word binary field.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IrigTimestamp {
    pub day: u16,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub microsecond: u32,
    pub freerun: bool,
}

impl IrigTimestamp {
    /// Absolute microseconds from start of year.
    #[must_use]
    pub fn to_total_microseconds(self) -> u64 {
        let day = u64::from(self.day);
        let hour = u64::from(self.hour);
        let minute = u64::from(self.minute);
        let second = u64::from(self.second);
        let micro = u64::from(self.microsecond);
        (day * 86_400 + hour * 3_600 + minute * 60 + second) * 1_000_000 + micro
    }

    /// Format as `DAY:HH:MM:SS.uuuuuu` (matches DDC vendor CSV layout).
    ///
    /// Per L2-DEC-014 the microsecond field SHALL be exactly six
    /// digits. Validation in `sync::validate_record` should reject any
    /// record whose microsecond is >= `1_000_000` (L2-SYN-004), so this
    /// truncation is a defensive belt-and-suspenders: if a caller
    /// constructs an out-of-range `IrigTimestamp` directly (bypassing
    /// validation), the formatter still produces a well-formed string.
    #[must_use]
    pub fn format(&self) -> String {
        let micro = self.microsecond % 1_000_000;
        format!(
            "{day}:{h:02}:{m:02}:{s:02}.{u:06}",
            day = self.day,
            h = self.hour,
            m = self.minute,
            s = self.second,
            u = micro
        )
    }

    /// Format under the selected rendering (L2-WRT-025).
    ///
    /// [`OutputTimeFormat::Doy`] is infallible and byte-identical to
    /// [`Self::format`]. The calendar renderings resolve `day` against
    /// `render.year` and refuse rather than approximate when that resolution
    /// has no answer -- day 366 of a common year does not become January 1st
    /// (L2-WRT-026 clause 3).
    ///
    /// Every rendering emits exactly six microsecond digits; L2-DEC-014 is not
    /// relaxed by the wider cell.
    ///
    /// # Errors
    ///
    /// Returns [`CalendarError`] when a calendar rendering cannot be resolved:
    /// no year was configured, the timestamp is freerun, or the day-of-year
    /// does not exist in that year. `Doy` never fails.
    pub fn format_with(&self, render: TimeRender) -> Result<String, CalendarError> {
        let micro = self.microsecond % 1_000_000;
        let (year, month, day_of_month) = match render.format {
            OutputTimeFormat::Doy => return Ok(self.format()),
            _ => {
                // L2-WRT-026 clause 2: a freerun record's fields are relative,
                // so resolving them against a year would produce a date that
                // looks entirely ordinary and means nothing.
                if self.freerun {
                    return Err(CalendarError::FreerunNotAnchored);
                }
                let year = render.year.ok_or(CalendarError::MissingYear)?;
                let (month, day_of_month) =
                    day_of_year_to_month_day(year, self.day).ok_or(CalendarError::NoSuchDay {
                        day: self.day,
                        year,
                    })?;
                (year, month, day_of_month)
            }
        };

        Ok(if render.format == OutputTimeFormat::Iso {
            format!(
                "{year:04}-{month:02}-{day_of_month:02}T{h:02}:{m:02}:{s:02}.{u:06}{zone}",
                h = self.hour,
                m = self.minute,
                s = self.second,
                u = micro,
                zone = format_utc_offset(render.utc_offset_minutes)
            )
        } else {
            format!(
                "{day_of_month:02}:{h:02}:{m:02}:{s:02}.{u:06}",
                h = self.hour,
                m = self.minute,
                s = self.second,
                u = micro
            )
        })
    }
}

/// Render a UTC offset in minutes as an ISO-8601 designator: `Z` at zero,
/// otherwise `+HH:MM` / `-HH:MM`.
#[must_use]
pub fn format_utc_offset(minutes: i16) -> String {
    if minutes == 0 {
        return "Z".to_string();
    }
    let sign = if minutes < 0 { '-' } else { '+' };
    let abs = minutes.unsigned_abs();
    format!("{sign}{hh:02}:{mm:02}", hh = abs / 60, mm = abs % 60)
}

/// Standard-format timestamp decoded from a 2-word free-running counter.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StandardTimestamp {
    pub raw_value: u32,
    pub upper_word: u16,
    pub lower_word: u16,
}

impl StandardTimestamp {
    /// Raw 32-bit free-running counter value, in unknown tick units.
    /// The tick rate is card-dependent and not encoded in the file, so
    /// callers cannot convert this to seconds without external calibration.
    #[must_use]
    pub fn raw_ticks(self) -> u32 {
        self.raw_value
    }

    /// Convert raw counter ticks to microseconds using an external tick rate.
    ///
    /// `standard_tick_rate_hz` is the card-dependent counter frequency in Hz,
    /// supplied out-of-band (the file does not encode it). Returns `None`
    /// unless the rate is finite and strictly positive, so an uncalibrated or
    /// invalid rate can never be mistaken for real timing.
    ///
    /// Rounding is half-away-from-zero, by `f64::round` on the exact quotient
    /// (see L2-DEC-017). `floor(x + 0.5)` is not equivalent: the addition
    /// rounds first.
    #[must_use]
    pub fn to_microseconds(self, standard_tick_rate_hz: f64) -> Option<u64> {
        // 2^64 exactly, which f64 represents without rounding -- so this bound
        // needs no cast of its own. Comparing against `u64::MAX as f64` would
        // have introduced the very kind of lossy cast this guard exists to
        // avoid (u64::MAX is not representable in f64 and rounds UP, so the
        // comparison would have admitted a value that then saturates).
        const TWO_POW_64: f64 = 18_446_744_073_709_551_616.0;

        if !standard_tick_rate_hz.is_finite() || standard_tick_rate_hz <= 0.0 {
            return None;
        }
        let micros = f64::from(self.raw_value) * 1_000_000.0 / standard_tick_rate_hz;
        // The function already answers with an Option, so a value that cannot
        // be a microsecond count answers "no value" rather than saturating.
        // `as` would clamp an absurd result -- a vanishingly small tick rate
        // makes `micros` enormous -- to u64::MAX, which would then read as a
        // real timestamp downstream. This is the one cast in this file where
        // the out-of-range case is reachable from operator input.
        let rounded = micros.round();
        if !rounded.is_finite() || !(0.0..TWO_POW_64).contains(&rounded) {
            return None;
        }
        #[allow(
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss,
            reason = "guarded immediately above: finite, non-negative, below 2^64"
        )]
        let micros = rounded as u64;
        Some(micros)
    }

    /// Format as `0xNNNNNNNN`.
    #[must_use]
    pub fn format(&self) -> String {
        format!("0x{:08X}", self.raw_value)
    }

    /// A free-running counter has no calendar meaning under any rendering, so
    /// this is [`Self::format`] whatever `render` selects.
    ///
    /// Under `doy` this is [`Self::format`]. Under a calendar rendering it
    /// refuses (L2-WRT-026 clause 2): a free-running counter has no epoch, so
    /// no year can place it on a calendar, and quietly emitting raw hex into a
    /// column the operator asked to be ISO-8601 would be its own kind of lie.
    ///
    /// # Errors
    ///
    /// Returns [`CalendarError::NotCalendarLocked`] under `Iso` or `Dom`.
    pub fn format_with(&self, render: TimeRender) -> Result<String, CalendarError> {
        if render.format.needs_calendar() {
            return Err(CalendarError::NotCalendarLocked);
        }
        Ok(self.format())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Timestamp {
    Irig(IrigTimestamp),
    Standard(StandardTimestamp),
}

impl Timestamp {
    /// Absolute microseconds from a known epoch, if convertible.
    ///
    /// `Some(us)` for IRIG (microseconds from start of year).
    ///
    /// For Standard, the result depends on calibration: `Some(us)` when
    /// `standard_tick_rate_hz` is a finite, strictly-positive counter
    /// frequency, otherwise `None` — raw counter ticks have no known tick
    /// rate or epoch, so DELTA in seconds cannot be computed truthfully
    /// without one. IRIG ignores `standard_tick_rate_hz`. See L2-DEC-017.
    #[must_use]
    pub fn to_microseconds(&self, standard_tick_rate_hz: Option<f64>) -> Option<u64> {
        match self {
            Self::Irig(t) => Some(t.to_total_microseconds()),
            Self::Standard(t) => standard_tick_rate_hz.and_then(|hz| t.to_microseconds(hz)),
        }
    }

    #[must_use]
    pub fn format(&self) -> String {
        match self {
            Self::Irig(t) => t.format(),
            Self::Standard(t) => t.format(),
        }
    }

    /// Format under the selected rendering (L2-WRT-025), dispatching on the
    /// variant. This is what the CSV writer calls; [`Self::format`] remains the
    /// day-of-year rendering that L2-WRT-011 pins and that `dump` uses.
    ///
    /// # Errors
    ///
    /// Returns [`CalendarError`] for whichever variant declines; see
    /// [`IrigTimestamp::format_with`] and [`StandardTimestamp::format_with`].
    pub fn format_with(&self, render: TimeRender) -> Result<String, CalendarError> {
        match self {
            Self::Irig(t) => t.format_with(render),
            Self::Standard(t) => t.format_with(render),
        }
    }
}

/// Number of 16-bit words consumed by each timestamp format.
#[must_use]
pub const fn timestamp_word_count(fmt: TimestampFormat) -> u16 {
    match fmt {
        TimestampFormat::Irig => 3,
        TimestampFormat::Standard => 2,
        TimestampFormat::Auto => 0,
    }
}

// ── TypeWord / CommandWord ────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TypeWord {
    pub message_type: u8,
    pub bus: Bus,
    pub word_count: u16,
    pub error: bool,
    pub raw: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CommandWord {
    /// Remote Terminal address (0–30; 31 = broadcast).
    pub rt: u8,
    pub direction: Direction,
    /// Subaddress (0–31; 0 and 31 are mode codes).
    pub subaddress: u8,
    /// Number of data words (1–32; raw 0 maps to 32).
    pub data_word_count: u8,
    pub raw: u16,
}

impl CommandWord {
    #[must_use]
    pub fn is_broadcast(&self) -> bool {
        self.rt == 31
    }
    #[must_use]
    pub fn is_mode_code(&self) -> bool {
        self.subaddress == 0 || self.subaddress == 31
    }
}

// ── DataWords (inline-buffer Vec replacement) ─────────────────────────

/// MIL-STD-1553B caps a single transaction at 32 data words.
pub const MAX_DATA_WORDS: usize = 32;

/// Fixed-capacity 16-bit word buffer. Avoids heap allocation per message.
#[derive(Clone, Copy)]
pub struct DataWords {
    buf: [u16; MAX_DATA_WORDS],
    len: u8,
}

impl DataWords {
    #[must_use]
    pub const fn new() -> Self {
        Self {
            buf: [0; MAX_DATA_WORDS],
            len: 0,
        }
    }

    /// Build from a slice, keeping at most [`MAX_DATA_WORDS`] words. A longer
    /// slice is truncated rather than panicking: MIL-STD-1553B caps a single
    /// transaction at 32 data words, so any standard-conforming caller is
    /// unaffected, and the over-length case stays total (L1-ROB-001) — matching
    /// [`DataWords::from_iter_capped`]. (In-crate callers already pre-cap, so
    /// the truncation is defensive.)
    #[must_use]
    pub fn from_slice(slice: &[u16]) -> Self {
        let n = slice.len().min(MAX_DATA_WORDS);
        let mut buf = [0u16; MAX_DATA_WORDS];
        buf[..n].copy_from_slice(&slice[..n]);
        // `n` is `slice.len().min(MAX_DATA_WORDS)` and MAX_DATA_WORDS is 32, so
        // this cannot truncate. Stated as an allow rather than a `try_from`
        // with an invented fallback: there is no sensible value to fall back
        // TO, and inventing one would hide a broken invariant instead of
        // documenting a held one.
        #[allow(
            clippy::cast_possible_truncation,
            reason = "n is clamped to MAX_DATA_WORDS (32) immediately above"
        )]
        let len = n as u8;
        Self { buf, len }
    }

    /// Build from an iterator of `u16`. Stops at `MAX_DATA_WORDS`.
    pub fn from_iter_capped<I: IntoIterator<Item = u16>>(iter: I) -> Self {
        let mut out = Self::new();
        for w in iter {
            if !out.try_push(w) {
                break;
            }
        }
        out
    }

    /// Append one word; returns false if already full.
    #[inline]
    pub fn try_push(&mut self, word: u16) -> bool {
        let i = self.len as usize;
        if i >= MAX_DATA_WORDS {
            return false;
        }
        self.buf[i] = word;
        self.len += 1;
        true
    }

    #[inline]
    #[must_use]
    pub fn len(&self) -> usize {
        self.len as usize
    }

    #[inline]
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    #[inline]
    #[must_use]
    pub fn as_slice(&self) -> &[u16] {
        &self.buf[..self.len as usize]
    }

    pub fn iter(&self) -> std::slice::Iter<'_, u16> {
        self.as_slice().iter()
    }
}

impl<'a> IntoIterator for &'a DataWords {
    type Item = &'a u16;
    type IntoIter = std::slice::Iter<'a, u16>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}

impl Default for DataWords {
    fn default() -> Self {
        Self::new()
    }
}

impl PartialEq for DataWords {
    fn eq(&self, other: &Self) -> bool {
        self.as_slice() == other.as_slice()
    }
}
impl Eq for DataWords {}

impl fmt::Debug for DataWords {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_list()
            .entries(self.as_slice().iter().map(|w| format!("0x{w:04X}")))
            .finish()
    }
}

// ── MieMessage ────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub struct MieMessage {
    pub timestamp: Timestamp,
    pub type_word: TypeWord,
    pub message_format: MessageFormat,
    pub command_word: Option<CommandWord>,
    pub command_word_2: Option<CommandWord>,
    pub status_word: Option<u16>,
    pub status_word_2: Option<u16>,
    pub data_words: DataWords,
    pub error_word: Option<u16>,
    /// Seconds since prior message with the same RT+MSG.
    ///
    /// `Some(0.0)` on first occurrence of an RT/MSG key with a calibrated
    /// timestamp. `Some(s)` for a non-negative gap. `None` when no DELTA
    /// is meaningful: `SPURIOUS_DATA` (no RT/MSG key), uncalibrated Standard
    /// timestamps (no known tick rate), and non-monotonic timestamps.
    pub delta: Option<f64>,
    pub file_offset: u64,
    /// MUX column value derived from the source file name (L2-WRT-020), shared
    /// (one `Arc<str>` per input file) so per-record carry stays O(1) in resident
    /// memory. `None` when MUX population is disabled or the configured filename
    /// field is absent/empty.
    pub mux: Option<std::sync::Arc<str>>,
}

impl MieMessage {
    #[must_use]
    pub fn rt(&self) -> Option<u8> {
        self.command_word.map(|c| c.rt)
    }

    #[must_use]
    pub fn subaddress(&self) -> Option<u8> {
        self.command_word.map(|c| c.subaddress)
    }

    #[must_use]
    pub fn bus(&self) -> Bus {
        self.type_word.bus
    }

    /// Message label in `<SA><T|R>` format, or empty for `SPURIOUS_DATA`.
    #[must_use]
    pub fn msg_label(&self) -> String {
        match self.command_word {
            None => String::new(),
            Some(cw) => {
                let suffix = match cw.direction {
                    Direction::Transmit => 'T',
                    Direction::Receive => 'R',
                };
                format!("{}{}", cw.subaddress, suffix)
            }
        }
    }

    /// Unique key for per-RT/MSG delta tracking. Empty for `SPURIOUS_DATA`.
    #[must_use]
    pub fn delta_key(&self) -> String {
        match self.command_word {
            None => String::new(),
            Some(cw) => {
                let suffix = match cw.direction {
                    Direction::Transmit => 'T',
                    Direction::Receive => 'R',
                };
                format!("{}:{}{}", cw.rt, cw.subaddress, suffix)
            }
        }
    }

    #[must_use]
    pub fn is_error(&self) -> bool {
        self.type_word.error
    }

    #[must_use]
    pub fn is_spurious(&self) -> bool {
        self.message_format == MessageFormat::SpuriousData
    }

    /// A `SPURIOUS_DATA` record that continues the errored record decoded
    /// immediately before it (decoder code `0x2000`, L2-ERR-005).
    ///
    /// Such a record is the second half of its parent's transaction, so the
    /// stages that remove records -- filtering (L2-FLT-001) and duplicate
    /// collapse (L2-MRG-007) -- decide its fate through its parent rather than
    /// on its own.
    #[must_use]
    pub fn is_continuation(&self) -> bool {
        self.is_spurious() && self.error_word == Some(ERROR_SPURIOUS_CONTINUATION)
    }

    /// CSV-column error label: `""`, `"ERROR"`, or `"SPURIOUS"`.
    ///
    /// The message type decides first. A `SPURIOUS_DATA` record is labelled
    /// `SPURIOUS` even with Type Word bit 14 set: it is decoded as spurious,
    /// coded `0x2000`/`0x2001` and timed accordingly, and an `ERROR` row
    /// promises a DDC hardware code read from an Error Word, which it does not
    /// carry. The raw bit stays visible in [`Self::is_error`] and in `dump`.
    #[must_use]
    pub fn error_label(&self) -> &'static str {
        if self.is_spurious() {
            "SPURIOUS"
        } else if self.type_word.error {
            "ERROR"
        } else {
            ""
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Requirements: L2-MSG-001
    #[test]
    fn message_type_round_trip() {
        for code in [0x01u8, 0x02, 0x04, 0x08, 0x10, 0x18, 0x20] {
            let mt = MessageType::from_code(code).unwrap();
            assert_eq!(mt as u8, code);
            assert!(is_valid_message_type(code));
        }
        assert!(MessageType::from_code(0x03).is_none());
        assert!(!is_valid_message_type(0x03));
    }

    /// Requirements: L2-WRT-011
    #[test]
    fn irig_format_matches_python_layout() {
        let t = IrigTimestamp {
            day: 10,
            hour: 15,
            minute: 54,
            second: 50,
            microsecond: 456_225,
            freerun: false,
        };
        assert_eq!(t.format(), "10:15:54:50.456225");
    }

    /// Requirements: L2-DEC-014
    #[test]
    fn irig_format_truncates_out_of_range_microseconds() {
        // L2-DEC-014: formatter SHALL emit exactly six microsecond
        // digits. Validation (L2-SYN-004) rejects records with
        // microsecond >= 1_000_000 before we get here, but a caller
        // who constructs an IrigTimestamp directly with a bad
        // microsecond MUST still produce a well-formed string.
        let t = IrigTimestamp {
            day: 1,
            hour: 0,
            minute: 0,
            second: 0,
            microsecond: 1_234_567, // > 999_999
            freerun: false,
        };
        let s = t.format();
        assert_eq!(s, "1:00:00:00.234567");
        // Sanity: the part after the '.' is exactly six characters.
        let micro_part = s.rsplit('.').next().unwrap();
        assert_eq!(micro_part.len(), 6);
    }

    /// Requirements: L2-DEC-007
    #[test]
    fn standard_format_hex() {
        let t = StandardTimestamp {
            raw_value: 0x1234_ABCD,
            upper_word: 0x1234,
            lower_word: 0xABCD,
        };
        assert_eq!(t.format(), "0x1234ABCD");
    }

    /// Requirements: L2-RDR-019, L2-DEC-017
    #[test]
    fn standard_to_microseconds_requires_calibration() {
        let t = StandardTimestamp {
            raw_value: 100_016,
            upper_word: 0x0001,
            lower_word: 0x86B0,
        };
        let ts = Timestamp::Standard(t);

        // Uncalibrated (and invalid rates) yield no microseconds.
        assert_eq!(ts.to_microseconds(None), None);
        assert_eq!(ts.to_microseconds(Some(0.0)), None);
        assert_eq!(ts.to_microseconds(Some(-1.0)), None);
        assert_eq!(ts.to_microseconds(Some(f64::NAN)), None);
        assert_eq!(ts.to_microseconds(Some(f64::INFINITY)), None);

        // 1 MHz: one tick == one microsecond.
        assert_eq!(ts.to_microseconds(Some(1_000_000.0)), Some(100_016));
        assert_eq!(t.to_microseconds(1_000_000.0), Some(100_016));
    }

    /// Requirements: L2-DEC-017
    #[test]
    fn standard_to_microseconds_rounds_half_away_from_zero() {
        // 3 ticks at 2 MHz = 1.5 µs → rounds up to 2 (half-away-from-zero).
        let t = StandardTimestamp {
            raw_value: 3,
            upper_word: 0,
            lower_word: 3,
        };
        assert_eq!(t.to_microseconds(2_000_000.0), Some(2));
        // 1 tick at 2 MHz = 0.5 µs → rounds up to 1.
        let one = StandardTimestamp {
            raw_value: 1,
            upper_word: 0,
            lower_word: 1,
        };
        assert_eq!(one.to_microseconds(2_000_000.0), Some(1));
        // Parity guard (L2-DEC-017): 1 tick at 2000000.0000000002 Hz is
        // x = 0.49999999999999994, one ULP below 0.5. `f64::round` gives 0;
        // `floor(x + 0.5)` gives 1 (the old C++ form, and Python's before it).
        assert_eq!(one.to_microseconds(2_000_000.000_000_000_2), Some(0));
        // Parity guard: from 2^52 to 2^53 doubles are spaced 1 apart, so
        // `x + 0.5` is a tie that rounds to even, and `floor(x + 0.5)` adds 1
        // to every odd count. 0xFFFFFFFF ticks at 0.9 Hz is the odd
        // 4_772_185_883_333_333 exactly.
        let max = StandardTimestamp {
            raw_value: 0xFFFF_FFFF,
            upper_word: 0xFFFF,
            lower_word: 0xFFFF,
        };
        assert_eq!(max.to_microseconds(0.9), Some(4_772_185_883_333_333));
    }

    /// Requirements: L3-RS-005
    #[test]
    fn data_words_inline_buffer() {
        let dw = DataWords::from_slice(&[1, 2, 3]);
        assert_eq!(dw.len(), 3);
        assert_eq!(dw.as_slice(), &[1, 2, 3]);
        assert!(!dw.is_empty());
        let empty = DataWords::new();
        assert!(empty.is_empty());
    }

    /// A tick rate that is valid but produces an unrepresentable result is
    /// declined rather than saturated.
    ///
    /// Reachable from ordinary input: `--standard-tick-rate-hz 1e-300` is
    /// finite and positive and passes the rate guard. `as u64` SATURATES, so
    /// the answer used to be `u64::MAX` — a fabricated timestamp that reads as
    /// real downstream. C++ had the same shape and it was undefined behaviour
    /// there; Python returned an integer neither of the others can hold. All
    /// three now decline.
    ///
    /// Requirements: L2-DEC-017
    #[test]
    fn standard_declines_unrepresentable_result() {
        let ts = StandardTimestamp {
            raw_value: 1_000_000,
            upper_word: 0x000F,
            lower_word: 0x4240,
        };
        assert_eq!(ts.to_microseconds(1e-300), None);
        assert_eq!(ts.to_microseconds(f64::MIN_POSITIVE), None);

        // The RATE predicate is unchanged: still "> 0", not "not near zero".
        // A tiny rate with nothing to scale still converts.
        let zero = StandardTimestamp {
            raw_value: 0,
            upper_word: 0,
            lower_word: 0,
        };
        assert_eq!(zero.to_microseconds(f64::MIN_POSITIVE), Some(0));
    }

    /// Requirements: L3-RS-005
    #[test]
    fn data_words_max_capacity() {
        let words: Vec<u16> = (0..u16::try_from(MAX_DATA_WORDS).expect("fits")).collect();
        let dw = DataWords::from_slice(&words);
        assert_eq!(dw.len(), MAX_DATA_WORDS);
    }

    /// An over-length slice is capped at `MAX_DATA_WORDS` rather than panicking
    /// (L1-ROB-001), matching `from_iter_capped`.
    /// Requirements: L3-RS-005, L1-ROB-001
    #[test]
    fn data_words_overflow_truncates() {
        let words: Vec<u16> = (0..(u16::try_from(MAX_DATA_WORDS).expect("fits") + 8)).collect();
        let dw = DataWords::from_slice(&words);
        assert_eq!(dw.len(), MAX_DATA_WORDS);
        assert_eq!(dw.as_slice(), &words[..MAX_DATA_WORDS]);
    }

    /// Requirements: L2-DEC-004
    #[test]
    fn command_word_predicates() {
        let bcast = CommandWord {
            rt: 31,
            direction: Direction::Receive,
            subaddress: 5,
            data_word_count: 1,
            raw: 0,
        };
        assert!(bcast.is_broadcast());
        assert!(!bcast.is_mode_code());

        let mode = CommandWord {
            rt: 1,
            direction: Direction::Transmit,
            subaddress: 0,
            data_word_count: 1,
            raw: 0,
        };
        assert!(!mode.is_broadcast());
        assert!(mode.is_mode_code());
    }

    /// Requirements: L2-ERR-003
    #[test]
    fn error_code_classification() {
        assert!(is_known_ddc_error_code(ERROR_MANCHESTER_PARITY));
        assert!(is_known_custom_error_code(ERROR_SPURIOUS_STANDALONE));
        assert!(is_known_error_code(ERROR_INVERTED_SYNC));
        assert!(!is_known_error_code(0xDEAD));
        assert_eq!(
            ddc_error_description(ERROR_NO_RESPONSE),
            "No Status Response or Too Few Data Words"
        );
        assert_eq!(ddc_error_description(0x9999), "");
    }

    /// Requirements: L2-DEC-002, L2-DEC-007
    #[test]
    fn timestamp_word_counts() {
        assert_eq!(timestamp_word_count(TimestampFormat::Irig), 3);
        assert_eq!(timestamp_word_count(TimestampFormat::Standard), 2);
    }

    /// Requirements: L2-MSG-003
    #[test]
    fn msg_label_and_delta_key() {
        let msg = make_msg(15, Direction::Receive, 11);
        assert_eq!(msg.msg_label(), "11R");
        assert_eq!(msg.delta_key(), "15:11R");

        let msg = make_msg(15, Direction::Transmit, 22);
        assert_eq!(msg.msg_label(), "22T");
    }

    /// The message type decides the label: a `SPURIOUS_DATA` record with Type
    /// Word bit 14 set reads `SPURIOUS`, not `ERROR` (docs/DATAFLOW.md row S7),
    /// while `is_error` still reports the raw bit.
    ///
    /// Requirements: L2-ERR-001, L2-ERR-006
    #[test]
    fn error_label_lets_the_message_type_decide() {
        let mut msg = make_msg(15, Direction::Receive, 11);
        assert_eq!(msg.error_label(), "");
        msg.type_word.error = true;
        assert_eq!(msg.error_label(), "ERROR");
        msg.message_format = MessageFormat::SpuriousData;
        msg.command_word = None;
        assert!(msg.is_error());
        assert_eq!(msg.error_label(), "SPURIOUS");
        msg.type_word.error = false;
        assert_eq!(msg.error_label(), "SPURIOUS");
    }

    /// Requirements: L2-MSG-003
    #[test]
    fn rt_and_subaddress_shortcuts() {
        let msg = make_msg(15, Direction::Receive, 11);
        assert_eq!(msg.rt(), Some(15));
        assert_eq!(msg.subaddress(), Some(11));

        // SPURIOUS_DATA carries no Command Word, so both shortcuts are None.
        let mut spurious = make_msg(15, Direction::Receive, 11);
        spurious.command_word = None;
        assert_eq!(spurious.rt(), None);
        assert_eq!(spurious.subaddress(), None);
    }

    fn make_msg(rt: u8, dir: Direction, sa: u8) -> MieMessage {
        MieMessage {
            timestamp: Timestamp::Standard(StandardTimestamp {
                raw_value: 0,
                upper_word: 0,
                lower_word: 0,
            }),
            type_word: TypeWord {
                message_type: 0x02,
                bus: Bus::A,
                word_count: 5,
                error: false,
                raw: 0,
            },
            message_format: MessageFormat::Receive,
            command_word: Some(CommandWord {
                rt,
                direction: dir,
                subaddress: sa,
                data_word_count: 1,
                raw: 0,
            }),
            command_word_2: None,
            status_word: None,
            status_word_2: None,
            data_words: DataWords::new(),
            error_word: None,
            delta: Some(0.0),
            file_offset: 0,
            mux: None,
        }
    }

    // -- Calendar resolution and the three renderings (L2-WRT-025/026) ------

    /// Requirements: L2-WRT-025
    #[test]
    fn leap_year_rule_is_proleptic_gregorian() {
        // Divisible by 4 -> leap, except centuries, which need 400.
        for year in [1996u16, 2000, 2004, 2020, 2024, 2028, 1600] {
            assert!(is_leap_year(year), "{year} should be a leap year");
        }
        for year in [1900u16, 1999, 2001, 2025, 2026, 2027, 2100, 2200, 2300] {
            assert!(!is_leap_year(year), "{year} should be a common year");
        }
    }

    /// The same day-of-year lands on different calendar days either side of a
    /// leap day. This is the whole reason a calendar rendering needs a year,
    /// and the example is the one from the format documentation.
    ///
    /// Requirements: L2-WRT-025, L2-WRT-026
    #[test]
    fn day_of_year_192_shifts_by_one_across_leap_and_common_years() {
        assert_eq!(day_of_year_to_month_day(2024, 192), Some((7, 10)));
        assert_eq!(day_of_year_to_month_day(2028, 192), Some((7, 10)));
        assert_eq!(day_of_year_to_month_day(2025, 192), Some((7, 11)));
        assert_eq!(day_of_year_to_month_day(2026, 192), Some((7, 11)));
    }

    /// Requirements: L2-WRT-025
    #[test]
    fn day_of_year_resolves_at_month_boundaries() {
        // Common year: day 59 is Feb 28, 60 is Mar 1.
        assert_eq!(day_of_year_to_month_day(2026, 1), Some((1, 1)));
        assert_eq!(day_of_year_to_month_day(2026, 31), Some((1, 31)));
        assert_eq!(day_of_year_to_month_day(2026, 32), Some((2, 1)));
        assert_eq!(day_of_year_to_month_day(2026, 59), Some((2, 28)));
        assert_eq!(day_of_year_to_month_day(2026, 60), Some((3, 1)));
        assert_eq!(day_of_year_to_month_day(2026, 365), Some((12, 31)));

        // Leap year: 60 is the leap day itself, and everything after shifts.
        assert_eq!(day_of_year_to_month_day(2024, 59), Some((2, 28)));
        assert_eq!(day_of_year_to_month_day(2024, 60), Some((2, 29)));
        assert_eq!(day_of_year_to_month_day(2024, 61), Some((3, 1)));
        assert_eq!(day_of_year_to_month_day(2024, 366), Some((12, 31)));
    }

    /// Day 366 exists only in a leap year. It must not silently roll into the
    /// next January (L2-WRT-026 clause 3).
    ///
    /// Requirements: L2-WRT-026
    #[test]
    fn day_366_has_no_date_in_a_common_year() {
        assert_eq!(day_of_year_to_month_day(2026, 366), None);
        assert_eq!(day_of_year_to_month_day(1900, 366), None);
        assert_eq!(day_of_year_to_month_day(2024, 366), Some((12, 31)));

        // Out of range in either direction, in either kind of year.
        for year in [2024u16, 2026] {
            assert_eq!(day_of_year_to_month_day(year, 0), None);
            assert_eq!(day_of_year_to_month_day(year, 367), None);
            assert_eq!(day_of_year_to_month_day(year, u16::MAX), None);
        }
    }

    /// Every day of both year kinds resolves, in order, with each month
    /// receiving exactly its own length. Cheap enough to run exhaustively, and
    /// it catches an off-by-one in the accumulator that spot checks would miss.
    ///
    /// Requirements: L2-WRT-025
    #[test]
    fn every_day_of_year_resolves_in_ascending_order() {
        for year in [2024u16, 2026] {
            let leap = is_leap_year(year);
            let length = if leap { 366 } else { 365 };
            let mut seen_per_month = [0u16; 13];
            let mut previous = (0u8, 0u8);

            for day in 1..=length {
                let resolved = day_of_year_to_month_day(year, day)
                    .unwrap_or_else(|| panic!("day {day} of {year} did not resolve"));
                assert!(
                    resolved > previous,
                    "day {day} of {year} resolved to {resolved:?}, not after {previous:?}"
                );
                previous = resolved;
                seen_per_month[resolved.0 as usize] += 1;
            }

            for (index, &base) in COMMON_YEAR_MONTH_LENGTHS.iter().enumerate() {
                let expected = if index == 1 && leap { base + 1 } else { base };
                assert_eq!(
                    seen_per_month[index + 1],
                    expected,
                    "month {} of {year} had the wrong number of days",
                    index + 1
                );
            }
        }
    }

    /// Requirements: L2-WRT-025
    #[test]
    fn utc_offset_renders_z_at_zero_and_signed_hhmm_otherwise() {
        assert_eq!(format_utc_offset(0), "Z");
        assert_eq!(format_utc_offset(-300), "-05:00");
        assert_eq!(format_utc_offset(330), "+05:30");
        assert_eq!(format_utc_offset(60), "+01:00");
        assert_eq!(format_utc_offset(-1), "-00:01");
        assert_eq!(format_utc_offset(1439), "+23:59");
    }

    /// Requirements: L2-WRT-026, L2-CFG-012
    #[test]
    fn time_render_new_checks_year_and_offset_ranges() {
        let iso = OutputTimeFormat::Iso;
        let ok = TimeRender::new(iso, Some(2026), -300).unwrap();
        assert_eq!((ok.year, ok.utc_offset_minutes), (Some(2026), -300));
        assert_eq!(TimeRender::new(iso, None, 0).unwrap().year, None);

        // Both ends of each range are inside it.
        for year in [1, 9999] {
            assert_eq!(check_year(year), Ok(u16::try_from(year).unwrap()));
        }
        for minutes in [-1439, 0, 1439] {
            assert_eq!(
                check_utc_offset(minutes),
                Ok(i16::try_from(minutes).unwrap())
            );
        }

        for year in [0, 10_000, -1, i64::from(u16::MAX) + 1, i64::MIN, i64::MAX] {
            assert_eq!(check_year(year), Err(TimeRenderError::YearOutOfRange(year)));
            assert!(TimeRender::new(iso, Some(year), 0).is_err(), "{year}");
        }
        // i16::MIN is the case `abs()` got wrong: it has no positive i16, so a
        // release build wrapped it to itself and it compared below 1439.
        for minutes in [-1440, 1440, 5000, i64::from(i16::MIN), 40_000, i64::MIN] {
            assert_eq!(
                check_utc_offset(minutes),
                Err(TimeRenderError::UtcOffsetOutOfRange(minutes))
            );
            assert!(
                TimeRender::new(iso, Some(2026), minutes).is_err(),
                "{minutes}"
            );
        }

        // The wording the Python bindings raise as ValueError.
        assert_eq!(
            TimeRenderError::YearOutOfRange(0).to_string(),
            "year must be in [1, 9999], got 0"
        );
        assert_eq!(
            TimeRenderError::UtcOffsetOutOfRange(-32768).to_string(),
            "utc_offset_minutes must be in [-1439, 1439], got -32768"
        );
    }

    fn sample_irig() -> IrigTimestamp {
        IrigTimestamp {
            day: 192,
            hour: 15,
            minute: 54,
            second: 50,
            microsecond: 456_225,
            freerun: false,
        }
    }

    /// The default rendering must stay byte-identical to what `format()`
    /// produced before rendering became selectable -- that is what keeps a
    /// no-flag decode vendor-diffable (L1-OUT-004).
    ///
    /// Requirements: L2-WRT-011, L2-WRT-025
    #[test]
    fn doy_rendering_is_unaffected_by_year_or_offset() {
        let ts = sample_irig();
        let expected = "192:15:54:50.456225";
        assert_eq!(ts.format(), expected);
        assert_eq!(ts.format_with(TimeRender::doy()).unwrap(), expected);

        // A year and an offset are inert under `doy` (L2-WRT-026 clause 5).
        let noisy = TimeRender {
            format: OutputTimeFormat::Doy,
            year: Some(2024),
            utc_offset_minutes: -300,
        };
        assert_eq!(ts.format_with(noisy).unwrap(), expected);
    }

    /// Requirements: L2-WRT-025
    #[test]
    fn iso_and_dom_render_the_resolved_calendar_date() {
        let ts = sample_irig();

        let iso_leap = TimeRender {
            format: OutputTimeFormat::Iso,
            year: Some(2024),
            utc_offset_minutes: 0,
        };
        assert_eq!(
            ts.format_with(iso_leap).unwrap(),
            "2024-07-10T15:54:50.456225Z"
        );

        let iso_common = TimeRender {
            format: OutputTimeFormat::Iso,
            year: Some(2026),
            utc_offset_minutes: 0,
        };
        assert_eq!(
            ts.format_with(iso_common).unwrap(),
            "2026-07-11T15:54:50.456225Z"
        );

        let iso_offset = TimeRender {
            format: OutputTimeFormat::Iso,
            year: Some(2026),
            utc_offset_minutes: -300,
        };
        assert_eq!(
            ts.format_with(iso_offset).unwrap(),
            "2026-07-11T15:54:50.456225-05:00"
        );

        let dom_leap = TimeRender {
            format: OutputTimeFormat::Dom,
            year: Some(2024),
            utc_offset_minutes: 0,
        };
        assert_eq!(ts.format_with(dom_leap).unwrap(), "10:15:54:50.456225");

        let dom_common = TimeRender {
            format: OutputTimeFormat::Dom,
            year: Some(2026),
            utc_offset_minutes: 0,
        };
        assert_eq!(ts.format_with(dom_common).unwrap(), "11:15:54:50.456225");
    }

    /// Every rendering emits exactly six microsecond digits; the wider ISO cell
    /// does not relax L2-DEC-014.
    ///
    /// Requirements: L2-DEC-014, L2-WRT-025
    #[test]
    fn all_renderings_emit_exactly_six_microsecond_digits() {
        for micro in [0u32, 1, 999_999, 1_000_000, 1_234_567] {
            let ts = IrigTimestamp {
                microsecond: micro,
                ..sample_irig()
            };
            for format in [
                OutputTimeFormat::Doy,
                OutputTimeFormat::Iso,
                OutputTimeFormat::Dom,
            ] {
                let rendered = ts
                    .format_with(TimeRender {
                        format,
                        year: Some(2026),
                        utc_offset_minutes: 0,
                    })
                    .unwrap();
                let fraction = rendered.split('.').next_back().unwrap();
                // ISO appends a zone designator after the fraction.
                let digits = fraction.chars().take_while(char::is_ascii_digit).count();
                assert_eq!(digits, 6, "{format:?} rendered {rendered:?}");
            }
        }
    }

    /// Requirements: L2-WRT-026
    #[test]
    fn calendar_renderings_refuse_rather_than_approximate() {
        let leap_day = IrigTimestamp {
            day: 366,
            ..sample_irig()
        };

        for format in [OutputTimeFormat::Iso, OutputTimeFormat::Dom] {
            // No year resolved at all.
            assert_eq!(
                sample_irig().format_with(TimeRender {
                    format,
                    year: None,
                    utc_offset_minutes: 0,
                }),
                Err(CalendarError::MissingYear)
            );

            // Day 366 against a common year has no date to render.
            assert_eq!(
                leap_day.format_with(TimeRender {
                    format,
                    year: Some(2026),
                    utc_offset_minutes: 0,
                }),
                Err(CalendarError::NoSuchDay {
                    day: 366,
                    year: 2026
                })
            );

            // The same record renders fine once the year actually has that day.
            assert!(
                leap_day
                    .format_with(TimeRender {
                        format,
                        year: Some(2024),
                        utc_offset_minutes: 0,
                    })
                    .is_ok()
            );
        }

        // `doy` never needs a calendar and so never refuses.
        assert!(leap_day.format_with(TimeRender::doy()).is_ok());
    }

    /// A Standard counter renders as raw hex under `doy` and REFUSES the
    /// calendar renderings -- it has no epoch, so no year can place it on a
    /// calendar, and emitting hex into a column the operator asked to be
    /// ISO-8601 would be its own kind of lie (L2-WRT-026 clause 2).
    ///
    /// Requirements: L2-WRT-025, L2-WRT-026
    #[test]
    fn standard_counter_refuses_calendar_renderings() {
        let ts = Timestamp::Standard(StandardTimestamp {
            raw_value: 100_000,
            upper_word: 0x0001,
            lower_word: 0x86A0,
        });

        assert_eq!(ts.format_with(TimeRender::doy()).unwrap(), "0x000186A0");

        for format in [OutputTimeFormat::Iso, OutputTimeFormat::Dom] {
            assert_eq!(
                ts.format_with(TimeRender {
                    format,
                    year: Some(2026),
                    utc_offset_minutes: 0,
                }),
                Err(CalendarError::NotCalendarLocked),
                "{format:?} must refuse a free-running counter"
            );
        }
    }

    /// A freerun IRIG record has calendar-shaped fields that are not
    /// calendar-anchored, which makes it the most dangerous case of the three:
    /// it would render as a perfectly ordinary date (L2-WRT-026 clause 2).
    ///
    /// Requirements: L2-WRT-026
    #[test]
    fn freerun_irig_refuses_calendar_renderings() {
        let freerun = IrigTimestamp {
            freerun: true,
            ..sample_irig()
        };

        // `doy` is unaffected -- it prints the fields without interpreting them.
        assert_eq!(
            freerun.format_with(TimeRender::doy()).unwrap(),
            "192:15:54:50.456225"
        );

        for format in [OutputTimeFormat::Iso, OutputTimeFormat::Dom] {
            assert_eq!(
                freerun.format_with(TimeRender {
                    format,
                    year: Some(2026),
                    utc_offset_minutes: 0,
                }),
                Err(CalendarError::FreerunNotAnchored),
                "{format:?} must refuse a freerun timestamp"
            );
            // The same instant, calendar-locked, renders fine -- so the refusal
            // is about the freerun bit and nothing else.
            assert!(
                sample_irig()
                    .format_with(TimeRender {
                        format,
                        year: Some(2026),
                        utc_offset_minutes: 0,
                    })
                    .is_ok()
            );
        }
    }

    /// Requirements: L2-CLI-018, L2-CFG-012
    #[test]
    fn output_time_format_names_parse_case_insensitively() {
        for (name, expected) in [
            ("doy", OutputTimeFormat::Doy),
            ("DOY", OutputTimeFormat::Doy),
            ("Iso", OutputTimeFormat::Iso),
            ("ISO", OutputTimeFormat::Iso),
            ("dom", OutputTimeFormat::Dom),
            ("DoM", OutputTimeFormat::Dom),
        ] {
            assert_eq!(OutputTimeFormat::from_name_ci(name), Some(expected));
        }
        for name in ["", "day", "iso8601", "doy ", "elapsed"] {
            assert_eq!(OutputTimeFormat::from_name_ci(name), None);
        }

        assert_eq!(OutputTimeFormat::default(), OutputTimeFormat::Doy);
        assert!(!OutputTimeFormat::Doy.needs_calendar());
        assert!(OutputTimeFormat::Iso.needs_calendar());
        assert!(OutputTimeFormat::Dom.needs_calendar());
    }
}
