//! Tiny logger. No facade trait, no external crate.
//!
//! A single global level controls emission across all modules. The macros
//! `debug!`, `info!`, `warn!`, `error!` defined in this crate format with
//! `format!` only when the level passes the filter, so they're cheap when
//! disabled.
//!
//! A line that passes the filter goes to stderr, unless an embedder has
//! installed a [`LogSink`] with [`set_sink`] -- the Python binding routes lines
//! into Python's `logging` that way. [`with_stderr`] forces stderr on the
//! calling thread regardless, which is how the CLI keeps its output identical
//! to the binary's under any embedder.

use std::cell::Cell;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{PoisonError, RwLock};

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
#[inline]
pub fn emit(level: Level, module: &str, args: std::fmt::Arguments<'_>) {
    // The level check stays first and alone, and `emit` is inlined so it sits
    // at each call site: a filtered-out line -- including the per-record DEBUG
    // lines on the decode path -- costs one relaxed load and a branch. The
    // delivery below is `#[cold]` so its code stays out of that path.
    if !enabled(level) {
        return;
    }
    deliver(level, module, args);
}

/// A destination for log lines, installed with [`set_sink`].
///
/// Called with the line's level, its module path (`aero1553::reader`) and its
/// message text, only for lines that passed the level filter. A plain function
/// pointer rather than a boxed closure: it holds no state, so installing one
/// allocates nothing, and the embedder keeps any state it needs on its own side.
pub type LogSink = fn(Level, &str, &str);

static SINK: RwLock<Option<LogSink>> = RwLock::new(None);

thread_local! {
    /// Set by [`with_stderr`] for the duration of its closure.
    static FORCE_STDERR: Cell<bool> = const { Cell::new(false) };
}

/// Route log lines to `sink` instead of stderr, or back to stderr with `None`.
///
/// Process-wide. Returns the sink it replaced, so a caller can restore it.
/// The level filter is unaffected: set it with [`set_level`] as before.
pub fn set_sink(sink: Option<LogSink>) -> Option<LogSink> {
    let mut slot = SINK.write().unwrap_or_else(PoisonError::into_inner);
    std::mem::replace(&mut *slot, sink)
}

/// Run `f` with this thread's log lines going to stderr, whatever sink is
/// installed.
///
/// [`crate::cli::run_to_code`] runs inside this, so the command line writes
/// its diagnostics exactly as the binary does even when an embedder has routed
/// the library's log lines elsewhere. Thread-local, so a CLI run on one thread
/// does not divert log lines another thread's library code emits meanwhile.
/// The previous setting is restored on return and on unwind.
pub fn with_stderr<R>(f: impl FnOnce() -> R) -> R {
    struct Restore(bool);
    impl Drop for Restore {
        fn drop(&mut self) {
            FORCE_STDERR.with(|flag| flag.set(self.0));
        }
    }
    let _restore = Restore(FORCE_STDERR.with(|flag| flag.replace(true)));
    f()
}

/// Write one line that has already passed the level filter.
#[cold]
#[inline(never)]
fn deliver(level: Level, module: &str, args: std::fmt::Arguments<'_>) {
    if !FORCE_STDERR.with(Cell::get) {
        let sink = *SINK.read().unwrap_or_else(PoisonError::into_inner);
        if let Some(sink) = sink {
            sink(level, module, &args.to_string());
            return;
        }
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

    // The sink is process-wide and the test harness runs tests in parallel, so
    // the tests that install one take this lock. Other tests may still emit
    // while a sink is installed; the assertions below look only for their own
    // marker text, so a stray line cannot make them pass or fail.
    static SINK_TESTS: std::sync::Mutex<()> = std::sync::Mutex::new(());
    static CAPTURED: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());

    fn capture(level: Level, module: &str, message: &str) {
        CAPTURED
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(format!("{}|{module}|{message}", level.label()));
    }

    fn captured_with(marker: &str) -> Vec<String> {
        CAPTURED
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .filter(|line| line.contains(marker))
            .cloned()
            .collect()
    }

    // `deliver` is exercised directly rather than through `emit`: the level is
    // global too, and other tests set it, so an `emit`-based assertion would
    // depend on test scheduling.

    /// An installed sink receives the level, the module path and the message,
    /// and `set_sink` hands back what it replaced so it can be restored.
    /// Requirements: L1-LOG-001
    #[test]
    fn an_installed_sink_receives_each_line() {
        let _serial = SINK_TESTS.lock().unwrap_or_else(PoisonError::into_inner);
        let previous = set_sink(Some(capture));
        deliver(
            Level::Warn,
            "aero1553::reader",
            format_args!("marker-{}", 17),
        );
        let restored = set_sink(previous);

        assert!(restored.is_some(), "set_sink returns the sink it replaced");
        assert_eq!(
            captured_with("marker-17"),
            vec!["WARN|aero1553::reader|marker-17".to_string()]
        );
    }

    /// `with_stderr` diverts this thread past the sink for the closure only,
    /// nests, and restores the previous setting on unwind as well as return.
    /// Requirements: L1-LOG-001
    #[test]
    fn with_stderr_bypasses_the_sink_and_restores() {
        let _serial = SINK_TESTS.lock().unwrap_or_else(PoisonError::into_inner);
        let previous = set_sink(Some(capture));

        with_stderr(|| {
            deliver(Level::Error, "aero1553::cli", format_args!("inside-first"));
            with_stderr(|| deliver(Level::Error, "aero1553::cli", format_args!("inside-nested")));
            // Still bypassing after the nested call returned.
            deliver(
                Level::Error,
                "aero1553::cli",
                format_args!("inside-after-nested"),
            );
        });
        deliver(
            Level::Error,
            "aero1553::cli",
            format_args!("outside-return"),
        );

        let unwound = std::panic::catch_unwind(|| with_stderr(|| panic!("unwind")));
        assert!(unwound.is_err());
        deliver(
            Level::Error,
            "aero1553::cli",
            format_args!("outside-unwind"),
        );

        set_sink(previous);
        assert!(captured_with("inside-first").is_empty());
        assert!(captured_with("inside-nested").is_empty());
        assert!(captured_with("inside-after-nested").is_empty());
        assert_eq!(
            captured_with("outside-").len(),
            2,
            "restored on return and on unwind"
        );
    }
}
