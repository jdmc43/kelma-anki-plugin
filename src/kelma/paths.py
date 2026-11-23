"""Filesystem locations used by the add-on."""

from __future__ import annotations

import os

from aqt import mw


def profile_dir() -> str:
    """The folder that holds the current profile's collection."""
    return os.path.dirname(mw.col.path)
