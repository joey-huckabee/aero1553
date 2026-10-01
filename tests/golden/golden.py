"""The golden recordings: one generator, pinned by hash, for tests and benchmarks.

MIE binaries are not committed (repository hygiene), and a benchmark needs a
recording far larger than any fixture. So the golden data is *generated* --
deterministically, from a fixed seed -- and the generator is pinned: the
SHA-256 of every recording it writes, and of the CSV the CLI decodes it to,
is committed in ``golden.json``. A change to the generator, to a template
fixture, or to the decoder's output shows up as a hash mismatch rather than
as a quietly different benchmark.

Every record is a real encoding, not a hand-built one: the templates are the
records of the shared conformance fixtures (``tests/conformance/inputs``),
re-timed onto one clock. Between them they cover every transaction type the
decoder knows -- BC->RT, RT->BC, RT->RT, both broadcasts, the five mode-code
shapes, bus B, an errored record with its SPURIOUS continuation, and
standalone SPURIOUS data.

Recordings (``NAMES``)
----------------------
``a-small``
    IRIG, every record kind, equal-timestamp runs (canonical order), one
    corrupt region (sync loss and recovery), and a clock that runs from day
    365 into day 366 -- which is 31 December in a leap year and does not exist
    in a common one.
``b-small``
    A second recorder on the same bus: one transaction in five is an exact
    duplicate of one of ``a-small``'s (collapsed by a merge with
    ``collapse_duplicates``); the rest are its own, interleaved in time.
``standard-small``
    Standard (free-running counter) timestamps.
``freerun-small``
    IRIG with the clock not locked to a time source.
``a-large`` / ``b-large``
    ``a-small`` / ``b-small`` at scale -- about 460 000 and 320 000 records
    (20.8 MB and 14.4 MB) -- for the performance suite.

File names follow the operator naming the MUX column reads (field 4), so a
decode fills MUX with ``aa`` / ``bb`` / ``std`` / ``fr``.

Usage::

    python tests/golden/golden.py --out DIR [NAME ...]      # write + verify
    python tests/golden/golden.py --update --cli PATH       # re-pin golden.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "conformance" / "inputs"
MANIFEST = HERE / "golden.json"

#: Conformance fixtures whose records serve as IRIG templates, with a weight
#: (how often the generator draws from it).
IRIG_TEMPLATES: list[tuple[str, int]] = [
    ("basic-multi-record", 30),
    ("bus-b", 8),
    ("rt-to-rt", 6),
    ("rt-to-rt-broadcast", 3),
    ("receive-broadcast", 3),
    ("mode-code-tx-data", 3),
    ("mode-code-rx-data", 3),
    ("mode-code-no-data", 2),
    ("mode-code-tx-no-data", 2),
    ("mode-code-bcast-data", 2),
    ("mode-code-bcast-no-data", 2),
    ("errors-inline", 4),
    ("spurious-standalone", 2),
]
STANDARD_TEMPLATES: list[tuple[str, int]] = [("standard-timestamps", 1)]

#: Junk between two records of ``a-*``: an unknown message type, so the
#: reader loses sync there and recovers at the next record.
CORRUPT = bytes.fromhex("FFFF1337DEADBEEF")

US_PER_DAY = 86_400_000_000

#: name -> (file name, recipe). Times are microseconds on the IRIG
#: day-of-year clock (day 0 at 0).
RECIPES: dict[str, dict] = {
    "a-small": {
        "file": "golden.draw.data.1553.aa.unused.mie_irig",
        "groups": 700,
        "seed": 1553,
        "start_us": 365 * US_PER_DAY + 86_399_950_000,  # day 365, 23:59:59.95
        "corrupt": True,
    },
    "b-small": {
        "file": "golden.draw.data.1553.bb.unused.mie_irig",
        "partner": "a-small",
        "groups": 300,
        "seed": 1554,
    },
    "standard-small": {
        "file": "golden.draw.data.1553.std.unused.mie_std",
        "groups": 200,
        "seed": 1555,
        "standard": True,
    },
    "freerun-small": {
        "file": "golden.draw.data.1553.fr.unused.mie_irig",
        "groups": 40,
        "seed": 1556,
        "start_us": 3 * US_PER_DAY,
        "freerun": True,
    },
    "a-large": {
        "file": "golden-large.draw.data.1553.aa.unused.mie_irig",
        "groups": 450_000,
        "seed": 2553,
        "start_us": 192 * US_PER_DAY,
        "corrupt": True,
    },
    "b-large": {
        "file": "golden-large.draw.data.1553.bb.unused.mie_irig",
        "partner": "a-large",
        "groups": 250_000,
        "seed": 2554,
    },
}
NAMES = tuple(RECIPES)


def _fixture(name: str) -> bytes:
    text = (FIXTURES / f"{name}.hex").read_text(encoding="utf-8")
    return bytes.fromhex("".join(line.split("#", 1)[0].strip() for line in text.splitlines()))


def _records(data: bytes) -> list[bytes]:
    """Split a fixture into records by each Type Word's word count."""
    out, offset = [], 0
    while offset + 2 <= len(data):
        type_word = int.from_bytes(data[offset : offset + 2], "little")
        if type_word == 0:
            break
        length = ((type_word >> 8) & 0x3F) * 2
        out.append(data[offset : offset + length])
        offset += length
    return out


