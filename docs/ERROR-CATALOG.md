# Aero1553 — Error Catalog

Operator-facing reference for every error and diagnostic the Aero1553 CLI and libraries can surface. Use this when:

- The CLI exited non-zero and you need to know what happened.
- The CSV `ERROR_CODE` column contains an unfamiliar code.
- You see a `WARN` line in the stderr log and need to know whether it's actionable.
- You're integrating one of the libraries (Rust crate or Python package) and need to know which error class to catch.

This doc covers the **observable surface**. Spec rationale lives in [`L1-REQ.md`](L1-REQ.md), [`L2-REQ.md`](L2-REQ.md), and [`L3-REQ.md`](L3-REQ.md); a forward trace from each requirement to its test artifacts lives in [`TRACE-MATRIX.md`](TRACE-MATRIX.md).

---

## 1. CLI exit codes

The exit-code taxonomy is pinned by L1-EXIT-001 through L1-EXIT-010 and the L2-CLI-011 table, and is identical across both implementations. Every decode invocation logs a one-line `decode exit class:` summary (L1-EXIT-005) so the class is grep-able even when only stderr is captured.

| Code | Class | Triggering errors | What it means | Operator action |
|------|-------|-------------------|---------------|-----------------|
| **0** | `complete` | (none — decode finished normally) | Every record decoded without sync loss. | None. |
| **0** | `partial-recovered` | (none — decode finished after sync loss recovery) | At least one mid-file sync loss occurred and was recovered. INFO summary names the recovery count. | Investigate the recording source if recovery counts are high or trending up. |
| **0** | `complete` (`--allow-partial`) | `UnrecoverableSyncLoss` on the unrecoverable-but-tolerated path | An unrecoverable sync loss occurred but `--allow-partial` preserved the rows decoded so far as `<dest>.partial`. | Inspect the `.partial` output, then triage the recording. |
| **0** | `complete (broken-pipe on stdout)` | a broken pipe on stdout output | A downstream consumer closed early (e.g. `aero1553 decode … \| head`). Not an error. | None. |
| **0** | `empty-recording` | (none — a valid but empty recording) | The input is a genuine MIE recording that captured **zero records**: its stream opens directly on the `0x0000` end-of-records terminator (e.g. an unused MIL-STD-1553 channel — literally the two bytes `00 00`). A **header-only CSV** is written and a WARN names the empty capture (L1-EXIT-010 / L2-RDR-021). Distinct from exit `2`, which is a *wrong-file* rejection. | None — the recording simply captured nothing. |
| **1** | runtime / decode error | `RecordTruncated`, `FirstRecordTruncated`, `PayloadError`, `InvalidTypeWord`, `UnknownTypeWord`, `UnknownErrorCode`, `WriterError` (non-broken-pipe), file I/O errors (incl. input not found) | Per-record validation failed in strict mode, the input couldn't be opened, or the output sink failed. | Read the stderr message; if it's a record error, lenient mode (`decode.strict = false`) usually skips and continues. |
| **2** | `no-records` | `NoValidRecords`, `HomogeneousPayload`, `TimestampFormatMismatch` (strict mode only — ambiguous auto-detection per L2-DEC-016, or a forced `--input-time-format` the recording decisively contradicts per L2-DEC-013) | The input file isn't an MIE recording at all (wrong file type, single-byte-pad, ambiguous timestamp format), or the forced timestamp format is wrong for the file. No output file is created. **Not** the same as a valid empty recording (exit `0`, `empty-recording`): that case is recognized by the `0x0000` terminator at offset 0. | Verify the input path. If it's actually a recording, check that records begin within the first 64 KB and that the timestamp format is recognizable; drop `--input-time-format` to auto-detect, or pass the correct `--input-time-format irig\|standard`. |
| **3** | `partial-unrecoverable` | `UnrecoverableSyncLoss` without `--allow-partial` | A mid-file sync loss could not be recovered within the 64 KB scan window. | Re-run with `--allow-partial` to keep the rows decoded before the loss as `<dest>.partial`, then triage the recording. |
| **4** | usage error | unknown/missing/invalid flag or argument, invalid flag value (e.g. `--detect-records 99`, `--standard-tick-rate-hz 0`), a `--glob` with a wildcard in its directory part (`captures/**/*.mie`), no subcommand | The command line itself is wrong. No input is opened and no output file is created. | Fix the invocation; run `--help` for the accepted flags. |
| **5** | configuration error | config file not found, malformed TOML, or an invalid config value (e.g. `input_time_format = "potato"`, out-of-range `detect_records`) | The `--config` file can't be loaded or fails validation. No input is opened and no output file is created. | Fix the TOML file named in the stderr message. |
| **6** | merge-incompatible inputs | `IncompatibleMergeInputs` — a multi-file merge whose inputs cannot share an absolute timeline: an input resolves to the Standard timestamp format, leads with a freerun IRIG record, or the set mixes timestamp formats (L1-EXIT-009 / L2-MRG-003) | Time-sorted merge requires every input to be calendar-locked IRIG. The offending file and its detected format are named on stderr. No output file is created. | Merge only calendar-locked IRIG recordings together; decode Standard or freerun inputs individually. |

