#!/usr/bin/env bash
# scripts/repo-hygiene.sh — the CI backstop for the pre-commit hook's
# file-level checks.
#
# `.githooks/pre-commit` inspects *staged blobs*, which makes it fast but also
# skippable: `git commit --no-verify` bypasses every one of its checks. Until
# v2.12.0 nothing in CI re-ran them, so CONTRIBUTING.md's "CI runs the same
# checks and will fail the merge anyway" was untrue for most of the list — a
# CRLF file, a stray `dbg!()`, a merge marker, an oversized blob or a committed
# `*.mie` recording could all land unnoticed.
#
# This script re-runs those checks over the whole *tracked tree* rather than a
# diff, so it catches anything already committed, however it got there. The
# hook stays staged-based (it must be fast); this is the safety net.
#
# Usage:  bash scripts/repo-hygiene.sh
# Exit:   0 all clean, 1 one or more checks failed (all are reported, not just
#         the first — a contributor should see the whole list in one run).

set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
[[ -t 2 ]] || { RED=''; GREEN=''; YELLOW=''; RESET=''; }

failures=0
step() { printf '%shygiene:%s %s\n' "$YELLOW" "$RESET" "$1" >&2; }
bad()  { printf '%shygiene: FAIL%s %s\n' "$RED" "$RESET" "$1" >&2; failures=$((failures + 1)); }
list() { printf '  %s\n' "$@" >&2; }

# Tracked files, NUL-safe. Binary-ish suffixes are skipped by the text checks.
mapfile -d '' -t TRACKED < <(git ls-files -z)

# Python interpreter for the checks that need one. CONTRIBUTING requires
# Python 3, not a particular command name: a Debian or WSL environment
# routinely ships only `python3`, Git Bash on Windows routinely ships only
# `python`. Probe for one that actually runs Python 3 rather than trusting the
# name — the `-c` probe also rejects Windows' App Execution Alias stub, which
# sits on PATH but runs nothing.
PY_BIN=''
for candidate in python3 python py; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' \
        >/dev/null 2>&1; then
        PY_BIN="$candidate"
        break
    fi
done
[[ -n "$PY_BIN" ]] || bad "no Python 3 interpreter on PATH (tried python3, python, py); checks needing one are skipped"

is_binary() {
    case "$1" in
        *.png|*.jpg|*.jpeg|*.gif|*.ico|*.pdf|*.bin|*.mie|*.svg) return 0 ;;
        *) return 1 ;;
    esac
}

# ── 1. Final newline ──────────────────────────────────────────────────
step "every tracked text file ends with a newline"
offenders=()
for f in "${TRACKED[@]}"; do
    is_binary "$f" && continue
    [[ -f "$f" ]] || continue
    [[ -s "$f" ]] || continue
    [[ $(tail -c 1 "$f" | od -An -tx1 | tr -d ' \n') == "0a" ]] || offenders+=("$f")
done
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "missing final newline"; }

# ── 2. CRLF line endings ──────────────────────────────────────────────
# Inspect the *index* (`i/`) rather than the worktree. On Windows a checkout
# can legitimately hold CRLF while the committed blob is LF — reading the
# worktree would fail here and pass on the Linux runner, which is worse than
# not checking at all.
step "no CRLF line endings in committed blobs"
# `git ls-files --eol` prints "i/<eol> w/<eol> attr/<...>\t<path>"; the path is
# the only tab-separated field, so split on tab and test the index column.
offenders=()
while IFS= read -r path; do
    [[ -n "$path" ]] && offenders+=("$path")
done < <(git ls-files --eol | awk -F'\t' '$1 ~ /^i\/crlf/ {print $2}')
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "CRLF in committed content; convert to LF"; }

# ── 3. Merge conflict markers ─────────────────────────────────────────
# This script itself contains the pattern in a string, so exclude it.
step "no merge conflict markers"
offenders=()
for f in "${TRACKED[@]}"; do
    is_binary "$f" && continue
    [[ -f "$f" ]] || continue
    [[ "$f" == "scripts/repo-hygiene.sh" ]] && continue
    grep -qE '^(<{7}|={7}|>{7})( |$)' "$f" && offenders+=("$f")
done
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "merge conflict markers committed"; }

