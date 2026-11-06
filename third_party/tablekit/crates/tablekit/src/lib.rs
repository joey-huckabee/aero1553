//! The table model every tablekit writer takes.
//!
//! A [`Batch`] is a set of named, equally long [`Column`]s. Each column holds one type of
//! value ([`Values`]) and says which rows have none ([`Column::with_missing`]). A tool builds
//! a batch from its own records; `tablekit-csv` and `tablekit-parquet` write it, so every
//! tool's files look the same.
//!
//! ```
//! use tablekit::{Batch, Column, DataType, Values};
//!
//! let batch = Batch::new(vec![
//!     Column::new("time", Values::F64(vec![0.5, 1.0])),
//!     Column::with_missing("Speed", Values::U16(vec![120, 0]), vec![true, false])?,
//!     Column::new("Valid", Values::Bool(vec![true, false])),
//! ])?;
//! assert_eq!(batch.rows(), 2);
//! assert_eq!(batch.schema()[1].data_type, DataType::U16);
//! assert!(!batch.columns()[1].is_present(1));
//! # Ok::<(), tablekit::TableError>(())
//! ```
//!
//! A tool that produces a table a batch at a time implements [`Source`]; [`copy`] drives any
//! source into any [`BatchWriter`] (`tablekit_csv::CsvWriter`, `tablekit_parquet::ParquetWriter`),
//! so a table of any size is written with a batch in memory at a time:
//!
//! ```
//! use tablekit::{Batch, BatchWriter, Column, Field, DataType, Source, TableError, Values, copy};
//!
//! /// Counts to `n`, `step` rows per batch.
//! struct Count { next: u64, n: u64, step: u64 }
//!
//! impl Source for Count {
//!     type Error = TableError;
//!     fn schema(&self) -> Vec<Field> {
//!         vec![Field { name: "n".into(), data_type: DataType::U64 }]
//!     }
//!     fn next_batch(&mut self) -> Result<Option<Batch>, TableError> {
//!         if self.next >= self.n {
//!             return Ok(None);
//!         }
//!         let end = (self.next + self.step).min(self.n);
//!         let batch = Batch::new(vec![Column::new("n", Values::U64((self.next..end).collect()))])?;
//!         self.next = end;
//!         Ok(Some(batch))
//!     }
//! }
//!
//! /// Sums what it is given.
//! struct Sum(u64);
//!
//! impl BatchWriter for Sum {
//!     fn write_batch(&mut self, batch: &Batch) -> Result<(), TableError> {
//!         if let Values::U64(v) = batch.columns()[0].values() {
//!             self.0 += v.iter().sum::<u64>();
//!         }
//!         Ok(())
//!     }
//! }
//!
//! let mut sum = Sum(0);
//! let rows = copy(&mut Count { next: 0, n: 10, step: 4 }, &mut sum)?;
//! assert_eq!((rows, sum.0), (10, 45));
//! # Ok::<(), TableError>(())
//! ```
//!
//! Files are written whole or not at all: [`write_atomically`] writes beside the target and
//! renames into place only when the writer succeeds.

#![forbid(unsafe_code)]
#![cfg_attr(
    not(test),
    deny(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::todo,
        clippy::unimplemented,
        clippy::unreachable
    )
)]

use std::fmt;
use std::fs::File;
use std::io::{self, BufWriter};
use std::path::{Path, PathBuf};

/// What one column holds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum DataType {
    /// `true` or `false`.
    Bool,
    /// Signed 8-bit integer.
    I8,
    /// Signed 16-bit integer.
    I16,
    /// Signed 32-bit integer.
    I32,
    /// Signed 64-bit integer.
    I64,
    /// Unsigned 8-bit integer.
    U8,
    /// Unsigned 16-bit integer.
    U16,
    /// Unsigned 32-bit integer.
    U32,
    /// Unsigned 64-bit integer.
    U64,
    /// 32-bit floating point.
    F32,
    /// 64-bit floating point.
    F64,
    /// UTF-8 text.
    Text,
}

/// One column's values, one per row. A missing row holds a placeholder (any value) that no
/// writer reads.
#[derive(Clone, Debug, PartialEq)]
pub enum Values {
    /// See [`DataType::Bool`].
    Bool(Vec<bool>),
    /// See [`DataType::I8`].
    I8(Vec<i8>),
    /// See [`DataType::I16`].
    I16(Vec<i16>),
    /// See [`DataType::I32`].
    I32(Vec<i32>),
    /// See [`DataType::I64`].
    I64(Vec<i64>),
    /// See [`DataType::U8`].
    U8(Vec<u8>),
    /// See [`DataType::U16`].
    U16(Vec<u16>),
    /// See [`DataType::U32`].
    U32(Vec<u32>),
    /// See [`DataType::U64`].
    U64(Vec<u64>),
    /// See [`DataType::F32`].
    F32(Vec<f32>),
    /// See [`DataType::F64`].
    F64(Vec<f64>),
    /// See [`DataType::Text`].
    Text(Text),
}

