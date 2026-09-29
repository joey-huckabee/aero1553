// SPDX-License-Identifier: Apache-2.0

//! The TOML config loader, for `aero1553.config`.
//!
//! `DecoderConfig` and `FilterConfig` stay Python dataclasses -- they are
//! values a caller builds and edits -- so the loader hands back the parsed
//! fields as a dict of plain values, the enums as their integer values, and
//! `aero1553.config` builds the dataclass from it. The grammar, the schema
//! checks and the unknown-key WARN are the core crate's, so a config file
//! means the same thing to the library as to the CLI.

use std::path::PathBuf;

use aero1553::config::{self as core, ConfigError};
use aero1553::models::Bus;
use pyo3::exceptions::{PyUnicodeDecodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::logbridge;

fn value_error(err: ConfigError) -> PyErr {
    PyValueError::new_err(err.0)
}

fn buses(values: &[Bus]) -> Vec<u8> {
    values.iter().map(|&b| b as u8).collect()
}

/// The fields of the config file at `path`, by `DecoderConfig` field name;
/// `filters` is a dict of the `FilterConfig` fields.
///
/// Every rejection is a `ValueError` carrying the core crate's message, except
/// a file that is not UTF-8: that raises `UnicodeDecodeError` (itself a
/// `ValueError`), as reading it as text in Python does.
#[pyfunction]
pub fn load_config(py: Python<'_>, path: PathBuf) -> PyResult<Bound<'_, PyDict>> {
    logbridge::sync_level(py)?;
    let c = match core::load_config(Some(&path)) {
        Ok(c) => c,
        Err(err) => {
            // Only a regular file is re-read: the loader refused anything else
            // precisely because reading it (`/dev/zero`, a FIFO) may not end.
            if path.is_file()
                && let Ok(bytes) = std::fs::read(&path)
                && let Err(utf8) = std::str::from_utf8(&bytes)
            {
                return Err(PyErr::from_value(
                    PyUnicodeDecodeError::new_utf8(py, &bytes, utf8)?.into_any(),
                ));
            }
            return Err(value_error(err));
        }
    };
    let f = &c.filters;
    let filters = PyDict::new(py);
    filters.set_item("exclude_types", &f.exclude_types)?;
    filters.set_item("exclude_rts", &f.exclude_rts)?;
    filters.set_item("exclude_buses", buses(&f.exclude_buses))?;
    filters.set_item("exclude_subaddresses", &f.exclude_subaddresses)?;
    filters.set_item("include_types", &f.include_types)?;
    filters.set_item("include_rts", &f.include_rts)?;
    filters.set_item("include_buses", buses(&f.include_buses))?;
    filters.set_item("include_subaddresses", &f.include_subaddresses)?;

    let d = PyDict::new(py);
    d.set_item("log_level", &c.log_level)?;
    d.set_item("irig_day_advisory", c.irig_day_advisory)?;
    d.set_item("input_time_format", c.input_time_format as u8)?;
    d.set_item("strict", c.strict)?;
    d.set_item("error_mode", c.error_mode as u8)?;
    d.set_item("filters", filters)?;
    d.set_item("output_format", &c.output_format)?;
    d.set_item("no_clobber", c.no_clobber)?;
    d.set_item("output_time_format", c.output_time_format as u8)?;
    d.set_item("year", c.year)?;
    d.set_item("utc_offset_minutes", c.utc_offset_minutes)?;
    d.set_item("allow_partial", c.allow_partial)?;
    d.set_item("detect_records", c.detect_records)?;
    d.set_item("lookahead_records", c.lookahead_records)?;
    d.set_item("standard_tick_rate_hz", c.standard_tick_rate_hz)?;
    d.set_item("mux_enabled", c.mux_enabled)?;
    d.set_item("mux_delimiter", &c.mux_delimiter)?;
    d.set_item("mux_field", c.mux_field)?;
    d.set_item("collapse_duplicates", c.collapse_duplicates)?;
    d.set_item("collapse_window_us", c.collapse_window_us)?;
    d.set_item("max_collapse_survivors", c.max_collapse_survivors)?;
    d.set_item("delta_scope", c.delta_scope as u8)?;
    d.set_item("max_sort_group", c.max_sort_group)?;
    Ok(d)
}

/// Minutes east of UTC for a `Z` / `+HH:MM` / `-HH:MM` designator
/// (L2-CFG-012); `ValueError` for anything else.
#[pyfunction]
pub fn parse_utc_offset(text: &str) -> PyResult<i16> {
    core::parse_utc_offset(text).map_err(value_error)
}
