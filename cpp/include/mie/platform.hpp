// SPDX-License-Identifier: Apache-2.0
//
// The ONLY module in this tree permitted to touch operating-system APIs.
//
// The MIE decoder proper -- decode, sync, models, filter, order, merge and the
// CSV row formatter -- operates on a `const uint8_t*` plus a length and emits
// bytes. It has no operating-system surface at all and compiles identically on
// SLES 12 SP5 and Windows. Five concerns do touch the OS, and all five live
// here so the rest of the tree stays free of conditional compilation:
//
//   1. mapping the input file read-only
//   2. the atomic temp-file + rename output strategy (L2-WRT-015, L3-WRT-001)
//   3. directory enumeration behind `--glob`
//   4. byte-exact stdout (no CRLF translation, L2-WRT-012; a closed pipe is a
//      write error, not a signal, L2-WRT-018)
//   5. path identity and encoding (L2-WRT-014's InputOutputCollision check)
//
// `scripts/assert-platform-confined.sh` fails the build if any other
// translation unit includes <windows.h>, <sys/mman.h> or their neighbours.
//
// This header names no OS type. Handles are carried as `void*` rather than
// through a pimpl so that including it costs no allocation and drags in no
// platform header -- which is what lets `reader.hpp` hold a MappedFile by
// value without <windows.h> leaking into every consumer.
//
// PATH ENCODING. Paths cross this boundary as UTF-8 std::string, always. The
// Win32 backend widens to UTF-16 and calls the W entry points, so a non-ASCII
// path behaves the way it does under the Rust implementation instead of being
// mangled by whatever the active ANSI codepage happens to be. The POSIX backend
// passes the bytes through untouched.
//
// ERROR STYLE. Nothing here throws. Every fallible call returns bool and fills
// an OsError out-parameter; mapping those to MieError happens in one place in
// the layers above. That keeps the platform layer directly testable and keeps
// OS error text out of the decoder's error type.

