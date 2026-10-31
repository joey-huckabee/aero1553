"""Pairwise flag combinations: every implementation, and the rules that must
hold whatever the flags.

WHY PAIRS. Every other check varies one thing at a time: a conformance case
pins one flag on one input, and the fuzzers vary the input under default flags.
A defect that needs two flags to appear -- the 2026-09 review found
``--separate-errors`` with ``--output-time-format`` broken in one
implementation, on a combination no test ran -- passes all of them. Every
combination of 19 decode settings is billions of runs; every PAIR of their
values fits in about a hundred, and most interaction defects need only two.

WHAT IS CHECKED, per generated command line:

1. **Implementations agree** -- exit code, stdout, stderr and every file
   written, compared all-pairs (``differential.py``'s rule: no majority, no
   reference implementation).

The rest are properties of the contract, checked against the FIRST
implementation only. Since v4.0.0 the Python CLI runs the Rust CLI in-process
(check 1 covers that hop), so repeating them per implementation would double
the runtime to re-test the same code.

2. **Sane outcome** -- a documented exit code (never a panic's 101), ASCII
   diagnostics (L2-CLI-014), and on success a CSV that starts with the header.
3. **Thread count is invisible** (L2-WRT-027) -- the same run at
   ``--threads`` 0, 1 and 3 gives identical bytes, exit code and stderr.
4. **Destination is invisible** -- an inline decode written to a file and to
   stdout gives the same CSV.
5. **``--separate-errors`` partitions** -- its main and errors files hold
   exactly the inline CSV's rows: the clean ones and the errored ones, each in
   their inline order (L2-WRT-019).
6. **Standard input is a file** (L2-CLI-023) -- a single-input decode from
   ``-`` gives what the file gives decoded with ``--no-mux``.
7. **Filters only remove** -- a filtered run's rows are a sub-multiset of the
   unfiltered run's rows, and every row kept matches the filter.

Deterministic: a fixed seed builds the same combinations every run
(``MIE_FLAG_PAIRS_SEED`` to vary it). A finding prints the full command line.
"""

from __future__ import annotations

import csv
import io
import itertools
import os
import random
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from differential import describe_stderr_problems

_DEFAULT_SEED = 20261031
#: The exit codes the CLI documents (L2-CLI-011). 101 is a Rust panic.
_DOCUMENTED_EXITS = {0, 1, 2, 3, 4, 5, 6}
_HEADER_START = b"TIME_STAMP,RT,MSG,"

#: Input sets: name -> fixtures. The names carry a MUX field (index 4 when
#: split on "."), so the default MUX population is exercised too.
INPUTS: dict[str, list[str]] = {
    "basic": ["basic-multi-record"],
    "errors": ["errors-inline"],
    "continuation": ["filter-continuation"],
    "resync": ["header-and-sync-recovery"],
    "truncated": ["truncated-tail-short"],
    "standard": ["standard-timestamps"],
    "ties": ["tie-spurious-pinned"],
    "empty": ["empty-recording"],
    "not-mie": ["text-not-mie"],
    "merge": ["merge-a", "merge-b"],
    "merge-dup": ["merge-continuation-a", "merge-collapse-b-continuation-copy"],
    # Real traffic from the golden generator (tests/golden). Ten copies of
    # a-small are ~7,200 records: more than three of the rendering threads'
    # 2048-record batches, which the thread-count check needs to mean anything.
    "golden-x10": ["golden:a-small*10"],
    "golden-merge": ["golden:a-small", "golden:b-small"],
}

