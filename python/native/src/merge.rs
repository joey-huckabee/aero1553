// SPDX-License-Identifier: Apache-2.0

//! The multi-file merge and DELTA tracking, for `aero1553.merge` and
//! `aero1553.delta`.

use std::path::PathBuf;

use aero1553::delta::{self, DeltaOutcome, DeltaTracker};
use aero1553::merge::{self as core, MergedRecordIter};
use aero1553::models::{DeltaScope, Timestamp};
use aero1553::reader::MieFileReader;
use pyo3::exceptions::{PyUnicodeDecodeError, PyValueError};
use pyo3::prelude::*;

use crate::errors;
use crate::logbridge;
use crate::models::{PyCommandWord, PyIrigTimestamp, PyStandardTimestamp};
use crate::reader::PyReader;
use crate::stream::{self, PyRecordIterator};

/// A time-sorted k-way merge over `readers` (L1-MRG / L2-MRG).
///
/// Validation of every input's leading record (L2-MRG-003) happens here,
/// before any record is yielded, as it does in the CLI.
#[pyfunction]
#[allow(clippy::too_many_arguments, reason = "one per merge option")]
#[allow(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the list by value"
)]
#[pyo3(signature = (
    readers, *, standard_tick_rate_hz, allow_partial, strict, collapse_duplicates,
    collapse_window_us, max_collapse_survivors, delta_scope,
))]
pub fn merge_readers(
    py: Python<'_>,
    readers: Vec<Bound<'_, PyReader>>,
    standard_tick_rate_hz: Option<f64>,
    allow_partial: bool,
    strict: bool,
    collapse_duplicates: bool,
    collapse_window_us: u64,
    max_collapse_survivors: usize,
    delta_scope: u8,
) -> PyResult<PyRecordIterator> {
    logbridge::sync_level(py)?;
    let scope = match delta_scope {
        0 => DeltaScope::PerFile,
        1 => DeltaScope::Global,
        other => {
            return Err(PyValueError::new_err(format!(
                "{other} is not a valid DeltaScope"
            )));
        }
    };
    let inner: Vec<&MieFileReader> = readers.iter().map(|r| &r.get().inner).collect();
    let merged =
        MergedRecordIter::new_detached(&inner, standard_tick_rate_hz, allow_partial, strict)
            .map_err(|err| errors::to_py(py, err))?
            .collapse(collapse_duplicates, collapse_window_us)
            .max_collapse_survivors(max_collapse_survivors)
            .delta_scope(scope);
    Ok(PyRecordIterator::new(Box::new(merged), stream::new_slot()))
}

/// The input paths a `--manifest` file lists (L2-MRG-001).
///
/// A manifest that is not valid UTF-8 raises `UnicodeDecodeError`, as reading
/// it as text in Python does -- not the `OSError` the core crate's
/// `InvalidData` would otherwise become.
#[pyfunction]
pub fn read_manifest(py: Python<'_>, path: PathBuf) -> PyResult<Vec<PathBuf>> {
    match core::read_manifest(&path) {
        Ok(paths) => Ok(paths),
        Err(err) if err.kind() == std::io::ErrorKind::InvalidData => {
            let bytes = std::fs::read(&path)?;
            match std::str::from_utf8(&bytes) {
                Err(utf8) => Err(PyErr::from_value(
                    PyUnicodeDecodeError::new_utf8(py, &bytes, utf8)?.into_any(),
                )),
                Ok(_) => Err(err.into()),
            }
        }
        Err(err) => Err(err.into()),
    }
}

/// Whether `name` matches the single-component `*` / `?` glob `pattern`.
#[pyfunction]
pub fn glob_match(pattern: &str, name: &str) -> bool {
    core::glob_match(pattern, name)
}

/// The regular files a `--glob` pattern names, sorted (L2-MRG-001).
#[pyfunction]
pub fn expand_glob(pattern: &str) -> PyResult<Vec<PathBuf>> {
    core::expand_glob(pattern).map_err(|e| {
        // A wildcard in the directory part is a malformed pattern, not an OS
        // failure: PyO3 would raise a bare `OSError` for `InvalidInput`.
        if e.kind() == std::io::ErrorKind::InvalidInput {
            PyValueError::new_err(e.to_string())
        } else {
            e.into()
        }
    })
}

/// The packed per-RT/MSG DELTA key.
#[pyfunction]
pub fn delta_key(rt: u8, subaddress: u8, transmit: bool) -> u32 {
    delta::delta_key(rt, subaddress, transmit)
}

/// One DELTA observation, as `aero1553.delta` builds its `DeltaOutcome` from
/// it: `(kind, seconds, prev_us, curr_us, key, first_for_key)`, `kind` being
/// the `DeltaKind` value.
type Observation = (u8, Option<f64>, Option<u64>, Option<u64>, Option<u32>, bool);

/// Last-seen timestamp per RT/MSG key, and the gap arithmetic over it.
#[pyclass(name = "DeltaTracker", module = "aero1553._native")]
pub struct PyDeltaTracker {
    inner: DeltaTracker,
}

#[pymethods]
impl PyDeltaTracker {
    #[new]
    #[pyo3(signature = (tick_rate_hz = None))]
    fn new(tick_rate_hz: Option<f64>) -> Self {
        Self {
            inner: DeltaTracker::new(tick_rate_hz),
        }
    }

    /// Record one message and report the gap since the previous one with its
    /// key. `command_word` is `None` for SPURIOUS_DATA, which has no key.
    #[pyo3(signature = (command_word, timestamp))]
    fn observe(
        &mut self,
        command_word: Option<&Bound<'_, PyCommandWord>>,
        timestamp: &Bound<'_, PyAny>,
    ) -> PyResult<Observation> {
        let ts = if let Ok(irig) = timestamp.cast::<PyIrigTimestamp>() {
            Timestamp::Irig(irig.get().inner)
        } else if let Ok(standard) = timestamp.cast::<PyStandardTimestamp>() {
            Timestamp::Standard(standard.get().inner)
        } else {
            return Err(PyValueError::new_err(
                "timestamp must be an IrigTimestamp or a StandardTimestamp",
            ));
        };
        let cw = command_word.map(|c| c.get().inner);
        Ok(match self.inner.observe(cw.as_ref(), &ts) {
            DeltaOutcome::First => (0, None, None, None, None, false),
            DeltaOutcome::Elapsed(seconds) => (1, Some(seconds), None, None, None, false),
            DeltaOutcome::Backward {
                prev_us,
                curr_us,
                first_for_key,
            } => {
                // The key is reported for a backward step so a caller can name
                // it; the command word is present, or there would be no key.
                let key = cw.map(|c| {
                    delta::delta_key(
                        c.rt,
                        c.subaddress,
                        matches!(c.direction, aero1553::models::Direction::Transmit),
                    )
                });
                (2, None, Some(prev_us), Some(curr_us), key, first_for_key)
            }
            DeltaOutcome::Uncalibrated => (3, None, None, None, None, false),
            DeltaOutcome::NoKey => (4, None, None, None, None, false),
        })
    }
}
