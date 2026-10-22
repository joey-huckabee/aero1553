//! Parallel row rendering for the CSV writer.
//!
//! Turning a decoded record into its CSV row is most of the cost of a decode,
//! and every row is rendered independently of every other: the stateful work
//! (sync, `DELTA`, the canonical-order stage, the merge) has already happened
//! upstream by the time the writer sees a record. So the writer's caller-side
//! thread keeps everything stateful -- it pulls records from the iterator,
//! which is where every log message is emitted, and it writes every byte --
//! while worker threads only render.
//!
//! Records travel in batches of [`BATCH`]. Batch `k` goes to worker
//! `k % workers` and its rendered block is collected from that same worker in
//! the same order, so the output is byte-identical to a serial render. At most
//! `workers * DEPTH` batches are in flight, which keeps memory constant
//! whatever the size of the input.
//!
//! A recording shorter than one batch is rendered on the calling thread and
//! starts no threads at all.

use std::sync::mpsc;
use std::{io, mem, thread};

use crate::error::{MieError, MieResult};
use crate::models::{MieMessage, TimeRender};
use crate::writer::render_message;

/// Records per batch: large enough that channel traffic is noise against the
/// rendering, small enough that `workers * DEPTH` batches stay a few MB.
const BATCH: usize = 2048;

/// Batches queued per worker before the caller waits for the oldest.
const DEPTH: usize = 2;

/// L2-WRT-027 valid range for `output.threads` / `--threads`. `0` renders on
/// the calling thread. Measured on an 8-core machine, the caller's own decode
/// and write are the bottleneck from 2 workers on and 8 add nothing over 4, so
/// the ceiling is a guard against a typo, not a tuning limit.
pub const THREADS_MIN: usize = 0;
pub const THREADS_MAX: usize = 16;

/// Workers the default starts on a machine with cores to spare. Two gave 90 to
/// 95% of the speed-up four did (1.3x to 1.5x against serial rendering, Windows
/// 11, 8 cores), at half the threads and half the in-flight memory, and the
/// same count on every machine is easier to reason about than one that scales
/// with the host.
const DEFAULT_THREADS_CAP: usize = 2;

/// Typical rendered row length, for sizing a block's buffer up front.
const ROW_BYTES: usize = 192;

/// Rows rendered from one batch, in input order. Under `split`, errored and
/// spurious rows go to `errors` and the rest to `main` (L2-WRT-008);
/// otherwise every row is in `main`.
pub(crate) struct Block {
    pub(crate) main: Vec<u8>,
    pub(crate) main_rows: u64,
    pub(crate) errors: Vec<u8>,
    pub(crate) error_rows: u64,
}

/// What a worker sends back: the block, the error that stopped it early (if
/// any), and the emptied batch for the caller to refill.
struct Rendered {
    block: Block,
    failure: Option<MieError>,
    batch: Vec<MieMessage>,
}

/// How filling a batch ended.
enum Fill {
    /// The batch is full; the iterator may have more.
    Full,
    /// The iterator is exhausted.
    End,
    /// The iterator yielded an error, which ends the stream.
    Err(MieError),
}

/// The rendering threads a run starts when none is configured (L2-WRT-027):
/// `min(cores - 1, 2)`, leaving one core for the thread that decodes and
/// writes. A single-core machine renders on the calling thread.
#[must_use]
pub fn default_threads() -> usize {
    thread::available_parallelism()
        .map_or(1, std::num::NonZero::get)
        .saturating_sub(1)
        .min(DEFAULT_THREADS_CAP)
}

fn fill<I>(iter: &mut I, batch: &mut Vec<MieMessage>) -> Fill
where
    I: Iterator<Item = MieResult<MieMessage>>,
{
    while batch.len() < BATCH {
        match iter.next() {
            Some(Ok(msg)) => batch.push(msg),
            Some(Err(e)) => return Fill::Err(e),
            None => return Fill::End,
        }
    }
    Fill::Full
}

/// Render `batch` in order. Stops at the first row that cannot be rendered
/// (a calendar rendering the configured year cannot resolve), returning the
/// rows before it together with that error -- exactly what a serial writer
/// would have written before failing.
fn render_batch(
    batch: &[MieMessage],
    render: TimeRender,
    split: bool,
) -> (Block, Option<MieError>) {
    let mut block = Block {
        main: Vec::with_capacity(batch.len() * ROW_BYTES),
        main_rows: 0,
        errors: Vec::new(),
        error_rows: 0,
    };
    for msg in batch {
        let (buf, rows) = if split && !msg.error_label().is_empty() {
            (&mut block.errors, &mut block.error_rows)
        } else {
            (&mut block.main, &mut block.main_rows)
        };
        if let Err(e) = render_message(buf, msg, render) {
            return (block, Some(e));
        }
        *rows += 1;
    }
    (block, None)
}

