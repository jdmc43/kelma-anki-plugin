"""Filesystem locations for the per-service shadow collections."""

from __future__ import annotations

import os

from aqt import mw

from . import consts


def profile_dir() -> str:
    """The folder that holds the master collection (per-profile)."""
    return os.path.dirname(mw.col.path)


def shadow_path(service: str) -> str:
    return os.path.join(profile_dir(), consts.SHADOW_FILENAME[service])


def shadow_exists(service: str) -> bool:
    return os.path.exists(shadow_path(service))
