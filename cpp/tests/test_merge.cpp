// SPDX-License-Identifier: Apache-2.0
//
// Merge tests: input resolution, the k-way order, and the rejections.
//
// The shared conformance oracles already prove the merged CSV is byte-correct.
// What they cannot reach is everything around it: a glob that matches nothing,
// a manifest full of comments, an input set over the cap, two recorders whose
// clocks disagree, a file that steps backward in time. Those are the conditions
// an operator running a batch actually hits, and none of them produces a CSV to
// compare against.

#include "mie/merge.hpp"

#include <catch2/catch.hpp>

#include <cstdio>
#include <string>
#include <vector>

#include "log_capture.hpp"
#include "mie/config.hpp"
#include "mie/error.hpp"
#include "mie/reader.hpp"
#include "mie/text.hpp"
#include "record_fixtures.hpp"
#include "temp_path.hpp"

namespace {

using mie::merge::glob_match;
using mie_test::TempFile;
using mie_test::TempPath;

/// A recording of `count` BC-to-RT records, one every 100 us from `start_us`.
std::vector<uint8_t> recording(uint32_t start_us, uint32_t count, uint8_t rt = 15) {
    std::vector<uint16_t> words;
    for (uint32_t i = 0; i < count; ++i) {
        mie_test::append(words, mie_test::bc_to_rt(rt, 11, 2, start_us + i * 100));
    }
    return mie_test::finish(words);
}

mie::ReaderOptions irig_options() {
    mie::ReaderOptions options;
    // Pinned rather than auto-detected: these fixtures are small, and an
    // ambiguous probe would make the test about detection instead of merging.
    options.input_time_format = mie::TIMESTAMP_IRIG;
    return options;
}

/// Drain a merge into the timestamps it produced, in order.
std::vector<uint64_t> merged_order(const std::vector<mie::MieFileReader*>& readers,
                                   const mie::merge::MergeOptions& options) {
    mie::merge::MergedSource source(readers, options);
    std::vector<uint64_t> out;
    mie::MieMessage message;
    while (source.next(message)) {
        out.push_back(message.timestamp.irig.to_total_microseconds());
    }
    return out;
}

}  // namespace

TEST_CASE("glob_match implements exactly the documented wildcards", "[merge][L3-CPP-019]") {
    SECTION("literals match whole strings only") {
        CHECK(glob_match("a.mie", "a.mie"));
        CHECK_FALSE(glob_match("a.mie", "xa.mie"));
        CHECK_FALSE(glob_match("a.mie", "a.mie.bak"));
        CHECK(glob_match("", ""));
        CHECK_FALSE(glob_match("", "a"));
    }

    SECTION("* matches any run, including an empty one") {
        CHECK(glob_match("*", ""));
        CHECK(glob_match("*", "anything"));
        CHECK(glob_match("*.mie", "a.mie"));
        CHECK(glob_match("*.mie", ".mie"));
        CHECK(glob_match("a*", "a"));
        CHECK(glob_match("a*c", "ac"));
        CHECK(glob_match("a*c", "abbbc"));
        CHECK_FALSE(glob_match("a*c", "abbb"));
    }

    SECTION("several stars backtrack correctly") {
        // The case a naive left-to-right matcher gets wrong: the first `*` must
        // give characters back so the later literals can line up.
        CHECK(glob_match("*a*b*", "xxayybzz"));
        CHECK(glob_match("*a*b", "ab"));
        CHECK_FALSE(glob_match("*a*b", "ba"));
        CHECK(glob_match("**", "abc"));
        CHECK(glob_match("*flight*.mie", "2026-flight-07.mie"));
    }

    SECTION("? matches exactly one character, never zero") {
        CHECK(glob_match("?", "a"));
        CHECK_FALSE(glob_match("?", ""));
        CHECK_FALSE(glob_match("?", "ab"));
        CHECK(glob_match("a?c", "abc"));
        CHECK_FALSE(glob_match("a?c", "ac"));
    }

    SECTION("? consumes a whole UTF-8 character, not one byte") {
        // The cross-implementation trap. Rust and Python match over Unicode
        // scalar values, so `a?c` matches "aXc" where X is one non-ASCII
        // character. A byte-wise matcher would need TWO `?` for a two-byte
        // character and would silently disagree on any non-ASCII filename.
        const std::string two_byte = "a\xC2\xA7";  // U+00A7 SECTION SIGN
        CHECK(glob_match("a?", two_byte));
        CHECK_FALSE(glob_match("a??", two_byte));

        const std::string four_byte = "\xF0\x9F\x9B\xA9";  // U+1F6E9, one character
        CHECK(glob_match("?", four_byte));
        CHECK(glob_match("*", four_byte));
    }

    SECTION("no other character is special") {
        CHECK(glob_match("a.b", "a.b"));
        CHECK_FALSE(glob_match("a.b", "axb"));  // '.' is a literal, not "any"
        CHECK(glob_match("[abc]", "[abc]"));    // no character classes
        CHECK(glob_match("{a,b}", "{a,b}"));    // no brace expansion
    }
}

