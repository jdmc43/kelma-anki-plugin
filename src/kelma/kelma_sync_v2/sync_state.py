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


def load_state(col: Collection) -> dict[str, Any]:  # type: ignore[name-defined]
    path = state_path_for(col)
    if not path.exists():
        return {"notes": [], "cards": [], "notetypes": [], "decks": []}
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return {"notes": [], "cards": [], "notetypes": [], "decks": []}
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
) -> dict[str, Any]:
    return {
        "version": 3,
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
