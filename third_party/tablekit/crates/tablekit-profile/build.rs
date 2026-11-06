//! Generates backwards-compatible constants from the bundled profile.
use std::fmt::Write as _;
use std::{env, fs, path::PathBuf};

// Compatibility constants are generated from the same profile, never maintained by hand.
fn main() {
    let source = "../../profiles/cloudera-impala-hive-v1.toml";
    println!("cargo:rerun-if-changed={source}");
    let text = fs::read_to_string(source).expect("read bundled target profile");
    let profile: toml::Value = text.parse().expect("parse bundled target profile");
    let mut generated = String::new();
    for (key, name) in [
        ("record_columns", "BUILTIN_RECORD_COLUMNS"),
        ("partition_columns", "BUILTIN_PARTITION_COLUMNS"),
    ] {
        let _ = write!(
            generated,
            "/// Compatibility view of the bundled profile.\npub const {name}: &[(&str, &str, &str)] = &[\n"
        );
        for column in profile["sql"][key].as_array().expect("column list") {
            let data_type = column["data_type"].as_str().expect("source type");
            let _ = writeln!(
                generated,
                "({:?}, {:?}, {:?}),",
                column["name"].as_str().expect("column name"),
                profile["mappings"][data_type]["sql"]
                    .as_str()
                    .expect("SQL type"),
                column["comment"].as_str().expect("column comment"),
            );
        }
        generated.push_str("];\n");
    }
    let _ = write!(
        generated,
        "/// Bundled unsigned 64-bit SQL type.\npub const BUILTIN_UINT64_TYPE: &str = {:?};\n\
         /// Bundled identifier limit.\npub const BUILTIN_MAX_IDENTIFIER: usize = {};\n",
        profile["mappings"]["uint64"]["sql"]
            .as_str()
            .expect("uint64 SQL type"),
        profile["sql"]["max_identifier"]
            .as_integer()
            .expect("identifier limit"),
    );
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR"));
    fs::write(out.join("compatibility.rs"), generated).expect("write compatibility constants");
}