/// Render every record of `messages` and hand the rows to `sink` one block at
/// a time, in input order.
///
/// Returns the first error from the iterator, from rendering, or from `sink`,
/// after every row that precedes it has been passed to `sink`. Records after
/// an error are never pulled from the iterator.
///
/// `workers` rendering threads are started (`0` renders on the calling
/// thread); a count above [`THREADS_MAX`] is clamped to it.
pub(crate) fn render_all<I, F>(
    messages: I,
    render: TimeRender,
    split: bool,
    workers: usize,
    mut sink: F,
) -> MieResult<()>
where
    I: IntoIterator<Item = MieResult<MieMessage>>,
    F: FnMut(&Block) -> MieResult<()>,
{
    let mut iter = messages.into_iter();
    let mut batch = Vec::with_capacity(BATCH);
    let state = fill(&mut iter, &mut batch);
    let workers = workers.min(THREADS_MAX);
    if workers == 0 || !matches!(state, Fill::Full) {
        return render_serial(&mut iter, batch, state, render, split, &mut sink);
    }

    thread::scope(|scope| {
        let mut jobs = Vec::with_capacity(workers);
        let mut results = Vec::with_capacity(workers);
        for _ in 0..workers {
            let (job_tx, job_rx) = mpsc::channel::<Vec<MieMessage>>();
            let (result_tx, result_rx) = mpsc::channel::<Rendered>();
            scope.spawn(move || {
                for batch in job_rx {
                    let (block, failure) = render_batch(&batch, render, split);
                    // Sent back full: the records are dropped on the calling
                    // thread, which also cloned them. A record's MUX is one
                    // `Arc` shared by the whole file, and dropping records
                    // here moved its reference count between cores on every
                    // record.
                    if result_tx
                        .send(Rendered {
                            block,
                            failure,
                            batch,
                        })
                        .is_err()
                    {
                        break;
                    }
                }
            });
            jobs.push(job_tx);
            results.push(result_rx);
        }

        let in_flight = workers * DEPTH;
        let mut spare: Vec<Vec<MieMessage>> = Vec::new();
        let (mut sent, mut done) = (0usize, 0usize);
        // Collect batch `done` from its worker and pass it on.
        let mut collect = |done: usize, spare: &mut Vec<Vec<MieMessage>>| -> MieResult<()> {
            let rendered = results[done % workers].recv().map_err(|_| worker_lost())?;
            sink(&rendered.block)?;
            if let Some(e) = rendered.failure {
                return Err(e);
            }
            let mut batch = rendered.batch;
            batch.clear();
            spare.push(batch);
            Ok(())
        };

        let mut state = state;
        let ended = loop {
            if !batch.is_empty() {
                if sent - done == in_flight {
                    collect(done, &mut spare)?;
                    done += 1;
                }
                let next = spare.pop().unwrap_or_else(|| Vec::with_capacity(BATCH));
                jobs[sent % workers]
                    .send(mem::replace(&mut batch, next))
                    .map_err(|_| worker_lost())?;
                sent += 1;
            }
            match state {
                Fill::Full => state = fill(&mut iter, &mut batch),
                Fill::End => break None,
                Fill::Err(e) => break Some(e),
            }
        };
        while done < sent {
            collect(done, &mut spare)?;
            done += 1;
        }
        ended.map_or(Ok(()), Err)
    })
}

/// The single-threaded path: the same batches, rendered on the calling thread.
fn render_serial<I, F>(
    iter: &mut I,
    mut batch: Vec<MieMessage>,
    mut state: Fill,
    render: TimeRender,
    split: bool,
    sink: &mut F,
) -> MieResult<()>
where
    I: Iterator<Item = MieResult<MieMessage>>,
    F: FnMut(&Block) -> MieResult<()>,
{
    loop {
        let (block, failure) = render_batch(&batch, render, split);
        sink(&block)?;
        if let Some(e) = failure {
            return Err(e);
        }
        match state {
            Fill::Full => {
                batch.clear();
                state = fill(iter, &mut batch);
            }
            Fill::End => return Ok(()),
            Fill::Err(e) => return Err(e),
        }
    }
}

