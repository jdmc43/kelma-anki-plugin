"""Apply v2 server records to a local Anki collection.

These helpers are deliberately narrow and explicit. They are intended to be
called by the plugin UI after the user chooses to accept server state, or by a
non-conflicting pull path.
"""
from __future__ import annotations

import time
from typing import Any

from anki.collection import Collection

from .conflict_policy import modified_timestamp


def _source_modified_seconds(record: dict[str, Any]) -> int:
    source_time = modified_timestamp(record)
    receipt_time = modified_timestamp({"modified_at": record.get("modified_at")})
    # A source timestamp cannot legitimately be far later than the server time
    # at which it was accepted. Keep legacy clock-skewed records from poisoning
    # Anki's local mod column with a future value.
    if receipt_time > 0 and source_time > receipt_time + 300:
        source_time = receipt_time
    return int(source_time or time.time())


def apply_deck(col: Collection, record: dict[str, Any]) -> str:
    """Create/update a deck config from a v2 deck record."""
    name = record.get("name") or "Default"
    did = col.decks.id(name)
    deck = col.decks.get(did)
    if deck is None:
        raise ValueError(f"could not create/get deck {name}")
    cfg = dict(record.get("config") or {})
    # Preserve local id/name; apply opaque config fields where possible.
    cfg.pop("id", None)
    cfg.pop("name", None)
    deck.update(cfg)
    deck["name"] = name
    deck["mod"] = _source_modified_seconds(record)
    deck["usn"] = 0
    # This legacy sync API is the one that preserves the supplied mtime/usn.
    col.decks.update(deck, preserve_usn=True)
    return name


def apply_server_deck(col: Collection, client, name: str) -> str:
    return apply_deck(col, client.get_deck(name))


