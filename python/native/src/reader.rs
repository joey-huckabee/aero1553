// SPDX-License-Identifier: Apache-2.0

//! The native reader behind `aero1553.reader.MieFileReader`.
//!
//! `MieFileReader` itself stays a small Python class so its constructor keeps a
//! typed signature (L3-PY-007); it holds one of these and hands out the
//! iterator below directly, so no Python code runs per record.

use std::path::PathBuf;

use aero1553::models::{TimestampFormat, check_year};
use aero1553::reader::{MieFileReader, ReaderOptions};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::errors;
use crate::logbridge;
use crate::stream::{self, PyRecordIterator};

fn timestamp_format_from(value: u8) -> PyResult<TimestampFormat> {
    match value {
        0 => Ok(TimestampFormat::Auto),
        1 => Ok(TimestampFormat::Irig),
        2 => Ok(TimestampFormat::Standard),
        _ => Err(PyValueError::new_err(format!(
            "{value} is not a valid TimestampFormat"
        ))),
    }
}

/// The decoder's reader for one recording.
#[pyclass(frozen, name = "NativeReader", module = "aero1553._native")]
pub struct PyReader {
    pub(crate) inner: MieFileReader,
}

#[pymethods]
impl PyReader {
    #[new]
    #[allow(clippy::too_many_arguments, reason = "one per reader option")]
    #[pyo3(signature = (
        path, *, strict, input_time_format, detect_records, lookahead_records,
        standard_tick_rate_hz, mux_enabled, mux_delimiter, mux_field, calendar_year,
    ))]
    fn new(
        py: Python<'_>,
        path: PathBuf,
        strict: bool,
        input_time_format: u8,
        detect_records: usize,
        lookahead_records: usize,
        standard_tick_rate_hz: Option<f64>,
        mux_enabled: bool,
        mux_delimiter: String,
        mux_field: i64,
        calendar_year: Option<i64>,
    ) -> PyResult<Self> {
        logbridge::sync_level(py)?;
        let options = ReaderOptions {
            strict,
            input_time_format: timestamp_format_from(input_time_format)?,
            detect_records,
            lookahead_records,
            standard_tick_rate_hz,
            mux_enabled,
            mux_delimiter,
            mux_field,
            // The same year check every rendering is built through: this year
            // is the calendar the reader's rollover WARN names.
            calendar_year: calendar_year
                .map(check_year)
                .transpose()
                .map_err(|err| PyValueError::new_err(err.to_string()))?,
        };
        MieFileReader::with_options(&path, options)
            .map(|inner| Self { inner })
            .map_err(|err| errors::to_py(py, err))
    }

    /// Size of the source file in bytes.
    #[getter]
    fn file_size(&self) -> u64 {
        self.inner.file_size()
    }

    /// Sync recoveries in the most recently started iteration.
    #[getter]
    fn sync_losses(&self) -> u64 {
        self.inner.sync_losses()
    }

    /// Whether the most recently started iteration found a valid but empty
    /// recording.
    #[getter]
    fn empty_recording(&self) -> bool {
        self.inner.empty_recording()
    }

    /// A fresh pass over the recording, from its first record.
    ///
    /// The iterator shares the file mapping with this reader rather than
    /// borrowing it, so it stays valid however long Python keeps it.
    fn records(&self, py: Python<'_>) -> PyResult<PyRecordIterator> {
        logbridge::sync_level(py)?;
        Ok(PyRecordIterator::new(
            Box::new(self.inner.iter_detached()),
            stream::new_slot(),
        ))
    }
}
