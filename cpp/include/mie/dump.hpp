// SPDX-License-Identifier: Apache-2.0
//
// Hex dump: the diagnostic view of a recording (L2-CLI-009, L2-CLI-013).
//
// Mirrors `rust/src/dump.rs` and `python/src/aero1553/dump.py`, closely
// enough that the three outputs diff cleanly.
//
// Two modes:
//
//   * RAW -- the classic hex+ASCII view of any byte range. It parses nothing,
//     which is the point: it is what an operator reaches for when the decoder
//     has already refused the file and the question is "what is actually in
//     there?"
//
//   * RECORD-AWARE -- walks records, printing a decoded header (Type Word,
//     timestamp, Command Word, and for an errored record the Error Word and its
//     DDC description) above each record's bytes.
//
// THIS MODULE RUNS ON FILES THE DECODER HAS ALREADY GIVEN UP ON. That shapes
// every decision in it: a record it cannot classify degrades to a label rather
// than raising, a scan that hits something impossible prints why and stops
// rather than propagating, and no anomaly here is ever an error the caller has
// to handle. A diagnostic tool that refuses to run on a broken file is no use
// at precisely the moment it is needed.
//
// The three scan-stop anomalies -- an invalid `word_count`, a record whose
// declared extent runs past EOF, and an offset overflow -- are each written
// INLINE into the report and ALSO logged (L2-CLI-013). Both, because the report
// may be piped somewhere the log is not, and the log may be watched by someone
// who never sees the report.
//
// Reading, not mapping. The dump loads the whole file, because it must be able
// to show a file the reader would reject outright, and because the ranges an
// operator asks for are small and arbitrary rather than a streaming walk.

#ifndef MIE_DUMP_HPP
#define MIE_DUMP_HPP

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>

#include "mie/optional.hpp"

namespace mie {
namespace dump {

/// Write a raw hex+ASCII dump of `[offset, offset + length)` to `out`.
///
/// `length` absent means "to end of file". Both bounds are operator-supplied
/// and are CLAMPED rather than validated: an offset past the end, or a length
/// that would overflow, yields an empty dump. Refusing would be unhelpful --
/// "your offset is past the end" is exactly what the empty output already says,
/// and the operator is exploring.
///
/// Throws MieError for a missing, empty or unreadable input, or a write
/// failure. A broken pipe is reported as such so the CLI can treat
/// `dump | head` as the success it is (L2-WRT-018).
void hex_dump_raw(const std::string& path, std::size_t offset, const Optional<std::size_t>& length,
                  std::FILE* out);

/// Write a record-aware dump to `out`, starting at `offset`.
///
/// `max_records` absent means "until the scan stops". The scan stops at the
/// first anomaly rather than trying to resynchronise: `decode`'s recovery walk
/// is the tool for reading past corruption, and a dump that silently skipped
/// ahead would misrepresent where the damage begins -- which is the one thing
/// this view exists to show.
void hex_dump_records(const std::string& path, const Optional<uint64_t>& max_records,
                      std::size_t offset, std::FILE* out);

}  // namespace dump
}  // namespace mie

#endif  // MIE_DUMP_HPP
