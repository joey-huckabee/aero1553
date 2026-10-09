// SPDX-License-Identifier: Apache-2.0
//
// Tests for the platform layer -- the only module that touches the OS, and
// therefore the only one whose behaviour can differ between the SLES 12 target
// and the shipped Windows binary.
//
// These deliberately do NOT use the platform layer to set up their own
// fixtures. Files are created and read back with <cstdio> so that a bug in
// AtomicFile cannot make an AtomicFile test pass by being consistently wrong in
// both directions.

#include "mie/platform.hpp"
#include "temp_path.hpp"

#include <catch2/catch.hpp>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

namespace plat = mie::platform;

void write_raw(const std::string& path, const std::string& contents) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    REQUIRE(f != 0);
    if (!contents.empty()) {
        REQUIRE(std::fwrite(contents.data(), 1, contents.size(), f) == contents.size());
    }
    // fclose is where a buffered write actually reports failure. A fixture that
    // never reached the disk would make the test it feeds fail for the wrong
    // reason, so this is asserted rather than ignored.
    REQUIRE(std::fclose(f) == 0);
}

/// Read a file back as raw bytes. Binary mode matters: the whole point of
/// several tests below is that no newline translation happened.
///
/// Every outcome the C library can produce is named, mirroring the discipline
/// of AtomicFile::raw_write on the other side of this file. That is not
/// pedantry. `fread` reports fewer bytes than asked for at end-of-file AND on
/// an I/O error, and the two are not interchangeable: collapsing them lets a
/// failed read return a TRUNCATED string, which the caller compares against the
/// expected content and reports as "AtomicFile lost bytes". The test would then
/// accuse the code under test of a bug it does not have, and the real fault --
/// a read that failed -- would not appear anywhere in the output.
///
/// A one-shot fseek/ftell/rewind/read was the alternative. It is fewer lines
/// and MORE error paths, each of which would need this same treatment, so the
/// loop wins.
std::string read_raw(const std::string& path) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    // Every caller reads a file it has just written. A missing one is a bug in
    // the test, and saying so here beats returning an empty string that fails a
    // content comparison three lines later with a misleading message.
    REQUIRE(f != 0);

    std::string out;
    char buffer[4096];
    for (;;) {
        const std::size_t n = std::fread(buffer, 1, sizeof(buffer), f);
        out.append(buffer, n);
        if (n == sizeof(buffer)) {
            // A full buffer says nothing about what follows -- fread had no
            // reason to look past what was asked for, so the EOF flag is not
            // set and reading again is well-defined. This is also why the loop
            // stops on a SHORT read rather than a zero one: at end-of-file
            // fread returns a positive count and sets EOF in the same call, so
            // `while (n > 0)` would go round once more with the stream already
            // in EOF state. clang-analyzer's unix.Stream check reports that,
            // and it is right to.
            continue;
        }

        // Short read: end-of-file or error, and which one decides everything.
        // Both flags are read before closing, and the close happens before the
        // assertions, because REQUIRE throws -- asserting first would leak the
        // handle on exactly the path that matters.
        const bool failed = std::ferror(f) != 0;
        const bool at_eof = std::feof(f) != 0;
        (void)std::fclose(f);
        REQUIRE_FALSE(failed);
        REQUIRE(at_eof);
        return out;
    }
}

bool raw_exists(const std::string& path) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (f == 0) {
        return false;
    }
    (void)std::fclose(f);
    return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// Path string helpers
// ---------------------------------------------------------------------------

TEST_CASE("path_parent and path_filename split at the final separator", "[platform][path]") {
    CHECK(plat::path_filename("/a/b/c.csv") == "c.csv");
    CHECK(plat::path_parent("/a/b/c.csv") == "/a/b");

    SECTION("a bare name has no parent") {
        CHECK(plat::path_parent("out.csv").empty());
        CHECK(plat::path_filename("out.csv") == "out.csv");
    }

    SECTION("the root separator survives as the parent") {
        // The parent of "/x" is "/", not "". Returning "" would make path_join
        // produce a relative path from an absolute one -- the temp file would
        // land in the working directory instead of beside the destination,
        // which silently breaks the single-filesystem rename guarantee.
        CHECK(plat::path_parent("/x") == "/");
    }

#if defined(_WIN32)
    SECTION("a drive's root separator survives too") {
        // "C:" alone is the current directory ON drive C, not its root. Parenting
        // "C:\x" as "C:" resolved a drive-root output against the working
        // directory, so `-o C:\a.mie` collided with a.mie in that directory.
        CHECK(plat::path_parent("C:\\x.csv") == "C:\\");
        CHECK(plat::path_parent("q:/x.csv") == "q:/");
        CHECK(plat::path_parent("C:\\dir\\x.csv") == "C:\\dir");
        CHECK(plat::path_join(plat::path_parent("C:\\x.csv"), "x_errors.csv") ==
              "C:\\x_errors.csv");
    }
#endif
}

