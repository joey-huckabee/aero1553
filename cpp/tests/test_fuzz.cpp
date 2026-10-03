// SPDX-License-Identifier: Apache-2.0
//
// L1-ROB-001: arbitrary bytes must not crash the decoder.
//
// WHY THIS SHAPE, AND NOT libFUZZER. The delivery plan called for libFuzzer
// targets. It is the wrong tool for THIS tree, for three reasons:
//
//   1. libFuzzer needs clang. The defining constraint here is GCC 4.8.5, the
//      SLES 12 system compiler (ADR-0001). A libFuzzer target would run on one
//      of the four C++ tiers and NOT on the one this implementation exists for.
//      A Catch2 test case runs on all of them -- modern g++, GCC 4.8.5, MSVC --
//      and again under ASan/UBSan and valgrind, which is where a genuine memory
//      fault on random input actually surfaces.
//
//   2. The generator below is byte-for-byte the one in
//      `rust/tests/integration.rs` and `python/tests/fuzz_support.py`: same
//      xorshift64, same seed, same size range, same little-endian fill, same
//      draw order. All three implementations therefore see THE SAME INPUTS.
//      L1-ROB-001 is a shared requirement, and testing it three different ways
//      would produce three incomparable results; this way a divergence on
//      identical bytes is detectable.
//
//   3. It is deterministic. Coverage-guided exploration means a required job
//      can fail on an input nobody's change produced, which is the concern
//      was raised against a blocking timed fuzz run when this was scoped.
//
// No committed corpus, for the same reason: the generator reproduces its inputs
// exactly, so a corpus would be storage for something already derivable.
//
// THREE KNOBS, SHARED WITH THE RUST AND PYTHON HARNESSES. All three read the
// same environment variables and mean the same thing by them, because a burn-in
// scoped differently per language cannot be compared across languages -- which
// is the subject of `docs/FUZZING.md` section 1.
//
//   MIE_FUZZ_ITERATIONS   inputs to generate (256 reader/dump, 512 merge)
//   MIE_FUZZ_STREAM_LOGS  `1` / `true` -> leave the decoder's logger at WARN so
//                         its diagnostics stream; anything else -> LEVEL_OFF
//   MIE_FUZZ_SUMMARY      file to append this run's FUZZ-SUMMARY line to
//
// WHY THE LOG KNOB IS THE HARNESS'S JOB AND NOT THE RUNNER'S. The logger writes
// to the stderr file descriptor, which Catch2 does not redirect -- so no runner
// flag controls it in either direction. Before this knob existed a scheduled
// burn-in wrote roughly forty megabytes of WARN lines into every CI run whether
// or not anyone had asked for them.
//
// This file holds THREE cases -- reader, dump and merge input resolution.
// The defaults run in seconds and ride along with every `make
// check`. `make check-fuzz` runs ONLY this file's cases, which is what the
// nightly burn-in uses so its wall time measures fuzzing rather than the whole
// suite; `make check` is deliberately left as the single unparameterised
// command a developer runs. Since the PRNG is deterministic, a burn-in is a
// strict SUPERSET of the default run -- the first 256 inputs are identical.

#include <catch2/catch.hpp>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "mie/dump.hpp"
#include "mie/error.hpp"
#include "mie/log.hpp"
#include "mie/merge.hpp"
#include "mie/optional.hpp"
#include "mie/order.hpp"
#include "mie/platform.hpp"
#include "mie/reader.hpp"
#include "mie/source.hpp"
#include "mie/text.hpp"
#include "temp_path.hpp"

