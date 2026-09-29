"""Tests for the multi-file time-sorted merge (L1-MRG / L2-MRG).

Mirrors the Rust `rust/tests/integration.rs` merge tests so both implementations
exercise the same behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aero1553.exceptions import (
    Aero1553Error,
    MieIncompatibleMergeInputsError,
    MieNonMonotonicInputError,
)
from aero1553.merge import (
    MAX_MERGE_FILES,
    expand_glob,
    glob_match,
    merge_readers,
    read_manifest,
)
from aero1553.models import (
    DeltaScope,
    MieMessage,
    TimestampFormat,
)
from aero1553.reader import MieFileReader
from tests.conftest import RECORD_RT15_SA11_RCV
from tests.fuzz_support import FUZZ_SEED, GLOB_PROBES, fuzz_logging, glob_pattern
from tests.fuzz_support import fill as fuzz_fill
from tests.fuzz_support import iterations as fuzz_iterations
from tests.fuzz_support import summary as fuzz_summary
from tests.fuzz_support import xorshift64 as fuzz_xorshift64


def rt15_record_at(
    day: int,
    hour: int,
    minute: int,
    second: int,
    micro: int,
    freerun: bool = False,
) -> bytes:
    """An RT15 SA11 Receive record at a chosen IRIG instant, by patching the
    timestamp triple of the canonical fixture (bytes 2..8). Mirrors the Rust
    `rt15_record_at` helper."""
    fr = (1 if freerun else 0) << 15
    upper = fr | ((day & 0x1FF) << 5) | (hour & 0x1F)
    middle = ((minute & 0x3F) << 10) | ((second & 0x3F) << 4) | ((micro >> 16) & 0xF)
    lower = micro & 0xFFFF
    ts = upper.to_bytes(2, "little") + middle.to_bytes(2, "little") + lower.to_bytes(2, "little")
    return RECORD_RT15_SA11_RCV[:2] + ts + RECORD_RT15_SA11_RCV[8:]


@pytest.mark.requirement("L1-MRG-001")
@pytest.mark.requirement("L2-MRG-002")
@pytest.mark.requirement("L2-MRG-005")
@pytest.mark.requirement("L3-PY-014")
def test_merge_orders_records_across_files_by_absolute_time(tmp_path: Path) -> None:
    # File A: 100µs, 300µs; File B: 200µs, 400µs (same sec → micros discriminate).
    a = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    b = rt15_record_at(192, 15, 54, 50, 200) + rt15_record_at(192, 15, 54, 50, 400)
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(a)
    fb.write_bytes(b)

    readers = [MieFileReader(fa), MieFileReader(fb)]
    msgs = list(merge_readers(readers))
    assert len(msgs) == 4

    us = [m.timestamp.to_microseconds(None) for m in msgs]
    assert all(us[i] < us[i + 1] for i in range(len(us) - 1)), f"not ordered: {us}"

    # Global DELTA (L2-MRG-005): first occurrence 0.0, then non-negative.
    assert msgs[0].delta == 0.0
    assert all(m.delta is not None and m.delta >= 0.0 for m in msgs[1:])


@pytest.mark.requirement("L2-MRG-001")
def test_merge_single_input_is_unchanged(tmp_path: Path) -> None:
    a = rt15_record_at(192, 15, 54, 50, 10) + rt15_record_at(192, 15, 54, 50, 20)
    fa = tmp_path / "a.mie"
    fa.write_bytes(a)
    msgs = list(merge_readers([MieFileReader(fa)]))
    assert len(msgs) == 2


@pytest.mark.requirement("L1-MRG-002")
@pytest.mark.requirement("L2-MRG-003")
def test_merge_rejects_freerun_leading_input(tmp_path: Path) -> None:
    good = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    freerun = rt15_record_at(0, 0, 0, 0, 0, freerun=True) + rt15_record_at(
        0, 0, 0, 1, 0, freerun=True
    )
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(good)
    fb.write_bytes(freerun)
    # Open the readers outside the `raises` block so only `merge_readers` can
    # satisfy it -- a constructor failure would otherwise pass this test for the
    # wrong reason (S5778).
    readers = [MieFileReader(fa), MieFileReader(fb)]
    with pytest.raises(MieIncompatibleMergeInputsError):
        merge_readers(readers)


@pytest.mark.requirement("L1-MRG-002")
@pytest.mark.requirement("L2-MRG-003")
def test_merge_rejects_standard_format_input(tmp_path: Path) -> None:
    a = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fa = tmp_path / "a.mie"
    fa.write_bytes(a)
    # Forcing Standard makes the records decode as Standard timestamps, which
    # have no shared epoch → not mergeable.
    readers = [
        MieFileReader(fa, input_time_format=TimestampFormat.STANDARD),
        MieFileReader(fa, input_time_format=TimestampFormat.STANDARD),
    ]
    with pytest.raises(MieIncompatibleMergeInputsError):
        merge_readers(readers)


@pytest.mark.requirement("L2-MRG-006")
def test_merge_warns_on_within_file_backward_step(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # One file whose microsecond keys step 100 → 200 → 150 (the third record is
    # older than the second): a within-file backward step. Lenient mode WARNs
    # once and still emits every record (never re-sorts).
    a = (
        rt15_record_at(192, 15, 54, 50, 100)
        + rt15_record_at(192, 15, 54, 50, 200)
        + rt15_record_at(192, 15, 54, 50, 150)
    )
    fa = tmp_path / "a.mie"
    fa.write_bytes(a)
    import logging

    with caplog.at_level(logging.WARNING, logger="aero1553.merge"):
        msgs = list(merge_readers([MieFileReader(fa)]))
    assert len(msgs) == 3, "lenient mode keeps all records despite the WARN"
    backward_warns = [r for r in caplog.records if "not internally time-sorted" in r.getMessage()]
    assert len(backward_warns) == 1, "exactly one backward-step WARN per file"


@pytest.mark.requirement("L2-MRG-006")
def test_merge_strict_fails_on_within_file_backward_step(tmp_path: Path) -> None:
    # The same backward step is a record error in strict mode (exit-1 class).
    a = (
        rt15_record_at(192, 15, 54, 50, 100)
        + rt15_record_at(192, 15, 54, 50, 200)
        + rt15_record_at(192, 15, 54, 50, 150)
    )
    fa = tmp_path / "a.mie"
    fa.write_bytes(a)
    # The raise is lazy (during the drain), so the generator must be consumed.
    readers = [MieFileReader(fa)]
    with pytest.raises(MieNonMonotonicInputError):
        list(merge_readers(readers, strict=True))


@pytest.mark.requirement("L2-MRG-001")
def test_read_manifest_skips_blanks_and_comments(tmp_path: Path) -> None:
    manifest = tmp_path / "list.txt"
    manifest.write_text(
        "# a comment\n\nfile1.mie\n  file2.mie  \n# another\nfile3.mie\n",
        encoding="utf-8",
    )
    paths = read_manifest(manifest)
    assert paths == [
        Path("file1.mie"),
        Path("file2.mie"),
        Path("file3.mie"),
    ]


@pytest.mark.requirement("L2-MRG-001")
def test_expand_glob_matches_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "b.mie").write_bytes(b"")
    (tmp_path / "a.mie").write_bytes(b"")
    (tmp_path / "c.csv").write_bytes(b"")
    matched = [p.name for p in expand_glob(str(tmp_path / "*.mie"))]
    assert matched == ["a.mie", "b.mie"]  # sorted, .csv excluded


@pytest.mark.requirement("L2-MRG-001")
def test_expand_glob_matches_files_and_skips_directories(tmp_path: Path) -> None:
    """A DIRECTORY whose name matches the pattern is not an input.

    It matched in the C++ implementation, which then failed to map it -- so the
    same ``--glob`` produced a full batch on two implementations and a failure
    on the third (L2-MRG-001 clause 4).
    """
    (tmp_path / "a.mie").write_bytes(b"\x00\x00")
    (tmp_path / "b.mie").write_bytes(b"\x00\x00")
    (tmp_path / "archive.mie").mkdir()
    matched = [p.name for p in expand_glob(str(tmp_path / "*.mie"))]
    assert matched == ["a.mie", "b.mie"]


@pytest.mark.requirement("L2-MRG-001")
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevation on Windows")
def test_expand_glob_follows_symlinks_and_skips_dangling_ones(tmp_path: Path) -> None:
    """A symlink to a recording IS a recording; a dangling one is not.

    This is the clause Rust's ``DirEntry::file_type`` got wrong -- it does not
    follow symlinks, so a symlinked recording answered "not a file" there while
    this implementation kept it.
    """
    real = tmp_path / "real.mie"
    real.write_bytes(b"\x00\x00")
    (tmp_path / "link.mie").symlink_to(real)
    (tmp_path / "dangling.mie").symlink_to(tmp_path / "gone.bin")
    matched = [p.name for p in expand_glob(str(tmp_path / "*.mie"))]
    assert matched == ["link.mie", "real.mie"]


@pytest.mark.requirement("L2-MRG-001")
@pytest.mark.skipif(sys.platform == "win32", reason="a backslash IS a separator on Windows")
def test_expand_glob_does_not_treat_a_backslash_as_a_separator_on_posix(
    tmp_path: Path,
) -> None:
    """On POSIX a backslash is an ordinary filename character.

    C++ split on it regardless of platform, so one pattern resolved to a file in
    the current directory here and to a pattern inside a subdirectory there
    (L2-MRG-001 clause 1).
    """
    (tmp_path / "odd\\name.mie").write_bytes(b"\x00\x00")
    (tmp_path / "plain.mie").write_bytes(b"\x00\x00")
    matched = [p.name for p in expand_glob(str(tmp_path / "odd\\name*.mie"))]
    assert matched == ["odd\\name.mie"]


@pytest.mark.requirement("L2-MRG-001")
@pytest.mark.requirement("L3-PY-014")
def test_glob_match_wildcards() -> None:
    assert glob_match("*.mie", "rec1.mie")
    assert glob_match("rec?.mie", "rec5.mie")
    assert not glob_match("rec?.mie", "rec55.mie")
    assert glob_match("*", "anything")
    assert glob_match("a*b*c", "axxbyyc")
    assert not glob_match("*.mie", "rec.csv")
    assert glob_match("", "")
    assert not glob_match("", "x")
    assert glob_match("a.b", "a.b")
    assert not glob_match("a.b", "axb")


@pytest.mark.requirement("L3-PY-014")
def test_max_merge_files_matches_rust() -> None:
    # The cap is shared in value with the Rust constant (L3-PY-014).
    assert MAX_MERGE_FILES == 256


@pytest.mark.requirement("L1-EXIT-009")
@pytest.mark.requirement("L2-MRG-003")
def test_cli_merge_incompatible_exits_6(tmp_path: Path) -> None:
    """A merge whose inputs can't share an absolute timeline (a freerun-leading
    file) exits 6 and writes no output. Mirrors the Rust
    `merge_incompatible_inputs_exit_6` CLI test."""
    from aero1553.cli import EXIT_MERGE_INCOMPATIBLE, main

    good = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    freerun = rt15_record_at(0, 0, 0, 0, 0, freerun=True) + rt15_record_at(
        0, 0, 0, 1, 0, freerun=True
    )
    fg = tmp_path / "good.mie"
    ff = tmp_path / "freerun.mie"
    fg.write_bytes(good)
    ff.write_bytes(freerun)
    out = tmp_path / "merged.csv"
    assert main(["decode", str(fg), str(ff), "-o", str(out)]) == EXIT_MERGE_INCOMPATIBLE
    assert not out.exists()


@pytest.mark.requirement("L2-MRG-006")
def test_cli_merge_strict_flag_exits_1_on_backward_step(tmp_path: Path) -> None:
    """The `--strict` CLI flag (parity with the Rust CLI) makes a within-file
    backward timestamp step fail the merge with exit 1. Lenient (no flag) exits
    0. This exercises the flag end-to-end through the CLI."""
    from aero1553.cli import EXIT_OK, EXIT_RUNTIME, main

    nonmono = (
        rt15_record_at(192, 15, 54, 50, 200)
        + rt15_record_at(192, 15, 54, 50, 400)
        + rt15_record_at(192, 15, 54, 50, 150)  # backward step
    )
    a = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fn = tmp_path / "nonmono.mie"
    fa = tmp_path / "a.mie"
    fn.write_bytes(nonmono)
    fa.write_bytes(a)

    out = tmp_path / "merged.csv"
    assert main(["decode", str(fn), str(fa), "--strict", "-o", str(out)]) == EXIT_RUNTIME
    # Lenient (default) keeps everything and succeeds.
    out2 = tmp_path / "merged2.csv"
    assert main(["decode", str(fn), str(fa), "-o", str(out2)]) == EXIT_OK


@pytest.mark.requirement("L2-MRG-004")
def test_cli_merge_allow_partial_priming_writes_dot_partial(tmp_path: Path) -> None:
    """A merge `decode` where one input fails at *priming* (a non-MIE first
    record) under `--allow-partial` writes the combined output as
    ``<out>.partial``, leaves the plain ``<out>`` absent, and exits 0. Mirrors
    the Rust `merge_allow_partial_priming_writes_dot_partial`."""
    from aero1553.cli import EXIT_OK, main

    good = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fg = tmp_path / "good.mie"
    fb = tmp_path / "bad.mie"
    fg.write_bytes(good)
    fb.write_bytes(b"\xff" * 4096)  # non-MIE first record
    out = tmp_path / "merged.csv"
    assert main(["decode", str(fg), str(fb), "-o", str(out), "--allow-partial"]) == EXIT_OK
    assert (tmp_path / "merged.csv.partial").exists()
    assert not out.exists()


@pytest.mark.requirement("L2-MRG-004")
def test_cli_merge_allow_partial_open_failure_writes_dot_partial(tmp_path: Path) -> None:
    """A merge where one input fails at *open* (an empty 0-byte file) under
    `--allow-partial` likewise writes a ``.partial`` and exits 0 — the per-file
    failure is tolerated whether it occurs at open, priming, or mid-file."""
    from aero1553.cli import EXIT_OK, main

    good = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fg = tmp_path / "good.mie"
    fe = tmp_path / "empty.mie"
    fg.write_bytes(good)
    fe.write_bytes(b"")  # 0-byte → fails at open
    out = tmp_path / "merged.csv"
    assert main(["decode", str(fg), str(fe), "-o", str(out), "--allow-partial"]) == EXIT_OK
    assert (tmp_path / "merged.csv.partial").exists()
    assert not out.exists()


@pytest.mark.requirement("L2-WRT-014")
def test_cli_merge_allow_partial_single_survivor_still_guards_output(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Regression: a merge (two inputs *requested*) where ``--allow-partial``
    drops one input to a single surviving reader must STILL reject an output path
    that collides with an input. Previously the collision guard gated on the
    surviving reader count (``len(readers) > 1``), so this case silently skipped
    both it and the writer's own single-input check (``WriteOptions`` receives
    ``input_path=None`` for any requested merge), risking an in-place overwrite of
    the input. Now gated on ``merge_requested``, mirroring the Rust CLI."""
    from aero1553.cli import EXIT_RUNTIME, main

    good = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fg = tmp_path / "good.mie"
    fe = tmp_path / "empty.mie"
    fg.write_bytes(good)
    fe.write_bytes(b"")  # 0-byte → dropped at open under --allow-partial
    before = fg.read_bytes()
    # Output path collides with the surviving input file.
    rc = main(["decode", str(fg), str(fe), "-o", str(fg), "--allow-partial"])
    assert rc == EXIT_RUNTIME
    # The collision guard fired specifically (not an incidental write error).
    assert "resolves to merge input" in capfd.readouterr().err
    assert fg.read_bytes() == before  # input left intact, never overwritten


