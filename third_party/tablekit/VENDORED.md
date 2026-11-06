# tablekit, vendored for an experiment

A snapshot of `joey-huckabee/tablekit` at commit `747c6d0` (PR #18: cell formats and
the row writer), so this experiment branch builds in CI without a credential for the
private repository. It is not for merging: once tablekit 0.7.0 is tagged, aero1553
depends on it by tag and this directory goes. `testdata/` and `.githooks/` are left out.

Only `tablekit` and `tablekit-csv` are kept, the workspace trimmed to them;
tablekit's documents, its Parquet and profile crates and
its lock file are left out, so the offline bundle and maturin's source package do not
reach for crates aero1553 does not use.