namespace {

/// The seed every fuzz harness in every implementation starts from.
const uint64_t FUZZ_SEED = 0x0DDCD1ECDDC0DEC0ULL;

/// The same xorshift64 the Rust and Python harnesses use. Not a good PRNG; a
/// REPRODUCIBLE one, which is the property that matters here.
uint64_t xorshift64(uint64_t& state) {
    uint64_t x = state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    state = x;
    return x;
}

/// The glob-pattern alphabet the merge harness draws from, shared verbatim with
/// `rust/tests/integration.rs` and `python/tests/fuzz_support.py`.
///
/// A pattern built by lossily UTF-8-decoding random bytes would be the obvious
/// thing, and it is wrong twice over. Random bytes almost never contain `*` or
/// `?`, so the matcher's interesting branches are never reached; and the three
/// languages' lossy decoders do not agree character-for-character on how many
/// U+FFFD an invalid sequence produces, so the counters could diverge without
/// the glob matchers disagreeing about anything.
///
/// `*` and `?` are weighted (three and two slots) because the first version of
/// this harness drew uniformly over patterns up to 95 characters long and
/// matched a probe ZERO times in 512 iterations -- it fuzzed the reject path
/// and nothing else.
///
/// The last two entries are deliberately non-ASCII, spelled as UTF-8 bytes so
/// the source stays ASCII: U+00E9 and U+4E2D. Rust and Python match over
/// scalar values and this matcher advances `?` by a whole UTF-8 character, and
/// this is the surface where that agreement is either real or it is not.
///
/// `/` is there so `expand_glob` sees a directory part at all: without it every
/// pattern was a bare filename, and neither the textual split at the last
/// separator nor the directory-wildcard refusal (L2-MRG-001) was ever reached.
/// `/` and not `\`: a forward slash is a separator on every platform, a
/// backslash only on Windows, so it would split the same pattern differently on
/// the two CI hosts and the counters would legitimately differ.
const std::size_t GLOB_ALPHABET_SIZE = 16;
const char* const GLOB_ALPHABET[GLOB_ALPHABET_SIZE] = {
    "*", "*", "*", "?", "?", ".", "a", "b", "m", "i", "e", "-", "x", "\xC3\xA9", "\xE4\xB8\xAD",
    "/"};

/// Names the generated patterns are matched against: ASCII, Latin-1, CJK.
/// Counted separately so a divergence says which one broke.
const char* const GLOB_PROBES[3] = {"some.name.mie", "caf\xC3\xA9.mie",
                                    "\xE4\xB8\xAD\xE6\x96\x87.mie"};

/// Digits only, explicit ASCII range -- `<cctype>` reads the locale table and
/// is banned tree-wide (scripts/assert-locale-free.sh).
///
/// The default is per-harness because the harnesses cost different amounts per
/// input. Every implementation uses the same default for the same harness,
/// which is what keeps the summary lines comparable.
std::size_t iterations_from_env(std::size_t default_value) {
    const char* raw = std::getenv("MIE_FUZZ_ITERATIONS");
    if (raw == 0) {
        return default_value;
    }
    std::size_t value = 0;
    for (const char* p = raw; *p != '\0'; ++p) {
        if (*p < '0' || *p > '9') {
            return default_value;  // unparseable: fall back rather than guess
        }
        value = value * 10 + static_cast<std::size_t>(*p - '0');
        if (value > 10000000) {
            return 10000000;
        }
    }
    return value == 0 ? default_value : value;
}

/// True when MIE_FUZZ_STREAM_LOGS asks for the decoder's diagnostics.
bool stream_logs_from_env() {
    const char* raw = std::getenv("MIE_FUZZ_STREAM_LOGS");
    if (raw == 0) {
        return false;
    }
    const std::string value(raw);
    return value == "1" || value == "true" || value == "TRUE";
}

/// Draw `size` bytes: eight at a time little-endian, then the tail one at a
/// time.
///
/// The draw ORDER is as much a part of the contract as the PRNG -- Rust and
/// Python consume the stream identically, which is what makes iteration N the
/// same bytes in all three implementations.
std::vector<uint8_t> fill_bytes(uint64_t& state, std::size_t size) {
    std::vector<uint8_t> bytes(size, 0);
    std::size_t j = 0;
    while (j + 8 <= size) {
        const uint64_t r = xorshift64(state);
        for (std::size_t k = 0; k < 8; ++k) {
            bytes[j + k] = static_cast<uint8_t>((r >> (8 * k)) & 0xFF);
        }
        j += 8;
    }
    while (j < size) {
        bytes[j] = static_cast<uint8_t>(xorshift64(state) & 0xFF);
        ++j;
    }
    return bytes;
}

/// Emit this harness's one-line summary.
///
/// The line has the same shape in all three implementations, so the burn-in
/// jobs produce artifacts that can be DIFFED rather than three differently
/// shaped pass messages ("1 passed" vs "2 passed" vs "484 test cases"). On
/// identical inputs the counters should be identical too; a difference is a
/// cross-implementation finding, and comparing them is the cheap precursor to a
/// real differential driver (docs/FUZZING.md 6.1).
///
/// Every field must be path-independent: the three harnesses name their temp
/// files differently, so anything derived from a path measures the harness
/// rather than the decoder.
///
/// Numbers go through `text::decimal` rather than `std::to_string`, which
/// routes through vsnprintf and would take its digits from the locale.
///
/// Built by successive `+=` rather than one concatenation chain: a six-term
/// chain is exactly the shape clang-format versions wrap differently, and the
/// local, WSL and CI toolchains here are three different versions.
void emit_summary(const std::string& harness, std::size_t iterations, const std::string& fields) {
    std::string line = "FUZZ-SUMMARY impl=cpp";
    line += " harness=" + harness;
    line += " seed=0x" + mie::text::hex_upper(FUZZ_SEED, 16);
    line += " iterations=" + mie::text::decimal(static_cast<uint64_t>(iterations));
    line += " " + fields;
    static_cast<void>(std::fputs(line.c_str(), stderr));
    static_cast<void>(std::fputc('\n', stderr));

    const char* target = std::getenv("MIE_FUZZ_SUMMARY");
    if (target == 0) {
        return;
    }
    // Binary append: the artifact is compared byte for byte against the Rust
    // and Python ones, and the Windows CRT would otherwise rewrite the newline.
    std::FILE* handle = std::fopen(target, "ab");
    if (handle == 0) {
        return;  // best effort -- a summary that cannot be written is not a failure
    }
    static_cast<void>(std::fwrite(line.data(), 1, line.size(), handle));
    static_cast<void>(std::fputc('\n', handle));
    static_cast<void>(std::fclose(handle));
}

/// Sets the logger level for a scope and restores it on the way out.
///
/// RAII rather than a call pair, for the reason `log_capture.hpp` gives about
/// its sink: Catch2 runs every case in ONE process, so a case that leaves the
/// global level changed breaks the cases after it, and a `FAIL` unwinding past
/// a restore call would leak it. That is not hypothetical -- silencing the
/// logger here without restoring made `test_log.cpp`'s "the default level is
/// WARN" fail with `4 == 2`, pointing at a file this harness never touches.
class ScopedLogLevel {
  public:
    explicit ScopedLogLevel(mie::log::Level level) : previous_(mie::log::current_level()) {
        mie::log::set_level(level);
    }

