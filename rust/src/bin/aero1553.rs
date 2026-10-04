#![cfg_attr(not(test), warn(clippy::expect_used, clippy::unwrap_used))]

use std::process::ExitCode;

fn main() -> ExitCode {
    // `args_os`, not `args`: `args` panics on the first argument that is not
    // UTF-8, and on Linux a file name need not be (L2-CLI-021).
    aero1553::cli::run_os(std::env::args_os().collect())
}
