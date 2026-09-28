#!/usr/bin/env python3
"""Regenerate docs/TRACE-MATRIX.md from requirement sources and test markers.

This tool walks the requirement documents and all three implementations' test
markers, and emits a single trace matrix document:

1. ``docs/L1-REQ.md`` — for L1 ids and their declared verification methods
2. ``docs/L2-REQ.md``, ``docs/L3-REQ.md`` — for L2/L3 ids with ``Parent:`` fields
3. ``python/tests/`` — for every id in every ``@pytest.mark.requirement(...)``
   marker, collected via AST parse
4. ``src/**/*.rs`` and ``tests/*.rs`` — for every ``/// Requirements: ...``
   doc-comment line immediately preceding a ``#[test]`` item, collected via a
   stateful line scan
5. ``cpp/tests/*.cpp`` — for every requirement id appearing inside a Catch2
   ``TEST_CASE`` tag string, e.g. ``TEST_CASE("...", "[atomic][L3-WRT-001]")``

Each implementation uses whatever marker its test framework can already select
on, rather than a common comment convention: pytest selects on markers, Catch2
selects on tags, and Rust has neither, so it uses a doc comment. That keeps
every emitted artifact string directly runnable.

The output per requirement row includes:

* L2/L3 children (from parent fields)
* Test artifacts (from markers) in pytest discovery format for Python tests
  and ``path::name`` for Rust and C++ tests. Direct markers on an L1
  requirement are rendered too — most L1s decompose into L2/L3 and carry
  none, but Test-verified L1 *leaves* (no L2 decomposition, e.g.
  ``L1-ROB-001`` for the fuzz harness) attach their tests at L1.
* Status rolled up by :func:`compute_status` per the same rule as the
  Message-Service version of this script.

The coverage-summary denominator is every L2 and L3 requirement plus the
Test-verifiable L1 *leaves*. Composite L1s are excluded from the count
because they are verified transitively through their (counted) children;
counting them too would double-count.

Usage:
    python scripts/build-trace-matrix.py            # regenerate in place
    python scripts/build-trace-matrix.py --check    # fail if output drifted
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L1_DOC = ROOT / "docs" / "L1-REQ.md"
L2_DOC = ROOT / "docs" / "L2-REQ.md"
L3_DOC = ROOT / "docs" / "L3-REQ.md"
TRACE_DOC = ROOT / "docs" / "TRACE-MATRIX.md"
PY_TESTS_DIR = ROOT / "python" / "tests"
RUST_SOURCE_ROOTS = [ROOT / "rust" / "src", ROOT / "rust" / "tests"]
CPP_TESTS_DIR = ROOT / "cpp" / "tests"

REQ_ID_PATTERN = re.compile(r"L(?P<level>[123])-(?P<cat>[A-Z]+)-(?P<num>\d+)")
# Catch2 test declaration: TEST_CASE("name", "[tag][tag]"). clang-format wraps
# long declarations across lines, so the whitespace between the tokens has to be
# permissive -- a stricter pattern silently stops matching the moment the
# formatter decides a line is too long, and the requirement quietly reads as
# untested rather than as a parsing failure.
CPP_TEST_CASE = re.compile(
    r'TEST_CASE\s*\(\s*"(?P<name>(?:[^"\\]|\\.)*)"\s*,\s*"(?P<tags>[^"]*)"',
    re.MULTILINE,
)
L1_HEADER = re.compile(r"^###\s+(L1-[A-Z]+-\d+)\s*$", re.MULTILINE)
L2_HEADER = re.compile(r"^####\s+(L2-[A-Z]+-\d+)\s*$", re.MULTILINE)
L2_PARENT_LINE = re.compile(r"^\*\*Parent\*\*:\s+(L1-[A-Z]+-\d+)\s*$", re.MULTILINE)
L3_LINE = re.compile(
    r"^\*\*L3-([A-Z]+)-(\d+)\*\*\s+·\s+Parent:\s+(L2-[A-Z]+-\d+)\s+·\s+Verification:\s+([^\n·]+)"
    r"(?:·\s+Evidence:\s+([^\n]+))?",
    re.MULTILINE,
)
L1_L2_VM_LINE = re.compile(
    r"^\*\*Verification Method\*\*:\s+([^\n]+)$",
    re.MULTILINE,
)
# Optional companion to Verification Method for requirements verified by
# Inspection / Analysis / Demonstration. A declared method says only *how*
# a requirement would be checked; it is not itself proof that anyone did.
# This names the artifact that carries the check — a script, a CI job, a
# source symbol — so an I/A/D requirement can be Implemented on the same
# terms as a Test one: because something concrete backs it.
L1_L2_EVIDENCE_LINE = re.compile(
    r"^\*\*Evidence\*\*:\s+([^\n]+)$",
    re.MULTILINE,
)
_BACKTICKED = re.compile(r"`([^`]+)`")
# Single-letter DO-178 verification codes embedded in either the
# free-form L1/L2 "Test (T), Inspection (I)" phrasing or the compact
# L3 "T, I" form. Each letter sits at a word boundary in both shapes.
_METHOD_LETTER = re.compile(r"\b([TIAD])\b")

# Categories in declaration order. L1 categories appear in L1-REQ.md;
# L2-only categories (RDR/MSG/WRT/FLT) have no L1 parent of their own —
# their L2 statements parent into other categories' L1s.
# L3-only categories (PY/RS) carry per-implementation technology constraints.
CATEGORIES: list[tuple[str, str]] = [
    # L1 + L2 categories
    ("DEC", "Binary decoding"),
    ("OUT", "CSV output and destination integrity"),
    ("DLT", "DELTA inter-arrival tracking"),
    ("CLI", "CLI capability surface"),
    ("LOG", "Diagnostic logging"),
    ("MODE", "Strict and lenient handling"),
    ("SYN", "Synchronization, validation, invariants"),
    ("ERR", "DDC error records and SPURIOUS_DATA"),
    ("CFG", "Configuration"),
    ("CONF", "Cross-implementation conformance"),
    ("EXIT", "Exit-code semantics and operational contract"),
    ("ROB", "Robustness against arbitrary input"),
    ("MRG", "Multi-file time-sorted merge"),
    # L2-only categories (no L1 parent of the same code)
    ("RDR", "Reader behavior (L2)"),
    ("MSG", "Message semantics (L2)"),
    ("WRT", "CSV writer mechanics (L2 + L3)"),
    ("FLT", "Filtering mechanics (L2)"),
    # L3-only per-implementation technology categories
    ("PY", "Python implementation details (L3)"),
    ("RS", "Rust implementation details (L3)"),
    ("CPP", "C++ implementation details (L3)"),
]


def parse_l1_ids(doc: str) -> list[str]:
    """L1 ids appear as level-3 headers ``### L1-XXX-NNN`` in L1-REQ.md."""
    return L1_HEADER.findall(doc)


