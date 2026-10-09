// SPDX-License-Identifier: Apache-2.0

//! The tabular views of a record stream: `MieMessage.to_dict()` and
//! `aero1553.columns()`.
//!
//! One schema, [`FIELDS`], serves both, and one function, [`value`], computes
//! every field of a record -- so a row dict and a column table cannot name,
//! type or derive a field differently. The views differ only in how they show
//! an absent value: `to_dict` says `None`; a column, whose buffer has no
//! `None`, uses NumPy's conventions -- `-1` for an integer, `NaN` for a float,
//! the minimum `int64` (NumPy's `NaT`) for a datetime.
//!
//! Time follows the rules the decoder already has, rather than new ones:
//! `time_us` is the DELTA rule (L2-DEC-017 -- IRIG always, Standard only with
//! a tick rate), and `datetime` is the calendar rule of the ISO rendering
//! (L2-WRT-025/026 -- a year is required, a freerun record has no date, and
//! day 366 of a common year does not exist).

use aero1553::models::{
    CommandWord, IrigTimestamp, MieMessage, TimeRenderError, Timestamp, check_utc_offset,
    check_year, day_of_year_to_month_day,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use std::sync::{Mutex, PoisonError};

use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};

use crate::logbridge;
use crate::stream;

/// How a field is stored in a column.
#[derive(Clone, Copy)]
enum Kind {
    U64,
    I64,
    I16,
    U8,
    I8,
    I32,
    Bool,
    F64,
    Text,
    OptText,
    Words,
    Datetime,
}

/// The schema, in column order. The `datetime` field exists only when a year
/// is given.
/// A field of the schema, so `value` dispatches on a discriminant rather than
/// comparing names for every field of every record.
#[derive(Clone, Copy)]
enum F {
    FileOffset,
    Timestamp,
    TimeUs,
    Ticks,
    Freerun,
    DayOfYear,
    TimeOfDayUs,
    Datetime,
    MessageType,
    MessageFormat,
    Bus,
    Error,
    Rt,
    Subaddress,
    Direction,
    MsgLabel,
    CommandWord,
    CommandWord2,
    StatusWord,
    StatusWord2,
    ErrorWord,
    DataWordCount,
    DataWords,
    Delta,
    Mux,
}

const FIELDS: &[(&str, Kind, F)] = &[
    ("file_offset", Kind::U64, F::FileOffset),
    ("timestamp", Kind::Text, F::Timestamp),
    ("time_us", Kind::I64, F::TimeUs),
    ("ticks", Kind::I64, F::Ticks),
    ("freerun", Kind::Bool, F::Freerun),
    ("day_of_year", Kind::I16, F::DayOfYear),
    ("time_of_day_us", Kind::I64, F::TimeOfDayUs),
    ("datetime", Kind::Datetime, F::Datetime),
    ("message_type", Kind::U8, F::MessageType),
    ("message_format", Kind::U8, F::MessageFormat),
    ("bus", Kind::U8, F::Bus),
    ("error", Kind::Bool, F::Error),
    ("rt", Kind::I8, F::Rt),
    ("subaddress", Kind::I8, F::Subaddress),
    ("direction", Kind::I8, F::Direction),
    ("msg_label", Kind::Text, F::MsgLabel),
    ("command_word", Kind::I32, F::CommandWord),
    ("command_word_2", Kind::I32, F::CommandWord2),
    ("status_word", Kind::I32, F::StatusWord),
    ("status_word_2", Kind::I32, F::StatusWord2),
    ("error_word", Kind::I32, F::ErrorWord),
    ("data_word_count", Kind::U8, F::DataWordCount),
    ("data_words", Kind::Words, F::DataWords),
    ("delta", Kind::F64, F::Delta),
    ("mux", Kind::OptText, F::Mux),
];

const DATETIME: usize = 7;
/// Records gathered before the columns are filled: bounded, so `columns` keeps
/// the stream's constant-memory property.
const BATCH: usize = 1024;
/// NumPy's `NaT` as an `int64`.
const NAT: i64 = i64::MIN;

/// The field names, in column order, for `aero1553.table.FIELDS`.
#[pyfunction]
pub fn table_fields() -> Vec<&'static str> {
    FIELDS.iter().map(|(name, _, _)| *name).collect()
}

/// The schema's keys as interned Python strings, made once. A caller's field
/// names written as literals are interned too, so resolving them is usually a
/// pointer comparison, and `to_dict` never builds a key string per record.
static KEYS: PyOnceLock<Vec<Py<PyString>>> = PyOnceLock::new();

