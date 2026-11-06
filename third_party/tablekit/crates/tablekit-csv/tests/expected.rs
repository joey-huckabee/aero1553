//! tablekit-csv writes, byte for byte, what pandas wrote for the same tables
//! (`testdata/expected/csv/make.py`).

use std::path::{Path, PathBuf};

use tablekit::{Batch, Column, DataType, TableError, Value, Values};
use tablekit_csv::{CsvFormat, CsvOptions, CsvWriter, Encoding, LineEnding, write_file, write_to};

fn expected(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../testdata/expected/csv")
        .join(name)
}

/// The table `make.py` gives pandas.
fn table() -> Batch {
    Batch::new(vec![
        Column::new("time", Values::F64(vec![0.5, 1e-05, 1e16, 123.0])),
        Column::new("flag", Values::Bool(vec![true, false, true, false])),
        Column::new("small", Values::I8(vec![-128, 0, 127, 5])),
        Column::new("count", Values::U64(vec![u64::MAX, 0, 1, 2])),
        Column::new("offset", Values::I64(vec![i64::MIN, i64::MAX, -1, 0])),
        Column::with_missing(
            "level",
            Values::F32(vec![0.1, 1e-4, 3.4e38, 0.0]),
            vec![true, true, true, false],
        )
        .unwrap(),
        Column::new(
            "edge",
            Values::F64(vec![f64::INFINITY, f64::NEG_INFINITY, 0.0, -0.0]),
        ),
        Column::new(
            "note",
            Values::Text(vec!["plain", "a, b", "say \"hi\"", "two\nlines"].into()),
        ),
        Column::with_missing(
            "Mode",
            Values::Text(vec!["On", "", "Off", ""].into()),
            vec![true, false, true, true],
        )
        .unwrap(),
        Column::new("Pos.X", Values::I16(vec![1, -2, 3, -4])),
    ])
    .unwrap()
}

fn check(batch: &Batch, options: &CsvOptions, file: &str) {
    let mut out = Vec::new();
    write_to(&mut out, batch, options).unwrap();
    let want = std::fs::read(expected(file)).unwrap();
    assert_eq!(
        String::from_utf8_lossy(&out),
        String::from_utf8_lossy(&want),
        "{file}"
    );
    assert_eq!(out, want, "{file}");
}

#[test]
fn default_options_write_what_pandas_writes() {
    check(&table(), &CsvOptions::default(), "table.csv");
}

#[test]
fn utf_8_sig_starts_with_a_byte_order_mark() {
    let options = CsvOptions {
        encoding: Encoding::Utf8Sig,
        ..CsvOptions::default()
    };
    check(&table(), &options, "table-utf-8-sig.csv");
}

#[test]
fn encodings_are_named_as_python_names_them() {
    for name in ["utf-8", "UTF-8", "utf8", "utf_8", "U8"] {
        assert_eq!(name.parse::<Encoding>(), Ok(Encoding::Utf8), "{name}");
    }
    for name in ["utf-8-sig", "UTF-8-SIG", "utf_8_sig", "utf8sig"] {
        assert_eq!(name.parse::<Encoding>(), Ok(Encoding::Utf8Sig), "{name}");
    }
    assert_eq!(
        "latin-1".parse::<Encoding>(),
        Err("tablekit writes the encodings \"utf-8\" and \"utf-8-sig\", not \"latin-1\"".into())
    );
    assert_eq!(Encoding::Utf8Sig.name(), "utf-8-sig");
    assert_eq!(Encoding::default(), Encoding::Utf8);
}

#[test]
fn rows_write_what_batches_write() {
    let whole = table();
    let mut format = CsvFormat::new(whole.schema(), CsvOptions::default()).unwrap();
    let mut out = Vec::new();
    format.start(&mut out);
    let mut row = Vec::new();
    for r in 0..whole.rows() {
        row.clear();
        row.extend(whole.columns().iter().map(|c| c.value(r)));
        format.row(&mut out, &row).unwrap();
    }
    assert_eq!(out, std::fs::read(expected("table.csv")).unwrap());

    let mut w = CsvWriter::new(Vec::new(), whole.schema(), CsvOptions::default()).unwrap();
    for r in 0..whole.rows() {
        let row: Vec<Value<'_>> = whole.columns().iter().map(|c| c.value(r)).collect();
        w.write_row(&row).unwrap();
    }
    let (written, rows) = w.finish().unwrap();
    assert_eq!(
        (written, rows),
        (std::fs::read(expected("table.csv")).unwrap(), 4)
    );
}

#[test]
fn a_row_must_fit_the_columns() {
    let fields = vec![
        tablekit::Field {
            name: "n".into(),
            data_type: DataType::U32,
        },
        tablekit::Field {
            name: "t".into(),
            data_type: DataType::Text,
        },
    ];
    let mut format = CsvFormat::new(fields, CsvOptions::default()).unwrap();
    let mut out = Vec::new();
    let e = format.row(&mut out, &[Value::U32(1)]).unwrap_err();
    assert!(
        matches!(
            e,
            TableError::RowLength {
                expected: 2,
                found: 1
            }
        ),
        "{e}"
    );
    let e = format
        .row(&mut out, &[Value::U64(1), Value::Text("a")])
        .unwrap_err();
    assert!(
        matches!(e, TableError::ValueType { ref column, expected: DataType::U32, found: DataType::U64 } if column == "n"),
        "{e}"
    );
    assert!(out.is_empty(), "a refused row appends nothing");
    format
        .row(&mut out, &[Value::Missing, Value::Text("a, b")])
        .unwrap();
    assert_eq!(out, b",\"a, b\"\r\n");
}