The `count` and `dump` subcommands inherit `0`, `1`, `2`, `4`, and `5` — they don't write a streaming output that could be partial (no exit `3`) and they don't merge (no exit `6`) (L2-CLI-011). A multi-file merge that exceeds `MAX_MERGE_FILES` (256) inputs, or combines input methods (positionals + `--manifest` / `--glob`), is a **usage error → exit 4**.

**`--allow-partial` on a merge where every input fails.** A merge under `--allow-partial` drops a failing input and commits the rest as `.partial`, exit `0` (L2-MRG-004). When **every** input fails at open or priming there is nothing to keep, so the flag does not apply: the run exits with the **first** failing input's own code — `1` for a missing or unreadable file, `2` for one that is not a recording — and writes no output file, exactly as without the flag. A valid empty recording counts as a success, so an empty recording beside a failing input is still a partial merge.

**Configuration vs. flag-value validation.** Out-of-range or malformed values are rejected *before* decoding begins, with a stderr message naming the offending key or flag. The same logical check yields a different code depending on the *source*: a bad **CLI flag value** (e.g. a non-positive `--standard-tick-rate-hz`, an out-of-range `--detect-records`) is a **usage error → exit 4**, while the same value supplied through a **TOML key** is a **configuration error → exit 5** (L2-CFG-011, L2-CLI-012). Either way the input is never opened and no output file is created.

---

## 2. Library exception / error hierarchy

The Python package exposes a class hierarchy rooted at `Aero1553Error`; the Rust crate exposes a single `MieError` enum with a `kind()` discriminant. The two are kept in lockstep — every variant has a counterpart in the other.

### Python (`aero1553.exceptions`)

```
Exception
└── Aero1553Error                    (catch-all base)
    ├── MieFileError                 (wrong file / file-system issue)
    │   ├── MieFileNotFoundError
    │   ├── MieFileEmptyError
    │   ├── MieFileIoError
    │   ├── MieNoValidRecordsError
    │   ├── MieHomogeneousPayloadError
    │   ├── MieTimestampFormatMismatchError
    │   ├── MieCalendarUnavailableError
    │   ├── MieIncompatibleMergeInputsError
    │   ├── MieInputOutputCollisionError
    │   └── MieClobberRefusedError
    ├── MieRecordError               (per-record problem; carries an offset)
    │   ├── MieInvalidTypeWordError
    │   ├── MieUnknownTypeWordError
    │   ├── MieRecordTruncatedError
    │   ├── MieFirstRecordTruncatedError
    │   ├── MiePayloadError
    │   ├── MieUnknownErrorCodeError
    │   └── MieUnrecoverableSyncLossError
    ├── MieWriterError               (output write failed)
    └── MieNonMonotonicInputError    (merge input not internally time-sorted)
```

