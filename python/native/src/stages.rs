// SPDX-License-Identifier: Apache-2.0

//! The streaming pipeline stages: filtering and canonical row order.
//!
//! Each takes any record source (see `stream::source`) and returns a
//! `RecordIterator`, so stages compose in Rust when given each other.

use aero1553::filter::{FilterConfig, FilterIterExt};
use aero1553::models::Bus;
use aero1553::order::OrderIterExt;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::logbridge;
use crate::stream::{self, PyRecordIterator};

fn buses(values: Vec<u8>) -> PyResult<Vec<Bus>> {
    values
        .into_iter()
        .map(|v| match v {
            0 => Ok(Bus::A),
            1 => Ok(Bus::B),
            _ => Err(PyValueError::new_err(format!("{v} is not a valid Bus"))),
        })
        .collect()
}

/// Drop the records `filters` excludes (L2-FLT-001). The eight sets arrive as
/// lists of integers; `aero1553.filters.apply_filters` unpacks a FilterConfig.
#[pyfunction]
#[allow(clippy::too_many_arguments, reason = "one per filter set")]
#[pyo3(signature = (
    messages, *, exclude_types, exclude_rts, exclude_buses, exclude_subaddresses,
    include_types, include_rts, include_buses, include_subaddresses,
))]
pub fn apply_filters(
    py: Python<'_>,
    messages: &Bound<'_, PyAny>,
    exclude_types: Vec<u8>,
    exclude_rts: Vec<u8>,
    exclude_buses: Vec<u8>,
    exclude_subaddresses: Vec<u8>,
    include_types: Vec<u8>,
    include_rts: Vec<u8>,
    include_buses: Vec<u8>,
    include_subaddresses: Vec<u8>,
) -> PyResult<PyRecordIterator> {
    logbridge::sync_level(py)?;
    let filters = FilterConfig {
        exclude_types,
        exclude_rts,
        exclude_buses: buses(exclude_buses)?,
        exclude_subaddresses,
        include_types,
        include_rts,
        include_buses: buses(include_buses)?,
        include_subaddresses,
    };
    let (source, slot) = stream::source(messages)?;
    Ok(PyRecordIterator::new(
        Box::new(source.filter_messages(filters)),
        slot,
    ))
}

/// Yield the records in canonical row order (L2-WRT-021), buffering at most
/// `max_group` of one equal-timestamp run (L2-WRT-022).
#[pyfunction]
pub fn order_rows(
    py: Python<'_>,
    messages: &Bound<'_, PyAny>,
    max_group: usize,
) -> PyResult<PyRecordIterator> {
    logbridge::sync_level(py)?;
    let (source, slot) = stream::source(messages)?;
    Ok(PyRecordIterator::new(
        Box::new(source.order_rows(max_group)),
        slot,
    ))
}
