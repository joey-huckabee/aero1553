"""End to end: every Python library feature, against the golden recordings.

The golden recordings (``tests/golden``) are generated and pinned by hash,
and so is the CSV the CLI decodes each to. That pinned CSV is the oracle
here: every library route -- a file, a stream, a filtered or merged or split
decode, the tabular ``columns()`` / ``to_dict()`` views -- must reproduce the
CLI's bytes, or agree with them field by field. Nothing is compared against a
value this file computed the same way it computes the thing under test.

It doubles as the tour the user guide follows: reading, filtering,
canonical order, merging recorders, CSV output in every mode, tables for
NumPy / pandas / dataclasses, dates with and without a year, Standard and
freerun time, configuration files, the hex dump, and logging.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import io
import itertools
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from aero1553 import MieFileReader, columns
from aero1553.cli import main
from aero1553.config import FilterConfig, load_config
from aero1553.dump import hex_dump_records
from aero1553.exceptions import (
    Aero1553Error,
    MieCalendarUnavailableError,
    MieClobberRefusedError,
)
from aero1553.filters import apply_filters
from aero1553.merge import merge_readers
from aero1553.models import Bus, MessageType, OutputTimeFormat, TimeRender
from aero1553.order import order_rows
from aero1553.writer import WriteOptions, write_csv, write_csv_split

_GOLDEN_PY = Path(__file__).resolve().parents[2] / "tests" / "golden" / "golden.py"
_spec = importlib.util.spec_from_file_location("golden", _GOLDEN_PY)
assert _spec is not None
assert _spec.loader is not None
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)

SMALL = ("a-small", "b-small", "standard-small", "freerun-small")
NAT = -(2**63)


@pytest.fixture(scope="module")
def rec(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The small golden recordings, written and checked against their pins."""
    directory = tmp_path_factory.mktemp("golden")
    return {name: golden.write(name, directory) for name in SMALL}


@pytest.fixture(scope="module")
def pins() -> dict[str, Any]:
    return golden.manifest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _cli(tmp_path: Path, *args: str, global_args: tuple[str, ...] = ()) -> Path:
    out = tmp_path / f"cli-{len(list(tmp_path.iterdir()))}.csv"
    assert main([*global_args, "decode", *args, "-o", str(out)]) == 0
    return out


def _lib(tmp_path: Path, messages: Any, **opts: Any) -> Path:
    out = tmp_path / f"lib-{len(list(tmp_path.iterdir()))}.csv"
    write_csv(order_rows(messages), out, WriteOptions(**opts))
    return out


# ------------------------------------------------------------------ decode


@pytest.mark.requirement("L3-PY-004")
@pytest.mark.parametrize("name", SMALL)
def test_the_library_decode_is_the_pinned_cli_decode(
    tmp_path: Path, rec: dict[str, Path], pins: dict[str, Any], name: str
) -> None:
    """Reader -> canonical order -> write_csv reproduces the CLI's pinned
    CSV byte for byte, for every kind of golden recording."""
    expected = pins["recordings"][name]["csv_sha256"]
    assert _sha(_lib(tmp_path, MieFileReader(rec[name]))) == expected
    assert _sha(_cli(tmp_path, str(rec[name]))) == expected


