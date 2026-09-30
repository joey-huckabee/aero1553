// SPDX-License-Identifier: Apache-2.0

//! The record types a reader yields: `MieMessage` and the values it holds.
//!
//! Each is a frozen class holding the decoder's own struct inline, so creating
//! one per record is a single allocation. A field becomes a Python object only
//! when it is read -- the nested `TypeWord`, the timestamp, the `data_words`
//! tuple -- where the pure-Python dataclasses built every one of them up front.
//!
//! They keep the dataclasses' surface: the same field and property names, the
//! same constructor (keyword or positional, in field order), equality, hashing,
//! a dataclass-style repr, pickling, and `__replace__` (the `copy.replace`
//! protocol). They are no longer dataclasses, so the `dataclasses` module's
//! helpers do not apply to them.

use std::sync::Arc;

use aero1553::models::{
    Bus, CalendarError, CommandWord, DataWords, Direction, IrigTimestamp, MessageFormat,
    MieMessage, OutputTimeFormat, StandardTimestamp, TimeRender, Timestamp, TypeWord,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyTuple, PyType};

use crate::enums::{PyEnum, member};

// ── shared dataclass-like behaviour ────────────────────────────────────────

/// A value class's fields, in constructor order, as Python objects.
///
/// Repr, hash, pickling and `__replace__` are all derived from this one list,
/// so they cannot disagree with each other or with the constructor.
type Fields<'py> = Vec<(&'static str, Bound<'py, PyAny>)>;

fn repr(name: &str, fields: &Fields<'_>) -> PyResult<String> {
    let mut parts = Vec::with_capacity(fields.len());
    for (key, value) in fields {
        parts.push(format!("{key}={}", value.repr()?));
    }
    Ok(format!("{name}({})", parts.join(", ")))
}

fn hash(py: Python<'_>, fields: &Fields<'_>) -> PyResult<isize> {
    PyTuple::new(py, fields.iter().map(|(_, value)| value))?.hash()
}

fn reduce<'py>(
    slf: &Bound<'py, PyAny>,
    fields: &Fields<'py>,
) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
    let args = PyTuple::new(slf.py(), fields.iter().map(|(_, value)| value))?;
    Ok((slf.get_type(), args))
}

fn replace<'py>(
    slf: &Bound<'py, PyAny>,
    fields: Fields<'py>,
    changes: Option<&Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyAny>> {
    let merged = PyDict::new(slf.py());
    for (key, value) in fields {
        merged.set_item(key, value)?;
    }
    if let Some(changes) = changes {
        merged.update(changes.as_mapping())?;
    }
    slf.get_type().call((), Some(&merged))
}

// ── enum values in from Python ─────────────────────────────────────────────

fn bus_from(value: u8) -> PyResult<Bus> {
    match value {
        0 => Ok(Bus::A),
        1 => Ok(Bus::B),
        _ => Err(PyValueError::new_err(format!("{value} is not a valid Bus"))),
    }
}

fn direction_from(value: u8) -> PyResult<Direction> {
    match value {
        0 => Ok(Direction::Receive),
        1 => Ok(Direction::Transmit),
        _ => Err(PyValueError::new_err(format!(
            "{value} is not a valid Direction"
        ))),
    }
}

fn message_format_from(value: u8) -> PyResult<MessageFormat> {
    use MessageFormat as F;
    Ok(match value {
        1 => F::Receive,
        2 => F::Transmit,
        3 => F::RtToRt,
        4 => F::ReceiveBroadcast,
        5 => F::RtToRtBroadcast,
        6 => F::ModeCodeTxData,
        7 => F::ModeCodeRxData,
        8 => F::ModeCodeNoData,
        9 => F::ModeCodeBcastNoData,
        10 => F::ModeCodeBcastData,
        11 => F::SpuriousData,
        _ => {
            return Err(PyValueError::new_err(format!(
                "{value} is not a valid MessageFormat"
            )));
        }
    })
}

/// A Python `TimeRender` (still a Python dataclass) as the decoder's own.
fn time_render(render: &Bound<'_, PyAny>) -> PyResult<TimeRender> {
    let format = match render.getattr("format")?.extract::<u8>()? {
        0 => OutputTimeFormat::Doy,
        1 => OutputTimeFormat::Iso,
        2 => OutputTimeFormat::Dom,
        other => {
            return Err(PyValueError::new_err(format!(
                "{other} is not a valid OutputTimeFormat"
            )));
        }
    };
    Ok(TimeRender {
        format,
        year: render.getattr("year")?.extract()?,
        utc_offset_minutes: render.getattr("utc_offset_minutes")?.extract()?,
    })
}

