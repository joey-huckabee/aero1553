# Aero1553 — Level 2 Requirements

## Purpose

This document establishes the Level 2 (L2) SHALL-statement requirements for Aero1553. L2 requirements are architectural derivations of the L1 requirements documented in `L1-REQ.md`: they specify *how* each L1 obligation is structurally satisfied, without yet prescribing implementation details (those belong to L3).

Every L2 requirement traces to exactly one L1 parent via the `**Parent**:` field. When an L2 is motivated by multiple L1 obligations, the primary parent is declared in `**Parent**:` and the supporting L1s are mentioned in prose. L3 requirements derive from these L2s.

## Conventions

L2 identifiers follow the format `L2-<CATEGORY>-<NNN>`. Each L2 declares its parent L1 explicitly. Metadata fields (Statement, Rationale, Verification Method) carry the same semantics as in `L1-REQ.md`.

L2s are organized by category. Full forward trace tables appear in `TRACE-MATRIX.md`. ID numbering is monotone within each category; gaps reflect retired identifiers and are never reused.

**Status and verification artifacts** are tracked in [`docs/TRACE-MATRIX.md`](TRACE-MATRIX.md), regenerated from test markers and parent links by `scripts/build-trace-matrix.py`. This file holds only the spec content above.

## Table of categories

| Code      | Title                                       |
|-----------|---------------------------------------------|
| `DEC`     | Binary decoding                             |
| `SYN`     | Synchronization, validation, invariants     |
| `RDR`     | Reader behavior                             |
| `MSG`     | Message semantics                           |
| `ERR`     | Error record handling                       |
| `WRT`     | CSV output and output destination integrity |
| `CFG`     | Configuration                               |
| `LOG`     | Diagnostic logging policy                   |
| `FLT`     | Filtering                                   |
| `CLI`     | Shared CLI capabilities                     |
| `CONF`    | Cross-implementation conformance            |

(Per-category and total requirement counts are intentionally omitted — they
drift as requirements are added. The requirement entries below, and the
auto-generated [`TRACE-MATRIX.md`](TRACE-MATRIX.md), are the source of truth.)

---

## L2-DEC: Binary decoding

#### L2-DEC-001

**Parent**: L1-DEC-001
**Statement**: A 16-bit Type Word SHALL decode `message_type` from bits 0-6, bus from bit 7, `word_count` from bits 8-13, and the errored-record flag from bit 14.
**Rationale**: The Type Word is the single bit-field that drives record framing, classification, and the error path. Its layout is fixed by the DDC MIE format and shared across both implementations.
**Verification Method**: Test (T)

#### L2-DEC-002

**Parent**: L1-DEC-002
**Statement**: A 3-word IRIG timestamp SHALL decode day-of-year, hour, minute, second, microsecond, and the freerun flag according to `docs/MIE-FORMAT.md`.
**Rationale**: The IRIG packing is the DDC-specific timestamp convention. Both implementations must extract the same six fields from the same three input words.
**Verification Method**: Test (T)

#### L2-DEC-003

**Parent**: L1-DEC-002
**Statement**: IRIG decoding SHALL decode the freerun flag from bit 15 of the upper timestamp word.
**Rationale**: The freerun flag indicates that the card's IRIG clock is not calendar-locked. Downstream validation (L2-SYN-019) relaxes the day-of-year constraint when this bit is set; misreading the bit would either reject valid free-run recordings or accept invalid calendar-locked ones.
**Verification Method**: Test (T)

#### L2-DEC-004

**Parent**: L1-DEC-003
**Statement**: A 16-bit Command Word SHALL decode RT address, T/R direction, subaddress, and data-word count, where a raw count of zero means 32 words.
**Rationale**: The 32-word special case is from the MIL-STD-1553 specification and must be honored to correctly size payloads on full-length transactions.
**Verification Method**: Test (T)

#### L2-DEC-007

**Parent**: L1-DEC-002
**Statement**: A Standard timestamp SHALL decode as a 32-bit free-running counter.
**Rationale**: Standard timestamps lack a calibrated tick rate; the decoder surfaces the raw counter and defers any time-domain interpretation. The 32-bit-counter shape distinguishes Standard from IRIG (which is 48 bits of structured fields).
**Verification Method**: Test (T)

#### L2-DEC-008

**Parent**: L1-DEC-001
**Statement**: All 16-bit words SHALL be read as little-endian values.
**Rationale**: DDC MIE files are written by x86 hardware as little-endian. Both implementations target little-endian decode regardless of host endianness.
**Verification Method**: Inspection (I), Test (T)

#### L2-DEC-009

**Parent**: L1-DEC-003
**Statement**: Payload extraction SHALL remain bounded by the Type Word's declared record extent and SHALL NOT consume bytes from a following record.
**Rationale**: A Command Word with `data_word_count = 32` declares a payload that may exceed the Type Word's declared extent on a malformed or truncated record. The decoder respects the Type Word extent as authoritative to avoid overrunning into the next record. Extraction is bounded to the record (`record_end = offset + word_count * 2`, already validated to fit the file): Rust slices the buffer (`let record_data = &self.data[..record_end]`) and Python passes `record_end` into `_extract_payload`, whose `_r16`/`_read_n` helpers return `None`/`()` on an out-of-bounds read — so an over-claim yields empty/partial data rather than reading past the record. There are **two** over-claim cases: (a) a single Command Word declaring more payload than the Type Word holds, which the L2-SYN-022 capacity invariant catches *before* extraction (it is computed from Cmd1); and (b) an RT-to-RT record whose **Cmd2** (the transmit command, which carries the data-word count) over-claims while Cmd1 stays small — the capacity invariant cannot see Cmd2, so the record-bounded reads are what let extraction complete safely (this was the regression behind the L1-ROB-001 fuzz `struct.error` in the Python reader). In case (b) the record-bounded read is the byte-level guarantee of this requirement; the over-claim itself is a Cmd1/Cmd2 `data_word_count` disagreement, so the post-extract **L2-SYN-027** invariant then rejects the record (strict errors, lenient skips). Two targeted tests cover both cases in both implementations: `payload_extraction_does_not_overrun_into_next_record` (case a — strict rejects, lenient decodes the successor intact) and `rt_to_rt_cmd2_overclaim_does_not_overrun` (case b — extraction completes without overrun, then L2-SYN-027 rejects, and the successor decodes intact at its true offset).
**Verification Method**: Test (T), Inspection (I)

#### L2-DEC-010

**Parent**: L1-DEC-001
**Statement**: Decoded records SHALL retain their source byte offset and raw Type and Command Word values where present, in the internal record representation.
**Rationale**: Offset and raw word values are needed by the reader to log record-class diagnostics and by analysts using a programmatic API. Surfacing these in CSV output is not required by L2-WRT-001 and is reserved for future debug-only output paths.
**Verification Method**: Inspection (I), Test (T)

#### L2-DEC-011

**Parent**: L1-DEC-002
**Statement**: Timestamp-format detection SHALL be file-level: the format is resolved once at the start of the decode invocation (by the bounded multi-record probe of L2-DEC-015) and used unchanged for every subsequent record in the same decode invocation. Per-record re-detection is not permitted.
**Rationale**: Mid-file re-detection would silently produce mixed time bases in one CSV, defeating the time-series semantics of `DELTA` (see L1-DLT-001). File-level resolution makes the contract simple to reason about.
**Verification Method**: Test (T)

#### L2-DEC-012

**Parent**: L1-DEC-002
**Statement**: When IRIG and Standard format detection score equally during auto-detection, IRIG SHALL be selected.
**Rationale**: Flight-test recordings overwhelmingly use IRIG; this tie-break preserves the most common path. Inverting the tie-break would silently break the dominant operational use case.
**Verification Method**: Test (T)

#### L2-DEC-013