/// The manifest grammar, pinned exactly, because leaving it at "one path per
/// line" is how three implementations came to disagree, a different way each.
///
/// Every one was found by the merge fuzz harness comparing its FUZZ-SUMMARY
/// counters against Rust's and Python's, and every one is now spelled out in
/// L2-MRG-001:
///
///   * `\n` is the ONLY line separator. Python used `str.splitlines()`, which
///     also breaks on vertical tab, form feed, U+0085 and U+2028/9 -- none of
///     which ends a line in a manifest, all of which are legal in a POSIX
///     filename.
///   * At most ONE trailing `\r` is stripped, so CRLF works and a filename
///     containing a bare CR survives. THIS implementation dropped every `\r`
///     in the line, and Python's reader translated a lone `\r` to `\n` before
///     the parser ever saw it.
///   * The text after the LAST `\n` is a line, read no differently for missing
///     its terminator. Rust used `str::lines()`, which strips the CR only when
///     an `\n` actually followed, so a truncated CRLF manifest ending
///     "b.mie\r" named a path with a CR in it there and `b.mie` here.
///   * Trimming is ASCII space and tab ONLY. Rust's `str::trim` and Python's
///     `str.strip` also remove U+00A0, U+3000 and the rest; this
///     implementation is locale-free by rule and cannot, so two
///     implementations silently edited a filename the third passed through.
///   * A manifest is a TEXT file. This one accepted arbitrary bytes as paths;
///     the other two refuse ill-formed UTF-8, and 498 of 512 generated
///     manifests were rejected there and decoded here.
///
/// Mirrors `read_manifest_grammar_is_exactly_specified` in Rust and Python.
TEST_CASE("read_manifest grammar is exactly specified", "[merge][L2-MRG-001][L3-CPP-019]") {
    SECTION("only newline separates lines") {
        // A form feed, a vertical tab and U+0085 are all part of the filename.
        const TempFile ff("mie-manifest-ff.txt", std::string("a.mie\014b.mie\n"));
        std::vector<std::string> paths;
        mie::platform::OsError err;
        REQUIRE(mie::merge::read_manifest(ff.str(), paths, err));
        REQUIRE(paths.size() == 1u);
        CHECK(paths[0] == std::string("a.mie\014b.mie"));

        const TempFile nel("mie-manifest-nel.txt", std::string("a.mie\302\205b.mie\n"));
        REQUIRE(mie::merge::read_manifest(nel.str(), paths, err));
        REQUIRE(paths.size() == 1u);
        CHECK(paths[0] == std::string("a.mie\302\205b.mie"));
    }

    SECTION("one trailing carriage return is the terminator, an interior one is not") {
        const TempFile crlf("mie-manifest-crlf.txt", std::string("a.mie\r\nb.mie\r\n"));
        std::vector<std::string> paths;
        mie::platform::OsError err;
        REQUIRE(mie::merge::read_manifest(crlf.str(), paths, err));
        REQUIRE(paths.size() == 2u);
        CHECK(paths[0] == "a.mie");
        CHECK(paths[1] == "b.mie");

        const TempFile inner("mie-manifest-cr.txt", std::string("a\rb.mie\n"));
        REQUIRE(mie::merge::read_manifest(inner.str(), paths, err));
        REQUIRE(paths.size() == 1u);
        CHECK(paths[0] == std::string("a\rb.mie"));

        // The last line counts even without its terminator, and its CR is
        // stripped like any other. Rust used `str::lines()`, which strips the
        // CR only when an `\n` actually followed it, so the one-byte manifest
        // "\r" was a path there and none here.
        const TempFile tail("mie-manifest-crtail.txt", std::string("a.mie\r\nb.mie\r"));
        REQUIRE(mie::merge::read_manifest(tail.str(), paths, err));
        REQUIRE(paths.size() == 2u);
        CHECK(paths[0] == "a.mie");
        CHECK(paths[1] == "b.mie");

        const TempFile bare("mie-manifest-crbare.txt", std::string("\r"));
        REQUIRE(mie::merge::read_manifest(bare.str(), paths, err));
        CHECK(paths.empty());
    }

    SECTION("ASCII blanks are trimmed and Unicode spaces are not") {
        const TempFile blanks("mie-manifest-blank.txt", std::string(" \ta.mie\t \n"));
        std::vector<std::string> paths;
        mie::platform::OsError err;
        REQUIRE(mie::merge::read_manifest(blanks.str(), paths, err));
        REQUIRE(paths.size() == 1u);
        CHECK(paths[0] == "a.mie");

        // U+00A0 NO-BREAK SPACE is part of the name, not padding.
        const TempFile nbsp("mie-manifest-nbsp.txt", std::string("\302\240a.mie\n"));
        REQUIRE(mie::merge::read_manifest(nbsp.str(), paths, err));
        REQUIRE(paths.size() == 1u);
        CHECK(paths[0] == std::string("\302\240a.mie"));
    }

    SECTION("ill-formed UTF-8 is refused, not decoded") {
        std::vector<std::string> paths;
        mie::platform::OsError err;

        const TempFile bad("mie-manifest-bad.txt", std::string("\377\376\na.mie\n"));
        CHECK_FALSE(mie::merge::read_manifest(bad.str(), paths, err));
        CHECK_FALSE(err.ok());

        // The shapes a length-driven decoder waves through: an overlong
        // encoding of '/', a lone surrogate half, and a truncated sequence.
        const TempFile overlong("mie-manifest-overlong.txt", std::string("\xC0\xAF\n"));
        CHECK_FALSE(mie::merge::read_manifest(overlong.str(), paths, err));

        const TempFile surrogate("mie-manifest-surrogate.txt", std::string("\xED\xA0\x80\n"));
        CHECK_FALSE(mie::merge::read_manifest(surrogate.str(), paths, err));

        const TempFile truncated("mie-manifest-truncated.txt", std::string("a\xE4\xB8"));
        CHECK_FALSE(mie::merge::read_manifest(truncated.str(), paths, err));

        // Well-formed multi-byte content still parses.
        const TempFile good("mie-manifest-utf8.txt", std::string("\xE4\xB8\xAD.mie\n"));
        REQUIRE(mie::merge::read_manifest(good.str(), paths, err));
        REQUIRE(paths.size() == 1u);
        CHECK(paths[0] == std::string("\xE4\xB8\xAD.mie"));
    }
}

