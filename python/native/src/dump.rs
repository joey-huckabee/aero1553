// SPDX-License-Identifier: Apache-2.0

//! The hex dump, for `aero1553.dump`.
//!
//! The core crate's `hex_dump_raw` / `hex_dump_records` write to any `Write`;
//! here that is the caller's text stream. The stream's own exceptions -- a
//! broken pipe included -- propagate unchanged, as they did when the dump was
//! a sequence of Python `print` calls.

use std::path::PathBuf;

use aero1553::dump as core;
use pyo3::prelude::*;

use crate::logbridge;
use crate::stream;
use crate::writer::PyTextSink;

/// A raw hex+ASCII dump of `[start_offset, start_offset + length)` to `stream`.
#[pyfunction]
#[pyo3(signature = (path, start_offset, length, stream))]
pub fn hex_dump_raw(
    py: Python<'_>,
    path: PathBuf,
    start_offset: usize,
    length: Option<usize>,
    stream: Py<PyAny>,
) -> PyResult<()> {
    logbridge::sync_level(py)?;
    let slot = stream::new_slot();
    let sink = PyTextSink::new(stream, slot.clone(), true);
    core::hex_dump_raw(&path, start_offset, length, sink)
        .map_err(|err| stream::raise(py, &slot, err))
}

/// A record-aware hex dump to `stream`.
#[pyfunction]
#[pyo3(signature = (path, max_records, start_offset, stream))]
pub fn hex_dump_records(
    py: Python<'_>,
    path: PathBuf,
    max_records: Option<u64>,
    start_offset: usize,
    stream: Py<PyAny>,
) -> PyResult<()> {
    logbridge::sync_level(py)?;
    let slot = stream::new_slot();
    let sink = PyTextSink::new(stream, slot.clone(), true);
    core::hex_dump_records(&path, max_records, start_offset, sink)
        .map_err(|err| stream::raise(py, &slot, err))
}
