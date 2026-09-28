// SPDX-License-Identifier: Apache-2.0
//
// The command-line interface: argument parsing, the subcommand runners, and
// the exit-code contract.
//
// Mirrors `rust/src/cli.rs` and `python/src/aero1553/cli.py`.
// `docs/CLI-REFERENCE.md` is the normative per-flag reference and
// `docs/ERROR-CATALOG.md` the normative exit-code table.
//
// THIS IS WHERE THE PIPELINE IS ASSEMBLED. Every other module is a stage that
// knows only its own job; this one knows the order they go in:
//
//     config -> overrides -> MieFileReader -> FilteredSource -> OrderedSource
//                                                                    |
//                                                               write_csv
//
// PRECEDENCE IS CLI > CONFIG FILE > DEFAULT, and the mechanism matters. A
// presence-only flag (`--strict`, `--no-clobber`, `--allow-partial`,
// `--no-mux`, `--separate-errors`) contributes an override only when it is
// GIVEN. Contributing `false` when absent would silently clobber a `true` the
// operator had set in their config file -- the flag would act as though it
// meant "off" rather than "not specified".
//
// SCOPE. This build implements `decode` and `count`. `dump`, `--manifest`,
// `--glob` and the multi-file merge arrive with the merge module; a flag
// belonging to those is refused with a message that says so, rather than
// reported as unknown, so an operator copying a working Rust invocation learns
// which part is missing rather than doubting the flag name.

#ifndef MIE_CLI_HPP
#define MIE_CLI_HPP

#include <cstdio>
#include <string>
#include <vector>

namespace mie {
namespace cli {

/// Process exit codes (L1-EXIT-*, L2-CLI-011).
///
/// The mapping is deliberately NOT one code per error. Four distinct outcomes
/// share exit 0 -- a complete decode, one that recovered from sync losses, a
/// valid but empty recording, and a downstream pipe closing -- because all four
/// mean "the tool did its job"; the exit-class log line is what tells them
/// apart. Three different errors share exit 2 because all three mean "this is
/// not an MIE recording", which is the distinction a caller acts on.
enum ExitCode {
    /// Success, including a partial-recovered decode and a broken pipe.
    EXIT_OK = 0,
    /// Runtime or decode failure: input I/O, writer failure, strict-mode
    /// record and structural-invariant failures.
    EXIT_RUNTIME = 1,
    /// The input is not an MIE recording: no valid records, a homogeneous pad,
    /// or a timestamp format the probe could not resolve under `--strict`.
    EXIT_NO_RECORDS = 2,
    /// Unrecoverable mid-file sync loss, without `--allow-partial`.
    EXIT_SYNC_LOSS = 3,
    /// Usage error: unknown, missing or invalid flag, argument or subcommand.
    EXIT_USAGE = 4,
    /// Configuration error: file not found, malformed TOML, invalid value.
    EXIT_CONFIG = 5,
    /// Merge inputs that cannot be ordered on a common absolute timeline.
    EXIT_MERGE_INCOMPATIBLE = 6
};

/// The streams the CLI reports on.
///
/// Injected rather than reached for, because `run()` writing to the process's
/// own `stdout` is a hidden dependency: a test asserting on what `count` prints
/// would otherwise have to redirect a file descriptor, and the two spellings of
/// that (`dup2` / `_dup2`) are OS surface this tree confines to the platform
/// backends. The same reasoning already produced `log::set_sink` and the
/// abstract `CsvSink`, so this is the house pattern rather than a new one.
///
/// Note the one thing NOT covered: `-o -` streams the CSV itself through the
/// writer's own stdout sink, so that path is proved end-to-end by the
/// conformance runner, which spawns the real binary and can capture its real
/// stdout.
struct Streams {
    /// Machine-readable output: the `count` integer, `--help`, `--version`.
    std::FILE* out;
    /// Diagnostics: error messages and the human-readable `count` sentence.
    std::FILE* err;

    /// The process's own streams.
    Streams();
    Streams(std::FILE* out_, std::FILE* err_);
};

/// Run the CLI. `args` excludes the program name.
///
/// Takes a vector rather than `(argc, argv)` so it is directly callable from a
/// test: the exit-code contract is the thing worth asserting, and asserting it
/// through a spawned process would make every case a subprocess.
int run(const std::vector<std::string>& args);

/// Run the CLI, reporting on `streams`.
int run(const std::vector<std::string>& args, const Streams& streams);

/// The `--help` text. Exposed so the surface-parity check can read the flag
/// set without spawning the binary.
const char* help_text();

/// The `--version` line, without its trailing newline.
std::string version_line();

}  // namespace cli
}  // namespace mie

#endif  // MIE_CLI_HPP
