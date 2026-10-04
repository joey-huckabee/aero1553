// SPDX-License-Identifier: Apache-2.0
//
// Whole-file CSV writes: atomic commit, the safety gates, --allow-partial, and
// split output.
//
// Row formatting lives in test_writer_rows.cpp, which needs no filesystem. What
// is here is the part that touches disk, and the property it all serves is one
// sentence: THE DESTINATION IS REPLACED ONLY ON SUCCESS. A failed write, a
// refused clobber and an input/output collision must each leave whatever was
// there before exactly as it was.
//
// Files are read back with <cstdio>, never through the writer's own machinery,
// so a bug cannot hide itself by being consistently wrong in both directions.

#include "mie/writer.hpp"

#include <catch2/catch.hpp>

#include <cstdio>
#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/error.hpp"
#include "mie/models.hpp"
#include "temp_path.hpp"
#include "writer_fixtures.hpp"

namespace {

using mie_test::errored;
using mie_test::sample;
using mie_test::split;
using mie_test::spurious;
using mie_test::TempPath;

/// Read a file back with <cstdio>. See the header note on why not AtomicFile.
std::string read_raw(const std::string& path) {
    std::FILE* handle = std::fopen(path.c_str(), "rb");
    REQUIRE(handle != 0);
    std::string out;
    char buffer[4096];
    for (;;) {
        const std::size_t n = std::fread(buffer, 1, sizeof(buffer), handle);
        out.append(buffer, n);
        if (n < sizeof(buffer)) {
            const bool failed = std::ferror(handle) != 0;
            (void)std::fclose(handle);
            REQUIRE_FALSE(failed);
            return out;
        }
    }
}

bool exists(const std::string& path) {
    std::FILE* handle = std::fopen(path.c_str(), "rb");
    if (handle == 0) {
        return false;
    }
    (void)std::fclose(handle);
    return true;
}

void write_raw(const std::string& path, const std::string& contents) {
    std::FILE* handle = std::fopen(path.c_str(), "wb");
    REQUIRE(handle != 0);
    if (!contents.empty()) {
        const std::size_t written = std::fwrite(contents.data(), 1, contents.size(), handle);
        if (written != contents.size()) {
            (void)std::fclose(handle);
            REQUIRE(written == contents.size());
        }
    }
    REQUIRE(std::fclose(handle) == 0);
}

/// Feeds a fixed list of records, optionally throwing once the list runs out.
///
/// The throw is what exercises --allow-partial: the writer has to tell an
/// unrecoverable sync loss apart from every other failure, and only the former
/// becomes a committed `.partial`.
class VectorSource : public mie::MessageSource {
  public:
    explicit VectorSource(const std::vector<mie::MieMessage>& messages)
        : messages_(messages),
          index_(0),
          throws_(false),
          error_(mie::MieError::payload_error(0, "unused")) {}

    void throw_at_end(const mie::MieError& error) {
        throws_ = true;
        error_ = error;
    }

    /// Create `path` holding `contents` as the first record is pulled: after
    /// the writer's pre-flight and before its commit, which is the window only
    /// the commit-time check (L2-WRT-023) can close.
    void plant_on_first(const std::string& path, const std::string& contents) {
        plant_path_ = path;
        plant_contents_ = contents;
    }

    bool next(mie::MieMessage& out) override {
        if (index_ == 0 && !plant_path_.empty()) {
            write_raw(plant_path_, plant_contents_);
        }
        if (index_ < messages_.size()) {
            out = messages_[index_++];
            return true;
        }
        if (throws_) {
            // Terminal, exactly as a real source is: a second call ends the
            // stream rather than throwing again.
            throws_ = false;
            throw error_;
        }
        return false;
    }

  private:
    std::vector<mie::MieMessage> messages_;
    std::size_t index_;
    bool throws_;
    mie::MieError error_;
    std::string plant_path_;
    std::string plant_contents_;
};

std::vector<mie::MieMessage> one(const mie::MieMessage& message) {
    return std::vector<mie::MieMessage>(1, message);
}

mie::Optional<std::string> to(const TempPath& path) {
    return mie::Optional<std::string>(path.str());
}

}  // namespace

// ---------------------------------------------------------------------------
// Single-file output
// ---------------------------------------------------------------------------