static CALENDAR_UNAVAILABLE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// `aero1553.models.CalendarUnavailableError`, worded exactly as the
/// pure-Python renderer raised it.
fn calendar_error(py: Python<'_>, err: CalendarError) -> PyErr {
    let message = match err {
        CalendarError::FreerunNotAnchored => "the record carries a freerun IRIG timestamp -- \
            the card had no valid IRIG-B lock, so its day and time fields are relative rather \
            than calendar-anchored"
            .to_string(),
        CalendarError::MissingYear => {
            "no year was configured; set [output] year or pass --year YYYY".to_string()
        }
        CalendarError::NoSuchDay { day, year } => format!(
            "the record carries day-of-year {day}, which does not exist in {year} ({year} is \
             not a leap year). The configured year is wrong for this recording"
        ),
        CalendarError::NotCalendarLocked => "this recording uses the Standard timestamp \
            encoding, a free-running counter with no epoch. No year places it on a calendar"
            .to_string(),
    };
    let class = CALENDAR_UNAVAILABLE.get_or_try_init(py, || -> PyResult<Py<PyType>> {
        Ok(py
            .import("aero1553.models")?
            .getattr("CalendarUnavailableError")?
            .cast_into::<PyType>()?
            .unbind())
    });
    match class {
        Ok(class) => PyErr::from_type(class.bind(py).clone(), message),
        Err(failure) => failure,
    }
}

// ── TypeWord ───────────────────────────────────────────────────────────────

/// Decoded DDC MIE record Type Word.
#[pyclass(
    frozen,
    eq,
    skip_from_py_object,
    name = "TypeWord",
    module = "aero1553.models"
)]
#[derive(Clone, PartialEq)]
pub struct PyTypeWord {
    pub inner: TypeWord,
}

impl PyTypeWord {
    fn fields<'py>(&self, py: Python<'py>) -> PyResult<Fields<'py>> {
        Ok(vec![
            (
                "message_type",
                self.inner.message_type.into_pyobject(py)?.into_any(),
            ),
            ("bus", member(py, PyEnum::Bus, self.inner.bus as u8)?),
            (
                "word_count",
                self.inner.word_count.into_pyobject(py)?.into_any(),
            ),
            (
                "error",
                self.inner.error.into_pyobject(py)?.to_owned().into_any(),
            ),
            ("raw", self.inner.raw.into_pyobject(py)?.into_any()),
        ])
    }
}

#[pymethods]
impl PyTypeWord {
    #[new]
    #[pyo3(signature = (message_type, bus, word_count, error, raw))]
    fn new(message_type: u8, bus: u8, word_count: u16, error: bool, raw: u16) -> PyResult<Self> {
        Ok(Self {
            inner: TypeWord {
                message_type,
                bus: bus_from(bus)?,
                word_count,
                error,
                raw,
            },
        })
    }

    /// DDC message type code (0x01-0x20).
    #[getter]
    fn message_type(&self) -> u8 {
        self.inner.message_type
    }
    /// Which redundant 1553 bus this message was captured on.
    #[getter]
    fn bus<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        member(py, PyEnum::Bus, self.inner.bus as u8)
    }
    /// Total record size in 16-bit words.
    #[getter]
    fn word_count(&self) -> u16 {
        self.inner.word_count
    }
    /// True if the recording card flagged an error (bit 14).
    #[getter]
    fn error(&self) -> bool {
        self.inner.error
    }
    /// The original 16-bit value.
    #[getter]
    fn raw(&self) -> u16 {
        self.inner.raw
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        repr("TypeWord", &self.fields(py)?)
    }
    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        hash(py, &self.fields(py)?)
    }
    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
        reduce(slf.as_any(), &slf.get().fields(slf.py())?)
    }
    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replace(slf.as_any(), slf.get().fields(slf.py())?, changes)
    }
}

// ── CommandWord ────────────────────────────────────────────────────────────

/// Decoded MIL-STD-1553 Command Word.
#[pyclass(
    frozen,
    eq,
    skip_from_py_object,
    name = "CommandWord",
    module = "aero1553.models"
)]
#[derive(Clone, PartialEq)]
pub struct PyCommandWord {
    pub inner: CommandWord,
}

