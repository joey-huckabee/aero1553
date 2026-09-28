// SPDX-License-Identifier: Apache-2.0

#define MIE_LOG_MODULE "aero1553::writer"

#include "mie/writer.hpp"

#include <cstdio>
#include <memory>

#include "mie/log.hpp"
#include "mie/text.hpp"

namespace mie {

const std::size_t VENDOR_COLUMN_COUNT = 44;
const std::size_t TOTAL_COLUMN_COUNT = 46;

// The vendor block is columns 1-44, TIME_STAMP through XMT_GAP, in the order
// the DDC tool emits them. ERROR and ERROR_CODE follow, because they are
// decoder additions the vendor tool does not emit at all (L1-OUT-001).
const char* const CSV_HEADER =
    "TIME_STAMP,RT,MSG,"
    "WD01,WD02,WD03,WD04,WD05,WD06,WD07,WD08,WD09,WD10,"
    "WD11,WD12,WD13,WD14,WD15,WD16,WD17,WD18,WD19,WD20,"
    "WD21,WD22,WD23,WD24,WD25,WD26,WD27,WD28,WD29,WD30,"
    "WD31,WD32,"
    "STAT,CMD,MUX,TERM_NAME,BUS,DELTA,IM_GAP,RCV_GAP,XMT_GAP,ERROR,ERROR_CODE\n";

namespace {

/// A 4-digit uppercase hex cell, matching the vendor's STAT / CMD / WDnn form.
std::string hex4(uint16_t value) { return text::hex_upper(value, 4); }

/// True when a record belongs in the errors file.
///
/// `error_label()` is the empty string for a clean record and "ERROR" or
/// "SPURIOUS" otherwise, so this is the same test the CSV column answers --
/// asked once, by name, rather than spelled as a character comparison at the
/// one place that branches on it.
bool is_error_row(const MieMessage& message) { return message.error_label()[0] != 0; }

MieError writer_failure(const std::string& destination, const platform::OsError& err) {
    return MieError::writer_error(destination, err.message, err.code);
}

}  // namespace

// ---------------------------------------------------------------------------
// Sinks
// ---------------------------------------------------------------------------

CsvSink::CsvSink() = default;
CsvSink::~CsvSink() = default;

StdoutCsvSink::StdoutCsvSink() : stream_(stdout) {}

StdoutCsvSink::StdoutCsvSink(std::FILE* stream) : stream_(stream) {}

bool StdoutCsvSink::write(const char* bytes, std::size_t length, platform::OsError& err) {
    err.clear();
    if (length == 0) {
        return true;
    }
    if (std::fwrite(bytes, 1, length, stream_) != length) {
        // A short write on stdout is a closed downstream pipe far more often
        // than a full disk. errno carries which, and MieError::writer_error
        // classifies it -- L2-WRT-018 turns a broken pipe into exit 0.
        platform::capture_stream_error(err);
        return false;
    }
    return true;
}

bool StdoutCsvSink::flush(platform::OsError& err) {
    err.clear();
    if (std::fflush(stream_) != 0) {
        platform::capture_stream_error(err);
        return false;
    }
    return true;
}

std::string StdoutCsvSink::destination() const { return std::string("stdout"); }

AtomicCsvSink::AtomicCsvSink() : open_(false) {}

void AtomicCsvSink::create(const std::string& path, bool no_clobber) {
    platform::OsError err;
    if (!file_.create(path, err)) {
        throw writer_failure(path, err);
    }
    file_.set_commit_mode(no_clobber ? platform::COMMIT_NO_REPLACE : platform::COMMIT_REPLACE);
    path_ = path;
    open_ = true;
}

bool AtomicCsvSink::write(const char* bytes, std::size_t length, platform::OsError& err) {
    return file_.write(bytes, length, err);
}

bool AtomicCsvSink::flush(platform::OsError& err) {
    // AtomicFile flushes as part of commit; there is no separate flush that
    // leaves the file open, and inventing one would mean a second code path
    // through the buffer for no caller.
    err.clear();
    return true;
}

std::string AtomicCsvSink::destination() const { return path_; }

void AtomicCsvSink::commit() {
    platform::OsError err;
    report(file_.commit(err), path_, err);
    open_ = false;
}

std::string AtomicCsvSink::commit_partial() {
    platform::OsError err;
    // The name comes from `partial_path_for` rather than being spelled here, so
    // the path this commits to and the path `commit_targets` pre-flights are
    // the same derivation and cannot drift (L2-WRT-014).
    // Not const: it is returned, and constness would block the move
    // (performance-no-automatic-move).
    std::string partial = partial_path_for(path_);
    report(file_.commit_with_suffix(".partial", err), partial, err);
    open_ = false;
    return partial;
}

void AtomicCsvSink::report(platform::CommitStatus status, const std::string& destination,
                           const platform::OsError& err) {
    if (status == platform::COMMIT_DONE) {
        return;
    }
    if (status == platform::COMMIT_EXISTS) {
        // A refusal, not an OS failure: the destination appeared between the
        // pre-flight and the commit (or was never pre-flighted, as a `.partial`
        // is not). Reporting it as clobber_refused is what makes the racing case
        // and the pre-flight case indistinguishable to the operator, which is
        // the point -- both mean "the destination exists and --no-clobber is on".
        throw MieError::clobber_refused(destination);
    }
    throw writer_failure(destination, err);
}

void AtomicCsvSink::abort() {
    file_.abort();
    open_ = false;
}

// ---------------------------------------------------------------------------
// Row formatting
// ---------------------------------------------------------------------------

std::string csv_quote(const std::string& value) {
    bool needs_quotes = false;
    for (std::size_t i = 0; i < value.size(); ++i) {
        const char c = value[i];
        if (c == ',' || c == '"' || c == '\n' || c == '\r') {
            needs_quotes = true;
            break;
        }
    }
    if (!needs_quotes) {
        return value;
    }

    std::string out;
    out.reserve(value.size() + 2);
    out += '"';
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '"') {
            out += "\"\"";
        } else {
            out += value[i];
        }
    }
    out += '"';
    return out;
}

