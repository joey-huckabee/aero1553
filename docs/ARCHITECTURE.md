# Aero1553 Architecture

**Document ID:** MIE-ARCH-001
**Version:** 2.0.0

How the decoder is organized to turn an MIE binary recording into vendor-compatible CSV. Covers the module structure, the cross-implementation correspondence between the Rust, Python and C++ trees, the synchronization strategy, the error pipeline, the structural-invariants subsystem, the output-safety machinery, and the streaming-vs-buffered trade-offs.

Companion docs: [`MIE-FORMAT.md`](MIE-FORMAT.md) (binary format reference), [`ERROR-CATALOG.md`](ERROR-CATALOG.md) (every error class), [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) (how to add things), [`L1-REQ.md`](L1-REQ.md) / [`L2-REQ.md`](L2-REQ.md) / [`L3-REQ.md`](L3-REQ.md) (the spec).

---

## 1. Three implementations, one architecture

Aero1553 ships as a Rust crate (`rust/src/`), a Python package (`python/src/aero1553/`) and a C++ implementation (`cpp/`). They are independent implementations that satisfy the same shared specification and produce byte-identical CSV output (verified by the cross-implementation conformance suite under `tests/conformance/`). The module structure is intentionally aligned so the architecture description fits all three.

> **The port is complete.** Every concern below is implemented in all three, and the C++ build passes the whole shared conformance manifest on Linux and Windows. The prose in the rest of this document is written against Rust and Python for readability; where it names a Rust module, the C++ and Python equivalents in the same row behave identically unless the text says otherwise.

| Concern | Rust module | Python module | C++ module |
|---------|-------------|---------------|------------|
| CLI / argument parsing | `rust/src/cli.rs` | `python/src/aero1553/cli.py` | `cpp/src/cli.cpp` |
| TOML configuration loader | `rust/src/config.rs` | `python/src/aero1553/config.py` | `cpp/src/config.cpp` + `cpp/src/toml.cpp` |
| Message filtering | `rust/src/filter.rs` | `python/src/aero1553/filters.py` | `cpp/src/filter.cpp` |
| Canonical row order (equal-timestamp ties) | `rust/src/order.rs` | `python/src/aero1553/order.py` | `cpp/src/order.cpp` |
| Reader pipeline (mmap → records) | `rust/src/reader.rs` | `python/src/aero1553/reader.py` | `cpp/src/reader.cpp` |
| Multi-file time-sorted merge | `rust/src/merge.rs` | `python/src/aero1553/merge.py` | `cpp/src/merge.cpp` |
| Per-RT/MSG `DELTA` tracking | `rust/src/delta.rs` | `python/src/aero1553/delta.py` | `cpp/src/delta.cpp` |
| Pure decode (bit-level field extraction) | `rust/src/decode.rs` | `python/src/aero1553/decode.py` | `cpp/src/decode.cpp` |
| Sync helpers (validate, find first, recover) | `rust/src/sync.rs` | `python/src/aero1553/sync.py` | `cpp/src/sync.cpp` |
| Domain models + error code constants | `rust/src/models.rs` | `python/src/aero1553/models.py` | `cpp/src/models.cpp` |
| Error types | `rust/src/error.rs` (single enum) | `python/src/aero1553/exceptions.py` (class hierarchy) | `cpp/src/error.cpp` (single enum + kind) |
| CSV writer | `rust/src/writer.rs` (streaming) | `python/src/aero1553/writer.py` (streaming, stdlib `csv`) | `cpp/src/writer.cpp` (streaming) |
| Logging | `rust/src/log.rs` (hand-rolled) | `python/src/aero1553/logger.py` (stdlib `logging`) | `cpp/src/log.cpp` (hand-rolled) |
| Hex dump | `rust/src/dump.rs` | `python/src/aero1553/dump.py` | `cpp/src/dump.cpp` |

The sync helpers (`sync.rs` / `sync.py` / `sync.cpp`) are **pure** in all three implementations — no logging, no I/O. Everything an operator sees about header detection, sync loss, and recovery is emitted by the reader, which is what keeps the two implementations' log output aligned and stops a helper from narrating an outcome the caller has more context about.

Per L1-CONF-001 all three implementations must remain aligned on shared format and CSV semantics. Per-implementation requirements (`L3-PY-*` / `L3-RS-*` / `L3-CPP-*`) cover the technology-specific obligations (the compiled Rust extension for Python; memmap2 / streaming `BufWriter` for Rust). See [`L3-REQ.md`](L3-REQ.md) for the per-impl details.