impl PyCommandWord {
    fn fields<'py>(&self, py: Python<'py>) -> PyResult<Fields<'py>> {
        Ok(vec![
            ("rt", self.inner.rt.into_pyobject(py)?.into_any()),
            (
                "direction",
                member(py, PyEnum::Direction, self.inner.direction as u8)?,
            ),
            (
                "subaddress",
                self.inner.subaddress.into_pyobject(py)?.into_any(),
            ),
            (
                "data_word_count",
                self.inner.data_word_count.into_pyobject(py)?.into_any(),
            ),
            ("raw", self.inner.raw.into_pyobject(py)?.into_any()),
        ])
    }
}

#[pymethods]
impl PyCommandWord {
    #[new]
    #[pyo3(signature = (rt, direction, subaddress, data_word_count, raw))]
    fn new(rt: u8, direction: u8, subaddress: u8, data_word_count: u8, raw: u16) -> PyResult<Self> {
        Ok(Self {
            inner: CommandWord {
                rt,
                direction: direction_from(direction)?,
                subaddress,
                data_word_count,
                raw,
            },
        })
    }

    /// Remote Terminal address (0-30; 31 = broadcast).
    #[getter]
    fn rt(&self) -> u8 {
        self.inner.rt
    }
    /// TRANSMIT or RECEIVE.
    #[getter]
    fn direction<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        member(py, PyEnum::Direction, self.inner.direction as u8)
    }
    /// Subaddress (0-31; 0 and 31 are mode codes).
    #[getter]
    fn subaddress(&self) -> u8 {
        self.inner.subaddress
    }
    /// Number of data words (1-32; raw 0 = 32).
    #[getter]
    fn data_word_count(&self) -> u8 {
        self.inner.data_word_count
    }
    /// The original 16-bit value.
    #[getter]
    fn raw(&self) -> u16 {
        self.inner.raw
    }
    /// True if this command targets all RTs (RT address 31).
    #[getter]
    fn is_broadcast(&self) -> bool {
        self.inner.is_broadcast()
    }
    /// True if this command is a mode code (SA 0 or SA 31).
    #[getter]
    fn is_mode_code(&self) -> bool {
        self.inner.is_mode_code()
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        repr("CommandWord", &self.fields(py)?)
    }
    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        hash(py, &self.fields(py)?)
    }
    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
        reduce(slf.as_any(), &slf.get().fields(slf.py())?)
    }
    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replace(slf.as_any(), slf.get().fields(slf.py())?, changes)
    }
}

// ── IrigTimestamp ──────────────────────────────────────────────────────────

/// IRIG-format timestamp decoded from a 3-word binary field.
#[pyclass(
    frozen,
    eq,
    skip_from_py_object,
    name = "IrigTimestamp",
    module = "aero1553.models"
)]
#[derive(Clone, PartialEq)]
pub struct PyIrigTimestamp {
    pub inner: IrigTimestamp,
}

impl PyIrigTimestamp {
    fn fields<'py>(&self, py: Python<'py>) -> PyResult<Fields<'py>> {
        let t = &self.inner;
        Ok(vec![
            ("day", t.day.into_pyobject(py)?.into_any()),
            ("hour", t.hour.into_pyobject(py)?.into_any()),
            ("minute", t.minute.into_pyobject(py)?.into_any()),
            ("second", t.second.into_pyobject(py)?.into_any()),
            ("microsecond", t.microsecond.into_pyobject(py)?.into_any()),
            (
                "freerun",
                t.freerun.into_pyobject(py)?.to_owned().into_any(),
            ),
        ])
    }
}

#[pymethods]
impl PyIrigTimestamp {
    #[new]
    #[pyo3(signature = (day, hour, minute, second, microsecond, freerun))]
    fn new(day: u16, hour: u8, minute: u8, second: u8, microsecond: u32, freerun: bool) -> Self {
        Self {
            inner: IrigTimestamp {
                day,
                hour,
                minute,
                second,
                microsecond,
                freerun,
            },
        }
    }

    /// Day of year (1-366).
    #[getter]
    fn day(&self) -> u16 {
        self.inner.day
    }
    /// Hour of day (0-23).
    #[getter]
    fn hour(&self) -> u8 {
        self.inner.hour
    }
    /// Minute of hour (0-59).
    #[getter]
    fn minute(&self) -> u8 {
        self.inner.minute
    }
    /// Second of minute (0-59).
    #[getter]
    fn second(&self) -> u8 {
        self.inner.second
    }
    /// Microsecond within the second (0-999999).
    #[getter]
    fn microsecond(&self) -> u32 {
        self.inner.microsecond
    }
    /// True if the external IRIG source was unavailable.
    #[getter]
    fn freerun(&self) -> bool {
        self.inner.freerun
    }

