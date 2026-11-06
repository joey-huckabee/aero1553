//! Cell formats and the row writer. Every way in -- a whole batch, `CsvFormat::row`,
//! `RowWriter` and `CsvWriter::write_row_with` -- writes, byte for byte, what pandas wrote
//! after Python formatted the same values (`testdata/expected/csv/make.py`,
//! `table-formats.csv`), and the row writer writes what `CsvFormat::row` writes for any
//! values of every type.

#![expect(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    reason = "random bits are cut to every width on purpose, to cover each type's whole range"
)]

use std::path::Path;

use tablekit::{Batch, Column, DataType, Field, TableError, Value, Values};
use tablekit_csv::{CellFormat, CsvFormat, CsvOptions, CsvWriter, RowWriter, write_to};

fn expected() -> Vec<u8> {
    std::fs::read(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../testdata/expected/csv/table-formats.csv"),
    )
    .unwrap()
}

/// `None` marks a missing value, as in `make.py`.
fn present<T: Copy + Default>(v: &[Option<T>]) -> (Vec<T>, Vec<bool>) {
    (
        v.iter().map(|x| x.unwrap_or_default()).collect(),
        v.iter().map(Option::is_some).collect(),
    )
}

const WORD: [Option<u16>; 10] = [
    Some(0x0400),
    Some(0),
    Some(0xFFFF),
    Some(0x2F),
    None,
    Some(1),
    Some(0xABCD),
    Some(0x10),
    Some(0x8000),
    Some(0x7FFF),
];
const BYTE: [Option<u8>; 10] = [
    Some(255),
    Some(0),
    Some(10),
    Some(1),
    Some(128),
    None,
    Some(15),
    Some(16),
    Some(254),
    Some(3),
];
const ADDR: [Option<u32>; 10] = [
    Some(0xDEAD_BEEF),
    Some(1),
    Some(0),
    Some(0xABC),
    Some(0xFFFF_FFFF),
    Some(0x10000),
    Some(7),
    None,
    Some(0x8000_0000),
    Some(2),
];
const BIG: [Option<u64>; 10] = [
    Some(u64::MAX),
    Some(0),
    Some(0x1234),
    Some(1 << 40),
    Some(0xFFFF),
    Some(0x10000),
    None,
    Some(5),
    Some(0x00AB_CDEF),
    Some(1),
];
const DELTA: [f64; 10] = [
    0.0,
    1.5e-05,
    123.456_789,
    -0.0,
    1.000_000_5,
    5e-07,
    -1e-09,
    1e20,
    f64::NAN,
    f64::INFINITY,
];
const LEVEL: [f32; 10] = [0.1, 1e-4, 3.4e38, -2.5, 0.0, 1.5, 7.0, 1e-3, 2.0, 0.5];
const N: [i32; 10] = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10];

fn options() -> CsvOptions {
    CsvOptions {
        cell_formats: vec![
            ("word".into(), CellFormat::HexUpper(4)),
            ("byte".into(), CellFormat::HexUpper(2)),
            ("addr".into(), CellFormat::HexUpper(8)),
            ("big".into(), CellFormat::HexLower(4)),
            ("delta".into(), CellFormat::Fixed(6)),
            ("level".into(), CellFormat::Fixed(3)),
        ],
        ..CsvOptions::default()
    }
}

fn batch() -> Batch {
    let (word, word_p) = present(&WORD);
    let (byte, byte_p) = present(&BYTE);
    let (addr, addr_p) = present(&ADDR);
    let (big, big_p) = present(&BIG);
    Batch::new(vec![
        Column::with_missing("word", Values::U16(word), word_p).unwrap(),
        Column::with_missing("byte", Values::U8(byte), byte_p).unwrap(),
        Column::with_missing("addr", Values::U32(addr), addr_p).unwrap(),
        Column::with_missing("big", Values::U64(big), big_p).unwrap(),
        Column::new("delta", Values::F64(DELTA.to_vec())),
        Column::new("level", Values::F32(LEVEL.to_vec())),
        Column::new("n", Values::I32(N.to_vec())),
    ])
    .unwrap()
}