/// A column of text: every row's text in one buffer, with where each row ends. Adding a row
/// allocates nothing once the buffer has grown to its size, so a source can fill a column of
/// millions of rows, or the same column batch after batch, without a string per row.
///
/// ```
/// use tablekit::Text;
///
/// let mut t = Text::new();
/// t.push("On");
/// t.push_hex(&[0xab, 0x01]);
/// assert_eq!(t.get(1), Some("ab01"));
/// assert_eq!(t.iter().collect::<Vec<_>>(), ["On", "ab01"]);
/// let t: Text = ["a", "b"].into_iter().collect();
/// assert_eq!(t.len(), 2);
/// ```
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Text {
    buffer: String,
    /// Where each row ends in `buffer`; row `i` starts where row `i - 1` ends.
    ends: Vec<usize>,
}

/// Lowercase hex digits.
const HEX_DIGITS: &[u8; 16] = b"0123456789abcdef";

impl Text {
    /// An empty column.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// An empty column with room for `rows` rows of `bytes` bytes in all.
    #[must_use]
    pub fn with_capacity(rows: usize, bytes: usize) -> Self {
        Text {
            buffer: String::with_capacity(bytes),
            ends: Vec::with_capacity(rows),
        }
    }

    /// A column from text already in one buffer and where each row ends in it, without
    /// copying: what another one-buffer text column hands over (`cmm-decode`'s
    /// `TextValues::into_parts`). `None` unless every end is at or after the one before, within
    /// `buffer`, and on a character boundary.
    ///
    /// ```
    /// use tablekit::Text;
    ///
    /// let t = Text::from_parts("OnOff".to_owned(), vec![2, 2, 5]).unwrap();
    /// assert_eq!(t.iter().collect::<Vec<_>>(), ["On", "", "Off"]);
    /// assert_eq!(Text::from_parts("été".to_owned(), vec![1]), None);
    /// assert_eq!(Text::from_parts("ab".to_owned(), vec![2, 1]), None);
    /// ```
    #[must_use]
    pub fn from_parts(buffer: String, ends: Vec<usize>) -> Option<Self> {
        let mut start = 0;
        for &end in &ends {
            if end < start || !buffer.is_char_boundary(end) {
                return None;
            }
            start = end;
        }
        Some(Text { buffer, ends })
    }

    /// Adds a row.
    // HOT PATH: per value.
    #[inline]
    pub fn push(&mut self, s: &str) {
        self.buffer.push_str(s);
        self.ends.push(self.buffer.len());
    }

    /// Adds a row of `bytes` in lowercase hex, two digits a byte.
    // HOT PATH: per value.
    #[inline]
    pub fn push_hex(&mut self, bytes: &[u8]) {
        self.buffer.reserve(bytes.len() * 2);
        for &b in bytes {
            self.buffer
                .push(char::from(HEX_DIGITS[usize::from(b >> 4)]));
            self.buffer
                .push(char::from(HEX_DIGITS[usize::from(b & 15)]));
        }
        self.ends.push(self.buffer.len());
    }

    /// The number of rows.
    #[must_use]
    pub fn len(&self) -> usize {
        self.ends.len()
    }

    /// Whether there are no rows.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.ends.is_empty()
    }

    /// Row `row`'s text.
    // HOT PATH: per value.
    #[must_use]
    #[inline]
    pub fn get(&self, row: usize) -> Option<&str> {
        let end = *self.ends.get(row)?;
        let start = if row == 0 { 0 } else { self.ends[row - 1] };
        self.buffer.get(start..end)
    }

    /// Every row's text, in order.
    pub fn iter(&self) -> impl Iterator<Item = &str> {
        (0..self.len()).filter_map(|r| self.get(r))
    }

    /// Removes every row, keeping the room they took.
    pub fn clear(&mut self) {
        self.buffer.clear();
        self.ends.clear();
    }
}

impl<S: AsRef<str>> FromIterator<S> for Text {
    fn from_iter<I: IntoIterator<Item = S>>(iter: I) -> Self {
        let mut t = Text::new();
        for s in iter {
            t.push(s.as_ref());
        }
        t
    }
}

