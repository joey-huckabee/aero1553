//! Per-RT/MSG `DELTA` tracking — the one definition of "same message", and the
//! one place the inter-arrival arithmetic lives.
//!
//! # Why this is its own module
//!
//! `DELTA` used to be tracked twice. `reader.rs` kept a `HashMap<u32, u64>`
//! keyed by a packed `(rt, subaddress, direction)` integer; `merge.rs` kept a
//! separate `HashMap<String, u64>` keyed by `MieMessage::delta_key()`, the
//! human-readable `"<rt>:<sa><T|R>"` spelling. Two containers, two key
//! representations, one concept — and nothing anywhere asserted that the two
//! agreed on what "the same RT/MSG" means. They did agree, by care rather than
//! by construction, which is the kind of arrangement that holds until someone
//! changes one of them.
//!
//! The arithmetic was duplicated with it, and had already drifted: the reader
//! warns once per key when a clock steps backwards, and the merge path did the
//! same computation silently.
//!
//! # This module does not log
//!
//! [`DeltaTracker::observe`] returns a [`DeltaOutcome`] describing what
//! happened, and the caller decides whether to say anything. That is the same
//! rule `sync` follows and for the same reason: a tracker cannot know whether a
//! backward step is worth a WARN (single-file decode: yes, once per key) or
//! expected background noise (a merge whose inputs are already known to be
//! unsorted, which reports at file granularity instead — L2-MRG-006).
//!
//! The "have I already mentioned this key" bookkeeping *is* kept here, because
//! the once-per-key guarantee is worth having in one place rather than in each
//! caller's own `HashSet`.

use std::collections::{HashMap, HashSet};

use crate::models::{CommandWord, Direction, Timestamp};

/// Encode `(rt, subaddress, direction)` into one `u32`.
///
/// **This is the definition of "the same message" for `DELTA` purposes.** The
/// three fields are 5, 5 and 1 bits on the wire, so the packing is lossless
/// with room to spare, and an integer key keeps the per-record path free of the
/// allocation a formatted string would cost.
///
/// [`MieMessage::delta_key`](crate::models::MieMessage::delta_key) is the
/// display spelling of this same tuple. The two are held together by
/// `packed_key_and_display_key_agree` in this module's tests — the check that
/// was missing when the two representations lived in different modules.
#[inline]
#[must_use]
pub fn delta_key(rt: u8, subaddress: u8, transmit: bool) -> u32 {
    (u32::from(rt) << 16) | (u32::from(subaddress) << 8) | u32::from(transmit)
}

/// What one observation meant.
///
/// Deliberately richer than the `Option<f64>` the CSV eventually needs, because
/// the CSV cannot distinguish "no gap yet" from "no honest gap" from "no key at
/// all" — and the caller has to, in order to narrate correctly.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum DeltaOutcome {
    /// First sighting of this key. `DELTA` is `0.000000` (L2-RDR-010).
    First,
    /// A non-negative gap, in seconds.
    Elapsed(f64),
    /// The clock went backwards for this key, so there is no honest gap to
    /// report (L2-RDR-017). The cursor still advances — the next record is
    /// measured from *this* record, not from the high-water mark.
    ///
    /// `first_for_key` is true exactly once per key per tracker, so a caller
    /// that warns on it gets one line per key rather than one per record.
    Backward {
        prev_us: u64,
        curr_us: u64,
        first_for_key: bool,
    },
    /// No microsecond basis: a Standard timestamp with no configured tick rate
    /// (L2-RDR-019). Nothing is recorded — an entry here would hand the next
    /// record a baseline that means nothing.
    Uncalibrated,
    /// No RT/MSG key at all: `SPURIOUS_DATA` (L2-RDR-018). Never tracked.
    NoKey,
}

impl DeltaOutcome {
    /// The value the `DELTA` column takes, which is where four of the five
    /// outcomes collapse into "empty".
    #[must_use]
    pub fn value(self) -> Option<f64> {
        match self {
            Self::First => Some(0.0),
            Self::Elapsed(seconds) => Some(seconds),
            Self::Backward { .. } | Self::Uncalibrated | Self::NoKey => None,
        }
    }
}