#: Each factor: name -> its values, each value the arguments it adds. ``()`` is
#: "flag absent" -- the default -- which is a value like any other.
FACTORS: dict[str, list[tuple[str, ...]]] = {
    "input": [(name,) for name in INPUTS],
    "source": [("file",), ("stdin",)],
    "dest": [("file",), ("stdout",)],
    "errors": [(), ("--separate-errors",)],
    "strict": [(), ("--strict",)],
    "partial": [(), ("--allow-partial",)],
    "clobber": [(), ("--no-clobber",)],
    "in-time": [(), ("--input-time-format", "irig"), ("--input-time-format", "standard")],
    "out-time": [
        (),
        ("--output-time-format", "doy"),
        ("--output-time-format", "iso", "--year", "2024", "--utc-offset", "+02:00"),
        ("--output-time-format", "dom", "--year", "2024"),
    ],
    "detect": [(), ("--detect-records", "1"), ("--detect-records", "32")],
    "lookahead": [(), ("--lookahead-records", "1"), ("--lookahead-records", "5")],
    "tick": [(), ("--standard-tick-rate-hz", "1000000")],
    "mux": [(), ("--no-mux",), ("--mux-delimiter", "-", "--mux-field", "-1")],
    "collapse": [
        (),
        ("--collapse-duplicates",),
        ("--collapse-duplicates", "--collapse-window-us", "50"),
    ],
    "delta": [(), ("--delta-scope", "global")],
    "sort-group": [(), ("--max-sort-group", "1"), ("--max-sort-group", "2")],
    "survivors": [(), ("--max-collapse-survivors", "1")],
    "threads": [(), ("--threads", "0"), ("--threads", "3")],
    "filter": [
        (),
        ("--include-rts", "15"),
        ("--exclude-rts", "15"),
        ("--exclude-buses", "B"),
        ("--include-subaddresses", "11,22"),
        ("--exclude-types", "SPURIOUS_DATA"),
    ],
}

#: Factors whose value is not a plain argument list.
_SPECIAL = ("input", "source", "dest")

_MERGES = {name for name, fixtures in INPUTS.items() if len(fixtures) > 1}
_STANDARD = {"standard"}


def _excluded(a: str, x: tuple[str, ...], b: str, y: tuple[str, ...]) -> bool:
    """Value pairs that can only end in a refusal pinned elsewhere, so a row
    holding one would spend every OTHER pair in it on a run that never
    decodes. Each refusal has its own conformance case."""
    pair = {a: x, b: y}
    inp = pair.get("input", ("",))[0]
    if pair.get("source") == ("stdin",) and inp in _MERGES:
        return True  # stdin in a merge: usage error (stdin-parity, cli tests)
    if pair.get("in-time", ("",))[-1:] == ("standard",) and inp and inp not in _STANDARD:
        return True  # IRIG data read as Standard: no valid records
    # A calendar date from a Standard counter: refused.
    return pair.get("out-time", ())[1:2] in (("iso",), ("dom",)) and inp in _STANDARD


def pairwise(factors: dict[str, list[tuple[str, ...]]], seed: int) -> list[dict[str, int]]:
    """Rows (factor -> value index) covering every pair of values of every two
    factors at least once. Greedy: each row takes, factor by factor in a
    shuffled order, the value covering most still-uncovered pairs with the
    choices already made; ties go to the random generator, so the set is
    deterministic for a seed."""
    rng = random.Random(seed)
    names = list(factors)
    uncovered = {
        (a, i, b, j)
        for a, b in itertools.combinations(names, 2)
        for i in range(len(factors[a]))
        for j in range(len(factors[b]))
        if not _excluded(a, factors[a][i], b, factors[b][j])
    }
    rows: list[dict[str, int]] = []
    while uncovered:
        # Seed each row with an uncovered pair, so every row makes progress.
        seed_pair = rng.choice(sorted(uncovered))
        for _ in range(20):
            row = _complete(factors, names, uncovered, seed_pair, rng)
            if row is not None:
                break
        else:
            # Allowed on its own, but no valid row holds it (with a third
            # value it is always one of the excluded pairs): infeasible.
            uncovered.discard(seed_pair)
            continue
        for x, y in itertools.combinations(names, 2):
            uncovered.discard((x, row[x], y, row[y]))
        rows.append(row)
    return rows


def _complete(
    factors: dict[str, list[tuple[str, ...]]],
    names: list[str],
    uncovered: set[tuple[str, int, str, int]],
    seed_pair: tuple[str, int, str, int],
    rng: random.Random,
) -> dict[str, int] | None:
    """A full row holding ``seed_pair``, or None at a dead end."""
    a, i, b, j = seed_pair
    row = {a: i, b: j}
    for name in rng.sample(names, len(names)):
        if name in row:
            continue
        best: list[int] = []
        best_gain = -1
        for value in range(len(factors[name])):
            if any(_excluded(x, factors[x][row[x]], name, factors[name][value]) for x in row):
                continue
            gain = sum(
                (
                    (x, row[x], name, value)
                    if names.index(x) < names.index(name)
                    else (name, value, x, row[x])
                )
                in uncovered
                for x in row
            )
            if gain > best_gain:
                best, best_gain = [value], gain
            elif gain == best_gain:
                best.append(value)
        if not best:
            return None
        row[name] = rng.choice(best)
    return row


