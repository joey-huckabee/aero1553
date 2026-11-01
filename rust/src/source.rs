//! How the crate reads an input recording: one place for both the reader and
//! `dump`.
//!
//! [`open_input`] opens the file and applies the checks every consumer shares
//! (missing, empty, unreadable). [`Window`] then reads it through a sliding
//! buffer filled by positional reads (`pread` on POSIX, `ReadFile` at an offset
//! on Windows), so memory is the window's size whatever the file's.
//!
//! Every sync and decode helper takes `&[u8]` plus offsets into it. A consumer
//! asks the window to [`Window::ensure`] that it reaches as far as the next
//! helper can look -- or to end of file -- and then hands over the window's
//! bytes and the offset relative to [`Window::base`], so a helper cannot tell
//! a window from the whole file.
//!
//! Offsets held by consumers stay file-absolute. Only the window knows which
//! part of the file it holds.
//!
//! A window can also read a forward-only stream -- standard input, for
//! `decode -` (L2-CLI-023). Every consumer already walks forwards, so the same
//! window serves it: a jump ahead is read and discarded, and the length is
//! unknown until the stream ends. A request behind the window cannot be met
//! from a stream and is an error, never a silent wrong answer.

use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use std::sync::Arc;

use crate::error::{MieError, MieResult};

/// Bytes read per refill. The I/O-backend benchmark that replaced the memory
/// map found 64 KiB, 256 KiB and 1 MiB within noise of each other and of the
/// map, on Linux and Windows, warm and cold; 256 KiB keeps a 256-file merge
/// under 100 MB.
pub(crate) const DEFAULT_CHUNK: usize = 256 * 1024;

/// Bytes kept before the requested offset on a refill, so a DEBUG dump of the
/// bytes around a failed record can still show what preceded it.
const KEEP_BEHIND: usize = 16;

/// Open an input recording, rejecting what no consumer can read.
///
/// # Errors
///
/// [`MieError::FileNotFound`] if the path does not exist,
/// [`MieError::FileEmpty`] for a zero-byte file, and [`MieError::FileIo`] for
/// anything else the OS reported -- a directory included, which some systems
/// let `open` succeed on and only refuse at the first read.
pub(crate) fn open_input(path: &Path) -> MieResult<(File, u64)> {
    let io_err = |source| MieError::FileIo {
        path: path.to_path_buf(),
        source,
    };
    if !path.exists() {
        return Err(MieError::FileNotFound {
            path: path.to_path_buf(),
        });
    }
    let file = File::open(path).map_err(io_err)?;
    let metadata = file.metadata().map_err(io_err)?;
    if metadata.is_dir() {
        let source = read_at(&file, &mut [0u8; 1], 0)
            .err()
            .unwrap_or_else(|| io::ErrorKind::IsADirectory.into());
        return Err(io_err(source));
    }
    let len = metadata.len();
    if len == 0 {
        return Err(MieError::FileEmpty {
            path: path.to_path_buf(),
        });
    }
    Ok((file, len))
}

/// What a window reads from.
enum Source {
    /// A file, read at offsets; the handle is shared between windows.
    File(Arc<File>),
    /// A forward-only stream, positioned at `base + end` of its window.
    Stream(Box<dyn Read + Send + Sync>),
}

/// One consumer's view of an input: `buf[..end]` holds the file's bytes from
/// `base`. Each iterator owns its window; the file handle is shared.
pub(crate) struct Window {
    source: Source,
    buf: Vec<u8>,
    end: usize,
    base: usize,
    chunk: usize,
}

impl Window {
    pub(crate) fn new(file: Arc<File>, chunk: usize) -> Self {
        Self::with_source(Source::File(file), chunk)
    }

    /// A window over a forward-only stream. Pass `usize::MAX` as the length to
    /// [`Self::ensure`]: it is lowered to the real length when the stream ends.
    pub(crate) fn stream(stream: Box<dyn Read + Send + Sync>, chunk: usize) -> Self {
        Self::with_source(Source::Stream(stream), chunk)
    }

    fn with_source(source: Source, chunk: usize) -> Self {
        Self {
            source,
            buf: Vec::new(),
            end: 0,
            base: 0,
            chunk,
        }
    }

    /// The bytes the window holds, starting at file offset [`Self::base`].
    pub(crate) fn bytes(&self) -> &[u8] {
        &self.buf[..self.end]
    }

    /// The file offset of `bytes()[0]`.
    pub(crate) fn base(&self) -> usize {
        self.base
    }

    /// Make the window cover `[offset, offset + need)`, or `[offset, EOF)`
    /// when the file ends sooner. Consumers walk forwards; an `offset` before
    /// the window is served by a fresh read, not by keeping old bytes.
    ///
    /// `file_len` is the length the caller believes the file has. A file that
    /// turns out shorter (truncated while being read) ends where the reads
    /// end: `file_len` is lowered to match, so the caller sees a short file
    /// rather than an error -- the case a memory map turned into a crash.
    ///
    /// # Errors
    ///
    /// Whatever the positional read reports.
    #[inline]
    pub(crate) fn ensure(
        &mut self,
        offset: usize,
        need: usize,
        file_len: &mut usize,
    ) -> io::Result<()> {
        let win_end = self.base + self.end;
        if offset >= self.base
            && offset <= win_end
            && (win_end >= *file_len || win_end - offset >= need)
        {
            return Ok(());
        }
        self.refill(offset, need, file_len)
    }

