"""Hidden collection copies left behind by the retired dual-sync releases.

Releases up to 1.0.131 kept one hidden copy of the collection per service next
to `collection.anki2`. Nothing uses them any more; this module finds and
deletes exactly those files. It never touches the profile's own collection.
"""

from __future__ import annotations

import os
import shutil

_NAMES = ("kelma_shadow_kelmasync", "kelma_shadow_ankiweb")
_FILE_SUFFIXES = (
    ".anki2",
    ".anki2-wal",
    ".anki2-shm",
    ".anki2-journal",
    ".media.db2",
    ".media.db2-wal",
    ".media.db2-shm",
    ".media.db2-journal",
)
_DIR_SUFFIX = ".media"


def find(profile_dir: str) -> list[str]:
    """Paths of leftover copy files and media folders in `profile_dir`."""
    found: list[str] = []
    for name in _NAMES:
        for suffix in _FILE_SUFFIXES:
            path = os.path.join(profile_dir, name + suffix)
            if os.path.isfile(path) and not os.path.islink(path):
                found.append(path)
        folder = os.path.join(profile_dir, name + _DIR_SUFFIX)
        if os.path.isdir(folder) and not os.path.islink(folder):
            found.append(folder)
    return found


def total_bytes(paths: list[str]) -> int:
    total = 0
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for filename in files:
                    try:
                        total += os.path.getsize(os.path.join(root, filename))
                    except OSError:
                        pass
        else:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return total


def delete(paths: list[str]) -> list[str]:
    """Delete the given copy paths; return the ones that could not be removed."""
    failed: list[str] = []
    for path in paths:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError:
            failed.append(path)
    return failed
