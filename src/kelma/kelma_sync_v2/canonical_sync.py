"""Explicitly publish the selected local client state to KelmaSync."""
from __future__ import annotations

from typing import Any

from anki.collection import Collection

from . import anki_local
from .client import V2Client
from .media_sync import sync_media_once

_BATCH = 1000
_NOTE_BATCH = 3000


def push_client_state(
    col: Collection,
    client: V2Client,
    *,
    deck_names: list[str],
    progress=None,
) -> dict[str, int]:
    """Force the scoped local collection state to KelmaSync.

    This operation never pulls. It is intentionally separate from source
    selection so the user decides the local canonical state before publishing.
    """
    manifest = anki_local.local_manifest(col, deck_names=deck_names, progress=progress)
    totals = {"notetypes": 0, "decks": 0, "notes": 0, "cards": 0, "media": 0, "deleted": 0}

    # Remove scoped server resources absent from the chosen client state. This
    # makes "Use Anki / AnkiWeb" canonical for server-only items as well as
    # changed items, instead of merely upserting what exists locally.
    from .content_sync import _scope_server_manifest_to_decks
    server = _scope_server_manifest_to_decks(client, client.manifest(), deck_names, progress=progress)
    local_cards = {str(x.get("logical_key")) for x in manifest["cards"]}
    local_notes = {str(x.get("guid")) for x in manifest["notes"]}
    for card in server.get("cards", []):
        key = str(card.get("logical_key") or f"{card.get('note_guid', '')}:{int(card.get('ord', 0) or 0)}")
        if key not in local_cards and card.get("card_id"):
            client.delete_card(int(card["card_id"]))
            totals["deleted"] += 1
    for note in server.get("notes", []):
        guid = str(note.get("guid") or "")
        if guid and guid not in local_notes:
            client.delete_note(guid)
            totals["deleted"] += 1

    resources: list[tuple[str, list[dict[str, Any]], Any]] = [
        ("notetypes", manifest["notetypes"], lambda item: anki_local.notetype_record(col, int(item["notetype_id"]))),
        ("decks", manifest["decks"], lambda item: anki_local.deck_record(col, str(item["name"]))),
        ("notes", manifest["notes"], lambda item: anki_local.note_record(col, str(item["guid"]))),
        ("cards", manifest["cards"], lambda item: anki_local.card_record(col, int(item["card_id"]))),
    ]
    for kind, entries, record_for in resources:
        if progress:
            progress(f"Publishing {len(entries)} {kind} to KelmaSync…")
        batch_size = _NOTE_BATCH if kind == "notes" else _BATCH
        for start in range(0, len(entries), batch_size):
            records = []
            for item in entries[start:start + batch_size]:
                record = record_for(item)
                if not record:
                    continue
                if kind == "notes":
                    record = {k: record[k] for k in ("guid", "notetype_id", "fields", "tags", "client_modified_at")}
                    record["base_checksum"] = ""
                elif kind == "cards":
                    record = {
                        k: record[k]
                        for k in (
                            "card_id", "note_guid", "deck_name", "ord", "scheduling",
                            "study_state", "study_state_client_modified_at",
                            "schedule_reset_through_review_id", "schedule_reset_client_modified_at",
                            "client_modified_at",
                        )
                    }
                elif kind == "notetypes":
                    record = {k: record[k] for k in ("notetype_id", "name", "definition", "client_modified_at")}
                    record["base_checksum"] = ""
                elif kind == "decks":
                    record = {k: record[k] for k in ("name", "config", "client_modified_at")}
                    record["base_checksum"] = ""
                records.append(record)
            payload = {"notes": [], "cards": [], "notetypes": [], "decks": []}
            payload[kind] = records
            response = client.batch_push(payload, force=True)
            totals[kind] += int((response.get("accepted") or {}).get(kind, 0))
            if progress:
                progress(f"{kind}: {min(start + batch_size, len(entries))}/{len(entries)} published")

    server_manifest = client.manifest()
    media = sync_media_once(
        col,
        client,
        server_manifest,
        progress=progress,
        deck_names=deck_names,
    )
    totals["media"] = media.uploaded
    return totals


