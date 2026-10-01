// SPDX-License-Identifier: Apache-2.0
//
// Streaming CSV output.
//
// Mirrors `rust/src/writer.rs` and `python/src/aero1553/writer.py`.
//
// TWO PROPERTIES DEFINE THIS MODULE, and both are easy to break by accident.
//
// 1. THE COLUMN LAYOUT IS THE VENDOR'S, FOR THE FIRST 44. Column N of a decoded
//    CSV is column N of a DDC-generated CSV, which is what makes a diff against
//    vendor output a validation rather than an exercise in re-mapping. `ERROR`
//    and `ERROR_CODE` are decoder additions with no vendor counterpart, so they
//    go at the TAIL (L1-OUT-001, L2-WRT-001). Through v2.9.0 they sat between
//    `DELTA` and `IM_GAP`, which shifted the three gap columns two positions off
//    their vendor indices -- every positional comparison past `DELTA` was wrong
//    while every column NAME still matched. If you add a column, append it.
//
// 2. ROWS STREAM. A row is formatted and handed to the sink as it arrives;
//    nothing accumulates a container of rows or of messages. That is what makes
//    the O(1)-in-record-count claim true for a multi-gigabyte recording
//    (L3-CPP-011), and it is a design point rather than an optimisation --
//    `AtomicFile` holds one fixed-capacity buffer that is flushed rather than
//    grown.
//
// OUTPUT IS BYTE-EXACT. The sink is opened in binary mode and every line ends
// `\n` on every host (L2-WRT-012); `DELTA` is formatted through
// `mie::text::fixed6`, never `snprintf` directly, so the decimal separator
// cannot follow the host's locale (L3-CPP-007).

#ifndef MIE_WRITER_HPP
#define MIE_WRITER_HPP

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "mie/error.hpp"
#include "mie/models.hpp"
#include "mie/optional.hpp"
#include "mie/platform.hpp"
#include "mie/source.hpp"

namespace mie {

/// The DDC vendor block: `TIME_STAMP` through `XMT_GAP`.
extern const std::size_t VENDOR_COLUMN_COUNT;

/// Every column, vendor block plus the two decoder additions.
extern const std::size_t TOTAL_COLUMN_COUNT;

/// The header row, newline included.
extern const char* const CSV_HEADER;

// ---------------------------------------------------------------------------
// Sinks
// ---------------------------------------------------------------------------

/// Where CSV bytes go.
///
/// An interface rather than a template, which is where this departs from the
/// Rust `CsvWriter<W: Write>`. C++ would have to put the whole writer in a
/// header to stay generic, and the dispatch cost is one indirect call per
/// *write*, not per byte -- against formatting a 46-column row it does not
/// register.
class CsvSink {
  public:
    virtual ~CsvSink();

    /// Append bytes. False on failure, with `err` filled.
    virtual bool write(const char* bytes, std::size_t length, platform::OsError& err) = 0;

    /// Push buffered bytes to the OS.
    virtual bool flush(platform::OsError& err) = 0;

    /// Name for diagnostics -- a path, or "stdout".
    virtual std::string destination() const = 0;

  protected:
    CsvSink();

  private:
    CsvSink(const CsvSink&);
    CsvSink& operator=(const CsvSink&);
};

/// Standard output, for `-o -`.
///
/// Never split (there is only one stdout) and never `.partial` (a truncated
/// stream is what the consumer would have seen anyway). A closed downstream
/// pipe surfaces as a broken-pipe MieError, which L2-WRT-018 turns into exit 0
/// -- `aero1553 decode x.mie | head` is a normal thing to type.
class StdoutCsvSink : public CsvSink {
  public:
    /// Writes to the process's own stdout.
    StdoutCsvSink();
    /// Writes to `stream` instead.
    ///
    /// The stream is a parameter so this sink is reachable from a test. It
    /// otherwise writes to a global, and the only ways to observe that are to
    /// redirect a file descriptor -- `dup2` / `_dup2`, OS surface this tree
    /// confines to the platform backends -- or to spawn a subprocess, which the
    /// GCC 4.8.5 and sanitizer tiers cannot do per case.
    explicit StdoutCsvSink(std::FILE* stream);
    bool write(const char* bytes, std::size_t length, platform::OsError& err) override;
    bool flush(platform::OsError& err) override;
    std::string destination() const override;

