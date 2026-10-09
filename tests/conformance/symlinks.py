"""Contract check: an input path is resolved through symlinks.

L2-RDR-005 and L2-RDR-006 require an input to be judged by the file a symlink
resolves to: a link to a recording decodes as that recording, and a link whose
target is gone is a missing file. L2-MRG-001 clause 4 says the same of
``--glob``. Rust's ``std::fs`` follows symlinks on every platform, the Python
package reads through the Rust extension, and the C++ POSIX backend uses
``stat()``. The C++ Windows backend used ``GetFileAttributesExW``, which
describes the link itself -- zero bytes, and present even when dangling -- so
there a symlinked recording was refused as empty, a dangling one reported an
I/O failure instead of "not found", and ``--glob`` matched a dangling link the
other two skip.

The manifest cannot express this: it materializes inputs from hex and has no
way to make a link. Windows is the platform the check exists for, and creating
a symlink there needs a privilege (administrator, or Developer Mode) that a
workstation often lacks. Without it the check SKIPs -- except under GitHub
Actions, whose runners have the privilege, where an inability to make a link
fails instead: a skip there would leave the platform this guards untested
while reporting green.

Each implementation is held to these outcomes on its own, so this is a
contract check and runs at any implementation count:

- ``decode`` through a link writes the same bytes as decoding the target;
- ``--glob`` matching a link and a dangling link resolves to the link alone;
- ``decode`` and ``dump`` of a dangling link report the input missing;
- ``--config`` naming a dangling link reports the config missing.

Run automatically by ``run.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FILE_NOT_FOUND_EXIT = 1
CONFIG_ERROR_EXIT = 5


def _make_links(work: Path) -> bool:
    """Create the fixture's links; False if this host may not."""
    links = (
        (work / "link.mie", work / "real.bin"),
        (work / "dangling.mie", work / "gone.bin"),
        (work / "glob" / "link.mie", work / "real.bin"),
        (work / "glob" / "dangling.mie", work / "gone.bin"),
        (work / "dangling.toml", work / "gone.toml"),
    )
    try:
        for link, target in links:
            link.symlink_to(target)
    except OSError:
        return False
    return True


def check_symlinks(invocations: dict[str, list[str]], root: Path, temp: Path) -> None:
    """Require every implementation to resolve input paths through symlinks.

    Raises:
        AssertionError: An implementation exited unexpectedly, wrote different
            bytes through a link than from its target, or did not report a
            dangling link as missing. Also raised under GitHub Actions when
            the host cannot create a symlink, rather than skipping.
    """
    from run import read_hex  # local import: run.py imports this module

    inputs = Path(__file__).resolve().parent / "inputs"
    work = temp / "symlinks"
    (work / "glob").mkdir(parents=True)
    # Named .bin so `glob/*.mie` and the decode-by-target run see only links
    # where a link is meant.
    (work / "real.bin").write_bytes(read_hex(inputs / "basic-multi-record.hex"))
    if not _make_links(work):
        reason = "this host cannot create symlinks (Windows needs admin or Developer Mode)"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise AssertionError(f"symlinks contract could not run: {reason}")
        print(f"SKIP symlinks ({reason})")
        return

    failures: list[str] = []
    for impl, prefix in invocations.items():
        out_dir = work / f"out-{impl}"
        out_dir.mkdir()

        def run(args: list[str], prefix: list[str] = prefix) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [*prefix, *args],
                cwd=root,
                capture_output=True,
                check=False,
                timeout=120,
            )

        def expect(
            label: str,
            result: subprocess.CompletedProcess[bytes],
            want_exit: int,
            want_stderr: str = "",
            impl: str = impl,
        ) -> bool:
            stderr = result.stderr.decode("utf-8", "replace")
            if result.returncode != want_exit or want_stderr not in stderr:
                wanted = f"exit {want_exit}" + (f" and {want_stderr!r}" if want_stderr else "")
                failures.append(
                    f"{impl} {label}: exited {result.returncode}, expected {wanted}\n"
                    f"stderr:\n{stderr}"
                )
                return False
            return True

        # The target, decoded directly: the reference for both link runs.
        # --no-mux because MUX is taken from the input's NAME (L2-WRT-020),
        # which is the one thing a link legitimately changes.
        direct = out_dir / "direct.csv"
        if not expect(
            "decode target",
            run(["decode", str(work / "real.bin"), "-o", str(direct), "--no-mux"]),
            0,
        ):
            continue
        reference = direct.read_bytes()

        via_link = out_dir / "link.csv"
        if (
            expect(
                "decode link",
                run(["decode", str(work / "link.mie"), "-o", str(via_link), "--no-mux"]),
                0,
            )
            and via_link.read_bytes() != reference
        ):
            failures.append(f"{impl} decode link: output differs from decoding the target")

        # Exactly one match, so a single input: the merge is bypassed and the
        # bytes are comparable. Had the dangling link matched, this would be a
        # two-input merge that fails opening it.
        via_glob = out_dir / "glob.csv"
        pattern = str(work / "glob" / "*.mie")
        if (
            expect(
                "glob",
                run(["decode", "--glob", pattern, "-o", str(via_glob), "--no-mux"]),
                0,
            )
            and via_glob.read_bytes() != reference
        ):
            failures.append(f"{impl} glob: output differs from decoding the target")

        dangling = str(work / "dangling.mie")
        expect(
            "decode dangling",
            run(["decode", dangling, "-o", str(out_dir / "d.csv")]),
            FILE_NOT_FOUND_EXIT,
            "MIE file not found",
        )
        expect("dump dangling", run(["dump", dangling]), FILE_NOT_FOUND_EXIT, "MIE file not found")
        expect(
            "config dangling",
            # --config is a global option, so it precedes the subcommand.
            run(
                [
                    "--config",
                    str(work / "dangling.toml"),
                    "decode",
                    str(work / "real.bin"),
                    "-o",
                    str(out_dir / "c.csv"),
                ]
            ),
            CONFIG_ERROR_EXIT,
            "Config file not found",
        )

    if failures:
        raise AssertionError("symlinks contract failed:\n" + "\n".join(failures))
    print(f"PASS symlinks ({', '.join(invocations)}; decode, glob, dump, config)")
