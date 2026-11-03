"""Per-deck storage breakdown + removing a deck's data from a cloud's shadow.

`deck_breakdown` attributes cards, notes and media bytes to each deck (media by
the files its notes reference — extracted with our own regex to avoid Anki's
files_in_str LaTeX-rendering side effect). `delete_from_cloud` removes a deck's
cards/notes from a service's shadow so the deletion uploads on the next sync; the
master collection is never touched.
"""

from __future__ import annotations

import os
import re

from anki.collection import Collection
from aqt import mw

from . import paths

_IMG_RE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']?([^"'>\s]+)""", re.I)
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_REMOTE_RE = re.compile(r"(https?|ftp)://", re.I)


def _media_refs(flds: str) -> list[str]:
    refs = [m.group(1) for m in _IMG_RE.finditer(flds)]
    refs += [m.group(1) for m in _SOUND_RE.finditer(flds)]
    return [f for f in refs if not _REMOTE_RE.match(f)]


def deck_breakdown(col: Collection) -> dict:
    """Return {"rows": [{name, cards, notes, media_bytes}], "total_media": bytes}.

    Rows are sorted by media size descending. Per-deck media may double-count
    files shared between decks; `total_media` is the unique total.
    """
    per_did: dict[int, dict] = {}
    for did, nid in col.db.all("select did, nid from cards"):
        entry = per_did.setdefault(did, {"cards": 0, "nids": set()})
        entry["cards"] += 1
        entry["nids"].add(nid)

    note_files: dict[int, list[str]] = {}
    for nid, flds in col.db.all("select id, flds from notes"):
        refs = _media_refs(flds)
        if refs:
            note_files[nid] = refs

    media_dir = col.media.dir()
    size_cache: dict[str, int] = {}

    def fsize(fname: str) -> int:
        s = size_cache.get(fname)
        if s is None:
            try:
                s = os.path.getsize(os.path.join(media_dir, fname))
            except OSError:
                s = 0
            size_cache[fname] = s
        return s

    did_name = {d.id: d.name for d in col.decks.all_names_and_ids()}
    all_files: set[str] = set()
    out = []
    for did, entry in per_did.items():
        files: set[str] = set()
        for nid in entry["nids"]:
            files.update(note_files.get(nid, ()))
        all_files.update(files)
        out.append(
            {
                "name": did_name.get(did, str(did)),
                "cards": entry["cards"],
                "notes": len(entry["nids"]),
                "media_bytes": sum(fsize(f) for f in files),
            }
        )
    out.sort(key=lambda r: r["media_bytes"], reverse=True)
    return {"rows": out, "total_media": sum(fsize(f) for f in all_files)}


def delete_from_cloud(service: str, deck_names: list[str]) -> int:
    """Remove the given decks (and subdecks) from a service's shadow, then purge
    any now-orphaned media so on-disk size drops immediately. The native sync
    uploads both the note/card and media deletions on the next sync. Returns
    cards removed. The master collection is never touched."""
    if not paths.shadow_exists(service):
        return 0
    shadow = Collection(paths.shadow_path(service))
    removed = 0
    try:
        for name in deck_names:
            cids = list(shadow.find_cards(f'deck:"{name}"'))
            if cids:
                shadow.remove_cards_and_orphaned_notes(cids)
                removed += len(cids)
            did = shadow.decks.id_for_name(name)
            if did is not None:
                try:
                    shadow.decks.remove([did])
                except Exception:  # noqa: BLE001 - deck may be gone already
                    pass
        # Reclaim media no longer referenced by any remaining note.
        try:
            unused = list(shadow.media.check().unused)
            if unused:
                shadow.media.trash_files(unused)
            shadow.media.empty_trash()
        except Exception:  # noqa: BLE001 - media GC is best-effort
            pass
    finally:
        shadow.close()
    return removed