#[test]
fn na_rep_for_missing_values_and_nan() {
    let options = CsvOptions {
        na_rep: "NA".into(),
        ..CsvOptions::default()
    };
    check(&table(), &options, "table-na.csv");
}

#[test]
fn line_feeds_alone() {
    let options = CsvOptions {
        line_ending: LineEnding::Lf,
        ..CsvOptions::default()
    };
    check(&table(), &options, "table-lf.csv");
}

#[test]
fn one_empty_field_is_written_as_two_quotes() {
    let batch = Batch::new(vec![
        Column::with_missing(
            "x",
            Values::Text(vec!["a", "", ""].into()),
            vec![true, true, false],
        )
        .unwrap(),
    ])
    .unwrap();
    check(&batch, &CsvOptions::default(), "one-column.csv");
}

#[test]
fn integers_stay_integers_when_some_are_missing() {
    // pandas would write 1.0 and 3.0 here; see docs/CSV-FORMAT.md.
    let batch = Batch::new(vec![
        Column::with_missing("n", Values::U32(vec![1, 0, 3]), vec![true, false, true]).unwrap(),
        Column::with_missing(
            "b",
            Values::Bool(vec![true, false, false]),
            vec![true, true, false],
        )
        .unwrap(),
    ])
    .unwrap();
    let mut out = Vec::new();
    write_to(&mut out, &batch, &CsvOptions::default()).unwrap();
    assert_eq!(out, b"n,b\r\n1,True\r\n,False\r\n3,\r\n");
}

#[test]
fn several_batches_make_one_table() {
    let whole = table();
    let mut w = CsvWriter::new(Vec::new(), whole.schema(), CsvOptions::default()).unwrap();
    w.write(&whole).unwrap();
    w.write(&whole).unwrap();
    let (out, rows) = w.finish().unwrap();
    assert_eq!(rows, 8);
    let one = std::fs::read(expected("table.csv")).unwrap();
    let header = one.iter().position(|&b| b == b'\n').unwrap() + 1;
    assert_eq!(out, [&one[..], &one[header..]].concat());

    let other = Batch::new(vec![Column::new("time", Values::F64(vec![1.0]))]).unwrap();
    let mut w = CsvWriter::new(Vec::new(), whole.schema(), CsvOptions::default()).unwrap();
    assert!(matches!(
        w.write(&other),
        Err(TableError::SchemaMismatch { .. })
    ));
}

#[test]
fn files_are_written_whole_or_not_at_all() {
    let dir = std::env::temp_dir().join(format!("tablekit-csv-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("out.csv");
    assert_eq!(
        write_file(&path, &table(), &CsvOptions::default()).unwrap(),
        4
    );
    assert_eq!(
        std::fs::read(&path).unwrap(),
        std::fs::read(expected("table.csv")).unwrap()
    );
    assert!(!tablekit::temporary_path(&path).exists());

    // A failed write leaves the earlier file as it was, and no temporary file.
    let failed = tablekit::write_atomically(&path, |out| {
        std::io::Write::write_all(out, b"partial")?;
        Err::<(), _>(TableError::NoColumns)
    });
    assert!(failed.is_err());
    assert_eq!(
        std::fs::read(&path).unwrap(),
        std::fs::read(expected("table.csv")).unwrap()
    );
    assert!(!tablekit::temporary_path(&path).exists());
    std::fs::remove_dir_all(&dir).unwrap();
}

/// Gives `table()` one row at a time.
struct Rows {
    whole: Batch,
    next: usize,
}

impl tablekit::Source for Rows {
    type Error = TableError;

    fn schema(&self) -> Vec<tablekit::Field> {
        self.whole.schema()
    }

    fn next_batch(&mut self) -> Result<Option<Batch>, TableError> {
        if self.next == self.whole.rows() {
            return Ok(None);
        }
        let r = self.next;
        self.next += 1;
        let columns = self
            .whole
            .columns()
            .iter()
            .map(|c| {
                let v = c.value(r);
                let values = match c.values() {
                    Values::Bool(x) => Values::Bool(vec![x[r]]),
                    Values::I8(x) => Values::I8(vec![x[r]]),
                    Values::I16(x) => Values::I16(vec![x[r]]),
                    Values::I64(x) => Values::I64(vec![x[r]]),
                    Values::U64(x) => Values::U64(vec![x[r]]),
                    Values::F32(x) => Values::F32(vec![x[r]]),
                    Values::F64(x) => Values::F64(vec![x[r]]),
                    Values::Text(x) => Values::Text(vec![x.get(r).unwrap_or_default()].into()),
                    other => panic!("not in table(): {other:?}"),
                };
                Column::with_missing(c.name(), values, vec![v != Value::Missing]).unwrap()
            })
            .collect();
        Ok(Some(Batch::new(columns)?))
    }
}

#[test]
fn a_source_written_batch_by_batch_is_the_whole_table() {
    let dir = std::env::temp_dir().join(format!("tablekit-csv-source-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("rows.csv");
    let mut rows = Rows {
        whole: table(),
        next: 0,
    };
    assert_eq!(
        tablekit_csv::write_source_file(&path, &mut rows, &CsvOptions::default()).unwrap(),
        4
    );
    assert_eq!(
        std::fs::read(&path).unwrap(),
        std::fs::read(expected("table.csv")).unwrap()
    );
    std::fs::remove_dir_all(&dir).unwrap();
}