# ── 4. Large files (> 1 MB) ───────────────────────────────────────────
step "no tracked file over 1 MB"
LIMIT=$((1024 * 1024))
offenders=()
for f in "${TRACKED[@]}"; do
    [[ -f "$f" ]] || continue
    size=$(wc -c <"$f")
    (( size > LIMIT )) && offenders+=("$f ($size bytes)")
done
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "file over 1 MB; use git-lfs or .gitignore"; }

# ── 5. No *.mie recordings ────────────────────────────────────────────
# Fixtures are reviewable hex under tests/conformance/inputs, never binaries.
step "no *.mie recordings tracked"
offenders=()
for f in "${TRACKED[@]}"; do
    [[ "$f" == *.mie ]] && offenders+=("$f")
done
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "*.mie recording committed"; }

# ── 6. Cargo.lock parity ──────────────────────────────────────────────
step "rust/Cargo.lock version matches rust/Cargo.toml"
toml_ver=$(awk '/^\[package\]/{p=1;next} /^\[/{p=0} p && /^version *=/{gsub(/[" ]/,""); sub(/version=/,""); print; exit}' rust/Cargo.toml)
lock_ver=$(awk '/^name = "aero1553"$/{getline; gsub(/[" ]/,""); sub(/version=/,""); print; exit}' rust/Cargo.lock)
if [[ -z "$toml_ver" || -z "$lock_ver" ]]; then
    bad "could not read version from Cargo.toml ($toml_ver) / Cargo.lock ($lock_ver)"
elif [[ "$toml_ver" != "$lock_ver" ]]; then
    bad "Cargo.toml is $toml_ver but Cargo.lock says $lock_ver — run \`cargo check\`"
fi

# ── 6b. Joint-cut version agreement ───────────────────────────────────
# Every release so far has been a JOINT CUT: the three implementations ship the
# same version from one tag. Nothing enforced that. The version lives in five
# places and the release checklist named four of them -- C++ was added to the
# repository after that checklist was written and never got added to it, so
# `aero1553 --version` could report a different number depending on which
# implementation the operator happened to run.
step "all three implementations declare the same version"
py_ver=$(awk '/^\[(tool\.poetry|project)\]/{p=1;next} /^\[/{p=0} p && /^version *=/{gsub(/[" ]/,""); sub(/version=/,""); print; exit}' python/pyproject.toml)
cpp_ver=$(awk -F'"' '/kVersion *=/{print $2; exit}' cpp/src/cli.cpp)
cmake_ver=$(awk '/^ *VERSION [0-9]/{print $2; exit}' cpp/CMakeLists.txt)
# The C++ CI smoke steps assert the exact --version STRING, once for Linux and
# once for Windows. They are a sixth place the version lives, and the first cut
# to use this gate still missed them: the gate passed while CI failed on
# `mie-decoder 2.12.0` != `mie-decoder 2.13.0`. Both are checked here now, and
# distinct values are collapsed so one wrong line is enough to fail.
smoke_vers=$(grep -o 'aero1553 [0-9][0-9.]*' .github/workflows/cpp-ci.yml \
             | awk '{print $2}' | sort -u)
smoke_count=$(printf '%s\n' "$smoke_vers" | grep -c .)
if [[ -z "$py_ver" || -z "$cpp_ver" || -z "$cmake_ver" || -z "$smoke_vers" ]]; then
    bad "could not read a version (python=$py_ver cpp=$cpp_ver cmake=$cmake_ver smoke=$smoke_vers)"
elif [[ "$toml_ver" != "$py_ver" || "$toml_ver" != "$cpp_ver" \
     || "$toml_ver" != "$cmake_ver" || "$smoke_count" != "1" || "$smoke_vers" != "$toml_ver" ]]; then
    list "rust/Cargo.toml            $toml_ver" \
         "python/pyproject.toml      $py_ver" \
         "cpp/src/cli.cpp            $cpp_ver" \
         "cpp/CMakeLists.txt         $cmake_ver" \
         "cpp-ci.yml --version smoke $(printf '%s ' $smoke_vers)"
    bad "version sources disagree — a joint cut must ship one number"
fi

# ── 7. No forgotten dbg!() ────────────────────────────────────────────
step "no dbg!() in Rust sources"
offenders=()
while IFS= read -r hit; do offenders+=("$hit"); done < <(
    grep -rn 'dbg!' rust/src rust/tests 2>/dev/null || true
)
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "dbg!() left in Rust source"; }

