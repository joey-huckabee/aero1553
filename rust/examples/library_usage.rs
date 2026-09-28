//! Library-usage example, kept in lockstep with the snippet in `rust/README.md`.
//!
//! Compiled by `cargo test --all-targets` (which builds `examples/`) on every
//! CI run and pre-commit, so the public library API shown here cannot silently
//! rot. The README block is *also* compiled as a `no_run` doctest via
//! `include_str!` in `src/lib.rs` — belt and suspenders.
//!
//! It is `no_run` in spirit: it references files that need not exist, so build
//! it (`cargo build --example library_usage`) rather than running it.

use aero1553::{
    filter::{FilterConfig, FilterIterExt},
    reader::MieFileReader,
    writer::{WriteOptions, write_csv},
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let reader = MieFileReader::new("recording.mie")?;

    // Basic iteration
    for msg in &reader {
        let msg = msg?;
        println!(
            "{} RT{:?} {}",
            msg.timestamp.format(),
            msg.rt(),
            msg.msg_label()
        );
    }

    // With filtering, streaming straight to CSV
    let reader = MieFileReader::new("recording.mie")?;
    let filters = FilterConfig {
        exclude_types: vec![0x20], // skip spurious
        ..Default::default()
    };
    let messages = reader.iter().filter_messages(filters);
    write_csv(
        messages,
        Some(std::path::Path::new("decoded.csv")),
        WriteOptions::default(),
    )?;
    Ok(())
}