TEST_CASE("is_separator treats backslash as platform-specific", "[platform][path]") {
    CHECK(plat::is_separator('/'));
#if defined(_WIN32)
    CHECK(plat::is_separator('\\'));
#else
    // On POSIX a backslash is an ordinary filename character. Treating it as a
    // separator would truncate any path that legitimately contains one -- a bug
    // that cannot reproduce on Windows.
    CHECK_FALSE(plat::is_separator('\\'));
    CHECK(plat::path_filename("/tmp/od\\d.csv") == "od\\d.csv");
#endif
}

TEST_CASE("path_join collapses a doubled separator", "[platform][path]") {
    const std::string joined = plat::path_join("dir/", "/name");
    CHECK(plat::path_filename(joined) == "name");
    CHECK(joined.find("//") == std::string::npos);

    CHECK(plat::path_join("", "name") == "name");
    CHECK(plat::path_join("dir", "") == "dir");
}

// ---------------------------------------------------------------------------
// Temp-name construction (L3-WRT-001)
// ---------------------------------------------------------------------------

TEST_CASE("make_temp_name sits beside the destination and never repeats",
          "[platform][atomic][L3-WRT-001][L3-CPP-004]") {
    const std::string destination = plat::path_join("/some/dir", "out.csv");

    const std::string first = plat::make_temp_name(destination);
    const std::string second = plat::make_temp_name(destination);

    SECTION("it is co-located with the destination") {
        // Same-directory placement is what keeps the later rename on one
        // filesystem, and therefore atomic.
        CHECK(plat::path_parent(first) == plat::path_parent(destination));
    }

    SECTION("it is recognisable in a directory listing") {
        CHECK(plat::path_filename(first).find("out.csv.aero1553.tmp.") == 0);
    }

    SECTION("two calls in one process cannot collide") {
        // The counter component exists for exactly this: a coarse clock makes
        // two calls in one tick a real case, not a theoretical one.
        CHECK(first != second);
    }
}

TEST_CASE("make_temp_name handles a destination with no directory",
          "[platform][atomic][L3-CPP-004]") {
    const std::string name = plat::make_temp_name("out.csv");
    CHECK(plat::path_parent(name).empty());
    CHECK(name.find("out.csv.aero1553.tmp.") == 0);
}

// ---------------------------------------------------------------------------
// MappedFile
// ---------------------------------------------------------------------------

TEST_CASE("MappedFile exposes the file as a flat byte range", "[platform][mmap][L3-CPP-004]") {
    const mie_test::TempPath path("map");
    const std::string contents("\x02\x24\x00\x00MIE", 7);
    write_raw(path.str(), contents);

    plat::MappedFile mapped;
    plat::OsError err;
    REQUIRE(mapped.open(path.str(), err));
    CHECK(err.ok());
    REQUIRE(mapped.is_open());
    REQUIRE(mapped.size() == contents.size());
    CHECK(std::memcmp(mapped.data(), contents.data(), contents.size()) == 0);

    mapped.close();
    CHECK_FALSE(mapped.is_open());
}

TEST_CASE("MappedFile reports a missing path as an error", "[platform][mmap][L3-CPP-004]") {
    plat::MappedFile mapped;
    plat::OsError err;
    CHECK_FALSE(mapped.open(mie_test::TempPath("absent").str(), err));
    CHECK_FALSE(err.ok());
    CHECK_FALSE(err.message.empty());
}

TEST_CASE("MappedFile rejects an empty file rather than mapping zero bytes",
          "[platform][mmap][L2-RDR-006][L3-CPP-004]") {
    // Callers reject empty inputs before reaching here, but the guard matters:
    // mmap of a zero-length range fails with EINVAL and CreateFileMapping
    // refuses outright, so without it the operator sees a confusing I/O error
    // instead of "the file is empty".
    const mie_test::TempPath path("empty");
    write_raw(path.str(), std::string());

    plat::MappedFile mapped;
    plat::OsError err;
    CHECK_FALSE(mapped.open(path.str(), err));
    CHECK_FALSE(err.ok());
}

TEST_CASE("MappedFile rejects a directory", "[platform][mmap][L3-CPP-004]") {
    // A directory stats as zero bytes on Windows. Opening before testing the
    // size is what keeps this reported as "not a regular file" rather than as
    // "empty" -- the distinction docs/ERROR-CATALOG.md pins for MieFileIoError.
    plat::MappedFile mapped;
    plat::OsError err;
    CHECK_FALSE(mapped.open(mie_test::temp_root(), err));
    CHECK_FALSE(err.ok());
}

TEST_CASE("a moved-from MappedFile releases its mapping", "[platform][mmap][L3-CPP-004]") {
    const mie_test::TempPath path("move");
    write_raw(path.str(), std::string("payload"));

    plat::MappedFile source;
    plat::OsError err;
    REQUIRE(source.open(path.str(), err));
    const uint8_t* original = source.data();

    const plat::MappedFile moved(static_cast<plat::MappedFile&&>(source));
    CHECK(moved.data() == original);
    CHECK(moved.size() == 7);
    // Interrogating a moved-from object is the ENTIRE POINT of this test, so the
    // analyzer's cplusplus.Move warning is suppressed here and nowhere else.
    // The source must not still believe it owns the mapping, or both objects
    // unmap the same range at scope exit -- a double-munmap that valgrind
    // catches only if something first makes the moved-from state observable.
    // NOLINTNEXTLINE(clang-analyzer-cplusplus.Move)
    CHECK_FALSE(source.is_open());
}