TEST_CASE("read_manifest keeps order and ignores comments", "[merge][L3-CPP-019]") {
    SECTION("blank lines, comments and surrounding blanks are dropped") {
        const TempFile manifest("mie-manifest.txt",
                                std::string("# a comment\n"
                                            "\n"
                                            "   \n"
                                            "  first.mie  \n"
                                            "second.mie\n"
                                            "   # an indented comment\n"
                                            "third.mie"));  // no trailing newline
        std::vector<std::string> paths;
        mie::platform::OsError err;
        REQUIRE(mie::merge::read_manifest(manifest.str(), paths, err));
        REQUIRE(paths.size() == 3u);
        // Order is the FILE's order and is never sorted: a manifest is how an
        // operator states an order explicitly, so sorting it would discard the
        // one thing it is for.
        CHECK(paths[0] == "first.mie");
        CHECK(paths[1] == "second.mie");
        CHECK(paths[2] == "third.mie");
    }

    SECTION("CRLF line endings are handled") {
        // A manifest is a text file an operator may well have written on
        // Windows, and a trailing CR would become part of the filename.
        const TempFile manifest("mie-manifest-crlf.txt", std::string("a.mie\r\nb.mie\r\n"));
        std::vector<std::string> paths;
        mie::platform::OsError err;
        REQUIRE(mie::merge::read_manifest(manifest.str(), paths, err));
        REQUIRE(paths.size() == 2u);
        CHECK(paths[0] == "a.mie");
        CHECK(paths[1] == "b.mie");
    }

    SECTION("an empty manifest is not an error, just empty") {
        const TempFile manifest("mie-manifest-empty.txt", std::string("# only a comment\n"));
        std::vector<std::string> paths;
        mie::platform::OsError err;
        REQUIRE(mie::merge::read_manifest(manifest.str(), paths, err));
        CHECK(paths.empty());
    }

    SECTION("a missing manifest reports the I/O failure") {
        const TempPath absent("mie-manifest-absent.txt");
        std::vector<std::string> paths;
        mie::platform::OsError err;
        CHECK_FALSE(mie::merge::read_manifest(absent.str(), paths, err));
        CHECK_FALSE(err.ok());
    }
}

TEST_CASE("expand_glob returns matching files in a deterministic order", "[merge][L3-CPP-019]") {
    // Files are created straight in the temp root under one unique prefix,
    // rather than in a directory of their own. Making a directory is not one of
    // the five concerns the platform layer owns, and growing that layer to a
    // sixth for the benefit of a test would weaken the confinement argument
    // `scripts/assert-platform-confined.sh` exists to enforce.
    TempPath anchor("glob-anchor");
    const std::string base = anchor.str();
    mie::platform::OsError err;

    // Created out of alphabetical order on purpose: the result must not depend
    // on the order the filesystem enumerates them in, or two hosts would merge
    // the same directory differently.
    const char* const leaves[] = {"-c.mie", "-a.mie", "-b.mie", "-notes.txt"};
    for (std::size_t i = 0; i < sizeof(leaves) / sizeof(leaves[0]); ++i) {
        const std::string path = anchor.also_remove(base + leaves[i]);
        std::FILE* handle = std::fopen(path.c_str(), "wb");
        REQUIRE(handle != NULL);
        REQUIRE(std::fputs("x", handle) >= 0);
        REQUIRE(std::fclose(handle) == 0);
    }

    SECTION("wildcards apply to the filename and results are sorted") {
        std::vector<std::string> found;
        REQUIRE(mie::merge::expand_glob(base + "-*.mie", found, err));
        REQUIRE(found.size() == 3u);
        CHECK(found[0] == base + "-a.mie");
        CHECK(found[1] == base + "-b.mie");
        CHECK(found[2] == base + "-c.mie");
    }

    SECTION("the separator the pattern used is the one that comes back") {
        // Regression pin. The prefix was rebuilt with a hardcoded '/', so a
        // Windows pattern returned `C:\dir/file.mie`. Windows OPENS that
        // happily, so every functional check passed and only the paths the tool
        // reported back -- in log lines and error messages -- were mangled.
        std::vector<std::string> found;
        REQUIRE(mie::merge::expand_glob(base + "-*.mie", found, err));
        REQUIRE_FALSE(found.empty());
        for (std::size_t i = 0; i < found.size(); ++i) {
            INFO(found[i]);
            // The result starts with the pattern's own directory text, byte for
            // byte, separator included.
            CHECK(found[i].compare(0, base.size(), base) == 0);
        }
    }

    SECTION("a glob matching nothing is empty, not a failure") {
        // The distinction matters: the CLI turns "matched nothing" into a usage
        // error naming the pattern, which it could not do if this reported I/O.
        std::vector<std::string> found;
        REQUIRE(mie::merge::expand_glob(base + "-*.nope", found, err));
        CHECK(found.empty());
        CHECK(err.ok());
    }

    SECTION("a missing directory is a failure") {
        std::vector<std::string> found;
        CHECK_FALSE(mie::merge::expand_glob(base + "-no-such-dir/*.mie", found, err));
    }
}

