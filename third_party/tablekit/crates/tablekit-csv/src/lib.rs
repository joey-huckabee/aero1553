//! Writes tables as CSV, in the one format every tablekit user shares:
//! `docs/CSV-FORMAT.md`. By default that is what pandas writes with
//! `DataFrame.to_csv(path, index=False)` on Windows.
//!
//! ```
//! use tablekit::{Batch, Column, Values};
//! use tablekit_csv::{CsvOptions, write_to};
//!
//! let batch = Batch::new(vec![
//!     Column::new("time", Values::F64(vec![0.5, 1e-5])),
//!     Column::with_missing("Speed", Values::U16(vec![120, 0]), vec![true, false])?,
//!     Column::new("Valid", Values::Bool(vec![true, false])),
//!     Column::new("Note", Values::Text(vec!["a, b", ""].into())),
//! ])?;
//! let mut out = Vec::new();
//! write_to(&mut out, &batch, &CsvOptions::default())?;
//! assert_eq!(
//!     out,
//!     b"time,Speed,Valid,Note\r\n0.5,120,True,\"a, b\"\r\n1e-05,,False,\r\n"
//! );
//! # Ok::<(), tablekit::TableError>(())
//! ```
//!
//! Three ways in, one format:
//!
//! | | Takes | For |
//! |---|---|---|
//! | [`write_file`], [`write_to`] | a whole [`Batch`] | most uses; a file is written whole or not at all |
//! | [`write_source_file`], [`write_source`] | a [`tablekit::Source`], a batch at a time | a table of any size, one batch in memory |
//! | [`CsvWriter`] | a table in several batches (a [`tablekit::BatchWriter`]) | driving the batches yourself |
//! | [`CsvFormat`] | a row at a time, as borrowed [`Value`]s, into the caller's buffer | a tool that writes each record as it reads it, allocating nothing per row |
//! | [`RowWriter`] ([`CsvFormat::begin_row`], [`CsvWriter::write_row_with`]) | a row a cell at a time, typed by the method called | a tool whose columns are fixed in its code, at the speed of a hand-written writer |
//!
//! A column can be given a [`CellFormat`] other than pandas' default -- hexadecimal for
//! raw words, a fixed number of decimals for a measured interval -- in
//! [`CsvOptions::cell_formats`]. Every way in writes the same bytes for it.

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

mod float;

use std::io::Write;
use std::path::Path;
use std::str::FromStr;

use tablekit::{
    Batch, BatchWriter, Column, Field, Source, TableError, Value, Values, copy, write_atomically,
};

/// How each line ends.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum LineEnding {
    /// `\r\n`: what pandas writes on Windows, and what RFC 4180 specifies. The default.
    #[default]
    CrLf,
    /// `\n`: what pandas writes on Linux and macOS.
    Lf,
}

impl LineEnding {
    fn bytes(self) -> &'static [u8] {
        match self {
            LineEnding::CrLf => b"\r\n",
            LineEnding::Lf => b"\n",
        }
    }
}

/// How the text is encoded, named as Python names it (pandas' `encoding`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Encoding {
    /// `"utf-8"`: UTF-8, no byte-order mark. pandas' default, and the default.
    #[default]
    Utf8,
    /// `"utf-8-sig"`: UTF-8 starting with a byte-order mark, which Excel needs to show
    /// non-ASCII text correctly.
    Utf8Sig,
}

impl Encoding {
    /// Every encoding tablekit writes, by its Python name.
    pub const NAMES: [&'static str; 2] = ["utf-8", "utf-8-sig"];

    /// Its Python name: `"utf-8"` or `"utf-8-sig"`.
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Encoding::Utf8 => "utf-8",
            Encoding::Utf8Sig => "utf-8-sig",
        }
    }

    /// What the file starts with.
    fn preamble(self) -> &'static [u8] {
        match self {
            Encoding::Utf8 => b"",
            Encoding::Utf8Sig => b"\xEF\xBB\xBF",
        }
    }
}

impl FromStr for Encoding {
    type Err = String;

    /// Reads a Python encoding name, as Python matches them: case and `-` or `_` aside
    /// (`"UTF-8"`, `"utf8"`, `"utf_8_sig"`).
    fn from_str(name: &str) -> Result<Self, Self::Err> {
        let key: String = name
            .chars()
            .filter(|c| !matches!(c, '-' | '_' | ' '))
            .flat_map(char::to_lowercase)
            .collect();
        match key.as_str() {
            "utf8" | "u8" => Ok(Encoding::Utf8),
            "utf8sig" => Ok(Encoding::Utf8Sig),
            _ => Err(format!(
                "tablekit writes the encodings {}, not {name:?}",
                Encoding::NAMES.map(|n| format!("{n:?}")).join(" and ")
            )),
        }
    }
}

/// How one column's values are written. [`CellFormat::Default`] is pandas'; the others are
/// what pandas writes for the column once it has been turned into text by Python's own
/// formatting (`docs/CSV-FORMAT.md`, "Cell formats").
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum CellFormat {
    /// What pandas writes for the column's type.
    #[default]
    Default,
    /// An unsigned integer in upper-case hexadecimal, zero-padded to at least this many digits
    /// (1 to 16): Python's `format(v, "04X")` for 4. More digits are written if the value
    /// needs them.
    HexUpper(u8),
    /// As [`CellFormat::HexUpper`], lower case: `format(v, "04x")`.
    HexLower(u8),
    /// A float with exactly this many decimals (0 to 17), rounded to nearest, ties to even on
    /// the exact binary value: Python's `f"{v:.6f}"` for 6, pandas' `float_format="%.6f"`.
    /// NaN and infinity are written as for [`CellFormat::Default`].
    Fixed(u8),
}

/// The most decimals [`CellFormat::Fixed`] takes: an `f64` holds at most 17 significant
/// digits, and `10^17` still fits the integer the common case is formatted through.
pub const MAX_FIXED_DECIMALS: u8 = 17;

/// The choices a user may make; [`CsvOptions::default`] is pandas' `to_csv` on Windows.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CsvOptions {
    /// Written for a missing value, and for a floating-point NaN: pandas' `na_rep`. Default
    /// empty.
    pub na_rep: String,
    /// How each line ends. Default [`LineEnding::CrLf`].
    pub line_ending: LineEnding,
    /// How the text is encoded: pandas' `encoding`. Default [`Encoding::Utf8`].
    pub encoding: Encoding,
    /// A [`CellFormat`] for each named column that is not written pandas' default way.
    /// Default none. A name that is not a column, a name given twice, or a format the
    /// column's type cannot take is refused when the format is built.
    pub cell_formats: Vec<(String, CellFormat)>,
}

/// Bytes [`CsvWriter`] gathers before each write to its output.
const FLUSH_AT: usize = 1 << 20;

