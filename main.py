"""NAIA EXten official entry point for naia_ext_api=1."""

from __future__ import annotations

import sys
from pathlib import Path

# NAIA loads main.py with importlib.spec_from_file_location().
# Explicitly expose this extension directory so sibling packages can be imported.
_EXT_ROOT = Path(__file__).resolve().parent
_EXT_ROOT_TEXT = str(_EXT_ROOT)
if _EXT_ROOT_TEXT not in sys.path:
    sys.path.insert(0, _EXT_ROOT_TEXT)

from naia_exten.extension import NAIAExten


_runtime = None


def register(ctx):
    """Required by NAIA Custom Extension API v1."""
    global _runtime
    _runtime = NAIAExten(ctx)
    _runtime.register()
