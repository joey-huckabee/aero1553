// SPDX-License-Identifier: Apache-2.0
//
// Record alignment, validation, and sync recovery.
//
// PURE FUNCTIONS ONLY: no logging, no I/O, no allocation beyond a returned
// string. This is a rule, not an observation -- the same rule the Rust and
// Python sync modules follow, and CLAUDE.md records why breaking it went wrong
// before: these helpers lack the caller's context, so they narrate outcomes
// wrongly. `find_first_record` returning "nothing" is the *expected* result for
// a valid empty recording, and logging "no valid record found" there contradicts
// the reader's own correct message. The reader decides what to say.
//
// Mirrors `rust/src/sync.rs` (which the Python package also runs).

#ifndef MIE_SYNC_HPP
#define MIE_SYNC_HPP

#include <cstddef>
#include <cstdint>

#include "mie/models.hpp"
#include "mie/optional.hpp"

namespace mie {
namespace sync {

/// 64 KB scan cap. Large enough for any reasonable header or corruption gap,
/// small enough that a runaway scan cannot walk a multi-gigabyte file.
const std::size_t MAX_SCAN_BYTES = 65536;

/// The word-count field is six bits, so 63 words -- 126 bytes -- is the largest
/// record the format can describe.
const std::size_t MAX_RECORD_BYTES = 126;

/// L2-SYN-026 look-ahead depth: the TOTAL number of records checked, including
/// the candidate. 1 means no look-ahead; 2 means one follower.
///
/// Deliberately left at 2. Raising it does reject more non-MIE input -- ordinary
/// prose yields a 2-record chain by chance often enough to matter -- but the
/// depth applies to ENTRY decisions only. Continuous validation of an
/// already-locked chain does no look-ahead at all, because a well-formed record
/// must never be discarded on account of its successor (L2-SYN-005). Operators
/// who want stricter wrong-input screening raise it per invocation.
const std::size_t DEFAULT_LOOKAHEAD_RECORDS = 2;

/// Why a candidate failed validation.
///
/// The boolean `validate_record` remains for callers that only need yes/no;
/// this exists so the reader and diagnostic tooling can say *which* rule failed
/// without reimplementing the rules.
enum ValidationFailure {
    VALIDATION_OK,
    VALIDATION_TYPE_WORD_UNREADABLE,
    VALIDATION_UNKNOWN_MESSAGE_TYPE,
    VALIDATION_INVALID_WORD_COUNT,
    VALIDATION_RECORD_TRUNCATED,
    VALIDATION_IRIG_HOUR_OUT_OF_RANGE,
    VALIDATION_IRIG_MINUTE_OUT_OF_RANGE,
    VALIDATION_IRIG_SECOND_OUT_OF_RANGE,
    VALIDATION_IRIG_MICROSECOND_OUT_OF_RANGE,
    VALIDATION_IRIG_DAY_OUT_OF_RANGE,
    VALIDATION_LOOKAHEAD_UNKNOWN_MESSAGE_TYPE,
    VALIDATION_LOOKAHEAD_INVALID_WORD_COUNT
};

/// Operator-facing phrasing, matching the Rust Display text word for word --
/// DEBUG log lines carry it and conformance compares stderr.
const char* validation_failure_text(ValidationFailure failure);

/// Where the next valid record starts, and how many bytes were skipped to
/// reach it.
struct ScanHit {
    std::size_t offset;
    std::size_t skipped;

    ScanHit();
    ScanHit(std::size_t offset, std::size_t skipped);
};

/// True when a valid record starts at `offset`, applying every heuristic
/// including the N-record look-ahead (L2-SYN-005, L2-SYN-026).
///
/// `ts_format` absent means "not yet determined", which selects the smaller
/// (Standard) minimum word count so an unknown format stays permissive.
bool validate_record(const uint8_t* data, std::size_t size, std::size_t offset,
                     std::size_t file_len, const Optional<TimestampFormat>& ts_format,
                     std::size_t lookahead_records);

/// Validate and report the precise failure.
///
/// This form honours the L2-SYN-028 end-of-records terminator: a null Type Word
/// at the candidate's next-record boundary CONFIRMS the candidate as the last
/// record rather than rejecting it. That is correct on the trusted-boundary
/// paths -- forward decode and first-record detection -- and wrong for
/// recovery, which is why `recover_sync` does not use it. See that function.
ValidationFailure validate_record_detailed(const uint8_t* data, std::size_t size,
                                           std::size_t offset, std::size_t file_len,
                                           const Optional<TimestampFormat>& ts_format,
                                           std::size_t lookahead_records);

/// Byte offset of the first valid record.
///
/// Scans on a 2-byte grid from 0. Returns immediately when offset 0 validates
/// (the common case -- MIE files have no header); otherwise returns the offset
/// just past whatever preceded the records. False when nothing valid is found
/// within the scan window, which is the EXPECTED outcome for an empty
/// recording, not an error -- the caller decides.
bool find_first_record(const uint8_t* data, std::size_t size, std::size_t file_len,
                       const Optional<TimestampFormat>& ts_format, std::size_t max_scan,
                       std::size_t lookahead_records, ScanHit& out);

/// Consecutive candidate records sampled for the L2-SYN-018 defence.
const std::size_t HOMOGENEITY_SAMPLE_RECORDS = 4;

/// L2-SYN-018: is this a pathological single-byte pad rather than a recording?
///
/// Compares the next HOMOGENEITY_SAMPLE_RECORDS chunks, ignoring the timestamp
/// triple. If they are byte-identical everywhere else, the input is almost
/// certainly a fill pattern. The case that motivates it: 0x20-fill, where
/// `0x20 0x20` parses as a valid SPURIOUS_DATA Type Word and look-ahead alone
/// happily admits the whole stream.
///
/// True means the input SHALL be rejected.
bool is_homogeneous_payload(const uint8_t* data, std::size_t size, std::size_t offset,
                            std::size_t record_bytes);

/// L2-RDR-004 diagnostic: find the first Type Word that fails ONLY the length
/// check.
///
/// Called after `find_first_record` finds nothing, to separate "this is not an
/// MIE file at all" (NoValidRecords) from "a valid Type Word is here but its
/// declared extent runs past EOF" (FirstRecordTruncated). The distinction
/// matters to an operator: the second means the recording was cut short, the
/// first means they passed the wrong file.
///
/// Walks the same grid as `find_first_record` but omits the fits-in-file check
/// and the look-ahead, so it matches a Type Word that WOULD have been valid had
/// the file been longer.
bool diagnose_header_scan_failure(const uint8_t* data, std::size_t size, std::size_t file_len,
                                  const Optional<TimestampFormat>& ts_format, std::size_t max_scan,
                                  std::size_t& out_offset, std::size_t& out_record_bytes,
                                  std::size_t& out_available);

/// Walk forward from `offset` looking for the next valid record, after a
/// mid-file validation failure.
///
/// Uses STRICT look-ahead -- the terminator is not honoured. A mis-aligned
/// candidate whose declared length happens to land its boundary on a zero
/// *data* word must not validate as a bogus "last record before the
/// terminator". Recovery requires a real follower, or EOF.
bool recover_sync(const uint8_t* data, std::size_t size, std::size_t offset, std::size_t file_len,
                  const Optional<TimestampFormat>& ts_format, std::size_t max_scan,
                  std::size_t lookahead_records, ScanHit& out);

}  // namespace sync
}  // namespace mie

#endif  // MIE_SYNC_HPP
