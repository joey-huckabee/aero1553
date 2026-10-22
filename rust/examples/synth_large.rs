//! Tile an IRIG recording into a synthetic one of any size, for benchmarks the
//! 21 MB golden recording is too small to show: multi-gigabyte decodes, reads
//! across many refills of the reader's window, and long runs of sync recovery.
//!
//! The template supplies the record mix. A golden recording (`a-large` from
//! `tests/golden/golden.py`) is the intended one: every record kind, errored and
//! SPURIOUS pairs included. Its records are located with the crate's own reader,
//! so a corrupt region in the template is skipped rather than copied. Each tile
//! is the template's records shifted later in time, so timestamps rise across
//! the whole file the way a long capture's do. `--corrupt-every N` writes 8
//! bytes of junk after every Nth record, so the reader loses and recovers sync
//! throughout the output.
//!
//! ```text
//! python tests/golden/golden.py --out DIR a-large
//! cargo run --release --example synth_large -- --template DIR/<a-large file> \
//!     --out big.mie --bytes 4294967296 [--corrupt-every 5000] [--shift-us 7]
//! ```
//!
//! The 5.0.0 to 5.2.0 benchmarks used 1 GiB and 4 GiB outputs, a 1 GiB output
//! with `--corrupt-every 5000`, and sixteen 32 MiB outputs with `--shift-us`
//! `0, 13, 26, ...` merged together. The original experiment, with its three
//! reader backends and driver, is tagged `archive/perf-io-backends`.

use std::fs::File;
use std::io::{BufWriter, Write};

use aero1553::reader::MieFileReader;

const US_PER_DAY: u64 = 86_400_000_000;
const JUNK: [u8; 8] = [0xFF, 0xFF, 0x13, 0x37, 0xDE, 0xAD, 0xBE, 0xEF];

type Error = Box<dyn std::error::Error>;

/// Absolute microseconds of a record's IRIG timestamp (bytes 2..8).
fn irig_us(rec: &[u8]) -> u64 {
    let w = |i: usize| u64::from(u16::from_le_bytes([rec[i], rec[i + 1]]));
    let (upper, middle, lower) = (w(2), w(4), w(6));
    let day = (upper >> 5) & 0x1FF;
    let hour = upper & 0x1F;
    let minute = middle >> 10;
    let sec = (middle >> 4) & 0x3F;
    let us = ((middle & 0xF) << 16) | lower;
    day * US_PER_DAY + ((hour * 60 + minute) * 60 + sec) * 1_000_000 + us
}

/// Rewrite a record's IRIG timestamp to `total` microseconds, keeping its
/// freerun bit.
fn put_irig(rec: &mut [u8], total: u64) -> Result<(), Error> {
    let freerun = u16::from_le_bytes([rec[2], rec[3]]) & 0x8000;
    let (day, rest) = (total / US_PER_DAY, total % US_PER_DAY);
    let (seconds, us) = (rest / 1_000_000, rest % 1_000_000);
    let (minutes, sec) = (seconds / 60, seconds % 60);
    let (hour, minute) = (minutes / 60, minutes % 60);
    if day > 0x1FF {
        return Err("the output runs past day 511; use a shorter --bytes".into());
    }
    let words = [
        freerun | (u16::try_from(day)? << 5) | u16::try_from(hour)?,
        (u16::try_from(minute)? << 10) | (u16::try_from(sec)? << 4) | u16::try_from(us >> 16)?,
        u16::try_from(us & 0xFFFF)?,
    ];
    for (i, w) in words.iter().enumerate() {
        rec[2 + 2 * i..4 + 2 * i].copy_from_slice(&w.to_le_bytes());
    }
    Ok(())
}

struct Args {
    template: String,
    out: String,
    bytes: u64,
    corrupt_every: u64,
    shift_us: u64,
}

fn parse_args() -> Result<Args, Error> {
    let mut a = Args {
        template: String::new(),
        out: String::new(),
        bytes: 0,
        corrupt_every: 0,
        shift_us: 0,
    };
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args.next().ok_or(format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--template" => a.template = value,
            "--out" => a.out = value,
            "--bytes" => a.bytes = value.parse()?,
            "--corrupt-every" => a.corrupt_every = value.parse()?,
            "--shift-us" => a.shift_us = value.parse()?,
            other => return Err(format!("unknown flag {other}").into()),
        }
    }
    if a.template.is_empty() || a.out.is_empty() || a.bytes == 0 {
        return Err(
            "usage: synth_large --template T.mie --out OUT.mie --bytes N \
                    [--corrupt-every N] [--shift-us N]"
                .into(),
        );
    }
    Ok(a)
}

fn main() -> Result<(), Error> {
    let args = parse_args()?;
    let template = std::fs::read(&args.template)?;

    // (offset, length) of every valid record, located by the reader. A record
    // is its Type Word's word count of 16-bit words.
    let reader = MieFileReader::new(&args.template)?;
    let mut records = Vec::new();
    for msg in &reader {
        let msg = msg?;
        let offset = usize::try_from(msg.file_offset)?;
        let len = usize::from(msg.type_word.word_count()) * 2;
        if len < 8 || offset + len > template.len() {
            return Err(format!("record at 0x{offset:X} does not fit the template").into());
        }
        records.push((offset, len));
    }
    let (Some(&(first, _)), Some(&(last, _))) = (records.first(), records.last()) else {
        return Err("the template holds no records".into());
    };
    let period = irig_us(&template[last..]) - irig_us(&template[first..]) + 1_000;

    let mut w = BufWriter::with_capacity(1 << 20, File::create(&args.out)?);
    let (mut written, mut n, mut rec) = (0u64, 0u64, [0u8; 128]);
    'tiles: for tile in 0u64.. {
        let shift = tile * period + args.shift_us;
        for &(offset, len) in &records {
            let rec = &mut rec[..len];
            rec.copy_from_slice(&template[offset..offset + len]);
            put_irig(rec, irig_us(&template[offset..]) + shift)?;
            w.write_all(rec)?;
            written += u64::try_from(len)?;
            n += 1;
            // Never between an errored record and its SPURIOUS continuation:
            // that would change what the pair means.
            let errored = template[offset + 1] & 0x40 != 0;
            if args.corrupt_every > 0 && n.is_multiple_of(args.corrupt_every) && !errored {
                w.write_all(&JUNK)?;
                written += u64::try_from(JUNK.len())?;
            }
            if written >= args.bytes {
                break 'tiles;
            }
        }
    }
    w.write_all(&[0, 0])?;
    w.flush()?;
    eprintln!("{}: {written} bytes, {n} records", args.out);
    Ok(())
}