**Parent**: L1-CFG-001
**Statement**: An explicit `--input-time-format` CLI flag or `decode.input_time_format` configuration value SHALL bypass auto-detection and force the chosen format for the entire decode. The forced format SHALL nonetheless be sanity-checked against the L2-DEC-015 detection probe before iteration begins: when the probe is **Decisive** (per L2-DEC-016) for the *other* format, the forced selection is an obviously-wrong selection and SHALL surface the timestamp-format-mismatch class (`MieError::TimestampFormatMismatch` / `MieTimestampFormatMismatchError`, shared with L2-DEC-016, exit code `2`) in strict mode, or a single WARN in lenient mode after which decoding proceeds with the forced format. A **Marginal** or **Ambiguous** probe SHALL NOT be flagged — those are exactly the cases where forcing is the legitimate operator override of a detection the heuristic cannot make confidently.
**Rationale**: Operators sometimes know the format ahead of time (e.g., from the recording campaign's documentation) and want to skip the auto-detect heuristic — but a typo such as `--input-time-format standard` on an IRIG recording would otherwise emit garbage timestamps for the whole file silently. Gating the check on a *Decisive* probe catches that mistake while never overriding the operator in the marginal/ambiguous cases where forcing exists precisely to correct a misdetection; lenient mode preserves the forced result with a warning so an intentional override still works.
**Verification Method**: Test (T)

#### L2-DEC-014

**Parent**: L1-OUT-001
**Statement**: IRIG timestamp text SHALL emit exactly six microsecond digits regardless of the decoded value. A microsecond value greater than or equal to 1,000,000 SHALL be considered unreachable given L2-SYN-004 validation, but if encountered on the defensive path the implementation SHALL truncate to six digits and SHALL log a WARN naming the offending record offset. The formatter SHALL NOT emit more than six microsecond digits under any circumstance.
**Rationale**: A seven-digit microsecond field would silently shift every downstream column in the vendor-compatible CSV by one character. The defensive truncate-plus-warn is a belt-and-braces guard against any path that bypasses L2-SYN-004 validation.
**Verification Method**: Test (T)

#### L2-DEC-015

**Parent**: L1-DEC-002
**Statement**: Auto-detection of timestamp format SHALL probe up to the first `N` records of the file, not only the first record, where `N` defaults to `8` and is configurable via the `decode.detect_records` configuration key (range `1..=32`) or the `--detect-records` CLI flag. The existing per-record scoring signals (T/R direction consistency with the Type Word, word-count plausibility under each candidate overhead, IRIG field range validity) SHALL be aggregated across the probe set; the format with the higher aggregate score is chosen. The chosen format SHALL be resolved before the first record is decoded and SHALL not change for the rest of the decode invocation, preserving the file-level resolution rule of L2-DEC-011. If fewer than `N` valid records are available before the file ends, the probe SHALL use what exists.
**Rationale**: Single-record scoring is defeated by a borderline first record (one that scores plausibly under both candidate overheads). A wrong choice corrupts timestamp values for the entire file without surfacing any diagnostic — the reader simply emits records with garbage timestamp fields and operators have to notice. Probing multiple records gives the scorer enough signal to disambiguate on files where the first record alone would not. Capping at `N` keeps the probe bounded, and a configurable default lets operators tune the trade-off for unusual recordings.
**Verification Method**: Test (T)

#### L2-DEC-016

**Parent**: L1-DEC-002
**Statement**: When the L2-DEC-015 probe completes with an indecisive result — specifically, when the winning aggregate score is below a low-confidence threshold (`max_score < 4` over the probe set) OR the margin between the two candidate scores is below a minimum-margin threshold (`|irig_score - std_score| < 3`) — a `MieTimestampFormatMismatch` error class SHALL be defined. In strict mode (`--strict` or `decode.strict = true`), this condition SHALL halt decoding with exit class `2` (the "wrong file type" class shared with `MieNoValidRecordsError` and `MieHomogeneousPayloadError` per `L1-EXIT-002`). In lenient mode (the default), the chosen format from L2-DEC-015 SHALL still be used (preserving backwards compatibility on borderline files that decoded acceptably before this requirement landed), but a single WARN SHALL be logged describing the indecisive outcome and naming both candidate scores so the operator can see how marginal the call was.
**Rationale**: The probe in L2-DEC-015 strengthens the common case (clear winner) without addressing the genuinely-ambiguous case (no clear winner). The strict-mode error gives operators who care about correctness a loud failure to act on (e.g., `--input-time-format` override or "this isn't an MIE recording"). The lenient-mode WARN preserves the current decode-and-hope behavior while making the ambiguity visible. The thresholds are intentionally conservative: they fire only when the probe genuinely could not distinguish, not when the call is decisive but the absolute score is low because of a small probe set.
**Verification Method**: Test (T)

#### L2-DEC-017

**Parent**: L1-DEC-002
**Statement**: The Standard timestamp is a free-running counter whose tick rate is card-dependent and not encoded in the file. When, and only when, a Standard tick rate is supplied out-of-band — via the `decode.standard_tick_rate_hz` configuration key or the `--standard-tick-rate-hz` CLI flag (see L2-CFG-011, L2-CLI-012) — the decoder SHALL convert a raw counter value to microseconds as `microseconds = round(raw_ticks × 1_000_000 / standard_tick_rate_hz)`, where `round` is half-away-from-zero. The supplied rate SHALL be a finite value strictly greater than zero; a non-finite or non-positive rate SHALL be rejected (L2-CFG-011, L2-CLI-012) and never silently treated as uncalibrated. A rate that passes that guard but yields a result **at or above 2⁶⁴ microseconds** SHALL also yield "no value", in every implementation. That case is reachable from ordinary input — `--standard-tick-rate-hz 1e-300` is finite and positive — and each implementation failed it differently before this was written down: Rust's `as u64` **saturated** to `u64::MAX`, a fabricated timestamp indistinguishable downstream from a real one; the C++ `static_cast` from an out-of-range `double` was **undefined behaviour**; and Python raised an uncaught **`OverflowError`** out of a decode, because the division overflowed to `inf` before `math.floor` saw it. Declining is the same answer the uncalibrated case already gets, for the same reason: a number that cannot be a timestamp must not be presented as one. When no rate is supplied, the Standard-to-microseconds conversion SHALL yield "no value" (`Timestamp::to_microseconds` returns `None` / `to_microseconds` returns `None`), preserving the historical behavior in which Standard records do not participate in `DELTA` (see L2-RDR-019). IRIG timestamps SHALL ignore the rate.
**Rationale**: Operators analyzing Standard-format recordings need inter-message timing, but the tick rate genuinely is not in the file, so the decoder cannot invent one. Making calibration explicit and opt-in keeps the default output truthful (an empty `DELTA` rather than a fabricated seconds value) while letting an operator who knows their card's counter frequency recover real timing. Pinning half-away-from-zero rounding keeps the two implementations byte-identical (Rust `f64::round` and Python `int(x + 0.5)` agree for the non-negative tick domain); banker's rounding would diverge at the half-tick boundary.
**Verification Method**: Test (T)

---

## L2-SYN: Synchronization, validation, invariants

#### L2-SYN-001

**Parent**: L1-SYN-001
**Statement**: Record validation SHALL reject unknown message types.
**Rationale**: An unknown message type indicates either a corrupt record or a format the decoder does not understand. Both cases must produce a clean rejection rather than be decoded with wrong assumptions.
**Verification Method**: Test (T)

#### L2-SYN-002

**Parent**: L1-SYN-001
**Statement**: Record validation SHALL reject word counts below the timestamp-format minimum or above 63.
**Rationale**: 63 is the architectural ceiling for the Type Word's 6-bit word-count field. The format-specific minimum guards against degenerate records too short to contain a usable timestamp.
**Verification Method**: Test (T)

#### L2-SYN-003

**Parent**: L1-SYN-001
**Statement**: Record validation SHALL reject records extending past end-of-file.
**Rationale**: A Type Word that declares more bytes than remain in the file indicates either a truncated tail (L1-DEC-005) or an in-record corruption; either way, the record cannot be decoded safely.
**Verification Method**: Test (T)

#### L2-SYN-004

**Parent**: L1-SYN-001
**Statement**: IRIG validation SHALL reject hour values >= 24, minute values >= 60, second values >= 60, day-of-year values < 1 or > 366, and microsecond values > 999,999.
**Rationale**: Calendar ranges are part of the IRIG-B specification. Out-of-range values indicate a corrupt timestamp; failing fast prevents downstream consumers from doing arithmetic on garbage time values.
**Verification Method**: Test (T)

#### L2-SYN-005

**Parent**: L1-SYN-001
**Statement**: Record validation SHALL confirm that the next `(N − 1)` record boundaries each contain a plausible Type Word, where `N` is the configured look-ahead depth per L2-SYN-026 (default `2`). The walk SHALL advance by each candidate's declared `word_count`; if fewer than 2 bytes remain at any candidate position, look-ahead SHALL terminate without rejecting the original candidate, and validation checks 1 through 5 (type, word count, fits-in-file, IRIG range, IRIG day-of-year) SHALL be authoritative for the records that were not reachable within the file. The minimum `N` is `1` (no look-ahead beyond the candidate itself); higher values catch wider classes of consecutive-same-shape corruption at small additional per-record-read cost.
**Rationale**: A single-record validation produces too many false positives during header detection and sync recovery. The two-record look-ahead (the historical default) is what made the validator usable in practice; the parameterization adds defense against two-consecutive same-shape corruption patterns that defeat the historical default. Wording is generalized in place rather than retired and re-issued so the trace matrix and the codebase keep a single canonical identifier for the look-ahead policy.
**Verification Method**: Test (T)

#### L2-SYN-006

**Parent**: L1-SYN-001
**Statement**: Header detection SHALL scan from offset zero in 2-byte, word-aligned increments.
**Rationale**: All record fields are 16-bit aligned. Byte-stepping would multiply the search space by two without finding any record the word-aligned scan misses.
**Verification Method**: Test (T)

#### L2-SYN-007

**Parent**: L1-SYN-002
**Statement**: Header detection SHALL cap its scan at 64 KB.
**Rationale**: Real MIE headers are well under this bound (typically <1 KB). Capping the scan prevents pathological inputs from forcing the decoder to read most of the file before reporting "no header found".
**Verification Method**: Test (T)

#### L2-SYN-008

**Parent**: L1-SYN-001
**Statement**: Header detection SHALL report when no valid record is found within the scan window.
**Rationale**: A failed header detection is a distinct error class from a mid-file failure; operators routinely diagnose this as "wrong file type" or "completely corrupted file".
**Verification Method**: Test (T)

#### L2-SYN-009

**Parent**: L1-SYN-001
**Statement**: Sync recovery SHALL scan forward from an invalid boundary in 2-byte, word-aligned increments.
**Rationale**: Same alignment argument as L2-SYN-006. Recovery uses the same step semantics as header detection to keep the validation path uniform.
**Verification Method**: Test (T)

#### L2-SYN-010

**Parent**: L1-SYN-002
**Statement**: Sync recovery SHALL cap its scan at 64 KB from the invalid boundary.
**Rationale**: Same scan-distance argument as L2-SYN-007. The cap is a per-recovery bound; cumulative bounding is L1-SYN-002.
**Verification Method**: Test (T)

#### L2-SYN-011

**Parent**: L1-SYN-001
**Statement**: Sync recovery SHALL report when no valid record is found within the scan window.
**Rationale**: Same diagnostic-classification argument as L2-SYN-008, applied to mid-file recovery failure. This is the trigger for the L1-EXIT-004 unrecoverable exit class.
**Verification Method**: Test (T)

#### L2-SYN-012

**Parent**: L1-LOG-001
**Statement**: Header detection SHALL log the detected header size at INFO level.
**Rationale**: Header size is operationally useful — it lets the operator confirm the file was recognized as MIE and tells them how many bytes were skipped before the first record.
**Verification Method**: Test (T)

#### L2-SYN-013

**Parent**: L1-LOG-001
**Statement**: Sync recovery SHALL log sync loss at WARNING and successful recovery at INFO. At DEBUG level, a validation failure SHALL additionally log one context hex line capped at 32 bytes.
**Rationale**: A sync loss is operationally noteworthy (the operator should be told the file is not pristine); a successful recovery is informative but does not warrant a warning of its own.
**Verification Method**: Test (T)

#### L2-SYN-014

**Parent**: L1-SYN-001
**Statement**: Header detection, continuous decoding, and sync recovery SHALL use the same full record-validation rules. The implementation SHALL expose both a compatibility boolean result and a detailed result identifying which validation check failed.
**Rationale**: Validators with subtly different semantics would inevitably drift. The boolean compatibility wrapper and additive detailed API share one implementation; header scan, per-record decode, and recovery therefore cannot disagree on validity. The detailed reason lets strict mode report the exact check without reimplementing classification in the reader.
**Verification Method**: Test (T), Inspection (I)

#### L2-SYN-015

**Parent**: L1-MODE-001
**Statement**: Lenient mode SHALL skip invalid records and continue from a recovered boundary when possible.
**Rationale**: Lenient mode is the field-deployment default. Invalid records are routine; the operator wants the maximum number of valid records extracted regardless.
**Verification Method**: Test (T)

#### L2-SYN-016

**Parent**: L1-MODE-001
**Statement**: Strict mode SHALL stop and surface an error on invalid record validation.
**Rationale**: Strict mode is used in CI and triage contexts where any invalid record is significant and must be reported, not silently elided.
**Verification Method**: Test (T)

#### L2-SYN-017

**Parent**: L1-ERR-001
**Statement**: Valid error records and SPURIOUS_DATA records SHALL remain eligible record boundaries during validation and recovery.
**Rationale**: Error records and SPURIOUS_DATA are first-class records, not failure modes. They pass validation normally and serve as recovery anchor points.
**Verification Method**: Test (T)

#### L2-SYN-018

**Parent**: L1-SYN-001
**Statement**: Header detection SHALL apply additional defenses against homogeneous-payload inputs. When the first N candidate records (with N >= 4) share identical bytes in payload positions (i.e., excluding positions where the timestamp word naturally varies), the implementation SHALL reject the input with a distinct error class.
**Rationale**: A pathological file padded with a single byte value (such as 0x20-fill) parses with a plausible Type Word (`0x20 0x20` is a valid SPURIOUS_DATA Type Word) and passes the two-record look-ahead. The homogeneity check defends against this class of input where every other check would admit it.
**Verification Method**: Test (T)

#### L2-SYN-019

**Parent**: L1-SYN-001
**Statement**: When the IRIG freerun flag (bit 15 of the upper timestamp word) is set, the day-of-year range constraint of L2-SYN-004 SHALL NOT apply. Hour, minute, second, and microsecond constraints continue to apply.
**Rationale**: The card's free-running oscillator is not calendar-locked, so day-of-year carries no calendar meaning when freerun is set. Applying the day-of-year range would falsely reject valid free-run recordings.
**Verification Method**: Test (T)

#### L2-SYN-020

**Parent**: L1-SYN-001
**Statement**: Records with Type Word message type `0x02` (BC→RT) SHALL have a Command Word with `direction = Receive`. Strict mode SHALL surface a record error; lenient mode SHALL log a WARN and skip the record (advance to the next record boundary without emission).
**Rationale**: BC→RT transactions are by definition receive operations at the RT. A transmit-direction Command Word on a `0x02` record is internally inconsistent and indicates corruption. Skipping such records in lenient mode (rather than emitting them) prevents corrupt records from propagating into downstream analysis. (Also derives from L1-MODE-001.)
**Verification Method**: Test (T)

#### L2-SYN-021

**Parent**: L1-SYN-001
**Statement**: Records with Type Word message type `0x04` (RT→BC) SHALL have a Command Word with `direction = Transmit`. Strict mode SHALL surface a record error; lenient mode SHALL log a WARN and skip the record.
**Rationale**: Counterpart to L2-SYN-020 in the opposite direction. (Also derives from L1-MODE-001.)
**Verification Method**: Test (T)

#### L2-SYN-022

**Parent**: L1-SYN-001
**Statement**: Type Word `word_count` SHALL be at least `1 (TypeWord) + ts_words + 1 (CommandWord) + payload_words(format, Cmd.data_word_count)`, where `payload_words` is the per-format declared payload size (e.g., `data_word_count + 1` for `Receive` and `Transmit`, `1` for `ModeCodeNoData`). A record whose Type Word declares a smaller capacity than the Command Word's declared payload is internally inconsistent. Strict mode SHALL surface a record error; lenient mode SHALL log a WARN and skip the record.
**Rationale**: This invariant catches records where the Type Word was corrupted to declare a smaller extent than the Command Word's `data_word_count` would require — a class of corruption that would otherwise be silently truncated. (Also derives from L1-MODE-001.)
**Verification Method**: Test (T)

#### L2-SYN-023

**Parent**: L1-SYN-001
**Statement**: For RT-to-RT (`0x08`) and Broadcast RT-to-RT (`0x18`) records, the second Command Word's `direction` field SHALL be `Receive`. Strict mode SHALL surface a record error; lenient mode SHALL log a WARN and skip the record.
**Rationale**: In an RT-to-RT transaction, the first Command Word targets the transmitting RT (direction = Transmit) and the second targets the receiving RT (direction = Receive). A second Command Word with direction = Transmit is internally inconsistent. (Also derives from L1-MODE-001.)
**Verification Method**: Test (T)

#### L2-SYN-024

**Parent**: L1-ERR-001
**Statement**: When a record carries a Status Word, the implementation SHOULD verify that `Status.rt == Cmd.rt`. On mismatch, the implementation SHALL log a WARN naming the offset, both RTs, and the raw Status Word, and SHALL continue emitting the record in both strict and lenient mode.
**Rationale**: This is an anomaly-class observation (Severity::AnomalyWarn) rather than a corruption rejection because real-bus RT response interference on a multi-drop bus can produce a status word from a different RT than the command targeted; rejecting on this case would produce false negatives on real recordings.
**Verification Method**: Test (T)

#### L2-SYN-025

**Parent**: L1-SYN-001
**Statement**: Type Word bit 15 is reserved. When a record's Type Word has bit 15 set, the implementation SHALL log a WARN naming the offset and the raw Type Word, and SHALL continue emitting the record in both strict and lenient mode.
**Rationale**: `docs/MIE-FORMAT.md` lists bit 15 as "Reserved for future use. Should be 0." Treating a set bit as corruption would prevent decoding any recording that uses an undocumented vendor extension; treating it as a silent no-op would hide a real signal from the operator. The WARN-and-emit compromise gives the operator visibility without breaking decode.
**Verification Method**: Test (T)

#### L2-SYN-026

**Parent**: L1-SYN-001
**Statement**: The look-ahead depth `N` referenced by L2-SYN-005 SHALL be configurable via the `decode.lookahead_records` TOML key or the `--lookahead-records` CLI flag, with valid range `[1, 32]` and default `2`. Values outside the range SHALL be rejected at config-load time or CLI parse time with a clear error naming the offending value and the valid range. The configured `N` SHALL apply uniformly to every sync-validation call site: header detection (`find_first_record`), mid-iteration per-record validation, and sync-recovery scan (`recover_sync`).
**Rationale**: A small number of operators encounter recordings where two consecutive corrupt frames happen to align on plausible-looking Type Words and defeat the default two-record look-ahead. Letting them increase `N` to (say) `4` or `8` catches a wider failure class without changing behavior for the common case. The `[1, 32]` range matches the equivalent range used by L2-DEC-015's `decode.detect_records` for consistency; values above `32` add little benefit (the look-ahead walk is bounded by the file's actual record count anyway).

**On the default staying `2`.** This statement read "default `8`" from the commit that raised it until v2.15.0, while every implementation shipped `2` — a normative requirement contradicting the code, and contradicting the L2-CFG schema table in this same document (which has always said `2`). The raise was reverted in the same area of work that introduced it: once continuous per-record validation stopped performing look-ahead (see L2-SYN-005), the depth no longer cost `N-1` valid records per corruption site, and the argument for raising it lost its counterweight. Raising `N` still screens out non-MIE input — every Markdown file in this repository decodes "successfully" at depth `2`, and depth `8` rejects all but one — but that is now an **operator choice** via `--lookahead-records`, not a default. The gap that let the contradiction persist was that no implementation had a test pinning the default *value*; all three now do.
**Verification Method**: Test (T)

#### L2-SYN-027

**Parent**: L1-SYN-001
**Statement**: For RT-to-RT (`0x08`) and Broadcast RT-to-RT (`0x18`) records, the first and second Command Words SHALL agree on `data_word_count`. A record whose two Command Words declare different counts is internally inconsistent. Strict mode SHALL surface a record error; lenient mode SHALL log a WARN and skip the record. This is a post-extract check (the second Command Word lives inside the payload), evaluated only after the record-bounded payload extraction of L2-DEC-009 has completed.
**Rationale**: An RT-to-RT transaction carries a single data-word count for the transfer; `docs/MIE-FORMAT.md` §6.3 specifies that both Command Words encode it and they must agree. The capacity invariant (L2-SYN-022) only sees the first Command Word, so a second Command Word that declares a different (often larger) count is not caught pre-extraction — including the over-claim that L2-DEC-009's record-bounded reads defend against at the byte level. Rejecting the mismatch turns "silently emit a record with truncated data" into an explicit corruption signal, consistent with the sibling post-extract check L2-SYN-023. (Also derives from L1-MODE-001.)
**Verification Method**: Test (T)

#### L2-SYN-028

**Parent**: L1-SYN-001
**Statement**: The N-record look-ahead of L2-SYN-005 SHALL treat a null Type Word (`0x0000`, the DDC end-of-records terminator) at a candidate's next-record boundary as a graceful end of stream — equivalent to EOF — thereby confirming the candidate rather than rejecting it, **but only on the trusted-boundary validation path** (forward per-record decode and first-record detection, `find_first_record`). Sync recovery (`recover_sync`) SHALL NOT honor the terminator: it SHALL require a real follower record (or EOF) so that a mis-aligned candidate whose declared length happens to land its boundary on a stray zero *data* word cannot validate as a bogus "last record". A single record followed by the terminator SHALL validate; the last record of a multi-record stream (which the recorder always caps with the terminator) SHALL validate and be emitted.
**Rationale**: Every well-formed DDC recording ends `…record, 0x0000`. Without terminator awareness the final record's look-ahead follower is the terminator, which fails the "next record must look like a record" heuristic (L2-SYN-005), so that record fails validation and is silently dropped — a correctness defect on *every* healthy file, plus the total failure of single-record files. Honoring the terminator fixes both. The recovery carve-out is essential: `recover_sync` probes arbitrary un-aligned offsets, and zeros are ubiquitous inside data words, so a blanket relaxation would let recovery accept mis-aligned junk (observed: a mis-framed `SPURIOUS_DATA` candidate whose boundary fell on a zero data word). Restricting the relaxation to trusted boundaries — where the candidate's boundary is reached by walking previously-validated records, or is offset 0 of a header-less file — keeps recovery false-positive-free while fixing the drop. The two public validators keep their signatures; the boundary/recovery split is an internal `honor_terminator` flag.
**Verification Method**: Test (T)

### Invariant severity classes (applies to L2-SYN-020 through L2-SYN-025, L2-SYN-027)

- **Severity::Reject** — Strict mode aborts with a record error class (e.g., `MieError::PayloadError`). Lenient mode logs a WARN and skips the record (advances past it without emission). Applies to L2-SYN-020 through L2-SYN-023 and L2-SYN-027.
- **Severity::AnomalyWarn** — Both strict and lenient modes log a WARN and continue emitting the record. Used when the bus-protocol or vendor-spec ambiguity makes outright rejection unsafe (real-bus noise, undocumented extensions). Applies to L2-SYN-024 and L2-SYN-025.

---

## L2-RDR: Reader behavior

#### L2-RDR-002

**Parent**: L1-DEC-005
**Statement**: Lenient mode SHALL stop cleanly at a truncated final record.
**Rationale**: A truncated tail is the most common form of recording-card termination (operator stop, power loss, disk full). Lenient mode treats it as end-of-stream, emits all preceding valid records, and exits cleanly.
**Verification Method**: Test (T)

#### L2-RDR-003

**Parent**: L1-MODE-001
**Statement**: Strict mode SHALL surface a truncation error when a readable Type Word declares a record extent beyond end-of-file.
**Rationale**: Counterpart to L2-RDR-002 in strict mode. In strict contexts, the operator wants the truncated tail surfaced rather than silently treated as a clean end-of-stream.
**Verification Method**: Test (T)

#### L2-RDR-004