fn keys(py: Python<'_>) -> &[Py<PyString>] {
    KEYS.get_or_init(py, || {
        FIELDS
            .iter()
            .map(|(name, _, _)| PyString::intern(py, name).unbind())
            .collect()
    })
}

/// The last `fields` object resolved, with strong references to it and to
/// each name it held, and the selection it resolved to.
///
/// `to_dict(fields=...)` runs once per record, almost always with the same
/// list, and resolving 18 names costs more than building the dict. Holding
/// the references is what makes the identity test sound: an object kept alive
/// cannot be freed and its address reused by another, and a `str` cannot
/// change. A list can be mutated in place, so its items are compared too.
struct Resolved {
    fields: Py<PyAny>,
    items: Vec<Py<PyAny>>,
    wanted: [bool; FIELDS.len()],
}

static LAST_RESOLVED: Mutex<Option<Resolved>> = Mutex::new(None);

fn cached(names: &Bound<'_, PyAny>, last: &Resolved) -> Option<[bool; FIELDS.len()]> {
    if !names.is(&last.fields) {
        return None;
    }
    if names.is_exact_instance_of::<PyTuple>() {
        return Some(last.wanted);
    }
    let list = names.cast::<PyList>().ok()?;
    if list.len() != last.items.len() {
        return None;
    }
    let same = list
        .iter()
        .zip(&last.items)
        .all(|(item, kept)| item.is(kept));
    same.then_some(last.wanted)
}

/// Which fields `names` selects, from the cache when it is the same object.
///
/// `LAST_RESOLVED` is locked only around the lookup and the store, never while
/// Python code runs. Iterating `names` runs the caller's code (a generator, a
/// custom `__iter__`, a `str` subclass's `__repr__` in the error message), and
/// that code can release the GIL. Holding the lock across it let one thread
/// own the lock while waiting for the GIL and another own the GIL while waiting
/// for the lock: the process hung for good. Dropping a replaced entry can run
/// `__del__` the same way, so that happens after the lock is released too.
/// Two threads that miss together both resolve, and the last store wins --
/// the cache is an optimisation, so either entry is correct.
fn resolve(py: Python<'_>, names: &Bound<'_, PyAny>) -> PyResult<[bool; FIELDS.len()]> {
    {
        let last = LAST_RESOLVED.lock().unwrap_or_else(PoisonError::into_inner);
        // `cached` compares object identities and indexes a `list` directly;
        // neither runs Python code.
        if let Some(hit) = last.as_ref().and_then(|l| cached(names, l)) {
            return Ok(hit);
        }
    }
    if names.is_instance_of::<PyString>() {
        return Err(PyValueError::new_err(
            "fields must be a sequence of field names, not one string",
        ));
    }
    let keys = keys(py);
    let mut wanted = [false; FIELDS.len()];
    let mut items = Vec::new();
    for name in names.try_iter()? {
        let name = name?;
        let index = match keys.iter().position(|k| name.is(k)) {
            Some(i) => Some(i),
            None => {
                let text = name.cast::<PyString>()?.to_str()?;
                FIELDS.iter().position(|(f, _, _)| *f == text)
            }
        };
        let Some(i) = index else {
            return Err(PyValueError::new_err(format!(
                "unknown field {}; valid fields: {}",
                name.repr()?,
                table_fields().join(", ")
            )));
        };
        wanted[i] = true;
        items.push(name.unbind());
    }
    if items.is_empty() {
        return Err(PyValueError::new_err("fields must name at least one field"));
    }
    if names.is_exact_instance_of::<PyTuple>() || names.is_exact_instance_of::<PyList>() {
        let entry = Some(Resolved {
            fields: names.clone().unbind(),
            items,
            wanted,
        });
        let replaced = std::mem::replace(
            &mut *LAST_RESOLVED.lock().unwrap_or_else(PoisonError::into_inner),
            entry,
        );
        drop(replaced);
    }
    Ok(wanted)
}

/// What the caller asked for: which fields, and the time context. Holds no
/// heap allocation, because `to_dict` builds one per record.
pub struct Spec {
    selected: [usize; FIELDS.len()],
    count: usize,
    tick_rate_hz: Option<f64>,
    year: Option<u16>,
    utc_offset_minutes: i16,
}

