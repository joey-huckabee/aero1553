"""The tabular views: ``aero1553.columns()`` and ``MieMessage.to_dict()``.

The central property is that the two are one schema: every column, read
record by record, equals the matching ``to_dict()`` value (with the column's
absent-value sentinel standing for ``None``). The time fields are also pinned
against the rules they reuse -- DELTA's tick-rate rule and the writer's ISO
calendar rendering -- so the tables cannot drift from the CSV.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
from pathlib import Path
from typing import Any

import pytest

from aero1553 import MieFileReader, columns
from aero1553.config import FilterConfig
from aero1553.exceptions import Aero1553Error
from aero1553.filters import apply_filters
from aero1553.models import (
    Bus,
    CommandWord,
    Direction,
    IrigTimestamp,
    MessageFormat,
    MieMessage,
    OutputTimeFormat,
    StandardTimestamp,
    TimeRender,
    TypeWord,
)
from aero1553.table import FIELDS
from aero1553.writer import WriteOptions, write_csv
from tests.conftest import conformance_input

NAT = -(2**63)
FIXTURES = [
    "basic-multi-record",
    "errors-inline",
    "spurious-standalone",
    "standard-timestamps",
    "rt-to-rt",
    "merge-freerun",
    "bus-b",
    "mode-code-rx-data",
]


def _file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.mie"
    path.write_bytes(conformance_input(name))
    return path


def _sentinel(field: str, value: Any) -> Any:
    """What a column holds where ``to_dict`` holds ``value``."""
    if field == "datetime":
        if value is None:
            return NAT
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        return (value - epoch) // dt.timedelta(microseconds=1)
    if value is not None:
        return value
    if field == "delta":
        return math.nan
    if field == "freerun":
        return False
    if field == "mux":
        return None
    return -1


def _rows(cols: dict[str, Any]) -> list[dict[str, Any]]:
    lists = {
        field: col.tolist() if isinstance(col, memoryview) else col for field, col in cols.items()
    }
    n = len(next(iter(lists.values())))
    return [
        {field: tuple(v[i]) if field == "data_words" else v[i] for field, v in lists.items()}
        for i in range(n)
    ]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return bool(a == b)


def _message(timestamp: IrigTimestamp | StandardTimestamp) -> MieMessage:
    return MieMessage(
        timestamp=timestamp,
        type_word=TypeWord(message_type=0x02, bus=Bus.A, word_count=7, error=False, raw=0x0702),
        message_format=MessageFormat.RECEIVE,
        command_word=CommandWord(
            rt=15, direction=Direction.RECEIVE, subaddress=11, data_word_count=1, raw=0x7961
        ),
        command_word_2=None,
        status_word=0x7800,
        status_word_2=None,
        data_words=(0x1234,),
        error_word=None,
        delta=None,
        file_offset=0,
    )


@pytest.mark.requirement("L3-PY-020")
def test_fields_are_the_to_dict_keys_and_the_column_keys() -> None:
    msg = _message(IrigTimestamp(192, 15, 54, 50, 1, False))
    assert tuple(msg.to_dict(year=2026)) == FIELDS
    assert "datetime" not in msg.to_dict(), "no year, no datetime"
    assert tuple(columns([msg], year=2026)) == FIELDS
    assert tuple(columns([msg])) == tuple(f for f in FIELDS if f != "datetime")


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.parametrize("name", FIXTURES)
def test_every_column_equals_to_dict_record_by_record(tmp_path: Path, name: str) -> None:
    path = _file(tmp_path, name)
    options: dict[str, Any] = {
        "year": 2024,
        "utc_offset_minutes": -300,
        "standard_tick_rate_hz": 1e6,
    }
    dicts = [m.to_dict(**options) for m in MieFileReader(path)]
    cols = columns(MieFileReader(path), **options)
    assert dicts, "fixture decoded no records"
    rows = _rows(cols)
    assert len(rows) == len(dicts)
    for i, (expected, row) in enumerate(zip(dicts, rows, strict=True)):
        for field in FIELDS:
            want = expected[field]
            if field == "data_words":
                count = expected["data_word_count"]
                assert row[field][:count] == want, (name, i)
                assert not any(row[field][count:]), (name, i, "zero padding")
                continue
            assert _same(row[field], _sentinel(field, want)), (name, i, field, row[field], want)
    assert all(len(col) == len(dicts) for col in cols.values())


@pytest.mark.requirement("L3-PY-020")
def test_a_python_iterable_and_a_filtered_stream_are_accepted(tmp_path: Path) -> None:
    path = _file(tmp_path, "basic-multi-record")
    whole = columns(MieFileReader(path), fields=["rt"])
    as_list = columns(list(MieFileReader(path)), fields=["rt"])
    assert whole["rt"].tolist() == as_list["rt"].tolist()
    kept = columns(
        apply_filters(MieFileReader(path), FilterConfig(exclude_rts={15})), fields=["rt"]
    )
    assert 15 not in kept["rt"].tolist()


@pytest.mark.requirement("L3-PY-020")
def test_fields_selects_in_schema_order_and_rejects_unknowns(tmp_path: Path) -> None:
    path = _file(tmp_path, "basic-multi-record")
    cols = columns(MieFileReader(path), fields=["rt", "file_offset"])
    assert list(cols) == ["file_offset", "rt"]
    msg = next(iter(MieFileReader(path)))
    assert list(msg.to_dict(fields=["rt", "file_offset"])) == ["file_offset", "rt"]
    with pytest.raises(ValueError, match="unknown field 'rtt'"):
        columns([], fields=["rtt"])
    with pytest.raises(ValueError, match="not one string"):
        columns([], fields="rt")
    with pytest.raises(ValueError, match="at least one"):
        columns([], fields=[])


@pytest.mark.requirement("L3-PY-020")
def test_datetime_needs_a_year_only_when_asked_for() -> None:
    msg = _message(IrigTimestamp(192, 15, 54, 50, 1, False))
    with pytest.raises(ValueError, match="needs year="):
        columns([msg], fields=["datetime"])
    with pytest.raises(ValueError, match="needs year="):
        msg.to_dict(fields=["datetime"])
    for bad in (
        {"year": 0},
        {"year": 10_000},
        {"utc_offset_minutes": 1440},
        {"standard_tick_rate_hz": 0.0},
    ):
        with pytest.raises(ValueError):
            columns([msg], **bad)


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.requirement("L2-WRT-025")
@pytest.mark.parametrize("offset", [0, -300, 330])
def test_datetime_is_the_instant_the_iso_rendering_names(tmp_path: Path, offset: int) -> None:
    """The datetime field and the CSV's ISO TIME_STAMP are the same instant."""
    path = _file(tmp_path, "basic-multi-record")
    render = TimeRender(format=OutputTimeFormat.ISO, year=2024, utc_offset_minutes=offset)
    buf = io.StringIO()
    write_csv(MieFileReader(path), buf, WriteOptions(time_render=render))
    iso = [row["TIME_STAMP"] for row in csv.DictReader(io.StringIO(buf.getvalue()))]
    rendered = [dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in iso]

    stamps = [
        m.to_dict(year=2024, utc_offset_minutes=offset)["datetime"] for m in MieFileReader(path)
    ]
    assert sorted(stamps) == sorted(rendered)
    assert all(s.utcoffset() == dt.timedelta(minutes=offset) for s in stamps)

    col = columns(MieFileReader(path), fields=["datetime"], year=2024, utc_offset_minutes=offset)
    assert sorted(col["datetime"].tolist()) == sorted(_sentinel("datetime", s) for s in rendered)


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.requirement("L2-WRT-026")
def test_records_with_no_date_are_nat_not_a_refusal() -> None:
    """Freerun, Standard, and day 366 of a common year have no date; the
    column marks each record rather than refusing the table."""
    leap_day = _message(IrigTimestamp(366, 12, 0, 0, 0, False))
    freerun = _message(IrigTimestamp(10, 0, 0, 0, 0, True))
    standard = _message(StandardTimestamp(1000, 0, 1000))
    ordinary = _message(IrigTimestamp(1, 0, 0, 0, 0, False))
    msgs = [leap_day, freerun, standard, ordinary]

    common = columns(msgs, fields=["datetime"], year=2026)["datetime"].tolist()
    assert common == [
        NAT,
        NAT,
        NAT,
        _sentinel("datetime", dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)),
    ]
    leap = columns(msgs, fields=["datetime"], year=2024)["datetime"].tolist()
    assert leap[0] == _sentinel("datetime", dt.datetime(2024, 12, 31, 12, tzinfo=dt.timezone.utc))
    assert [m.to_dict(year=2026)["datetime"] for m in msgs[:3]] == [None, None, None]


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.requirement("L2-DEC-017")
def test_time_us_follows_the_delta_rule() -> None:
    """IRIG always has time_us; Standard only with a tick rate -- the rule
    that decides whether a Standard record gets a DELTA."""
    irig = _message(IrigTimestamp(1, 0, 0, 1, 5, False))
    standard = _message(StandardTimestamp(3, 0, 3))
    assert irig.to_dict()["time_us"] == 86_401_000_005
    assert irig.to_dict()["ticks"] is None
    assert standard.to_dict()["time_us"] is None
    assert standard.to_dict()["ticks"] == 3
    assert standard.to_dict(standard_tick_rate_hz=2.0)["time_us"] == 1_500_000
    assert standard.to_dict()["day_of_year"] is None
    assert irig.to_dict()["time_of_day_us"] == 1_000_005


