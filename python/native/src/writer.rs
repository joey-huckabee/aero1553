// SPDX-License-Identifier: Apache-2.0

//! The CSV writer, for `aero1553.writer`.
//!
//! A file destination is the core crate's `write_csv` / `write_csv_split`
//! unchanged: preflight, atomic temp + rename, `.partial` under
//! `allow_partial`. A stream destination -- any object with a text `write`
//! method, `sys.stdout` included -- is the core crate's `CsvWriter` over an
//! adapter that hands the bytes to that method, so the rows are the same bytes
//! either way.

use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::PoisonError;

use aero1553::error::MieError;
use aero1553::log::{self, Level};
use aero1553::models::{MieMessage, OutputTimeFormat, TimeRender};
use aero1553::writer::{self as core, CSV_HEADER, CsvWriter, WriteOptions, WriteOutcome};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;

use crate::logbridge;
use crate::models::PyMieMessage;
use crate::stream::{self, ErrorSlot};

fn time_render(format: u8, year: Option<u16>, utc_offset_minutes: i16) -> PyResult<TimeRender> {
    let format = match format {
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
        year,
        utc_offset_minutes,
    })
}

/// `(normal_count, error_count, partial)`, where `partial` is
/// `(main_path, errors_path, offset, sync_losses)`. The Python wrapper builds
/// the `WriteOutcome` / `PartialCommit` dataclasses from it.
type Outcome = (u64, u64, Option<(PathBuf, Option<PathBuf>, u64, u64)>);

fn outcome(o: WriteOutcome) -> Outcome {
    (
        o.normal_count,
        o.error_count,
        o.partial
            .map(|p| (p.main_path, p.errors_path, p.offset, p.sync_losses)),
    )
}

/// Whether a Python exception is a consumer closing the pipe.
///
/// Asks `aero1553.writer.is_broken_pipe`, so there is one definition of that
/// rule for Python exceptions (including the Windows `EINVAL`/`EPIPE` case)
/// rather than a copy here that could drift from it.
fn is_broken_pipe(py: Python<'_>, err: &PyErr) -> bool {
    py.import("aero1553.writer")
        .and_then(|m| m.call_method1("is_broken_pipe", (err.value(py),)))
        .and_then(|verdict| verdict.extract::<bool>())
        .unwrap_or(false)
}

/// `Write` over a Python text stream's `write` method.
///
/// Bytes are buffered and handed over in chunks; each hand-over takes the GIL
/// for its own duration. A broken pipe becomes an `io::Error` of kind
/// `BrokenPipe` so the writer can treat it as a clean stop (L2-WRT-018); any
/// other `OSError` becomes an I/O error, reported as `MieWriterError`; any
/// other exception is parked in the stream's error slot and raised unchanged.
///
/// With `pass_through` set, EVERY exception is parked -- a broken pipe and an
/// `OSError` included -- for a caller whose Python contract is that the
/// stream's own exception propagates (`aero1553.dump`).
pub(crate) struct PyTextSink {
    stream: Py<PyAny>,
    buf: Vec<u8>,
    slot: ErrorSlot,
    pass_through: bool,
}

const SINK_CHUNK: usize = 64 * 1024;

impl PyTextSink {
    pub(crate) fn new(stream: Py<PyAny>, slot: ErrorSlot, pass_through: bool) -> Self {
        Self {
            stream,
            buf: Vec::new(),
            slot,
            pass_through,
        }
    }

    fn hand_over(&mut self) -> io::Result<()> {
        if self.buf.is_empty() {
            return Ok(());
        }
        // The CSV is ASCII by construction (L2-CLI-014) apart from a MUX value
        // taken from a file name, which is valid UTF-8 because it came from a
        // Rust `str`; lossy decoding cannot trigger.
        let text = String::from_utf8_lossy(&self.buf).into_owned();
        self.buf.clear();
        Python::attach(
            |py| match self.stream.bind(py).call_method1("write", (text,)) {
                Ok(_) => Ok(()),
                Err(err) => Err(self.classify(py, err)),
            },
        )
    }

