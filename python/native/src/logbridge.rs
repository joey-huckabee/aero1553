// SPDX-License-Identifier: Apache-2.0

//! The decoder's log lines -> Python's `logging`.
//!
//! The Rust logger's module path is the Python logger name with `::` for `.`
//! (`aero1553::reader` -> `aero1553.reader`), so a library user's handlers,
//! levels and `caplog` see what they always saw. The command line is not
//! routed: `cli::run_to_code` pins its thread to stderr, so it writes exactly
//! as the binary does.

use aero1553::log::{self, Level};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;

static LOGGING: PyOnceLock<Py<PyModule>> = PyOnceLock::new();

fn logging(py: Python<'_>) -> PyResult<&Bound<'_, PyModule>> {
    Ok(LOGGING
        .get_or_try_init(py, || py.import("logging").map(Bound::unbind))?
        .bind(py))
}

/// Route the decoder's log lines into Python's `logging` from now on.
pub fn install() {
    log::set_sink(Some(sink));
}

fn sink(level: Level, module: &str, message: &str) {
    Python::attach(|py| {
        // A logging failure has nowhere better to go than Python's own
        // unraisable hook: raising it would abort a decode over a log line.
        if let Err(err) = forward(py, level, module, message) {
            err.write_unraisable(py, None);
        }
    });
}

fn forward(py: Python<'_>, level: Level, module: &str, message: &str) -> PyResult<()> {
    let name = module.replace("::", ".");
    let logger = logging(py)?.call_method1("getLogger", (name,))?;
    // Passed as the message with no arguments, so a `%` in it is never read
    // as a format directive.
    logger.call_method1("log", (python_level(level), message))?;
    Ok(())
}

fn python_level(level: Level) -> i32 {
    match level {
        Level::Debug => 10,
        Level::Info => 20,
        Level::Warn => 30,
        Level::Error | Level::Off => 40,
    }
}

/// The most verbose effective level among the `aero1553` logger and every
/// existing `aero1553.*` logger.
///
/// The decoder has one global level, while Python levels are per logger: an
/// application (or pytest's `caplog.at_level(..., logger="aero1553.reader")`)
/// may turn up one module alone. Taking the most verbose means the decoder
/// produces every line SOME aero1553 logger wants; each logger's own level and
/// handlers still decide what is shown, so the cost of erring verbose is only
/// the formatting of a line Python then drops.
fn most_verbose_level(py: Python<'_>) -> PyResult<i32> {
    let logging = logging(py)?;
    let mut level: i32 = logging
        .call_method1("getLogger", ("aero1553",))?
        .call_method0("getEffectiveLevel")?
        .extract()?;
    let logger_class = logging.getattr("Logger")?;
    let registry = logging
        .getattr("root")?
        .getattr("manager")?
        .getattr("loggerDict")?
        .call_method0("copy")?;
    for (name, logger) in registry.cast_into::<pyo3::types::PyDict>()?.iter() {
        let name: String = name.extract()?;
        // A PlaceHolder (an intermediate name nobody has asked for) has no level.
        if name.starts_with("aero1553.") && logger.is_instance(&logger_class)? {
            let own: i32 = logger.call_method0("getEffectiveLevel")?.extract()?;
            level = level.min(own);
        }
    }
    Ok(level)
}

/// Set the decoder's level from the Python loggers (see
/// [`most_verbose_level`]), and its day-of-year advisory switch from
/// `aero1553.logger`.
///
/// Called where a reader is created and where iteration starts. The decoder
/// formats a line only when its own level lets it through, so matching that
/// level to Python's means a line no Python logger would take is never built.
pub fn sync_level(py: Python<'_>) -> PyResult<()> {
    let effective = most_verbose_level(py)?;
    log::set_level(match effective {
        i32::MIN..=10 => Level::Debug,
        11..=20 => Level::Info,
        21..=30 => Level::Warn,
        31..=40 => Level::Error,
        _ => Level::Off,
    });
    let advisory: bool = py
        .import("aero1553.logger")?
        .call_method0("irig_day_advisory")?
        .extract()?;
    log::set_irig_day_advisory(advisory);
    Ok(())
}