/// The CSV format for one table's columns: formats the header and each row into a buffer the
/// caller owns. [`CsvWriter`] is built on it; a tool that writes each record as it reads it
/// uses it directly.
///
/// ```
/// use tablekit::{DataType, Field, Value};
/// use tablekit_csv::{CsvFormat, CsvOptions};
///
/// let fields = vec![
///     Field { name: "label".into(), data_type: DataType::U32 },
///     Field { name: "time".into(), data_type: DataType::F64 },
/// ];
/// let mut format = CsvFormat::new(fields, CsvOptions::default())?;
/// let mut out = Vec::new();
/// format.start(&mut out);
/// format.row(&mut out, &[Value::U32(7), Value::F64(0.5)])?;
/// format.row(&mut out, &[Value::U32(8), Value::Missing])?;
/// assert_eq!(out, b"label,time\r\n7,0.5\r\n8,\r\n");
/// # Ok::<(), tablekit::TableError>(())
/// ```
#[derive(Clone, Debug)]
pub struct CsvFormat {
    schema: Vec<Field>,
    options: CsvOptions,
    /// Each column's format, in column order: [`CsvOptions::cell_formats`] resolved.
    formats: Vec<CellFormat>,
    /// Whether any column has a format other than the default, so a table without one runs
    /// exactly the loops it ran before formats existed.
    formatted: bool,
    /// Whether `na_rep` is empty: a missing cell then writes nothing.
    na_empty: bool,
}

impl CsvFormat {
    /// The format for these columns.
    ///
    /// # Errors
    ///
    /// [`TableError::NoColumns`] for none, or [`TableError::Format`] for a
    /// [`CsvOptions::cell_formats`] entry that names no column, names one twice, or does not
    /// suit the column's type.
    pub fn new(schema: Vec<Field>, options: CsvOptions) -> Result<Self, TableError> {
        if schema.is_empty() {
            return Err(TableError::NoColumns);
        }
        let formats = resolve_cell_formats(&schema, &options.cell_formats)?;
        let formatted = formats.iter().any(|&f| f != CellFormat::Default);
        let na_empty = options.na_rep.is_empty();
        Ok(CsvFormat {
            schema,
            options,
            formats,
            formatted,
            na_empty,
        })
    }

    /// The columns.
    #[must_use]
    pub fn schema(&self) -> &[Field] {
        &self.schema
    }

