//! Multi-file, time-sorted streaming k-way merge (L1-MRG-*, L2-MRG-*).
//!
//! Accepts several decoded recordings and yields a single stream of
//! `MieMessage`s in global time order, holding at most one record per open
//! file in a min-heap (resident memory O(number of files), independent of the
//! total record count — L2-MRG-002). The merged stream feeds the existing
//! `write_csv` / `write_csv_split` unchanged.
//!
//! Merge requires every input to be calendar-locked IRIG; Standard-format,
//! freerun-leading, or mixed-format inputs are rejected up front
//! (`MieError::IncompatibleMergeInputs`, CLI exit 6 — L2-MRG-003). DELTA is
//! measured **per input file** by default — each reader already computed it for
//! its own file, so this module leaves it alone and a merged record's value
//! equals its single-file value by construction. `--delta-scope global`
//! recomputes it across the merged timeline instead (L2-MRG-005).
//!
//! No new external dependency: the heap is `std::collections::BinaryHeap`
//! and the `--glob` matcher is hand-rolled (L3-RS-014, preserving L3-RS-002).

use std::borrow::Borrow;
use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, VecDeque};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

use crate::delta::DeltaTracker;
use crate::error::{MieError, MieResult};
use crate::log_warn;
use crate::models::{CommandWord, DeltaScope, MieMessage, Timestamp, TypeWord};
use crate::reader::{MieFileReader, RecordIter};

/// Maximum number of input files a single merge invocation may process.
/// Bounds open mappings / file descriptors so resource use is predictable;
/// exceeding it is a usage error (the CLI maps it to exit 4). Shared in value
/// with the Python implementation (L3-RS-014 / L3-PY-014).
pub const MAX_MERGE_FILES: usize = 256;

// ── Input resolution helpers ──────────────────────────────────────────────

/// Read a manifest file into a list of paths: one path per line, in order.
/// Blank lines and lines whose first non-whitespace character is `#` are
/// ignored; surrounding whitespace is trimmed (L2-MRG-001).
///
/// # Errors
///
/// Returns the [`io::Error`] from reading the manifest. A manifest containing
/// no usable lines is **not** an error here — it yields an empty list, and the
/// CLI decides what to say about it.
pub fn read_manifest(path: &Path) -> io::Result<Vec<PathBuf>> {
    let text = fs::read_to_string(path)?;
    let mut out = Vec::new();
    // `split('\n')`, NOT `str::lines()`. `lines()` strips a trailing `\r` only
    // from a line that was actually terminated by `\n`, so a manifest whose
    // final line is unterminated keeps its carriage return -- the one-byte
    // manifest `"\r"` was one path here and none in Python and C++, which strip
    // the CR from EVERY line as L2-MRG-001 rule 3 requires. Found by the merge
    // fuzz harness's cross-implementation summary comparison; a truncated CRLF
    // manifest ending `"b.mie\r"` is the case an operator actually hits.
    for raw in text.split('\n') {
        // Strip at most one trailing carriage return -- that is what a CRLF
        // line ending is. No other `\r` is touched, so a filename containing an
        // interior CR survives.
        let line = raw.strip_suffix('\r').unwrap_or(raw);
        // ASCII space and tab only, NOT `str::trim`. `trim` removes Unicode
        // whitespace -- a no-break space, an ideographic space, an ogham space
        // mark -- and the C++ implementation cannot: it is locale-free by rule
        // (scripts/assert-locale-free.sh), so it trims ASCII blanks and would
        // have to embed a Unicode table to agree. Two implementations silently
        // editing a filename that legitimately begins with U+00A0, while the
        // third passed it through, is the divergence this closes (L2-MRG-001).
        let trimmed = line.trim_matches([' ', '\t']);
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        out.push(PathBuf::from(trimmed));
    }
    Ok(out)
}

/// Whole-string wildcard match: `*` matches any run (including empty), `?`
/// matches exactly one character; no other metacharacters are special.
/// Iterative backtracking matcher (no recursion, no allocation beyond the
/// char vectors). Identical semantics to the Python implementation.
#[must_use]
pub fn glob_match(pattern: &str, name: &str) -> bool {
    let pat: Vec<char> = pattern.chars().collect();
    let txt: Vec<char> = name.chars().collect();
    let (mut p, mut t) = (0usize, 0usize);
    let mut star: Option<usize> = None;
    let mut mark = 0usize;
    while t < txt.len() {
        // `*` is tested FIRST. A pattern `*` is never literal (L2-MRG-001), but
        // testing equality first consumed it as one against a `*` in the name
        // without recording it for backtracking, so `*` failed to match `*x`.
        if p < pat.len() && pat[p] == '*' {
            star = Some(p);
            mark = t;
            p += 1;
        } else if p < pat.len() && (pat[p] == '?' || pat[p] == txt[t]) {
            p += 1;
            t += 1;
        } else if let Some(sp) = star {
            p = sp + 1;
            mark += 1;
            t = mark;
        } else {
            return false;
        }
    }
    while p < pat.len() && pat[p] == '*' {
        p += 1;
    }
    p == pat.len()
}

/// Why a pattern with a wildcard in its directory part is refused. Shared by
/// every caller so the CLI and the Python binding say the same thing.
const GLOB_DIRECTORY_WILDCARD_MESSAGE: &str = "wildcards are only supported in the filename, \
     not in the directory part (no recursive **)";