#ifndef MIE_PLATFORM_HPP
#define MIE_PLATFORM_HPP

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace mie {
namespace platform {

/// A failed OS call, already decoded to text.
///
/// `code` is errno on POSIX and GetLastError() on Win32. The two number spaces
/// are unrelated, so callers must not compare a code across platforms --
/// `message` is the portable part.
struct OsError {
    int code;
    std::string message;

    OsError() : code(0) {}

    bool ok() const { return code == 0; }
    void clear() {
        code = 0;
        message.clear();
    }
};

// ---------------------------------------------------------------------------
// 1. Read-only memory map
// ---------------------------------------------------------------------------

/// A whole file mapped read-only, exposed as a flat byte range.
///
/// This deliberately mirrors the shape the Rust reader gets from memmap2
/// (a &[u8]), because `sync` and `decode` are ports of modules written against
/// that shape. Keeping the interface identical is what makes them
/// transliterations rather than reimplementations, and transliterations are
/// what the shared conformance oracles can actually hold to account.
///
/// A zero-byte file is rejected as FileEmpty by the caller *before* it gets
/// here (L2-RDR-006), which is also what sidesteps CreateFileMapping's refusal
/// to map a zero-length file -- the empty case never reaches the mapping call
/// on either platform.
class MappedFile {
  public:
    MappedFile();
    ~MappedFile();

    // noexcept is load-bearing, not decoration: a container holding MappedFile
    // falls back to COPYING on reallocation if the move operations can throw,
    // and this type is deliberately non-copyable, so the fallback is a compile
    // error rather than a slow path. These genuinely cannot throw -- they move
    // four scalars and null the source.
    MappedFile(MappedFile&& other) noexcept;
    MappedFile& operator=(MappedFile&& other) noexcept;

    /// Map `utf8_path` read-only. On failure returns false and fills `err`.
    ///
    /// Opens before stat'ing, deliberately: an unopenable path must report I/O
    /// rather than "empty", because a directory stats as zero bytes on Windows
    /// (see the MieFileIoError note in docs/ERROR-CATALOG.md).
    bool open(const std::string& utf8_path, OsError& err);

    void close();

    bool is_open() const { return data_ != nullptr; }
    const uint8_t* data() const { return data_; }
    uint64_t size() const { return size_; }

  private:
    MappedFile(const MappedFile&);
    MappedFile& operator=(const MappedFile&);

    void* file_handle_;     // fd cast through intptr_t on POSIX, HANDLE on Win32
    void* mapping_handle_;  // unused on POSIX, HANDLE on Win32
    const uint8_t* data_;
    uint64_t size_;
};

// ---------------------------------------------------------------------------
// 2. Atomic output
// ---------------------------------------------------------------------------

/// Buffered writer over a temp file that is renamed into place on commit.
///
/// The temp file is created *beside* the destination so the rename stays on one
/// filesystem and is therefore atomic, and its name carries the process id, a
/// per-process counter and a wall-clock nanosecond stamp (L3-WRT-001) so that
/// two writers targeting one destination -- including two inside one process --
/// cannot collide. Creation is exclusive (O_CREAT|O_EXCL / CREATE_NEW); on the
/// near-impossible clash the next name is tried.
///
/// WINDOWS TRAP, and the reason this is not std::rename: the MSVC CRT's
/// rename() *fails* when the destination already exists. Rust's std::fs::rename
/// uses MoveFileEx with MOVEFILE_REPLACE_EXISTING under the hood, so a C++ port
/// written with plain rename() would diverge from the Rust behaviour on Windows
/// only -- green on Linux, broken on the shipped Windows binary. The Win32
/// backend uses MoveFileExW for exactly this.
///
/// Output is byte-exact: the handle is opened in binary mode, so a newline is
/// never rewritten to CRLF (L2-WRT-012).
/// How a finished temp file is moved onto its destination (L2-WRT-023).
///
/// `COMMIT_REPLACE` is the shipped default: overwriting an existing destination
/// is what an operator re-running a batch expects. `COMMIT_NO_REPLACE` is what
/// `--no-clobber` selects, and it has to be enforced BY THE COMMIT ITSELF -- a
/// `path_exists` test before the file is opened answers a question about the
/// past, and between that answer and the rename any other process may create the
/// destination. The rename then destroys it, which is the exact outcome the flag
/// exists to prevent.
enum CommitMode {
    /// rename(2) / MoveFileExW(MOVEFILE_REPLACE_EXISTING): replaces.
    COMMIT_REPLACE,
    /// The destination is claimed atomically; an existing one is refused.
    COMMIT_NO_REPLACE
};

/// What a commit did. Three outcomes, not two: "the destination already exists"
/// is a refusal the caller reports as MieError::clobber_refused, and flattening
/// it into the failure arm would report a policy decision as an OS error.
enum CommitStatus {
    /// The destination now holds the temp file's contents.
    COMMIT_DONE,
    /// COMMIT_NO_REPLACE only: the destination existed; nothing was written.
    COMMIT_EXISTS,
    /// The commit failed for some other reason; `err` says which.
    COMMIT_ERROR
};

class AtomicFile {
  public:
    AtomicFile();
    ~AtomicFile();

    /// Create the temp file beside `final_utf8_path`. Does not touch the
    /// destination.
    bool create(const std::string& final_utf8_path, OsError& err);

    /// Select the commit mode for EVERY target this file can produce -- the
    /// destination itself and `<destination>.partial` alike (L2-WRT-023).
    /// Defaults to `COMMIT_REPLACE`, so L2-WRT-017's "overwrite succeeds by
    /// default" is what a caller that says nothing gets.
    void set_commit_mode(CommitMode mode);

    /// Append bytes. Buffered; a partial OS write is retried to completion.
    bool write(const char* bytes, std::size_t len, OsError& err);

    /// Flush, close, and move onto the destination per the commit mode.
    CommitStatus commit(OsError& err);

    /// Flush, close, and move onto <destination><suffix> instead -- the
    /// `.partial` path taken by --allow-partial (L3-WRT-002). The destination
    /// itself is left untouched.
    CommitStatus commit_with_suffix(const std::string& suffix, OsError& err);

    /// Close and unlink the temp file. Safe to call twice; safe after commit.
    void abort();

    const std::string& temp_path() const { return temp_path_; }
    const std::string& final_path() const { return final_path_; }

  private:
    AtomicFile(const AtomicFile&);
    AtomicFile& operator=(const AtomicFile&);

    bool flush(OsError& err);
    bool raw_write(const char* bytes, std::size_t len, OsError& err);
    /// Close the handle and report whether the close itself failed. Shared by
    /// both commit entry points so the "buffered data never reached the
    /// filesystem" case cannot be handled in one and forgotten in the other.
    bool finish_stream(OsError& err);
    /// The move itself, once the handle is closed. Honours the commit mode.
    CommitStatus place(const std::string& destination, OsError& err);
    /// Record that the temp file no longer exists under its own name, so
    /// abort() must not unlink it. Shared by every successful move in both
    /// backends.
    void mark_committed();

    void* handle_;
    std::string temp_path_;
    std::string final_path_;
    std::vector<char> buffer_;
    bool committed_;
    CommitMode mode_;
};

/// Bytes buffered before an OS write is issued. Exposed for the test that
/// proves buffering happens at all rather than every row hitting a syscall.
extern const std::size_t kWriteBufferSize;

#if !defined(_WIN32)
/// The `COMMIT_NO_REPLACE` fallback on POSIX: claim `dest_utf8` with an
/// exclusive create, then rename `temp_utf8` over that reservation.
///
/// `AtomicFile` reaches it only when link(2) fails for a reason other than
/// EEXIST -- FAT/exFAT, some network filesystems -- which no test host's
/// filesystem does. Exposed so a test can drive it directly, as Rust's
/// `reserve_then_rename` is; otherwise the path an operator on a USB stick
/// takes would be the one path never run. Windows needs no fallback:
/// MoveFileExW without MOVEFILE_REPLACE_EXISTING is natively non-replacing.
CommitStatus reserve_then_rename(const std::string& temp_utf8, const std::string& dest_utf8,
                                 OsError& err);
#endif

/// Build the unique temp-file name L3-WRT-001 requires, beside `final_path`.
///
/// Portable and side-effect-free -- it composes a name, it does not create a
/// file -- so the uniqueness property is testable without touching a disk.
/// Successive calls in one process never return the same name.
std::string make_temp_name(const std::string& final_path);

// ---------------------------------------------------------------------------
// 3. Directory enumeration
// ---------------------------------------------------------------------------

/// List the entry names (not paths) directly inside `utf8_dir`.
///
/// Non-recursive, and the dot entries are omitted. Ordering is whatever the OS
/// returns; callers that need determinism sort, because --glob results must not
/// depend on directory layout.
bool list_directory(const std::string& utf8_dir, std::vector<std::string>& names, OsError& err);

// ---------------------------------------------------------------------------
// 4. Byte-exact output
// ---------------------------------------------------------------------------

/// Prepare stdout and stderr for the decoder's output. Call once, early, from
/// main().
///
/// On Windows this puts both streams into binary mode: the CRT otherwise
/// rewrites every newline into CRLF, which silently breaks every stdout
/// conformance oracle on that platform alone, and gave every diagnostic line a
/// different ending from the one Rust and Python write (L2-CLI-022). It was
/// `prepare_stdout` and left stderr in text mode until then.
///
/// On POSIX it ignores SIGPIPE. The default disposition kills the process the
/// moment a downstream reader closes the pipe (`aero1553 decode x.mie | head`),
/// so it exits 141 and the EPIPE that L2-WRT-018 turns into exit 0 is never
/// seen. Rust's runtime and CPython both ignore SIGPIPE before main; C++ is the
/// only implementation where it has to be asked for.
void prepare_output_streams();

// ---------------------------------------------------------------------------
// 5. Path identity, metadata, and encoding
// ---------------------------------------------------------------------------

/// Resolve `utf8_path` to a canonical absolute form, following symlinks.
///
/// Requires the path to exist. Used for the input/output collision check, which
/// must be symlink-safe: decoding a file onto itself is unsafe under a mapping.
bool canonical_path(const std::string& utf8_path, std::string& out, OsError& err);

/// True when both paths resolve to the same file.
///
/// Mirrors paths_refer_to_same_file in rust/src/writer.rs: a collision is only
/// ever reported when *both* sides canonicalize. An output path that does not
/// exist yet cannot collide, so its parent directory is canonicalized instead
/// and the filename compared.
bool paths_same_file(const std::string& utf8_input, const std::string& utf8_output, bool& same,
                     OsError& err);

/// The program's arguments as UTF-8, excluding the program name.
///
/// THE ARGUMENTS THEMSELVES ARE OS SURFACE ON WINDOWS. `main(argc, argv)`
/// delivers them in the process ANSI codepage, and any character that codepage
/// cannot represent has already been replaced by `?` before a single line of
/// this program runs. No amount of care further down can recover it: a
/// recording at a non-ASCII path was simply unopenable, and the diagnostic
/// printed the mangled name back as though the file were missing.
///
/// So Windows ignores `argv` entirely and re-reads the real UTF-16 command
/// line. POSIX passes `argv` through, where it is already bytes.
///
/// This is why the program takes its arguments as a vector of UTF-8 strings
/// rather than `(argc, argv)` -- the conversion has to happen at the boundary,
/// and the boundary is here.
std::vector<std::string> command_line_arguments(int argc, char** argv);

/// Open `utf8_path` for binary reading, or NULL with `err` set.
///
/// THIS IS OS SURFACE, despite `fopen` being standard C. On Windows the CRT
/// interprets a `char*` path in the ANSI codepage, so a UTF-8 path containing
/// any non-ASCII byte opens the wrong name -- or, far more often, nothing at
/// all. Paths are carried as UTF-8 throughout this program (ADR-0003), so every
/// read by path has to widen at this boundary like every other path operation.
///
/// `scripts/assert-platform-confined.sh` cannot enforce this one: it bans OS
/// *headers*, and a bare `std::fopen` needs none. The config loader, the merge
/// manifest reader and the dump all called it directly, and all three failed on
/// a non-ASCII path on Windows while passing every test on Linux.
///
/// The caller closes the handle.
std::FILE* open_read(const std::string& utf8_path, OsError& err);

/// Read the whole file at `utf8_path` into `bytes`.
///
/// One implementation of the read loop, because distinguishing end-of-file from
/// a real error is subtle enough to get wrong -- `fread` reports a short read
/// for both, asking again in the EOF state does nothing, and after a FAILED
/// read the stream position is indeterminate so a retry can duplicate or skip
/// bytes. That reasoning was written out three times in this tree before it
/// lived here once.
bool read_file(const std::string& utf8_path, std::vector<uint8_t>& bytes, OsError& err);

/// Existence test. Never fails -- a path that cannot be stat'ed is "no".
/// Follows symlinks, so a dangling one does not exist.
bool path_exists(const std::string& utf8_path);

/// Size in bytes, and whether the path is a regular file -- both of what a
/// symlink points to, never of the link itself, on every platform.
bool file_metadata(const std::string& utf8_path, uint64_t& size, bool& is_regular, OsError& err);

bool remove_file(const std::string& utf8_path, OsError& err);

/// Capture why the last C-runtime STREAM call (`fwrite`, `fflush`) failed.
///
/// Separate from the OS-call errors elsewhere in this header because the code
/// spaces differ. `fwrite` reports through `errno` on every platform, including
/// Windows -- but `OsError::code` is documented as `GetLastError()` there, and
/// that is the space `MieError`'s broken-pipe classification matches against.
/// The Win32 backend therefore TRANSLATES into the Win32 space rather than
/// leaking a CRT errno through a field that means something else.
///
/// The translation matters for one case in particular. A downstream consumer
/// closing the pipe -- `aero1553 decode x.mie | head` -- surfaces on POSIX
/// as EPIPE, which L2-WRT-018 turns into exit 0. Windows does not report it the
/// same way: the CRT gives EPIPE through some paths and EINVAL through others,
/// which is exactly the divergence that once made that command exit 1 on
/// Windows while exiting 0 on Linux (see the note in
/// `python/src/aero1553/writer.py`, which reached the same conclusion).
void capture_stream_error(OsError& err);

/// The current process id, as it appears in a temp-file name.
uint64_t process_id();

/// Wall clock in nanoseconds since the epoch (L3-WRT-001's timestamp
/// component). Portable via std::chrono -- present in libstdc++ 4.8.
uint64_t wall_clock_nanos();

// --- Portable path string helpers ---
//
// Deliberately string manipulation, not filesystem calls: <filesystem> is C++17
// and the SLES 12 floor is C++11. On Windows both forward and back slash
// separate; on POSIX only forward slash does, and a backslash is a legal
// filename character. Getting that backwards corrupts paths on one platform
// while looking correct on the other, so it is decided here once.

bool is_separator(char c);

/// Everything before the final separator, or an empty string when there is none.
std::string path_parent(const std::string& utf8_path);

/// Everything after the final separator.
std::string path_filename(const std::string& utf8_path);

/// Join with the platform's preferred separator, collapsing a doubled one.
std::string path_join(const std::string& dir, const std::string& name);

// --- UTF-8 <-> UTF-16 ---
//
// Declared unconditionally so the round-trip property is stated in one place,
// but only the Win32 backend does real work: the POSIX build passes bytes
// through, since POSIX paths are opaque byte strings. On Windows the narrow
// form is WTF-8 (text::utf16_to_wtf8), so a file name holding an unpaired
// surrogate survives the round trip (L2-CLI-021).

std::wstring to_wide(const std::string& utf8);
std::string from_wide(const std::wstring& wide);

}  // namespace platform
}  // namespace mie

#endif  // MIE_PLATFORM_HPP
