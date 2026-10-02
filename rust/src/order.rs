//! Canonical CSV row ordering (L1-OUT-003, L2-WRT-021, L2-WRT-022).
//!
//! Rows leave the decoder in a canonical order: ascending `TIME_STAMP`, then
//! ascending `RT`, then `MSG` — subaddress ascending and, within one
//! subaddress, receive (`R`) before transmit (`T`). The upstream stages already
//! deliver ascending timestamps (a single file is in chronological capture
//! order; a merge is heap-ordered by absolute IRIG microseconds), so this stage
//! is only responsible for the **tie**: it buffers each run of consecutive
//! records sharing one timestamp and stable-sorts that run by `(RT, subaddress,
//! direction)`.
//!
//! Two properties make this safe to run over a streaming pipeline:
//!
//! - It **never reorders across a timestamp boundary**. Only a run of
//!   consecutive equal timestamps is permuted, so a non-monotonic input
//!   (L2-MRG-006, which forbids re-sorting) is left exactly as it arrived: the
//!   stream `T, T, U, T` sorts the leading pair and leaves the trailing `T`
//!   where it is.
//! - Buffering is capped by `max_group` (L2-WRT-022). A corrupt recording whose
//!   timestamps all decode to one value would otherwise buffer the whole file;
//!   at the cap the run is flushed in arrival order with one WARN and decoding
//!   continues.
//!
//! `SPURIOUS_DATA` records carry no Command Word, so they have no `RT`/`MSG` to
//! sort on. They are **pinned**: excluded from the permutation and re-emitted at
//! their original offset within the run. That preserves the adjacency the
//! `0x2000` "continuation of a preceding error" code is defined in terms of
//! (L2-ERR-005) — separating a spurious record from its parent error would leave
//! that code describing nothing.
//!
//! DELTA needs no recomputation downstream of this stage: it is tracked per
//! `RT`/`MSG` key, and two records in one run that share a key also share a
//! timestamp, so their gap is zero regardless of their relative order. The
//! reorder is DELTA-invariant by construction.

use crate::models::{Direction, MieMessage, Timestamp};

/// Default cap on one buffered equal-timestamp run (L2-WRT-022). Far above any
/// real tie — a 1553 bus carries one transaction at a time, so genuine ties come
/// from the two concurrent buses or from overlapping recorders in a merge —
/// while bounding worst-case buffering to a few hundred kilobytes. Shared in
/// value with the Python implementation (L3-WRT-003).
pub const DEFAULT_MAX_SORT_GROUP: usize = 4096;

/// Valid range for `output.max_sort_group` / `--max-sort-group` (L3-WRT-003).
/// `1` disables reordering entirely (every run is already at the cap), which is
/// the supported way to restore raw DDC capture order for a vendor-CSV diff.
pub const MAX_SORT_GROUP_MIN: usize = 1;
pub const MAX_SORT_GROUP_MAX: usize = 1_048_576;

/// The canonical tie-break key: `(RT, subaddress, direction)`, with direction
/// `0` = receive and `1` = transmit so `R` orders before `T`.
type SortKey = (u8, u8, u8);

/// Sort key for one record, or `None` for a record with no Command Word
/// (`SPURIOUS_DATA`), which is pinned rather than sorted.
///
/// Keying on the decoded fields — not on the rendered `MSG` string — is
/// deliberate: `"11R"` sorts before `"2R"` lexicographically, which is not the
/// required order. `Direction::Receive` is `0` and `Transmit` is `1`, so
/// R-before-T falls out of the discriminants and cannot drift from Python's
/// `IntEnum`.
fn sort_key(msg: &MieMessage) -> Option<SortKey> {
    msg.command_word.map(|cw| {
        let dir = match cw.direction {
            Direction::Receive => 0u8,
            Direction::Transmit => 1u8,
        };
        (cw.rt, cw.subaddress, dir)
    })
}

/// True when two timestamps are the same row-ordering group. Compares the
/// **variant** as well as the value: a mixed IRIG/Standard stream is impossible
/// today (the format is resolved once per file, and a merge rejects mixed sets),
/// so an equality test on the value alone would be silently wrong if that ever
/// changed.
fn same_group(a: &Timestamp, b: &Timestamp) -> bool {
    match (a, b) {
        (Timestamp::Irig(x), Timestamp::Irig(y)) => {
            x.to_total_microseconds() == y.to_total_microseconds()
        }
        (Timestamp::Standard(x), Timestamp::Standard(y)) => x.raw_value == y.raw_value,
        _ => false,
    }
}