TEST_CASE("write_csv emits a header and one row per record", "[writer][L2-WRT-001]") {
    const TempPath out("out.csv");
    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(sample(200));
    VectorSource source(messages);

    const mie::WriteOutcome outcome = mie::write_csv(source, to(out), mie::WriteOptions());

    CHECK(outcome.normal_count == 2);
    CHECK_FALSE(outcome.partial.has_value());

    const std::vector<std::string> lines = split(read_raw(out.str()), '\n');
    REQUIRE(lines.size() == 4);  // header + two rows + the trailing empty
    CHECK(lines[0] + "\n" == std::string(mie::CSV_HEADER));
    CHECK(lines[3].empty());
}

TEST_CASE("inline mode keeps errored and spurious rows in the one CSV", "[writer][L2-ERR-011]") {
    // The default mode, and the one a vendor-CSV diff uses, because the vendor
    // tool also emits a single file.
    const TempPath out("inline.csv");
    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(errored());
    messages.push_back(spurious());
    VectorSource source(messages);

    const mie::WriteOutcome outcome = mie::write_csv(source, to(out), mie::WriteOptions());

    CHECK(outcome.normal_count == 3);
    const std::string csv = read_raw(out.str());
    CHECK(csv.find("SPURIOUS") != std::string::npos);
    CHECK(csv.find("011E") != std::string::npos);
    CHECK_FALSE(exists(mie::error_path_for(out.str())));
}

TEST_CASE("an empty stream still writes the header", "[writer][L1-EXIT-010]") {
    // The empty-recording path: zero records is a valid outcome, and a
    // header-only CSV is what the operator gets at exit 0.
    const TempPath out("empty.csv");
    // A named local, not `VectorSource source(std::vector<...>())`: that is a
    // function declaration, not an object -- the most vexing parse.
    const std::vector<mie::MieMessage> nothing;
    VectorSource source(nothing);

    const mie::WriteOutcome outcome = mie::write_csv(source, to(out), mie::WriteOptions());

    CHECK(outcome.normal_count == 0);
    CHECK(read_raw(out.str()) == std::string(mie::CSV_HEADER));
}

TEST_CASE("the destination is replaced only on success", "[writer][L2-WRT-015]") {
    const TempPath out("replace.csv");
    write_raw(out.str(), "STALE");

    SECTION("a completed write replaces it") {
        VectorSource source(one(sample()));
        mie::write_csv(source, to(out), mie::WriteOptions());
        CHECK(read_raw(out.str()).find("STALE") == std::string::npos);
    }

    SECTION("a failing source leaves it byte for byte") {
        VectorSource source(one(sample()));
        source.throw_at_end(mie::MieError::payload_error(0x40, "planted"));
        // allow_partial is off, so this is a failure and not a .partial. The
        // temp file is unlinked and the destination never renamed over.
        CHECK_THROWS_AS(mie::write_csv(source, to(out), mie::WriteOptions()), mie::MieError);
        CHECK(read_raw(out.str()) == "STALE");
    }
}

TEST_CASE("no-clobber refuses an existing destination", "[writer][L2-WRT-017]") {
    const TempPath out("noclobber.csv");
    write_raw(out.str(), "ORIGINAL");

    mie::WriteOptions options;
    options.no_clobber = true;
    VectorSource source(one(sample()));

    CHECK_THROWS_AS(mie::write_csv(source, to(out), options), mie::MieError);
    // Refused BEFORE anything was opened, so the original survives and no temp
    // file was left behind to clean up.
    CHECK(read_raw(out.str()) == "ORIGINAL");
}

TEST_CASE("writing onto the input is refused", "[writer][L2-WRT-014]") {
    const TempPath path("collide.mie");
    write_raw(path.str(), "INPUT");

    // Decoding in place is unsafe under a memory-mapped reader: the writer
    // would be rewriting the bytes the reader is still walking.
    mie::WriteOptions options;
    options.input_path = path.str();
    VectorSource source(one(sample()));

    CHECK_THROWS_AS(mie::write_csv(source, to(path), options), mie::MieError);
    CHECK(read_raw(path.str()) == "INPUT");
}

