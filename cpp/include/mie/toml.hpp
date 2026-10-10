// SPDX-License-Identifier: Apache-2.0
//
// A parser for a deliberately small subset of TOML: flat `[section]` headers
// and `key = value` lines, where a value is a string, integer, float, boolean,
// or a single-line array of those.
//
// ═══════════════════════════════════════════════════════════════════════════
// THIS COMPONENT IS MEANT TO BE LIFTABLE
// ═══════════════════════════════════════════════════════════════════════════
//
// It knows nothing about MIE. No decoder type appears in this header, no
// decoder header is included, and nothing here can fail in a way that mentions
// a recording, a record, or a CSV column. What it produces is a document; what
// that document MEANS is `mie/config.hpp`'s business.
//
// That separation is the point. In Fowler's terms this is the mapper's data
// source, not the mapper: the schema — which keys exist, what ranges they take,
// which enum spellings are legal, what the defaults are — lives one layer up.
// Mixing the two is what makes a config loader impossible to reuse, because
// every question you ask it comes back in the vocabulary of one application.
//
// TO LIFT THIS INTO ANOTHER PROJECT you need exactly:
//
//     include/mie/toml.hpp   include/mie/text.hpp
//     src/toml.cpp           src/text.cpp
//     tests/test_toml.cpp
//
// and a namespace rename. `text` is the ASCII-classification and number
// formatting helper; it is depended on rather than duplicated because it is
// itself domain-free, and because this parser MUST NOT use `<cctype>` — those
// functions read the locale table, and under `tr_TR` the lowercase of 'I' is a
// dotless i, which silently breaks case-insensitive key matching. There are no
// other dependencies: no exceptions are thrown, no logging happens, and the
// only standard headers used are <string>, <vector> and <cstdint>.
//
// ═══════════════════════════════════════════════════════════════════════════
// WHY A SUBSET, AND WHY REJECTIONS ARE A FEATURE
// ═══════════════════════════════════════════════════════════════════════════
//
// The Python implementation parses config with the standard library's full
// `tomllib`. Rust and C++ hand-roll this subset. Where full TOML accepts a form
// the flat schema cannot MODEL — a dotted key, an inline table, a nested table
// header, an array of tables — the hand-rolled parsers must REJECT it rather
// than silently store it somewhere the schema will never look. A silently
// dropped `output.no_clobber = true` is a safety option that appears to be set
// and is not.
//
// Every rejection below therefore has a twin: a form `tomllib` accepts and this
// refuses, on purpose, so all three implementations land in the same class.
// `tests/conformance/config_parity.py` and `config_fuzz.py` hold that line.