class Run:
    """One CLI invocation's whole outcome."""

    def __init__(self, code: int, stdout: bytes, stderr: str, files: dict[str, bytes]) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.files = files

    def key(self) -> tuple[object, ...]:
        return (self.code, self.stdout, self.stderr, tuple(sorted(self.files.items())))

    def csv(self) -> bytes:
        """The CSV this run produced: stdout, or its main output file."""
        for name in ("out.csv", "out.csv.partial"):
            if name in self.files:
                return self.files[name]
        return self.stdout


def _rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("ascii", "replace"))))


class Pairs:
    def __init__(self, invocations: dict[str, list[str]], root: Path, suite: Path, temp: Path):
        import importlib.util

        from run import read_hex  # local import: run.py imports this module

        spec = importlib.util.spec_from_file_location(
            "golden", root / "tests" / "golden" / "golden.py"
        )
        assert spec is not None and spec.loader is not None
        golden = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(golden)
        golden_dir = temp / "flag-pairs" / "golden"
        golden_dir.mkdir(parents=True, exist_ok=True)

        def load(fixture: str) -> bytes:
            if not fixture.startswith("golden:"):
                return read_hex(suite / "inputs" / f"{fixture}.hex")
            name, _, times = fixture[len("golden:") :].partition("*")
            one: bytes = Path(golden.write(name, golden_dir)).read_bytes()
            # Each copy but the last loses its end-of-records terminator, or
            # the decode would stop after the first. The clock steps back at
            # each joint: a non-monotonic recording, an edge case of its own.
            body = one.removesuffix(b"\x00\x00")
            return body * (int(times or 1) - 1) + one

        self.invocations = invocations
        self.root = root
        self.temp = temp / "flag-pairs"
        self.inputs_dir = self.temp / "in"
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        self.files: dict[str, list[Path]] = {}
        self.data: dict[str, bytes] = {}
        for name, fixtures in INPUTS.items():
            paths = []
            for k, fixture in enumerate(fixtures):
                path = self.inputs_dir / f"{name}.rec.a.b.R{k + 7}.mie"
                data = load(fixture)
                path.write_bytes(data)
                self.data[path.name] = data
                paths.append(path)
            self.files[name] = paths
        self.count = 0

    def command(
        self, settings: dict[str, tuple[str, ...]], prefix: list[str], out: Path
    ) -> tuple[list[str], bytes | None]:
        """The command line for ``settings``, and the bytes to feed as stdin."""
        inputs = self.files[settings["input"][0]]
        stdin = None
        if settings["source"] == ("stdin",):
            # A merge input set from stdin is a usage error worth reaching too:
            # the stream stands in for the first file, the rest stay files.
            sources = ["-", *(str(p) for p in inputs[1:])]
            stdin = self.data[inputs[0].name]
        else:
            sources = [str(p) for p in inputs]
        args = [*prefix, "decode", *sources]
        for name, value in settings.items():
            if name not in _SPECIAL:
                args += value
        if settings["dest"] == ("file",):
            args += ["-o", str(out / "out.csv")]
        return args, stdin

    def run(self, settings: dict[str, tuple[str, ...]], prefix: list[str]) -> tuple[Run, list[str]]:
        self.count += 1
        out = self.temp / f"out-{self.count}"
        out.mkdir()
        cmd, stdin = self.command(settings, prefix, out)
        result = subprocess.run(
            cmd,
            cwd=self.root,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )
        stderr = result.stderr.decode("utf-8", "surrogateescape")
        for path, placeholder in ((str(out), "<OUT>"), (str(self.inputs_dir), "<IN>")):
            stderr = stderr.replace(path, placeholder)
        files = {p.name: p.read_bytes() for p in sorted(out.iterdir())}
        shutil.rmtree(out)
        return Run(result.returncode, result.stdout, stderr, files), cmd


