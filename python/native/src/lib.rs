// SPDX-License-Identifier: Apache-2.0

//! `aero1553._native`: the compiled half of the `aero1553` Python package.
//!
//! The Python modules under `python/src/aero1553/` present the package's
//! public API; this extension is where the work happens. It is a thin layer
//! over the `aero1553` Rust crate and holds no decoding logic of its own.

use std::io::Write;

use pyo3::prelude::*;

mod enums;
mod errors;
mod logbridge;
mod models;
mod reader;
mod stages;
mod stream;

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
fn run_cli(py: Python<'_>, argv: Vec<String>) -> u8 {
    py.detach(|| {
        let code = aero1553::cli::run_to_code(argv);
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
    // From here on the library's log lines reach Python's `logging`; the CLI
    // keeps writing to stderr (see `logbridge`).
    logbridge::install();
    Ok(())
}
