"""Installed-package public surface (L3-PY-007 and L3-PY-003).

L3-PY-007 requires the ``aero1553`` package to expose its decoder entry
point as a typed callable importable from the package root; L3-PY-003
requires the ``aero1553`` console script to be registered via
``[project.scripts]``. These tests assert both directly — the root
re-export, that the entry point is a typed callable, that the public
surface is documented, and that the console-script entry point is
installed — so each requirement traces to a real package-surface check
rather than borrowing an unrelated parent's test. (The broader "all
public APIs carry type annotations" clause is enforced separately by the
CI-gated ``mypy src`` strict run.)
"""

from __future__ import annotations

import inspect
from importlib.metadata import entry_points

import pytest


@pytest.mark.requirement("L3-PY-007")
def test_decoder_entry_point_importable_from_package_root() -> None:
    """``from aero1553 import MieFileReader`` resolves to the reader."""
    import aero1553
    from aero1553 import MieFileReader
    from aero1553.reader import MieFileReader as ReaderModuleClass

    # Same object as the submodule definition — a genuine re-export, not a
    # shadowing stub.
    assert MieFileReader is ReaderModuleClass
    # Advertised in the package's public surface.
    assert "MieFileReader" in aero1553.__all__


@pytest.mark.requirement("L3-PY-007")
def test_decoder_entry_point_is_a_typed_callable() -> None:
    """``MieFileReader`` is callable and its constructor is fully typed."""
    from aero1553 import MieFileReader

    assert callable(MieFileReader)
    sig = inspect.signature(MieFileReader)
    assert sig.parameters, "entry point should take at least an input path"
    unannotated = [
        name for name, p in sig.parameters.items() if p.annotation is inspect.Parameter.empty
    ]
    assert not unannotated, f"untyped constructor parameters: {unannotated}"


@pytest.mark.requirement("L3-PY-007")
def test_message_type_importable_from_package_root() -> None:
    """The yielded record type is also importable from the root."""
    import aero1553
    from aero1553 import MieMessage
    from aero1553.models import MieMessage as ModelsMessage

    assert MieMessage is ModelsMessage
    assert "MieMessage" in aero1553.__all__


@pytest.mark.requirement("L3-PY-007")
def test_public_surface_is_documented() -> None:
    """Package and entry point carry docstrings (documented public API)."""
    import aero1553
    from aero1553 import MieFileReader

    assert aero1553.__doc__ is not None
    assert aero1553.__doc__.strip()
    assert MieFileReader.__doc__ is not None
    assert MieFileReader.__doc__.strip()


@pytest.mark.requirement("L3-PY-003")
def test_console_script_entry_point_registered() -> None:
    """The `aero1553` console script is registered (L3-PY-003).

    The conformance suite drives the CLI via ``python -m aero1553`` (the
    ``__main__`` shim), so the ``[project.scripts]`` console-script entry
    point is otherwise only exercised manually. This pins that it is
    installed and points at ``cli:main_cli`` — the process-entry wrapper,
    not the importable ``main`` — catching a packaging regression that the
    rest of the suite would miss.
    """
    scripts = entry_points(group="console_scripts")
    mie = [e for e in scripts if e.name == "aero1553"]
    assert mie, "aero1553 console script is not registered"
    assert mie[0].value == "aero1553.cli:main_cli"