fn row_values(i: usize) -> Vec<Value<'static>> {
    fn or_missing<T>(v: Option<T>, f: impl Fn(T) -> Value<'static>) -> Value<'static> {
        v.map_or(Value::Missing, f)
    }
    vec![
        or_missing(WORD[i], Value::U16),
        or_missing(BYTE[i], Value::U8),
        or_missing(ADDR[i], Value::U32),
        or_missing(BIG[i], Value::U64),
        Value::F64(DELTA[i]),
        Value::F32(LEVEL[i]),
        Value::I32(N[i]),
    ]
}

fn write_cells(r: &mut RowWriter<'_, '_>, i: usize) {
    match WORD[i] {
        Some(v) => r.hex::<4>(v),
        None => r.missing(),
    }
    match BYTE[i] {
        Some(v) => r.hex::<2>(v),
        None => r.missing(),
    }
    match ADDR[i] {
        Some(v) => r.hex::<8>(v),
        None => r.missing(),
    }
    match BIG[i] {
        Some(v) => r.hex_lower::<4>(v),
        None => r.missing(),
    }
    r.fixed::<6>(DELTA[i]);
    r.fixed_f32::<3>(LEVEL[i]);
    r.i32(N[i]);
}

#[test]
fn a_batch_writes_what_python_formats() {
    let mut out = Vec::new();
    write_to(&mut out, &batch(), &options()).unwrap();
    assert_eq!(
        String::from_utf8_lossy(&out),
        String::from_utf8_lossy(&expected())
    );
}

#[test]
fn row_writes_what_python_formats() {
    let b = batch();
    let mut format = CsvFormat::new(b.schema(), options()).unwrap();
    let mut out = Vec::new();
    format.start(&mut out);
    for i in 0..10 {
        format.row(&mut out, &row_values(i)).unwrap();
    }
    assert_eq!(
        String::from_utf8_lossy(&out),
        String::from_utf8_lossy(&expected())
    );
}

#[test]
fn the_row_writer_writes_what_python_formats() {
    let format = CsvFormat::new(batch().schema(), options()).unwrap();
    let mut out = Vec::new();
    format.start(&mut out);
    for i in 0..10 {
        let mut r = format.begin_row(&mut out);
        write_cells(&mut r, i);
        r.finish().unwrap();
    }
    assert_eq!(
        String::from_utf8_lossy(&out),
        String::from_utf8_lossy(&expected())
    );
}

#[test]
fn csv_writer_rows_write_what_python_formats() {
    let mut w = CsvWriter::new(Vec::new(), batch().schema(), options()).unwrap();
    for i in 0..10 {
        w.write_row_with(|r| write_cells(r, i)).unwrap();
    }
    let (out, rows) = w.finish().unwrap();
    assert_eq!(rows, 10);
    assert_eq!(
        String::from_utf8_lossy(&out),
        String::from_utf8_lossy(&expected())
    );
}

/// A small deterministic generator: the same values on every run.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn pick<T: Copy>(&mut self, from: &[T]) -> T {
        from[usize::try_from(self.next() % from.len() as u64).unwrap()]
    }
}

/// Floats that exercise every branch: whole units, ties, negatives, both zeros, huge, tiny,
/// subnormal, not finite, and arbitrary bits.
fn float(rng: &mut Rng) -> f64 {
    let special = [
        0.0,
        -0.0,
        0.5,
        -0.5,
        1.5,
        2.5,
        0.125,
        1e-6,
        1.5e-5,
        5e-7,
        1.000_000_5,
        123.456_789,
        -123.456_789,
        1e15,
        9_007_199_254.740_993,
        1e20,
        -1e300,
        5e-324,
        f64::MIN_POSITIVE,
        f64::MAX,
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
    ];
    match rng.next() % 4 {
        0 => rng.pick(&special),
        // Whole microseconds, as an interval measured in them is.
        1 => (rng.next() % 10_000_000_000) as f64 / 1e6,
        2 => f64::from_bits(rng.next()),
        _ => (rng.next() % 2_000_000) as f64 / 1e3 - 1000.0,
    }
}

const TEXTS: [&str; 7] = [
    "",
    "plain",
    "a, b",
    "say \"hi\"",
    "two\nlines",
    "cr\r",
    "\u{e9}t\u{e9}",
];