    /// Appends what the file starts with: the encoding's byte-order mark, if it has one, and
    /// the header row.
    pub fn start(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(self.options.encoding.preamble());
        let line_start = out.len();
        for (i, field) in self.schema.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            text(out, &field.name);
        }
        self.end_row(out, line_start);
    }

    /// Appends one row: a value per column, each of the column's type or
    /// [`Value::Missing`].
    ///
    /// # Errors
    ///
    /// [`TableError::RowLength`] or [`TableError::ValueType`]; nothing is appended.
    // HOT PATH: per row.
    #[inline]
    pub fn row(&mut self, out: &mut Vec<u8>, row: &[Value<'_>]) -> Result<(), TableError> {
        if row.len() != self.schema.len() {
            return Err(row_length(self.schema.len(), row.len()));
        }
        if self.formatted {
            return self.row_formatted(out, row);
        }
        // One pass: each value is checked as it is written, and a refused row is taken back.
        // Room for a typical row up front, so the pushes below do not grow the buffer.
        out.reserve(row.len() * 24 + 2);
        let line_start = out.len();
        for (i, (&value, field)) in row.iter().zip(&self.schema).enumerate() {
            if let Some(found) = value.data_type() {
                if found != field.data_type {
                    out.truncate(line_start);
                    return Err(value_type(field, found));
                }
            }
            if i > 0 {
                out.push(b',');
            }
            self.value(out, value);
        }
        self.end_row(out, line_start);
        Ok(())
    }

    /// [`CsvFormat::row`] for a table with [`CellFormat`]s. Its own function, so a table
    /// without them compiles and runs exactly the loop it did before formats existed.
    // HOT PATH: per row, for a table with cell formats. Cold so that a table without them
    // keeps the code layout `row` had before formats existed (measured: callgrind).
    #[cold]
    #[inline(never)]
    fn row_formatted(&self, out: &mut Vec<u8>, row: &[Value<'_>]) -> Result<(), TableError> {
        out.reserve(row.len() * 24 + 2);
        let line_start = out.len();
        for (i, ((&value, field), &format)) in
            row.iter().zip(&self.schema).zip(&self.formats).enumerate()
        {
            if let Some(found) = value.data_type() {
                if found != field.data_type {
                    out.truncate(line_start);
                    return Err(value_type(field, found));
                }
            }
            if i > 0 {
                out.push(b',');
            }
            self.formatted_value(out, format, value);
        }
        self.end_row(out, line_start);
        Ok(())
    }

    /// Starts a row in `out`, to be written a cell at a time. Write every column's cell in
    /// column order with the [`RowWriter`] method for its type and format, then call
    /// [`RowWriter::finish`].
    ///
    /// ```
    /// use tablekit::{DataType, Field};
    /// use tablekit_csv::{CellFormat, CsvFormat, CsvOptions};
    ///
    /// let fields = vec![
    ///     Field { name: "word".into(), data_type: DataType::U16 },
    ///     Field { name: "delta".into(), data_type: DataType::F64 },
    ///     Field { name: "bus".into(), data_type: DataType::Text },
    /// ];
    /// let options = CsvOptions {
    ///     cell_formats: vec![
    ///         ("word".into(), CellFormat::HexUpper(4)),
    ///         ("delta".into(), CellFormat::Fixed(6)),
    ///     ],
    ///     ..CsvOptions::default()
    /// };
    /// let format = CsvFormat::new(fields, options)?;
    /// let mut out = Vec::new();
    /// let mut row = format.begin_row(&mut out);
    /// row.hex::<4>(0x2F_u16);
    /// row.fixed::<6>(0.25);
    /// row.text("A");
    /// row.finish()?;
    /// assert_eq!(out, b"002F,0.250000,A\r\n");
    /// # Ok::<(), tablekit::TableError>(())
    /// ```
    #[inline]
    pub fn begin_row<'f, 'o>(&'f self, out: &'o mut Vec<u8>) -> RowWriter<'f, 'o> {
        let line_start = out.len();
        RowWriter {
            format: self,
            out,
            col: 0,
            line_start,
            mismatch: None,
        }
    }

    /// Ends a row. A row of one empty field is written `""`, as Python's `csv` module (and so
    /// pandas) writes it, so the line is not mistaken for a blank one.
    #[inline]
    fn end_row(&self, out: &mut Vec<u8>, line_start: usize) {
        if self.schema.len() == 1 && out.len() == line_start {
            out.extend_from_slice(b"\"\"");
        }
        out.extend_from_slice(self.options.line_ending.bytes());
    }

    /// Appends row `row` of `column`, formatted straight from the column's values: one
    /// dispatch on its type, where going through [`Value`] would take two.
    // HOT PATH: per value.
    #[inline]
    fn cell(&self, out: &mut Vec<u8>, column: &Column, row: usize) {
        if column.has_missing() && !column.is_present(row) {
            text(out, &self.options.na_rep);
            return;
        }
        match column.values() {
            Values::Bool(v) => write_bool(out, v[row]),
            Values::I8(v) => integer(out, v[row]),
            Values::I16(v) => integer(out, v[row]),
            Values::I32(v) => integer(out, v[row]),
            Values::I64(v) => integer(out, v[row]),
            Values::U8(v) => integer(out, v[row]),
            Values::U16(v) => integer(out, v[row]),
            Values::U32(v) => integer(out, v[row]),
            Values::U64(v) => integer(out, v[row]),
            Values::F32(v) => self.value(out, Value::F32(v[row])),
            Values::F64(v) => self.value(out, Value::F64(v[row])),
            Values::Text(v) => text(out, v.get(row).unwrap_or_default()),
        }
    }

    /// [`CsvFormat::cell`] for a column with a [`CellFormat`] other than the default.
    // HOT PATH: per value.
    #[inline]
    fn formatted_cell(&self, out: &mut Vec<u8>, format: CellFormat, column: &Column, row: usize) {
        if column.has_missing() && !column.is_present(row) {
            text(out, &self.options.na_rep);
            return;
        }
        match (format, column.values()) {
            (CellFormat::HexUpper(w), Values::U8(v)) => hex(out, u64::from(v[row]), w, HEX_UPPER),
            (CellFormat::HexUpper(w), Values::U16(v)) => hex(out, u64::from(v[row]), w, HEX_UPPER),
            (CellFormat::HexUpper(w), Values::U32(v)) => hex(out, u64::from(v[row]), w, HEX_UPPER),
            (CellFormat::HexUpper(w), Values::U64(v)) => hex(out, v[row], w, HEX_UPPER),
            (CellFormat::HexLower(w), Values::U8(v)) => hex(out, u64::from(v[row]), w, HEX_LOWER),
            (CellFormat::HexLower(w), Values::U16(v)) => hex(out, u64::from(v[row]), w, HEX_LOWER),
            (CellFormat::HexLower(w), Values::U32(v)) => hex(out, u64::from(v[row]), w, HEX_LOWER),
            (CellFormat::HexLower(w), Values::U64(v)) => hex(out, v[row], w, HEX_LOWER),
            (CellFormat::Fixed(d), Values::F32(v)) => self.fixed_value(out, f64::from(v[row]), d),
            (CellFormat::Fixed(d), Values::F64(v)) => self.fixed_value(out, v[row], d),
            _ => self.default_value(out, column_value(column, row)),
        }
    }

    /// Appends NaN as `na_rep`, and infinity as `inf` or `-inf`.
    #[cold]
    fn not_finite(&self, out: &mut Vec<u8>, nan: bool, positive: bool) {
        if nan {
            text(out, &self.options.na_rep);
        } else {
            infinity(out, positive);
        }
    }

    /// Appends one value, or `na_rep` for a missing one or a NaN.
    // HOT PATH: per value.
    #[inline]
    fn value(&self, out: &mut Vec<u8>, value: Value<'_>) {
        match value {
            Value::Missing => text(out, &self.options.na_rep),
            Value::Bool(b) => write_bool(out, b),
            Value::I8(v) => integer(out, v),
            Value::I16(v) => integer(out, v),
            Value::I32(v) => integer(out, v),
            Value::I64(v) => integer(out, v),
            Value::U8(v) => integer(out, v),
            Value::U16(v) => integer(out, v),
            Value::U32(v) => integer(out, v),
            Value::U64(v) => integer(out, v),
            Value::F32(x) if x.is_finite() => float::write_f32(out, x),
            Value::F64(x) if x.is_finite() => float::write_f64(out, x),
            Value::F32(x) => self.not_finite(out, x.is_nan(), x > 0.0),
            Value::F64(x) => self.not_finite(out, x.is_nan(), x > 0.0),
            Value::Text(s) => text(out, s),
        }
    }

    /// [`CsvFormat::value`] in the column's [`CellFormat`]. A value of the wrong type has
    /// been refused before this is called.
    // HOT PATH: per value.
    #[inline]
    fn formatted_value(&self, out: &mut Vec<u8>, format: CellFormat, value: Value<'_>) {
        match (format, value) {
            (CellFormat::HexUpper(w), Value::U8(v)) => hex(out, u64::from(v), w, HEX_UPPER),
            (CellFormat::HexUpper(w), Value::U16(v)) => hex(out, u64::from(v), w, HEX_UPPER),
            (CellFormat::HexUpper(w), Value::U32(v)) => hex(out, u64::from(v), w, HEX_UPPER),
            (CellFormat::HexUpper(w), Value::U64(v)) => hex(out, v, w, HEX_UPPER),
            (CellFormat::HexLower(w), Value::U8(v)) => hex(out, u64::from(v), w, HEX_LOWER),
            (CellFormat::HexLower(w), Value::U16(v)) => hex(out, u64::from(v), w, HEX_LOWER),
            (CellFormat::HexLower(w), Value::U32(v)) => hex(out, u64::from(v), w, HEX_LOWER),
            (CellFormat::HexLower(w), Value::U64(v)) => hex(out, v, w, HEX_LOWER),
            (CellFormat::Fixed(d), Value::F32(x)) => self.fixed_value(out, f64::from(x), d),
            (CellFormat::Fixed(d), Value::F64(x)) => self.fixed_value(out, x, d),
            _ => self.default_value(out, value),
        }
    }

    /// [`CsvFormat::value`], for the paths that write [`CellFormat`]s. A copy on purpose:
    /// `value` and `cell` are inlined into the loops of a table without formats only while
    /// those loops are their sole callers, and a second caller made the compiler call them
    /// out of line there instead, 6% more instructions per row (`CHANGELOG.md`, 0.7.0).
    // HOT PATH: per value.
    #[inline(never)]
    fn default_value(&self, out: &mut Vec<u8>, value: Value<'_>) {
        match value {
            Value::Missing => text(out, &self.options.na_rep),
            Value::Bool(b) => write_bool(out, b),
            Value::I8(v) => integer(out, v),
            Value::I16(v) => integer(out, v),
            Value::I32(v) => integer(out, v),
            Value::I64(v) => integer(out, v),
            Value::U8(v) => integer(out, v),
            Value::U16(v) => integer(out, v),
            Value::U32(v) => integer(out, v),
            Value::U64(v) => integer(out, v),
            Value::F32(x) if x.is_finite() => float::write_f32(out, x),
            Value::F64(x) if x.is_finite() => float::write_f64(out, x),
            Value::F32(x) => self.not_finite(out, x.is_nan(), x > 0.0),
            Value::F64(x) => self.not_finite(out, x.is_nan(), x > 0.0),
            Value::Text(s) => text(out, s),
        }
    }

    /// `x` with `d` decimals, or NaN and infinity as [`CellFormat::Default`] writes them.
    // HOT PATH: per value.
    #[inline]
    fn fixed_value(&self, out: &mut Vec<u8>, x: f64, d: u8) {
        if x.is_finite() {
            fixed_dyn(out, x, d);
        } else {
            self.not_finite(out, x.is_nan(), x > 0.0);
        }
    }
}

/// Each column's [`CellFormat`], in column order, from [`CsvOptions::cell_formats`]. Out of
/// line, so that [`CsvFormat::new`] stays small enough to inline where a caller builds a
/// format beside the loop that uses it (callgrind: a larger `new` changed that loop).
#[inline(never)]
fn resolve_cell_formats(
    schema: &[Field],
    cell_formats: &[(String, CellFormat)],
) -> Result<Vec<CellFormat>, TableError> {
    let mut formats = vec![CellFormat::Default; schema.len()];
    for (name, format) in cell_formats {
        let Some(i) = schema.iter().position(|f| &f.name == name) else {
            return Err(TableError::Format(format!(
                "cell format for {name:?}: no such column"
            )));
        };
        if formats[i] != CellFormat::Default {
            return Err(TableError::Format(format!(
                "cell format for {name:?} given twice"
            )));
        }
        check_cell_format(&schema[i], *format)?;
        formats[i] = *format;
    }
    Ok(formats)
}

/// Refuses a [`CellFormat`] the column's type cannot take.
fn check_cell_format(field: &Field, format: CellFormat) -> Result<(), TableError> {
    use tablekit::DataType as T;
    let ok = match format {
        CellFormat::Default => true,
        CellFormat::HexUpper(w) | CellFormat::HexLower(w) => {
            (1..=16).contains(&w) && matches!(field.data_type, T::U8 | T::U16 | T::U32 | T::U64)
        }
        CellFormat::Fixed(d) => {
            d <= MAX_FIXED_DECIMALS && matches!(field.data_type, T::F32 | T::F64)
        }
    };
    if ok {
        Ok(())
    } else {
        Err(TableError::Format(format!(
            "cell format {format:?} does not suit column {:?} of type {:?} (hexadecimal: \
             unsigned integers, 1 to 16 digits; fixed: floats, 0 to {MAX_FIXED_DECIMALS} \
             decimals)",
            field.name, field.data_type
        )))
    }
}

/// Row `row` of a column with a value there.
// HOT PATH: per value.
#[inline]
fn column_value(column: &Column, row: usize) -> Value<'_> {
    match column.values() {
        Values::Bool(v) => Value::Bool(v[row]),
        Values::I8(v) => Value::I8(v[row]),
        Values::I16(v) => Value::I16(v[row]),
        Values::I32(v) => Value::I32(v[row]),
        Values::I64(v) => Value::I64(v[row]),
        Values::U8(v) => Value::U8(v[row]),
        Values::U16(v) => Value::U16(v[row]),
        Values::U32(v) => Value::U32(v[row]),
        Values::U64(v) => Value::U64(v[row]),
        Values::F32(v) => Value::F32(v[row]),
        Values::F64(v) => Value::F64(v[row]),
        Values::Text(v) => Value::Text(v.get(row).unwrap_or_default()),
    }
}