**Parent**: L1-MODE-001
**Statement**: Header detection followed by a first-record truncation (the first valid Type Word's declared extent runs past EOF) SHALL surface a distinct error class in strict mode (e.g., `MieError::FirstRecordTruncated`). In lenient mode it SHALL emit zero records and SHALL terminate with the no-valid-records class (exit `2` per L1-EXIT-002), logging a WARN that names the truncation specifically rather than the generic "no records" wording.
**Rationale**: This is the post-header counterpart to L2-RDR-002/003. The strict-mode error class stays distinct because "the header parsed but the first record is truncated" is operationally different from "no records at all". Lenient mode previously terminated *cleanly* — exit `0` with a header-only CSV — which was indistinguishable from a successful decode and, worse, was a route by which non-MIE inputs reported success (v2.12.0). A decode that produces no rows from a file that is not a valid empty recording is a failure; the WARN preserves the specific diagnosis while the exit code stays honest.
**Verification Method**: Test (T)

#### L2-RDR-005

**Parent**: L1-EXIT-001
**Statement**: Opening a missing input file SHALL surface a file-not-found error.
**Rationale**: Distinct from format errors and validation errors; usually means the operator typed the path wrong.
**Verification Method**: Test (T)

#### L2-RDR-006

**Parent**: L1-EXIT-001
**Statement**: Opening an empty input file SHALL surface an empty-file error.
**Rationale**: Distinct from "no valid records found" (which implies the file had content but none of it parsed). An empty input file is usually an upstream pipeline failure that the operator can investigate directly.
**Verification Method**: Test (T)

#### L2-RDR-007

**Parent**: L1-DEC-003
**Statement**: Receive records SHALL extract Data Words before Status Word.
**Rationale**: The on-bus ordering of a Receive transaction is Cmd → Data... → Status. The CSV preserves this ordering so the row reads as the bus saw it.
**Verification Method**: Test (T)

#### L2-RDR-008

**Parent**: L1-DEC-003
**Statement**: Transmit records SHALL extract Status Word before Data Words.
**Rationale**: The on-bus ordering of a Transmit transaction is Cmd → Status → Data.... Counterpart to L2-RDR-007.
**Verification Method**: Test (T)

#### L2-RDR-009

**Parent**: L1-DLT-001
**Statement**: `DELTA` SHALL be calculated against the most recent prior message sharing the same RT and MSG identifier.
**Rationale**: The analyst-meaningful inter-arrival time is between transactions on the same RT/subaddress pair; aggregating across different subaddresses would conflate two independent traffic patterns.
**Verification Method**: Test (T)

#### L2-RDR-010

**Parent**: L1-DLT-001
**Statement**: The first occurrence of each RT/MSG key SHALL have `DELTA` equal to `0.000000`.
**Rationale**: A first-occurrence sentinel distinguishes "first time seen" from "previously seen". `0.000000` is the chosen sentinel because it is unambiguous when read in the CSV (no prior arrival means zero elapsed time).
**Verification Method**: Test (T)

#### L2-RDR-015

**Parent**: L1-SYN-001
**Statement**: Every record SHALL pass the full shared validation path before decoding.
**Rationale**: Same uniformity argument as L2-SYN-014 from the reader's perspective. Bypassing validation for any record class would create a class-specific drift surface.
**Verification Method**: Inspection (I), Test (T)

#### L2-RDR-016

**Parent**: L1-DLT-001
**Statement**: Errored records (Type Word bit 14 set) SHALL participate in `DELTA` tracking — they update the per-RT/MSG cursor and SHALL receive a `DELTA` computed against the prior message sharing the same key.
**Rationale**: An errored record still represents a bus transaction that took bus time, even if the data is unusable. Excluding it from DELTA would falsely widen the gap to the next valid record on the same key.
**Verification Method**: Test (T)

#### L2-RDR-017

**Parent**: L1-DLT-001
**Statement**: When a record's timestamp is older than the prior message for the same RT/MSG key, `DELTA` SHALL be empty and the implementation SHALL log a WARN. The WARN SHALL be emitted at most once per RT/MSG key per decoded file to avoid log flooding.
**Rationale**: A timestamp regression on the same key is a corruption signal that the operator should see. Per-key de-duplication keeps the log usable when a recording has hundreds of regressions on one key.
**Verification Method**: Test (T)

#### L2-RDR-018

**Parent**: L1-DLT-001
**Statement**: SPURIOUS_DATA records have no RT/MSG key and SHALL have an empty `DELTA`; they SHALL NOT update any per-key cursor.
**Rationale**: SPURIOUS_DATA is by definition a fragment without a Command Word, so it has no RT or subaddress to key on. Updating any cursor with it would corrupt the key state for unrelated transactions.
**Verification Method**: Test (T)

#### L2-RDR-019

**Parent**: L1-DLT-001
**Statement**: Standard-format timestamps have no tick rate encoded in the file. When no Standard tick rate is configured, records carrying a Standard timestamp SHALL have an empty `DELTA` and SHALL NOT participate in per-key tracking. When a valid Standard tick rate is configured (per L2-DEC-017), Standard timestamps SHALL be converted to microseconds and SHALL participate in per-key `DELTA` tracking on the same terms as IRIG timestamps (L2-RDR-016 through L2-RDR-018), including the first-occurrence `0.0` rule and the non-monotonic empty-`DELTA`-with-WARN rule.
**Rationale**: A numeric DELTA computed from raw 32-bit counter ticks in unknown units would be misleading, so the truthful default per L1-DLT-001 is emptiness. Once an operator supplies the card's counter frequency out-of-band, the ticks acquire a real microsecond basis and there is no longer any reason to withhold `DELTA` — the conversion is well-defined (L2-DEC-017) and the existing tracking rules apply unchanged.
**Verification Method**: Test (T)

#### L2-RDR-020

**Parent**: L1-EXIT-006
**Statement**: Both implementations SHALL open the input file with read-only access semantics. Writable, copy-on-write, or shared-write memory-mapping modes SHALL NOT be used. The specific access mode and API is pinned by L3-PY-009 (Python) and L3-RS-003 (Rust).
**Rationale**: L1-EXIT-006 is the operational contract that the decoder never modifies the input file. Read-only mmap is the implementation-level enforcement of that contract — any other mode would create a code path through which the input could be mutated, undermining the contract regardless of operator intent.
**Verification Method**: Inspection (I)

#### L2-RDR-021

**Parent**: L1-EXIT-010
**Statement**: The reader SHALL recognize the DDC end-of-records terminator (a null Type Word, `0x0000`) as a clean end of the record stream. Reaching the terminator at a record boundary during forward decode SHALL end iteration normally (no sync loss, no error), after the preceding real record has been emitted (per L2-SYN-028). When `find_first_record` locates no valid record **and** the file opens on the terminator (the word at offset 0 is `0x0000`), the reader SHALL classify the input as a valid but *empty recording*: it SHALL yield zero records with no error, expose an `empty_recording` indicator (`MieFileReader::empty_recording()` in Rust; the `empty_recording` property in Python), and log a WARN naming the empty capture. An input that opens on any non-terminator word with no valid record SHALL retain the existing L2-RDR-004 / L2-SYN-011 diagnosis (`FirstRecordTruncated` or `NoValidRecords`), so genuinely non-MIE inputs are not misclassified as empty.
**Rationale**: DDC files carry no parsed header — records begin at byte 0 and the stream is capped with the `0x0000` terminator; an empty recording (e.g. an unused channel) is literally that two-byte terminator. Teaching the reader the terminator lets it (a) end normal decodes cleanly at the true end of data instead of via a sync-loss→truncation fallback, and (b) distinguish a legitimate empty recording from a wrong-file input by the positive terminator signature, which is what makes the L1-EXIT-010 exit-`0`/header-only-CSV outcome safe. The `empty_recording` indicator is the mechanism by which the CLI reports the distinct exit class (mirroring how `sync_losses` surfaces the partial-recovered class).
**Verification Method**: Test (T)

---

## L2-MSG: Message semantics

#### L2-MSG-001

**Parent**: L1-DEC-003
**Statement**: The decoder SHALL classify all 10 supported MIL-STD-1553 transaction formats plus SPURIOUS_DATA. The supported transaction formats are: (1) BC→RT Receive, (2) RT→BC Transmit, (3) RT-to-RT, (4) Receive Broadcast (BC→RT broadcast), (5) RT-to-RT Broadcast, (6) Mode Code Transmit with data, (7) Mode Code Receive with data, (8) Mode Code with no data, (9) Mode Code Broadcast with no data, (10) Mode Code Broadcast with data. SPURIOUS_DATA is the 11th classification and represents records lacking a Command Word.
**Rationale**: Enumeration prevents accidental omissions and makes the classification space testable. Each format has a distinct payload extraction shape (L2-RDR-007/008 and the mode-code variants).
**Verification Method**: Test (T)

#### L2-MSG-002

**Parent**: L1-DEC-004
**Statement**: Bus SHALL be represented as `A` or `B` in CSV output.
**Rationale**: Single-character A/B is the DDC vendor CSV convention. Both implementations preserve it for column compatibility.
**Verification Method**: Test (T)

#### L2-MSG-003

**Parent**: L1-DEC-003
**Statement**: A decoded message SHALL expose an MSG label in `<subaddress><T|R>` form when a Command Word is present.
**Rationale**: The `<subaddress><T|R>` form is the DDC vendor CSV convention and is used as the secondary key for DELTA tracking. SPURIOUS_DATA has no Command Word and therefore has no MSG label.
**Verification Method**: Test (T)

#### L2-MSG-004

**Parent**: L1-DEC-003
**Statement**: When sub-classifying a Mode Command (L2-MSG-001 formats 6–10), the data-vs-no-data decision SHALL be made relative to the record's **timestamp word count** (IRIG = 3 words, Standard = 2 words), not against absolute word-count thresholds. Specifically, a broadcast mode code (RT 31) carries data iff `word_count ≥ timestamp_words + 3` (otherwise no-data), and a non-broadcast receive mode code carries data iff `word_count ≥ timestamp_words + 4` (otherwise no-data). A non-broadcast **transmit** mode code likewise carries data iff `word_count ≥ timestamp_words + 4`; a transmit mode code with **no** data word (e.g. MIL-STD-1553 mode codes 0–15, such as "transmit status word") SHALL be classified `MODE_CODE_NO_DATA` — identical to the receive no-data case, since the wire shape is `ModeCmd + Status` either way and the `CMD` column preserves the direction — and SHALL NOT be forced to `MODE_CODE_TX_DATA` (which expects a data word, fails the L2-SYN-022 capacity check, and silently drops the record in lenient mode). Classification SHALL therefore be correct for both directions under both timestamp formats.
**Rationale**: A Standard timestamp occupies one fewer word than IRIG, so every mode-code shape's total word count is one smaller under Standard. Fixed IRIG-sized thresholds misclassified Standard mode-code-with-data records (broadcast at `word_count = 5`, receive at `word_count = 6`) as no-data, emitting the data word in the Status position. Deriving the threshold from the resolved timestamp word count makes the classifier correct for both formats while leaving IRIG output byte-identical.
**Verification Method**: Test (T)

---

## L2-ERR: Error record handling

#### L2-ERR-001

**Parent**: L1-ERR-001
**Statement**: Type Word bit 14 SHALL identify an errored record.
**Rationale**: Bit 14 is the DDC card's "this record encountered a bus error" indicator. Both implementations key error-record routing off this bit.
**Verification Method**: Test (T)

#### L2-ERR-002

**Parent**: L1-ERR-001
**Statement**: The final word of an errored record SHALL be decoded as its DDC Error Word.
**Rationale**: When bit 14 is set, the card truncates the payload and appends an Error Word in the last 16-bit slot. The decoder extracts this word as the error class.
**Verification Method**: Test (T)

#### L2-ERR-003

**Parent**: L1-ERR-001
**Statement**: Known DDC Error Word values SHALL be recognized.
**Rationale**: The known set is the `0x01xx` family documented in `docs/MIE-FORMAT.md`. Unknown values are surfaced as `UNKNOWN` in the CSV in lenient mode; strict mode rejects them (L2-ERR-004).
**Verification Method**: Test (T)

#### L2-ERR-004

**Parent**: L1-MODE-001
**Statement**: Strict mode SHALL reject unknown DDC Error Word values.
**Rationale**: An unrecognized error code indicates either a corrupt record or an undocumented card behavior. In strict mode the operator wants this surfaced rather than silently passed through as `UNKNOWN`.
**Verification Method**: Test (T)

#### L2-ERR-005

**Parent**: L1-ERR-001
**Statement**: SPURIOUS_DATA records immediately following an errored record SHALL use decoder code `0x2000`. "Immediately following" refers to the immediately preceding *successfully decoded* record, not the immediately preceding error record. A classification failure or unrecoverable validation error between an error record and a SPURIOUS_DATA record SHALL reset the continuation flag — the corruption itself is treated as a boundary, and the SPURIOUS_DATA SHALL fall through to L2-ERR-006 (standalone, `0x2001`).
**Rationale**: The continuation flag is what distinguishes "leftover data from a truncated errored transaction" from "an unrelated SPURIOUS_DATA fragment". Resetting on a corruption boundary prevents stale state from misclassifying a fragment that is no longer continuous with the prior error.
**Verification Method**: Test (T)

#### L2-ERR-006

**Parent**: L1-ERR-001
**Statement**: Standalone SPURIOUS_DATA records SHALL use decoder code `0x2001`.
**Rationale**: Distinct code from `0x2000` so the analyst can tell continuation fragments from genuinely orphan ones.
**Verification Method**: Test (T)

#### L2-ERR-007

**Parent**: L1-OUT-001
**Statement**: CSV output SHALL include `ERROR` and `ERROR_CODE` columns.
**Rationale**: These columns are part of the DDC vendor CSV layout. They are populated in inline mode and empty in clean rows of the main file in separate mode.
**Verification Method**: Test (T)

#### L2-ERR-008

**Parent**: L1-ERR-001
**Statement**: Separate mode — opt-in via `--separate-errors` / `[decode] error_mode = "separate"` — SHALL write normal messages to the main CSV and errored or spurious messages to `<stem>_errors<suffix>`, where `<stem>` is the destination filename up to and excluding the final `.`, and `<suffix>` is the final `.` and extension (or empty if the destination has no extension). Examples: `out.csv` → `out_errors.csv`; `out` → `out_errors`; `data.bar.csv` → `data.bar_errors.csv`.
**Rationale**: The stem/suffix split preserves the operator's chosen extension on the errors file. The split also handles extension-less destinations cleanly.
**Verification Method**: Test (T)

#### L2-ERR-010

**Parent**: L1-OUT-001
**Statement**: CSV `ERROR` SHALL be empty, `ERROR`, or `SPURIOUS` as appropriate; `ERROR_CODE` SHALL contain the corresponding uppercase hexadecimal code.
**Rationale**: Empty / `ERROR` / `SPURIOUS` is the DDC vendor convention; the hex code follows the same `0x` prefix policy as other 16-bit values in the CSV (see L2-WRT-003).
**Verification Method**: Test (T)

#### L2-ERR-011

**Parent**: L1-ERR-001
**Statement**: Inline mode SHALL write normal, errored, and spurious messages to one CSV, and SHALL be the **default** error mode. Separate-file output is opt-in via `--separate-errors` / `[decode] error_mode = "separate"`.
**Rationale**: Inline mode produces a single output for byte-exact diff against the DDC vendor CSV, which is the layout the vendor tool itself emits; separate-mode output by definition has no vendor-CSV counterpart. Defaulting to inline means a decode is directly comparable to vendor output with no flags, and no errored record is silently absent from the file the operator opened. Stdout cannot be split, so it is inline regardless.
**Verification Method**: Test (T)

---

## L2-WRT: CSV output and output destination integrity

#### L2-WRT-001

**Parent**: L1-OUT-001
**Statement**: CSV columns SHALL appear in this order: `TIME_STAMP`, `RT`, `MSG`, `WD01`-`WD32`, `STAT`, `CMD`, `MUX`, `TERM_NAME`, `BUS`, `DELTA`, `IM_GAP`, `RCV_GAP`, `XMT_GAP`, `ERROR`, `ERROR_CODE`. The first **44** columns (`TIME_STAMP` through `XMT_GAP`) are the DDC vendor layout and SHALL appear in exactly that order and no other. `ERROR` and `ERROR_CODE` are decoder-added columns with no vendor counterpart and SHALL be **appended after** the vendor block, never interleaved within it.
**Rationale**: Column order for the vendor block is dictated by the DDC vendor CSV; reordering or "cleaning up" the empty vendor columns would break the column compatibility contract. `ERROR` / `ERROR_CODE` are not vendor columns — the vendor tool emits 44 columns and does not report bus errors as CSV fields at all (they are a decoder feature, L2-ERR-002). Through v2.9.0 they were placed *inside* the vendor block, between `DELTA` and `IM_GAP`, which silently shifted `IM_GAP` / `RCV_GAP` / `XMT_GAP` two positions right of their vendor indices and made a positional (column-index) comparison against vendor output wrong for every column after `DELTA`. Appending them restores index alignment for the whole vendor block, so column *N* of a decoded CSV is column *N* of the vendor CSV for all 44, and anything the decoder adds is strictly additive at the tail — the position new columns should take in future too.
**Verification Method**: Test (T)

#### L2-WRT-002

**Parent**: L1-OUT-001
**Statement**: Unused Data Word columns and unavailable fields SHALL be empty.
**Rationale**: Empty cells are the DDC vendor CSV convention for "no value here"; emitting `0000` would falsely indicate a zero word was on the bus.
**Verification Method**: Test (T)

#### L2-WRT-003

**Parent**: L1-OUT-001
**Statement**: Data Words, Status Word, Command Word, and Error Word SHALL use 4-character uppercase hexadecimal without a `0x` prefix.
**Rationale**: This is the DDC vendor CSV convention. Width 4 zero-pads narrow values and uppercase matches the vendor casing.
**Verification Method**: Test (T)

#### L2-WRT-004

**Parent**: L1-OUT-001
**Statement**: `DELTA` SHALL use exactly six decimal places when populated, and SHALL be an empty CSV cell when no `DELTA` is computed (see L2-RDR-016 through L2-RDR-019).
**Rationale**: Six decimal places is microsecond precision in seconds — matching the IRIG timestamp basis and the DDC vendor CSV convention. Empty cells communicate "no DELTA available" without falsifying a number.
**Verification Method**: Test (T)

#### L2-WRT-007

**Parent**: L1-OUT-001
**Statement**: CSV output SHALL support a file destination and stdout.
**Rationale**: File output is the normal case; stdout is for pipeline integration where the next stage consumes the CSV directly.
**Verification Method**: Test (T)

#### L2-WRT-011

**Parent**: L1-OUT-001
**Statement**: Under the default day-of-year rendering (`doy`, L2-WRT-025), IRIG timestamp text SHALL use `DAY:HH:MM:SS.uuuuuu` formatting. This is the rendering against which L1-OUT-001 vendor compatibility is defined; the alternative renderings of L2-WRT-025 are opt-in and depart from it deliberately.
**Rationale**: This is the DDC vendor convention. Zero-padded fields keep column alignment under monospace rendering. The scoping clause was added when rendering became operator-selectable (L1-OUT-004): this requirement continues to state what the *default* emits, so that "vendor-compatible" remains a property of an invocation that passes no rendering flag at all.
**Verification Method**: Test (T)

#### L2-WRT-012

**Parent**: L1-OUT-001
**Statement**: CSV output SHALL use LF (`\n`) line endings on every supported platform.
**Rationale**: LF-only line endings make CSV byte-exact diff work across Windows and Linux. CRLF would break the diff and confuse downstream consumers on Linux.
**Verification Method**: Test (T)

#### L2-WRT-013

**Parent**: L1-OUT-001
**Statement**: CSV output SHALL preserve the vendor compatibility columns `TERM_NAME`, `IM_GAP`, `RCV_GAP`, and `XMT_GAP` as empty, and SHALL preserve the `MUX` column in its vendor layout position. `MUX` is populated from the input file name per L2-WRT-020 (and is empty when that population is disabled or yields no value); the other four remain empty.
**Rationale**: These columns are part of the vendor layout and are preserved for column-order fidelity. `MUX` is the first of them to carry decoder-derived content (L2-WRT-020); the rest stay empty until a future version defines a meaning for them.
**Verification Method**: Test (T)

#### L2-WRT-014

**Parent**: L1-OUT-002
**Statement**: **No path a decode run could commit** SHALL resolve to the same canonical path as **any** of its inputs. The set of commit targets SHALL be enumerated from the destination and the run's mode, and comprises:

1. the destination itself;
2. the derived errors file `<stem>_errors<suffix>`, in separate-errors mode (L2-WRT-019);
3. `<destination>.partial`, under `--allow-partial` (L2-WRT-016); and
4. the errors file's own `<...>.partial`, when both of the above apply.

Every target SHALL be tested against every resolved input — the whole set on the merge path (L2-MRG-001), not just the first. Implementations SHALL surface a distinct error class (e.g., `MieError::InputOutputCollision` / `MieOutputPathError`) **before opening any output**, and SHALL NOT create any output file on rejection. Targets 3 and 4 SHALL be checked even though a clean decode never writes them. Stdout output is exempt because it has no filesystem identity.

The enumeration SHALL be the same code the writer commits through, not a second spelling of the same rule.

**Rationale**: Decoding a file onto itself would truncate the input mid-decode and produce undefined behavior under mmap. Catching this before the output is opened is the only safe guard.

Through v2.16.0 this requirement said "the output path ... the input file", singular on both sides, and all three implementations implemented exactly that — each carrying a comment reasoning that the derived errors path needed no check because it "is derived from output, which was already checked". That does not follow. A derived path is an ordinary path that can name an ordinary file, and being derived from a *safe* path says nothing about whether it collides with a *different* input: `-o capture.mie --separate-errors` derives `capture_errors.mie`, a perfectly plausible name for one of the recordings being decoded. Both derived families were destructive in practice — the errors file and the `.partial` file each committed straight over an input, and **the run exited 0** while doing it. On POSIX the damage is silent: the reader's mapping survives on the old inode, so the decode completes normally against bytes no longer on disk.

`.partial` targets are checked up front, rather than at the moment a sync loss makes one real, because "before opening the output" is the only point at which refusing is still safe — and by then nobody knows whether the decode will lose sync. Refusing a run that *might* have destroyed an input is the conservative direction, and the cost is a rejected invocation the operator can fix by renaming.

The single-enumeration rule exists because the two halves are owned by different layers: the writer owns the derivations (it is what renames onto them), while only the CLI knows the full input set. A CLI that re-derived `_errors` and `.partial` itself would agree with the writer exactly until someone changed a suffix.

**Verification Method**: Test (T)

#### L2-WRT-015

**Parent**: L1-OUT-002
**Statement**: File output SHALL be written via a temporary file in the destination's directory, then renamed atomically over the destination on successful completion. The temp file SHALL live on the same filesystem as the destination so the rename is atomic.
**Rationale**: Atomicity guarantees that a downstream consumer never observes a half-written CSV. Same-filesystem placement is required because cross-filesystem rename is a copy-then-unlink and is not atomic.
**Verification Method**: Test (T)

#### L2-WRT-016

**Parent**: L1-EXIT-004
**Statement**: On a decode failure that triggers the default `partial-unrecoverable` exit class (L1-EXIT-004), the temp file SHALL be unlinked before the process exits. When `--allow-partial` is in effect, the temp file SHALL instead be renamed to `<destination>.partial` so the operator can inspect it; in that case the original `<destination>` SHALL remain untouched.
**Rationale**: Unlinking by default prevents the operator from being handed a partial result they might mistake for a complete one. `--allow-partial` is the explicit opt-in for operators doing forensics on a known-bad recording.
**Verification Method**: Test (T)

#### L2-WRT-017

**Parent**: L1-OUT-002
**Statement**: Overwrite of an existing destination SHALL succeed by default. An optional `--no-clobber` CLI flag (and equivalent `output.no_clobber` configuration key) SHALL refuse the overwrite and surface a distinct error class. The refusal itself is specified by L2-WRT-023; an existence test performed before the output is opened MAY report the condition earlier but SHALL NOT be relied on as the guarantee.
**Rationale**: Overwrite by default matches operator expectation for batch reruns. `--no-clobber` is the explicit guard for pipelines where the operator wants to fail rather than overwrite a possibly-newer result.
**Verification Method**: Test (T)

#### L2-WRT-018

**Parent**: L1-EXIT-001
**Statement**: A broken-pipe condition on stdout output (downstream consumer closed early) SHALL exit `0` with no error. Disk-full and permission errors SHALL surface as a writer error preserving the underlying OS error message.
**Rationale**: Broken pipe on stdout is the expected termination signal in shell pipelines (`aero1553 ... | head`). Treating it as an error would falsely fail every pipeline that consumes only the first N rows. Disk-full and permission errors are genuine failures.
**Verification Method**: Test (T)

#### L2-WRT-019

**Parent**: L1-OUT-002
**Statement**: In separate error mode (`--separate-errors`; inline is the default since v2.8.0), the main CSV and the errors CSV SHALL each be committed via its own atomic temp+rename (L2-WRT-015), and the main output SHALL be committed **before** the errors output. This holds on **every** path that commits the pair, the `--allow-partial` path (L2-WRT-016) included: `<destination>.partial` SHALL be committed before `<errors-destination>.partial`. The two commits are sequential — no cross-file atomic rename exists — so this is explicitly **not** an all-or-nothing guarantee across the two files: a failure of the second (errors) commit SHALL leave the already-committed main output in place, and a failure of the first (main) commit SHALL leave neither file (the errors output is still an un-renamed temp and is unlinked). All implementations SHALL use this main-before-errors order.
**Rationale**: There is no portable way to atomically commit two files together. Since one file may survive a mid-commit failure, the residue must be the main output — the primary deliverable — never an orphan errors file with no corresponding main output. Pinning the order also removes a latent cross-implementation divergence: Rust previously committed errors-first while Python committed main-first, so the file left behind on failure differed by implementation.

Through v2.16.0 this requirement said "the main CSV ... before the errors CSV", naming the artifacts of the *clean* commit — and all three implementations read it that way, committing main-first on the normal path and errors-first on the `--allow-partial` path. The residue that produced is the exact one the rule forbids: when the errors `.partial` rename succeeded and the main `.partial` rename then failed, the operator was left holding `<dest>_errors.csv.partial` and no main output at all. That the branch which gets this wrong is the *failure* branch is not a coincidence — it is the branch nobody re-reads — so the rule is now stated over every path that commits the pair rather than over "the commit".
**Verification Method**: Test (T)

#### L2-WRT-020

**Parent**: L1-OUT-001
**Statement**: The `MUX` column SHALL be populated from a field of each record's **source file name**. The file's basename SHALL be split on a configurable `delimiter` (default `.`) and the field at a configurable 0-based `field` index (default `4`; a negative index counts from the end) SHALL be used as the MUX value, trimmed of surrounding whitespace. When the index is out of range, the selected field is empty, the delimiter is empty, or population is disabled, `MUX` SHALL be empty. Population is **enabled by default** and SHALL be disabled by `[mux] enabled = false` (TOML) or `--no-mux` (CLI), with `[mux] delimiter` / `--mux-delimiter` and `[mux] field` / `--mux-field` overriding the extraction. In multi-file merge mode each record SHALL carry the MUX value of the **file it was decoded from**. A MUX value containing the CSV delimiter, a double quote, or a line break SHALL be RFC4180-quoted identically in both implementations.
**Rationale**: Operators encode a source/recorder identity in a file-name field (e.g. `…1553.aa.unused.mie_irig`); surfacing it in the long-empty `MUX` column lets a decoded CSV identify its origin without an external lookup. Delimiter+index extraction is dependency-free (no regex), preserving the hand-rolled / single-dependency property. Default-on serves the common operator workflow; `--no-mux` restores vendor-exact output for a byte-for-byte vendor-CSV diff (see `docs/VENDOR-CSV-DIFFS.md`). Per-file carry through the merge is what makes the value meaningful when several recorders are combined, and is the first concrete step of the ROADMAP "recorder identity from a parsed file-naming convention" item.
**Verification Method**: Test (T)

#### L2-WRT-021

**Parent**: L1-OUT-003
**Statement**: Canonical row ordering SHALL be realized as a single reorder stage applied to the decoded message stream, positioned as the **final** stage before the writer — after the merge (when one runs) and after filtering — so that the ordering guarantee holds over exactly the rows that reach the CSV. The stage SHALL buffer one run of consecutive records sharing an identical `TIME_STAMP`, stable-sort that run by the key `(RT, subaddress, direction)` with `Direction::Receive` ordering before `Direction::Transmit`, and emit it before starting the next run. A record with no Command Word SHALL be excluded from the sort and re-emitted at its original offset within the run, immediately after whichever record preceded it on input. Timestamp identity SHALL compare both the timestamp **variant** (IRIG vs Standard) and its value, so records of differing variants never compare as equal. Records that are not `Ok` (a decoder error surfacing mid-stream) SHALL pass through in position, and the stage SHALL flush its buffered run **before** propagating them so an `--allow-partial` run never loses a buffered group from the committed `.partial`. The stage SHALL apply identically to the single-input and merge paths, and in separate error mode each of the two output files SHALL individually be in canonical order. The stage SHALL NOT be applied to the `dump` subcommand, whose purpose is to report raw file layout.
**Rationale**: A single stage shared by both input paths is what makes the two paths agree, and placing it last is what makes the guarantee checkable: a sort applied before filtering would still be order-preserving, but the pinning of Command-Word-less records would be anchored to records that may never be written. Sorting on `(RT, subaddress, direction)` rather than on the rendered `MSG` string avoids a lexicographic comparison in which `"11R"` sorts before `"2R"`; both implementations already encode receive as `0` and transmit as `1`, so R-before-T needs no special-casing and cannot drift apart. Comparing the timestamp variant as well as the value costs nothing and removes a latent trap: a mixed-variant stream is impossible today (format is resolved once per file and merge rejects mixed sets), so an equality test on value alone would be silently wrong if that ever changed. Flushing before an error propagates is the difference between a `.partial` that ends mid-group and one that ends on a group boundary. DELTA needs no re-computation after the stage: DELTA is per-`RT`/`MSG` key, and two records in one run that share a key also share a timestamp, so their gap is zero in either order — the reorder is DELTA-invariant by construction. Excluding `dump` keeps the diagnostic view honest about what is actually on disk.
**Verification Method**: Test (T)

#### L2-WRT-022

**Parent**: L1-OUT-003
**Statement**: The length of one buffered equal-timestamp run SHALL be bounded by the configuration key `[output] max_sort_group` (CLI `--max-sort-group`), a positive integer defaulting to `4096` and validated at load time against the range `[MAX_SORT_GROUP_MIN, MAX_SORT_GROUP_MAX]` = `[1, 1048576]`. When a run reaches the cap, the stage SHALL emit the buffered records in arrival order, SHALL emit exactly one WARN per capped run naming the timestamp and the cap, and SHALL continue decoding — it SHALL NOT buffer beyond the cap, fail, or drop records. A cap of `1` SHALL disable reordering entirely, restoring pre-L1-OUT-003 capture order.
**Rationale**: The reorder stage is one of only two parts of the pipeline whose memory is a function of the data rather than of the file count (the other is L2-MRG-008's de-duplication window), so it needs its own bound to preserve the crate's constant-memory design point and L1-ROB-001's no-unbounded-growth guarantee. The motivating input is not hypothetical: a corrupt or misconfigured recording whose timestamps all decode to one value (all-zero timestamp words being the common case) would otherwise buffer the entire file. Degrading to arrival order on overflow — rather than failing — keeps a pathological file decodable, and matches how the reader already degrades rather than aborts in lenient mode; the single WARN per run tells the operator the guarantee was suspended without flooding the log. The default of `4096` is far above any real equal-timestamp run (a 1553 bus carries one transaction at a time, so genuine ties come from the two concurrent buses or from overlapping recorders in a merge — single digits in practice) while still bounding worst-case buffering to a few hundred kilobytes, and is shared with L2-MRG-008 so an operator who has reasoned about one bound need not re-derive the other. Exposing `1` as a documented "off" value gives operators who need byte-exact DDC capture order — the vendor-CSV diff workflow of `docs/VENDOR-CSV-DIFFS.md` — a supported way to get it without a second flag.
**Verification Method**: Test (T)

#### L2-WRT-023

**Parent**: L1-OUT-002
**Statement**: When `--no-clobber` / `output.no_clobber` is in effect, **every** path a decode run actually commits SHALL be claimed by an operation that is **atomic and non-replacing** — one that either publishes the output under that name or reports that the name is taken, and that cannot destroy a file created between the decision to write and the write itself. This covers the destination, the derived errors file, and the `.partial` variant of each (the L2-WRT-014 commit-target set). On refusal the implementation SHALL surface the same `ClobberRefused` error class the pre-flight uses, naming the target that was taken, and SHALL leave that target's existing contents unmodified.

A pre-flight existence test MAY additionally be performed, and SHALL be understood as an early report rather than the guarantee. Pre-flighting SHALL be confined to the destinations a run definitely creates; `.partial` targets SHALL NOT be pre-flighted, because a stale `.partial` must not refuse a run that was never going to write one.

The default (`no_clobber` unset) is unchanged and SHALL remain a replacing rename (L2-WRT-017).

**Rationale**: `exists()` answers a question about the past. Between that answer and the rename, any other process — a second `aero1553`, a scheduled job, an operator — may create the destination, and the replacing rename then destroys it: two concurrent runs of the same command both pass the pre-flight and the second silently overwrites the first's output. That is the precise outcome `--no-clobber` exists to prevent, so the refusal has to happen at the moment of the write, where the filesystem can make it atomic. The `.partial` targets were worse than racy — they were never checked at all, so `--no-clobber --allow-partial` overwrote an existing `.partial` unconditionally.

No portable single call does this. `rename(2)` replaces, and so does Windows' `MoveFileEx` with `MOVEFILE_REPLACE_EXISTING`; `renameat2(RENAME_NOREPLACE)` needs a kernel and glibc newer than the SLES 12 floor. The requirement is therefore stated over the *property* — atomic and non-replacing — and not over a syscall, so each implementation can reach it the way its platform allows: `MoveFileExW` with no flags is natively atomic-no-replace on Windows, while POSIX gets there with `link(2)` (which fails `EEXIST`) plus an exclusive-create reservation as the fallback for filesystems without hard links.

**Verification Method**: Test (T)

#### L2-WRT-024

**Parent**: L1-OUT-002
**Statement**: A file-output commit comprises the final flush, the close of the temp file, and the move onto the destination. A failure of **any** of the three SHALL surface as the implementation's writer-error class (`MieError::WriterError` / `MieWriterError`) preserving the underlying OS error, SHALL NOT modify the destination, and SHALL leave no temp file behind. Cleanup performed on the failure path SHALL NOT be capable of masking the failure or of skipping its own unlink: a close that raises during cleanup SHALL be swallowed there, because the commit has already reported the real error.

**Rationale**: The final flush is the *most likely* place for a disk-full error to appear, not an unlikely one — it is where the last buffered rows actually reach the filesystem, and every earlier write may have returned having only filled a buffer. Leaving it outside the commit's error handling is therefore not a corner case; it is a hole under the common failure. The Python writer had exactly that shape: `commit()` closed the stream before its `try`, so a full disk surfaced as a raw `OSError` from a method documented to raise `MieWriterError`, and the CLI reported an unexpected crash rather than a write failure. The follow-on was worse — the context manager's `close()` re-raised while flushing the same unwritten buffer, which skipped the unlink and left the temp file on the disk that had just filled up. Rust and C++ already classified all three stages, so this requirement pins behaviour two implementations had and one did not, which is the kind of gap a cross-implementation contract exists to catch.

**Verification Method**: Test (T)

#### L2-WRT-025

**Parent**: L1-OUT-004
**Statement**: The `TIME_STAMP` rendering SHALL be selectable from exactly three values, resolved per L2-CFG-003 from `--output-time-format` / `[output] output_time_format` and defaulting to `doy`:

- **`doy`** — IRIG renders `DAY:HH:MM:SS.uuuuuu` per L2-WRT-011; Standard renders `0x` followed by eight uppercase hex digits. This is the default, and its bytes SHALL be identical to those emitted before this requirement existed.
- **`iso`** — IRIG renders `YYYY-MM-DDTHH:MM:SS.uuuuuu` followed by a zone designator, where `YYYY` is the resolved year (L2-WRT-026), `MM-DD` is the calendar date of that year's day-of-year, and the designator is `Z` when the resolved UTC offset is zero and `+HH:MM` / `-HH:MM` otherwise.
- **`dom`** — IRIG renders `DD:HH:MM:SS.uuuuuu`, where `DD` is the day of month of the same resolved calendar date, zero-padded to two digits. The month is deliberately not represented in the cell.

All three SHALL emit exactly six microsecond digits; L2-DEC-014 applies unchanged and is not relaxed by the wider cell. Day-of-year resolution SHALL use proleptic Gregorian leap rules — a year is a leap year when divisible by 4, except centuries, which must be divisible by 400. The rendering SHALL NOT participate in ordering: the canonical row order of L1-OUT-003 and the merge heap key of L2-MRG-004 SHALL continue to be computed from absolute microseconds and never from rendered text.

**Rationale**: Three renderings rather than an operator-supplied format string, because every one of the three has different *preconditions* — `doy` needs nothing, `iso` and `dom` need a year, `iso` additionally carries a zone claim — and a free-form template would let an operator write a specifier the recording cannot satisfy with no place to say so. `doy` is the default because L1-OUT-001 byte compatibility has to be a property of the plain invocation. `dom` keeps the field count and punctuation of `doy`, so an operator reading a familiar layout gets a day number that matches a wall calendar without the column changing shape; the deliberate cost, recorded here so it is not later mistaken for an oversight, is that the cell alone does not identify a date — day `10` of an unstated month — and the month must come from the campaign context. The explicit leap-year rule exists because the same day-of-year renders one day apart across leap and common years, which is the defect this requirement's test matrix is built around. The ordering clause is stated because the failure it forbids is easy to reach by accident: sorting the rendered string would order `10:...` before `192:...` and silently reshuffle a file the moment the rendering changed.
**Verification Method**: Test (T)

#### L2-WRT-026

**Parent**: L1-OUT-004
**Statement**: The calendar renderings of L2-WRT-025 (`iso`, `dom`) carry preconditions, and each unmet precondition SHALL be refused rather than approximated:

1. **A year is required.** It is resolved per L2-CFG-003 from `--year` / `[output] year` and SHALL be an integer in `[1, 9999]`. When a calendar rendering is selected and neither source supplies a year, the implementation SHALL reject the invocation as a usage error (exit `4`) before the output is opened, naming **both** the configuration key and the CLI flag in the diagnostic. It SHALL NOT infer a year from the system clock, from the input file's filesystem timestamps, or from its name.
2. **The recording must be calendar-locked.** When a calendar rendering is selected and the resolved timestamp format is Standard, or the first record's IRIG timestamp carries the freerun bit, the implementation SHALL surface the calendar-unavailable class (`MieError::CalendarUnavailable` / `MieCalendarUnavailableError`, exit class `2` per L1-EXIT-002) before any row is written. Both conditions are determinable before the first row: format resolution completes before iteration (L2-DEC-011, L2-DEC-015) and freerun is a bit of the first record's timestamp.
3. **Day 366 against a common year** SHALL surface the same calendar-unavailable class, in strict and lenient mode alike, rather than rolling into January or emitting an alternative rendering for the one record.
4. **Year rollover SHALL warn, not adjust.** When a calendar rendering is active and a record's day-of-year is strictly less than that of a previous record from the same input, the implementation SHALL emit a single WARN per input naming the configured year, and SHALL continue to use that year unchanged for every record.
5. **Inert under `doy`.** A configured `year` or `utc_offset` while `doy` is active SHALL have no effect and SHALL NOT be an error.

**Rationale**: Each clause closes a way to produce a well-formed, authoritative-looking, wrong date. Clause 1 refuses to guess: a system-clock fallback dates a 2019 archive with the year it was decoded in, and a file-mtime fallback is destroyed by any copy, rsync or checkout — both fail silently and both fail *later*, in whatever consumed the CSV. Clause 3 fails the whole run rather than the row because day 366 against a common year does not indicate one bad record; it indicates the configured year is wrong, which makes every date in the file suspect. Clause 4 declines to auto-increment because a backward day step is produced by corruption as readily as by a genuine New Year crossing, and the decoder cannot distinguish them — advancing the year on corruption would date a damaged record a year into the future, which is worse than the off-by-one-year it would have fixed. Clause 5 follows `standard_tick_rate_hz`, which is likewise inert outside the format it calibrates (L2-DEC-017): an inert key is not an operator error, and rejecting one would make a single site config unusable across mixed recordings.
**Verification Method**: Test (T)

---

## L2-CFG: Configuration

#### L2-CFG-001

**Parent**: L1-CFG-001
**Statement**: TOML configuration SHALL support logging level, timestamp format, strict mode, error mode, exclusion filters, and output format.
**Rationale**: These are the operator-facing knobs that vary between recording campaigns. The TOML schema is documented in `config/default.toml` and pinned by the schema reference below.
**Verification Method**: Test (T)

#### L2-CFG-003

**Parent**: L1-CFG-001
**Statement**: Configuration precedence SHALL be CLI values over configuration-file values over built-in defaults.
**Rationale**: CLI overrides are the operator's most explicit signal of intent and must always win. Built-in defaults are the bottom-of-stack fallback.
**Verification Method**: Test (T)

#### L2-CFG-004

**Parent**: L1-CLI-002
**Statement**: CLI filter arguments SHALL merge with configuration-file filters.
**Rationale**: Operators routinely have a base set of exclusions in their site config and want CLI flags to add to that set rather than replace it. Replace semantics would force the operator to re-specify the base set on every invocation.
**Verification Method**: Test (T)

#### L2-CFG-005

**Parent**: L1-CFG-001
**Statement**: The CLI SHALL accept a TOML configuration file path.
**Rationale**: The TOML file is the persistence mechanism for site-wide and campaign-wide configuration. A path argument is the only way to point at it.
**Verification Method**: Test (T)

#### L2-CFG-006

**Parent**: L1-CLI-002
**Statement**: Exclusion filters SHALL support message type, RT address, bus, and subaddress.
**Rationale**: These four axes are the discriminating fields in a 1553 transaction header (per L1-CLI-002).
**Verification Method**: Test (T)

#### L2-CFG-007

**Parent**: L1-CLI-002
**Statement**: Type filters SHALL accept documented symbolic names and hexadecimal type codes.
**Rationale**: Operators think in symbolic names (`Receive`, `Transmit`, `ModeCodeNoData`) but the underlying values are hex codes (`0x02`, `0x04`, `0x40`). Supporting both lets the operator use whichever is convenient.
**Verification Method**: Test (T)

#### L2-CFG-008

**Parent**: L1-CFG-001
**Statement**: The configuration schema and key names demonstrated by `config/default.toml` SHALL remain supported. Implementations MAY add additional keys under namespaces that do not collide with shared keys; such additional keys SHALL be ignored or warned by implementations that do not support them.
**Rationale**: Operators rely on `config/default.toml` as the schema reference. Implementations that want to add features can extend the schema in their own namespace without breaking the shared one.
**Verification Method**: Test (T)

#### L2-CFG-009

**Parent**: L1-CFG-001
**Statement**: Unknown top-level TOML keys SHALL produce a WARN at load time naming the offending `[section] key`, but SHALL NOT fail the load.
**Rationale**: Forward compatibility: an older binary opening a newer config should warn but not break. Failing the load would make config rollouts much harder to manage.
**Verification Method**: Test (T)

#### L2-CFG-010

**Parent**: L1-CFG-001
**Statement**: All schema validations (type, range, enum membership, unknown-key detection) SHALL apply at configuration load time, not at use time. A loaded `DecoderConfig` SHALL represent already-validated state; consumers SHALL NOT perform additional validation.
**Rationale**: Load-time validation produces immediate operator feedback and makes the loaded config a trustworthy value. Use-site validation drifts and inevitably creates inconsistent error messages depending on which code path first observed the bad value.
**Verification Method**: Test (T)

#### L2-CFG-011

**Parent**: L1-CFG-001
**Statement**: The configuration schema SHALL accept an optional `decode.standard_tick_rate_hz` key of numeric type (TOML float, or integer coerced to float). When present, its value SHALL be validated at load time as a finite value strictly greater than zero; a non-finite or non-positive value SHALL be rejected with an error naming the key. When absent, the loaded configuration SHALL leave the rate unset (no calibration), preserving the L2-RDR-019 default. The validated value feeds the Standard tick calibration of L2-DEC-017.
**Rationale**: The tick rate is the one piece of timing information the file cannot supply, so it must come from configuration. Validating it at load time (per L2-CFG-010) keeps a bad rate from silently producing garbage microseconds far from the config that introduced it. Accepting an integer as well as a float lets operators write the natural `1000000` instead of being forced to `1000000.0`.
**Verification Method**: Test (T)

#### L2-CFG-012

**Parent**: L1-CFG-001
**Statement**: The configuration schema SHALL accept three optional keys governing timestamp rendering, all in the `[output]` section: `output_time_format` (string, one of `doy`/`iso`/`dom`, matched case-insensitively, default `doy`); `year` (integer in `[1, 9999]`, unset by default); and `utc_offset` (string, either `Z` or a signed `+HH:MM` / `-HH:MM` with `HH` in `[0, 23]` and `MM` in `[0, 59]`, default `+00:00`). Each SHALL be validated at load time per L2-CFG-010, with the diagnostic naming the offending key.

The `decode.time_format` key of earlier versions SHALL NOT be silently ignored. The generic unknown-key rule of L2-CFG-009 is a WARN, which would leave a configuration that forced a timestamp format quietly resolving to `auto`; implementations SHALL therefore special-case the retired key and reject it at load time (the same class and exit `5` as any other invalid configuration value), with a diagnostic naming `decode.input_time_format` as its replacement.

**Rationale**: The keys are validated at load time for the reason L2-CFG-010 gives generally — a bad value should be reported next to the configuration that introduced it, not as a malformed cell hundreds of thousands of rows later. `year` is deliberately schema-optional rather than defaulted: there is no year a decoder could supply that would be right, so "unset" has to be representable, and the requirement to supply one lives with the rendering that needs it (L2-WRT-026) rather than with the schema.

The retired-key clause exists because the unknown-key WARN is exactly the wrong behaviour for a *rename*. For a genuinely unknown key, warn-and-continue is right: a config written for a newer version should still run. For a key that has moved, warn-and-continue means the operator's explicit instruction is discarded while the run reports success — a recording forced to `irig` would silently revert to auto-detection, and auto-detection is right often enough that the loss would not be noticed until it wasn't. A rename must fail where it is written down, not degrade quietly.
**Verification Method**: Test (T)

### L2-CFG schema reference

The table below pins the accepted TOML keys, their types, valid ranges, and unknown-value handling. This schema is normative for `L2-CFG-001`, `L2-CFG-008`, `L2-CFG-009`, `L2-CFG-010`, and `L2-CFG-011`.

| Key | Type | Range / Enum | Unknown-value handling |
|-----|------|--------------|------------------------|
| `logging.level` | string | one of `DEBUG`/`INFO`/`WARNING`/`WARN`/`ERROR`/`CRITICAL`/`OFF` (case-insensitive); `CRITICAL`/`OFF` silence all output | reject at load time |
| `logging.irig_day_advisory` | bool | TOML boolean only (see L2-LOG-001) | reject non-bool |
| `decode.input_time_format` | string | one of `auto`/`irig`/`standard` | reject at load time |
| `decode.strict` | bool | TOML boolean only (not coerced from strings) | reject non-bool |
| `decode.error_mode` | string | one of `separate`/`inline` | reject at load time |
| `decode.allow_partial` | bool | TOML boolean only (see L1-EXIT-004) | reject non-bool |
| `decode.detect_records` | int | `[1, 32]` (see L2-DEC-015); default `8` | reject out-of-range at load time |
| `decode.lookahead_records` | int | `[1, 32]` (see L2-SYN-026); default `2` | reject out-of-range at load time |
| `decode.standard_tick_rate_hz` | float (int coerced) | finite and `> 0` (see L2-DEC-017); unset = no calibration | reject non-finite/non-positive at load time |
| `output.format` | string | `csv` is the only currently valid value | reject at load time |
| `output.no_clobber` | bool | TOML boolean only (see L2-WRT-017) | reject non-bool |
| `output.output_time_format` | string | one of `doy`/`iso`/`dom` (see L2-WRT-025); default `doy` | reject at load time |
| `output.year` | int | `[1, 9999]` (see L2-WRT-026); unset = no year | reject out-of-range at load time |
| `output.utc_offset` | string | `Z`, or `+HH:MM` / `-HH:MM` with `HH` in `[0, 23]` and `MM` in `[0, 59]` (see L2-WRT-025); default `+00:00` | reject at load time |
| `filter.exclude_types` | array of string\|int | per-element validated against `L2-CFG-007` | reject at load time |
| `filter.exclude_rts` | array of int | each in `[0, 31]` (1553 RT range) | reject out-of-range at load time |
| `filter.exclude_buses` | array of string | each in `{A, B}` | reject at load time |
| `filter.exclude_subaddresses` | array of int | each in `[0, 31]` (1553 subaddress range) | reject out-of-range at load time |
| Any unknown `[section] key` | — | — | WARN at load time per L2-CFG-009 |

---

## L2-LOG: Diagnostic logging policy

#### L2-LOG-001

**Parent**: L1-LOG-001
**Statement**: The one-time IRIG day-of-year advisory SHALL be emitted at `INFO`, once per decode, on the first calendar-locked (non-freerun) IRIG record. It SHALL be suppressible outright, at every level, via `--no-irig-day-advisory` or `[logging] irig_day_advisory = false`, with CLI-over-config precedence per L2-CFG-003.

**Rationale**: The advisory is a **standing disclaimer about card firmware, not an observation about the recording being decoded**. Nothing in the decoder compares the decoded day-of-year against anything — it cannot, since the card model is not recorded in the file — so the advisory fires on every calendar-locked IRIG file from every card, whether or not that card is affected. That is the distinction the level encodes: `WARNING` is for things the decoder observed about *this* input (a sync recovery, a non-monotonic DELTA step, a hit sort-group cap), and putting an unconditional disclaimer at the same level meant it appeared in the default output of every run and competed with those. Demoting it to `INFO` keeps it discoverable — `--log-level INFO` still shows it, once — without spending the default level's attention budget on a message that is identical every time.

The separate opt-out exists because the level alone cannot express "I have already validated this". A site that has diffed its card model's day-of-year against vendor CSV (the workflow in `VENDOR-CSV-DIFFS.md` §6) has answered the question the advisory asks, permanently; it should be able to run at `--log-level INFO` for genuine troubleshooting without that one known-noise line. Suppression is deliberately *not* achievable by raising the level, since `--log-level ERROR` would also discard the observational warnings that matter.

**Verification Method**: Test (T)

#### L2-LOG-002

**Parent**: L1-LOG-001
**Statement**: When a calendar rendering (`iso` or `dom`, L2-WRT-025) is active, the L2-LOG-001 day-of-year advisory SHALL be emitted at `WARNING` rather than `INFO`, on the same one-per-decode cadence and on the same trigger. The `--no-irig-day-advisory` / `[logging] irig_day_advisory = false` opt-out SHALL continue to suppress it outright at every level, unchanged.

**Rationale**: L2-LOG-001 demoted this advisory to `INFO` on the grounds that it is an unconditional disclaimer rather than an observation about the file in front of it, and that reasoning is unaffected here — what changes is the consequence if the disclaimer turns out to apply. Under `doy` a firmware-skewed day-of-year is one field an analyst reads as "day 192", visibly a day number and easy to re-examine. Under a calendar rendering the same skew is resolved into `2026-07-11`, which looks like a fact, gets pasted into incident reports, and is correlated against other systems that have no way to know it is suspect. Selecting a calendar rendering is the operator stating that the day-of-year field is load-bearing for them, which is precisely the condition under which a standing caveat about that field earns the default level's attention. The opt-out is untouched because a site that has validated its card model against vendor CSV has answered the question at any rendering.
**Verification Method**: Test (T)

---

## L2-FLT: Filtering

#### L2-FLT-001

**Parent**: L1-CLI-002
**Statement**: Filtering SHALL omit messages matching configured exclusion criteria and yield all other messages unchanged.
**Rationale**: Filtering operates on the post-decode message stream; it does not alter validation or decode semantics. Omission is the only effect.
**Verification Method**: Test (T)

#### L2-FLT-002

**Parent**: L1-CLI-002
**Statement**: Exclusion criteria SHALL use OR logic across configured type, RT, bus, and subaddress filters.
**Rationale**: OR is the most useful default — operators usually want to exclude messages matching *any* of the configured criteria. AND would require the operator to specify the full Cartesian product per excluded message.
**Verification Method**: Test (T)

---

## L2-CLI: Shared CLI capabilities

#### L2-CLI-001

**Parent**: L1-CLI-001
**Statement**: Decode capability SHALL accept **one or more** input paths, supplied by exactly one of the three mutually exclusive methods of L2-MRG-001 (positionals, `--manifest`, `--glob`). A single input SHALL follow the single-file decode path unchanged; two or more SHALL invoke the time-sorted merge of L2-MRG-002.
**Rationale**: Through v2.5.x this requirement read "one input path", and delegated multi-file decode to the operator's shell loop. The multi-file merge (L1-MRG-001 / L2-MRG-001) superseded that: a shell loop cannot interleave records from several recorders onto one absolute timeline, which is the whole point of the merge. The statement is restated here rather than left to L2-MRG-* because a reader looking up the CLI's input contract starts at L2-CLI-001, and finding "one input path" there contradicted both the shipped interface and `docs/CLI-REFERENCE.md`.
**Verification Method**: Test (T)

#### L2-CLI-002

**Parent**: L1-CLI-001
**Statement**: Decode capability SHALL accept an optional output path. The flag's value SHALL be treated as a **path and nothing else**: no value is special-cased, so `-o -` writes a file named `-`. Writing to stdout SHALL be selected by **omitting** the flag, and that SHALL be the only way to select it.
**Rationale**: When absent, the implementation writes to stdout (per L2-WRT-007). `-` for stdout is a real Unix convention, and the C++ implementation honoured it for a while — but the other two did not, and the CLI-surface-parity gate cannot catch that: it compares flag *names*, not what their values mean. Since omitting `-o` already writes stdout in every implementation, a magic filename adds a second spelling for something that already works, and takes a filename away from anyone whose file really is called `-`. The rule is stated here because its absence is exactly what let the three drift apart unnoticed.
**Verification Method**: Test (T)

#### L2-CLI-004

**Parent**: L1-LOG-001
**Statement**: The CLI SHALL accept a configurable logging level.
**Rationale**: Operators want to change the logging level per-invocation without editing a config file. CLI argument is the natural mechanism.
**Verification Method**: Test (T)

#### L2-CLI-005

**Parent**: L1-EXIT-001
**Statement**: Successful commands SHALL return exit code zero; usage or runtime failures SHALL return non-zero.
**Rationale**: Foundational exit-code contract. The specific non-zero codes are pinned by L1-EXIT-002 through L1-EXIT-008 and the L2-CLI-011 table.
**Verification Method**: Test (T)

#### L2-CLI-006

**Parent**: L1-LOG-001
**Statement**: Human-readable diagnostics SHALL be written to stderr rather than mixed into CSV stdout.
**Rationale**: L1-LOG-001 obligates the decoder to provide configurable diagnostic logging; this requirement pins the destination stream. Mixing diagnostics into stdout would corrupt the CSV output and break downstream consumers parsing it.
**Verification Method**: Test (T)

#### L2-CLI-008

**Parent**: L1-CLI-001
**Statement**: The CLI SHALL provide message-counting capability without requiring CSV output.
**Rationale**: Operators often want a record count to sanity-check a file size or compare two recordings. Producing CSV just to count rows is wasteful.
**Verification Method**: Test (T)

#### L2-CLI-009

**Parent**: L1-CLI-001
**Statement**: The CLI SHALL provide raw and record-aware diagnostic dump capability.
**Rationale**: When investigating a corrupt or unusual file, operators want to see the raw bytes (for offset-targeted hex examination) and the record-aware decoded view (for "what did the decoder think this record was"). Both modes are diagnostic.
**Verification Method**: Test (T)

#### L2-CLI-010

**Parent**: L1-CLI-001
**Statement**: The CLI SHALL accept timestamp-format selection, TOML configuration, and shared exclusion filters.
**Rationale**: These are the per-invocation knobs operators use during analysis. Each must be available via a CLI flag even when a config file is in use.
**Verification Method**: Test (T)

#### L2-CLI-011

**Parent**: L1-EXIT-001
**Statement**: Exit codes SHALL follow L1-EXIT-002 through L1-EXIT-009 and SHALL be identical across both implementations for the same condition:

| Code | Class | Condition |
|------|-------|-----------|
| `0` | success | complete decode, recovered decode, or `--allow-partial` partial |
| `1` | runtime/decode error | input I/O (incl. file-not-found), writer failure, strict-mode record or structural-invariant failure |
| `2` | no valid records | input is not an MIE recording (wrong file type, homogeneous-payload, ambiguous timestamp format in strict mode) |
| `3` | unrecoverable sync loss | mid-file sync loss without `--allow-partial` |
| `4` | CLI usage error | unknown/missing/invalid flag or argument, invalid flag value, no subcommand, combined input methods, or more than `MAX_MERGE_FILES` inputs |
| `5` | configuration error | config file not found, malformed TOML, or invalid config value |
| `6` | merge-incompatible inputs | multi-file merge where an input is Standard-format, leads with a freerun IRIG record, or the set mixes timestamp formats (L1-EXIT-009) |

The `count` and `dump` commands inherit `0`, `1`, `2`, `4`, and `5` but SHALL NOT produce exit `3` (they do not write a streaming output that could be partial) or `6` (they do not merge).
**Rationale**: The exit-code taxonomy is the single most operationally useful piece of CLI behavior, so each failure class an operator can act on differently gets its own code: a bad command line (`4`), a bad config file (`5`), a bad input (`2`), and a corruption event (`3`) are distinct situations with distinct fixes. Usage errors use `4` rather than the argparse / Unix default `2` because `2` is the no-valid-records class — overloading it would conflate "you typed a bad flag" with "you pointed me at the wrong file". The count/dump exemption from `3` keeps `3` specifically about a partial output that did not complete.
**Verification Method**: Test (T)

#### L2-CLI-012

**Parent**: L1-CFG-001
**Statement**: The `decode` command SHALL accept a `--standard-tick-rate-hz <HZ>` flag (both space-separated and `=`-joined forms) that overrides `decode.standard_tick_rate_hz` per the standard precedence (CLI over config over default). The supplied value SHALL be validated at parse time as a finite value strictly greater than zero, mirroring the load-time validation of L2-CFG-011; an invalid value SHALL be rejected before decoding begins with a diagnostic naming the flag, following each implementation's existing convention for rejecting a bad flag value (the same path as `--detect-records` / `--lookahead-records`). The validated value enables the Standard tick calibration of L2-DEC-017.
**Rationale**: A per-invocation flag lets an operator calibrate one recording without editing a config file, and matches how the other decode-tuning knobs are exposed. Parse-time validation gives immediate feedback consistent with the config path so the two entry points reject the same inputs.
**Verification Method**: Test (T)

#### L2-CLI-013

**Parent**: L1-CLI-001
**Statement**: The record-aware dump SHALL emit each scan-stop anomaly it encounters — invalid Type Word `word_count`, a record whose declared extent runs past EOF (truncated record), and (where the host integer type can overflow) record-offset overflow — through the logger at `WARN`, in addition to the inline `!! …` note written into the hex report. The log message SHALL name the byte offset. Emission is subject to the configured global log level (default `WARN`); the inline report note is unchanged.
**Rationale**: The record-aware dump previously surfaced these anomalies only inside the report stream, so an operator piping the dump report elsewhere — or any caller that captures the report separately — could not see the diagnostics on the normal stderr log channel the way the reader's diagnostics appear. Routing them through the logger as well makes the dump's diagnostics consistent with the reader's and visible at the configured level, while the inline note is retained for the at-a-glance visual report. (The reader's logger writes to process stderr in Rust and through the `aero1553` logger in Python; the dump uses the same channels.)
**Note on the overflow anomaly**: it is unreachable through a real scan, in both implementations, and the wording above is scoped accordingly. Python integers do not overflow at all. In Rust the scan loop only advances while `offset + MIN_RECORD_BYTES <= file_len`, and `file_len` is a mapped file length, so `offset` stays far below `usize::MAX` while a record's declared extent is capped at 126 bytes (`word_count` is the Type Word's 6-bit field, `(raw >> 8) & 0x3F`, so at most 63 words) — the sum cannot wrap. The guard remains as defense in depth for the contract of `dump_record_extent`, which accepts an arbitrary `offset`, and is verified by calling that helper directly (`dump_record_extent_notes_offset_overflow`). It SHALL NOT be credited to the truncated-record tests, which never reach it.
**Verification Method**: Test (T), Inspection (I)

