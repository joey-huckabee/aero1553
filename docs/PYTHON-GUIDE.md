# Aero1553 Python library guide

The `aero1553` Python package decodes DDC MIL-STD-1553 MIE recordings: it reads
a recording, gives you each bus transaction as a Python object or a whole
recording as NumPy/pandas-ready columns, and writes the same CSV the `aero1553`
command-line tool writes. Under the hood it *is* the Rust decoder -- the package
is a thin Python layer over the Rust crate -- so the library and the CLI decode
every file identically, and at the same speed.

This guide covers the library. For the command-line tool see
[USER-GUIDE.md](USER-GUIDE.md) and [CLI-REFERENCE.md](CLI-REFERENCE.md); for
what the fields mean see [MIE-FORMAT.md](MIE-FORMAT.md) and
[ERROR-CATALOG.md](ERROR-CATALOG.md).

**Every example here is run by the test suite** (`python/tests/test_python_guide.py`),
in a folder holding four sample recordings, and wherever an example shows its
output, the test checks that output exactly. The sample recordings are:

| File | What it is |
|---|---|
| `flight.mie` | 723 transactions on an IRIG clock that runs from day 365 into day 366, including errors, spurious data, mode codes, both buses, and one corrupt stretch |
| `flight-2.mie` | a second recorder on the same bus, some of whose transactions duplicate `flight.mie`'s |
| `counter.mie` | a recording with Standard (free-running counter) timestamps |
| `freerun.mie` | a recording whose IRIG clock was not locked to a time source |

## Contents