    /// The rest of [`Self::ensure`]: the window does not reach far enough.
    /// Once per 256 KiB, so kept out of line; the test above runs per record.
    #[cold]
    #[inline(never)]
    fn refill(&mut self, offset: usize, need: usize, file_len: &mut usize) -> io::Result<()> {
        let win_end = self.base + self.end;
        if let Source::Stream(stream) = &mut self.source
            && (offset < self.base || offset > win_end)
        {
            if offset < self.base {
                return Err(io::Error::other(format!(
                    "internal error: a stream cannot be read backwards \
                     (offset {offset}, window starts at {})",
                    self.base
                )));
            }
            // Ahead of the stream: read up to `offset` and drop it.
            self.base = win_end;
            self.end = 0;
            if self.buf.len() < self.chunk {
                self.buf.resize(self.chunk, 0);
            }
            while self.base < offset {
                let step = (offset - self.base).min(self.buf.len());
                let n = read_retrying(|| stream.read(&mut self.buf[..step]))?;
                if n == 0 {
                    *file_len = self.base;
                    return Ok(());
                }
                self.base += n;
            }
        } else if offset >= win_end || offset < self.base {
            // Nothing held is wanted: start afresh at `offset`.
            self.base = offset;
            self.end = 0;
        } else {
            let keep_from = offset.saturating_sub(KEEP_BEHIND).max(self.base) - self.base;
            self.buf.copy_within(keep_from..self.end, 0);
            self.end -= keep_from;
            self.base += keep_from;
        }
        if self.base >= *file_len {
            return Ok(());
        }
        let target = self.chunk.max(need + (offset - self.base));
        if self.buf.len() < target {
            self.buf.resize(target, 0);
        }
        let want_end = target.min(*file_len - self.base);
        while self.end < want_end {
            let pos = (self.base + self.end) as u64;
            let dest = &mut self.buf[self.end..want_end];
            let n = match &mut self.source {
                Source::File(file) => read_retrying(|| read_at(file, dest, pos))?,
                Source::Stream(stream) => read_retrying(|| stream.read(dest))?,
            };
            if n == 0 {
                *file_len = self.base + self.end;
                break;
            }
            self.end += n;
        }
        Ok(())
    }
}

/// One read, repeated while a signal interrupts it before any byte arrives:
/// `Interrupted` says nothing about the input, so it must not end a decode.
fn read_retrying(mut read: impl FnMut() -> io::Result<usize>) -> io::Result<usize> {
    loop {
        match read() {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            other => return other,
        }
    }
}

#[cfg(unix)]
fn read_at(file: &File, buf: &mut [u8], pos: u64) -> io::Result<usize> {
    std::os::unix::fs::FileExt::read_at(file, buf, pos)
}

#[cfg(windows)]
fn read_at(file: &File, buf: &mut [u8], pos: u64) -> io::Result<usize> {
    std::os::windows::fs::FileExt::seek_read(file, buf, pos)
}