#### L2-CLI-014

**Parent**: L1-CLI-001
**Statement**: **Every byte any implementation writes to stdout or stderr SHALL be ASCII.** This binds stdout **payload** — the `dump` report, the `count` integer, CSV written to stdout — and equally the human-facing **prose**: log messages, diagnostics, usage errors and help text. No implementation SHALL manipulate the console code page, or reconfigure a stream's encoding, in order to make non-ASCII output render.

The `dump` report additionally SHALL be **byte-identical across every implementation** for the same input and flags, and SHALL use LF line endings on every supported platform, including when written to stdout.

**Rationale**: The report is data, not decoration: it is piped, redirected, and diffed against another implementation's. Three properties were each violated before this was written down. The Python report used box-drawing (`─`), arrow (`→`) and en-dash (`–`) characters where Rust and C++ used `-`, `->` and `-`, so 11 of 34 lines differed on a typical fixture. Those characters could not be encoded on a redirected Windows stdout at the cp1252 code page — they raised `UnicodeEncodeError` and aborted the dump — which had been worked around by forcing the stream to UTF-8 rather than by removing the cause. And that workaround reconfigured the encoding only, leaving text-mode newline translation in place, so **every stdout payload from the Python CLI emitted CRLF on Windows**, including CSV written with `-o -`, in violation of L2-WRT-012. ASCII removes the encoding hazard at its source and needs no console-codepage manipulation — which matters most for the C++ implementation, whose platform layer is deliberately confined and which is targeted at a console (SLES 12 SP5) where a UTF-8 assumption is least safe.