/// The columns of the random-row test: every type, then every format.
fn every_column() -> Vec<Field> {
    let field = |name: &str, data_type| Field {
        name: name.into(),
        data_type,
    };
    vec![
        field("flag", DataType::Bool),
        field("i8", DataType::I8),
        field("i16", DataType::I16),
        field("i32", DataType::I32),
        field("i64", DataType::I64),
        field("u8", DataType::U8),
        field("u16", DataType::U16),
        field("u32", DataType::U32),
        field("u64", DataType::U64),
        field("f32", DataType::F32),
        field("f64", DataType::F64),
        field("text", DataType::Text),
        field("hex16", DataType::U16),
        field("hex64", DataType::U64),
        field("hex8", DataType::U8),
        field("hex32", DataType::U32),
        field("fix6", DataType::F64),
        field("fix0", DataType::F64),
        field("fix17", DataType::F64),
        field("fix2f32", DataType::F32),
        field("built", DataType::Text),
    ]
}

/// One random row for [`every_column`], about one cell in eight missing.
fn random_row(rng: &mut Rng) -> Vec<Value<'static>> {
    let bits = rng.next();
    let wide = rng.next();
    let mut values = vec![
        Value::Bool(bits & 1 == 1),
        Value::I8(bits as i8),
        Value::I16((bits >> 8) as i16),
        Value::I32((bits >> 16) as i32),
        Value::I64(wide as i64),
        Value::U8(bits as u8),
        Value::U16((bits >> 8) as u16),
        Value::U32((bits >> 16) as u32),
        Value::U64(wide),
        Value::F32(float(rng) as f32),
        Value::F64(float(rng)),
        Value::Text(rng.pick(&TEXTS)),
        Value::U16((bits >> 24) as u16),
        Value::U64(wide.rotate_left(17)),
        Value::U8((bits >> 40) as u8),
        Value::U32((bits >> 32) as u32),
        Value::F64(float(rng)),
        Value::F64(float(rng)),
        Value::F64(float(rng)),
        Value::F32(float(rng) as f32),
        Value::Text(rng.pick(&TEXTS)),
    ];
    let missing = rng.next();
    for (col, value) in values.iter_mut().enumerate() {
        if (missing >> (col * 3)).trailing_zeros() >= 3 {
            *value = Value::Missing;
        }
    }
    values
}

