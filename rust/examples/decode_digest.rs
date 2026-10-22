//! Emit FNV-1a digests over **exhaustive** sweeps of the field decoders.
//!
//! It was written so the C++ implementation (removed in 5.0.0) could be proved
//! to decode identically to this one over the *entire* input space of each word
//! decoder, rather than over a hand-picked sample: the C++ suite pinned the
//! numbers this prints and recomputed them from its own decoders. Nothing pins
//! them now. It remains useful for showing that a change to the word decoders
//! is behaviour-preserving: run it before and after, and a divergence anywhere
//! in 65 536 inputs changes the digest.
//!
//! Why a digest rather than a committed table: the sweeps below cover several
//! hundred thousand decodes, and a table of them would be megabytes of
//! generated data nobody reads. A digest is one number per sweep, and its only
//! job is to differ when anything differs.
//!
//! Run it with:
//!
//! ```text
//! cargo run --release --example decode_digest
//! ```
//!
//! It is deterministic and takes no input, so re-running it on any host must
//! print the same numbers.

use aero1553::decode::{
    decode_command_word, decode_irig_timestamp, decode_standard_timestamp, decode_type_word,
};

/// FNV-1a, 64-bit. Chosen for being trivial to reimplement identically in
/// another language -- the point is agreement, not cryptographic strength.
struct Fnv1a(u64);

impl Fnv1a {
    fn new() -> Self {
        Self(0xcbf2_9ce4_8422_2325)
    }

    fn byte(&mut self, b: u8) {
        self.0 ^= u64::from(b);
        self.0 = self.0.wrapping_mul(0x0000_0100_0000_01B3);
    }

    fn u16(&mut self, v: u16) {
        self.byte((v & 0xFF) as u8);
        self.byte((v >> 8) as u8);
    }

    fn u32(&mut self, v: u32) {
        self.u16((v & 0xFFFF) as u16);
        self.u16((v >> 16) as u16);
    }
}

fn type_word_digest() -> u64 {
    let mut h = Fnv1a::new();
    for raw in 0..=u16::MAX {
        let tw = decode_type_word(raw);
        h.byte(tw.message_type());
        h.byte(tw.bus() as u8);
        h.u16(tw.word_count());
        h.byte(u8::from(tw.error()));
    }
    h.0
}

fn command_word_digest() -> u64 {
    let mut h = Fnv1a::new();
    for raw in 0..=u16::MAX {
        let cw = decode_command_word(raw);
        h.byte(cw.rt);
        h.byte(cw.direction as u8);
        h.byte(cw.subaddress);
        h.byte(cw.data_word_count);
    }
    h.0
}

/// Three sweeps, one per word, each covering that word's full 16-bit range
/// while the other two hold fixed at values with a mixed bit pattern. A single
/// sweep over all three words would be 2^48 decodes; this covers every bit
/// position of every field, which is what the decoder can actually get wrong.
fn irig_digest() -> u64 {
    let mut h = Fnv1a::new();
    let feed = |h: &mut Fnv1a, upper: u16, middle: u16, lower: u16| {
        let ts = decode_irig_timestamp(upper, middle, lower);
        h.u16(ts.day);
        h.byte(ts.hour);
        h.byte(ts.minute);
        h.byte(ts.second);
        h.u32(ts.microsecond);
        h.byte(u8::from(ts.freerun));
    };
    for upper in 0..=u16::MAX {
        feed(&mut h, upper, 0xA5A5, 0x5A5A);
    }
    for middle in 0..=u16::MAX {
        feed(&mut h, 0xA5A5, middle, 0x5A5A);
    }
    for lower in 0..=u16::MAX {
        feed(&mut h, 0xA5A5, 0x5A5A, lower);
    }
    h.0
}

fn standard_digest() -> u64 {
    let mut h = Fnv1a::new();
    for upper in 0..=u16::MAX {
        // The complement gives the lower word a different bit pattern from the
        // upper one, so a decoder that swapped them would change the digest.
        let ts = decode_standard_timestamp(upper, !upper);
        h.u32(ts.raw_value);
        h.u16(ts.upper_word);
        h.u16(ts.lower_word);
    }
    h.0
}

fn main() {
    println!("type_word    0x{:016X}", type_word_digest());
    println!("command_word 0x{:016X}", command_word_digest());
    println!("irig         0x{:016X}", irig_digest());
    println!("standard     0x{:016X}", standard_digest());
}