def _shown(cmd: list[str], root: Path) -> str:
    return " ".join(arg.replace(str(root), ".") for arg in cmd[1:])


def check_flag_pairs(
    invocations: dict[str, list[str]], root: Path, suite: Path, temp: Path
) -> None:
    """Run every pairwise combination; raise listing every finding.

    Raises:
        AssertionError: on any divergence or broken property.
    """
    seed = int(os.environ.get("MIE_FLAG_PAIRS_SEED", _DEFAULT_SEED))
    rows = pairwise(FACTORS, seed)
    pairs = Pairs(invocations, root, suite, temp)
    findings: list[str] = []
    checked: Counter[str] = Counter()
    for row in rows:
        settings = {name: FACTORS[name][index] for name, index in row.items()}
        _check_row(pairs, settings, invocations, root, findings, checked)

    if findings:
        shown = findings[:10]
        raise AssertionError(
            f"flag-pairs: {len(findings)} finding(s) over {len(rows)} combinations "
            f"(seed {seed}); the first {len(shown)}:\n\n" + "\n\n".join(shown)
        )
    detail = ", ".join(f"{label} x{n}" for label, n in checked.items())
    print(
        f"PASS flag-pairs ({len(rows)} combinations covering every pair of "
        f"{sum(len(v) for v in FACTORS.values())} values over {len(FACTORS)} settings, "
        f"seed {seed}; {pairs.count} runs: {detail})"
    )


def _check_row(
    pairs: Pairs,
    settings: dict[str, tuple[str, ...]],
    invocations: dict[str, list[str]],
    root: Path,
    findings: list[str],
    checked: Counter[str],
) -> None:
    """Every check for one combination, appending what it finds."""
    first_impl, first_prefix = next(iter(invocations.items()))

    def finding(what: str, cmd: list[str]) -> None:
        findings.append(f"{what}\n      aero1553 {_shown(cmd, root)}")

    # 1. Every implementation, all-pairs.
    runs = {impl: pairs.run(settings, prefix) for impl, prefix in invocations.items()}
    base, cmd = runs[first_impl]
    for impl, (other, _) in runs.items():
        if other.key() != base.key():
            what = [
                label
                for label, a, b in (
                    ("exit code", base.code, other.code),
                    ("stdout", base.stdout, other.stdout),
                    ("stderr", base.stderr, other.stderr),
                    ("files", base.files, other.files),
                )
                if a != b
            ]
            finding(f"{first_impl} and {impl} differ in {', '.join(what)}", cmd)
    checked["implementations agree"] += 1

    # 2. A sane outcome.
    if base.code not in _DOCUMENTED_EXITS:
        finding(f"undocumented exit code {base.code}:\n{base.stderr}", cmd)
    if problem := describe_stderr_problems({first_impl: base.stderr}, compare=False):
        finding(problem, cmd)
    if base.code == 0 and base.csv() and not base.csv().startswith(_HEADER_START):
        finding("exit 0 but the CSV does not start with the header", cmd)
    checked["sane outcome"] += 1

    def again(**change: tuple[str, ...]) -> tuple[Run, list[str]]:
        return pairs.run({**settings, **change}, first_prefix)

    def same(label: str, other: Run, other_cmd: list[str], *, stderr: bool = True) -> None:
        mine = base.key() if stderr else (base.code, base.csv())
        theirs = other.key() if stderr else (other.code, other.csv())
        if mine != theirs:
            finding(f"{label}: differs from\n      aero1553 {_shown(other_cmd, root)}", cmd)
        checked[label] += 1

    # 3. Thread count.
    for threads in (("--threads", "0"), ("--threads", "1"), ("--threads", "3")):
        if threads != settings["threads"]:
            same("thread count is invisible", *again(threads=threads))

    # 4. Destination (inline only: stdout cannot be split, and says so).
    inline_to_file = settings["dest"] == ("file",) and settings["errors"] == ()
    if inline_to_file and base.code == 0 and "out.csv" in base.files:
        same("destination is invisible", *again(dest=("stdout",)), stderr=False)

    # 5. --separate-errors partitions the inline CSV.
    if settings["errors"] and settings["dest"] == ("file",) and base.code == 0:
        inline, inline_cmd = again(errors=())
        _check_partition(base, inline, cmd, inline_cmd, root, finding)
        checked["--separate-errors partitions"] += 1

    # 6. Standard input against the file, MUX aside.
    if len(pairs.files[settings["input"][0]]) == 1:
        if settings["source"] == ("stdin",):
            stream, stream_cmd = base, cmd
            from_file, file_cmd = again(source=("file",), mux=("--no-mux",))
        else:
            stream, stream_cmd = again(source=("stdin",))
            from_file, file_cmd = again(mux=("--no-mux",))
        name = pairs.files[settings["input"][0]][0]
        want = from_file.stderr.replace(f"<IN>{os.sep}{name.name}", "<stdin>")
        got = (stream.code, stream.csv(), stream.files.keys(), stream.stderr)
        if got != (from_file.code, from_file.csv(), from_file.files.keys(), want):
            shown = _shown(file_cmd, root)
            finding(f"standard input differs from the file:\n      aero1553 {shown}", stream_cmd)
        checked["standard input is a file"] += 1

    # 7. Filters only remove rows.
    if settings["filter"] and base.code == 0:
        unfiltered, unfiltered_cmd = again(filter=())
        if unfiltered.code == 0:
            _check_subset(settings["filter"], base, unfiltered, cmd, unfiltered_cmd, root, finding)
            checked["filters only remove"] += 1


