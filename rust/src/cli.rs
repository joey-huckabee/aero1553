//! Hand-rolled argument parser and CLI dispatch.
//!
//! Surface:
//!
//! ```text
//! aero1553 [--log-level L] [--config PATH] <command> [opts...]
//! ```
//!
//! Commands: `decode`, `count`, `dump`.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use crate::config::{ConfigOverrides, DecoderConfig, load_config, parse_bus_name, parse_type_name};
use crate::dump::{hex_dump_raw_to_stdout, hex_dump_records_to_stdout};
use crate::error::MieError;
use crate::filter::FilterIterExt;
use crate::log::{self, Level};
use crate::models::{ErrorMode, OutputTimeFormat, TimeRender, TimestampFormat, YEAR_MAX, YEAR_MIN};
use crate::order::OrderIterExt;
use crate::reader::{MieFileReader, ReaderOptions};
use crate::writer::{WriteOptions, write_csv, write_csv_split};
use crate::{log_error, log_info, log_warn};

const VERSION: &str = env!("CARGO_PKG_VERSION");

const HELP: &str = "\
aero1553 -- DDC MIL-STD-1553 MIE binary decoder

USAGE:
  aero1553 [--log-level L] [--config PATH] <command> [options]

COMMANDS:
  decode <INPUT>... Decode MIE file(s) to CSV (2+ inputs -> time-sorted merge)
  count  <INPUT>    Print message count (no CSV)
  dump   <INPUT>    Hex dump (raw or record-aware)

GLOBAL OPTIONS:
  --log-level LEVEL                     DEBUG|INFO|WARNING|WARN|ERROR|
                                        CRITICAL|OFF (default WARNING;
                                        case-insensitive; CRITICAL/OFF silence)
  --config PATH                         TOML configuration file
  --no-irig-day-advisory                Never emit the one-time IRIG
                                        day-of-year advisory. It is logged at
                                        INFO, so it is already silent at the
                                        default level; this suppresses it at
                                        INFO/DEBUG too (L2-LOG-001)
  -V, -v, --version                     Print version and exit
  -h, --help                            Print this help and exit