@pytest.mark.requirement("L3-PY-020")
def test_an_empty_stream_has_empty_columns_of_the_right_shape(tmp_path: Path) -> None:
    cols = columns(MieFileReader(_file(tmp_path, "empty-recording")))
    assert all(len(col) == 0 for col in cols.values())
    # memoryview cannot have a zero in its shape; reshape(-1, 32) restores it.
    assert cols["data_words"].shape == (0,)


@pytest.mark.requirement("L3-PY-020")
def test_a_decoder_error_mid_stream_is_raised(tmp_path: Path) -> None:
    """The same exception plain iteration raises, not a partial table."""
    path = _file(tmp_path, "partial-unrecoverable")
    first, second = MieFileReader(path, strict=True), MieFileReader(path, strict=True)
    with pytest.raises(Aero1553Error) as iterating:
        list(first)
    with pytest.raises(type(iterating.value)) as tabulating:
        columns(second)
    assert str(tabulating.value) == str(iterating.value)


@pytest.mark.requirement("L3-PY-020")
def test_numpy_and_pandas_wrap_the_columns_without_copying(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    path = _file(tmp_path, "errors-inline")
    cols = columns(MieFileReader(path), year=2024)

    rt = np.asarray(cols["rt"])
    assert rt.dtype == np.int8
    assert np.shares_memory(rt, np.asarray(cols["rt"])), "a view, not a copy"
    words = np.asarray(cols["data_words"])
    assert words.dtype == np.uint16
    assert words.shape == (len(rt), 32)
    empty = columns([], fields=["data_words"])["data_words"]
    assert np.asarray(empty).reshape(-1, 32).shape == (0, 32)
    when = np.asarray(cols["datetime"]).view("datetime64[us]")
    assert when.dtype == np.dtype("datetime64[us]")

    df = pd.DataFrame({k: v for k, v in cols.items() if k != "data_words"})
    assert len(df) == len(rt)
    assert df["delta"].dtype == np.float64


def _by_attributes(m: MieMessage) -> dict[str, Any]:
    """The schema as a caller would read it from the record's attributes."""
    cw, ts = m.command_word, m.timestamp
    irig = isinstance(ts, IrigTimestamp)
    return {
        "file_offset": m.file_offset,
        "timestamp": ts.format(),
        "time_us": ts.to_microseconds(),
        "ticks": None if irig else ts.raw_ticks(),
        "freerun": ts.freerun if irig else None,
        "day_of_year": ts.day if irig else None,
        "time_of_day_us": ((ts.hour * 60 + ts.minute) * 60 + ts.second) * 1_000_000 + ts.microsecond
        if irig
        else None,
        "message_type": m.type_word.message_type,
        "message_format": int(m.message_format),
        "bus": int(m.bus),
        "error": m.is_error,
        "rt": m.rt,
        "subaddress": m.subaddress,
        "direction": None if cw is None else int(cw.direction),
        "msg_label": m.msg_label,
        "command_word": None if cw is None else cw.raw,
        "command_word_2": None if m.command_word_2 is None else m.command_word_2.raw,
        "status_word": m.status_word,
        "status_word_2": m.status_word_2,
        "error_word": m.error_word,
        "data_word_count": len(m.data_words),
        "data_words": m.data_words,
        "delta": m.delta,
        "mux": m.mux,
    }


@pytest.mark.requirement("L3-PY-020")
@pytest.mark.parametrize("name", FIXTURES)
def test_to_dict_agrees_with_the_records_attributes(tmp_path: Path, name: str) -> None:
    """The column-vs-dict test compares two views of one Rust function; this
    pins that function's values to the record's own attributes."""
    for m in MieFileReader(_file(tmp_path, name)):
        assert m.to_dict() == _by_attributes(m), name
