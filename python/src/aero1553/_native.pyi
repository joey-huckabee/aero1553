"""Type stub for ``aero1553._native``, the compiled PyO3 extension.

Built from ``python/native/src/lib.rs``. Keep this file in step with the
``#[pyfunction]`` / ``m.add`` calls there: mypy trusts it, and nothing else
checks it against the binary.
"""

__version__: str
TERMINATOR_TYPE_WORD: int

# Run the aero1553 command line; argv[0] is the program name. Returns the
# L2-CLI-011 exit status.
def run_cli(argv: list[str]) -> int: ...