TEST_CASE("writing onto an input the ERRORS .partial path resolves to is refused",
          "[writer][L2-WRT-014][L2-WRT-016][L2-WRT-019]") {
    // The FOURTH commit target: the errors file's own `.partial`, written when
    // split mode meets an --allow-partial sync loss. It had only the
    // enumeration test below covering it -- that it appears in a list -- and
    // nothing exercising it end to end. "The other three are checked and this
    // one is in the same list" is a composition argument, and a composition
    // argument ("the path is derived from `output`, which was just checked") is
    // what put the hole here in the first place.
    //
    // Deliberately isolated: with `-o capture.mie` none of the other three
    // targets matches this input, so only the target under test can pass it.
    TempPath dest("capture.mie");
    const std::string victim =
        dest.also_remove(mie::partial_path_for(mie::error_path_for(dest.str())));
    write_raw(victim, "INPUT");

    // Confirm the isolation rather than trusting it: exactly one target may
    // name this file, or the test proves nothing.
    const std::vector<std::string> targets = mie::commit_targets(dest.str(), true, true);
    std::size_t matching = 0;
    for (std::size_t i = 0; i < targets.size(); ++i) {
        if (targets[i] == victim) {
            ++matching;
        }
    }
    CHECK(matching == 1u);

    mie::WriteOptions options;
    options.input_path = victim;

    {
        // Without allow_partial the errors `.partial` is not a target at all.
        VectorSource clean(one(sample()));
        CHECK_NOTHROW(mie::write_csv_split(clean, dest.str(), options));
        CHECK(exists(dest.str()));
        (void)std::remove(dest.str().c_str());
    }

    options.allow_partial = true;
    VectorSource source(one(errored()));
    CHECK_THROWS_AS(mie::write_csv_split(source, dest.str(), options), mie::MieError);
    CHECK(read_raw(victim) == "INPUT");
    CHECK_FALSE(exists(dest.str()));
}

TEST_CASE("commit_targets enumerates every committable path", "[writer][L2-WRT-014]") {
    // The enumeration itself, pinned: main, errors, then their `.partial`
    // variants. The errors file's own `.partial` is the one an audit forgets,
    // and split mode commits it.
    const std::string out = "capture.csv";

    std::vector<std::string> t = mie::commit_targets(out, false, false);
    REQUIRE(t.size() == 1u);
    CHECK(t[0] == "capture.csv");

    t = mie::commit_targets(out, true, false);
    REQUIRE(t.size() == 2u);
    CHECK(t[1] == "capture_errors.csv");

    t = mie::commit_targets(out, false, true);
    REQUIRE(t.size() == 2u);
    CHECK(t[1] == "capture.csv.partial");

    t = mie::commit_targets(out, true, true);
    REQUIRE(t.size() == 4u);
    CHECK(t[0] == "capture.csv");
    CHECK(t[1] == "capture_errors.csv");
    CHECK(t[2] == "capture.csv.partial");
    CHECK(t[3] == "capture_errors.csv.partial");
}

TEST_CASE("writing onto an input the ERRORS path resolves to is refused", "[writer][L2-WRT-014]") {
    // Every path a run could commit is a collision candidate -- not just the
    // destination the operator named. The derived errors path is an ordinary
    // path that can name a DIFFERENT input: `-o capture.mie --separate-errors`
    // derives `capture_errors.mie`, a plausible recording name. Before this,
    // the errors file committed straight over that input and the run exited 0.
    TempPath dest("capture.mie");
    const std::string victim = dest.also_remove(mie::error_path_for(dest.str()));
    write_raw(victim, "INPUT");

    mie::WriteOptions options;
    options.input_path = victim;
    VectorSource source(one(errored()));

    CHECK_THROWS_AS(mie::write_csv_split(source, dest.str(), options), mie::MieError);
    CHECK(read_raw(victim) == "INPUT");
    CHECK_FALSE(exists(dest.str()));
}

TEST_CASE("writing onto an input the .partial path resolves to is refused",
          "[writer][L2-WRT-014][L2-WRT-016]") {
    // `<destination>.partial` is a commit target under --allow-partial, so an
    // input named `out.csv.partial` collides with `-o out.csv`. It is
    // enumerated even though a clean decode never writes one: the guard runs
    // before the output is opened, which is the only point at which refusing is
    // still safe, and by then nobody knows whether the decode will lose sync.
    TempPath dest("out.csv");
    const std::string victim = dest.sibling(".partial");
    write_raw(victim, "INPUT");

    mie::WriteOptions options;
    options.input_path = victim;

    {
        // Without allow_partial there is no `.partial` target, so the same pair
        // of paths is a perfectly ordinary decode.
        VectorSource clean(one(sample()));
        CHECK_NOTHROW(mie::write_csv(clean, to(dest), options));
        CHECK(exists(dest.str()));
        (void)std::remove(dest.str().c_str());
    }

    options.allow_partial = true;
    VectorSource source(one(sample()));
    CHECK_THROWS_AS(mie::write_csv(source, to(dest), options), mie::MieError);
    CHECK(read_raw(victim) == "INPUT");
    CHECK_FALSE(exists(dest.str()));
}

