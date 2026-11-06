//! Explicit, validated SQL and Parquet schemas loaded before any records are written.
//!
//! The bundled profile is data in `profiles/cloudera-impala-hive-v1.toml`. Both the SQL
//! generator and the Parquet writer consume this contract; no values are sampled for types.
//! Profile parsing and validation happen once, outside the writing loops.
//!
//! ```
//! use tablekit::{DataType};
//! use tablekit_profile::TargetProfile;
//! let profile = TargetProfile::builtin()?;
//! assert_eq!(profile.mapping(DataType::U64).sql, "DECIMAL(20,0)");
//! # Ok::<(), tablekit_profile::ProfileError>(())
//! ```

#![forbid(unsafe_code)]

use std::collections::{BTreeMap, HashSet};
use std::fmt;
use std::path::Path;
use std::sync::OnceLock;

use serde::Deserialize;
use tablekit::DataType;

include!(concat!(env!("OUT_DIR"), "/compatibility.rs"));

/// The editable source of the default profile, also useful for making a custom profile.
pub const BUILTIN_TOML: &str = include_str!("../../../profiles/cloudera-impala-hive-v1.toml");

/// A profile that could not be read, or would make an unsupported or lossy schema.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProfileError(pub String);

impl fmt::Display for ProfileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

impl std::error::Error for ProfileError {}

/// Explicit SQL type and Parquet schema for one source type. No field is inferred.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TypeMapping {
    /// SQL type spelling.
    pub sql: String,
    /// Parquet primitive type spelling.
    pub physical: String,
    /// Parquet annotation, including the signed width for integers.
    pub logical: String,
    /// Fixed byte array length, required only for decimals.
    pub length: Option<i32>,
    /// Decimal precision.
    pub precision: Option<i32>,
    /// Decimal scale.
    pub scale: Option<i32>,
}

/// An application-supplied record or partition column.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SqlColumn {
    /// Output identifier.
    pub name: String,
    /// Canonical source type, resolved through the profile mappings.
    pub data_type: String,
    /// Literal SQL column comment.
    pub comment: String,
}

/// Templates supplied by the profile, with named placeholders.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommentTemplates {
    /// Primitive field.
    pub field: String,
    /// Character array.
    pub text: String,
    /// Enum name.
    pub enum_name: String,
    /// Enum name where a number has aliases.
    pub enum_alias: String,
    /// Enum numeric code.
    pub enum_code: String,
}

/// SQL schema and presentation policy. Templates are opaque to the table writer.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SqlProfile {
    /// Prefix for the database derived from the source version.
    pub database_prefix: String,
    /// Separator for flattened paths.
    pub path_separator: String,
    /// Maximum identifier length.
    pub max_identifier: usize,
    /// Text used when no source version is given.
    pub missing_version: String,
    /// SQL file header template.
    pub header: String,
    /// Table comment template.
    pub table_comment: String,
    /// Field comment templates.
    pub comments: CommentTemplates,
    /// Extra columns, before the source fields.
    pub record_columns: Vec<SqlColumn>,
    /// Partition columns, in order.
    pub partition_columns: Vec<SqlColumn>,
}

/// A fully validated target profile. Construct through [`Self::parse`] or [`Self::load`].
#[derive(Clone, Debug)]
pub struct TargetProfile {
    schema_version: u32,
    id: String,
    mappings: BTreeMap<String, TypeMapping>,
    sql: SqlProfile,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawProfile {
    schema_version: u32,
    id: String,
    mappings: BTreeMap<String, TypeMapping>,
    sql: SqlProfile,
}

const TYPES: [DataType; 12] = [
    DataType::Bool,
    DataType::I8,
    DataType::I16,
    DataType::I32,
    DataType::I64,
    DataType::U8,
    DataType::U16,
    DataType::U32,
    DataType::U64,
    DataType::F32,
    DataType::F64,
    DataType::Text,
];

/// Canonical profile key for a tablekit source type.
#[must_use]
pub fn type_key(data_type: DataType) -> &'static str {
    match data_type {
        DataType::Bool => "bool",
        DataType::I8 => "int8",
        DataType::I16 => "int16",
        DataType::I32 => "int32",
        DataType::I64 => "int64",
        DataType::U8 => "uint8",
        DataType::U16 => "uint16",
        DataType::U32 => "uint32",
        DataType::U64 => "uint64",
        DataType::F32 => "float32",
        DataType::F64 => "float64",
        DataType::Text => "text",
    }
}

