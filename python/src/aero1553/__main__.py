"""Entry point for ``python -m aero1553``.

Delegates all CLI logic to :mod:`aero1553.cli`.
"""

from __future__ import annotations

import sys

from aero1553.cli import main_cli

if __name__ == "__main__":
    sys.exit(main_cli())