// ---------------------------------------------------------------------------
// AtomicFile
// ---------------------------------------------------------------------------

TEST_CASE("AtomicFile commits through a temp file", "[platform][atomic][L2-WRT-015][L3-CPP-006]") {
    const mie_test::TempPath destination("commit.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));

    const std::string temp = file.temp_path();
    CHECK(raw_exists(temp));
    CHECK_FALSE(raw_exists(destination.str()));

    const std::string row("TIME_STAMP,RT,MSG\n");
    REQUIRE(file.write(row.data(), row.size(), err));
    REQUIRE(file.commit(err) == plat::COMMIT_DONE);

    CHECK(read_raw(destination.str()) == row);
    CHECK_FALSE(raw_exists(temp));
}

TEST_CASE("AtomicFile replaces an existing destination",
          "[platform][atomic][L2-WRT-015][L3-CPP-006]") {
    // THE Windows trap, and the reason the backend uses MoveFileExW with
    // MOVEFILE_REPLACE_EXISTING: the MSVC CRT's rename() fails outright when the
    // destination exists. Rust's std::fs::rename uses MoveFileEx internally, so
    // a port written with plain rename() passes on Linux and is broken on the
    // shipped Windows binary. This test is what catches that.
    const mie_test::TempPath destination("replace.csv");
    write_raw(destination.str(), std::string("STALE"));

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    const std::string fresh("FRESH");
    REQUIRE(file.write(fresh.data(), fresh.size(), err));
    REQUIRE(file.commit(err) == plat::COMMIT_DONE);

    CHECK(read_raw(destination.str()) == fresh);
}

TEST_CASE("AtomicFile in no-replace mode refuses an existing destination",
          "[platform][atomic][L2-WRT-023][L3-CPP-032]") {
    // The window the pre-flight cannot see: the destination is created AFTER
    // the writer opened its temp, which is exactly where a second process
    // lands. A `path_exists` test before the open answers a question about the
    // past, so the refusal has to happen here, at the move.
    const mie_test::TempPath destination("noreplace.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    file.set_commit_mode(plat::COMMIT_NO_REPLACE);
    const std::string ours("OURS");
    REQUIRE(file.write(ours.data(), ours.size(), err));

    const std::string temp = file.temp_path();
    write_raw(destination.str(), std::string("THEIRS"));

    CHECK(file.commit(err) == plat::COMMIT_EXISTS);
    CHECK(read_raw(destination.str()) == "THEIRS");
    // COMMIT_EXISTS is a refusal, not a failure, so `err` must stay clean --
    // the writer reports it as clobber_refused, not as an OS error.
    CHECK(err.ok());
    file.abort();
    CHECK_FALSE(raw_exists(temp));
}

TEST_CASE("AtomicFile in no-replace mode commits onto a free destination",
          "[platform][atomic][L2-WRT-023][L3-CPP-032]") {
    const mie_test::TempPath destination("noreplace-free.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    file.set_commit_mode(plat::COMMIT_NO_REPLACE);
    const std::string rows("ROWS");
    REQUIRE(file.write(rows.data(), rows.size(), err));

    const std::string temp = file.temp_path();
    REQUIRE(file.commit(err) == plat::COMMIT_DONE);
    CHECK(read_raw(destination.str()) == rows);
    // The POSIX mechanism publishes a second NAME for the same bytes rather
    // than moving them, so the temp has to be unlinked explicitly. A leak here
    // is invisible to every other assertion.
    CHECK_FALSE(raw_exists(temp));
}

TEST_CASE("AtomicFile in no-replace mode reports a failed move as an error, not a refusal",
          "[platform][atomic][L2-WRT-023][L3-CPP-032]") {
    // A destination in a directory that does not exist: on POSIX link(2)
    // fails with ENOENT, which is NOT "the destination exists", so the commit
    // falls through to the reservation fallback -- which fails the same way.
    // Either way the answer must be COMMIT_ERROR with the OS reason, never
    // COMMIT_EXISTS, which the writer would report as a --no-clobber refusal.
    const mie_test::TempPath destination("noreplace-unreachable.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    file.set_commit_mode(plat::COMMIT_NO_REPLACE);
    const std::string rows("ROWS");
    REQUIRE(file.write(rows.data(), rows.size(), err));

    const std::string temp = file.temp_path();
    CHECK(file.commit_with_suffix("-missing-dir/out.csv", err) == plat::COMMIT_ERROR);
    CHECK_FALSE(err.ok());
    // Not committed, so the temp is still ours to clean up.
    CHECK(raw_exists(temp));
    file.abort();
    CHECK_FALSE(raw_exists(temp));
}

#if !defined(_WIN32)
TEST_CASE("the no-replace fallback commits and refuses like the link path",
          "[platform][atomic][L2-WRT-023][L3-CPP-032]") {
    // On a filesystem with no hard links the commit reserves the name with
    // O_EXCL instead, and must still refuse a taken one. No test host's
    // filesystem fails link(2), so the fallback is driven directly -- as Rust's
    // reserve_then_rename is -- rather than left as the one path never run.
    const mie_test::TempPath destination("fallback.csv");
    const mie_test::TempPath temp("fallback.tmp");
    plat::OsError err;

    SECTION("a free destination commits, and a taken one is refused intact") {
        write_raw(temp.str(), std::string("ROWS"));
        REQUIRE(plat::reserve_then_rename(temp.str(), destination.str(), err) == plat::COMMIT_DONE);
        CHECK(err.ok());
        CHECK(read_raw(destination.str()) == "ROWS");
        CHECK_FALSE(raw_exists(temp.str()));

        write_raw(temp.str(), std::string("OTHER"));
        CHECK(plat::reserve_then_rename(temp.str(), destination.str(), err) == plat::COMMIT_EXISTS);
        CHECK(err.ok());
        // Refused, so no zero-byte reservation replaced the first writer's rows.
        CHECK(read_raw(destination.str()) == "ROWS");
    }

    SECTION("a failed rename takes its reservation back out") {
        // No temp file: the reservation succeeds and the rename then fails.
        // Leaving the reservation would hand the operator an empty CSV where
        // the error says nothing was written.
        CHECK(plat::reserve_then_rename(temp.str(), destination.str(), err) == plat::COMMIT_ERROR);
        CHECK_FALSE(err.ok());
        CHECK_FALSE(raw_exists(destination.str()));
    }

    SECTION("a reservation that cannot be made is an error, not a refusal") {
        write_raw(temp.str(), std::string("ROWS"));
        const std::string unreachable = destination.str() + "-missing-dir/out.csv";
        CHECK(plat::reserve_then_rename(temp.str(), unreachable, err) == plat::COMMIT_ERROR);
        CHECK_FALSE(err.ok());
        CHECK(raw_exists(temp.str()));
    }
}
#endif

TEST_CASE("AtomicFile in no-replace mode refuses an existing suffixed target",
          "[platform][atomic][L2-WRT-016][L2-WRT-023][L3-CPP-032][L3-WRT-005]") {
    // `<destination>.partial` is never pre-flighted -- a stale one must not
    // refuse a run that was never going to write one -- so the commit is the
    // ONLY thing standing between --no-clobber and an overwritten forensic
    // artifact.
    mie_test::TempPath destination("noreplace-partial.csv");
    const std::string partial = destination.sibling(".partial");
    write_raw(partial, std::string("EARLIER FORENSICS"));

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    file.set_commit_mode(plat::COMMIT_NO_REPLACE);
    const std::string ours("OURS");
    REQUIRE(file.write(ours.data(), ours.size(), err));

    const std::string temp = file.temp_path();
    CHECK(file.commit_with_suffix(".partial", err) == plat::COMMIT_EXISTS);
    CHECK(read_raw(partial) == "EARLIER FORENSICS");
    CHECK(err.ok());
    file.abort();
    CHECK_FALSE(raw_exists(temp));
}

TEST_CASE("AtomicFile reports a commit with no open handle as an error",
          "[platform][atomic][L2-WRT-024]") {
    // Closing the temp file is PART OF THE COMMIT (L2-WRT-024), and its failure
    // has to come back as a classified error rather than as a crash or a silent
    // success. A closed handle is the reachable stand-in for the real cause --
    // a close that reports the buffered data never reached the filesystem, which
    // needs a full disk to provoke -- and it exercises the same arm.
    const mie_test::TempPath destination("commit-twice.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    const std::string row("ROW");
    REQUIRE(file.write(row.data(), row.size(), err));
    REQUIRE(file.commit(err) == plat::COMMIT_DONE);

    CHECK(file.commit(err) == plat::COMMIT_ERROR);
    CHECK_FALSE(err.ok());
    CHECK_FALSE(err.message.empty());
    // The committed destination is untouched by the failed second attempt.
    CHECK(read_raw(destination.str()) == row);
}

TEST_CASE("AtomicFile leaves the destination untouched when aborted",
          "[platform][atomic][L2-WRT-015][L3-CPP-006]") {
    const mie_test::TempPath destination("abort.csv");
    write_raw(destination.str(), std::string("ORIGINAL"));

    std::string temp;
    {
        plat::AtomicFile file;
        plat::OsError err;
        REQUIRE(file.create(destination.str(), err));
        temp = file.temp_path();
        const std::string partial("half a row");
        REQUIRE(file.write(partial.data(), partial.size(), err));
        // No commit: the destructor must clean up. This is the sync-loss path
        // without --allow-partial, where the destination.str() must not be modified.
    }

    CHECK(read_raw(destination.str()) == "ORIGINAL");
    CHECK_FALSE(raw_exists(temp));
}

TEST_CASE("commit_with_suffix writes the .partial file and spares the destination",
          "[platform][atomic][L3-WRT-002][L3-CPP-006]") {
    // Not const: registering the .partial sibling for removal MUTATES the
    // guard. Saying so beats a `mutable` member that quietly pretends
    // otherwise -- the object really is being changed.
    mie_test::TempPath destination("partial.csv");
    write_raw(destination.str(), std::string("PREVIOUS"));

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    const std::string rows("partial rows\n");
    REQUIRE(file.write(rows.data(), rows.size(), err));
    REQUIRE(file.commit_with_suffix(".partial", err) == plat::COMMIT_DONE);

    CHECK(read_raw(destination.sibling(".partial")) == rows);
    CHECK(read_raw(destination.str()) == "PREVIOUS");
}

TEST_CASE("AtomicFile writes bytes verbatim, with no newline translation",
          "[platform][atomic][L2-WRT-012][L3-CPP-005]") {
    // On Windows the CRT rewrites a newline into CRLF unless the handle is
    // opened in binary mode, which would break every byte-exact CSV oracle on
    // that platform alone while Linux stayed green.
    const mie_test::TempPath destination("newline.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));
    const std::string payload("a\nb\n");
    REQUIRE(file.write(payload.data(), payload.size(), err));
    REQUIRE(file.commit(err) == plat::COMMIT_DONE);

    const std::string round_tripped = read_raw(destination.str());
    CHECK(round_tripped.size() == 4);
    CHECK(round_tripped == payload);
    CHECK(round_tripped.find('\r') == std::string::npos);
}

TEST_CASE("AtomicFile survives payloads that straddle the buffer boundary",
          "[platform][atomic][L3-CPP-004]") {
    // Three shapes that each take a different branch: comfortably inside the
    // buffer, exactly at it, and a single write larger than it (which bypasses
    // buffering entirely).
    const mie_test::TempPath destination("buffered.csv");

    plat::AtomicFile file;
    plat::OsError err;
    REQUIRE(file.create(destination.str(), err));

    std::string expected;
    const std::string small(100, 'a');
    const std::string exact(plat::kWriteBufferSize, 'b');
    const std::string oversized(plat::kWriteBufferSize * 2 + 7, 'c');

    REQUIRE(file.write(small.data(), small.size(), err));
    expected += small;
    REQUIRE(file.write(exact.data(), exact.size(), err));
    expected += exact;
    REQUIRE(file.write(oversized.data(), oversized.size(), err));
    expected += oversized;
    REQUIRE(file.commit(err) == plat::COMMIT_DONE);

    CHECK(read_raw(destination.str()).size() == expected.size());
    CHECK(read_raw(destination.str()) == expected);
}

// ---------------------------------------------------------------------------
// Directory enumeration
// ---------------------------------------------------------------------------

TEST_CASE("list_directory returns entry names without the dot entries",
          "[platform][glob][L3-CPP-004]") {
    const mie_test::TempPath marker("listed.mie");
    write_raw(marker.str(), std::string("x"));

    std::vector<std::string> names;
    plat::OsError err;
    REQUIRE(plat::list_directory(mie_test::temp_root(), names, err));

    // Findings are accumulated and asserted once, rather than asserted inside
    // the loop. The shared temp directory holds an unpredictable number of
    // unrelated files, so per-entry assertions make the suite's assertion count
    // depend on the host -- which turns a legitimate "157 vs 130" comparison
    // between two CI tiers into noise nobody can read.
    bool found_marker = false;
    bool saw_dot_entry = false;
    bool every_entry_is_a_bare_name = true;
    for (std::size_t i = 0; i < names.size(); ++i) {
        if (names[i] == "." || names[i] == "..") {
            saw_dot_entry = true;
        }
        // Names, not paths: --glob matches over the filename only.
        if (plat::path_filename(names[i]) != names[i]) {
            every_entry_is_a_bare_name = false;
        }
        if (names[i] == plat::path_filename(marker.str())) {
            found_marker = true;
        }
    }
    CHECK_FALSE(saw_dot_entry);
    CHECK(every_entry_is_a_bare_name);
    CHECK(found_marker);
}

TEST_CASE("list_directory reports a missing directory as an error",
          "[platform][glob][L3-CPP-004]") {
    std::vector<std::string> names;
    plat::OsError err;
    CHECK_FALSE(plat::list_directory(mie_test::TempPath("no-such-dir").str(), names, err));
    CHECK_FALSE(err.ok());
}

// ---------------------------------------------------------------------------
// Path identity
// ---------------------------------------------------------------------------

TEST_CASE("paths_same_file detects an output aimed at its own input",
          "[platform][identity][L2-WRT-014][L3-CPP-008]") {
    const mie_test::TempPath path("collide.mie");
    write_raw(path.str(), std::string("data"));

    bool same = false;
    plat::OsError err;
    REQUIRE(plat::paths_same_file(path.str(), path.str(), same, err));
    CHECK(same);
}

TEST_CASE("paths_same_file says no for two distinct files",
          "[platform][identity][L2-WRT-014][L3-CPP-008]") {
    const mie_test::TempPath input("in.mie");
    const mie_test::TempPath output("out.csv");
    write_raw(input.str(), std::string("data"));
    write_raw(output.str(), std::string("other"));

    bool same = true;
    plat::OsError err;
    REQUIRE(plat::paths_same_file(input.str(), output.str(), same, err));
    CHECK_FALSE(same);
}

TEST_CASE("paths_same_file tolerates an output that does not exist yet",
          "[platform][identity][L2-WRT-014][L3-CPP-008]") {
    // The normal case: the destination is about to be created. That must not be
    // an error, and it must not be reported as a collision.
    const mie_test::TempPath input("in2.mie");
    const mie_test::TempPath output("not-created-yet.csv");
    write_raw(input.str(), std::string("data"));

    bool same = true;
    plat::OsError err;
    REQUIRE(plat::paths_same_file(input.str(), output.str(), same, err));
    CHECK_FALSE(same);
}

#if defined(_WIN32)
TEST_CASE("an output at a drive root is not the input in the working directory",
          "[platform][identity][L2-WRT-014][L3-CPP-008]") {
    // The output's parent used to come back as "C:" -- the current directory on
    // drive C -- so `-o C:\a.mie` resolved to <cwd>\a.mie and was refused as the
    // same file as an input a.mie in the working directory. Nothing is written
    // to the root: the output does not exist, which is the branch that resolves
    // its parent directory.
    struct RemoveOnExit {
        std::string path;
        ~RemoveOnExit() { (void)std::remove(path.c_str()); }
    };
    const RemoveOnExit input = {plat::path_filename(mie_test::TempPath("driveroot.mie").str())};
    write_raw(input.path, std::string("data"));

    std::string cwd;
    plat::OsError err;
    REQUIRE(plat::canonical_path(".", cwd, err));
    const std::string long_prefix = "\\\\?\\";
    if (cwd.compare(0, long_prefix.size(), long_prefix) == 0) {
        cwd.erase(0, long_prefix.size());
    }
    REQUIRE(cwd.size() >= 3);
    REQUIRE(cwd[1] == ':');
    const std::string root = cwd.substr(0, 3);
    if (cwd.size() == 3) {
        SUCCEED("the working directory is a drive root; nothing to distinguish");
        return;
    }
    REQUIRE_FALSE(plat::path_exists(root + input.path));

    bool same = true;
    REQUIRE(plat::paths_same_file(input.path, root + input.path, same, err));
    CHECK_FALSE(same);
}
#endif

TEST_CASE("paths_same_file fails when the input cannot be resolved",
          "[platform][identity][L2-WRT-014][L3-CPP-008]") {
    // Failing to resolve the INPUT is a real failure -- that path is about to
    // be opened -- where failing to resolve the output is routine.
    bool same = false;
    plat::OsError err;
    CHECK_FALSE(plat::paths_same_file(mie_test::TempPath("absent.mie").str(),
                                      mie_test::TempPath("out.csv").str(), same, err));
    CHECK_FALSE(err.ok());
}

TEST_CASE("canonical_path resolves an existing file", "[platform][identity][L3-CPP-008]") {
    const mie_test::TempPath path("canon.mie");
    write_raw(path.str(), std::string("data"));

    std::string canonical;
    plat::OsError err;
    REQUIRE(plat::canonical_path(path.str(), canonical, err));
    CHECK_FALSE(canonical.empty());
    CHECK(plat::path_filename(canonical) == plat::path_filename(path.str()));
}

// ---------------------------------------------------------------------------
// Metadata and encoding
// ---------------------------------------------------------------------------

TEST_CASE("file_metadata reports size and regular-file status",
          "[platform][metadata][L3-CPP-004]") {
    const mie_test::TempPath path("meta.mie");
    write_raw(path.str(), std::string("1234567890"));

    uint64_t size = 0;
    bool is_regular = false;
    plat::OsError err;
    REQUIRE(plat::file_metadata(path.str(), size, is_regular, err));
    CHECK(size == 10);
    CHECK(is_regular);

    SECTION("a directory is not a regular file") {
        REQUIRE(plat::file_metadata(mie_test::temp_root(), size, is_regular, err));
        CHECK_FALSE(is_regular);
    }
}

TEST_CASE("path_exists answers without reporting an error", "[platform][metadata][L3-CPP-004]") {
    const mie_test::TempPath path("exists.mie");
    CHECK_FALSE(plat::path_exists(path.str()));
    write_raw(path.str(), std::string("x"));
    CHECK(plat::path_exists(path.str()));
}

TEST_CASE("remove_file deletes and then reports the absence", "[platform][metadata][L3-CPP-004]") {
    const mie_test::TempPath path("remove.mie");
    write_raw(path.str(), std::string("x"));

    plat::OsError err;
    REQUIRE(plat::remove_file(path.str(), err));
    CHECK_FALSE(raw_exists(path.str()));
    CHECK_FALSE(plat::remove_file(path.str(), err));
    CHECK_FALSE(err.ok());
}

TEST_CASE("wide/narrow conversion round-trips ASCII on every platform",
          "[platform][encoding][L3-CPP-009]") {
    const std::string original("C:/logs/flight-01.mie");
    CHECK(plat::from_wide(plat::to_wide(original)) == original);
    CHECK(plat::to_wide(std::string()).empty());
    CHECK(plat::from_wide(std::wstring()).empty());
}

#if defined(_WIN32)
TEST_CASE("wide/narrow conversion round-trips non-ASCII UTF-8 on Windows",
          "[platform][encoding][L3-CPP-009]") {
    // Windows is a shipping target, so a non-ASCII path has to survive the trip
    // through the W entry points rather than being mangled by whatever the
    // active ANSI codepage happens to be. The POSIX backend does not convert at
    // all -- POSIX paths are opaque bytes -- so this property is Windows-only.
    const std::string original("C:/vuelos/a\xC3\xB1o-2026/registro.mie");
    const std::wstring wide = plat::to_wide(original);
    CHECK_FALSE(wide.empty());
    CHECK(plat::from_wide(wide) == original);
}
#endif

// ---------------------------------------------------------------------------
// Exhaustive path-helper properties
// ---------------------------------------------------------------------------
//
// The path helpers are pure string manipulation, so their input space can be
// swept properly rather than sampled. They are also the piece most likely to
// behave differently between platforms -- a backslash is a separator on Windows
// and an ordinary filename character on POSIX -- so the properties below are
// written to hold on both, with the platform-specific parts guarded.

TEST_CASE("parent and filename always partition a path", "[platform][path][exhaustive]") {
    // For any path, filename() is the tail after the last separator and
    // parent() is what precedes it. Reassembling them must recover the original
    // except for the separator itself -- which is the property a caller relies
    // on when it splits a destination to place a temp file beside it.
    const char* const paths[] = {
        "",     "a",          "a.csv", "/",      "/a",
        "/a/b", "/a/b/c.csv", "a/b",   "a/b/",   "//a",
        "a//b", "./a",        "../a",  "/a/b/.", "/very/deep/path/with/many/segments/out.csv"};

    for (std::size_t i = 0; i < sizeof(paths) / sizeof(paths[0]); ++i) {
        const std::string path(paths[i]);
        const std::string parent = plat::path_parent(path);
        const std::string name = plat::path_filename(path);

        INFO("path = '" << path << "' parent = '" << parent << "' name = '" << name << "'");

        // The filename never contains a separator: that is what makes it a name.
        for (std::size_t k = 0; k < name.size(); ++k) {
            REQUIRE_FALSE(plat::is_separator(name[k]));
        }
        // The name is always a suffix of the path.
        REQUIRE(path.size() >= name.size());
        REQUIRE(path.compare(path.size() - name.size(), name.size(), name) == 0);
        // Everything before the name is the parent plus at most one separator.
        const std::size_t head = path.size() - name.size();
        REQUIRE(head >= parent.size());
        REQUIRE(head - parent.size() <= 1);
    }
}

TEST_CASE("joining a parent to a filename never doubles or loses a separator",
          "[platform][path][exhaustive]") {
    // Every combination of trailing separator on the directory and leading
    // separator on the name -- the four shapes a caller can produce by
    // concatenating configured strings.
    const char* const dirs[] = {"dir", "dir/", "/abs", "/abs/", "/", ""};
    const char* const names[] = {"out.csv", "/out.csv", ""};

    for (std::size_t d = 0; d < sizeof(dirs) / sizeof(dirs[0]); ++d) {
        for (std::size_t n = 0; n < sizeof(names) / sizeof(names[0]); ++n) {
            const std::string joined = plat::path_join(dirs[d], names[n]);
            INFO("dir = '" << dirs[d] << "' name = '" << names[n] << "' -> '" << joined << "'");

            // No doubled forward slash is ever introduced. A doubled separator
            // is harmless to open() but makes a path that no longer matches the
            // one an operator typed, which matters for the collision check.
            REQUIRE(joined.find("//") == std::string::npos);

            const std::string name(names[n]);
            if (!name.empty()) {
                // The name survives intact at the tail.
                const std::string bare = plat::is_separator(name[0]) ? name.substr(1) : name;
                REQUIRE(joined.size() >= bare.size());
                REQUIRE(joined.compare(joined.size() - bare.size(), bare.size(), bare) == 0);
            }
        }
    }
}

TEST_CASE("every byte value is classified as a separator consistently",
          "[platform][path][exhaustive]") {
    for (int i = 0; i < 256; ++i) {
        const char c = static_cast<char>(i);
        const bool sep = plat::is_separator(c);
#if defined(_WIN32)
        REQUIRE(sep == (c == '/' || c == '\\'));
#else
        // On POSIX a backslash is an ordinary filename character. Treating it
        // as a separator would truncate any path that legitimately contains
        // one -- a bug that cannot reproduce on Windows.
        REQUIRE(sep == (c == '/'));
#endif
    }
}

TEST_CASE("a filename with no separator is its own path", "[platform][path][exhaustive]") {
    // Sweeps every single-character filename, including the ones that are
    // separators on the other platform.
    for (int i = 1; i < 256; ++i) {
        const char c = static_cast<char>(i);
        if (plat::is_separator(c)) {
            continue;
        }
        const std::string name(1, c);
        INFO("byte = " << i);
        REQUIRE(plat::path_filename(name) == name);
        REQUIRE(plat::path_parent(name).empty());
    }
}

TEST_CASE("temp names stay unique across many consecutive calls",
          "[platform][atomic][exhaustive][L3-WRT-001]") {
    // The uniqueness guarantee is what stops two writers clobbering each other.
    // A thousand calls in a tight loop is the case a coarse clock breaks: if
    // the name depended on the timestamp alone, many of these would collide.
    const std::string destination = plat::path_join("/some/dir", "out.csv");
    std::vector<std::string> names;
    names.reserve(1000);
    for (int i = 0; i < 1000; ++i) {
        names.push_back(plat::make_temp_name(destination));
    }

    std::sort(names.begin(), names.end());
    CHECK(std::adjacent_find(names.begin(), names.end()) == names.end());

    // Every one of them is still beside the destination and still recognisable.
    for (std::size_t i = 0; i < names.size(); ++i) {
        REQUIRE(plat::path_parent(names[i]) == plat::path_parent(destination));
        REQUIRE(plat::path_filename(names[i]).find("out.csv.aero1553.tmp.") == 0);
    }
}

TEST_CASE("wide and narrow conversion round-trips every ASCII string",
          "[platform][encoding][exhaustive]") {
    // Every single-character ASCII string, plus the empty one. On POSIX the
    // conversion is a byte pass-through and this proves it loses nothing; on
    // Windows it is a real UTF-8/UTF-16 conversion and this proves the same.
    CHECK(plat::from_wide(plat::to_wide(std::string())).empty());
    for (int i = 1; i < 128; ++i) {
        const std::string s(1, static_cast<char>(i));
        if (plat::from_wide(plat::to_wide(s)) != s) {
            INFO("byte = " << i);
            REQUIRE(plat::from_wide(plat::to_wide(s)) == s);
        }
    }
    SUCCEED("every ASCII character survives the wide round-trip");
}

TEST_CASE("paths are UTF-8 end to end, including non-ASCII ones", "[platform][L3-CPP-030]") {
    // The defect this pins: on Windows the C runtime interprets a narrow path
    // in the process ANSI codepage, so a UTF-8 path with any non-ASCII byte
    // named a different file -- usually one that did not exist. The C++ build
    // could not open ANY file at such a path, while every Linux test passed,
    // because on POSIX a path is just bytes and `fopen` is already correct.
    //
    // Written and read through the platform layer, which is the whole point:
    // the same test using std::fopen would fail on Windows for the very reason
    // this exists to prevent.
    const mie_test::TempPath path("plat-Ã¼nÃ¯code-éç½®.bin");
    const std::string payload = "bytes at a non-ASCII path";

    mie::platform::OsError err;
    {
        mie::platform::AtomicFile file;
        REQUIRE(file.create(path.str(), err));
        REQUIRE(file.write(payload.data(), payload.size(), err));
        REQUIRE(file.commit(err) == plat::COMMIT_DONE);
    }

    REQUIRE(mie::platform::path_exists(path.str()));

    std::vector<uint8_t> read_back;
    REQUIRE(mie::platform::read_file(path.str(), read_back, err));
    REQUIRE(std::string(read_back.begin(), read_back.end()) == payload);
}

TEST_CASE("read_file distinguishes end of file from a read failure", "[platform][L3-CPP-030]") {
    SECTION("a missing path fails rather than reporting empty") {
        // An empty result and a failed read are not the same answer, and a
        // caller that cannot tell them apart reports a missing file as an empty
        // one.
        const mie_test::TempPath absent("plat-absent.bin");
        std::vector<uint8_t> bytes;
        mie::platform::OsError err;
        CHECK_FALSE(mie::platform::read_file(absent.str(), bytes, err));
        CHECK_FALSE(err.ok());
    }

    SECTION("a genuinely empty file reads as empty and succeeds") {
        const mie_test::TempFile empty("plat-empty.bin", std::string());
        std::vector<uint8_t> bytes;
        mie::platform::OsError err;
        CHECK(mie::platform::read_file(empty.str(), bytes, err));
        CHECK(bytes.empty());
        CHECK(err.ok());
    }

    SECTION("a file larger than the read buffer round-trips exactly") {
        // The loop reads in 8 KiB chunks, so a payload spanning several of them
        // is what catches an off-by-one in the append.
        std::string payload;
        for (std::size_t i = 0; i < 20000; ++i) {
            payload += static_cast<char>('a' + (i % 26));
        }
        const mie_test::TempFile big("plat-big.bin", payload);
        std::vector<uint8_t> bytes;
        mie::platform::OsError err;
        REQUIRE(mie::platform::read_file(big.str(), bytes, err));
        REQUIRE(bytes.size() == payload.size());
        REQUIRE(std::string(bytes.begin(), bytes.end()) == payload);
    }
}