/// Stable-sort one buffered run, keeping each Command-Word-less record attached
/// to the record it followed on input.
///
/// The run is first split into **chunks**: one sortable "anchor" record plus any
/// pinned records trailing it. Chunks — not individual records — are then sorted
/// by the anchor's key, so a pin travels with its anchor.
///
/// Preserving a pin's *index* is not sufficient and was the original bug here: a
/// pin held at index 1 while sorting moved a different record into index 0 ends
/// up trailing the wrong record, which breaks exactly the `0x2000`
/// error-continuation adjacency the pin exists to protect. A pin's position is
/// defined relative to its predecessor, so the predecessor has to carry it.
///
/// Pins arriving before any anchor (a run that opens with `SPURIOUS_DATA`) form a
/// leading chunk keyed `None`, which sorts ahead of every `Some` — keeping them
/// at the front of the run, where they arrived.
fn sort_run(buf: Vec<MieMessage>) -> Vec<MieMessage> {
    /// One anchor record plus the pinned records trailing it.
    type Chunk = (Option<SortKey>, Vec<MieMessage>);
    let mut chunks: Vec<Chunk> = Vec::new();
    for msg in buf {
        match sort_key(&msg) {
            Some(k) => chunks.push((Some(k), vec![msg])),
            None => match chunks.last_mut() {
                Some((_, group)) => group.push(msg),
                None => chunks.push((None, vec![msg])),
            },
        }
    }
    if chunks.len() > 1 {
        // `sort_by_key` is stable, so chunks with a fully equal key keep their
        // relative input order (L1-OUT-003). `None` (leading pins) sorts first.
        chunks.sort_by_key(|(k, _)| *k);
    }
    chunks.into_iter().flat_map(|(_, group)| group).collect()
}

/// Iterator adapter imposing canonical row order on a decoded message stream
/// (L2-WRT-021). Generic over the error type so it composes with both the
/// reader's and the merge's item types.
///
/// Positioned as the last adapter before the writer. An `Err` item passes
/// through in position, but only after the buffered run is flushed, so an
/// `--allow-partial` run never loses a buffered group from its committed
/// `.partial`.
pub struct Ordered<I, E> {
    inner: I,
    /// The run of consecutive equal-timestamp records being accumulated.
    buf: Vec<MieMessage>,
    /// Records already sorted and awaiting emission, in order, held reversed so
    /// `pop` yields the front.
    out: Vec<MieMessage>,
    /// L2-WRT-022 cap on `buf`.
    max_group: usize,
    /// Set once the inner iterator is exhausted so the final run is flushed
    /// exactly once.
    done: bool,
    /// An `Err` observed while a run was buffered, surfaced after the flush.
    pending: Option<E>,
}

impl<I, E> Ordered<I, E> {
    /// Move the buffered run into the emission queue, sorting it first.
    fn flush(&mut self) {
        if self.buf.is_empty() {
            return;
        }
        let run = sort_run(std::mem::take(&mut self.buf));
        // Reversed so `Vec::pop` emits front-to-back.
        self.out.extend(run.into_iter().rev());
    }

    /// Add `msg` to the run being accumulated.
    ///
    /// Flushes first if `msg` opens a *new* equal-timestamp run, and again
    /// afterwards if the run has reached the L2-WRT-022 cap. Split out of
    /// `next` so that method carries only the iteration control flow (drain,
    /// pending error, exhaustion) and this one carries only the run-boundary
    /// rule — each is then readable on its own.
    fn accept(&mut self, msg: MieMessage) {
        let starts_new_run = self
            .buf
            .first()
            .is_some_and(|head| !same_group(&head.timestamp, &msg.timestamp));
        if starts_new_run {
            self.flush();
        }
        self.buf.push(msg);
        if self.buf.len() >= self.max_group {
            self.flush_capped();
        }
    }

