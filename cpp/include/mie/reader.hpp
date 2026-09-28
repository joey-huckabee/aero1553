// SPDX-License-Identifier: Apache-2.0
//
// Memory-mapped sequential reader: the module that turns a file into records.
//
// Mirrors `rust/src/reader.rs` and `python/src/aero1553/reader.py`. It maps
// the input, finds the first record (skipping any leading bytes), resolves the
// timestamp format once, and then walks the record chain yielding decoded
// MieMessages in file order.
//
// THIS IS THE MODULE THAT TALKS. `sync` and `decode` are pure by rule; every
// user-facing WARN, INFO and DEBUG line about the record stream is emitted from
// here, because only here is the caller's context available. `find_first_record`
// returning nothing is the EXPECTED outcome for a valid empty recording and a
// rejection for a wrong file, and the same call cannot know which -- so it says
// nothing and the reader decides.
//
// SYNC RECOVERY IS INTERNAL. A validation failure mid-file is handled here:
// lenient mode scans forward and resumes, and the caller never sees it except
// as a WARN and a bump in `sync_losses()`. Only a strict-mode rejection or an
// exhausted recovery reaches the caller.
//
// ERRORS ARE THROWN, AND THEY ARE TERMINAL. Rust yields `Some(Err(_))` and then
// latches `done`; Python raises out of a generator, which ends it. Throwing
// MieError from `next()` gives C++ the same shape as Python, and the iterator
// latches its done flag BEFORE throwing so that a caller which catches and
// resumes gets a clean end of stream rather than a second attempt at the record
// that just failed.

#ifndef MIE_READER_HPP
#define MIE_READER_HPP

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

#include "mie/decode.hpp"
#include "mie/delta.hpp"
#include "mie/error.hpp"
#include "mie/models.hpp"
#include "mie/optional.hpp"
#include "mie/platform.hpp"
#include "mie/sync.hpp"

namespace mie {

/// A construction-time failure held until the first `next()` call.
///
/// `Optional<MieError>` would be the obvious spelling and it does not compile:
/// Optional stores its T by value and therefore needs it default-constructible,
/// while MieError deliberately has no default constructor -- its named
/// constructors ARE its schema, and a default-constructed error would be an
/// error with no kind. A shared_ptr sidesteps that, costs one allocation per
/// walk at most, and copies without throwing.
using PendingError = std::shared_ptr<MieError>;

/// `[[noreturn]]` is C++11 and both floors have it (GCC 4.8, MSVC 2015+), but
/// spelling it once here keeps the option of swapping in a compiler-specific
/// form if a toolchain in the matrix turns out not to honour it.
#define MIE_NORETURN [[noreturn]]

/// Construction-time options. Every field has a default that matches the Rust
/// `ReaderOptions::default()` and the Python reader's keyword defaults.
struct ReaderOptions {
    /// Reject on the first anomaly instead of recovering. Off by default: an
    /// operator decoding a field recording wants the records that survived.
    bool strict;

    /// AUTO (the default) runs the L2-DEC-015 probe. An explicit choice is
    /// honoured even when the probe disagrees -- see `check_forced_format` in
    /// the implementation for what "disagrees" costs in each mode.
    TimestampFormat input_time_format;

    /// L2-DEC-015 probe size: how many records auto-detection walks before
    /// committing. Clamped to at least 1 here; the CLI and config loader clamp
    /// to [1, 32] upstream.
    std::size_t detect_records;

    /// L2-SYN-026 look-ahead depth -- the TOTAL records `validate_record`
    /// checks, candidate included. Clamped to at least 1 here.
    std::size_t lookahead_records;

    /// L2-DEC-017: Standard-counter tick rate in Hz. Present and
    /// strictly-positive enables tick-to-microsecond conversion, which is what
    /// lets a Standard recording carry DELTA at all; absent (the default)
    /// leaves DELTA empty on Standard records.
    Optional<double> standard_tick_rate_hz;

    /// L2-WRT-020 MUX population from the input file NAME.
    bool mux_enabled;
    std::string mux_delimiter;
    /// 0-based field index; negative counts from the end.
    int64_t mux_field;

    /// L2-WRT-026: the calendar year a calendar rendering will resolve
    /// day-of-year against, or absent when the output rendering is `doy`.
    ///
    /// The reader has no rendering concern of its own; it takes this because
    /// two of its diagnostics change meaning when the day-of-year field
    /// becomes load-bearing -- the L2-LOG-002 advisory escalates to WARN, and a
    /// backward day step becomes a year-rollover warning (clause 4).
    Optional<int> calendar_year;

    ReaderOptions();
};

class RecordIter;

/// An open recording.
///
/// Non-copyable: it owns a mapping. Construct, then call `iter()`.
class MieFileReader {
  public:
    MieFileReader();
    ~MieFileReader();