DECODE OPTIONS:
  -o, --output PATH                     Output CSV (default stdout)
  --manifest PATH                       Read input paths from a file (one per
                                        line; blank/#-comment lines ignored).
                                        Mutually exclusive with positionals /
                                        --glob (L2-MRG-001)
  --glob PATTERN                        Expand a single-directory *|? filename
                                        glob (no recursion). Mutually exclusive
                                        with positionals / --manifest
  --separate-errors                     Route errored/spurious records to a
                                        separate <stem>_errors.csv. Default:
                                        every record inline in the main CSV
                                        with ERROR/ERROR_CODE populated
  --no-clobber                          Refuse to overwrite an existing
                                        output file (L2-WRT-017)
  --allow-partial                       On unrecoverable mid-file sync
                                        loss, write a <output>.partial
                                        file and exit 0 instead of 3
                                        (L1-EXIT-004)
  --input-time-format auto|irig|standard
                                        How timestamps are PARSED from the
                                        file. Default auto (case-insensitive)
  --output-time-format doy|iso|dom      How TIME_STAMP is WRITTEN to the CSV.
                                        doy (default) DAY:HH:MM:SS.uuuuuu, the
                                        vendor rendering; iso
                                        YYYY-MM-DDTHH:MM:SS.uuuuuu plus zone;
                                        dom DD:HH:MM:SS.uuuuuu. iso and dom
                                        need a year. L2-WRT-025
  --year YYYY                           Calendar year used to resolve the IRIG
                                        day-of-year field (range 1..=9999). An
                                        MIE file carries no year, so iso and
                                        dom require one; doy ignores it.
                                        L2-WRT-026
  --utc-offset Z|+HH:MM|-HH:MM          Zone designator for the iso rendering
                                        (default Z, meaning UTC). IRIG-B
                                        carries no timezone, so this states
                                        what the recording could not. L2-WRT-025
  --detect-records N                    Records probed by timestamp-
                                        format auto-detection (1..=32,
                                        default 8). L2-DEC-015.
  --lookahead-records N                 Total records checked by sync
                                        validation per call (1 candidate
                                        + N-1 look-ahead, range 1..=32,
                                        default 2). L2-SYN-026.
  --standard-tick-rate-hz HZ            Standard-counter frequency in Hz.
                                        When set, Standard timestamps are
                                        converted to microseconds and join
                                        DELTA tracking. Must be > 0
                                        (default: unset -> empty DELTA for
                                        Standard). L2-DEC-017.
  --strict                              Raise on invalid records
  --format csv                          Output format (csv only at present)
  --no-mux                              Leave the MUX column empty
                                        (vendor-exact). Default: MUX is
                                        derived from the file name (L2-WRT-020)
  --mux-delimiter D                     MUX field separator (default '.')
  --mux-field N                         0-based MUX field index; negative
                                        counts from the end (default 4)
  --collapse-duplicates                 Collapse the same bus transaction seen
                                        by multiple recorders into one row
                                        (multi-file merge only). Default: off
  --collapse-window-us N                Timestamp tolerance in microseconds for
                                        collapsing (default 0 = exact match)
  --delta-scope per-file|global         Scope DELTA is measured over in a
                                        multi-file merge (default per-file:
                                        each gap is to the previous same-key
                                        record from its OWN file, matching a
                                        single-file decode). global measures
                                        across the merged timeline. No effect
                                        on a single input. L2-MRG-005.
  --max-sort-group N                    Max consecutive same-TIME_STAMP records
                                        buffered to order rows by RT then MSG
                                        (range 1..=1048576, default 4096). Use 1
                                        to disable reordering and emit raw
                                        capture order. L2-WRT-022.
  --max-collapse-survivors N            Max records the --collapse-duplicates
                                        window retains at once (range
                                        1..=1048576, default 4096). Bounds the
                                        set by COUNT where the window bounds it
                                        by TIME. Past the cap collapsing is
                                        best-effort, with one WARN. L2-MRG-008.
  --exclude-types VAL                   Comma-separated names or 0xNN
  --exclude-rts VAL                     Comma-separated RT addresses
  --exclude-buses VAL                   Comma-separated A|B
  --exclude-subaddresses VAL            Comma-separated subaddresses
  --include-types VAL                   (same syntax as --exclude-types)
  --include-rts VAL
  --include-buses VAL
  --include-subaddresses VAL

  Filter flags accept ONE value (comma-separable). Repeat the flag to
  accumulate. `--include-rts 15,31` and `--include-rts 15 --include-rts 31`
  are equivalent. Appending `=value` (e.g. `--include-rts=15`) also works.

DUMP OPTIONS:
  --raw                                 Raw hex dump (no record parsing)
  --offset N                            Start offset (decimal or 0xHEX)
  --length N                            Bytes to dump, raw mode (decimal or 0xHEX)
  --records N                           Max records, record mode (decimal or 0xHEX)

EXAMPLES:
  aero1553 decode rec.mie -o out.csv
  aero1553 decode rec.mie --separate-errors --include-rts 15
  aero1553 decode a.mie b.mie c.mie -o merged.csv   # time-sorted merge
  aero1553 decode --glob 'recordings/*.mie' -o merged.csv
  aero1553 count rec.mie
  aero1553 dump rec.mie --records 10
";

#[derive(Debug)]
enum Command {
    // Boxed because DecodeArgs is much larger than the other variants
    // (clippy::large_enum_variant). Heap-allocating the rare path keeps
    // the common variants cheap.
    Decode(Box<DecodeArgs>),
    Count(PathBuf),
    Dump(Box<DumpArgs>),
}

#[derive(Debug, Default)]
struct DecodeArgs {
    /// One or more positional input paths. Mutually exclusive with
    /// `manifest` / `glob` (L2-MRG-001). More than one resolved input ⇒ merge.
    inputs: Vec<PathBuf>,
    /// `--manifest <file>`: read input paths from a file (one per line).
    manifest: Option<PathBuf>,
    /// `--glob <pattern>`: expand a single-directory `*`/`?` filename glob.
    glob: Option<String>,
    output: Option<PathBuf>,
    separate_errors: bool,
    no_clobber: bool,
    allow_partial: bool,
    input_time_format: Option<TimestampFormat>,
    /// L2-CLI-018: which rendering the `TIME_STAMP` column uses.
    output_time_format: Option<OutputTimeFormat>,
    /// L2-WRT-026: calendar year for the `iso` / `dom` renderings.
    year: Option<u16>,
    /// L2-WRT-025: UTC offset in minutes for the `iso` zone designator.
    utc_offset_minutes: Option<i16>,
    detect_records: Option<usize>,
    lookahead_records: Option<usize>,
    standard_tick_rate_hz: Option<f64>,
    strict: Option<bool>,
    output_format: Option<String>,
    /// `--no-mux`: disable MUX-from-filename population (vendor-exact output).
    no_mux: bool,
    /// `--mux-delimiter <D>`: field separator for MUX extraction.
    mux_delimiter: Option<String>,
    /// `--mux-field <N>`: 0-based field index (negative = from end) for MUX.
    mux_field: Option<i64>,
    /// `--collapse-duplicates`: collapse cross-recorder duplicate messages.
    collapse_duplicates: bool,
    /// `--collapse-window-us <N>`: timestamp tolerance (µs) for collapsing.
    collapse_window_us: Option<i64>,
    /// `--delta-scope <SCOPE>`: DELTA measurement scope in a merge (L2-MRG-005).
    delta_scope: Option<crate::models::DeltaScope>,
    /// `--max-sort-group <N>`: cap on one buffered equal-timestamp run
    /// (L2-WRT-022); `1` disables canonical reordering.
    max_sort_group: Option<usize>,
    /// `--max-collapse-survivors <N>`: cap on the de-duplication survivor set
    /// (L2-MRG-008).
    max_collapse_survivors: Option<usize>,

    exclude_types: Vec<u8>,
    exclude_rts: Vec<u8>,
    exclude_buses: Vec<crate::models::Bus>,
    exclude_subaddresses: Vec<u8>,

    include_types: Vec<u8>,
    include_rts: Vec<u8>,
    include_buses: Vec<crate::models::Bus>,
    include_subaddresses: Vec<u8>,
}

#[derive(Debug, Default)]
struct DumpArgs {
    input: PathBuf,
    raw: bool,
    offset: usize,
    length: Option<usize>,
    records: Option<u64>,
}

#[derive(Debug, Default)]
struct GlobalArgs {
    log_level: Option<String>,
    config: Option<PathBuf>,
    /// `--no-irig-day-advisory`. `Some(false)` when passed; `None` leaves the
    /// config-file value (or the default) in place, per L2-CFG-003 precedence.
    irig_day_advisory: Option<bool>,
}

/// Process exit codes, the normative contract pinned by L2-CLI-011 /
/// L1-EXIT-002..008. Kept as named constants so every exit site is
/// self-documenting and the taxonomy lives in one place.
mod exit_code {
    /// Success, including a completed `--allow-partial` decode (L1-EXIT-004).
    pub(crate) const SUCCESS: u8 = 0;
    /// Runtime / decode error: input I/O (incl. file-not-found), writer
    /// failure, strict-mode record & structural-invariant failures.
    pub(crate) const RUNTIME: u8 = 1;
    /// No valid records — the input is not an MIE recording.
    pub(crate) const NO_RECORDS: u8 = 2;
    /// Unrecoverable mid-file sync loss without `--allow-partial`.
    pub(crate) const SYNC_LOSS: u8 = 3;
    /// CLI usage error: unknown/missing/invalid flag or argument,
    /// unknown subcommand, bad flag value.
    pub(crate) const USAGE: u8 = 4;
    /// Configuration error: config file not found, malformed TOML, or an
    /// invalid configuration value.
    pub(crate) const CONFIG: u8 = 5;
    /// Merge-incompatible inputs: a multi-file merge whose inputs cannot be
    /// ordered on a common absolute timeline (L1-EXIT-009 / L2-MRG-003).
    pub(crate) const MERGE_INCOMPATIBLE: u8 = 6;
}

/// A subcommand failure carrying the exit code it should map to. Lets the
/// runners distinguish a configuration error (`CONFIG`) from a generic
/// runtime/decode error (`RUNTIME`) without flattening both to exit 1.
struct CliError {
    code: u8,
    message: String,
}

impl CliError {
    fn runtime(message: impl Into<String>) -> Self {
        Self {
            code: exit_code::RUNTIME,
            message: message.into(),
        }
    }
    fn config(message: impl Into<String>) -> Self {
        Self {
            code: exit_code::CONFIG,
            message: message.into(),
        }
    }
    fn usage(message: impl Into<String>) -> Self {
        Self {
            code: exit_code::USAGE,
            message: message.into(),
        }
    }
    /// Map a decode-time `MieError` surfaced by the reader to the CLI exit
    /// class, mirroring `classify_decode_exit`'s error arms so `count` and
    /// `decode` agree: a "wrong file type" rejection (`NoValidRecords`,
    /// `HomogeneousPayload`, or a strict-mode `TimestampFormatMismatch`)
    /// maps to `NO_RECORDS` (exit 2); everything else stays `RUNTIME`
    /// (exit 1). (Previously `count` flattened all reader errors to exit 1.)
    fn from_decode_error(e: MieError) -> Self {
        let code = match e.kind() {
            crate::error::MieErrorKind::NoValidRecords
            | crate::error::MieErrorKind::HomogeneousPayload
            | crate::error::MieErrorKind::TimestampFormatMismatch
            | crate::error::MieErrorKind::CalendarUnavailable => exit_code::NO_RECORDS,
            _ => exit_code::RUNTIME,
        };
        Self {
            code,
            message: format_mie_error(e),
        }
    }
}

// ── Top-level entry ───────────────────────────────────────────────────

/// Run the CLI and return the process exit status.
///
/// `argv[0]` is the program name and is skipped, as in `std::env::args()`.
/// The status is the L2-CLI-011 taxonomy (`0` success, `1` runtime, `2` no
/// records, `3` sync loss, `4` usage, `5` config, `6` merge-incompatible).
#[must_use]
pub fn run(argv: Vec<String>) -> ExitCode {
    ExitCode::from(run_to_code(argv))
}

/// [`run`], returning the exit status as a number.
///
/// `std::process::ExitCode` is opaque -- nothing can read the status back out
/// of it -- which is right for a `main` and wrong for an embedder. The Python
/// package's `aero1553.cli.main()` returns this value as an `int`.
///
/// Diagnostics go to stderr exactly as the binary writes them, even when an
/// embedder has installed a [`log::set_sink`] for the library's log lines:
/// the command line's output is its contract, and it does not change with
/// the host it runs in.
#[must_use]
pub fn run_to_code(argv: Vec<String>) -> u8 {
    log::with_stderr(|| run_command(argv))
}

fn run_command(argv: Vec<String>) -> u8 {
    let mut iter = argv.into_iter().skip(1).peekable();

    // Pull global flags + --help / --version that may appear before the command.
    let mut globals = GlobalArgs::default();
    let cmd_token = match parse_global_flags(&mut iter, &mut globals) {
        Ok(token) => token,
        Err(code) => return code,
    };

    // Parse subcommand-specific args. Process control (printing help,
    // selecting an exit code) is decided inside `parse_subcommand`.
    let command = match parse_subcommand(&cmd_token, &mut iter) {
        Ok(c) => c,
        Err(code) => return code,
    };

    // Apply log level early so the version banner respects it. CLI
    // value if provided, else WARN default. An invalid CLI value
    // (e.g. `--log-level NOPE`) fails fast here via `die` (exit 4,
    // the usage class) instead of being silently ignored. The config file's level is layered
    // on top later inside resolve_config.
    if let Some(s) = globals.log_level.as_deref() {
        if let Err(msg) = apply_log_level("--log-level", s) {
            return die(&msg);
        }
    } else {
        log::set_level(Level::Warn);
    }

    log_info!("aero1553 v{VERSION}");

    // Decode returns a Result<u8, CliError> so it can choose exit
    // codes 2 (no-records) and 3 (partial-unrecoverable) directly. The
    // CliError on the failure path carries its own code so a config
    // error (5) is distinguished from a generic runtime error (1).
    // Count/Dump use the simpler Result<(), CliError> contract and map
    // Ok to exit 0.
    let result: Result<u8, CliError> = match command {
        Command::Decode(args) => run_decode(globals, *args),
        Command::Count(input) => run_count(globals, input).map(|()| exit_code::SUCCESS),
        Command::Dump(args) => run_dump(globals, *args).map(|()| exit_code::SUCCESS),
    };

    match result {
        Ok(code) => code,
        Err(e) => {
            log_error!("{}", e.message);
            eprintln!("Error: {}", e.message);
            e.code
        }
    }
}

fn die(msg: &str) -> u8 {
    eprintln!("Error: {msg}\n\n{HELP}");
    exit_code::USAGE
}

/// True for any accepted spelling of the version flag: the short `-V` / `-v`,
/// or a `--version` long flag in any letter case (`--version`, `--VERSION`,
/// `--Version`, …). Kept lenient on purpose so the two CLIs agree on every
/// spelling a user might reach for.
fn is_version_flag(arg: &str) -> bool {
    arg == "-V"
        || arg == "-v"
        || arg
            .strip_prefix("--")
            .is_some_and(|word| word.eq_ignore_ascii_case("version"))
}

/// Consume leading global flags (`--log-level`, `--config`,
/// `--no-irig-day-advisory`) and `-h`/`-V`.
///
/// Returns `Ok(token)` with the first non-flag token (the subcommand), or
/// `Err(code)` when the caller (`run`) should return that exit code
/// immediately — help or version was printed, or the command line was a usage
/// error (including no subcommand at all).
fn parse_global_flags(iter: &mut ArgIter<'_>, globals: &mut GlobalArgs) -> Result<String, u8> {
    loop {
        // Peek, because the first non-flag token is the SUBCOMMAND and belongs
        // to the caller. Split a clone so the joined and separated spellings
        // resolve here exactly as they do for subcommand flags -- globals are
        // parsed in this separate loop, so before the cursor each spelling had
        // a second, independent implementation.
        let Some(peeked) = iter.peek().cloned() else {
            eprint!("{HELP}");
            return Err(exit_code::USAGE);
        };
        let a = Arg::split(peeked);
        match a.name.as_str() {
            "-h" | "--help" if a.bare() => {
                print!("{HELP}");
                return Err(exit_code::SUCCESS);
            }
            // `--version=1` is not a version request: it declines here and is
            // returned below as the subcommand token, reporting itself as an
            // unknown command.
            s if a.bare() && is_version_flag(s) => {
                println!("aero1553 {VERSION}");
                return Err(exit_code::SUCCESS);
            }
            // POSIX end-of-options. Scoped to THIS parser: the subcommand
            // gets a fresh scan, so `-- decode rec.mie --no-mux` still honours
            // `--no-mux`. The next token becomes the subcommand verbatim, so
            // `-- --version` is an unknown command rather than a version
            // request -- which is what argparse does, `--` having turned it
            // into a positional.
            "--" if a.bare() => {
                iter.next(); // the separator itself
                return match iter.next() {
                    Some(token) => Ok(token),
                    None => {
                        eprint!("{HELP}");
                        Err(exit_code::USAGE)
                    }
                };
            }
            "--log-level" => {
                iter.next(); // the flag token itself
                match a.value("--log-level", iter) {
                    Ok(v) => globals.log_level = Some(v),
                    Err(_) => return Err(die("--log-level requires a value")),
                }
            }
            // Value-less, so `bare()` declines the `=value` spelling rather
            // than silently discarding it (same rule as `--no-mux`).
            "--no-irig-day-advisory" if a.bare() => {
                iter.next();
                globals.irig_day_advisory = Some(false);
            }
            "--config" => {
                iter.next();
                match a.value("--config", iter) {
                    // Its own message: a path, not a generic value.
                    Ok(v) => globals.config = Some(PathBuf::from(v)),
                    Err(_) => return Err(die("--config requires a path")),
                }
            }
            _ => {
                // `peek` just returned `Some`, so `next` cannot be `None`.
                // `unwrap_or_default` keeps the function total rather than
                // introducing a panic site (L1-ROB-001); an empty token would
                // fall through to the unknown-command arm, which is safe.
                return Ok(iter.next().unwrap_or_default());
            }
        }
    }
}

/// Turn a subcommand parse result into `Command`, or an `Err(code)` for `run`
/// to return: `HelpRequested` prints help and exits `0`; `Other` dies (usage).
/// Is a help request still pending in the unconsumed arguments?
///
/// An operator whose command line is wrong is the one most likely to have
/// asked for help, so help outranks a *deferred* diagnostic: an unknown
/// option, or a value this CLI validates after parsing. Python and C++ both
/// answer help for `decode --nonsense --help`; Rust reported the unknown
/// option and was the odd one out.
///
/// It does NOT outrank a failed value *consumption* -- see `next_value`, which
/// drains the iterator precisely so this returns `false` there. That split is
/// `argparse`'s: it raises immediately when it cannot structurally take a
/// value, and defers everything else until after the help action has had its
/// chance.
///
/// Bounded by `--`, because after the end-of-options separator a `-h` is a
/// path and not a request (L2-CLI-016).
fn help_pending(iter: &mut ArgIter<'_>) -> bool {
    for token in iter.by_ref() {
        if token == "--" {
            return false;
        }
        if token == "-h" || token == "--help" {
            return true;
        }
    }
    false
}

fn parse_or_help<T>(result: Result<T, ParseError>, iter: &mut ArgIter<'_>) -> Result<T, u8> {
    match result {
        Ok(c) => Ok(c),
        Err(ParseError::HelpRequested) => {
            print!("{HELP}");
            Err(exit_code::SUCCESS)
        }
        Err(ParseError::Other(e)) => {
            // A pending `-h` outranks a deferred diagnostic; see `help_pending`.
            if help_pending(iter) {
                print!("{HELP}");
                return Err(exit_code::SUCCESS);
            }
            Err(die(&e))
        }
    }
}

/// Dispatch `cmd_token` to its argument parser, wrapping the parsed args in a
/// `Command`. `Err(code)` means `run` should return that exit code.
fn parse_subcommand(cmd_token: &str, iter: &mut ArgIter<'_>) -> Result<Command, u8> {
    match cmd_token {
        "decode" => Ok(Command::Decode(Box::new(parse_or_help(
            parse_decode(iter),
            iter,
        )?))),
        "count" => Ok(Command::Count(parse_or_help(parse_count(iter), iter)?)),
        "dump" => Ok(Command::Dump(Box::new(parse_or_help(
            parse_dump(iter),
            iter,
        )?))),
        // No `-h`/`--help` arm: `parse_global_flags` intercepts both before a
        // subcommand token is ever produced, so one here was unreachable — until
        // `--` made it reachable, and then wrong. After the end-of-options
        // separator the next token is a subcommand NAME, so `-- -h` must report
        // an unknown command rather than print help, which is what `argparse`
        // does (there `-h` becomes a positional and fails the choice check).
        // An unrecognised command is a deferred diagnostic like any other, so a
        // pending `-h` outranks it: `--nonsense --help` prints help, matching
        // both of the other implementations.
        other => {
            if help_pending(iter) {
                print!("{HELP}");
                return Err(exit_code::SUCCESS);
            }
            Err(die(&format!("Unknown command: {other:?}")))
        }
    }
}

// ── Subcommand parsing ────────────────────────────────────────────────

type ArgIter<'a> = std::iter::Peekable<std::iter::Skip<std::vec::IntoIter<String>>>;

/// Outcome of parsing subcommand arguments.
///
/// `HelpRequested` is a control-flow signal, not a failure: the user
/// passed `-h`/`--help` and the caller (`run`) is responsible for
/// printing help text and returning the appropriate exit code. Library
/// helpers MUST NOT call `std::process::exit` directly; that decision
/// belongs to the binary entry point.
#[derive(Debug)]
pub enum ParseError {
    HelpRequested,
    Other(String),
}

impl From<String> for ParseError {
    fn from(s: String) -> Self {
        Self::Other(s)
    }
}

/// Does this token look like an option rather than a value?
///
/// Used to decide whether the **separated** form may consume the next token.
/// Without this, `--mux-delimiter --no-mux` set the delimiter to the string
/// `"--no-mux"` and the `--no-mux` flag silently never ran — a wrong decode
/// that exited 0. Refusing turns that into a usage error, which is the whole
/// value of the check: the failure was silent, not merely inconsistent.
///
/// The rule is `argparse`'s, deliberately, because Python behaves this way and
/// the alternative was to make two implementations agree with each other and
/// disagree with the third. Its three exemptions are not quirks to be tidied
/// away — each keeps a real invocation working:
///
/// * **A lone `-`** is a value. `-o -` writes a file named `-` (L2-CLI-005).
/// * **Anything beginning like a number** is a value; see
///   [`starts_like_a_number`], which is also where the one version-dependent
///   corner of `argparse` is documented. `--mux-field -1` is a documented
///   feature — negative indices count from the end — and
///   `--collapse-window-us -5` must still reach its own validator to be
///   refused for being negative, rather than being refused here for looking
///   like a flag.
/// * **A token containing a space** is a value, since no option is spelled
///   that way.
///
/// Note what this does NOT cover: the joined form. `--mux-delimiter=--no-mux`
/// is unambiguous and stays legal in all three implementations, which is why
/// it is the documented way to pass a value that looks like a flag.
fn looks_like_option(token: &str) -> bool {
    if !token.starts_with('-') || token.chars().count() < 2 || token.contains(' ') {
        return false;
    }
    !starts_like_a_number(token)
}

/// Does this token begin like a number? `argparse`'s `-\.?\d`, applied as a
/// **prefix** test the way `re.match` applies it — a dash, an optional decimal
/// point, then a digit. Hand-rolled because this crate has no regex dependency
/// and is not going to acquire one for three lines.
///
/// This deliberately accepts more than "is a number": `-5e3`, `-0x5` and `-1a`
/// all begin like one and are therefore values. That is the point. Such a
/// token is far likelier to be a mistyped number than a flag, and letting it
/// through means the flag's **own** validator reports it, naming the bad
/// value — `--mux-field -1a` gives `invalid --mux-field: "-1a"` instead of the
/// much less helpful "the next argument is an option".
///
/// **This rule is version-dependent in Python and we pin the newer one.**
/// Through 3.13 `argparse` used an anchored full match,
/// `^-\d+$|^-\d*\.\d+$`, so only plain decimals were exempt; 3.14 replaced
/// it with the prefix test above. Since this project supports Python 3.10
/// through 3.14, `argparse` does not agree with *itself* across the supported
/// range, and no choice here can match all of them. We follow 3.14: it is
/// simpler, it is where Python is going, and it is the more permissive of the
/// two, so adopting it cannot newly reject an invocation that used to work.
///
/// The shapes this disagrees with Python 3.10-3.13 about — dash, digit, then
/// junk — are consequently **outside** the specified contract (L2-CLI-015) and
/// are kept out of the conformance suite. What every version agrees on, and
/// what the contract does cover, is that a token spelled like an option name
/// (`-x`, `-o`, `--foo`) is refused and a plain decimal is not.
fn starts_like_a_number(token: &str) -> bool {
    let Some(rest) = token.strip_prefix('-') else {
        return false;
    };
    // The decimal point is optional; a digit after it is not.
    let rest = rest.strip_prefix('.').unwrap_or(rest);
    rest.bytes().next().is_some_and(|b| b.is_ascii_digit())
}

fn next_value(name: &str, iter: &mut ArgIter<'_>) -> Result<String, String> {
    // A flag that cannot get its value is a HARD stop, and the error arms
    // below drain the iterator to say so. Everything after such a flag is
    // uninterpretable -- we do not know which tokens were meant to be its
    // value -- so nothing in it may be honoured, including a `-h`.
    //
    // That is the line `argparse` draws too: it raises immediately when it
    // cannot structurally take a value, so `--log-level -h` and
    // `--config --help` are usage errors there, while a DEFERRED diagnostic
    // (an unknown option, or a value validated after parsing) lets a later
    // help request win. Draining is what makes `help_pending` answer `false`
    // here, without adding a variant to the public `ParseError`.
    let failure = match iter.peek() {
        None => format!("{name} requires a value"),
        Some(token) if looks_like_option(token) => format!(
            "{name} requires a value, but the next argument is an option: {token}; \
             to pass it as a value, write {name}={token}"
        ),
        // `peek` returned `Some`, so `next` cannot be `None`.
        Some(_) => return Ok(iter.next().unwrap_or_default()),
    };
    while iter.next().is_some() {}
    Err(failure)
}

/// One argument token, split at the first `=`.
///
/// `--flag=value` and `--flag value` are the same invocation, and every flag
/// that takes a value accepts both. Doing that split *once*, here, is what lets
/// a flag be handled by a single match arm: before this the two spellings were
/// two arms apiece — an exact-name arm and a `starts_with("--flag=")` arm that
/// re-sliced the token by a hard-coded prefix length — repeated at 26 sites. A
/// flag added with only the first arm would silently reject the joined form,
/// and no gate would have noticed: `cli-surface-parity` compares flag *names*.
///
/// Three rules, each load-bearing and each pinned by a test:
///
/// * **Only `--` tokens split.** A positional path may contain `=`
///   (`a=b.mie`), and `-o=out.csv` is not a spelling this CLI accepts — both
///   must survive untouched.
/// * **Only the first `=` separates**, so `--mux-delimiter==` sets the
///   delimiter to `=`.
/// * **`--flag=` is an empty value, not a missing one.** The flag's own
///   validator then decides: an empty filter list is fine, an empty delimiter
///   is not. C++ got this wrong in the other direction and reported an unknown
///   option where Rust and Python decoded normally.
///
/// `raw` is kept because the unknown-option message quotes the token as the
/// user typed it, and a flag that takes no value rejects `--flag=x` by falling
/// through to exactly that message.
struct Arg {
    /// The token exactly as it arrived.
    raw: String,
    /// The flag name: everything before the first `=`, or the whole token.
    name: String,
    /// The value that was joined with `=`, if any. `Some("")` for `--flag=`.
    inline: Option<String>,
}

impl Arg {
    fn split(raw: String) -> Self {
        // Single-dash and bare tokens are never split; see the type docs.
        if raw.starts_with("--")
            && let Some(at) = raw.find('=')
        {
            return Self {
                name: raw[..at].to_string(),
                inline: Some(raw[at + 1..].to_string()),
                raw,
            };
        }
        Self {
            name: raw.clone(),
            inline: None,
            raw,
        }
    }

    /// The value for a flag that requires one: the joined value if there was
    /// one, otherwise the next token.
    ///
    /// `name` is passed rather than read from `self.name` so the "requires a
    /// value" message names the long form even when the short one was used
    /// (`-o` reports `--output`), which is what the message did before.
    fn value(&self, name: &str, iter: &mut ArgIter<'_>) -> Result<String, String> {
        match &self.inline {
            Some(v) => Ok(v.clone()),
            None => next_value(name, iter),
        }
    }

    /// True when this token carried no `=`, which is the guard a value-less
    /// flag needs: `--no-mux=true` must not quietly set the flag and discard
    /// the value, so the arm declines and the token falls through to the
    /// unknown-option message carrying `raw`.
    fn bare(&self) -> bool {
        self.inline.is_none()
    }
}

fn parse_int_value(s: &str, name: &str) -> Result<usize, String> {
    let s = s.trim();
    let parsed = if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        usize::from_str_radix(hex, 16)
    } else {
        s.parse::<usize>()
    };
    parsed.map_err(|_| format!("{name} expected integer, got {s:?}"))
}

