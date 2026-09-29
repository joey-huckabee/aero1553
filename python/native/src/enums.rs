// SPDX-License-Identifier: Apache-2.0

//! The six enums stay Python `IntEnum` classes, defined in `aero1553.models`.
//!
//! A getter that returns one hands back the enum's own member object, looked
//! up in the class's `_value2member_map_` -- a dict hit, done only when the
//! field is read. So `msg.bus is Bus.A`, `isinstance(msg.bus, int)` and the
//! member's name and repr are exactly what the pure-Python package gave.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyDict;

/// Which `aero1553.models` enum a value belongs to.
#[derive(Clone, Copy)]
pub enum PyEnum {
    Bus,
    Direction,
    MessageFormat,
}

static BUS: PyOnceLock<Py<PyDict>> = PyOnceLock::new();
static DIRECTION: PyOnceLock<Py<PyDict>> = PyOnceLock::new();
static MESSAGE_FORMAT: PyOnceLock<Py<PyDict>> = PyOnceLock::new();

/// The `IntEnum` member whose value is `value`.
///
/// The value comes from a decoded record, so it is always one the enum
/// defines; a value it does not define falls back to the plain integer rather
/// than raising, so a getter can never fail on a record the decoder produced.
pub fn member(py: Python<'_>, which: PyEnum, value: u8) -> PyResult<Bound<'_, PyAny>> {
    let (cell, name) = match which {
        PyEnum::Bus => (&BUS, "Bus"),
        PyEnum::Direction => (&DIRECTION, "Direction"),
        PyEnum::MessageFormat => (&MESSAGE_FORMAT, "MessageFormat"),
    };
    let table = cell.get_or_try_init(py, || -> PyResult<Py<PyDict>> {
        let class = py.import("aero1553.models")?.getattr(name)?;
        Ok(class
            .getattr("_value2member_map_")?
            .cast_into::<PyDict>()?
            .unbind())
    })?;
    match table.bind(py).get_item(value)? {
        Some(found) => Ok(found),
        None => Ok(value.into_pyobject(py)?.into_any()),
    }
}