#[cfg(not(any(unix, windows)))]
fn read_at(file: &File, buf: &mut [u8], pos: u64) -> io::Result<usize> {
    use std::io::{Read, Seek, SeekFrom};
    let mut f = file;
    f.seek(SeekFrom::Start(pos))?;
    f.read(buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn temp(bytes: &[u8], tag: &str) -> (std::path::PathBuf, Arc<File>) {
        let p =
            std::env::temp_dir().join(format!("aero1553-source-{tag}-{}.bin", std::process::id()));
        File::create(&p).unwrap().write_all(bytes).unwrap();
        let f = Arc::new(File::open(&p).unwrap());
        (p, f)
    }

    fn pattern(n: usize) -> Vec<u8> {
        (0..n)
            .map(|i| u8::try_from((i * 7 + i / 251) % 256).unwrap())
            .collect()
    }

    #[test]
    fn walking_forward_in_small_steps_sees_the_file() {
        let data = pattern(10_000);
        let (p, f) = temp(&data, "walk");
        let mut w = Window::new(f, 64);
        let mut len = data.len();
        let mut off = 0;
        while off < len {
            w.ensure(off, 40, &mut len).unwrap();
            let rel = off - w.base();
            let got = &w.bytes()[rel..];
            let want = &data[off..data.len().min(off + 40)];
            assert_eq!(&got[..want.len()], want, "at {off}");
            off += 13;
        }
        assert_eq!(len, data.len());
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn a_jump_past_the_window_reads_at_the_target() {
        let data = pattern(5_000);
        let (p, f) = temp(&data, "jump");
        let mut w = Window::new(f, 32);
        let mut len = data.len();
        w.ensure(0, 16, &mut len).unwrap();
        w.ensure(4_000, 100, &mut len).unwrap();
        let rel = 4_000 - w.base();
        assert_eq!(&w.bytes()[rel..rel + 100], &data[4_000..4_100]);
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn a_request_past_eof_is_empty_not_an_error() {
        let data = pattern(100);
        let (p, f) = temp(&data, "eof");
        let mut w = Window::new(f, 32);
        let mut len = data.len();
        w.ensure(500, 10, &mut len).unwrap();
        assert_eq!(w.bytes(), &[] as &[u8]);
        assert_eq!(len, 100);
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn a_file_shorter_than_believed_lowers_the_length() {
        let data = pattern(100);
        let (p, f) = temp(&data, "short");
        let mut w = Window::new(f, 4096);
        let mut len = 1_000;
        w.ensure(0, 500, &mut len).unwrap();
        assert_eq!(len, 100);
        assert_eq!(w.bytes(), &data[..]);
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn a_refill_keeps_a_little_context_behind_the_offset() {
        let data = pattern(1_000);
        let (p, f) = temp(&data, "behind");
        let mut w = Window::new(f, 64);
        let mut len = data.len();
        w.ensure(0, 64, &mut len).unwrap();
        w.ensure(60, 64, &mut len).unwrap();
        assert_eq!(w.base(), 60 - KEEP_BEHIND);
        std::fs::remove_file(p).unwrap();
    }

    fn stream(data: &[u8]) -> Box<dyn Read + Send + Sync> {
        Box::new(io::Cursor::new(data.to_vec()))
    }

    /// Requirements: L2-CLI-023
    #[test]
    fn a_stream_walks_forward_and_learns_its_length_at_the_end() {
        let data = pattern(10_000);
        let mut w = Window::stream(stream(&data), 64);
        let mut len = usize::MAX;
        let mut off = 0;
        while off < len {
            w.ensure(off, 40, &mut len).unwrap();
            let rel = off - w.base();
            let got = &w.bytes()[rel..];
            let want = &data[off.min(data.len())..data.len().min(off + 40)];
            assert_eq!(&got[..want.len()], want, "at {off}");
            off += 13;
        }
        assert_eq!(len, data.len());
    }

    /// Requirements: L2-CLI-023
    #[test]
    fn a_stream_jump_reads_and_drops_the_bytes_in_between() {
        let data = pattern(5_000);
        let mut w = Window::stream(stream(&data), 32);
        let mut len = usize::MAX;
        w.ensure(0, 16, &mut len).unwrap();
        w.ensure(4_000, 100, &mut len).unwrap();
        let rel = 4_000 - w.base();
        assert_eq!(&w.bytes()[rel..rel + 100], &data[4_000..4_100]);
        // Past the end: empty, and the length is now known.
        w.ensure(9_000, 10, &mut len).unwrap();
        assert_eq!(len, data.len());
    }

    /// Requirements: L2-CLI-023
    #[test]
    fn a_stream_refuses_to_go_back_rather_than_answer_wrongly() {
        let data = pattern(1_000);
        let mut w = Window::stream(stream(&data), 64);
        let mut len = usize::MAX;
        w.ensure(0, 64, &mut len).unwrap();
        w.ensure(60, 64, &mut len).unwrap();
        // Within KEEP_BEHIND of the window is still held ...
        w.ensure(60 - KEEP_BEHIND, 8, &mut len).unwrap();
        assert_eq!(w.bytes()[0], data[60 - KEEP_BEHIND]);
        // ... but anything earlier cannot be read again.
        let err = w.ensure(10, 8, &mut len).unwrap_err();
        assert!(err.to_string().contains("backwards"), "{err}");
    }

    /// Requirements: L2-CLI-023
    #[test]
    fn an_interrupted_read_is_retried_not_reported() {
        let mut calls = 0;
        let n = read_retrying(|| {
            calls += 1;
            if calls < 3 {
                Err(io::ErrorKind::Interrupted.into())
            } else {
                Ok(5)
            }
        })
        .unwrap();
        assert_eq!((n, calls), (5, 3));
        let err = read_retrying(|| Err(io::ErrorKind::BrokenPipe.into())).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::BrokenPipe);
    }

    #[test]
    fn open_input_classifies_missing_empty_and_directory() {
        use crate::error::MieErrorKind;
        let dir = std::env::temp_dir().join(format!("aero1553-source-dir-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let empty = dir.join("empty.mie");
        File::create(&empty).unwrap();
        let kind = |p: &Path| open_input(p).err().map(|e| e.kind());
        assert_eq!(
            kind(&dir.join("missing.mie")),
            Some(MieErrorKind::FileNotFound)
        );
        assert_eq!(kind(&empty), Some(MieErrorKind::FileEmpty));
        assert_eq!(kind(&dir), Some(MieErrorKind::FileIo));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