  private:
    std::FILE* stream_;
};

/// A file written through the temp-and-rename strategy (L2-WRT-015).
///
/// Thin over `platform::AtomicFile`: the platform layer owns the atomicity, and
/// this owns the CSV-shaped error reporting around it.
class AtomicCsvSink : public CsvSink {
  public:
    AtomicCsvSink();

    /// Create the temp file beside `path`. Throws MieError on failure.
    ///
    /// `no_clobber` selects L2-WRT-023's no-replace commit for EVERY target this
    /// sink can produce -- the destination itself and `<destination>.partial`
    /// alike. That refusal is the guarantee; the pre-flight `path_exists` test
    /// only reports the same condition earlier.
    void create(const std::string& path, bool no_clobber = false);

    bool write(const char* bytes, std::size_t length, platform::OsError& err) override;
    bool flush(platform::OsError& err) override;
    std::string destination() const override;

    /// Move onto the destination. Throws MieError on failure -- including
    /// MieError::clobber_refused when `no_clobber` is set and the destination
    /// exists at the moment of the commit.
    void commit();

    /// Move onto `<destination>.partial` instead, leaving the destination
    /// untouched (L3-WRT-002). Returns the path written. Under `no_clobber` an
    /// existing `.partial` is refused rather than replaced: it is an actual
    /// commit target, so L2-WRT-023 covers it like any other.
    std::string commit_partial();

    /// Discard the temp file. Safe after commit, and safe twice.
    void abort();

    bool is_open() const { return open_; }

  private:
    /// Turn a CommitStatus into the right exception, or nothing. Shared by both
    /// commit entry points so the refusal arm cannot be handled in one and
    /// forgotten in the other.
    static void report(platform::CommitStatus status, const std::string& destination,
                       const platform::OsError& err);

    platform::AtomicFile file_;
    std::string path_;
    bool open_;
};

// ---------------------------------------------------------------------------
// Row writer
// ---------------------------------------------------------------------------

/// Streaming CSV row writer. Emits the header when constructed.
class CsvWriter {
  public:
    /// Writes the header immediately. Throws MieError if that fails.
    ///
    /// `render` selects the TIME_STAMP rendering (L2-WRT-025). It is required:
    /// a one-argument overload that defaulted to day-of-year was how the split
    /// writer's main CSV came to ignore `--output-time-format` while its errors
    /// CSV honoured it. Pass `TimeRender()` for the vendor rendering.
    CsvWriter(CsvSink& sink, const TimeRender& render);

    /// Format and emit one row. Throws MieError on a write failure.
    void write_message(const MieMessage& message);

    /// Flush and report the row count. Throws MieError on a flush failure.
    ///
    /// Separate from the destructor deliberately: flushing is fallible and a
    /// destructor cannot report that. A CsvWriter that is destroyed without
    /// `finish()` has NOT promised its rows reached the sink.
    uint64_t finish();

    uint64_t rows_written() const { return rows_written_; }

  private:
    CsvWriter(const CsvWriter&);
    CsvWriter& operator=(const CsvWriter&);

    void emit(const char* bytes, std::size_t length);
    void emit(const std::string& text);