    /// Absolute microseconds from the start of the year (one-based day).
    fn to_total_microseconds(&self) -> u64 {
        self.inner.to_total_microseconds()
    }
    /// Absolute microseconds; the tick rate is accepted and ignored for IRIG.
    #[pyo3(signature = (_standard_tick_rate_hz = None))]
    fn to_microseconds(&self, _standard_tick_rate_hz: Option<f64>) -> u64 {
        self.inner.to_total_microseconds()
    }
    /// Format as ``DAY:HH:MM:SS.uuuuuu``.
    fn format(&self) -> String {
        self.inner.format()
    }
    /// Format under the selected rendering (L2-WRT-025).
    fn format_with(&self, py: Python<'_>, render: &Bound<'_, PyAny>) -> PyResult<String> {
        self.inner
            .format_with(time_render(render)?)
            .map_err(|err| calendar_error(py, err))
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        repr("IrigTimestamp", &self.fields(py)?)
    }
    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        hash(py, &self.fields(py)?)
    }
    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
        reduce(slf.as_any(), &slf.get().fields(slf.py())?)
    }
    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replace(slf.as_any(), slf.get().fields(slf.py())?, changes)
    }
}

// ── StandardTimestamp ──────────────────────────────────────────────────────

/// Standard-format timestamp decoded from a 2-word binary field.
#[pyclass(
    frozen,
    eq,
    skip_from_py_object,
    name = "StandardTimestamp",
    module = "aero1553.models"
)]
#[derive(Clone, PartialEq)]
pub struct PyStandardTimestamp {
    pub inner: StandardTimestamp,
}

impl PyStandardTimestamp {
    fn fields<'py>(&self, py: Python<'py>) -> PyResult<Fields<'py>> {
        let t = &self.inner;
        Ok(vec![
            ("raw_value", t.raw_value.into_pyobject(py)?.into_any()),
            ("upper_word", t.upper_word.into_pyobject(py)?.into_any()),
            ("lower_word", t.lower_word.into_pyobject(py)?.into_any()),
        ])
    }
}

#[pymethods]
impl PyStandardTimestamp {
    #[new]
    #[pyo3(signature = (raw_value, upper_word, lower_word))]
    fn new(raw_value: u32, upper_word: u16, lower_word: u16) -> Self {
        Self {
            inner: StandardTimestamp {
                raw_value,
                upper_word,
                lower_word,
            },
        }
    }

    /// The full 32-bit counter value.
    #[getter]
    fn raw_value(&self) -> u32 {
        self.inner.raw_value
    }
    /// Raw upper 16-bit word (bits [31:16]).
    #[getter]
    fn upper_word(&self) -> u16 {
        self.inner.upper_word
    }
    /// Raw lower 16-bit word (bits [15:0]).
    #[getter]
    fn lower_word(&self) -> u16 {
        self.inner.lower_word
    }

    /// Raw 32-bit free-running counter value, in card-dependent ticks.
    fn raw_ticks(&self) -> u32 {
        self.inner.raw_ticks()
    }
    /// Counter ticks as microseconds, or ``None`` when uncalibrated.
    #[pyo3(signature = (standard_tick_rate_hz = None))]
    fn to_microseconds(&self, standard_tick_rate_hz: Option<f64>) -> Option<u64> {
        standard_tick_rate_hz.and_then(|rate| self.inner.to_microseconds(rate))
    }
    /// Format as ``0xNNNNNNNN``.
    fn format(&self) -> String {
        self.inner.format()
    }
    /// Format under the selected rendering (L2-WRT-025).
    fn format_with(&self, py: Python<'_>, render: &Bound<'_, PyAny>) -> PyResult<String> {
        self.inner
            .format_with(time_render(render)?)
            .map_err(|err| calendar_error(py, err))
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        repr("StandardTimestamp", &self.fields(py)?)
    }
    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        hash(py, &self.fields(py)?)
    }
    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
        reduce(slf.as_any(), &slf.get().fields(slf.py())?)
    }
    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replace(slf.as_any(), slf.get().fields(slf.py())?, changes)
    }
}

// ── MieMessage ─────────────────────────────────────────────────────────────

