//! The Rust half of the performance suite (`perf/README.md`).
//!
//! Hand-rolled, like the rest of the crate: no benchmark framework, so the
//! crate keeps `memmap2` as its only dependency, dev-dependencies included.
//! One invocation times one case and prints one JSON line; `perf/run.py`
//! drives every case of both implementations, checks each result against the
//! golden pins (`tests/golden/golden.json`), and writes the report.
//!
//! ```text
//! cargo bench --bench perf -- --case decode_csv --input a.mie --output out.csv --repeat 5
//! ```
//!
//! Cases: `iterate`, `decode_csv`, `filter_order`, `merge_csv`.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

use aero1553::MieResult;
use aero1553::filter::{FilterConfig, FilterIterExt};
use aero1553::merge::MergedRecordIter;
use aero1553::models::MieMessage;
use aero1553::order::{DEFAULT_MAX_SORT_GROUP, OrderIterExt};
use aero1553::reader::MieFileReader;
use aero1553::writer::{WriteOptions, write_csv};

struct Args {
    case: String,
    inputs: Vec<PathBuf>,
    output: Option<PathBuf>,
    repeat: usize,
}

fn parse() -> Result<Args, String> {
    let mut args = Args {
        case: String::new(),
        inputs: Vec::new(),
        output: None,
        repeat: 3,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = || it.next().ok_or(format!("{arg} needs a value"));
        match arg.as_str() {
            "--case" => args.case = value()?,
            "--input" => args.inputs.push(PathBuf::from(value()?)),
            "--output" => args.output = Some(PathBuf::from(value()?)),
            "--repeat" => args.repeat = value()?.parse().map_err(|e| format!("--repeat: {e}"))?,
            // `cargo bench` appends `--bench` to every bench target's argv.
            "--bench" => {}
            other => return Err(format!("unknown argument {other}")),
        }
    }
    if args.case.is_empty() || args.inputs.is_empty() {
        return Err(
            "usage: perf --case NAME --input PATH [--input PATH] [--output PATH] [--repeat N]"
                .into(),
        );
    }
    Ok(args)
}

/// Rows and the sum of their RT addresses: the fingerprint the driver checks.
#[derive(Default)]
struct Tally {
    rows: u64,
    rt_sum: u64,
}

fn tally(messages: impl IntoIterator<Item = MieResult<MieMessage>>) -> MieResult<Tally> {
    let mut t = Tally::default();
    for m in messages {
        let m = m?;
        t.rows += 1;
        t.rt_sum += u64::from(m.rt().unwrap_or(0));
    }
    Ok(t)
}

fn run(args: &Args) -> MieResult<Tally> {
    let first = &args.inputs[0];
    let output = args.output.as_deref();
    match args.case.as_str() {
        "iterate" => tally(&MieFileReader::new(first)?),
        "filter_order" => {
            let reader = MieFileReader::new(first)?;
            let filters = FilterConfig {
                exclude_types: vec![0x20],
                ..FilterConfig::default()
            };
            tally(
                reader
                    .iter()
                    .filter_messages(filters)
                    .order_rows(DEFAULT_MAX_SORT_GROUP),
            )
        }
        "decode_csv" => {
            let reader = MieFileReader::new(first)?;
            let out = write_csv(
                reader.iter().order_rows(DEFAULT_MAX_SORT_GROUP),
                output,
                WriteOptions::default(),
            )?;
            Ok(Tally {
                rows: out.normal_count + out.error_count,
                rt_sum: 0,
            })
        }
        "merge_csv" => {
            let readers = args
                .inputs
                .iter()
                .map(MieFileReader::new)
                .collect::<MieResult<Vec<_>>>()?;
            let merged = MergedRecordIter::new(&readers, None, false, false)?.collapse(true, 0);
            let out = write_csv(
                merged.order_rows(DEFAULT_MAX_SORT_GROUP),
                output,
                WriteOptions::default(),
            )?;
            Ok(Tally {
                rows: out.normal_count + out.error_count,
                rt_sum: 0,
            })
        }
        other => panic!("unknown case {other}"),
    }
}

/// Peak resident memory, where the platform reports it without a dependency.
fn peak_bytes() -> Option<u64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let line = status.lines().find(|l| l.starts_with("VmHWM:"))?;
    let kb: u64 = line.split_whitespace().nth(1)?.parse().ok()?;
    Some(kb * 1024)
}

fn main() -> ExitCode {
    // `cargo test --all-targets` and a bare `cargo bench` run every bench
    // target with no case: there is nothing to time, which is not an error.
    if !std::env::args().any(|a| a == "--case") {
        eprintln!("perf: no --case given; run through perf/run.py (see perf/README.md)");
        return ExitCode::SUCCESS;
    }
    let args = match parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    let mut runs = Vec::with_capacity(args.repeat);
    let mut tally_out = Tally::default();
    for _ in 0..args.repeat {
        let start = Instant::now();
        match run(&args) {
            Ok(t) => tally_out = t,
            Err(e) => {
                eprintln!("{}: {e}", args.case);
                return ExitCode::FAILURE;
            }
        }
        runs.push(start.elapsed().as_secs_f64());
    }
    let runs = runs
        .iter()
        .map(|s| format!("{s:.6}"))
        .collect::<Vec<_>>()
        .join(", ");
    let peak = peak_bytes().map_or_else(|| "null".to_string(), |b| b.to_string());
    println!(
        "{{\"case\": \"rust.{}\", \"runs\": [{runs}], \"rows\": {}, \"rt_sum\": {}, \"peak_bytes\": {peak}}}",
        args.case, tally_out.rows, tally_out.rt_sum
    );
    ExitCode::SUCCESS
}
