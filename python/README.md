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

Or, for development, via Poetry from the repository root:

```bash
poetry -C python sync     # creates the venv, installs locked deps + the package
```

`poetry sync` installs the exact dependency versions recorded in `poetry.lock`
and removes packages that are not part of the locked environment.

## Library usage

```python
from aero1553 import MieFileReader

reader = MieFileReader("recording.mie")
for message in reader:
    print(message.timestamp, message.rt, message.msg_label)
```

`MieFileReader` and the `MieMessage` records it yields are importable directly
from the package root (`aero1553`).

## Development

```bash
poetry -C python run pytest        # test suite
poetry -C python run mypy src      # strict type check (CI-gated)
poetry -C python run aero1553 --help
poetry -P python build             # wheel + sdist — note the -P (see below)
```

> **Why `-P` for the build, not `-C` like everything else?** `-P` (`--project`,
> requires Poetry ≥ 2.0) is used only for `build`: `poetry -C python build`
> doubles the source path on Windows (looks for `…/python/src/src/…`) and the
> wheel build fails, while `-P python build` produces both the sdist and the
> wheel cleanly on every platform. Every other command uses `-C python …`.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full development workflow.

## Package structure

```
python/
├── pyproject.toml      Poetry + PEP 621 hybrid; pytest markers registered here
├── poetry.lock         pinned dependencies; committed
├── src/aero1553/    package source (mirrors the Rust module names)
└── tests/              pytest suite
```

## License

Apache-2.0 — see [LICENSE](../LICENSE).