impl Spec {
    /// Validate the options. With no `fields`, every field is selected except
    /// `datetime` without a year; naming `datetime` without a year is an error,
    /// because the file cannot answer it.
    pub fn new(
        py: Python<'_>,
        fields: Option<&Bound<'_, PyAny>>,
        tick_rate_hz: Option<f64>,
        year: Option<i64>,
        utc_offset_minutes: i64,
    ) -> PyResult<Self> {
        if let Some(hz) = tick_rate_hz
            && !(hz.is_finite() && hz > 0.0)
        {
            return Err(PyValueError::new_err(format!(
                "standard_tick_rate_hz must be finite and positive, got {hz}"
            )));
        }
        // The core's range checks, the ones every rendering is built through.
        // This once had its own copy, which tested `abs() > 1439` and so passed
        // i16::MIN: its absolute value does not fit, and wraps back to itself.
        let range = |err: TimeRenderError| PyValueError::new_err(err.to_string());
        let year = year.map(check_year).transpose().map_err(range)?;
        let utc_offset_minutes = check_utc_offset(utc_offset_minutes).map_err(range)?;
        let wanted = match fields {
            None => {
                let mut all = [true; FIELDS.len()];
                all[DATETIME] = year.is_some();
                all
            }
            Some(names) => {
                let wanted = resolve(py, names)?;
                if wanted[DATETIME] && year.is_none() {
                    return Err(PyValueError::new_err(
                        "the datetime field needs year=: an MIE recording stores the \
                         day of the year but not the year",
                    ));
                }
                wanted
            }
        };
        let mut selected = [0; FIELDS.len()];
        let mut count = 0;
        for (i, _) in wanted.iter().enumerate().filter(|(_, w)| **w) {
            selected[count] = i;
            count += 1;
        }
        Ok(Self {
            selected,
            count,
            tick_rate_hz,
            year,
            utc_offset_minutes,
        })
    }

    fn selected(&self) -> &[usize] {
        &self.selected[..self.count]
    }
}

/// Days from 1970-01-01 to `year`-01-01 (proleptic Gregorian).
fn days_to_new_year(year: u16) -> i64 {
    let y = i64::from(year) - 1;
    // Days from 0001-01-01 to `year`-01-01, minus the same for 1970.
    y * 365 + y / 4 - y / 100 + y / 400 - 719_162
}

/// The instant a calendar-locked IRIG record names, given the year and the
/// offset of the recorder's clock from UTC.
fn datetime_us(m: &MieMessage, spec: &Spec) -> Option<i64> {
    let (Timestamp::Irig(t), Some(year)) = (m.timestamp, spec.year) else {
        return None;
    };
    if t.freerun {
        return None;
    }
    day_of_year_to_month_day(year, t.day)?;
    let days = days_to_new_year(year) + i64::from(t.day) - 1;
    let seconds =
        days * 86_400 + i64::from(t.hour) * 3_600 + i64::from(t.minute) * 60 + i64::from(t.second)
            - i64::from(spec.utc_offset_minutes) * 60;
    Some(seconds * 1_000_000 + i64::from(t.microsecond % 1_000_000))
}

// The per-field derivations, shared by `put` (for `to_dict`) and
// `Column::push_batch` (for `columns`), so the two views differ only in how
// they store a value, never in how they compute it.

fn irig(m: &MieMessage) -> Option<IrigTimestamp> {
    match m.timestamp {
        Timestamp::Irig(t) => Some(t),
        Timestamp::Standard(_) => None,
    }
}

fn time_us(m: &MieMessage, spec: &Spec) -> Option<i64> {
    m.timestamp
        .to_microseconds(spec.tick_rate_hz)
        .and_then(|us| i64::try_from(us).ok())
}

fn ticks(m: &MieMessage) -> Option<i64> {
    match m.timestamp {
        Timestamp::Standard(s) => Some(i64::from(s.raw_ticks())),
        Timestamp::Irig(_) => None,
    }
}

fn time_of_day_us(t: IrigTimestamp) -> i64 {
    (i64::from(t.hour) * 3_600 + i64::from(t.minute) * 60 + i64::from(t.second)) * 1_000_000
        + i64::from(t.microsecond % 1_000_000)
}

fn direction(m: &MieMessage) -> Option<u8> {
    m.command_word.map(|c| c.direction as u8)
}

fn raw(c: Option<CommandWord>) -> Option<u16> {
    c.map(|c| c.raw)
}