### Rust (`aero1553::MieError`)

A single `enum MieError { … }` with the same set of variants. `MieError::kind()` returns a `MieErrorKind` discriminant. Of the two predicates, only `is_record_error()` mirrors the Python class split — it matches `MieRecordError` exactly. `is_file_error()` is deliberately **narrower** than `MieFileError`; see the note below the listing.

```
MieError ├── FileNotFound             ── MieErrorKind::FileNotFound             (is_file_error)
         ├── FileEmpty                ── MieErrorKind::FileEmpty                (is_file_error)
         ├── FileIo                   ── MieErrorKind::FileIo                   (is_file_error)   ↔ MieFileIoError
         ├── NoValidRecords           ── MieErrorKind::NoValidRecords
         ├── HomogeneousPayload       ── MieErrorKind::HomogeneousPayload
         ├── TimestampFormatMismatch  ── MieErrorKind::TimestampFormatMismatch
         ├── IncompatibleMergeInputs  ── MieErrorKind::IncompatibleMergeInputs  (is_file_error analogue)
         ├── InputOutputCollision     ── MieErrorKind::InputOutputCollision
         ├── ClobberRefused           ── MieErrorKind::ClobberRefused
         ├── InvalidTypeWord          ── MieErrorKind::InvalidTypeWord          (is_record_error)
         ├── UnknownTypeWord          ── MieErrorKind::UnknownTypeWord          (is_record_error)
         ├── RecordTruncated          ── MieErrorKind::RecordTruncated          (is_record_error)
         ├── FirstRecordTruncated     ── MieErrorKind::FirstRecordTruncated     (is_record_error)
         ├── PayloadError             ── MieErrorKind::PayloadError             (is_record_error)
         ├── UnknownErrorCode         ── MieErrorKind::UnknownErrorCode         (is_record_error)
         ├── UnrecoverableSyncLoss    ── MieErrorKind::UnrecoverableSyncLoss    (is_record_error)
         ├── WriterError              ── MieErrorKind::WriterError              (neither predicate)
         └── NonMonotonicInput        ── MieErrorKind::NonMonotonicInput        (neither predicate — no byte offset)
```

`MieRecordError` and `MieError::is_record_error()` cover the same seven failures exactly, `UnrecoverableSyncLoss` included. (Rust omitted it until v2.12.0 — see the CHANGELOG.)

Python's `MieFileError` is **wider** than `MieError::is_file_error()`, which answers only "did input I/O fail" (`FileNotFound`, `FileEmpty`, `FileIo`). The whole-file rejections (`NoValidRecords`, `HomogeneousPayload`, `TimestampFormatMismatch`, `IncompatibleMergeInputs`) and the destination guards (`InputOutputCollision`, `ClobberRefused`) extend `MieFileError` in Python but answer `false` to **both** Rust predicates; match on `kind()` for those. `WriterError` and `NonMonotonicInput` sit directly under `Aero1553Error` and likewise answer `false` to both.

---

## 3. File-level errors

These fire before any record is decoded, or before the writer touches the destination. All but the last row are catchable in Python as `MieFileError`; `MieNonMonotonicInputError` is grouped here because it fires at the same stage, but it extends `Aero1553Error` **directly** and a `MieFileError` handler will not catch it. In Rust only the I/O subset (`FileNotFound`, `FileEmpty`, `FileIo`) answers `true` to `is_file_error()`; for the rest of this section, match on `MieError::kind()` (see §2).

