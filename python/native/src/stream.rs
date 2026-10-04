// SPDX-License-Identifier: Apache-2.0

//! Record streams: what the reader hands out and what every pipeline stage
//! (`apply_filters`, `order_rows`, and later the writer and the merge) takes.
//!
//! One Python class, `RecordIterator`, wraps a boxed Rust iterator of
//! `MieResult<MieMessage>`. A stage given a `RecordIterator` takes its Rust
//! iterator over directly -- the chain `reader -> filters -> order -> writer`
//! runs in Rust with no Python object between stages. Given any other Python
//! iterable, it pulls records through Python instead.
//!
//! A Python iterable can raise anything, and the core crate's stages carry
//! only `MieError`. So a Python source translates the one exception a stage
//! acts on -- `MieUnrecoverableSyncLossError`, which the writer turns into a
//! `.partial` commit under `allow_partial` -- and parks every other exception
//! in the stream's error slot, handing the stage a placeholder error. When
//! that placeholder surfaces, the parked exception is raised instead: the same
//! object the Python code raised.

use std::sync::{Arc, Mutex, PoisonError};

use aero1553::error::{MieError, MieResult};
use aero1553::models::MieMessage;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::PyIterator;

use crate::errors;
use crate::models::PyMieMessage;

pub type Item = MieResult<MieMessage>;
pub type BoxedStream = Box<dyn Iterator<Item = Item> + Send + Sync>;

/// Where a Python source parks an exception the Rust stages cannot carry.
/// Shared by every stage of one chain, so whichever stage surfaces the
/// placeholder raises the original.
pub type ErrorSlot = Arc<Mutex<Option<PyErr>>>;

pub fn new_slot() -> ErrorSlot {
    Arc::new(Mutex::new(None))
}

/// The placeholder a stage sees in place of a parked Python exception. Never
/// shown: the slot is checked first wherever a stream error becomes Python's.
fn parked() -> MieError {
    MieError::PayloadError {
        offset: 0,
        detail: "a Python exception raised by the record source".into(),
    }
}

/// How many records pass between checks for a pending signal. A decode runs
/// with the GIL released, so nothing else notices Ctrl-C until it returns; at
/// a few million records a second this is a check every millisecond or so, at
/// a cost that does not register against the decode itself.
const SIGNAL_CHECK_EVERY: u32 = 1024;

/// Wrap `stream` so a pending signal -- Ctrl-C's `KeyboardInterrupt` -- stops
/// it.
///
/// A long-running call (`write_csv`, `write_csv_split`, `columns`) releases the
/// GIL, and CPython runs its signal handlers only between bytecodes, so Ctrl-C
/// used to wait for the whole call -- and a file destination was COMMITTED
/// before the `KeyboardInterrupt` surfaced, leaving a complete-looking output
/// from a run the user had cancelled. Every `SIGNAL_CHECK_EVERY` records this
/// takes the GIL and asks Python whether a signal is pending; if one is, its
/// exception is parked and the stream ends in an error, which every consumer
/// treats as a failure: the writer abandons its temp file without committing,
/// and the parked exception -- the `KeyboardInterrupt` itself -- is what the
/// caller sees.
///
/// The wrapper is returned as its own type, not re-boxed: the consumers are
/// generic over their iterator, so its `next` inlines into their loops and a
/// record pays for a counter increment, not a second indirect call.
pub fn interruptible(stream: BoxedStream, slot: ErrorSlot) -> Interruptible {
    Interruptible {
        inner: stream,
        slot,
        since_check: 0,
        done: false,
    }
}

pub struct Interruptible {
    inner: BoxedStream,
    slot: ErrorSlot,
    since_check: u32,
    done: bool,
}

impl Iterator for Interruptible {
    type Item = Item;

    #[inline]
    fn next(&mut self) -> Option<Item> {
        if self.done {
            return None;
        }
        self.since_check += 1;
        if self.since_check >= SIGNAL_CHECK_EVERY {
            self.since_check = 0;
            if let Err(err) = Python::attach(|py| py.check_signals()) {
                self.done = true;
                *self.slot.lock().unwrap_or_else(PoisonError::into_inner) = Some(err);
                return Some(Err(parked()));
            }
        }
        self.inner.next()
    }
}

/// Turn a stream error into the exception to raise: the parked Python
/// exception if there is one, otherwise the decoder error's own class.
pub fn raise(py: Python<'_>, slot: &ErrorSlot, err: MieError) -> PyErr {
    let parked = slot.lock().unwrap_or_else(PoisonError::into_inner).take();
    parked.unwrap_or_else(|| errors::to_py(py, err))
}

/// A decoded record stream, as Python sees it.
#[pyclass(name = "RecordIterator", module = "aero1553._native")]
pub struct PyRecordIterator {
    /// `None` once a stage has taken the stream over: the iterator is then
    /// consumed, as any Python iterator is once another has read from it.
    stream: Option<BoxedStream>,
    slot: ErrorSlot,
}

impl PyRecordIterator {
    pub fn new(stream: BoxedStream, slot: ErrorSlot) -> Self {
        Self {
            stream: Some(stream),
            slot,
        }
    }
}