#[cold]
fn row_length(expected: usize, found: usize) -> TableError {
    TableError::RowLength { expected, found }
}

#[cold]
fn value_type(field: &Field, found: tablekit::DataType) -> TableError {
    TableError::ValueType {
        column: field.name.clone(),
        expected: field.data_type,
        found,
    }
}

/// One row, written a cell at a time straight into the output: for a tool whose columns are
/// fixed in its code, at the speed of a hand-written writer. Start one with
/// [`CsvFormat::begin_row`] or [`CsvWriter::write_row_with`].
///
/// Call one method per column, in column order: the one for the column's type
/// ([`RowWriter::u16`], [`RowWriter::text`], ...) or, for a column with a [`CellFormat`], the
/// one for its format ([`RowWriter::hex`], [`RowWriter::fixed`]); [`RowWriter::missing`] for
/// any column. Then [`RowWriter::finish`]. Each method names its column's type and format, so
/// nothing is looked up per cell; that is where its speed comes from. The bytes are exactly
/// what [`CsvFormat::row`] writes for the same values.
///
/// What is checked:
///
/// - **Every build:** [`RowWriter::finish`] refuses a row with the wrong number of cells and
///   takes it back. Text is quoted wherever it needs to be, [`RowWriter::text_with`]'s
///   included, so the file is always valid CSV.
/// - **Debug builds** (and so tests): that each cell's method suits its column's type and
///   format; [`RowWriter::finish`] refuses the row otherwise, naming the column. A release
///   build does not check this per cell: a caller's code calls the same methods for every row,
///   so a test that writes one row of each shape proves them all. Where the columns are not
///   fixed in the caller's code, use [`CsvFormat::row`], which checks every value.
///
/// A row that is never finished leaves its cells in the output; a caller that abandons one
/// truncates the buffer to where it was.
pub struct RowWriter<'f, 'o> {
    format: &'f CsvFormat,
    out: &'o mut Vec<u8>,
    col: usize,
    line_start: usize,
    /// Debug builds: the first cell whose method did not suit its column, and the type the
    /// method writes.
    mismatch: Option<(usize, tablekit::DataType)>,
}

#[expect(
    clippy::inline_always,
    reason = "the row writer is fast only when every cell inlines into the caller's row: measured in aero1553, where without it a 46-column row ran 1.20x its hand-written writer"
)]
impl RowWriter<'_, '_> {
    /// Counts a cell; in debug builds, checks the method that writes it (`ty`, `format`)
    /// suits the column. `None` suits any column. Each cell is followed by a separator
    /// ([`RowWriter::end_cell`]), and [`RowWriter::finish`] turns the last one into the line
    /// ending: no cell has to ask whether it is the first.
    // HOT PATH: per value.
    #[inline(always)]
    fn begin_cell(&mut self, expect: Option<(tablekit::DataType, CellFormat)>) {
        if cfg!(debug_assertions) {
            self.check(expect);
        }
        self.col += 1;
    }