def _extract_methods(text: str) -> set[str]:
    """Extract DO-178 verification method letters from free-form text.

    Handles both the L1/L2 phrasing ("Test (T), Inspection (I)") and
    the L3 compact form ("T, I"). Returns a set of single-letter codes
    drawn from ``{T, I, A, D}``.
    """
    return set(_METHOD_LETTER.findall(text))


def _extract_evidence(text: str) -> list[str]:
    """Pull the backticked artifact names out of an ``**Evidence**`` line.

    Falls back to the whole stripped line when nothing is backticked, so a
    prose-only evidence note still counts as evidence rather than silently
    reading as none.
    """
    found = _BACKTICKED.findall(text)
    if found:
        return found
    stripped = text.strip()
    return [stripped] if stripped else []


def parse_l1_methods(doc: str) -> dict[str, set[str]]:
    """Return mapping L1-id -> set of verification-method letters."""
    result: dict[str, set[str]] = {}
    blocks = re.split(r"^###\s+(L1-[A-Z]+-\d+)\s*$", doc, flags=re.MULTILINE)
    for i in range(1, len(blocks), 2):
        l1_id = blocks[i]
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        m = L1_L2_VM_LINE.search(body)
        if m:
            result[l1_id] = _extract_methods(m.group(1))
    return result


def parse_l1_evidence(doc: str) -> dict[str, list[str]]:
    """Return mapping L1-id -> declared non-test evidence artifacts."""
    result: dict[str, list[str]] = {}
    blocks = re.split(r"^###\s+(L1-[A-Z]+-\d+)\s*$", doc, flags=re.MULTILINE)
    for i in range(1, len(blocks), 2):
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        m = L1_L2_EVIDENCE_LINE.search(body)
        if m:
            result[blocks[i]] = _extract_evidence(m.group(1))
    return result