/// Parse a MIL-STD-1553 RT address or subaddress filter value: [0, 31].
///
/// Checking only that the value fit a `u8` let `--include-rts 40` through to
/// a filter no record can match, so the decode exited `0` with an empty CSV.
/// The config loader (`parse_int_rt_sa`) and the C++ CLI already rejected it.
fn parse_rt_sa_value(s: &str, name: &str) -> Result<u8, String> {
    parse_int_value(s, name).and_then(|n| match u8::try_from(n) {
        Ok(v) if v <= 31 => Ok(v),
        _ => Err(format!(
            "{name} value out of MIL-STD-1553 range [0, 31]: {n}"
        )),
    })
}

/// Split a single value on commas (trimmed, empties dropped).
///
/// Replaces the old greedy "consume tokens until next flag" helper, which
/// produced surprising behavior when a positional argument followed a
/// filter flag (`--include-rts 15 file.mie` ate `file.mie` as another RT).
/// Filter flags now take one value; pass multiple with commas
/// (`--include-rts 15,31`) or by repeating the flag
/// (`--include-rts 15 --include-rts 31`).
fn split_csv(s: &str) -> Vec<String> {
    s.split(',')
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect()
}

/// Parse a comma-separated filter value and push each parsed item into `target`.
/// Shared by all `--exclude-*` / `--include-*` flags so their per-value loop
/// lives in one place.
fn push_filter<T>(
    csv: &str,
    target: &mut Vec<T>,
    parse: impl Fn(&str) -> Result<T, ParseError>,
) -> Result<(), ParseError> {
    for v in split_csv(csv) {
        target.push(parse(&v)?);
    }
    Ok(())
}

/// One `--exclude-types` / `--include-types` element → a message-type code.
fn parse_type_filter(v: &str) -> Result<u8, ParseError> {
    parse_type_name(v).map_err(|e| e.0.into())
}

/// One `--exclude-buses` / `--include-buses` element → a `Bus`.
fn parse_bus_filter(v: &str) -> Result<crate::models::Bus, ParseError> {
    parse_bus_name(v).map_err(|e| e.0.into())
}

fn parse_decode(iter: &mut ArgIter<'_>) -> Result<DecodeArgs, ParseError> {
    let mut args = DecodeArgs::default();
    // POSIX end-of-options: the FIRST `--` is dropped and everything after it
    // is a path, however it is spelled. A later `--` is an ordinary
    // positional, which is the only way to name a file called `--`.
    let mut end_of_options = false;

    while let Some(token) = iter.next() {
        if end_of_options {
            args.inputs.push(PathBuf::from(token));
            continue;
        }
        if token == "--" {
            end_of_options = true;
            continue;
        }
        // One arm per flag: `Arg::split` has already resolved `--flag=value`
        // against `--flag value`, so neither spelling appears below.
        let a = Arg::split(token);
        match a.name.as_str() {
            "-o" | "--output" => {
                args.output = Some(PathBuf::from(a.value("--output", iter)?));
            }
            // Value-less flags decline a joined value rather than discard it;
            // `--no-mux=true` falls through to the unknown-option arm.
            "--separate-errors" if a.bare() => args.separate_errors = true,
            "--no-clobber" if a.bare() => args.no_clobber = true,
            "--allow-partial" if a.bare() => args.allow_partial = true,
            "--strict" if a.bare() => args.strict = Some(true),
            "--no-mux" if a.bare() => args.no_mux = true,
            "--collapse-duplicates" if a.bare() => args.collapse_duplicates = true,
            "--input-time-format" => {
                args.input_time_format = Some(parse_input_time_format_arg(
                    &a.value("--input-time-format", iter)?,
                )?);
            }
            "--output-time-format" => {
                args.output_time_format = Some(parse_output_time_format_arg(
                    &a.value("--output-time-format", iter)?,
                )?);
            }
            "--year" => {
                args.year = Some(parse_year_arg(&a.value("--year", iter)?)?);
            }
            "--utc-offset" => {
                args.utc_offset_minutes =
                    Some(parse_utc_offset_arg(&a.value("--utc-offset", iter)?)?);
            }
            // L2-CLI-019: `--time-format` was split in v3.0.0. This arm exists
            // so the diagnostic can name both replacements -- the generic
            // unknown-option path below would report only that the flag is
            // unrecognised, which is the one thing an operator hitting this
            // already knows. Matched with no `a.bare()` guard so the joined
            // form (`--time-format=irig`) lands here too.
            "--time-format" => {
                return Err(ParseError::Other(RETIRED_TIME_FORMAT_MESSAGE.to_string()));
            }
            "--detect-records" => {
                args.detect_records =
                    Some(parse_detect_records(&a.value("--detect-records", iter)?)?);
            }
            "--lookahead-records" => {
                args.lookahead_records = Some(parse_lookahead_records(
                    &a.value("--lookahead-records", iter)?,
                )?);
            }
            "--standard-tick-rate-hz" => {
                args.standard_tick_rate_hz = Some(parse_standard_tick_rate_hz(
                    &a.value("--standard-tick-rate-hz", iter)?,
                )?);
            }
            "--format" => {
                args.output_format = Some(parse_output_format_arg(&a.value("--format", iter)?)?);
            }
            "--mux-delimiter" => {
                args.mux_delimiter = Some(parse_mux_delimiter(&a.value("--mux-delimiter", iter)?)?);
            }
            "--mux-field" => {
                args.mux_field = Some(parse_mux_field(&a.value("--mux-field", iter)?)?);
            }
            "--max-sort-group" => {
                args.max_sort_group =
                    Some(parse_max_sort_group(&a.value("--max-sort-group", iter)?)?);
            }
            "--delta-scope" => {
                args.delta_scope = Some(parse_delta_scope(&a.value("--delta-scope", iter)?)?);
            }
            "--collapse-window-us" => {
                args.collapse_window_us = Some(parse_collapse_window_us(
                    &a.value("--collapse-window-us", iter)?,
                )?);
            }
            "--max-collapse-survivors" => {
                args.max_collapse_survivors = Some(parse_max_collapse_survivors(
                    &a.value("--max-collapse-survivors", iter)?,
                )?);
            }
            "--manifest" => {
                args.manifest = Some(PathBuf::from(a.value("--manifest", iter)?));
            }
            "--glob" => args.glob = Some(a.value("--glob", iter)?),
            // Filter flags: each takes ONE value. Multiple values either
            // repeat the flag or comma-separate within one value:
            //   --include-rts 15
            //   --include-rts 15,20,31
            //   --include-rts 15 --include-rts 31
            // Any of those leaves trailing positionals like `file.mie`
            // free to bind to `args.inputs`.
            "--exclude-types" => push_filter(
                &a.value("--exclude-types", iter)?,
                &mut args.exclude_types,
                parse_type_filter,
            )?,
            "--include-types" => push_filter(
                &a.value("--include-types", iter)?,
                &mut args.include_types,
                parse_type_filter,
            )?,
            "--exclude-rts" => push_filter(
                &a.value("--exclude-rts", iter)?,
                &mut args.exclude_rts,
                |v| parse_rt_sa_value(v, "--exclude-rts").map_err(Into::into),
            )?,
            "--include-rts" => push_filter(
                &a.value("--include-rts", iter)?,
                &mut args.include_rts,
                |v| parse_rt_sa_value(v, "--include-rts").map_err(Into::into),
            )?,
            "--exclude-buses" => push_filter(
                &a.value("--exclude-buses", iter)?,
                &mut args.exclude_buses,
                parse_bus_filter,
            )?,
            "--include-buses" => push_filter(
                &a.value("--include-buses", iter)?,
                &mut args.include_buses,
                parse_bus_filter,
            )?,
            "--exclude-subaddresses" => push_filter(
                &a.value("--exclude-subaddresses", iter)?,
                &mut args.exclude_subaddresses,
                |v| parse_rt_sa_value(v, "--exclude-subaddresses").map_err(Into::into),
            )?,
            "--include-subaddresses" => push_filter(
                &a.value("--include-subaddresses", iter)?,
                &mut args.include_subaddresses,
                |v| parse_rt_sa_value(v, "--include-subaddresses").map_err(Into::into),
            )?,
            "-h" | "--help" if a.bare() => return Err(ParseError::HelpRequested),
            // `raw`, not `name`: the message quotes what was typed, so
            // `--no-mux=true` reports itself in full.
            _ if a.name.starts_with('-') => {
                return Err(format!("unknown decode option: {}", a.raw).into());
            }
            // Positional input path(s). One or more is accepted; more than one
            // resolved input triggers the time-sorted merge (L2-MRG-001).
            _ => args.inputs.push(PathBuf::from(a.raw)),
        }
    }

    // Exactly one input *method* (positionals XOR --manifest XOR --glob).
    let methods = usize::from(!args.inputs.is_empty())
        + usize::from(args.manifest.is_some())
        + usize::from(args.glob.is_some());
    if methods == 0 {
        return Err(
            "decode requires an input file (positional, --manifest, or --glob)"
                .to_string()
                .into(),
        );
    }
    if methods > 1 {
        return Err(
            "decode accepts only one input method: positional paths, --manifest, or --glob -- not a combination"
                .to_string()
                .into(),
        );
    }
    Ok(args)
}

fn parse_count(iter: &mut ArgIter<'_>) -> Result<PathBuf, ParseError> {
    let mut path: Option<PathBuf> = None;
    let mut end_of_options = false;
    for token in iter.by_ref() {
        if !end_of_options && token == "--" {
            end_of_options = true;
            continue;
        }
        if end_of_options {
            if path.is_some() {
                return Err(format!("unexpected positional argument: {token}").into());
            }
            path = Some(PathBuf::from(token));
            continue;
        }
        // `count` has no value-taking flag today, but it goes through the same
        // cursor so that adding one cannot reintroduce a per-flag `=` arm.
        let a = Arg::split(token);
        match a.name.as_str() {
            "-h" | "--help" if a.bare() => return Err(ParseError::HelpRequested),
            _ if a.name.starts_with('-') => {
                return Err(format!("unknown count option: {}", a.raw).into());
            }
            _ => {
                if path.is_some() {
                    return Err(format!("unexpected positional argument: {}", a.raw).into());
                }
                path = Some(PathBuf::from(a.raw));
            }
        }
    }
    path.ok_or_else(|| ParseError::Other("count requires an input file".to_string()))
}

fn parse_dump(iter: &mut ArgIter<'_>) -> Result<DumpArgs, ParseError> {
    let mut args = DumpArgs::default();
    let mut input_seen = false;
    let mut end_of_options = false;

    while let Some(token) = iter.next() {
        if !end_of_options && token == "--" {
            end_of_options = true;
            continue;
        }
        if end_of_options {
            if input_seen {
                return Err(format!("unexpected positional argument: {token}").into());
            }
            args.input = PathBuf::from(token);
            input_seen = true;
            continue;
        }
        let a = Arg::split(token);
        match a.name.as_str() {
            "--raw" if a.bare() => args.raw = true,
            "--offset" => {
                args.offset = parse_int_value(&a.value("--offset", iter)?, "--offset")?;
            }
            "--length" => {
                args.length = Some(parse_int_value(&a.value("--length", iter)?, "--length")?);
            }
            "--records" => {
                args.records =
                    Some(parse_int_value(&a.value("--records", iter)?, "--records")? as u64);
            }
            "-h" | "--help" if a.bare() => return Err(ParseError::HelpRequested),
            _ if a.name.starts_with('-') => {
                return Err(format!("unknown dump option: {}", a.raw).into());
            }
            _ => {
                if input_seen {
                    return Err(format!("unexpected positional argument: {}", a.raw).into());
                }
                args.input = PathBuf::from(a.raw);
                input_seen = true;
            }
        }
    }

    if !input_seen {
        return Err("dump requires an input file".to_string().into());
    }
    Ok(args)
}

fn parse_input_time_format_arg(s: &str) -> Result<TimestampFormat, String> {
    TimestampFormat::from_name_ci(s)
        .ok_or_else(|| format!("invalid --input-time-format: {s:?}; valid: auto, irig, standard"))
}

fn parse_output_time_format_arg(s: &str) -> Result<OutputTimeFormat, String> {
    OutputTimeFormat::from_name_ci(s)
        .ok_or_else(|| format!("invalid --output-time-format: {s:?}; valid: doy, iso, dom"))
}

/// L2-CLI-018: `--year YYYY`, range-checked at parse time so a bad value is a
/// usage error rather than a malformed cell a hundred thousand rows later.
fn parse_year_arg(s: &str) -> Result<u16, String> {
    let invalid = || format!("invalid --year: {s:?}; valid range: [{YEAR_MIN}, {YEAR_MAX}]");
    // Parsed as `u32` first so that an out-of-range four-plus-digit year
    // reports the range rather than an integer-overflow message.
    let value: u32 = s.parse().map_err(|_| invalid())?;
    match u16::try_from(value) {
        Ok(y) if (YEAR_MIN..=YEAR_MAX).contains(&y) => Ok(y),
        _ => Err(invalid()),
    }
}

