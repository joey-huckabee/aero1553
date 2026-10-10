//! Text rules shared across modules, mirroring `cpp/include/mie/text.hpp`.

/// Remove leading and trailing ASCII spaces and tabs -- nothing else.
///
/// The one trim for text taken from a file name: a `--manifest` line
/// (L2-MRG-001 rule 4) and the `MUX` field (L2-WRT-020) both call it, as
/// `text::trim_ascii_blank` does in C++. The Python package reaches both
/// through this crate. The config parser uses it too, for the blanks around a
/// line, a key, a value and an array item (L2-CFG-010).
///
/// Deliberately NOT `str::trim`, which also removes Unicode whitespace -- a
/// no-break space (U+00A0), an ideographic space (U+3000), a line break. A
/// filename may legitimately contain any of them, and the C++ implementation
/// cannot classify them: it is locale-free by rule
/// (`scripts/assert-locale-free.sh`) and would have to embed a Unicode table to
/// agree. Each caller once differed exactly here, each found separately --
/// two implementations silently editing text the third passed through.
pub(crate) fn trim_ascii_blank(s: &str) -> &str {
    s.trim_matches([' ', '\t'])
}

/// `bytes` as printable ASCII, for a diagnostic that repeats what the operator
/// wrote (L2-CLI-014, L2-CLI-022).
///
/// A printable ASCII byte stands for itself, except `"` and `\`, which are
/// written `\"` and `\\`; every other byte is `\xNN` in upper-case hex. Byte by
/// byte rather than character by character, so a non-ASCII character reads the
/// same here as in C++, which sees only bytes: an e-acute is `\xC3\xA9` in both.
///
/// `{:?}` did this job before and did it differently: it escapes control
/// characters but passes non-ASCII through, so a diagnostic could carry the
/// very bytes a Windows console turns into mojibake.
pub(crate) fn escape_bytes(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut out = String::with_capacity(bytes.len());
    for &b in bytes {
        match b {
            b'"' => out.push_str("\\\""),
            b'\\' => out.push_str("\\\\"),
            0x20..=0x7E => out.push(char::from(b)),
            _ => {
                let _ = write!(out, "\\x{b:02X}");
            }
        }
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
    use super::{escape_bytes, quote, trim_ascii_blank};

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

    /// Requirements: L2-CLI-014, L2-CLI-022
    #[test]
    fn quote_writes_every_byte_as_printable_ascii() {
        assert_eq!(quote("per-file"), "\"per-file\"");
        assert_eq!(quote(""), "\"\"");
        assert_eq!(quote("a\"b\\c"), "\"a\\\"b\\\\c\"");
        assert_eq!(quote("a\tb\r\n"), "\"a\\x09b\\x0D\\x0A\"");
        assert_eq!(quote("~ \x7f"), "\"~ \\x7F\"");
        // Byte by byte, upper-case hex, as C++ writes it.
        let e_acute = char::from_u32(0xE9).unwrap().to_string();
        assert_eq!(quote(&e_acute), "\"\\xC3\\xA9\"");
        let bom = char::from_u32(0xFEFF).unwrap().to_string();
        assert_eq!(quote(&bom), "\"\\xEF\\xBB\\xBF\"");
        assert_eq!(escape_bytes(&[0xFF, b'a']), "\\xFFa");
        for b in 0..=u8::MAX {
            assert!(
                escape_bytes(&[b])
                    .bytes()
                    .all(|c| (0x20..=0x7E).contains(&c))
            );
        }
    }
}