/// Writes `values` (a [`random_row`]) through the row writer's method for each column.
fn write_by_cell(cells: &mut RowWriter<'_, '_>, values: &[Value<'_>]) {
    for (col, &value) in values.iter().enumerate() {
        match (col, value) {
            (_, Value::Missing) => cells.missing(),
            (0, Value::Bool(v)) => cells.bool(v),
            (1, Value::I8(v)) => cells.i8(v),
            (2, Value::I16(v)) => cells.i16(v),
            (3, Value::I32(v)) => cells.i32(v),
            (4, Value::I64(v)) => cells.i64(v),
            (5, Value::U8(v)) => cells.u8(v),
            (6, Value::U16(v)) => cells.u16(v),
            (7, Value::U32(v)) => cells.u32(v),
            (8, Value::U64(v)) => cells.u64(v),
            (9, Value::F32(v)) => cells.f32(v),
            (10, Value::F64(v)) => cells.f64(v),
            (11, Value::Text(v)) => cells.text(v),
            (12, Value::U16(v)) => cells.hex::<4>(v),
            (13, Value::U64(v)) => cells.hex_lower::<16>(v),
            (14, Value::U8(v)) => cells.hex::<1>(v),
            (15, Value::U32(v)) => cells.hex_lower::<3>(v),
            (16, Value::F64(v)) => cells.fixed::<6>(v),
            (17, Value::F64(v)) => cells.fixed::<0>(v),
            (18, Value::F64(v)) => cells.fixed::<17>(v),
            (19, Value::F32(v)) => cells.fixed_f32::<2>(v),
            // Built in place, quoted afterwards if it needs it.
            (20, Value::Text(v)) => cells.text_with(|t| t.push_str(v)),
            other => panic!("unexpected {other:?}"),
        }
    }
}

/// Every column type, with and without a format, against `CsvFormat::row`: the same bytes
/// for 20,000 random rows, missing values included, with an empty and a non-empty `na_rep`.
#[test]
fn the_row_writer_writes_what_row_writes() {
    for na_rep in ["", "NA"] {
        let options = CsvOptions {
            na_rep: na_rep.into(),
            cell_formats: vec![
                ("hex16".into(), CellFormat::HexUpper(4)),
                ("hex64".into(), CellFormat::HexLower(16)),
                ("hex8".into(), CellFormat::HexUpper(1)),
                ("hex32".into(), CellFormat::HexLower(3)),
                ("fix6".into(), CellFormat::Fixed(6)),
                ("fix0".into(), CellFormat::Fixed(0)),
                ("fix17".into(), CellFormat::Fixed(17)),
                ("fix2f32".into(), CellFormat::Fixed(2)),
            ],
            ..CsvOptions::default()
        };
        let mut format = CsvFormat::new(every_column(), options).unwrap();
        let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
        let (mut by_row, mut by_cell) = (Vec::new(), Vec::new());
        for _ in 0..20_000 {
            let values = random_row(&mut rng);
            format.row(&mut by_row, &values).unwrap();
            let mut cells = format.begin_row(&mut by_cell);
            write_by_cell(&mut cells, &values);
            cells.finish().unwrap();
            assert_eq!(by_cell.len(), by_row.len(), "row {values:?}");
        }
        assert_eq!(
            String::from_utf8_lossy(&by_cell),
            String::from_utf8_lossy(&by_row),
            "na_rep {na_rep:?}"
        );
    }
}

/// Every fixed value against Rust's own `{:.N}`, which is exact.
#[test]
fn fixed_matches_core_fmt() {
    let field = |name: &str| Field {
        name: name.into(),
        data_type: DataType::F64,
    };
    let options = CsvOptions {
        cell_formats: vec![
            ("d3".into(), CellFormat::Fixed(3)),
            ("d6".into(), CellFormat::Fixed(6)),
            ("d9".into(), CellFormat::Fixed(9)),
            ("d1".into(), CellFormat::Fixed(1)),
        ],
        ..CsvOptions::default()
    };
    let format = CsvFormat::new(
        vec![field("d3"), field("d6"), field("d9"), field("d1")],
        options,
    )
    .unwrap();
    let mut rng = Rng(42);
    for _ in 0..200_000 {
        let x = float(&mut rng);
        if !x.is_finite() {
            continue;
        }
        let mut out = Vec::new();
        let mut w = format.begin_row(&mut out);
        w.fixed::<3>(x);
        w.fixed::<6>(x);
        w.fixed::<9>(x);
        w.fixed::<1>(x);
        w.finish().unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            format!("{x:.3},{x:.6},{x:.9},{x:.1}\r\n"),
            "{x:e}"
        );
    }
}

#[test]
fn built_text_is_quoted_when_it_needs_to_be() {
    let schema = vec![Field {
        name: "t".into(),
        data_type: DataType::Text,
    }];
    let format = CsvFormat::new(schema, CsvOptions::default()).unwrap();
    let built = |build: &dyn Fn(&mut tablekit_csv::TextBuilder<'_>)| {
        let mut out = Vec::new();
        let mut w = format.begin_row(&mut out);
        w.text_with(|t| build(t));
        w.finish().unwrap();
        String::from_utf8(out).unwrap()
    };
    assert_eq!(
        built(&|t| {
            t.digits(7, 3);
            t.push(b':');
            t.digits(123_456, 2);
        }),
        "007:123456\r\n"
    );
    assert_eq!(
        built(&|t| {
            t.digits(1, 0);
            t.push(b',');
            t.digits(2, 1);
        }),
        "\"1,2\"\r\n"
    );
    assert_eq!(
        built(&|t| {
            t.push(b'"');
        }),
        "\"\"\"\"\r\n"
    );
    assert_eq!(
        built(&|t| {
            t.push(b'\n');
        }),
        "\"\n\"\r\n"
    );
    assert_eq!(
        built(&|t| {
            t.push(0xE9);
        }),
        "\u{FFFD}\r\n"
    );
    assert_eq!(
        built(&|t| {
            t.digits(u64::MAX, 0);
        }),
        "18446744073709551615\r\n"
    );
}

