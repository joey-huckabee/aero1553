//! Tiny stderr logger. ~50 lines, no facade trait, no external crate.
//!
//! A single global level controls emission across all modules. The macros
//! `debug!`, `info!`, `warn!`, `error!` defined in this crate format with
//! `format!` only when the level passes the filter, so they're cheap when
//! disabled.

use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};

/// Log severity. Higher numeric value = more important.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[repr(u8)]
pub enum Level {
    Debug = 0,
    Info = 1,
    Warn = 2,
    Error = 3,
    Off = 4,
}

impl Level {
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        match name.to_ascii_uppercase().as_str() {
            "DEBUG" => Some(Self::Debug),
            "INFO" => Some(Self::Info),
            "WARNING" | "WARN" => Some(Self::Warn),
            "ERROR" => Some(Self::Error),
            "CRITICAL" | "OFF" => Some(Self::Off),
            _ => None,
        }
    }

    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Debug => "DEBUG",
            Self::Info => "INFO",
            Self::Warn => "WARN",
            Self::Error => "ERROR",
            Self::Off => "OFF",
        }
    }
}

/// Default to WARN, matching the Python CLI default.
static LEVEL: AtomicU8 = AtomicU8::new(Level::Warn as u8);

pub fn set_level(level: Level) {
    LEVEL.store(level as u8, Ordering::Relaxed);
}

#[inline]
pub fn current_level() -> Level {
    match LEVEL.load(Ordering::Relaxed) {
        0 => Level::Debug,
        1 => Level::Info,
        2 => Level::Warn,
        3 => Level::Error,
        _ => Level::Off,
    }
}

#[inline]
pub fn enabled(level: Level) -> bool {
    (level as u8) >= LEVEL.load(Ordering::Relaxed)
}

/// Whether the one-time IRIG day-of-year advisory is emitted at all. Enabled by
/// default; `--no-irig-day-advisory` / `[logging] irig_day_advisory = false`
/// turns it off.
static IRIG_DAY_ADVISORY: AtomicBool = AtomicBool::new(true);

/// Enable or disable the IRIG day-of-year advisory (L2-LOG-001).
///
/// This lives beside the global level rather than in [`crate::ReaderOptions`]
/// because it is a diagnostics switch, not a decode parameter: it is applied
/// where `--log-level` is applied, so it covers `decode`, `count` and `dump`
/// uniformly without each command wiring it through.
pub fn set_irig_day_advisory(enabled: bool) {
    IRIG_DAY_ADVISORY.store(enabled, Ordering::Relaxed);
}

/// Whether the IRIG day-of-year advisory may be emitted. The level filter still
/// applies on top of this: the advisory is logged at INFO, so at the default
/// WARNING level it stays silent even when this returns `true`.
#[inline]
#[must_use]
pub fn irig_day_advisory() -> bool {
    IRIG_DAY_ADVISORY.load(Ordering::Relaxed)
}

/// Internal write — used by the `log_*!` macros. `args` is already-formatted
/// message text.
///
/// `pub` only because `#[macro_export]` macros expand at their CALL site, which
/// may be in another crate: `$crate::log::emit` has to resolve there. That is
/// what `#[doc(hidden)]` says — the item is reachable but not API — and it is
/// the right marker for it. The former `_emit` spelling used an underscore to
/// mean the same thing, which contradicts the convention that `_name` is
/// *unused*: clippy's `used_underscore_items` is correct to object.
#[doc(hidden)]
pub fn emit(level: Level, module: &str, args: std::fmt::Arguments<'_>) {
    if !enabled(level) {
        return;
    }
    let _ = std::io::Write::write_fmt(
        &mut std::io::stderr().lock(),
        format_args!("{} [{}] {}\n", level.label(), module, args),
    );
}

/// The former name of [`emit`], kept so `aero1553::log::_emit` still
/// resolves.
///
/// Renaming a `pub` item would be an API removal, and `cargo-semver-checks`
/// exclusions are per-LINT rather than per-item — allowing `function_missing`
/// crate-wide to rename one internal helper would disable a check worth having.
/// A re-export costs nothing and keeps the gate honest.
#[doc(hidden)]
pub use self::emit as _emit;

#[macro_export]
macro_rules! log_debug {
    ($($arg:tt)*) => {
        $crate::log::emit($crate::log::Level::Debug, module_path!(), format_args!($($arg)*))
    };
}
#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => {
        $crate::log::emit($crate::log::Level::Info, module_path!(), format_args!($($arg)*))
    };
}
#[macro_export]
macro_rules! log_warn {
    ($($arg:tt)*) => {
        $crate::log::emit($crate::log::Level::Warn, module_path!(), format_args!($($arg)*))
    };
}
#[macro_export]
macro_rules! log_error {
    ($($arg:tt)*) => {
        $crate::log::emit($crate::log::Level::Error, module_path!(), format_args!($($arg)*))
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Requirements: L2-CLI-004
    #[test]
    fn level_parse() {
        assert_eq!(Level::parse("DEBUG"), Some(Level::Debug));
        assert_eq!(Level::parse("warning"), Some(Level::Warn));
        assert_eq!(Level::parse("warn"), Some(Level::Warn));
        // CRITICAL and OFF both map to Off (silence all output).
        assert_eq!(Level::parse("CRITICAL"), Some(Level::Off));
        assert_eq!(Level::parse("OFF"), Some(Level::Off));
        assert_eq!(Level::parse("off"), Some(Level::Off));
        assert_eq!(Level::parse("nope"), None);
    }

    /// Requirements: L1-LOG-001
    #[test]
    fn level_ordering() {
        assert!(Level::Debug < Level::Info);
        assert!(Level::Warn < Level::Error);
    }
}