    ~ScopedLogLevel() { mie::log::set_level(previous_); }

  private:
    mie::log::Level previous_;
};

/// Adapts a RecordIter to the pipeline's MessageSource contract.
///
/// The CLI has its own copy of this; making the reader implement MessageSource
/// would give it a vtable it does not otherwise need.
class IterSource : public mie::MessageSource {
  public:
    explicit IterSource(mie::RecordIter& iter) : iter_(&iter) {}
    bool next(mie::MieMessage& out) { return iter_->next(out); }

  private:
    mie::RecordIter* iter_;
};

}  // namespace

TEST_CASE("arbitrary bytes never crash the decoder", "[fuzz][robustness][L1-ROB-001]") {
    const ScopedLogLevel log_level(stream_logs_from_env() ? mie::log::LEVEL_WARN
                                                          : mie::log::LEVEL_OFF);

    uint64_t state = FUZZ_SEED;
    const std::size_t iterations = iterations_from_env(256);
    uint64_t total_bytes = 0;
    uint64_t opened = 0;
    uint64_t records = 0;
    uint64_t iter_errors = 0;

    for (std::size_t i = 0; i < iterations; ++i) {
        // 32 B (just above MIN_RECORD_BYTES_STANDARD, so record headers stay
        // reachable) to ~8 KB (so each iteration stays fast).
        const std::size_t size = 32 + static_cast<std::size_t>(xorshift64(state) % 8192);
        const std::vector<uint8_t> bytes = fill_bytes(state, size);
        total_bytes += static_cast<uint64_t>(size);

        INFO("seed=0x0DDCD1ECDDC0DEC0 iteration=" << i << " size=" << size);

        const mie_test::TempFile input("mie-fuzz.mie", bytes);

        // Construction and iteration are caught SEPARATELY, matching the Python
        // harness and Rust's `if let Ok(reader)`. One combined try cannot tell
        // "the file was rejected" from "the walk hit a bad record", and those
        // are the two counters the summary line reports.
        mie::MieFileReader reader;
        bool open_ok = false;
        try {
            reader.open(input.str(), mie::ReaderOptions());
            open_ok = true;
        } catch (const mie::MieError&) {  // NOLINT(bugprone-empty-catch)
            // Empty ON PURPOSE, and the NOLINT is the machine-readable form of
            // saying so. Opening an empty or unrecognisable file lands here and
            // is the decoder behaving as specified. Swallowing it is the
            // assertion -- what this test forbids is any OTHER exception type,
            // which escapes and fails the case.
        }

        if (!open_ok) {
            continue;
        }
        ++opened;

        try {
            mie::RecordIter iter = reader.iter();
            IterSource source(iter);

            // The canonical-order stage is on the fuzzed path deliberately:
            // random bytes readily decode to repeated or all-zero timestamps,
            // which is exactly the equal-timestamp run its max_sort_group cap
            // (L2-WRT-022) exists to bound. A small cap is used so the
            // cap-overflow branch is reachable at all.
            mie::OrderedSource ordered(source, 8);

            mie::MieMessage message;
            uint64_t yielded = 0;
            while (ordered.next(message)) {
                ++yielded;
                ++records;
                // Defence in depth: if the walk ever fails to terminate, this
                // surfaces it as a failure rather than a hung runner.
                //
                // A plain `if` rather than REQUIRE: Catch2 decomposes and
                // counts every REQUIRE it evaluates, and this one sits in the
                // per-RECORD loop. The Rust and Python harnesses spend an
                // `assert!` and an `if` here, so a per-record Catch2 assertion
                // made the C++ harness slower than the other two at the same
                // work. FAIL still reports through Catch2 when it fires.
                if (yielded >= 100000) {
                    FAIL("iterator yielded over 100k items on a "
                         << size << "-byte input -- possible unbounded loop (iteration " << i
                         << ")");
                }
            }
        } catch (const mie::MieError&) {
            // Losing sync mid-stream is the documented error path. Counted, not
            // ignored: it is one of the numbers the three implementations are
            // expected to agree on.
            ++iter_errors;
        }
        // Any OTHER exception type escapes this block and fails the test. That
        // is the point: MieError is the contract, and anything else -- a
        // std::out_of_range from an unchecked index, a std::bad_alloc from a
        // length read straight off the wire -- is a defect.
    }

    std::string fields = "bytes=" + mie::text::decimal(total_bytes);
    fields += " opened=" + mie::text::decimal(opened);
    fields += " open_errors=" + mie::text::decimal(static_cast<uint64_t>(iterations) - opened);
    fields += " records=" + mie::text::decimal(records);
    fields += " iter_errors=" + mie::text::decimal(iter_errors);
    fields += " outcome=ok";
    emit_summary("reader", iterations, fields);

    // The loop above deliberately spends no per-record Catch2 assertion, so
    // without this the case would report "no assertions" on a clean run.
    REQUIRE(opened <= static_cast<uint64_t>(iterations));
}