namespace {

/// Turn a CalendarError into the operator-facing detail of a
/// `MieError::calendar_unavailable`, naming the record it came from.
std::string describe_calendar_error(CalendarError error, const MieMessage& message,
                                    const TimeRender& render) {
    switch (error) {
        case CALENDAR_MISSING_YEAR:
            return "no year was configured; set [output] year or pass --year YYYY";
        case CALENDAR_NO_SUCH_DAY: {
            const std::string year_text =
                render.year.has_value() ? text::decimal(static_cast<uint64_t>(render.year.value()))
                                        : std::string("the configured year");
            return "the record at offset 0x" + text::hex_upper(message.file_offset, 1) +
                   " carries day-of-year " + text::decimal(message.timestamp.irig.day) +
                   ", which does not exist in " + year_text + " (" + year_text +
                   " is not a leap year). The configured year is wrong for this recording";
        }
        case CALENDAR_NOT_LOCKED:
            return "this recording uses the Standard timestamp encoding, a free-running "
                   "counter with no epoch. No year places it on a calendar";
        case CALENDAR_FREERUN:
            return "the record at offset 0x" + text::hex_upper(message.file_offset, 1) +
                   " carries a freerun IRIG timestamp -- the card had no valid IRIG-B lock, "
                   "so its day and time fields are relative rather than calendar-anchored";
        case CALENDAR_OK:
        default: return "unknown";
    }
}

}  // namespace

std::string format_row(const MieMessage& message) { return format_row(message, TimeRender()); }

std::string format_row(const MieMessage& message, const TimeRender& render) {
    std::string row;
    // A 46-column row runs a couple of hundred bytes with a full payload.
    // Reserved once so a row costs at most one allocation.
    row.reserve(256);

    // TIME_STAMP
    //
    // L2-WRT-025: resolved BEFORE any other column is appended, because a
    // calendar rendering can fail where `doy` cannot -- a day-of-year the
    // configured year does not have (L2-WRT-026 clause 3). Resolving it first
    // keeps that refusal from leaving a half-built row.
    std::string timestamp;
    const CalendarError calendar = message.timestamp.format_with(render, timestamp);
    if (calendar != CALENDAR_OK) {
        throw MieError::calendar_unavailable(describe_calendar_error(calendar, message, render));
    }
    row += timestamp;
    row += ',';

    // RT -- empty for a record with no Command Word.
    const Optional<uint8_t> rt = message.rt();
    if (rt.has_value()) {
        row += text::decimal(rt.value());
    }
    row += ',';

    // MSG
    row += message.msg_label();
    row += ',';

    // WD01..WD32. Every one of the 32 cells is emitted whether or not it holds
    // a word: the vendor block is fixed-width, and a short payload must leave
    // empty cells rather than shift STAT left.
    for (std::size_t i = 0; i < MAX_DATA_WORDS; ++i) {
        if (i < message.data_words.size()) {
            row += hex4(message.data_words[i]);
        }
        row += ',';
    }

    // STAT
    if (message.status_word.has_value()) {
        row += hex4(message.status_word.value());
    }
    row += ',';

    // CMD -- the raw word, not the decoded fields.
    if (message.command_word.has_value()) {
        row += hex4(message.command_word.value().raw);
    }
    row += ',';

    // MUX (L2-WRT-020), then TERM_NAME, which is always empty.
    if (message.mux) {
        row += csv_quote(*message.mux);
    }
    row += ",,";

    // BUS
    row += bus_name(message.bus());
    row += ',';

    // DELTA -- an empty cell wherever no honest gap exists: SPURIOUS_DATA, an
    // uncalibrated Standard counter, or a backward clock step.
    if (message.delta.has_value()) {
        row += text::fixed6(message.delta.value());
    }
    row += ',';

    // IM_GAP, RCV_GAP, XMT_GAP -- columns by spec, always empty (L2-WRT-013),
    // and the tail of the vendor block.
    row += ",,,";

    // ERROR then ERROR_CODE: the decoder additions, after the vendor block.
    row += message.error_label();
    row += ',';
    if (message.error_word.has_value()) {
        row += hex4(message.error_word.value());
    }
    row += '\n';

    return row;
}