| Variant | When it fires | Exit | What to do |
|---------|---------------|------|------------|
| `MieFileNotFoundError` / `FileNotFound` | Input path does not exist (L2-RDR-005). | 1 | Check the path; verify mount / permissions. |
| `MieFileEmptyError` / `FileEmpty` | Input file exists but is zero bytes (L2-RDR-006). | 1 | Upstream recording or transfer failure; check the source. |
| `MieFileIoError` / `FileIo` | Open, stat, read or mmap fails — permission denied, the path is not a regular file, a device error. Both implementations attempt the **open before the size check**, so an unopenable path reports I/O rather than "empty" (a directory stats as zero bytes on Windows). Python wraps the originating `OSError` and keeps it as `.source`; before v2.12.0 it escaped unconverted. | 1 | Inspect the underlying OS error; usually a filesystem permission or hardware issue. |
| `MieNoValidRecordsError` / `NoValidRecords` | The first 64 KB contain no valid MIE record at all (L1-EXIT-002). Typical cause: input isn't an MIE recording. | **2** | Verify the input is actually MIE; if it is, records may begin past the 64 KB scan window. |
| `MieHomogeneousPayloadError` / `HomogeneousPayload` | The first 4 candidate records are byte-identical in non-timestamp positions (L2-SYN-018). Typical cause: 0x20-padded or otherwise pathological single-byte file. | **2** | Verify the input file; almost always a wrong-file-type or corruption indicator. |
| `MieTimestampFormatMismatchError` / `TimestampFormatMismatch` | L2-DEC-015 multi-record probe completed with an L2-DEC-016 Ambiguous classification (max aggregate score < 4 OR margin < 3). **Strict mode only**: lenient mode logs a single WARN with the score breakdown and proceeds with the chosen format. Typical cause: the file genuinely isn't an MIE recording, OR the first N records score weakly enough that the probe can't pick a side. | **2** | First confirm the file is actually an MIE recording. If it is, pass `--input-time-format irig` or `--input-time-format standard` to force the choice. If a one-time decode is acceptable with the auto-picked format (IRIG on ties per L2-DEC-012), drop `--strict` to take the lenient path. |
| `MieCalendarUnavailableError` / `CalendarUnavailable` | A calendar rendering (`--output-time-format iso` or `dom`) was requested but the recording cannot supply a calendar date (L2-WRT-026). Three causes: the recording is **Standard-format** -- a free-running counter with no epoch, so no year places it on a calendar; the IRIG timestamp is **freerun**, so its day and time fields are relative rather than calendar-anchored; or a record carries **day-of-year 366 against a common year**, which does not exist. The first two are detected before any row is written; the third fails the whole run rather than the row, because it means the configured year is wrong and every date in the output is therefore suspect. | **2** | For a Standard or freerun recording, use `--output-time-format doy` -- there is no calendar date to render, and the decoder refuses rather than fabricating one. For day 366, correct `--year` / `[output] year`: the recording spans a leap day, so the year you supplied is not the year it was recorded in. |
| `MieInputOutputCollisionError` / `InputOutputCollision` | A path the run could commit resolves to one of the inputs (L2-WRT-014). Not only the destination: the derived `<stem>_errors<suffix>` and the `<dest>.partial` variants are commit targets too, so `-o capture.mie --separate-errors` is refused when any input is named `capture_errors.mie`. | 1 | Choose a different output path; decoding in-place is unsafe under mmap. The message names the *target* that collided, which may not be the path you passed to `-o`. |
| `MieClobberRefusedError` / `ClobberRefused` | A path the run is committing exists and `--no-clobber` / `output.no_clobber = true` is set (L2-WRT-017). Raised in either of two places, indistinguishably to the operator: at the **pre-flight**, before anything is opened; or at the **commit**, when the target appeared while the decode was running (L2-WRT-023). The latter also covers `<destination>.partial` and the errors file's own `.partial`, which are never pre-flighted -- a stale one must not refuse a run that was never going to write one. | 1 | Remove the existing file or unset the flag. The message names the *target* that was taken, which under `--allow-partial` may be a `.partial` rather than the path you passed to `-o`. |
| `MieIncompatibleMergeInputsError` / `IncompatibleMergeInputs` | A multi-file merge input is Standard-format, leads with a freerun IRIG record, or the set mixes timestamp formats — so the inputs can't be ordered on a common absolute timeline (L2-MRG-003). Raised before any output. | **6** | Merge only calendar-locked IRIG recordings; decode Standard / freerun inputs individually. |
| `MieNonMonotonicInputError` / `NonMonotonicInput` | During a multi-file merge, one input is not internally time-sorted — a record's IRIG microsecond key stepped backward relative to the previous record from the **same** file (L2-MRG-006). Typical cause: sync-loss recovery or a day/year rollover inside that recording. **Strict mode only**: lenient mode logs a single WARN per input and still merges every record (the merged output may carry those rows out of order). | 1 | Investigate the named input (its own clock ran backward — possible corruption). To merge anyway, drop strict mode (`decode.strict = false`) and accept the WARN; the merge completes with all records. |

