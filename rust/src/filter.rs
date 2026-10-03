//! Message filtering. Both `exclude_*` (negative) and `include_*`
//! (positive) filters are supported. A message passes if:
//!   - it matches no `exclude_*` set, AND
//!   - every active `include_*` set contains its value.
//!
//! Inactive sets (empty) are ignored on both sides.

use crate::models::{Bus, MieMessage};

#[derive(Debug, Clone, Default)]
pub struct FilterConfig {
    pub exclude_types: Vec<u8>,
    pub exclude_rts: Vec<u8>,
    pub exclude_buses: Vec<Bus>,
    pub exclude_subaddresses: Vec<u8>,

    pub include_types: Vec<u8>,
    pub include_rts: Vec<u8>,
    pub include_buses: Vec<Bus>,
    pub include_subaddresses: Vec<u8>,
}

/// The four fields every filter looks at, read off a message once.
///
/// Extracted so the exclude and include halves share one decode rather than
/// each pulling the Command Word apart again.
struct FilterFields {
    message_type: u8,
    bus: Bus,
    rt: Option<u8>,
    subaddress: Option<u8>,
}

impl FilterFields {
    fn of(msg: &MieMessage) -> Self {
        let (rt, subaddress) = msg
            .command_word
            .map_or((None, None), |cw| (Some(cw.rt), Some(cw.subaddress)));
        Self {
            message_type: msg.type_word.message_type,
            bus: msg.type_word.bus,
            rt,
            subaddress,
        }
    }
}

impl FilterConfig {
    #[must_use]
    pub fn is_active(&self) -> bool {
        !self.exclude_types.is_empty()
            || !self.exclude_rts.is_empty()
            || !self.exclude_buses.is_empty()
            || !self.exclude_subaddresses.is_empty()
            || !self.include_types.is_empty()
            || !self.include_rts.is_empty()
            || !self.include_buses.is_empty()
            || !self.include_subaddresses.is_empty()
    }

    /// True if `msg` should be dropped from output, judged on its own.
    ///
    /// The two halves are genuinely different rules, which is why they are
    /// separate functions rather than one run of guards: an *exclude* set drops
    /// a record that IS listed, while an *include* set drops a record that is
    /// not — and an include set only means anything when it is non-empty.
    ///
    /// A `0x2000` continuation is not judged this way in a stream: the
    /// [`Filtered`] adapter judges it through its parent (L2-FLT-003).
    #[must_use]
    pub fn should_exclude(&self, msg: &MieMessage) -> bool {
        let field = FilterFields::of(msg);
        self.excluded_by_negative(&field) || self.excluded_by_positive(&field, true)
    }

    /// The parent half of the pairing rule (L2-FLT-003): would `parent`'s RT,
    /// subaddress, bus or type-exclusion filters drop it? That decides its
    /// continuation's fate too. `include_types` is left out on purpose: a type
    /// *selection* is judged on each record's own type, so
    /// `--include-types SPURIOUS_DATA` keeps continuations it asked for even
    /// though it drops their parents.
    fn excludes_as_parent(&self, parent: &MieMessage) -> bool {
        let field = FilterFields::of(parent);
        self.excluded_by_negative(&field) || self.excluded_by_positive(&field, false)
    }

    /// The continuation half: is the record's own type (`SPURIOUS_DATA`)
    /// excluded, or left out of an active type selection?
    fn excludes_type_of(&self, msg: &MieMessage) -> bool {
        let t = msg.type_word.message_type;
        self.exclude_types.contains(&t)
            || (!self.include_types.is_empty() && !self.include_types.contains(&t))
    }

    /// Dropped because the record IS in an exclusion set.
    fn excluded_by_negative(&self, field: &FilterFields) -> bool {
        if self.exclude_types.contains(&field.message_type)
            || self.exclude_buses.contains(&field.bus)
        {
            return true;
        }
        if let Some(rt) = field.rt
            && self.exclude_rts.contains(&rt)
        {
            return true;
        }
        if let Some(sa) = field.subaddress
            && self.exclude_subaddresses.contains(&sa)
        {
            return true;
        }
        false
    }

