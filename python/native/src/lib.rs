// SPDX-License-Identifier: Apache-2.0

//! `aero1553._native`: the compiled half of the `aero1553` Python package.
//!
//! The Python modules under `python/src/aero1553/` present the package's
//! public API; this extension is where the work happens. It is a thin layer
//! over the `aero1553` Rust crate and holds no decoding logic of its own.

use std::io::Write;

use pyo3::prelude::*;

mod config;
mod dump;
mod enums;
mod errors;
mod logbridge;
mod merge;
mod models;
mod reader;
mod stages;
mod stream;
mod table;
mod writer;

/// Run the `aero1553` command line and return its exit status.
///
/// `argv[0]` is the program name, as in `sys.argv`. The CLI writes straight to
/// the process's stdout and stderr file descriptors -- not to Python's
/// `sys.stdout` -- so the caller flushes Python's streams first.
///
/// The GIL is released for the whole run: a decode can take seconds, and
/// holding the interpreter lock that long would stall every other Python
/// thread for no reason, since nothing here touches a Python object.
#[pyfunction]
fn run_cli(py: Python<'_>, argv: Vec<std::ffi::OsString>) -> u8 {
    // OS strings, not `String`s: `sys.argv` carries a non-UTF-8 file name as a
    // surrogate-escaped `str`, which a `String` cannot hold -- the call raised
    // `UnicodeEncodeError` before the CLI ran (L2-CLI-021).
    py.detach(|| {
        let code = aero1553::cli::run_os_to_code(argv);
        // Rust's stdout is line-buffered: a `print!` without a newline (the
        // help text's last line, a partial CSV row) can still be sitting in
        // its buffer. A standalone binary flushes at exit; an embedded call
        // returns to Python, which may read the descriptor straight away.
        // A failed flush has nowhere better to be reported -- the CLI has
        // already chosen its status.
        let _ = std::io::stdout().flush();
        let _ = std::io::stderr().flush();
        code
    })
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add(
        "DEFAULT_DETECT_RECORDS",
        aero1553::decode::DEFAULT_DETECT_RECORDS,
    )?;
    m.add(
        "DEFAULT_LOOKAHEAD_RECORDS",
        aero1553::sync::DEFAULT_LOOKAHEAD_RECORDS,
    )?;
    m.add("DEFAULT_MUX_ENABLED", aero1553::decode::DEFAULT_MUX_ENABLED)?;
    m.add(
        "DEFAULT_MUX_DELIMITER",
        aero1553::decode::DEFAULT_MUX_DELIMITER,
    )?;
    m.add("DEFAULT_MUX_FIELD", aero1553::decode::DEFAULT_MUX_FIELD)?;
    // A value read from the core crate, so importing the module proves the
    // extension links the decoder and not just PyO3.
    m.add(
        "TERMINATOR_TYPE_WORD",
        aero1553::decode::TERMINATOR_TYPE_WORD,
    )?;
    m.add_function(wrap_pyfunction!(run_cli, m)?)?;
    m.add_class::<models::PyTypeWord>()?;
    m.add_class::<models::PyCommandWord>()?;
    m.add_class::<models::PyIrigTimestamp>()?;
    m.add_class::<models::PyStandardTimestamp>()?;
    m.add_class::<models::PyMieMessage>()?;
    m.add_class::<reader::PyReader>()?;
    m.add_class::<stream::PyRecordIterator>()?;
    m.add_function(wrap_pyfunction!(stages::apply_filters, m)?)?;
    m.add_function(wrap_pyfunction!(stages::order_rows, m)?)?;
    m.add_function(wrap_pyfunction!(writer::write_csv, m)?)?;
    m.add_function(wrap_pyfunction!(writer::write_csv_split, m)?)?;
    m.add_function(wrap_pyfunction!(writer::csv_header, m)?)?;
    m.add_function(wrap_pyfunction!(writer::message_to_row, m)?)?;
    m.add_function(wrap_pyfunction!(writer::commit_targets, m)?)?;
    m.add_function(wrap_pyfunction!(writer::paths_refer_to_same_file, m)?)?;
    m.add_function(wrap_pyfunction!(merge::merge_readers, m)?)?;
    m.add_function(wrap_pyfunction!(merge::read_manifest, m)?)?;
    m.add_function(wrap_pyfunction!(merge::glob_match, m)?)?;
    m.add_function(wrap_pyfunction!(merge::expand_glob, m)?)?;
    m.add_function(wrap_pyfunction!(merge::delta_key, m)?)?;
    m.add_function(wrap_pyfunction!(table::columns, m)?)?;
    m.add_function(wrap_pyfunction!(table::table_fields, m)?)?;
    m.add_function(wrap_pyfunction!(dump::hex_dump_raw, m)?)?;
    m.add_function(wrap_pyfunction!(dump::hex_dump_records, m)?)?;
    m.add_function(wrap_pyfunction!(config::load_config, m)?)?;
    m.add_function(wrap_pyfunction!(config::parse_utc_offset, m)?)?;
    m.add_class::<merge::PyDeltaTracker>()?;
    // From here on the library's log lines reach Python's `logging`; the CLI
    // keeps writing to stderr (see `logbridge`).
    logbridge::install();
    Ok(())
}