    /// The separator after a cell.
    // HOT PATH: per value.
    #[inline(always)]
    fn end_cell(&mut self) {
        self.out.push(b',');
    }

    /// The debug-build check of [`RowWriter::begin_cell`].
    fn check(&mut self, expect: Option<(tablekit::DataType, CellFormat)>) {
        let Some((ty, format)) = expect else { return };
        let suits = match (
            self.format.schema.get(self.col),
            self.format.formats.get(self.col),
        ) {
            (Some(field), Some(&f)) => field.data_type == ty && f == format,
            _ => true, // too many cells: finish() reports it
        };
        if !suits && self.mismatch.is_none() {
            self.mismatch = Some((self.col, ty));
        }
    }

    /// A missing value, in any column: `na_rep`.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn missing(&mut self) {
        self.begin_cell(None);
        if !self.format.na_empty {
            text(self.out, &self.format.options.na_rep);
        }
        self.end_cell();
    }

    /// A `Bool` column's value: `True` or `False`.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn bool(&mut self, v: bool) {
        self.begin_cell(Some((tablekit::DataType::Bool, CellFormat::Default)));
        write_bool(self.out, v);
        self.end_cell();
    }

    /// A `Text` column's value, quoted if it holds `,`, `"`, CR or LF.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn text(&mut self, s: &str) {
        self.begin_cell(Some((tablekit::DataType::Text, CellFormat::Default)));
        if needs_quoting(s.as_bytes()) {
            text(self.out, s);
        } else {
            self.out.extend_from_slice(s.as_bytes());
        }
        self.end_cell();
    }

    /// A `Text` column's value, built by `build` straight into the output through a
    /// [`TextBuilder`]: for text made of numbers and fixed characters (a timestamp, a label)
    /// without a string of its own. Whatever is built, the cell is quoted if it needs to be,
    /// so the file stays valid CSV; text built from numbers and literal characters is known
    /// not to need it without being read back.
    ///
    /// ```
    /// use tablekit::{DataType, Field};
    /// use tablekit_csv::{CsvFormat, CsvOptions};
    ///
    /// let fields = vec![Field { name: "time".into(), data_type: DataType::Text }];
    /// let format = CsvFormat::new(fields, CsvOptions::default())?;
    /// let mut out = Vec::new();
    /// let mut row = format.begin_row(&mut out);
    /// row.text_with(|t| {
    ///     t.digits(192, 1);
    ///     t.push(b':');
    ///     t.digits(5, 2);
    ///     t.push_str(", late");
    /// });
    /// row.finish()?;
    /// assert_eq!(out, b"\"192:05, late\"\r\n");
    /// # Ok::<(), tablekit::TableError>(())
    /// ```
    // HOT PATH: per value.
    #[inline(always)]
    pub fn text_with(&mut self, build: impl FnOnce(&mut TextBuilder<'_>)) {
        self.begin_cell(Some((tablekit::DataType::Text, CellFormat::Default)));
        let start = self.out.len();
        let mut builder = TextBuilder {
            out: self.out,
            quote: false,
        };
        build(&mut builder);
        if builder.quote {
            quote_in_place(self.out, start);
        }
        self.end_cell();
    }

    /// An `F32` column's value, as pandas writes it.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn f32(&mut self, x: f32) {
        self.begin_cell(Some((tablekit::DataType::F32, CellFormat::Default)));
        if x.is_finite() {
            float::write_f32(self.out, x);
        } else {
            self.format.not_finite(self.out, x.is_nan(), x > 0.0);
        }
        self.end_cell();
    }

    /// An `F64` column's value, as pandas writes it.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn f64(&mut self, x: f64) {
        self.begin_cell(Some((tablekit::DataType::F64, CellFormat::Default)));
        if x.is_finite() {
            float::write_f64(self.out, x);
        } else {
            self.format.not_finite(self.out, x.is_nan(), x > 0.0);
        }
        self.end_cell();
    }

    /// An `F64` column with [`CellFormat::Fixed`]`(D)`: exactly `D` decimals. `D` above
    /// [`MAX_FIXED_DECIMALS`] does not compile.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn fixed<const D: u8>(&mut self, x: f64) {
        self.begin_cell(Some((tablekit::DataType::F64, CellFormat::Fixed(D))));
        self.fixed_cell::<D>(x);
        self.end_cell();
    }

    /// An `F32` column with [`CellFormat::Fixed`]`(D)`, as [`RowWriter::fixed`].
    // HOT PATH: per value.
    #[inline(always)]
    pub fn fixed_f32<const D: u8>(&mut self, x: f32) {
        self.begin_cell(Some((tablekit::DataType::F32, CellFormat::Fixed(D))));
        self.fixed_cell::<D>(f64::from(x));
        self.end_cell();
    }

    // HOT PATH: per value.
    #[inline(always)]
    fn fixed_cell<const D: u8>(&mut self, x: f64) {
        if x.is_finite() {
            fixed::<D>(self.out, x);
        } else {
            self.format.not_finite(self.out, x.is_nan(), x > 0.0);
        }
    }

    /// An unsigned integer column (`U8` to `U64`) with [`CellFormat::HexUpper`]`(W)`: at
    /// least `W` upper-case hexadecimal digits. `W` outside 1 to 16 does not compile.
    /// The value's own type picks the column type it is checked against, so give a literal
    /// one (`0x2F_u16`).
    // HOT PATH: per value.
    #[inline(always)]
    pub fn hex<const W: u8>(&mut self, v: impl HexValue) {
        self.begin_cell(Some((v.data_type(), CellFormat::HexUpper(W))));
        hex_const::<W>(self.out, v.into_u64(), HEX_UPPER);
        self.end_cell();
    }

    /// As [`RowWriter::hex`], lower case ([`CellFormat::HexLower`]`(W)`).
    // HOT PATH: per value.
    #[inline(always)]
    pub fn hex_lower<const W: u8>(&mut self, v: impl HexValue) {
        self.begin_cell(Some((v.data_type(), CellFormat::HexLower(W))));
        hex_const::<W>(self.out, v.into_u64(), HEX_LOWER);
        self.end_cell();
    }

    /// Ends the row.
    ///
    /// # Errors
    ///
    /// [`TableError::RowLength`] for the wrong number of cells, or, in debug builds,
    /// [`TableError::ValueType`] for a cell whose method did not suit its column; the row is
    /// taken back.
    #[inline(always)]
    pub fn finish(self) -> Result<(), TableError> {
        let expected = self.format.schema.len();
        if self.col != expected {
            self.out.truncate(self.line_start);
            return Err(row_length(expected, self.col));
        }
        if let Some((col, found)) = self.mismatch {
            self.out.truncate(self.line_start);
            return Err(value_type(&self.format.schema[col], found));
        }
        // The separator after the last cell (there is one: no table has no columns).
        self.out.pop();
        self.format.end_row(self.out, self.line_start);
        Ok(())
    }
}