/// A rendering thread went away without answering, which only a panic in it
/// can cause; the scope re-raises that panic when it joins.
fn worker_lost() -> MieError {
    MieError::WriterError {
        destination: "row renderer".to_string(),
        source: io::Error::other("a row-rendering thread stopped unexpectedly"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{
        Bus, CommandWord, DataWords, DeltaSeconds, Direction, IrigTimestamp, MessageFormat,
        OptionalWords, OutputTimeFormat, Timestamp, TypeWord,
    };
    use std::cell::Cell;

    /// Record `i`: its day, data and error flag vary with `i`, so a row that
    /// lands out of order or in the wrong file changes the output.
    fn record(i: u32) -> MieMessage {
        let error = i % 7 == 3;
        MieMessage {
            timestamp: Timestamp::Irig(IrigTimestamp {
                day: u16::try_from(1 + i % 365).unwrap_or(1),
                hour: 1,
                minute: 2,
                second: 3,
                microsecond: i % 1_000_000,
                freerun: false,
            }),
            type_word: TypeWord {
                message_type: 0x02,
                bus: Bus::A,
                word_count: 4,
                error,
                raw: if error { 0x6402 } else { 0x2402 },
            },
            message_format: MessageFormat::Receive,
            command_word: Some(CommandWord {
                rt: u8::try_from(i % 31).unwrap_or(0),
                direction: Direction::Receive,
                subaddress: 11,
                data_word_count: 2,
                raw: 0x797E,
            }),
            command_word_2: None,
            optional_words: OptionalWords::new(Some(0x7800), None, None),
            data_words: DataWords::from_slice(&[(i & 0xFFFF) as u16, (i >> 16) as u16]),
            delta: DeltaSeconds::new(f64::from(i) / 1_000_000.0),
            file_offset: u64::from(i),
            mux: None,
        }
    }

    /// Everything `render_all` hands its sink, concatenated.
    #[derive(Debug, PartialEq, Eq, Default)]
    struct Sunk {
        main: Vec<u8>,
        main_rows: u64,
        errors: Vec<u8>,
        error_rows: u64,
    }

    fn run<I>(
        messages: I,
        render: TimeRender,
        split: bool,
        workers: usize,
    ) -> (Sunk, Option<String>)
    where
        I: IntoIterator<Item = MieResult<MieMessage>>,
    {
        let mut sunk = Sunk::default();
        let outcome = render_all(messages, render, split, workers, |b| {
            sunk.main.extend_from_slice(&b.main);
            sunk.main_rows += b.main_rows;
            sunk.errors.extend_from_slice(&b.errors);
            sunk.error_rows += b.error_rows;
            Ok(())
        });
        (sunk, outcome.err().map(|e| e.to_string()))
    }

    const WORKER_COUNTS: [usize; 4] = [1, 2, 3, 8];

    /// Requirements: L2-WRT-027, L3-RS-019
    #[test]
    fn threaded_output_is_byte_identical_to_serial() {
        // Not a multiple of BATCH, so the last batch is a short one.
        let n = u32::try_from(BATCH * 9 + 77).unwrap_or(u32::MAX);
        for split in [false, true] {
            let (serial, err) = run((0..n).map(|i| Ok(record(i))), TimeRender::doy(), split, 0);
            assert_eq!(err, None);
            assert_eq!(serial.main_rows + serial.error_rows, u64::from(n));
            assert_eq!(serial.error_rows > 0, split);
            for workers in WORKER_COUNTS {
                let (threaded, err) = run(
                    (0..n).map(|i| Ok(record(i))),
                    TimeRender::doy(),
                    split,
                    workers,
                );
                assert_eq!(err, None);
                assert!(
                    threaded == serial,
                    "split={split} workers={workers} differs"
                );
            }
        }
    }

    /// A row the configured year cannot place stops the run there: every row
    /// before it is passed on, none after it, whatever the thread count.
    ///
    /// Requirements: L2-WRT-027, L2-WRT-026
    #[test]
    fn a_rendering_failure_stops_at_the_same_row_threaded_or_not() {
        let bad = u32::try_from(BATCH * 3 + 5).unwrap_or(0);
        let n = bad * 2;
        let messages = move || {
            (0..n).map(move |i| {
                let mut m = record(i);
                if let Timestamp::Irig(t) = &mut m.timestamp {
                    // 2025 has no day 366.
                    t.day = if i == bad { 366 } else { 1 };
                }
                Ok(m)
            })
        };
        let render = TimeRender {
            format: OutputTimeFormat::Iso,
            year: Some(2025),
            utc_offset_minutes: 0,
        };
        let (serial, err) = run(messages(), render, false, 0);
        assert_eq!(serial.main_rows, u64::from(bad));
        assert!(err.as_deref().unwrap_or("").contains("366"), "{err:?}");
        for workers in WORKER_COUNTS {
            let (threaded, terr) = run(messages(), render, false, workers);
            assert!(threaded == serial, "workers={workers} differs");
            assert_eq!(terr, err);
        }
    }

    /// An error from the iterator ends the stream after every record before it
    /// is passed on, and nothing after it is pulled.
    ///
    /// Requirements: L2-WRT-027, L2-WRT-016
    #[test]
    fn an_iterator_error_ends_the_stream_after_the_rows_before_it() {
        let stop = u32::try_from(BATCH * 4 + 11).unwrap_or(0);
        for workers in [0, 1, 3, 8] {
            let pulled = Cell::new(0u32);
            let messages = (0..stop * 2).map(|i| {
                pulled.set(i + 1);
                if i == stop {
                    Err(MieError::UnrecoverableSyncLoss {
                        offset: u64::from(i),
                        sync_losses: 1,
                    })
                } else {
                    Ok(record(i))
                }
            });
            let (sunk, err) = run(messages, TimeRender::doy(), false, workers);
            assert_eq!(sunk.main_rows, u64::from(stop), "workers={workers}");
            assert!(err.is_some(), "workers={workers}");
            assert_eq!(
                pulled.get(),
                stop + 1,
                "workers={workers} read past the error"
            );
        }
    }
}