@pytest.mark.requirement("L2-SYN-015")
@pytest.mark.requirement("L2-SYN-016")
def test_sync_loss_is_recovered_leniently_and_refused_strictly(
    rec: dict[str, Path], pins: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    reader = MieFileReader(rec["a-small"])
    with caplog.at_level(logging.WARNING, logger="aero1553"):
        count = sum(1 for _ in reader)
    assert count == pins["recordings"]["a-small"]["rows"]
    assert reader.sync_losses == 1, "the one corrupt region, recovered"
    assert any("sync lost" in r.getMessage() for r in caplog.records), "reaches Python logging"

    strict = MieFileReader(rec["a-small"], strict=True)
    with pytest.raises(Aero1553Error):
        list(strict)


@pytest.mark.requirement("L2-WRT-021")
def test_canonical_order_sorts_each_equal_timestamp_run(rec: dict[str, Path]) -> None:
    raw = columns(
        MieFileReader(rec["a-small"]), fields=["time_us", "rt", "subaddress", "direction"]
    )
    ordered = columns(
        order_rows(MieFileReader(rec["a-small"])),
        fields=["time_us", "rt", "subaddress", "direction"],
    )

    def keys(c: dict[str, Any]) -> list[tuple[int, int, int, int]]:
        return list(
            zip(*(c[f].tolist() for f in ("time_us", "rt", "subaddress", "direction")), strict=True)
        )

    before, after = keys(raw), keys(ordered)
    assert before != after, "the recording has out-of-order ties for the stage to sort"
    for prev, cur in itertools.pairwise(after):
        if cur[0] == prev[0] and cur[1] >= 0 and prev[1] >= 0:
            assert cur[1:] >= prev[1:], "equal timestamps sorted by RT, SA, R-before-T"


# ----------------------------------------------------------------- filters

FILTERS: list[tuple[str, FilterConfig, list[str]]] = [
    (
        "exclude-types",
        FilterConfig(exclude_types={MessageType.SPURIOUS_DATA, MessageType.MODE_COMMAND}),
        ["--exclude-types", "SPURIOUS_DATA,MODE_COMMAND"],
    ),
    ("exclude-rts", FilterConfig(exclude_rts={15, 5}), ["--exclude-rts", "15,5"]),
    ("exclude-buses", FilterConfig(exclude_buses={Bus.B}), ["--exclude-buses", "B"]),
    (
        "exclude-subaddresses",
        FilterConfig(exclude_subaddresses={11}),
        ["--exclude-subaddresses", "11"],
    ),
    (
        "include-types",
        FilterConfig(include_types={MessageType.BC_TO_RT}),
        ["--include-types", "BC_TO_RT"],
    ),
    ("include-rts", FilterConfig(include_rts={15}), ["--include-rts", "15"]),
    ("include-buses", FilterConfig(include_buses={Bus.B}), ["--include-buses", "B"]),
    (
        "include-subaddresses",
        FilterConfig(include_subaddresses={0, 31}),
        ["--include-subaddresses", "0,31"],
    ),
    (
        "combined",
        FilterConfig(include_rts={15}, exclude_buses={Bus.B}),
        ["--include-rts", "15", "--exclude-buses", "B"],
    ),
]


@pytest.mark.requirement("L2-FLT-001")
@pytest.mark.requirement("L2-FLT-002")
@pytest.mark.parametrize(("label", "config", "flags"), FILTERS, ids=[f[0] for f in FILTERS])
def test_every_filter_matches_the_cli_and_its_own_predicate(
    tmp_path: Path, rec: dict[str, Path], label: str, config: FilterConfig, flags: list[str]
) -> None:
    lib = _lib(tmp_path, apply_filters(MieFileReader(rec["a-small"]), config))
    cli = _cli(tmp_path, str(rec["a-small"]), *flags)
    assert _sha(lib) == _sha(cli), label

    kept = columns(apply_filters(MieFileReader(rec["a-small"]), config))
    everything = [m.to_dict() for m in MieFileReader(rec["a-small"])]
    expected = [
        d
        for d in everything
        if not config.should_exclude(d["message_type"], d["rt"], Bus(d["bus"]), d["subaddress"])
    ]
    assert 0 < len(kept["rt"]) < len(everything), f"{label} selects a proper subset"
    assert sorted(kept["file_offset"].tolist()) == sorted(d["file_offset"] for d in expected)


@pytest.mark.requirement("L2-CFG-001")
def test_a_config_file_drives_the_same_pipeline_as_the_cli(
    tmp_path: Path, rec: dict[str, Path]
) -> None:
    cfg = tmp_path / "site.toml"
    cfg.write_text(
        '[filter]\nexclude_rts = [15]\nexclude_types = ["SPURIOUS_DATA"]\n', encoding="utf-8"
    )
    config = load_config(cfg)
    lib = _lib(tmp_path, apply_filters(MieFileReader(rec["a-small"]), config.filters))
    # --config is a global option: it precedes the subcommand.
    cli = _cli(tmp_path, str(rec["a-small"]), global_args=("--config", str(cfg)))
    assert _sha(lib) == _sha(cli)


# ------------------------------------------------------------------- merge


@pytest.mark.requirement("L2-MRG-007")
@pytest.mark.requirement("L2-WRT-020")
def test_merging_two_recorders_collapses_their_shared_transactions(
    tmp_path: Path, rec: dict[str, Path], pins: dict[str, Any]
) -> None:
    readers = [MieFileReader(rec["a-small"]), MieFileReader(rec["b-small"])]
    merged = _lib(tmp_path, merge_readers(readers, collapse_duplicates=True))
    pinned = pins["merges"]["a-small+b-small"]
    assert _sha(merged) == pinned["csv_sha256"]

    rows = _rows(merged)
    sizes = pins["recordings"]
    assert len(rows) == pinned["rows"] < sizes["a-small"]["rows"] + sizes["b-small"]["rows"]
    assert set(Counter(r["MUX"] for r in rows)) == {"aa", "bb"}, "MUX names each row's recorder"
    times = [r["TIME_STAMP"] for r in rows]
    assert times == sorted(times), "one time-ordered stream"


# -------------------------------------------------------------- CSV output


@pytest.mark.requirement("L2-WRT-011")
def test_separate_errors_splits_the_pinned_rows(tmp_path: Path, rec: dict[str, Path]) -> None:
    inline = _rows(_cli(tmp_path, str(rec["a-small"])))
    out = tmp_path / "split.csv"
    outcome = write_csv_split(order_rows(MieFileReader(rec["a-small"])), out)
    main_rows = _rows(out)
    error_rows = _rows(tmp_path / "split_errors.csv")
    assert outcome.normal_count == len(main_rows)
    assert outcome.error_count == len(error_rows)
    assert error_rows == [r for r in inline if r["ERROR"]]
    assert main_rows == [r for r in inline if not r["ERROR"]]
    assert {r["ERROR"] for r in error_rows} == {"ERROR", "SPURIOUS"}


def test_a_stream_destination_gets_the_file_bytes(tmp_path: Path, rec: dict[str, Path]) -> None:
    buf = io.StringIO()
    write_csv(order_rows(MieFileReader(rec["a-small"])), buf)
    assert buf.getvalue() == _cli(tmp_path, str(rec["a-small"])).read_text(encoding="utf-8")


@pytest.mark.requirement("L2-WRT-017")
def test_no_clobber_refuses_an_existing_destination(tmp_path: Path, rec: dict[str, Path]) -> None:
    out = tmp_path / "exists.csv"
    out.write_text("keep me", encoding="utf-8")
    reader = MieFileReader(rec["a-small"])
    opts = WriteOptions(no_clobber=True)
    with pytest.raises(MieClobberRefusedError):
        write_csv(reader, out, opts)
    assert out.read_text(encoding="utf-8") == "keep me"


@pytest.mark.requirement("L2-WRT-025")
@pytest.mark.requirement("L2-WRT-026")
def test_calendar_renderings_need_a_year_that_has_the_day(rec: dict[str, Path]) -> None:
    """a-small runs from day 365 into day 366: 30-31 December 2024, but day
    366 does not exist in 2026 -- where the CSV writer refuses the file."""

    def render(fmt: OutputTimeFormat, year: int | None) -> list[str]:
        opts = WriteOptions(time_render=TimeRender(format=fmt, year=year, utc_offset_minutes=0))
        buf = io.StringIO()
        write_csv(order_rows(MieFileReader(rec["a-small"])), buf, opts)
        return [r["TIME_STAMP"] for r in csv.DictReader(io.StringIO(buf.getvalue()))]

    iso = render(OutputTimeFormat.ISO, 2024)
    assert iso[0].startswith("2024-12-30T23:59:59")
    assert iso[-1].startswith("2024-12-31T")
    dom = render(OutputTimeFormat.DOM, 2024)
    assert dom[0].startswith("30:23:59:59")
    assert dom[-1].startswith("31:")
    with pytest.raises(MieCalendarUnavailableError):
        render(OutputTimeFormat.ISO, 2026)


# ------------------------------------------------------------------ tables


def _csv_value(row: dict[str, str], field: str) -> Any:
    """The CSV's text for ``field`` in the columns' representation."""
    text = {
        "rt": row["RT"],
        "timestamp": row["TIME_STAMP"],
        "msg_label": row["MSG"],
        "mux": row["MUX"],
    }
    if field == "rt":
        return int(text["rt"]) if text["rt"] else -1
    if field == "mux":
        return text["mux"] or None  # absent MUX is None; an empty label is ""
    return text[field]


@pytest.mark.requirement("L3-PY-020")
def test_columns_agree_with_the_pinned_csv(tmp_path: Path, rec: dict[str, Path]) -> None:
    rows = _rows(_cli(tmp_path, str(rec["a-small"])))
    cols = columns(order_rows(MieFileReader(rec["a-small"])))
    assert len(cols["rt"]) == len(rows)
    for field in ("rt", "timestamp", "msg_label", "mux"):
        col = cols[field]
        values = col.tolist() if isinstance(col, memoryview) else col
        assert values == [_csv_value(r, field) for r in rows], field
    deltas = cols["delta"].tolist()
    assert [f"{d:.6f}" if d == d else "" for d in deltas] == [r["DELTA"] for r in rows]
    words = cols["data_words"].tolist()
    counts = cols["data_word_count"].tolist()
    for row, w, n in zip(rows, words, counts, strict=True):
        assert [f"{x:04X}" for x in w[:n]] == [row[f"WD{i:02d}"] for i in range(1, n + 1)]


@pytest.mark.requirement("L3-PY-020")
def test_selecting_fields_returns_the_same_values(rec: dict[str, Path]) -> None:
    full = columns(MieFileReader(rec["a-small"]))
    some = columns(MieFileReader(rec["a-small"]), fields=["rt", "delta", "error_word"])
    assert list(some) == ["rt", "error_word", "delta"], "schema order, not argument order"
    for field, col in some.items():
        assert col.tolist() == full[field].tolist() or field == "delta"
    assert [d for d in some["delta"].tolist() if d == d] == [
        d for d in full["delta"].tolist() if d == d
    ]


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.requirement("L2-WRT-026")
def test_without_a_year_there_is_no_datetime_and_no_error(rec: dict[str, Path]) -> None:
    cols = columns(MieFileReader(rec["a-small"]))
    assert "datetime" not in cols
    days = set(cols["day_of_year"].tolist())
    assert days == {365, 366}, "the clock reading is still there"
    assert "datetime" not in next(iter(MieFileReader(rec["a-small"]))).to_dict()
    reader = MieFileReader(rec["a-small"])
    with pytest.raises(ValueError, match="needs year="):
        columns(reader, fields=["datetime"])


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.requirement("L2-WRT-026")
def test_with_a_year_datetime_is_the_iso_instant_or_nat(rec: dict[str, Path]) -> None:
    """2024 has day 366, so every record is dated; 2026 does not, so the
    day-366 records are NaT -- per record, where the CSV refuses the file."""
    leap = columns(MieFileReader(rec["a-small"]), year=2024)
    common = columns(MieFileReader(rec["a-small"]), year=2026)
    days = leap["day_of_year"].tolist()
    assert NAT not in leap["datetime"].tolist()
    for day, when in zip(days, common["datetime"].tolist(), strict=True):
        assert (when == NAT) == (day == 366)

    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    first = epoch + dt.timedelta(microseconds=leap["datetime"][0])
    assert first.date() == dt.date(2024, 12, 30)
    record = next(iter(MieFileReader(rec["a-small"])))
    assert record.to_dict(year=2024)["datetime"] == first

    shifted = columns(MieFileReader(rec["a-small"]), year=2024, utc_offset_minutes=-300)
    gaps = {
        s - u for s, u in zip(shifted["datetime"].tolist(), leap["datetime"].tolist(), strict=True)
    }
    assert gaps == {300 * 60 * 1_000_000}, "a clock 5 h behind UTC names instants 5 h later"


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.requirement("L2-DEC-017")
def test_standard_time_needs_a_tick_rate_and_never_has_a_date(rec: dict[str, Path]) -> None:
    plain = columns(MieFileReader(rec["standard-small"]), year=2024)
    assert set(plain["time_us"].tolist()) == {-1}, "no tick rate, no microseconds"
    assert set(plain["datetime"].tolist()) == {NAT}, "a counter has no date"
    assert -1 not in plain["ticks"].tolist()
    rated = columns(MieFileReader(rec["standard-small"]), standard_tick_rate_hz=1e6)
    assert rated["time_us"].tolist() == rated["ticks"].tolist(), "1 MHz: one tick per microsecond"


@pytest.mark.requirement("L3-PY-020")
def test_freerun_records_are_undated_but_keep_their_clock(rec: dict[str, Path]) -> None:
    cols = columns(MieFileReader(rec["freerun-small"]), year=2024)
    assert set(cols["freerun"].tolist()) == {True}
    assert set(cols["datetime"].tolist()) == {NAT}
    assert -1 not in cols["time_us"].tolist(), "the DELTA rule still gives microseconds"


@pytest.mark.requirement("L3-PY-020")
def test_a_user_dataclass_from_to_dict(tmp_path: Path, rec: dict[str, Path]) -> None:
    @dataclasses.dataclass(frozen=True)
    class Transaction:
        timestamp: str
        rt: int | None
        msg_label: str
        delta: float | None
        data_words: tuple[int, ...]

    wanted = [f.name for f in dataclasses.fields(Transaction)]
    rows = [
        Transaction(**m.to_dict(fields=wanted)) for m in order_rows(MieFileReader(rec["a-small"]))
    ]
    csv_rows = _rows(_cli(tmp_path, str(rec["a-small"])))
    assert [(t.timestamp, t.msg_label) for t in rows] == [
        (r["TIME_STAMP"], r["MSG"]) for r in csv_rows
    ]


@pytest.mark.requirement("L3-PY-020")
def test_numpy_and_pandas_analysis(tmp_path: Path, rec: dict[str, Path]) -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    cols = columns(order_rows(MieFileReader(rec["a-small"])), year=2024)
    words = np.asarray(cols.pop("data_words"))
    df = pd.DataFrame(
        {k: np.asarray(v) if isinstance(v, memoryview) else v for k, v in cols.items()}
    )
    df["datetime"] = df["datetime"].to_numpy().view("datetime64[us]")

    rows = _rows(_cli(tmp_path, str(rec["a-small"])))
    per_rt = df[df["rt"] >= 0].groupby("rt").size().to_dict()
    assert per_rt == dict(Counter(int(r["RT"]) for r in rows if r["RT"]))
    assert df["datetime"].dt.year.unique().tolist() == [2024]
    assert words.shape == (len(df), 32)
    assert words.dtype == np.uint16
    first = rows[0]
    n = int(df["data_word_count"].iloc[0])
    assert [f"{x:04X}" for x in words[0, :n]] == [first[f"WD{i:02d}"] for i in range(1, n + 1)]


# -------------------------------------------------------------- dump / logs


@pytest.mark.requirement("L2-CLI-009")
def test_the_hex_dump_annotates_records_and_stops_at_corruption(
    rec: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    out = io.StringIO()
    with caplog.at_level(logging.WARNING, logger="aero1553.dump"):
        hex_dump_records(rec["a-small"], stream=out)
    text = out.getvalue()
    assert "Record #0" in text, "each record is annotated"
    assert "Error:" in text, "including an errored record's Error Word"
    assert "!!" in text, "the record scan notes where it stopped"
    assert caplog.records, "and says so through logging"


def test_freerun_time_is_warned_about(
    rec: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aero1553"):
        list(MieFileReader(rec["freerun-small"]))
    assert any("freerun" in r.getMessage() for r in caplog.records)