    /// Dropped because an inclusion set is active and the record is not in it.
    ///
    /// A record with no Command Word — `SPURIOUS_DATA` — has no RT or subaddress
    /// to match, so an active RT or subaddress include set drops it. That is
    /// deliberate: "keep only RT 5" cannot meaningfully keep a record that
    /// names no RT. (A `0x2000` continuation is the exception, judged through
    /// its parent by [`Filtered`].)
    ///
    /// `with_types: false` skips the type selection, for judging a parent on
    /// its continuation's behalf (see `excludes_as_parent`).
    fn excluded_by_positive(&self, field: &FilterFields, with_types: bool) -> bool {
        if with_types
            && !self.include_types.is_empty()
            && !self.include_types.contains(&field.message_type)
        {
            return true;
        }
        if !self.include_buses.is_empty() && !self.include_buses.contains(&field.bus) {
            return true;
        }
        if !self.include_rts.is_empty()
            && !field.rt.is_some_and(|rt| self.include_rts.contains(&rt))
        {
            return true;
        }
        if !self.include_subaddresses.is_empty()
            && !field
                .subaddress
                .is_some_and(|sa| self.include_subaddresses.contains(&sa))
        {
            return true;
        }
        false
    }
}

/// Iterator adapter wrapping any `Iterator<Item = MieResult<MieMessage>>`.
///
/// Reports the same diagnostics as the Python `apply_filters` generator: the
/// active filter sets once at construction (INFO), each dropped record (DEBUG),
/// and a passed/excluded tally when the stream is finished (INFO). The tally is
/// emitted from `Drop` rather than at end-of-iteration so it still appears when
/// a consumer stops early — a broken pipe, `| head` — where an end-of-stream
/// hook would never run.
///
/// A `0x2000` continuation shares its parent's fate (L2-FLT-003): it is kept
/// exactly when its errored parent passes the RT, subaddress, bus and
/// type-exclusion filters, and its own type passes any type filter. It always
/// arrives directly after its parent -- the reader emits them back to back,
/// they share one timestamp so a merge cannot separate them (L2-ERR-005), and
/// collapse keeps or drops the pair together (L2-MRG-007) -- so one remembered
/// verdict is all the state this needs.
pub struct Filtered<I> {
    inner: I,
    filters: FilterConfig,
    passed: u64,
    excluded: u64,
    /// For the record just seen, when it was an errored (non-spurious) one:
    /// whether it passed the parent-side filters. `None` after any other
    /// record, so a continuation with no errored record before it is judged on
    /// its own, like a standalone record.
    parent_kept: Option<bool>,
}

impl<I> Filtered<I> {
    /// Whether to drop `msg`, applying the continuation pairing rule.
    fn drops(&mut self, msg: &MieMessage) -> bool {
        let drop = match self.parent_kept {
            Some(parent_kept) if msg.is_continuation() => {
                !parent_kept || self.filters.excludes_type_of(msg)
            }
            _ => self.filters.should_exclude(msg),
        };
        self.parent_kept =
            (msg.is_error() && !msg.is_spurious()).then(|| !self.filters.excludes_as_parent(msg));
        drop
    }
}

impl<I, E> Iterator for Filtered<I>
where
    I: Iterator<Item = Result<MieMessage, E>>,
{
    type Item = Result<MieMessage, E>;
    fn next(&mut self) -> Option<Self::Item> {
        loop {
            match self.inner.next()? {
                Err(e) => return Some(Err(e)),
                Ok(msg) => {
                    if !self.drops(&msg) {
                        self.passed += 1;
                        return Some(Ok(msg));
                    }
                    self.excluded += 1;
                    log_filtered_out(&msg);
                }
            }
        }
    }
}