def push_selected_client_state(
    col: Collection,
    client: V2Client,
    *,
    changes: list[dict[str, Any]],
    deck_names: list[str],
    progress=None,
) -> dict[str, int]:
    """Publish only joint-state decisions that differ from KelmaSync.

    ``changes`` contains resource/key pairs and the old server manifest item.
    The chosen value has already been applied to ``col``. This avoids the old
    behavior of force-uploading the entire routed collection when KelmaSync
    already supplied the chosen versions.
    """
    totals = {"notetypes": 0, "decks": 0, "notes": 0, "cards": 0, "media": 0, "deleted": 0}
    by_resource: dict[str, list[dict[str, Any]]] = {
        "notetypes": [], "decks": [], "notes": [], "cards": [],
    }
    namespace_changes: list[dict[str, Any]] = []
    for change in changes:
        if change.get("namespace"):
            namespace_changes.append(change)
            continue
        resource = str(change["resource"])
        if resource in by_resource:
            by_resource[resource].append(change)

    def local_record(resource: str, key: str):
        if resource == "notes":
            return anki_local.note_record(col, key)
        if resource == "notetypes":
            return anki_local.notetype_record(col, int(key))
        if resource == "decks":
            return anki_local.deck_record(col, key)
        guid, ord_text = key.rsplit(":", 1)
        card_id = col.db.scalar(
            "SELECT c.id FROM cards c JOIN notes n ON n.id=c.nid "
            "WHERE n.guid=? AND c.ord=? ORDER BY c.id LIMIT 1",
            guid, int(ord_text),
        )
        return anki_local.card_record(col, int(card_id)) if card_id else None

    def delete_server(resource: str, key: str, server_item: dict[str, Any] | None) -> None:
        if not server_item:
            return
        if resource == "notes":
            client.delete_note(key)
        elif resource == "cards" and server_item.get("card_id"):
            client.delete_card(int(server_item["card_id"]))
        elif resource == "notetypes":
            client.delete_notetype(int(key))
        elif resource == "decks":
            client.delete_deck(key)
        totals["deleted"] += 1

    # A namespace mismatch is one user decision but can cover thousands of
    # cards. Publish it as one deck upsert plus batched logical-card upserts,
    # then remove the old empty server deck path.
    for change in namespace_changes:
        selected_name = str(change.get("selected_namespace") or "")
        server_name = str(change.get("target_namespace") or "")
        if not selected_name:
            raise RuntimeError("namespace change is missing selected_namespace")
        deck_record = anki_local.deck_record(col, selected_name)
        if not deck_record:
            raise RuntimeError(f"selected namespace deck not found: {selected_name}")
        deck_payload = {
            key: deck_record[key]
            for key in ("name", "config", "client_modified_at")
        }
        deck_payload["base_checksum"] = ""
        response = client.batch_push({
            "notes": [], "cards": [], "notetypes": [], "decks": [deck_payload],
        }, force=True)
        totals["decks"] += int((response.get("accepted") or {}).get("decks", 0))

        deck = col.decks.by_name(selected_name)
        if deck is None:
            raise RuntimeError(f"selected namespace deck not found: {selected_name}")
        card_ids = [
            int(card_id)
            for card_id in col.db.list(
                "SELECT id FROM cards WHERE did=? ORDER BY id", int(deck["id"])
            )
        ]
        if progress:
            progress(
                f"Publishing deck namespace {selected_name}: {len(card_ids)} cards…"
            )
        for start in range(0, len(card_ids), _BATCH):
            records = []
            for card_id in card_ids[start:start + _BATCH]:
                record = anki_local.card_record(col, card_id)
                if not record:
                    continue
                records.append({
                    key: record[key]
                    for key in (
                        "card_id", "note_guid", "deck_name", "ord",
                        "scheduling", "client_modified_at",
                    )
                })
            response = client.batch_push({
                "notes": [], "cards": records, "notetypes": [], "decks": [],
            }, force=True)
            totals["cards"] += int((response.get("accepted") or {}).get("cards", 0))
            if progress:
                progress(
                    f"Deck namespace: {min(start + _BATCH, len(card_ids))}/"
                    f"{len(card_ids)} cards published"
                )
        if server_name and server_name != selected_name:
            client.delete_deck(server_name)
            totals["deleted"] += 1

    for resource in ("decks", "notetypes", "notes", "cards"):
        records = []
        for change in by_resource[resource]:
            key = str(change["key"])
            record = local_record(resource, key)
            if record is None:
                delete_server(resource, key, change.get("server_item"))
                continue
            if resource == "notes":
                record = {k: record[k] for k in ("guid", "notetype_id", "fields", "tags", "client_modified_at")}
                record["base_checksum"] = ""
            elif resource == "cards":
                record = {
                    k: record[k]
                    for k in (
                        "card_id", "note_guid", "deck_name", "ord", "scheduling",
                        "study_state", "study_state_client_modified_at",
                        "schedule_reset_through_review_id", "schedule_reset_client_modified_at",
                        "client_modified_at",
                    )
                }
            elif resource == "notetypes":
                record = {k: record[k] for k in ("notetype_id", "name", "definition", "client_modified_at")}
                record["base_checksum"] = ""
            else:
                record = {k: record[k] for k in ("name", "config", "client_modified_at")}
                record["base_checksum"] = ""
            records.append(record)

        if progress and (records or by_resource[resource]):
            progress(f"Publishing {len(records)} changed {resource} to KelmaSync…")
        batch_size = _NOTE_BATCH if resource == "notes" else _BATCH
        for start in range(0, len(records), batch_size):
            payload = {"notes": [], "cards": [], "notetypes": [], "decks": []}
            payload[resource] = records[start:start + batch_size]
            response = client.batch_push(payload, force=True)
            totals[resource] += int((response.get("accepted") or {}).get(resource, 0))

    # Media can only have become newly relevant when note content changed.
    if by_resource["notes"]:
        if progress:
            progress("Checking media referenced by the changed notes…")
        media = sync_media_once(
            col,
            client,
            client.manifest(),
            progress=progress,
            deck_names=deck_names,
        )
        totals["media"] = media.uploaded
    return totals