    fn classify(&self, py: Python<'_>, err: PyErr) -> io::Error {
        if self.pass_through {
            *self.slot.lock().unwrap_or_else(PoisonError::into_inner) = Some(err);
            return io::Error::other("a Python exception raised by the output stream");
        }
        if is_broken_pipe(py, &err) {
            return io::Error::new(io::ErrorKind::BrokenPipe, err.to_string());
        }
        if err.is_instance_of::<PyOSError>(py) {
            let errno = err
                .value(py)
                .getattr("errno")
                .and_then(|e| e.extract::<Option<i32>>())
                .ok()
                .flatten();
            return errno.map_or_else(
                || io::Error::other(err.to_string()),
                io::Error::from_raw_os_error,
            );
        }
        *self.slot.lock().unwrap_or_else(PoisonError::into_inner) = Some(err);
        io::Error::other("a Python exception raised by the output stream")
    }
}

impl Write for PyTextSink {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.buf.extend_from_slice(data);
        if self.buf.len() >= SINK_CHUNK {
            self.hand_over()?;
        }
        Ok(data.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.hand_over()
    }
}

/// Write every record to one CSV (inline error mode).
///
/// `output` is a path (atomic file write) or a text stream.
#[pyfunction]
#[allow(
    clippy::too_many_arguments,
    reason = "the WriteOptions fields, flattened"
)]
#[pyo3(signature = (
    messages, output, *, destination, input_path, no_clobber, allow_partial,
    time_format, year, utc_offset_minutes,
))]
pub fn write_csv(
    py: Python<'_>,
    messages: &Bound<'_, PyAny>,
    output: &Bound<'_, PyAny>,
    destination: String,
    input_path: Option<PathBuf>,
    no_clobber: bool,
    allow_partial: bool,
    time_format: u8,
    year: Option<u16>,
    utc_offset_minutes: i16,
) -> PyResult<Outcome> {
    logbridge::sync_level(py)?;
    let render = time_render(time_format, year, utc_offset_minutes)?;
    let (source, slot) = stream::source(messages)?;
    if let Ok(path) = output.extract::<PathBuf>() {
        let opts = WriteOptions {
            input_path,
            no_clobber,
            allow_partial,
            time_render: render,
        };
        let result = py.detach(|| core::write_csv(source, Some(&path), opts));
        return result
            .map(outcome)
            .map_err(|err| stream::raise(py, &slot, err));
    }
    // A stream: `sys.stdout` or any object with a text `write` method.
    let sink = output.clone().unbind();
    let counted = py.detach(|| to_stream(source, sink, &slot, &destination, render, allow_partial));
    match counted {
        Ok(rows) => {
            log::emit(
                Level::Info,
                "aero1553::writer",
                format_args!("wrote {rows} rows to {destination}"),
            );
            Ok((rows, 0, None))
        }
        Err(err) => Err(stream::raise(py, &slot, err)),
    }
}

/// Stream rows to a Python text stream, returning how many were written (on a
/// broken pipe, how many were written before it).
fn to_stream(
    source: stream::BoxedStream,
    sink_stream: Py<PyAny>,
    slot: &ErrorSlot,
    destination: &str,
    render: TimeRender,
    allow_partial: bool,
) -> Result<u64, MieError> {
    let sink = PyTextSink::new(sink_stream, slot.clone(), false);
    let mut writer = CsvWriter::new(sink, destination)?.with_time_render(render);
    let mut failure: Option<MieError> = None;
    for item in source {
        let step = match item {
            Ok(msg) => writer.write_message(&msg),
            // A stream has no `.partial`: the rows already sent are what the
            // consumer has seen, so allow_partial simply stops here.
            Err(MieError::UnrecoverableSyncLoss { .. }) if allow_partial => {
                log::emit(
                    Level::Debug,
                    "aero1553::writer",
                    format_args!("unrecoverable sync loss on stream output (--allow-partial)"),
                );
                break;
            }
            Err(err) => Err(err),
        };
        if let Err(err) = step {
            failure = Some(err);
            break;
        }
    }
    let rows = writer.rows_written();
    let finished = match failure {
        // The consumer of a stream has seen every row written before the
        // failure -- the pure-Python writer sent each row as it went -- so what
        // is buffered is handed over before the error surfaces. A failure of
        // that hand-over is secondary to the one being reported.
        Some(err) => {
            let _ = writer.finish();
            Err(err)
        }
        None => writer.finish(),
    };
    match finished {
        Ok(n) => Ok(n),
        // L2-WRT-018: the consumer closed early. A clean stop.
        Err(err) if err.is_broken_pipe() => {
            log::emit(
                Level::Info,
                "aero1553::writer",
                format_args!("stream consumer closed early (broken pipe) -- exit 0"),
            );
            Ok(rows)
        }
        Err(err) => Err(err),
    }
}