#[test]
fn text_with_is_quoted_like_text() {
    let schema = vec![Field {
        name: "t".into(),
        data_type: DataType::Text,
    }];
    let format = CsvFormat::new(schema, CsvOptions::default()).unwrap();
    for s in ["plain", "a, b", "say \"hi\"", "two\nlines", "", "\"", ","] {
        let (mut a, mut b) = (Vec::new(), Vec::new());
        let mut w = format.begin_row(&mut a);
        w.text(s);
        w.finish().unwrap();
        let mut w = format.begin_row(&mut b);
        w.text_with(|t| t.push_str(s));
        w.finish().unwrap();
        assert_eq!(a, b, "{s:?}");
    }
}

fn formats_error(cell_formats: Vec<(String, CellFormat)>) -> String {
    let schema = vec![
        Field {
            name: "w".into(),
            data_type: DataType::U16,
        },
        Field {
            name: "x".into(),
            data_type: DataType::F64,
        },
        Field {
            name: "s".into(),
            data_type: DataType::I32,
        },
    ];
    let options = CsvOptions {
        cell_formats,
        ..CsvOptions::default()
    };
    match CsvFormat::new(schema, options) {
        Err(TableError::Format(message)) => message,
        other => panic!("expected a Format error, got {other:?}"),
    }
}

#[test]
fn unsuitable_cell_formats_are_refused() {
    let f = |name: &str, format| vec![(name.to_string(), format)];
    assert!(formats_error(f("nope", CellFormat::HexUpper(4))).contains("no such column"));
    assert!(
        formats_error(vec![
            ("w".into(), CellFormat::HexUpper(4)),
            ("w".into(), CellFormat::HexUpper(2)),
        ])
        .contains("twice")
    );
    for (name, format) in [
        ("x", CellFormat::HexUpper(4)), // a float
        ("s", CellFormat::HexLower(4)), // a signed integer
        ("w", CellFormat::Fixed(6)),    // an integer
        ("w", CellFormat::HexUpper(0)),
        ("w", CellFormat::HexUpper(17)),
        ("x", CellFormat::Fixed(18)),
    ] {
        assert!(
            formats_error(f(name, format)).contains("does not suit"),
            "{name} {format:?}"
        );
    }
}

fn two_columns() -> CsvFormat {
    let schema = vec![
        Field {
            name: "w".into(),
            data_type: DataType::U16,
        },
        Field {
            name: "t".into(),
            data_type: DataType::Text,
        },
    ];
    let options = CsvOptions {
        cell_formats: vec![("w".into(), CellFormat::HexUpper(4))],
        ..CsvOptions::default()
    };
    CsvFormat::new(schema, options).unwrap()
}

#[test]
fn a_row_with_the_wrong_number_of_cells_is_refused_and_taken_back() {
    let format = two_columns();
    let mut out = b"kept\r\n".to_vec();
    let mut w = format.begin_row(&mut out);
    w.hex::<4>(1_u16);
    assert!(matches!(
        w.finish(),
        Err(TableError::RowLength {
            expected: 2,
            found: 1
        })
    ));
    let mut w = format.begin_row(&mut out);
    w.hex::<4>(1_u16);
    w.text("a");
    w.text("b");
    assert!(matches!(
        w.finish(),
        Err(TableError::RowLength {
            expected: 2,
            found: 3
        })
    ));
    assert_eq!(out, b"kept\r\n");
}

/// Debug builds check each cell's method against its column, and refuse the row naming it;
/// release builds do not (`RowWriter`'s documentation).
#[test]
fn debug_builds_refuse_a_cell_that_does_not_suit_its_column() {
    let format = two_columns();
    let mut out = Vec::new();
    for (case, cells) in [
        (
            "decimal for a hex column",
            (|w: &mut RowWriter<'_, '_>| w.u16(1)) as fn(&mut RowWriter<'_, '_>),
        ),
        ("other hex width", |w| w.hex::<2>(1_u16)),
        ("other hex case", |w| w.hex_lower::<4>(1_u16)),
        ("other type", |w| w.hex::<4>(1_u32)),
    ] {
        let mut w = format.begin_row(&mut out);
        cells(&mut w);
        w.text("a");
        let result = w.finish();
        if cfg!(debug_assertions) {
            match result {
                Err(TableError::ValueType { column, .. }) => assert_eq!(column, "w", "{case}"),
                other => panic!("{case}: expected ValueType, got {other:?}"),
            }
            assert!(out.is_empty(), "{case}: the row is taken back");
        } else {
            assert!(result.is_ok(), "{case}");
            out.clear();
        }
    }
}
