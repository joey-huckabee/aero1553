// SPDX-License-Identifier: Apache-2.0
//
// Win32 backend for the platform layer. Exactly one of this file and
// platform_posix.cpp is compiled into any given binary.
//
// This is one of the two files permitted to include OS headers. See
// scripts/assert-platform-confined.sh.
//
// Windows is a shipping target, not a development convenience, so this backend
// is held to behavioural parity with the POSIX one rather than to "it compiles
// and mostly works":
//
//   * Paths are widened to UTF-16 and the W entry points are called, so a
//     non-ASCII path behaves as it does under the Rust implementation instead
//     of being mangled by the active ANSI codepage.
//   * The commit uses MoveFileExW(MOVEFILE_REPLACE_EXISTING), because the CRT's
//     rename() FAILS when the destination exists. Rust's std::fs::rename uses
//     MoveFileEx internally, so a port written with rename() would be green on
//     Linux and silently wrong on the shipped Windows binary.
//   * Handles are closed before the rename: NTFS refuses to rename a file that
//     still has an open handle, where POSIX does not care.
//   * The input is opened with all three FILE_SHARE bits so decoding never
//     locks a recording another tool is reading.

#include "mie/platform.hpp"

#if !defined(_WIN32)
#error "platform_win32.cpp must not be compiled for non-Windows targets"
#endif

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>

#include <shellapi.h>
// <windows.h> must precede these.
#include <errno.h>
#include <fcntl.h>
#include <io.h>
#include <stdio.h>

#include "mie/text.hpp"