/// L2-CLI-018: `--utc-offset Z|+HH:MM|-HH:MM`, shared with the config loader's
/// grammar so the two spellings cannot drift.
fn parse_utc_offset_arg(s: &str) -> Result<i16, String> {
    crate::config::parse_utc_offset(s).map_err(|_| {
        format!(
            "invalid --utc-offset: {s:?}; valid: Z, or +HH:MM / -HH:MM \
             with HH in [0, 23] and MM in [0, 59]"
        )
    })
}

/// L2-CLI-019: what `--time-format` says now that it has been split in two.
///
/// The message names both replacements and states which concern each covers,
/// because the operator's intent is still expressible and the only open
/// question is which of the two they meant -- exactly what a generic "unknown
/// option" cannot answer.
const RETIRED_TIME_FORMAT_MESSAGE: &str = "\
--time-format was split in v3.0.0 and is no longer accepted. Use \
--input-time-format auto|irig|standard to choose how timestamps are PARSED \
from the file, or --output-time-format doy|iso|dom to choose how they are \
WRITTEN to the CSV.";

/// L1-EXIT-007: validate `--format` where every other flag value is validated
/// -- at parse time, so an unsupported value is a **usage** error (exit `4`).
///
/// This used to be checked after the config layer had merged CLI overrides,
/// which made `--format json` a *runtime* error (exit `1`) and put the CLI in
/// direct contradiction with L1-EXIT-007's "invalid flag value SHALL exit 4".
/// The three exit codes for one mistake were: `4` for a bad `--input-time-format`,
/// `1` for a bad `--format`, and `5` for the same value in a config file.
/// Only the middle one was wrong.
///
/// The config-file spelling stays a **load-time** error (exit `5`, L2-CFG-010)
/// -- a bad value in a file is a configuration error, not a mistyped command.
fn parse_output_format_arg(s: &str) -> Result<String, String> {
    if s == "csv" {
        return Ok(s.to_string());
    }
    Err(format!("invalid --format: {s:?}; valid: csv"))
}

/// L2-DEC-015: validate the `--detect-records` argument against the
/// `[1, 32]` range pinned by `DETECT_RECORDS_MIN` / `DETECT_RECORDS_MAX`.
/// The same range is checked at config-load time for the TOML form;
/// duplicating the validation here surfaces malformed CLI input with a
/// clear error before the config layer is even consulted.
fn parse_detect_records(s: &str) -> Result<usize, String> {
    let n: usize = s
        .trim()
        .parse()
        .map_err(|_| format!("invalid --detect-records: {s:?}; must be an integer"))?;
    if !(crate::config::DETECT_RECORDS_MIN..=crate::config::DETECT_RECORDS_MAX).contains(&n) {
        return Err(format!(
            "invalid --detect-records: {n}; valid range: [{}, {}]",
            crate::config::DETECT_RECORDS_MIN,
            crate::config::DETECT_RECORDS_MAX
        ));
    }
    Ok(n)
}

/// L2-SYN-026: validate the `--lookahead-records` argument against
/// `[1, 32]`. Same shape as `parse_detect_records`.
fn parse_lookahead_records(s: &str) -> Result<usize, String> {
    let n: usize = s
        .trim()
        .parse()
        .map_err(|_| format!("invalid --lookahead-records: {s:?}; must be an integer"))?;
    if !(crate::config::LOOKAHEAD_RECORDS_MIN..=crate::config::LOOKAHEAD_RECORDS_MAX).contains(&n) {
        return Err(format!(
            "invalid --lookahead-records: {n}; valid range: [{}, {}]",
            crate::config::LOOKAHEAD_RECORDS_MIN,
            crate::config::LOOKAHEAD_RECORDS_MAX
        ));
    }
    Ok(n)
}

/// L2-DEC-017 / L2-CLI-012: validate the `--standard-tick-rate-hz`
/// argument. Mirrors the config-load validation in
/// `config::parse_into_config` so the CLI and TOML paths reject the same
/// inputs with the same shape of message: the rate must be a finite,
/// strictly-positive frequency.
fn parse_standard_tick_rate_hz(s: &str) -> Result<f64, String> {
    let hz: f64 = s
        .trim()
        .parse()
        .map_err(|_| format!("invalid --standard-tick-rate-hz: {s:?}; must be a number"))?;
    if !hz.is_finite() || hz <= 0.0 {
        return Err(format!(
            "invalid --standard-tick-rate-hz: {hz}; must be a finite value greater than 0"
        ));
    }
    Ok(hz)
}

fn parse_mux_delimiter(s: &str) -> Result<String, String> {
    if s.is_empty() {
        return Err("invalid --mux-delimiter: must be a non-empty string".to_string());
    }
    Ok(s.to_string())
}

fn parse_mux_field(s: &str) -> Result<i64, String> {
    s.trim()
        .parse::<i64>()
        .map_err(|_| format!("invalid --mux-field: {s:?}; must be an integer"))
}

/// `--max-sort-group` (L2-WRT-022): cap on one buffered equal-timestamp run.
/// Range-checked here so a bad value is a usage error (exit 4) rather than a
/// silent clamp, mirroring `--detect-records`.
fn parse_max_sort_group(s: &str) -> Result<usize, String> {
    let n: usize = s
        .trim()
        .parse()
        .map_err(|_| format!("invalid --max-sort-group: {s:?}; must be an integer"))?;
    if !(crate::order::MAX_SORT_GROUP_MIN..=crate::order::MAX_SORT_GROUP_MAX).contains(&n) {
        return Err(format!(
            "invalid --max-sort-group: {n}; valid range: [{}, {}]",
            crate::order::MAX_SORT_GROUP_MIN,
            crate::order::MAX_SORT_GROUP_MAX
        ));
    }
    Ok(n)
}

/// `--max-collapse-survivors` (L2-MRG-008): cap on the de-duplication survivor
/// set. Range-checked here so a bad value is a usage error (exit 4) rather than
/// a silent clamp, mirroring `--max-sort-group`.
fn parse_max_collapse_survivors(s: &str) -> Result<usize, String> {
    let n: usize = s
        .trim()
        .parse()
        .map_err(|_| format!("invalid --max-collapse-survivors: {s:?}; must be an integer"))?;
    if !(crate::merge::MAX_COLLAPSE_SURVIVORS_MIN..=crate::merge::MAX_COLLAPSE_SURVIVORS_MAX)
        .contains(&n)
    {
        return Err(format!(
            "invalid --max-collapse-survivors: {n}; valid range: [{}, {}]",
            crate::merge::MAX_COLLAPSE_SURVIVORS_MIN,
            crate::merge::MAX_COLLAPSE_SURVIVORS_MAX
        ));
    }
    Ok(n)
}

/// `--delta-scope` (L2-MRG-005). Shares `DeltaScope::from_name_ci` with the
/// config loader so the CLI and TOML accept exactly the same spellings.
fn parse_delta_scope(s: &str) -> Result<crate::models::DeltaScope, String> {
    crate::models::DeltaScope::from_name_ci(s.trim())
        .ok_or_else(|| format!("invalid --delta-scope: {s:?}. Valid: per-file, global"))
}

fn parse_collapse_window_us(s: &str) -> Result<i64, String> {
    match s.trim().parse::<i64>() {
        Ok(n) if n >= 0 => Ok(n),
        _ => Err(format!(
            "invalid --collapse-window-us: {s:?}; must be a non-negative integer"
        )),
    }
}

// ── Subcommand runners ────────────────────────────────────────────────

/// Apply a log-level string. Returns Err on an unrecognized name so the
/// caller can surface the failure instead of silently no-op'ing.
///
/// `source` is included in the error for diagnosability (it'll be
/// `--log-level` for CLI-supplied values, `[logging].level` for config
/// file values). Validated names are DEBUG, INFO, WARNING, ERROR,
/// CRITICAL (CRITICAL maps to OFF).
fn apply_log_level(source: &str, value: &str) -> Result<(), String> {
    match Level::parse(value) {
        Some(lvl) => {
            log::set_level(lvl);
            Ok(())
        }
        None => Err(format!(
            "invalid {source}: {value:?}; valid: DEBUG, INFO, WARNING, WARN, ERROR, CRITICAL, OFF"
        )),
    }
}

/// Load `--config` (or the built-in defaults if none was specified) and
/// apply log-level precedence: config overrides the `run()` default; CLI
/// overrides config. Used by every subcommand so a malformed config
/// file is rejected uniformly regardless of whether you ran `decode`,
/// `count`, or `dump`.
fn resolve_config(globals: &GlobalArgs) -> Result<DecoderConfig, CliError> {
    let cfg = load_config(globals.config.as_deref()).map_err(|e| CliError::config(e.0))?;

    // The config file's log_level is validated at load time (see
    // config::parse_into_config), so apply_log_level cannot fail here
    // unless someone constructed a DecoderConfig manually with a bogus
    // string — treat that as a configuration error.
    apply_log_level("[logging].level (in config)", &cfg.log_level).map_err(CliError::config)?;

    // An invalid `--log-level` is a CLI usage error. In practice run()
    // validates it earlier (and exits via die()), so this is defensive.
    if let Some(s) = &globals.log_level {
        apply_log_level("--log-level", s).map_err(CliError::usage)?;
    }

    // L2-LOG-001, same precedence as the level above: config file first, CLI
    // on top. Applied here rather than threaded through `ReaderOptions` so it
    // covers `decode`, `count` and `dump` from one place -- every subcommand
    // reaches the reader through this function.
    log::set_irig_day_advisory(globals.irig_day_advisory.unwrap_or(cfg.irig_day_advisory));

    Ok(cfg)
}

/// Open the input file and configure the reader from `cfg`. The
/// String-flavored error type is what every subcommand runner returns,
/// so the conversion is folded in here.
/// Resolve the merged configuration into a [`TimeRender`], enforcing the
/// L2-WRT-026 clause 1 precondition.
///
/// The check lives here rather than in either loader because neither can answer
/// it alone: a config file may set the rendering and the CLI supply the year,
/// or the reverse. It runs before the output is opened, so a run that cannot
/// produce the requested dates writes nothing at all.
fn resolve_time_render(cfg: &DecoderConfig) -> Result<TimeRender, CliError> {
    if cfg.output_time_format.needs_calendar() && cfg.year.is_none() {
        return Err(CliError::usage(format!(
            "--output-time-format {} needs a calendar year, and an MIE recording \
             does not carry one: IRIG-B encodes day-of-year but not the year. \
             Set [output] year = YYYY in a config file or pass --year YYYY. \
             (--output-time-format doy needs no year.)",
            cfg.output_time_format.as_str()
        )));
    }
    Ok(TimeRender {
        format: cfg.output_time_format,
        year: cfg.year,
        utc_offset_minutes: cfg.utc_offset_minutes,
    })
}

fn open_reader(path: &Path, cfg: &DecoderConfig) -> Result<MieFileReader, CliError> {
    MieFileReader::with_options(
        path,
        ReaderOptions {
            strict: cfg.strict,
            input_time_format: cfg.input_time_format,
            detect_records: cfg.detect_records,
            lookahead_records: cfg.lookahead_records,
            standard_tick_rate_hz: cfg.standard_tick_rate_hz,
            mux_enabled: cfg.mux_enabled,
            mux_delimiter: cfg.mux_delimiter.clone(),
            mux_field: cfg.mux_field,
            // `Some(_)` only when a calendar rendering is actually in force, so
            // a `year` left in a site config does not change the reader's
            // diagnostics for a `doy` run (L2-WRT-026 clause 5).
            calendar_year: if cfg.output_time_format.needs_calendar() {
                cfg.year
            } else {
                None
            },
        },
    )
    .map_err(|e| CliError::runtime(format_mie_error(e)))
}

/// Assemble the CLI `ConfigOverrides` from parsed decode args (CLI > config >
/// default). The filter vectors are moved out of `args` (leaving them empty),
/// so `args.output` stays usable afterward.
fn build_config_overrides(
    args: &mut DecodeArgs,
    log_level: Option<String>,
    irig_day_advisory: Option<bool>,
) -> ConfigOverrides {
    ConfigOverrides {
        irig_day_advisory,
        input_time_format: args.input_time_format,
        strict: args.strict,
        // `--separate-errors` opts into the split-file mode; its absence leaves
        // the config value intact (the built-in default is now Inline).
        error_mode: if args.separate_errors {
            Some(ErrorMode::Separate)
        } else {
            None
        },
        output_format: args.output_format.clone(),
        output_time_format: args.output_time_format,
        year: args.year,
        utc_offset_minutes: args.utc_offset_minutes,
        // A CLI flag flips the option on; absence leaves the config value intact
        // (Some(false) would clobber a `true` from the config).
        no_clobber: if args.no_clobber { Some(true) } else { None },
        allow_partial: if args.allow_partial { Some(true) } else { None },
        detect_records: args.detect_records,
        lookahead_records: args.lookahead_records,
        standard_tick_rate_hz: args.standard_tick_rate_hz,
        // --no-mux disables MUX population; absence leaves the config value.
        mux_enabled: if args.no_mux { Some(false) } else { None },
        mux_delimiter: args.mux_delimiter.clone(),
        mux_field: args.mux_field,
        collapse_duplicates: if args.collapse_duplicates {
            Some(true)
        } else {
            None
        },
        collapse_window_us: args.collapse_window_us,
        max_sort_group: args.max_sort_group,
        max_collapse_survivors: args.max_collapse_survivors,
        delta_scope: args.delta_scope,
        exclude_types: std::mem::take(&mut args.exclude_types),
        exclude_rts: std::mem::take(&mut args.exclude_rts),
        exclude_buses: std::mem::take(&mut args.exclude_buses),
        exclude_subaddresses: std::mem::take(&mut args.exclude_subaddresses),
        include_types: std::mem::take(&mut args.include_types),
        include_rts: std::mem::take(&mut args.include_rts),
        include_buses: std::mem::take(&mut args.include_buses),
        include_subaddresses: std::mem::take(&mut args.include_subaddresses),
        log_level,
    }
}

/// Open a reader per resolved input (L2-MRG-001). Under `--allow-partial` a
/// *merge* tolerates a per-file open failure — it drops that input with a WARN
/// and reports `open_dropped = true` so the batch commits a `.partial`
/// (L2-MRG-004). A single-input decode propagates the open error. Returns the
/// opened readers and whether any input was dropped.
fn open_all_readers(
    input_paths: &[PathBuf],
    cfg: &DecoderConfig,
    merge_requested: bool,
) -> Result<(Vec<MieFileReader>, bool), CliError> {
    let mut readers: Vec<MieFileReader> = Vec::with_capacity(input_paths.len());
    let mut open_dropped = false;
    for p in input_paths {
        match open_reader(p, cfg) {
            Ok(r) => readers.push(r),
            Err(e) if merge_requested && cfg.allow_partial => {
                log_warn!(
                    "merge: input {} could not be opened; truncating it from the \
                     merge (--allow-partial): {}",
                    p.display(),
                    e.message
                );
                open_dropped = true;
            }
            Err(e) => return Err(e),
        }
    }
    Ok((readers, open_dropped))
}