    /// Flush a run that hit the L2-WRT-022 cap: emitted in arrival order, with
    /// one WARN naming the timestamp and the cap.
    fn flush_capped(&mut self) {
        crate::log_warn!(
            "equal-timestamp run at {} reached the {}-record max_sort_group cap; \
             emitting this run in arrival order (raise [output] max_sort_group / \
             --max-sort-group to restore canonical RT/MSG order for it)",
            self.buf
                .first()
                .map(|m| m.timestamp.format())
                .unwrap_or_default(),
            self.max_group
        );
        self.out.extend(self.buf.drain(..).rev());
    }
}

impl<I, E> Iterator for Ordered<I, E>
where
    I: Iterator<Item = Result<MieMessage, E>>,
{
    type Item = Result<MieMessage, E>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            // Drain anything already ordered before touching the inner stream.
            if let Some(msg) = self.out.pop() {
                return Some(Ok(msg));
            }
            if let Some(e) = self.pending.take() {
                return Some(Err(e));
            }
            if self.done {
                return None;
            }
            match self.inner.next() {
                None => {
                    self.done = true;
                    self.flush();
                }
                Some(Err(e)) => {
                    // Flush first, then surface the error on the next call, so
                    // the buffered rows reach the writer ahead of the failure.
                    self.pending = Some(e);
                    self.flush();
                }
                Some(Ok(msg)) => self.accept(msg),
            }
        }
    }
}

/// Extension trait: `iter.order_rows(max_group)`.
pub trait OrderIterExt<E>: Sized {
    fn order_rows(self, max_group: usize) -> Ordered<Self, E>;
}

