"""Contract check: an output file's permissions are the platform default.

L3-WRT-001 requires every file the decoder commits to be created asking for
mode ``0666``, so what lands on disk is ``0666 & ~umask`` -- what ``touch``, a
shell redirect and every other ordinary tool would produce. Rust's
``OpenOptions`` asks for ``0666``, and the Python package writes through the
Rust extension. The C++ build asked for ``0644``, which agrees with the other
two only under a umask of ``022``: under ``002`` (a per-user-group default) it
wrote ``0644`` where they wrote ``0664``, so a group-shared output directory
held CSVs whose group-write bit depended on which implementation produced them.

The manifest cannot express this -- it compares bytes, not metadata -- so this
check runs each implementation under several umasks and reads the mode of
every file it committed. A single umask of ``022`` would pass vacuously on the
very defect the check was written for, so the set includes masks that leave
the group and other write bits open.

Every commit path is covered, because each creates its file differently:
the plain rename (main CSV and the lazily created errors file), the
``--no-clobber`` non-replacing commit, and the ``--allow-partial`` ``.partial``
commit on an unrecoverable stream.

POSIX only. Windows has no umask or mode bits for the decoder to choose, so
there is nothing there to diverge. It is a contract check, not a differential
one, so it runs at any implementation count.

Run automatically by ``run.py``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

#: Masks to run under. ``022`` is the common default and ``002`` the
#: per-user-group one; ``000`` and ``077`` are the extremes. All four must yield
#: ``0666 & ~umask``.
UMASKS = (0o022, 0o002, 0o000, 0o077)


def check_output_mode(invocations: dict[str, list[str]], root: Path, temp: Path) -> None:
    """Require ``0666 & ~umask`` on every committed output, under each umask.

    Raises:
        AssertionError: Any implementation exited unexpectedly, did not write a
            file the run should have committed, or committed one with a
            different mode.
    """
    if os.name != "posix":
        print("SKIP output-mode (POSIX only: Windows has no umask)")
        return

    from run import read_hex  # local import: run.py imports this module

    inputs = Path(__file__).resolve().parent / "inputs"
    errored = temp / "output-mode-errors.mie"
    errored.write_bytes(read_hex(inputs / "errors-inline.hex"))
    broken = temp / "output-mode-partial.mie"
    broken.write_bytes(read_hex(inputs / "partial-unrecoverable.hex"))

    failures: list[str] = []
    for impl, prefix in invocations.items():
        for umask in UMASKS:
            out_dir = temp / f"output-mode-{impl}-{umask:03o}"
            out_dir.mkdir()
            out = out_dir / "out.csv"
            # (label, arguments, expected exit, files the run must commit)
            runs = (
                (
                    "separate-errors",
                    [str(errored), "-o", str(out), "--separate-errors"],
                    0,
                    [out, out_dir / "out_errors.csv"],
                ),
                (
                    "no-clobber",
                    [str(errored), "-o", str(out_dir / "nc.csv"), "--no-clobber"],
                    0,
                    [out_dir / "nc.csv"],
                ),
                (
                    # Exit 0: --allow-partial downgrades the sync loss
                    # (L1-EXIT-004).
                    "allow-partial",
                    [str(broken), "-o", str(out_dir / "p.csv"), "--allow-partial"],
                    0,
                    [out_dir / "p.csv.partial"],
                ),
            )
            for label, args, want_exit, targets in runs:
                result = subprocess.run(
                    [*prefix, "decode", *args],
                    cwd=root,
                    capture_output=True,
                    check=False,
                    timeout=120,
                    umask=umask,
                )
                where = f"{impl} {label} umask {umask:03o}"
                if result.returncode != want_exit:
                    failures.append(
                        f"{where}: exited {result.returncode}, expected {want_exit}\n"
                        f"stderr:\n{result.stderr.decode('utf-8', 'replace')}"
                    )
                    continue
                want = 0o666 & ~umask
                for target in targets:
                    if not target.is_file():
                        failures.append(f"{where}: {target.name} was not written")
                        continue
                    got = stat.S_IMODE(target.stat().st_mode)
                    if got != want:
                        failures.append(
                            f"{where}: {target.name} has mode {got:04o}, expected {want:04o}"
                        )

    if failures:
        raise AssertionError("output-mode contract failed:\n" + "\n".join(failures))
    masks = ", ".join(f"{u:03o}" for u in UMASKS)
    print(f"PASS output-mode ({', '.join(invocations)}; umask {masks})")