/// Whether the directory part of a glob (everything up to and including the
/// last separator) holds a `*` or `?`.
///
/// On Windows the extended-length prefix `\\?\` is skipped: its `?` is syntax,
/// not a wildcard, and refusing it would refuse every long-path pattern.
fn directory_has_wildcard(directory: &str) -> bool {
    let directory = if cfg!(windows) {
        directory.strip_prefix(r"\\?\").unwrap_or(directory)
    } else {
        directory
    };
    directory.contains(['*', '?'])
}

/// Expand a single-directory glob `DIR/PATTERN` (or `PATTERN` for the current
/// directory). PATTERN wildcards (`*`, `?`) apply to the **filename only** —
/// no recursive `**`, no brace expansion. Returns matching regular files
/// sorted lexicographically by path (deterministic across implementations,
/// L2-MRG-001).
///
/// "Regular file" is decided **after following symlinks** (`Path::is_file`, not
/// `DirEntry::file_type`). The two disagree on exactly one entry — a symlink
/// pointing at a recording — and `DirEntry::file_type` does not follow it, so
/// this used to drop the symlink that Python's `entry.is_file()` kept. A
/// recording reached through a symlink is a recording; the same `--glob` has to
/// resolve to the same set in every implementation (L2-MRG-001), and this is the
/// reading that agrees with the shell.
///
/// A dangling symlink answers `false` and is skipped, which is also what a
/// broken link deserves: the merge would only fail to open it a moment later.
///
/// # Errors
///
/// Returns an [`io::ErrorKind::InvalidInput`] error, without touching the
/// filesystem, when the directory part holds a wildcard (`captures/**/*.mie`,
/// `capt*/a.mie`): the CLI reports that as a usage error. Otherwise returns the
/// [`io::Error`] from enumerating the directory. A pattern matching nothing is
/// **not** an error — it yields an empty list, which is what lets the CLI report
/// "matched no files" distinctly from "could not read the directory".
pub fn expand_glob(pattern: &str) -> io::Result<Vec<PathBuf>> {
    // A textual split at the LAST separator, with the platform deciding what a
    // separator is (L2-MRG-001 clause 1). This used `Path::file_name` and
    // `Path::parent`, which normalise first: they drop a trailing `/` and a
    // trailing `/.`, so `dir/*.mie/` expanded as `dir/*.mie` here while C++
    // read a directory named `*.mie` with an empty filename pattern -- as the
    // grammar says. A pattern ending in a separator matches nothing.
    let (dir, name_pat) = match pattern.rfind(std::path::is_separator) {
        None => (PathBuf::from("."), pattern),
        // Rooted at the separator itself, e.g. `/recordings*.mie`.
        Some(0) => (PathBuf::from(&pattern[..1]), &pattern[1..]),
        Some(i) => (PathBuf::from(&pattern[..i]), &pattern[i + 1..]),
    };
    // Wildcards apply to the filename only (L2-MRG-001 clause 2). Read
    // literally, `captures/**` is a directory named `**`, and the failure to
    // open it reported "No such file or directory" -- a missing folder, when
    // the mistake is the pattern.
    if directory_has_wildcard(&pattern[..pattern.len() - name_pat.len()]) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            GLOB_DIRECTORY_WILDCARD_MESSAGE,
        ));
    }
    let mut out = Vec::new();
    for entry in fs::read_dir(&dir)? {
        let entry = entry?;
        // Name test before the stat: a directory of thousands of entries should
        // cost one `stat` per *match*, not one per entry.
        let fname = entry.file_name().to_string_lossy().into_owned();
        if !glob_match(name_pat, &fname) {
            continue;
        }
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        out.push(path);
    }
    out.sort();
    Ok(out)
}

// ── k-way merge ────────────────────────────────────────────────────────────

/// One record at the front of an input file, ordered by the merge key
/// `(microseconds, file index, within-file sequence)` for a total,
/// deterministic order including ties (L2-MRG-002).
struct HeapEntry {
    us: u64,
    file_index: usize,
    seq: u64,
    msg: MieMessage,
}

impl HeapEntry {
    fn key(&self) -> (u64, usize, u64) {
        (self.us, self.file_index, self.seq)
    }
}

impl PartialEq for HeapEntry {
    fn eq(&self, other: &Self) -> bool {
        self.key() == other.key()
    }
}
impl Eq for HeapEntry {}
impl PartialOrd for HeapEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for HeapEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        self.key().cmp(&other.key())
    }
}

/// The microsecond merge key for a message. IRIG always yields `Some`; the
/// fallback is unreachable for a validated (IRIG-only) merge.
fn merge_micros(msg: &MieMessage, tick: Option<f64>) -> u64 {
    msg.timestamp.to_microseconds(tick).unwrap_or(0)
}

/// Reject an input whose leading record cannot anchor an absolute timeline.
fn check_mergeable(msg: &MieMessage, file_index: usize, path: &Path) -> MieResult<()> {
    match msg.timestamp {
        Timestamp::Standard(_) => Err(MieError::IncompatibleMergeInputs {
            file_index,
            path: path.to_path_buf(),
            detail: "resolves to the Standard timestamp format".into(),
        }),
        Timestamp::Irig(t) if t.freerun => Err(MieError::IncompatibleMergeInputs {
            file_index,
            path: path.to_path_buf(),
            detail: "leads with a freerun IRIG record (no calendar time)".into(),
        }),
        Timestamp::Irig(_) => Ok(()),
    }
}

// ── Cross-recorder duplicate collapsing (L2-MRG-007) ─────────────────────────