#ifndef MIE_TOML_HPP
#define MIE_TOML_HPP

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace mie {
namespace toml {

/// Which of the five kinds a value is.
///
/// No date, time, or datetime: the schema has no use for them, and accepting
/// them would mean carrying a date type for one caller that does not exist.
enum ValueKind { VALUE_STRING, VALUE_INTEGER, VALUE_FLOAT, VALUE_BOOLEAN, VALUE_ARRAY };

/// One parsed value.
///
/// A tagged struct rather than a real union. Both make the same guarantee about
/// which member is live; only one of them can be read in a debugger. The
/// payload is a string, an integer, a double and a vector — a few dozen bytes,
/// for a document that holds tens of entries once per process.
// A value that can CONTAIN values is a recursive type by definition, so its
// implicit copy operations sit in a cycle through the vector. The grammar
// bounds the depth at two -- an array holds only scalars -- so the recursion
// cannot run away. See `misc-no-recursion` in cpp/.clang-tidy.
class Value {
  public:
    Value();

    static Value of_string(const std::string& value);
    static Value of_integer(int64_t value);
    static Value of_float(double value);
    static Value of_boolean(bool value);
    static Value of_array(const std::vector<Value>& items);

    ValueKind kind() const { return kind_; }

    bool is_string() const { return kind_ == VALUE_STRING; }
    bool is_integer() const { return kind_ == VALUE_INTEGER; }
    bool is_float() const { return kind_ == VALUE_FLOAT; }
    bool is_boolean() const { return kind_ == VALUE_BOOLEAN; }
    bool is_array() const { return kind_ == VALUE_ARRAY; }

    /// Accessors. Reading the wrong one returns that member's default rather
    /// than misinterpreting the payload, so a caller that forgets to check
    /// `kind()` gets a wrong ANSWER instead of undefined behaviour. Callers
    /// that care — the schema layer does — check first and report a type error
    /// naming the key.
    const std::string& as_string() const { return string_; }
    int64_t as_integer() const { return integer_; }
    double as_float() const { return float_; }
    bool as_boolean() const { return boolean_; }
    const std::vector<Value>& as_array() const { return array_; }

    /// "string", "integer", "float", "boolean", "array" -- for a type-error
    /// message that can say what was found as well as what was wanted.
    const char* kind_name() const;

  private:
    ValueKind kind_;
    std::string string_;
    int64_t integer_;
    double float_;
    bool boolean_;
    std::vector<Value> array_;
};

/// Why parsing stopped.
///
/// A struct rather than an exception, matching the platform layer's
/// convention: the whole component becomes usable from a codebase that
/// compiles without exceptions, and the caller decides how a failure is
/// reported.
struct ParseError {
    /// 1-based. Zero when the failure is not attributable to a line.
    std::size_t line;
    /// The problem, with no "line N:" prefix -- `format()` composes that, so a
    /// caller embedding this in its own message is not forced to strip it.
    std::string message;

    ParseError();

    /// `line N: message`, the spelling the other implementations use.
    std::string format() const;
};

/// One `(section, key) = value` binding, in the order it was read.
struct Entry {
    /// Empty for a key written before any `[section]` header.
    std::string section;
    std::string key;
    Value value;
    /// 1-based line the binding was read from. Kept so a schema layer can
    /// point at the offending line for a problem the PARSER cannot see -- an
    /// out-of-range number, an unknown key -- which is most of what a schema
    /// rejects.
    std::size_t line;
    /// The value as written, blanks and comment removed, so a schema message
    /// can repeat the operator's text rather than a re-rendering of the parsed
    /// number -- a double has no rendering every implementation produces alike
    /// (L2-CLI-022).
    std::string written;

    Entry();
};

/// A parsed document: a flat list of bindings.
///
/// A list, not a map, and the order is the file's. Two callers need it:
/// unknown-key reporting wants to walk every binding as written, and a
/// diagnostic that cites line numbers wants them ascending.
class Document {
  public:
    Document();

    std::size_t size() const { return entries_.size(); }
    const Entry& at(std::size_t index) const { return entries_[index]; }

    /// Look up one binding. False when absent; `out` is untouched.
    bool get(const std::string& section, const std::string& key, Value& out) const;
    bool contains(const std::string& section, const std::string& key) const;
    /// The whole binding, for a caller that needs its line or its text.
    bool get_entry(const std::string& section, const std::string& key, Entry& out) const;

    /// Append. Used by the parser; exposed so a caller can build a document
    /// without parsing text, which is what makes the schema layer testable
    /// without a file.
    void add(const Entry& entry);

  private:
    std::vector<Entry> entries_;
};

/// Parse `text`. False on failure, with `error` filled and `out` unspecified.
///
/// Accepts, and nothing else:
///
///   * blank lines, and `# comment` to end of line (outside strings)
///   * `[section]` where section is a plain identifier
///   * `key = value` where key is a plain identifier
///   * values: `"string"`, integer, float, `true` / `false`, and a
///     single-line `[array]` of those
///
/// Rejects — each because full TOML accepts it and the flat schema cannot
/// model it, so storing it would silently drop a setting the operator wrote:
///
///   * `[[array of tables]]`, `[dotted.header]`, `[quoted "header"]`
///   * `dotted.key = ...`, `"quoted key" = ...`
///   * inline tables `{ a = 1 }`, multi-line arrays, nested arrays
///   * a section or key repeated (the TOML spec forbids both)
///   * numeric forms TOML does not allow that a native parse would take:
///     leading zeros (`08`), a bare trailing dot (`1.`), `0x`/`0o`/`0b`,
///     and digit separators (`1_000`)
bool parse(const std::string& text, Document& out, ParseError& error);

/// True when `name` is a plain identifier: one or more of `[A-Za-z0-9_]`.
///
/// No dash, and no other punctuation: this is exactly Python's `_IDENT_RE`
/// (`^\w+$` under `re.ASCII`) and Rust's `is_ascii_alphanumeric() || '_'`.
/// Widening it here would make a section or key name that one implementation
/// accepts and the others refuse.
///
/// Exposed because the schema layer validates names it did not get from this
/// parser -- a CLI `--set section.key=value` would want the same rule -- and
/// two spellings of "plain identifier" is exactly the kind of near-duplicate
/// that drifts.
bool is_plain_identifier(const std::string& name);

/// True when `literal` matches TOML's number grammar:
/// `[+-]? ( 0 | [1-9][0-9]* ) ( . [0-9]+ )? ( [eE] [+-]? [0-9]+ )?`
///
/// Exposed for the same reason as `is_plain_identifier`, and because it is the
/// single most divergence-prone rule in the grammar: every language's native
/// number parser is MORE permissive than TOML, so a parser that delegates to
/// one accepts `08` and `1.` and drifts from a strict implementation.
bool is_number_literal(const std::string& literal);

}  // namespace toml
}  // namespace mie

#endif  // MIE_TOML_HPP