@pytest.mark.requirement("L2-WRT-014")
@pytest.mark.requirement("L2-MRG-001")
def test_cli_merge_rejects_input_a_derived_output_would_overwrite(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A merge must check every path it could commit, not just the destination.

    The writer cannot do this one: on the merge path it is handed
    ``input_path=None`` precisely because it is given one stream and never
    learns how many files fed it, so the CLI guard is the only thing standing
    between ``capture_errors.mie`` and being overwritten by the errors file
    derived from ``-o capture.mie``. Asserted on the artifact: exit
    ``EXIT_RUNTIME``, every input byte identical, and no output created.
    """
    from aero1553.cli import EXIT_RUNTIME, main

    rec = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    first = tmp_path / "recording.mie"
    first.write_bytes(rec)
    # Plausible name for a second recorder's file -- and exactly what
    # `-o capture.mie --separate-errors` derives.
    victim = tmp_path / "capture_errors.mie"
    victim.write_bytes(rec)
    before = victim.read_bytes()
    dest = tmp_path / "capture.mie"

    rc = main(["decode", str(first), str(victim), "-o", str(dest), "--separate-errors"])
    assert rc == EXIT_RUNTIME
    assert "resolves to merge input" in capfd.readouterr().err
    assert victim.read_bytes() == before, "input modified despite the rejection"
    assert not dest.exists(), "no output may be created once the run is refused"


# ── CLI bad-input / cap / robustness (L2-MRG-001, L1-ROB-001) ──────────────


@pytest.mark.requirement("L2-MRG-001")
def test_cli_rejects_combined_input_methods(tmp_path: Path) -> None:
    from aero1553.cli import EXIT_USAGE, main

    out = tmp_path / "o.csv"
    # positional + --manifest, positional + --glob, --manifest + --glob
    assert main(["decode", "a.mie", "--manifest", "list.txt", "-o", str(out)]) == EXIT_USAGE
    assert main(["decode", "a.mie", "--glob", "*.mie", "-o", str(out)]) == EXIT_USAGE
    assert main(["decode", "--manifest", "l.txt", "--glob", "*.mie", "-o", str(out)]) == EXIT_USAGE
    assert not out.exists()


@pytest.mark.requirement("L2-MRG-001")
def test_cli_rejects_over_cap(tmp_path: Path) -> None:
    from aero1553.cli import EXIT_USAGE, main

    manifest = tmp_path / "many.txt"
    manifest.write_text(
        "\n".join(f"f{i}.mie" for i in range(MAX_MERGE_FILES + 1)) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.csv"
    # Cap is checked before any file is opened, so non-existent paths are fine.
    assert main(["decode", "--manifest", str(manifest), "-o", str(out)]) == EXIT_USAGE
    assert not out.exists()


@pytest.mark.requirement("L2-MRG-001")
def test_cli_glob_no_match_is_usage_error(tmp_path: Path) -> None:
    from aero1553.cli import EXIT_USAGE, main

    out = tmp_path / "o.csv"
    assert main(["decode", "--glob", str(tmp_path / "*.nomatch"), "-o", str(out)]) == EXIT_USAGE


@pytest.mark.requirement("L1-ROB-001")
def test_cli_manifest_missing_is_runtime_error(tmp_path: Path) -> None:
    from aero1553.cli import EXIT_RUNTIME, main

    out = tmp_path / "o.csv"
    assert (
        main(["decode", "--manifest", str(tmp_path / "nope.txt"), "-o", str(out)]) == EXIT_RUNTIME
    )


@pytest.mark.requirement("L1-ROB-001")
def test_cli_manifest_non_utf8_is_runtime_error(tmp_path: Path) -> None:
    # Matches the Rust reader's read_to_string failure → exit 1 (not a usage
    # error), keeping the two implementations' exit codes identical.
    from aero1553.cli import EXIT_RUNTIME, main

    manifest = tmp_path / "bin.txt"
    manifest.write_bytes(b"\xff\xfe\x00\x01\x80\x81 not utf-8")
    out = tmp_path / "o.csv"
    assert main(["decode", "--manifest", str(manifest), "-o", str(out)]) == EXIT_RUNTIME


@pytest.mark.requirement("L2-MRG-004")
def test_merge_allow_partial_writes_partial_on_file_failure(tmp_path: Path) -> None:
    """L2-MRG-004 / L1-EXIT-004: with allow_partial, a merge whose input hits
    an unrecoverable sync loss truncates that file, completes from the rest,
    and the writer commits the combined output as ``.partial``. Mirrors the
    Rust ``merge_allow_partial_writes_partial_on_file_failure``."""
    from aero1553.writer import WriteOptions, write_csv

    a = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    b = (
        rt15_record_at(192, 15, 54, 50, 200)
        + rt15_record_at(192, 15, 54, 50, 400)
        + b"\xff" * 70_000  # >64 KB of non-resyncing garbage → unrecoverable
    )
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(a)
    fb.write_bytes(b)
    readers = [MieFileReader(fa), MieFileReader(fb)]
    merged = merge_readers(readers, allow_partial=True)

    out = tmp_path / "out.csv"
    outcome = write_csv(merged, output=out, opts=WriteOptions(allow_partial=True))
    assert outcome.partial is not None
    # A:100 + B:200 + A:300 + the record immediately before B's sync loss.
    # That last one used to be discarded because its *successor* boundary was
    # corrupt; continuous validation no longer looks ahead (L2-SYN-005), so a
    # well-formed record is no longer lost to its neighbour's damage.
    assert outcome.normal_count == 4
    assert (tmp_path / "out.csv.partial").exists()


@pytest.mark.requirement("L2-MRG-004")
def test_merge_allow_partial_writes_partial_on_priming_failure(tmp_path: Path) -> None:
    """L2-MRG-004: a *priming-time* failure — an input whose first record is
    unreadable / non-MIE (4 KB of 0xFF) — under allow_partial must arm the
    deferred terminal so the writer commits a ``.partial``. Regression: pre-fix
    the priming failure was skipped silently and a plain ``.csv`` was written
    with ``outcome.partial is None``."""
    from aero1553.writer import WriteOptions, write_csv

    a = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(a)
    fb.write_bytes(b"\xff" * 4096)  # no valid first record
    readers = [MieFileReader(fa), MieFileReader(fb)]
    merged = merge_readers(readers, allow_partial=True)

    out = tmp_path / "out.csv"
    outcome = write_csv(merged, output=out, opts=WriteOptions(allow_partial=True))
    assert outcome.partial is not None, "priming failure must commit a .partial"
    assert outcome.normal_count == 2  # A's two good records
    assert (tmp_path / "out.csv.partial").exists()


@pytest.mark.requirement("L2-MRG-004")
def test_merge_no_allow_partial_priming_failure_raises(tmp_path: Path) -> None:
    """L2-MRG-004: without allow_partial, a priming-time failure fails the batch
    (the error surfaces when the generator is consumed)."""
    a = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(a)
    fb.write_bytes(b"\xff" * 4096)
    readers = [MieFileReader(fa), MieFileReader(fb)]
    with pytest.raises(Aero1553Error):
        list(merge_readers(readers, allow_partial=False))


@pytest.mark.requirement("L2-MRG-004")
def test_merge_allow_partial_all_inputs_bad(tmp_path: Path) -> None:
    """L2-MRG-004: a merge where every input fails to prime still commits an
    (empty) ``.partial`` under allow_partial."""
    from aero1553.writer import WriteOptions, write_csv

    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(b"\xff" * 4096)
    fb.write_bytes(b"\xff" * 4096)
    readers = [MieFileReader(fa), MieFileReader(fb)]
    merged = merge_readers(readers, allow_partial=True)

    out = tmp_path / "out.csv"
    outcome = write_csv(merged, output=out, opts=WriteOptions(allow_partial=True))
    assert outcome.partial is not None
    assert outcome.normal_count == 0
    assert (tmp_path / "out.csv.partial").exists()


@pytest.mark.requirement("L2-MRG-004")
def test_merge_allow_partial_bad_input_then_good(tmp_path: Path) -> None:
    """L2-MRG-004: a bad first input (index 0) plus a good second input still
    yields a ``.partial`` carrying the good rows — the priming terminal survives
    the drain."""
    from aero1553.writer import WriteOptions, write_csv

    good = rt15_record_at(192, 15, 54, 50, 100) + rt15_record_at(192, 15, 54, 50, 300)
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(b"\xff" * 4096)  # index 0: bad
    fb.write_bytes(good)  # index 1: good
    readers = [MieFileReader(fa), MieFileReader(fb)]
    merged = merge_readers(readers, allow_partial=True)

    out = tmp_path / "out.csv"
    outcome = write_csv(merged, output=out, opts=WriteOptions(allow_partial=True))
    assert outcome.partial is not None
    assert outcome.normal_count == 2
    assert (tmp_path / "out.csv.partial").exists()


@pytest.mark.requirement("L2-MRG-001")
def test_read_manifest_grammar_is_exactly_specified(tmp_path: Path) -> None:
    """The manifest grammar, pinned exactly.

    Leaving it at "one path per line" is how three implementations came to
    disagree, a different way each, all found by the merge fuzz harness
    comparing its ``FUZZ-SUMMARY`` counters and all now spelled out in
    L2-MRG-001:

    * ``\\n`` is the only line separator. This reader used ``str.splitlines()``,
      which also breaks on vertical tab, form feed, U+0085 and U+2028/9 -- none
      of which ends a line in a manifest, all of which are legal in a POSIX
      filename. One file became two nonexistent ones.
    * At most one trailing ``\\r`` is stripped, so CRLF works and a filename
      containing a bare CR survives.
    * Trimming is ASCII space and tab only. ``str.strip()`` also removes U+00A0,
      U+3000 and the rest; the C++ implementation is locale-free by rule and
      cannot, so two implementations silently edited a filename the third passed
      through.

    Mirrors ``read_manifest_grammar_is_exactly_specified`` in Rust and the
    C++ case of the same name.
    """

    def read(body: bytes) -> list[str]:
        path = tmp_path / "grammar.txt"
        path.write_bytes(body)
        return [str(p) for p in read_manifest(path)]

    # Only "\n" separates; a form feed, VT or U+0085 is part of the filename.
    assert read(b"a.mie\x0cb.mie\n") == ["a.mie\x0cb.mie"]
    assert read(b"a.mie\x0bb.mie\n") == ["a.mie\x0bb.mie"]
    assert read("a.mie\u0085b.mie\n".encode()) == ["a.mie\u0085b.mie"]  # U+0085 NEL

    # One trailing CR is the CRLF terminator; an interior CR is a filename.
    assert read(b"a.mie\r\nb.mie\r\n") == ["a.mie", "b.mie"]
    assert read(b"a\rb.mie\n") == ["a\rb.mie"]
    # The last line counts even without its terminator, and its CR is stripped
    # like any other. Rust used ``str::lines()``, which strips the CR only when
    # an ``\n`` actually followed, so ``b"\r"`` was a one-character path there
    # and no path at all here.
    assert read(b"a.mie\r\nb.mie\r") == ["a.mie", "b.mie"]
    assert read(b"\r") == []

    # ASCII blanks are trimmed; Unicode spaces are part of the name.
    assert read(b" \ta.mie\t \n") == ["a.mie"]
    assert read("\u00a0a.mie\n".encode()) == ["\u00a0a.mie"]  # U+00A0 NBSP

    # A manifest is a text file: ill-formed UTF-8 is refused, not decoded.
    with pytest.raises(UnicodeDecodeError):
        read(b"\xff\xfe\na.mie\n")


@pytest.mark.requirement("L1-ROB-001")
@pytest.mark.requirement("L2-MRG-001")
def test_merge_input_resolution_tolerates_arbitrary_bytes(tmp_path: Path) -> None:
    """L1-ROB-001 for the merge input-resolution surface.

    ``read_manifest`` on arbitrary bytes must only ever return a list or raise
    ``UnicodeDecodeError``, and the hand-rolled glob matcher and directory
    expansion must tolerate any pattern.

    Mirrors ``merge_input_resolution_tolerates_arbitrary_bytes`` in
    ``rust/tests/integration.rs`` and the ``[fuzz]`` merge case in
    ``cpp/tests/test_fuzz.cpp``: same generator, same alphabet, same probes, so
    the ``FUZZ-SUMMARY`` counters are comparable. This harness used to seed
    ``random.Random`` and cover only the manifest -- it was the one harness in
    the project that did not see the same inputs as its counterparts.

    ``expand_glob`` is called for crash-safety only and its result is
    deliberately *not* counted: it reads the working directory, so what it
    returns depends on where the suite ran, and a summary field has to mean the
    same thing in every implementation on every host.
    """
    state = FUZZ_SEED
    # 512 by default rather than the reader harness's 256: each iteration is
    # cheap (no decode, no mmap) and the matcher has more branches than 256
    # inputs comfortably cover. The shared knob still overrides.
    count = fuzz_iterations(512)

    total_bytes = 0
    manifest_ok = 0
    manifest_errors = 0
    manifest_paths = 0
    glob_hits = [0, 0, 0]

    manifest = tmp_path / "fuzz.txt"
    with fuzz_logging():
        for _ in range(count):
            state, r = fuzz_xorshift64(state)
            size = r % 96
            state, payload = fuzz_fill(state, size)
            total_bytes += size

            # The pattern is drawn separately from the manifest bytes, and
            # short: the two surfaces want different input shapes, and deriving
            # one from the other means neither gets the shape it needs.
            state, r = fuzz_xorshift64(state)
            state, pattern_payload = fuzz_fill(state, r % 12)
            pattern = glob_pattern(pattern_payload)

            manifest.write_bytes(payload)
            try:
                result = read_manifest(manifest)
            except UnicodeDecodeError:
                manifest_errors += 1  # non-UTF8 is a documented failure
            else:
                assert isinstance(result, list)
                manifest_ok += 1
                manifest_paths += len(result)

            for index, probe in enumerate(GLOB_PROBES):
                if glob_match(pattern, probe):
                    glob_hits[index] += 1
            expand_glob(pattern)  # crash-safety only; see the docstring

    fuzz_summary(
        "merge",
        count,
        f"bytes={total_bytes} manifest_ok={manifest_ok} "
        f"manifest_errors={manifest_errors} manifest_paths={manifest_paths} "
        f"glob_ascii={glob_hits[0]} glob_latin1={glob_hits[1]} "
        f"glob_cjk={glob_hits[2]} outcome=ok",
    )


@pytest.mark.requirement("L1-MRG-003")
@pytest.mark.requirement("L2-MRG-007")
@pytest.mark.requirement("L3-PY-015")
def test_merge_collapse_cross_recorder_duplicate(tmp_path: Path) -> None:
    # The same bus transaction (identical wire content at the same µs) recorded
    # by two recorders collapses to a single row under collapse_duplicates.
    rec = rt15_record_at(192, 15, 54, 50, 100)
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(rec)
    fb.write_bytes(rec)  # identical content + timestamp, different file
    readers = [MieFileReader(fa), MieFileReader(fb)]
    msgs = list(merge_readers(readers, collapse_duplicates=True))
    assert len(msgs) == 1, "the second recorder's duplicate is collapsed"


@pytest.mark.requirement("L2-MRG-007")
def test_merge_collapse_keeps_different_time(tmp_path: Path) -> None:
    # Identical content at different timestamps (beyond the window) is real
    # periodic traffic, not a duplicate — both rows survive.
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(rt15_record_at(192, 15, 54, 50, 100))
    fb.write_bytes(rt15_record_at(192, 15, 54, 50, 300))
    readers = [MieFileReader(fa), MieFileReader(fb)]
    msgs = list(merge_readers(readers, collapse_duplicates=True))
    assert len(msgs) == 2, "distinct timestamps are distinct events"


@pytest.mark.requirement("L2-MRG-007")
def test_merge_collapse_same_file_not_collapsed(tmp_path: Path) -> None:
    # Identical records from the same recorder (same input file) are never
    # collapsed — collapsing is strictly cross-recorder.
    rec = rt15_record_at(192, 15, 54, 50, 100)
    fa = tmp_path / "a.mie"
    fa.write_bytes(rec + rec)  # two identical records, one file
    readers = [MieFileReader(fa)]
    msgs = list(merge_readers(readers, collapse_duplicates=True))
    assert len(msgs) == 2, "same-recorder duplicates are kept"


@pytest.mark.requirement("L2-MRG-007")
def test_merge_collapse_within_window(tmp_path: Path) -> None:
    # With a non-zero window, near-simultaneous identical content from two
    # recorders whose clocks differ slightly collapses (3µs skew, 5µs window).
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(rt15_record_at(192, 15, 54, 50, 100))
    fb.write_bytes(rt15_record_at(192, 15, 54, 50, 103))
    readers = [MieFileReader(fa), MieFileReader(fb)]
    msgs = list(merge_readers(readers, collapse_duplicates=True, collapse_window_us=5))
    assert len(msgs) == 1, "within-window clock skew collapses"


@pytest.mark.requirement("L2-MRG-008")
def test_dedup_survivor_set_is_capped_when_the_window_cannot_bound_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The window bounds retention in TIME; the cap bounds it in COUNT.

    Every record shares one timestamp, so every one of them is legitimately
    inside the collapse window -- the input the window alone cannot bound, and
    the same "timestamps all decode alike" case L2-WRT-022 cites for the reorder
    stage. The records differ in RT, so none is a duplicate of another: the cap
    is reached by distinct survivors, not by a collapse.

    The merge must degrade rather than fail -- one WARN when the cap is reached,
    and every record still in the output (L2-MRG-008). (That retention does not
    depend on arrival order is pinned where the window lives, in
    ``rust/src/merge.rs``.)
    """
    import logging

    from tests.conftest import receive_record_rt_sa_us

    def recording(name: str, rts: range) -> MieFileReader:
        path = tmp_path / name
        path.write_bytes(b"".join(receive_record_rt_sa_us(rt, 11, 100) for rt in rts))
        return MieFileReader(path)

    readers = [recording("a.mie", range(1, 16)), recording("b.mie", range(16, 31))]
    with caplog.at_level(logging.WARNING, logger="aero1553.merge"):
        merged = list(
            merge_readers(
                readers,
                collapse_duplicates=True,
                collapse_window_us=2**62,
                max_collapse_survivors=4,
            )
        )

    assert len(merged) == 30, "no record may be dropped when the cap is reached"
    warns = [r for r in caplog.records if "max_collapse_survivors" in r.getMessage()]
    assert len(warns) == 1, "exactly one cap WARN per merge, not one per capped record"


@pytest.mark.requirement("L2-MRG-006")
@pytest.mark.requirement("L2-MRG-007")
def test_merge_collapse_survives_lenient_non_monotonic(tmp_path: Path) -> None:
    # A within-file backward timestamp step (100 → 200 → 150) makes the merged
    # stream step backward; collapsing must handle the negative gap gracefully.
    a = (
        rt15_record_at(192, 15, 54, 50, 100)
        + rt15_record_at(192, 15, 54, 50, 200)
        + rt15_record_at(192, 15, 54, 50, 150)
    )
    fa = tmp_path / "a.mie"
    fa.write_bytes(a)
    msgs = list(merge_readers([MieFileReader(fa)], collapse_duplicates=True))
    # Single file → nothing is cross-recorder → every record survives.
    assert len(msgs) == 3, "lenient non-monotonic + collapse keeps all rows"


@pytest.mark.requirement("L2-MRG-006")
@pytest.mark.requirement("L2-MRG-007")
def test_merge_collapse_no_over_collapse_after_backward_step(tmp_path: Path) -> None:
    # File A: one record at 1000µs. File B non-monotonic: 1002µs then 10µs.
    # Merged order by absolute time: A@1000, B@1002, B@10 (backward at the end).
    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(rt15_record_at(192, 15, 54, 50, 1000))
    fb.write_bytes(rt15_record_at(192, 15, 54, 50, 1002) + rt15_record_at(192, 15, 54, 50, 10))
    readers = [MieFileReader(fa), MieFileReader(fb)]
    # window 5µs: B@1002 collapses into A@1000 (2µs apart); B@10 is 990µs away —
    # outside the window — so it must be kept, not over-collapsed.
    msgs = list(merge_readers(readers, collapse_duplicates=True, collapse_window_us=5))
    assert len(msgs) == 2, "the far backward record is kept, not over-collapsed"


# ── L2-MRG-005: DELTA scope ─────────────────────────────────────────────────


def _delta_by_row(msgs: list[MieMessage]) -> list[tuple[str, float | None]]:
    """`(RT:MSG, DELTA)` per record, for scope comparisons."""
    return [(m.delta_key, m.delta) for m in msgs]


def _two_files_sharing_a_key(tmp_path: Path) -> tuple[Path, Path]:
    """File A carries a key unique to it plus one shared with B; B carries only
    the shared key, offset in time. The shared key is where the two scopes
    differ; the unique key must be identical under both."""
    from tests.conftest import receive_record_rt_sa_us

    fa = tmp_path / "a.mie"
    fb = tmp_path / "b.mie"
    fa.write_bytes(
        receive_record_rt_sa_us(15, 11, 100_000)
        + receive_record_rt_sa_us(20, 5, 100_000)
        + receive_record_rt_sa_us(15, 11, 300_000)
        + receive_record_rt_sa_us(20, 5, 300_000)
    )
    fb.write_bytes(
        receive_record_rt_sa_us(20, 5, 200_000) + receive_record_rt_sa_us(20, 5, 400_000)
    )
    return fa, fb


@pytest.mark.requirement("L2-MRG-005")
@pytest.mark.requirement("L3-WRT-004")
def test_per_file_delta_matches_single_file_decode(tmp_path: Path) -> None:
    """The guarantee that makes `per-file` the default: every merged record's
    DELTA equals the value that record gets when its own file is decoded alone.
    """
    fa, fb = _two_files_sharing_a_key(tmp_path)

    alone = {}
    for f in (fa, fb):
        for m in MieFileReader(f):
            alone[(f.name, m.file_offset)] = m.delta

    merged = list(
        merge_readers(
            [MieFileReader(fa), MieFileReader(fb)],
            delta_scope=DeltaScope.PER_FILE,
        )
    )
    assert len(merged) == 6
    # Every merged record must carry its own file's DELTA. Records are matched
    # by (file, offset) rather than by position, since the merge interleaves.
    by_offset: dict[int, list[float | None]] = {}
    for m in merged:
        by_offset.setdefault(m.file_offset, []).append(m.delta)
    for (_name, offset), delta in alone.items():
        assert delta in by_offset[offset], f"offset {offset}: {delta} not in {by_offset[offset]}"


@pytest.mark.requirement("L2-MRG-005")
def test_global_scope_measures_across_the_merged_timeline(tmp_path: Path) -> None:
    """`global` compresses the shared key's gaps (0.2s per file becomes 0.1s
    across the merge) while leaving the file-unique key untouched."""
    fa, fb = _two_files_sharing_a_key(tmp_path)
    merged = list(
        merge_readers(
            [MieFileReader(fa), MieFileReader(fb)],
            delta_scope=DeltaScope.GLOBAL,
        )
    )
    shared = [d for k, d in _delta_by_row(merged) if k == "20:5R"]
    unique = [d for k, d in _delta_by_row(merged) if k == "15:11R"]
    assert shared == [0.0, 0.1, 0.1, 0.1], "shared key compresses under global scope"
    assert unique == [0.0, 0.2], "a key unique to one file is unaffected by scope"


@pytest.mark.requirement("L2-MRG-005")
def test_per_file_is_the_default_scope(tmp_path: Path) -> None:
    fa, fb = _two_files_sharing_a_key(tmp_path)
    default = list(merge_readers([MieFileReader(fa), MieFileReader(fb)]))
    explicit = list(
        merge_readers(
            [MieFileReader(fa), MieFileReader(fb)],
            delta_scope=DeltaScope.PER_FILE,
        )
    )
    assert _delta_by_row(default) == _delta_by_row(explicit)


@pytest.mark.requirement("L2-MRG-005")
def test_scope_does_not_affect_a_single_input(tmp_path: Path) -> None:
    """With one file the two scopes are the same computation by definition."""
    fa, _fb = _two_files_sharing_a_key(tmp_path)
    per_file = list(merge_readers([MieFileReader(fa)], delta_scope=DeltaScope.PER_FILE))
    global_ = list(merge_readers([MieFileReader(fa)], delta_scope=DeltaScope.GLOBAL))
    assert _delta_by_row(per_file) == _delta_by_row(global_)


@pytest.mark.requirement("L2-MRG-005")
@pytest.mark.requirement("L2-MRG-007")
def test_collapse_does_not_alter_per_file_delta(tmp_path: Path) -> None:
    """Under per-file scope a surviving record's DELTA is a property of its own
    file, so suppressing a cross-recorder duplicate cannot change it."""
    from tests.conftest import receive_record_rt_sa_us

    same = receive_record_rt_sa_us(20, 5, 100_000) + receive_record_rt_sa_us(20, 5, 300_000)
    fa = tmp_path / "rec_a.mie"
    fb = tmp_path / "rec_b.mie"
    fa.write_bytes(same)
    fb.write_bytes(same)

    collapsed = list(
        merge_readers(
            [MieFileReader(fa), MieFileReader(fb)],
            collapse_duplicates=True,
            collapse_window_us=0,
        )
    )
    assert [d for _k, d in _delta_by_row(collapsed)] == [0.0, 0.2], (
        "collapsing leaves each survivor's own-file DELTA intact"
    )