/// Content identity of a message for cross-recorder de-duplication: the bits a
/// recorder reads off the wire — Type Word (message type, bus, word count, error
/// flag, raw), Command/Status Words, Error Word, and the valid data words.
/// Timestamp, file offset, MUX, and DELTA are intentionally excluded — the
/// timestamp drives the window (not equality), and the rest are per-recorder.
#[derive(PartialEq, Eq)]
struct DedupKey {
    type_word: TypeWord,
    command_word: Option<CommandWord>,
    command_word_2: Option<CommandWord>,
    status_word: Option<u16>,
    status_word_2: Option<u16>,
    error_word: Option<u16>,
    data_words: Vec<u16>,
}

impl DedupKey {
    fn of(msg: &MieMessage) -> Self {
        Self {
            type_word: msg.type_word,
            command_word: msg.command_word,
            command_word_2: msg.command_word_2,
            status_word: msg.status_word,
            status_word_2: msg.status_word_2,
            error_word: msg.error_word,
            data_words: msg.data_words.as_slice().to_vec(),
        }
    }
}

/// Smallest accepted `merge.max_collapse_survivors` (L2-MRG-008).
pub const MAX_COLLAPSE_SURVIVORS_MIN: usize = 1;
/// Largest accepted `merge.max_collapse_survivors` (L2-MRG-008).
pub const MAX_COLLAPSE_SURVIVORS_MAX: usize = 1_048_576;
/// Default cap on the de-duplication survivor set (L2-MRG-008). Far above any
/// genuine population of one collapse window — a 1553 bus carries one
/// transaction at a time, so a window holds one record per recorder per
/// transaction — while bounding worst-case retention to a few hundred kilobytes.
/// Matches `DEFAULT_MAX_SORT_GROUP` deliberately: the two caps guard the same
/// class of pathological input and an operator who has reasoned about one
/// should not have to re-derive the other.
pub const DEFAULT_MAX_COLLAPSE_SURVIVORS: usize = 4096;

/// Sliding time-window de-duplicator over the merged stream (L2-MRG-007), with
/// the survivor-set cap of L2-MRG-008.
///
/// Retention is defined on the **absolute** time distance to the current record
/// and is therefore independent of the order survivors were appended in: a
/// survivor is kept iff `|survivor_us - us| <= window_us`. That order
/// independence is the requirement, not an optimisation. This used to evict only
/// from the FRONT, testing the one-sided `us - front_us`, which is correct only
/// while the stream is sorted. After a lenient backward step (L2-MRG-006) the
/// front can hold a timestamp in the *future* of the current record, the
/// one-sided test is then never true, and the front never leaves — blocking
/// eviction of everything behind it. An alternating 1000us / 0us probe with a
/// zero-width window retained all 10 000 records and ran quadratically (2x the
/// records, 4x the time), contradicting the bounded-memory guarantee L2-MRG-007
/// makes and L2-MRG-002 depends on.
///
/// The window bounds retention in TIME; it does not bound it in COUNT. A corrupt
/// recording whose timestamps all decode to one value, or a wide operator-set
/// `collapse_window_us` on a dense bus, puts arbitrarily many records inside one
/// window. `max_survivors` is the second, independent bound that makes the
/// guarantee unconditional — the same reasoning, and the same default, as
/// `max_sort_group` (L2-WRT-022) applies to the reorder stage.
struct DedupWindow {
    window_us: u64,
    max_survivors: usize,
    survivors: VecDeque<(u64, usize, DedupKey)>,
    /// One WARN per merge, not per capped record: a pathological input hits the
    /// cap on nearly every record, and the cadence that matters to an operator
    /// is "this run stopped being exact", once. Mirrors the one-WARN-per-input
    /// cadence L2-MRG-006 uses for the same reason.
    capped_warned: bool,
}

impl DedupWindow {
    fn new(window_us: u64, max_survivors: usize) -> Self {
        Self {
            window_us,
            max_survivors: max_survivors.max(MAX_COLLAPSE_SURVIVORS_MIN),
            survivors: VecDeque::new(),
            capped_warned: false,
        }
    }

    /// Returns true if `msg` (at `us`, from `file_index`) duplicates a recent
    /// survivor from a **different** input within the window — i.e. the same bus
    /// transaction witnessed by another recorder. Same-file identical content is
    /// never a duplicate. A non-duplicate is recorded as a survivor.
    fn is_duplicate(&mut self, us: u64, file_index: usize, msg: &MieMessage) -> bool {
        // Evict on absolute distance, over the whole set rather than the front.
        // `retain` is O(survivors), which is what the match scan below already
        // costs, so the sorted-stream case is no slower than the front-only
        // eviction it replaces -- and the unsorted case is now bounded at all.
        let window = self.window_us;
        self.survivors
            .retain(|(buf_us, _, _)| buf_us.abs_diff(us) <= window);

        let key = DedupKey::of(msg);
        // Every retained survivor is already within the window, so the match
        // test is content and provenance only.
        if self
            .survivors
            .iter()
            .any(|(_, fi, k)| *fi != file_index && *k == key)
        {
            return true;
        }

        // L2-MRG-008: make room rather than grow. Dropping the oldest ARRIVAL is
        // the honest choice once the window itself is over-full -- there is no
        // "least useful" survivor to pick, and arrival order is the one ordering
        // that is meaningful on input this badly behaved. Records are never
        // dropped from the OUTPUT; only the ability to recognise a later
        // duplicate of this one is given up.
        while self.survivors.len() >= self.max_survivors {
            self.survivors.pop_front();
            if !self.capped_warned {
                self.capped_warned = true;
                log_warn!(
                    "de-duplication survivor set hit the {}-record max_collapse_survivors \
                     cap; collapsing is best-effort past this point (raise [merge] \
                     max_collapse_survivors / --max-collapse-survivors, or narrow \
                     --collapse-window-us)",
                    self.max_survivors
                );
            }
        }
        self.survivors.push_back((us, file_index, key));
        false
    }
}