/// Last-seen timestamp per RT/MSG key, and the gap arithmetic over it.
///
/// One instance per DELTA scope: the reader makes one per file, and the merge
/// makes one for the whole merged timeline when `--delta-scope global` is set
/// (L2-MRG-005).
#[derive(Debug)]
pub struct DeltaTracker {
    last_us: HashMap<u32, u64>,
    /// Keys that have already produced a backward-step report. Kept here rather
    /// than in the caller so the once-per-key promise has a single owner.
    warned_keys: HashSet<u32>,
    /// L2-DEC-017 Standard-counter calibration. `None` keeps Standard records
    /// out of tracking entirely.
    tick_rate_hz: Option<f64>,
}

impl DeltaTracker {
    #[must_use]
    pub fn new(tick_rate_hz: Option<f64>) -> Self {
        Self {
            last_us: HashMap::new(),
            warned_keys: HashSet::new(),
            tick_rate_hz,
        }
    }

    /// Record one message and report the gap since the previous one sharing its
    /// RT/MSG key.
    ///
    /// `command_word` is `None` for `SPURIOUS_DATA`, which has no key. Taking
    /// the Command Word rather than a whole `MieMessage` is what lets the
    /// reader call this *before* the message exists — on the errored-record
    /// path the `DELTA` is computed and then handed to the constructor.
    pub fn observe(
        &mut self,
        command_word: Option<&CommandWord>,
        timestamp: &Timestamp,
    ) -> DeltaOutcome {
        let Some(cmd) = command_word else {
            return DeltaOutcome::NoKey;
        };
        let Some(curr_us) = timestamp.to_microseconds(self.tick_rate_hz) else {
            return DeltaOutcome::Uncalibrated;
        };

        let key = delta_key(
            cmd.rt,
            cmd.subaddress,
            matches!(cmd.direction, Direction::Transmit),
        );

        let outcome = match self.last_us.get(&key) {
            None => DeltaOutcome::First,
            Some(&prev_us) if curr_us >= prev_us => {
                // f64 loses precision above 2^53 microseconds, which is
                // roughly 285 years of elapsed time between two records
                // sharing one RT/MSG key. DELTA is a gap within a recording.
                #[allow(
                    clippy::cast_precision_loss,
                    reason = "a gap over 2^53 us is ~285 years; DELTA is intra-recording"
                )]
                let seconds = (curr_us - prev_us) as f64 / 1_000_000.0;
                DeltaOutcome::Elapsed(seconds)
            }
            Some(&prev_us) => DeltaOutcome::Backward {
                prev_us,
                curr_us,
                first_for_key: self.warned_keys.insert(key),
            },
        };

        // Unconditional, and deliberately so on the backward path too: the next
        // record for this key is measured from THIS one. Keeping the older,
        // larger value would report a gap that no pair of records in the file
        // actually has.
        self.last_us.insert(key, curr_us);
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{IrigTimestamp, StandardTimestamp};

    fn cmd(rt: u8, subaddress: u8, transmit: bool) -> CommandWord {
        CommandWord {
            rt,
            direction: if transmit {
                Direction::Transmit
            } else {
                Direction::Receive
            },
            subaddress,
            data_word_count: 2,
            raw: 0,
        }
    }

    /// The absolute microseconds `at(micros)` decodes to.
    ///
    /// IRIG timestamps are absolute -- day 10 alone contributes 864 000 000 000
    /// microseconds -- so a test that expects to see its own argument back is
    /// asserting against a number the tracker never had. Derived rather than
    /// written out, so the two cannot disagree.
    fn us(micros: u64) -> u64 {
        at(micros)
            .to_microseconds(None)
            .expect("IRIG is always calibrated")
    }

    /// An IRIG timestamp `micros` microseconds into day 10.
    fn at(micros: u64) -> Timestamp {
        Timestamp::Irig(IrigTimestamp {
            day: 10,
            hour: 0,
            minute: 0,
            second: u8::try_from(micros / 1_000_000).unwrap_or(0),
            microsecond: u32::try_from(micros % 1_000_000).unwrap_or(0),
            freerun: false,
        })
    }

    /// Requirements: L2-RDR-010
    #[test]
    fn first_sighting_of_a_key_is_zero() {
        let mut tracker = DeltaTracker::new(None);
        let outcome = tracker.observe(Some(&cmd(3, 5, false)), &at(0));
        assert_eq!(outcome, DeltaOutcome::First);
        assert_eq!(outcome.value(), Some(0.0));
    }

    /// Requirements: L2-RDR-009
    #[test]
    fn a_later_record_reports_the_gap_in_seconds() {
        let mut tracker = DeltaTracker::new(None);
        tracker.observe(Some(&cmd(3, 5, false)), &at(0));
        let outcome = tracker.observe(Some(&cmd(3, 5, false)), &at(250_000));
        assert_eq!(outcome, DeltaOutcome::Elapsed(0.25));
        assert_eq!(outcome.value(), Some(0.25));
    }

    /// Requirements: L2-RDR-009
    #[test]
    fn keys_are_tracked_independently() {
        // Interleaved traffic from two terminals. A single cursor would make
        // every gap the inter-record spacing rather than the per-key period --
        // a plausible-looking wrong answer.
        let mut tracker = DeltaTracker::new(None);
        assert_eq!(
            tracker.observe(Some(&cmd(3, 5, false)), &at(0)).value(),
            Some(0.0)
        );
        assert_eq!(
            tracker
                .observe(Some(&cmd(9, 1, false)), &at(100_000))
                .value(),
            Some(0.0)
        );
        assert_eq!(
            tracker
                .observe(Some(&cmd(3, 5, false)), &at(200_000))
                .value(),
            Some(0.2)
        );
        assert_eq!(
            tracker
                .observe(Some(&cmd(9, 1, false)), &at(300_000))
                .value(),
            Some(0.2)
        );
    }

    /// Requirements: L2-RDR-009
    #[test]
    fn direction_is_part_of_the_key() {
        // Same RT, same subaddress, opposite direction: two different messages
        // on the bus, so two independent periods.
        let mut tracker = DeltaTracker::new(None);
        assert_eq!(
            tracker.observe(Some(&cmd(3, 5, false)), &at(0)).value(),
            Some(0.0)
        );
        assert_eq!(
            tracker
                .observe(Some(&cmd(3, 5, true)), &at(100_000))
                .value(),
            Some(0.0)
        );
        assert_eq!(
            tracker
                .observe(Some(&cmd(3, 5, false)), &at(400_000))
                .value(),
            Some(0.4)
        );
    }

    /// Requirements: L2-RDR-017
    #[test]
    fn a_backward_step_reports_no_gap_and_flags_only_the_first() {
        let mut tracker = DeltaTracker::new(None);
        tracker.observe(Some(&cmd(3, 5, false)), &at(500_000));

        let first = tracker.observe(Some(&cmd(3, 5, false)), &at(100_000));
        assert_eq!(
            first,
            DeltaOutcome::Backward {
                prev_us: us(500_000),
                curr_us: us(100_000),
                first_for_key: true,
            }
        );
        assert_eq!(first.value(), None);

        // Reported against the PREVIOUS record, not the high-water mark, which
        // is the same property `the_cursor_advances_across_a_backward_step`
        // checks from the other side.
        let second = tracker.observe(Some(&cmd(3, 5, false)), &at(50_000));
        assert_eq!(
            second,
            DeltaOutcome::Backward {
                prev_us: us(100_000),
                curr_us: us(50_000),
                first_for_key: false,
            }
        );
    }

    /// Requirements: L2-RDR-017
    #[test]
    fn the_cursor_advances_across_a_backward_step() {
        // The recovery is measured from the last record SEEN, not from the
        // high-water mark -- otherwise the gap reported would be one no pair of
        // records in the file actually has.
        let mut tracker = DeltaTracker::new(None);
        tracker.observe(Some(&cmd(3, 5, false)), &at(500_000));
        tracker.observe(Some(&cmd(3, 5, false)), &at(100_000));
        let outcome = tracker.observe(Some(&cmd(3, 5, false)), &at(600_000));
        assert_eq!(outcome, DeltaOutcome::Elapsed(0.5));
    }

    /// Requirements: L2-RDR-017
    #[test]
    fn each_key_gets_its_own_first_backward_flag() {
        let mut tracker = DeltaTracker::new(None);
        tracker.observe(Some(&cmd(3, 5, false)), &at(500_000));
        tracker.observe(Some(&cmd(9, 1, false)), &at(500_000));

        for rt in [3u8, 9u8] {
            let subaddress = if rt == 3 { 5 } else { 1 };
            match tracker.observe(Some(&cmd(rt, subaddress, false)), &at(100_000)) {
                DeltaOutcome::Backward { first_for_key, .. } => assert!(first_for_key),
                other => panic!("expected Backward, got {other:?}"),
            }
        }
    }

    /// Requirements: L2-RDR-018
    #[test]
    fn a_record_with_no_command_word_is_never_tracked() {
        let mut tracker = DeltaTracker::new(None);
        assert_eq!(tracker.observe(None, &at(0)), DeltaOutcome::NoKey);
        assert_eq!(tracker.observe(None, &at(0)).value(), None);
        // And it left no cursor behind for a real key to trip over.
        assert_eq!(
            tracker.observe(Some(&cmd(3, 5, false)), &at(0)),
            DeltaOutcome::First
        );
    }

    /// Requirements: L2-RDR-019
    #[test]
    fn an_uncalibrated_standard_counter_is_not_tracked() {
        let mut tracker = DeltaTracker::new(None);
        let ticks = Timestamp::Standard(StandardTimestamp {
            raw_value: 1_000,
            upper_word: 0,
            lower_word: 1_000,
        });
        assert_eq!(
            tracker.observe(Some(&cmd(3, 5, false)), &ticks),
            DeltaOutcome::Uncalibrated
        );
    }

    /// Requirements: L2-DEC-017, L2-RDR-019
    #[test]
    fn a_calibrated_standard_counter_is_tracked_like_irig() {
        let mut tracker = DeltaTracker::new(Some(1_000_000.0));
        let ticks = |value: u32| {
            Timestamp::Standard(StandardTimestamp {
                raw_value: value,
                upper_word: (value >> 16) as u16,
                lower_word: (value & 0xFFFF) as u16,
            })
        };
        assert_eq!(
            tracker.observe(Some(&cmd(3, 5, false)), &ticks(0)).value(),
            Some(0.0)
        );
        assert_eq!(
            tracker
                .observe(Some(&cmd(3, 5, false)), &ticks(1_000_000))
                .value(),
            Some(1.0)
        );
    }

    /// The check that did not exist while the two representations lived in
    /// different modules: the packed key and the display key must partition the
    /// `(rt, subaddress, direction)` space identically. If one ever collapses
    /// two distinct messages that the other keeps apart, DELTA means different
    /// things on the single-file and merge paths.
    ///
    /// Requirements: L2-RDR-009, L2-MSG-003, L3-RDR-001
    #[test]
    fn packed_key_and_display_key_agree() {
        use crate::models::{Bus, DataWords, MessageFormat, MessageType, MieMessage, TypeWord};
        use std::collections::HashMap;

        let mut packed_to_display: HashMap<u32, String> = HashMap::new();
        let mut display_to_packed: HashMap<String, u32> = HashMap::new();

        for rt in 0u8..32 {
            for subaddress in 0u8..32 {
                for transmit in [false, true] {
                    let command = cmd(rt, subaddress, transmit);
                    let packed = delta_key(rt, subaddress, transmit);

                    // MieMessage has no Default, deliberately -- every field
                    // is decoded from the wire and a default-constructed
                    // record would be a record that never existed. Only the
                    // Command Word matters to delta_key(); the rest is inert
                    // filler.
                    let message = MieMessage {
                        timestamp: at(0),
                        type_word: TypeWord {
                            message_type: MessageType::BcToRt as u8,
                            bus: Bus::A,
                            word_count: 8,
                            error: false,
                            raw: 0,
                        },
                        message_format: MessageFormat::Receive,
                        command_word: Some(command),
                        command_word_2: None,
                        status_word: None,
                        status_word_2: None,
                        data_words: DataWords::new(),
                        error_word: None,
                        delta: None,
                        file_offset: 0,
                        mux: None,
                    };
                    let display = message.delta_key();

                    // Injective in both directions: same packed key iff same
                    // display key, for every encodable message on the bus.
                    if let Some(previous) = packed_to_display.insert(packed, display.clone()) {
                        assert_eq!(previous, display, "packed key {packed:#010X} reused");
                    }
                    if let Some(previous) = display_to_packed.insert(display.clone(), packed) {
                        assert_eq!(previous, packed, "display key {display} reused");
                    }
                }
            }
        }

        assert_eq!(packed_to_display.len(), 32 * 32 * 2);
        assert_eq!(display_to_packed.len(), 32 * 32 * 2);
    }
}
