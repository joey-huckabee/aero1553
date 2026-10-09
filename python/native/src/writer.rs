// SPDX-License-Identifier: Apache-2.0

//! The CSV writer, for `aero1553.writer`.
//!
//! A file destination is the core crate's `write_csv` / `write_csv_split`
//! unchanged: preflight, atomic temp + rename, `.partial` under
//! `allow_partial`. A stream destination -- any object with a text `write`
//! method, `sys.stdout` included -- is the core crate's `CsvWriter` over an
//! adapter that hands the bytes to the stream's binary layer where it has one
//! and to that method where it does not, so the rows are the same bytes
//! either way.

use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::PoisonError;

use aero1553::error::MieError;
use aero1553::log::{self, Level};
use aero1553::models::{MieMessage, TimeRender};
use aero1553::writer::{self as core, CSV_HEADER, CsvWriter, WriteOptions, WriteOutcome};
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::logbridge;
use crate::models::{PyMieMessage, render_from};
use crate::stream::{self, ErrorSlot};

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

/// `Write` over a Python text stream.
///
/// Bytes are buffered and handed over in chunks; each hand-over takes the GIL
/// for its own duration. A broken pipe becomes an `io::Error` of kind
/// `BrokenPipe` so the writer can treat it as a clean stop (L2-WRT-018); any
/// other `OSError` becomes an I/O error, reported as `MieWriterError`; any
/// other exception is parked in the stream's error slot and raised unchanged.
///
/// A stream with a binary layer under it (`stream.buffer`, as `sys.stdout` and
/// every `open(path, "w")` file have) is given the bytes themselves, through
/// that layer, so they are the CLI's bytes whatever the stream's newline and
/// encoding settings (L3-PY-023). Handing it `str` instead let a text stream
/// on Windows turn every `\n` into `\r\n`, against the LF rule of L2-WRT-012.
/// A stream without one (`io.StringIO`, a notebook's output stream, any object
/// with a `write` method) is given `str`, as before.
///
/// With `pass_through` set, EVERY exception is parked -- a broken pipe and an
/// `OSError` included -- for a caller whose Python contract is that the
/// stream's own exception propagates (`aero1553.dump`).
pub(crate) struct PyTextSink {
    stream: Py<PyAny>,
    target: Target,
    buf: Vec<u8>,
    slot: ErrorSlot,
    pass_through: bool,
}

/// Where a hand-over goes: decided on the first one, with the GIL held.
enum Target {
    Unresolved,
    /// The stream's own `write`, given `str`.
    Text,
    /// The binary layer under the stream, given bytes.
    Binary(Py<PyAny>),
}

const SINK_CHUNK: usize = 64 * 1024;

/// The binary layer under `stream`, if it has one, with the text layer flushed
/// so that whatever the caller wrote to the stream before this call comes out
/// first.
///
/// Only a real `io` binary stream counts: an attribute that merely happens to
/// be called `buffer` is not a promise to accept bytes.
fn binary_layer<'py>(
    py: Python<'py>,
    stream: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    // A missing attribute, or a detached stream's `ValueError`, alike mean
    // there is no binary layer to write to.
    let Ok(buffer) = stream.getattr("buffer") else {
        return Ok(None);
    };
    let io = py.import("io")?;
    let binary = buffer.is_instance(&io.getattr("BufferedIOBase")?)?
        || buffer.is_instance(&io.getattr("RawIOBase")?)?;
    if !binary {
        return Ok(None);
    }
    stream.call_method0("flush")?;
    Ok(Some(buffer))
}

impl PyTextSink {
    pub(crate) fn new(stream: Py<PyAny>, slot: ErrorSlot, pass_through: bool) -> Self {
        Self {
            stream,
            target: Target::Unresolved,
            buf: Vec::new(),
            slot,
            pass_through,
        }
    }

    fn hand_over(&mut self) -> io::Result<()> {
        if self.buf.is_empty() {
            return Ok(());
        }
        Python::attach(|py| {
            let sent = self.send(py);
            self.buf.clear();
            sent.map_err(|err| self.classify(py, err))
        })
    }

    fn send(&mut self, py: Python<'_>) -> PyResult<()> {
        if matches!(self.target, Target::Unresolved) {
            self.target = match binary_layer(py, self.stream.bind(py))? {
                Some(buffer) => Target::Binary(buffer.unbind()),
                None => Target::Text,
            };
        }
        let Target::Binary(buffer) = &self.target else {
            // The CSV is ASCII by construction (L2-CLI-014) apart from a MUX
            // value taken from a file name, which is valid UTF-8 because it
            // came from a Rust `str`; lossy decoding cannot trigger.
            let text = String::from_utf8_lossy(&self.buf);
            self.stream.bind(py).call_method1("write", (text,))?;
            return Ok(());
        };
        // A raw layer (`python -u`'s stdout) may take only part of a write.
        let buffer = buffer.bind(py);
        let mut rest: &[u8] = &self.buf;
        while !rest.is_empty() {
            let taken: Option<usize> = buffer
                .call_method1("write", (PyBytes::new(py, rest),))?
                .extract()?;
            match taken {
                Some(n) if n > 0 => rest = &rest[n.min(rest.len())..],
                _ => return Err(PyOSError::new_err("the output stream accepted no bytes")),
            }
        }
        Ok(())
    }

    /// Flush the binary layer, so that whatever the caller writes to the
    /// stream after this call comes out after it. A text target is the
    /// caller's to flush, as it always was.
    fn flush_target(&self) -> io::Result<()> {
        let Target::Binary(buffer) = &self.target else {
            return Ok(());
        };
        Python::attach(|py| {
            buffer
                .bind(py)
                .call_method0("flush")
                .map(drop)
                .map_err(|err| self.classify(py, err))
        })
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
        self.hand_over()?;
        self.flush_target()
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
    year: Option<i64>,
    utc_offset_minutes: i64,
) -> PyResult<Outcome> {
    logbridge::sync_level(py)?;
    let render = render_from(time_format, year, utc_offset_minutes)?;
    let (source, slot) = stream::source(messages)?;
    let source = stream::interruptible(source, slot.clone());
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
    // A stream: `sys.stdout` or any object with a text `write` method. It has
    // no `.partial` to commit, so `allow_partial` does not apply: a sync loss
    // fails the call after the rows before it are handed over, exactly as the
    // CLI's stdout output exits 3 with those rows printed (L3-PY-022).
    let sink = output.clone().unbind();
    let counted = py.detach(|| to_stream(source, sink, &slot, &destination, render));
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
    source: stream::Interruptible,
    sink_stream: Py<PyAny>,
    slot: &ErrorSlot,
    destination: &str,
    render: TimeRender,
) -> Result<u64, MieError> {
    let sink = PyTextSink::new(sink_stream, slot.clone(), false);
    let mut writer = CsvWriter::new(sink, destination)?.with_time_render(render);
    let mut failure: Option<MieError> = None;
    for item in source {
        if let Err(err) = item.and_then(|msg| writer.write_message(&msg)) {
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
    year: Option<i64>,
    utc_offset_minutes: i64,
) -> PyResult<Outcome> {
    logbridge::sync_level(py)?;
    let opts = WriteOptions {
        input_path,
        no_clobber,
        allow_partial,
        time_render: render_from(time_format, year, utc_offset_minutes)?,
    };
    let (source, slot) = stream::source(messages)?;
    let source = stream::interruptible(source, slot.clone());
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
    year: Option<i64>,
    utc_offset_minutes: i64,
) -> PyResult<Vec<String>> {
    let render = render_from(time_format, year, utc_offset_minutes)?;
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