impl TargetProfile {
    /// Reads and validates TOML. Missing entries, unknown settings, unsupported conversions,
    /// mismatched SQL/Parquet types and unknown template placeholders are errors.
    ///
    /// # Errors
    /// Returns a diagnostic identifying the invalid profile entry.
    pub fn parse(text: &str) -> Result<Self, ProfileError> {
        let raw: RawProfile = toml::from_str(text).map_err(|e| ProfileError(e.to_string()))?;
        let profile = Self {
            schema_version: raw.schema_version,
            id: raw.id,
            mappings: raw.mappings,
            sql: raw.sql,
        };
        profile.validate()?;
        Ok(profile)
    }

    /// Reads and validates a profile from disk.
    ///
    /// # Errors
    /// The file cannot be read or is not a supported profile.
    pub fn load(path: &Path) -> Result<Self, ProfileError> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| ProfileError(format!("{}: {e}", path.display())))?;
        Self::parse(&text).map_err(|e| ProfileError(format!("{}: {e}", path.display())))
    }

    /// The bundled profile, parsed and validated once per process.
    ///
    /// # Errors
    /// The bundled profile is invalid (a packaging error).
    pub fn builtin() -> Result<&'static Self, ProfileError> {
        static PROFILE: OnceLock<Result<TargetProfile, ProfileError>> = OnceLock::new();
        PROFILE
            .get_or_init(|| Self::parse(BUILTIN_TOML))
            .as_ref()
            .map_err(Clone::clone)
    }

    /// Stable profile identity, which should be versioned when its output changes.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    /// SQL policy from this profile.
    #[must_use]
    pub fn sql(&self) -> &SqlProfile {
        &self.sql
    }

    /// Projects configured partition columns into caller order, preserving their types/comments.
    /// An empty list selects an unpartitioned table. The bundled profile keeps all six.
    ///
    /// # Errors
    /// A selected name is absent from this profile or repeated.
    pub fn select_partitions(&self, names: &[String]) -> Result<Self, ProfileError> {
        let mut selected = self.clone();
        selected.sql.partition_columns = names
            .iter()
            .map(|name| {
                self.sql
                    .partition_columns
                    .iter()
                    .find(|c| &c.name == name)
                    .cloned()
                    .ok_or_else(|| {
                        ProfileError(format!(
                            "partition {name:?} is not configured in profile {}",
                            self.id
                        ))
                    })
            })
            .collect::<Result<_, _>>()?;
        selected.validate()?;
        Ok(selected)
    }

    /// Resolved mapping. Completeness is checked at construction, with no fallback mapping.
    #[must_use]
    pub fn mapping(&self, data_type: DataType) -> &TypeMapping {
        // A validated profile has exactly one entry for every DataType. Deserialization is
        // private to parse(), and callers cannot mutate the map after validation.
        &self.mappings[type_key(data_type)]
    }

    /// Resolves a canonical source key used by a configured extra column.
    ///
    /// # Errors
    /// The key is unknown.
    pub fn named_mapping(&self, key: &str) -> Result<&TypeMapping, ProfileError> {
        self.mappings
            .get(key)
            .ok_or_else(|| ProfileError(format!("unknown source type {key:?}")))
    }

    fn validate(&self) -> Result<(), ProfileError> {
        if self.schema_version != 1 || self.id.trim().is_empty() {
            return Err(ProfileError(
                "expected schema_version = 1 and a nonempty id".into(),
            ));
        }
        if self.mappings.len() != TYPES.len() {
            return Err(ProfileError(
                "mappings must contain exactly the 12 canonical source types".into(),
            ));
        }
        for data_type in TYPES {
            let key = type_key(data_type);
            let m = self.named_mapping(key)?;
            validate_mapping(data_type, m)
                .map_err(|e| ProfileError(format!("mappings.{key}: {e}")))?;
        }
        let mut names = HashSet::new();
        for column in self
            .sql
            .record_columns
            .iter()
            .chain(&self.sql.partition_columns)
        {
            self.named_mapping(&column.data_type)?;
            let name = &column.name;
            if !valid_identifier(name)
                || name.len() > self.sql.max_identifier
                || !names.insert(name.to_ascii_lowercase())
            {
                return Err(ProfileError(format!(
                    "invalid or duplicate configured column {name:?}"
                )));
            }
        }
        if self.sql.max_identifier == 0
            || self.sql.max_identifier > 128
            || self.sql.path_separator != "_"
            || self.sql.database_prefix != "cmm_"
        {
            return Err(ProfileError("this version requires path_separator = '_' and database_prefix = 'cmm_', and max_identifier in 1..=128".into()));
        }
        // Record columns remain fixed. Partitions may select/reorder the known capture fields;
        // consumers use this validated order for both directories and SQL declarations.
        let expected_records = BUILTIN_RECORD_COLUMNS.iter().map(|c| c.0);
        if !self
            .sql
            .record_columns
            .iter()
            .map(|c| c.name.as_str())
            .eq(expected_records)
            || self.sql.partition_columns.iter().any(|c| {
                !BUILTIN_PARTITION_COLUMNS
                    .iter()
                    .any(|known| known.0 == c.name)
            })
            || self
                .sql
                .record_columns
                .iter()
                .any(|c| c.data_type != "float64")
            || self
                .sql
                .partition_columns
                .iter()
                .any(|c| c.data_type != "text")
        {
            return Err(ProfileError(
                "record layout must remain fixed; partition columns must be known capture fields of type text"
                    .into(),
            ));
        }
        validate_template(&self.sql.header, &["package", "version"])?;
        validate_template(
            &self.sql.table_comment,
            &["label", "message_name", "qualified_type", "size"],
        )?;
        for text in [
            &self.sql.comments.field,
            &self.sql.comments.text,
            &self.sql.comments.enum_name,
            &self.sql.comments.enum_alias,
            &self.sql.comments.enum_code,
        ] {
            validate_template(text, &["source_path", "xml_type", "count"])?;
        }
        Ok(())
    }
}