    /// Map `path` and prepare to decode. Throws MieError:
    ///
    ///   * FileNotFound  -- the path does not exist
    ///   * FileEmpty     -- zero bytes (L2-RDR-006); checked BEFORE mapping,
    ///                      which is also what keeps Windows out of
    ///                      CreateFileMapping's refusal to map an empty file
    ///   * FileIo        -- anything else the OS reported
    ///
    /// An unopenable path reports I/O rather than "empty", deliberately: a
    /// directory stats as zero bytes on Windows, and calling that an empty
    /// recording would be actively misleading.
    void open(const std::string& path, const ReaderOptions& options);

    const std::string& path() const { return path_; }
    uint64_t file_size() const { return file_size_; }

    /// Cumulative sync recoveries from the most recent `iter()` walk. Reset at
    /// the start of each `iter()`. The CLI reads it after iteration to separate
    /// L1-EXIT-003 (partial, recovered) from L1-EXIT-002 (clean).
    uint64_t sync_losses() const { return sync_losses_; }

    /// True when the most recent walk found a valid but EMPTY recording -- the
    /// stream opens directly on the end-of-records terminator. Zero records,
    /// but not a wrong-file rejection: per L1-EXIT-010 the CLI writes a
    /// header-only CSV and exits 0. Reset at the start of each `iter()`.
    bool empty_recording() const { return empty_recording_; }

    /// Begin a walk. The returned iterator borrows this reader, so it must not
    /// outlive it, and only one walk may be live at a time -- a second `iter()`
    /// resets the counters the first is still reporting into.
    RecordIter iter();

  private:
    MieFileReader(const MieFileReader&);
    MieFileReader& operator=(const MieFileReader&);

    friend class RecordIter;

    /// The format used when detection never runs: an explicit choice, or IRIG
    /// standing in for AUTO.
    TimestampFormat default_resolved_format() const;

    /// Reject a homogeneous-payload pad (L2-SYN-018), then auto-detect
    /// (L2-DEC-015) or sanity-check a forced format (L2-DEC-013). Returns the
    /// format to decode with; sets `error` when iteration must stop first.
    TimestampFormat resolve_format_for_hit(const sync::ScanHit& hit, PendingError& error) const;
    TimestampFormat resolve_auto_format(const sync::ScanHit& hit, PendingError& error) const;
    TimestampFormat check_forced_format(const sync::ScanHit& hit, PendingError& error) const;

    /// No first record: empty recording, truncated first record (L2-RDR-004),
    /// or wrong file (L1-EXIT-002). Returns true when iteration should end
    /// cleanly with zero records.
    bool diagnose_no_records(const Optional<TimestampFormat>& format_hint, PendingError& error);

    std::string path_;
    platform::MappedFile mapping_;
    uint64_t file_size_;
    bool strict_;
    TimestampFormat input_time_format_;
    std::size_t detect_records_;
    std::size_t lookahead_records_;
    Optional<double> standard_tick_rate_hz_;
    /// L2-WRT-026 clause 4 / L2-LOG-002. See `ReaderOptions::calendar_year`.
    Optional<int> calendar_year_;

    /// L2-WRT-020: resolved once from the file name and shared by pointer onto
    /// every message, so carrying it stays O(1) per record no matter how many
    /// records or how many merged inputs. Null when MUX is off or the
    /// configured field is absent. Rust uses `Arc<str>` for the same reason.
    std::shared_ptr<const std::string> mux_;

    /// Plain members, not atomics. Rust needs `AtomicU64`/`AtomicBool` because
    /// its `iter()` takes a shared borrow and therefore cannot mutate; `iter()`
    /// here is non-const, so that constraint does not exist.
    uint64_t sync_losses_;
    bool empty_recording_;
};

/// A single forward walk over the record stream.
///
/// Pull-shaped rather than STL-iterator-shaped: `next(out)` fills a
/// caller-owned message and answers whether there was one. An
/// `operator*`/`operator++` pair would have to hold a decoded message inside
/// the iterator and hand out a reference to it, which is the same thing with
/// more ceremony and a worse story for the errors, which have to be able to end
/// the walk.
class RecordIter {
  public:
    /// Decode the next record into `out`. False at end of stream.
    ///
    /// Throws MieError on a terminal failure -- a strict-mode rejection, or a
    /// lenient-mode recovery that ran out of file to scan. After a throw the
    /// walk is over: `next()` answers false from then on.
    bool next(MieMessage& out);

    /// Recoveries so far in this walk.
    uint64_t sync_losses() const { return sync_losses_; }

    /// Messages yielded so far.
    uint64_t message_count() const { return msg_count_; }

    /// The format this walk committed to. Fixed before the first record.
    TimestampFormat resolved_format() const { return resolved_format_; }