/// Streaming k-way merge over per-file readers. Yields the same item type as a
/// single reader (`MieResult<MieMessage>`) so the writer consumes it unchanged.
pub struct MergedRecordIter<'a> {
    iters: Vec<RecordIter<'a>>,
    heap: BinaryHeap<Reverse<HeapEntry>>,
    next_seq: Vec<u64>,
    tick: Option<f64>,
    allow_partial: bool,
    /// In strict mode a within-file backward timestamp step (L2-MRG-006) is a
    /// record error that fails the batch; in lenient mode it only WARNs.
    strict: bool,
    /// Microsecond key of the previous record pulled from each input file, in
    /// capture order. `None` until a file's first record is seen. Used to
    /// detect a within-file backward step (L2-MRG-006).
    prev_us: Vec<Option<u64>>,
    /// One-time-per-file guard so a non-monotonic input WARNs at most once
    /// (lenient mode), mirroring the single-file non-monotonic-DELTA WARN.
    warned_backward: Vec<bool>,
    /// Input paths in resolved order, for naming a file in the L2-MRG-006
    /// non-monotonic WARN / error (the per-file readers are not retained).
    paths: Vec<PathBuf>,
    /// L2-MRG-005 global-scope `DELTA` state.
    ///
    /// The same `DeltaTracker` the reader uses per file, which is the point of
    /// it: the merged timeline and a single-file decode now answer "the same
    /// RT/MSG" identically by construction rather than by two implementations
    /// that happened to agree.
    delta_tracker: DeltaTracker,
    /// Error to surface on the *next* `next()` call (non-`--allow-partial`
    /// mid-stream failure — fails the batch).
    pending_error: Option<MieError>,
    /// Error to surface once the heap drains (an `--allow-partial` deferred
    /// unrecoverable loss — lets the writer commit a `.partial`).
    pending_terminal: Option<MieError>,
    /// Cross-recorder duplicate collapsing (L2-MRG-007). `Some` when enabled via
    /// `--collapse-duplicates`; `None` keeps every row (the default).
    dedup: Option<DedupWindow>,
    /// L2-MRG-008 cap, held here rather than only inside `DedupWindow` so
    /// `collapse` and `max_collapse_survivors` compose in either order.
    max_survivors: usize,
    /// L2-MRG-005: scope DELTA is measured over. `PerFile` (the default) leaves
    /// each reader's own DELTA untouched; `Global` recomputes across the merged
    /// stream via `delta_tracker`.
    delta_scope: DeltaScope,
    /// Count of records suppressed as cross-recorder duplicates, for the CLI's
    /// end-of-run summary. Shared so the CLI can read it after the iterator is
    /// consumed by the writer (mirrors the `sync_losses` `AtomicU64` pattern).
    collapsed: Arc<AtomicU64>,
}

impl<'a> MergedRecordIter<'a> {
    /// Open a merge over already-constructed readers. Pulls each file's first
    /// record and validates it is calendar-locked IRIG; an incompatible input
    /// is rejected here (L2-MRG-003). With `allow_partial`, a file that fails
    /// to produce a first record is skipped with a WARN instead of failing the
    /// batch (L2-MRG-004).
    /// # Errors
    ///
    /// Returns [`MieError::IncompatibleMergeInputs`] for an input that cannot
    /// anchor an absolute timeline — one resolving to the Standard format, or
    /// leading with a freerun IRIG record (L2-MRG-003). Without
    /// `allow_partial`, a file that fails to yield a first record propagates
    /// that failure; with it, the file is dropped from the merge with a WARN
    /// and the run commits what it decoded (L2-MRG-004).
    pub fn new(
        readers: &'a [MieFileReader],
        tick: Option<f64>,
        allow_partial: bool,
        strict: bool,
    ) -> MieResult<Self> {
        Self::open(
            readers.iter().map(MieFileReader::iter).collect(),
            readers.iter().map(|r| r.path().to_path_buf()).collect(),
            tick,
            allow_partial,
            strict,
        )
    }