/// Run the decode (single input) or k-way merge (two or more) and return the
/// writer result. `Err(code)` is an early exit: an incompatible-merge or
/// prime-time failure (L2-MRG-003) whose exit code is chosen here.
fn execute_decode_or_merge(
    readers: &[MieFileReader],
    cfg: &DecoderConfig,
    output: Option<&Path>,
    write_opts: WriteOptions,
    merge_requested: bool,
    open_dropped: bool,
) -> Result<crate::error::MieResult<crate::writer::WriteOutcome>, u8> {
    if !merge_requested {
        // L2-WRT-021: canonical row order is the LAST stage before the writer, so
        // the guarantee holds over exactly the rows that reach the CSV.
        let messages = readers[0]
            .iter()
            .filter_messages(cfg.filters.clone())
            .order_rows(cfg.max_sort_group);
        return Ok(write_messages(messages, output, cfg.error_mode, write_opts));
    }

    match crate::merge::MergedRecordIter::new(
        readers,
        cfg.standard_tick_rate_hz,
        cfg.allow_partial,
        cfg.strict,
    ) {
        Ok(merged) => {
            let merged = merged
                .collapse(cfg.collapse_duplicates, cfg.collapse_window_us)
                .max_collapse_survivors(cfg.max_collapse_survivors)
                .delta_scope(cfg.delta_scope);
            // Clone the suppressed-duplicate counter before the writer consumes
            // the iterator, then report it after (L2-MRG-007).
            let collapsed = merged.collapsed_handle();
            // An input dropped at open time surfaces a terminal after the good
            // rows so the writer commits a `.partial` (L2-MRG-004).
            let open_tail =
                open_dropped.then_some(Err(crate::error::MieError::UnrecoverableSyncLoss {
                    offset: 0,
                    sync_losses: 0,
                }));
            // L2-WRT-021: order_rows sits after the merge and the filters, and
            // before `open_tail` — the tail is a terminal error that must stay
            // last, and the reorder stage would otherwise hold rows behind it.
            let result = write_messages(
                merged
                    .filter_messages(cfg.filters.clone())
                    .order_rows(cfg.max_sort_group)
                    .chain(open_tail),
                output,
                cfg.error_mode,
                write_opts,
            );
            let n = collapsed.load(std::sync::atomic::Ordering::Relaxed);
            if n > 0 {
                log_info!("merge: collapsed {n} duplicate message(s) across recorders");
            }
            Ok(result)
        }
        // Incompatible inputs (L2-MRG-003) and prime-time file failures surface
        // here, before any output — route through the same exit classifier.
        Err(e) => Err(classify_decode_exit(Err(e), 0, false)),
    }
}

fn run_decode(globals: GlobalArgs, mut args: DecodeArgs) -> Result<u8, CliError> {
    let cfg = resolve_config(&globals)?;

    // Resolve the input set before `build_config_overrides` moves the filter
    // fields out of `args` (so we can still read inputs/manifest/glob).
    let input_paths = resolve_inputs(&args)?;

    let overrides = build_config_overrides(
        &mut args,
        globals.log_level.clone(),
        globals.irig_day_advisory,
    );
    let cfg = cfg.with_overrides(overrides);

    // No post-load `output_format` check: both ways of setting it are now
    // rejected before this point -- the CLI value at parse time (exit `4`,
    // `parse_output_format_arg`) and the config-file value at load time
    // (exit `5`, L2-CFG-010). A third check here would be unreachable.

    // Open a reader per resolved input file (L2-MRG-001). `input_paths` was
    // resolved above, before `with_overrides` consumed the filter fields.
    // Under --allow-partial a *merge* tolerates a per-file OPEN failure (an
    // empty / unreadable / missing input): it drops that input with a WARN and
    // commits the batch as `.partial` (L2-MRG-004), mirroring how a priming-time
    // or mid-file failure is handled. A single-input decode is unaffected.
    let merge_requested = input_paths.len() > 1;
    let (readers, open_dropped) = open_all_readers(&input_paths, &cfg, merge_requested)?;
    for r in &readers {
        log_info!("opened {} ({} bytes)", r.path().display(), r.file_size());
    }

    // WriteOptions: file-output safety checks (collision per L2-WRT-014,
    // no-clobber per L2-WRT-017) and L1-EXIT-004 allow_partial. For a single
    // input the writer performs its own input/output collision check; for a
    // merge we check the output against *every* input here and disable the
    // writer's single-path check.
    if merge_requested && let Some(out) = args.output.as_deref() {
        check_output_collision(
            out,
            &input_paths,
            cfg.error_mode == ErrorMode::Separate,
            cfg.allow_partial,
        )?;
    }
    // L2-WRT-026 clause 1: a calendar rendering needs a year, and whether one
    // was supplied is a question about the *resolved* pair -- either source may
    // have provided it -- so it is asked here, after the merge, and before the
    // output is opened.
    let time_render = resolve_time_render(&cfg)?;

    let write_opts = WriteOptions {
        input_path: if merge_requested {
            None
        } else {
            input_paths.first().cloned()
        },
        no_clobber: cfg.no_clobber,
        allow_partial: cfg.allow_partial,
        time_render,
    };

    // One input → the single-file path; two or more → the time-sorted k-way
    // merge. DELTA is per-file on both paths unless `--delta-scope global` is
    // given (L2-MRG-005). Both feed the same writer. An incompatible-merge /
    // prime-time failure returns its exit code directly.
    let write_result = match execute_decode_or_merge(
        &readers,
        &cfg,
        args.output.as_deref(),
        write_opts,
        merge_requested,
        open_dropped,
    ) {
        Ok(r) => r,
        Err(code) => return Ok(code),
    };

    // Cumulative sync-loss count across all inputs drives the L1-EXIT-005
    // exit-class summary. Safe to query after the iterator(s) are consumed.
    let sync_losses: u64 = readers.iter().map(MieFileReader::sync_losses).sum();

    // L1-EXIT-010: report the empty-recording class only when *every* opened
    // input was a valid empty recording (so a merge that also drew rows from a
    // non-empty input stays `complete`). A single-file empty decode is the
    // common case; the writer has already produced a header-only CSV.
    let empty_recording = !readers.is_empty() && readers.iter().all(MieFileReader::empty_recording);

    Ok(classify_decode_exit(
        write_result,
        sync_losses,
        empty_recording,
    ))
}

/// Resolve the `decode` input set from exactly one method (positionals,
/// `--manifest`, or `--glob`; mutual exclusivity already enforced at parse
/// time), enforcing the `MAX_MERGE_FILES` cap (L2-MRG-001).
fn resolve_inputs(args: &DecodeArgs) -> Result<Vec<PathBuf>, CliError> {
    let paths = if let Some(manifest) = &args.manifest {
        crate::merge::read_manifest(manifest).map_err(|e| {
            CliError::runtime(format!(
                "failed to read manifest {}: {e}",
                manifest.display()
            ))
        })?
    } else if let Some(pattern) = &args.glob {
        crate::merge::expand_glob(pattern).map_err(|e| {
            // A wildcard in the directory part is a malformed pattern, refused
            // before any I/O: the command line is wrong, so exit 4, not 1.
            if e.kind() == std::io::ErrorKind::InvalidInput {
                CliError::usage(format!("--glob {pattern:?}: {e}"))
            } else {
                CliError::runtime(format!("failed to expand --glob {pattern:?}: {e}"))
            }
        })?
    } else {
        args.inputs.clone()
    };

    if paths.is_empty() {
        return Err(CliError::usage(match (&args.manifest, &args.glob) {
            (Some(m), _) => format!("manifest {} contains no input paths", m.display()),
            (_, Some(g)) => format!("--glob {g:?} matched no files"),
            _ => "decode requires at least one input file".to_string(),
        }));
    }
    if paths.len() > crate::merge::MAX_MERGE_FILES {
        return Err(CliError::usage(format!(
            "too many input files: {} (maximum is {}); split the set into smaller batches",
            paths.len(),
            crate::merge::MAX_MERGE_FILES
        )));
    }
    Ok(paths)
}

/// Reject a merge that could commit over one of its inputs (L2-WRT-014
/// extended across the input set). Best-effort canonicalization; falls back to
/// a literal path comparison when a path cannot be canonicalized (e.g. the
/// output does not exist yet).
///
/// Checks **every** path the run could commit, not just the destination the
/// operator named: `crate::writer::commit_targets` enumerates the derived
/// errors file and the `.partial` variants alongside it. The writer runs the
/// same enumeration for a single-input decode, where it knows the one input;
/// on the merge path it is given `input_path: None` and this is the only guard
/// that sees the input set at all.
fn check_output_collision(
    output: &Path,
    inputs: &[PathBuf],
    split_errors: bool,
    allow_partial: bool,
) -> Result<(), CliError> {
    for target in crate::writer::commit_targets(output, split_errors, allow_partial) {
        let target_canon = std::fs::canonicalize(&target).ok();
        for inp in inputs {
            let collides = match (&target_canon, std::fs::canonicalize(inp).ok()) {
                (Some(o), Some(i)) => *o == i,
                _ => target == *inp,
            };
            if collides {
                // Naming which target collided matters: told only that the
                // output collides, an operator looks at `-o` and sees a name
                // that is plainly different from every input.
                let role = if target == output {
                    "output path"
                } else {
                    "derived output path"
                };
                return Err(CliError::runtime(format!(
                    "{role} {} resolves to merge input {}; choose a different output path",
                    target.display(),
                    inp.display()
                )));
            }
        }
    }
    Ok(())
}

/// Route a message stream to the CSV writer per the configured error mode.
/// Generic over the iterator so both the single-file reader and the merge
/// iterator are monomorphized (no dynamic dispatch on the hot path).
fn write_messages<I>(
    messages: I,
    output: Option<&Path>,
    error_mode: ErrorMode,
    write_opts: WriteOptions,
) -> crate::error::MieResult<crate::writer::WriteOutcome>
where
    I: Iterator<Item = crate::error::MieResult<crate::models::MieMessage>>,
{
    // Separate mode requires a file path; stdout in that case forces inline
    // behavior with a WARN (you cannot split stdout) per L3-RS-009.
    if error_mode == ErrorMode::Separate {
        match output {
            None => {
                crate::log_warn!("stdout output forces inline error mode");
                // The whole options struct is forwarded, not a fresh default.
                // Only the rendering actually applies to a stream -- `write_csv`
                // ignores the path-safety fields on its stdout branch -- but
                // rebuilding a default here silently dropped `time_render`, and
                // would silently drop the next option added too.
                write_csv(messages, None, write_opts)
            }
            Some(out) => write_csv_split(messages, out, write_opts).inspect(|outcome| {
                log_info!(
                    "wrote {} messages + {} errors to {}",
                    outcome.normal_count,
                    outcome.error_count,
                    out.display()
                );
            }),
        }
    } else {
        write_csv(messages, output, write_opts)
    }
}

/// Map a writer-side result + the reader's sync-loss count to an
/// exit status per L1-EXIT-002 through L1-EXIT-005 and L2-CLI-011. Emits the
/// one-line exit-class summary required by L1-EXIT-005 in every branch.
fn classify_decode_exit(
    r: crate::error::MieResult<crate::writer::WriteOutcome>,
    sync_losses: u64,
    empty_recording: bool,
) -> u8 {
    match r {
        Ok(outcome) => {
            // L1-EXIT-010: a valid but empty recording (opened on the
            // end-of-records terminator) is a successful decode that produces a
            // header-only CSV. Distinguish it from an ordinary complete decode
            // so operators can tell "captured nothing" apart from "captured and
            // wrote rows" by the exit-class line alone.
            let empty = empty_recording
                && outcome.partial.is_none()
                && outcome.normal_count == 0
                && outcome.error_count == 0;
            let class = if outcome.partial.is_some() {
                "partial-unrecoverable"
            } else if empty {
                "empty-recording"
            } else if sync_losses > 0 {
                "partial-recovered"
            } else {
                "complete"
            };
            log_info!("decode exit class: {class} (sync_losses={sync_losses})");
            exit_code::SUCCESS
        }
        Err(e) if e.is_broken_pipe() => {
            log_info!("decode exit class: complete (broken-pipe on stdout)");
            exit_code::SUCCESS
        }
        Err(e @ MieError::NoValidRecords { .. }) => {
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!("decode exit class: no-records");
            exit_code::NO_RECORDS
        }
        Err(e @ MieError::HomogeneousPayload { .. }) => {
            // L2-SYN-018 + L1-EXIT-002: semantically a "wrong file
            // type" rejection (the input is a single-byte pad rather
            // than an MIE recording), same exit-code class as
            // NoValidRecords.
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!("decode exit class: no-records");
            exit_code::NO_RECORDS
        }
        Err(e @ MieError::TimestampFormatMismatch { .. }) => {
            // L2-DEC-016 + L1-EXIT-002: ambiguous timestamp format is
            // semantically another "wrong file type" rejection — the
            // probe could not confidently distinguish IRIG from
            // Standard, so we treat the file the same way we'd treat
            // an unrecognized stream. Same exit class (2) as
            // NoValidRecords / HomogeneousPayload.
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!("decode exit class: no-records (timestamp-format-mismatch)");
            exit_code::NO_RECORDS
        }
        Err(e @ MieError::CalendarUnavailable { .. }) => {
            // L2-WRT-026 + L1-EXIT-002: the operator asked for a rendering the
            // recording cannot supply. Same class as TimestampFormatMismatch
            // and for the same reason -- the flag and the file disagree, and
            // the file wins. Day 366 against a common year lands here too: it
            // says the configured year is wrong, which makes every date in the
            // run suspect, not just the one record.
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!("decode exit class: no-records (calendar-unavailable)");
            exit_code::NO_RECORDS
        }
        Err(e @ MieError::UnrecoverableSyncLoss { .. }) => {
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!(
                "decode exit class: partial-unrecoverable (sync_losses={sync_losses}); \
                 pass --allow-partial to preserve the rows decoded so far"
            );
            exit_code::SYNC_LOSS
        }
        Err(e @ MieError::IncompatibleMergeInputs { .. }) => {
            // L1-EXIT-009 / L2-MRG-003: inputs cannot be ordered on a common
            // absolute timeline. Rejected before any output was written.
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!("decode exit class: merge-incompatible");
            exit_code::MERGE_INCOMPATIBLE
        }
        Err(e @ MieError::NonMonotonicInput { .. }) => {
            // L2-MRG-006: a strict-mode merge hit an input whose records are
            // not in chronological capture order. Same record-error class as
            // other strict-mode record failures (exit 1).
            log_error!("{e}");
            eprintln!("Error: {e}");
            log_info!("decode exit class: non-monotonic-input (strict)");
            exit_code::RUNTIME
        }
        Err(e) => {
            log_error!("{e}");
            eprintln!("Error: {e}");
            exit_code::RUNTIME
        }
    }
}