def _groups(names: list[tuple[str, int]]) -> list[tuple[list[bytes], int]]:
    """Template transactions with weights. An errored record keeps the
    SPURIOUS record that follows it, so the pair stays a continuation."""
    groups = []
    for name, weight in names:
        records = _records(_fixture(name))
        i = 0
        while i < len(records):
            group = [records[i]]
            errored = (int.from_bytes(records[i][:2], "little") >> 14) & 1
            nxt = records[i + 1] if i + 1 < len(records) else None
            if errored and nxt is not None and nxt[0] & 0x7F == 0x20:
                group.append(nxt)
                i += 1
            groups.append((group, weight))
            i += 1
    return groups


def _irig(total_us: int, freerun: bool) -> bytes:
    day, rest = divmod(total_us, US_PER_DAY)
    seconds, us = divmod(rest, 1_000_000)
    minutes, sec = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    upper = (int(freerun) << 15) | (day << 5) | hour
    middle = (minute << 10) | (sec << 4) | ((us >> 16) & 0xF)
    lower = us & 0xFFFF
    return b"".join(w.to_bytes(2, "little") for w in (upper, middle, lower))


def _retime(record: bytes, when: int, *, standard: bool, freerun: bool) -> bytes:
    if standard:
        ticks = when & 0xFFFF_FFFF
        words = (ticks >> 16).to_bytes(2, "little") + (ticks & 0xFFFF).to_bytes(2, "little")
        return record[:2] + words + record[6:]
    return record[:2] + _irig(when, freerun) + record[8:]


def _vary_payload(record: bytes, rng: random.Random) -> bytes:
    """A Standard BC->RT record with fresh data words.

    The Standard templates are few, and a run of records identical in every
    non-timestamp word is exactly what the reader rejects as a homogeneous
    pad (L2-SYN-018). The data words are payload -- nothing validates them --
    so varying them makes the recording realistic without making it invalid.
    Layout: Type Word, 2 timestamp words, Command Word, data, Status Word.
    """
    if record[0] & 0x7F != 0x02:
        return record
    command = int.from_bytes(record[6:8], "little")
    count = command & 0x1F or 32
    data = b"".join(rng.getrandbits(16).to_bytes(2, "little") for _ in range(count))
    return record[:8] + data + record[8 + 2 * count :]


def _schedule(recipe: dict, rng: random.Random) -> list[tuple[int, list[bytes]]]:
    """(time, group) for every transaction of a recording."""
    standard = recipe.get("standard", False)
    pool = _groups(STANDARD_TEMPLATES if standard else IRIG_TEMPLATES)
    groups = [g for g, _ in pool]
    weights = [w for _, w in pool]
    when = recipe.get("start_us", 0) if not standard else 1_000
    out = []
    for i in range(recipe["groups"]):
        # One transaction in twenty shares its predecessor's timestamp: the
        # runs the canonical-order stage sorts.
        if i == 0 or rng.random() >= 0.05:
            when += rng.randint(20, 300) * (1 if not standard else 7)
        out.append((when, rng.choices(groups, weights)[0]))
    return out