namespace {

uint64_t count_lines(const std::string& text) {
    uint64_t lines = 0;
    for (std::size_t i = 0; i < text.size(); ++i) {
        if (text[i] == '\n') {
            ++lines;
        }
    }
    return lines;
}

/// A `std::FILE*` closed exactly once, on the way out.
///
/// RAII because the dump below can throw between open and close, and a leaked
/// handle across 25 000 burn-in iterations exhausts the descriptor table --
/// which presents as the decoder failing to open its input, several hundred
/// iterations after the harness caused it.
class OpenFile {
  public:
    explicit OpenFile(const std::string& path) : handle_(std::fopen(path.c_str(), "wb")) {}
    ~OpenFile() { close(); }

    std::FILE* get() const { return handle_; }

    void close() {
        if (handle_ != 0) {
            static_cast<void>(std::fclose(handle_));
            handle_ = 0;
        }
    }

  private:
    OpenFile(const OpenFile&);
    OpenFile& operator=(const OpenFile&);

    std::FILE* handle_;
};

}  // namespace

/// L1-ROB-001 for the `dump` subcommand: the record-aware and raw hex dumps
/// must tolerate arbitrary bytes, letting nothing but a documented MieError
/// escape.
///
/// The record dump reads headers under a bounds guard and slices to the record
/// extent for the body -- it never reads payload by a Command Word's
/// `data_word_count`, so it has no over-claim/overrun class of its own. This
/// case guards that property against regression. Sizes are skewed small to
/// exercise the truncation and loop-guard paths densely, and zero-length inputs
/// reach the empty-file rejection.
///
/// Mirrors `dump_arbitrary_bytes_never_panics` in Rust and
/// `test_dump_arbitrary_bytes_never_raise_unexpected_exceptions` in Python.
/// Output volume is counted in LINES, not bytes: all three dumps print the
/// input path in their header and the three harnesses name their temp files
/// differently, so a byte count measures the path rather than the decoder.
TEST_CASE("dump tolerates arbitrary bytes", "[fuzz][robustness][L1-ROB-001][L2-CLI-009]") {
    const ScopedLogLevel log_level(stream_logs_from_env() ? mie::log::LEVEL_WARN
                                                          : mie::log::LEVEL_OFF);

    uint64_t state = FUZZ_SEED;  // same seed family as the reader harness
    const std::size_t iterations = iterations_from_env(256);
    uint64_t total_bytes = 0;
    uint64_t records_errors = 0;
    uint64_t records_lines = 0;
    uint64_t raw_errors = 0;
    uint64_t raw_lines = 0;

    for (std::size_t i = 0; i < iterations; ++i) {
        const std::size_t size = static_cast<std::size_t>(xorshift64(state) % 512);
        const std::vector<uint8_t> bytes = fill_bytes(state, size);
        total_bytes += static_cast<uint64_t>(size);

        INFO("seed=0x0DDCD1ECDDC0DEC0 iteration=" << i << " size=" << size);

        const mie_test::TempFile input("mie-dumpfuzz.mie", bytes);

        {
            const mie_test::TempPath out("dump-fuzz-records");
            OpenFile file(out.str());
            if (file.get() == 0) {
                FAIL("could not open a temp file for the record dump");
            }
            try {
                mie::dump::hex_dump_records(input.str(), mie::Optional<uint64_t>(64), 0,
                                            file.get());
            } catch (const mie::MieError&) {
                // Empty or unreadable input is the documented error path.
                ++records_errors;
            }
            file.close();
            std::string text;
            if (mie_test::read_file(out.str(), text)) {
                records_lines += count_lines(text);
            }
        }

        {
            const mie_test::TempPath out("dump-fuzz-raw");
            OpenFile file(out.str());
            if (file.get() == 0) {
                FAIL("could not open a temp file for the raw dump");
            }
            try {
                mie::dump::hex_dump_raw(input.str(), 0, mie::Optional<std::size_t>(), file.get());
            } catch (const mie::MieError&) {
                ++raw_errors;
            }
            file.close();
            std::string text;
            if (mie_test::read_file(out.str(), text)) {
                raw_lines += count_lines(text);
            }
        }
        // Any OTHER exception type escapes and fails the case. That is the
        // point: MieError is the contract.
    }

    std::string fields = "bytes=" + mie::text::decimal(total_bytes);
    fields += " records_errors=" + mie::text::decimal(records_errors);
    fields += " records_lines=" + mie::text::decimal(records_lines);
    fields += " raw_errors=" + mie::text::decimal(raw_errors);
    fields += " raw_lines=" + mie::text::decimal(raw_lines);
    fields += " outcome=ok";
    emit_summary("dump", iterations, fields);

    REQUIRE(records_errors <= static_cast<uint64_t>(iterations));
}