def mark_selected_state_for_ankiweb(
    col: Collection,
    changes: list[dict[str, Any]],
) -> tuple[int, int]:
    """Mark only joint-state choices that differ from AnkiWeb as pending.

    Deletions already have graves from the apply step. Deck/notetype manager
    updates carry their own sync metadata, so only changed notes and scheduling
    cards need explicit ``usn=-1`` stamps here.
    """
    note_guids = {
        str(change["key"])
        for change in changes
        if change.get("resource") == "notes"
    }
    card_keys = {
        str(change["key"])
        for change in changes
        if change.get("resource") == "cards"
    }
    note_count = 0
    card_count = 0
    if note_guids:
        guids = sorted(note_guids)
        for start in range(0, len(guids), _NOTE_BATCH):
            chunk = guids[start:start + _NOTE_BATCH]
            marks = ",".join("?" for _ in chunk)
            note_count += int(col.db.scalar(
                f"SELECT count(*) FROM notes WHERE guid IN ({marks})", *chunk
            ) or 0)
            col.db.execute(f"UPDATE notes SET usn=-1 WHERE guid IN ({marks})", *chunk)
    parsed_cards = [
        (key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1]))
        for key in sorted(card_keys)
    ]
    # Two bind variables per logical key; 400 stays below conservative SQLite
    # variable limits while replacing thousands of per-card queries.
    for start in range(0, len(parsed_cards), 400):
        chunk = parsed_cards[start:start + 400]
        values_sql = ",".join("(?,?)" for _ in chunk)
        args = [value for pair in chunk for value in pair]
        card_count += int(col.db.scalar(
            f"""
            WITH wanted(guid, ord) AS (VALUES {values_sql})
            SELECT count(*) FROM cards c
            JOIN notes n ON n.id=c.nid
            JOIN wanted w ON w.guid=n.guid AND w.ord=c.ord
            """,
            *args,
        ) or 0)
        col.db.execute(
            f"""
            WITH wanted(guid, ord) AS (VALUES {values_sql})
            UPDATE cards SET usn=-1 WHERE id IN (
                SELECT c.id FROM cards c
                JOIN notes n ON n.id=c.nid
                JOIN wanted w ON w.guid=n.guid AND w.ord=c.ord
            )
            """,
            *args,
        )
    return note_count, card_count


def mark_client_state_for_ankiweb(col: Collection, deck_names: list[str]) -> tuple[int, int]:
    """Mark scoped notes/cards pending so native AnkiWeb publishes local state."""
    dids = anki_local._deck_ids_for_names(col, deck_names)
    if not dids:
        return 0, 0
    marks = ",".join("?" for _ in dids)
    valid_note = "nid IN (SELECT id FROM notes WHERE guid != '')"
    note_count = int(col.db.scalar(
        f"SELECT count(DISTINCT nid) FROM cards WHERE did IN ({marks}) AND {valid_note}", *dids
    ) or 0)
    card_count = int(col.db.scalar(
        f"SELECT count(*) FROM cards WHERE did IN ({marks}) AND {valid_note}", *dids
    ) or 0)
    col.db.execute(
        f"UPDATE cards SET usn=-1 WHERE did IN ({marks}) AND {valid_note}", *dids
    )
    col.db.execute(
        f"UPDATE notes SET usn=-1 WHERE guid != '' AND id IN "
        f"(SELECT DISTINCT nid FROM cards WHERE did IN ({marks}))",
        *dids,
    )
    return note_count, card_count