    /// [`new`](Self::new), over iterators that do not borrow the readers.
    ///
    /// Each input is read through [`MieFileReader::iter_detached`], so the
    /// merge holds no borrow and may outlive `readers` -- what an embedder
    /// needs when the merge's lifetime is not a Rust scope (the Python binding
    /// keeps one inside a Python object). Behaviour is otherwise identical.
    ///
    /// # Errors
    ///
    /// As [`new`](Self::new).
    ///
    /// `readers` may hold the readers themselves or references to them, since
    /// an embedder's readers need not sit in one contiguous slice.
    pub fn new_detached<R: Borrow<MieFileReader>>(
        readers: &[R],
        tick: Option<f64>,
        allow_partial: bool,
        strict: bool,
    ) -> MieResult<MergedRecordIter<'static>> {
        MergedRecordIter::open(
            readers.iter().map(|r| r.borrow().iter_detached()).collect(),
            readers
                .iter()
                .map(|r| r.borrow().path().to_path_buf())
                .collect(),
            tick,
            allow_partial,
            strict,
        )
    }

    fn open(
        mut iters: Vec<RecordIter<'a>>,
        paths: Vec<PathBuf>,
        tick: Option<f64>,
        allow_partial: bool,
        strict: bool,
    ) -> MieResult<Self> {
        let mut heap = BinaryHeap::new();
        let mut next_seq = vec![0u64; paths.len()];
        let mut prev_us = vec![None; paths.len()];
        // A priming-time failure under `allow_partial` arms this terminal so the
        // writer commits a `.partial` (L2-MRG-004), exactly like a mid-file
        // failure. The file contributed no records (truncated at offset 0).
        let mut pending_terminal: Option<MieError> = None;

        for (idx, iter) in iters.iter_mut().enumerate() {
            match iter.next() {
                Some(Ok(msg)) => {
                    check_mergeable(&msg, idx, &paths[idx])?;
                    let us = merge_micros(&msg, tick);
                    prev_us[idx] = Some(us);
                    heap.push(Reverse(HeapEntry {
                        us,
                        file_index: idx,
                        seq: 0,
                        msg,
                    }));
                    next_seq[idx] = 1;
                }
                Some(Err(e)) => {
                    if allow_partial {
                        log_warn!(
                            "merge: input #{} ({}) could not be read; truncating it \
                             from the merge (--allow-partial): {}",
                            idx,
                            paths[idx].display(),
                            e
                        );
                        pending_terminal = Some(MieError::UnrecoverableSyncLoss {
                            offset: 0,
                            sync_losses: 0,
                        });
                    } else {
                        return Err(e);
                    }
                }
                None => {
                    // File produced no records; contributes nothing.
                }
            }
        }

        let warned_backward = vec![false; paths.len()];
        Ok(Self {
            iters,
            heap,
            next_seq,
            tick,
            allow_partial,
            strict,
            prev_us,
            warned_backward,
            paths,
            delta_tracker: DeltaTracker::new(tick),
            pending_error: None,
            pending_terminal,
            dedup: None,
            max_survivors: DEFAULT_MAX_COLLAPSE_SURVIVORS,
            delta_scope: DeltaScope::PerFile,
            collapsed: Arc::new(AtomicU64::new(0)),
        })
    }

    /// Enable cross-recorder duplicate collapsing on this merge (L2-MRG-007),
    /// builder-style so `new` keeps a stable signature. `enabled == false` (the
    /// default) is a no-op; `window_us` is the timestamp tolerance.
    #[must_use]
    pub fn collapse(mut self, enabled: bool, window_us: u64) -> Self {
        let cap = self.max_survivors;
        self.dedup = enabled.then(|| DedupWindow::new(window_us, cap));
        self
    }

    /// Cap the de-duplication survivor set (L2-MRG-008), builder-style.
    ///
    /// A separate method rather than a third parameter on [`Self::collapse`] because
    /// `collapse` is public API and widening it would be a breaking change; this
    /// composes with it in **either** order. Values are clamped into
    /// `[MAX_COLLAPSE_SURVIVORS_MIN, MAX_COLLAPSE_SURVIVORS_MAX]` — the CLI and
    /// config loader both reject out-of-range values with a message, so a
    /// library caller reaching this with one has bypassed that and is better
    /// served by a working bound than by a panic.
    #[must_use]
    pub fn max_collapse_survivors(mut self, cap: usize) -> Self {
        let cap = cap.clamp(MAX_COLLAPSE_SURVIVORS_MIN, MAX_COLLAPSE_SURVIVORS_MAX);
        self.max_survivors = cap;
        if let Some(dedup) = self.dedup.as_mut() {
            dedup.max_survivors = cap;
        }
        self
    }

    /// Select the DELTA scope (L2-MRG-005), builder-style so `new` keeps a
    /// stable signature. `PerFile` (the default) is a no-op by design: it leaves
    /// the DELTA each reader already computed for its own file in place, which
    /// is what makes a merged record's value identical to the one it would get
    /// from a single-file decode — the same code path produced both.
    #[must_use]
    pub fn delta_scope(mut self, scope: DeltaScope) -> Self {
        self.delta_scope = scope;
        self
    }

    /// A shared handle to the suppressed-duplicate counter (L2-MRG-007). The CLI
    /// clones this before the iterator is consumed by the writer, then reads it
    /// afterward for the end-of-run summary.
    #[must_use]
    pub fn collapsed_handle(&self) -> Arc<AtomicU64> {
        Arc::clone(&self.collapsed)
    }

    /// Recompute DELTA on the merged global timeline (L2-MRG-005). The stream
    /// is timestamp-sorted, so per-key gaps are non-negative.
    ///
    /// Under `DeltaScope::PerFile` this returns the message untouched, keeping
    /// the value its own reader computed.
    fn apply_global_delta(&mut self, mut msg: MieMessage) -> MieMessage {
        if self.delta_scope == DeltaScope::PerFile {
            return msg;
        }
        // SPURIOUS_DATA (no Command Word), an uncalibrated Standard counter,
        // and a backward step all resolve to an empty cell, and the tracker
        // tells them apart so it can keep the right cursor for each. No WARN
        // here: a backward step on the merged timeline means an input was not
        // internally sorted, which `advance` already reports once per file
        // (L2-MRG-006). Repeating it per record would bury that.
        msg.delta = self
            .delta_tracker
            .observe(msg.command_word.as_ref(), &msg.timestamp)
            .value();
        msg
    }

    /// Advance the file the just-popped record came from, pushing its next
    /// record onto the heap. Records a pending error on failure.
    fn advance(&mut self, file_index: usize) {
        match self.iters[file_index].next() {
            Some(Ok(msg)) => {
                let us = merge_micros(&msg, self.tick);
                // L2-MRG-006: each input is assumed internally time-sorted
                // (capture order is chronological). A backward step means the
                // merged output may be out of order for this file. Strict mode
                // fails the batch; lenient mode WARNs once per file.
                if let Some(prev) = self.prev_us[file_index]
                    && us < prev
                {
                    if self.strict {
                        self.pending_error = Some(MieError::NonMonotonicInput {
                            file_index,
                            path: self.paths[file_index].clone(),
                            prev_us: prev,
                            curr_us: us,
                        });
                    } else if !self.warned_backward[file_index] {
                        self.warned_backward[file_index] = true;
                        log_warn!(
                            "merge: input #{} ({}) is not internally time-sorted: \
                                 timestamp stepped backward (prev_us={} curr_us={}) -- merged \
                                 output may be out of order for this input \
                                 (further occurrences suppressed)",
                            file_index,
                            self.paths[file_index].display(),
                            prev,
                            us
                        );
                    }
                }
                self.prev_us[file_index] = Some(us);
                let seq = self.next_seq[file_index];
                self.next_seq[file_index] = seq + 1;
                self.heap.push(Reverse(HeapEntry {
                    us,
                    file_index,
                    seq,
                    msg,
                }));
            }
            Some(Err(e)) => {
                if self.allow_partial
                    && e.kind() == crate::error::MieErrorKind::UnrecoverableSyncLoss
                {
                    log_warn!(
                        "merge: input #{} truncated at its failure point: {}",
                        file_index,
                        e
                    );
                    // Defer until the heap drains so all good records are
                    // written first, then the writer commits a `.partial`.
                    self.pending_terminal = Some(e);
                } else {
                    // Surface on the next call (after the popped record).
                    self.pending_error = Some(e);
                }
            }
            None => {} // file exhausted
        }
    }
}