fn valid_identifier(name: &str) -> bool {
    !name.is_empty()
        && !name.as_bytes()[0].is_ascii_digit()
        && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
}

// Only lossless conversions supported by the column encoders may pass. SQL spelling and
// Parquet schema are checked together. These are encoder capabilities, not default policy.
fn validate_mapping(source: DataType, m: &TypeMapping) -> Result<(), String> {
    let (physical, logical) = match m.sql.as_str() {
        "BOOLEAN" => ("BOOLEAN", "NONE"),
        "TINYINT" => ("INT32", "INTEGER(8,signed)"),
        "SMALLINT" => ("INT32", "INTEGER(16,signed)"),
        "INT" => ("INT32", "INTEGER(32,signed)"),
        "BIGINT" => ("INT64", "INTEGER(64,signed)"),
        "DECIMAL(20,0)" => ("FIXED_LEN_BYTE_ARRAY", "DECIMAL"),
        "FLOAT" => ("FLOAT", "NONE"),
        "DOUBLE" => ("DOUBLE", "NONE"),
        "STRING" => ("BYTE_ARRAY", "STRING"),
        other => return Err(format!("unsupported SQL type {other:?}")),
    };
    if m.physical != physical || m.logical != logical {
        return Err("SQL type disagrees with the explicit Parquet physical/logical type".into());
    }
    let supported = match source {
        DataType::Bool => m.sql == "BOOLEAN",
        DataType::I8 => matches!(m.sql.as_str(), "TINYINT" | "SMALLINT" | "INT"),
        DataType::I16 | DataType::U8 => matches!(m.sql.as_str(), "SMALLINT" | "INT"),
        DataType::I32 | DataType::U16 => m.sql == "INT",
        DataType::I64 | DataType::U32 => m.sql == "BIGINT",
        DataType::U64 => m.sql == "DECIMAL(20,0)",
        DataType::F32 => m.sql == "FLOAT",
        DataType::F64 => m.sql == "DOUBLE",
        DataType::Text => m.sql == "STRING",
    };
    if !supported {
        return Err("lossy or unsupported conversion from the source type".into());
    }
    if source == DataType::U64 {
        if (m.length, m.precision, m.scale) != (Some(9), Some(20), Some(0)) {
            return Err("uint64 requires length = 9, precision = 20, scale = 0".into());
        }
    } else if m.length.is_some() || m.precision.is_some() || m.scale.is_some() {
        return Err("length, precision and scale are only supported for uint64 decimal".into());
    }
    Ok(())
}