---

## 4. Record-level errors

These fire when a specific record fails decoding. Catchable in Python as `MieRecordError`; in Rust via `MieError::is_record_error()`. All variants carry the byte `offset` of the failing record (4-digit uppercase hex in formatted messages, matching DDC vendor conventions).

In **lenient mode** (default), most record errors result in the record being skipped with a `WARN` log line; the iterator continues. In **strict mode** (`decode.strict = true`), the error is raised and decoding stops.

| Variant | When it fires | Lenient | Strict |
|---------|---------------|---------|--------|
| `MieInvalidTypeWordError` / `InvalidTypeWord` | Type Word has zero or below-minimum word count (L2-SYN-002). | skip + WARN | raise |
| `MieUnknownTypeWordError` / `UnknownTypeWord` | Type Word's message type field is not in the known set (L2-SYN-001). | skip + WARN | raise |
| `MieRecordTruncatedError` / `RecordTruncated` | A non-first record's declared extent runs past EOF (L2-RDR-002, L2-RDR-003). | stop iteration cleanly | raise |
| `MieFirstRecordTruncatedError` / `FirstRecordTruncated` | The *first* record after header detection has a declared extent past EOF (L2-RDR-004). Distinct class so it doesn't get confused with mid-stream truncation. | WARN naming the truncation, then `NoValidRecords` (exit 2), zero records emitted | raise |
| `MiePayloadError` / `PayloadError` | Record's payload is internally inconsistent — IRIG range failure, structural invariant violation (L2-SYN-020/021/022/023), or generic extraction failure. | skip + WARN | raise |
| `MieUnknownErrorCodeError` / `UnknownErrorCode` | An errored record (Type Word bit 14 set) carries an Error Word value outside the known DDC + decoder set (L2-ERR-004). | log WARN, emit row with the unknown code | raise |
| `MieUnrecoverableSyncLossError` / `UnrecoverableSyncLoss` | After a sync loss, `recover_sync` scanned the full 64 KB window without finding a valid record (L2-SYN-011 / L1-EXIT-004). | terminal `Err` on the iterator; CLI exits **3** by default, or 0 + `.partial` with `--allow-partial` | same |

For sync-validation failures, `sync.validate_record_detailed` /
`sync::validate_record_detailed` exposes the shared `ValidationFailure` reason
without changing the existing record-error classes. DEBUG logging adds a single
context line capped at 32 bytes; normal WARNING output remains compact.

---

## 5. Writer errors

| Variant | When it fires | Exit |
|---------|---------------|------|
| `MieWriterError` / `WriterError` | An underlying `io::Error` from the CSV writer (disk full, permission denied, etc.). Preserves the source OS error message. | 1 |
| `MieWriterError` / `WriterError` (broken-pipe variant) | The stdout consumer closed before the writer flushed. Classified by `MieError::is_broken_pipe()` in Rust and `aero1553.writer.is_broken_pipe()` in Python. Note the Python predicate is **not** just `isinstance(exc, BrokenPipeError)`: CPython raises that only on POSIX, while Windows reports a closed pipe as a bare `OSError` with `EINVAL`, so both forms are matched (the widened `errno` match is scoped to Windows so a real POSIX `EINVAL` write failure stays a failure). Applies to `decode` and `dump` alike. (L2-WRT-018) | **0** |

