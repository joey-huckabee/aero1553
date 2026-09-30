# Aero1553 (Python)

The Python implementation of Aero1553: a decoder for DDC MIL-STD-1553 MIE
binary recording files, exposing both an `aero1553` CLI and an importable
`aero1553` package. Supports Python 3.10–3.14.

Shared documentation — the project overview, CLI reference, configuration
schema, supported message formats, error catalog, and vendor-CSV alignment —
lives at the [repository root](../README.md) and under [`docs/`](../docs/).

## Install

From a source checkout (editable install, run from the repository root):

```bash
pip install -e ./python
```

The package includes a compiled extension, `aero1553._native` -- a PyO3
binding over the Rust decoder in [`../rust`](../rust) -- so building from
source needs a Rust toolchain ([rustup](https://rustup.rs/)) as well as Python.

Or, for development, via [uv](https://docs.astral.sh/uv/) from the repository
root:

```bash
uv --directory python sync     # creates python/.venv, installs locked deps, builds + installs the package
```

`uv sync` installs the exact dependency versions recorded in `uv.lock` and
removes packages that are not part of the locked environment. The package is
installed editable; its Rust extension is rebuilt automatically the next time
you `uv run` after a change to any `.rs` file, in the binding or the core crate
(see `[tool.uv] cache-keys` in `pyproject.toml`).

## Library usage

```python
from aero1553 import MieFileReader

reader = MieFileReader("recording.mie")
for message in reader:
    print(message.timestamp, message.rt, message.msg_label)
```

`MieFileReader` and the `MieMessage` records it yields are importable directly
from the package root (`aero1553`).

The [Python library guide](../docs/PYTHON-GUIDE.md) covers the whole package:
filtering and merging, writing CSV, NumPy and pandas tables with
`aero1553.columns()`, dataclasses with `MieMessage.to_dict()`, dates with and
without a year, configuration, logging and errors. Every example in it is run
by the test suite.

## Development

```bash
uv --directory python run pytest        # test suite
uv --directory python run mypy src      # strict type check (CI-gated)
uv --directory python run aero1553 --help
uv --directory python build             # sdist + one abi3 wheel, via maturin
```

The build produces a single `cp310-abi3` wheel per platform, which installs on
every supported CPython from 3.10 up.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full development workflow.

## Package structure

```
python/
├── pyproject.toml      PEP 621 metadata, maturin build, PEP 735 dev group; pytest markers
├── uv.lock             pinned dependencies; committed
├── native/             PyO3 binding crate, built as aero1553._native
├── src/aero1553/       package source (mirrors the Rust module names)
└── tests/              pytest suite
```

## License

Apache-2.0 — see [LICENSE](../LICENSE).
