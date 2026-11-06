//! Writes a table of every type, some values missing, as Parquet and as CSV, to check with
//! another reader before a release:
//!
//! ```text
//! cargo run --example sample -- DIR
//! python -c "import pandas; print(pandas.read_parquet('DIR/sample.parquet'))"
//! ```

use std::path::PathBuf;

use tablekit::{Batch, Column, TableError, Values};
use tablekit_csv::CsvOptions;
use tablekit_parquet::ParquetOptions;

fn main() -> Result<(), TableError> {
    let dir = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| ".".into()));
    let some = vec![true, false, true];
    let batch = Batch::new(vec![
        Column::new("record_time", Values::F64(vec![0.5, 1e-5, 1e16])),
        Column::with_missing("flag", Values::Bool(vec![true, false, false]), some.clone())?,
        Column::new("i8", Values::I8(vec![-128, 0, 127])),
        Column::new("i16", Values::I16(vec![i16::MIN, 0, i16::MAX])),
        Column::new("i32", Values::I32(vec![i32::MIN, 0, i32::MAX])),
        Column::new("i64", Values::I64(vec![i64::MIN, 0, i64::MAX])),
        Column::new("u8", Values::U8(vec![0, 1, u8::MAX])),
        Column::new("u16", Values::U16(vec![0, 1, u16::MAX])),
        Column::with_missing("u32", Values::U32(vec![0, 1, u32::MAX]), some.clone())?,
        Column::with_missing("u64", Values::U64(vec![0, 1, u64::MAX]), some.clone())?,
        Column::new("f32", Values::F32(vec![0.1, -1.5, f32::MAX])),
        Column::new("f64", Values::F64(vec![0.1, -0.0, f64::MIN_POSITIVE])),
        Column::with_missing(
            "mode",
            Values::Text(vec!["On", "", "a, \"b\""].into()),
            some,
        )?,
    ])?;
    tablekit_parquet::write_file(
        &dir.join("sample.parquet"),
        &batch,
        &ParquetOptions::default(),
    )?;
    tablekit_csv::write_file(&dir.join("sample.csv"), &batch, &CsvOptions::default())?;
    println!("wrote sample.parquet and sample.csv in {}", dir.display());
    Ok(())
}