Disk-full and permission failures during the atomic temp-file write are surfaced via `WriterError` and the temp file is unlinked before the process exits (L2-WRT-015 / L2-WRT-016).

---

## 6. DDC hardware error codes (`0x01xx` family)

When the DDC card detects a bus error during recording, it sets bit 14 of the Type Word, truncates the payload, and appends a 16-bit Error Word containing one of these codes. The decoder copies that value into the CSV `ERROR_CODE` column.

| Code | Constant | Description (from DDC documentation) |
|------|----------|--------------------------------------|
| `0x011E` | `ERROR_MANCHESTER_PARITY` | Manchester encoding error, parity error, or bit-count error on the wire. |
| `0x0120` | `ERROR_NO_RESPONSE` | RT did not respond with a Status Word, or the response had too few data words. |
| `0x0136` | `ERROR_INVERTED_SYNC` | Inverted sync pattern detected on a data word (data word looks like a Cmd/Status word). |
| `0x0140` | `ERROR_TOO_MANY_WORDS` | More data words received than the Command Word specified. |
| `0x0150` | `ERROR_UNKNOWN_DDC` | Catch-all for DDC errors not in the above set (firmware variation, undocumented condition). |

Any `0x01xx` code outside this set surfaces as `MieUnknownErrorCodeError` / `MieError::UnknownErrorCode` in strict mode, and as a WARN log line in lenient mode. The CSV row still emits the unknown code so it's visible to analysts.

---

## 7. Decoder-assigned codes (`0x20xx` family)

When the DDC card emits a `SPURIOUS_DATA` record (message type `0x20`) — i.e., orphan data words without an associated Command Word — the decoder assigns one of two synthetic codes based on the record's relationship to the preceding record (L2-ERR-005 / L2-ERR-006).

| Code | Constant | Meaning |
|------|----------|---------|
| `0x2000` | `ERROR_SPURIOUS_CONTINUATION` | This SPURIOUS_DATA record immediately follows an errored record (Type Word bit 14 set on the previous decoded record). Most likely the tail of a transaction the bus cut short. |
| `0x2001` | `ERROR_SPURIOUS_STANDALONE` | Standalone SPURIOUS_DATA — the immediately preceding decoded record was not an error. Genuinely orphan fragment. |

The continuation flag resets on any decode-time corruption boundary between the error and the SPURIOUS — see L2-ERR-005's "immediately preceding *successfully decoded* record" clause.

**Adjacency in the CSV is guaranteed.** `0x2000` is defined in terms of the *preceding row*, so it would be meaningless if row ordering could separate a SPURIOUS_DATA record from the errored record it continues. Canonical row ordering (L1-OUT-003) therefore **pins** Command-Word-less records: because a SPURIOUS_DATA row has no `RT` or `MSG` to sort on, it is excluded from the equal-timestamp sort and travels with whichever record preceded it on input. So when you read `ERROR_CODE = 2000`, the row immediately above it in the CSV *is* the error it continues — even when a lower-`RT` record shares the same `TIME_STAMP` and sorts ahead of the pair. That holds because the continuation is reported at its parent's `TIME_STAMP` (L2-ERR-005), even when the card stamped the leftover words later: the pair always shares one time, so neither the equal-timestamp sort nor a multi-file merge can put another record between them. (`dump` still shows the card's own stamp; the `TIME_STAMP` cell is the one documented difference from the vendor CSV, `VENDOR-CSV-DIFFS.md` §3d.)

**Filters and `--collapse-duplicates` keep the pair together.** A continuation shares its parent's fate (L2-FLT-003, L2-MRG-007): an RT, subaddress or bus filter, or a type exclusion naming the parent's type, keeps or removes both rows; `--collapse-duplicates` collapses both or neither. The one way to see a `2000` row without its parent is to ask for exactly that: `--include-types SPURIOUS_DATA` selects spurious rows by their own type, so it keeps continuations while dropping every other record. (`--exclude-types SPURIOUS_DATA` removes every spurious row and keeps the errors.)

---