def build(name: str) -> bytes:
    """The bytes of recording ``name``: deterministic for a given generator."""
    recipe = RECIPES[name]
    rng = random.Random(recipe["seed"])
    standard = recipe.get("standard", False)
    freerun = recipe.get("freerun", False)
    if "partner" in recipe:
        # Own transactions, plus every fifth of the partner's verbatim.
        partner = RECIPES[recipe["partner"]]
        base = _schedule(partner, random.Random(partner["seed"]))
        own = _schedule({**partner, "groups": recipe["groups"]}, rng)
        shifted = [(t + 37, g) for t, g in own]  # never on a partner timestamp
        shared = base[::5][: recipe["groups"] // 4]
        schedule = sorted(shifted + shared, key=lambda item: item[0])
        corrupt_at = None
    else:
        schedule = _schedule(recipe, rng)
        corrupt_at = len(schedule) // 2 if recipe.get("corrupt") else None

    out = bytearray()
    for i, (when, group) in enumerate(schedule):
        if i == corrupt_at:
            out += CORRUPT
        for record in group:
            record = _retime(record, when, standard=standard, freerun=freerun)
            out += _vary_payload(record, rng) if standard else record
    out += b"\x00\x00"  # the end-of-records terminator
    return bytes(out)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def write(name: str, directory: Path, *, verify: bool = True) -> Path:
    """Write recording ``name`` into ``directory``; check it against the pin.

    Raises:
        RuntimeError: if ``verify`` is set and the built bytes do not match the
            SHA-256 pinned in golden.json (nothing is written in that case).
    """
    data = build(name)
    if verify:
        pinned = manifest()["recordings"][name]["sha256"]
        if sha256(data) != pinned:
            raise RuntimeError(
                f"golden recording {name} does not match golden.json "
                f"(built {sha256(data)}, pinned {pinned}); the generator or a "
                "template fixture changed -- re-pin with --update if intended"
            )
    path = directory / RECIPES[name]["file"]
    path.write_bytes(data)
    return path


def _decode(cli: Path, *args: str) -> tuple[str, int, int]:
    """SHA-256 of the CSV the CLI decodes to, its row count, and the sum of
    its RT column -- the fingerprint a benchmark that writes no CSV checks."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.csv"
        subprocess.run([str(cli), "decode", *args, "-o", str(out)], check=True, capture_output=True)
        data = out.read_bytes()
    lines = data.decode("utf-8").splitlines()[1:]
    rt_sum = sum(int(field) for line in lines if (field := line.split(",", 2)[1]))
    return sha256(data), len(lines), rt_sum


def update(cli: Path) -> None:
    """Re-pin every recording and the CSV the CLI decodes it to."""
    pinned: dict = {"recordings": {}, "merges": {}}
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: write(name, Path(tmp), verify=False) for name in NAMES}
        for name, path in paths.items():
            csv_sha, rows, rt_sum = _decode(cli, str(path))
            pinned["recordings"][name] = {
                "file": RECIPES[name]["file"],
                "sha256": sha256(path.read_bytes()),
                "bytes": path.stat().st_size,
                "rows": rows,
                "rt_sum": rt_sum,
                "csv_sha256": csv_sha,
            }
        for a, b in (("a-small", "b-small"), ("a-large", "b-large")):
            csv_sha, rows, rt_sum = _decode(
                cli, str(paths[a]), str(paths[b]), "--collapse-duplicates"
            )
            pinned["merges"][f"{a}+{b}"] = {"rows": rows, "rt_sum": rt_sum, "csv_sha256": csv_sha}
    MANIFEST.write_bytes((json.dumps(pinned, indent=2) + "\n").encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*", help=f"Recordings to write (default: all of {NAMES}).")
    parser.add_argument("--out", type=Path, help="Directory to write the recordings into.")
    parser.add_argument("--update", action="store_true", help="Re-pin golden.json.")
    parser.add_argument("--cli", type=Path, help="The aero1553 binary, for --update.")
    args = parser.parse_args()
    unknown = [n for n in args.names if n not in NAMES]
    if unknown:
        parser.error(f"unknown recording(s) {unknown}; choose from {NAMES}")
    if args.update:
        if args.cli is None:
            parser.error("--update needs --cli")
        update(args.cli)
        return 0
    if args.out is None:
        parser.error("--out is required")
    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.names or NAMES:
        print(write(name, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