TEST_CASE("expand_glob matches files and never directories", "[merge][L2-MRG-001][L3-CPP-033]") {
    // A glob resolves to FILES. `list_directory` returns every entry -- this
    // implementation used to match them all, so a DIRECTORY named `archive.mie`
    // became an input here and was invisible to Rust and Python; the merge then
    // failed to map it, and a batch the other two decoded whole came back short.
    //
    // The directory used is the temp root itself, globbed from ITS parent. There
    // is no mkdir in the platform layer and adding one for a test would widen
    // the surface `scripts/assert-platform-confined.sh` exists to keep narrow
    // (the same reasoning test_config.cpp records for its not-a-regular-file
    // case). The temp root is a directory that is guaranteed to exist and to
    // have a parent, which is exactly what this needs.
    const std::string root = mie_test::temp_root();
    const std::string parent = mie::platform::path_parent(root);
    const std::string leaf = mie::platform::path_filename(root);
    // A root with no parent (a bare "/", or a drive root) has nothing to
    // enumerate from; there is no case to run rather than a case that fails.
    if (parent.empty() || leaf.empty()) {
        SUCCEED("temp root has no enumerable parent on this host");
        return;
    }

    std::vector<std::string> found;
    mie::platform::OsError err;
    REQUIRE(mie::merge::expand_glob(mie::platform::path_join(parent, leaf), found, err));
    // The pattern has no wildcards, so the ONLY thing it can match is the temp
    // root -- and the temp root is a directory. An empty result is the whole
    // assertion.
    CHECK(found.empty());
    CHECK(err.ok());
}

#ifndef _WIN32
TEST_CASE("expand_glob does not treat a backslash as a separator on POSIX",
          "[merge][L2-MRG-001][L3-CPP-033]") {
    // On POSIX a backslash is an ordinary filename character. This
    // implementation split on it regardless of platform, so
    // `--glob 'odd\name*.mie'` was one pattern in the current directory to Rust
    // and Python -- whose Path types agree with the platform -- and a
    // `name*.mie` pattern inside a directory called `odd` here.
    //
    // Windows genuinely disagrees (a backslash IS a separator there, and is not
    // a legal filename character), so this is compiled out rather than skipped:
    // it is not a capability question.
    TempPath anchor("glob-backslash");
    const std::string base = anchor.str();
    const std::string odd = anchor.also_remove(base + "-odd\\name.mie");
    const std::string plain = anchor.also_remove(base + "-plain.mie");
    for (int i = 0; i < 2; ++i) {
        const std::string& path = (i == 0) ? odd : plain;
        std::FILE* handle = std::fopen(path.c_str(), "wb");
        REQUIRE(handle != NULL);
        REQUIRE(std::fputs("x", handle) >= 0);
        REQUIRE(std::fclose(handle) == 0);
    }

    std::vector<std::string> found;
    mie::platform::OsError err;
    REQUIRE(mie::merge::expand_glob(base + "-odd\\name*.mie", found, err));
    REQUIRE(found.size() == 1u);
    CHECK(found[0] == odd);
}
#endif

