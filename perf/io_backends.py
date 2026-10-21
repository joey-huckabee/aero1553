"""EXPERIMENT (branch perf/io-backends): time the reader's three I/O backends.

One release binary holds all three; ``AERO1553_IO`` picks one per process
(``mmap`` / ``readall`` / ``stream:BYTES``), so code layout is identical across
variants. Every run is a fresh process. Each round runs every (case, variant)
pair once, in a shuffled order, so drift on the machine lands on all variants
alike.

Measured per run: wall time, user and system CPU, peak resident memory, page
faults; on Linux also the peak of anonymous (heap) and file-backed (mapped)
resident memory, sampled from /proc, because a mapping's pages count towards
RSS while a read buffer's do not.

Before any timing, a correctness pass decodes every input under every variant
and requires the CSV (stdout) and the log (stderr) to be byte-identical to the
mmap variant's.

    python perf/io_backends.py --cli BIN --bench BENCH --data DIR --rounds 7 \\
        --out report.json [--cold] [--cases iterate,count,decode,merge]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

VARIANTS = ["mmap", "readall", "stream:65536", "stream:262144", "stream:1048576"]
LINUX = sys.platform.startswith("linux")


def evict(paths: list[Path]) -> None:
    """Drop each input's pages from the page cache (Linux, no root needed)."""
    for p in paths:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


def _sample_proc(pid: int, peaks: dict, stop: threading.Event) -> None:
    path = f"/proc/{pid}/status"
    while not stop.is_set():
        try:
            for line in Path(path).read_text().splitlines():
                key = line.split(":", 1)[0]
                if key in ("RssAnon", "RssFile"):
                    kb = int(line.split()[1])
                    peaks[key] = max(peaks.get(key, 0), kb * 1024)
        except (OSError, ValueError, IndexError):
            pass
        stop.wait(0.005)


def _windows_stats(handle: int) -> dict:
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
    ft = [wintypes.FILETIME() for _ in range(4)]
    ctypes.windll.kernel32.GetProcessTimes(handle, *[ctypes.byref(f) for f in ft])

    def secs(f: wintypes.FILETIME) -> float:
        return ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7

    return {
        "user_s": secs(ft[3]),
        "sys_s": secs(ft[2]),
        "peak_rss": pmc.PeakWorkingSetSize,
        "peak_private": pmc.PeakPagefileUsage,
        "faults": pmc.PageFaultCount,
    }


def run_once(argv: list[str], variant: str, stdout=subprocess.DEVNULL) -> dict:
    env = {**os.environ, "AERO1553_IO": variant}
    start = time.perf_counter()
    p = subprocess.Popen(argv, env=env, stdout=stdout, stderr=subprocess.DEVNULL)
    if LINUX:
        peaks: dict = {}
        stop = threading.Event()
        t = threading.Thread(target=_sample_proc, args=(p.pid, peaks, stop), daemon=True)
        t.start()
        _, status, ru = os.wait4(p.pid, 0)
        wall = time.perf_counter() - start
        stop.set()
        t.join()
        p.returncode = os.waitstatus_to_exitcode(status)
        stats = {
            "user_s": ru.ru_utime,
            "sys_s": ru.ru_stime,
            "peak_rss": ru.ru_maxrss * 1024,
            "minflt": ru.ru_minflt,
            "majflt": ru.ru_majflt,
            "peak_anon": peaks.get("RssAnon"),
            "peak_file": peaks.get("RssFile"),
        }
    else:
        p.wait()
        wall = time.perf_counter() - start
        stats = _windows_stats(int(p._handle))  # noqa: SLF001
    if p.returncode != 0:
        raise RuntimeError(f"{argv} under {variant} exited {p.returncode}")
    return {"wall_s": wall, **stats}


