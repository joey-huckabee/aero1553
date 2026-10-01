#!/usr/bin/env python3
"""Assert that no shipped string literal can put a non-ASCII byte on a stream.

WHY THIS GATE EXISTS
--------------------

L2-CLI-014 originally required ASCII only of stdout *payloads* -- the ``dump``
report, the ``count`` integer, CSV -- on the reasoning that a payload is piped
and diffed while stderr prose is merely read by a human. The carve-out did not
survive contact with a Windows console.

Windows consoles do not default to UTF-8. A stock ``cmd.exe`` or conhost session
runs at the OEM code page, 437 in a US install, and every implementation writes
UTF-8 bytes unconditionally. An em dash is three bytes, ``e2 80 94``, which
CP437 draws as three unrelated glyphs::

    mie-decoder --- DDC MIL-STD-1553 MIE binary decoder     (intended)
    mie-decoder GCo DDC MIL-STD-1553 MIE binary decoder     (rendered, roughly)

That is the whole of the "garbage characters" an operator reports, and it looks
exactly like memory corruption to anyone who has not counted the bytes. Prose is
therefore subject to the same rule as payload, and L2-CLI-014 now says so.

The fix is ASCII at the source rather than a console call. ``SetConsoleOutputCP``
would mean new OS surface in a platform layer deliberately confined to five
concerns (``scripts/assert-platform-confined.sh``), a process-global state
change, and no help at all when stderr is redirected to a file that some later
tool reads at the code page. Rust and Python would each need their own
equivalent. ASCII needs none of that and cannot regress per-platform.

WHAT IS SCANNED
---------------

Shipped sources only -- ``cpp/src``, ``cpp/include``, ``rust/src``,
``python/src`` -- and within them only **string literals**. Comments and doc
comments are free to use whatever punctuation reads best; they are never
written to a stream.

The separate test trees (``cpp/tests``, ``rust/tests``, ``python/tests``) are
NOT scanned. Those suites legitimately construct non-ASCII strings in order to
prove the decoder handles them -- most visibly the UTF-8 filename cases behind
the ``?`` glob rule, where a two-byte and a four-byte character are the point of
the test.

Rust is the one exception, and it is deliberate rather than overlooked: Rust
unit tests live in ``#[cfg(test)]`` modules *inside* ``src/``, so their literals
are scanned too. Excluding them would mean tracking module attributes through
the scanner for no benefit -- an assertion message is printed only when a test
fails, and holding those to ASCII costs nothing. A Rust test that genuinely
needs a non-ASCII string should build it from an escape in a ``char`` or from
``char::from_u32``, or live in ``rust/tests/``.

Both spellings of a non-ASCII character are caught, because they are equally
capable of reaching a console and only one of them is greppable:

* a raw byte >= 0x80 sitting in the literal, and
* an escape that denotes one -- ``\\xE2``, ``\\u2014``, ``\\u{2014}``,
  ``\\N{EM DASH}``, or an octal ``\\342``.

C++ is the reason the escape half exists. ``cpp/src/cli.cpp`` spelled its help
banner ``"mie-decoder \\xE2\\x80\\x94 DDC ..."``, which is pure ASCII on disk and
an em dash on the wire, so a byte-level scan of the tree reported it clean while
the console showed mojibake.

Run standalone, or via ``scripts/repo-hygiene.sh``.

Exit 0 when clean, 1 when a violation is found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Shipped source roots, by language. Tests are deliberately absent -- see the
# module docstring.
TARGETS = (
    ("cpp", ("cpp/src", "cpp/include"), (".cpp", ".hpp")),
    ("rust", ("rust/src",), (".rs",)),
    ("python", ("python/src",), (".py",)),
)

# Vendored third-party code is not ours to reformat.
EXCLUDED_PARTS = ("third_party", "vendor", "__pycache__")


class Literal:
    """One string literal: its text, the line it started on, and its span.

    ``start``/``end`` bound the text *inside* the quotes. They exist so a
    companion rewriter can edit literals without touching comments -- this
    project's prose uses em dashes freely and correctly, and only the shipped
    strings are in scope.
    """

    def __init__(self, text: str, line: int, raw: bool, start: int = -1, end: int = -1) -> None:
        self.text = text
        self.line = line
        self.raw = raw
        self.start = start
        self.end = end


def _scan_c_like(src: str, rust: bool) -> list[Literal]:
    """Extract string literals from C++ or Rust source.

    A hand-rolled scanner rather than a regex: a regex cannot tell a quote
    inside a ``//`` comment from one that opens a literal, and this file's whole
    purpose is to be believed. Character literals are skipped -- a ``char`` or
    Rust ``char`` cannot carry a multi-byte sequence into a format string
    without going through a string first, and skipping them avoids mistaking
    the apostrophe in a comment for a literal opener.
    """
    out: list[Literal] = []
    i, line, n = 0, 1, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += src.count("\n", i, end)
            i = end
        elif rust and c == "r" and _rust_raw_opens(src, i):
            i, line = _take_rust_raw(src, i, line, out)
        elif c == '"':
            i, line = _take_quoted(src, i, line, out)
        elif c == "'":
            i = _skip_char_literal(src, i, rust)
        else:
            i += 1
    return out


def _rust_raw_opens(src: str, i: int) -> bool:
    """True when ``src[i]`` is the ``r`` of a raw string, not of an identifier.

    Without the preceding-character test, the ``r"`` in ``separator"`` -- the
    tail of an ordinary literal -- reads as a raw-string opener and the scanner
    desynchronises for the rest of the file.
    """
    if i and (src[i - 1].isalnum() or src[i - 1] == "_"):
        return False
    j = i + 1
    while j < len(src) and src[j] == "#":
        j += 1
    return j < len(src) and src[j] == '"'


def _take_rust_raw(src: str, i: int, line: int, out: list[Literal]) -> tuple[int, int]:
    hashes = 0
    j = i + 1
    while src[j] == "#":
        hashes += 1
        j += 1
    close = '"' + "#" * hashes
    end = src.find(close, j + 1)
    end = len(src) if end < 0 else end
    out.append(Literal(src[j + 1 : end], line, raw=True, start=j + 1, end=end))
    return end + len(close), line + src.count("\n", i, end)


def _take_quoted(src: str, i: int, line: int, out: list[Literal]) -> tuple[int, int]:
    """Consume one ``"``-delimited literal, honouring backslash escapes."""
    j = i + 1
    buf: list[str] = []
    n = len(src)
    while j < n:
        c = src[j]
        if c == "\\" and j + 1 < n:
            buf.append(src[j : j + 2])
            j += 2
            continue
        if c == '"':
            break
        buf.append(c)
        j += 1
    text = "".join(buf)
    out.append(Literal(text, line, raw=False, start=i + 1, end=j))
    return j + 1, line + text.count("\n")


