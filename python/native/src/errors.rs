// SPDX-License-Identifier: Apache-2.0

//! `MieError` -> the matching `aero1553.exceptions` class.
//!
//! The Python exception classes stay the Python package's own: every variant
//! maps to the class whose constructor takes the same fields, and the class
//! builds its own message, so `str(exc)` and every typed attribute are what
//! the pure-Python decoder raised.

use std::path::Path;

use aero1553::error::MieError;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

/// Convert a decoder error into the Python exception to raise.
pub fn to_py(py: Python<'_>, err: MieError) -> PyErr {
    build(py, err).unwrap_or_else(|failure| failure)
}

fn path(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

/// The Python `OSError` for an I/O error, as the instance a constructor takes.
fn os_error(py: Python<'_>, source: std::io::Error) -> Bound<'_, PyAny> {
    PyErr::from(source).into_value(py).into_bound(py).into_any()
}

fn build(py: Python<'_>, err: MieError) -> PyResult<PyErr> {
    let module = py.import("aero1553.exceptions")?;
    let (class, args): (&str, Bound<'_, PyTuple>) = match err {
        MieError::FileNotFound { path: p } => {
            ("MieFileNotFoundError", (path(&p),).into_pyobject(py)?)
        }
        MieError::FileEmpty { path: p } => ("MieFileEmptyError", (path(&p),).into_pyobject(py)?),
        MieError::FileIo { path: p, source } => (
            "MieFileIoError",
            (path(&p), os_error(py, source)).into_pyobject(py)?,
        ),
        MieError::InvalidTypeWord {
            offset,
            raw_type_word,
            word_count,
        } => (
            "MieInvalidTypeWordError",
            (offset, raw_type_word, word_count).into_pyobject(py)?,
        ),
        MieError::UnknownTypeWord {
            offset,
            raw_type_word,
            message_type,
        } => (
            "MieUnknownTypeWordError",
            (offset, raw_type_word, message_type).into_pyobject(py)?,
        ),
        MieError::RecordTruncated {
            offset,
            record_bytes,
            available_bytes,
        } => (
            "MieRecordTruncatedError",
            (offset, record_bytes, available_bytes).into_pyobject(py)?,
        ),
        MieError::FirstRecordTruncated {
            offset,
            record_bytes,
            available_bytes,
        } => (
            "MieFirstRecordTruncatedError",
            (offset, record_bytes, available_bytes).into_pyobject(py)?,
        ),
        MieError::PayloadError { offset, detail } => {
            ("MiePayloadError", (offset, detail).into_pyobject(py)?)
        }
        MieError::UnknownErrorCode { offset, error_code } => (
            "MieUnknownErrorCodeError",
            (offset, error_code).into_pyobject(py)?,
        ),
        MieError::NoValidRecords {
            path: p,
            scan_bytes,
        } => (
            "MieNoValidRecordsError",
            (path(&p), scan_bytes).into_pyobject(py)?,
        ),
        MieError::HomogeneousPayload {
            path: p,
            offset,
            sample_records,
        } => (
            "MieHomogeneousPayloadError",
            (path(&p), offset, sample_records).into_pyobject(py)?,
        ),
        MieError::WriterError {
            destination,
            source,
        } => (
            "MieWriterError",
            (destination, os_error(py, source)).into_pyobject(py)?,
        ),
        MieError::InputOutputCollision { path: p } => (
            "MieInputOutputCollisionError",
            (path(&p),).into_pyobject(py)?,
        ),
        MieError::ClobberRefused { path: p } => {
            ("MieClobberRefusedError", (path(&p),).into_pyobject(py)?)
        }
        MieError::UnrecoverableSyncLoss {
            offset,
            sync_losses,
        } => (
            "MieUnrecoverableSyncLossError",
            (offset, sync_losses).into_pyobject(py)?,
        ),
        MieError::TimestampFormatMismatch {
            offset,
            irig_score,
            std_score,
            records_probed,
        } => (
            "MieTimestampFormatMismatchError",
            (offset, irig_score, std_score, records_probed).into_pyobject(py)?,
        ),
        MieError::CalendarUnavailable { detail } => {
            ("MieCalendarUnavailableError", (detail,).into_pyobject(py)?)
        }
        MieError::IncompatibleMergeInputs {
            file_index,
            path: p,
            detail,
        } => (
            "MieIncompatibleMergeInputsError",
            (file_index, path(&p), detail).into_pyobject(py)?,
        ),
        MieError::NonMonotonicInput {
            file_index,
            path: p,
            prev_us,
            curr_us,
        } => (
            "MieNonMonotonicInputError",
            (file_index, path(&p), prev_us, curr_us).into_pyobject(py)?,
        ),
        #[allow(
            unreachable_patterns,
            reason = "a variant added to the core crate must still surface"
        )]
        other => return Ok(PyRuntimeError::new_err(other.to_string())),
    };
    let instance = module.getattr(class)?.call1(args)?;
    Ok(PyErr::from_value(instance))
}