impl<I, E> OrderIterExt<E> for I
where
    I: Iterator<Item = Result<MieMessage, E>>,
{
    fn order_rows(self, max_group: usize) -> Ordered<Self, E> {
        Ordered {
            inner: self,
            buf: Vec::new(),
            out: Vec::new(),
            // A zero cap would flush every push and could never make progress
            // on a run; clamp defensively even though config validation
            // (L2-WRT-022) already rejects it.
            max_group: max_group.max(MAX_SORT_GROUP_MIN),
            done: false,
            pending: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;

    /// IRIG record at `us` microseconds past day 0 with the given RT / SA /
    /// direction. `us` is placed in the microsecond field for values under a
    /// second, which is all these tests need.
    fn rec(us: u32, rt: u8, sa: u8, dir: Direction) -> MieMessage {
        MieMessage {
            timestamp: Timestamp::Irig(IrigTimestamp {
                day: 1,
                hour: 0,
                minute: 0,
                second: 0,
                microsecond: us,
                freerun: false,
            }),
            type_word: TypeWord {
                message_type: 0x02,
                bus: Bus::A,
                word_count: 1,
                error: false,
                raw: 0,
            },
            message_format: MessageFormat::Receive,
            command_word: Some(CommandWord {
                rt,
                direction: dir,
                subaddress: sa,
                data_word_count: 1,
                raw: 0,
            }),
            command_word_2: None,
            status_word: None,
            status_word_2: None,
            data_words: DataWords::new(),
            error_word: None,
            delta: Some(0.0),
            file_offset: 0,
            mux: None,
        }
    }

    /// A `SPURIOUS_DATA` record (no Command Word) at `us`.
    fn spurious(us: u32) -> MieMessage {
        let mut m = rec(us, 0, 0, Direction::Receive);
        m.command_word = None;
        m.message_format = MessageFormat::SpuriousData;
        m.type_word.message_type = 0x20;
        m
    }

    /// An errored record at `us` (Type Word bit 14), used to test that its
    /// spurious continuation stays adjacent.
    fn errored(us: u32, rt: u8, sa: u8) -> MieMessage {
        let mut m = rec(us, rt, sa, Direction::Receive);
        m.type_word.error = true;
        m.error_word = Some(crate::models::ERROR_MANCHESTER_PARITY);
        m
    }

    /// Run the adapter over `msgs` and return `(rt, sa, dir)` triples, with
    /// `None` for a pinned spurious record.
    fn ordered(msgs: Vec<MieMessage>, cap: usize) -> Vec<Option<SortKey>> {
        msgs.into_iter()
            .map(Ok::<_, ()>)
            .order_rows(cap)
            .map(|r| sort_key(&r.unwrap()))
            .collect()
    }

    /// Requirements: L1-OUT-003, L2-WRT-021
    #[test]
    fn sorts_tied_rows_by_rt() {
        let got = ordered(
            vec![
                rec(10, 21, 3, Direction::Receive),
                rec(10, 3, 3, Direction::Receive),
                rec(10, 15, 3, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(
            got,
            vec![Some((3, 3, 0)), Some((15, 3, 0)), Some((21, 3, 0))]
        );
    }

    /// Requirements: L1-OUT-003, L2-WRT-021
    #[test]
    fn sorts_tied_rows_by_subaddress_within_rt() {
        let got = ordered(
            vec![
                rec(10, 5, 11, Direction::Receive),
                rec(10, 5, 2, Direction::Receive),
                rec(10, 5, 31, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        // Numeric, not lexicographic: 2 < 11 < 31 (as strings, "11" < "2").
        assert_eq!(
            got,
            vec![Some((5, 2, 0)), Some((5, 11, 0)), Some((5, 31, 0))]
        );
    }

    /// Requirements: L1-OUT-003, L2-WRT-021
    #[test]
    fn receive_orders_before_transmit_at_equal_subaddress() {
        let got = ordered(
            vec![
                rec(10, 5, 7, Direction::Transmit),
                rec(10, 5, 7, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(got, vec![Some((5, 7, 0)), Some((5, 7, 1))]);
    }

    /// All three key levels at once, plus R-before-T, from a deliberately
    /// wrong input order.
    ///
    /// Requirements: L1-OUT-003, L2-WRT-021
    #[test]
    fn full_key_ordering() {
        let got = ordered(
            vec![
                rec(10, 21, 3, Direction::Receive),
                rec(10, 3, 11, Direction::Transmit),
                rec(10, 3, 11, Direction::Receive),
                rec(10, 3, 2, Direction::Transmit),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(
            got,
            vec![
                Some((3, 2, 1)),
                Some((3, 11, 0)),
                Some((3, 11, 1)),
                Some((21, 3, 0)),
            ]
        );
    }

    /// Requirements: L1-OUT-003, L3-RS-016
    #[test]
    fn equal_keys_keep_arrival_order() {
        // Distinguish otherwise-identical records by file_offset.
        let mut a = rec(10, 5, 7, Direction::Receive);
        a.file_offset = 0x100;
        let mut b = rec(10, 5, 7, Direction::Receive);
        b.file_offset = 0x200;
        let got: Vec<u64> = vec![Ok::<_, ()>(a), Ok(b)]
            .into_iter()
            .order_rows(DEFAULT_MAX_SORT_GROUP)
            .map(|r| r.unwrap().file_offset)
            .collect();
        assert_eq!(got, vec![0x100, 0x200], "stable sort must preserve order");
    }

    /// Requirements: L1-OUT-003, L2-WRT-021
    #[test]
    fn never_reorders_across_differing_timestamps() {
        // Descending RT across ascending timestamps must stay as-is.
        let got = ordered(
            vec![
                rec(10, 21, 1, Direction::Receive),
                rec(20, 15, 1, Direction::Receive),
                rec(30, 3, 1, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(
            got,
            vec![Some((21, 1, 0)), Some((15, 1, 0)), Some((3, 1, 0))]
        );
    }

    /// A non-monotonic stream (L2-MRG-006 lenient mode) must not be re-sorted:
    /// only the leading consecutive run is permuted.
    ///
    /// Requirements: L1-OUT-003, L2-MRG-006
    #[test]
    fn non_monotonic_run_is_left_in_place() {
        // T(RT 9), T(RT 4), U, T(RT 1) — the trailing T is its own run.
        let got = ordered(
            vec![
                rec(10, 9, 1, Direction::Receive),
                rec(10, 4, 1, Direction::Receive),
                rec(20, 7, 1, Direction::Receive),
                rec(10, 1, 1, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(
            got,
            vec![
                Some((4, 1, 0)), // leading run sorted
                Some((9, 1, 0)),
                Some((7, 1, 0)), // U untouched
                Some((1, 1, 0)), // trailing T stays where it arrived
            ]
        );
    }

    /// Requirements: L1-OUT-003, L2-WRT-021, L2-ERR-005
    #[test]
    fn spurious_record_stays_pinned_to_its_predecessor() {
        // errored RT 20, its spurious continuation, then a lower-RT clean row.
        // The clean row sorts ahead of RT 20, but the spurious must still
        // follow the errored record it continues.
        let got = ordered(
            vec![
                errored(10, 20, 5),
                spurious(10),
                rec(10, 4, 5, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(
            got,
            vec![
                Some((4, 5, 0)),  // sorted ahead
                Some((20, 5, 0)), // errored record
                None,             // continuation still adjacent
            ]
        );
    }

    /// A pin at the head of a run has no predecessor and simply keeps index 0.
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn leading_spurious_keeps_its_slot() {
        let got = ordered(
            vec![
                spurious(10),
                rec(10, 9, 1, Direction::Receive),
                rec(10, 2, 1, Direction::Receive),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(got, vec![None, Some((2, 1, 0)), Some((9, 1, 0))]);
    }

    /// Two pins in one run each keep their own slot.
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn multiple_pins_keep_their_slots() {
        let got = ordered(
            vec![
                rec(10, 9, 1, Direction::Receive),
                spurious(10),
                rec(10, 2, 1, Direction::Receive),
                spurious(10),
            ],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(got, vec![Some((2, 1, 0)), None, Some((9, 1, 0)), None]);
    }

    /// Requirements: L2-WRT-022
    #[test]
    fn cap_emits_run_in_arrival_order() {
        // Cap of 2 flushes as soon as the run reaches 2, so the third record
        // starts a fresh run — descending RTs survive unsorted across the flush.
        let got = ordered(
            vec![
                rec(10, 9, 1, Direction::Receive),
                rec(10, 8, 1, Direction::Receive),
                rec(10, 7, 1, Direction::Receive),
                rec(10, 6, 1, Direction::Receive),
            ],
            2,
        );
        assert_eq!(
            got,
            vec![
                Some((9, 1, 0)),
                Some((8, 1, 0)),
                Some((7, 1, 0)),
                Some((6, 1, 0)),
            ],
            "a capped run keeps arrival order"
        );
    }

    /// `max_sort_group = 1` is the documented "off" switch: every record is its
    /// own run, so output is raw capture order.
    ///
    /// Requirements: L2-WRT-022
    #[test]
    fn cap_of_one_disables_reordering() {
        let got = ordered(
            vec![
                rec(10, 21, 3, Direction::Receive),
                rec(10, 3, 3, Direction::Receive),
            ],
            MAX_SORT_GROUP_MIN,
        );
        assert_eq!(got, vec![Some((21, 3, 0)), Some((3, 3, 0))]);
    }

    /// A zero cap is rejected by config validation; the adapter clamps it rather
    /// than stalling.
    ///
    /// Requirements: L2-WRT-022
    #[test]
    fn zero_cap_is_clamped_not_stalled() {
        let got = ordered(vec![rec(10, 5, 1, Direction::Receive)], 0);
        assert_eq!(got, vec![Some((5, 1, 0))]);
    }

    /// Requirements: L2-WRT-021, L3-RS-016
    #[test]
    fn buffered_run_is_flushed_before_a_mid_stream_error() {
        let msgs: Vec<Result<MieMessage, &'static str>> = vec![
            Ok(rec(10, 9, 1, Direction::Receive)),
            Ok(rec(10, 2, 1, Direction::Receive)),
            Err("unrecoverable"),
        ];
        let got: Vec<Result<Option<SortKey>, &'static str>> = msgs
            .into_iter()
            .order_rows(DEFAULT_MAX_SORT_GROUP)
            .map(|r| r.map(|m| sort_key(&m)))
            .collect();
        assert_eq!(
            got,
            vec![
                Ok(Some((2, 1, 0))), // sorted run reaches the writer first...
                Ok(Some((9, 1, 0))),
                Err("unrecoverable"), // ...then the failure surfaces
            ]
        );
    }

    /// An error arriving with no run buffered passes straight through.
    ///
    /// Requirements: L2-WRT-021, L3-RS-016
    #[test]
    fn error_with_empty_buffer_passes_through() {
        let msgs: Vec<Result<MieMessage, &'static str>> = vec![Err("boom")];
        let got: Vec<_> = msgs
            .into_iter()
            .order_rows(DEFAULT_MAX_SORT_GROUP)
            .map(|r| r.map(|_| ()))
            .collect();
        assert_eq!(got, vec![Err("boom")]);
    }

    /// Requirements: L2-WRT-021
    #[test]
    fn empty_stream_yields_nothing() {
        assert_eq!(
            ordered(vec![], DEFAULT_MAX_SORT_GROUP),
            Vec::<Option<(u8, u8, u8)>>::new()
        );
    }

    /// A single record needs no permutation and is emitted unchanged.
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn single_record_passes_through() {
        let got = ordered(
            vec![rec(10, 5, 1, Direction::Receive)],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(got, vec![Some((5, 1, 0))]);
    }

    /// Standard-format timestamps group on the raw counter value, so ties in a
    /// Standard recording order canonically too.
    ///
    /// Requirements: L1-OUT-003, L2-WRT-021
    #[test]
    fn standard_timestamps_group_on_raw_counter() {
        let mk = |ticks: u32, rt: u8| {
            let mut m = rec(0, rt, 1, Direction::Receive);
            m.timestamp = Timestamp::Standard(StandardTimestamp {
                raw_value: ticks,
                upper_word: 0,
                lower_word: 0,
            });
            m
        };
        let got = ordered(
            vec![mk(500, 9), mk(500, 2), mk(600, 7)],
            DEFAULT_MAX_SORT_GROUP,
        );
        assert_eq!(got, vec![Some((2, 1, 0)), Some((9, 1, 0)), Some((7, 1, 0))]);
    }

    /// Differing timestamp variants never compare equal, so they never share a
    /// run (defensive — a mixed stream cannot occur today).
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn differing_timestamp_variants_are_separate_groups() {
        let irig = rec(0, 9, 1, Direction::Receive);
        let mut std_ts = rec(0, 2, 1, Direction::Receive);
        std_ts.timestamp = Timestamp::Standard(StandardTimestamp {
            raw_value: 0,
            upper_word: 0,
            lower_word: 0,
        });
        assert!(!same_group(&irig.timestamp, &std_ts.timestamp));
        let got = ordered(vec![irig, std_ts], DEFAULT_MAX_SORT_GROUP);
        // Not merged into one run, so RT 9 stays ahead of RT 2.
        assert_eq!(got, vec![Some((9, 1, 0)), Some((2, 1, 0))]);
    }

    /// IRIG grouping uses absolute microseconds, so the same instant expressed
    /// through different fields is one group.
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn irig_grouping_uses_absolute_microseconds() {
        let a = Timestamp::Irig(IrigTimestamp {
            day: 1,
            hour: 0,
            minute: 1,
            second: 0,
            microsecond: 0,
            freerun: false,
        });
        let b = Timestamp::Irig(IrigTimestamp {
            day: 1,
            hour: 0,
            minute: 0,
            second: 60,
            microsecond: 0,
            freerun: false,
        });
        assert!(same_group(&a, &b));
    }

    /// The `freerun` flag is not part of the rendered `TIME_STAMP` and so is not
    /// part of grouping.
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn freerun_flag_does_not_split_a_group() {
        let mut a = rec(10, 9, 1, Direction::Receive);
        let mut b = rec(10, 2, 1, Direction::Receive);
        if let Timestamp::Irig(ref mut t) = a.timestamp {
            t.freerun = true;
        }
        if let Timestamp::Irig(ref mut t) = b.timestamp {
            t.freerun = false;
        }
        assert!(same_group(&a.timestamp, &b.timestamp));
        let got = ordered(vec![a, b], DEFAULT_MAX_SORT_GROUP);
        assert_eq!(got, vec![Some((2, 1, 0)), Some((9, 1, 0))]);
    }

    /// A run consisting only of pinned records is emitted untouched.
    ///
    /// Requirements: L2-WRT-021
    #[test]
    fn all_pinned_run_is_untouched() {
        let got = ordered(vec![spurious(10), spurious(10)], DEFAULT_MAX_SORT_GROUP);
        assert_eq!(got, vec![None, None]);
    }

    /// Requirements: L2-WRT-022
    #[test]
    fn constants_are_in_range() {
        const {
            assert!(DEFAULT_MAX_SORT_GROUP >= MAX_SORT_GROUP_MIN);
            assert!(DEFAULT_MAX_SORT_GROUP <= MAX_SORT_GROUP_MAX);
        }
    }
}