**The prose carve-out was a defect, and this requirement originally contained it.** As first written, the rule bound payload only, reasoning that a payload is piped and diffed while stderr prose is merely read by a human. That reasoning inverted the risk. A payload is usually consumed by a program, which either handles the bytes or fails loudly; prose is consumed by a person, on whatever console they happen to have. Windows consoles do not default to UTF-8 — a stock `cmd.exe` runs at the OEM code page, 437 in a US install — while all three implementations write UTF-8 unconditionally. So `mie-decoder --help` opened with `mie-decoder ΓÇö DDC MIL-STD-1553 MIE binary decoder`, and the IRIG day-of-year advisory ended `... ΓÇö see docs/VENDOR-CSV-DIFFS.md ┬º5`. Only three characters were ever involved — U+2014, U+2192, U+00A7 — across 46 sites and all three implementations, and every one of them was in the half the rule had exempted. The failure mode is worse than cosmetic: unexplained bytes in a decoder's own diagnostics read as memory corruption, which is exactly how it was reported. Enforcement is `scripts/assert-ascii-output.py`, which scans shipped string literals in all three trees and catches both spellings — a raw byte and an escape such as `\xE2\x80\x94`, the form C++ had used, which is invisible to a byte-level grep of the source. Comments and doc comments are exempt and unchanged; they are never written to a stream. Removing the characters also retired two C++ workarounds that existed only to survive them, both turning on C++'s hex escape being greedy: `"\x92BC"` is one out-of-range escape rather than an arrow followed by `BC`, so two literals had been split mid-word to avoid it.
**Verification Method**: Test (T)