fn run_count(globals: GlobalArgs, input: PathBuf) -> Result<(), CliError> {
    let cfg = resolve_config(&globals)?;
    let reader = open_reader(&input, &cfg)?;

    // Apply config's filters to the count, matching decode's behavior.
    // A user who wants raw counts can omit [filter] from their config.
    let filter_cfg = cfg.filters.clone();
    let mut count: u64 = 0;
    for item in reader.iter().filter_messages(filter_cfg) {
        match item {
            Ok(_) => count += 1,
            // Align with `decode`: a wrong-file rejection maps to exit 2, not 1
            // (L2-CLI-011). An empty recording surfaces no error at all — the
            // iterator simply yields zero records — so `count` prints 0 and
            // exits 0 (L1-EXIT-010).
            Err(e) => return Err(CliError::from_decode_error(e)),
        }
    }
    // L3-RS-008: integer count to stdout (the machine-readable data),
    // human-friendly status with path context to stderr (always emitted,
    // not gated by --log-level so an interactive operator sees it
    // without having to opt into INFO logging).
    println!("{count}");
    if reader.empty_recording() {
        eprintln!(
            "no records in {} (empty recording -- opens on the end-of-records terminator)",
            reader.path().display()
        );
    } else {
        eprintln!("counted {count} messages in {}", reader.path().display());
    }
    Ok(())
}

fn run_dump(globals: GlobalArgs, args: DumpArgs) -> Result<(), CliError> {
    // dump only consumes log_level from config; input_time_format / strict /
    // filters don't apply to a hex view. We still call resolve_config
    // so a malformed config errors out consistently with the other
    // subcommands.
    let _cfg = resolve_config(&globals)?;

    let result = if args.raw {
        hex_dump_raw_to_stdout(&args.input, args.offset, args.length)
    } else {
        hex_dump_records_to_stdout(&args.input, args.records, args.offset)
    };
    finish_dump(result)
}

/// Map a dump result to the CLI contract. A broken pipe on stdout (e.g.
/// `dump | head`) is a clean termination and exits `0` per L2-WRT-018; any
/// other writer error (disk full, permission denied) — now that the dump
/// propagates them instead of swallowing them — surfaces as a runtime error.
fn finish_dump(result: crate::error::MieResult<()>) -> Result<(), CliError> {
    match result {
        Ok(()) => Ok(()),
        Err(e) if e.is_broken_pipe() => {
            log_info!("dump: broken-pipe on stdout, exiting 0");
            Ok(())
        }
        Err(e) => Err(CliError::runtime(format_mie_error(e))),
    }
}