macro_rules! row_writer_integers {
    ($($name:ident: $ty:ty => $data_type:ident),* $(,)?) => {
        impl RowWriter<'_, '_> {
            $(
                #[doc = concat!("A `", stringify!($data_type), "` column's value, in decimal.")]
                // HOT PATH: per value.
                #[inline(always)]
                pub fn $name(&mut self, v: $ty) {
                    self.begin_cell(Some((tablekit::DataType::$data_type, CellFormat::Default)));
                    integer(self.out, v);
                    self.end_cell();
                }
            )*
        }
    };
}

row_writer_integers! {
    i8: i8 => I8, i16: i16 => I16, i32: i32 => I32, i64: i64 => I64,
    u8: u8 => U8, u16: u16 => U16, u32: u32 => U32, u64: u64 => U64,
}

/// Builds one text cell for [`RowWriter::text_with`]. It notes, as text is added, whether
/// the cell will need quoting, so nothing is read back: numbers never need it, and for a
/// literal character the check is settled when compiling.
pub struct TextBuilder<'o> {
    out: &'o mut Vec<u8>,
    quote: bool,
}

#[expect(
    clippy::inline_always,
    reason = "the row writer is fast only when every cell inlines into the caller's row: measured in aero1553, where without it a 46-column row ran 1.20x its hand-written writer"
)]
impl TextBuilder<'_> {
    /// Appends `n` in decimal, zero-padded to at least `width` digits.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn digits(&mut self, n: u64, width: usize) {
        // Laid out in a buffer on the stack, already zero-padded, then copied whole -- a
        // copy of a fixed size, which compiles to a few stores -- and the output trimmed
        // to length: a copy of a length known only when running was a `memcpy` call per
        // cell. A number that fits `width` fills exactly `width` places, straight-line code
        // where the width is a constant; a wider one counts its digits first.
        const ROOM: usize = 20; // u64::MAX has 20 digits
        if width > ROOM {
            wide_digits(self.out, n, width);
            return;
        }
        let mut buffer = [b'0'; ROOM];
        let mut total = width;
        let mut rest = n;
        for slot in buffer[..total].iter_mut().rev() {
            *slot = b'0' + u8::try_from(rest % 10).unwrap_or(0);
            rest /= 10;
        }
        if rest != 0 || total == 0 {
            total = n
                .checked_ilog10()
                .map_or(1, |log| log as usize + 1)
                .max(width);
            rest = n;
            for slot in buffer[..total].iter_mut().rev() {
                *slot = b'0' + u8::try_from(rest % 10).unwrap_or(0);
                rest /= 10;
            }
        }
        let start = self.out.len();
        self.out.extend_from_slice(&buffer);
        self.out.truncate(start + total);
    }

    /// Appends one ASCII character. A byte that is not ASCII would make the cell invalid
    /// UTF-8, so it is written as U+FFFD instead.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn push(&mut self, c: u8) {
        if c.is_ascii() {
            self.quote |= matches!(c, b',' | b'"' | b'\r' | b'\n');
            self.out.push(c);
        } else {
            self.out.extend_from_slice("\u{FFFD}".as_bytes());
        }
    }

    /// Appends text, which is read once to tell whether the cell needs quoting.
    // HOT PATH: per value.
    #[inline(always)]
    pub fn push_str(&mut self, s: &str) {
        self.quote |= needs_quoting(s.as_bytes());
        self.out.extend_from_slice(s.as_bytes());
    }
}

/// An unsigned integer [`RowWriter::hex`] takes, naming the column type it belongs in.
pub trait HexValue: Copy + sealed::Sealed {
    /// The column type this value belongs in.
    fn data_type(self) -> tablekit::DataType;
    /// The value, widened.
    fn into_u64(self) -> u64;
}

mod sealed {
    pub trait Sealed {}
}

macro_rules! hex_values {
    ($($ty:ty => $data_type:ident),*) => {$(
        impl sealed::Sealed for $ty {}
        impl HexValue for $ty {
            #[inline]
            fn data_type(self) -> tablekit::DataType {
                tablekit::DataType::$data_type
            }
            #[inline]
            fn into_u64(self) -> u64 {
                u64::from(self)
            }
        }
    )*};
}

hex_values!(u8 => U8, u16 => U16, u32 => U32, u64 => U64);

const HEX_UPPER: &[u8; 16] = b"0123456789ABCDEF";
const HEX_LOWER: &[u8; 16] = b"0123456789abcdef";

/// `v` in hexadecimal, at least `width` digits.
// HOT PATH: per value.
#[inline]
fn hex(out: &mut Vec<u8>, v: u64, width: u8, digits: &[u8; 16]) {
    // Hexadecimal digits `v` needs: one per started nibble, at least one.
    let needed = (64 - v.leading_zeros()).div_ceil(4).max(1);
    for shift in (0..needed.max(u32::from(width))).rev() {
        out.push(digits[usize::try_from((v >> (shift * 4)) & 0xF).unwrap_or(0)]);
    }
}

/// [`hex`] for a width known when compiling: a value that fits it, the common case, is
/// written without a loop.
// HOT PATH: per value.
#[expect(
    clippy::inline_always,
    reason = "the row writer is fast only when every cell inlines into the caller's row: measured in aero1553, where without it a 46-column row ran 1.20x its hand-written writer"
)]
#[inline(always)]
fn hex_const<const W: u8>(out: &mut Vec<u8>, v: u64, digits: &[u8; 16]) {
    const { assert!(W >= 1 && W <= 16, "hexadecimal width must be 1 to 16") };
    if W < 16 && v >> (u32::from(W) * 4) != 0 {
        hex(out, v, W, digits);
        return;
    }
    let mut buf = [0u8; 16];
    let cells = &mut buf[..usize::from(W)];
    let mut rest = v;
    for slot in cells.iter_mut().rev() {
        *slot = digits[usize::try_from(rest & 0xF).unwrap_or(0)];
        rest >>= 4;
    }
    out.extend_from_slice(cells);
}