TEST_CASE("a distinct output path is not a collision", "[writer][L2-WRT-014]") {
    // The check must not fire on the ordinary case, where the destination does
    // not exist yet and therefore cannot be canonicalized at all.
    const TempPath input("in.mie");
    const TempPath out("out2.csv");
    write_raw(input.str(), "INPUT");

    mie::WriteOptions options;
    options.input_path = input.str();
    VectorSource source(one(sample()));

    CHECK_NOTHROW(mie::write_csv(source, to(out), options));
    CHECK(exists(out.str()));
}

// ---------------------------------------------------------------------------
// --allow-partial
// ---------------------------------------------------------------------------

TEST_CASE("allow-partial commits what was decoded to a .partial file",
          "[writer][L1-EXIT-004][L3-WRT-002]") {
    TempPath out("partial.csv");
    const std::string partial = out.sibling(".partial");

    mie::WriteOptions options;
    options.allow_partial = true;
    VectorSource source(one(sample()));
    source.throw_at_end(mie::MieError::unrecoverable_sync_loss(0x1234, 3));

    const mie::WriteOutcome outcome = mie::write_csv(source, to(out), options);

    REQUIRE(outcome.partial.has_value());
    CHECK(outcome.partial.value().main_path == partial);
    CHECK(outcome.partial.value().offset == 0x1234);
    CHECK(outcome.partial.value().sync_losses == 3);
    CHECK(outcome.normal_count == 1);

    // The rows decoded before the corruption are there to inspect...
    CHECK(read_raw(partial).find("192:15:54:50.000100") != std::string::npos);
    // ...and the real destination was never created, so a stale previous
    // decode would still be intact beside it.
    CHECK_FALSE(exists(out.str()));
}

TEST_CASE("allow-partial names the reason it stopped", "[writer][L1-EXIT-004][L2-MRG-004]") {
    // A sync loss keeps its offset-and-attempts wording; a merge that left
    // inputs out says so, rather than reporting a sync loss at offset 0 that
    // never occurred.
    mie::WriteOptions options;
    options.allow_partial = true;
    {
        TempPath out("reason-loss.csv");
        (void)out.sibling(".partial");
        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        VectorSource source(one(sample()));
        source.throw_at_end(mie::MieError::unrecoverable_sync_loss(0x1234, 3));
        (void)mie::write_csv(source, to(out), options);
        CHECK(capture.count_containing(
                  "unrecoverable sync loss at 0x1234 after 3 recovery attempt(s); wrote 1 rows "
                  "to ") == 1u);
    }
    {
        TempPath out("reason-dropped.csv");
        (void)out.sibling(".partial");
        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        VectorSource source(one(sample()));
        source.throw_at_end(mie::MieError::merge_inputs_dropped(1, 0, 2));
        const mie::WriteOutcome outcome = mie::write_csv(source, to(out), options);
        REQUIRE(outcome.partial.has_value());
        CHECK(outcome.partial.value().offset == 0u);
        CHECK(outcome.partial.value().sync_losses == 0u);
        CHECK(capture.count_containing("1 of 2 merge inputs could not be read and was left out "
                                       "(see the per-input warnings above); wrote 1 rows to ") ==
              1u);
        CHECK(capture.count_containing("sync loss") == 0u);
    }
    {
        TempPath out("reason-split.csv");
        const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));
        (void)out.sibling(".partial");
        (void)out.also_remove(errors_path + ".partial");
        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        std::vector<mie::MieMessage> messages;
        messages.push_back(sample(100));
        messages.push_back(errored());
        VectorSource source(messages);
        source.throw_at_end(mie::MieError::merge_inputs_dropped(1, 0, 2));
        const mie::WriteOutcome outcome = mie::write_csv_split(source, out.str(), options);
        REQUIRE(outcome.partial.has_value());
        CHECK(outcome.partial.value().errors_path.has_value());
        CHECK(capture.count_containing("1 of 2 merge inputs could not be read and was left out "
                                       "(see the per-input warnings above); wrote 1 normal + 1 "
                                       "error rows as partial to ") == 1u);
    }
}