#[pymethods]
impl PyRecordIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Stop early and release the source, as a generator's ``close()`` does.
    ///
    /// Anything buffered (an ordering stage's pending run) is discarded, not
    /// flushed: a consumer that closed wants no further records. Releasing the
    /// source also releases the file mapping it holds. Closing twice is fine.
    fn close(&mut self) {
        self.stream = None;
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<PyMieMessage>> {
        let Some(stream) = self.stream.as_mut() else {
            return Ok(None);
        };
        match stream.next() {
            Some(Ok(inner)) => Ok(Some(PyMieMessage { inner })),
            // Every error ends the stream, as a generator that raised ends.
            Some(Err(err)) => {
                self.stream = None;
                Err(raise(py, &self.slot, err))
            }
            None => {
                self.stream = None;
                Ok(None)
            }
        }
    }
}

/// A Python iterable pulled from Rust, one record at a time.
struct PySource {
    iter: Py<PyIterator>,
    slot: ErrorSlot,
    done: bool,
}

impl Iterator for PySource {
    type Item = Item;

    #[inline]
    fn next(&mut self) -> Option<Item> {
        if self.done {
            return None;
        }
        Python::attach(|py| {
            let step = self.iter.bind(py).clone().next();
            match step {
                None => {
                    self.done = true;
                    None
                }
                Some(Ok(obj)) => match obj.cast::<PyMieMessage>() {
                    Ok(msg) => Some(Ok(msg.get().inner.clone())),
                    Err(_) => {
                        self.done = true;
                        let err = PyTypeError::new_err(format!(
                            "expected aero1553.models.MieMessage, got {}",
                            obj.get_type()
                                .name()
                                .map_or_else(|_| "?".to_string(), |n| n.to_string())
                        ));
                        Some(Err(self.park(err)))
                    }
                },
                Some(Err(err)) => {
                    self.done = true;
                    Some(Err(self.translate(py, err)))
                }
            }
        })
    }
}

impl PySource {
    fn park(&self, err: PyErr) -> MieError {
        *self.slot.lock().unwrap_or_else(PoisonError::into_inner) = Some(err);
        parked()
    }

    /// `MieUnrecoverableSyncLossError` and `MieMergeInputsDroppedError` become
    /// the decoder errors they stand for, because the writer acts on them (it
    /// commits a `.partial`); anything else is parked.
    fn translate(&self, py: Python<'_>, err: PyErr) -> MieError {
        let sync_loss = py
            .import("aero1553.exceptions")
            .and_then(|m| m.getattr("MieUnrecoverableSyncLossError"));
        if let Ok(class) = sync_loss {
            let value = err.value(py);
            if value.is_instance(&class).unwrap_or(false) {
                let fields = value
                    .getattr("offset")
                    .and_then(|o| o.extract::<u64>())
                    .and_then(|offset| {
                        let losses = value.getattr("sync_losses")?.extract::<u64>()?;
                        Ok((offset, losses))
                    });
                if let Ok((offset, sync_losses)) = fields {
                    return MieError::UnrecoverableSyncLoss {
                        offset,
                        sync_losses,
                    };
                }
            }
        }
        // `MieMergeInputsDroppedError` likewise ends a partial merge in a
        // `.partial` (L2-MRG-004), so it too becomes the error it stands for.
        let dropped = py
            .import("aero1553.exceptions")
            .and_then(|m| m.getattr("MieMergeInputsDroppedError"));
        if let Ok(class) = dropped {
            let value = err.value(py);
            if value.is_instance(&class).unwrap_or(false) {
                let count = |name: &str| value.getattr(name).and_then(|v| v.extract::<u64>());
                if let (Ok(left_out), Ok(truncated), Ok(total)) =
                    (count("left_out"), count("truncated"), count("total"))
                {
                    return MieError::MergeInputsDropped {
                        left_out,
                        truncated,
                        total,
                    };
                }
            }
        }
        self.park(err)
    }
}

/// The records `obj` yields, as a Rust stream, with the error slot to raise
/// through. A `RecordIterator` is taken over whole (and left consumed);
/// anything else is iterated through Python.
pub fn source(obj: &Bound<'_, PyAny>) -> PyResult<(BoxedStream, ErrorSlot)> {
    if let Some(taken) = take_native(obj) {
        return Ok(taken);
    }
    // Not a stream itself, but perhaps something whose iterator is one -- a
    // `MieFileReader`, whose `__iter__` hands out the native stream. Taking
    // that over keeps `write_csv(reader)` or `order_rows(reader)` in Rust;
    // iterating it through Python instead built a Python object per record
    // only to convert it straight back (measured: +17% on a whole decode).
    let iter = obj.try_iter()?;
    if let Some(taken) = take_native(&iter) {
        return Ok(taken);
    }
    let slot = new_slot();
    let source = PySource {
        iter: iter.unbind(),
        slot: slot.clone(),
        done: false,
    };
    Ok((Box::new(source), slot))
}

/// Take a native `RecordIterator`'s stream over, if `obj` is one.
fn take_native(obj: &Bound<'_, PyAny>) -> Option<(BoxedStream, ErrorSlot)> {
    let native = obj.cast::<PyRecordIterator>().ok()?;
    let mut native = native.borrow_mut();
    let stream = native
        .stream
        .take()
        .unwrap_or_else(|| Box::new(std::iter::empty()));
    Some((stream, native.slot.clone()))
}
