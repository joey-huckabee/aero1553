//! Text rules shared across modules.

/// Remove leading and trailing ASCII spaces and tabs -- nothing else.
///
/// The one trim for text taken from a file name: a `--manifest` line
/// (L2-MRG-001 rule 4) and the `MUX` field (L2-WRT-020) both call it, as
/// `text::trim_ascii_blank` did in the former C++ tree. The Python package reaches both
/// through this crate. The config parser uses it too, for the blanks around a
/// line, a key, a value and an array item (L2-CFG-010).
///
/// Deliberately NOT `str::trim`, which also removes Unicode whitespace -- a
/// no-break space (U+00A0), an ideographic space (U+3000), a line break. A
/// filename may legitimately contain any of them, and the C++ implementation
/// (removed in 5.0.0) could not classify them: it was locale-free by rule and
/// would have had to embed a Unicode table to agree. Each caller once differed
/// exactly here, each found separately -- two implementations silently editing
/// text the third passed through.
pub(crate) fn trim_ascii_blank(s: &str) -> &str {
    s.trim_matches([' ', '\t'])
}

// ── Printing text from outside (L2-CLI-025) ──────────────────────────
//
// One rule for every character the program prints that it did not write
// itself -- a file name, an argument, a config value, an operating-system
// message: printable ASCII stands for itself, and every other character is
// written `\u{hex}`, newline and tab included. A byte that is not UTF-8 (a
// POSIX file name may hold one) is `\xNN`, and an unpaired surrogate (a
// Windows file name may hold one) is `\u{d800}`-style. So all output is ASCII
// (L2-CLI-014), reads the same on every console, and a newline in a file name
// cannot start a forged log line. The CSV is data, not printed text: its `MUX`
// cell keeps the characters it was given (L2-WRT-020).

/// Write `c` by the L2-CLI-025 rule.
fn push_escaped(out: &mut String, c: char) {
    use std::fmt::Write as _;
    if (' '..='~').contains(&c) {
        out.push(c);
    } else {
        let _ = write!(out, "\\u{{{:x}}}", u32::from(c));
    }
}

/// `s` by the L2-CLI-025 rule; borrowed when nothing needs escaping, which is
/// every message about an ASCII name.
pub(crate) fn escape_text(s: &str) -> std::borrow::Cow<'_, str> {
    if s.bytes().all(|b| (0x20..=0x7E).contains(&b)) {
        return std::borrow::Cow::Borrowed(s);
    }
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        push_escaped(&mut out, c);
    }
    std::borrow::Cow::Owned(out)
}

/// `bytes` by the L2-CLI-025 rule: valid UTF-8 character by character, and
/// each byte of an invalid sequence as `\xNN`.
fn escape_lossless_bytes(bytes: &[u8], out: &mut String) {
    use std::fmt::Write as _;
    for chunk in bytes.utf8_chunks() {
        for c in chunk.valid().chars() {
            push_escaped(out, c);
        }
        for b in chunk.invalid() {
            let _ = write!(out, "\\x{b:02X}");
        }
    }
}

/// An OS string -- a path, an argument -- by the L2-CLI-025 rule, from the
/// platform's own form, so a byte or code unit that is not text is shown as
/// itself rather than as the U+FFFD a lossy conversion leaves.
#[must_use]
pub fn escape_os(os: &std::ffi::OsStr) -> String {
    let mut out = String::with_capacity(os.len() + 8);
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        escape_lossless_bytes(os.as_bytes(), &mut out);
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        for unit in char::decode_utf16(os.encode_wide()) {
            match unit {
                Ok(c) => push_escaped(&mut out, c),
                Err(e) => {
                    use std::fmt::Write as _;
                    let _ = write!(out, "\\u{{{:x}}}", e.unpaired_surrogate());
                }
            }
        }
    }
    #[cfg(not(any(unix, windows)))]
    for c in os.to_string_lossy().chars() {
        push_escaped(&mut out, c);
    }
    out
}

/// A path as a diagnostic shows it (L2-CLI-025): `format!("{}", show(p))` in
/// place of `p.display()`.
pub(crate) fn show<P: AsRef<std::path::Path> + ?Sized>(p: &P) -> Shown<'_> {
    Shown(p.as_ref())
}

/// [`show`]'s result: renders on demand, allocating only to format.
pub(crate) struct Shown<'a>(&'a std::path::Path);

impl std::fmt::Display for Shown<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&escape_os(self.0.as_os_str()))
    }
}

/// `bytes` as a diagnostic repeats a value, a key or a line the operator wrote
/// (L2-CLI-022), inside double quotes: `"` and `\` are written `\"` and `\\`
/// so the quotes delimit it unambiguously, and everything else follows the
/// L2-CLI-025 rule -- an e-acute is `\u{e9}` here as in a file name, and a byte
/// that is not UTF-8 is `\xNN`.
pub(crate) fn escape_bytes(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len());
    for chunk in bytes.utf8_chunks() {
        for c in chunk.valid().chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                c => push_escaped(&mut out, c),
            }
        }
        let mut tail = String::new();
        escape_lossless_bytes(chunk.invalid(), &mut tail);
        out.push_str(&tail);
    }
    out
}