TEST_CASE("expand_glob refuses a wildcard in the directory part", "[merge][L2-MRG-001]") {
    // Wildcards apply to the filename only. Read literally, `captures/**` is a
    // directory named `**`, and the failure to open it reported "No such file or
    // directory" -- a missing folder, when the mistake is the pattern. The
    // refusal comes before any I/O, so none of these directories need exist.
    const std::string root = mie_test::temp_root();
    const char* const patterns[] = {"captures/**/*.mie", "capt*/a.mie", "capture?/*.mie",
                                    "**/*.mie"};
    for (std::size_t i = 0; i < sizeof(patterns) / sizeof(patterns[0]); ++i) {
        const std::string pattern = mie::platform::path_join(root, patterns[i]);
        INFO(pattern);
        std::vector<std::string> found;
        mie::platform::OsError err;
        CHECK_FALSE(mie::merge::expand_glob(pattern, found, err));
        CHECK(err.code == mie::merge::GLOB_INVALID_PATTERN);
        CHECK(err.message.find("directory part") != std::string::npos);
    }

    SECTION("a wildcard in the filename part is the supported case") {
        std::vector<std::string> found;
        mie::platform::OsError err;
        CHECK(mie::merge::expand_glob(mie::platform::path_join(root, "*.nope"), found, err));
        CHECK(err.ok());
    }

#if defined(_WIN32)
    SECTION("the ? of the extended-length prefix is not a wildcard") {
        std::vector<std::string> found;
        mie::platform::OsError err;
        static_cast<void>(mie::merge::expand_glob("\\\\?\\" + root + "\\*.nope", found, err));
        CHECK(err.code != mie::merge::GLOB_INVALID_PATTERN);
    }
#endif
}

TEST_CASE("the merge interleaves inputs by timestamp", "[merge][L3-CPP-020]") {
    const TempFile a("mie-merge-a.mie", recording(100, 3));
    const TempFile b("mie-merge-b.mie", recording(150, 3));

    mie::MieFileReader reader_a;
    mie::MieFileReader reader_b;
    reader_a.open(a.str(), irig_options());
    reader_b.open(b.str(), irig_options());

    std::vector<mie::MieFileReader*> readers;
    readers.push_back(&reader_a);
    readers.push_back(&reader_b);

    const mie::merge::MergeOptions options;
    const std::vector<uint64_t> order = merged_order(readers, options);
    REQUIRE(order.size() == 6u);
    // Strictly non-decreasing, and actually interleaved rather than
    // concatenated -- a merge that returned all of A then all of B would also
    // be "sorted" if A happened to end before B began, so the fixtures overlap.
    for (std::size_t i = 1; i < order.size(); ++i) {
        CHECK(order[i - 1] <= order[i]);
    }
    const uint64_t base = order[0];
    CHECK(order[1] - base == 50u);
    CHECK(order[2] - base == 100u);
    CHECK(order[3] - base == 150u);
}

TEST_CASE("a merge rejects inputs that cannot share a timeline", "[merge][L3-CPP-021]") {
    // L2-MRG-003. Each of these resolves to a number that means something
    // different per file, so interleaving on it would be meaningless -- and
    // silently so, which is why it is a rejection rather than a warning.
    const TempFile good("mie-merge-good.mie", recording(100, 3));

    SECTION("a freerun-leading IRIG input") {
        std::vector<uint16_t> words;
        for (uint32_t i = 0; i < 3; ++i) {
            std::vector<uint16_t> record;
            record.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8));
            mie_test::push_irig(record, 100 + i * 100, 10, 15, 54, 50, /*freerun=*/true);
            record.push_back(mie_test::command_word(15, mie::DIRECTION_RECEIVE, 11, 2));
            record.push_back(static_cast<uint16_t>(0x1100 + i));
            record.push_back(static_cast<uint16_t>(0x2200 + i));
            record.push_back(mie_test::status_word(15));
            mie_test::append(words, record);
        }
        const TempFile freerun("mie-merge-freerun.mie", mie_test::finish(words));

        mie::MieFileReader reader_a;
        mie::MieFileReader reader_b;
        reader_a.open(good.str(), irig_options());
        reader_b.open(freerun.str(), irig_options());
        std::vector<mie::MieFileReader*> readers;
        readers.push_back(&reader_a);
        readers.push_back(&reader_b);

        const mie::merge::MergeOptions options;
        try {
            // Constructing it IS the operation under test: the merge primes
            // itself here, which is where an input that cannot share a timeline
            // is rejected -- before a single row exists.
            const mie::merge::MergedSource source(readers, options);
            FAIL("expected an incompatible-merge rejection");
        } catch (const mie::MieError& error) {
            CHECK(error.kind() == mie::KIND_INCOMPATIBLE_MERGE_INPUTS);
            // The message must name the input, because an operator merging
            // forty files needs to know WHICH one to remove.
            CHECK(error.message().find("freerun") != std::string::npos);
        }
    }

    SECTION("a Standard-format input") {
        std::vector<uint16_t> words;
        for (uint32_t i = 0; i < 3; ++i) {
            std::vector<uint16_t> record;
            record.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 7));
            mie_test::push_standard(record, 1000 + i * 100);
            record.push_back(mie_test::command_word(15, mie::DIRECTION_RECEIVE, 11, 2));
            record.push_back(static_cast<uint16_t>(0x3300 + i));
            record.push_back(static_cast<uint16_t>(0x4400 + i));
            record.push_back(mie_test::status_word(15));
            mie_test::append(words, record);
        }
        const TempFile standard("mie-merge-standard.mie", mie_test::finish(words));

        mie::ReaderOptions standard_options;
        standard_options.input_time_format = mie::TIMESTAMP_STANDARD;

        mie::MieFileReader reader_a;
        mie::MieFileReader reader_b;
        reader_a.open(good.str(), irig_options());
        reader_b.open(standard.str(), standard_options);
        std::vector<mie::MieFileReader*> readers;
        readers.push_back(&reader_a);
        readers.push_back(&reader_b);

        const mie::merge::MergeOptions options;
        try {
            // Constructing it IS the operation under test: the merge primes
            // itself here, which is where an input that cannot share a timeline
            // is rejected -- before a single row exists.
            const mie::merge::MergedSource source(readers, options);
            FAIL("expected an incompatible-merge rejection");
        } catch (const mie::MieError& error) {
            CHECK(error.kind() == mie::KIND_INCOMPATIBLE_MERGE_INPUTS);
        }
    }
}