TEST_CASE("allow-partial does not swallow other failures", "[writer][L1-EXIT-004]") {
    // --allow-partial is specifically about an unrecoverable mid-file sync
    // loss. Widening it into "ignore errors" would silently turn a rejected
    // file into a short CSV that looks complete.
    const TempPath out("notpartial.csv");
    mie::WriteOptions options;
    options.allow_partial = true;
    VectorSource source(one(sample()));
    source.throw_at_end(mie::MieError::unknown_error_code(0x20, 0x0199));

    CHECK_THROWS_AS(mie::write_csv(source, to(out), options), mie::MieError);
    CHECK_FALSE(exists(out.str()));
    CHECK_FALSE(exists(out.str() + ".partial"));
}

// ---------------------------------------------------------------------------
// Split mode
// ---------------------------------------------------------------------------

TEST_CASE("error_path_for derives the errors file name", "[writer][L2-ERR-008]") {
    CHECK(mie::error_path_for("out.csv") == "out_errors.csv");
    CHECK(mie::error_path_for("out") == "out_errors");
    CHECK(mie::error_path_for("archive.tar.gz") == "archive.tar_errors.gz");
    // A dotfile is all stem and no extension, matching Rust's Path::file_stem.
    CHECK(mie::error_path_for(".mie") == ".mie_errors");
}

TEST_CASE("split mode routes clean and errored records apart", "[writer][L2-ERR-008]") {
    TempPath out("split.csv");
    // Registered rather than removed by hand: the errors name is an INFIX, so
    // sibling() cannot express it, and a manual remove would leak the file on
    // any failing assertion below.
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));

    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(errored());
    messages.push_back(spurious());
    messages.push_back(sample(400));
    VectorSource source(messages);

    const mie::WriteOutcome outcome = mie::write_csv_split(source, out.str(), mie::WriteOptions());

    CHECK(outcome.normal_count == 2);
    CHECK(outcome.error_count == 2);

    const std::string main_csv = read_raw(out.str());
    const std::string errors_csv = read_raw(errors_path);
    CHECK(main_csv.find("SPURIOUS") == std::string::npos);
    CHECK(main_csv.find("011E") == std::string::npos);
    CHECK(errors_csv.find("SPURIOUS") != std::string::npos);
    CHECK(errors_csv.find("011E") != std::string::npos);
    // Both files carry the same header: an errors CSV is a CSV.
    CHECK(errors_csv.compare(0, std::string(mie::CSV_HEADER).size(), mie::CSV_HEADER) == 0);
}