/// `x` (finite) with exactly `D` decimals, as Rust's and Python's `.{D}f` write it: rounded
/// to nearest, ties to even, on the exact binary value.
///
/// The common case -- `x` a whole number of `10^-D` units, as a measured interval is -- is
/// written from that integer, with divisions by a constant; anything else goes through
/// `core::fmt`, which is exact.
// HOT PATH: per value.
#[expect(
    clippy::inline_always,
    reason = "the row writer is fast only when every cell inlines into the caller's row: measured in aero1553, where without it a 46-column row ran 1.20x its hand-written writer"
)]
#[inline(always)]
fn fixed<const D: u8>(out: &mut Vec<u8>, x: f64) {
    let scale: u64 = const {
        assert!(
            D <= MAX_FIXED_DECIMALS,
            "at most MAX_FIXED_DECIMALS decimals"
        );
        10u64.pow(D as u32)
    };
    if let Some(units) = whole_units(x.abs(), scale) {
        if x.is_sign_negative() {
            out.push(b'-');
        }
        integer(out, units / scale);
        if D > 0 {
            out.push(b'.');
            let start = out.len();
            out.resize(start + usize::from(D), b'0');
            let mut frac = units % scale;
            for slot in out[start..].iter_mut().rev() {
                *slot = b'0' + u8::try_from(frac % 10).unwrap_or(0);
                frac /= 10;
            }
        }
        return;
    }
    fixed_fmt(out, x, D);
}

/// [`fixed`] for a decimal count known only when running ([`CsvFormat::row`] and the batch
/// writer): the six decimals of an interval in microseconds take the constant path.
// HOT PATH: per value.
#[inline]
fn fixed_dyn(out: &mut Vec<u8>, x: f64, d: u8) {
    match d {
        3 => fixed::<3>(out, x),
        6 => fixed::<6>(out, x),
        9 => fixed::<9>(out, x),
        _ => fixed_fmt(out, x, d),
    }
}

/// `x` with `d` decimals through `core::fmt`: exact, and slower.
#[cold]
fn fixed_fmt(out: &mut Vec<u8>, x: f64, d: u8) {
    // Writing to a Vec cannot fail.
    let _ = write!(out, "{x:.prec$}", prec = usize::from(d));
}

/// `v` (non-negative) as a whole number of `1/scale` units, if it is exactly one: the units,
/// converted back, give `v`'s bits. Below 2^53 units every whole number is exact.
#[inline]
fn whole_units(v: f64, scale: u64) -> Option<u64> {
    #[expect(
        clippy::cast_precision_loss,
        reason = "scale is a power of ten up to 10^17; exactness is checked below"
    )]
    let scale_f = scale as f64;
    let scaled = (v * scale_f).round();
    // v is finite and non-negative, so scaled is too: no NaN to compare.
    if scaled >= 9_007_199_254_740_992.0 {
        return None;
    }
    #[expect(
        clippy::cast_possible_truncation,
        clippy::cast_sign_loss,
        reason = "0 <= scaled < 2^53, a whole number"
    )]
    let units = scaled as u64;
    #[expect(clippy::cast_precision_loss, reason = "units < 2^53, exact in f64")]
    let back = units as f64 / scale_f;
    (back.to_bits() == v.to_bits()).then_some(units)
}

/// [`TextBuilder::digits`] for a width over 20, padding beyond any `u64`'s digits.
#[cold]
fn wide_digits(out: &mut Vec<u8>, n: u64, width: usize) {
    let mut buffer = itoa::Buffer::new();
    let text = buffer.format(n);
    for _ in text.len()..width {
        out.push(b'0');
    }
    out.extend_from_slice(text.as_bytes());
}

/// `True` or `False`. Four bytes are always copied (`True` or `Fals`) and the `e` of `False`
/// is pushed after: two fixed-size stores. Written as one copy of 4 or 5 bytes, the compiler
/// may merge the two into a single copy of variable length, a `memcpy` call per value, which
/// it did in `CsvFormat::row` once this crate had more callers (callgrind, 0.7.0).
// HOT PATH: per value.
#[expect(
    clippy::inline_always,
    reason = "the row writer is fast only when every cell inlines into the caller's row: measured in aero1553, where without it a 46-column row ran 1.20x its hand-written writer"
)]
#[inline(always)]
fn write_bool(out: &mut Vec<u8>, b: bool) {
    out.extend_from_slice(if b { b"True" } else { b"Fals" });
    if !b {
        out.push(b'e');
    }
}

/// Whether `bytes` hold a `,`, `"`, CR or LF, and so must be quoted.
// HOT PATH: per value.
#[expect(
    clippy::inline_always,
    reason = "the row writer is fast only when every cell inlines into the caller's row: measured in aero1553, where without it a 46-column row ran 1.20x its hand-written writer"
)]
#[inline(always)]
fn needs_quoting(bytes: &[u8]) -> bool {
    bytes
        .iter()
        .any(|&b| matches!(b, b',' | b'"' | b'\r' | b'\n'))
}

/// Quotes the text from `start` to the end of `out` in place: a double quote either side and
/// each inner one doubled, as [`text`] writes it.
#[cold]
fn quote_in_place(out: &mut Vec<u8>, start: usize) {
    let cell: Vec<u8> = out.split_off(start);
    out.push(b'"');
    for b in cell {
        if b == b'"' {
            out.push(b'"');
        }
        out.push(b);
    }
    out.push(b'"');
}

/// Writes a table as CSV in one or more batches: the header first, then each batch's rows.
pub struct CsvWriter<W: Write> {
    out: W,
    format: CsvFormat,
    buffer: Vec<u8>,
    rows: u64,
}

impl<W: Write> CsvWriter<W> {
    /// Starts a table with these columns: writes the encoding's byte-order mark, if any, and
    /// the header.
    ///
    /// # Errors
    ///
    /// [`TableError::NoColumns`] for no columns, or the output failed.
    pub fn new(mut out: W, schema: Vec<Field>, options: CsvOptions) -> Result<Self, TableError> {
        let format = CsvFormat::new(schema, options)?;
        let mut buffer = Vec::with_capacity(FLUSH_AT + 4096);
        format.start(&mut buffer);
        out.write_all(&buffer)?;
        buffer.clear();
        Ok(CsvWriter {
            out,
            format,
            buffer,
            rows: 0,
        })
    }

    /// Appends `batch`'s rows.
    ///
    /// # Errors
    ///
    /// [`TableError::SchemaMismatch`] if its columns differ from the header's, or the output
    /// failed.
    pub fn write(&mut self, batch: &Batch) -> Result<(), TableError> {
        let found = batch.schema();
        if found != self.format.schema {
            return Err(TableError::SchemaMismatch {
                expected: self.format.schema.clone(),
                found,
            });
        }
        let columns = batch.columns();
        // The buffer is moved out of `self` while rows are formatted into it, and the rows are
        // formatted by a function of their own that sees only the format, the columns and the
        // buffer: it compiles to the same loop wherever the writer lives (a local, behind a
        // reference, in a caller's closure), with nothing to reload through `self`.
        let mut out = std::mem::take(&mut self.buffer);
        let rows = batch.rows();
        let mut row = 0;
        while row < rows {
            row = if self.format.formatted {
                fill_formatted(&self.format, columns, row, rows, &mut out)
            } else {
                fill(&self.format, columns, row, rows, &mut out)
            };
            if out.len() >= FLUSH_AT {
                if let Err(e) = self.out.write_all(&out) {
                    self.buffer = out;
                    return Err(e.into());
                }
                out.clear();
            }
        }
        self.buffer = out;
        self.rows += batch.rows() as u64;
        Ok(())
    }