TEST_CASE("duplicate collapsing spans recorders but never one file", "[merge][L3-CPP-022]") {
    // L2-MRG-007. Two recorders on one bus see the same transaction; the same
    // content twice within ONE file is the bus really carrying it twice, and
    // dropping that would be losing data rather than de-duplicating it.
    mie::merge::DedupWindow window(10, mie::DEFAULT_MAX_COLLAPSE_SURVIVORS);

    mie::MieMessage message;
    message.type_word = mie::TypeWord(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 8, false, 0x0802);
    message.command_word = mie::CommandWord(15, mie::DIRECTION_RECEIVE, 11, 2, 0x7962);
    message.status_word = static_cast<uint16_t>(0x7800);
    const uint16_t payload[2] = {0x1234, 0xABCD};
    message.data_words = mie::DataWords::from_words(payload, 2);

    SECTION("the same content from another input within the window collapses") {
        CHECK_FALSE(window.is_duplicate(1000, 0, message));
        CHECK(window.is_duplicate(1005, 1, message));
    }

    SECTION("the same content from the SAME input never collapses") {
        mie::merge::DedupWindow same_file(10, mie::DEFAULT_MAX_COLLAPSE_SURVIVORS);
        CHECK_FALSE(same_file.is_duplicate(1000, 0, message));
        CHECK_FALSE(same_file.is_duplicate(1005, 0, message));
    }

    SECTION("outside the window it is a distinct record") {
        mie::merge::DedupWindow narrow(10, mie::DEFAULT_MAX_COLLAPSE_SURVIVORS);
        CHECK_FALSE(narrow.is_duplicate(1000, 0, message));
        CHECK_FALSE(narrow.is_duplicate(1011, 1, message));
    }

    SECTION("different payload is never a duplicate") {
        mie::merge::DedupWindow other(10, mie::DEFAULT_MAX_COLLAPSE_SURVIVORS);
        CHECK_FALSE(other.is_duplicate(1000, 0, message));
        mie::MieMessage changed(message);
        const uint16_t different[2] = {0x1234, 0x0000};
        changed.data_words = mie::DataWords::from_words(different, 2);
        CHECK_FALSE(other.is_duplicate(1002, 1, changed));
    }

    SECTION("a backward step neither collapses wrongly nor underflows") {
        // A lenient non-monotonic input (L2-MRG-006) can step backward. The
        // window compares ABSOLUTE distance: a one-sided subtraction would wrap
        // to an enormous unsigned value and read as "outside the window" only
        // by luck.
        mie::merge::DedupWindow backward(10, mie::DEFAULT_MAX_COLLAPSE_SURVIVORS);
        CHECK_FALSE(backward.is_duplicate(1000, 0, message));
        CHECK(backward.is_duplicate(995, 1, message));
    }
}

namespace {

/// A message whose wire content is driven by `seq`, so a stream of them
/// collapses nothing. Content uniqueness is load-bearing in the probes below:
/// if everything collapsed, the survivor set would stay small for the wrong
/// reason and the assertions would pass vacuously.
mie::MieMessage probe_message(uint64_t seq) {
    mie::MieMessage message;
    message.type_word = mie::TypeWord(mie::MESSAGE_TYPE_BC_TO_RT, mie::BUS_A, 4, false, 0x0224);
    message.error_word = static_cast<uint16_t>(seq & 0xFFFF);
    return message;
}

}  // namespace

TEST_CASE("dedup retention does not depend on the order survivors arrived",
          "[merge][L2-MRG-007][L2-MRG-008]") {
    // The reported probe, as a test: alternating 1000us / 0us, zero-width
    // window. Front-only eviction never fired here -- the front held a timestamp
    // in the FUTURE of the current record, so the one-sided `us - front_us` was
    // never greater than the window -- and the front then blocked eviction of
    // everything behind it. All 10 000 records were retained and the per-record
    // scan went quadratic (2x records, 4x time).
    //
    // Retention is now on absolute distance, so the 1000us survivors go the
    // moment a 0us record arrives, and vice versa.
    mie::merge::DedupWindow window(0, mie::DEFAULT_MAX_COLLAPSE_SURVIVORS);
    for (uint64_t i = 0; i < 10000; ++i) {
        const uint64_t us = (i % 2 == 0) ? 1000 : 0;
        window.is_duplicate(us, static_cast<std::size_t>(i % 2), probe_message(i));
    }
    CHECK(window.survivor_count() <= 2u);
}