def digest(argv: list[str], variant: str) -> tuple[str, str]:
    env = {**os.environ, "AERO1553_IO": variant}
    r = subprocess.run(argv, env=env, capture_output=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{argv} under {variant} exited {r.returncode}: {r.stderr[-400:]!r}")
    out = r.stdout
    if out.startswith(b'{"case"'):
        # The bench prints its own timings; compare what it counted.
        j = json.loads(out)
        out = f"{j['rows']} {j['rt_sum']}".encode()
    return hashlib.sha256(out).hexdigest(), hashlib.sha256(r.stderr).hexdigest()


def cases(args, files: dict[str, list[Path]]) -> dict[str, list[str]]:
    out = {}
    for name, paths in files.items():
        p = [str(x) for x in paths]
        if len(paths) > 1:
            if "merge" in args.cases:
                out[f"merge/{name}"] = [args.cli, "decode", *p]
            continue
        if "iterate" in args.cases:
            out[f"iterate/{name}"] = [
                args.bench, "--case", "iterate", "--input", p[0], "--repeat", "1"
            ]
        if "count" in args.cases:
            out[f"count/{name}"] = [args.cli, "count", p[0]]
        if "decode" in args.cases:
            out[f"decode/{name}"] = [args.cli, "decode", p[0]]
    return out


def bootstrap_ratio_ci(a: list[float], b: list[float], n: int = 4000) -> tuple[float, float]:
    rng = random.Random(1553)
    ratios = sorted(
        statistics.median(rng.choices(a, k=len(a))) / statistics.median(rng.choices(b, k=len(b)))
        for _ in range(n)
    )
    return ratios[int(0.025 * n)], ratios[int(0.975 * n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", required=True)
    ap.add_argument("--bench", required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--files", default="", help="comma list of input names under --data")
    ap.add_argument("--merge", default="", help="glob under --data for a merge set")
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--cases", default="iterate,count,decode,merge")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    variants = args.variants.split(",")

    files = {n: [args.data / n] for n in args.files.split(",") if n}
    if args.merge:
        files[f"merge{len(sorted(args.data.glob(args.merge)))}"] = sorted(
            args.data.glob(args.merge)
        )
    plan = cases(args, files)
    inputs = {c: [Path(a) for a in argv if Path(a).is_file() and a not in (args.cli, args.bench)]
              for c, argv in plan.items()}

    verify = {}
    if not args.skip_verify:
        for c, argv in plan.items():
            ref = digest(argv, "mmap")
            for v in variants:
                got = digest(argv, v) if v != "mmap" else ref
                verify[f"{c}|{v}"] = got == ref
                print(f"verify {c:40s} {v:16s} {'OK' if got == ref else 'MISMATCH'}", flush=True)
        if not all(verify.values()):
            print("correctness check failed; not timing", file=sys.stderr)
            args.out.write_text(json.dumps({"verify": verify}, indent=1))
            return 1

    results: dict[str, dict[str, list[dict]]] = {c: {v: [] for v in variants} for c in plan}
    rng = random.Random(42)
    for r in range(args.rounds):
        order = [(c, v) for c in plan for v in variants]
        rng.shuffle(order)
        for c, v in order:
            if args.cold and LINUX:
                evict(inputs[c])
            m = run_once(plan[c], v)
            results[c][v].append(m)
        print(f"round {r + 1}/{args.rounds} done", flush=True)

    summary = {}
    for c, by_v in results.items():
        base = [m["wall_s"] for m in by_v["mmap"]] if "mmap" in by_v else None
        summary[c] = {}
        for v, ms in by_v.items():
            walls = [m["wall_s"] for m in ms]
            row = {
                "median_s": statistics.median(walls),
                "min_s": min(walls),
                "max_s": max(walls),
                "user_s": statistics.median(m["user_s"] for m in ms),
                "sys_s": statistics.median(m["sys_s"] for m in ms),
                "peak_rss_mb": max(m["peak_rss"] for m in ms) / 2**20,
            }
            for k in ("peak_anon", "peak_file", "peak_private"):
                vals = [m.get(k) for m in ms if m.get(k) is not None]
                if vals:
                    row[f"{k}_mb"] = max(vals) / 2**20
            for k in ("minflt", "majflt", "faults"):
                vals = [m[k] for m in ms if k in m]
                if vals:
                    row[k] = statistics.median(vals)
            if base and v != "mmap":
                row["ratio_vs_mmap"] = statistics.median(walls) / statistics.median(base)
                row["ratio_ci95"] = bootstrap_ratio_ci(walls, base)
            summary[c][v] = row
    args.out.write_text(
        json.dumps(
            {
                "platform": sys.platform,
                "cold": args.cold,
                "rounds": args.rounds,
                "verify": verify,
                "summary": summary,
                "runs": results,
            },
            indent=1,
        )
    )
    for c, by_v in summary.items():
        print(f"\n{c}")
        for v, row in by_v.items():
            ratio = row.get("ratio_vs_mmap")
            ci = row.get("ratio_ci95")
            extra = f"  x{ratio:.3f} [{ci[0]:.3f},{ci[1]:.3f}]" if ratio else ""
            mem = row.get("peak_anon_mb", row.get("peak_private_mb", 0))
            print(
                f"  {v:16s} med {row['median_s']:8.3f}s  user {row['user_s']:7.3f}"
                f"  sys {row['sys_s']:6.3f}  rss {row['peak_rss_mb']:8.1f}MB"
                f"  anon/private {mem:8.1f}MB{extra}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