fn format_mie_error(e: MieError) -> String {
    e.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> ArgIter<'static> {
        let v: Vec<String> = values.iter().map(|s| (*s).to_string()).collect();
        // skip(0) is required to match the ArgIter alias's
        // Skip<IntoIter<...>> shape; clippy can't see past the alias.
        #[allow(clippy::iter_skip_zero)]
        v.into_iter().skip(0).peekable()
    }

    /// `--help` must propagate as `ParseError::HelpRequested`, never as
    /// `process::exit`. The whole point of the refactor.
    /// Requirements: L2-CLI-001
    #[test]
    fn parse_decode_help_returns_help_requested() {
        let mut it = args(&["--help"]);
        match parse_decode(&mut it) {
            Err(ParseError::HelpRequested) => {}
            other => panic!("expected HelpRequested, got {other:?}"),
        }
    }

    /// L1-EXIT-007: an unsupported `--format` is an invalid FLAG VALUE, so it
    /// is a usage error (exit `4`) settled at parse time.
    ///
    /// It was a runtime error (exit `1`) through v2.14.0 because the check ran
    /// after the config layer merged CLI overrides. That put one mistake on
    /// three different exit codes depending on where it was written: `4` for a
    /// bad `--input-time-format`, `1` for a bad `--format`, `5` for the same value in
    /// a config file. Only the middle one was wrong; the config-file spelling
    /// is still `5` (L2-CFG-010).
    /// Requirements: L2-CLI-011
    #[test]
    fn output_format_arg_rejects_anything_but_csv() {
        assert_eq!(parse_output_format_arg("csv").unwrap(), "csv");
        for bad in ["json", "parquet", "CSV", "", " csv"] {
            let err = parse_output_format_arg(bad).unwrap_err();
            assert!(err.contains("--format"), "got: {err}");
        }
        // Through the parser, both spellings, since the joined form takes a
        // different path. A usage error here is what maps to exit 4.
        for argv in [
            vec!["--format", "json", "rec.mie"],
            vec!["--format=json", "rec.mie"],
        ] {
            let err = parse_decode(&mut args(&argv)).unwrap_err();
            let ParseError::Other(msg) = err else {
                panic!("{argv:?}: expected a usage error");
            };
            assert!(msg.contains("--format"), "{argv:?}: got {msg:?}");
        }
        assert!(parse_decode(&mut args(&["--format", "csv", "rec.mie"])).is_ok());
    }

    /// `--input-time-format` accepts any letter case (auto/irig/standard), matching
    /// the config loader; an unrecognized value is a usage error.
    /// Requirements: L2-CLI-001
    #[test]
    fn input_time_format_arg_is_case_insensitive() {
        assert_eq!(
            parse_input_time_format_arg("IRIG").unwrap(),
            TimestampFormat::Irig
        );
        assert_eq!(
            parse_input_time_format_arg("Irig").unwrap(),
            TimestampFormat::Irig
        );
        assert_eq!(
            parse_input_time_format_arg("AUTO").unwrap(),
            TimestampFormat::Auto
        );
        assert_eq!(
            parse_input_time_format_arg("Standard").unwrap(),
            TimestampFormat::Standard
        );
        assert!(parse_input_time_format_arg("bogus").is_err());
    }

    /// Requirements: L2-CLI-008, L3-RS-008
    #[test]
    fn parse_count_help_returns_help_requested() {
        let mut it = args(&["-h"]);
        match parse_count(&mut it) {
            Err(ParseError::HelpRequested) => {}
            other => panic!("expected HelpRequested, got {other:?}"),
        }
    }

    /// Requirements: L2-CLI-009
    #[test]
    fn parse_dump_help_returns_help_requested() {
        let mut it = args(&["--help"]);
        match parse_dump(&mut it) {
            Err(ParseError::HelpRequested) => {}
            other => panic!("expected HelpRequested, got {other:?}"),
        }
    }

    /// Every numeric `dump` argument accepts decimal and `0x` hex identically,
    /// and rejects a negative value (the parser is unsigned). Mirrors the Python
    /// `_nonneg_int` parity test.
    /// Requirements: L2-CLI-009
    #[test]
    fn parse_dump_numeric_args_accept_hex_and_reject_negative() {
        let d = parse_dump(&mut args(&[
            "f.mie",
            "--offset",
            "0x20",
            "--length",
            "16",
            "--records",
            "0x10",
        ]))
        .unwrap();
        assert_eq!(d.offset, 0x20);
        assert_eq!(d.length, Some(16));
        assert_eq!(d.records, Some(0x10));

        for flag in ["--offset", "--length", "--records"] {
            assert!(
                parse_dump(&mut args(&["f.mie", flag, "-1"])).is_err(),
                "{flag} should reject a negative value"
            );
        }
    }

    /// Parse errors should still surface as `ParseError::Other`, not panics
    /// or exits.
    /// Requirements: L2-CLI-005
    #[test]
    fn parse_decode_unknown_flag_returns_other() {
        let mut it = args(&["--nope"]);
        match parse_decode(&mut it) {
            Err(ParseError::Other(msg)) => assert!(msg.contains("--nope")),
            other => panic!("expected Other, got {other:?}"),
        }
    }

    /// Requirements: L2-CLI-001
    #[test]
    fn parse_decode_missing_input_returns_other() {
        let mut it = args(&[]);
        match parse_decode(&mut it) {
            Err(ParseError::Other(msg)) => assert!(msg.contains("input file")),
            other => panic!("expected Other, got {other:?}"),
        }
    }

    /// Happy path still produces a value.
    /// Requirements: L2-CLI-002
    #[test]
    fn parse_decode_minimal_ok() {
        let mut it = args(&["recording.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("recording.mie")]);
    }

    /// Regression test for the team's exact reproducer:
    /// `decode --include-rts 15 file.mie` previously consumed `file.mie`
    /// as another RT value (greedy multi-value). Now filter flags take
    /// exactly one value, so the positional input binds correctly.
    /// Requirements: L2-CLI-010
    #[test]
    fn filter_flag_does_not_eat_positional_input() {
        let mut it = args(&["--include-rts", "15", "file.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("file.mie")]);
        assert_eq!(parsed.include_rts, vec![15]);
    }

    /// Comma-separated values within a single flag.
    /// Requirements: L2-CLI-010
    #[test]
    fn filter_flag_accepts_comma_separated_values() {
        let mut it = args(&["--include-rts", "15,20,31", "file.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("file.mie")]);
        assert_eq!(parsed.include_rts, vec![15, 20, 31]);
    }

    /// Repeating a filter flag accumulates values.
    /// Requirements: L2-CLI-010
    #[test]
    fn filter_flag_repeats_accumulate() {
        let mut it = args(&["--include-rts", "15", "--include-rts", "31", "file.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("file.mie")]);
        assert_eq!(parsed.include_rts, vec![15, 31]);
    }

    /// RT and subaddress filter values are MIL-STD-1553 wire fields, so the
    /// CLI holds them to [0, 31] like the config loader does. Anything that
    /// fit a `u8` used to pass, and filtered every record out.
    /// Requirements: L2-CLI-010
    #[test]
    fn rt_and_subaddress_filters_reject_values_above_31() {
        for flag in [
            "--include-rts",
            "--exclude-rts",
            "--include-subaddresses",
            "--exclude-subaddresses",
        ] {
            assert!(parse_decode(&mut args(&[flag, "31", "f.mie"])).is_ok());
            assert!(parse_decode(&mut args(&[flag, "0x1F", "f.mie"])).is_ok());
            for bad in ["32", "255", "256", "0x20"] {
                let err = parse_decode(&mut args(&[flag, bad, "f.mie"])).unwrap_err();
                let ParseError::Other(msg) = err else {
                    panic!("{flag} {bad}: expected a usage error");
                };
                assert!(
                    msg.contains(flag) && msg.contains("[0, 31]"),
                    "{flag} {bad}: got {msg:?}"
                );
            }
        }
    }

    /// `--flag=value` syntax with comma-separation.
    /// Help outranks a **deferred** diagnostic.
    ///
    /// An operator whose command line is wrong is the one most likely to be
    /// asking for help. Python and C++ both answered help here; Rust reported
    /// the unknown option and was the odd one out.
    /// Requirements: L2-CLI-017
    #[test]
    fn help_outranks_a_deferred_diagnostic() {
        // Unrecognised option, then a help request.
        assert!(help_pending(&mut args(&["--help"])));
        assert!(help_pending(&mut args(&["rec.mie", "-h"])));

        // Nothing pending is the ordinary case.
        assert!(!help_pending(&mut args(&["rec.mie"])));
        assert!(!help_pending(&mut args(&[])));
    }

    /// A help flag after `--` is a path, not a request (L2-CLI-016), so it
    /// cannot rescue a broken command line either.
    /// Requirements: L2-CLI-016, L2-CLI-017
    #[test]
    fn help_after_the_end_of_options_marker_is_not_pending() {
        assert!(!help_pending(&mut args(&["--", "--help"])));
        assert!(!help_pending(&mut args(&["--", "-h"])));
        // ...but before it, it still counts.
        assert!(help_pending(&mut args(&["-h", "--", "--help"])));
    }

    /// Help does NOT outrank a failed value **consumption**.
    ///
    /// When a flag cannot take a value the rest of the command line is
    /// uninterpretable — nothing downstream can be attributed to the right
    /// flag — so `next_value` drains the iterator and a later `-h` is not
    /// pending. That is `argparse`'s split too: it raises immediately here and
    /// defers everything else.
    /// Requirements: L2-CLI-015, L2-CLI-017
    #[test]
    fn a_failed_value_consumption_is_not_rescued_by_a_later_help() {
        // The option-like guard fires, and the drain leaves nothing pending.
        let mut it = args(&["-h", "--help", "rec.mie"]);
        let err = next_value("--mux-delimiter", &mut it).unwrap_err();
        assert!(err.contains("--mux-delimiter"), "got {err:?}");
        assert!(
            !help_pending(&mut it),
            "a consumption failure must consume the rest of the line"
        );

        // Same when the arguments simply ran out.
        let mut it = args(&[]);
        assert!(next_value("--output", &mut it).is_err());
        assert!(!help_pending(&mut it));

        // And the success path leaves the iterator exactly where it was.
        let mut it = args(&["value", "--help"]);
        assert_eq!(next_value("--mux-delimiter", &mut it).unwrap(), "value");
        assert!(help_pending(&mut it));
    }

    /// POSIX end-of-options. After `--` every token is a path, however it is
    /// spelled — which is the only way to decode a file whose name begins
    /// with a dash.
    /// Requirements: L2-CLI-016
    #[test]
    fn end_of_options_makes_every_later_token_positional() {
        let parsed = parse_decode(&mut args(&["--", "rec.mie", "--no-mux", "-o", "out.csv"]))
            .expect("everything after -- is an input");
        assert_eq!(
            parsed.inputs,
            vec![
                PathBuf::from("rec.mie"),
                PathBuf::from("--no-mux"),
                PathBuf::from("-o"),
                PathBuf::from("out.csv"),
            ]
        );
        // None of them acted as a flag.
        assert!(!parsed.no_mux);
        assert_eq!(parsed.output, None);
    }

    /// Only the FIRST `--` is a separator; a later one is an ordinary
    /// positional, which is how a file actually named `--` is named.
    /// Requirements: L2-CLI-016
    #[test]
    fn only_the_first_end_of_options_marker_is_consumed() {
        let parsed = parse_decode(&mut args(&["--", "--", "rec.mie"])).unwrap();
        assert_eq!(
            parsed.inputs,
            vec![PathBuf::from("--"), PathBuf::from("rec.mie")]
        );

        // And before it, flags still work normally.
        let parsed = parse_decode(&mut args(&["--no-mux", "--", "rec.mie"])).unwrap();
        assert!(parsed.no_mux);
        assert_eq!(parsed.inputs, vec![PathBuf::from("rec.mie")]);
    }

    /// The separator is scoped to the parser that consumes it. A `--` before
    /// the subcommand means "the next token is the subcommand NAME" — it does
    /// not put the subcommand's own parser into end-of-options mode, so
    /// `-- decode rec.mie --no-mux` still honours `--no-mux`.
    ///
    /// It also suppresses the global flags, so `-- --version` asks for a
    /// subcommand called `--version` and is reported as an unknown command.
    /// Requirements: L2-CLI-016
    #[test]
    fn end_of_options_before_the_subcommand_names_it() {
        let mut globals = GlobalArgs::default();
        let sub = parse_global_flags(&mut args(&["--", "decode", "rec.mie"]), &mut globals)
            .expect("-- then a subcommand");
        assert_eq!(sub, "decode");

        // The subcommand parser gets a fresh scan.
        let parsed = parse_decode(&mut args(&["rec.mie", "--no-mux"])).unwrap();
        assert!(parsed.no_mux);

        for token in ["--version", "-h", "--help", "-V"] {
            let mut globals = GlobalArgs::default();
            let sub = parse_global_flags(&mut args(&["--", token]), &mut globals)
                .expect("-- suppresses the global flags");
            assert_eq!(
                sub, token,
                "{token} should be handed back as a command name"
            );
        }
    }

    /// `count` and `dump` take one positional each, so after `--` a second one
    /// is the same mistake it always was.
    /// Requirements: L2-CLI-016
    #[test]
    fn end_of_options_applies_to_count_and_dump() {
        let path = parse_count(&mut args(&["--", "-weird.mie"])).unwrap();
        assert_eq!(path, PathBuf::from("-weird.mie"));

        let err = parse_count(&mut args(&["--", "a.mie", "b.mie"])).unwrap_err();
        let ParseError::Other(msg) = err else {
            panic!("expected a usage error");
        };
        assert!(
            msg.contains("unexpected positional argument"),
            "got {msg:?}"
        );

        let parsed = parse_dump(&mut args(&["--", "-weird.mie"])).unwrap();
        assert_eq!(parsed.input, PathBuf::from("-weird.mie"));

        // After `--`, `--records` is a path, so it lands as a second
        // positional rather than as a flag.
        let err = parse_dump(&mut args(&["--", "a.mie", "--records"])).unwrap_err();
        let ParseError::Other(msg) = err else {
            panic!("expected a usage error");
        };
        assert!(
            msg.contains("unexpected positional argument"),
            "got {msg:?}"
        );
    }

    /// The predicate itself, at the boundary, because the parse-level tests
    /// below cannot reach every shape cheaply.
    ///
    /// These values are Python 3.14's, read out of its `argparse` source
    /// rather than guessed: the matcher is `-\.?\d` applied as a **prefix**
    /// test, so "-5e3", "-0x5" and "-1a" all begin like numbers and are
    /// values. Python 3.10-3.13 used an anchored full match and called those
    /// three options instead. We pin 3.14's; `starts_like_a_number` explains
    /// why, and those shapes are deliberately outside the L2-CLI-015 contract
    /// because `argparse` does not agree with itself about them.
    /// Requirements: L2-CLI-015
    #[test]
    fn looks_like_option_matches_argparse_3_14() {
        // Values — consumed as the argument to a flag.
        for token in [
            "", "-", "-5", "-0", "-5.5", "-.5", "-00.5", "-5.", "- x", "-1 2", "x", "a=b",
            // Begin like a number, so a value; the flag's own validator gets
            // to complain about the rest, which is the more useful message.
            "-5e3", "-0x5", "-1a", "-5.5.5", "-1e-3",
        ] {
            assert!(
                !looks_like_option(token),
                "{token:?} should be treated as a value"
            );
        }
        // Option-like — refused as the argument to a flag.
        for token in [
            "-x",
            "-o",
            "-abc",
            "-.",
            "-..5",
            "-+5",
            "--",
            "--1",
            "--foo",
            "--no-mux",
            "--flag=value",
        ] {
            assert!(
                looks_like_option(token),
                "{token:?} should be treated as an option"
            );
        }
    }

    /// The separated form must not swallow the following flag.
    ///
    /// This was the last cross-implementation divergence in flag parsing:
    /// `--mux-delimiter --no-mux` set the delimiter to the string "--no-mux"
    /// in Rust and C++ -- so `--no-mux` silently never ran and the decode
    /// succeeded with a wrong MUX column -- while Python refused it. A silent
    /// wrong answer is the worst of the three possible behaviours, so both
    /// moved to Python's.
    /// Requirements: L2-CLI-015
    #[test]
    fn a_flag_like_value_is_refused_in_the_separated_form() {
        // Only shapes every supported Python version agrees on; see
        // `looks_like_option_matches_argparse_3_14` for the corner it does not.
        for token in ["--no-mux", "--foo", "-o", "-x"] {
            let err = parse_decode(&mut args(&["--mux-delimiter", token, "rec.mie"])).unwrap_err();
            let ParseError::Other(msg) = err else {
                panic!("{token}: expected a usage error");
            };
            assert!(msg.contains("--mux-delimiter"), "{token}: got {msg:?}");
            // The message has to say how to get what the user probably wanted.
            assert!(
                msg.contains(&format!("--mux-delimiter={token}")),
                "{token}: message should suggest the joined form, got {msg:?}"
            );
        }
    }

    /// The joined form is unambiguous, so it still accepts a flag-like value
    /// -- and is what the refusal message points at.
    /// Requirements: L2-CLI-015
    #[test]
    fn the_joined_form_still_takes_a_flag_like_value() {
        let parsed = parse_decode(&mut args(&["--mux-delimiter=--no-mux", "rec.mie"])).unwrap();
        assert_eq!(parsed.mux_delimiter.as_deref(), Some("--no-mux"));
        // ...and it really is a delimiter, not the flag: `no_mux` stays unset.
        assert!(!parsed.no_mux);
    }

    /// The three exemptions, each of which keeps a real invocation working.
    /// Requirements: L2-CLI-005, L2-CLI-015
    #[test]
    fn the_exemptions_are_still_values() {
        // A lone `-` is a path, not stdout: `-o -` writes a file named `-`.
        let parsed = parse_decode(&mut args(&["-o", "-", "rec.mie"])).unwrap();
        assert_eq!(parsed.output, Some(PathBuf::from("-")));

        // A negative number: `--mux-field -1` counts from the end.
        let parsed = parse_decode(&mut args(&["--mux-field", "-1", "rec.mie"])).unwrap();
        assert_eq!(parsed.mux_field, Some(-1));

        // A negative number still reaches its OWN validator, so a flag that
        // forbids negatives refuses it for that reason and not for looking
        // like an option.
        let err = parse_decode(&mut args(&["--collapse-window-us", "-5", "rec.mie"])).unwrap_err();
        let ParseError::Other(msg) = err else {
            panic!("expected a usage error");
        };
        assert!(
            !msg.contains("is an option"),
            "should be refused by the validator, not the option guard: {msg:?}"
        );

        // A token containing a space is a value; no option is spelled so.
        let parsed = parse_decode(&mut args(&["--mux-delimiter", "- x", "rec.mie"])).unwrap();
        assert_eq!(parsed.mux_delimiter.as_deref(), Some("- x"));
    }

    /// Globals are a separate parse loop and need the guard too. Before it,
    /// `--log-level --config x` made Rust take "--config" as the level, then
    /// read "x" as the subcommand -- exit 4, but for an unrelated reason and
    /// with a message that named neither flag.
    /// Requirements: L2-CLI-015
    #[test]
    fn global_flags_refuse_a_flag_like_value() {
        let mut globals = GlobalArgs::default();
        let err = parse_global_flags(
            &mut args(&["--log-level", "--config", "x", "decode"]),
            &mut globals,
        )
        .unwrap_err();
        assert_eq!(err, exit_code::USAGE);
        assert!(
            globals.log_level.is_none(),
            "must not have consumed a value"
        );
    }

    /// `run_to_code` hands an embedder the L2-CLI-011 status as a number --
    /// the Python binding returns it from `aero1553.cli.main()` -- so the
    /// number itself is the contract, not just the process exit it becomes.
    /// Requirements: L2-CLI-011
    #[test]
    fn run_to_code_returns_the_exit_taxonomy_as_a_number() {
        let argv = |a: &[&str]| -> Vec<String> {
            std::iter::once("aero1553")
                .chain(a.iter().copied())
                .map(String::from)
                .collect()
        };
        assert_eq!(run_to_code(argv(&["--version"])), exit_code::SUCCESS);
        assert_eq!(run_to_code(argv(&["no-such-command"])), exit_code::USAGE);
        assert_eq!(
            run_to_code(argv(&["count", "definitely-missing-input.mie"])),
            exit_code::RUNTIME
        );
    }

    /// Every valued flag, both spellings, compared structurally rather than by
    /// exit code — a spelling that parsed but dropped its value would still
    /// exit 0. `DecodeArgs` has no `PartialEq` (it is private, and deriving one
    /// only for a test is surface for its own sake), so `Debug` stands in: it
    /// renders every field, which is exactly the comparison wanted.
    ///
    /// This sweep is what the `Arg` cursor buys. Before it each of these flags
    /// carried two match arms, and the test could only have been written as a
    /// list of near-copies.
    /// Requirements: L2-CLI-015
    #[test]
    fn every_valued_flag_accepts_both_spellings() {
        let cases: &[(&str, &str)] = &[
            ("--output", "out.csv"),
            ("--input-time-format", "irig"),
            ("--format", "csv"),
            ("--detect-records", "4"),
            ("--lookahead-records", "2"),
            ("--standard-tick-rate-hz", "1000000"),
            ("--max-sort-group", "64"),
            ("--mux-delimiter", "_"),
            ("--mux-field", "0"),
            ("--delta-scope", "global"),
            ("--collapse-window-us", "10"),
            ("--manifest", "list.txt"),
            ("--glob", "*.mie"),
            ("--exclude-rts", "31"),
            ("--include-rts", "15"),
            ("--exclude-buses", "B"),
            ("--include-buses", "A"),
            ("--exclude-subaddresses", "30"),
            ("--include-subaddresses", "11"),
            ("--exclude-types", "RT_TO_RT"),
            ("--include-types", "BC_TO_RT"),
        ];

        for (flag, value) in cases {
            // --manifest and --glob are themselves input methods, so adding a
            // positional would fail the parse for an unrelated reason.
            let positional: &[&str] = if matches!(*flag, "--manifest" | "--glob") {
                &[]
            } else {
                &["rec.mie"]
            };

            let mut separated = vec![*flag, *value];
            separated.extend_from_slice(positional);
            let joined_flag = format!("{flag}={value}");
            let mut joined = vec![joined_flag.as_str()];
            joined.extend_from_slice(positional);

            let a = parse_decode(&mut args(&separated))
                .unwrap_or_else(|e| panic!("{flag} separated form failed: {e:?}"));
            let b = parse_decode(&mut args(&joined))
                .unwrap_or_else(|e| panic!("{flag} joined form failed: {e:?}"));
            assert_eq!(
                format!("{a:?}"),
                format!("{b:?}"),
                "{flag} spellings differ"
            );
        }
    }

    /// `--flag=` carries an EMPTY value, which the flag's own validator then
    /// accepts or rejects. It is not a malformed token.
    ///
    /// C++ had this wrong in the opposite direction: it required a character
    /// after the `=`, so `--exclude-rts=` was an "unknown option" (exit 4)
    /// where Rust and Python decoded normally (exit 0).
    /// Requirements: L2-CLI-015
    #[test]
    fn eq_form_with_empty_value_is_an_empty_value() {
        // Accepted: an empty filter excludes nothing, so the parse must match
        // the one that never passed the flag at all.
        let with = parse_decode(&mut args(&["--exclude-rts=", "rec.mie"])).unwrap();
        let without = parse_decode(&mut args(&["rec.mie"])).unwrap();
        assert_eq!(with.exclude_rts, Vec::<u8>::new());
        assert_eq!(format!("{with:?}"), format!("{without:?}"));

        // Rejected: the validator refuses this, not the cursor.
        let err = parse_decode(&mut args(&["--mux-delimiter=", "rec.mie"])).unwrap_err();
        let ParseError::Other(msg) = err else {
            panic!("expected a usage error");
        };
        assert!(
            msg.contains("--mux-delimiter"),
            "message should name the flag, got {msg:?}"
        );
        assert!(
            !msg.contains("unknown"),
            "must reach the validator, not the unknown-option arm: {msg:?}"
        );
    }

    /// Only the first `=` separates; the remainder is the value verbatim.
    /// Requirements: L2-CLI-015
    #[test]
    fn only_the_first_equals_separates() {
        let parsed = parse_decode(&mut args(&["--mux-delimiter==", "rec.mie"])).unwrap();
        assert_eq!(parsed.mux_delimiter.as_deref(), Some("="));

        let parsed = parse_decode(&mut args(&["--glob=a=b*.mie"])).unwrap();
        assert_eq!(parsed.glob.as_deref(), Some("a=b*.mie"));
    }

    /// A flag that takes no value must REJECT a joined one rather than set
    /// itself and silently discard it, and the message quotes the token as it
    /// was typed.
    /// Requirements: L2-CLI-015
    #[test]
    fn a_valueless_flag_rejects_a_joined_value() {
        for token in [
            "--no-mux=true",
            "--no-mux=",
            "--separate-errors=1",
            "--strict=false",
        ] {
            let err = parse_decode(&mut args(&[token, "rec.mie"])).unwrap_err();
            let ParseError::Other(msg) = err else {
                panic!("{token}: expected a usage error");
            };
            assert!(
                msg.contains(token),
                "{token}: message should quote the whole token, got {msg:?}"
            );
        }
    }

    /// Splitting is confined to `--` tokens: a positional path may contain
    /// `=`, and `-o=value` is not a spelling this CLI accepts.
    /// Requirements: L2-CLI-015
    #[test]
    fn only_double_dash_tokens_are_split() {
        let parsed = parse_decode(&mut args(&["a=b.mie"])).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("a=b.mie")]);

        let err = parse_decode(&mut args(&["-o=out.csv", "rec.mie"])).unwrap_err();
        let ParseError::Other(msg) = err else {
            panic!("expected a usage error");
        };
        assert!(msg.contains("-o=out.csv"), "got {msg:?}");
    }

    /// Globals are parsed in their own loop, so both spellings need proving
    /// there independently. `--version=1` is a value on a flag that takes
    /// none, so it is NOT a version request: it falls through as the
    /// subcommand token and is reported as an unknown command.
    /// Requirements: L2-CLI-015
    #[test]
    fn global_flags_accept_both_spellings() {
        for (argv, expected) in [
            (vec!["--log-level", "ERROR", "decode"], "ERROR"),
            (vec!["--log-level=ERROR", "decode"], "ERROR"),
            (vec!["--log-level=", "decode"], ""),
        ] {
            let mut globals = GlobalArgs::default();
            let sub = parse_global_flags(&mut args(&argv), &mut globals)
                .unwrap_or_else(|_| panic!("{argv:?} should yield a subcommand"));
            assert_eq!(sub, "decode", "{argv:?}");
            assert_eq!(globals.log_level.as_deref(), Some(expected), "{argv:?}");
        }

        for argv in [
            vec!["--config", "site.toml", "decode"],
            vec!["--config=site.toml", "decode"],
        ] {
            let mut globals = GlobalArgs::default();
            let sub = parse_global_flags(&mut args(&argv), &mut globals).unwrap();
            assert_eq!(sub, "decode");
            assert_eq!(globals.config, Some(PathBuf::from("site.toml")));
        }

        // Not a version request: handed back as the subcommand token.
        let mut globals = GlobalArgs::default();
        let sub = parse_global_flags(&mut args(&["--version=1"]), &mut globals).unwrap();
        assert_eq!(sub, "--version=1");
    }

    /// `dump` has its own loop as well.
    /// Requirements: L2-CLI-015
    #[test]
    fn dump_flags_accept_both_spellings() {
        let a = parse_dump(&mut args(&["rec.mie", "--records", "2", "--offset", "8"])).unwrap();
        let b = parse_dump(&mut args(&["rec.mie", "--records=2", "--offset=8"])).unwrap();
        assert_eq!(format!("{a:?}"), format!("{b:?}"));

        let err = parse_dump(&mut args(&["rec.mie", "--raw=true"])).unwrap_err();
        let ParseError::Other(msg) = err else {
            panic!("expected a usage error");
        };
        assert!(msg.contains("--raw=true"), "got {msg:?}");
    }

    /// Requirements: L2-CLI-010
    #[test]
    fn filter_flag_accepts_eq_form() {
        let mut it = args(&["--include-rts=15,20", "file.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("file.mie")]);
        assert_eq!(parsed.include_rts, vec![15, 20]);
    }

    /// Sanity-check the same property for the other filter flags.
    /// Requirements: L2-CLI-010
    #[test]
    fn all_filter_flags_take_single_value() {
        let mut it = args(&[
            "--exclude-types",
            "SPURIOUS_DATA",
            "--include-buses",
            "A",
            "--exclude-subaddresses",
            "0,31",
            "rec.mie",
        ]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("rec.mie")]);
        assert_eq!(parsed.exclude_types, vec![0x20]);
        assert_eq!(parsed.include_buses, vec![crate::models::Bus::A]);
        assert_eq!(parsed.exclude_subaddresses, vec![0, 31]);
    }

    /// A single-value filter flag consumes exactly one value; any further
    /// tokens are positional inputs. With multi-file input (L2-MRG-001),
    /// `--include-rts 15 31 file.mie` parses as include-rts=[15] and inputs
    /// `["31", "file.mie"]` — the stray "31" becomes a path (failing later at
    /// open time) rather than being silently absorbed as a second RT value.
    /// Requirements: L2-CLI-010, L2-MRG-001
    #[test]
    fn filter_flag_takes_single_value_rest_are_positional_inputs() {
        let mut it = args(&["--include-rts", "15", "31", "file.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.include_rts, vec![15]);
        assert_eq!(
            parsed.inputs,
            vec![PathBuf::from("31"), PathBuf::from("file.mie")]
        );
    }

    /// Requirements: L2-CLI-012
    #[test]
    fn parse_decode_standard_tick_rate_hz_space_and_eq_forms() {
        let mut it = args(&["--standard-tick-rate-hz", "1000000", "rec.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.inputs, vec![PathBuf::from("rec.mie")]);
        assert_eq!(parsed.standard_tick_rate_hz, Some(1_000_000.0));

        let mut it = args(&["--standard-tick-rate-hz=2.5e6", "rec.mie"]);
        let parsed = parse_decode(&mut it).unwrap();
        assert_eq!(parsed.standard_tick_rate_hz, Some(2_500_000.0));
    }

    /// Requirements: L2-CLI-012
    #[test]
    fn parse_decode_standard_tick_rate_hz_rejects_nonpositive() {
        for bad in ["0", "-1", "0.0"] {
            let mut it = args(&["--standard-tick-rate-hz", bad, "rec.mie"]);
            match parse_decode(&mut it) {
                Err(ParseError::Other(msg)) => {
                    assert!(
                        msg.contains("--standard-tick-rate-hz"),
                        "error should name the flag for {bad:?}: {msg}"
                    );
                }
                other => panic!("expected Other for {bad:?}, got {other:?}"),
            }
        }
    }

    /// Requirements: L2-CLI-012
    #[test]
    fn parse_decode_standard_tick_rate_hz_rejects_non_numeric() {
        let mut it = args(&["--standard-tick-rate-hz", "fast", "rec.mie"]);
        match parse_decode(&mut it) {
            Err(ParseError::Other(msg)) => assert!(msg.contains("--standard-tick-rate-hz")),
            other => panic!("expected Other, got {other:?}"),
        }
    }

    // ── --config plumbing through count/dump ──────────────────────────
    //
    // Regression: before, count and dump ignored --config entirely so a
    // malformed config file passed alongside those subcommands silently
    // succeeded. After the fix, all three subcommands load the config
    // up-front and surface parse errors uniformly.

    fn write_temp_file(suffix: &str, content: &[u8]) -> PathBuf {
        use std::io::Write;
        use std::sync::atomic::{AtomicU64, Ordering};
        static C: AtomicU64 = AtomicU64::new(0);
        let n = C.fetch_add(1, Ordering::Relaxed);
        let pid = std::process::id();
        let p = std::env::temp_dir().join(format!("mie-cli-test-{pid}-{n}{suffix}"));
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(content).unwrap();
        f.flush().unwrap();
        p
    }

    /// Requirements: L2-CLI-005, L2-CLI-011, L1-EXIT-008
    #[test]
    fn run_count_propagates_config_load_error() {
        let bad = write_temp_file(".toml", b"[decode]\ninput_time_format = \"potato\"\n");
        let globals = GlobalArgs {
            log_level: None,
            irig_day_advisory: None,
            config: Some(bad.clone()),
        };
        // Input doesn't matter: config error fires before the file is opened.
        let result = run_count(globals, PathBuf::from("/no/such/recording.mie"));
        let _ = std::fs::remove_file(&bad);
        match result {
            Err(e) => {
                assert_eq!(e.code, exit_code::CONFIG, "config error should exit 5");
                assert!(
                    e.message.contains("Invalid input_time_format"),
                    "expected config error, got: {}",
                    e.message
                );
            }
            Ok(()) => panic!("expected config error, got Ok"),
        }
    }

    /// Requirements: L2-CLI-005, L2-CLI-011, L1-EXIT-008
    #[test]
    fn run_dump_propagates_config_load_error() {
        let bad = write_temp_file(".toml", b"[decode]\ninput_time_format = \"potato\"\n");
        let globals = GlobalArgs {
            log_level: None,
            irig_day_advisory: None,
            config: Some(bad.clone()),
        };
        let dump_args = DumpArgs {
            input: PathBuf::from("/no/such/recording.mie"),
            ..Default::default()
        };
        let result = run_dump(globals, dump_args);
        let _ = std::fs::remove_file(&bad);
        match result {
            Err(e) => {
                assert_eq!(e.code, exit_code::CONFIG, "config error should exit 5");
                assert!(
                    e.message.contains("Invalid input_time_format"),
                    "expected config error, got: {}",
                    e.message
                );
            }
            Ok(()) => panic!("expected config error, got Ok"),
        }
    }

    /// Requirements: L2-WRT-018
    #[test]
    fn finish_dump_maps_broken_pipe_to_ok() {
        let broken = MieError::WriterError {
            destination: "stdout".to_string(),
            source: std::io::Error::new(std::io::ErrorKind::BrokenPipe, "pipe closed"),
        };
        assert!(
            finish_dump(Err(broken)).is_ok(),
            "broken pipe on dump stdout should exit 0"
        );
    }

    /// Requirements: L2-WRT-018
    #[test]
    fn finish_dump_propagates_real_write_error() {
        let disk_full = MieError::WriterError {
            destination: "stdout".to_string(),
            source: std::io::Error::other("No space left on device"),
        };
        let err = finish_dump(Err(disk_full)).unwrap_err();
        assert_eq!(err.code, exit_code::RUNTIME, "disk-full dump error exits 1");
    }

    /// Requirements: L2-CFG-005, L2-CLI-011, L1-EXIT-008
    #[test]
    fn run_count_propagates_missing_config_file() {
        let globals = GlobalArgs {
            log_level: None,
            irig_day_advisory: None,
            config: Some(PathBuf::from("/no/such/config.toml")),
        };
        let result = run_count(globals, PathBuf::from("/no/such/recording.mie"));
        match result {
            Err(e) => {
                assert_eq!(
                    e.code,
                    exit_code::CONFIG,
                    "missing config file should exit 5"
                );
                assert!(
                    e.message.contains("Config file not found"),
                    "expected 'Config file not found' error, got: {}",
                    e.message
                );
            }
            Ok(()) => panic!("expected error, got Ok"),
        }
    }

    // ── Log-level validation ─────────────────────────────────────────
    //
    // Regression: --log-level NOPE used to be silently ignored (the
    // code did `if let Some(lvl) = Level::parse(s)` and never bothered
    // with the None branch). Now invalid values fail loudly:
    //   - CLI input fails at run() entry with exit 4 (usage error)
    //   - Config-file value fails at config load time with exit 5
    //     (configuration error)

    /// Requirements: L2-CLI-004
    #[test]
    fn apply_log_level_accepts_known_names() {
        for name in [
            "DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL", "off",
        ] {
            apply_log_level("--log-level", name)
                .unwrap_or_else(|e| panic!("expected {name} to parse, got: {e}"));
        }
    }

    /// Requirements: L2-CLI-004
    #[test]
    fn apply_log_level_rejects_unknown_names() {
        match apply_log_level("--log-level", "NOPE") {
            Err(msg) => {
                assert!(msg.contains("--log-level"));
                assert!(msg.contains("NOPE"));
                assert!(msg.contains("valid:"));
            }
            Ok(()) => panic!("expected error, got Ok"),
        }
    }

    /// Requirements: L2-CLI-004
    #[test]
    fn apply_log_level_includes_source_in_error() {
        let err = apply_log_level("[logging].level (in config)", "WHATEVER")
            .err()
            .unwrap();
        assert!(err.contains("[logging].level"));
        assert!(err.contains("WHATEVER"));
    }

    /// Requirements: L2-CFG-010, L2-CLI-011, L1-EXIT-008
    #[test]
    fn run_count_with_invalid_config_log_level_fails() {
        let bad = write_temp_file(".toml", b"[logging]\nlevel = \"NOPE\"\n");
        let globals = GlobalArgs {
            log_level: None,
            irig_day_advisory: None,
            config: Some(bad.clone()),
        };
        let result = run_count(globals, PathBuf::from("/no/such/recording.mie"));
        let _ = std::fs::remove_file(&bad);
        match result {
            Err(e) => {
                assert_eq!(
                    e.code,
                    exit_code::CONFIG,
                    "config-level error should exit 5"
                );
                assert!(
                    e.message.contains("Invalid logging.level"),
                    "expected config-level error, got: {}",
                    e.message
                );
            }
            Ok(()) => panic!("expected error, got Ok"),
        }
    }

    /// Requirements: L2-CLI-004, L2-CLI-011, L1-EXIT-007
    #[test]
    fn run_count_with_invalid_cli_log_level_fails_via_resolve_config() {
        // The run() entry-point catches bad CLI levels first (exit 4),
        // but resolve_config — which is what the runners use — also
        // re-validates the CLI value. Test that path directly; a bad
        // CLI value is a usage error (exit 4).
        let globals = GlobalArgs {
            log_level: Some("NOPE".to_string()),
            irig_day_advisory: None,
            config: None,
        };
        let result = run_count(globals, PathBuf::from("/no/such/recording.mie"));
        match result {
            Err(e) => {
                assert_eq!(e.code, exit_code::USAGE, "bad --log-level should exit 4");
                assert!(
                    e.message.contains("invalid --log-level"),
                    "expected CLI-level error, got: {}",
                    e.message
                );
            }
            Ok(()) => panic!("expected error, got Ok"),
        }
    }

    /// Combining input methods (positionals + --manifest / --glob) is a usage
    /// error at parse time.
    /// Requirements: L2-MRG-001
    #[test]
    fn decode_rejects_combined_input_methods() {
        for argv in [
            vec!["a.mie", "--manifest", "list.txt"],
            vec!["a.mie", "--glob", "*.mie"],
            vec!["--manifest", "list.txt", "--glob", "*.mie"],
        ] {
            let mut it = args(&argv);
            match parse_decode(&mut it) {
                Err(ParseError::Other(msg)) => {
                    assert!(msg.contains("only one input method"), "got: {msg}");
                }
                other => panic!("expected combined-method usage error, got {other:?}"),
            }
        }
    }

    /// More than `MAX_MERGE_FILES` resolved inputs is a usage error (exit 4),
    /// rejected before any file is opened.
    /// Requirements: L2-MRG-001
    #[test]
    fn resolve_inputs_rejects_over_cap() {
        let a = DecodeArgs {
            inputs: (0..=crate::merge::MAX_MERGE_FILES)
                .map(|i| PathBuf::from(format!("f{i}.mie")))
                .collect(),
            ..Default::default()
        };
        match resolve_inputs(&a) {
            Err(e) => {
                assert_eq!(e.code, exit_code::USAGE);
                assert!(
                    e.message.contains("too many input files"),
                    "got: {}",
                    e.message
                );
            }
            Ok(_) => panic!("expected over-cap usage error"),
        }
    }

    /// Requirements: L2-CLI-014
    ///
    /// The help banner is prose on a stream, and L2-CLI-014 binds prose to the
    /// same ASCII rule as payload. It did not always: the rule once exempted
    /// stderr prose, and this banner opened with a U+2014 em dash that a
    /// Windows console -- which runs at the OEM code page, not UTF-8 -- drew
    /// as three unrelated glyphs. Operators reported it as memory corruption.
    ///
    /// `scripts/assert-ascii-output.py` enforces this across all three trees at
    /// the source level. This test is the runtime half, on the string that is
    /// actually written.
    ///
    /// Written as a computed value plus one assertion rather than a `panic!`
    /// arm: a failure branch is by definition never taken while the property
    /// holds, so it reads as uncovered new code on every release that does not
    /// break it.
    #[test]
    fn help_text_is_pure_ascii() {
        let offenders: Vec<(usize, char)> = HELP
            .char_indices()
            .filter(|(_, c)| !c.is_ascii())
            .map(|(i, c)| (HELP[..i].lines().count(), c))
            .collect();
        assert!(
            offenders.is_empty(),
            "HELP carries non-ASCII at (line, char): {offenders:?}"
        );
    }

    /// Requirements: L2-CLI-014
    ///
    /// The same rule, on the other kind of prose: a diagnostic an operator sees
    /// on stderr. Both were mojibake on a stock Windows console.
    #[test]
    fn error_messages_are_pure_ascii() {
        let messages = [
            MieError::FirstRecordTruncated {
                offset: 0x40,
                record_bytes: 80,
                available_bytes: 12,
            }
            .to_string(),
            MieError::NoValidRecords {
                path: PathBuf::from("rec.mie"),
                scan_bytes: 4096,
            }
            .to_string(),
        ];
        for message in &messages {
            assert!(message.is_ascii(), "diagnostic is not ASCII: {message}");
        }
    }
}