def _skip_char_literal(src: str, i: int, rust: bool) -> int:
    """Step over a ``'x'`` literal, or over a lone apostrophe.

    Rust lifetimes (``'a``) and English possessives in prose both produce an
    apostrophe with no closing partner. A bounded look-ahead keeps either from
    swallowing the rest of the file.
    """
    j = i + 1
    if j < len(src) and src[j] == "\\":
        j += 2
        while j < len(src) and src[j] != "'" and src[j] != "\n":
            j += 1
        return j + 1
    if rust and j < len(src) and (src[j].isalpha() or src[j] == "_"):
        # A lifetime unless a quote closes it immediately after one character.
        if j + 1 < len(src) and src[j + 1] == "'":
            return j + 2
        return j
    if j + 1 < len(src) and src[j + 1] == "'":
        return j + 2
    return i + 1


def _scan_python(src: str) -> list[Literal]:
    """Extract string literals from Python source using the real tokenizer.

    ``ast`` would drop the prefix, and the prefix is what says whether ``\\u`` is
    an escape or two characters. Docstrings are string literals syntactically
    but are not output, so they are dropped by position: a docstring is the
    entire body of its own expression statement.

    **F-strings need their own arm, and the reason is a version split.** Under
    PEP 701 (Python 3.12+) an f-string is no longer one ``STRING`` token: it
    arrives as ``FSTRING_START`` / ``FSTRING_MIDDLE`` / ``FSTRING_END``, so a
    scanner that looks only at ``STRING`` sees every f-string as empty. On 3.11
    and earlier the same source yields a single ``STRING`` and is scanned.

    That is not hypothetical. This gate shipped in v2.15.0's predecessor with
    only the ``STRING`` arm, was run on 3.12, and reported the tree clean while
    five non-ASCII f-strings sat in ``python/src`` -- including the Python twins
    of two C++ messages the same release had just fixed. It was caught by
    running the identical gate under 3.10. Handling both shapes is therefore
    load-bearing, not tidiness.
    """
    import io
    import tokenize

    out: list[Literal] = []
    offsets = [0]
    for ln in src.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(ln))

    def offset_of(row: int, col: int) -> int:
        return offsets[row - 1] + col

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out

    # FSTRING_* exist only on 3.12+; getattr keeps this importable on 3.10/3.11,
    # where the STRING arm alone already covers f-strings.
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)

    fstring_raw = False
    for idx, tok in enumerate(toks):
        if fstring_start is not None and tok.type == fstring_start:
            # Prefix of the f-string being opened, e.g. `f"`, `rf"`, `fr"""`.
            fstring_raw = "r" in _py_prefix(tok.string)
            continue
        if fstring_middle is not None and tok.type == fstring_middle:
            # The literal text between replacement fields. Never a docstring:
            # an f-string is not a documentation string.
            out.append(
                Literal(
                    tok.string,
                    tok.start[0],
                    raw=fstring_raw,
                    start=offset_of(*tok.start),
                    end=offset_of(*tok.end),
                )
            )
            continue
        if tok.type != tokenize.STRING:
            continue
        if _is_docstring(toks, idx):
            continue
        out.append(
            Literal(
                tok.string,
                tok.start[0],
                raw="r" in _py_prefix(tok.string),
                start=offset_of(*tok.start),
                end=offset_of(*tok.end),
            )
        )
    return out