#### L2-CLI-015

**Parent**: L1-CLI-001
**Statement**: Every CLI flag that takes a value SHALL accept both the separated spelling (`--flag value`) and the joined spelling (`--flag=value`), identically, in every implementation, for global flags as well as subcommand flags. The two spellings SHALL be indistinguishable in effect: same exit code, same diagnostics, byte-identical output. Three boundaries are part of the contract:

- `--flag=` SHALL be the flag carrying an **empty value**, not an unknown option. The flag's own validator then decides — an empty filter list is accepted (and excludes nothing), an empty `--mux-delimiter` is rejected as a usage error.
- Only the **first** `=` SHALL separate; the remainder is the value verbatim, so `--mux-delimiter==` sets the delimiter to `=`.
- A flag that takes **no** value SHALL reject a joined value (`--no-mux=true` is a usage error) rather than set the flag and discard the value.

Splitting SHALL apply only to `--` tokens, so a positional path may contain `=` and `-o=value` is not an accepted spelling.

The **separated** form SHALL NOT consume a following token that looks like an option. A token looks like an option when it begins with `-`, is longer than one character, contains no space, and does not begin like a number (`-`, an optional `.`, then a digit). Such a token SHALL be refused as a usage error (exit `4`) naming the flag, and the diagnostic SHALL point at the joined spelling, which remains legal for exactly these values. The three exemptions are normative, not incidental: a lone `-` is a path (L2-CLI-005), a number-leading token is a value (`--mux-field -1` counts from the end, and `--collapse-window-us -5` must reach its own validator to be refused for being *negative* rather than for looking like a flag), and no option is spelled with a space.

