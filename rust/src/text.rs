//! Text rules shared across modules, mirroring `cpp/include/mie/text.hpp`.

/// Remove leading and trailing ASCII spaces and tabs -- nothing else.
///
/// The one trim for text taken from a file name: a `--manifest` line
/// (L2-MRG-001 rule 4) and the `MUX` field (L2-WRT-020) both call it, as
/// `text::trim_ascii_blank` does in C++. The Python package reaches both
/// through this crate.
///
/// Deliberately NOT `str::trim`, which also removes Unicode whitespace -- a
/// no-break space (U+00A0), an ideographic space (U+3000), a line break. A
/// filename may legitimately contain any of them, and the C++ implementation
/// cannot classify them: it is locale-free by rule
/// (`scripts/assert-locale-free.sh`) and would have to embed a Unicode table to
/// agree. Both callers once differed exactly here, each found separately --
/// two implementations silently editing a name the third passed through.
pub(crate) fn trim_ascii_blank(s: &str) -> &str {
    s.trim_matches([' ', '\t'])
}

#[cfg(test)]
mod tests {
    use super::trim_ascii_blank;

    /// Requirements: L2-MRG-001, L2-WRT-020
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
}