impl<I> Drop for Filtered<I> {
    fn drop(&mut self) {
        if self.filters.is_active() {
            crate::log_info!(
                "Filter results: {} passed, {} excluded",
                self.passed,
                self.excluded
            );
        }
    }
}

/// One-time INFO summary of the configured filter sets. `none` for an inactive
/// (empty) set, matching the Python wording.
fn log_active_filters(f: &FilterConfig) {
    // Sorted so the line is stable regardless of the order values were parsed
    // in, and rendered identically on both implementations (Python holds these
    // as sets, whose repr order is not guaranteed).
    fn show<T: std::fmt::Display + Ord + Copy>(v: &[T]) -> String {
        if v.is_empty() {
            return "none".to_string();
        }
        let mut sorted: Vec<T> = v.to_vec();
        sorted.sort_unstable();
        let items: Vec<String> = sorted.iter().map(ToString::to_string).collect();
        format!("[{}]", items.join(", "))
    }
    crate::log_info!(
        "Filtering active: exclude_types={} exclude_rts={} exclude_buses={} \
         exclude_subaddresses={} include_types={} include_rts={} include_buses={} \
         include_subaddresses={}",
        show(&f.exclude_types),
        show(&f.exclude_rts),
        show(&f.exclude_buses),
        show(&f.exclude_subaddresses),
        show(&f.include_types),
        show(&f.include_rts),
        show(&f.include_buses),
        show(&f.include_subaddresses),
    );
}

/// DEBUG line for a message dropped by the filters.
fn log_filtered_out(msg: &MieMessage) {
    let (rt, sa) = match msg.command_word {
        Some(cw) => (cw.rt.to_string(), cw.subaddress.to_string()),
        None => ("-".to_string(), "-".to_string()),
    };
    crate::log_debug!(
        "Filtered out: offset=0x{:X} type=0x{:02X} RT{} SA{} Bus {}",
        msg.file_offset,
        msg.type_word.message_type,
        rt,
        sa,
        msg.type_word.bus
    );
}

/// Extension trait: `iter.filter_messages(cfg)`.
pub trait FilterIterExt: Sized {
    fn filter_messages(self, filters: FilterConfig) -> Filtered<Self>;
}