def parse_l2_parent_map(doc: str) -> dict[str, str]:
    """Return mapping L2-id -> L1-parent-id from L2-REQ.md."""
    result: dict[str, str] = {}
    blocks = re.split(r"^####\s+(L2-[A-Z]+-\d+)\s*$", doc, flags=re.MULTILINE)
    for i in range(1, len(blocks), 2):
        l2_id = blocks[i]
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        m = L2_PARENT_LINE.search(body)
        if m:
            result[l2_id] = m.group(1)
    return result


def parse_l2_methods(doc: str) -> dict[str, set[str]]:
    """Return mapping L2-id -> set of verification-method letters."""
    result: dict[str, set[str]] = {}
    blocks = re.split(r"^####\s+(L2-[A-Z]+-\d+)\s*$", doc, flags=re.MULTILINE)
    for i in range(1, len(blocks), 2):
        l2_id = blocks[i]
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        m = L1_L2_VM_LINE.search(body)
        if m:
            result[l2_id] = _extract_methods(m.group(1))
    return result


def parse_l2_evidence(doc: str) -> dict[str, list[str]]:
    """Return mapping L2-id -> declared non-test evidence artifacts."""
    result: dict[str, list[str]] = {}
    blocks = re.split(r"^####\s+(L2-[A-Z]+-\d+)\s*$", doc, flags=re.MULTILINE)
    for i in range(1, len(blocks), 2):
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        m = L1_L2_EVIDENCE_LINE.search(body)
        if m:
            result[blocks[i]] = _extract_evidence(m.group(1))
    return result


def parse_l3_parent_map(doc: str) -> dict[str, str]:
    """Return mapping L3-id -> L2-parent-id from L3-REQ.md."""
    result: dict[str, str] = {}
    for match in L3_LINE.finditer(doc):
        cat, num, parent, _verification, _evidence = match.groups()
        result[f"L3-{cat}-{num}"] = parent
    return result


def parse_l3_methods(doc: str) -> dict[str, set[str]]:
    """Return mapping L3-id -> set of verification-method letters."""
    result: dict[str, set[str]] = {}
    for match in L3_LINE.finditer(doc):
        cat, num, _parent, verification, _evidence = match.groups()
        result[f"L3-{cat}-{num}"] = _extract_methods(verification)
    return result


def parse_l3_evidence(doc: str) -> dict[str, list[str]]:
    """Return mapping L3-id -> declared non-test evidence artifacts.

    L3 statements are one-liners, so evidence rides on the same line as a
    trailing ``· Evidence: ...`` segment.
    """
    result: dict[str, list[str]] = {}
    for match in L3_LINE.finditer(doc):
        cat, num, _parent, _verification, evidence = match.groups()
        if evidence:
            result[f"L3-{cat}-{num}"] = _extract_evidence(evidence)
    return result


def collect_python_markers(tests_dir: Path) -> dict[str, list[str]]:
    """Walk every ``.py`` file under tests_dir and collect requirement markers."""
    marker_map: dict[str, list[str]] = defaultdict(list)
    if not tests_dir.is_dir():
        return marker_map
    for py_file in sorted(tests_dir.rglob("*.py")):
        if py_file.name == "__init__.py" or "conftest" in py_file.name:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                for req_id in _extract_pytest_requirement_ids(decorator):
                    rel = py_file.relative_to(ROOT).as_posix()
                    marker_map[req_id].append(f"{rel}::{node.name}")
    return marker_map