TEST_CASE("the dedup survivor set is capped when the window cannot bound it",
          "[merge][L2-MRG-008]") {
    // The window bounds retention in TIME; the cap bounds it in COUNT. This is
    // the input the window alone cannot bound -- every record shares one
    // timestamp, so every one of them is legitimately inside the window. It is
    // the same "timestamps all decode alike" case L2-WRT-022 cites for the
    // reorder stage, and it is why absolute-distance eviction is not on its own
    // sufficient.
    const std::size_t cap = 64;
    const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
    mie::merge::DedupWindow window(UINT64_MAX, cap);
    for (uint64_t i = 0; i < 10000; ++i) {
        window.is_duplicate(0, static_cast<std::size_t>(i % 2), probe_message(i));
    }
    CHECK(window.survivor_count() == cap);

    // Exactly one WARN per merge, not one per capped record.
    CHECK(capture.count_containing("max_collapse_survivors cap") == 1u);
}

TEST_CASE("a non-monotonic input warns once and keeps going", "[merge][L3-CPP-023]") {
    // L2-MRG-006: inputs are assumed internally time-sorted. A backward step
    // means the merged output may be out of order for that input, which the
    // operator has to be told -- once. Repeating it per record would bury the
    // message in its own output.
    std::vector<uint16_t> words;
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 500));
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 600));
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 200));  // steps backward
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 300));
    mie_test::append(words, mie_test::bc_to_rt(15, 11, 2, 100));  // and again
    const TempFile backward("mie-merge-backward.mie", mie_test::finish(words));
    const TempFile forward("mie-merge-forward.mie", recording(1000, 2));

    mie::MieFileReader reader_a;
    mie::MieFileReader reader_b;
    reader_a.open(backward.str(), irig_options());
    reader_b.open(forward.str(), irig_options());
    std::vector<mie::MieFileReader*> readers;
    readers.push_back(&reader_a);
    readers.push_back(&reader_b);

    SECTION("lenient mode warns exactly once for that input") {
        const mie_test::LogCapture capture(mie::log::LEVEL_WARN);
        mie::merge::MergeOptions options;
        options.strict = false;
        const std::vector<uint64_t> order = merged_order(readers, options);
        CHECK(order.size() == 7u);

        std::size_t warnings = 0;
        const std::vector<std::string>& lines = mie_test::captured_lines();
        for (std::size_t i = 0; i < lines.size(); ++i) {
            if (lines[i].find("not internally time-sorted") != std::string::npos) {
                warnings += 1;
            }
        }
        CHECK(warnings == 1u);
    }

    SECTION("strict mode fails the batch") {
        mie::merge::MergeOptions options;
        options.strict = true;
        mie::merge::MergedSource source(readers, options);
        mie::MieMessage message;
        bool raised = false;
        try {
            while (source.next(message)) {
                // drain
            }
        } catch (const mie::MieError& error) {
            raised = true;
            CHECK(error.kind() == mie::KIND_NON_MONOTONIC_INPUT);
        }
        CHECK(raised);
    }
}

TEST_CASE("an empty input contributes nothing rather than failing", "[merge][L3-CPP-020]") {
    // A valid but empty recording is not a broken one (L1-EXIT-010): the stream
    // opens on the end-of-records terminator. In a merge it simply has no
    // records to offer.
    const std::vector<uint16_t> nothing;
    const TempFile empty("mie-merge-empty.mie", mie_test::finish(nothing));
    const TempFile full("mie-merge-full.mie", recording(100, 3));

    mie::MieFileReader reader_a;
    mie::MieFileReader reader_b;
    reader_a.open(empty.str(), irig_options());
    reader_b.open(full.str(), irig_options());
    std::vector<mie::MieFileReader*> readers;
    readers.push_back(&reader_a);
    readers.push_back(&reader_b);

    const mie::merge::MergeOptions options;
    CHECK(merged_order(readers, options).size() == 3u);
}

// ---------------------------------------------------------------------------
// L2-MRG-007: a 0x2000 continuation is collapsed exactly when its parent is
// ---------------------------------------------------------------------------

namespace {

/// One recorder's error + continuation: an errored BC-to-RT record for `rt` at
/// `micros`, then a SPURIOUS_DATA continuation stamped 10 us later holding
/// `leftover` (the reader reports it at the error's time, L2-ERR-005).
std::vector<uint8_t> error_and_continuation(uint8_t rt, uint16_t leftover, uint32_t micros) {
    std::vector<uint16_t> words;
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_BC_TO_RT, 8, /*error=*/true));
    mie_test::push_irig(words, micros);
    words.push_back(mie_test::command_word(rt, mie::DIRECTION_RECEIVE, 11, 4));
    words.push_back(0x0000);
    words.push_back(0x0000);
    words.push_back(mie::ERROR_MANCHESTER_PARITY);
    words.push_back(mie_test::type_word(mie::MESSAGE_TYPE_SPURIOUS_DATA, 5));
    mie_test::push_irig(words, micros + 10);
    words.push_back(leftover);
    return mie_test::finish(words);
}

