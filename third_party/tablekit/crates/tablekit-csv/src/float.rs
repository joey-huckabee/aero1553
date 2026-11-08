//! Floats as pandas writes them: the shortest digits that read back as the same value, laid
//! out as NumPy's `str` does.
//!
//! | | positional when | otherwise |
//! |---|---|---|
//! | `f64` | `1e-4 <= abs < 1e16`, or zero | scientific |
//! | `f32` | `1e-4 <= abs < 1e6`, or zero | scientific |
//!
//! Positional always has a decimal point (`123.0`, `0.0001`); scientific has one only with
//! more than one digit, and an exponent with a sign and at least two digits (`1e-05`,
//! `1.2345679e+08`, `1e-300`). Checked against NumPy's output in `testdata/floats/`.
//!
//! The digits come from Żmij (the `zmij` crate, a port of Victor Zverovich's algorithm), which
//! finds the shortest digits with no allocation and, where two are equally short and equally
//! close, takes the even one, as Python and NumPy do. Rust's own `{:e}` takes the upper one
//! there. zmij formats a double in about half the time Ryū (0.3.1) took, and a quarter of
//! the time `{:e}` takes.

/// Below this magnitude, scientific notation.
const POSITIONAL_MIN: f64 = 1e-4;
/// From this magnitude up, scientific notation: NumPy's cutoffs.
const POSITIONAL_MAX_F64: f64 = 1e16;
const POSITIONAL_MAX_F32: f64 = 1e6;

/// Appends finite `v` to `out`.
// HOT PATH: per value.
#[inline]
pub(crate) fn write_f64(out: &mut Vec<u8>, v: f64) {
    let mut buffer = zmij::Buffer::new();
    let text = buffer.format_finite(v).as_bytes();
    write(out, text, positional(v.abs(), POSITIONAL_MAX_F64));
}

/// Appends finite `v` to `out`, with `f32`'s shortest digits and cutoffs.
// HOT PATH: per value.
#[inline]
pub(crate) fn write_f32(out: &mut Vec<u8>, v: f32) {
    let mut buffer = zmij::Buffer::new();
    let text = buffer.format_finite(v).as_bytes();
    write(
        out,
        text,
        positional(f64::from(v.abs()), POSITIONAL_MAX_F32),
    );
}

/// Appends zmij's `text` in NumPy's layout. In the positional range zmij's text without an
/// exponent is already NumPy's (`0.000921`, `123.0`, `-0.0`), so it is copied as it is: the
/// common case for times and measurements. Anything else is laid out digit by digit.
// HOT PATH: per value.
#[inline]
fn write(out: &mut Vec<u8>, text: &[u8], positional: bool) {
    // zmij writes an exponent last (`e+16`, `e-7`, `e-324`), so only the tail can hold one.
    // A loop, not `contains`: on a byte slice that calls `memchr`, about 2% of a CSV dump.
    let tail = &text[text.len().saturating_sub(5)..];
    #[allow(clippy::manual_contains)]
    let exponent = tail.iter().any(|&b| b == b'e');
    if positional && !exponent {
        out.extend_from_slice(text);
    } else {
        lay_out(out, text, positional);
    }
}

fn positional(abs: f64, max: f64) -> bool {
    abs == 0.0 || (POSITIONAL_MIN..max).contains(&abs)
}