def _extract_pytest_requirement_ids(decorator: ast.expr) -> list[str]:
    """Return EVERY requirement id from a ``@pytest.mark.requirement(...)``.

    A marker may name several — ``@pytest.mark.requirement("L2-WRT-022",
    "L3-WRT-003", "L2-CFG-001")`` — and 23 of them do. This read only
    ``args[0]`` and discarded the rest, so a requirement named second or
    third lost the artifact that verifies it and the matrix understated its
    coverage. Nothing failed: the row still rendered, just with fewer
    artifacts than exist, which is the same silent-undercount failure the
    collector had once before with Rust attributes.

    Returns:
        The string ids in marker order, or an empty list if this decorator
        is not a requirement marker.
    """
    if not isinstance(decorator, ast.Call):
        return []
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr == "requirement"):
        return []
    return [
        arg.value
        for arg in decorator.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


_FN_DECL = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[(<]")


def collect_rust_markers(source_roots: list[Path]) -> dict[str, list[str]]:
    """Walk Rust source files and collect ``/// Requirements:`` markers.

    Each ``#[test]`` (or ``#[tokio::test]``) item may be preceded by a doc
    comment of the form ``/// Requirements: L2-WRT-015, L3-RS-007``. This
    function pairs each such marker with the next ``fn name(`` declaration
    and emits ``path::name`` artifacts.
    """
    marker_map: dict[str, list[str]] = defaultdict(list)
    for source_root in source_roots:
        if not source_root.is_dir():
            continue
        for rs_file in sorted(source_root.rglob("*.rs")):
            try:
                text = rs_file.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = rs_file.relative_to(ROOT).as_posix()
            pending_ids: list[str] = []
            saw_test_attr = False
            # Depth of an attribute that spans lines. A multi-line
            # `#[allow(..., reason = "...")]` sitting between a
            # `/// Requirements:` marker and its `fn` used to fall through to
            # the reset below, silently dropping the trace link -- the marker
            # was still in the source but the artifact vanished from the
            # matrix, which reads as an untested requirement.
            attr_depth = 0
            for line in text.splitlines():
                stripped = line.strip()
                if attr_depth > 0:
                    attr_depth += stripped.count("[") - stripped.count("]")
                    continue
                if stripped.startswith("///") and "Requirements:" in stripped:
                    _, _, after = stripped.partition("Requirements:")
                    for match in REQ_ID_PATTERN.finditer(after):
                        pending_ids.append(
                            f"L{match.group('level')}-"
                            f"{match.group('cat')}-{match.group('num')}"
                        )
                    continue
                if stripped.startswith("#["):
                    if (
                        "test]" in stripped
                        or "::test]" in stripped
                        or stripped.startswith("#[test")
                        or stripped.startswith("#[tokio::test")
                        or stripped.startswith("#[rstest")
                    ):
                        saw_test_attr = True
                    attr_depth = stripped.count("[") - stripped.count("]")
                    continue
                if stripped.startswith("//"):
                    continue
                if not stripped:
                    continue
                fn_match = _FN_DECL.match(line)
                if fn_match and saw_test_attr and pending_ids:
                    name = fn_match.group(1)
                    for req_id in pending_ids:
                        marker_map[req_id].append(f"{rel}::{name}")
                    pending_ids = []
                    saw_test_attr = False
                    continue
                # Any other code line resets the pending state
                pending_ids = []
                saw_test_attr = False
    return marker_map


def collect_cpp_markers(tests_dir: Path) -> dict[str, list[str]]:
    """Walk every ``.cpp`` file under tests_dir and collect Catch2 tag markers.

    A requirement id written as a Catch2 tag -- ``"[atomic][L3-WRT-001]"`` --
    is the marker, rather than a comment convention as in Rust. That is
    deliberate: Catch2 can *select* on tags, so the artifact string this emits
    is directly runnable against the built binary::

        ./aero1553_tests "[L3-WRT-001]"

    A single ``TEST_CASE`` may name several requirements, and the same
    requirement may be named by several test cases; both are normal.
    """
    marker_map: dict[str, list[str]] = defaultdict(list)
    if not tests_dir.is_dir():
        return marker_map
    for cpp_file in sorted(tests_dir.rglob("*.cpp")):
        try:
            source = cpp_file.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = cpp_file.relative_to(ROOT).as_posix()
        for match in CPP_TEST_CASE.finditer(source):
            for req_match in REQ_ID_PATTERN.finditer(match.group("tags")):
                req_id = (
                    f"L{req_match.group('level')}-"
                    f"{req_match.group('cat')}-{req_match.group('num')}"
                )
                marker_map[req_id].append(f"{rel}::{match.group('name')}")
    return marker_map


def collect_all_markers() -> dict[str, list[str]]:
    """Merge the Python, Rust and C++ marker collections."""
    merged: dict[str, list[str]] = defaultdict(list)
    for source in (
        collect_python_markers(PY_TESTS_DIR),
        collect_rust_markers(RUST_SOURCE_ROOTS),
        collect_cpp_markers(CPP_TESTS_DIR),
    ):
        for req_id, artifacts in source.items():
            merged[req_id].extend(artifacts)
    for req_id in merged:
        merged[req_id] = sorted(set(merged[req_id]))
    return merged


def build_matrix() -> str:
    """Build the full trace-matrix markdown."""
    l1_doc = L1_DOC.read_text(encoding="utf-8")
    l2_doc = L2_DOC.read_text(encoding="utf-8")
    l3_doc = L3_DOC.read_text(encoding="utf-8")

    l1_ids = parse_l1_ids(l1_doc)
    l1_methods = parse_l1_methods(l1_doc)
    l1_evidence = parse_l1_evidence(l1_doc)
    l2_parent = parse_l2_parent_map(l2_doc)
    l2_methods = parse_l2_methods(l2_doc)
    l2_evidence = parse_l2_evidence(l2_doc)
    l3_parent = parse_l3_parent_map(l3_doc)
    l3_methods = parse_l3_methods(l3_doc)
    l3_evidence = parse_l3_evidence(l3_doc)
    test_markers = collect_all_markers()

    l1_to_l2: dict[str, list[str]] = defaultdict(list)
    for l2_id, l1_id in l2_parent.items():
        l1_to_l2[l1_id].append(l2_id)
    for l1_id in l1_to_l2:
        l1_to_l2[l1_id].sort(key=_sort_key)

    l2_to_l3: dict[str, list[str]] = defaultdict(list)
    for l3_id, l2_id in l3_parent.items():
        l2_to_l3[l2_id].append(l3_id)
    for l2_id in l2_to_l3:
        l2_to_l3[l2_id].sort(key=_sort_key)

    lines: list[str] = []
    lines.append("# Aero1553 — Requirements Trace Matrix")
    lines.append("")
    lines.append("<!-- AUTO-GENERATED by scripts/build-trace-matrix.py. Do not edit by hand. -->")
    lines.append("")
    lines.append("## Purpose")
    lines.append("")
    lines.append(
        "Forward trace from L1 through L2 and L3 to verification artifacts. "
        "This file is regenerated from `L1-REQ.md`, `L2-REQ.md`, `L3-REQ.md`, "
        "the `@pytest.mark.requirement` markers in `python/tests/`, the "
        "`/// Requirements:` doc-comment tags above `#[test]` items in Rust "
        "source, and the Catch2 tag markers in `cpp/tests/`, each time "
        "`scripts/build-trace-matrix.py` is run."
    )
    lines.append("")
    lines.append("## Status rollup")
    lines.append("")
    lines.append(
        "Status is computed by `scripts/build-trace-matrix.py`'s rollup rule. "
        "This matrix is the single source of truth for live status; the source "
        "docs `L1-REQ.md`, `L2-REQ.md`, and `L3-REQ.md` carry only spec content."
    )
    lines.append("")
    lines.append("* **Draft** — Test verification is required but no test marker found.")
    lines.append(
        "* **Implemented** — at least one test marker exists (leaf), or every"
        " child rolls up to Implemented."
    )
    lines.append(
        "* **Implemented (I)** / **(A)** / **(D)** — the spec declares"
        " verification by Inspection / Analysis / Demonstration only **and**"
        " names the artifact carrying that check on an `**Evidence**` line,"
        " shown in the artifact column. A declared method with no evidence is"
        " a plan rather than a result and stays **Draft**. Combinations"
        " appear as e.g. ``Implemented (A+I)``."
    )
    lines.append(
        "* **Partially Implemented** — at least one child is Implemented but"
        " others are Draft, or the row itself has direct artifacts but its"
        " children include Drafts."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for cat_code, cat_title in CATEGORIES:
        cat_l1s = [req for req in l1_ids if req.startswith(f"L1-{cat_code}-")]
        if not cat_l1s:
            continue
        lines.append(f"### L1-{cat_code}: {cat_title}")
        lines.append("")

        lines.append("**L1 -> L2**")
        lines.append("")
        lines.append("| L1 ID | L2 Children | Test Artifacts | Status |")
        lines.append("|-------|-------------|----------------|--------|")
        for l1_id in cat_l1s:
            children = l1_to_l2.get(l1_id, [])
            children_str = ", ".join(children) if children else "_(none)_"
            child_statuses = [
                _l2_status(
                    l2_id,
                    l2_to_l3,
                    test_markers,
                    l2_methods,
                    l3_methods,
                    l2_evidence,
                    l3_evidence,
                )
                for l2_id in children
            ]
            # Render direct L1 markers. Most L1s decompose into L2/L3 and
            # carry none; the exceptions are Test-verified L1 *leaves*
            # (no L2 decomposition, e.g. L1-ROB-001) whose tests would
            # otherwise be invisible in the matrix.
            l1_artifacts = sorted(test_markers.get(l1_id, []))
            artifacts_str = (
                "<br>".join(f"`{a}`" for a in l1_artifacts)
                if l1_artifacts
                else "_(none)_"
            )
            status = compute_status(
                has_direct_artifacts=bool(test_markers.get(l1_id)),
                children_statuses=child_statuses,
                verification_methods=l1_methods.get(l1_id),
                evidence=l1_evidence.get(l1_id),
            )
            lines.append(f"| {l1_id} | {children_str} | {artifacts_str} | {status} |")
        lines.append("")

        lines.append("**L2 -> L3 -> Verification Artifacts**")
        lines.append("")
        lines.append("| L2 ID | L3 Children | Test Artifacts | Status |")
        lines.append("|-------|-------------|----------------|--------|")
        l1_set = set(cat_l1s)
        cat_l2s = sorted(
            [l2 for l2, parent in l2_parent.items() if parent in l1_set],
            key=_sort_key,
        )
        for l2_id in cat_l2s:
            l3_children = l2_to_l3.get(l2_id, [])
            artifacts: list[str] = list(test_markers.get(l2_id, []))
            for l3_id in l3_children:
                artifacts.extend(test_markers.get(l3_id, []))
            artifacts = sorted(set(artifacts))

            children_str = ", ".join(l3_children) if l3_children else "_(none)_"
            # With no test markers anywhere in the subtree, fall back to the
            # declared non-test evidence — the artifact column then names what
            # actually carries the check instead of reading "_(TBD)_" beside a
            # status claiming the requirement is met.
            if not artifacts:
                artifacts = sorted(
                    set(l2_evidence.get(l2_id, []))
                    | {e for l3_id in l3_children for e in l3_evidence.get(l3_id, [])}
                )
            artifacts_str = (
                "<br>".join(f"`{a}`" for a in artifacts) if artifacts else "_(TBD)_"
            )
            status = _l2_status(
                l2_id,
                l2_to_l3,
                test_markers,
                l2_methods,
                l3_methods,
                l2_evidence,
                l3_evidence,
            )
            lines.append(f"| {l2_id} | {children_str} | {artifacts_str} | {status} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Coverage summary")
    lines.append("")
    lines.append(
        "* **Tested** — at least one test marker (`@pytest.mark.requirement`,"
        " `/// Requirements:`, or a Catch2 tag) names this requirement."
    )
    lines.append(
        "* **Verified** — Tested, OR the spec declares verification by"
        " Inspection / Analysis / Demonstration only (no test required)."
    )
    lines.append("")
    lines.append("| Category | L1 | L2 | L3 | L2 tested | L3 tested | L2 verified | L3 verified |")
    lines.append("|----------|----|----|-----|-----------|-----------|-------------|-------------|")
    total_l1 = total_l2 = total_l3 = 0
    total_l2_tested = total_l3_tested = 0
    total_l2_verified = total_l3_verified = 0

    def _is_verified(
        req_id: str,
        methods: dict[str, set[str]],
        evidence: dict[str, list[str]] | None = None,
    ) -> bool:
        if test_markers.get(req_id):
            return True
        m = methods.get(req_id, set())
        # An I/A/D declaration counts only when backed by evidence, on the
        # same terms as compute_status — otherwise the summary would keep
        # reporting requirements as verified that nothing checks.
        return bool(m) and "T" not in m and bool((evidence or {}).get(req_id))

    for cat_code, _ in CATEGORIES:
        l1s = [req for req in l1_ids if req.startswith(f"L1-{cat_code}-")]
        l2s = [req for req in l2_parent if req.startswith(f"L2-{cat_code}-")]
        l3s = [req for req in l3_parent if req.startswith(f"L3-{cat_code}-")]
        l2_tested = sum(1 for l2 in l2s if test_markers.get(l2))
        l3_tested = sum(1 for l3 in l3s if test_markers.get(l3))
        l2_verified = sum(1 for l2 in l2s if _is_verified(l2, l2_methods, l2_evidence))
        l3_verified = sum(1 for l3 in l3s if _is_verified(l3, l3_methods, l3_evidence))
        lines.append(
            f"| {cat_code} | {len(l1s)} | {len(l2s)} | {len(l3s)} | "
            f"{l2_tested} | {l3_tested} | {l2_verified} | {l3_verified} |"
        )
        total_l1 += len(l1s)
        total_l2 += len(l2s)
        total_l3 += len(l3s)
        total_l2_tested += l2_tested
        total_l3_tested += l3_tested
        total_l2_verified += l2_verified
        total_l3_verified += l3_verified
    lines.append(
        f"| **Total** | **{total_l1}** | **{total_l2}** | **{total_l3}** | "
        f"**{total_l2_tested}** | **{total_l3_tested}** | "
        f"**{total_l2_verified}** | **{total_l3_verified}** |"
    )
    lines.append("")

    # L1 requirements are normally decomposed into L2/L3 and verified
    # transitively (so they are NOT double-counted in the denominator).
    # The exceptions are Test-verifiable L1 *leaves* — L1 requirements with
    # no L2 child (e.g. L1-ROB-001) — which are the only place certain tests
    # (the fuzz harness) attach. Those leaves are countable requirements in
    # their own right and are folded into the totals below.
    l1_leaves = [l1 for l1 in l1_ids if not l1_to_l2.get(l1)]
    l1_leaf_tested = sum(1 for l1 in l1_leaves if test_markers.get(l1))
    l1_leaf_verified = sum(1 for l1 in l1_leaves if _is_verified(l1, l1_methods, l1_evidence))

    countable = total_l2 + total_l3 + len(l1_leaves)
    if countable > 0:
        tested_n = total_l2_tested + total_l3_tested + l1_leaf_tested
        verified_n = total_l2_verified + total_l3_verified + l1_leaf_verified
        tested_pct = tested_n * 100 / countable
        verified_pct = verified_n * 100 / countable
        lines.append(
            f"The countable requirement set is every L2 and L3 requirement plus "
            f"the {len(l1_leaves)} Test-verifiable L1 *leaf* requirement(s) "
            f"(L1s with no L2 decomposition, e.g. `L1-ROB-001`, where the test "
            f"markers attach directly). Composite L1s are verified transitively "
            f"through their L2/L3 children, which are counted individually above."
        )
        lines.append("")
        lines.append(
            f"**Tested by at least one test marker**: "
            f"{tested_n} of {countable} ({tested_pct:.1f}%)."
        )
        lines.append("")
        lines.append(
            f"**Verified (Test or declared Inspection/Analysis/Demonstration)**: "
            f"{verified_n} of {countable} ({verified_pct:.1f}%)."
        )
        lines.append("")

    orphan_l2s = [l2 for l2 in l2_parent if l2_parent[l2] not in l1_ids]
    orphan_l3s = [l3 for l3 in l3_parent if l3_parent[l3] not in l2_parent]
    lines.append("### Orphan check")
    lines.append("")
    lines.append(f"* Orphan L2s (parent L1 not found): **{len(orphan_l2s)}**")
    lines.append(f"* Orphan L3s (parent L2 not found): **{len(orphan_l3s)}**")
    if orphan_l2s:
        lines.append("")
        lines.append("**Orphan L2s:**")
        for l2 in orphan_l2s:
            lines.append(f"* {l2} -> parent {l2_parent[l2]} not in L1-REQ.md")
    if orphan_l3s:
        lines.append("")
        lines.append("**Orphan L3s:**")
        for l3 in orphan_l3s:
            lines.append(f"* {l3} -> parent {l3_parent[l3]} not in L2-REQ.md")
    lines.append("")

    all_known = set(l1_ids) | set(l2_parent) | set(l3_parent)
    unknown_markers = sorted(set(test_markers) - all_known)
    lines.append("### Marker reference check")
    lines.append("")
    lines.append(f"* Markers referencing unknown requirement ids: **{len(unknown_markers)}**")
    if unknown_markers:
        lines.append("")
        for req_id in unknown_markers:
            count = len(test_markers[req_id])
            lines.append(f"* `{req_id}` — referenced by {count} test(s)")

    return "\n".join(lines) + "\n"


def _sort_key(req_id: str) -> tuple[str, int]:
    """Sort requirement ids by category then numeric suffix."""
    m = REQ_ID_PATTERN.search(req_id)
    if not m:
        return (req_id, 0)
    return (m.group("cat"), int(m.group("num")))


def compute_status(
    *,
    has_direct_artifacts: bool,
    children_statuses: list[str],
    verification_methods: set[str] | None = None,
    evidence: list[str] | None = None,
) -> str:
    """Roll up status for one requirement node.

    Verification-method awareness: a leaf with no test marker that
    declares only Inspection / Analysis / Demonstration verification
    can reach ``Implemented (I)`` / ``(A)`` / ``(D)`` (or a
    combination) — but **only** if it also declares ``**Evidence**``
    naming what carries the check. A declared method describes how a
    requirement *would* be verified; on its own it is a plan, not a
    result, and until v2.12.0 three requirements were reported
    ``Implemented (I)`` with a literal ``_(TBD)_`` in their artifact
    column. Method without evidence is now ``Draft``, exactly as a
    Test-verified leaf with no test marker is ``Draft``.

    Parent rollup treats any ``Implemented...`` child as a positive
    credit when deciding ``Implemented`` vs ``Partially Implemented``
    vs ``Draft``.
    """

    def _non_test_status() -> str | None:
        """``Implemented (…)`` for an evidenced I/A/D node, else None."""
        if not verification_methods or "T" in verification_methods:
            return None
        if not evidence:
            return None
        return f"Implemented ({'+'.join(sorted(verification_methods))})"

    if not children_statuses:
        if has_direct_artifacts:
            return "Implemented"
        return _non_test_status() or "Draft"

    n = len(children_statuses)
    impl_count = sum(1 for s in children_statuses if s.startswith("Implemented"))
    draft_count = sum(1 for s in children_statuses if s == "Draft")

    if impl_count == n:
        return "Implemented"
    if draft_count == n and not has_direct_artifacts:
        return _non_test_status() or "Draft"
    return "Partially Implemented"


def _l2_status(
    l2_id: str,
    l2_to_l3: dict[str, list[str]],
    test_markers: dict[str, list[str]],
    l2_methods: dict[str, set[str]],
    l3_methods: dict[str, set[str]],
    l2_evidence: dict[str, list[str]] | None = None,
    l3_evidence: dict[str, list[str]] | None = None,
) -> str:
    """Compute one L2's status by rolling up its L3 children + direct markers."""
    l2_evidence = l2_evidence or {}
    l3_evidence = l3_evidence or {}
    l3_children = l2_to_l3.get(l2_id, [])
    child_statuses = [
        compute_status(
            has_direct_artifacts=bool(test_markers.get(l3_id)),
            children_statuses=[],
            verification_methods=l3_methods.get(l3_id),
            evidence=l3_evidence.get(l3_id),
        )
        for l3_id in l3_children
    ]
    return compute_status(
        has_direct_artifacts=bool(test_markers.get(l2_id)),
        children_statuses=child_statuses,
        verification_methods=l2_methods.get(l2_id),
        evidence=l2_evidence.get(l2_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if the file would change.",
    )
    args = parser.parse_args(argv)

    new_content = build_matrix()
    if args.check:
        try:
            current = TRACE_DOC.read_bytes().decode("utf-8")
        except OSError:
            current = ""
        if current != new_content:
            print(
                f"{TRACE_DOC.relative_to(ROOT).as_posix()} is out of date. "
                "Run `python scripts/build-trace-matrix.py` to regenerate.",
                file=sys.stderr,
            )
            return 1
        return 0

    # Pin LF line endings so the file is portable across platforms and
    # passes the repo's CRLF guard. Path.write_text on Windows defaults
    # to translating "\n" to "\r\n"; bypass via write_bytes.
    TRACE_DOC.write_bytes(new_content.encode("utf-8"))
    print(f"Wrote {TRACE_DOC.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