/// Collapse-merge recorder A's pair with recorder B's; describe what survives
/// as "RT<n>" / "cont:<word>" in order, with the suppressed count.
std::string collapse_pair(const std::vector<uint8_t>& a, const std::vector<uint8_t>& b,
                          uint64_t& collapsed) {
    const TempFile fa("mie-collapse-a.mie", a);
    const TempFile fb("mie-collapse-b.mie", b);
    mie::MieFileReader reader_a;
    mie::MieFileReader reader_b;
    reader_a.open(fa.str(), irig_options());
    reader_b.open(fb.str(), irig_options());
    std::vector<mie::MieFileReader*> readers;
    readers.push_back(&reader_a);
    readers.push_back(&reader_b);

    mie::merge::MergeOptions options;
    options.collapse_duplicates = true;
    mie::merge::MergedSource source(readers, options);
    std::string out;
    mie::MieMessage message;
    while (source.next(message)) {
        if (!out.empty()) {
            out += ",";
        }
        out += message.command_word.has_value()
                   ? "RT" + mie::text::decimal(message.command_word.value().rt)
                   : "cont:" + mie::text::hex_upper(message.data_words[0], 4);
    }
    collapsed = source.collapsed();
    return out;
}

}  // namespace

TEST_CASE("collapsing a parent also collapses its continuation", "[merge][L2-MRG-007]") {
    // First example: B's error copies A's, but B's continuation holds different
    // words. B's error collapses and its continuation goes with it -- A's
    // continuation (word 0000) is the one that survives.
    uint64_t collapsed = 0;
    CHECK(collapse_pair(error_and_continuation(15, 0x0000, 500),
                        error_and_continuation(15, 0x2222, 500), collapsed) == "RT15,cont:0000");
    CHECK(collapsed == 2u);
}

TEST_CASE("a continuation is not collapsed while its parent survives", "[merge][L2-MRG-007]") {
    // Second example: B's error differs (RT16), but B's continuation copies A's.
    // Judged on content alone it would collapse and strand B's error; it shares
    // its parent's fate instead, and B's parent survives.
    uint64_t collapsed = 0;
    CHECK(collapse_pair(error_and_continuation(15, 0x0000, 500),
                        error_and_continuation(16, 0x0000, 500),
                        collapsed) == "RT15,cont:0000,RT16,cont:0000");
    CHECK(collapsed == 0u);
}

TEST_CASE("a copied pair collapses as a pair", "[merge][L2-MRG-007]") {
    uint64_t collapsed = 0;
    CHECK(collapse_pair(error_and_continuation(15, 0x0000, 500),
                        error_and_continuation(15, 0x0000, 500), collapsed) == "RT15,cont:0000");
    CHECK(collapsed == 2u);
}

// ---------------------------------------------------------------------------
// L2-MRG-004: --allow-partial when every input fails
// ---------------------------------------------------------------------------

TEST_CASE("a partial merge in which every input fails reports the first input's error",
          "[merge][L2-MRG-004]") {
    // --allow-partial keeps what could be decoded. When nothing could be, there
    // is nothing to keep: the merge fails exactly as it would without the flag,
    // instead of handing the writer an empty .partial to commit as a success.
    const TempFile a("mie-allfail-a.mie", std::vector<uint8_t>(4096, 0xFF));
    const TempFile b("mie-allfail-b.mie", std::vector<uint8_t>(2048, 0xFF));
    mie::MieFileReader reader_a;
    mie::MieFileReader reader_b;
    reader_a.open(a.str(), irig_options());
    reader_b.open(b.str(), irig_options());
    std::vector<mie::MieFileReader*> readers;
    readers.push_back(&reader_a);
    readers.push_back(&reader_b);

    mie::merge::MergeOptions options;
    options.allow_partial = true;
    bool threw = false;
    try {
        const mie::merge::MergedSource source(readers, options);
    } catch (const mie::MieError& error) {
        threw = true;
        CHECK(error.kind() == mie::KIND_NO_VALID_RECORDS);
        CHECK(error.message().find("mie-allfail-a.mie") != std::string::npos);
    }
    CHECK(threw);
}

TEST_CASE("an empty recording counts as a successful input in a partial merge",
          "[merge][L2-MRG-004]") {
    // A valid empty recording (the terminator alone) contributed nothing but did
    // not fail, so a failing input beside it is a partial merge, not "every
    // input failed".
    const TempFile empty("mie-partial-empty.mie", std::vector<uint8_t>(2, 0x00));
    const TempFile bad("mie-partial-bad.mie", std::vector<uint8_t>(4096, 0xFF));
    mie::MieFileReader reader_a;
    mie::MieFileReader reader_b;
    reader_a.open(empty.str(), irig_options());
    reader_b.open(bad.str(), irig_options());
    std::vector<mie::MieFileReader*> readers;
    readers.push_back(&reader_a);
    readers.push_back(&reader_b);

    mie::merge::MergeOptions options;
    options.allow_partial = true;
    mie::merge::MergedSource source(readers, options);
    mie::MieMessage message;
    // No records, then the terminal marker that makes the writer commit .partial.
    CHECK_THROWS_AS(source.next(message), mie::MieError);
}