def _py_prefix(literal: str) -> str:
    m = re.match(r"[A-Za-z]*", literal)
    return (m.group(0) if m else "").lower()


def _is_docstring(toks: list, idx: int) -> bool:
    """True when this STRING token is a whole expression statement by itself.

    That is exactly the shape of a module, class or function docstring, and of
    a bare string used as a comment. Anything else -- an argument, an operand,
    a dict value -- is adjacent to a token that is not a statement boundary.
    """
    import tokenize as tk

    def significant(indices):
        for k in indices:
            if toks[k].type not in (tk.COMMENT, tk.NL):
                return toks[k]
        return None

    # Starts a logical line: nothing but a statement boundary precedes it.
    prev = significant(range(idx - 1, -1, -1))
    starts_line = prev is None or prev.type in (
        tk.NEWLINE,
        tk.INDENT,
        tk.DEDENT,
        tk.ENCODING,
    )
    if not starts_line:
        return False
    # Ends that logical line immediately: nothing is done with its value.
    nxt = significant(range(idx + 1, len(toks)))
    return nxt is not None and nxt.type == tk.NEWLINE


# Escapes that denote a codepoint. Octal is included because C++ accepts it and
# \342\200\224 is the same em dash by another spelling.
ESCAPE_PATTERNS = (
    (re.compile(r"\\x([0-9A-Fa-f]{2})"), 16, r"\xHH"),
    (re.compile(r"\\u\{([0-9A-Fa-f]+)\}"), 16, r"\u{...}"),
    (re.compile(r"\\u([0-9A-Fa-f]{4})"), 16, r"\uXXXX"),
    (re.compile(r"\\U([0-9A-Fa-f]{8})"), 16, r"\UXXXXXXXX"),
    (re.compile(r"\\([0-7]{1,3})"), 8, r"\NNN"),
)

NAMED_ESCAPE = re.compile(r"\\N\{([^}]*)\}")


def violations_in(lit: Literal) -> list[tuple[int, str]]:
    """Every reason this literal could emit a non-ASCII byte.

    Returns ``(line, reason)`` pairs with the line resolved to the offending
    character's own line, not the literal's first. The help banners are one
    literal spanning ninety lines, so the difference is the difference between
    a usable report and a pointer at the wrong end of the string.
    """
    found: list[tuple[int, str]] = []

    def at(offset: int) -> int:
        return lit.line + lit.text.count("\n", 0, offset)

    for offset, ch in enumerate(lit.text):
        if ord(ch) > 0x7F:
            found.append((at(offset), "raw U+%04X" % ord(ch)))
    if lit.raw:
        # A raw literal has no escapes; backslashes in it are data.
        return found
    for pattern, base, _label in ESCAPE_PATTERNS:
        for m in pattern.finditer(lit.text):
            value = int(m.group(1), base)
            if value > 0x7F:
                found.append((at(m.start()), "escape %s -> U+%04X" % (m.group(0), value)))
    for m in NAMED_ESCAPE.finditer(lit.text):
        found.append((at(m.start()), "named escape %s" % m.group(0)))
    return sorted(set(found))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    failures = 0
    scanned = 0
    for lang, roots, suffixes in TARGETS:
        for rel in roots:
            base = root / rel
            if not base.is_dir():
                print("assert-ascii-output: no such directory: %s" % rel, file=sys.stderr)
                return 1
            for path in sorted(base.rglob("*")):
                if path.suffix not in suffixes or not path.is_file():
                    continue
                if any(part in EXCLUDED_PARTS for part in path.parts):
                    continue
                scanned += 1
                src = path.read_text(encoding="utf-8", errors="surrogateescape")
                if lang == "python":
                    lits = _scan_python(src)
                else:
                    lits = _scan_c_like(src, rust=(lang == "rust"))
                shown = path.relative_to(root).as_posix()
                lines = src.splitlines()
                for lit in lits:
                    for line_no, reason in violations_in(lit):
                        failures += 1
                        text = lines[line_no - 1].strip() if line_no <= len(lines) else ""
                        print(
                            "assert-ascii-output: FAIL %s:%d  %s" % (shown, line_no, reason),
                            file=sys.stderr,
                        )
                        print("    %s" % text[:100], file=sys.stderr)

    if failures:
        print("", file=sys.stderr)
        print(
            "Shipped string literals must be ASCII (L2-CLI-014). Windows consoles\n"
            "default to the OEM code page, not UTF-8, so a non-ASCII byte reaches\n"
            "an operator as mojibake and reads as memory corruption. Use ASCII:\n"
            "    --      for an em dash        ->  for an arrow\n"
            "    -       for an en dash        *   for a bullet\n"
            "    section for a section sign    ... for an ellipsis\n"
            "Comments and doc comments are exempt; they are never written out.",
            file=sys.stderr,
        )
        return 1

    print("assert-ascii-output: OK (%d shipped source files, all string literals ASCII)" % scanned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