impl Iterator for MergedRecordIter<'_> {
    type Item = MieResult<MieMessage>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some(e) = self.pending_error.take() {
                return Some(Err(e));
            }
            let Some(Reverse(entry)) = self.heap.pop() else {
                return self.pending_terminal.take().map(Err);
            };
            let file_index = entry.file_index;
            // Collapse cross-recorder duplicates *before* the global-DELTA stage
            // (L2-MRG-007): a suppressed duplicate must not advance the per-key
            // DELTA tracker, so DELTA is measured across the deduped timeline.
            if let Some(dedup) = self.dedup.as_mut()
                && dedup.is_duplicate(entry.us, file_index, &entry.msg)
            {
                self.collapsed.fetch_add(1, AtomicOrdering::Relaxed);
                self.advance(file_index);
                continue;
            }
            let msg = self.apply_global_delta(entry.msg);
            self.advance(file_index);
            return Some(Ok(msg));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Requirements: L2-MRG-001, L3-RS-014
    #[test]
    fn glob_match_wildcards() {
        assert!(glob_match("*.mie", "rec1.mie"));
        assert!(glob_match("rec?.mie", "rec5.mie"));
        assert!(!glob_match("rec?.mie", "rec55.mie"));
        assert!(glob_match("*", "anything"));
        assert!(glob_match("a*b*c", "axxbyyc"));
        assert!(!glob_match("*.mie", "rec.csv"));
        assert!(glob_match("", ""));
        assert!(!glob_match("", "x"));
        // No special meaning for other metacharacters.
        assert!(glob_match("a.b", "a.b"));
        assert!(!glob_match("a.b", "axb"));
    }

    /// A pattern `*` is a wildcard even where the name holds a literal `*`.
    /// Equality was tested before the star, so the two were consumed as a
    /// literal pair and the star was never recorded for backtracking.
    /// Requirements: L2-MRG-001
    #[test]
    fn glob_star_is_a_wildcard_against_a_literal_star() {
        assert!(glob_match("*", "*x"));
        assert!(glob_match("*", "*"));
        assert!(glob_match("a*", "a*"));
        assert!(glob_match("a*", "a*b"));
        assert!(glob_match("*.mie", "*x.mie"));
        assert!(glob_match("*x.mie", "*x.mie"));
        assert!(!glob_match("*.mie", "*x.csv"));
    }

    /// A scratch directory under the system temp, removed on drop.
    struct GlobDir(PathBuf);

    impl GlobDir {
        fn new(tag: &str) -> Self {
            static C: AtomicU64 = AtomicU64::new(0);
            let n = C.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let dir =
                std::env::temp_dir().join(format!("mie-glob-{tag}-{}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }

        fn file(&self, name: &str) -> PathBuf {
            let p = self.0.join(name);
            fs::write(&p, b"\x00\x00").unwrap();
            p
        }

        fn names(&self, pattern: &str) -> Vec<String> {
            expand_glob(&self.0.join(pattern).to_string_lossy())
                .unwrap()
                .into_iter()
                .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
                .collect()
        }
    }

    impl Drop for GlobDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    /// A DIRECTORY whose name matches the pattern is not an input. It matched in
    /// the C++ implementation, which then failed to map it -- so the same
    /// `--glob` produced a full batch on two implementations and a failure on
    /// the third (L2-MRG-001 clause 4).
    /// Requirements: L2-MRG-001
    #[test]
    fn expand_glob_matches_files_and_skips_directories() {
        let d = GlobDir::new("dirs");
        d.file("a.mie");
        d.file("b.mie");
        fs::create_dir(d.0.join("archive.mie")).unwrap();

        let mut got = d.names("*.mie");
        got.sort();
        assert_eq!(got, vec!["a.mie".to_string(), "b.mie".to_string()]);
    }

    /// Sorting is lexicographic by full path, so two hosts enumerating one
    /// directory merge in the same order whatever the filesystem returns.
    /// Requirements: L2-MRG-001
    #[test]
    fn expand_glob_sorts_lexicographically() {
        let d = GlobDir::new("sort");
        for name in ["c.mie", "a.mie", "b.mie"] {
            d.file(name);
        }
        assert_eq!(
            d.names("*.mie"),
            vec![
                "a.mie".to_string(),
                "b.mie".to_string(),
                "c.mie".to_string()
            ]
        );
    }

    /// A symlink to a recording IS a recording; a dangling one is not.
    ///
    /// This is the clause `DirEntry::file_type` got wrong: it does not follow
    /// symlinks, so a symlinked recording answered "not a file" here while
    /// Python's `entry.is_file()` kept it. Unix-only because creating a symlink
    /// on Windows needs Developer Mode or an elevated process, and a test that
    /// silently skips on most Windows hosts is worse than one that says so.
    /// Requirements: L2-MRG-001
    #[cfg(unix)]
    #[test]
    fn expand_glob_follows_symlinks_and_skips_dangling_ones() {
        let d = GlobDir::new("links");
        let real = d.file("real.mie");
        std::os::unix::fs::symlink(&real, d.0.join("link.mie")).unwrap();
        std::os::unix::fs::symlink(d.0.join("gone.bin"), d.0.join("dangling.mie")).unwrap();

        let mut got = d.names("*.mie");
        got.sort();
        assert_eq!(
            got,
            vec!["link.mie".to_string(), "real.mie".to_string()],
            "a symlinked recording is matched; a dangling link is not"
        );
    }

    /// On POSIX a backslash is an ordinary filename character, not a separator.
    /// C++ split on it regardless of platform, so one pattern resolved to a file
    /// in the current directory for Rust and Python and to a pattern inside a
    /// subdirectory for C++ (L2-MRG-001 clause 1).
    /// Requirements: L2-MRG-001
    #[cfg(unix)]
    #[test]
    fn expand_glob_does_not_treat_a_backslash_as_a_separator_on_posix() {
        let d = GlobDir::new("backslash");
        d.file("odd\\name.mie");
        d.file("plain.mie");
        assert_eq!(d.names("odd\\name*.mie"), vec!["odd\\name.mie".to_string()]);
    }

    /// The split is textual: a pattern ending in a separator names a directory
    /// and an empty filename pattern, as in C++. `Path::parent` normalised the
    /// trailing `/` (and `/.`) away, so `a.mie/` matched the file `a.mie`.
    /// No wildcard here, so the directory check does not pre-empt the split.
    /// Requirements: L2-MRG-001
    #[test]
    fn expand_glob_does_not_normalise_a_trailing_separator() {
        let d = GlobDir::new("trailing");
        d.file("a.mie");
        let base = d.0.to_string_lossy().into_owned();
        let sep = std::path::MAIN_SEPARATOR;
        for pattern in [
            format!("{base}{sep}a.mie{sep}"),
            format!("{base}{sep}a.mie{sep}."),
        ] {
            // `a.mie` is a file, so enumerating it as a directory fails; the
            // kind is the platform's. Either way `a.mie` is not matched.
            assert!(expand_glob(&pattern).is_err(), "{pattern}");
        }
        // An existing directory with a trailing separator: the filename
        // pattern is empty, so nothing matches -- and that is not an error.
        assert_eq!(
            expand_glob(&format!("{base}{sep}")).unwrap(),
            Vec::<PathBuf>::new()
        );
    }

    /// A wildcard in the directory part is refused as `InvalidInput` before any
    /// I/O -- even where a directory literally named `capt*` exists, since the
    /// answer must not depend on the filesystem. Read literally it reported
    /// "No such file or directory", a missing folder rather than a bad pattern.
    /// Requirements: L2-MRG-001
    #[test]
    fn expand_glob_refuses_a_wildcard_in_the_directory_part() {
        let d = GlobDir::new("dirwild");
        fs::create_dir(d.0.join("captures")).unwrap();
        fs::write(d.0.join("captures").join("a.mie"), b"\x00\x00").unwrap();
        let base = d.0.to_string_lossy().into_owned();
        let sep = std::path::MAIN_SEPARATOR;
        for pattern in [
            format!("{base}{sep}captures{sep}**{sep}*.mie"),
            format!("{base}{sep}capt*{sep}a.mie"),
            format!("{base}{sep}capture?{sep}*.mie"),
            format!("**{sep}*.mie"),
        ] {
            let err = expand_glob(&pattern).unwrap_err();
            assert_eq!(err.kind(), io::ErrorKind::InvalidInput, "{pattern}");
            assert!(err.to_string().contains("directory part"), "{pattern}");
        }
        // Wildcards in the filename part are the supported case.
        assert_eq!(d.names(&format!("captures{sep}*.mie")).len(), 1);
    }

    /// The `?` of Windows' extended-length prefix `\\?\` is syntax, not a
    /// wildcard; refusing it would refuse every long-path pattern.
    /// Requirements: L2-MRG-001
    #[test]
    fn directory_wildcard_check_skips_the_extended_length_prefix() {
        assert!(directory_has_wildcard("data/**/"));
        assert!(directory_has_wildcard("da?a/"));
        assert!(!directory_has_wildcard("data/ch10/"));
        assert!(!directory_has_wildcard(""));
        assert_eq!(directory_has_wildcard(r"\\?\C:\data\"), !cfg!(windows));
        assert!(directory_has_wildcard(r"\\?\C:\d*\"));
    }

    /// A message whose wire content is driven by `seq`, so a stream of them
    /// collapses nothing. Content uniqueness is load-bearing in the probes
    /// below: if everything collapsed, the survivor set would stay small for
    /// the wrong reason and the assertions would pass vacuously.
    fn probe_message(seq: u64) -> MieMessage {
        MieMessage {
            timestamp: Timestamp::Irig(crate::models::IrigTimestamp {
                day: 192,
                hour: 15,
                minute: 54,
                second: 50,
                microsecond: 0,
                freerun: false,
            }),
            type_word: TypeWord {
                message_type: 0x02,
                bus: crate::models::Bus::A,
                word_count: 4,
                error: false,
                raw: 0x0224,
            },
            message_format: crate::models::MessageFormat::RtToRt,
            command_word: None,
            command_word_2: None,
            status_word: None,
            status_word_2: None,
            data_words: crate::models::DataWords::new(),
            error_word: Some((seq & 0xFFFF) as u16),
            delta: None,
            file_offset: 0,
            mux: None,
        }
    }

    /// Requirements: L2-MRG-007, L2-MRG-008
    ///
    /// The reported probe, as a test: an alternating 1000us / 0us stream with a
    /// zero-width window. Front-only eviction never fired here -- the front held
    /// a timestamp in the FUTURE of the current record, so the one-sided
    /// `us - front_us` was never greater than the window -- and the front then
    /// blocked eviction of everything behind it. All 10 000 records were
    /// retained and the per-record scan went quadratic (2x records, 4x time).
    ///
    /// Retention is now on absolute distance, so the 1000us survivors go the
    /// moment a 0us record arrives, and vice versa.
    #[test]
    fn dedup_window_retention_is_independent_of_arrival_order() {
        let mut w = DedupWindow::new(0, DEFAULT_MAX_COLLAPSE_SURVIVORS);
        for i in 0..10_000u64 {
            let us = if i.is_multiple_of(2) { 1000 } else { 0 };
            w.is_duplicate(us, (i % 2) as usize, &probe_message(i));
        }
        assert!(
            w.survivors.len() <= 2,
            "survivor set grew to {} on an alternating stream; retention must not \
             depend on the order survivors were appended in",
            w.survivors.len()
        );
    }

    /// Requirements: L2-MRG-008
    ///
    /// The window bounds retention in TIME; the cap bounds it in COUNT. This is
    /// the input the window alone cannot bound -- every record shares one
    /// timestamp, so every one of them is legitimately inside the window. It is
    /// the same "timestamps all decode alike" case L2-WRT-022 cites for the
    /// reorder stage, and it is why the absolute-distance fix above is not on
    /// its own sufficient.
    #[test]
    fn dedup_survivor_set_is_capped_when_the_window_cannot_bound_it() {
        const CAP: usize = 64;
        let mut w = DedupWindow::new(u64::MAX, CAP);
        for i in 0..10_000u64 {
            w.is_duplicate(0, (i % 2) as usize, &probe_message(i));
        }
        assert_eq!(
            w.survivors.len(),
            CAP,
            "the survivor set must stop at the cap, not grow with the record count"
        );
    }

    /// Requirements: L2-MRG-008
    ///
    /// The two builder methods compose in either order -- the reason the cap is
    /// a separate method rather than a third parameter on `collapse` (which is
    /// public API) is that it must not depend on call order.
    #[test]
    fn max_collapse_survivors_composes_with_collapse_in_either_order() {
        let cap_of = |iter: &MergedRecordIter<'_>| iter.dedup.as_ref().map(|d| d.max_survivors);
        let readers: Vec<crate::reader::MieFileReader> = Vec::new();

        let a = MergedRecordIter::new(&readers, None, false, false)
            .expect("empty merge builds")
            .collapse(true, 0)
            .max_collapse_survivors(77);
        assert_eq!(cap_of(&a), Some(77));

        let b = MergedRecordIter::new(&readers, None, false, false)
            .expect("empty merge builds")
            .max_collapse_survivors(77)
            .collapse(true, 0);
        assert_eq!(cap_of(&b), Some(77));

        // Out-of-range values clamp rather than panic: the CLI and the config
        // loader both reject them with a message, so anything arriving here has
        // bypassed those and is better served by a working bound.
        let c = MergedRecordIter::new(&readers, None, false, false)
            .expect("empty merge builds")
            .collapse(true, 0)
            .max_collapse_survivors(0);
        assert_eq!(cap_of(&c), Some(MAX_COLLAPSE_SURVIVORS_MIN));
    }

    /// Requirements: L2-MRG-002
    #[test]
    fn heap_entry_orders_by_key_tuple() {
        // Lower microseconds sort first; ties break on file index then seq.
        let mk = |us, fi, seq| (us, fi, seq);
        assert!(mk(10, 0, 0) < mk(20, 0, 0));
        assert!(mk(10, 0, 5) < mk(10, 1, 0));
        assert!(mk(10, 1, 0) < mk(10, 1, 1));
    }
}