## 8. Anomaly observations (WARN-only, no error class)

Two structural invariants emit `WARN` log lines but do **not** raise or skip the record. Both strict and lenient mode emit the row. These observations exist because the underlying behavior is sometimes legitimate (real-bus noise on a multi-drop bus, undocumented vendor extensions) so outright rejection would produce false negatives on real recordings.

| Invariant | When it fires | Log line |
|-----------|---------------|----------|
| **L2-SYN-024** (`STATUS_RT_MISMATCH`) | A Status Word's RT field doesn't match the Command Word's RT. Can happen on a multi-drop bus when a different RT responds to a broadcast. | `L2-SYN anomaly at 0x<offset>: Status RT = <N> does not match Cmd RT = <M> (raw Status = 0x<raw>); possible bus interference` |
| **L2-SYN-025** (`TYPE_WORD_RESERVED_BIT`) | Type Word bit 15 (reserved per `docs/MIE-FORMAT.md`) is set. May indicate undocumented vendor extension or wire corruption. | `L2-SYN anomaly at 0x<offset>: Type Word bit 15 (reserved) is set in raw 0x<raw>; possible undocumented vendor extension` |

If you see a high rate of either WARN in a recording, the recording itself is the thing to investigate — not the decoder.

---

## 9. Decision tree: "I got X, what now?"

**The CLI exited non-zero.** Look at the `decode exit class:` line in stderr first; it names the exit class. Then:

- `exit class: complete` (exit 0) — nothing failed; you may have hit `--allow-partial` and produced a `.partial`.
- `exit class: no-records` (exit 2) — verify the input file is actually MIE. If it is, check that records begin within the first 64 KB. If you see `HomogeneousPayload`, the file is a single-byte pad (e.g. zeros, 0x20-fill).
- `exit class: partial-unrecoverable` (exit 3) — the recording has unrecoverable corruption. Re-run with `--allow-partial` to capture what decoded before the loss, then triage the source.
- Otherwise (exit 1) — read the stderr error line; usually a per-record error or a file-safety preflight (input/output collision, no-clobber).

**The CSV `ERROR` column shows `ERROR`.** Look at `ERROR_CODE`:
- `011E`/`0120`/`0136`/`0140`/`0150` — a documented DDC bus error. See section 6.
- Anything else in `01xx` — undocumented DDC firmware variation; treat as bus error.

**The CSV `ERROR` column shows `SPURIOUS`.** Look at `ERROR_CODE`:
- `2000` — continuation of a preceding errored transaction. Usually paired with the row above.
- `2001` — orphan fragment. May indicate a recording-card hiccup.

**Stderr shows a `WARN` line.**
- `non-monotonic timestamp at …` — a record's timestamp went backwards relative to a prior record on the same RT/MSG (L2-RDR-017). `DELTA` is left empty. One WARN per RT/MSG per file.
- `L2-SYN anomaly at …` — see section 8. Not an error; the record was emitted.
- `L2-SYN structural invariant violation at …` — strict-mode would have rejected; lenient mode skipped the record. Check whether your recording has a corruption pattern worth investigating.
- `sync lost at …` / `sync recovered at …` — mid-file sync loss, recovered automatically. The `partial-recovered` exit-class summary counts these.

---

## 10. Trace links

Every error class and code in this catalog satisfies one or more L1/L2/L3 requirements. The mapping is preserved in `docs/TRACE-MATRIX.md`; the key sections are:

- **L1-ERR / L2-ERR-***: error-record decoding, SPURIOUS_DATA, separate vs inline output modes.
- **L1-EXIT-* / L2-CLI-011**: exit-code semantics and the `decode exit class:` summary.
- **L1-SYN / L2-SYN-***: validation invariants, header detection, sync recovery, the homogeneous-payload defense.
- **L1-OUT-002 / L2-WRT-014..017**: output destination integrity (collision detection, no-clobber, atomic write, partial preservation).
- **L1-ROB-001**: the fuzz harness verifying no error path panics on arbitrary input bytes.
