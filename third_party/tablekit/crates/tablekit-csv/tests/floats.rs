//! Every float in `testdata/floats/` is written exactly as NumPy, and so pandas, writes it.

use std::path::Path;

use tablekit::{Batch, Column, Values};
use tablekit_csv::{CsvOptions, LineEnding, write_to};

fn corpus(name: &str) -> Vec<(u64, String)> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../testdata/floats")
        .join(name);
    std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .map(|line| {
            let (bits, text) = line.split_once(' ').unwrap();
            (u64::from_str_radix(bits, 16).unwrap(), text.to_owned())
        })
        .collect()
}

fn written(values: Values) -> Vec<String> {
    let batch = Batch::new(vec![Column::new("x", values)]).unwrap();
    let options = CsvOptions {
        line_ending: LineEnding::Lf,
        ..CsvOptions::default()
    };
    let mut out = Vec::new();
    write_to(&mut out, &batch, &options).unwrap();
    String::from_utf8(out)
        .unwrap()
        .lines()
        .skip(1)
        .map(str::to_owned)
        .collect()
}

fn compare(name: &str, expected: &[(u64, String)], got: &[String]) {
    assert_eq!(expected.len(), got.len());
    let wrong: Vec<String> = expected
        .iter()
        .zip(got)
        .filter(|((_, want), got)| want != *got)
        .take(10)
        .map(|((bits, want), got)| format!("{bits:x}: NumPy {want}, tablekit {got}"))
        .collect();
    assert!(wrong.is_empty(), "{name}:\n{}", wrong.join("\n"));
}

#[test]
fn doubles_match_numpy() {
    let cases = corpus("f64.txt");
    let values = cases.iter().map(|&(b, _)| f64::from_bits(b)).collect();
    compare("f64", &cases, &written(Values::F64(values)));
}

#[test]
fn floats_match_numpy() {
    let cases = corpus("f32.txt");
    let values = cases
        .iter()
        .map(|&(b, _)| f32::from_bits(u32::try_from(b).unwrap()))
        .collect();
    compare("f32", &cases, &written(Values::F32(values)));
}
