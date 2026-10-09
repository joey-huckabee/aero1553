// SPDX-License-Identifier: Apache-2.0
//
// POSIX backend for the platform layer. Built on Linux (the SLES 12 SP5 target
// and the WSL2 development host); the Win32 half lives in platform_win32.cpp
// and exactly one of the two is compiled into any given binary.
//
// This is one of the two files permitted to include OS headers. See
// scripts/assert-platform-confined.sh.
//
// _FILE_OFFSET_BITS=64 is supplied by the build, not defined here: it has to be
// set before ANY libc header is reached, including ones pulled in transitively
// by the C++ standard library, and a #define at the top of one translation unit
// is not a reliable way to guarantee that. MIE recordings routinely exceed
// 2 GiB, so an off_t that silently stays 32-bit is a live failure, not a
// hypothetical one.

#include "mie/platform.hpp"

#if defined(_WIN32)
#error "platform_posix.cpp must not be compiled for Windows targets"
#endif

#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>

namespace mie {
namespace platform {

namespace {

/// The mode every output file is created with, before the umask (L3-WRT-001).
///
/// The platform default -- what Rust's OpenOptions and a shell redirect ask
/// for -- so that what lands on disk is `0666 & ~umask` whichever
/// implementation wrote it. The operator's umask is the policy; the decoder
/// does not impose a narrower one of its own.
const mode_t kOutputMode = 0666;

/// Encode a file descriptor into the opaque void* the header carries.
///
/// Offset by one so that a zeroed handle means "closed". Descriptor 0 is
/// legitimately open-able -- if stdin were closed before the decoder ran, the
/// next open() returns 0 -- and treating that as "closed" would leak the
/// descriptor and then map a file the object claims not to hold.
///
/// NOLINT(performance-no-int-to-ptr) here and in decode_fd, deliberately and
/// only here. The check is right in general, but the void* in the header is not
/// a pointer -- it is opaque storage, chosen so that mie/platform.hpp can be
/// included without dragging <windows.h> into every consumer. The Win32 backend
/// stores a real HANDLE in the same slot. Changing the member to intptr_t would
/// not remove the cast, it would move it to the Windows side, where the same
/// check would fire on a genuine pointer instead of on an integer.
///
/// Suppressed at the two call sites rather than disabled in .clang-tidy, so a
/// real int-to-pointer cast introduced anywhere else still trips the gate.
void* encode_fd(int fd) {
    // NOLINTNEXTLINE(performance-no-int-to-ptr)
    return reinterpret_cast<void*>(static_cast<intptr_t>(fd) + 1);
}

int decode_fd(void* handle) {
    if (handle == nullptr) {
        return -1;
    }
    return static_cast<int>(reinterpret_cast<intptr_t>(handle) - 1);
}

void fill_errno(OsError& err, int captured) {
    err.code = captured;
    // strerror_r has two incompatible signatures across libc configurations and
    // strerror is not thread-safe. The decoder is single-threaded, so plain
    // strerror is correct here and avoids a portability fight that buys nothing.
    const char* text = strerror(captured);
    err.message = text != nullptr ? text : "unknown error";
}

}  // namespace

// ---------------------------------------------------------------------------
// MappedFile
// ---------------------------------------------------------------------------

MappedFile::MappedFile()
    : file_handle_(nullptr), mapping_handle_(nullptr), data_(nullptr), size_(0) {}

MappedFile::~MappedFile() { close(); }

MappedFile::MappedFile(MappedFile&& other) noexcept
    : file_handle_(other.file_handle_),
      mapping_handle_(other.mapping_handle_),
      data_(other.data_),
      size_(other.size_) {
    other.file_handle_ = nullptr;
    other.mapping_handle_ = nullptr;
    other.data_ = nullptr;
    other.size_ = 0;
}

MappedFile& MappedFile::operator=(MappedFile&& other) noexcept {
    if (this != &other) {
        close();
        file_handle_ = other.file_handle_;
        mapping_handle_ = other.mapping_handle_;
        data_ = other.data_;
        size_ = other.size_;
        other.file_handle_ = nullptr;
        other.mapping_handle_ = nullptr;
        other.data_ = nullptr;
        other.size_ = 0;
    }
    return *this;
}

bool MappedFile::open(const std::string& utf8_path, OsError& err) {
    close();
    err.clear();

    // Open BEFORE stat, deliberately. An unopenable path has to report I/O
    // rather than "empty" -- docs/ERROR-CATALOG.md pins this, because a
    // directory stats as zero bytes on Windows and the two implementations
    // already in the tree agree on opening first.
    const int fd = ::open(utf8_path.c_str(), O_RDONLY);
    if (fd < 0) {
        fill_errno(err, errno);
        return false;
    }

    struct stat st;
    if (::fstat(fd, &st) != 0) {
        fill_errno(err, errno);
        ::close(fd);
        return false;
    }

    if (!S_ISREG(st.st_mode)) {
        err.code = EINVAL;
        err.message = "not a regular file";
        ::close(fd);
        return false;
    }

    if (st.st_size == 0) {
        // Callers reject empty inputs as FileEmpty before reaching here
        // (L2-RDR-006). Guarded anyway because mmap of a zero-length range
        // fails with EINVAL, which would surface as a confusing I/O error
        // rather than the specific one the operator needs.
        err.code = EINVAL;
        err.message = "file is empty";
        ::close(fd);
        return false;
    }

    const auto length = static_cast<size_t>(st.st_size);
    // const void*: the mapping is PROT_READ, so nothing here may write through
    // it, and saying so lets the compiler and cppcheck both hold that line.
    const void* addr = ::mmap(nullptr, length, PROT_READ, MAP_PRIVATE, fd, 0);
    if (addr == MAP_FAILED) {
        fill_errno(err, errno);
        ::close(fd);
        return false;
    }

    file_handle_ = encode_fd(fd);
    data_ = static_cast<const uint8_t*>(addr);
    size_ = static_cast<uint64_t>(length);
    return true;
}

void MappedFile::close() {
    if (data_ != nullptr) {
        ::munmap(const_cast<void*>(static_cast<const void*>(data_)), static_cast<size_t>(size_));
        data_ = nullptr;
        size_ = 0;
    }
    const int fd = decode_fd(file_handle_);
    if (fd >= 0) {
        ::close(fd);
        file_handle_ = nullptr;
    }
}

// ---------------------------------------------------------------------------
// AtomicFile
// ---------------------------------------------------------------------------

AtomicFile::AtomicFile() : handle_(nullptr), committed_(false), mode_(COMMIT_REPLACE) {
    buffer_.reserve(kWriteBufferSize);
}

AtomicFile::~AtomicFile() { abort(); }

bool AtomicFile::create(const std::string& final_utf8_path, OsError& err) {
    abort();
    err.clear();
    committed_ = false;
    final_path_ = final_utf8_path;

    // Exclusive create, retrying on the near-impossible name clash (L3-WRT-001).
    // The retry count is small on purpose: if eight consecutive
    // pid+counter+nanosecond names all exist, something is wrong that another
    // attempt will not fix.
    for (int attempt = 0; attempt < 8; ++attempt) {
        const std::string candidate = make_temp_name(final_utf8_path);
        // The temp file becomes the operator's output, so its mode IS the
        // output's mode. This asked for 0644, which agreed with Rust and Python
        // only under umask 022.
        const int fd = ::open(candidate.c_str(), O_WRONLY | O_CREAT | O_EXCL, kOutputMode);
        if (fd >= 0) {
            handle_ = encode_fd(fd);
            temp_path_ = candidate;
            buffer_.clear();
            return true;
        }
        if (errno != EEXIST) {
            fill_errno(err, errno);
            return false;
        }
    }
    err.code = EEXIST;
    err.message = "could not create a unique temporary file beside the destination";
    return false;
}

bool AtomicFile::raw_write(const char* bytes, std::size_t len, OsError& err) {
    const int fd = decode_fd(handle_);
    if (fd < 0) {
        err.code = EBADF;
        err.message = "temporary file is not open";
        return false;
    }
    std::size_t written = 0;
    while (written < len) {
        // A short write is normal, not an error: write(2) is permitted to
        // transfer fewer bytes than asked, and a pipe or a full-ish filesystem
        // will do so. Looping is what makes "the row was written" true.
        const ssize_t n = ::write(fd, bytes + written, len - written);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            fill_errno(err, errno);
            return false;
        }
        if (n == 0) {
            err.code = EIO;
            err.message = "write made no progress";
            return false;
        }
        written += static_cast<std::size_t>(n);
    }
    return true;
}

bool AtomicFile::flush(OsError& err) {
    if (buffer_.empty()) {
        return true;
    }
    if (!raw_write(&buffer_[0], buffer_.size(), err)) {
        return false;
    }
    buffer_.clear();
    return true;
}

bool AtomicFile::write(const char* bytes, std::size_t len, OsError& err) {
    err.clear();
    if (len >= kWriteBufferSize) {
        // Bypass the buffer for a payload that would immediately overflow it.
        // Copying into a buffer only to flush it in the next statement is pure
        // cost, and the streaming design point is about not accumulating rows,
        // not about forcing every byte through one path.
        return flush(err) && raw_write(bytes, len, err);
    }
    if (buffer_.size() + len > kWriteBufferSize && !flush(err)) {
        return false;
    }
    buffer_.insert(buffer_.end(), bytes, bytes + len);
    return true;
}

void AtomicFile::set_commit_mode(CommitMode mode) { mode_ = mode; }

CommitStatus AtomicFile::commit(OsError& err) { return commit_with_suffix(std::string(), err); }

CommitStatus AtomicFile::commit_with_suffix(const std::string& suffix, OsError& err) {
    err.clear();
    // The final flush and the close are PART OF THE COMMIT (L2-WRT-024): they
    // are where the last buffered rows actually reach the filesystem, so they
    // are the likeliest place for a disk-full error to land, and a failure in
    // either means the destination must not be replaced.
    if (!flush(err)) {
        return COMMIT_ERROR;
    }
    if (!finish_stream(err)) {
        return COMMIT_ERROR;
    }
    return place(final_path_ + suffix, err);
}

bool AtomicFile::finish_stream(OsError& err) {
    const int fd = decode_fd(handle_);
    if (fd < 0) {
        err.code = EBADF;
        err.message = "temporary file is not open";
        return false;
    }
    if (::close(fd) != 0) {
        // close() reporting an error is the last chance to learn that buffered
        // data never reached the filesystem. Swallowing it would let a
        // truncated CSV be renamed into place and reported as success.
        fill_errno(err, errno);
        handle_ = nullptr;
        return false;
    }
    handle_ = nullptr;
    return true;
}

CommitStatus AtomicFile::place(const std::string& destination, OsError& err) {
    if (mode_ == COMMIT_REPLACE) {
        if (::rename(temp_path_.c_str(), destination.c_str()) != 0) {
            fill_errno(err, errno);
            return COMMIT_ERROR;
        }
        committed_ = true;
        temp_path_.clear();
        return COMMIT_DONE;
    }

    // COMMIT_NO_REPLACE, mechanism 1: link(2) fails with EEXIST when the
    // destination exists, and otherwise publishes the COMPLETE file under its
    // final name in one atomic step -- so a concurrent reader never observes a
    // partial or empty file, exactly as with the replacing rename.
    //
    // renameat2(RENAME_NOREPLACE) would do it in one call, but it needs Linux
    // 3.15 and a glibc that wraps it (2.28). SLES 12 SP5 ships glibc 2.22, so
    // reaching it would mean a raw syscall(SYS_renameat2, ...) with a
    // hand-written fallback for every kernel that lacks it -- more OS surface,
    // in the layer that exists to contain OS surface, for no behaviour link(2)
    // does not already give.
    if (::link(temp_path_.c_str(), destination.c_str()) == 0) {
        // The link published a second name for the same bytes rather than
        // moving them, so the temp is still there by design. Unlinking is best
        // effort: the destination is committed either way, and leaving
        // `committed_` false when the unlink fails just gives abort() a second
        // attempt at it.
        if (::unlink(temp_path_.c_str()) == 0) {
            committed_ = true;
            temp_path_.clear();
        }
        return COMMIT_DONE;
    }
    if (errno == EEXIST) {
        return COMMIT_EXISTS;
    }

    // Mechanism 2: the link failed for a reason that has nothing to do with
    // the destination.
    const CommitStatus status = reserve_then_rename(temp_path_, destination, err);
    if (status == COMMIT_DONE) {
        committed_ = true;
        temp_path_.clear();
    }
    return status;
}

CommitStatus reserve_then_rename(const std::string& temp_utf8, const std::string& dest_utf8,
                                 OsError& err) {
    // Hard links do not exist on FAT/exFAT and are refused by some network
    // filesystems. O_EXCL claims the name atomically -- one of two racing
    // processes wins it -- and the rename that follows overwrites only OUR OWN
    // zero-byte reservation. The narrow cost is that the destination is briefly
    // an empty file, which is why this is the fallback and not the primary.
    const int reserved = ::open(dest_utf8.c_str(), O_WRONLY | O_CREAT | O_EXCL, kOutputMode);
    if (reserved < 0) {
        if (errno == EEXIST) {
            return COMMIT_EXISTS;
        }
        fill_errno(err, errno);
        return COMMIT_ERROR;
    }
    ::close(reserved);
    if (::rename(temp_utf8.c_str(), dest_utf8.c_str()) != 0) {
        fill_errno(err, errno);
        // Take the reservation back out. Leaving it would hand the operator an
        // empty CSV where the failure message says nothing was written.
        ::unlink(dest_utf8.c_str());
        return COMMIT_ERROR;
    }
    return COMMIT_DONE;
}

void AtomicFile::abort() {
    const int fd = decode_fd(handle_);
    if (fd >= 0) {
        ::close(fd);
        handle_ = nullptr;
    }
    if (!committed_ && !temp_path_.empty()) {
        ::unlink(temp_path_.c_str());
        temp_path_.clear();
    }
    buffer_.clear();
}

// ---------------------------------------------------------------------------
// Directory enumeration, metadata, identity
// ---------------------------------------------------------------------------

std::vector<std::string> command_line_arguments(int argc, char** argv) {
    // POSIX arguments are bytes and a UTF-8 locale passes them through intact,
    // so there is nothing to convert. The Windows backend is where this earns
    // its existence.
    std::vector<std::string> out;
    if (argc > 1) {
        out.reserve(static_cast<std::size_t>(argc - 1));
        for (int i = 1; i < argc; ++i) {
            out.emplace_back(argv[i]);
        }
    }
    return out;
}

std::FILE* open_read(const std::string& utf8_path, OsError& err) {
    err.clear();
    // POSIX filenames are bytes, and UTF-8 is already bytes, so no conversion
    // is needed here. The Windows backend is where this earns its existence.
    std::FILE* handle = std::fopen(utf8_path.c_str(), "rb");
    if (handle == nullptr) {
        fill_errno(err, errno);
        return nullptr;
    }
    return handle;
}

bool list_directory(const std::string& utf8_dir, std::vector<std::string>& names, OsError& err) {
    err.clear();
    names.clear();

    DIR* dir = ::opendir(utf8_dir.c_str());
    if (dir == nullptr) {
        fill_errno(err, errno);
        return false;
    }

    // readdir returning NULL is ambiguous between "end of stream" and "error",
    // and the two are told apart only by errno -- which readdir does not clear.
    errno = 0;
    for (const struct dirent* entry = ::readdir(dir); entry != nullptr; entry = ::readdir(dir)) {
        const char* name = entry->d_name;
        if (name[0] == '.' && (name[1] == '\0' || (name[1] == '.' && name[2] == '\0'))) {
            continue;
        }
        names.emplace_back(name);
        errno = 0;
    }
    const int captured = errno;
    ::closedir(dir);

    if (captured != 0) {
        fill_errno(err, captured);
        names.clear();
        return false;
    }
    return true;
}

void prepare_stdout() {
    // POSIX streams do not translate line endings, so there is no binary mode
    // to set. What there is to do is SIGPIPE: left at its default, a closed
    // downstream pipe kills the process (exit 141) before the failed write can
    // return EPIPE, and L2-WRT-018's "broken pipe exits 0" never gets a chance
    // to apply. Ignored, the write fails with EPIPE and the writer's existing
    // classification takes over. This is what Rust's runtime and CPython do
    // before main.
    //
    // The result is discarded deliberately. signal() fails only for an invalid
    // signal number (EINVAL), and SIGPIPE is valid on every POSIX system; were
    // it ever to fail, the disposition would stay at its default, which is the
    // behaviour this call improves on rather than a new failure to report.
    (void)::signal(SIGPIPE, SIG_IGN);
}

bool canonical_path(const std::string& utf8_path, std::string& out, OsError& err) {
    err.clear();
    out.clear();

    // realpath(path, NULL) allocates, which is the only form that is safe
    // against a path longer than PATH_MAX. The array form silently overflows on
    // some libc versions and has been a source of CVEs.
    char* resolved = ::realpath(utf8_path.c_str(), nullptr);
    if (resolved == nullptr) {
        fill_errno(err, errno);
        return false;
    }
    out.assign(resolved);
    ::free(resolved);
    return true;
}

bool file_metadata(const std::string& utf8_path, uint64_t& size, bool& is_regular, OsError& err) {
    err.clear();
    size = 0;
    is_regular = false;

    struct stat st;
    if (::stat(utf8_path.c_str(), &st) != 0) {
        fill_errno(err, errno);
        return false;
    }
    size = static_cast<uint64_t>(st.st_size);
    is_regular = S_ISREG(st.st_mode) != 0;
    return true;
}

bool remove_file(const std::string& utf8_path, OsError& err) {
    err.clear();
    if (::unlink(utf8_path.c_str()) != 0) {
        fill_errno(err, errno);
        return false;
    }
    return true;
}

void capture_stream_error(OsError& err) { fill_errno(err, errno); }

uint64_t process_id() { return static_cast<uint64_t>(::getpid()); }

// ---------------------------------------------------------------------------
// UTF-8 <-> UTF-16
// ---------------------------------------------------------------------------
//
// POSIX paths are opaque byte strings, so nothing here ever needs to widen one.
// These exist to keep the header's declaration set identical across backends;
// they widen and narrow byte-for-byte and are not a Unicode conversion. Nothing
// in the POSIX build calls them outside the test that pins that fact.

std::wstring to_wide(const std::string& utf8) {
    std::wstring out;
    out.reserve(utf8.size());
    for (std::size_t i = 0; i < utf8.size(); ++i) {
        out.push_back(static_cast<wchar_t>(static_cast<unsigned char>(utf8[i])));
    }
    return out;
}

std::string from_wide(const std::wstring& wide) {
    std::string out;
    out.reserve(wide.size());
    for (std::size_t i = 0; i < wide.size(); ++i) {
        out.push_back(static_cast<char>(static_cast<unsigned char>(wide[i] & 0xFF)));
    }
    return out;
}

}  // namespace platform
}  // namespace mie