fn validate_template(template: &str, keys: &[&str]) -> Result<(), ProfileError> {
    let values: Vec<_> = keys.iter().map(|&key| (key, "")).collect();
    render(template, &values).map(|_| ())
}

/// Substitutes named placeholders once. Inserted text is never interpreted as a template.
///
/// # Errors
/// A brace is unmatched or a placeholder has no supplied value.
pub fn render(template: &str, values: &[(&str, &str)]) -> Result<String, ProfileError> {
    let mut out = String::with_capacity(template.len());
    render_into(template, values, &mut out)?;
    Ok(out)
}

/// Appends substitutions to a reusable string. Inserted values are never reparsed.
///
/// ```
/// let mut out = String::from("prefix: ");
/// tablekit_profile::render_into("{name}", &[("name", "value")], &mut out)?;
/// assert_eq!(out, "prefix: value");
/// # Ok::<(), tablekit_profile::ProfileError>(())
/// ```
///
/// # Errors
/// Same template errors as [`render`]. On error an appended prefix may remain in `out`;
/// discard failed output or clear a scratch buffer before reusing it.
pub fn render_into(
    template: &str,
    values: &[(&str, &str)],
    out: &mut String,
) -> Result<(), ProfileError> {
    // HOT PATH: append to caller-owned capacity rather than allocate per value/comment.
    let mut rest = template;
    while let Some(start) = rest.find(['{', '}']) {
        out.push_str(&rest[..start]);
        if rest.as_bytes()[start] == b'}' {
            return Err(ProfileError("unmatched closing brace in template".into()));
        }
        let after = &rest[start + 1..];
        let end = after
            .find('}')
            .ok_or_else(|| ProfileError("unclosed template placeholder".into()))?;
        let key = &after[..end];
        let value = values
            .iter()
            .find(|(name, _)| *name == key)
            .ok_or_else(|| ProfileError(format!("unknown template placeholder {{{key}}}")))?;
        out.push_str(value.1);
        rest = &after[end + 1..];
    }
    out.push_str(rest);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundled_profile_and_generated_constants_agree() {
        let p = TargetProfile::builtin().unwrap();
        assert_eq!(p.id(), "cloudera-impala-hive-v1");
        assert_eq!(p.mapping(DataType::U64).sql, BUILTIN_UINT64_TYPE);
        for (columns, expected) in [
            (&p.sql.record_columns, BUILTIN_RECORD_COLUMNS),
            (&p.sql.partition_columns, BUILTIN_PARTITION_COLUMNS),
        ] {
            for (column, &(name, sql, comment)) in columns.iter().zip(expected) {
                assert_eq!(
                    (
                        &*column.name,
                        &*p.named_mapping(&column.data_type).unwrap().sql,
                        &*column.comment
                    ),
                    (name, sql, comment)
                );
            }
        }
    }

    #[test]
    fn invalid_profiles_never_fall_back() {
        for text in [
            BUILTIN_TOML.replace("schema_version = 1", "schema_version = 2"),
            BUILTIN_TOML.replace("precision = 20", "precision = 19"),
            BUILTIN_TOML.replace("[mappings.uint8]", "[mappings.typo]"),
            BUILTIN_TOML.replace("sql = \"DECIMAL(20,0)\"", "sql = \"BIGINT\""),
            BUILTIN_TOML.replace("{xml_type}", "{misspelled_type}"),
            BUILTIN_TOML.replace("max_identifier = 128", "max_identifier = 0"),
            BUILTIN_TOML.replace("length = 9", "length = 8"),
            BUILTIN_TOML.replace(
                "schema_version = 1",
                "schema_version = 1\nunknown_setting = true",
            ),
        ] {
            assert!(TargetProfile::parse(&text).is_err(), "{text}");
        }
    }

    #[test]
    fn substituted_values_are_literal() {
        assert_eq!(
            render(
                "{source_path}: {xml_type}",
                &[("source_path", "{xml_type}"), ("xml_type", "bool")]
            )
            .unwrap(),
            "{xml_type}: bool"
        );
    }

    #[test]
    fn partitions_select_order_and_profile_comments_without_changing_types() {
        let profile = TargetProfile::builtin().unwrap();
        let selected = profile
            .select_partitions(&["tail_name".into(), "venue".into()])
            .unwrap();
        assert_eq!(
            selected
                .sql
                .partition_columns
                .iter()
                .map(|c| c.name.as_str())
                .collect::<Vec<_>>(),
            ["tail_name", "venue"]
        );
        assert_eq!(
            selected.sql.partition_columns[0].comment,
            profile.sql.partition_columns[4].comment
        );
        assert_eq!(
            selected.mapping(DataType::U64).sql,
            profile.mapping(DataType::U64).sql
        );
        assert_eq!(
            profile
                .select_partitions(&[])
                .unwrap()
                .sql
                .partition_columns
                .len(),
            0
        );
        assert!(
            profile
                .select_partitions(&["venue".into(), "venue".into()])
                .is_err()
        );
        assert!(profile.select_partitions(&["unknown".into()]).is_err());
        let start = BUILTIN_TOML.find("[[sql.partition_columns]]").unwrap();
        let end = BUILTIN_TOML.find("[mappings.bool]").unwrap();
        let unpartitioned = format!("{}{}", &BUILTIN_TOML[..start], &BUILTIN_TOML[end..]).replace(
            "max_identifier = 128",
            "max_identifier = 128\npartition_columns = []",
        );
        assert_eq!(
            TargetProfile::parse(&unpartitioned)
                .unwrap()
                .sql
                .partition_columns
                .len(),
            0
        );
    }
}