static DATETIME_FROM_US: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// A timezone-aware `datetime` for `us` microseconds since the epoch, shown
/// in the recorder's zone.
fn py_datetime<'py>(py: Python<'py>, us: i64, offset_minutes: i16) -> PyResult<Bound<'py, PyAny>> {
    let convert = DATETIME_FROM_US.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        let ns = PyDict::new(py);
        py.run(
            c"import datetime as _d\n\
_EPOCH = _d.datetime(1970, 1, 1, tzinfo=_d.timezone.utc)\n\
def convert(us, offset_minutes):\n    \
return (_EPOCH + _d.timedelta(microseconds=us)).astimezone(\n        \
_d.timezone(_d.timedelta(minutes=offset_minutes)))\n",
            Some(&ns),
            None,
        )?;
        Ok(ns
            .get_item("convert")?
            .ok_or_else(|| PyValueError::new_err("datetime converter missing"))?
            .unbind())
    })?;
    convert.bind(py).call1((us, offset_minutes))
}

/// One record as a dict of plain values.
pub fn to_dict<'py>(py: Python<'py>, m: &MieMessage, spec: &Spec) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    let keys = keys(py);
    for &field in spec.selected() {
        put(&d, keys[field].bind(py), FIELDS[field].2, m, spec)?;
    }
    Ok(d)
}

/// Set field `f` of `m` in `d`, as its plain Python value (`None` when
/// absent). The same derivations as `Column::push_batch`.
fn put(
    d: &Bound<'_, PyDict>,
    key: &Bound<'_, PyString>,
    f: F,
    m: &MieMessage,
    spec: &Spec,
) -> PyResult<()> {
    let py = d.py();
    match f {
        F::FileOffset => d.set_item(key, m.file_offset),
        F::Timestamp => d.set_item(key, m.timestamp.format()),
        F::TimeUs => d.set_item(key, time_us(m, spec)),
        F::Ticks => d.set_item(key, ticks(m)),
        F::Freerun => d.set_item(key, irig(m).map(|t| t.freerun)),
        F::DayOfYear => d.set_item(key, irig(m).map(|t| t.day)),
        F::TimeOfDayUs => d.set_item(key, irig(m).map(time_of_day_us)),
        F::Datetime => match datetime_us(m, spec) {
            Some(us) => d.set_item(key, py_datetime(py, us, spec.utc_offset_minutes)?),
            None => d.set_item(key, py.None()),
        },
        F::MessageType => d.set_item(key, m.type_word.message_type),
        F::MessageFormat => d.set_item(key, m.message_format as u8),
        F::Bus => d.set_item(key, m.bus() as u8),
        F::Error => d.set_item(key, m.is_error()),
        F::Rt => d.set_item(key, m.rt()),
        F::Subaddress => d.set_item(key, m.subaddress()),
        F::Direction => d.set_item(key, direction(m)),
        F::MsgLabel => d.set_item(key, m.msg_label()),
        F::CommandWord => d.set_item(key, raw(m.command_word)),
        F::CommandWord2 => d.set_item(key, raw(m.command_word_2)),
        F::StatusWord => d.set_item(key, m.status_word),
        F::StatusWord2 => d.set_item(key, m.status_word_2),
        F::ErrorWord => d.set_item(key, m.error_word),
        F::DataWordCount => d.set_item(key, m.data_words.len()),
        F::DataWords => d.set_item(key, PyTuple::new(py, m.data_words.as_slice())?),
        F::Delta => d.set_item(key, m.delta),
        F::Mux => d.set_item(key, m.mux.as_deref()),
    }
}

/// One column's buffer, typed, so a push is a plain `Vec::push`.
enum Column {
    U64(Vec<u64>),
    I64(Vec<i64>),
    I16(Vec<i16>),
    U8(Vec<u8>),
    I8(Vec<i8>),
    I32(Vec<i32>),
    Bool(Vec<bool>),
    F64(Vec<f64>),
    Text(Vec<Option<String>>),
    Words(Vec<u16>),
}

fn column_for(kind: Kind) -> Column {
    match kind {
        Kind::U64 => Column::U64(Vec::new()),
        Kind::I64 | Kind::Datetime => Column::I64(Vec::new()),
        Kind::I16 => Column::I16(Vec::new()),
        Kind::U8 => Column::U8(Vec::new()),
        Kind::I8 => Column::I8(Vec::new()),
        Kind::I32 => Column::I32(Vec::new()),
        Kind::Bool => Column::Bool(Vec::new()),
        Kind::F64 => Column::F64(Vec::new()),
        Kind::Text | Kind::OptText => Column::Text(Vec::new()),
        Kind::Words => Column::Words(Vec::new()),
    }
}