impl From<Vec<String>> for Text {
    fn from(v: Vec<String>) -> Self {
        v.into_iter().collect()
    }
}

impl From<Vec<&str>> for Text {
    fn from(v: Vec<&str>) -> Self {
        v.into_iter().collect()
    }
}

impl Values {
    /// The number of rows.
    #[must_use]
    pub fn len(&self) -> usize {
        match self {
            Values::Bool(v) => v.len(),
            Values::I8(v) => v.len(),
            Values::I16(v) => v.len(),
            Values::I32(v) => v.len(),
            Values::I64(v) => v.len(),
            Values::U8(v) => v.len(),
            Values::U16(v) => v.len(),
            Values::U32(v) => v.len(),
            Values::U64(v) => v.len(),
            Values::F32(v) => v.len(),
            Values::F64(v) => v.len(),
            Values::Text(v) => v.len(),
        }
    }

    /// Whether there are no rows.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The type of the values.
    #[must_use]
    pub fn data_type(&self) -> DataType {
        match self {
            Values::Bool(_) => DataType::Bool,
            Values::I8(_) => DataType::I8,
            Values::I16(_) => DataType::I16,
            Values::I32(_) => DataType::I32,
            Values::I64(_) => DataType::I64,
            Values::U8(_) => DataType::U8,
            Values::U16(_) => DataType::U16,
            Values::U32(_) => DataType::U32,
            Values::U64(_) => DataType::U64,
            Values::F32(_) => DataType::F32,
            Values::F64(_) => DataType::F64,
            Values::Text(_) => DataType::Text,
        }
    }
}

/// A named column of values, some of which may be missing.
#[derive(Clone, Debug, PartialEq)]
pub struct Column {
    name: String,
    values: Values,
    /// `None`: every row is present. Otherwise one entry per row, `false` where missing.
    present: Option<Vec<bool>>,
}

impl Column {
    /// A column with every row present.
    pub fn new(name: impl Into<String>, values: Values) -> Self {
        Column {
            name: name.into(),
            values,
            present: None,
        }
    }

    /// A column where `present[i]` says whether row `i` has a value.
    ///
    /// # Errors
    ///
    /// [`TableError::PresentLength`] if `present` and `values` differ in length.
    pub fn with_missing(
        name: impl Into<String>,
        values: Values,
        present: Vec<bool>,
    ) -> Result<Self, TableError> {
        let name = name.into();
        if present.len() != values.len() {
            return Err(TableError::PresentLength {
                column: name,
                values: values.len(),
                present: present.len(),
            });
        }
        let present = (!present.iter().all(|&p| p)).then_some(present);
        Ok(Column {
            name,
            values,
            present,
        })
    }

    /// The column's name.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The column's values; a missing row's is a placeholder.
    #[must_use]
    pub fn values(&self) -> &Values {
        &self.values
    }

    /// Whether row `row` has a value.
    // HOT PATH: per value.
    #[must_use]
    #[inline]
    pub fn is_present(&self, row: usize) -> bool {
        self.present
            .as_ref()
            .is_none_or(|p| p.get(row).copied().unwrap_or(false))
    }

    /// Whether any row is missing.
    #[must_use]
    pub fn has_missing(&self) -> bool {
        self.present.is_some()
    }

    /// Row `row`'s value: [`Value::Missing`] where it has none, or past the last row.
    // HOT PATH: per value.
    #[must_use]
    #[inline]
    pub fn value(&self, row: usize) -> Value<'_> {
        if !self.is_present(row) {
            return Value::Missing;
        }
        let v = match &self.values {
            Values::Bool(v) => v.get(row).map(|&x| Value::Bool(x)),
            Values::I8(v) => v.get(row).map(|&x| Value::I8(x)),
            Values::I16(v) => v.get(row).map(|&x| Value::I16(x)),
            Values::I32(v) => v.get(row).map(|&x| Value::I32(x)),
            Values::I64(v) => v.get(row).map(|&x| Value::I64(x)),
            Values::U8(v) => v.get(row).map(|&x| Value::U8(x)),
            Values::U16(v) => v.get(row).map(|&x| Value::U16(x)),
            Values::U32(v) => v.get(row).map(|&x| Value::U32(x)),
            Values::U64(v) => v.get(row).map(|&x| Value::U64(x)),
            Values::F32(v) => v.get(row).map(|&x| Value::F32(x)),
            Values::F64(v) => v.get(row).map(|&x| Value::F64(x)),
            Values::Text(v) => v.get(row).map(Value::Text),
        };
        v.unwrap_or(Value::Missing)
    }
}

