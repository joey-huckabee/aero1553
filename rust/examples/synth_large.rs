//! EXPERIMENT (branch perf/io-backends): tile a clean IRIG template recording
//! into a synthetic one of any size, for the I/O-backend benchmark.
//!
//! The template supplies the record mix (the golden generator's weighted
//! transactions, errored + SPURIOUS pairs included); each tile is the template
//! shifted later in time, so timestamps rise across the whole file the way a
//! long capture's do. `--corrupt-every N` drops 8 bytes of junk after every
//! Nth record, so the reader loses and recovers sync throughout the file --
//! including across the stream backend's window boundaries.
//!
//! ```text
//! cargo run --release --example synth_large -- --template T.mie --out big.mie \
//!     --bytes 4294967296 [--corrupt-every 5000] [--shift-us 7]
//! ```

use std::fs::File;
use std::io::{BufWriter, Write};

const US_PER_DAY: u64 = 86_400_000_000;
const JUNK: [u8; 8] = [0xFF, 0xFF, 0x13, 0x37, 0xDE, 0xAD, 0xBE, 0xEF];

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

fn put_irig(rec: &mut [u8], total: u64) {
    let freerun = u16::from_le_bytes([rec[2], rec[3]]) & 0x8000;
    let (day, rest) = (total / US_PER_DAY, total % US_PER_DAY);
    let (seconds, us) = (rest / 1_000_000, rest % 1_000_000);
    let (minutes, sec) = (seconds / 60, seconds % 60);
    let (hour, minute) = (minutes / 60, minutes % 60);
    let words = [
        freerun | ((day as u16) << 5) | hour as u16,
        ((minute as u16) << 10) | ((sec as u16) << 4) | ((us >> 16) as u16 & 0xF),
        (us & 0xFFFF) as u16,
    ];
    for (i, w) in words.iter().enumerate() {
        rec[2 + 2 * i..4 + 2 * i].copy_from_slice(&w.to_le_bytes());
    }
}

fn main() -> std::io::Result<()> {
    let (mut template, mut out, mut bytes, mut corrupt_every, mut shift_us) =
        (String::new(), String::new(), 0u64, 0u64, 0u64);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        let v = args.next().expect("flag needs a value");
        match a.as_str() {
            "--template" => template = v,
            "--out" => out = v,
            "--bytes" => bytes = v.parse().expect("--bytes"),
            "--corrupt-every" => corrupt_every = v.parse().expect("--corrupt-every"),
            "--shift-us" => shift_us = v.parse().expect("--shift-us"),
            other => panic!("unknown flag {other}"),
        }
    }
    let t = std::fs::read(&template)?;
    let mut recs = Vec::new();
    let mut off = 0;
    while off + 2 <= t.len() {
        let tw = u16::from_le_bytes([t[off], t[off + 1]]);
        if tw == 0 {
            break;
        }
        let len = usize::from((tw >> 8) & 0x3F) * 2;
        recs.push((off, len));
        off += len;
    }
    let first = irig_us(&t[recs[0].0..]);
    let last = irig_us(&t[recs[recs.len() - 1].0..]);
    let period = last - first + 1_000;

    let mut w = BufWriter::with_capacity(1 << 20, File::create(&out)?);
    let (mut written, mut n, mut rec) = (0u64, 0u64, [0u8; 128]);
    'tiles: for tile in 0.. {
        let shift = tile * period + shift_us;
        for &(o, len) in &recs {
            rec[..len].copy_from_slice(&t[o..o + len]);
            put_irig(&mut rec[..len], irig_us(&t[o..]) + shift);
            w.write_all(&rec[..len])?;
            written += len as u64;
            n += 1;
            if corrupt_every > 0 && n.is_multiple_of(corrupt_every) {
                // Never between an errored record and its SPURIOUS
                // continuation: that would change what the pair means.
                let errored = t[o + 1] & 0x40 != 0;
                if !errored {
                    w.write_all(&JUNK)?;
                    written += JUNK.len() as u64;
                }
            }
            if written >= bytes {
                break 'tiles;
            }
        }
    }
    w.write_all(&[0, 0])?;
    w.flush()?;
    eprintln!("{out}: {written} bytes, {n} records");
    Ok(())
}