# ── 8. unsafe blocks carry a // SAFETY: comment ───────────────────────
# Detection mirrors `.githooks/pre-commit` exactly — `unsafe` at the start of
# a line, with a SAFETY note in the 3 lines above. Deliberately identical
# rather than stricter: a backstop that rejects what the hook accepts would
# just move the surprise from commit time to merge time.
#
# Known limitation, shared with the hook: a mid-line `unsafe` (as in
# `let m = unsafe { ... }`) is not detected by either. Today's only such site,
# reader.rs, does carry a SAFETY note.
step "every line-leading unsafe block has a // SAFETY: comment"
offenders=()
while IFS=: read -r file line _; do
    [[ -z "${file:-}" ]] && continue
    start=$(( line > 3 ? line - 3 : 1 ))
    if ! sed -n "${start},${line}p" "$file" | grep -q 'SAFETY:'; then
        offenders+=("$file:$line")
    fi
done < <(grep -rnE '^[[:space:]]*unsafe[[:space:]]*([{(]|fn[[:space:]])' rust/src rust/tests 2>/dev/null || true)
(( ${#offenders[@]} )) && { list "${offenders[@]}"; bad "unsafe block without a // SAFETY: comment"; }

# ── 9. CI job table in MAINTAINER-GUIDE matches the workflows ─────────
# §9 of the guide documents every CI job in a table. That table stayed
# accurate while the prose above it did not ("seven jobs" when there were
# fifteen), so the number is gone and the table is the list — which only works
# if something keeps it honest. Adding a job without a row now fails here.
#
# Both gating workflows are scanned. When cpp-ci.yml was added it was invisible
# to this check, which read only ci.yml — so the guide could have gone on
# describing two implementations' worth of CI while a third ran unlisted, and
# the check would still have reported success. A gate that silently narrows as
# the repository grows is worse than one that was never written.
step "MAINTAINER-GUIDE §9 lists every CI job"
# FNR==1 resets the in-jobs flag at the start of each file. Without it the flag
# set by ci.yml stays on into cpp-ci.yml, and every two-space key ABOVE that
# file's own `jobs:` — `push:`, `group:`, `contents:`, `CXX:` — gets collected
# as though it were a job name, demanding guide rows for things that are not
# jobs.
mapfile -t yml_jobs < <(
    awk 'FNR==1{inj=0} /^jobs:/{inj=1;next} inj && /^  [a-z0-9-]+:/{gsub(/[ :]/,"");print}' \
        .github/workflows/ci.yml .github/workflows/cpp-ci.yml \
        .github/workflows/differential.yml .github/workflows/fuzz.yml
)
# A job name reused across the two workflows would satisfy this check with ONE
# guide row, leaving the other job undocumented while the gate reported success.
# That is not hypothetical: the C++ conformance job was first written as
# `conformance`, which is also ci.yml's Rust/Python job, and it passed here
# having never been described. Names are the key this check looks things up by,
# so they have to be unique across every workflow it scans.
dupe_jobs=$(printf '%s\n' "${yml_jobs[@]}" | sort | uniq -d)
if [ -n "$dupe_jobs" ]; then
    list $dupe_jobs
    bad "CI job name(s) used in more than one workflow; one guide row cannot describe two jobs"
fi

missing_rows=()
for job in "${yml_jobs[@]}"; do
    grep -qF "| \`$job\` |" docs/MAINTAINER-GUIDE.md || missing_rows+=("$job")
done
if (( ${#missing_rows[@]} )); then
    list "${missing_rows[@]}"
    bad "CI job(s) with no row in MAINTAINER-GUIDE.md section 9"
fi

# ── 10. Config key set agrees across its three text sources ───────────
# CONFIG-REFERENCE.md calls itself the reference for *every* TOML key, and
# states each one twice: a copyable quick-reference block and a normative
# table. config/default.toml is the third copy. merge.delta_scope shipped in
# v2.11.0 into two of the three, which is exactly the drift this catches.
# (The two loaders are the fourth and fifth copies; they are code, and their
# own parity is covered by tests/conformance/config_parity.py.)
step "config keys agree: CONFIG-REFERENCE block, table, and default.toml"
if [[ -z "$PY_BIN" ]]; then
    list "skipped: no Python 3 interpreter"
elif ! "$PY_BIN" - <<'PY'
import re, sys, pathlib

def block_keys(text):
    sec, keys = None, set()
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^\[([a-z_]+)\]', s)
        if m:
            sec = m.group(1)
            continue
        m = re.match(r'^#?\s*([a-z_]+)\s*=', s)
        if m and sec:
            keys.add(f"{sec}.{m.group(1)}")
    return keys

doc = pathlib.Path("docs/CONFIG-REFERENCE.md").read_text(encoding="utf-8")
quick = block_keys(doc.split("## Quick reference", 1)[1].split("```")[1])
table = set(re.findall(r'^\| `([a-z_]+\.[a-z_]+)` \|', doc, re.M))
default = block_keys(pathlib.Path("config/default.toml").read_text(encoding="utf-8"))

sources = {"quick-reference block": quick, "normative table": table,
           "config/default.toml": default}
union = set().union(*sources.values())
bad = False
for key in sorted(union):
    absent = [n for n, s in sources.items() if key not in s]
    if absent:
        bad = True
        print(f"  {key} missing from: {', '.join(absent)}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
then
    bad "config key set differs between CONFIG-REFERENCE.md and config/default.toml"
fi

# ── 11. Declared Rust MSRV agrees everywhere it is written down ───────
# `rust-version` in rust/Cargo.toml is the only enforceable copy; the number
# is also written into CI, CONTRIBUTING, both READMEs, CLAUDE.md, the
# MAINTAINER-GUIDE and L3-RS-001. Until v2.12.0 the *rationale* in four of
# those was false (it credited memmap2, which declares 1.65 — the real driver
# is the crate's own let-chains). Prose explaining a number is not mechanically
# checkable, but the number is: this pins every floor-declaring statement to
# Cargo.toml, so a bump can't land in six places and miss the seventh.
#
# CHANGELOG.md is exempt: it is a historical record and legitimately names
# superseded floors.
step "declared Rust MSRV agrees across Cargo.toml, CI, and the docs"
if [[ -z "$PY_BIN" ]]; then
    list "skipped: no Python 3 interpreter"
elif ! "$PY_BIN" - <<'PY'
import re, sys, pathlib, subprocess

CARGO = pathlib.Path("rust/Cargo.toml")
m = re.search(r'^rust-version\s*=\s*"([^"]+)"', CARGO.read_text(encoding="utf-8"), re.M)
if not m:
    print("  rust/Cargo.toml declares no rust-version", file=sys.stderr)
    sys.exit(1)
declared = m.group(1)

# Forms that *declare the floor*. Deliberately narrow: contrast statements
# ("edition 2024 requires only 1.85", "memmap2 declares rust-version = 1.65")
# are facts about other things and must not trip this.
PATTERNS = [
    r'MSRV[ ]*\(?\*{0,2}(\d+\.\d+)',
    r'toolchain (?:>=|≥) \*{0,2}(\d+\.\d+)',
    r'cargo \+(\d+\.\d+)',
    r'rustup (?:toolchain install|default) (\d+\.\d+)',
    r'pinned \*\*(\d+\.\d+)\*\* toolchain',
]

# Files that must each carry at least one floor declaration — losing the
# statement entirely is drift too.
REQUIRED = [
    "rust/Cargo.toml", "CLAUDE.md", "CONTRIBUTING.md", "rust/README.md",
    "docs/L3-REQ.md", "docs/MAINTAINER-GUIDE.md", ".github/workflows/ci.yml",
]

tracked = subprocess.run(["git", "ls-files", "-z"], capture_output=True,
                         check=True).stdout.decode().split("\0")
EXEMPT = {"CHANGELOG.md", "scripts/repo-hygiene.sh"}
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".bin",
               ".mie", ".svg", ".lock")

bad = False
seen = {}
for name in tracked:
    if not name or name in EXEMPT or name.endswith(SKIP_SUFFIX):
        continue
    path = pathlib.Path(name)
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    if name == "rust/Cargo.toml":
        seen[name] = True
        continue
    for pat in PATTERNS:
        for found in re.findall(pat, text):
            seen[name] = True
            if found != declared:
                bad = True
                print(f"  {name}: declares MSRV {found}, "
                      f"rust/Cargo.toml declares {declared}", file=sys.stderr)

for name in REQUIRED:
    if name not in seen:
        bad = True
        print(f"  {name}: states no Rust MSRV; expected {declared}",
              file=sys.stderr)

sys.exit(1 if bad else 0)
PY
then
    bad "declared Rust MSRV differs between rust/Cargo.toml and the docs/CI"
fi

# ── 12. ROADMAP does not restate a TRACE-MATRIX status ────────────────
# ROADMAP.md is hand-written and forward-looking; TRACE-MATRIX.md is
# generated. Any status the roadmap copies out of the matrix is therefore
# guaranteed to rot, and it rotted: an entry deferred the L2-DEC-012 tie-break
# test "still listed as Draft in docs/TRACE-MATRIX.md" long after the matrix
# had it Implemented with a test on each side. Link to the matrix; don't
# restate it. Matched case-sensitively on the generator's own status tokens,
# so ordinary lowercase prose ("not yet implemented") is unaffected.
step "ROADMAP.md does not restate a TRACE-MATRIX status"
if grep -nE '\b(Draft|Implemented|Verified)\b' docs/ROADMAP.md >&2; then
    bad "docs/ROADMAP.md asserts a verification status; link to docs/TRACE-MATRIX.md instead of copying it"
fi

# ── 13. Python exception hierarchy agrees with its two drawings ───────
# exceptions.py is the truth; ERROR-CATALOG.md §2 redraws it as an ASCII tree
# and docs/diagrams/class.puml redraws it again as UML, and both drawings
# claim to be complete ("every variant has a counterpart", "correspond one to
# one, in both directions"). Both had drifted: the ASCII tree lost
# MieFileIoError when it was added in v2.12.0, and the diagram was missing
# three leaf classes outright. A hand-maintained copy of a class tree is not
# checkable by reading it — every child's *parent* has to match.
step "Python exception hierarchy matches ERROR-CATALOG §2 and class.puml"
if [[ -z "$PY_BIN" ]]; then
    list "skipped: no Python 3 interpreter"
elif ! "$PY_BIN" - <<'PY'
import re, sys, pathlib

src = pathlib.Path("python/src/aero1553/exceptions.py").read_text(encoding="utf-8")
truth = dict(re.findall(r'^class (Mie\w+|Aero1553Error)\((\w+)\):', src, re.M))

# ERROR-CATALOG §2 ASCII tree: depth comes from the column the "── " marker
# sits in, so a stack of (depth, name) recovers each node's parent.
doc = pathlib.Path("docs/ERROR-CATALOG.md").read_text(encoding="utf-8")
block = doc.split("### Python (`aero1553.exceptions`)", 1)[1].split("```")[1]
tree, stack = {}, []
for line in block.splitlines():
    m = re.match(r'^(.*?)(?:└──|├──) (Mie\w+|Aero1553Error)', line)
    if not m:
        continue
    depth, name = len(m.group(1)), m.group(2)
    while stack and stack[-1][0] >= depth:
        stack.pop()
    tree[name] = stack[-1][1] if stack else "Exception"
    stack.append((depth, name))

puml = pathlib.Path("docs/diagrams/class.puml").read_text(encoding="utf-8")
uml = {c: p for p, c in re.findall(r'^\s*(Mie\w+|Aero1553Error) <\|-- (Mie\w+)', puml, re.M)}

bad = False
# Labels stay ASCII: this prints to a Windows console under cp437 as readily
# as to a UTF-8 CI runner, and a UnicodeEncodeError here would look like a
# check failure rather than a terminal limitation.
for label, drawing in (("ERROR-CATALOG.md section 2 tree", tree), ("class.puml", uml)):
    for name, parent in sorted(truth.items()):
        if name == "Aero1553Error":
            continue          # the root; drawn as a child of Exception
        if name not in drawing:
            bad = True
            print(f"  {label}: missing {name} (exceptions.py: {parent})", file=sys.stderr)
        elif drawing[name] != parent:
            bad = True
            print(f"  {label}: {name} drawn under {drawing[name]}, "
                  f"exceptions.py says {parent}", file=sys.stderr)
    for name in sorted(set(drawing) - set(truth) - {"Aero1553Error"}):
        bad = True
        print(f"  {label}: draws {name}, which exceptions.py does not define",
              file=sys.stderr)

sys.exit(1 if bad else 0)
PY
then
    bad "the drawn Python exception hierarchy differs from python/src/aero1553/exceptions.py"
fi

# ── 14. No trace-matrix row claims Implemented with no artifact ───────
# Until v2.12.0 build-trace-matrix.py returned "Implemented (I)" for any leaf
# declaring Inspection/Analysis/Demonstration, from the declared method alone
# — so L2-SYN-014, L2-CONF-001 and L2-CONF-004 read "Implemented (I)" beside a
# literal _(TBD)_ in their own artifact column. The generator now requires an
# **Evidence** line naming what carries the check. This is the backstop: it
# fails on the rendered output, so reverting that rule in the generator is
# caught here rather than by a reader noticing the contradiction.
step "no TRACE-MATRIX row claims Implemented with a _(TBD)_ artifact"
offenders=()
while IFS= read -r row; do
    [[ -n "$row" ]] && offenders+=("$row")
done < <(awk -F'|' '
    /^\| L[123]-/ && $4 ~ /_\(TBD\)_/ && $5 ~ /Implemented/ {
        gsub(/^[ \t]+|[ \t]+$/, "", $2); gsub(/^[ \t]+|[ \t]+$/, "", $5)
        print $2 " -> " $5
    }' docs/TRACE-MATRIX.md)
if (( ${#offenders[@]} )); then
    list "${offenders[@]}"
    bad "TRACE-MATRIX rows claim Implemented with no verification artifact"
fi

# ── 15. Shipped output is ASCII in all three implementations ──────────
#
# L2-CLI-014. A Windows console runs at the OEM code page (437 in a US
# install), not UTF-8, so a non-ASCII byte in a log message or help banner
# reaches the operator as mojibake -- and mojibake in a decoder's own
# diagnostics reads as memory corruption, which is how it was reported.
#
# The check is delegated because it needs a real string-literal parser rather
# than a grep: C++ had spelled its em dash "\xE2\x80\x94", which is pure ASCII
# on disk and non-ASCII on the wire, so a byte-level scan of the tree reported
# it clean while the console showed garbage.
step "shipped string literals are ASCII (cpp, rust, python)"
if [[ -n "$PY_BIN" ]]; then
    if ! "$PY_BIN" scripts/assert-ascii-output.py >/dev/null 2>&1; then
        "$PY_BIN" scripts/assert-ascii-output.py 2>&1 >/dev/null | sed 's/^/  /' >&2
        bad "non-ASCII string literal in a shipped source file (L2-CLI-014)"
    fi
fi

# ── 16. Coverage floors agree between the gate and the docs ───────────
#
# Each implementation's floor lives in exactly one enforcing file. The docs
# then repeat the numbers in prose, and prose drifts silently: through v2.14.0
# the Rust gate enforced 90 lines / 89 regions while rust/README.md,
# CONTRIBUTING.md (twice) and MAINTAINER-GUIDE.md all said 87/86 -- and so did
# the rationale comment sitting three lines above the alias in the very file
# that sets them. Nothing failed, because nothing compared them.
#
# Enforcing sources, in order: the `cov-ci` alias in rust/.cargo/config.toml,
# `fail_under` in python/pyproject.toml, COVERAGE_MIN_* in cpp/Makefile.
step "documented coverage floors match the gates that enforce them"
rust_lines=$(grep -oE 'fail-under-lines[[:space:]]+[0-9]+' rust/.cargo/config.toml \
             | grep -oE '[0-9]+' | head -1)
rust_regions=$(grep -oE 'fail-under-regions[[:space:]]+[0-9]+' rust/.cargo/config.toml \
               | grep -oE '[0-9]+' | head -1)
py_floor=$(grep -oE '^fail_under[[:space:]]*=[[:space:]]*[0-9]+' python/pyproject.toml \
           | grep -oE '[0-9]+' | head -1)
cpp_line=$(grep -oE '^COVERAGE_MIN_LINE[[:space:]]*\?=[[:space:]]*[0-9]+' cpp/Makefile \
           | grep -oE '[0-9]+' | head -1)
cpp_branch=$(grep -oE '^COVERAGE_MIN_BRANCH[[:space:]]*\?=[[:space:]]*[0-9]+' cpp/Makefile \
             | grep -oE '[0-9]+' | head -1)

if [[ -z "$rust_lines" || -z "$rust_regions" || -z "$py_floor" || -z "$cpp_line" || -z "$cpp_branch" ]]; then
    bad "could not read a coverage floor from its enforcing file (rust/.cargo/config.toml, python/pyproject.toml, cpp/Makefile)"
else
    offenders=()
    # A doc that names a coverage floor must name the CURRENT one. Match the
    # "<n>% line / <m>% region" shape the docs use, and compare both numbers.
    while IFS= read -r hit; do
        file=${hit%%:*}
        pair=$(printf '%s' "$hit" | grep -oE '[0-9]+% line / [0-9]+% region' | head -1)
        [[ -n "$pair" ]] || continue
        got_l=$(printf '%s' "$pair" | grep -oE '^[0-9]+')
        got_r=$(printf '%s' "$pair" | sed -E 's|^[0-9]+% line / ([0-9]+)% region|\1|')
        if [[ "$got_l" != "$rust_lines" || "$got_r" != "$rust_regions" ]]; then
            offenders+=("$file: says ${got_l}/${got_r}, gate enforces ${rust_lines}/${rust_regions}")
        fi
    done < <(grep -rnE '[0-9]+% line / [0-9]+% region' \
                 rust/README.md CONTRIBUTING.md docs/MAINTAINER-GUIDE.md 2>/dev/null || true)

    # CONTRIBUTING spells the same pair out as a bulleted pair of floors.
    con_l=$(grep -oE '^- \*\*Lines: [0-9]+%\*\* floor' CONTRIBUTING.md | grep -oE '[0-9]+' | head -1)
    con_r=$(grep -oE '^- \*\*Regions: [0-9]+%\*\* floor' CONTRIBUTING.md | grep -oE '[0-9]+' | head -1)
    if [[ -n "$con_l" && "$con_l" != "$rust_lines" ]]; then
        offenders+=("CONTRIBUTING.md: 'Lines: ${con_l}%' but the gate enforces ${rust_lines}%")
    fi
    if [[ -n "$con_r" && "$con_r" != "$rust_regions" ]]; then
        offenders+=("CONTRIBUTING.md: 'Regions: ${con_r}%' but the gate enforces ${rust_regions}%")
    fi

    # Python and C++ floors, as the CI table states them.
    if ! grep -qE "${py_floor}% combined line\+branch floor" docs/MAINTAINER-GUIDE.md; then
        offenders+=("docs/MAINTAINER-GUIDE.md: python floor is ${py_floor}% (fail_under) but the CI table does not say so")
    fi
    if ! grep -qE "${cpp_line}% lines and ${cpp_branch}% branches" docs/MAINTAINER-GUIDE.md; then
        offenders+=("docs/MAINTAINER-GUIDE.md: C++ floors are ${cpp_line}/${cpp_branch} (cpp/Makefile) but the CI table does not say so")
    fi

    if (( ${#offenders[@]} )); then
        list "${offenders[@]}"
        bad "documented coverage floors disagree with the gates that enforce them"
    fi
fi

# ── 17. cpp/README does not deny a gate that exists ───────────────────
#
# It claimed "no coverage gate" while cpp/Makefile set COVERAGE_MIN_* and
# cpp-ci.yml ran a gcovr job, and "deliberately no fuzz tier" while fuzz.yml
# ran a fuzz-cpp burn-in. A README that under-claims is as misleading as one
# that over-claims: it sends a contributor looking for work already done.
step "cpp/README does not deny the coverage or fuzz tiers it has"
offenders=()
grep -qiE 'no coverage gate' cpp/README.md \
    && offenders+=("cpp/README.md claims 'no coverage gate'; cpp/Makefile sets COVERAGE_MIN_* and cpp-ci.yml runs gcovr")
grep -qiE 'deliberately \*\*no fuzz tier\*\*|no fuzz tier' cpp/README.md \
    && offenders+=("cpp/README.md claims 'no fuzz tier'; .github/workflows/fuzz.yml runs a fuzz-cpp job")
if (( ${#offenders[@]} )); then
    list "${offenders[@]}"
    bad "cpp/README.md understates the tiers that actually run"
fi

# ── 18. The analyser version agrees between the Makefile and CI ───────
# `cpp/Makefile`'s LLVM_VERSION drives `make verify-ci`, whose entire purpose
# is to reproduce CI's analyser gates locally. `.github/workflows/cpp-ci.yml`
# pins the same number as `env.LLVM_VERSION` for the clang-tidy and
# clang-format jobs. Both files carried a comment asking the other to be
# edited alongside it, and a comment is not a mechanism.
#
# The failure this prevents is silent in the worst direction. If verify-ci
# runs an OLDER analyser than CI, it passes locally and the PR fails; if it
# runs a NEWER one, it reports findings CI will never show, and the natural
# response is to stop trusting the target. Either way the number nobody
# noticed had drifted is the explanation, and nothing points at it.
step "the C++ analyser version agrees between cpp/Makefile and cpp-ci.yml"
mk_llvm=$(sed -n 's/^LLVM_VERSION[[:space:]]*?\{0,1\}=[[:space:]]*\([0-9][0-9]*\).*/\1/p' cpp/Makefile | head -1)
ci_llvm=$(sed -n 's/^[[:space:]]*LLVM_VERSION:[[:space:]]*"\{0,1\}\([0-9][0-9]*\)"\{0,1\}[[:space:]]*$/\1/p' \
              .github/workflows/cpp-ci.yml | head -1)
if [[ -z "$mk_llvm" ]]; then
    list "cpp/Makefile declares no LLVM_VERSION"
    bad "cannot read the analyser version from cpp/Makefile"
elif [[ -z "$ci_llvm" ]]; then
    list ".github/workflows/cpp-ci.yml declares no env.LLVM_VERSION"
    bad "cannot read the analyser version from cpp-ci.yml"
elif [[ "$mk_llvm" != "$ci_llvm" ]]; then
    list "cpp/Makefile: LLVM_VERSION = $mk_llvm" \
         ".github/workflows/cpp-ci.yml: env.LLVM_VERSION = $ci_llvm"
    bad "analyser version differs; 'make verify-ci' no longer predicts the CI gates"
fi

# Nothing may reintroduce a hardcoded major alongside the variable: a stray
# `clang-tidy-20` next to `LLVM_VERSION: "22"` would satisfy the comparison
# above while running the wrong tool, which is precisely the drift being
# closed. The variable spellings are excluded by requiring a literal digit.
step "no hardcoded clang-tidy/clang-format major outside LLVM_VERSION"
if grep -nE 'clang-(tidy|format)-[0-9]+|llvm-toolchain-noble-[0-9]+' \
        .github/workflows/cpp-ci.yml cpp/Makefile >&2; then
    bad "spell the analyser major as \$LLVM_VERSION / \$(LLVM_VERSION), not a literal"
fi

# ── 20. Nothing claims the fidelity container has a CMake ─────────────
# The gcc:4.8 image has NO CMake and cannot install one -- Debian 7 is past end
# of life, its repositories are archived, `apt-get update` there exits 100 and
# `apt-cache policy cmake` reports Candidate: (none). That fact is the whole
# reason ADR-0002 keeps two build descriptions.
#
# It was written down as "ships CMake 2.8" in SIX places -- ADR-0002, CLAUDE.md,
# CONTRIBUTING.md, cpp/Makefile, cpp/CMakeLists.txt and L3-CPP-002 -- and was
# wrong in all six. The error mattered in a specific direction: "2.8" reads as
# surmountable (lower the floor, backport one), so it invites re-opening a
# question that is more closed than the record admitted. Which is what happened.
#
# Three exemptions, each for a different reason:
#   - CHANGELOG.md is a historical record and legitimately quotes what was
#     believed at the time.
#   - The ADR-0002 amendment quotes the wrong text deliberately, to correct it.
#   - This file, which necessarily contains the string it searches for. A check
#     that greps for a phrase cannot also be a document that explains the
#     phrase, unless it declines to read itself.
step "no document claims the gcc:4.8 fidelity container has a CMake"
mapfile -t cmake28_hits < <(
    grep -rniE 'container[^.]{0,40}(ships|has|with)[^.]{0,20}cmake[ -]?2|cmake 2\.8' \
        --include='*.md' --include='Makefile' --include='*.txt' --include='*.sh' \
        --exclude-dir=target --exclude-dir=.venv . 2>/dev/null \
    | grep -v '^\./CHANGELOG\.md:' \
    | grep -v '^\./docs/adr/0002-' \
    | grep -v '^\./scripts/repo-hygiene\.sh:' || true
)
if (( ${#cmake28_hits[@]} )); then
    list "${cmake28_hits[@]}"
    bad "the fidelity container has no CMake at all; see the ADR-0002 amendment"
fi

# ── Summary ───────────────────────────────────────────────────────────
if (( failures )); then
    printf '%shygiene: %d check(s) failed%s\n' "$RED" "$failures" "$RESET" >&2
    exit 1
fi
printf '%shygiene: all checks passed%s\n' "$GREEN" "$RESET" >&2
