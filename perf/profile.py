"""Where a decode spends its CPU: user and system time per thread (Linux).

Run from the repository root, after ``cargo build --release`` in ``rust/``::

    python3 perf/profile.py [--cli rust/target/release/aero1553] -- decode BIG.mie -o out.csv
    python3 perf/profile.py --stdin BIG.mie -- decode - -o out.csv
    python3 perf/profile.py --perf -- decode BIG.mie -o out.csv   # + a perf profile

A decode runs on the calling thread -- read, validate, decode, filter, order,
merge and write -- with rendering threads formatting CSV beside it (L2-WRT-027).
Since 5.2.0 the calling thread is what a decode waits on, so the first question
before any optimisation is how its time divides between user code and the
kernel. ``perf`` at the usual ``perf_event_paranoid=2`` does not show kernel
time, so this samples ``/proc/<pid>/task/*/stat`` instead -- the method the
5.5.0 profile used by hand.

It is a measurement only: it runs the CLI exactly as given and changes nothing
about how it runs. The report gives, per thread, user and system CPU seconds
and their share of the wall time. The calling thread is the one whose id is the
process id; the others are rendering threads.

With ``--perf`` it also records the run with ``perf record -g`` (when ``perf``
is installed) and prints the top symbols of the calling thread, which is where
an optimisation would start. The two runs are separate: a profiled run is
slower, so its times are not mixed into the table.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "rust" / "target" / "release" / "aero1553"
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _cpu_ticks(stat_path: Path) -> tuple[int, int] | None:
    """(utime, stime) in clock ticks from a ``/proc/.../stat`` file, or None
    once the task has gone."""
    try:
        text = stat_path.read_text(encoding="ascii")
    except OSError:
        return None
    # The command name is in parentheses and may hold spaces; fields resume
    # after the last ')'. utime and stime are fields 14 and 15 (1-based).
    fields = text[text.rindex(")") + 2 :].split()
    return int(fields[11]), int(fields[12])


def _peak_rss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def sample(cmd: list[str], stdin: Path | None, interval: float) -> dict[str, object]:
    """Run ``cmd`` and sample its threads' CPU time until it exits.

    Raises:
        SystemExit: if the command exits non-zero -- a failed run's times
            describe nothing.
    """
    with stdin.open("rb") if stdin else Path(os.devnull).open("rb") as feed:
        start = time.perf_counter()
        proc = subprocess.Popen(cmd, stdin=feed, stdout=subprocess.DEVNULL)
        ticks: dict[int, tuple[int, int]] = {}
        peak = None
        while proc.poll() is None:
            task_dir = Path(f"/proc/{proc.pid}/task")
            try:
                tids = [int(p.name) for p in task_dir.iterdir()]
            except OSError:
                break
            for tid in tids:
                got = _cpu_ticks(task_dir / str(tid) / "stat")
                if got is not None:
                    ticks[tid] = got
            peak = _peak_rss_kib(proc.pid) or peak
            time.sleep(interval)
        code = proc.wait()
        wall = time.perf_counter() - start
    if code != 0:
        raise SystemExit(f"the command exited {code}: {' '.join(cmd)}")
    return {"pid": proc.pid, "ticks": ticks, "wall": wall, "peak_kib": peak}


def report(result: dict[str, object], interval: float) -> str:
    pid = result["pid"]
    ticks: dict[int, tuple[int, int]] = result["ticks"]  # type: ignore[assignment]
    wall: float = result["wall"]  # type: ignore[assignment]
    lines = [
        f"wall {wall:.3f} s, peak RSS {result['peak_kib'] or 0:,} KiB, "
        f"{len(ticks)} threads, {os.cpu_count()} CPUs",
        "",
        "| Thread | User s | System s | Busy (of wall) |",
        "|---|---:|---:|---:|",
    ]
    others = 0
    for tid, (user, system) in sorted(ticks.items(), key=lambda kv: (kv[0] != pid, kv[0])):
        if tid == pid:
            name = "calling (read, decode, order, write)"
        else:
            others += 1
            name = f"rendering {others}"
        busy = (user + system) / TICK / wall
        lines.append(f"| {name} | {user / TICK:.2f} | {system / TICK:.2f} | {busy:.0%} |")
    lines += [
        "",
        f"Sampled every {interval * 1000:.0f} ms; CPU time counts in "
        f"{1 / TICK * 1000:.0f} ms ticks, and up to one interval at the end of a "
        "thread's life is not seen.",
    ]
    return "\n".join(lines)


def perf_top(cmd: list[str], stdin: Path | None, pid_hint: str) -> str:
    """The calling thread's top symbols under ``perf record``."""
    if shutil.which("perf") is None:
        return "perf is not installed; skipped the symbol profile."
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "perf.data"
        with stdin.open("rb") if stdin else Path(os.devnull).open("rb") as feed:
            subprocess.run(
                ["perf", "record", "-q", "-F", "999", "-g", "-o", str(data), "--", *cmd],
                stdin=feed,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        out = subprocess.run(
            [
                "perf",
                "report",
                "-i",
                str(data),
                "--stdio",
                "--no-children",
                "--sort",
                "tid,symbol",
                "--percent-limit",
                "1",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    keep = [line for line in out.splitlines() if line and not line.startswith("#")]
    return f"perf report (threads and symbols over 1%, {pid_hint}):\n" + "\n".join(keep[:60])


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("profile.py reads /proc and runs on Linux only.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cli", type=Path, default=CLI, help=f"The CLI to run (default {CLI}).")
    parser.add_argument("--stdin", type=Path, help="A file to feed the CLI as standard input.")
    parser.add_argument("--perf", action="store_true", help="Also record a perf symbol profile.")
    parser.add_argument("--interval", type=float, default=0.02, help="Seconds between samples.")
    parser.add_argument("args", nargs="+", help="The CLI's arguments, after --.")
    a = parser.parse_args()
    cmd = [str(a.cli), *a.args]
    print(report(sample(cmd, a.stdin, a.interval), a.interval))
    if a.perf:
        print()
        print(perf_top(cmd, a.stdin, "the calling thread is the lowest tid"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
