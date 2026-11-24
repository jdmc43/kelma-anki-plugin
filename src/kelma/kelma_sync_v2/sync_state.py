"""Local sync state snapshot for detecting local deletes.

After each successful sync, we record which server-known resources exist locally.
On the next sync, any resource present in the snapshot but absent locally is a
local deletion — we push a DELETE to the server so other clients learn about it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def state_path_for(col: Collection) -> Path:  # type: ignore[name-defined]
    """Derive a state file path from the collection's media dir."""
    media_dir = Path(col.media.dir())
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir / "kelma_sync_v2_state.json"


def empty_state() -> dict[str, Any]:
    return {
        "notes": [], "cards": [], "notetypes": [], "decks": [],
        "cardScheduleResets": {}, "cardDueDateOverrides": {}, "completedAt": "",
    }


def load_state(col: Collection) -> dict[str, Any]:  # type: ignore[name-defined]
    path = state_path_for(col)
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return empty_state()
    for key in ("notes", "cards", "notetypes", "decks"):
        if not isinstance(data.get(key), list):
            data[key] = []
    if not isinstance(data.get("cardScheduleResets"), dict):
        data["cardScheduleResets"] = {}
    if not isinstance(data.get("cardDueDateOverrides"), dict):
        data["cardDueDateOverrides"] = {}
    if not isinstance(data.get("completedAt"), str):
        data["completedAt"] = ""
    return data


def save_state(col: Collection, state: dict[str, Any]) -> None:  # type: ignore[name-defined]
    path = state_path_for(col)
    path.write_text(json.dumps(state, indent=2), "utf-8")


def build_state(
    notes: list[str],
    cards: list[str],
    notetypes: list[str],
    decks: list[str],
    *,
    scope: list[str] | None = None,
    card_schedule_resets: dict[str, dict[str, Any]] | None = None,
    card_due_date_overrides: dict[str, dict[str, Any]] | None = None,
    completed_at: str = "",
    server: str = "",
) -> dict[str, Any]:
    return {
        "version": 3,
        "server": server,
        "scope": sorted(set(scope or [])),
        "notes": sorted(notes),
        "cards": sorted(cards),
        "notetypes": sorted(notetypes),
        "decks": sorted(decks),
        "cardScheduleResets": card_schedule_resets or {},
        "cardDueDateOverrides": card_due_date_overrides or {},
        "completedAt": completed_at,
    }


def compute_local_deletes(
    snapshot: dict[str, Any],
    local_keys: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Return resources in the snapshot but not in local_keys — i.e. locally deleted."""
    out: dict[str, list[str]] = {}
    for resource in ("notes", "cards", "notetypes", "decks"):
        known = set(snapshot.get(resource, []))
        current = local_keys.get(resource, set())
        deleted = sorted(known - current)
        if deleted:
            out[resource] = deleted
    return out


# Late import to avoid requiring anki for type-checking-only usage.
from anki.collection import Collection  # noqa: E402


def same_server(snapshot: dict[str, Any], endpoint: str) -> bool:
    """Whether the snapshot was taken against `endpoint`.

    Snapshots written before servers could be switched carry no server and
    always came from the profile's own server.
    """
    recorded = str(snapshot.get("server") or "").rstrip("/")
    return not recorded or recorded == str(endpoint or "").rstrip("/")