TEST_CASE("split mode renders TIME_STAMP the same way in both files",
          "[writer][L2-ERR-008][L2-WRT-025]") {
    // The main writer was once built without the time rendering while the
    // errors writer had it, so `--separate-errors --output-time-format iso`
    // wrote ISO to _errors.csv and day-of-year to the main CSV. A default
    // WriteOptions renders day-of-year either way, which is why the other split
    // tests never saw it.
    TempPath out("split_iso.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));

    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(errored());
    VectorSource source(messages);

    mie::WriteOptions options;
    options.time_render.format = mie::OUTPUT_TIME_ISO;
    options.time_render.year = 2026;
    mie::write_csv_split(source, out.str(), options);

    // Day 192 of 2026 is 11 July. Each file's first data row follows the header
    // line, so search from the end of the header.
    const std::string header(mie::CSV_HEADER);
    const std::string main_csv = read_raw(out.str());
    const std::string errors_csv = read_raw(errors_path);
    CHECK(main_csv.compare(header.size(), 11, "2026-07-11T") == 0);
    CHECK(errors_csv.compare(header.size(), 11, "2026-07-11T") == 0);
    CHECK(main_csv.find("192:") == std::string::npos);
    CHECK(errors_csv.find("192:") == std::string::npos);
}

TEST_CASE("a clean recording leaves no errors file behind", "[writer][L2-ERR-008]") {
    // The errors file is opened LAZILY, on the first error row, so a clean
    // recording produces neither an empty _errors.csv nor a temp file.
    TempPath out("clean.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));

    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(sample(200));
    VectorSource source(messages);

    const mie::WriteOutcome outcome = mie::write_csv_split(source, out.str(), mie::WriteOptions());

    CHECK(outcome.normal_count == 2);
    CHECK(outcome.error_count == 0);
    CHECK(exists(out.str()));
    CHECK_FALSE(exists(errors_path));
}

TEST_CASE("split mode with only errors writes a header-only main CSV", "[writer][L2-ERR-008]") {
    TempPath out("onlyerrors.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));

    VectorSource source(one(spurious()));
    const mie::WriteOutcome outcome = mie::write_csv_split(source, out.str(), mie::WriteOptions());

    CHECK(outcome.normal_count == 0);
    CHECK(outcome.error_count == 1);
    CHECK(read_raw(out.str()) == std::string(mie::CSV_HEADER));
    CHECK(exists(errors_path));
}

TEST_CASE("split mode leaves neither file behind on failure", "[writer][L2-WRT-019]") {
    // Main is committed FIRST so a failed errors commit can never leave an
    // orphan errors file beside no main output. Here nothing commits at all,
    // which is the stronger version of the same guarantee.
    TempPath out("splitfail.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));

    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(spurious());
    VectorSource source(messages);
    source.throw_at_end(mie::MieError::payload_error(0x40, "planted"));

    CHECK_THROWS_AS(mie::write_csv_split(source, out.str(), mie::WriteOptions()), mie::MieError);
    CHECK_FALSE(exists(out.str()));
    CHECK_FALSE(exists(errors_path));
}

TEST_CASE("split mode commits the MAIN .partial first",
          "[writer][L2-WRT-016][L2-WRT-019][L2-WRT-023]") {
    // The --allow-partial half of the main-before-errors rule, and the direct
    // regression for the bug that branch carried: it committed errors first, so
    // an errors `.partial` was already on disk by the time a failing main
    // `.partial` commit unwound -- an orphan forensic artifact with no main
    // output beside it.
    //
    // The main commit is made to fail with --no-clobber and a `<dest>.partial`
    // that appears mid-run (a stale one is refused by the pre-flight, L2-WRT-017,
    // before any commit happens) rather than by planting a directory on the path. There is
    // no mkdir in the platform layer and adding one for a test would widen the
    // surface `assert-platform-confined.sh` exists to keep narrow (the same
    // reasoning test_config.cpp records for its not-a-regular-file case). This
    // spelling is the better test anyway: it is a real supported invocation
    // rather than a planted filesystem oddity, and it pins L2-WRT-023's rule
    // that a `.partial` is a commit target like any other in the same breath.
    TempPath out("splitpartialorder.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));
    const std::string main_partial = out.sibling(".partial");
    const std::string errors_partial = out.also_remove(errors_path + ".partial");

    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(errored());
    VectorSource source(messages);
    source.throw_at_end(mie::MieError::unrecoverable_sync_loss(0x99, 2));
    source.plant_on_first(main_partial, "EARLIER FORENSICS");

    mie::WriteOptions options;
    options.no_clobber = true;
    options.allow_partial = true;

    CHECK_THROWS_AS(mie::write_csv_split(source, out.str(), options), mie::MieError);
    CHECK(read_raw(main_partial) == "EARLIER FORENSICS");
    CHECK_FALSE(exists(errors_partial));
    CHECK_FALSE(exists(out.str()));
    CHECK_FALSE(exists(errors_path));
}

TEST_CASE("split mode leaves the main .partial when the errors .partial is refused",
          "[writer][L2-WRT-016][L2-WRT-019][L2-WRT-023]") {
    // The mirror image. The main `.partial` commits, the errors one is refused,
    // and what is left on disk is the primary artifact -- which is the whole
    // point of the order. The errors `.partial` appears mid-run: a stale one is
    // refused by the pre-flight (L2-WRT-017) before any commit happens.
    TempPath out("splitpartialorder2.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));
    const std::string main_partial = out.sibling(".partial");
    const std::string errors_partial = out.also_remove(errors_path + ".partial");

    std::vector<mie::MieMessage> messages;
    messages.push_back(sample(100));
    messages.push_back(errored());
    VectorSource source(messages);
    source.throw_at_end(mie::MieError::unrecoverable_sync_loss(0x99, 2));
    source.plant_on_first(errors_partial, "EARLIER ERRORS");

    mie::WriteOptions options;
    options.no_clobber = true;
    options.allow_partial = true;

    CHECK_THROWS_AS(mie::write_csv_split(source, out.str(), options), mie::MieError);
    CHECK(read_raw(main_partial).find("192:15:54:50.000100") != std::string::npos);
    CHECK(read_raw(errors_partial) == "EARLIER ERRORS");
}

TEST_CASE("no-clobber refuses a destination that appears mid-decode",
          "[writer][L2-WRT-017][L2-WRT-023]") {
    // The race the pre-flight cannot close: the destination is created while the
    // stream is still draining, which is after the pre-flight and before the
    // commit. Two concurrent runs of the same command both pass the pre-flight,
    // and a replacing rename then lets the second silently destroy the first's
    // output -- the exact outcome --no-clobber exists to prevent.
    const TempPath out("clobberrace.csv");

    class RacingSource : public mie::MessageSource {
      public:
        RacingSource(const mie::MieMessage& message, const std::string& destination)
            : message_(message), destination_(destination), sent_(false) {}

        bool next(mie::MieMessage& target) override {
            if (sent_) {
                return false;
            }
            sent_ = true;
            target = message_;
            // The other process wins the race, right here.
            write_raw(destination_, "THEIRS");
            return true;
        }

      private:
        mie::MieMessage message_;
        std::string destination_;
        bool sent_;
    };

    mie::WriteOptions options;
    options.no_clobber = true;
    RacingSource source(sample(), out.str());

    CHECK_THROWS_AS(mie::write_csv(source, to(out), options), mie::MieError);
    CHECK(read_raw(out.str()) == "THEIRS");
}

TEST_CASE("split mode honours no-clobber on the errors path too",
          "[writer][L2-WRT-017][L2-ERR-008]") {
    // The errors path needs its own check: it is derived from the output, so
    // checking only the output would happily overwrite an existing
    // _errors.csv.
    TempPath out("splitclobber.csv");
    const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));
    write_raw(errors_path, "PREVIOUS ERRORS");

    mie::WriteOptions options;
    options.no_clobber = true;
    VectorSource source(one(spurious()));

    CHECK_THROWS_AS(mie::write_csv_split(source, out.str(), options), mie::MieError);
    CHECK(read_raw(errors_path) == "PREVIOUS ERRORS");
    CHECK_FALSE(exists(out.str()));
}

TEST_CASE("no-clobber pre-flights the .partial names only under allow-partial",
          "[writer][L2-WRT-017]") {
    // The pre-flight refuses every path these options could commit, without
    // predicting which the run will -- so under --allow-partial a stale
    // `.partial` refuses even a run that would never write one. Without
    // --allow-partial no `.partial` can be written, so a stale one is not
    // checked.
    mie::WriteOptions options;
    options.no_clobber = true;

    SECTION("single file") {
        TempPath out("preflightpartial.csv");
        const std::string partial = out.sibling(".partial");
        write_raw(partial, "STALE");

        options.allow_partial = true;
        VectorSource refused(one(sample()));
        try {
            (void)mie::write_csv(refused, to(out), options);
            FAIL("a stale .partial must refuse an --allow-partial run");
        } catch (const mie::MieError& error) {
            CHECK(error.kind() == mie::KIND_CLOBBER_REFUSED);
            CHECK(error.message().find(partial) != std::string::npos);
        }
        CHECK_FALSE(exists(out.str()));
        CHECK(read_raw(partial) == "STALE");

        options.allow_partial = false;
        VectorSource accepted(one(sample()));
        (void)mie::write_csv(accepted, to(out), options);
        CHECK(exists(out.str()));
    }

    SECTION("split mode") {
        TempPath out("preflightpartialsplit.csv");
        const std::string errors_path = out.also_remove(mie::error_path_for(out.str()));
        const std::string errors_partial = out.also_remove(errors_path + ".partial");
        write_raw(errors_partial, "STALE ERRORS");

        options.allow_partial = true;
        VectorSource source(one(sample()));
        try {
            (void)mie::write_csv_split(source, out.str(), options);
            FAIL("a stale errors .partial must refuse an --allow-partial run");
        } catch (const mie::MieError& error) {
            CHECK(error.kind() == mie::KIND_CLOBBER_REFUSED);
            CHECK(error.message().find(errors_partial) != std::string::npos);
        }
        CHECK_FALSE(exists(out.str()));
        CHECK(read_raw(errors_partial) == "STALE ERRORS");
    }
}
