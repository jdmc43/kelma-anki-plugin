"""KelmaSync v2 sync engine.

This module sits between the raw REST client (`v2_client.py`) and the UI. It
builds a local manifest, fetches the server manifest, classifies resources, and
provides explicit resolution operations. It intentionally does not run
implicitly/destructively; callers decide which actions to execute.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from anki.collection import Collection

from .client import V2Client, V2Conflict
from . import anki_local

Resource = Literal["notes", "cards", "notetypes", "decks"]
Status = Literal["in-sync", "local-only", "server-only", "changed", "conflict"]


@dataclass
class DiffItem:
    resource: Resource
    key: str
    status: Status
    local: dict[str, Any] | None = None
    server: dict[str, Any] | None = None


@dataclass
class SyncPlan:
    notes: list[DiffItem] = field(default_factory=list)
    cards: list[DiffItem] = field(default_factory=list)
    notetypes: list[DiffItem] = field(default_factory=list)
    decks: list[DiffItem] = field(default_factory=list)
    server_time: str = ""

    @property
    def conflicts(self) -> list[DiffItem]:
        return [x for xs in (self.notes, self.cards, self.notetypes, self.decks) for x in xs if x.status == "conflict"]

    @property
    def changed(self) -> list[DiffItem]:
        return [x for xs in (self.notes, self.cards, self.notetypes, self.decks) for x in xs if x.status != "in-sync"]


def build_plan(col: Collection, client: V2Client, since: str | None = None) -> SyncPlan:
    """Compare local vs server manifests and return a non-mutating sync plan.

    For notes/notetypes/decks, a checksum mismatch is labelled `changed` here;
    the actual PUT may return `409`, at which point the item becomes a true
    conflict. For cards, timestamp/newest-wins is handled by the server.
    """
    local = anki_local.local_manifest(col)
    server = client.manifest(since=since)
    plan = SyncPlan(server_time=server.get("server_time", ""))
    plan.notes = _diff_keyed("notes", local.get("notes", []), server.get("notes", []), "guid")
    plan.cards = _diff_keyed("cards", local.get("cards", []), server.get("cards", []), "card_id")
    plan.notetypes = _diff_keyed("notetypes", local.get("notetypes", []), server.get("notetypes", []), "notetype_id")
    plan.decks = _diff_keyed("decks", local.get("decks", []), server.get("decks", []), "name")
    return plan


def push_local(col: Collection, client: V2Client, item: DiffItem, *, force: bool = False) -> dict[str, Any] | None:
    """Push one local resource to the server.

    Returns the saved server record. Raises `V2Conflict` for notes/notetypes/decks
    when the server changed since the client's base checksum.
    """
    if item.resource == "notes":
        rec = anki_local.note_record(col, item.key)
        if not rec:
            return None
        base = (item.server or {}).get("checksum", "") if item.status != "local-only" else ""
        return client.put_note(
            item.key,
            notetype_id=rec["notetype_id"],
            fields=rec["fields"],
            tags=rec["tags"],
            client_modified_at=rec["client_modified_at"],
            base_checksum=base,
            force=force,
        )
    if item.resource == "cards":
        rec = anki_local.card_record(col, int(item.key))
        if not rec:
            return None
        return client.put_card(
            int(item.key),
            note_guid=rec["note_guid"],
            deck_name=rec["deck_name"],
            ord=rec["ord"],
            scheduling=rec["scheduling"],
            client_modified_at=rec["client_modified_at"],
        )
    if item.resource == "notetypes":
        rec = anki_local.notetype_record(col, int(item.key))
        if not rec:
            return None
        base = (item.server or {}).get("checksum", "") if item.status != "local-only" else ""
        return client.put_notetype(
            int(item.key),
            name=rec["name"],
            definition=rec["definition"],
            client_modified_at=rec["client_modified_at"],
            base_checksum=base,
            force=force,
        )
    if item.resource == "decks":
        rec = anki_local.deck_record(col, item.key)
        if not rec:
            return None
        base = (item.server or {}).get("checksum", "") if item.status != "local-only" else ""
        return client.put_deck(
            item.key,
            config=rec["config"],
            client_modified_at=rec["client_modified_at"],
            base_checksum=base,
            force=force,
        )
    raise ValueError(f"unknown resource: {item.resource}")


def pull_server(client: V2Client, item: DiffItem) -> dict[str, Any] | None:
    """Fetch one full server resource. Does not write to local collection."""
    if item.resource == "notes":
        return client.get_note(item.key)
    if item.resource == "cards":
        return client.get_card(int(item.key))
    if item.resource == "notetypes":
        return client.get_notetype(int(item.key))
    if item.resource == "decks":
        return client.get_deck(item.key)
    raise ValueError(f"unknown resource: {item.resource}")


def push_all_non_conflicting(col: Collection, client: V2Client, plan: SyncPlan) -> dict[str, Any]:
    """Push local-only/changed resources, collecting conflicts instead of raising.

    This is the first half of a sync. It does not mutate local state. Server-only
    pulls are handled separately so UI can decide whether to apply them.
    """
    out: dict[str, Any] = {"accepted": [], "conflicts": []}
    for item in plan.changed:
        if item.status == "server-only":
            continue
        try:
            saved = push_local(col, client, item)
            if saved is not None:
                out["accepted"].append({"item": item, "server": saved})
        except V2Conflict as conflict:
            item.status = "conflict"
            out["conflicts"].append({"item": item, "conflict": conflict.payload})
    return out


def force_local(col: Collection, client: V2Client, item: DiffItem) -> dict[str, Any] | None:
    """Explicit resolution: local wins, server is overwritten."""
    return push_local(col, client, item, force=True)


def accept_server(client: V2Client, item: DiffItem) -> dict[str, Any] | None:
    """Explicit resolution: fetch server record for caller to apply locally.

    Applying to Anki is intentionally separate because note/card/notetype/deck
    writes need to go through rslib/Anki APIs carefully.
    """
    return pull_server(client, item)


def canonical_override(client: V2Client, resource: Resource, key: str, record: dict[str, Any]) -> dict[str, Any]:
    """Explicit resolution: user edited a merged canonical record; force it."""
    if resource == "notes":
        return client.put_note(
            key,
            notetype_id=int(record["notetype_id"]),
            fields=list(record["fields"]),
            tags=list(record.get("tags", [])),
            client_modified_at=record["client_modified_at"],
            base_checksum=record.get("base_checksum", ""),
            force=True,
        )
    if resource == "notetypes":
        return client.put_notetype(
            int(key),
            name=record["name"],
            definition=record["definition"],
            client_modified_at=record["client_modified_at"],
            base_checksum=record.get("base_checksum", ""),
            force=True,
        )
    if resource == "decks":
        return client.put_deck(
            key,
            config=record["config"],
            client_modified_at=record["client_modified_at"],
            base_checksum=record.get("base_checksum", ""),
            force=True,
        )
    if resource == "cards":
        return client.put_card(
            int(key),
            note_guid=record["note_guid"],
            deck_name=record["deck_name"],
            ord=int(record["ord"]),
            scheduling=record.get("scheduling", {}),
            client_modified_at=record["client_modified_at"],
        )
    raise ValueError(f"unknown resource: {resource}")


def _diff_keyed(resource: Resource, local: list[dict[str, Any]], server: list[dict[str, Any]], key: str) -> list[DiffItem]:
    lmap = {str(x.get(key, "")): x for x in local if x.get(key, "") != ""}
    smap = {str(x.get(key, "")): x for x in server if x.get(key, "") != ""}
    out: list[DiffItem] = []
    for k in sorted(set(lmap) | set(smap)):
        l = lmap.get(k)
        s = smap.get(k)
        if l and not s:
            out.append(DiffItem(resource, k, "local-only", l, None))
        elif s and not l:
            out.append(DiffItem(resource, k, "server-only", None, s))
        else:
            assert l is not None and s is not None
            if resource == "cards":
                # Cards don't carry checksums; the server resolves by timestamp
                # when pushed. If both exist, mark changed only if timestamps differ.
                status: Status = "in-sync" if l.get("modified_at") == s.get("modified_at") else "changed"
            else:
                status = "in-sync" if l.get("checksum") == s.get("checksum") else "changed"
            out.append(DiffItem(resource, k, status, l, s))
    return out
