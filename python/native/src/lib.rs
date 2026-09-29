// SPDX-License-Identifier: Apache-2.0

//! `aero1553._native`: the compiled half of the `aero1553` Python package.
//!
//! The Python modules under `python/src/aero1553/` present the package's
//! public API; this extension is where the work happens. It is a thin layer
//! over the `aero1553` Rust crate and holds no decoding logic of its own.

use pyo3::prelude::*;

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    // A value read from the core crate, so importing the module proves the
    // extension links the decoder and not just PyO3.
    m.add(
        "TERMINATOR_TYPE_WORD",
        aero1553::decode::TERMINATOR_TYPE_WORD,
    )?;
    Ok(())
}