    // Movable so `iter()` can return one by value; non-copyable because two
    // copies walking one reader would double-count into its sync-loss total.
    //
    // noexcept is not decoration. A move constructor that can throw is one a
    // standard container will refuse to use, falling back to copying -- which
    // this type does not permit, so the fallback would be a compile error
    // rather than a slow path. The body moves scalars, two shared_ptrs and two
    // node-based containers, none of which allocates.
    RecordIter(RecordIter&& other) noexcept;

  private:
    friend class MieFileReader;

    explicit RecordIter(MieFileReader& owner);

    // Copying is suppressed by the move constructor above -- a user-declared
    // move constructor deletes both copy operations, and the move ASSIGNMENT
    // operator is never implicitly declared at all. These say so out loud.
    RecordIter(const RecordIter&);
    RecordIter& operator=(const RecordIter&);

    /// What the decode loop should do next. The three cases are the loop's
    /// `continue`, `return the message`, and `stop` -- named so the work can
    /// live in helpers that cannot themselves drive the caller's loop.
    enum Step { STEP_CONTINUE, STEP_YIELD, STEP_STOP };

    Step decode_one(MieMessage& out);
    Step handle_sync_loss(sync::ValidationFailure failure, uint16_t type_raw, const TypeWord& tw,
                          std::size_t record_bytes);
    bool decode_timestamp_at(TimestampFormat resolved, Timestamp& out);
    void spurious_message(const TypeWord& tw, const Timestamp& timestamp, uint16_t ts_words,
                          std::size_t cmd_byte_offset, std::size_t record_bytes, MieMessage& out);
    Step decode_normal_record(const TypeWord& tw, const CommandWord& cmd,
                              const Timestamp& timestamp, uint16_t ts_words,
                              std::size_t cmd_byte_offset, std::size_t record_bytes,
                              MieMessage& out);
    void decode_error_record(const TypeWord& tw, const Timestamp& timestamp, const CommandWord& cmd,
                             std::size_t cmd_byte_offset, uint16_t ts_words,
                             const Optional<double>& delta, MieMessage& out);

    /// This record's DELTA, and the WARN that sometimes goes with it.
    ///
    /// The arithmetic and the per-key state belong to `DeltaTracker`; what is
    /// left here is narration, which is the reader's job and nobody else's.
    Optional<double> delta_for(const CommandWord& cmd, const Timestamp& timestamp);

    void advance_after_yield(std::size_t record_bytes);
    void log_complete() const;

    /// Raise `error`, latching the walk closed first. Never returns -- and the
    /// attribute is load-bearing, not documentation: MSVC compiles this at
    /// `/W4 /WX`, where a statement after a call that cannot return is warning
    /// C4702 and therefore an error. Marking it lets every call site end there
    /// with no unreachable filler.
    MIE_NORETURN void fail(const MieError& error);

    MieFileReader* owner_;
    const uint8_t* data_;
    std::size_t file_len_;
    std::size_t offset_;
    bool done_;

    /// Detected while the iterator was being built (no valid records, an
    /// ambiguous format under --strict, a homogeneous pad). Thrown by the first
    /// `next()` rather than by `iter()`, so that "this file has no records" is a
    /// real failure the caller sees instead of a silently empty stream.
    PendingError pending_error_;

    bool strict_;
    TimestampFormat resolved_format_;
    std::size_t lookahead_records_;
    /// Whether the immediately preceding decoded record carried the Type Word
    /// error bit. This is the whole basis for the 0x2000-vs-0x2001 distinction
    /// on a following SPURIOUS_DATA record, and it is why the reader is the
    /// only place that classification can happen.
    bool prev_was_error_;

    /// Per-RT/MSG `DELTA` state (L2-RDR-009/010/017/018/019).
    ///
    /// Owned by `mie/delta.hpp`, which is what `merge` will use for
    /// `--delta-scope global` when it lands. Rust and Python reached that
    /// second caller with a SECOND tracker keyed differently from the reader's,
    /// and nothing asserted the two agreed on what "the same RT/MSG" meant
    /// (L3-RDR-001). Landing the shared type before the second caller exists is
    /// how C++ skips that.
    DeltaTracker delta_tracker_;

    /// PRA-9: whether the one-time IRIG day-of-year advisory has fired.
    bool warned_irig_day_;
    /// L2-WRT-026 clause 4 / L2-LOG-002: the calendar year in force, or
    /// absent under `doy`.
    Optional<int> calendar_year_;
    /// Highest day-of-year seen so far from a calendar-locked record, for the
    /// clause 4 rollover watch. Absent until the first such record.
    Optional<int> last_irig_day_;
    /// Whether the clause 4 rollover warning has fired for this input.
    bool warned_day_rollover_;

    uint64_t msg_count_;
    uint64_t sync_losses_;

    std::shared_ptr<const std::string> mux_;
};

}  // namespace mie

#endif  // MIE_READER_HPP