The `MUX` column value (L2-WRT-020) is resolved **once per input file** from its name when the reader is constructed (config → `ReaderOptions` / reader kwargs), and attached to every `MieMessage` the reader yields — Rust as a shared `Arc<str>` (a refcount-bump clone per record), Python as a shared `str` reference. The value therefore rides along through the filter and merge iterators unchanged (so a merged stream keeps each record's source-file MUX), and the writer emits it without any extra per-record allocation — preserving the O(1)-in-record-count streaming guarantee.

---

## 2. Module dependency diagram (Rust shape; Python has the same topology)

```
┌──────────────────────────────────────────────────────────────┐
│                  rust/src/bin/aero1553.rs                    │
│                (delegates to cli::run(argv))                 │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                          cli.rs                              │
│      (hand-rolled argparse: decode / count / dump)           │
└──────┬──────────┬──────────┬──────────┬──────────────────────┘
       │          │          │          │
       ▼          │          │          ▼
┌──────────┐      │          │  ┌──────────────┐
│  log.rs  │      │          │  │  config.rs   │
│ stderr   │      │          │  │ TOML loader  │
│ logger   │      │          │  │DecoderConfig │
└──────────┘      │          │  └──────────────┘
                  │          ▼
                  │  ┌──────────────┐
                  │  │  filter.rs   │
                  │  │ FilterConfig │
                  │  │ + Iter adptr │
                  │  └──────┬───────┘
                  │         ▼
                  │  ┌──────────────┐
                  │  │  order.rs    │
                  │  │ canonical    │
                  │  │ RT/MSG order │
                  │  │ + Iter adptr │
                  │  └──────────────┘
                  ▼
          ┌───────────────┐      ┌───────────────────┐
          │  reader.rs    │      │    writer.rs      │
          │ MieFileReader │─────▶│ streaming CSV     │
          │ (Iterator)    │      │ + atomic commit   │
          └───────┬───────┘      │ + .partial path   │
                  │              └───────────────────┘
          ┌───────┴───────┐
          │               │
          ▼               ▼
  ┌───────────────┐ ┌────────────────────────┐
  │  decode.rs    │ │       sync.rs          │
  │ pure decode   │ │ validate_record,       │
  │ + classify    │ │ find_first_record,     │
  │ + invariants  │ │ recover_sync,          │
  │ + Severity    │ │ diagnose_header_scan,  │
  └───────┬───────┘ │ is_homogeneous_payload │
          │         └────────────────────────┘
          ▼
  ┌───────────────────────────────┐    ┌────────────────────┐
  │          models.rs            │    │     delta.rs       │
  │  Enums, structs, DataWords,   │    │ DeltaTracker       │
  │  DDC + decoder error consts   │    │ ONE key definition │
  └───────────────┬───────────────┘    │ shared by reader.rs│
                  │                    │ and merge.rs       │
                  │                    └────────────────────┘
  ┌───────────────┴───────────────┐
  │          error.rs             │    ┌───────────┐
  │  MieError enum + Display      │    │  dump.rs  │
  │  + MieErrorKind discriminant  │    │ hex dump  │
  └───────────────────────────────┘    └───────────┘
```

The only external Rust runtime dependency is `memmap2` (per `L3-RS-002`). Argument parsing, CSV emission, TOML parsing, logging, and error types are all hand-rolled. The Python package's CSV writer and config loader are the Rust ones, run through its compiled extension (`L3-PY-004`, `L3-PY-005`), so the package declares no runtime dependency.

---

## 3. Synchronization strategy

The reader maintains sync through a four-phase approach, all sharing the same validation path per L2-SYN-014.

### Phase 1 — Initial alignment (header detection)

Before decoding begins, `find_first_record()` scans from offset 0 to find the first position that passes the full validation path. This handles:

- Files starting directly with records (offset 0 returned immediately).
- Files with proprietary DDC headers containing ASCII equipment names, configuration data, or padding bytes.

The scan advances in 2-byte (word-aligned) steps and caps at 64 KB (`MAX_SCAN_BYTES`, per L1-SYN-002 / L2-SYN-007).

When `find_first_record` returns None, the reader calls `diagnose_header_scan_failure` (sync module) to distinguish two cases (per L2-RDR-004):

- **`HomogeneousPayload`** — if `is_homogeneous_payload` reports byte-identical candidate records, the file is a single-byte pad (e.g., 0x20-fill that happens to parse as a SPURIOUS_DATA stream). Both modes reject.
- **`FirstRecordTruncated`** — if there's a structurally-valid Type Word at or after the header but its declared extent runs past EOF, surface this distinct class. Strict mode raises; lenient mode terminates cleanly with zero records emitted.
- **`NoValidRecords`** — otherwise the file isn't an MIE recording at all. Both modes raise.

### Phase 2 — Continuous validation

At each record boundary, the reader validates before decoding **using the same sync validation rules as Phases 1 and 4**. There is one validation path; the per-record loop, the header-skip scan, and the recovery walk all share it (L2-SYN-014). A weaker per-record check would let corrupt-but-plausible records pass through and emit garbage rows.

The public boolean `validate_record` API remains the compatibility surface for
scanners. The additive `validate_record_detailed` API returns a
`ValidationFailure` reason so readers can report a precise strict-mode failure
without duplicating validation logic. At DEBUG level, a validation failure also
emits one context line capped at 32 bytes.

### Phase 3 — N-record look-ahead

`validate_record` uses an N-record look-ahead: a candidate is confirmed valid only if the next `N − 1` records each start with a valid Type Word (message type in the known set, word count plausible). The walk advances by each candidate's declared `word_count` so it checks the *next records*, not the next 2-byte positions. This dramatically reduces false positives from coincidental byte patterns. When fewer than 2 bytes remain at any look-ahead position, the walk terminates without rejecting the original candidate — checks 1–5 alone are authoritative for records that don't exist in the file (L2-SYN-005, L2-SYN-026).

The format has no per-record sync marker, so the Type Word's `word_count` is the only framing; the look-ahead turns the redundancy of consecutive self-consistent lengths into a *synthetic* sync check. `MIE-FORMAT.md` §2.3 ("Why the look-ahead exists") is the deep rationale — the false-positive math, the chaining argument, and why the end-of-records terminator is accepted as an end-of-chain on the forward paths but not during recovery.

The look-ahead depth `N` is configurable via the `decode.lookahead_records` TOML key or the `--lookahead-records` CLI flag, range `[1, 32]`, default `8` (raised from `2` in v2.12.0). Higher values catch wider classes of consecutive-same-shape corruption — and, more importantly, wrong-input false positives: ordinary prose contains chains that satisfy `N = 2` (every Markdown file in this repo decoded "successfully" at that depth), while `N = 8` rejects all but one of them. The cost is small (one Type Word read per extra look-ahead record).

### Phase 4 — Sync recovery (walk forward)

If validation fails mid-file, `recover_sync` scans forward in 2-byte steps until it finds a valid record. If recovery fails within the per-recovery scan window (64 KB), the reader yields a terminal `Err(MieError::UnrecoverableSyncLoss)` / raises `MieUnrecoverableSyncLossError` (L1-EXIT-004). The cumulative recovery scan distance across a full decode is bounded by the file size — recovery scans don't re-traverse already-scanned bytes (L1-SYN-002).

### Validation heuristics (applied in order, fast checks first)

1. Type Word message type (bits 0–6) ∈ known set (L2-SYN-001).
2. Word count ∈ `[min_wc, 63]` (L2-SYN-002).
3. Record does not extend past EOF (L2-SYN-003).
4. IRIG timestamp fields in valid ranges; freerun exemption for day-of-year (L2-SYN-004 / L2-SYN-019).
5. Next record's Type Word also has valid type and word count (L2-SYN-005).

Plus the post-acceptance homogeneity defense (`is_homogeneous_payload`, L2-SYN-018) applied once at header detection.

### Performance

- All checks use O(1) bit operations on 16-bit words.
- No allocations during scanning.
- Look-ahead reads only 2 bytes (the next Type Word).
- Scan advances 2 bytes per step (word-aligned).
- Maximum scan distance caps at 64 KB per recovery.

### Error records, SPURIOUS_DATA, and sync

Error records (Type Word bit 14 set) and SPURIOUS_DATA records are valid records with valid Type Words — they pass sync validation normally (L2-SYN-017) and serve as eligible recovery anchor points. Sync loss only occurs when the DDC card writes truly corrupt data (truncated mid-word, power loss during recording).

---

## 4. Structural invariants subsystem

Beyond the five sync validation checks, the reader applies seven **structural invariants** to every decoded record (L2-SYN-020 through L2-SYN-025, plus L2-SYN-027). These catch corruption patterns where the Type Word + word count are structurally valid but the record's internal fields contradict each other.

Invariants are classified into two severity classes:

| Severity | Strict mode | Lenient mode | Used for |
|----------|-------------|--------------|----------|
| `Reject` | Surface `MieError::PayloadError` and stop | Log WARN and skip the record (advance past it without emission) | Internally inconsistent records that almost certainly indicate corruption |
| `AnomalyWarn` | Log WARN and continue emitting the record | Same | Patterns that may be legitimate (real-bus noise, undocumented vendor extensions) so outright rejection produces false negatives |

The seven invariants:

| ID | Severity | What it catches |
|----|----------|-----------------|
| L2-SYN-020 | Reject | Type `0x02` (BC→RT) with Cmd direction = Transmit |
| L2-SYN-021 | Reject | Type `0x04` (RT→BC) with Cmd direction = Receive |
| L2-SYN-022 | Reject | Type Word `word_count` too small for the Cmd Word's declared payload |
| L2-SYN-023 | Reject | RT-to-RT records (`0x08` / `0x18`) where the second Cmd Word's direction isn't Receive |
| L2-SYN-024 | AnomalyWarn | Status Word's RT field doesn't match the Cmd Word's RT (possible multi-drop bus interference) |
| L2-SYN-025 | AnomalyWarn | Type Word bit 15 (reserved) is set (possible undocumented vendor extension) |
| L2-SYN-027 | Reject | RT-to-RT records where Cmd1 and Cmd2 disagree on `data_word_count`. Checked post-extract: the L2-SYN-022 capacity invariant is computed from Cmd1 alone and cannot see a Cmd2 that over-claims. |

Implementation: a `WhichInvariant` enum names which specific invariant fired; the reader logs an L2-SYN diagnostic line containing the offset, the invariant name, and the raw bytes. The same enum is exposed in both crates so callers can branch on the specific failure rather than parsing the diagnostic string.

---

## 5. Error handling pipeline

```
  Record at offset N
        │
        ├── validate_record(N) fails?
        │       │
        │       ├── strict ──── raise (UnknownTypeWord / InvalidTypeWord /
        │       │                       RecordTruncated / PayloadError)
        │       └── lenient ──  recover_sync ──── if exhausted:
        │                                            raise UnrecoverableSyncLoss
        │
        ├── Type Word bit 14 = 1? ──── Error record path:
        │       │                        1. Last word = Error Word (DDC code)
        │       │                        2. Validate code against known set
        │       │                        3. Payload = truncated data words
        │       │                        4. Set prev_was_error = True
        │       │                        5. Yield MieMessage(error_word=code)
        │       │                                ↓
        │       ▼
        │  Record at offset N + record_bytes
        │       │
        │       ├── Type = 0x20 (SPURIOUS_DATA)?
        │       │       │
        │       │       ├── prev_was_error = True  → error_word = 0x2000 (continuation)
        │       │       └── prev_was_error = False → error_word = 0x2001 (standalone)
        │       │
        │       └── Type ≠ 0x20 ── reset prev_was_error, normal decode
        │
        └── Normal record:
                │
                ├── validate_structural_invariants (L2-SYN-020..023) ──── if fails:
                │       strict → raise PayloadError("L2-SYN-020..025: ...")
                │       lenient → log WARN, skip record (advance, continue)
                │
                ├── extract payload per message format
                │
                ├── validate_post_extract_invariants (L2-SYN-023 Cmd2 direction,
                │       L2-SYN-027 Cmd1/Cmd2 data_word_count agreement)
                │       same strict/lenient policy
                │
                ├── detect_record_anomalies (L2-SYN-024 / 025)
                │       both modes log WARN; record still emitted
                │
                └── yield MieMessage
```

The "Error record path" branch and the "structural invariants" branch are independent — a record can be both errored (bit 14 set) AND fail an invariant. The reader checks bit 14 first; an errored record skips the invariant checks because its payload is truncated by definition.

For the full per-error-class behavior reference (when each fires, strict vs lenient, exit code), see [`ERROR-CATALOG.md`](ERROR-CATALOG.md).

---

## 6. Output safety subsystem

L1-OUT-002 obligates the writer to preserve output destination integrity: atomic writes, refuse-to-overwrite-input, partial cleanup on failure. The implementation:

```
write_csv(messages, dest_path, opts)
  │
  ├── L2-WRT-014: refuse if ANY commit target (dest, <stem>_errors<sfx>,
  │                and their .partial variants) resolves to ANY input
  │       (compared via canonical path; stdout is exempt)
  │
  ├── L2-WRT-017: if opts.no_clobber and dest_path exists → ClobberRefused
  │       (an EARLY report only -- the guarantee is at the commit, below)
  │
  ├── L2-WRT-015: exclusively create a unique temp file
  │       <dest>.aero1553.tmp.<pid>.<counter>.<nanos> on the SAME
  │       filesystem as dest_path (so rename is atomic). Per L3-WRT-001.
  │
  ├── stream rows through BufWriter<File> wrapped around the temp
  │
  ├── normal completion → commit_onto(dest_path)
  │
  └── failure path:
        ├── opts.allow_partial?
        │   └── yes → commit_onto(<dest_path>.partial); leave dest untouched.
        │            Exit class: complete (allow_partial). Per L3-WRT-002.
        │
        └── no → unlink(temp); leave dest untouched.
                Exit class: partial-unrecoverable (exit 3).

commit_onto(target)          ← ONE routine, both entry points (L3-WRT-005)
  │
  ├── flush + close the temp        ┐ all three stages are part of the
  ├── move it onto `target`         ┘ commit for error classification
  │                                   (L2-WRT-024): any failure is a
  │                                   writer error, the destination is
  │                                   untouched, no temp is left behind
  │
  └── the move honours the commit mode, fixed at construction:
        ├── Replace   (default)   rename / MoveFileEx(REPLACE_EXISTING)
        └── NoReplace (--no-clobber, L2-WRT-023)
              ├── Win32:  MoveFileExW with NO flags -- natively atomic
              │           and non-replacing; ERROR_ALREADY_EXISTS is the
              │           refusal. Works on FAT. No fallback needed.
              └── POSIX:  link(2) (fails EEXIST) then unlink the temp;
                          on a filesystem with no hard links, fall back
                          to an O_EXCL reservation plus a rename over
                          our own zero-byte file.
```

The atomic temp+rename is what guarantees a crash or kill mid-decode never produces a half-written destination file. The output destination is touched exactly once, by the final move, and only when decoding completed cleanly. The L2-WRT-015 cleanup path also covers strict-mode errors raised during decoding — the temp file is unlinked before the exception propagates.

**Why `--no-clobber` is enforced at the commit and not at the pre-flight (L2-WRT-023).** `exists()` answers a question about the past. Between that answer and the rename, any other process may create the destination — and a replacing rename then destroys it. Two concurrent runs of the same command both pass the pre-flight, and the second silently overwrites the first's output, which is the exact outcome the flag exists to prevent. Moving the refusal into the move is what closes the window, because that is the only point at which the filesystem can make it atomic.

The same change is what finally covers the `.partial` targets. They are never pre-flighted, deliberately — a stale `<dest>.partial` must not refuse a run that was never going to write one — so before this they were overwritten unconditionally even under `--no-clobber`. Routing both commit entry points through `commit_onto` means the rule reaches every target by construction rather than by remembering to apply it twice.

No portable single call does this, which is why the requirement is stated over the *property* (atomic, non-replacing) rather than over a syscall: `renameat2(RENAME_NOREPLACE)` needs a glibc newer than the SLES 12 floor, and `std::fs` exposes no no-replace rename at all. Rust reaches it with `std::fs::hard_link`, which maps to `link(2)` and `CreateHardLinkW` and fails on both when the destination exists — one standard-library call spanning both platforms, and no new dependency (L3-RS-002).

Stdout output (L2-WRT-007) bypasses the temp+rename machinery — it's a stream, not a file with a destination — and inherits broken-pipe-on-stdout semantics (L2-WRT-018: exit 0 with no error).

---

## 7. Error type

The Python class hierarchy and the Rust enum are kept in lockstep. Every variant in one language has a counterpart in the other.

### Rust — single `enum MieError`

```
MieError {
    // File-level (open / format-rejection)
    FileNotFound          { path }
    FileEmpty             { path }
    FileIo                { path, source: io::Error }
    NoValidRecords        { path, scan_bytes }
    HomogeneousPayload    { path, offset, sample_records }
    TimestampFormatMismatch { offset, irig_score, std_score, records_probed }
    IncompatibleMergeInputs { file_index, path, detail }
    InputOutputCollision  { path }
    ClobberRefused        { path }

    // Record-level (per-record failures, carry offset)
    InvalidTypeWord       { offset, raw_type_word, word_count }
    UnknownTypeWord       { offset, raw_type_word, message_type }
    RecordTruncated       { offset, record_bytes, available_bytes }
    FirstRecordTruncated  { offset, record_bytes, available_bytes }
    PayloadError          { offset, detail }
    UnknownErrorCode      { offset, error_code }
    UnrecoverableSyncLoss { offset, sync_losses }

    // Merge
    NonMonotonicInput     { file_index, path, prev_us, curr_us }

    // Output
    WriterError           { destination, source: io::Error }
}
```

`MieError::kind()` returns a `MieErrorKind` discriminant for callers that need to branch on the failure mode without matching on the full enum. The `is_record_error()` predicate matches Python's `MieRecordError` exactly; `is_file_error()` is deliberately **narrower** than Python's `MieFileError` — see the mapping note below the Python tree.

### Python — class hierarchy rooted at `Aero1553Error`

```
Aero1553Error                            (base, catches everything)
├── MieFileError
│   ├── MieFileNotFoundError
│   ├── MieFileIoError                   (open/mmap failure; wraps OSError)
│   ├── MieFileEmptyError
│   ├── MieNoValidRecordsError
│   ├── MieHomogeneousPayloadError
│   ├── MieTimestampFormatMismatchError
│   ├── MieIncompatibleMergeInputsError
│   ├── MieInputOutputCollisionError
│   └── MieClobberRefusedError
├── MieRecordError                       (carries `offset`)
│   ├── MieInvalidTypeWordError
│   ├── MieUnknownTypeWordError
│   ├── MieRecordTruncatedError
│   ├── MieFirstRecordTruncatedError
│   ├── MiePayloadError
│   ├── MieUnknownErrorCodeError
│   └── MieUnrecoverableSyncLossError
├── MieNonMonotonicInputError
└── MieWriterError
```

`MieRecordError` and `MieError::is_record_error()` cover exactly the same seven failures — the classes listed above, including `MieUnrecoverableSyncLossError`. (Rust omitted the sync-loss terminal from that predicate until v2.12.0.)

`MieFileError` is **wider** than `MieError::is_file_error()`. The Rust predicate answers "did input I/O fail" and covers only `FileNotFound`, `FileEmpty` and `FileIo`. Python's class additionally covers the whole-file rejections (`NoValidRecords`, `HomogeneousPayload`, `TimestampFormatMismatch`, `IncompatibleMergeInputs`) and the destination guards (`InputOutputCollision`, `ClobberRefused`), for which the Rust side answers `false` to both predicates and callers match on `kind()` instead.

Note that carrying an `offset` is not what makes a failure record-class: `HomogeneousPayload` and `TimestampFormatMismatch` both cite an offset but reject the file as a whole. Both implementations pin the full boundary by test — `every_error_kind_is_deliberately_classified` in `rust/src/error.rs` and `test_every_exception_class_is_classified` in `python/tests/test_exceptions.py` — so a new variant cannot be added without classifying it on both sides.

For per-variant cause / lenient-vs-strict behavior / exit-code mapping, see [`ERROR-CATALOG.md`](ERROR-CATALOG.md).

---

## 8. Error-mode output

```
  --separate-errors:                      inline (default):
  ┌──────────────────────┐                ┌──────────────────────┐
  │  main.csv            │                │  output.csv          │
  │  Normal messages     │                │  All messages        │
  │  ERROR col = empty   │                │  ERROR = ERROR|      │
  │  ERROR_CODE = empty  │                │         SPURIOUS|    │
  └──────────────────────┘                │         empty        │
  ┌──────────────────────┐                │  ERROR_CODE = 0x01xx │
  │  main_errors.csv     │                │              0x20xx  │
  │  Errored + spurious  │                │              empty   │
  │  ERROR = ERROR|      │                └──────────────────────┘
  │         SPURIOUS     │
  │  ERROR_CODE = codes  │
  └──────────────────────┘
```

In separate mode the errors file is **not created** if no error rows occur (lazy creation, per L2-ERR-008). Each file is written atomically *on its own* via temp + rename, but the two files are **committed sequentially, not as a single transaction** — there is no cross-file atomicity (the platforms provide no two-file atomic rename). All three implementations commit **main first, then errors**: if the first (main) commit fails the errors file is still an un-renamed temp and neither file appears, and if the second (errors) commit fails after main is in place the residual file is the primary `main.csv` rather than an orphan errors file. This is a known limitation — acknowledged in every writer — and is *not* all-or-nothing across the two files.

The order holds on **every** path that commits the pair, the `--allow-partial` path included: `<dest>.partial` before `<errors-dest>.partial` (L2-WRT-019). Through v2.16.0 that branch ran the other way round in all three implementations, so a failing main `.partial` rename left an orphan `<dest>_errors.csv.partial` beside no main output — the precise residue the order exists to make impossible. That the branch which got it wrong was the *failure* branch is not a coincidence; it is the branch nobody re-reads.

Stdout output forces inline mode (you can't split stdout into two streams).

---

## 9. Data flow

```
  .mie binary file
        │
        ▼
  find_first_record  ←── sync: header detection (scans 64 KB, returns Option<offset>)
        │
        ├── None? ───── diagnose_header_scan_failure ────┐
        │                                                │
        │   ┌─ HomogeneousPayload → raise (exit 2)      │
        │   ├─ FirstRecordTruncated:                    │
        │   │    strict → raise (exit 1)                │
        │   │    lenient → terminate cleanly            │
        │   └─ otherwise → raise NoValidRecords (exit 2)│
        │                                                │
        ▼                                                │
  detect_timestamp_format ←── decode: IRIG vs Standard   │
        │                                                │
        ▼                                                │
  ┌─── for each record ──────────────────────────────────┐
  │                                                      │
  │  validate_record (5 checks + look-ahead)             │
  │     │                                                │
  │     ├── valid ──── classify_message_format           │
  │     │              + validate_structural_invariants  │
  │     │              + extract_payload                 │
  │     │              + validate_post_extract           │
  │     │              + detect_record_anomalies         │
  │     │              + compute DELTA                   │
  │     │              + yield MieMessage                │
  │     │                                                │
  │     └── invalid ── recover_sync ── scan forward      │
  │                        │                             │
  │                        ├── found → continue          │
  │                        └── exhausted → raise         │
  │                                       UnrecoverableSyncLoss
  │                                                      │
  │  apply_filters  ←── filter: exclude by type/RT/      │
  │                     bus/SA (or include on Rust)      │
  │                                                      │
  │  yield MieMessage                                    │
  └──────────────────────────────────────────────────────┘
        │
        ▼
  order_rows  ←── order: canonical row order (L2-WRT-021).
        │         Buffers one run of consecutive equal-TIME_STAMP
        │         records, stable-sorts it by (RT, subaddress,
        │         direction), pins Command-Word-less records to
        │         their predecessor, capped by max_sort_group
        │         (L2-WRT-022). LAST stage before the writer.
        ▼
  write_csv / write_csv_split  ←── writer: streaming row → BufWriter
        │                          + atomic temp + rename (L2-WRT-015)
        │                          + .partial on allow_partial (L2-WRT-016)
        │                          + no-clobber check (L2-WRT-017)
        │                          + I/O collision check (L2-WRT-014)
        ▼
  .csv output file(s) — exit class summary logged (L1-EXIT-005)
```

---

## 10. Configuration hierarchy

CLI arguments > config file > built-in defaults (L2-CFG-003). Filter arrays merge across the levels rather than replace (L2-CFG-004).

```
  config/default.toml         Built-in reference config
        │
        ▼
  load_config(path)           Parse TOML, validate at load time (L2-CFG-010),
        │                     WARN on unknown keys (L2-CFG-009)
        ▼
  config.with_overrides()     Merge CLI args on top; filter arrays union
        │
        ▼
  DecoderConfig               Final merged, fully-validated config
    ├── log_level             logging.level
    ├── input_time_format           decode.input_time_format
    ├── strict                decode.strict
    ├── error_mode            decode.error_mode
    ├── allow_partial         decode.allow_partial      (L2-WRT-016)
    ├── detect_records        decode.detect_records     (L2-DEC-015)
    ├── lookahead_records     decode.lookahead_records  (L2-SYN-026)
    ├── standard_tick_rate_hz decode.standard_tick_rate_hz (L2-DEC-017)
    ├── filters
    │   ├── exclude_types     filter.exclude_types
    │   ├── exclude_rts       filter.exclude_rts
    │   ├── exclude_buses     filter.exclude_buses
    │   ├── exclude_subaddrs  filter.exclude_subaddresses
    │   └── include_*         (CLI-only; no config key — L3-RS-010 / L3-PY-013)
    ├── output_format         output.format
    ├── no_clobber            output.no_clobber         (L2-WRT-017)
    ├── mux_enabled           mux.enabled               (L2-WRT-020)
    ├── mux_delimiter         mux.delimiter
    ├── mux_field             mux.field
    ├── collapse_duplicates   merge.collapse_duplicates (L2-MRG-007)
    ├── collapse_window_us    merge.collapse_window_us
    └── max_collapse_survivors
                              merge.max_collapse_survivors (L2-MRG-008)
```

An override is applied when it is **present**, not when it is truthy — Rust
models that with `Option<T>` and Python matches it by testing for `None`. A
truthiness test would silently drop a zero-valued override such as
`--input-time-format auto` (`TimestampFormat::Auto == 0`).

For the full schema reference (every key, its type, valid values, validation behavior, CLI override), see [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md).

---

## 11. Logging strategy

Rust's `log.rs` is hand-rolled — no `log` crate, no `env_logger`. A single global `AtomicU8` holds the current level; the `log_debug!`, `log_info!`, `log_warn!`, `log_error!` macros emit to stderr only when the level passes. Python uses the stdlib `logging` module with the same five levels.

The level is set from the CLI `--log-level` flag or the config file's `logging.level`; CLI overrides config (L2-CFG-003).

One message has a switch of its own beside the level: the IRIG day-of-year advisory (L2-LOG-001), off-switchable via `--no-irig-day-advisory` / `[logging] irig_day_advisory`. It lives in the logging module in all three implementations rather than in the reader's options, because it is a diagnostics switch rather than a decode parameter -- applied where the level is applied, so one call covers `decode`, `count` and `dump`.

| Level | What gets logged |
|-------|-----------------|
| DEBUG | Per-record decode trace, CLI parsed arguments, header-skip-zero (`first record at offset 0 (no header)`), record-class details |
| INFO | File open, header detected with size (L2-SYN-012), timestamp format auto-detect, sync recoveries (L2-SYN-013), decode complete with counts, **exit-class summary** (L1-EXIT-005), CSV row counts, progress every 100k msgs, merge duplicate-collapse count (L2-MRG-007), active-filter summary and passed/excluded tally, one-time IRIG day-of-year advisory (L2-LOG-001). |
| WARN | Sync loss (L2-SYN-013), unknown error codes (lenient), freerun timestamps, structural invariant violations (lenient skip), L2-SYN anomalies (L2-SYN-024 status RT mismatch / L2-SYN-025 reserved bit set), non-monotonic timestamps (L2-RDR-017, once per RT/MSG), unclassifiable records (lenient), stdout-forces-inline-mode |
| ERROR | No valid records found, homogeneous-payload rejection, unrecoverable sync loss, file/write failures, first-record truncated (strict) |

Per L2-CLI-006, all diagnostics go to stderr — never mixed into CSV stdout.

---

## 12. Streaming CSV (memory profile)

Both implementations stream rows directly to the output and decode in
constant memory.

| Implementation | Per-record cost | Total memory | Notes |
|----------------|-----------------|--------------|-------|
| **Rust** | ~0 bytes (inline `DataWords` + bounded log buffers) | O(1) in record count | Streams rows directly to a `BufWriter<File>`. The only growable per-decode allocation is `delta_tracker: HashMap<u32, u64>` whose keys are bounded by `RT × SA × direction ≤ 32 × 32 × 2 = 2048`. Tracked as `L3-RS-012`. |
| **Python** | One transient `dict` per row (freed after the row is written) | O(1) in record count | Streams each row to the output handle via the stdlib `csv` module (no DataFrame, no row buffering). The only growable per-decode allocation is the `delta` tracker, keyed identically to Rust. Tracked as `L3-PY-012`. |

For either implementation, decoding a 10 GB recording uses the same memory as decoding a 10 MB recording. The streaming property is load-bearing in **both**: changes to a writer that buffer rows (e.g., a `Vec<Row>` collection step in Rust, or re-materializing a DataFrame in Python) would break `L3-RS-012` / `L3-PY-012` and must be rejected at review.

**The two bounded exceptions.** Exactly two stages hold a buffer whose size is a function of the *data* rather than of the file count. Both are bounded, both degrade rather than fail on overflow, and both defaults are `4096` — deliberately the same number, so an operator who has reasoned about one need not re-derive the other.

**1. The reorder stage (`order.rs` / `order.py`).** Canonical row ordering (L1-OUT-003) holds one run of consecutive equal-`TIME_STAMP` records so it can sort that run. In practice a run is 1–2 records — a 1553 bus carries one transaction at a time, so genuine ties come from the two concurrent buses or from overlapping recorders in a merge. The pathological case is real though: a corrupt recording whose timestamps all decode to the same value would buffer the whole file. That is why the run length is capped by `max_sort_group` (L2-WRT-022, default `4096`), which turns the exception back into a constant — worst case a few hundred kilobytes, independent of record count. On overflow the stage emits the run in arrival order with one WARN and continues; it never grows past the cap and never drops a row.

**2. The de-duplication window (`merge.rs` / `merge.py` / `merge.cpp`).** With `--collapse-duplicates`, the merge retains the survivors inside the collapse window so later records can be compared against them. `collapse_window_us` bounds that set in **time** and cannot bound it in **count**: a window admits however many records fall inside it, and *that* is a property of the data. A recording whose timestamps all decode to one value puts every record inside a single window, and so does a legitimately dense bus under a wide operator-set window. `max_collapse_survivors` (L2-MRG-008, default `4096`) supplies the count bound; at the cap the oldest survivor is evicted, **one** WARN is emitted for the whole run, collapsing continues best-effort, and no row is dropped from the output.

Removing either cap would silently reintroduce an unbounded allocation and must be rejected at review — as would replacing the reorder stage's run buffer with a whole-file sort. Note also that the dedup window's *retention* rule is stated over the retained set (`|survivor_us - current_us| <= collapse_window_us`) rather than as a loop over one end of a deque: after a lenient backward timestamp step the front of an arrival-ordered deque can hold a timestamp in the future of the current record, at which point a one-sided test never evicts it and blocks everything behind it (L2-MRG-007).

### Multi-file time-sorted merge (streaming k-way merge)

> **Status: implemented in v2.1.0** (L1-MRG / L2-MRG), in both implementations
> (`rust/src/merge.rs`, `python/src/aero1553/merge.py`). `decode` accepts more
> than one input via multiple positionals, `--manifest`, or `--glob` (mutually
> exclusive, capped at `MAX_MERGE_FILES = 256`); a single input bypasses the
> merge module and behaves exactly as before.

The feature takes N input recordings and emits a single CSV in global time
order **without** loading all records into memory — preserving the
O(1)-in-record-count guarantee above, at the cost of O(k) where k is the
number of input files.

The mechanism is a **streaming k-way merge**, and it works because of one
property each file already has: a `.mie` recording is written
chronologically as bus traffic occurs, so **within a single file, capture
order already is time order**. We therefore never sort the global record
set — we *merge* k already-sorted streams. A binary min-heap holds exactly
one record per open file (each file's current front record), keyed by
timestamp:

1. Open all k files as lazy `MieFileReader` iterators (with `mmap`, opening
   is cheap — pages fault in on demand, nothing is bulk-read).
2. Prime the heap with the first record from each file (k entries).
3. Pop the minimum-timestamp record, write it to the CSV.
4. Pull the *next* record from the file that record came from, push it.
5. Repeat until every iterator is drained and the heap is empty.

Each pop emits one record in global order; each file advances independently
and only as far as the writer consumes. Resident memory is **O(k) records**
— ~100 record structs (~8 KB) for 100 files — *independent of total record
count*. A merge of 100 files × 10 M records each streams 1e9 records through
a ~100-record heap. Time is O(N log k); for k = 100 that is ~7 comparisons
per record. The intuition: 100 sorted decks fanned face-up — repeatedly take
the lowest visible card and flip the next from that same deck; you only ever
see k cards at once yet produce one fully sorted stream.

The heap is the easy part; the constraints come from the MIE timestamp model
(each pinned by an `L2-MRG-*` requirement in `docs/L2-REQ.md`):

- **Merge key** is `IrigTimestamp::to_total_microseconds()` (Rust) /
  `IrigTimestamp.to_total_microseconds()` (Python) — microseconds from the
  start of the year — with a deterministic `(microseconds, file_index,
  within-file sequence)` tiebreak so records leave the heap reproducibly
  (L2-MRG-002). That tiebreak governs the **heap's internal** order only; the
  equal-timestamp order the CSV actually shows is the canonical `RT`/`MSG` order
  imposed downstream by `order.rs` / `order.py` (L1-OUT-003, L2-WRT-021). A
  richer heap key could not do that job: the heap holds one record per input, so
  it never sees two same-timestamp records from the *same* input at once.
- **Absolute time only.** Standard-format counters (card-local, no shared
  epoch) and freerun IRIG records are not comparable across files; mixed
  formats likewise. Each input's leading record is validated up front; an
  incompatible set is **hard-rejected before any output** with
  `IncompatibleMergeInputs` → exit 6 (L1-EXIT-009 / L2-MRG-003), never
  silently mis-ordered. (A future opt-in `--order file` non-time mode is on
  the ROADMAP.)
- **Global DELTA.** DELTA is recomputed on the merged stream so inter-arrival
  gaps reflect one unified timeline (L2-MRG-005); the sorted stream makes
  per-key gaps non-negative. (A future recorder-context convention may switch
  identified recorders to per-file DELTA — see ROADMAP.)
- **Cross-recorder collapsing (opt-in).** With `--collapse-duplicates`
  (L2-MRG-007), the merge suppresses a record whose wire content (Type /
  Command / Status Words, error word, data words — not the timestamp, MUX, or
  DELTA) matches a recently-emitted record from a **different** input within
  `--collapse-window-us` µs. It runs **before** the Global DELTA stage so a
  dropped duplicate never poisons the per-key tracker, and holds only the
  survivors inside the window (`VecDeque` / `deque`), so resident memory stays
  bounded by the window — preserving the O(k) streaming guarantee. Off by
  default; a file never collapses against itself; a single-file decode is
  unaffected (L3-RS-015 / L3-PY-015). Retention is a property of the retained
  **set** — a survivor is kept iff `|survivor_us - current_us| <=
  collapse_window_us` — not a loop over one end of a container: after a lenient
  backward step (L2-MRG-006) the oldest-arrived survivor can hold a timestamp in
  the *future* of the current record, and a one-sided test then never evicts it.
  The survivor count is separately capped by `max_collapse_survivors`
  (L2-MRG-008, default `4096`), because the window bounds retention in **time**
  and cannot bound it in **count**; see the memory note below.
- **Per-file failure** reuses the single-file `--strict` / lenient /
  `--allow-partial` policy across the batch; `--allow-partial` truncates a
  failed file, finishes from the rest, and the writer commits a combined
  `.partial` (L2-MRG-004).
- **No new dependency.** The heap is `std::collections::BinaryHeap`
  (with `Reverse`) in Rust, the stdlib `heapq` in Python, and
  `std::priority_queue` in C++; `--glob` uses a hand-rolled single-directory
  `*`/`?` matcher with identical semantics in all three (L3-RS-014 /
  L3-PY-014).
- **`--glob` is a grammar, not just a matcher** (L2-MRG-001). "Expand it
  identically" turned out to have three answers per implementation and only one
  of them was about wildcards. What is applied *to* the pattern is now specified
  as tightly as the pattern itself: the split uses the **platform's** separator
  set (a backslash separates on Windows and is an ordinary filename character on
  POSIX), and only entries resolving to a **regular file** are matched, with
  symlinks followed — so a symlinked recording counts, a dangling link does not,
  and a directory named `archive.mie` never does. Each implementation's unit
  tests pin it against its own reading, which is exactly the thing in question
  when two readings differ, so the cross-implementation gate is
  `tests/conformance/glob_parity.py`: it builds a directory holding the entries
  the three disagreed about and compares the **resolved set**.
- **Single year.** IRIG-B carries day-of-year but no year, so the key totally
  orders only within one calendar year; cross-year / New-Year-boundary inputs
  cannot be ordered from the timestamp alone. This intersects the IRIG
  day-field decoding limitation (§/ROADMAP) on the affected card models.
- **Within-file monotonicity** is verified, not just assumed (L2-MRG-006). The
  merge tracks the previous record's key per input and detects a strictly
  backward step in O(1) (capture order broken by sync-loss recovery or a
  day/year rollover). Lenient mode WARNs once per input and still emits every
  record (it never re-sorts — that would defeat the O(k) streaming guarantee);
  strict mode raises `NonMonotonicInput` → exit 1. Detection mirrors the
  single-file non-monotonic-DELTA advisory (L2-RDR-017).
- **File-local error classification.** The `prev_was_error` state that tags
  `SPURIOUS_DATA` as `0x2000` vs `0x2001` is per-file and is resolved inside
  each file's reader *before* merge — never on the merged stream — so a
  spurious record from one file is never mis-attributed to an error in
  another that merely sorts just before it.

---

## 13. Data Words container

`models::DataWords` (Rust) replaces a `Vec<u16>` with a fixed-capacity inline buffer:

```rust
pub struct DataWords {
    buf: [u16; MAX_DATA_WORDS],   // MAX_DATA_WORDS = 32, the MIL-STD-1553B per-transaction maximum
    len: u8,
}
```

This avoids one heap allocation per decoded record. For files with millions of records this is a measurable win. The Python implementation uses a `tuple[int, ...]` (immutable, hashable, slightly cheaper than a list); the structural invariant is the same — at most 32 data words per record (L3-RS-005).

---

## 14. Operational limits

### Concurrent modification of the input (L1-EXIT-006)

The input file is opened with a read-only mmap (L2-RDR-020). POSIX `ftruncate` on a mapped file can produce SIGBUS on subsequent access; Windows generally locks the file against truncate while mapped but mmap does not grow if the file is extended. The operator is responsible for exclusive access for the duration of the decode (L1-EXIT-006 is an operational contract, not a behavior the program can enforce).

### Sync-recovery scan bounds (L1-SYN-002)

Each `recover_sync` invocation scans at most `MAX_SCAN_BYTES` (64 KB) forward from the failed boundary. Across a full decode, cumulative recovery scan distance is bounded by the file size — `recover_sync` starts at the current offset and every successful recovery advances the offset past the scanned region, so the iterator can never re-traverse already-scanned bytes. The combination guarantees terminating iteration even on pathological inputs.

### Fuzz harness (L1-ROB-001)

All three implementations carry a deterministic-PRNG fuzz harness that feeds random byte sequences (32 B - 8 KB each) through the `MieFileReader -> message iterator -> canonical-order` path and asserts that every outcome is either a successfully decoded message or a documented decoder error variant - never a panic, `IndexError`, `struct.error`, an undocumented exception type, or unbounded iteration. All of them use the same xorshift64 PRNG with seed `0x0DDCD1ECDDC0DEC0`, the same size bands and the same draw order, so iteration N is the same bytes everywhere and a failure in one implementation is exactly reproducible against the others.

- Rust: `rust/tests/integration.rs::fuzz_arbitrary_bytes_never_panic` (and `dump_arbitrary_bytes_never_panics`)
- Python: `python/tests/test_e2e.py::TestFuzzHarness` (reader + dump)
- C++: `cpp/tests/test_fuzz.cpp`, tagged `[fuzz]` (reader, dump and merge)

Each harness ends by writing one `FUZZ-SUMMARY` line of counters - inputs, bytes generated, readers opened, records yielded, errors - defined to mean the same thing in all three. Sharing a generator without sharing an assertion means three implementations can each prove "I did not crash" while decoding the same bytes differently; the `fuzz-compare` job in `fuzz.yml` diffs those lines all-pairs across seven runner configurations, and `differential.yml` runs the same comparison on every push. Beyond the totals, `tests/conformance/record_fuzz.py` compares the decoded **CSV bytes** per input: it builds recordings from the committed fixtures, damages them, and drives all three CLIs over each one.

The default-suite iteration count (256) is sized so the harness completes in a few seconds per implementation. `MIE_FUZZ_ITERATIONS` raises it; the scheduled burn-in in `.github/workflows/fuzz.yml` runs 25 000 across all three implementations on both Linux and Windows. See [`FUZZING.md`](FUZZING.md) for the full picture, including what is *not* fuzzed.

---

## 15. Cross-implementation conformance

The conformance suite under `tests/conformance/` materializes hex-text inputs into temporary `.mie` files, invokes both CLIs, and requires byte-identical CSV output (or matching exit code for negative cases). The suite runs in CI on every push and pull request (L1-CONF-001 / L2-CONF-005).

Adding a new case is a four-step operation: hex fixture under `inputs/`, oracle CSV under `expected/` (or `expected_exit` for negative cases), entry in `manifest.json`, run locally to verify cross-impl agreement. See [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) §6 for the full procedure.

The suite is the regression net: every cross-implementation drift caught in the wild becomes a permanent fixture so it can't silently regress.

---

## 16. See also

- [`MIE-FORMAT.md`](MIE-FORMAT.md) — binary format reference (what the decoder consumes).
- [`USER-GUIDE.md`](USER-GUIDE.md) — operator workflow.
- [`EXAMPLES.md`](EXAMPLES.md) — runnable cookbook.
- [`CONFIG-REFERENCE.md`](CONFIG-REFERENCE.md) — TOML schema.
- [`ERROR-CATALOG.md`](ERROR-CATALOG.md) — per-error-class reference.
- [`VENDOR-CSV-DIFFS.md`](VENDOR-CSV-DIFFS.md) — alignment vs vendor CSV.
- [`MAINTAINER-GUIDE.md`](MAINTAINER-GUIDE.md) — how to add things.
- [`L1-REQ.md`](L1-REQ.md) / [`L2-REQ.md`](L2-REQ.md) / [`L3-REQ.md`](L3-REQ.md) — normative spec.
- [`TRACE-MATRIX.md`](TRACE-MATRIX.md) — auto-generated forward trace.
