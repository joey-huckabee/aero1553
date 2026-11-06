# tablekit

See the [project crate and tool map](docs/PROJECT-MAP.md) for diagrams, ownership,
overlap with the other repositories, and planned improvements with their prerequisites.

SQL and Parquet type mappings now share an explicit target profile:
[`profiles/cloudera-impala-hive-v1.toml`](profiles/cloudera-impala-hive-v1.toml).
See [docs/PROFILES.md](docs/PROFILES.md) for loading the same profile in both generators.

Writes tables to CSV and Parquet in one format, for every FATMOPs tool. A tool builds a table
from its own records; tablekit writes it. So every CSV looks like what pandas writes, every
Parquet file has the types Impala reads, and no tool's output drifts from another's.

| Crate | What it does |
|---|---|
| `tablekit` | The table model: named, typed columns, missing values, batches; writing a file whole or not at all |
| `tablekit-csv` | CSV, as pandas writes it by default ([docs/CSV-FORMAT.md](docs/CSV-FORMAT.md)) |
| `tablekit-parquet` | Parquet, with Impala's types ([docs/PARQUET.md](docs/PARQUET.md)) |
| `tablekit-profile` | Validated TOML shared by SQL generation and Parquet writing: types, SQL comments and schema policy ([docs/PROFILES.md](docs/PROFILES.md)) |

| Document | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Why one library, where it sits, how an export runs, where concurrency will go, the decisions |
| [docs/CSV-FORMAT.md](docs/CSV-FORMAT.md) | Every rule of the CSV output, its options, and where it differs from pandas |
| [docs/PARQUET.md](docs/PARQUET.md) | The Parquet types and file layout, and column names for Impala |
| [ROADMAP.md](ROADMAP.md) | What comes next |
| [AGENTS.md](AGENTS.md) | The rules for working in this repository |
| `cargo doc --open` | The API, with runnable examples |

## Use

Add the crates you need, pinned to a commit:

```toml
[dependencies]
tablekit = { git = "https://github.com/joey-huckabee/tablekit", rev = "COMMIT" }
tablekit-csv = { git = "https://github.com/joey-huckabee/tablekit", rev = "COMMIT" }
tablekit-parquet = { git = "https://github.com/joey-huckabee/tablekit", rev = "COMMIT" }
```

The repository is private: the build fetches it through `git`, so put
`[net] git-fetch-with-cli = true` in a `.cargo/config.toml` at the root of the repository
that depends on it.

```rust
use tablekit::{Batch, Column, Values};

let batch = Batch::new(vec![
    Column::new("time", Values::F64(vec![0.5, 1.0])),
    Column::with_missing("Speed", Values::U16(vec![120, 0]), vec![true, false])?,
    Column::new("Valid", Values::Bool(vec![true, false])),
])?;
tablekit_csv::write_file("out.csv".as_ref(), &batch, &tablekit_csv::CsvOptions::default())?;
tablekit_parquet::write_file("out.parquet".as_ref(), &batch, &tablekit_parquet::ParquetOptions::default())?;
```

Every call returns `Result<_, tablekit::TableError>`.

For a table too large to hold at once, implement `tablekit::Source` (a batch at a time) and
write it with `tablekit_csv::write_source_file` or `tablekit_parquet::write_source_file`; or
drive `CsvWriter` and `ParquetWriter` yourself. A tool that writes CSV as it reads each
record uses `tablekit_csv::CsvFormat`, a row at a time into its own buffer; one whose columns
are fixed in its code can write each row a cell at a time with `CsvFormat::begin_row`
(`RowWriter`), at the speed of a writer hand-written for those columns. A column can be
written in hexadecimal or with fixed decimals (`CsvOptions::cell_formats`,
[docs/CSV-FORMAT.md](docs/CSV-FORMAT.md#cell-formats)).

Every change that touches the writing path is measured against what it replaces, A against
B on the same input, byte-identical output, wall time and callgrind instruction counts; a
change that is slower is not merged. CHANGELOG.md records the figures.

## Build and test

Rust 1.85 or later. There is no CI; run these before pushing:

```bash
git config core.hooksPath .githooks       # once per clone
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps
cargo test --workspace --release
cargo +1.85 build --workspace --all-targets
```

The expected files come from pandas and NumPy themselves. To regenerate them (pandas 3,
NumPy 2), run `python make.py` in `testdata/expected/csv/` and in `testdata/floats/`.

Before a release, read a Parquet file tablekit wrote with pyarrow
(`pandas.read_parquet`), as an independent check of the format.