/// `v` as one contiguous native-endian byte buffer.
fn ne_bytes<T, const N: usize>(v: &[T], f: impl Fn(&T) -> [u8; N]) -> Vec<u8> {
    let mut out = Vec::with_capacity(v.len() * N);
    for x in v {
        out.extend_from_slice(&f(x));
    }
    out
}

impl Column {
    /// Compute field `f` of every record in `batch` straight into this typed
    /// column, an absent value as the column's sentinel.
    ///
    /// The match on `f` runs once per column per batch, not once per field
    /// per record: each arm is then a tight, monomorphic loop. Filling a
    /// record at a time instead cost ~5 ns per field per record in a
    /// data-dependent jump -- measured against hand-written per-field code.
    #[allow(clippy::too_many_lines, reason = "one arm per schema field")]
    fn push_batch(&mut self, f: F, batch: &[MieMessage], spec: &Spec) {
        // Narrowings are of values the decoder bounds: RT, subaddress and
        // direction fit an i8, a day of year an i16, a data-word count a u8.
        #[allow(clippy::cast_possible_truncation)]
        let small = |v: Option<u8>| v.map_or(-1, |x| x as i8);
        let word = |v: Option<u16>| v.map_or(-1, i32::from);
        #[allow(clippy::cast_possible_truncation)]
        match (f, self) {
            (F::FileOffset, Self::U64(v)) => v.extend(batch.iter().map(|m| m.file_offset)),
            (F::Timestamp, Self::Text(v)) => {
                v.extend(batch.iter().map(|m| Some(m.timestamp.format())))
            }
            (F::TimeUs, Self::I64(v)) => {
                v.extend(batch.iter().map(|m| time_us(m, spec).unwrap_or(-1)))
            }
            (F::Ticks, Self::I64(v)) => v.extend(batch.iter().map(|m| ticks(m).unwrap_or(-1))),
            (F::Freerun, Self::Bool(v)) => {
                v.extend(batch.iter().map(|m| irig(m).is_some_and(|t| t.freerun)))
            }
            (F::DayOfYear, Self::I16(v)) => {
                v.extend(batch.iter().map(|m| irig(m).map_or(-1, |t| t.day as i16)))
            }
            (F::TimeOfDayUs, Self::I64(v)) => {
                v.extend(batch.iter().map(|m| irig(m).map_or(-1, time_of_day_us)))
            }
            (F::Datetime, Self::I64(v)) => {
                v.extend(batch.iter().map(|m| datetime_us(m, spec).unwrap_or(NAT)))
            }
            (F::MessageType, Self::U8(v)) => {
                v.extend(batch.iter().map(|m| m.type_word.message_type))
            }
            (F::MessageFormat, Self::U8(v)) => {
                v.extend(batch.iter().map(|m| m.message_format as u8))
            }
            (F::Bus, Self::U8(v)) => v.extend(batch.iter().map(|m| m.bus() as u8)),
            (F::Error, Self::Bool(v)) => v.extend(batch.iter().map(|m| m.is_error())),
            (F::Rt, Self::I8(v)) => v.extend(batch.iter().map(|m| small(m.rt()))),
            (F::Subaddress, Self::I8(v)) => v.extend(batch.iter().map(|m| small(m.subaddress()))),
            (F::Direction, Self::I8(v)) => v.extend(batch.iter().map(|m| small(direction(m)))),
            (F::MsgLabel, Self::Text(v)) => v.extend(batch.iter().map(|m| Some(m.msg_label()))),
            (F::CommandWord, Self::I32(v)) => {
                v.extend(batch.iter().map(|m| word(raw(m.command_word))))
            }
            (F::CommandWord2, Self::I32(v)) => {
                v.extend(batch.iter().map(|m| word(raw(m.command_word_2))))
            }
            (F::StatusWord, Self::I32(v)) => v.extend(batch.iter().map(|m| word(m.status_word))),
            (F::StatusWord2, Self::I32(v)) => v.extend(batch.iter().map(|m| word(m.status_word_2))),
            (F::ErrorWord, Self::I32(v)) => v.extend(batch.iter().map(|m| word(m.error_word))),
            (F::DataWordCount, Self::U8(v)) => {
                v.extend(batch.iter().map(|m| m.data_words.len() as u8))
            }
            (F::Delta, Self::F64(v)) => v.extend(batch.iter().map(|m| m.delta.unwrap_or(f64::NAN))),
            (F::Mux, Self::Text(v)) => {
                v.extend(batch.iter().map(|m| m.mux.as_deref().map(str::to_owned)))
            }
            (F::DataWords, Self::Words(v)) => {
                for m in batch {
                    let w = m.data_words.as_slice();
                    let len = w.len().min(32);
                    let mut row = [0u16; 32];
                    row[..len].copy_from_slice(&w[..len]);
                    v.extend_from_slice(&row);
                }
            }
            _ => unreachable!("a field's column does not match its kind"),
        }
    }