A token spelled like an option name (`-x`, `-o`, `-abc`, `--foo`, `--1`) is refused. A lone `-`, a plain decimal (`-1`, `-5.5`, `-.5`), a token containing a space, and **any token that begins like a number** — including *dash, digit, then something else* (`-5e3`, `-0x5`, `-1a`, `-5.`) — are values. A number-leading token that the flag cannot use SHALL be rejected by that flag's own validation (exit `4`, naming the value), not by the option-like rule: `--mux-field -1a` reports an invalid `--mux-field` value, never that `-1a` is an option.

**Rationale**: Both spellings are standard, both have always worked, and nothing shared proved it — three Rust unit tests, one C++ test, no Python tests, no conformance cases. The CLI-surface-parity gate compares flag *names*, so it cannot see how a value attaches to one, exactly as it could not see the `-o -` divergence recorded in L2-CLI-005.

The risk is asymmetric because two of the three parsers were hand-rolled. Python inherited the joined form from `argparse` (until v4.0.0, when its command line became the Rust CLI); C++ resolves it in one place; Rust repeated a per-flag `starts_with("--flag=")` arm at 26 sites, where a flag added with only the separated arm would silently reject the joined form. C++ failed the first boundary above in the opposite direction: it required at least one character after the `=`, so `--exclude-rts=` was reported as an unknown option (exit 4) where Rust and Python applied an empty filter and decoded normally (exit 0).

The option-like rule is `argparse`'s, adopted deliberately rather than invented. Rust and C++ previously consumed the following token unconditionally, so `--mux-delimiter --no-mux` set the delimiter to the string `"--no-mux"` and `--no-mux` **silently never ran** — a wrong decode that exited `0`, which is worse than either a refusal or a disagreement. Python had always refused it. Levelling toward Python turns a silent wrong answer into a usage error; levelling the other way was impossible without abandoning `argparse`.

The number-leading rule is the one `argparse` adopted in Python 3.14, where the exemption became a **prefix** test, `-\.?\d`. It is simpler to state than the alternative, and it yields the better diagnostic for the realistic case — `--mux-field -1a` is almost always a mistyped number, and letting it through means the flag's own validator names the bad value instead of reporting that the next argument is an option.

**History: this contract was bounded until v4.0.0.** While the Python CLI was built on `argparse`, `argparse` did not agree with itself across the supported versions: through 3.13 the exemption was an anchored full match, `^-\d+$|^-\d*\.\d+$` — only plain decimals — so `-5e3` was an option error on 3.10–3.13 and a value on 3.14, and **no** single rule could match all five interpreters. Rust and C++ followed 3.14, and the requirement excluded the *dash, digit, then something else* shapes and barred them from the conformance suite. (That boundary was found by CI, not by inspection: the first implementation matched 3.13 because that is what the development machine ran, and the 3.14 jobs failed on exactly `-5e3`, `-0x5` and `-1a`.) In v4.0.0 the Python package's command line became the Rust CLI, run in-process, so the interpreter no longer takes part in parsing; the exclusion is lifted and the conformance suite pins those shapes in all three implementations.

**Verification Method**: Test (T)

#### L2-CLI-016

**Parent**: L1-CLI-001
**Statement**: The CLI SHALL honour `--` as the POSIX end-of-options separator, in every implementation. Within the token stream of a given parser, the **first** `--` SHALL be discarded and every token after it SHALL be treated as a positional argument regardless of spelling; a subsequent `--` is itself an ordinary positional, which is the only way to name a file called `--`.

The separator is **scoped to the parser that consumes it**. A `--` appearing before the subcommand SHALL cause the next token to be taken as the subcommand *name* verbatim — so `-- --version` reports an unknown command rather than printing the version, and `-- -h` does not print help — and SHALL NOT put the subcommand's own parser into end-of-options mode: `-- decode rec.mie --no-mux` still honours `--no-mux`.

Both positions are binding: after the subcommand (`decode -- rec.mie`), which is the one operators actually need, and before it (`-- decode rec.mie`). A **trailing** `--` with nothing after it SHALL be a no-op wherever it appears, including behind an option's value (`decode rec.mie -o out.csv --`).

`--` SHALL NOT be accepted as a flag's value in the separated form (`-o -- x.mie` is a usage error), which follows from L2-CLI-015 since `--` looks like an option.

**Rationale**: Without it there is no way to decode a file whose name begins with a dash, and the tool is the only thing standing between an operator and a recording they cannot rename. Python inherited the behaviour from `argparse` and had it from the beginning; Rust and C++ reported `--` itself as `unknown option` (exit `4`), so the same command line worked or failed depending on which implementation was installed — precisely the class of drift the CLI-surface-parity gate cannot see, because it compares flag *names*.

The scoping rule is the part worth stating explicitly, because the plausible alternative is wrong: carrying end-of-options across the subcommand boundary would make `-- decode rec.mie --no-mux` silently ignore `--no-mux`, which is the same silent-wrong-answer failure L2-CLI-015 exists to remove.

**History: until v4.0.0 the contract bound the post-subcommand position only**, because of two divergences in `argparse`, each found by CI rather than by inspection. A pre-subcommand `--` was Python **3.12+** — and not even a minor-version boundary: it reached 3.12 in a patch release, so Ubuntu 24.04's system 3.12.3 rejected it while 3.12.10 accepted it. And a trailing separator behind an option's value (`decode rec.mie -o out.csv --`) was rejected on every version, the `--` surviving as an unrecognized argument. Rust and C++ supported both on every version; levelling downward would have meant removing working capability to match the oldest interpreter. In v4.0.0 the Python package's command line became the Rust CLI, run in-process, so both divergences are gone, both positions are binding, and the conformance suite pins each of them in all three implementations.
**Verification Method**: Test (T)

#### L2-CLI-017

**Parent**: L1-CLI-001
**Statement**: A pending `-h`/`--help` SHALL outrank a **deferred** diagnostic. When the command line is otherwise invalid — an unrecognised option, or a flag value this CLI validates after parsing — and a help flag appears later and before any `--`, the CLI SHALL print help and exit `0` rather than report the error.

It SHALL NOT outrank a failed value **consumption**. When a flag that requires a value cannot take one — because the argument list ended, or because the next token looks like an option (L2-CLI-015) — that is a usage error (exit `4`) and a later help flag SHALL NOT rescue it. `--log-level -h` and `--config --help` are usage errors in every implementation.

A help flag after `--` is a path, not a request (L2-CLI-016), so `decode --nonsense -- --help` is a usage error.

**Rationale**: An operator whose command line is wrong is the one most likely to be asking for help; answering the question they did not ask is the least useful response available. Python and C++ both did this and Rust did not, so `decode --nonsense --help` printed help in two implementations and reported the unknown option in the third.

The split between the two halves is `argparse`'s, and it is a real distinction rather than an accident: it raises immediately when it cannot structurally take a value — at which point the rest of the command line is uninterpretable, because nothing downstream can be attributed to the right flag — and defers everything else until after the help action has had its chance. Rust expresses it by draining the argument iterator on a consumption failure, so the "is help still pending" test answers `false` without a new variant on the public `ParseError`.

Version is **global-only**: recognised before the subcommand and an unknown option after it, in every implementation. Help is accepted both places. Version matching is case-insensitive (`--VERSION`); help matching is **not** (`--HELP` is a usage error). That asymmetry is deliberate and is shared by all three.

A flag value that parses but fails its flag's validation is a **deferred** diagnostic like any other: `--max-sort-group abc --help` prints help and exits `0`.

**History:** until v4.0.0 that last shape exited `4` in Python. `argparse` wired `--max-sort-group`'s conversion into parsing via `type=int`, so the failure was immediate there, while other flags were validated after `parse_args` and let help win — an implementation accident of which validations happened to live inside `argparse`, not a rule, and so never levelled. In v4.0.0 the Python package's command line became the Rust CLI, run in-process, and the divergence is gone; the conformance suite now pins it in all three implementations.

C++ was the outlier on six shapes until its help/version scan was replaced. It ran over the whole argument vector, so it could not tell a help token apart from one being consumed as a flag's **value** (`--log-level -h` printed help), and did not know where the subcommand began (`decode rec.mie --version` printed the version). Both are now resolved positionally, and the deferred-versus-consumption split is expressed the same way as in Rust: the argument reader abandons the rest of the line when a flag cannot take its value, so the "is help pending" test answers `false` there.
**Verification Method**: Test (T)

#### L2-CLI-018

**Parent**: L1-CLI-001
**Statement**: The `decode` command SHALL accept four timestamp flags, each in both the space-separated and the `=`-joined form, each overriding its configuration key per L2-CFG-003:

| Flag | Value | Overrides |
|---|---|---|
| `--input-time-format` | `auto` \| `irig` \| `standard`, case-insensitive | `decode.input_time_format` |
| `--output-time-format` | `doy` \| `iso` \| `dom`, case-insensitive | `output.output_time_format` |
| `--year` | integer in `[1, 9999]` | `output.year` |
| `--utc-offset` | `Z`, or `+HH:MM` / `-HH:MM` | `output.utc_offset` |

All four SHALL be rejected at parse time on an invalid value as a usage error (exit `4`) naming the flag, on the same path as `--detect-records` and `--lookahead-records`. All four SHALL be `decode`-only: `count` and `dump` SHALL reject them as unknown options, consistent with `dump` consuming only `[logging] level` and `count` taking no flags at all.
**Rationale**: `--input-time-format` is `--time-format` renamed, and the rename is the point: the old name claimed a generality it never had. It selected how the *bytes* were parsed and said nothing about how the column was *rendered*, so once rendering became selectable (L1-OUT-004) one flag named `--time-format` could not honestly carry both concerns, and whichever concern it kept would leave the other looking like an afterthought. Naming the two symmetrically — input versus output — makes it impossible to reach for the wrong one from memory, which the alternatives (`--time-format` retained for one side, a differently-shaped name for the other) did not.

Keeping all four on `decode` follows the existing division rather than inventing one: `dump` is a pre-decode hex tool that resolves no timestamp format at all and renders its `Time:` line as day-of-year unconditionally, so a rendering flag there would advertise a choice it could not honour.
**Verification Method**: Test (T)

#### L2-CLI-019

**Parent**: L1-CLI-001
**Statement**: The retired `--time-format` flag SHALL be rejected as a usage error (exit `4`), SHALL NOT be accepted as a deprecated alias for either replacement, and SHALL NOT write any output. Its diagnostic SHALL be produced by an explicit arm naming **both** `--input-time-format` and `--output-time-format` and stating which concern each covers — not by the generic unknown-option path, which would report only that the flag is unrecognised.
**Rationale**: The `--inline-errors` removal in v2.8.0 established that a retired flag fails loudly rather than aliasing, and that stays right: a script still passing `--time-format irig` is asserting something about parsing that it must keep asserting, and silently dropping it would resolve the format by auto-detection instead — right often enough that the loss would go unnoticed until it wasn't.

But this is a rename, not a removal, and the two deserve different diagnostics. `--inline-errors` had no successor to point at, so the generic "unknown option" message lost nothing. Here the operator's intent is still fully expressible and the only open question is *which* of two flags they meant — the one question the generic message cannot answer, and the one an operator hitting this at 2am most needs answered. Naming both replacements and what each does turns a lookup in the changelog into a fix on the spot. This is new machinery in all three parsers, which is why it is specified rather than left to each implementation's unknown-option arm.
**Verification Method**: Test (T)

---

## L2-MRG: Multi-file time-sorted merge

#### L2-MRG-001

**Parent**: L1-MRG-001
**Statement**: The `decode` command SHALL accept the input set via exactly one of three mutually exclusive methods: one or more positional paths, a `--manifest <file>`, or a `--glob <pattern>`. Supplying more than one method, or resolving to more than `MAX_MERGE_FILES` inputs, SHALL be a usage error (exit `4`). Resolving to a single input SHALL invoke the existing single-file path unchanged; resolving to two or more SHALL invoke the merge. The `--glob` pattern SHALL be a single-directory pattern supporting `*` and `?` wildcards over the filename only (no recursive `**`, no brace expansion), and every implementation SHALL expand it identically and in a deterministic (lexicographic) order.

The **manifest grammar** SHALL be exactly:

1. The file SHALL be well-formed **UTF-8**. Ill-formed input SHALL be rejected, not decoded — including overlong encodings, surrogate halves and code points above U+10FFFF.
2. `\n` SHALL be the **only** line separator. No other character terminates a line. The text after the final `\n` SHALL be a line in its own right; a missing final terminator SHALL NOT change how that line is read.
3. At most **one trailing `\r`** SHALL be stripped from **every** line — including the final one when the file does not end in `\n` — so a CRLF file reads correctly whether or not its last line is terminated, and a filename containing a carriage return survives intact. No other `\r` SHALL be removed, and no implementation SHALL apply universal-newline translation while reading.
4. Each line SHALL then be trimmed of **ASCII space (0x20) and tab (0x09) only**. No other whitespace SHALL be trimmed.
5. A line that is empty after trimming, or whose first character after trimming is `#`, SHALL be ignored. Every other line SHALL contribute one path, in file order.

The **glob expansion** SHALL be exactly:

1. The pattern SHALL be split at its **last path separator** into a directory and a filename pattern; with no separator the directory is the current one. What counts as a separator is the **platform's** answer, not a fixed set: `/` on POSIX, `/` or `\` on Windows. A backslash is a legal POSIX filename character, so treating it as a separator there splits a pattern that named one file into a directory and a name.
2. Wildcards SHALL apply to the filename pattern only. `*` matches any run of characters including the empty one, `?` matches exactly one character, and no other character is special. There is no recursive `**`, no brace expansion, and no character class.
3. `?` SHALL advance by one **character**, not one byte, so a pattern matches the same non-ASCII names in every implementation.
4. Only directory entries that resolve to a **regular file** SHALL be matched. Resolution SHALL follow symlinks, so a recording reached through a symlink is matched and a dangling symlink is not. Directories — including one whose name matches the pattern, such as `archive.mie` — sockets, fifos and devices SHALL NOT be matched. An entry whose type cannot be determined SHALL be skipped rather than raise: a directory listing racing a deletion is not a reason to fail the run.
5. The result SHALL be sorted lexicographically by full path, so two hosts enumerating one directory merge in the same order.
6. A pattern matching nothing SHALL yield an empty set, which is **not** an error at this layer — it is what lets the CLI distinguish "matched no files" from "could not read the directory".

**Rationale**: Positionals serve ad-hoc use, a manifest serves large/scripted sets, and a tool-expanded glob serves directories on shells (Windows) that do not expand globs. Mutual exclusivity avoids ambiguous union/ordering semantics. A fixed file-count cap keeps open mappings/descriptors within OS limits. Constraining the glob to a small, identical syntax lets the Rust crate stay dependency-free while keeping cross-implementation behavior byte-identical.

The expansion grammar is spelled out for the same reason the manifest grammar is: "expand it identically" was the requirement, and three implementations wrote three expansions that agreed on the *wildcards* and differed on everything around them. C++ matched every entry `list_directory` returned, so a directory named `archive.mie` became an input there and was skipped by the other two — the C++ run then failed to map it and came back short on a batch the others decoded whole. Rust filtered on `DirEntry::file_type`, which does not follow symlinks, so a symlinked recording was dropped there and kept by Python. And C++ split on `\` on both platforms, so `--glob 'odd\name*.mie'` was one pattern in the current directory to two implementations and a pattern inside a directory to the third. None of these is a wildcard question, which is why stating the wildcard rules alone did not settle them.

**On the grammar being spelled out.** Through v2.15.1 this requirement said only "one path per line; blank lines and `#`-prefixed comment lines ignored", and each implementation filled the gaps with whatever its standard library made easy. Every gap below was filled a different way in a different tree, and every one was found by the merge fuzz harness comparing counters across implementations (`docs/FUZZING.md`):

| Gap | What happened | Outlier |
|---|---|---|
| Text vs bytes | `std::string` validates nothing, so C++ accepted arbitrary bytes as paths; `fs::read_to_string` and `read_text(encoding="utf-8")` both refuse. 498 of 512 generated manifests were rejected by two implementations and decoded by the third. | C++ |
| Line separators | `str.splitlines()` also breaks on vertical tab, form feed, U+0085 and U+2028/9 — none of which ends a line in a manifest, all of which are legal in a POSIX filename. One file became two nonexistent ones. | Python |
| Carriage returns | C++ dropped **every** `\r` in a line, silently editing a filename containing one; Python's reader translated a lone `\r` to `\n` before the parser saw it, splitting one path into two. | C++, Python |
| Trimming | `str::trim` and `str.strip` remove Unicode whitespace (U+00A0, U+3000, …); the C++ implementation is locale-free by rule (`scripts/assert-locale-free.sh`) and cannot, so two implementations edited a filename the third passed through. | Rust, Python |
| The **unterminated** last line | `str::lines()` strips a trailing `\r` only from a line an `\n` actually followed, so a file ending `"b.mie\r"` — a CRLF manifest whose final terminator was lost — yielded a path with a CR in Rust and `b.mie` in the other two. The one-byte manifest `"\r"` was one path in Rust and none elsewhere. | Rust |