/// L1-ROB-001 for the merge input-resolution surface: a manifest of arbitrary
/// bytes, and an arbitrary glob pattern driven through the matcher and the
/// directory expansion, must tolerate anything.
///
/// `expand_glob` is called for crash-safety only and its result is deliberately
/// NOT counted: it reads the working directory, so what it returns depends on
/// where the suite ran, and a summary field has to mean the same thing in every
/// implementation on every host.
///
/// The glob matcher is hand-rolled three times, and the C++ one advances `?` by
/// a whole UTF-8 character so it agrees with Rust's and Python's matching over
/// scalar values. The non-ASCII alphabet entries and probes below are the part
/// of this harness that tests that claim rather than restating it.
TEST_CASE("merge input resolution tolerates arbitrary bytes",
          "[fuzz][robustness][L1-ROB-001][L2-MRG-001]") {
    const ScopedLogLevel log_level(stream_logs_from_env() ? mie::log::LEVEL_WARN
                                                          : mie::log::LEVEL_OFF);

    uint64_t state = FUZZ_SEED;
    // 512 by default rather than the reader harness's 256: each iteration is
    // cheap (no decode, no mmap) and the matcher has more branches than 256
    // inputs comfortably cover. The shared knob still overrides.
    const std::size_t iterations = iterations_from_env(512);

    uint64_t total_bytes = 0;
    uint64_t manifest_ok = 0;
    uint64_t manifest_errors = 0;
    uint64_t manifest_paths = 0;
    uint64_t glob_hits[3] = {0, 0, 0};
    uint64_t glob_refused = 0;

    for (std::size_t i = 0; i < iterations; ++i) {
        const std::size_t size = static_cast<std::size_t>(xorshift64(state) % 96);
        const std::vector<uint8_t> bytes = fill_bytes(state, size);
        total_bytes += static_cast<uint64_t>(size);

        // The pattern is drawn separately from the manifest bytes, and short:
        // the two surfaces want different input shapes, and deriving one from
        // the other means neither gets the shape it needs.
        const std::size_t pattern_len = static_cast<std::size_t>(xorshift64(state) % 12);
        const std::vector<uint8_t> pattern_bytes = fill_bytes(state, pattern_len);
        std::string pattern;
        for (std::size_t k = 0; k < pattern_bytes.size(); ++k) {
            pattern += GLOB_ALPHABET[pattern_bytes[k] % GLOB_ALPHABET_SIZE];
        }

        INFO("seed=0x0DDCD1ECDDC0DEC0 iteration=" << i << " size=" << size
                                                  << " pattern=" << pattern);

        const mie_test::TempFile manifest("mie-mergefuzz.txt", bytes);

        std::vector<std::string> paths;
        mie::platform::OsError err;
        if (mie::merge::read_manifest(manifest.str(), paths, err)) {
            ++manifest_ok;
            manifest_paths += static_cast<uint64_t>(paths.size());
        } else {
            // Non-UTF-8 content is a documented failure, not a crash.
            ++manifest_errors;
        }

        for (std::size_t p = 0; p < 3; ++p) {
            if (mie::merge::glob_match(pattern, GLOB_PROBES[p])) {
                ++glob_hits[p];
            }
        }

        // What it returns is crash-safety only (see above), but WHETHER it
        // refuses the pattern as a directory wildcard is decided before any
        // I/O, from the pattern's text alone -- so that is comparable across
        // implementations and hosts.
        std::vector<std::string> expanded;
        mie::platform::OsError glob_err;
        if (!mie::merge::expand_glob(pattern, expanded, glob_err) &&
            glob_err.code == mie::merge::GLOB_INVALID_PATTERN) {
            ++glob_refused;
        }
    }

    std::string fields = "bytes=" + mie::text::decimal(total_bytes);
    fields += " manifest_ok=" + mie::text::decimal(manifest_ok);
    fields += " manifest_errors=" + mie::text::decimal(manifest_errors);
    fields += " manifest_paths=" + mie::text::decimal(manifest_paths);
    fields += " glob_ascii=" + mie::text::decimal(glob_hits[0]);
    fields += " glob_latin1=" + mie::text::decimal(glob_hits[1]);
    fields += " glob_cjk=" + mie::text::decimal(glob_hits[2]);
    fields += " glob_refused=" + mie::text::decimal(glob_refused);
    fields += " outcome=ok";
    emit_summary("merge", iterations, fields);

    REQUIRE(manifest_ok + manifest_errors == static_cast<uint64_t>(iterations));
}