// ---------------------------------------------------------------------------
// CsvWriter
// ---------------------------------------------------------------------------

CsvWriter::CsvWriter(CsvSink& sink) : sink_(&sink), rows_written_(0), render_() {
    emit(CSV_HEADER, std::char_traits<char>::length(CSV_HEADER));
}

CsvWriter::CsvWriter(CsvSink& sink, const TimeRender& render)
    : sink_(&sink), rows_written_(0), render_(render) {
    emit(CSV_HEADER, std::char_traits<char>::length(CSV_HEADER));
}

void CsvWriter::emit(const char* bytes, std::size_t length) {
    platform::OsError err;
    if (!sink_->write(bytes, length, err)) {
        throw writer_failure(sink_->destination(), err);
    }
}

void CsvWriter::emit(const std::string& text) { emit(text.data(), text.size()); }

void CsvWriter::write_message(const MieMessage& message) {
    emit(format_row(message, render_));
    rows_written_ += 1;
}

uint64_t CsvWriter::finish() {
    platform::OsError err;
    if (!sink_->flush(err)) {
        throw writer_failure(sink_->destination(), err);
    }
    return rows_written_;
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

WriteOptions::WriteOptions()
    : no_clobber(false), allow_partial(false), time_render(), stdout_stream(stdout) {}

PartialCommit::PartialCommit() : offset(0), sync_losses(0) {}

WriteOutcome::WriteOutcome() : normal_count(0), error_count(0) {}

std::string error_path_for(const std::string& output) {
    const std::string parent = platform::path_parent(output);
    const std::string name = platform::path_filename(output);

    // Split on the LAST dot, and only when it is not the leading character: a
    // dotfile like ".mie" is all stem and no extension, matching Rust's
    // Path::file_stem, so it becomes ".mie_errors" rather than "_errors.mie".
    const std::string::size_type dot = name.rfind('.');
    std::string derived;
    if (dot == std::string::npos || dot == 0) {
        derived = name + "_errors";
    } else {
        derived = name.substr(0, dot) + "_errors" + name.substr(dot);
    }
    return parent.empty() ? derived : platform::path_join(parent, derived);
}

std::string partial_path_for(const std::string& destination) { return destination + ".partial"; }

std::vector<std::string> commit_targets(const std::string& output, bool split_errors,
                                        bool allow_partial) {
    std::vector<std::string> targets;
    targets.push_back(output);
    if (split_errors) {
        targets.push_back(error_path_for(output));
    }
    if (allow_partial) {
        // Snapshot the count before appending: the partial of the ERRORS file
        // is a real commit target in split mode (`write_csv_split` commits both
        // as `.partial`), and it is the one an audit forgets. Indexing rather
        // than iterating because the loop grows the vector it reads.
        const std::size_t committed = targets.size();
        for (std::size_t i = 0; i < committed; ++i) {
            targets.push_back(partial_path_for(targets[i]));
        }
    }
    return targets;
}

namespace {

/// L2-WRT-014 then L2-WRT-017, in that order, before anything is opened.
///
/// No filesystem state is touched here. The point is to fail before a temp file
/// exists, so a refused write leaves nothing behind to clean up.
///
/// `split_errors` says whether the caller is `write_csv_split`, which decides
/// both which paths can be committed (via `commit_targets`) and whether the
/// derived errors destination gets its own no-clobber check.
///
/// The collision test covers EVERY commit target and IS the guarantee:
/// L2-WRT-014 is a pre-open rule, and once the mapping is live there is nothing
/// left to refuse.
///
/// The no-clobber test here is NOT the guarantee -- that lives in the commit
/// (L2-WRT-023, platform::COMMIT_NO_REPLACE). `path_exists` answers a question
/// about the past, and between the answer and the rename any other process may
/// create the destination. What this test buys is an EARLY refusal, before a
/// temp file exists and before a whole file is decoded, with the destination
/// named. It covers only the two paths a run definitely creates: `.partial`
/// targets are deliberately left to the commit, so a stale `<dest>.partial`
/// lying around does not refuse a run that was never going to write one.
void preflight_output(const std::string& output, bool split_errors, const WriteOptions& options) {
    if (options.input_path.has_value()) {
        const std::vector<std::string> targets =
            commit_targets(output, split_errors, options.allow_partial);
        for (std::size_t i = 0; i < targets.size(); ++i) {
            bool same = false;
            platform::OsError err;
            // A path that cannot be resolved is not a collision. Decoding to a
            // brand-new file is the common case and must not be blocked by the
            // check that exists to stop decoding ONTO the input.
            if (platform::paths_same_file(options.input_path.value(), targets[i], same, err) &&
                same) {
                throw MieError::input_output_collision(targets[i]);
            }
        }
    }
    if (options.no_clobber) {
        if (platform::path_exists(output)) {
            throw MieError::clobber_refused(output);
        }
        if (split_errors) {
            const std::string errors = error_path_for(output);
            if (platform::path_exists(errors)) {
                throw MieError::clobber_refused(errors);
            }
        }
    }
}

/// Carries an `--allow-partial` sync loss out of the streaming loop.
struct PartialStop {
    bool hit;
    uint64_t offset;
    uint64_t sync_losses;

    PartialStop() : hit(false), offset(0), sync_losses(0) {}
};

/// Pull the next record, converting an allow-partial sync loss into a stop
/// rather than a failure.
///
/// Rethrows everything else: `--allow-partial` is specifically about an
/// unrecoverable MID-FILE sync loss (L1-EXIT-004), not a general "ignore
/// errors" switch, and widening it here would silently turn a rejected file
/// into a short CSV.
bool pull(MessageSource& messages, MieMessage& out, const WriteOptions& options,
          PartialStop& stop) {
    try {
        return messages.next(out);
    } catch (const MieError& error) {
        if (options.allow_partial && error.kind() == KIND_UNRECOVERABLE_SYNC_LOSS) {
            stop.hit = true;
            stop.offset = error.offset().value_or(0);
            stop.sync_losses = error.sync_losses().value_or(0);
            return false;
        }
        throw;
    }
}

}  // namespace

WriteOutcome write_csv(MessageSource& messages, const Optional<std::string>& output,
                       const WriteOptions& options) {
    WriteOutcome outcome;

    if (!output.has_value()) {
        StdoutCsvSink sink(options.stdout_stream);
        CsvWriter writer(sink, options.time_render);
        MieMessage message;
        // allow_partial is ignored here, deliberately: a truncated stdout
        // stream is exactly what the consumer would have seen anyway, and there
        // is no destination to name a `.partial` beside.
        while (messages.next(message)) {
            writer.write_message(message);
        }
        outcome.normal_count = writer.finish();
        MIE_LOG_INFO("wrote " + text::decimal(outcome.normal_count) + " rows to stdout");
        return outcome;
    }

    const std::string& path = output.value();
    preflight_output(path, false, options);

    AtomicCsvSink sink;
    sink.create(path, options.no_clobber);

    PartialStop stop;
    uint64_t rows = 0;
    try {
        CsvWriter writer(sink, options.time_render);
        MieMessage message;
        while (pull(messages, message, options, stop)) {
            writer.write_message(message);
        }
        rows = writer.finish();
    } catch (...) {
        // Anything that escapes leaves the destination untouched: the temp file
        // is unlinked and never renamed. That is the whole point of the
        // temp-and-rename strategy -- a failed decode must not half-overwrite
        // the operator's previous output.
        sink.abort();
        throw;
    }

    outcome.normal_count = rows;
    if (!stop.hit) {
        sink.commit();
        MIE_LOG_INFO("wrote " + text::decimal(rows) + " rows to " + path);
        return outcome;
    }

    const std::string partial_path = sink.commit_partial();
    MIE_LOG_WARN("unrecoverable sync loss at 0x" + text::hex_upper(stop.offset, 1) + " after " +
                 text::decimal(stop.sync_losses) + " recovery attempt(s); wrote " +
                 text::decimal(rows) + " rows to " + partial_path + " (--allow-partial)");

    PartialCommit partial;
    partial.main_path = partial_path;
    partial.offset = stop.offset;
    partial.sync_losses = stop.sync_losses;
    outcome.partial = partial;
    return outcome;
}

WriteOutcome write_csv_split(MessageSource& messages, const std::string& output,
                             const WriteOptions& options) {
    // Covers the errors destination and every `.partial` variant, not just
    // `output`. This used to reason that the errors path needed no collision
    // check because "the path is derived from `output`, which was just
    // checked", which does not follow: a derived path is an ordinary path that
    // can name a DIFFERENT input, and `capture_errors.mie` is a plausible
    // recording name (L2-WRT-014).
    preflight_output(output, true, options);

    const std::string errors_path = error_path_for(output);

    AtomicCsvSink main_sink;
    main_sink.create(output, options.no_clobber);

    AtomicCsvSink errors_sink;
    PartialStop stop;
    uint64_t normal_rows = 0;
    uint64_t error_rows = 0;

    try {
        CsvWriter main_writer(main_sink);
        // Owned by a unique_ptr because the errors writer does not exist until
        // the first error row -- a clean recording must leave no empty
        // _errors.csv behind, and no temp file either. A raw new/delete pair
        // would leak it on any throw from the loop below, which is precisely
        // where a throw is expected.
        std::unique_ptr<CsvWriter> errors_writer;

        MieMessage message;
        while (pull(messages, message, options, stop)) {
            if (!is_error_row(message)) {
                main_writer.write_message(message);
                continue;
            }
            if (errors_writer.get() == nullptr) {
                errors_sink.create(errors_path, options.no_clobber);
                errors_writer.reset(new CsvWriter(errors_sink, options.time_render));
            }
            errors_writer->write_message(message);
        }

        // Both are flushed before EITHER is renamed, so a flush failure cannot
        // leave one destination replaced and the other not.
        if (errors_writer.get() != nullptr) {
            error_rows = errors_writer->finish();
        }
        normal_rows = main_writer.finish();
    } catch (...) {
        main_sink.abort();
        errors_sink.abort();
        throw;
    }

    WriteOutcome outcome;
    outcome.normal_count = normal_rows;
    outcome.error_count = error_rows;

    if (!stop.hit) {
        // MAIN FIRST, and the order is load-bearing (L2-WRT-019). If the errors
        // commit then fails, what is left on disk is the primary artifact --
        // never an orphan errors file next to no main output. If the main
        // commit fails, the errors temp is still un-renamed and gets unlinked,
        // so neither file appears.
        main_sink.commit();
        MIE_LOG_INFO("wrote " + text::decimal(normal_rows) + " normal rows to " + output);
        if (normal_rows == 0) {
            MIE_LOG_WARN("main CSV is empty (header only)");
        }
        if (errors_sink.is_open()) {
            errors_sink.commit();
            MIE_LOG_INFO("wrote " + text::decimal(error_rows) + " error/spurious rows to " +
                         errors_path);
        } else {
            MIE_LOG_INFO("no error/spurious records -- error file not created");
        }
        return outcome;
    }

    // MAIN FIRST, for the same reason as the normal path above and under the
    // same rule (L2-WRT-019). This is the branch that used to run the other way
    // round in all three implementations: an errors `.partial` that committed
    // before a main `.partial` whose rename then failed left the operator an
    // orphan `<dest>_errors.csv.partial` next to no main output at all -- the
    // precise residue the main-first order exists to make impossible. That the
    // normal path got it right and the failure path did not is what a rule
    // stated over "the commit" rather than over "every commit" buys you.
    PartialCommit partial;
    partial.main_path = main_sink.commit_partial();
    if (errors_sink.is_open()) {
        partial.errors_path = errors_sink.commit_partial();
    }
    partial.offset = stop.offset;
    partial.sync_losses = stop.sync_losses;
    outcome.partial = partial;

    MIE_LOG_WARN("unrecoverable sync loss at 0x" + text::hex_upper(stop.offset, 1) + " after " +
                 text::decimal(stop.sync_losses) + " recovery attempt(s); wrote " +
                 text::decimal(normal_rows) + " normal + " + text::decimal(error_rows) +
                 " error rows as partial to " + partial.main_path + " (--allow-partial)");
    return outcome;
}

}  // namespace mie