def _check_partition(
    split: Run,
    inline: Run,
    cmd: list[str],
    inline_cmd: list[str],
    root: Path,
    finding: Callable[[str, list[str]], None],
) -> None:
    if inline.code != split.code:
        finding(f"--separate-errors exit {split.code}, inline exit {inline.code}", cmd)
        return
    rows = _rows(inline.csv())
    if not rows:
        return
    header, body = rows[0], rows[1:]
    if "ERROR" not in header or any(len(r) != len(header) for r in body):
        finding("the inline CSV is malformed: a row's width differs from the header's", inline_cmd)
        return
    err = header.index("ERROR")
    clean = [r for r in body if not r[err]]
    errored = [r for r in body if r[err]]
    main = _rows(split.files.get("out.csv", b""))
    errors = _rows(split.files.get("out_errors.csv", b""))
    if main[:1] != [header] or main[1:] != clean or (errors[1:] if errors else []) != errored:
        finding(
            f"--separate-errors does not partition the inline rows "
            f"({len(clean)} clean + {len(errored)} errored inline; "
            f"{len(main) - 1} main + {max(len(errors) - 1, 0)} errors split):\n"
            f"      aero1553 {_shown(inline_cmd, root)}",
            cmd,
        )


def _check_subset(
    flt: tuple[str, ...],
    filtered: Run,
    unfiltered: Run,
    cmd: list[str],
    unfiltered_cmd: list[str],
    root: Path,
    finding: Callable[[str, list[str]], None],
) -> None:
    rows = _rows(filtered.csv())
    every = _rows(unfiltered.csv())
    if not rows or not every:
        return
    header = rows[0]
    if any(len(r) != len(header) for r in rows[1:]):
        finding("the CSV is malformed: a row's width differs from the header's", cmd)
        return
    kept = Counter(tuple(r) for r in rows[1:])
    have = Counter(tuple(r) for r in every[1:])
    extra = kept - have
    if extra:
        finding(
            f"the filter ADDED or changed {sum(extra.values())} row(s), e.g. {next(iter(extra))}:\n"
            f"      aero1553 {_shown(unfiltered_cmd, root)}",
            cmd,
        )
        return
    flag, value = flt
    column = {"rts": "RT", "buses": "BUS"}.get(flag.split("-", 3)[-1])
    if column is None:
        return
    values = set(value.split(","))
    at = header.index(column)
    for r in rows[1:]:
        if not r[at]:
            continue  # spurious continuations carry no RT
        matches = r[at] in values
        if matches != flag.startswith("--include"):
            finding(f"{flag} {value} kept a row with {column}={r[at]}", cmd)
            return
