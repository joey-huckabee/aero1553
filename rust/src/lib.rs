//! Aero1553: parser for DDC MIL-STD-1553 MIE binary recording files.
//!
//! See [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for the module
//! diagram and synchronization strategy.
//!
//! The crate README (rendered below) is included as crate-level documentation
//! via `include_str!`, so its `rust` code block is compiled as a `no_run`
//! doctest by `cargo test --doc` — the library example cannot silently rot.
#![doc = include_str!("../README.md")]
#![cfg_attr(
    not(test),
    warn(
        clippy::expect_used,
        clippy::unwrap_used,
        clippy::panic,
        clippy::unreachable,
        clippy::todo,
        clippy::unimplemented
    )
)]

pub mod cli;
pub mod config;
pub mod decode;
// Public since v4.0.0: the Python package's `aero1553.delta.DeltaTracker` is
// this tracker, so the gap arithmetic has one implementation. (It was
// crate-private while only `reader` and `merge` used it.) A plain comment, not
// a doc comment: combined with the module's own `//!` docs, an outer doc here
// would make rustdoc resolve those docs' links from the crate root.
pub mod delta;
pub mod dump;
pub mod error;
pub mod filter;
pub mod log;
pub mod merge;
pub mod models;
pub mod order;
pub mod reader;
pub mod sync;
mod text;
pub mod writer;

pub use reader::{MieFileReader, ReaderOptions};
pub use sync::ValidationFailure;

pub use error::{MieError, MieErrorKind, MieResult};
pub use models::{
    Bus, CommandWord, DataWords, Direction, ErrorMode, IrigTimestamp, MessageFormat, MessageType,
    MieMessage, StandardTimestamp, Timestamp, TimestampFormat, TypeWord,
};