fn timestamp_object(py: Python<'_>, ts: Timestamp) -> PyResult<Bound<'_, PyAny>> {
    Ok(match ts {
        Timestamp::Irig(inner) => Bound::new(py, PyIrigTimestamp { inner })?.into_any(),
        Timestamp::Standard(inner) => Bound::new(py, PyStandardTimestamp { inner })?.into_any(),
    })
}

fn timestamp_from(obj: &Bound<'_, PyAny>) -> PyResult<Timestamp> {
    if let Ok(irig) = obj.cast::<PyIrigTimestamp>() {
        return Ok(Timestamp::Irig(irig.get().inner));
    }
    if let Ok(standard) = obj.cast::<PyStandardTimestamp>() {
        return Ok(Timestamp::Standard(standard.get().inner));
    }
    Err(PyValueError::new_err(
        "timestamp must be an IrigTimestamp or a StandardTimestamp",
    ))
}

fn command_word_object(py: Python<'_>, cw: Option<CommandWord>) -> PyResult<Bound<'_, PyAny>> {
    match cw {
        Some(inner) => Ok(Bound::new(py, PyCommandWord { inner })?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// A single decoded MIL-STD-1553 message from an MIE binary file.
#[pyclass(
    frozen,
    eq,
    skip_from_py_object,
    name = "MieMessage",
    module = "aero1553.models"
)]
#[derive(Clone, PartialEq)]
pub struct PyMieMessage {
    pub inner: MieMessage,
}

impl PyMieMessage {
    fn fields<'py>(&self, py: Python<'py>) -> PyResult<Fields<'py>> {
        let m = &self.inner;
        Ok(vec![
            ("timestamp", timestamp_object(py, m.timestamp)?),
            (
                "type_word",
                Bound::new(py, PyTypeWord { inner: m.type_word })?.into_any(),
            ),
            (
                "message_format",
                member(py, PyEnum::MessageFormat, m.message_format as u8)?,
            ),
            ("command_word", command_word_object(py, m.command_word)?),
            ("command_word_2", command_word_object(py, m.command_word_2)?),
            ("status_word", m.status_word.into_pyobject(py)?.into_any()),
            (
                "status_word_2",
                m.status_word_2.into_pyobject(py)?.into_any(),
            ),
            (
                "data_words",
                PyTuple::new(py, m.data_words.as_slice())?.into_any(),
            ),
            ("error_word", m.error_word.into_pyobject(py)?.into_any()),
            ("delta", m.delta.into_pyobject(py)?.into_any()),
            ("file_offset", m.file_offset.into_pyobject(py)?.into_any()),
            ("mux", m.mux.as_deref().into_pyobject(py)?.into_any()),
        ])
    }
}

#[pymethods]
impl PyMieMessage {
    #[new]
    #[allow(
        clippy::too_many_arguments,
        reason = "mirrors the dataclass's twelve fields"
    )]
    #[pyo3(signature = (
        timestamp, type_word, message_format, command_word, command_word_2,
        status_word, status_word_2, data_words, error_word, delta, file_offset, mux = None,
    ))]
    fn new(
        timestamp: &Bound<'_, PyAny>,
        type_word: &Bound<'_, PyTypeWord>,
        message_format: u8,
        command_word: Option<&Bound<'_, PyCommandWord>>,
        command_word_2: Option<&Bound<'_, PyCommandWord>>,
        status_word: Option<u16>,
        status_word_2: Option<u16>,
        data_words: Vec<u16>,
        error_word: Option<u16>,
        delta: Option<f64>,
        file_offset: u64,
        mux: Option<String>,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: MieMessage {
                timestamp: timestamp_from(timestamp)?,
                type_word: type_word.get().inner,
                message_format: message_format_from(message_format)?,
                command_word: command_word.map(|cw| cw.get().inner),
                command_word_2: command_word_2.map(|cw| cw.get().inner),
                status_word,
                status_word_2,
                // Capped at 32, as the dataclass's __post_init__ capped it.
                data_words: DataWords::from_slice(&data_words),
                error_word,
                delta,
                file_offset,
                mux: mux.map(Arc::from),
            },
        })
    }

    /// IRIG or Standard timestamp.
    #[getter]
    fn timestamp<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        timestamp_object(py, self.inner.timestamp)
    }
    /// Decoded Type Word containing message metadata.
    #[getter]
    fn type_word(&self) -> PyTypeWord {
        PyTypeWord {
            inner: self.inner.type_word,
        }
    }
    /// Classified message format.
    #[getter]
    fn message_format<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        member(py, PyEnum::MessageFormat, self.inner.message_format as u8)
    }
    /// Primary Command Word; ``None`` for SPURIOUS_DATA.
    #[getter]
    fn command_word(&self) -> Option<PyCommandWord> {
        self.inner.command_word.map(|inner| PyCommandWord { inner })
    }
    /// Second Command Word for RT-to-RT; ``None`` otherwise.
    #[getter]
    fn command_word_2(&self) -> Option<PyCommandWord> {
        self.inner
            .command_word_2
            .map(|inner| PyCommandWord { inner })
    }
    /// Primary Status Word, if any.
    #[getter]
    fn status_word(&self) -> Option<u16> {
        self.inner.status_word
    }
    /// Second Status Word for RT-to-RT, if any.
    #[getter]
    fn status_word_2(&self) -> Option<u16> {
        self.inner.status_word_2
    }
    /// Raw 16-bit data words in bus wire order.
    #[getter]
    fn data_words<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(py, self.inner.data_words.as_slice())
    }
    /// DDC error code (0x01xx) or decoder-assigned code (0x20xx), if any.
    #[getter]
    fn error_word(&self) -> Option<u16> {
        self.inner.error_word
    }
    /// Seconds since the prior message with the same RT+MSG, if meaningful.
    #[getter]
    fn delta(&self) -> Option<f64> {
        self.inner.delta
    }
    /// Byte offset of this record in the source file.
    #[getter]
    fn file_offset(&self) -> u64 {
        self.inner.file_offset
    }
    /// MUX column value derived from the source file name, if any.
    #[getter]
    fn mux(&self) -> Option<&str> {
        self.inner.mux.as_deref()
    }

    /// Remote Terminal address, or ``None`` for SPURIOUS_DATA.
    #[getter]
    fn rt(&self) -> Option<u8> {
        self.inner.rt()
    }
    /// Subaddress, or ``None`` for SPURIOUS_DATA.
    #[getter]
    fn subaddress(&self) -> Option<u8> {
        self.inner.subaddress()
    }
    /// Bus identifier shortcut.
    #[getter]
    fn bus<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        member(py, PyEnum::Bus, self.inner.bus() as u8)
    }
    /// Message label in ``<SA><T|R>`` format, or empty for SPURIOUS_DATA.
    #[getter]
    fn msg_label(&self) -> String {
        self.inner.msg_label()
    }
    /// Key for per-RT/MSG DELTA tracking; empty for SPURIOUS_DATA.
    #[getter]
    fn delta_key(&self) -> String {
        self.inner.delta_key()
    }
    /// True if this record has the error flag set (bit 14).
    #[getter]
    fn is_error(&self) -> bool {
        self.inner.is_error()
    }
    /// This record as a dict of plain values, keyed by the `aero1553.columns`
    /// schema; an absent value is `None`.
    #[pyo3(signature = (*, fields=None, standard_tick_rate_hz=None, year=None, utc_offset_minutes=0))]
    fn to_dict<'py>(
        &self,
        py: Python<'py>,
        fields: Option<Bound<'py, PyAny>>,
        standard_tick_rate_hz: Option<f64>,
        year: Option<u16>,
        utc_offset_minutes: i16,
    ) -> PyResult<Bound<'py, PyDict>> {
        let spec = crate::table::Spec::new(
            py,
            fields.as_ref(),
            standard_tick_rate_hz,
            year,
            utc_offset_minutes,
        )?;
        crate::table::to_dict(py, &self.inner, &spec)
    }
    /// True if this is a SPURIOUS_DATA record (type 0x20).
    #[getter]
    fn is_spurious(&self) -> bool {
        self.inner.is_spurious()
    }
    /// ``""``, ``"ERROR"`` or ``"SPURIOUS"``, for the CSV ERROR column.
    #[getter]
    fn error_label(&self) -> &'static str {
        self.inner.error_label()
    }

    /// Return a copy of this message carrying a new DELTA.
    fn with_delta(&self, delta: Option<f64>) -> Self {
        let mut inner = self.inner.clone();
        inner.delta = delta;
        Self { inner }
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        repr("MieMessage", &self.fields(py)?)
    }
    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        hash(py, &self.fields(py)?)
    }
    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
        reduce(slf.as_any(), &slf.get().fields(slf.py())?)
    }
    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replace(slf.as_any(), slf.get().fields(slf.py())?, changes)
    }
}