1. [Install](#1-install)
2. [A first look](#2-a-first-look)
3. [The record](#3-the-record)
4. [Reading options](#4-reading-options)
5. [Filtering and canonical order](#5-filtering-and-canonical-order)
6. [Writing CSV](#6-writing-csv)
7. [Merging recorders](#7-merging-recorders)
8. [Tables: choosing a route](#8-tables-choosing-a-route)
9. [NumPy](#9-numpy)
10. [pandas](#10-pandas)
11. [Dataclasses](#11-dataclasses)
12. [Time](#12-time)
13. [Configuration files](#13-configuration-files)
14. [The hex dump](#14-the-hex-dump)
15. [Logging](#15-logging)
16. [Errors](#16-errors)
17. [Running the CLI from Python](#17-running-the-cli-from-python)
18. [Performance](#18-performance)
19. [Supported platforms](#19-supported-platforms)

---

## 1. Install

```bash
pip install aero1553
```

The package supports CPython 3.10 to 3.14 and has **no runtime dependencies**.
NumPy and pandas are optional: install them if you want to use them, and the
[table functions](#8-tables-choosing-a-route) will hand them data without a copy.

Installing from a source checkout needs a Rust toolchain
([rustup](https://rustup.rs/)) as well as Python, because the package contains
a compiled extension:

```bash
pip install ./python          # from the repository root
```

Check the install:

```python
import aero1553

print(aero1553.__version__ != "")
```

```text
True
```

---

## 2. A first look

`MieFileReader` opens a recording; iterating it yields one `MieMessage` per
recorded bus transaction, in file order.

```python
from aero1553 import MieFileReader

reader = MieFileReader("flight.mie")
first = next(iter(reader))
print(first.timestamp.format(), first.rt, first.msg_label, first.bus.name)
print(first.data_words[:4])
```

```text
365:23:59:59.950175 15 11R B
(1024, 0, 0, 47)
```

A reader can be iterated as many times as you like; each iteration starts at
the beginning. Counting the records:

```python
from aero1553 import MieFileReader

print(sum(1 for _ in MieFileReader("flight.mie")))
```

```text
723
```

`flight.mie` has a corrupt stretch in the middle. Reading it logs a warning
through Python's `logging` (see [Logging](#15-logging)) and carries on from the
next valid record -- the same recovery the CLI performs.

---

## 3. The record

A `MieMessage` is read-only. Its fields, and the shortcuts built from them:

| Attribute | Type | Meaning |
|---|---|---|
| `timestamp` | `IrigTimestamp` or `StandardTimestamp` | when the transaction was recorded; see [Time](#12-time) |
| `type_word` | `TypeWord` | the recorder's header: `message_type`, `bus`, `word_count`, `error`, `raw` |
| `message_format` | `MessageFormat` | the transaction shape: `RECEIVE`, `TRANSMIT`, `RT_TO_RT`, the mode codes, `SPURIOUS_DATA`, ... |
| `command_word`, `command_word_2` | `CommandWord` or `None` | `rt`, `direction`, `subaddress`, `data_word_count`, `raw`; the second only for RT-to-RT |
| `status_word`, `status_word_2` | `int` or `None` | the raw 16-bit status words |
| `data_words` | `tuple[int, ...]` | the data words, in bus order (up to 32) |
| `error_word` | `int` or `None` | the DDC error code (`0x01xx`) or decoder code (`0x20xx`) |
| `delta` | `float` or `None` | seconds since the previous transaction on the same RT and message |
| `file_offset` | `int` | byte offset of the record in the file |
| `mux` | `str` or `None` | the recorder name taken from the file name, if configured |
| `rt`, `subaddress` | `int` or `None` | from the command word; `None` for spurious data |
| `bus` | `Bus` | `Bus.A` or `Bus.B` |
| `msg_label` | `str` | the CSV's `MSG` column, e.g. `11R`; empty for spurious data |
| `is_error`, `is_spurious` | `bool` | an errored transaction; a `SPURIOUS_DATA` record |
| `error_label` | `str` | the CSV's `ERROR` column: `ERROR`, `SPURIOUS` or empty |

The enumerations (`Bus`, `Direction`, `MessageType`, `MessageFormat`, ...) are
Python `IntEnum`s in `aero1553.models`, so they compare equal to their numbers
and print as their names.

When the recorder saw a bus error, it truncated the transaction and appended
an error word; any words left over follow as a separate `SPURIOUS_DATA` record:

```python
from aero1553 import MieFileReader
from aero1553.models import DDC_ERROR_DESCRIPTIONS

messages = list(MieFileReader("flight.mie"))
i = next(i for i, m in enumerate(messages) if m.is_error)
errored, following = messages[i], messages[i + 1]
print(errored.msg_label, f"0x{errored.error_word:04X}", DDC_ERROR_DESCRIPTIONS[errored.error_word])
print(following.message_format.name, following.error_label, f"0x{following.error_word:04X}")
```

```text
11R 0x011E Manchester/Parity Error or Bit Count Error
SPURIOUS_DATA SPURIOUS 0x2000
```

`0x2000` marks spurious data that continues the error before it; standalone
spurious data is `0x2001`. [ERROR-CATALOG.md](ERROR-CATALOG.md) lists every code.

Records compare by value, hash, pickle, and can be copied with changes:

```python
import copy
import sys

from aero1553 import MieFileReader

m = next(iter(MieFileReader("flight.mie")))
changed = m.__replace__(delta=None) if sys.version_info < (3, 13) else copy.replace(m, delta=None)
print(changed == m, changed.rt == m.rt, changed.delta)
```

```text
False True None
```

`copy.replace` is Python 3.13+; on older versions call `__replace__` directly.
Records are not dataclasses -- they are compiled classes -- so
`dataclasses.replace` and `dataclasses.asdict` do not apply; use
[`to_dict()`](#11-dataclasses) instead.

---

## 4. Reading options

`MieFileReader(path, *, ...)` takes keyword options, each matching a CLI flag
and a configuration key:

| Option | Default | Meaning |
|---|---|---|
| `strict` | `False` | raise on the first corrupt record instead of recovering |
| `input_time_format` | `TimestampFormat.AUTO` | force `IRIG` or `STANDARD` instead of detecting it |
| `standard_tick_rate_hz` | `None` | the Standard counter's rate, which gives Standard records a `delta` |
| `detect_records`, `lookahead_records` | 8, 2 | how many records format detection and sync validation look at |
| `mux_enabled`, `mux_delimiter`, `mux_field` | `True`, `"."`, `4` | how the `mux` value is taken from the file name |

After a full iteration, `sync_losses` says how many corrupt stretches were
skipped:

```python
from aero1553 import MieFileReader
from aero1553.exceptions import Aero1553Error

reader = MieFileReader("flight.mie")
count = sum(1 for _ in reader)
print(count, "records,", reader.sync_losses, "corrupt stretch skipped")

strict = MieFileReader("flight.mie", strict=True)
try:
    list(strict)
except Aero1553Error as error:
    print("strict:", type(error).__name__)
```

```text
723 records, 1 corrupt stretch skipped
strict: MieUnknownTypeWordError
```

A Standard counter's rate is not stored in the file, so without it a Standard
record has no `delta`; with it, it does:

```python
from aero1553 import MieFileReader

plain = list(MieFileReader("counter.mie"))
rated = list(MieFileReader("counter.mie", standard_tick_rate_hz=1_000_000))
print(plain[5].delta, rated[5].delta)
```

```text
None 0.001435
```

---

## 5. Filtering and canonical order

`apply_filters` keeps or drops records by type, RT, bus or subaddress. Each
`exclude_*` set drops what it names; each non-empty `include_*` set keeps only
what it names. These are the CLI's `--exclude-*` / `--include-*` flags.
A spurious continuation (`error_word == 0x2000`) goes wherever its error goes,
so the first count below includes the continuations of RT 15's errors; only a
type filter judges it by its own type.

```python
from aero1553 import MieFileReader
from aero1553.config import FilterConfig
from aero1553.filters import apply_filters
from aero1553.models import Bus, MessageType

only_rt15_on_a = FilterConfig(include_rts={15}, include_buses={Bus.A})
print(sum(1 for _ in apply_filters(MieFileReader("flight.mie"), only_rt15_on_a)))

no_spurious_or_modes = FilterConfig(exclude_types={MessageType.SPURIOUS_DATA, MessageType.MODE_COMMAND})
print(sum(1 for _ in apply_filters(MieFileReader("flight.mie"), no_spurious_or_modes)))
```

```text
512
586
```

`order_rows` puts each run of records with the same timestamp into canonical
order -- by RT, then subaddress, receive before transmit -- which is the order
the CSV uses. It only reorders ties, never the timeline, and it keeps a
spurious record next to the record it continues.

```python
from aero1553 import MieFileReader
from aero1553.order import order_rows

raw = [m.file_offset for m in MieFileReader("flight.mie")]
ordered = [m.file_offset for m in order_rows(MieFileReader("flight.mie"))]
print("same records:", sorted(raw) == sorted(ordered))
print("same order:", raw == ordered)
moved = sum(a != b for a, b in zip(raw, ordered))
print(moved, "records change place")
```

```text
same records: True
same order: False
28 records change place
```

The CLI's pipeline is exactly *reader, then filters, then order, then writer*.
Build the same chain in Python and you get the CLI's result; the whole chain
runs in Rust, without a Python object per record, however you compose it.

---

## 6. Writing CSV

`aero1553.writer.write_csv` writes the CSV the `aero1553 decode` command
writes: the 44 columns of DDC's own recording software, in its order, then
`ERROR` and `ERROR_CODE`. The file is written to a temporary name and renamed
into place, so a reader never sees a half-written file.

### To a file

```python
from aero1553 import MieFileReader
from aero1553.order import order_rows
from aero1553.writer import write_csv

outcome = write_csv(order_rows(MieFileReader("flight.mie")), "flight.csv")
print(outcome.normal_count, "rows written")
```

```text
723 rows written
```

In this single-file mode every row -- errored and spurious ones included, with
their `ERROR` / `ERROR_CODE` columns filled -- counts in `normal_count`;
`error_count` is for the [separate errors file](#errors-in-a-separate-file).
That is byte for byte what the command line produces:

```python
from pathlib import Path

from aero1553 import MieFileReader
from aero1553.cli import main
from aero1553.order import order_rows
from aero1553.writer import write_csv

write_csv(order_rows(MieFileReader("flight.mie")), "library.csv")
main(["decode", "flight.mie", "-o", "cli.csv"])
print(Path("library.csv").read_bytes() == Path("cli.csv").read_bytes())
```

```text
True
```

### To a stream

Pass any object with a text `write` method -- `sys.stdout`, an open file, an
`io.StringIO`. Leaving the destination out writes to standard output.
`CSV_HEADER` lists the column names; `CSV_COLUMNS` pairs each with a
description.

```python
import io

from aero1553 import MieFileReader
from aero1553.writer import CSV_COLUMNS, CSV_HEADER, write_csv

buffer = io.StringIO()
write_csv(MieFileReader("flight.mie"), buffer)
lines = buffer.getvalue().splitlines()
print(len(CSV_HEADER), "columns:", ", ".join(CSV_HEADER[:3]), "...", ", ".join(CSV_HEADER[-3:]))
print(lines[0] == ",".join(CSV_HEADER))
print(lines[1][:60])
print(dict(CSV_COLUMNS)["RT"])
```

```text
46 columns: TIME_STAMP, RT, MSG ... XMT_GAP, ERROR, ERROR_CODE
True
365:23:59:59.950175,15,11R,0400,0000,0000,002F,CA22,002F,CA2
Remote Terminal address 0-30
```

### Errors in a separate file

`write_csv_split` puts clean rows in one file and errored and spurious rows in
`<name>_errors.csv` -- the CLI's `--separate-errors`. The errors file is only
created if there is something to put in it.

```python
from pathlib import Path

from aero1553 import MieFileReader
from aero1553.order import order_rows
from aero1553.writer import write_csv_split

outcome = write_csv_split(order_rows(MieFileReader("flight.mie")), "clean.csv")
print(outcome.normal_count, outcome.error_count)
print(sorted(p.name for p in Path(".").glob("clean*.csv")))
```

```text
667 56
['clean.csv', 'clean_errors.csv']
```

### Write options

`WriteOptions` carries the rest of the CLI's output flags:

| Field | CLI flag | Meaning |
|---|---|---|
| `no_clobber` | `--no-clobber` | refuse to replace an existing file -- checked at the moment of the rename, not just beforehand |
| `allow_partial` | `--allow-partial` | on unrecoverable corruption, keep what was decoded as `<name>.partial` rather than nothing |
| `time_render` | `--output-time-format`, `--year`, `--utc-offset` | how `TIME_STAMP` is written |

```python
from pathlib import Path

from aero1553 import MieFileReader
from aero1553.exceptions import MieClobberRefusedError
from aero1553.writer import WriteOptions, write_csv

Path("keep.csv").write_text("important\n", encoding="utf-8")
try:
    write_csv(MieFileReader("flight.mie"), "keep.csv", WriteOptions(no_clobber=True))
except MieClobberRefusedError:
    print("refused;", Path("keep.csv").read_text(encoding="utf-8").strip(), "is untouched")
```

```text
refused; important is untouched
```

### Dates in the CSV

By default `TIME_STAMP` is the recorder's day-of-year clock, as DDC's tool
writes it. For a calendar date, give a `TimeRender` with the year the
recording was made -- the file does not contain it. `utc_offset_minutes` is the
recorder clock's offset from UTC (here, five hours behind).

```python
import csv
import io

from aero1553 import MieFileReader
from aero1553.models import OutputTimeFormat, TimeRender
from aero1553.writer import WriteOptions, write_csv


def first_and_last(render):
    buffer = io.StringIO()
    write_csv(MieFileReader("flight.mie"), buffer, WriteOptions(time_render=render))
    rows = list(csv.DictReader(io.StringIO(buffer.getvalue())))
    return rows[0]["TIME_STAMP"], rows[-1]["TIME_STAMP"]


print(*first_and_last(TimeRender()))
print(*first_and_last(TimeRender(format=OutputTimeFormat.ISO, year=2024, utc_offset_minutes=-300)))
print(*first_and_last(TimeRender(format=OutputTimeFormat.DOM, year=2024)))
```

```text
365:23:59:59.950175 366:00:00:00.058025
2024-12-30T23:59:59.950175-05:00 2024-12-31T00:00:00.058025-05:00
30:23:59:59.950175 31:00:00:00.058025
```

A date the year does not have is refused rather than guessed: `flight.mie`
reaches day 366, which 2026 does not have.

```python
import io

from aero1553 import MieFileReader
from aero1553.exceptions import MieCalendarUnavailableError
from aero1553.models import OutputTimeFormat, TimeRender
from aero1553.writer import WriteOptions, write_csv

render = TimeRender(format=OutputTimeFormat.ISO, year=2026)
try:
    write_csv(MieFileReader("flight.mie"), io.StringIO(), WriteOptions(time_render=render))
except MieCalendarUnavailableError:
    print("2026 has no day 366")
```

```text
2026 has no day 366
```

(The [table functions](#12-time) mark such a record as having no date instead
of refusing the whole table.)

### One row at a time

`message_to_row` gives one record as a dict keyed by CSV column -- the exact
strings the CSV would hold:

```python
from aero1553 import MieFileReader
from aero1553.writer import message_to_row

row = message_to_row(next(iter(MieFileReader("flight.mie"))))
print({key: row[key] for key in ("TIME_STAMP", "RT", "MSG", "WD01", "STAT", "BUS")})
```

```text
{'TIME_STAMP': '365:23:59:59.950175', 'RT': '15', 'MSG': '11R', 'WD01': '0400', 'STAT': '7800', 'BUS': 'B'}
```

### Reading the CSV back

Every CSV column is text, and several are legitimately empty, so read them as
strings:

```python
import csv

from aero1553 import MieFileReader
from aero1553.writer import write_csv

write_csv(MieFileReader("flight.mie"), "flight.csv")
with open("flight.csv", newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
errors = [row for row in rows if row["ERROR"]]
print(len(rows), len(errors), errors[0]["ERROR"], errors[0]["ERROR_CODE"])
```

```text
723 56 SPURIOUS 2001
```

---

## 7. Merging recorders

Several recorders on the same bus make several files. `merge_readers`
interleaves them into one time-ordered stream; with `collapse_duplicates=True`
a transaction that more than one recorder captured appears once.

```python
from aero1553 import MieFileReader
from aero1553.merge import merge_readers

def readers():
    return [MieFileReader("flight.mie"), MieFileReader("flight-2.mie")]

each = [sum(1 for _ in r) for r in readers()]
merged = sum(1 for _ in merge_readers(readers()))
collapsed = sum(1 for _ in merge_readers(readers(), collapse_duplicates=True))
print(each, merged, collapsed)
```

```text
[723, 380] 1103 1025
```

A merge needs every input on a calendar-locked IRIG clock -- otherwise there is
no common timeline -- and says so rather than guessing:

```python
from aero1553 import MieFileReader
from aero1553.exceptions import MieIncompatibleMergeInputsError
from aero1553.merge import merge_readers

try:
    merge_readers([MieFileReader("flight.mie"), MieFileReader("counter.mie")])
except MieIncompatibleMergeInputsError:
    print("counter.mie has no IRIG clock to merge on")
```

```text
counter.mie has no IRIG clock to merge on
```

Other options: `collapse_window_us` (how far apart two copies of a transaction
may be timestamped), `delta_scope` (`DeltaScope.PER_FILE` keeps each file's
`delta`; `GLOBAL` recomputes it on the merged timeline), `allow_partial` and
`strict`. For file lists, `read_manifest(path)` and `expand_glob(pattern)`
resolve inputs exactly as the CLI's `--manifest` and `--glob` do. The merged
stream feeds `apply_filters`, `order_rows`, `write_csv` and `columns` like any
other.

---

## 8. Tables: choosing a route

There are three ways to get a recording into a table, and they cost very
different amounts:

| Route | Use it for | Speed (460,575 records, Linux) |
|---|---|---|
| `aero1553.columns(stream)` | NumPy, pandas, any analysis of a whole recording | about 0.6 s to a DataFrame |
| `message.to_dict()` | a few records, dataclasses, JSON | about 3.8 s to a DataFrame |
| write CSV, then `pandas.read_csv` | when you want the CSV file anyway | about 3.0 s |

`columns()` builds its table in Rust and hands NumPy and pandas each column
without copying, so it creates no Python object per record. Per-record routes
create about twenty Python objects per record, and that -- not pandas -- is
where their time goes. Numbers are from the [performance suite](#18-performance).

Both views share one schema, `aero1553.table.FIELDS`:

```python
from aero1553.table import FIELDS

print(len(FIELDS))
print(", ".join(FIELDS))
```

```text
25
file_offset, timestamp, time_us, ticks, freerun, day_of_year, time_of_day_us, datetime, message_type, message_format, bus, error, rt, subaddress, direction, msg_label, command_word, command_word_2, status_word, status_word_2, error_word, data_word_count, data_words, delta, mux
```

`datetime` appears only when you give a year (see [Time](#12-time)). A value
the record does not have -- spurious data has no RT and so no `delta`; a
Standard recording has no `delta` without its tick rate -- is `None` in a
dict. (The first message on an RT/message has a `delta` of `0.0`, as in the
CSV.) A column is a typed array and cannot
hold `None`, so it follows NumPy's conventions: **`-1`** for a missing integer,
**`NaN`** for a missing float, **`NaT`** for a missing date.

```python
from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"))
print(type(cols["rt"]).__name__, cols["rt"].format, len(cols["rt"]))
print(type(cols["msg_label"]).__name__, cols["msg_label"][:3])
print(cols["data_words"].shape if len(cols["data_words"]) else "empty")
```

```text
memoryview b 723
list ['11R', '0R', '22R']
(723, 32)
```

Numeric columns are Python `memoryview`s -- usable directly, and wrapped by
NumPy or pandas without a copy. Text columns (`timestamp`, `msg_label`, `mux`)
are lists. `data_words` is a two-dimensional `(records, 32)` table, padded with
zeros; `data_word_count` says how many of each row are real.

`fields=` picks columns, and is the one knob that matters for speed: the text
columns are the only ones that create a Python object per record.

```python
from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"), fields=["rt", "subaddress", "delta"])
print(list(cols))
```

```text
['rt', 'subaddress', 'delta']
```

`columns()` takes any record stream -- a reader, a filtered or ordered stream,
a merge, or a plain list of records.

---

## 9. NumPy

```python
import numpy as np

from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"))
rt = np.asarray(cols["rt"])
error = np.asarray(cols["error"])
print(rt.dtype, rt.shape, error.dtype)

# -1 marks "no RT" (spurious data): mask it out before counting.
per_rt = np.bincount(rt[rt >= 0])
busiest = int(per_rt.argmax())
print("busiest RT:", busiest, "with", int(per_rt[busiest]), "messages")
print("errored transactions:", int(error.sum()))
```

```text
int8 (723,) bool
busiest RT: 15 with 566 messages
errored transactions: 23
```

Data words as a matrix -- one row per record, 32 columns, zero-padded:

```python
import numpy as np

from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"))
words = np.asarray(cols["data_words"])
counts = np.asarray(cols["data_word_count"])
print(words.dtype, words.shape)

# Word 1 of every RT 15 receive to subaddress 11:
rt = np.asarray(cols["rt"])
sa = np.asarray(cols["subaddress"])
direction = np.asarray(cols["direction"])
chosen = (rt == 15) & (sa == 11) & (direction == 0)
print(int(chosen.sum()), "messages; first words:", words[chosen, 0][:3].tolist())
print("every row's real words are counted:", bool((counts <= 32).all()))
```

```text
uint16 (723, 32)
219 messages; first words: [1024, 1024, 1024]
every row's real words are counted: True
```

Missing floats are `NaN`, so NumPy's `nan*` functions skip them:

```python
import numpy as np

from aero1553 import MieFileReader, columns

delta = np.asarray(columns(MieFileReader("flight.mie"), fields=["delta"])["delta"])
print(int(np.isnan(delta).sum()), "records without a delta")
print(f"median gap on an RT/message: {np.nanmedian(delta) * 1e3:.3f} ms")
```

```text
33 records without a delta
median gap on an RT/message: 0.519 ms
```

Dates: with a year, `datetime` is microseconds since 1970 (UTC) in an `int64`
column; view it as `datetime64[us]`, and a missing date shows as `NaT`.

```python
import numpy as np

from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"), fields=["datetime", "day_of_year"], year=2024)
when = np.asarray(cols["datetime"]).view("datetime64[us]")
print(when[0], when[-1])
print(sorted(set(np.asarray(cols["day_of_year"]).tolist())))
```

```text
2024-12-30T23:59:59.950175 2024-12-31T00:00:00.058025
[365, 366]
```

---

## 10. pandas

Build a DataFrame from the columns -- the numeric columns are wrapped without
a copy -- and keep `data_words` beside it as a NumPy matrix (a DataFrame column
of arrays would cost a Python object per row):

```python
import numpy as np
import pandas as pd

from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"), year=2024)
words = np.asarray(cols.pop("data_words"))
frame = pd.DataFrame({name: np.asarray(col) if isinstance(col, memoryview) else col for name, col in cols.items()})
frame["datetime"] = frame["datetime"].to_numpy().view("datetime64[us]")
print(frame.shape, words.shape)
first = frame.iloc[0]
print(first["timestamp"], int(first["rt"]), first["msg_label"], int(first["bus"]), first["datetime"])
```

```text
(723, 24) (723, 32)
365:23:59:59.950175 15 11R 1 2024-12-30 23:59:59.950175
```

Turning the `-1` sentinels into pandas' missing value, for columns where you
want pandas' own handling:

```python
import numpy as np
import pandas as pd

from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"), fields=["rt", "subaddress", "msg_label"])
frame = pd.DataFrame({name: np.asarray(col) if isinstance(col, memoryview) else col for name, col in cols.items()})
frame["rt"] = frame["rt"].astype("Int16").mask(frame["rt"] < 0)
print(int(frame["rt"].isna().sum()), "records have no RT")
top = frame.dropna(subset=["rt"]).groupby(["rt", "msg_label"]).size().sort_values(ascending=False).head(3)
print([(int(rt), label, int(n)) for (rt, label), n in top.items()])
```

```text
33 records have no RT
[(15, '11R', 219), (15, '22T', 184), (15, '22R', 127)]
```

Message rate per RT, per second of recording:

```python
import numpy as np
import pandas as pd

from aero1553 import MieFileReader, columns

cols = columns(MieFileReader("flight.mie"), fields=["rt", "datetime"], year=2024)
frame = pd.DataFrame({"rt": np.asarray(cols["rt"]), "when": np.asarray(cols["datetime"]).view("datetime64[us]")})
frame = frame[frame["rt"] >= 0]
seconds = (frame["when"].max() - frame["when"].min()).total_seconds()
rates = (frame.groupby("rt").size() / seconds).sort_values(ascending=False).head(3)
print(f"{seconds:.3f} s of recording")
print([(int(rt), round(rate, 1)) for rt, rate in rates.items()])
```

```text
0.108 s of recording
[(15, 5248.0), (5, 593.4), (31, 556.3)]
```

Filtering in the decoder first is cheaper than filtering in pandas -- rows that
are never built cost nothing:

```python
import numpy as np
import pandas as pd

from aero1553 import MieFileReader, columns
from aero1553.config import FilterConfig
from aero1553.filters import apply_filters

stream = apply_filters(MieFileReader("flight.mie"), FilterConfig(include_rts={15}))
frame = pd.DataFrame({name: np.asarray(col) for name, col in columns(stream, fields=["rt", "delta"]).items()})
print(len(frame), sorted(frame["rt"].unique().tolist()))
```

```text
589 [-1, 15]
```

The `-1` rows are the spurious continuations of RT 15's errors: an `include_rts`
filter keeps a continuation with the error it continues, and a record with no
Command Word has no RT, which `columns()` writes as `-1`.

And the CSV route, for when you want the file anyway. Read every column as
text -- several are legitimately empty, and pandas would otherwise turn the hex
data words into numbers or `NaN`:

```python
import pandas as pd

from aero1553 import MieFileReader
from aero1553.writer import write_csv

write_csv(MieFileReader("flight.mie"), "flight.csv")
frame = pd.read_csv("flight.csv", dtype=str, keep_default_na=False)
print(frame.shape, frame.loc[0, "WD01"], repr(frame.loc[0, "MUX"]))
```

```text
(723, 46) 0400 ''
```

A DataFrame's own `to_csv` does not write the vendor-compatible layout; use
`write_csv` (above) when the file is meant to be compared with DDC's output.

---

## 11. Dataclasses

`to_dict()` gives one record as plain Python values -- the same fields as
`columns()`, with `None` for anything missing. `fields=` picks the ones you
want, which is what makes filling your own dataclass a one-liner:

```python
from dataclasses import dataclass, fields

from aero1553 import MieFileReader


@dataclass(frozen=True, slots=True)
class Transaction:
    timestamp: str
    rt: int | None
    subaddress: int | None
    msg_label: str
    data_words: tuple[int, ...]
    delta: float | None


wanted = [f.name for f in fields(Transaction)]
rows = [Transaction(**m.to_dict(fields=wanted)) for m in MieFileReader("flight.mie")]
print(len(rows))
print(rows[0])
```

```text
723
Transaction(timestamp='365:23:59:59.950175', rt=15, subaddress=11, msg_label='11R', data_words=(1024, 0, 0, 47, 51746, 47, 51746, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 51057), delta=0.0)
```

The enumerations arrive as numbers; convert back where you want the names:

```python
from dataclasses import dataclass

from aero1553 import MieFileReader
from aero1553.models import Bus, MessageFormat


@dataclass
class Summary:
    rt: int | None
    bus: Bus
    message_format: MessageFormat

    @classmethod
    def of(cls, message):
        d = message.to_dict(fields=["rt", "bus", "message_format"])
        return cls(d["rt"], Bus(d["bus"]), MessageFormat(d["message_format"]))


first = Summary.of(next(iter(MieFileReader("flight.mie"))))
print(first.rt, first.bus.name, first.message_format.name)
```

```text
15 B RECEIVE
```

Dataclasses turn into JSON with the standard library:

```python
import json
from dataclasses import asdict, dataclass, fields

from aero1553 import MieFileReader


@dataclass
class Row:
    timestamp: str
    rt: int | None
    msg_label: str
    data_words: tuple[int, ...]


wanted = [f.name for f in fields(Row)]
row = Row(**next(iter(MieFileReader("flight.mie"))).to_dict(fields=wanted))
print(json.dumps(asdict(row))[:80])
```

```text
{"timestamp": "365:23:59:59.950175", "rt": 15, "msg_label": "11R", "data_words":
```

For a whole recording, prefer [`columns()`](#8-tables-choosing-a-route): a
dataclass per record is the per-record route, with its cost.

---

## 12. Time

An MIE recording holds one of two kinds of timestamp, and the table fields
describe both without inventing anything the file does not say.

**IRIG** is a clock reading -- day of the year, hour, minute, second,
microsecond -- with **no year**. A `freerun` flag says the recorder's clock was
not locked to a time source, so the reading is only the recorder's own count.

**Standard** is a raw counter. Its rate is not stored in the file and it has
no starting date: only the difference between two records means anything, and
only once you know the rate.

| Field | IRIG record | Standard record |
|---|---|---|
| `timestamp` | `DAY:HH:MM:SS.uuuuuu`, as in the CSV | the counter, as in the CSV |
| `day_of_year`, `time_of_day_us` | the clock reading | missing |
| `freerun` | `True` if the clock was not locked | missing (`False` in a column) |
| `ticks` | missing | the raw counter |
| `time_us` | microseconds on the day-of-year clock | microseconds, only with `standard_tick_rate_hz` |
| `datetime` | only with `year=`; missing if freerun or the day is not in that year | always missing |

`time_us` follows the rule the decoder uses for `delta`, so a record has a
`time_us` exactly when it can have a `delta`.

### Without a year

There is no `datetime` field, and nothing fails -- the clock reading is still
all there:

```python
from aero1553 import MieFileReader

d = next(iter(MieFileReader("flight.mie"))).to_dict()
print("datetime" in d, d["day_of_year"], d["time_of_day_us"], d["time_us"])
```

```text
False 365 86399950175 31622399950175
```

Why not a day of the month instead? Because that needs the year too: day 60
is 1 March in 2026 but 29 February in 2024, and every later day shifts with it.

Asking for `datetime` by name without a year *is* an error -- you asked for
something the file cannot answer:

```python
from aero1553 import MieFileReader, columns

try:
    columns(MieFileReader("flight.mie"), fields=["datetime"])
except ValueError as error:
    print(str(error).split(":")[0])
```

```text
the datetime field needs year=
```

### With a year

`to_dict()` gives a timezone-aware `datetime`; a column gives
microseconds since 1970 in UTC (see [NumPy](#9-numpy)). `utc_offset_minutes`
is the recorder clock's offset from UTC.

```python
from aero1553 import MieFileReader

records = list(MieFileReader("flight.mie"))
first, last = records[0], records[-1]
print(first.to_dict(year=2024)["datetime"])
print(last.to_dict(year=2024)["datetime"])
print(first.to_dict(year=2024, utc_offset_minutes=-300)["datetime"])
print(last.to_dict(year=2026)["datetime"])
```

```text
2024-12-30 23:59:59.950175+00:00
2024-12-31 00:00:00.058025+00:00
2024-12-30 23:59:59.950175-05:00
None
```

Day 366 does not exist in 2026, so that record has no date (`None`, or `NaT`
in a column) -- the rest of the table is unaffected. The CSV writer, which has
no way to mark a single cell as undated, refuses the file instead (see
[Dates in the CSV](#dates-in-the-csv)).

### Standard and freerun recordings

```python
from aero1553 import MieFileReader, columns

plain = columns(MieFileReader("counter.mie"), fields=["ticks", "time_us"], year=2024)
rated = columns(MieFileReader("counter.mie"), fields=["ticks", "time_us"], standard_tick_rate_hz=1_000_000)
print(plain["ticks"][0], plain["time_us"][0], rated["time_us"][0])

freerun = MieFileReader("freerun.mie")
d = next(iter(freerun)).to_dict(year=2024)
print(d["freerun"], d["datetime"], d["time_us"] is not None)
```

```text
2379 -1 2379
True None True
```

---

## 13. Configuration files

A TOML configuration file sets defaults for the CLI and for your own scripts
alike. `load_config` returns a `DecoderConfig`; its `filters` field feeds
`apply_filters` directly. [CONFIG-REFERENCE.md](CONFIG-REFERENCE.md) lists
every key.

```python
from pathlib import Path

from aero1553 import MieFileReader
from aero1553.config import load_config
from aero1553.filters import apply_filters

Path("site.toml").write_text(
    '[filter]\nexclude_types = ["SPURIOUS_DATA"]\nexclude_rts = [15]\n\n[output]\nyear = 2024\n',
    encoding="utf-8",
)
config = load_config("site.toml")
print(sorted(config.filters.exclude_rts), config.year)
kept = apply_filters(MieFileReader("flight.mie"), config.filters)
print(sum(1 for _ in kept))
```

```text
[15] 2024
124
```

A key the decoder does not know is reported as a warning rather than silently
ignored, and a bad value is a `ValueError` naming the key.

---

## 14. The hex dump

For looking at the raw bytes -- a record at a time, annotated, or a plain hex
view:

```python
from aero1553.dump import hex_dump_raw, hex_dump_records

hex_dump_records("flight.mie", max_records=1)
hex_dump_raw("flight.mie", start_offset=0, length=32)
```

```text
File: flight.mie (32306 bytes)
Record dump starting at offset 0x00000000

------------------------------------------------------------------------
  Record #0  @  0x00000000  (72 bytes, 36 words)
  Type:   0x2482  ->  BC->RT (Receive)  Bus B  error flag (bit 14): clear
  Format: RECEIVE
  Time:   365:23:59:59.950175
  Cmd:    0x797E  ->  RT15 SA11 R WC=30
    00000000  82 24 B7 2D BE EF 9F 7F 7E 79 00 04 00 00 00 00   |.$.-....~y......|
    00000010  2F 00 22 CA 2F 00 22 CA 00 00 00 00 00 00 00 00   |/."./.".........|
    00000020  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00   |................|
    00000030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00   |................|
    00000040  00 00 00 00 71 C7 00 78                           |....q..x|

------------------------------------------------------------------------
1 records dumped.
File: flight.mie (32306 bytes)
Range: 0x00000000-0x00000020

  00000000  82 24 B7 2D BE EF 9F 7F 7E 79 00 04 00 00 00 00   |.$.-....~y......|
  00000010  2F 00 22 CA 2F 00 22 CA 00 00 00 00 00 00 00 00   |/."./.".........|
```

Both write to standard output unless given a `stream=`.

---

## 15. Logging

The decoder reports what it notices -- a corrupt stretch skipped, a freerun
clock, an unknown configuration key -- through Python's standard `logging`,
under the `aero1553` logger and its children (`aero1553.reader`,
`aero1553.merge`, ...). Configure them like any other logger:

```python
import logging

from aero1553 import MieFileReader


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append((record.name, record.levelname, record.getMessage()))


collect = Collect()
package = logging.getLogger("aero1553")
package.setLevel(logging.WARNING)  # INFO adds format detection and progress notes
package.addHandler(collect)
list(MieFileReader("flight.mie"))
package.removeHandler(collect)
print(collect.messages)
```

```text
[('aero1553.reader', 'WARNING', 'sync lost at 0x3F32 (type=0x7F wc=63); scanning forward')]
```

For quick scripts, `aero1553.logger.configure_logging("INFO")` sends the
package's messages to standard error in the CLI's format.

---

## 16. Errors

Everything the package raises derives from `aero1553.exceptions.Aero1553Error`:

| Class | Raised when |
|---|---|
| `MieFileError` | the input file is the problem: `MieFileNotFoundError`, `MieFileEmptyError`, `MieFileIoError`, `MieNoValidRecordsError`, `MieHomogeneousPayloadError`, `MieTimestampFormatMismatchError`, `MieIncompatibleMergeInputsError`, `MieClobberRefusedError`, `MieInputOutputCollisionError`, `MieCalendarUnavailableError` |
| `MieRecordError` | one record is the problem (in `strict` mode, or beyond recovery): `MieInvalidTypeWordError`, `MieUnknownTypeWordError`, `MieRecordTruncatedError`, `MieFirstRecordTruncatedError`, `MiePayloadError`, `MieUnrecoverableSyncLossError` |
| `MieWriterError` | the output could not be written (a full disk, a directory in the way) |
| `MieNonMonotonicInputError` | a merge input's time went backwards |

```python
from aero1553 import MieFileReader
from aero1553.exceptions import Aero1553Error, MieFileError, MieFileNotFoundError

try:
    MieFileReader("no-such-file.mie")
except MieFileNotFoundError as error:
    print(isinstance(error, MieFileError), isinstance(error, Aero1553Error))
```

```text
True True
```

[ERROR-CATALOG.md](ERROR-CATALOG.md) describes every error and the CLI exit
code it maps to.

---

## 17. Running the CLI from Python

`aero1553.cli.main(argv)` runs a command line in-process -- the same code as
the `aero1553` program -- and returns its exit status instead of exiting:

```python
from aero1553.cli import EXIT_OK, main

status = main(["count", "flight.mie"])
print("exit", status, status == EXIT_OK)
```

```text
723
exit 0 True
```

`count` printed the number to standard output, as the program does. Output
goes to the process's real standard output and error, so capture it at that
level (a subprocess, or pytest's `capfd`) rather than by swapping `sys.stdout`.

---

## 18. Performance

The repository's performance suite ([perf/README.md](../perf/README.md)) times
the library against the Rust crate it wraps on every pull request. On a
460,575-record recording (21 MB), on Linux:

| Task | Time | Records/s |
|---|---|---|
| iterate every record in Python | 0.16 s | 2.8 million |
| decode to CSV (`write_csv`) | 1.3 s | 350,000 -- the same as the Rust CLI |
| `columns()` to NumPy | 0.48 s | 970,000 |
| `columns()` to pandas, numeric fields only | 0.15 s | 3.1 million |
| `columns()` to pandas, all fields | 0.56 s | 820,000 |
| `to_dict()` per record, then pandas | 3.8 s | 120,000 |
| CSV, then `pandas.read_csv` | 3.0 s | 156,000 |
| `import pandas` (once per process) | 0.3 s | |

What that means in practice:

- **Keep work in the pipeline.** Chaining `apply_filters`, `order_rows`,
  `merge_readers`, `write_csv` and `columns` runs in Rust end to end; a Python
  loop over the records in between is where time goes.
- **For a table, use `columns()`,** and pass `fields=` without the text columns
  when you do not need them.
- **Filter in the decoder,** not after: rows that are never built cost
  nothing.
- **Per-record objects are the expensive part.** `to_dict()` and dataclasses
  are right for a handful of records, and the slow route for a whole file.

---

## 19. Supported platforms

- **Python:** CPython 3.10 to 3.14. One wheel per platform serves every one of
  them (a `cp310-abi3` wheel).
- **Linux:** x86_64, glibc 2.17 or newer and kernel 3.2 or newer -- the floor
  of the Rust standard library the decoder is built with.
- **Windows:** x86_64, Windows 10 or Windows Server 2016 or newer.
- **Anywhere else** Python and Rust both run, the package builds from source.

Which of these are published as ready-made wheels is settled at release; see
the project's release notes.