    /// Appends one row of borrowed values (see [`CsvFormat::row`]).
    ///
    /// # Errors
    ///
    /// As [`CsvFormat::row`], or the output failed.
    // HOT PATH: per row.
    pub fn write_row(&mut self, row: &[Value<'_>]) -> Result<(), TableError> {
        self.format.row(&mut self.buffer, row)?;
        self.rows += 1;
        if self.buffer.len() >= FLUSH_AT {
            self.out.write_all(&self.buffer)?;
            self.buffer.clear();
        }
        Ok(())
    }

    /// Appends one row written a cell at a time by `cells` (see [`RowWriter`]).
    ///
    /// # Errors
    ///
    /// As [`RowWriter::finish`] (the row is not written), or the output failed.
    // HOT PATH: per row.
    pub fn write_row_with(
        &mut self,
        cells: impl FnOnce(&mut RowWriter<'_, '_>),
    ) -> Result<(), TableError> {
        let mut row = self.format.begin_row(&mut self.buffer);
        cells(&mut row);
        row.finish()?;
        self.rows += 1;
        if self.buffer.len() >= FLUSH_AT {
            self.out.write_all(&self.buffer)?;
            self.buffer.clear();
        }
        Ok(())
    }

    /// Writes what is buffered and returns the output, and how many rows were written.
    ///
    /// # Errors
    ///
    /// The output failed.
    pub fn finish(mut self) -> Result<(W, u64), TableError> {
        self.out.write_all(&self.buffer)?;
        self.out.flush()?;
        Ok((self.out, self.rows))
    }
}

/// Formats rows `row..rows` of `columns` into `out`, stopping after the row that takes `out`
/// to [`FLUSH_AT`] bytes or more. Returns the next row to format.
// HOT PATH: per row. Kept out of line so that the loop is compiled once, the same for every
// caller, with the format and the buffer as its own arguments.
#[inline(never)]
fn fill(
    format: &CsvFormat,
    columns: &[Column],
    mut row: usize,
    rows: usize,
    out: &mut Vec<u8>,
) -> usize {
    // HOT PATH: begin. One line per row, one field per value, into the reused buffer.
    while row < rows {
        out.reserve(columns.len() * 24 + 2);
        let line_start = out.len();
        for (i, column) in columns.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            format.cell(out, column, row);
        }
        format.end_row(out, line_start);
        row += 1;
        if out.len() >= FLUSH_AT {
            break;
        }
    }
    // HOT PATH: end.
    row
}

/// [`fill`] for a table with [`CellFormat`]s. Its own function, so a table without them runs
/// exactly the loop it ran before formats existed.
// HOT PATH: per row.
#[inline(never)]
fn fill_formatted(
    format: &CsvFormat,
    columns: &[Column],
    mut row: usize,
    rows: usize,
    out: &mut Vec<u8>,
) -> usize {
    // HOT PATH: begin.
    while row < rows {
        out.reserve(columns.len() * 24 + 2);
        let line_start = out.len();
        for (i, (column, &cell_format)) in columns.iter().zip(&format.formats).enumerate() {
            if i > 0 {
                out.push(b',');
            }
            format.formatted_cell(out, cell_format, column, row);
        }
        format.end_row(out, line_start);
        row += 1;
        if out.len() >= FLUSH_AT {
            break;
        }
    }
    // HOT PATH: end.
    row
}

/// Appends an integer's decimal digits, formatted on the stack by `itoa`.
impl<W: Write> BatchWriter for CsvWriter<W> {
    fn write_batch(&mut self, batch: &Batch) -> Result<(), TableError> {
        self.write(batch)
    }
}

// HOT PATH: per value.
#[inline]
fn integer(out: &mut Vec<u8>, v: impl itoa::Integer) {
    out.extend_from_slice(itoa::Buffer::new().format(v).as_bytes());
}

#[inline]
fn infinity(out: &mut Vec<u8>, positive: bool) {
    out.extend_from_slice(if positive { b"inf" } else { b"-inf" });
}

/// Appends `s`, in double quotes (each inner quote doubled) only if it holds a comma, a
/// double quote, a carriage return or a line feed: Python's `csv.QUOTE_MINIMAL`, pandas'
/// default.
// HOT PATH: per value.
#[inline]
fn text(out: &mut Vec<u8>, s: &str) {
    let bytes = s.as_bytes();
    if !bytes
        .iter()
        .any(|&b| matches!(b, b',' | b'"' | b'\r' | b'\n'))
    {
        out.extend_from_slice(bytes);
        return;
    }
    out.push(b'"');
    for &b in bytes {
        if b == b'"' {
            out.push(b'"');
        }
        out.push(b);
    }
    out.push(b'"');
}

/// Writes `batch`, header and rows, to `out`. Returns the rows written.
///
/// # Errors
///
/// The output failed.
pub fn write_to(out: impl Write, batch: &Batch, options: &CsvOptions) -> Result<u64, TableError> {
    let mut w = CsvWriter::new(out, batch.schema(), options.clone())?;
    w.write(batch)?;
    Ok(w.finish()?.1)
}

/// Writes `batch` to the file at `path`, whole or not at all (see
/// [`tablekit::write_atomically`]); an existing file is replaced. Returns the rows written.
///
/// # Errors
///
/// The file could not be written.
pub fn write_file(path: &Path, batch: &Batch, options: &CsvOptions) -> Result<u64, TableError> {
    write_atomically(path, |out| write_to(out, batch, options))
}

/// Writes every batch of `source`, header first, to `out`. Returns the rows written. One
/// batch is in memory at a time.
///
/// # Errors
///
/// Whatever the source fails with, or the output failed.
pub fn write_source<S: Source>(
    out: impl Write,
    source: &mut S,
    options: &CsvOptions,
) -> Result<u64, S::Error> {
    let mut w = CsvWriter::new(out, source.schema(), options.clone())?;
    let rows = copy(source, &mut w)?;
    w.finish()?;
    Ok(rows)
}

/// Writes every batch of `source` to the file at `path`, whole or not at all (see
/// [`tablekit::write_atomically`]); an existing file is replaced. Returns the rows written.
///
/// # Errors
///
/// Whatever the source fails with, or the file could not be written.
pub fn write_source_file<S: Source>(
    path: &Path,
    source: &mut S,
    options: &CsvOptions,
) -> Result<u64, S::Error> {
    write_atomically(path, |out| write_source(out, source, options))
}
