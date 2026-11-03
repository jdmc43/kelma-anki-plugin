"""Inspect/diff surface for the Kelma plugin.

Fetches the server's read-only collection manifest via ``GET /sync/inspect``,
builds the same manifest from the local master collection, and diffs them
deck-by-deck so the user can see what will change *before* committing to a sync.

See ``kelma_sync/docs/REDESIGN.md`` for the design. This replaces the blind
auto-reconcile guesswork: the user sees the actual delta and decides.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Optional

from anki.collection import Collection

from . import config


# --- Server manifest ---------------------------------------------------------


def fetch_server_manifest(hkey: str, endpoint: str) -> dict:
    """GET ``/sync/inspect`` using credentials captured on Anki's main thread.

    This function runs in a worker thread and deliberately does not touch
    ``mw`` or ``addonManager``; Qt/Anki objects are not thread-safe.
    """
    endpoint = endpoint.rstrip("/")
    url = f"{endpoint}/sync/inspect"
    header = json.dumps({"v": 11, "k": hkey, "c": "kelma-plugin", "s": ""})
    # Cloudflare's bot filter rejects Python's default `Python-urllib` agent
    # before the request reaches the gateway. Identify this as the Anki client,
    # matching Anki's own HttpClient behavior.
    req = urllib.request.Request(
        url,
        headers={"anki-sync": header, "User-Agent": "Anki (Kelma plugin)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            manifest = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"server returned HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"could not reach {endpoint}: {err.reason}") from err
    if not isinstance(manifest, dict) or not isinstance(manifest.get("decks"), list):
        raise RuntimeError("server returned an invalid manifest")
    notes = manifest.get("notes")
    if not isinstance(notes, list):
        raise RuntimeError("server returned an invalid note manifest")
    if notes and any("decks" not in note or "hash" not in note for note in notes):
        raise RuntimeError(
            "server conflict details are not available yet; deployment may still be in progress"
        )
    return manifest


def fetch_server_note(hkey: str, endpoint: str, nid: int, guid: str = "") -> Optional[dict]:
    """GET ``/sync/inspect/note?nid=...&guid=...`` — full field content for
    one note.

    Used by the conflict drill-in to show what actually differs between the
    local and server copy of a note. Returns ``None`` if the note isn't on the
    server. Runs in a worker thread (does not touch Qt/Anki objects).

    Sends both ``nid`` (preferred — unique per note) and ``guid`` (fallback).
    The server uses nid when present, guid otherwise.
    """
    endpoint = endpoint.rstrip("/")
    # GUIDs and nid go in query params, not the path: Anki GUIDs use a base91
    # alphabet with URL-unsafe chars (/, ?, #, .) that break a path segment
    # even when percent-encoded.
    import urllib.parse
    params = {"guid": guid}
    if nid:
        params["nid"] = str(nid)
    query = urllib.parse.urlencode(params)
    url = f"{endpoint}/sync/inspect/note?{query}"
    header = json.dumps({"v": 11, "k": hkey, "c": "kelma-plugin", "s": ""})
    req = urllib.request.Request(
        url,
        headers={"anki-sync": header, "User-Agent": "Anki (Kelma plugin)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise RuntimeError(f"server returned HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"could not reach {endpoint}: {err.reason}") from err


def write_server_note(hkey: str, endpoint: str, note: dict) -> dict:
    """PUT ``/sync/notes/:guid`` — push a local note to become the server's
    copy ("force local → server").

    ``note`` must carry ``guid``, ``mid``, ``mod``, ``flds``, ``tags`` and a
    ``cards`` list of ``{ord, did}``. The server updates (or creates) the note
    and bumps its USN so the next Anki sync sees the change. Returns the
    server's outcome (action + new usn). Runs in a worker thread.
    """
    endpoint = endpoint.rstrip("/")
    import urllib.parse
    guid = note.get("guid", "")
    url = f"{endpoint}/sync/notes/{urllib.parse.quote(guid, safe='')}"
    header = json.dumps({"v": 11, "k": hkey, "c": "kelma-plugin", "s": ""})
    body = json.dumps({
        "guid": guid,
        "mid": int(note.get("mid", 0)),
        "mod": int(note.get("mod", 0)),
        "flds": note.get("flds", ""),
        "tags": note.get("tags", ""),
        "cards": [
            {"ord": int(c.get("ord", 0)), "did": int(c.get("did", 0))}
            for c in note.get("cards", [])
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "anki-sync": header,
            "User-Agent": "Anki (Kelma plugin)",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"server returned HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"could not reach {endpoint}: {err.reason}") from err


def local_note_detail(col: Collection, nid: int, guid: str = "") -> Optional[dict]:
    """Full field content for one local note. Mirrors the server's
    ``NoteDetail`` shape (including the per-card list) so the diff dialog can
    compare field-by-field and card-by-card.

    Fetches by ``nid`` (unique per note) when available; falls back to ``guid``
    for older manifests that don't carry nid. A guid fetch is ambiguous when
    multiple notes share a guid (notably the empty string).
    """
    if nid:
        row = col.db.first(
            "SELECT id, guid, mid, mod, flds, tags FROM notes WHERE id = ?", nid
        )
    else:
        row = col.db.first(
            "SELECT id, guid, mid, mod, flds, tags FROM notes WHERE guid = ?", guid
        )
    if not row:
        return None
    nid_, guid_, mid, mod_, flds, tags = row
    cards = [
        {"ord": int(ord_), "did": int(did)}
        for ord_, did in col.db.all(
            "SELECT ord, did FROM cards WHERE nid = ? ORDER BY ord", nid_
        )
    ]
    return {
        "guid": guid_,
        "nid": int(nid_),
        "mid": int(mid),
        "mod": int(mod_),
        "flds": flds,
        "tags": tags or "",
        "cards": cards,
    }


# --- Local manifest ----------------------------------------------------------


def build_local_manifest(col: Collection, service: Optional[str] = None) -> dict:
    """Build the same manifest shape as ``/sync/inspect`` from a local
    collection. Uses the same SQL + sha256 hashing so the two sides are
    directly comparable.
    """
    db = col.db

    # Collection meta.
    row = db.first("SELECT mod, scm, usn, ver FROM col") or (0, 0, 0, 0)
    mod_, scm, usn, ver = row

    # Schema 15+ stores decks in a normalized table. Fall back to col.decks
    # only for old collections.
    try:
        deck_names = {
            int(did): name for did, name in db.all("SELECT id, name FROM decks")
        }
    except Exception:
        decks_json = db.scalar("SELECT decks FROM col") or "{}"
        deck_map = json.loads(decks_json)
        deck_names = {}
        for _key, val in deck_map.items():
            did = val.get("id")
            name = val.get("name")
            if did is not None and name:
                deck_names[int(did)] = name

    # Per-deck card counts.
    card_counts = {
        int(did): (cnt or 0)
        for did, cnt in db.all("SELECT did, COUNT(*) FROM cards GROUP BY did")
    }

    # DISTINCT collapses multiple card templates for the same note in one deck,
    # while retaining a note in each deck if its cards span decks.
    deck_hashes = defaultdict(hashlib.sha256)
    deck_note_counts = defaultdict(int)
    deck_mods = defaultdict(int)
    # Per-note deck membership + card counts, keyed by note id (nid), NOT
    # guid. Multiple notes can share a guid (notably the empty string ""),
    # and keying by guid would aggregate their cards into one entry —
    # inflating counts and making the drill-in fetch ambiguous. Nid is
    # unique per note, so each manifest entry carries its own accurate data.
    note_decks = defaultdict(list)
    for did, guid, nmod, nid, flds in db.all(
        "SELECT DISTINCT c.did, n.guid, n.mod, n.id, n.flds "
        "FROM cards c JOIN notes n ON c.nid = n.id "
        "ORDER BY c.did, n.guid, n.id"
    ):
        did = int(did)
        nid = int(nid)
        note_decks[nid].append(did)
        deck_note_counts[did] += 1
        deck_mods[did] = max(deck_mods[did], int(nmod))
        h = deck_hashes[did]
        h.update(guid.encode("utf-8"))
        h.update(b"\x1f")
        h.update(int(nmod).to_bytes(8, "little", signed=True))
        h.update(b"\x1f")
        h.update(flds.encode("utf-8"))
        h.update(b"\x1e")

    # Per-note per-deck card count (NOT distinct — counts every card template).
    # Captures card-template additions/removals that DISTINCT collapses.
    # Keyed by nid so duplicate-guid notes don't aggregate.
    note_cards = defaultdict(lambda: defaultdict(int))
    for nid, did, cnt in db.all(
        "SELECT n.id, c.did, COUNT(*) "
        "FROM cards c JOIN notes n ON c.nid = n.id "
        "GROUP BY n.id, c.did"
    ):
        note_cards[int(nid)][int(did)] = int(cnt)

    # Build deck list, sorted by name.
    decks = []
    for did, name in deck_names.items():
        cnt = card_counts.get(did, 0)
        max_mod = deck_mods.get(did, 0)
        notes = deck_note_counts.get(did, 0)
        h = deck_hashes[did]
        decks.append(
            {
                "id": did,
                "name": name,
                "cards": cnt,
                "notes": notes,
                "mod": max_mod,
                "hash": "sha256:" + h.hexdigest(),
            }
        )
    decks.sort(key=lambda d: d["name"].lower())
    if service is not None:
        # Compare only local decks routed to this service. Server-only decks are
        # intentionally retained on the server side of the diff so stale or
        # remotely-created decks remain visible.
        decks = [
            deck for deck in decks
            if service in config.services_for_deck(deck["name"])
        ]

    # Full notes list (drill-in diff data).
    notes = []
    for nid, guid, mid, nmod, flds in db.all(
        "SELECT id, guid, mid, mod, flds FROM notes ORDER BY guid"
    ):
        nid = int(nid)
        note_deck_ids = note_decks.get(nid, [])
        card_map = note_cards.get(nid, {})
        cards_per_deck = [card_map.get(did, 0) for did in note_deck_ids]
        notes.append(
            {
                "guid": guid,
                "nid": nid,
                "mid": int(mid),
                "mod": int(nmod),
                "decks": note_deck_ids,
                "cards_per_deck": cards_per_deck,
                "hash": "sha256:" + hashlib.sha256(flds.encode("utf-8")).hexdigest(),
                "preview": _note_preview(flds),
            }
        )

    # Anki's Python DB handle is the collection database, not media.db. Keep a
    # neutral summary here instead of accidentally querying a collection table
    # named `meta`; media differences are handled by Anki's media sync surface.
    media = {"usn": 0, "files": 0}

    import time
    return {
        "ts": int(time.time()),
        "mod": mod_,
        "scm": scm,
        "usn": usn,
        "schema": ver,
        "decks": decks,
        "notes": notes,
        "media": media,
    }


# --- Diff --------------------------------------------------------------------


def diff_manifests(local: dict, server: dict) -> list[dict]:
    """Diff two manifests deck-by-deck (keyed by name). Returns a list of
    ``{name, status, local?, server?}`` entries sorted by name.

    Status is one of: ``in-sync``, ``local-newer``, ``server-newer``,
    ``server-only``, ``local-only``, ``conflict``.
    """
    local_by_name = {d["name"]: d for d in local.get("decks", [])}
    server_by_name = {d["name"]: d for d in server.get("decks", [])}
    all_names = set(local_by_name) | set(server_by_name)

    diffs = []
    for name in all_names:
        l = local_by_name.get(name)
        s = server_by_name.get(name)
        if l and s:
            if l["hash"] == s["hash"]:
                diffs.append({"name": name, "status": "in-sync", "local": l, "server": s})
            elif l["mod"] > s["mod"]:
                diffs.append({"name": name, "status": "local-newer", "local": l, "server": s})
            elif s["mod"] > l["mod"]:
                diffs.append({"name": name, "status": "server-newer", "local": l, "server": s})
            else:
                diffs.append({"name": name, "status": "conflict", "local": l, "server": s})
        elif s:
            diffs.append({"name": name, "status": "server-only", "server": s})
        elif l:
            diffs.append({"name": name, "status": "local-only", "local": l})

    diffs.sort(key=lambda d: d["name"].lower())
    return diffs


def diff_deck_notes(local: dict, server: dict, deck_diff: dict) -> list[dict]:
    """Explain a shared deck conflict note-by-note, keyed by note GUID."""
    local_deck = deck_diff.get("local")
    server_deck = deck_diff.get("server")
    if not local_deck or not server_deck:
        return []
    if server.get("notes") and any(
        "decks" not in note or "hash" not in note for note in server["notes"]
    ):
        raise ValueError("server manifest lacks per-note conflict metadata")

    # Group by guid instead of using {guid: note}. Real collections can contain
    # duplicate/empty GUIDs; a dict would silently overwrite all but one note,
    # leaving a deck-level conflict with "0 differing notes". We still match
    # local↔server primarily by guid (the only stable cross-collection key),
    # but preserve duplicate entries and surface leftovers as one-sided notes.
    local_notes_by_guid = defaultdict(list)
    for note in local.get("notes", []):
        if local_deck["id"] in note.get("decks", []):
            local_notes_by_guid[note["guid"]].append(note)
    server_notes_by_guid = defaultdict(list)
    for note in server.get("notes", []):
        if server_deck["id"] in note.get("decks", []):
            server_notes_by_guid[note["guid"]].append(note)

    priority = {
        "conflict": 0,
        "card-count": 0,
        "local-newer": 1,
        "server-newer": 1,
        "deck-count": 0,
        "deck-hash": 0,
        "local-extra": 2,
        "server-extra": 2,
        "local-only": 2,
        "server-only": 2,
        "in-sync": 3,
    }

    def _card_count(note: dict, deck_id: int) -> int:
        """Number of cards this note has in the given deck."""
        decks = note.get("decks", [])
        cpd = note.get("cards_per_deck", [])
        total = 0
        for i, did in enumerate(decks):
            if did == deck_id and i < len(cpd):
                total += cpd[i]
        return total

    out = []
    local_did = local_deck["id"]
    server_did = server_deck["id"]

    def _status(local_note: Optional[dict], server_note: Optional[dict]) -> str:
        if local_note and server_note:
            fields_match = (
                local_note.get("hash") == server_note.get("hash")
                and local_note.get("mod") == server_note.get("mod")
            )
            if fields_match:
                # Fields are identical — but card count in this deck may differ
                # (a card template was added/removed, or a card was moved).
                lc = _card_count(local_note, local_did)
                sc = _card_count(server_note, server_did)
                return "card-count" if lc != sc else "in-sync"
            if local_note.get("mod", 0) > server_note.get("mod", 0):
                return "local-newer"
            if server_note.get("mod", 0) > local_note.get("mod", 0):
                return "server-newer"
            return "conflict"
        if local_note:
            return "local-only"
        return "server-only"

    def _append(
        guid: str,
        local_note: Optional[dict],
        server_note: Optional[dict],
        status_override: Optional[str] = None,
    ) -> None:
        note = local_note or server_note or {}
        out.append(
            {
                "guid": guid,
                "preview": note.get("preview") or "(no preview)",
                "status": status_override or _status(local_note, server_note),
                "local": local_note,
                "server": server_note,
            }
        )

    for guid in set(local_notes_by_guid) | set(server_notes_by_guid):
        locals_ = sorted(local_notes_by_guid.get(guid, []), key=lambda n: n.get("nid", 0))
        servers_ = sorted(server_notes_by_guid.get(guid, []), key=lambda n: n.get("nid", 0))
        had_local = bool(locals_)
        had_server = bool(servers_)

        # Pair notes one-to-one. Prefer exact hash+mod matches, so duplicate GUID
        # groups show only the true extra/missing notes instead of creating
        # artificial conflicts between otherwise identical duplicates.
        while locals_ and servers_:
            local_note = locals_.pop(0)
            match_idx = next(
                (
                    i for i, server_note in enumerate(servers_)
                    if server_note.get("hash") == local_note.get("hash")
                    and server_note.get("mod") == local_note.get("mod")
                ),
                0,
            )
            server_note = servers_.pop(match_idx)
            _append(guid, local_note, server_note)

        for local_note in locals_:
            _append(
                guid,
                local_note,
                None,
                "local-extra" if had_server else "local-only",
            )
        for server_note in servers_:
            _append(
                guid,
                None,
                server_note,
                "server-extra" if had_local else "server-only",
            )
    if out and all(diff["status"] == "in-sync" for diff in out):
        local_cards = int(local_deck.get("cards", 0))
        server_cards = int(server_deck.get("cards", 0))
        local_notes_count = int(local_deck.get("notes", 0))
        server_notes_count = int(server_deck.get("notes", 0))
        if local_cards != server_cards or local_notes_count != server_notes_count:
            out.append(
                {
                    "guid": "",
                    "preview": (
                        "Deck summary mismatch: "
                        f"cards local {local_cards}, server {server_cards}; "
                        f"notes local {local_notes_count}, server {server_notes_count}"
                    ),
                    "status": "deck-count",
                    "local": None,
                    "server": None,
                }
            )
        elif local_deck.get("hash") != server_deck.get("hash"):
            out.append(
                {
                    "guid": "",
                    "preview": "Deck hash differs, but individual note details matched. This can happen with duplicate/empty GUID ordering.",
                    "status": "deck-hash",
                    "local": None,
                    "server": None,
                }
            )
    out.sort(key=lambda d: (priority[d["status"]], d["preview"].lower(), d["guid"]))
    return out


def _note_preview(flds: str) -> str:
    first = flds.split("\x1f", 1)[0]
    plain = html.unescape(re.sub(r"<[^>]*>", "", first))
    collapsed = " ".join(plain.split())
    return collapsed[:120] + ("…" if len(collapsed) > 120 else "")


# --- Per-note resolution actions ---------------------------------------------


def generate_guid(col: Collection, nid: int) -> str:
    """Generate and assign a unique base91 GUID to the local note ``nid``.

    Fixes the root cause of duplicate/empty-GUID problems: Anki's sync and the
    inspect manifest match notes by GUID, and a note with ``guid=""`` can't be
    distinguished from other empty-GUID notes. After assigning a real GUID,
    sync, inspect, and the diff all work correctly.

    Returns the new GUID.
    """
    from anki.utils import guid64
    new_guid = guid64()
    col.db.execute(
        "UPDATE notes SET guid = ? WHERE id = ?", new_guid, nid
    )
    col.flush_scheduler()
    return new_guid


def preview_accept_server(local_note: dict | None, server_note: dict) -> dict:
    """Preview what 'accept server' would change on the local note.

    Returns a dict with ``fields`` (list of {index, old, new}), ``tags``
    ({old, new}), ``cards_added`` (list of ords), ``cards_deleted`` (list of
    ords), ``mod_change`` ({old, new}).
    """
    lf = (local_note or {}).get("flds", "").split("\x1f") if local_note else []
    sf = (server_note or {}).get("flds", "").split("\x1f")
    max_len = max(len(lf), len(sf))
    lf += [""] * (max_len - len(lf))
    sf += [""] * (max_len - len(sf))
    fields = [
        {"index": i, "old": lf[i], "new": sf[i]}
        for i in range(max_len) if lf[i] != sf[i]
    ]
    lt = (local_note or {}).get("tags", "") if local_note else ""
    st = (server_note or {}).get("tags", "")
    local_cards = {int(c["ord"]) for c in (local_note or {}).get("cards", [])} if local_note else set()
    server_cards = {int(c["ord"]) for c in (server_note or {}).get("cards", [])}
    return {
        "fields": fields,
        "tags": {"old": lt, "new": st} if lt != st else None,
        "cards_added": sorted(server_cards - local_cards),
        "cards_deleted": sorted(local_cards - server_cards),
        "mod_change": {
            "old": int((local_note or {}).get("mod", 0)) if local_note else 0,
            "new": int((server_note or {}).get("mod", 0)),
        },
    }

def accept_server_note(col: Collection, nid: int, server_note: dict, deck_id: int = 0) -> dict:
    """Update the local note to match the server's fields, tags, and cards.

    If ``nid`` is 0 (note doesn't exist locally), creates a new note from the
    server's data — this is the whole point of "accept server": pull just
    this note without a blind full sync.

    Card templates (ord) that exist on the server but not locally are created
    via Anki's note-update (which generates cards from the note type's
    templates); extra local cards are deleted. The note's mod is set to the
    server's mod so the next sync doesn't push the local copy back.
    """
    import time
    from anki.utils import guid64

    if nid:
        # Update existing local note.
        note = col.get_note(nid)
        note.fields = (server_note.get("flds") or "").split("\x1f")
        note.tags = (server_note.get("tags") or "").split()
        note.mod = int(server_note.get("mod", 0)) or int(time.time())
        note.flush()
        col.update_note(note)
        return preview_accept_server(local_note_detail(col, nid), server_note)

    # No local note — create one from the server's data.
    mid = int(server_note.get("mid", 0))
    notetype = col.models.get(mid)
    if not notetype:
        raise ValueError(f"note type {mid} not found locally — cannot create note")
    note = col.new_note(notetype)
    note.fields = (server_note.get("flds") or "").split("\x1f")
    note.tags = (server_note.get("tags") or "").split()
    # Use the server's guid so the note matches across syncs. Generate one if
    # the server's guid is empty (the root cause of the whole duplicate mess).
    server_guid = server_note.get("guid") or ""
    note.guid = server_guid if server_guid else guid64()
    target_deck = deck_id or col.decks.get_current_id()
    col.add_note(note, target_deck)
    # Set mod to server's mod so sync sees them as equal.
    note.mod = int(server_note.get("mod", 0)) or int(time.time())
    note.flush()
    return preview_accept_server(local_note_detail(col, note.id), server_note)


def preview_push_local(local_note: dict, server_note: dict | None) -> dict:
    """Preview what 'push to server' would change.

    Pushing to server means: make the local note newer (bump mod), then the
    next sync pushes it. The preview shows what the server will receive.
    """
    lf = (local_note or {}).get("flds", "").split("\x1f")
    sf = (server_note or {}).get("flds", "").split("\x1f") if server_note else []
    max_len = max(len(lf), len(sf))
    lf += [""] * (max_len - len(lf))
    sf += [""] * (max_len - len(sf))
    fields = [
        {"index": i, "old": sf[i], "new": lf[i]}
        for i in range(max_len) if lf[i] != sf[i]
    ]
    local_cards = {int(c["ord"]) for c in (local_note or {}).get("cards", [])}
    server_cards = {int(c["ord"]) for c in (server_note or {}).get("cards", [])} if server_note else set()
    return {
        "fields": fields,
        "tags": {
            "old": (server_note or {}).get("tags", ""),
            "new": (local_note or {}).get("tags", ""),
        } if (server_note or {}).get("tags", "") != (local_note or {}).get("tags", "") else None,
        "cards_added": sorted(local_cards - server_cards),
        "cards_deleted": sorted(server_cards - local_cards),
        "mod_change": {
            "old": int((server_note or {}).get("mod", 0)) if server_note else 0,
            "new": "(bumped to now)",
        },
    }


def push_local_note(col: Collection, nid: int) -> None:
    """Bump the local note's mod to now, so the next sync pushes it to server."""
    import time
    col.db.execute(
        "UPDATE notes SET mod = ? WHERE id = ?", int(time.time()), nid
    )
    col.flush_scheduler()