/// One value of one row, borrowed: what a writer that takes a table a row at a time (such
/// as `tablekit_csv::CsvFormat`) is given, so a tool can write each record as it reads it
/// without building columns.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Value<'a> {
    /// No value; any column may have one.
    Missing,
    /// See [`DataType::Bool`].
    Bool(bool),
    /// See [`DataType::I8`].
    I8(i8),
    /// See [`DataType::I16`].
    I16(i16),
    /// See [`DataType::I32`].
    I32(i32),
    /// See [`DataType::I64`].
    I64(i64),
    /// See [`DataType::U8`].
    U8(u8),
    /// See [`DataType::U16`].
    U16(u16),
    /// See [`DataType::U32`].
    U32(u32),
    /// See [`DataType::U64`].
    U64(u64),
    /// See [`DataType::F32`].
    F32(f32),
    /// See [`DataType::F64`].
    F64(f64),
    /// See [`DataType::Text`].
    Text(&'a str),
}

impl Value<'_> {
    /// The type of the value; `None` for [`Value::Missing`], which fits any column.
    #[must_use]
    pub fn data_type(&self) -> Option<DataType> {
        Some(match self {
            Value::Missing => return None,
            Value::Bool(_) => DataType::Bool,
            Value::I8(_) => DataType::I8,
            Value::I16(_) => DataType::I16,
            Value::I32(_) => DataType::I32,
            Value::I64(_) => DataType::I64,
            Value::U8(_) => DataType::U8,
            Value::U16(_) => DataType::U16,
            Value::U32(_) => DataType::U32,
            Value::U64(_) => DataType::U64,
            Value::F32(_) => DataType::F32,
            Value::F64(_) => DataType::F64,
            Value::Text(_) => DataType::Text,
        })
    }
}

/// A column's name and type.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Field {
    /// The column's name.
    pub name: String,
    /// What it holds.
    pub data_type: DataType,
}

/// A set of named columns of equal length: the rows of a table, or a slice of them.
#[derive(Clone, Debug, PartialEq)]
pub struct Batch {
    columns: Vec<Column>,
    rows: usize,
}

impl Batch {
    /// Checks the columns are all one length.
    ///
    /// # Errors
    ///
    /// [`TableError::NoColumns`] for none, [`TableError::RowCount`] if they differ in length.
    pub fn new(columns: Vec<Column>) -> Result<Self, TableError> {
        let rows = columns.first().ok_or(TableError::NoColumns)?.values.len();
        if let Some(c) = columns.iter().find(|c| c.values.len() != rows) {
            return Err(TableError::RowCount {
                column: c.name.clone(),
                rows: c.values.len(),
                expected: rows,
            });
        }
        Ok(Batch { columns, rows })
    }

    /// The number of rows.
    #[must_use]
    pub fn rows(&self) -> usize {
        self.rows
    }

    /// The columns, in order.
    #[must_use]
    pub fn columns(&self) -> &[Column] {
        &self.columns
    }

    /// Each column's name and type, in order.
    #[must_use]
    pub fn schema(&self) -> Vec<Field> {
        self.columns
            .iter()
            .map(|c| Field {
                name: c.name.clone(),
                data_type: c.values.data_type(),
            })
            .collect()
    }
}

/// A table that cannot be written as given.
#[derive(Debug)]
pub enum TableError {
    /// A batch needs at least one column.
    NoColumns,
    /// A column's length differs from the first column's.
    RowCount {
        /// The column.
        column: String,
        /// Its rows.
        rows: usize,
        /// The first column's rows.
        expected: usize,
    },
    /// A column's presence list and values differ in length.
    PresentLength {
        /// The column.
        column: String,
        /// Its values.
        values: usize,
        /// Its presence entries.
        present: usize,
    },
    /// A batch's columns differ from the ones the writer was started with.
    SchemaMismatch {
        /// What the writer was started with.
        expected: Vec<Field>,
        /// What the batch has.
        found: Vec<Field>,
    },
    /// Two columns with one name, which the format does not allow.
    DuplicateName(String),
    /// A row has a different number of values from the table's columns.
    RowLength {
        /// The table's columns.
        expected: usize,
        /// The row's values.
        found: usize,
    },
    /// A row's value is not of its column's type.
    ValueType {
        /// The column.
        column: String,
        /// The column's type.
        expected: DataType,
        /// The value's.
        found: DataType,
    },
    /// Reading or writing failed.
    Io(io::Error),
    /// The format's own writer failed (Parquet), with its message.
    Format(String),
}