Rule 3's "every line" and rule 2's sentence about the final line were both added for that last row: the first wording said "each line", which reads as settled until one standard library decides an unterminated tail is not quite a line. Rule 4 resolves the trimming row **toward** the constrained implementation rather than away from it: C++ cannot classify Unicode whitespace without embedding a table, and "trim spaces and tabs" is what a manifest format actually needs. Each rule is pinned by a `read_manifest_grammar_is_exactly_specified` test in all three trees.
**Verification Method**: Test (T)

#### L2-MRG-002

**Parent**: L1-MRG-001
**Statement**: The merge SHALL be a streaming k-way merge driven by a min-heap holding at most one decoded record per open input. The heap's ordering key SHALL be the tuple `(IRIG total microseconds, input index in resolved order, within-file sequence number)`, giving the merge a total, deterministic internal order including for equal timestamps. This key governs the order in which records leave the heap; it does **not** govern the order in which rows reach the CSV, which for equal timestamps is the canonical `RT`/`MSG` order imposed downstream by L2-WRT-021. Resident memory SHALL be O(number of inputs) and independent of the total record count.
**Rationale**: Each recording is already chronological within itself, so a heap-merge of k sorted streams yields global order without buffering. The index/sequence tiebreak makes the heap's own output reproducible regardless of heap internals, which is what keeps the merge deterministic; a heap key alone cannot produce canonical equal-timestamp order, because the heap holds only one record per input and so never sees two same-timestamp records from the *same* input at once — that is precisely why L2-WRT-021's reorder stage exists downstream rather than as a richer heap key here. The O(k) bound preserves the constant-record-memory guarantee.
**Verification Method**: Test (T)

#### L2-MRG-003

**Parent**: L1-MRG-002
**Statement**: Before emitting any output, the merge SHALL confirm every input resolves to the IRIG timestamp format and that each input's leading valid record is calendar-locked (not freerun). If any input is Standard-format, leads with a freerun record, or the set mixes formats, the merge SHALL reject the whole invocation with exit code `6` (L1-EXIT-009), naming the offending file and its detected format, and SHALL NOT create an output file. A freerun IRIG record encountered after the leading record SHALL emit a WARN and still be ordered by its key.
**Rationale**: Absolute-time ordering requires a shared calendar-anchored clock; Standard counters and freerun IRIG do not provide one. Validating each file's leading record is an O(1)-per-file guard that catches the common case before any work; the mid-stream freerun WARN surfaces the rarer in-file transition without aborting a partially-written merge.
**Verification Method**: Test (T)

#### L2-MRG-004

**Parent**: L1-MRG-001
**Statement**: Per-file failure during a merge SHALL follow the same strict/lenient/`--allow-partial` policy as a single-file decode, applied across the batch: strict mode SHALL surface the first record or structural-invariant failure in any file; lenient mode SHALL skip invalid records; an unrecoverable failure in any file (unrecoverable sync loss, or an unreadable/empty/non-MIE input) SHALL fail the batch unless `--allow-partial`, in which case that file SHALL be truncated at its failure point with a WARN naming it, the merge SHALL complete from the remaining inputs, and the combined output SHALL be written with the `.partial` suffix **appended to the whole file name** — `<output>.partial`, and `<stem>_errors<suffix>.partial` in separate mode (so `out.csv` yields `out.csv.partial` and `out_errors.csv.partial`) — exit `0`. This applies regardless of **where** the per-file failure is detected — at **open** (an empty / unreadable / missing file), at **priming** (the first record is non-MIE), or **mid-file** (an unrecoverable sync loss): each truncates that input at its failure point (offset 0 for an open- or priming-time failure) and contributes whatever it already decoded, if anything, to the one combined `.partial`.
**Rationale**: Reusing the established within-file semantics means operators learn one failure model. Because the output is a single time-sorted stream, an incomplete batch yields one `.partial` artifact containing everything decoded, consistent with how a single-file partial is surfaced.
**Verification Method**: Test (T)

#### L2-MRG-005

**Parent**: L1-DLT-001
**Statement**: In merge mode the scope over which DELTA is measured SHALL be selectable, via the `merge.delta_scope` config key and the `--delta-scope` CLI flag, between exactly two values:

- **`per-file`** (the **default**): DELTA SHALL be measured within the input file each record was decoded from, so that a record's gap is to the previous same-key record **from its own file**. The value for any given record SHALL be identical to the value that record would receive were its file decoded on its own. The first occurrence of a key within each file SHALL therefore be `0.000000`.
- **`global`**: DELTA SHALL be measured across the merged (globally time-ordered) stream, so each gap reflects the unified timeline across all inputs and only the first occurrence of a key in the whole stream is `0.000000`. The merged stream is monotonic per key by construction, so DELTA SHALL be non-negative.

A single-input decode SHALL be unaffected by the setting: with one file the two scopes are the same computation by definition, and the flag SHALL be accepted as a no-op rather than rejected. Under `per-file`, cross-recorder duplicate collapsing (L2-MRG-007) SHALL NOT alter any surviving record's DELTA, because that value is a property of the record's own file and not of the merged stream.
**Rationale**: The two scopes answer genuinely different questions — "how long since *this recorder* last saw this key" versus "how long since *any* recorder last saw it" — and which is correct depends on what the inputs are, which the decoder cannot infer. `per-file` is the default because it is the answer that composes: it matches what the DDC vendor tool produces (the vendor has no merge feature and always reports per-file), it matches what this decoder produces for the same file decoded alone, and it stays meaningful no matter how the input set is assembled. `global` silently compresses inter-arrival gaps whenever one RT/SA key appears in more than one input — for overlapping recorders it degenerates to alternating `0.000000` values — so making it the default made merged DELTA depend on an operator's choice of input set rather than on the bus traffic. Restricting the choice to two named scopes (rather than, say, keying on a parsed recorder identity) keeps the semantics stateable in one sentence each and verifiable against a single-file decode.
**Verification Method**: Test (T)

#### L2-MRG-006

**Parent**: L1-MRG-001
**Statement**: The merge SHALL verify that each input is internally time-sorted: when a record's absolute IRIG microsecond key is strictly less than that of the previous record pulled from the **same** input (capture order), the merge SHALL detect the backward step in O(1) per record. In lenient mode it SHALL emit a WARN naming the offending input, at most once per input, and SHALL still emit every record in heap order (it SHALL NOT re-sort). In strict mode it SHALL surface a record error (the `NonMonotonicInput` / `MieNonMonotonicInputError` class, CLI exit `1`), consistent with the strict/lenient policy of L2-MRG-004. Equal keys (ties) SHALL NOT be treated as a backward step. The canonical-order reorder stage of L2-WRT-021 SHALL NOT be construed as violating the no-re-sort clause above: that stage permutes only within a run of **consecutive** identical timestamps and never moves a record across a timestamp boundary, so a backward step remains visible in the output exactly where it occurred.
**Rationale**: The k-way merge (L2-MRG-002) is only correct if each input is itself chronological; a file made non-monotonic by sync-loss recovery or a day/year rollover otherwise produces silently out-of-order merged rows. Detection mirrors the existing within-file non-monotonic-DELTA advisory (L2-RDR-017): WARN, never re-sort (re-sorting would defeat the streaming O(k) memory guarantee). Strict mode escalates to a failure because a backward step inside one recorder's own file is the same class of data-integrity anomaly that strict mode already rejects at the record level. The one-time-per-input WARN cadence avoids log flooding on a badly corrupted input.
**Verification Method**: Test (T)

#### L2-MRG-007

**Parent**: L1-MRG-003
**Statement**: When cross-recorder duplicate collapsing is enabled (the `merge.collapse_duplicates` config key / `--collapse-duplicates` flag, off by default), the merge SHALL suppress a record whose wire content matches a recently-emitted record from a **different** input within `merge.collapse_window_us` microseconds (`--collapse-window-us`, default `0` = exact-microsecond match). "Wire content" SHALL be the decoded Type Word, Command Word(s), Status Word(s), Error Word, and data words — excluding the timestamp, file offset, MUX, and DELTA. The first record of a duplicate set in heap order SHALL survive. Identical content from the **same** input SHALL NOT be collapsed, and a single-input decode SHALL be unaffected. De-duplication SHALL run **before** the global-DELTA stage (L2-MRG-005) so DELTA is computed over the surviving stream. Its retention rule SHALL be stated on the **absolute** time distance to the record being tested — a survivor SHALL be retained if and only if `|survivor_us - current_us| <= merge.collapse_window_us` — and SHALL therefore be **independent of the order in which survivors were appended**. Eviction SHALL NOT be restricted to the oldest-arrived end of the set. Together with the count bound of L2-MRG-008 this preserves the L2-MRG-002 streaming guarantee. The window comparison SHALL use the **absolute** time distance between two records, so a lenient non-monotonic input (L2-MRG-006) whose merged stream steps backward SHALL neither fault (no integer underflow) nor collapse records that lie outside `merge.collapse_window_us`; on such known-bad ordering collapsing is best-effort.
**Rationale**: Recorders on a shared bus see the same transactions; collapsing the cross-recorder copies of one event restores an accurate count. A content key over the wire fields (not the timestamp) recognises the same transaction even when recorder clocks differ slightly; the window absorbs that skew, with an exact-match default that can never over-collapse genuinely distinct traffic. Requiring a *different* input distinguishes "one event seen twice" from a single recorder's own repeated periodic traffic. Running before DELTA keeps inter-arrival gaps measured across the deduped timeline; the bounded window keeps the merge streaming. The window distance is absolute (not a one-sided subtraction) because a lenient non-monotonic input can emit a record whose timestamp is *earlier* than a buffered survivor; a one-sided gap would underflow (a debug-build panic in Rust) and could match a record far outside the window (an over-collapse in Python) — both regressions are pinned by test.

**On retention being order-independent.** Through v2.16.0 the *matching* test used the absolute distance but *eviction* did not: all three implementations evicted only from the front of an arrival-ordered deque, testing the one-sided `current_us - front_us`. That is correct only while the stream is sorted. After a lenient backward step (L2-MRG-006) the front can hold a timestamp in the **future** of the current record; the one-sided test is then never true, the front never leaves, and it blocks eviction of everything behind it. An alternating 1000µs / 0µs probe with a zero-width window retained all 10 000 records and ran quadratically — 2× the records for 4× the time — which is precisely the unbounded growth this requirement claimed to prevent and L1-ROB-001 forbids. Stating the rule as a property of the retained set, rather than as a loop over one end of a container, is what makes it checkable independently of how survivors happen to be stored.
**Verification Method**: Test (T)

#### L2-MRG-008

**Parent**: L1-ROB-001
**Statement**: The number of survivors retained by the de-duplication window of L2-MRG-007 SHALL be bounded by the configuration key `[merge] max_collapse_survivors` (CLI `--max-collapse-survivors`), a positive integer defaulting to `4096` and validated at load time against the range `[MAX_COLLAPSE_SURVIVORS_MIN, MAX_COLLAPSE_SURVIVORS_MAX]` = `[1, 1048576]`. When the set is at the cap and a further survivor must be recorded, the merge SHALL evict the **oldest-arrived** survivor to make room, SHALL emit exactly **one** WARN per merge run naming the cap, and SHALL continue — it SHALL NOT retain beyond the cap, fail, or drop any record from the **output**. Past the cap, collapsing SHALL be best-effort: a duplicate whose partner has been evicted SHALL be emitted rather than suppressed. This bound SHALL hold independently of `merge.collapse_window_us` and of whether the input is monotonic.
**Rationale**: L2-MRG-007's window bounds retention in **time**; it cannot bound it in **count**. A window admits however many records fall inside it, and that number is a property of the data: a corrupt or misconfigured recording whose timestamps all decode to one value (all-zero timestamp words being the common case) puts *every* record inside a single window, and so does a legitimately dense bus under a wide operator-set `collapse_window_us`. Fixing the order-independence bug of L2-MRG-007 alone would therefore have left the guarantee conditional on the data being well-behaved, which is exactly the assumption L1-ROB-001 exists to remove.

This is the same argument, and deliberately the same default, as L2-WRT-022 makes for the canonical-order stage — the two are now the only pipeline stages whose memory is a function of the data rather than of the file count, and an operator who has reasoned about one should not have to re-derive the other. Evicting the oldest arrival (rather than, say, the most distant timestamp) is the honest choice once the window is over-full: on input this badly behaved there is no "least useful" survivor to identify, and arrival order is the one ordering still meaningful. Degrading to best-effort collapsing rather than failing keeps a pathological file decodable, matching how the reader already degrades in lenient mode; the single WARN per run tells the operator the guarantee was suspended without flooding a log that would otherwise carry one line per record.
**Verification Method**: Test (T)

---

## L2-CONF: Cross-implementation conformance

#### L2-CONF-001

**Parent**: L1-CONF-001
**Statement**: Shared conformance inputs SHALL be stored as reviewable hexadecimal text rather than committed `.mie` binary recordings.
**Rationale**: Hex text is reviewable in PR diffs; committed binaries are opaque and grow the repository unnecessarily. The conformance runner converts hex to binary at execution time.
**Verification Method**: Inspection (I)
**Evidence**: `scripts/repo-hygiene.sh` — its no-MIE-recordings-tracked check scans the whole tracked tree and fails the repo-hygiene CI job on any committed binary recording; `.githooks/pre-commit` applies the same check to staged blobs. The rule is mechanically enforced rather than reviewed for.

#### L2-CONF-002

**Parent**: L1-CONF-001
**Statement**: The conformance runner SHALL invoke both maintained CLIs and require byte-identical CSV output.
**Rationale**: Byte-identical output is the only contract that prevents silent drift between implementations. "Almost identical" allows trailing whitespace or rounding differences that compound over time.
**Verification Method**: Test (T)

#### L2-CONF-003

**Parent**: L1-CONF-001
**Statement**: Each implementation's output SHALL match the checked-in CSV oracle.
**Rationale**: The oracle is the third party in the diff — it ensures both implementations agree with a frozen expected output, not just with each other.
**Verification Method**: Test (T)

#### L2-CONF-004

**Parent**: L1-CONF-001
**Statement**: Expected CSV oracles SHALL be updated only after both implementations agree.
**Rationale**: Updating the oracle to match one implementation while the other still differs would silently de-couple them. Both must agree before the oracle moves.
**Verification Method**: Inspection (I)
**Evidence**: `tests/conformance/run.py` — every fixture is decoded by **every** implementation under test and byte-compared against the one shared oracle, so an oracle moved to match a single implementation fails on the others; `--update-expected` refuses to run unless all of them are present. The `conformance` job in `.github/workflows/ci.yml` runs Rust and Python on Linux and Windows, and `cpp-conformance` in `.github/workflows/cpp-ci.yml` runs the C++ binary against the same oracles on both; all of them block the merge. The requirement is a process rule, but the process cannot be violated silently: the enforcement is that no single implementation can ratify an oracle change on its own.

#### L2-CONF-005

**Parent**: L1-CONF-001
**Statement**: CI SHALL run the conformance suite on every push and pull request.
**Rationale**: The whole point of having a conformance suite is to catch drift before merge. Running it post-merge would let drift land in `main`.
**Verification Method**: Inspection (I)

#### L2-CONF-006

**Parent**: L1-CONF-001
**Statement**: Each maintained implementation SHALL expose a documented public library API for programmatic (non-CLI) use, with its primary decode entry point importable from the package/crate root.
**Rationale**: Both implementations are maintained as embeddable libraries, not only as CLIs; downstream code SHALL be able to depend on either implementation's decode entry point from the root without reaching into internal modules. A typed, root-level public surface kept intentional — rather than incidental to module layout — is what keeps the two implementations interchangeable for embedders, the same way the conformance suite (L2-CONF-002..005) keeps their CSV output interchangeable. The per-implementation realizations are pinned by L3-PY-007 (Python) and L3-RS-013 (Rust).
**Verification Method**: Test (T)