/// Write clean records to `output` and errored/spurious ones to
/// `<stem>_errors<suffix>` (separate error mode).
#[pyfunction]
#[allow(
    clippy::too_many_arguments,
    reason = "the WriteOptions fields, flattened"
)]
#[pyo3(signature = (
    messages, output, *, input_path, no_clobber, allow_partial,
    time_format, year, utc_offset_minutes,
))]
pub fn write_csv_split(
    py: Python<'_>,
    messages: &Bound<'_, PyAny>,
    output: PathBuf,
    input_path: Option<PathBuf>,
    no_clobber: bool,
    allow_partial: bool,
    time_format: u8,
    year: Option<u16>,
    utc_offset_minutes: i16,
) -> PyResult<Outcome> {
    logbridge::sync_level(py)?;
    let opts = WriteOptions {
        input_path,
        no_clobber,
        allow_partial,
        time_render: time_render(time_format, year, utc_offset_minutes)?,
    };
    let (source, slot) = stream::source(messages)?;
    py.detach(|| core::write_csv_split(source, &output, opts))
        .map(outcome)
        .map_err(|err| stream::raise(py, &slot, err))
}

/// The CSV header's column names, in order.
#[pyfunction]
pub fn csv_header() -> Vec<&'static str> {
    CSV_HEADER.trim_end().split(',').collect()
}

/// One record as the writer renders it: a cell per column, in column order.
///
/// Produced by the writer itself -- one row written to memory and split -- so
/// a row here is byte-for-byte a row in the file, with the one CSV quoting
/// layer (only MUX, a file-name field, can need it) removed.
#[pyfunction]
#[pyo3(signature = (msg, *, time_format, year, utc_offset_minutes))]
pub fn message_to_row(
    py: Python<'_>,
    msg: &Bound<'_, PyMieMessage>,
    time_format: u8,
    year: Option<u16>,
    utc_offset_minutes: i16,
) -> PyResult<Vec<String>> {
    let render = time_render(time_format, year, utc_offset_minutes)?;
    let record: &MieMessage = &msg.get().inner;
    let mut bytes = Vec::new();
    let mut writer = CsvWriter::new(&mut bytes, "<row>")
        .map(|w| w.with_time_render(render))
        .map_err(|err| crate::errors::to_py(py, err))?;
    writer
        .write_message(record)
        .and_then(|()| writer.finish().map(|_| ()))
        .map_err(|err| crate::errors::to_py(py, err))?;
    let text = String::from_utf8_lossy(&bytes);
    // Line 1 is the header the writer always emits; line 2 is the record.
    let row = text
        .split_once('\n')
        .map_or("", |(_header, rest)| rest)
        .trim_end_matches('\n');
    Ok(split_csv_row(row))
}

/// Split one RFC 4180 line into its cells, undoing the writer's quoting.
fn split_csv_row(line: &str) -> Vec<String> {
    let mut cells = Vec::new();
    let mut cell = String::new();
    let mut chars = line.chars().peekable();
    let mut quoted = false;
    while let Some(ch) = chars.next() {
        match (quoted, ch) {
            (false, ',') => cells.push(std::mem::take(&mut cell)),
            (false, '"') if cell.is_empty() => quoted = true,
            (true, '"') if chars.peek() == Some(&'"') => {
                chars.next();
                cell.push('"');
            }
            (true, '"') => quoted = false,
            (_, other) => cell.push(other),
        }
    }
    cells.push(cell);
    cells
}

/// The paths a run could commit (L2-WRT-014), as the writer derives them.
#[pyfunction]
pub fn commit_targets(output: PathBuf, split_errors: bool, allow_partial: bool) -> Vec<PathBuf> {
    core::commit_targets(&output, split_errors, allow_partial)
}

/// Whether two paths name the same file; `False` when either cannot be
/// resolved (a destination that cannot be resolved cannot collide).
#[pyfunction]
pub fn paths_refer_to_same_file(input_path: PathBuf, output_path: PathBuf) -> bool {
    core::paths_refer_to_same_file(&input_path, &output_path).unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::split_csv_row;

    #[test]
    fn split_undoes_the_writers_quoting() {
        assert_eq!(split_csv_row("a,,b"), vec!["a", "", "b"]);
        assert_eq!(split_csv_row("x,\"a,b\",y"), vec!["x", "a,b", "y"]);
        assert_eq!(split_csv_row("\"say \"\"hi\"\"\","), vec!["say \"hi\"", ""]);
        assert_eq!(split_csv_row(""), vec![""]);
    }
}
