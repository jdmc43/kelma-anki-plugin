"""KelmaSync v2 local collection helpers.

This module converts an Anki collection into the resource shapes expected by the
v2 REST API. It does not decide conflict policy; it only builds local records
and lightweight manifests.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from anki.collection import Collection

from .checksum_rs import (
    card_checksum,
    card_checksums_batch,
    deck_checksum,
    note_checksum,
    note_checksums_batch,
    notetype_checksum,
)


def iso_from_anki_mod(mod_seconds: int) -> str:
    """Convert Anki's integer seconds timestamp to RFC3339/ISO string."""
    return datetime.fromtimestamp(int(mod_seconds or 0), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def checksum(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def note_record(col: Collection, guid: str) -> dict[str, Any] | None:
    row = col.db.first("SELECT id, guid, mid, mod, flds, tags FROM notes WHERE guid = ?", guid)
    if not row:
        return None
    _nid, guid, mid, mod, flds, tags = row
    fields = str(flds or "").split("\x1f")
    tag_list = [t for t in str(tags or "").split() if t]
    return {
        "guid": guid,
        "notetype_id": int(mid),
        "fields": fields,
        "tags": tag_list,
        "checksum": note_checksum(fields, tag_list),
        "client_modified_at": iso_from_anki_mod(int(mod or 0)),
    }


def note_manifest(col: Collection, deck_name: str | None = None, deck_names: list[str] | None = None, progress=None) -> list[dict[str, Any]]:
    """Return note manifest, optionally restricted to Kelma-routed decks."""
    if progress:
        progress("Reading local notes…")
    out: list[dict[str, Any]] = []
    if deck_name and not deck_names:
        deck_names = [deck_name]
    if deck_names:
        dids = _deck_ids_for_names(col, deck_names)
        if not dids:
            return []
        marks = ",".join("?" for _ in dids)
        rows = col.db.all(
            f"""
            SELECT DISTINCT n.guid, n.mid, n.mod, n.flds, n.tags
            FROM notes n JOIN cards c ON c.nid = n.id
            WHERE c.did IN ({marks})
            """,
            *dids,
        )
    else:
        rows = col.db.all("SELECT guid, mid, mod, flds, tags FROM notes")
    parsed = []
    for guid, mid, mod, flds, tags in rows:
        if not guid:
            # Empty GUIDs are ambiguous and cannot be v2 identities. The UI
            # should offer a generate-GUID action before v2 sync.
            continue
        fields = str(flds or "").split("\x1f")
        tag_list = [t for t in str(tags or "").split() if t]
        parsed.append((guid, mid, mod, fields, tag_list))
    if progress:
        progress(f"Checksumming {len(parsed)} notes…")
    checksums = note_checksums_batch([(p[3], p[4]) for p in parsed])
    for (guid, mid, mod, fields, tag_list), cs in zip(parsed, checksums):
        out.append({
            "guid": guid,
            "checksum": cs,
            "modified_at": iso_from_anki_mod(int(mod or 0)),
            "notetype_id": int(mid),
        })
    return out


def card_record(col: Collection, card_id: int) -> dict[str, Any] | None:
    row = col.db.first(
        """
        SELECT c.id, c.nid, c.did, c.ord, c.mod, c.type, c.queue, c.due,
               c.ivl, c.factor, c.reps, c.lapses, c.left, c.odue, c.odid,
               c.flags, c.data, n.guid,
               COALESCE((SELECT MAX(r.id) FROM revlog r WHERE r.cid=c.id AND r.ease BETWEEN 1 AND 4),0),
               COALESCE((SELECT MAX(r.id) FROM revlog r WHERE r.cid=c.id AND r.type=4),0)
        FROM cards c JOIN notes n ON n.id = c.nid
        WHERE c.id = ?
        """,
        card_id,
    )
    if not row:
        return None
    (
        cid, _nid, did, ord_, mod, typ, queue, due, ivl, factor, reps, lapses,
        left, odue, odid, flags, data, guid, max_review_id, max_manual_review_id,
    ) = row
    deck = col.decks.get(int(did))
    deck_name = deck.get("name", str(did)) if deck else str(did)
    scheduling = {
        "type": int(typ or 0),
        "queue": int(queue or 0),
        "due": int(due or 0),
        "ivl": int(ivl or 0),
        "factor": int(factor or 0),
        "reps": int(reps or 0),
        "lapses": int(lapses or 0),
        "left": int(left or 0),
        "odue": int(odue or 0),
        "odid": int(odid or 0),
        "flags": int(flags or 0),
        "data": data or "",
        # Collection creation timestamp. Day-based `due`/`odue` values are
        # relative to this; other collections use it to convert to their own
        # day scale on apply. Excluded from the card checksum (checksum ignores
        # scheduling), so it never causes false conflicts.
        "_crt": int(col.crt),
    }
    modified_at = iso_from_anki_mod(int(mod or 0))
    return {
        "card_id": int(cid),
        "note_guid": guid or "",
        "deck_name": deck_name,
        "ord": int(ord_ or 0),
        "scheduling": scheduling,
        "study_state": "suspended" if int(queue or 0) == -1 else "active",
        "study_state_client_modified_at": modified_at,
        "schedule_reset_through_review_id": (
            int(max_review_id or 0) if int(typ or 0) == 0 and int(queue or 0) == 0 else 0
        ),
        "max_review_id": int(max_review_id or 0),
        "max_manual_review_id": int(max_manual_review_id or 0),
        "schedule_reset_client_modified_at": modified_at,
        "checksum": card_checksum(guid or "", deck_name, int(ord_ or 0), scheduling),
        "client_modified_at": modified_at,
    }


def repair_duplicate_cards(col: Collection, deck_names: list[str] | None = None, progress=None) -> int:
    """Remove duplicate cards in a deck scope by logical card identity.

    Anki card ids are local creation timestamps and are not stable across
    collections. The logical identity of a generated card is normally
    (note_guid, ord). Empty note GUIDs are invalid for sync, but older imports
    can contain them; for those, use (mid, fields, tags, ord) so exact duplicate
    blank-GUID notes don't inflate deck counts forever.

    Keeps the newest card id in each duplicate group and deletes the rest. If a
    deleted card's note has no remaining cards, the orphan note is deleted too.
    """
    dids = _deck_ids_for_names(col, deck_names or []) if deck_names else []
    where = ""
    params: list[int] = []
    if deck_names:
        if not dids:
            return 0
        where = "WHERE c.did IN (" + ",".join("?" for _ in dids) + ")"
        params = dids
    rows = col.db.all(
        f"""
        SELECT c.id, c.nid, c.ord, n.guid, n.mid, n.flds, n.tags
        FROM cards c JOIN notes n ON n.id = c.nid
        {where}
        ORDER BY c.id
        """,
        *params,
    )
    groups: dict[tuple, list[tuple[int, int]]] = {}
    for cid, nid, ord_, guid, mid, flds, tags in rows:
        if guid:
            key = ("guid", str(guid), int(ord_ or 0))
        else:
            key = ("blank", int(mid or 0), str(flds or ""), str(tags or ""), int(ord_ or 0))
        groups.setdefault(key, []).append((int(cid), int(nid)))

    deleted = 0
    for key, cards in groups.items():
        if len(cards) <= 1:
            continue
        # Keep newest/highest id; delete older duplicates.
        cards = sorted(cards, key=lambda x: x[0])
        for cid, nid in cards[:-1]:
            col.db.execute("DELETE FROM revlog WHERE cid = ?", cid)
            col.db.execute("DELETE FROM cards WHERE id = ?", cid)
            deleted += 1
            remaining = col.db.scalar("SELECT COUNT(*) FROM cards WHERE nid = ?", nid) or 0
            if int(remaining) == 0:
                col.db.execute("DELETE FROM notes WHERE id = ?", nid)
    if deleted:
        try:
            col.save()
        except Exception:
            # save() is deprecated in newer Anki and may be unnecessary.
            pass
        if progress:
            progress(f"Repaired {deleted} duplicate local card(s)")
    return deleted


def card_manifest(col: Collection, deck_names: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    dids = _deck_ids_for_names(col, deck_names or []) if deck_names else []
    where = ""
    params: list[int] = []
    if deck_names:
        if not dids:
            return []
        where = "WHERE c.did IN (" + ",".join("?" for _ in dids) + ")"
        params = dids
    rows = col.db.all(
        f"""
        SELECT c.id, c.did, c.ord, c.mod, c.type, c.queue, c.due,
               c.ivl, c.factor, c.reps, c.lapses, c.left, c.odue, c.odid,
               c.flags, c.data, n.id, n.guid,
               COALESCE((SELECT MAX(r.id) FROM revlog r WHERE r.cid=c.id AND r.ease BETWEEN 1 AND 4),0),
               COALESCE((SELECT MAX(r.id) FROM revlog r WHERE r.cid=c.id AND r.type=4),0)
        FROM cards c JOIN notes n ON n.id = c.nid
        {where}
        """,
        *params,
    )
    deck_names: dict[int, str] = {}
    for (
        cid, did, ord_, mod, typ, queue, due, ivl, factor, reps, lapses,
        left, odue, odid, flags, data, nid, guid, max_review_id, max_manual_review_id,
    ) in rows:
        did_int = int(did)
        if did_int not in deck_names:
            deck = col.decks.get(did_int)
            deck_names[did_int] = deck.get("name", str(did_int)) if deck else str(did_int)
        if not guid:
            # Empty note GUIDs cannot be represented safely in v2. Notes already
            # skip them; keep cards consistent so they are not pushed as a
            # single ambiguous empty-guid identity.
            continue
        deck_name = deck_names[did_int]
        scheduling = {
            "type": int(typ or 0), "queue": int(queue or 0), "due": int(due or 0),
            "ivl": int(ivl or 0), "factor": int(factor or 0), "reps": int(reps or 0),
            "lapses": int(lapses or 0), "left": int(left or 0), "odue": int(odue or 0),
            "odid": int(odid or 0), "flags": int(flags or 0), "data": data or "",
            "_crt": int(col.crt),
        }
        out.append({
            "card_id": int(cid),
            "note_id": int(nid),
            "note_guid": guid or "",
            "ord": int(ord_ or 0),
            "deck_id": did_int,
            "deck_name": deck_name,
            "logical_key": f"{guid or ''}:{int(ord_ or 0)}",
            "checksum": "",
            "scheduling": scheduling,
            "study_state": "suspended" if int(queue or 0) == -1 else "active",
            "study_state_client_modified_at": iso_from_anki_mod(int(mod or 0)),
            "schedule_reset_through_review_id": (
                int(max_review_id or 0) if int(typ or 0) == 0 and int(queue or 0) == 0 else 0
            ),
            "max_review_id": int(max_review_id or 0),
            "max_manual_review_id": int(max_manual_review_id or 0),
            "schedule_reset_client_modified_at": iso_from_anki_mod(int(mod or 0)),
            "modified_at": iso_from_anki_mod(int(mod or 0)),
        })
    checksums = card_checksums_batch([
        (item["note_guid"], item["deck_name"], item["ord"])
        for item in out
    ])
    for item, checksum in zip(out, checksums):
        item["checksum"] = checksum
    return out


def _normalized_deck_config(deck: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(deck)
    # Name is the identity; id/mod/usn are local bookkeeping and should not
    # create conflicts across clients.
    for key in ("name", "id", "mod", "usn", "newToday", "revToday", "lrnToday", "timeToday"):
        cfg.pop(key, None)
    return cfg


def _normalized_notetype_definition(nt: dict[str, Any]) -> dict[str, Any]:
    definition = dict(nt)
    # notetype_id/name are stored separately; mod/usn are local bookkeeping.
    for key in ("id", "mod", "usn"):
        definition.pop(key, None)
    # Deep-normalize: remove auto-generated ids and materialize properties that
    # newer Anki releases require. A missing property from an old client and
    # its modern default must produce the same cross-client checksum.
    nested_defaults = {
        "flds": {
            "collapsed": False,
            "description": "",
            "excludeFromSearch": False,
            "font": "Arial",
            "plainText": False,
            "preventDeletion": False,
            "rtl": False,
            "size": 20,
            "sticky": False,
            "tag": None,
        },
        "tmpls": {
            "afmt": "",
            "bafmt": "",
            "bfont": "",
            "bqfmt": "",
            "bsize": 0,
            "did": None,
            "qfmt": "",
        },
    }
    for arr_key, defaults in nested_defaults.items():
        arr = definition.get(arr_key)
        if isinstance(arr, list):
            new_arr = []
            for item in arr:
                if isinstance(item, dict):
                    cleaned = dict(item)
                    cleaned.pop("id", None)
                    for key, value in defaults.items():
                        cleaned.setdefault(key, value)
                    new_arr.append(cleaned)
                else:
                    new_arr.append(item)
            definition[arr_key] = new_arr
    return definition


def deck_record(col: Collection, name: str) -> dict[str, Any] | None:
    deck = next((d for d in col.decks.all() if d.get("name") == name), None)
    if not deck:
        return None
    cfg = _normalized_deck_config(deck)
    return {
        "name": name,
        "config": cfg,
        "checksum": deck_checksum(cfg),
        "client_modified_at": iso_from_anki_mod(int(deck.get("mod", 0) or 0)),
    }


def deck_manifest(col: Collection, deck_names: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    allowed = set(deck_names or [])

    def _in_scope(name: str) -> bool:
        # Include picked decks AND their subdecks, matching the prefix-based
        # note/card scoping so a subdeck deck record isn't reported one-sided.
        return name in allowed or any(name.startswith(a + "::") for a in allowed)

    for deck in col.decks.all():
        name = deck.get("name", "")
        if deck_names and not _in_scope(name):
            continue
        cfg = _normalized_deck_config(deck)
        out.append({
            "name": name,
            "checksum": deck_checksum(cfg),
            "modified_at": iso_from_anki_mod(int(deck.get("mod", 0) or 0)),
        })
    return out


def notetype_record(col: Collection, notetype_id: int) -> dict[str, Any] | None:
    nt = col.models.get(notetype_id)
    if not nt:
        return None
    name = nt.get("name", str(notetype_id))
    modified = int(nt.get("mod", 0) or 0)
    definition = _normalized_notetype_definition(nt)
    return {
        "notetype_id": int(notetype_id),
        "name": name,
        "definition": definition,
        "checksum": notetype_checksum(name, definition),
        "client_modified_at": iso_from_anki_mod(modified),
    }


def notetype_manifest(col: Collection, notetype_ids: set[int] | None = None) -> list[dict[str, Any]]:
    out = []
    for nt in col.models.all():
        ntid = int(nt.get("id", 0))
        if notetype_ids is not None and ntid not in notetype_ids:
            continue
        name = nt.get("name", str(ntid))
        definition = _normalized_notetype_definition(nt)
        out.append({
            "notetype_id": ntid,
            "checksum": notetype_checksum(name, definition),
            "modified_at": iso_from_anki_mod(int(nt.get("mod", 0) or 0)),
        })
    return out


def local_manifest(col: Collection, deck_name: str | None = None, deck_names: list[str] | None = None, progress=None) -> dict[str, Any]:
    if deck_name and not deck_names:
        deck_names = [deck_name]
    notes = note_manifest(col, deck_names=deck_names, progress=progress)
    used_notetypes = {int(n["notetype_id"]) for n in notes}
    if progress:
        progress("Reading local cards…")
    cards = card_manifest(col, deck_names=deck_names)
    if progress:
        progress(f"Read {len(cards)} cards · reading notetypes…")
    # Unused stock notetypes in a fresh collection are not user content.
    notetypes = notetype_manifest(col, notetype_ids=used_notetypes)
    if progress:
        progress(f"Read {len(notetypes)} notetypes · reading decks…")
    decks = deck_manifest(col, deck_names=deck_names)
    if progress:
        progress(f"Read {len(decks)} decks")
    return {
        "notes": notes,
        "cards": cards,
        "notetypes": notetypes,
        "decks": decks,
    }


def _deck_ids_for_name(col: Collection, name: str) -> list[int]:
    return _deck_ids_for_names(col, [name])


def _deck_ids_for_names(col: Collection, names: list[str]) -> list[int]:
    wanted = set(names)
    ids: list[int] = []
    for deck in col.decks.all():
        dname = str(deck.get("name", ""))
        if dname in wanted or any(dname.startswith(name + "::") for name in wanted):
            try:
                ids.append(int(deck.get("id")))
            except Exception:
                pass
    return ids