#[cfg(test)]
mod reusable_render_tests {
    use super::*;
    fn legacy_render(template: &str, values: &[(&str, &str)]) -> Result<String, ProfileError> {
        let mut out = String::with_capacity(template.len());
        let mut rest = template;
        while let Some(start) = rest.find(['{', '}']) {
            out.push_str(&rest[..start]);
            if rest.as_bytes()[start] == b'}' {
                return Err(ProfileError("unmatched closing brace in template".into()));
            }
            let after = &rest[start + 1..];
            let end = after
                .find('}')
                .ok_or_else(|| ProfileError("unclosed template placeholder".into()))?;
            let key = &after[..end];
            let value = values
                .iter()
                .find(|(name, _)| *name == key)
                .ok_or_else(|| ProfileError(format!("unknown template placeholder {{{key}}}")))?;
            out.push_str(value.1);
            rest = &after[end + 1..];
        }
        out.push_str(rest);
        Ok(out)
    }
    // Frozen 0.6.0 parser above is independent of render_into.
    #[test]
    fn reusable_renderer_matches_frozen_values_errors_and_reuse() {
        let cases = [
            "",
            "plain é😀",
            "{x}",
            "{x}{x}",
            "{}",
            "{é}",
            "{x{y}",
            "{bad}{x}",
            "pre{x}{bad}",
            "}",
            "{",
            "{x",
            "{x}}",
            "{{x}",
            "\r\n'{x}\\{y}",
        ];
        let values = [
            ("x", "é'\\\r\n{not_reparsed}"),
            ("x", "ignored duplicate"),
            ("y", ""),
            ("", "empty key"),
            ("é", "Unicode key"),
            ("x{y", "brace in key"),
        ];
        let mut scratch = String::new();
        for template in cases {
            let expected = legacy_render(template, &values);
            let actual = render(template, &values);
            assert_eq!(
                actual.as_ref().map_err(ToString::to_string),
                expected.as_ref().map_err(ToString::to_string)
            );
            scratch.clear();
            let result = render_into(template, &values, &mut scratch);
            match expected {
                Ok(text) => {
                    assert!(result.is_ok());
                    assert_eq!(scratch, text);
                }
                Err(error) => assert_eq!(result.unwrap_err().to_string(), error.to_string()),
            }
        }
        scratch.clear();
        scratch.push_str("existing:");
        render_into("{x}", &values, &mut scratch).unwrap();
        assert_eq!(scratch, format!("existing:{}", values[0].1));
        scratch.clear();
        scratch.push_str("existing:");
        assert!(render_into("pre{x}{bad}", &values, &mut scratch).is_err());
        assert_eq!(scratch, format!("existing:pre{}", values[0].1));
    }
    #[test]
    fn large_then_short_and_empty_values_do_not_leave_stale_text() {
        let mut out = String::new();
        for value in ["é😀'\\\r\n{x}".repeat(8192), "tiny".into(), String::new()] {
            for template in ["{x}", "literal", "{x}{x}", ""] {
                out.clear();
                render_into(template, &[("x", &value)], &mut out).unwrap();
                assert_eq!(out, legacy_render(template, &[("x", &value)]).unwrap());
            }
        }
    }
}