    /// The column as a Python object: a typed `memoryview`, or a `list` of
    /// text.
    fn into_py(self, py: Python<'_>, n: usize) -> PyResult<Bound<'_, PyAny>> {
        match self {
            Self::U64(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "Q", &[n]),
            Self::I64(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "q", &[n]),
            Self::I16(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "h", &[n]),
            Self::U8(v) => view(py, &v, "B", &[n]),
            Self::I8(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "b", &[n]),
            Self::I32(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "i", &[n]),
            Self::Bool(v) => view(py, &ne_bytes(&v, |x| [u8::from(*x)]), "?", &[n]),
            Self::F64(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "d", &[n]),
            // An empty stream's matrix is a 1-D view of length 0 (see
            // `view`); `reshape(-1, 32)` gives (0, 32) from it.
            Self::Words(v) => view(py, &ne_bytes(&v, |x| x.to_ne_bytes()), "H", &[n, 32]),
            Self::Text(v) => Ok(PyList::new(py, v)?.into_any()),
        }
    }
}

/// A typed `memoryview` over `bytes`, cast to `format` and `shape`. The bytes
/// are copied once into a Python `bytes`; NumPy and pandas then wrap the view
/// without copying again.
fn view<'py>(
    py: Python<'py>,
    bytes: &[u8],
    format: &str,
    shape: &[usize],
) -> PyResult<Bound<'py, PyAny>> {
    let raw = PyBytes::new(py, bytes);
    let mv = py
        .import("builtins")?
        .getattr("memoryview")?
        .call1((raw,))?;
    // `memoryview.cast` refuses a zero anywhere in an explicit shape, but
    // casts an empty buffer without one to a 1-D view of length 0.
    if bytes.is_empty() {
        return mv.call_method1("cast", (format,));
    }
    mv.call_method1("cast", (format, shape.to_vec()))
}

/// Every record of `messages` as a dict of column buffers, in schema order.
#[pyfunction]
#[pyo3(signature = (messages, *, fields, standard_tick_rate_hz, year, utc_offset_minutes))]
pub fn columns<'py>(
    py: Python<'py>,
    messages: &Bound<'py, PyAny>,
    fields: Option<Bound<'py, PyAny>>,
    standard_tick_rate_hz: Option<f64>,
    year: Option<i64>,
    utc_offset_minutes: i64,
) -> PyResult<Bound<'py, PyDict>> {
    logbridge::sync_level(py)?;
    let spec = Spec::new(
        py,
        fields.as_ref(),
        standard_tick_rate_hz,
        year,
        utc_offset_minutes,
    )?;
    let (source, slot) = stream::source(messages)?;
    let source = stream::interruptible(source, slot.clone());
    let filled = py.detach(|| {
        let mut cols: Vec<Column> = spec
            .selected()
            .iter()
            .map(|&f| column_for(FIELDS[f].1))
            .collect();
        let mut n = 0usize;
        let mut batch: Vec<MieMessage> = Vec::with_capacity(BATCH);
        let flush = |batch: &mut Vec<MieMessage>, cols: &mut [Column]| {
            for (col, &field) in cols.iter_mut().zip(spec.selected()) {
                col.push_batch(FIELDS[field].2, batch, &spec);
            }
            batch.clear();
        };
        for item in source {
            batch.push(item?);
            n += 1;
            if batch.len() == BATCH {
                flush(&mut batch, &mut cols);
            }
        }
        flush(&mut batch, &mut cols);
        Ok((cols, n))
    });
    let (cols, n) = filled.map_err(|err| stream::raise(py, &slot, err))?;
    let d = PyDict::new(py);
    let keys = keys(py);
    for (col, &field) in cols.into_iter().zip(spec.selected()) {
        d.set_item(keys[field].bind(py), col.into_py(py, n)?)?;
    }
    Ok(d)
}