namespace mie {
namespace platform {

namespace {

/// NULL, not INVALID_HANDLE_VALUE, is this layer's "closed" sentinel.
///
/// The two differ: CreateFile reports failure as INVALID_HANDLE_VALUE ((HANDLE)-1)
/// while CreateFileMapping reports it as NULL. Normalising to NULL at every
/// failure site means the destructors have exactly one value to test, instead of
/// each one having to remember which API it came from -- which is a classic way
/// to close a handle of -1 or leak a real one.
HANDLE as_handle(void* p) { return static_cast<HANDLE>(p); }

void fill_last_error(OsError& err, DWORD captured) {
    err.code = static_cast<int>(captured);
    err.message.clear();

    LPWSTR buffer = 0;
    const DWORD flags =
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS;
    const DWORD length =
        ::FormatMessageW(flags, 0, captured, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
                         reinterpret_cast<LPWSTR>(&buffer), 0, 0);
    if (length != 0 && buffer != 0) {
        std::wstring wide(buffer, length);
        // FormatMessage terminates its text with CRLF. Leaving it in would put
        // a line break in the middle of a diagnostic that the logger then
        // prefixes -- one message across two lines, only on Windows.
        while (!wide.empty() &&
               (wide[wide.size() - 1] == L'\r' || wide[wide.size() - 1] == L'\n')) {
            wide.erase(wide.size() - 1);
        }
        err.message = from_wide(wide);
    }
    if (buffer != 0) {
        ::LocalFree(buffer);
    }
    if (err.message.empty()) {
        err.message = "Windows error";
    }
}

void fill_last_error(OsError& err) { fill_last_error(err, ::GetLastError()); }

/// Strip the \\?\ extended-length prefix GetFinalPathNameByHandleW returns.
///
/// Both sides of a comparison go through this function, so keeping the prefix
/// would still compare correctly -- it is removed because the canonical path
/// also reaches operators in error messages, and "\\?\C:\logs\out.csv" reads
/// like a corrupted path to someone who has not met that prefix before.
std::wstring strip_extended_prefix(const std::wstring& path) {
    static const wchar_t kUnc[] = L"\\\\?\\UNC\\";
    static const wchar_t kDos[] = L"\\\\?\\";
    if (path.compare(0, 8, kUnc) == 0) {
        return std::wstring(L"\\\\") + path.substr(8);
    }
    if (path.compare(0, 4, kDos) == 0) {
        return path.substr(4);
    }
    return path;
}

/// Full paths shorter than this are passed to Win32 exactly as given.
///
/// MAX_PATH (260) is the classic limit, but CreateDirectoryW reserves twelve
/// characters for an 8.3 file name inside the new directory, so 248 is where a
/// legacy path can first fail. Rust's std uses the same threshold.
const std::size_t kLegacyPathLimit = 248;

/// Widen a UTF-8 path for a Win32 FILE API, extended-length when it has to be.
///
/// Without a `\\?\` prefix the W entry points still refuse a full path longer
/// than MAX_PATH unless the process manifest opts in AND the machine enables
/// LongPathsEnabled, so a long path failed as "file not found" here while the
/// Rust build, whose std adds the prefix, read it fine.
///
/// The prefix is added only when needed, never by default. `\\?\` turns off
/// Win32 path parsing: `/` stops being a separator, `.` and `..` stop being
/// resolved, and device names such as `NUL` stop being devices -- so
/// `-o NUL` would become a file. The full path is therefore resolved first
/// (GetFullPathNameW does that parsing, and makes a short relative path that
/// is long once joined to the working directory count as long), and only a
/// result at or over the legacy limit is prefixed. A path short both as given
/// and resolved passes through exactly as given, so every path that worked
/// before this function existed reaches Win32 unchanged. Paths already in the
/// `\\?\` or `\\.\` namespaces pass through untouched.
std::wstring to_wide_path(const std::string& utf8_path) {
    const std::wstring wide = to_wide(utf8_path);
    if (wide.compare(0, 4, L"\\\\?\\") == 0 || wide.compare(0, 4, L"\\\\.\\") == 0) {
        return wide;
    }
    const DWORD needed = ::GetFullPathNameW(wide.c_str(), 0, 0, 0);
    if (needed == 0) {
        return wide;  // let the real call report why the path is unusable
    }
    std::wstring full(static_cast<std::size_t>(needed), L'\0');
    const DWORD written = ::GetFullPathNameW(wide.c_str(), needed, &full[0], 0);
    if (written == 0 || written >= needed) {
        return wide;
    }
    full.resize(static_cast<std::size_t>(written));
    if (full.size() < kLegacyPathLimit) {
        // Short once resolved. A path that is short as given goes through
        // exactly as given; one that is only long as given (a run of `.\`
        // segments, say) goes through resolved, which names the same file and
        // fits the legacy limit.
        return wide.size() < kLegacyPathLimit ? wide : full;
    }
    if (full.compare(0, 2, L"\\\\") == 0) {
        return std::wstring(L"\\\\?\\UNC\\") + full.substr(2);
    }
    return std::wstring(L"\\\\?\\") + full;
}

}  // namespace

// ---------------------------------------------------------------------------
// MappedFile
// ---------------------------------------------------------------------------

MappedFile::MappedFile() : file_handle_(0), mapping_handle_(0), data_(0), size_(0) {}

MappedFile::~MappedFile() { close(); }

MappedFile::MappedFile(MappedFile&& other) noexcept
    : file_handle_(other.file_handle_),
      mapping_handle_(other.mapping_handle_),
      data_(other.data_),
      size_(other.size_) {
    other.file_handle_ = 0;
    other.mapping_handle_ = 0;
    other.data_ = 0;
    other.size_ = 0;
}

MappedFile& MappedFile::operator=(MappedFile&& other) noexcept {
    if (this != &other) {
        close();
        file_handle_ = other.file_handle_;
        mapping_handle_ = other.mapping_handle_;
        data_ = other.data_;
        size_ = other.size_;
        other.file_handle_ = 0;
        other.mapping_handle_ = 0;
        other.data_ = 0;
        other.size_ = 0;
    }
    return *this;
}

bool MappedFile::open(const std::string& utf8_path, OsError& err) {
    close();
    err.clear();

    const std::wstring wide = to_wide_path(utf8_path);

    // Open BEFORE testing the size, deliberately, and matching the POSIX
    // backend: a directory reports a zero size here, so a size-first check
    // would report "empty" for a path that is really the wrong kind of thing.
    const HANDLE file = ::CreateFileW(wide.c_str(), GENERIC_READ,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, 0,
                                      OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, 0);
    if (file == INVALID_HANDLE_VALUE) {
        fill_last_error(err);
        return false;
    }

    BY_HANDLE_FILE_INFORMATION info;
    if (::GetFileInformationByHandle(file, &info) == 0) {
        fill_last_error(err);
        ::CloseHandle(file);
        return false;
    }
    if ((info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) {
        err.code = ERROR_DIRECTORY_NOT_SUPPORTED;
        err.message = "not a regular file";
        ::CloseHandle(file);
        return false;
    }

    LARGE_INTEGER file_size;
    if (::GetFileSizeEx(file, &file_size) == 0) {
        fill_last_error(err);
        ::CloseHandle(file);
        return false;
    }
    if (file_size.QuadPart == 0) {
        // Callers reject empty inputs as FileEmpty before reaching here
        // (L2-RDR-006). Guarded anyway because CreateFileMapping refuses a
        // zero-length file outright, which would surface as a confusing
        // mapping failure rather than the specific error the operator needs.
        err.code = ERROR_HANDLE_EOF;
        err.message = "file is empty";
        ::CloseHandle(file);
        return false;
    }

    const HANDLE mapping = ::CreateFileMappingW(file, 0, PAGE_READONLY, 0, 0, 0);
    if (mapping == 0) {
        fill_last_error(err);
        ::CloseHandle(file);
        return false;
    }

    LPVOID view = ::MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
    if (view == 0) {
        fill_last_error(err);
        ::CloseHandle(mapping);
        ::CloseHandle(file);
        return false;
    }

    file_handle_ = file;
    mapping_handle_ = mapping;
    data_ = static_cast<const uint8_t*>(view);
    size_ = static_cast<uint64_t>(file_size.QuadPart);
    return true;
}

void MappedFile::close() {
    if (data_ != 0) {
        ::UnmapViewOfFile(static_cast<LPCVOID>(data_));
        data_ = 0;
        size_ = 0;
    }
    if (mapping_handle_ != 0) {
        ::CloseHandle(as_handle(mapping_handle_));
        mapping_handle_ = 0;
    }
    if (file_handle_ != 0) {
        ::CloseHandle(as_handle(file_handle_));
        file_handle_ = 0;
    }
}

// ---------------------------------------------------------------------------
// AtomicFile
// ---------------------------------------------------------------------------

AtomicFile::AtomicFile()
    : handle_(0), temp_path_(), final_path_(), buffer_(), committed_(false), mode_(COMMIT_REPLACE) {
    buffer_.reserve(kWriteBufferSize);
}

AtomicFile::~AtomicFile() { abort(); }

bool AtomicFile::create(const std::string& final_utf8_path, OsError& err) {
    abort();
    err.clear();
    committed_ = false;
    final_path_ = final_utf8_path;

    for (int attempt = 0; attempt < 8; ++attempt) {
        const std::string candidate = make_temp_name(final_utf8_path);
        const std::wstring wide = to_wide_path(candidate);
        // FILE_SHARE_READ | FILE_SHARE_DELETE, not 0.
        //
        // Share mode 0 denies every other opener, which makes the in-progress
        // temp file unopenable even for reading. That is a behavioural
        // divergence from both of the other implementations -- POSIX places no
        // such lock, and Rust's File::create_new shares read, write and delete
        // -- and it is not merely cosmetic: an antivirus or backup agent that
        // touches the file mid-write turns into a spurious decode failure.
        //
        // FILE_SHARE_WRITE is deliberately withheld. Readers are harmless;
        // a second writer into the temp file would corrupt the CSV being
        // streamed into it. FILE_SHARE_DELETE is granted so cleanup can unlink
        // the file even while a scanner holds it open.
        const HANDLE file =
            ::CreateFileW(wide.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_DELETE, 0,
                          CREATE_NEW, FILE_ATTRIBUTE_NORMAL, 0);
        if (file != INVALID_HANDLE_VALUE) {
            handle_ = file;
            temp_path_ = candidate;
            buffer_.clear();
            return true;
        }
        const DWORD captured = ::GetLastError();
        if (captured != ERROR_FILE_EXISTS && captured != ERROR_ALREADY_EXISTS) {
            fill_last_error(err, captured);
            return false;
        }
    }
    err.code = ERROR_FILE_EXISTS;
    err.message = "could not create a unique temporary file beside the destination";
    return false;
}

bool AtomicFile::raw_write(const char* bytes, std::size_t len, OsError& err) {
    if (handle_ == 0) {
        err.code = ERROR_INVALID_HANDLE;
        err.message = "temporary file is not open";
        return false;
    }
    std::size_t written = 0;
    while (written < len) {
        const std::size_t remaining = len - written;
        // WriteFile counts in DWORD. A single call therefore cannot exceed 4 GiB,
        // and the loop below already handles a short write, so clamping here is
        // free rather than a special case.
        const DWORD chunk = remaining > 0x40000000u ? 0x40000000u : static_cast<DWORD>(remaining);
        DWORD produced = 0;
        if (::WriteFile(as_handle(handle_), bytes + written, chunk, &produced, 0) == 0) {
            fill_last_error(err);
            return false;
        }
        if (produced == 0) {
            err.code = ERROR_WRITE_FAULT;
            err.message = "write made no progress";
            return false;
        }
        written += produced;
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
        return flush(err) && raw_write(bytes, len, err);
    }
    if (buffer_.size() + len > kWriteBufferSize && !flush(err)) {
        return false;
    }
    buffer_.insert(buffer_.end(), bytes, bytes + len);
    return true;
}

CommitStatus AtomicFile::commit(OsError& err) { return commit_with_suffix(std::string(), err); }

void AtomicFile::set_commit_mode(CommitMode mode) { mode_ = mode; }

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
    if (handle_ == 0) {
        err.code = ERROR_INVALID_HANDLE;
        err.message = "temporary file is not open";
        return false;
    }

    // Close before renaming: NTFS refuses to rename a file with an open handle.
    // POSIX has no such rule, which is why this ordering has to be deliberate
    // rather than incidental -- code that renames first is correct on Linux and
    // fails on every Windows run.
    if (::CloseHandle(as_handle(handle_)) == 0) {
        fill_last_error(err);
        handle_ = 0;
        return false;
    }
    handle_ = 0;
    return true;
}

CommitStatus AtomicFile::place(const std::string& destination, OsError& err) {
    const std::wstring wide_temp = to_wide_path(temp_path_);
    const std::wstring wide_dest = to_wide_path(destination);

    // MOVEFILE_REPLACE_EXISTING is the whole reason this is not std::rename.
    // Withholding it is equally deliberate: MoveFileExW with no flags is an
    // ATOMIC no-replace move -- it fails with ERROR_ALREADY_EXISTS rather than
    // destroying the destination -- which is precisely what L2-WRT-023 asks for,
    // in one call, on every filesystem including FAT. The POSIX backend needs
    // link(2) and a reservation fallback to reach the same guarantee; here the
    // OS offers it directly, so this backend takes it.
    const DWORD flags = (mode_ == COMMIT_REPLACE) ? MOVEFILE_REPLACE_EXISTING : 0;
    if (::MoveFileExW(wide_temp.c_str(), wide_dest.c_str(), flags) == 0) {
        const DWORD captured = ::GetLastError();
        if (mode_ == COMMIT_NO_REPLACE &&
            (captured == ERROR_ALREADY_EXISTS || captured == ERROR_FILE_EXISTS)) {
            return COMMIT_EXISTS;
        }
        fill_last_error(err, captured);
        return COMMIT_ERROR;
    }
    mark_committed();
    return COMMIT_DONE;
}

void AtomicFile::abort() {
    if (handle_ != 0) {
        ::CloseHandle(as_handle(handle_));
        handle_ = 0;
    }
    if (!committed_ && !temp_path_.empty()) {
        ::DeleteFileW(to_wide_path(temp_path_).c_str());
        temp_path_.clear();
    }
    buffer_.clear();
}

// ---------------------------------------------------------------------------
// Directory enumeration, metadata, identity
// ---------------------------------------------------------------------------

std::vector<std::string> command_line_arguments(int argc, char** argv) {
    // `argv` is DELIBERATELY IGNORED. The CRT built it from the command line in
    // the ANSI codepage, so a non-representable character is already `?` by the
    // time this runs. GetCommandLineW returns what the OS actually received.
    (void)argc;
    (void)argv;

    std::vector<std::string> out;
    int wide_count = 0;
    LPWSTR* wide = ::CommandLineToArgvW(::GetCommandLineW(), &wide_count);
    if (wide == NULL) {
        // Nothing sensible to fall back to but the mangled arguments, which is
        // still better than exiting with no diagnostic at all.
        if (argc > 1) {
            for (int i = 1; i < argc; ++i) {
                out.push_back(std::string(argv[i]));
            }
        }
        return out;
    }
    for (int i = 1; i < wide_count; ++i) {
        out.push_back(from_wide(std::wstring(wide[i])));
    }
    ::LocalFree(wide);
    return out;
}

std::FILE* open_read(const std::string& utf8_path, OsError& err) {
    err.clear();
    // _wfopen, not fopen. The CRT reads a narrow path in the ANSI codepage, so
    // a UTF-8 path with any non-ASCII byte names a different file -- usually
    // one that does not exist. Widening here is the same boundary every other
    // path operation in this backend crosses.
    const std::wstring wide = to_wide_path(utf8_path);
    std::FILE* handle = ::_wfopen(wide.c_str(), L"rb");
    if (handle == NULL) {
        // capture_stream_error, not fill_last_error: _wfopen is a CRT call and
        // reports through errno, while OsError::code carries a GetLastError
        // value on this platform. That translation is exactly what
        // capture_stream_error owns.
        capture_stream_error(err);
        return NULL;
    }
    return handle;
}

bool list_directory(const std::string& utf8_dir, std::vector<std::string>& names, OsError& err) {
    err.clear();
    names.clear();

    const std::wstring pattern = to_wide_path(path_join(utf8_dir, std::string("*")));

    WIN32_FIND_DATAW entry;
    const HANDLE search = ::FindFirstFileW(pattern.c_str(), &entry);
    if (search == INVALID_HANDLE_VALUE) {
        const DWORD captured = ::GetLastError();
        if (captured == ERROR_FILE_NOT_FOUND) {
            // An empty directory is not an error; it is a directory with no
            // matches. Reporting it as failure would turn a --glob that matched
            // nothing into a crash instead of an empty result.
            return true;
        }
        fill_last_error(err, captured);
        return false;
    }

    do {
        const std::wstring name(entry.cFileName);
        if (name == L"." || name == L"..") {
            continue;
        }
        names.push_back(from_wide(name));
    } while (::FindNextFileW(search, &entry) != 0);

    const DWORD captured = ::GetLastError();
    ::FindClose(search);

    if (captured != ERROR_NO_MORE_FILES) {
        fill_last_error(err, captured);
        names.clear();
        return false;
    }
    return true;
}

void prepare_output_streams() {
    // Without this the CRT rewrites every newline into CRLF on the way out,
    // which breaks every stdout conformance oracle on Windows alone while the
    // file-destination oracles keep passing -- a split that is very hard to
    // read as "one missing call".
    ::_setmode(::_fileno(stdout), _O_BINARY);
    // stderr too. Rust and Python write its lines with LF on every platform,
    // so a diagnostic redirected to a file differed from theirs in every line
    // ending (L2-CLI-022) -- and a log that interleaves this tool's lines with
    // another's got mixed line endings.
    ::_setmode(::_fileno(stderr), _O_BINARY);
}

bool canonical_path(const std::string& utf8_path, std::string& out, OsError& err) {
    err.clear();
    out.clear();

    const std::wstring wide = to_wide_path(utf8_path);
    // FILE_FLAG_BACKUP_SEMANTICS is required to open a DIRECTORY handle, and
    // this function is called on directories -- paths_same_file resolves the
    // output's parent when the output itself does not exist yet.
    const HANDLE handle = ::CreateFileW(wide.c_str(), FILE_READ_ATTRIBUTES,
                                        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, 0,
                                        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, 0);
    if (handle == INVALID_HANDLE_VALUE) {
        fill_last_error(err);
        return false;
    }

    DWORD needed = ::GetFinalPathNameByHandleW(handle, 0, 0, VOLUME_NAME_DOS);
    if (needed == 0) {
        fill_last_error(err);
        ::CloseHandle(handle);
        return false;
    }

    std::vector<wchar_t> buffer(needed + 1, L'\0');
    const DWORD written = ::GetFinalPathNameByHandleW(handle, &buffer[0], needed, VOLUME_NAME_DOS);
    ::CloseHandle(handle);

    if (written == 0 || written >= needed + 1) {
        fill_last_error(err);
        return false;
    }

    out = from_wide(strip_extended_prefix(std::wstring(&buffer[0], written)));
    return true;
}

bool file_metadata(const std::string& utf8_path, uint64_t& size, bool& is_regular, OsError& err) {
    err.clear();
    size = 0;
    is_regular = false;

    // Through an opened handle, not GetFileAttributesExW. That call describes a
    // symlink ITSELF -- zero bytes, and present even when its target is not --
    // where POSIX stat() and Rust's fs::metadata describe what it points to. So
    // a recording reached through a link was refused as an empty recording
    // (L2-RDR-006), and a dangling link "existed": decoding one reported an I/O
    // failure instead of a missing file (L2-RDR-005), and --glob matched it
    // where the other two skip it (L2-MRG-001). Opening without
    // FILE_FLAG_OPEN_REPARSE_POINT follows the link, as Rust's std does.
    //
    // FILE_READ_ATTRIBUTES alone is exempt from sharing checks, so this still
    // answers for a file another process holds open exclusively -- a recording
    // still being written, say. FILE_FLAG_BACKUP_SEMANTICS admits directories.
    const std::wstring wide = to_wide_path(utf8_path);
    const HANDLE handle = ::CreateFileW(wide.c_str(), FILE_READ_ATTRIBUTES,
                                        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, 0,
                                        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, 0);
    if (handle == INVALID_HANDLE_VALUE) {
        fill_last_error(err);
        return false;
    }

    BY_HANDLE_FILE_INFORMATION info;
    const bool got = ::GetFileInformationByHandle(handle, &info) != 0;
    // A disk file, not a device such as NUL or CON that also opens by name.
    const bool on_disk = ::GetFileType(handle) == FILE_TYPE_DISK;
    if (!got) {
        fill_last_error(err);
    }
    ::CloseHandle(handle);
    if (!got) {
        return false;
    }

    ULARGE_INTEGER combined;
    combined.HighPart = info.nFileSizeHigh;
    combined.LowPart = info.nFileSizeLow;
    size = static_cast<uint64_t>(combined.QuadPart);
    is_regular = on_disk && (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) == 0;
    return true;
}

bool remove_file(const std::string& utf8_path, OsError& err) {
    err.clear();
    if (::DeleteFileW(to_wide_path(utf8_path).c_str()) == 0) {
        fill_last_error(err);
        return false;
    }
    return true;
}

void capture_stream_error(OsError& err) {
    // Translated INTO the Win32 space, not passed through. OsError::code means
    // GetLastError() on this platform, and MieError classifies broken pipes
    // against Win32 codes; handing it a CRT errno would put a number from one
    // space into a field read as the other -- and 32 is EPIPE in one and
    // ERROR_SHARING_VIOLATION in the other, so the collision is real.
    const int captured = errno;
    if (captured == EPIPE || captured == EINVAL) {
        // A closed downstream pipe. The CRT reports it as EPIPE through some
        // paths and EINVAL through others, and neither reaches GetLastError
        // reliably once the CRT has handled the failure.
        fill_last_error(err, ERROR_BROKEN_PIPE);
        return;
    }
    const DWORD last = ::GetLastError();
    if (last != ERROR_SUCCESS) {
        fill_last_error(err, last);
        return;
    }
    // No Win32 error to report: describe the CRT failure rather than claiming
    // success on a call that demonstrably failed.
    err.code = captured;
    err.message = "stream write failed";
}

uint64_t process_id() { return static_cast<uint64_t>(::GetCurrentProcessId()); }

// ---------------------------------------------------------------------------
// UTF-8 <-> UTF-16
// ---------------------------------------------------------------------------
//
// Real conversions here, unlike the POSIX backend's byte pass-through. This is
// what makes a non-ASCII path work rather than being mangled by the active ANSI
// codepage, and it is why Windows can be a shipping target rather than a
// development convenience.

// WTF-8 rather than UTF-8 (text::utf16_to_wtf8). A Windows file name may hold
// an unpaired surrogate; MultiByteToWideChar / WideCharToMultiByte, which these
// used to call, replace it with U+FFFD, so such a name named a different file
// -- usually one that does not exist -- where Rust opened it (L2-CLI-021).

std::wstring to_wide(const std::string& utf8) {
    const std::u16string units = text::wtf8_to_utf16(utf8);
    return std::wstring(units.begin(), units.end());
}

std::string from_wide(const std::wstring& wide) {
    return text::utf16_to_wtf8(std::u16string(wide.begin(), wide.end()));
}

}  // namespace platform
}  // namespace mie