/// Rewrites zmij's text (`-1.5`, `0.0001`, `1e+16`, `1.234e-7`: a sign, digits with an
/// optional point, an optional `e` and signed exponent) in NumPy's layout.
// HOT PATH: per value.
fn lay_out(out: &mut Vec<u8>, text: &[u8], positional: bool) {
    let (negative, text) = match text.split_first() {
        Some((b'-', rest)) => (true, rest),
        _ => (false, text),
    };
    let e_at = text.iter().position(|&b| b == b'e').unwrap_or(text.len());
    let (mantissa, exponent) = text.split_at(e_at);
    let exponent = parse_exponent(exponent.get(1..).unwrap_or_default());

    // The significant digits, without the point or leading and trailing zeros, and where
    // the point falls among them: the value is 0.DIGITS x 10^point.
    let mut digits = [0u8; 32];
    let mut count = 0;
    let mut point = exponent;
    let mut seen_point = false;
    for &b in mantissa {
        if b == b'.' {
            seen_point = true;
        } else if count == 0 && b == b'0' {
            if seen_point {
                point -= 1;
            }
        } else if count < digits.len() {
            digits[count] = b;
            count += 1;
            if !seen_point {
                point += 1;
            }
        }
    }
    while count > 0 && digits[count - 1] == b'0' {
        count -= 1;
    }
    let digits = &digits[..count];

    if negative {
        out.push(b'-');
    }
    let Some((&first, rest)) = digits.split_first() else {
        out.extend_from_slice(b"0.0");
        return;
    };
    if positional {
        if point <= 0 {
            out.extend_from_slice(b"0.");
            out.extend(std::iter::repeat_n(b'0', point.unsigned_abs() as usize));
            out.extend_from_slice(digits);
        } else {
            let point = point.unsigned_abs() as usize;
            if point >= digits.len() {
                out.extend_from_slice(digits);
                out.extend(std::iter::repeat_n(b'0', point - digits.len()));
                out.extend_from_slice(b".0");
            } else {
                out.extend_from_slice(&digits[..point]);
                out.push(b'.');
                out.extend_from_slice(&digits[point..]);
            }
        }
    } else {
        out.push(first);
        if !rest.is_empty() {
            out.push(b'.');
            out.extend_from_slice(rest);
        }
        let e = point - 1;
        out.push(b'e');
        out.push(if e < 0 { b'-' } else { b'+' });
        let e = e.unsigned_abs();
        if e < 10 {
            out.push(b'0');
        }
        out.extend_from_slice(itoa::Buffer::new().format(e).as_bytes());
    }
}

fn parse_exponent(text: &[u8]) -> i32 {
    let (negative, digits) = match text.split_first() {
        Some((b'-', rest)) => (true, rest),
        Some((b'+', rest)) => (false, rest),
        _ => (false, text),
    };
    let magnitude = digits
        .iter()
        .fold(0i32, |n, &d| n * 10 + i32::from(d.wrapping_sub(b'0')));
    if negative { -magnitude } else { magnitude }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f64s(v: f64) -> String {
        let mut out = Vec::new();
        write_f64(&mut out, v);
        String::from_utf8(out).unwrap()
    }

    fn f32s(v: f32) -> String {
        let mut out = Vec::new();
        write_f32(&mut out, v);
        String::from_utf8(out).unwrap()
    }

    #[test]
    fn as_numpy_writes_them() {
        let cases: &[(f64, &str)] = &[
            (0.1, "0.1"),
            (123.0, "123.0"),
            (-0.0, "-0.0"),
            (0.0, "0.0"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (0.000_123_4, "0.0001234"),
            (1e16, "1e+16"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1.5e15, "1500000000000000.0"),
            (1.234_567_890_123_456_8e17, "1.2345678901234568e+17"),
            (5e-324, "5e-324"),
            (1e-300, "1e-300"),
            (-2.5e-3, "-0.0025"),
            (-1_275_969_321_916_370.2, "-1275969321916370.2"),
        ];
        for &(v, want) in cases {
            assert_eq!(f64s(v), want, "{v:?}");
        }
        let cases: &[(f32, &str)] = &[
            (0.1, "0.1"),
            (1e-4, "1e-04"),
            (0.000_11, "0.00011"),
            (65504.0, "65504.0"),
            (999_999.94, "999999.94"),
            (1e6, "1e+06"),
            (16_777_216.0, "1.6777216e+07"),
            (3.4e38, "3.4e+38"),
            (1e-45, "1e-45"),
            (-0.0, "-0.0"),
            (354_853.62, "354853.62"),
        ];
        for &(v, want) in cases {
            assert_eq!(f32s(v), want, "{v:?}");
        }
    }
}