def apply_card(
    col: Collection,
    record: dict[str, Any],
    *,
    apply_scheduling: bool = True,
    apply_study_state: bool = True,
    apply_schedule_reset: bool = False,
    apply_due_date_override: bool = False,
) -> int:
    """Apply a server card to an existing local card.

    Card creation is driven by notes/notetypes; if the card id is absent locally
    we skip with an explicit error rather than inventing a card. The card is
    resolved by logical identity (note_guid, ord) when available, because
    card_id is a local creation timestamp and differs across collections.
    Day-based scheduling (`due`/`odue`) is shifted from the writing collection's
    creation-day scale to this collection's, using the `_crt` tag.
    """
    sched = dict(record.get("scheduling") or {})
    note_guid = record.get("note_guid") or ""
    ord_ = record.get("ord")
    cid = 0
    if note_guid and ord_ is not None:
        row = col.db.first(
            "SELECT c.id FROM cards c JOIN notes n ON n.id = c.nid WHERE n.guid = ? AND c.ord = ?",
            note_guid, int(ord_),
        )
        if row:
            cid = int(row[0])
    if not cid:
        # Card doesn't exist locally (e.g. note was just created but the
        # notetype template didn't generate this ord). Skip gracefully.
        return 0

    mod = _source_modified_seconds(record)
    # Resolve the deck by name so pulled cards land in the correct deck,
    # not just wherever add_note placed them.
    deck_name = record.get("deck_name") or ""
    deck_id = 0
    if deck_name:
        deck_id = col.decks.id(deck_name)

    if apply_schedule_reset:
        col.sched.schedule_cards_as_new(
            [cid], restore_position=True, reset_counts=True
        )
        col.db.execute("UPDATE cards SET mod=?, usn=0 WHERE id=?", mod, cid)
    if apply_scheduling:
        queue = int(sched.get("queue", 0) or 0)
        due = int(sched.get("due", 0) or 0)
        odue = int(sched.get("odue", 0) or 0)
        odid = int(sched.get("odid", 0) or 0)
        writer_crt = int(sched.get("_crt", 0) or 0)
        if writer_crt:
            day_shift = (writer_crt // 86400) - (int(col.crt) // 86400)
            if queue in (2, 3):
                due += day_shift
                if odid != 0:
                    odue += day_shift
        fields = {
            "type": int(sched.get("type", 0) or 0),
            "queue": queue,
            "due": due,
            "ivl": int(sched.get("ivl", 0) or 0),
            "factor": int(sched.get("factor", 0) or 0),
            "reps": int(sched.get("reps", 0) or 0),
            "lapses": int(sched.get("lapses", 0) or 0),
            "left": int(sched.get("left", 0) or 0),
            "odue": odue,
            "odid": odid,
            "flags": int(sched.get("flags", 0) or 0),
            "data": sched.get("data", "") or "",
        }
        col.db.execute(
            """
            UPDATE cards SET type=?, queue=?, due=?, ivl=?, factor=?, reps=?,
                             lapses=?, left=?, odue=?, odid=?, flags=?, data=?, mod=?, did=?
            WHERE id=?
            """,
            fields["type"], fields["queue"], fields["due"], fields["ivl"],
            fields["factor"], fields["reps"], fields["lapses"], fields["left"],
            fields["odue"], fields["odid"], fields["flags"], fields["data"], mod,
            deck_id, cid,
        )
    else:
        col.db.execute("UPDATE cards SET mod=?, did=? WHERE id=?", mod, deck_id, cid)
    if apply_due_date_override:
        due_at_ms = int(record.get("due_date_override_ms", 0) or 0)
        if due_at_ms > 0:
            target_collection_day = due_at_ms // 86_400_000 - int(col.crt) // 86_400
            days_from_today = target_collection_day - int(col.sched.today)
            col.sched.set_due_date([cid], str(max(0, days_from_today)))
            col.db.execute("UPDATE cards SET mod=?, usn=0 WHERE id=?", mod, cid)
    if apply_study_state:
        desired_state = (
            "suspended" if record.get("study_state") == "suspended" else "active"
        )
        current_queue = int(col.db.scalar("SELECT queue FROM cards WHERE id=?", cid) or 0)
        if desired_state == "suspended" and current_queue != -1:
            col.sched.suspend_cards([cid])
            col.db.execute("UPDATE cards SET mod=?, usn=0 WHERE id=?", mod, cid)
        elif desired_state == "active" and current_queue == -1:
            col.sched.unsuspend_cards([cid])
            col.db.execute("UPDATE cards SET mod=?, usn=0 WHERE id=?", mod, cid)

    # Do NOT leave usn=-1 here. A KelmaSync pull is not automatically an
    # AnkiWeb edit; the explicit publish step marks chosen cards pending.
    return cid


def apply_server_card(col: Collection, client, card_id: int) -> int:
    return apply_card(col, client.get_card(card_id))


def _anki_compatible_notetype_definition(
    col: Collection,
    definition: dict[str, Any],
    existing: dict[str, Any] | None,
    name: str,
) -> dict[str, Any]:
    """Fill fields added by newer Anki releases without changing server data.

    KelmaSync definitions are intentionally opaque and can have been uploaded
    by an older Anki or another client. Rslib's current JSON decoder requires
    every field/template property (for example ``sticky``), so seed each nested
    object from Anki's current constructors before overlaying the remote values.
    Existing generated IDs are retained when possible.
    """
    compatible = dict(existing) if existing else dict(col.models.new(name))
    compatible.update({
        key: value
        for key, value in definition.items()
        if key not in ("flds", "tmpls")
    })

    def hydrate(
        key: str,
        constructor,
        fallback_prefix: str,
    ) -> None:
        if key not in definition:
            return
        remote_items = definition.get(key)
        if not isinstance(remote_items, list):
            raise ValueError(f"server notetype {key} must be a list")
        old_items = compatible.get(key) if isinstance(compatible.get(key), list) else []
        hydrated = []
        for index, item in enumerate(remote_items):
            if not isinstance(item, dict):
                raise ValueError(f"server notetype {key}[{index}] must be an object")
            match = next(
                (
                    old
                    for old in old_items
                    if isinstance(old, dict)
                    and (
                        old.get("ord") == item.get("ord")
                        or old.get("name") == item.get("name")
                    )
                ),
                None,
            )
            current = dict(match) if match else dict(
                constructor(str(item.get("name") or f"{fallback_prefix} {index + 1}"))
            )
            current.update(item)
            current["ord"] = int(item.get("ord", index) or 0)
            hydrated.append(current)
        compatible[key] = hydrated

    hydrate("flds", col.models.new_field, "Field")
    hydrate("tmpls", col.models.new_template, "Card")
    return compatible


def apply_notetype(col: Collection, record: dict[str, Any]) -> int:
    """Create or update a local notetype from a v2 notetype record."""
    ntid = int(record.get("notetype_id") or 0)
    definition = dict(record.get("definition") or {})
    if not ntid:
        raise ValueError("server notetype missing notetype_id")
    name = str(record.get("name") or definition.get("name") or ntid)
    existing = col.models.get(ntid)
    definition = _anki_compatible_notetype_definition(
        col, definition, existing, name,
    )
    definition["id"] = ntid
    definition["name"] = name
    # Preserve the source timestamp so this pull does not look like a newer
    # local edit during the next comparison.
    definition["mod"] = _source_modified_seconds(record)
    definition["usn"] = 0
    col.models.update(definition, preserve_usn=True, skip_checks=True)
    return ntid


def apply_server_notetype(col: Collection, client, notetype_id: int) -> int:
    return apply_notetype(col, client.get_notetype(notetype_id))


def apply_note(col: Collection, record: dict[str, Any], *, deck_name: str = "Default") -> int:
    """Create or update a local note from a v2 note record.

    Returns the local note id. Existing notes are found by guid. New notes are
    created with the requested notetype when available. This does not attempt to
    map remote deck membership yet; note placement defaults to `deck_name`.
    """
    guid = record.get("guid") or ""
    if not guid:
        raise ValueError("server note has empty guid")
    fields = list(record.get("fields") or [])
    tags = list(record.get("tags") or [])
    mid = int(record.get("notetype_id") or 0)
    source_mod = _source_modified_seconds(record)

    row = col.db.first("SELECT id FROM notes WHERE guid = ?", guid)
    if row:
        nid = int(row[0])
        note = col.get_note(nid)
        # Keep the local notetype; field count must match local schema. Pad or
        # truncate so Anki APIs don't fail on stale schema mismatches.
        field_count = len(note.fields)
        padded = (fields + [""] * field_count)[:field_count]
        note.fields = padded
        note.tags = tags
        col.update_note(note)
        col.db.execute("UPDATE notes SET mod=?, usn=0 WHERE id=?", source_mod, nid)
        return nid

    nt = col.models.get(mid) if mid else None
    if nt is None:
        raise ValueError(f"missing local notetype {mid}")
    note = col.new_note(nt)
    field_count = len(note.fields)
    note.fields = (fields + [""] * field_count)[:field_count]
    note.tags = tags
    # Preserve the server GUID after Anki creates the note object.
    note.guid = guid
    did = _deck_id(col, deck_name)
    col.add_note(note, did)
    nid = int(note.id)
    col.db.execute("UPDATE notes SET mod=?, usn=0 WHERE id=?", source_mod, nid)
    return nid


def delete_note(col: Collection, guid: str) -> bool:
    row = col.db.first("SELECT id FROM notes WHERE guid = ?", guid)
    if not row:
        return False
    nid = int(row[0])
    col.remove_notes([nid])
    return True


def delete_card(col: Collection, card_id: int) -> bool:
    row = col.db.first("SELECT id FROM cards WHERE id = ?", int(card_id))
    if not row:
        return False
    col.remove_cards([int(card_id)])
    return True


def delete_deck(col: Collection, name: str) -> bool:
    did = col.decks.id(name)
    if not did:
        return False
    # Refuse to delete decks that still contain cards. Note tombstones should be
    # applied first; deck deletion is structural and should be conservative.
    count = col.db.scalar("SELECT COUNT(*) FROM cards WHERE did = ?", int(did)) or 0
    if int(count) > 0:
        raise ValueError(f"cannot delete non-empty deck: {name}")
    col.decks.remove([int(did)])
    return True


def delete_notetype(col: Collection, notetype_id: int) -> bool:
    ntid = int(notetype_id)
    if not col.models.get(ntid):
        return False
    count = col.db.scalar("SELECT COUNT(*) FROM notes WHERE mid = ?", ntid) or 0
    if int(count) > 0:
        raise ValueError(f"cannot delete notetype still used by notes: {ntid}")
    col.models.remove(ntid)
    return True


def apply_server_note(col: Collection, client, guid: str, *, deck_name: str = "Default") -> int:
    """Fetch a server note and apply it locally."""
    record = client.get_note(guid)
    return apply_note(col, record, deck_name=deck_name)


def _deck_id(col: Collection, name: str) -> int:
    did = col.decks.id(name)
    if did:
        return int(did)
    return int(col.decks.id("Default"))