impl<I, E> FilterIterExt for I
where
    I: Iterator<Item = Result<MieMessage, E>>,
{
    fn filter_messages(self, filters: FilterConfig) -> Filtered<Self> {
        if filters.is_active() {
            log_active_filters(&filters);
        } else {
            crate::log_debug!("No filters active, passing all messages through");
        }
        Filtered {
            inner: self,
            filters,
            passed: 0,
            excluded: 0,
            parent_kept: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::*;

    fn msg(rt: u8, sa: u8, bus: Bus, mt: u8) -> MieMessage {
        MieMessage {
            timestamp: Timestamp::Standard(StandardTimestamp {
                raw_value: 0,
                upper_word: 0,
                lower_word: 0,
            }),
            type_word: TypeWord {
                message_type: mt,
                bus,
                word_count: 5,
                error: false,
                raw: 0,
            },
            message_format: MessageFormat::Receive,
            command_word: Some(CommandWord {
                rt,
                direction: Direction::Receive,
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

    /// Requirements: L2-FLT-001
    #[test]
    fn empty_config_is_inactive() {
        let cfg = FilterConfig::default();
        assert!(!cfg.is_active());
        assert!(!cfg.should_exclude(&msg(1, 1, Bus::A, 0x02)));
    }

    /// Requirements: L2-CFG-006
    #[test]
    fn exclude_by_rt() {
        let cfg = FilterConfig {
            exclude_rts: vec![31],
            ..Default::default()
        };
        assert!(cfg.should_exclude(&msg(31, 0, Bus::A, 0x02)));
        assert!(!cfg.should_exclude(&msg(15, 0, Bus::A, 0x02)));
    }

    /// Requirements: L2-CFG-006
    #[test]
    fn exclude_by_type_and_bus() {
        let cfg = FilterConfig {
            exclude_types: vec![0x20],
            exclude_buses: vec![Bus::B],
            ..Default::default()
        };
        assert!(cfg.should_exclude(&msg(1, 1, Bus::A, 0x20)));
        assert!(cfg.should_exclude(&msg(1, 1, Bus::B, 0x02)));
        assert!(!cfg.should_exclude(&msg(1, 1, Bus::A, 0x02)));
    }

    /// Requirements: L3-RS-010
    #[test]
    fn include_filters_drop_non_matches() {
        let cfg = FilterConfig {
            include_rts: vec![15],
            ..Default::default()
        };
        assert!(!cfg.should_exclude(&msg(15, 0, Bus::A, 0x02)));
        assert!(cfg.should_exclude(&msg(14, 0, Bus::A, 0x02)));
    }

    /// Requirements: L2-FLT-001
    #[test]
    fn iterator_adapter() {
        let msgs: Vec<Result<MieMessage, ()>> = vec![
            Ok(msg(15, 1, Bus::A, 0x02)),
            Ok(msg(31, 1, Bus::A, 0x02)),
            Ok(msg(0, 1, Bus::B, 0x02)),
        ];
        let cfg = FilterConfig {
            exclude_rts: vec![31],
            ..Default::default()
        };
        let filtered: Vec<_> = msgs
            .into_iter()
            .filter_messages(cfg)
            .collect::<Result<_, _>>()
            .unwrap();
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].rt(), Some(15));
        assert_eq!(filtered[1].rt(), Some(0));
    }

    /// A `SPURIOUS_DATA` record; `code` is its decoder code (0x2000 / 0x2001).
    fn spurious(code: u16) -> MieMessage {
        MieMessage {
            type_word: TypeWord {
                message_type: MessageType::SpuriousData as u8,
                ..msg(0, 0, Bus::A, 0).type_word
            },
            message_format: MessageFormat::SpuriousData,
            command_word: None,
            error_word: Some(code),
            delta: None,
            ..msg(0, 0, Bus::A, 0)
        }
    }

    /// The conformance fixture's three records (`filter-continuation.hex`): an
    /// errored RT15 SA11 `BC_TO_RT` on bus A, its continuation, and a clean
    /// record differing in every filtered field -- RT3 SA22 `RT_TO_BC` on bus B.
    fn pair_and_other() -> Vec<MieMessage> {
        let mut parent = msg(15, 11, Bus::A, MessageType::BcToRt as u8);
        parent.type_word.error = true;
        parent.error_word = Some(0x011E);
        vec![
            parent,
            spurious(ERROR_SPURIOUS_CONTINUATION),
            msg(3, 22, Bus::B, MessageType::RtToBc as u8),
        ]
    }

    /// Which of the stream's records survive `cfg`, as labels.
    fn kept(stream: Vec<MieMessage>, cfg: FilterConfig) -> Vec<&'static str> {
        stream
            .into_iter()
            .map(Ok::<_, ()>)
            .filter_messages(cfg)
            .map(|m| {
                let m = m.unwrap();
                match (m.is_continuation(), m.is_spurious(), m.rt()) {
                    (true, _, _) => "continuation",
                    (_, true, _) => "standalone",
                    (_, _, Some(15)) => "parent",
                    _ => "other",
                }
            })
            .collect()
    }

    /// A `0x2000` continuation shares its parent's fate under every RT,
    /// subaddress, bus and type-exclusion filter, include or exclude, and is
    /// judged on its own type by a type filter. One row per filter case, the
    /// same table the conformance cases pin.
    /// Requirements: L2-FLT-003, L2-FLT-001
    #[test]
    fn continuation_shares_its_parents_filter_fate() {
        let types = |names: &[MessageType]| names.iter().map(|t| *t as u8).collect();
        let cases: [(&str, FilterConfig, &[&str]); 12] = [
            (
                "exclude-rts parent",
                FilterConfig {
                    exclude_rts: vec![15],
                    ..Default::default()
                },
                &["other"],
            ),
            (
                "exclude-rts other",
                FilterConfig {
                    exclude_rts: vec![3],
                    ..Default::default()
                },
                &["parent", "continuation"],
            ),
            (
                "include-rts parent",
                FilterConfig {
                    include_rts: vec![15],
                    ..Default::default()
                },
                &["parent", "continuation"],
            ),
            (
                "include-rts other",
                FilterConfig {
                    include_rts: vec![3],
                    ..Default::default()
                },
                &["other"],
            ),
            (
                "exclude-subaddresses parent",
                FilterConfig {
                    exclude_subaddresses: vec![11],
                    ..Default::default()
                },
                &["other"],
            ),
            (
                "include-subaddresses parent",
                FilterConfig {
                    include_subaddresses: vec![11],
                    ..Default::default()
                },
                &["parent", "continuation"],
            ),
            (
                "exclude-buses parent",
                FilterConfig {
                    exclude_buses: vec![Bus::A],
                    ..Default::default()
                },
                &["other"],
            ),
            (
                "include-buses parent",
                FilterConfig {
                    include_buses: vec![Bus::A],
                    ..Default::default()
                },
                &["parent", "continuation"],
            ),
            (
                "exclude-types parent",
                FilterConfig {
                    exclude_types: types(&[MessageType::BcToRt]),
                    ..Default::default()
                },
                &["other"],
            ),
            (
                "exclude-types spurious",
                FilterConfig {
                    exclude_types: types(&[MessageType::SpuriousData]),
                    ..Default::default()
                },
                &["parent", "other"],
            ),
            (
                "include-types spurious",
                FilterConfig {
                    include_types: types(&[MessageType::SpuriousData]),
                    ..Default::default()
                },
                &["continuation"],
            ),
            (
                "include-types parent",
                FilterConfig {
                    include_types: types(&[MessageType::BcToRt]),
                    ..Default::default()
                },
                &["parent"],
            ),
        ];
        for (label, cfg, want) in cases {
            assert_eq!(kept(pair_and_other(), cfg), want, "{label}");
        }
    }

    /// A standalone `0x2001` record has no parent and is judged on its own,
    /// exactly as before: an RT include drops it, an RT exclude cannot match it.
    /// Requirements: L2-FLT-003
    #[test]
    fn standalone_spurious_is_judged_on_its_own() {
        let stream = || {
            vec![
                msg(15, 11, Bus::A, MessageType::BcToRt as u8),
                spurious(ERROR_SPURIOUS_STANDALONE),
            ]
        };
        let include = FilterConfig {
            include_rts: vec![15],
            ..Default::default()
        };
        assert_eq!(kept(stream(), include), ["parent"]);
        let exclude = FilterConfig {
            exclude_rts: vec![15],
            ..Default::default()
        };
        assert_eq!(kept(stream(), exclude), ["standalone"]);
    }

    /// Safety net: a continuation with no errored record directly before it is
    /// judged on its own rather than on a stale verdict.
    /// Requirements: L2-FLT-003
    #[test]
    fn continuation_without_a_parent_is_judged_on_its_own() {
        let mut parent = msg(15, 11, Bus::A, MessageType::BcToRt as u8);
        parent.type_word.error = true;
        let stream = vec![
            parent,
            msg(3, 22, Bus::B, MessageType::RtToBc as u8),
            spurious(ERROR_SPURIOUS_CONTINUATION),
        ];
        let cfg = FilterConfig {
            exclude_rts: vec![15],
            ..Default::default()
        };
        // The parent's exclusion does not reach a continuation it is not
        // directly followed by; an RT exclude cannot match it on its own.
        assert_eq!(kept(stream, cfg), ["other", "continuation"]);
    }
}