impl fmt::Display for TableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TableError::NoColumns => write!(f, "a table needs at least one column"),
            TableError::RowCount {
                column,
                rows,
                expected,
            } => write!(
                f,
                "column {column} has {rows} rows, but the first column has {expected}"
            ),
            TableError::PresentLength {
                column,
                values,
                present,
            } => write!(
                f,
                "column {column} has {values} values but {present} presence entries"
            ),
            TableError::SchemaMismatch { .. } => write!(
                f,
                "a batch's columns differ from the ones the writer was started with"
            ),
            TableError::DuplicateName(name) => {
                write!(
                    f,
                    "two columns are named {name}, which the format does not allow"
                )
            }
            TableError::RowLength { expected, found } => write!(
                f,
                "a row has {found} values, but the table has {expected} columns"
            ),
            TableError::ValueType {
                column,
                expected,
                found,
            } => write!(
                f,
                "column {column} holds {expected:?}, but a row gives it {found:?}"
            ),
            TableError::Io(e) => write!(f, "{e}"),
            TableError::Format(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for TableError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            TableError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for TableError {
    fn from(e: io::Error) -> Self {
        TableError::Io(e)
    }
}

/// The temporary file [`write_atomically`] writes before renaming: `NAME.tmp` beside `path`.
#[must_use]
pub fn temporary_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    path.with_file_name(name)
}

/// Writes `path` whole or not at all: `write` fills a temporary file beside it
/// ([`temporary_path`]), which replaces `path` only if `write` succeeds and the file is
/// flushed. On failure the temporary file is removed and `path` is untouched.
///
/// # Errors
///
/// Whatever `write` returns, or the file could not be created, flushed or renamed.
pub fn write_atomically<T, E: From<TableError>>(
    path: &Path,
    write: impl FnOnce(&mut BufWriter<File>) -> Result<T, E>,
) -> Result<T, E> {
    let tmp = temporary_path(path);
    let io = |e: io::Error| E::from(TableError::Io(e));
    let result = (|| {
        let mut out = BufWriter::with_capacity(1 << 20, File::create(&tmp).map_err(io)?);
        let value = write(&mut out)?;
        let file = out
            .into_inner()
            .map_err(|e| io(io::IntoInnerError::into_error(e)))?;
        file.sync_all().map_err(io)?;
        drop(file);
        std::fs::rename(&tmp, path).map_err(io)?;
        Ok(value)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

/// A table produced a batch at a time: by reading and decoding records, say. Every batch has
/// the columns [`Source::schema`] names. A tool keeps its own error type (a capture that
/// cannot be read, a user's Ctrl-C), which must take a [`TableError`] from the writer.
pub trait Source {
    /// What reading the source, or writing its table, can fail with.
    type Error: From<TableError>;

    /// The columns every batch has.
    fn schema(&self) -> Vec<Field>;

    /// The next batch, or `None` after the last.
    ///
    /// # Errors
    ///
    /// The source could not give it.
    fn next_batch(&mut self) -> Result<Option<Batch>, Self::Error>;
}

/// Takes a table a batch at a time: `tablekit_csv::CsvWriter` and
/// `tablekit_parquet::ParquetWriter`.
pub trait BatchWriter {
    /// Appends `batch`'s rows.
    ///
    /// # Errors
    ///
    /// Its columns differ from the table's, or writing failed.
    fn write_batch(&mut self, batch: &Batch) -> Result<(), TableError>;
}

/// Writes every batch of `source` to `writer`, in order, and returns the rows written. One
/// batch is in memory at a time. The caller finishes the writer.
///
/// # Errors
///
/// Whatever the source or the writer fails with.
pub fn copy<S: Source, W: BatchWriter>(source: &mut S, writer: &mut W) -> Result<u64, S::Error> {
    let mut rows = 0;
    while let Some(batch) = source.next_batch()? {
        writer.write_batch(&batch)?;
        rows += batch.rows() as u64;
    }
    Ok(rows)
}

/// Rejects two columns with one name, for formats that need unique names.
///
/// # Errors
///
/// [`TableError::DuplicateName`] naming the first repeated name.
pub fn unique_names(fields: &[Field]) -> Result<(), TableError> {
    let mut seen = std::collections::HashSet::new();
    match fields.iter().find(|f| !seen.insert(f.name.as_str())) {
        Some(f) => Err(TableError::DuplicateName(f.name.clone())),
        None => Ok(()),
    }
}