    CsvSink* sink_;
    uint64_t rows_written_;
    /// L2-WRT-025: how the TIME_STAMP column is rendered.
    TimeRender render_;
};

/// Write one field with RFC4180 minimal quoting, matching Python's
/// `csv.QUOTE_MINIMAL`: a value containing a comma, a double quote, CR or LF is
/// wrapped in quotes with internal quotes doubled; anything else goes verbatim.
///
/// Only the MUX cell can carry such a value -- it comes from a file name -- so
/// this is what keeps MUX output byte-identical across the three
/// implementations rather than merely similar.
std::string csv_quote(const std::string& value);

/// The formatted row for `message`, newline included.
///
/// Exposed because it is the unit worth testing: every column decision lives
/// here, and a test that asserts on a string does not need a file.
std::string format_row(const MieMessage& message);

/// The formatted row for `message` under `render` (L2-WRT-025).
///
/// Throws `MieError::calendar_unavailable` when a calendar rendering cannot be
/// resolved for this record (L2-WRT-026). The timestamp is resolved before any
/// other column is appended, so a refusal cannot leave a half-built row.
std::string format_row(const MieMessage& message, const TimeRender& render);

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/// Output-side safety checks. Defaults are "no checks", so a library caller
/// that wants raw behaviour still gets it.
struct WriteOptions {
    /// Input path for the L2-WRT-014 same-file collision check. Absent skips
    /// it -- there is no input to collide with, or the caller already checked.
    Optional<std::string> input_path;
    /// L2-WRT-017: refuse to overwrite an existing destination.
    bool no_clobber;
    /// L1-EXIT-004 / L2-WRT-016: on an unrecoverable mid-file sync loss, commit
    /// what was decoded as `<destination>.partial` and call the run successful
    /// rather than unlinking the temp and failing.
    bool allow_partial;
    /// L2-WRT-025: how the TIME_STAMP column is rendered. Defaults to the
    /// day-of-year form, so a caller that does not set it gets exactly the
    /// vendor-compatible output every version before v3.0.0 produced.
    TimeRender time_render;
    /// Where a destination-less write goes. Defaults to the process's stdout;
    /// ignored when a destination path is given.
    std::FILE* stdout_stream;

    WriteOptions();
};

/// Where a `--allow-partial` run put its output.
struct PartialCommit {
    std::string main_path;
    /// Set only when split mode produced error rows before the sync loss.
    Optional<std::string> errors_path;
    uint64_t offset;
    uint64_t sync_losses;

    PartialCommit();
};

/// What a successful write produced.
struct WriteOutcome {
    uint64_t normal_count;
    uint64_t error_count;
    /// Present only when `allow_partial` converted a sync loss into success.
    Optional<PartialCommit> partial;

    WriteOutcome();
};

/// Stream every record to one CSV, errored and spurious rows included with
/// their `ERROR` / `ERROR_CODE` columns populated. This is INLINE mode, the
/// default (L2-ERR-011), and the mode a vendor-CSV diff uses.
///
/// `output` absent means stdout, which skips the pre-flight checks (it has no
/// filesystem identity) and ignores `allow_partial`.
WriteOutcome write_csv(MessageSource& messages, const Optional<std::string>& output,
                       const WriteOptions& options);

/// Split output (L2-ERR-008): clean records to `output`, errored and spurious
/// to `<stem>_errors<ext>`.
///
/// The errors file is opened LAZILY, on the first error row, so a clean
/// recording leaves no empty `_errors.csv` behind -- and no temp file either.
WriteOutcome write_csv_split(MessageSource& messages, const std::string& output,
                             const WriteOptions& options);

/// `<stem>_errors<ext>` beside `output`. Exposed so the CLI can name the file
/// in a diagnostic without recomputing the rule.
std::string error_path_for(const std::string& output);

/// `<destination>.partial` -- where an `--allow-partial` run commits the rows
/// decoded before an unrecoverable sync loss (L2-WRT-016).
///
/// Used by both `AtomicCsvSink::commit_partial` (which renames onto it) and
/// `commit_targets` (which pre-flights it), so the path guarded and the path
/// written are one derivation.
std::string partial_path_for(const std::string& destination);

/// Every path a decode run could commit, given its destination and mode.
///
/// The L2-WRT-014 collision guard has to test ALL of them, not just the
/// destination the operator named. A derived path is an ordinary path that can
/// name an ordinary file, and "it was derived from a path we already checked"
/// says nothing about whether it collides with a DIFFERENT input:
/// `-o capture.mie --separate-errors` derives `capture_errors.mie`, which is a
/// perfectly plausible name for one of the recordings being decoded. Both
/// destructive cases were live until this existed -- the errors file and the
/// `.partial` file each committed straight over an input, and the run exited 0.
///
/// `.partial` targets are enumerated even though a clean decode never writes
/// one: the guard runs before the output is opened, which is the only point at
/// which refusing is still safe, and by then nobody knows whether the decode
/// will lose sync. Refusing a run that MIGHT have destroyed an input is the
/// conservative direction.
///
/// Ordering is main, errors, then their `.partial` variants, so the error names
/// the most direct collision when more than one target matches.
std::vector<std::string> commit_targets(const std::string& output, bool split_errors,
                                        bool allow_partial);

}  // namespace mie

#endif  // MIE_WRITER_HPP
