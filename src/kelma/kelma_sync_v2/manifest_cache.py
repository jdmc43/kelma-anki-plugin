"""A local copy of the server manifest, kept current with `since` deltas.

A full manifest lists every note, card, review and media file; the server needs
seconds to build it. After each sync we keep the last full manifest next to the
collection, and the next sync asks only for what changed since its
`server_time` (rows by `modified_at`, deletions as tombstones) and merges that
in. The merged result is the same as a fresh full manifest; a full refresh
still happens at least daily and whenever the cache does not fit the account.
"""
from __future__ import annotations

from datetime import datetime
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

CACHE_VERSION = 1
FULL_REFRESH_SECONDS = 24 * 3600
_FILENAME = "kelma_server_manifest.json.gz"

_KEY_OF: dict[str, Callable[[dict[str, Any]], str]] = {
    "notes": lambda row: str(row.get("guid", "")),
    "cards": lambda row: str(row.get("card_id", "")),
    "notetypes": lambda row: str(row.get("notetype_id", "")),
    "decks": lambda row: str(row.get("name", "")),
    "media": lambda row: str(row.get("filename", "")),
    "reviews": lambda row: str(row.get("review_id", "")),
    "review_retractions": lambda row: str(row.get("review_id", "")),
    "study_days": lambda row: f"{row.get('day', '')}\x00{row.get('deck_name', '')}",
    "tombstones": lambda row: str(
        row.get("id") or f"{row.get('type', '')}\x00{row.get('resource_id', '')}\x00{row.get('deleted_at', '')}"
    ),
}
_TOMBSTONE_RESOURCE = {
    "note": "notes",
    "card": "cards",
    "notetype": "notetypes",
    "deck": "decks",
    "media": "media",
}


def _millis(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Apply a `since` manifest delta to a full manifest."""
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    for resource, key_of in _KEY_OF.items():
        current = {key_of(row): row for row in base.get(resource) or []}
        for row in delta.get(resource) or []:
            current[key_of(row)] = row
        rows[resource] = current
    delta_keys = {
        resource: {_KEY_OF[resource](row) for row in delta.get(resource) or []}
        for resource in _TOMBSTONE_RESOURCE.values()
    }
    for tombstone in delta.get("tombstones") or []:
        resource = _TOMBSTONE_RESOURCE.get(str(tombstone.get("type", "")))
        if resource is None:
            continue
        resource_id = str(tombstone.get("resource_id", ""))
        deleted_at = _millis(tombstone.get("deleted_at"))
        row = rows[resource].get(resource_id)
        # A row changed in this delta after the deletion was re-created.
        if row is not None and not (
            resource_id in delta_keys[resource] and _millis(row.get("modified_at")) > deleted_at
        ):
            del rows[resource][resource_id]
        if resource == "notes":
            # Cards go with their note even where the server wrote no card
            # tombstone of their own.
            for card_id, card in list(rows["cards"].items()):
                if str(card.get("note_guid", "")) == resource_id and not (
                    card_id in delta_keys["cards"] and _millis(card.get("modified_at")) > deleted_at
                ):
                    del rows["cards"][card_id]
    # The server drops a media tombstone when the file is uploaded again.
    for key, tombstone in list(rows["tombstones"].items()):
        if tombstone.get("type") == "media":
            media = rows["media"].get(str(tombstone.get("resource_id", "")))
            if media is not None and _millis(media.get("modified_at")) > _millis(tombstone.get("deleted_at")):
                del rows["tombstones"][key]
    merged = {key: value for key, value in base.items() if key not in _KEY_OF}
    merged.update({key: value for key, value in delta.items() if key not in _KEY_OF})
    for resource, current in rows.items():
        merged[resource] = list(current.values())
    return merged


def _path(col) -> Path:
    return Path(os.path.dirname(col.path)) / _FILENAME


def _identity(client) -> dict[str, str]:
    token = str(getattr(client, "token", "") or "")
    return {
        "endpoint": str(getattr(client, "endpoint", "") or ""),
        # Tells sign-ins apart without storing the token itself; a new sign-in
        # simply starts with a full manifest.
        "account": hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else "",
    }


def _load(col, client) -> dict[str, Any] | None:
    try:
        with gzip.open(_path(col), "rt", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(cached, dict) or cached.get("version") != CACHE_VERSION:
        return None
    identity = _identity(client)
    if cached.get("endpoint") != identity["endpoint"] or cached.get("account") != identity["account"]:
        return None
    if time.time() - float(cached.get("full_at", 0) or 0) > FULL_REFRESH_SECONDS:
        return None
    manifest = cached.get("manifest")
    if not isinstance(manifest, dict) or not manifest.get("server_time"):
        return None
    return cached


def fetch(col, client, *, progress=None) -> dict[str, Any]:
    """The current full server manifest: cached copy plus delta, or a full fetch."""
    cached = _load(col, client)
    if cached is not None:
        try:
            delta = client.manifest(since=cached["manifest"]["server_time"])
        except Exception:  # noqa: BLE001 - fall back to a full manifest
            delta = None
        if isinstance(delta, dict) and delta.get("server_time"):
            merged = merge(cached["manifest"], delta)
            merged["_kelma_full_at"] = cached.get("full_at", 0)
            merged["_kelma_changes"] = _changes(delta)
            if progress:
                progress(f"Server manifest: {merged['_kelma_changes']} change(s) since the last sync")
            return merged
    if progress:
        progress("Fetching the full server manifest…")
    manifest = client.manifest()
    manifest["_kelma_full_at"] = time.time()
    manifest["_kelma_changes"] = -1  # a full fetch is always saved
    return manifest


def refresh(client, manifest: dict[str, Any]) -> dict[str, Any]:
    """`manifest` updated with what changed on the server since it was taken."""
    delta = client.manifest(since=manifest["server_time"])
    merged = merge(manifest, delta)
    merged["_kelma_full_at"] = manifest.get("_kelma_full_at", 0)
    previous = int(manifest.get("_kelma_changes", -1))
    merged["_kelma_changes"] = -1 if previous < 0 else previous + _changes(delta)
    return merged


def _changes(delta: dict[str, Any]) -> int:
    return sum(len(delta.get(key) or []) for key in _KEY_OF)


def save(col, client, manifest: dict[str, Any]) -> None:
    if manifest.get("_kelma_changes") == 0 and _path(col).exists():
        # Nothing changed on the server since the cached copy was taken, so it
        # and its server_time are still exact; skip rewriting a large file.
        return
    payload = {
        "version": CACHE_VERSION,
        **_identity(client),
        "full_at": manifest.get("_kelma_full_at", 0),
        "manifest": {key: value for key, value in manifest.items() if not key.startswith("_kelma")},
    }
    path = _path(col)
    temporary = path.with_name(path.name + ".tmp")
    try:
        # One encode and one compress: json.dump into a gzip stream makes
        # hundreds of thousands of small writes and takes seconds.
        data = gzip.compress(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"), compresslevel=1
        )
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def forget(col) -> None:
    try:
        _path(col).unlink()
    except OSError:
        pass