/// `s` escaped by [`escape_bytes`] and wrapped in double quotes: the one form in
/// which a diagnostic repeats a value, a key or a line the operator wrote.
pub(crate) fn quote(s: &str) -> String {
    format!("\"{}\"", escape_bytes(s.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::{escape_bytes, escape_os, escape_text, quote, show, trim_ascii_blank};

    /// Requirements: L2-MRG-001, L2-WRT-020, L2-CFG-010
    #[test]
    fn trims_spaces_and_tabs_only() {
        assert_eq!(trim_ascii_blank(" \t aa \t "), "aa");
        assert_eq!(trim_ascii_blank(" \t "), "");
        // Interior blanks are part of the name.
        assert_eq!(trim_ascii_blank(" a b "), "a b");
        // Unicode spaces and the other ASCII whitespace bytes are kept. The
        // non-ASCII ones are built at run time: a source literal holding one
        // would trip the shipped-literal ASCII gate (L2-CLI-014).
        let unicode = [0xA0, 0x3000, 0x2003, 0x85].map(|c| char::from_u32(c).unwrap());
        for kept in unicode.into_iter().chain(['\r', '\n', '\x0b', '\x0c']) {
            let s = format!("{kept}aa{kept}");
            assert_eq!(trim_ascii_blank(&s), s, "trimmed {kept:?}");
        }
    }

    /// Requirements: L2-CLI-014, L2-CLI-022, L2-CLI-025
    #[test]
    fn quote_follows_the_one_rule_inside_quotes() {
        assert_eq!(quote("per-file"), "\"per-file\"");
        assert_eq!(quote(""), "\"\"");
        assert_eq!(quote("a\"b\\c"), "\"a\\\"b\\\\c\"");
        assert_eq!(quote("a\tb\r\n"), "\"a\\u{9}b\\u{d}\\u{a}\"");
        assert_eq!(quote("~ \x7f"), "\"~ \\u{7f}\"");
        // Character by character, as everywhere else (L2-CLI-025).
        let e_acute = char::from_u32(0xE9).unwrap().to_string();
        assert_eq!(quote(&e_acute), "\"\\u{e9}\"");
        let bom = char::from_u32(0xFEFF).unwrap().to_string();
        assert_eq!(quote(&bom), "\"\\u{feff}\"");
        // A byte that is not UTF-8 is shown as itself.
        assert_eq!(escape_bytes(&[0xFF, b'a']), "\\xFFa");
        assert_eq!(escape_bytes(&[b'a', 0xC3]), "a\\xC3");
        for b in 0..=u8::MAX {
            assert!(
                escape_bytes(&[b])
                    .bytes()
                    .all(|c| (0x20..=0x7E).contains(&c))
            );
        }
    }

    fn c(code: u32) -> String {
        char::from_u32(code).unwrap().to_string()
    }

    /// Requirements: L2-CLI-014, L2-CLI-025
    #[test]
    fn escape_text_keeps_printable_ascii_and_escapes_the_rest() {
        let cases: [(String, &str); 9] = [
            ("rec.mie".into(), "rec.mie"),
            (r"C:\data\rec.mie".into(), r"C:\data\rec.mie"),
            (format!("caf{}.mie", c(0xE9)), r"caf\u{e9}.mie"),
            (format!("{}{}", c(0x4E2D), c(0x6587)), r"\u{4e2d}\u{6587}"),
            (c(0x1F600), r"\u{1f600}"),
            ("a\nERROR [x] forged".into(), r"a\u{a}ERROR [x] forged"),
            ("tab\there".into(), r"tab\u{9}here"),
            ("cr\rbell\x07esc\x1b".into(), r"cr\u{d}bell\u{7}esc\u{1b}"),
            ("del\x7f".into(), r"del\u{7f}"),
        ];
        for (input, want) in cases {
            assert_eq!(escape_text(&input), want, "{input:?}");
            assert!(escape_text(&input).is_ascii());
        }
        // Already-ASCII text is not copied.
        assert!(matches!(
            escape_text("rec.mie"),
            std::borrow::Cow::Borrowed(_)
        ));
    }

    /// Requirements: L2-CLI-025
    #[test]
    fn show_renders_a_path_from_its_os_form() {
        let name = format!("dir/caf{}\n.mie", c(0xE9));
        assert_eq!(
            show(std::path::Path::new(&name)).to_string(),
            r"dir/caf\u{e9}\u{a}.mie"
        );
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            // Built from numbers: a byte-string literal holding them would
            // trip the shipped-literal ASCII gate, which scans this module.
            let bytes = [b"bad".as_slice(), &[0xFF, 0xC3], b"name"].concat();
            let os = std::ffi::OsStr::from_bytes(&bytes);
            assert_eq!(escape_os(os), r"bad\xFF\xC3name");
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStringExt;
            let os = std::ffi::OsString::from_wide(&[0x61, 0xD800, 0x62]);
            assert_eq!(escape_os(&os), r"a\u{d800}b");
        }
    }
}
