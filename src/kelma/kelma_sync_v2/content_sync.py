"""Content sync orchestration: notetypes first, then notes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anki.collection import Collection

from .client import V2Client
from .card_sync import CardSyncConflict, CardSyncResult, sync_cards_once
from .deck_sync import DeckSyncConflict, DeckSyncResult, sync_decks_once
from .media_sync import MediaSyncResult, sync_media_once
from .notetype_sync import NotetypeSyncConflict, NotetypeSyncResult, sync_notetypes_once
from .note_sync import NoteSyncConflict, NoteSyncResult, sync_notes_once
from .review_sync import ReviewSyncConflict, ReviewSyncResult, sync_reviews_once
from .tombstone_sync import TombstoneSyncResult, apply_tombstones
from . import anki_local, sync_state


@dataclass
class ContentSyncResult:
    tombstones: TombstoneSyncResult
    local_deletes: dict[str, list[str]] = field(default_factory=dict)
    decks: DeckSyncResult = None  # type: ignore[assignment]
    notetypes: NotetypeSyncResult = None  # type: ignore[assignment]
    notes: NoteSyncResult = None  # type: ignore[assignment]
    cards: CardSyncResult = None  # type: ignore[assignment]
    reviews: ReviewSyncResult = None  # type: ignore[assignment]
    media: MediaSyncResult = None  # type: ignore[assignment]
    server_time: str = ""


class ContentSyncConflict(RuntimeError):
    def __init__(self, resource: str, conflicts: list[dict]) -> None:
        super().__init__(f"{len(conflicts)} {resource} conflict(s)")
        self.resource = resource
        self.conflicts = conflicts


class DeletionSafetyError(RuntimeError):
    def __init__(self, deletes: dict[str, list[str]]) -> None:
        self.deletes = deletes
        total = sum(len(values) for values in deletes.values())
        breakdown = ", ".join(f"{len(values)} {kind}" for kind, values in deletes.items())
        super().__init__(
            f"Refusing to delete {total} KelmaSync resources ({breakdown}) without explicit approval"
        )


def _chunks(xs: list, n: int = 3000):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _card_logical_key(item: dict[str, Any]) -> str:
    key = str(item.get("logical_key") or "")
    if key:
        return key
    guid = str(item.get("note_guid") or "")
    if not guid:
        return ""
    return f"{guid}:{int(item.get('ord', 0) or 0)}"


def _server_used_notetype_ids(
    client: V2Client, manifest: dict[str, Any], progress=None
) -> set[int]:
    notes = list(manifest.get("notes", []))
    if not notes:
        return set()
    if all(note.get("notetype_id") is not None for note in notes):
        return {int(note["notetype_id"]) for note in notes}

    # Compatibility with older/self-hosted servers whose note manifest predates
    # notetype_id. Pull in 3,000-note batches only for structural scoping.
    if progress:
        progress("Notetypes: resolving server note types in 3,000-note batches…")
    out: set[int] = set()
    guids = [str(note["guid"]) for note in notes if note.get("guid")]
    for chunk in _chunks(guids):
        for note in client.batch_pull(notes=chunk).get("notes", []):
            if note.get("notetype_id") is not None:
                out.add(int(note["notetype_id"]))
    return out


def _safe_fresh_or_interrupted_restore(
    local_notes: list[dict[str, Any]],
    local_cards: list[dict[str, Any]],
    server_manifest: dict[str, Any],
) -> bool:
    """True when local content is empty or an untouched partial server pull.

    A failed first restore may have already pulled notes, which generates new
    cards in Anki's Default deck before canonical card records are applied. We
    can resume server-authoritatively only when every local note matches a
    server checksum, every card identity exists on the server, and every card
    is still pristine/unreviewed. Any local-only note, field edit, or review
    disables this shortcut and falls back to normal conflict protection.
    """
    if not local_notes and not local_cards:
        return True
    server_notes = {
        str(item.get("guid", "")): str(item.get("checksum", ""))
        for item in server_manifest.get("notes", [])
    }
    if any(
        server_notes.get(str(note.get("guid", "")))
        != str(note.get("checksum", ""))
        for note in local_notes
    ):
        return False
    server_cards = {
        str(
            item.get("logical_key")
            or f"{item.get('note_guid', '')}:{int(item.get('ord', 0) or 0)}"
        )
        for item in server_manifest.get("cards", [])
    }
    if any(str(card.get("logical_key", "")) not in server_cards for card in local_cards):
        return False
    return all(
        int((card.get("scheduling") or {}).get(field, 0) or 0) == 0
        for card in local_cards
        for field in ("type", "queue", "ivl", "reps", "lapses")
    )


def _scope_server_manifest_to_decks(client: V2Client, manifest: dict[str, Any], deck_names: list[str] | None, progress=None) -> dict[str, Any]:
    """Filter server manifest to the deck picker scope.

    Server note manifest entries don't include deck membership, so derive scope
    from full server cards (card -> deck_name + note_guid), then keep only notes
    referenced by scoped cards. ``deck_names=None`` means unscoped (all decks).
    """
    if deck_names is None:
        # Unscoped: build logical keys from manifest data without pulling cards.
        for m in manifest.get("cards", []):
            if "logical_key" not in m:
                guid = str(m.get("note_guid", ""))
                ord_ = int(m.get("ord", 0) or 0)
                m["logical_key"] = f"{guid}:{ord_}"
        return manifest

    allowed = set(deck_names)

    def _in_scope(deck: str) -> bool:
        # Match the deck itself OR any of its subdecks ("Parent::Child"),
        # consistent with local deck scoping which is prefix-based.
        if deck in allowed:
            return True
        return any(deck.startswith(name + "::") for name in allowed)

    manifest_cards = list(manifest.get("cards", []))
    scoped_card_ids: set[int] = set()
    scoped_note_guids: set[str] = set()

    # Current manifests carry deck_name + logical card identity. Use those cheap
    # fields to identify scope before pulling full scheduling records: selecting
    # a few Kelma decks must not download all 92,000 Anki cards merely to learn
    # their deck assignments. Older servers fall back to the full scan.
    has_manifest_decks = all(
        not card.get("card_id") or bool(card.get("deck_name"))
        for card in manifest_cards
    )
    if has_manifest_decks:
        candidates = [
            card for card in manifest_cards
            if card.get("card_id") and _in_scope(str(card.get("deck_name", "")))
        ]
        card_ids = [int(card["card_id"]) for card in candidates]
        scoped_card_ids.update(card_ids)
        scoped_note_guids.update(
            str(card["note_guid"])
            for card in candidates
            if card.get("note_guid")
        )
    else:
        card_ids = [
            int(card["card_id"])
            for card in manifest_cards
            if card.get("card_id")
        ]

    if card_ids:
        if progress:
            progress(
                f"Server scope: loading {len(card_ids)} selected card record(s) "
                f"from {len(manifest_cards)} total…"
            )
        done = 0
        for chunk in _chunks(card_ids):
            pulled = client.batch_pull(cards=chunk).get("cards", [])
            by_id = {int(c.get("card_id")): c for c in pulled if c.get("card_id")}
            for c in pulled:
                deck = str(c.get("deck_name", ""))
                if _in_scope(deck):
                    scoped_card_ids.add(int(c.get("card_id")))
                    guid = c.get("note_guid")
                    if guid:
                        scoped_note_guids.add(str(guid))
            # Enrich manifest entries with stable logical card identity and
            # scheduling. card_id differs across clients; (note_guid, ord) is
            # the cross-device key.
            for m in manifest_cards:
                cid = int(m.get("card_id", 0) or 0)
                c = by_id.get(cid)
                if c:
                    m["note_guid"] = c.get("note_guid") or m.get("note_guid") or ""
                    m["ord"] = int(c.get("ord") or m.get("ord") or 0)
                    m["deck_name"] = c.get("deck_name") or m.get("deck_name") or ""
                    m["scheduling"] = dict(c.get("scheduling") or {})
                    m["logical_key"] = f"{m['note_guid']}:{m['ord']}"
            done += len(chunk)
            if progress:
                progress(
                    f"Server scope: {done}/{len(card_ids)} selected cards loaded"
                )
    scoped = dict(manifest)
    scoped["cards"] = [c for c in manifest.get("cards", []) if int(c.get("card_id", 0)) in scoped_card_ids]
    scoped["notes"] = [n for n in manifest.get("notes", []) if str(n.get("guid", "")) in scoped_note_guids]
    scoped["decks"] = [d for d in manifest.get("decks", []) if _in_scope(str(d.get("name", "")))]
    scoped["reviews"] = [
        review for review in manifest.get("reviews", [])
        if _in_scope(str(review.get("deck_name", "")))
    ]
    scoped["study_days"] = [
        day for day in manifest.get("study_days", [])
        if _in_scope(str(day.get("deck_name", "")))
    ]
    # Server note manifests don't carry notetype_id, so notetype scoping is
    # handled by the caller (which has local notetype IDs from scoped notes).
    return scoped


def _tombstones_for_snapshot(manifest: dict[str, Any], snapshot: dict[str, Any]) -> list[dict]:
    """Only apply tombstones for resources this exact scoped client knew."""
    known = {
        "note": set(snapshot.get("notes", [])),
        "card": set(snapshot.get("cards", [])),
        "notetype": set(snapshot.get("notetypes", [])),
        "deck": set(snapshot.get("decks", [])),
    }
    return [
        tombstone
        for tombstone in manifest.get("tombstones", [])
        if tombstone.get("type") in known
        and str(tombstone.get("resource_id", "")) in known[tombstone["type"]]
    ]


def _push_local_deletes(col: Collection, client: V2Client, deletes: dict[str, list[str]], progress=None) -> None:
    """Push scoped local deletions in transactional 3,000-item batches."""
    total = sum(len(v) for v in deletes.values())
    done = 0
    if progress:
        progress(f"Deletes: pushing {total} approved local tombstone(s) in batches…")
    resources = (
        ("notes", deletes.get("notes", [])),
        ("cards", [int(value) for value in deletes.get("cards", [])]),
        ("notetypes", [int(value) for value in deletes.get("notetypes", [])]),
        ("decks", deletes.get("decks", [])),
    )
    for resource, values in resources:
        for chunk in _chunks(list(values), 3000):
            payload = {"notes": [], "cards": [], "notetypes": [], "decks": []}
            payload[resource] = chunk
            client.batch_delete(**payload)
            done += len(chunk)
            if progress:
                progress(f"Deletes: {done}/{total} complete ({resource} batch of {len(chunk)})")


def _limit_deletes_to_scoped_server(
    deletes: dict[str, list[str]], manifest: dict[str, Any]
) -> dict[str, list[str]]:
    """Never delete an identifier outside the currently scoped server view."""
    server_keys = {
        "notes": {str(item.get("guid", "")) for item in manifest.get("notes", [])},
        "cards": {str(item.get("card_id", "")) for item in manifest.get("cards", [])},
        "notetypes": {str(item.get("notetype_id", "")) for item in manifest.get("notetypes", [])},
        "decks": {str(item.get("name", "")) for item in manifest.get("decks", [])},
    }
    return {
        resource: sorted(set(values) & server_keys[resource])
        for resource, values in deletes.items()
        if resource in server_keys and set(values) & server_keys[resource]
    }


def sync_content_once(
    col: Collection,
    client: V2Client,
    *,
    since: str | None = None,
    deck_name: str | None = None,
    deck_names: list[str] | None = None,
    apply_note_pulls: bool = True,
    allow_large_deletes: bool = False,
    newest_wins: bool = False,
    accept_server_timestamp_truncation: bool = False,
    scheduling_mode: str = "server_fsrs",
    progress=None,
) -> ContentSyncResult:
    """Run one content sync pass.

    Order:
      1. apply server tombstones locally
      2. detect local deletes (compare to last snapshot) and push DELETEs
      3. sync decks → notetypes → notes → cards → media
      4. save new snapshot
    """
    if progress:
        progress("Phase 1/10: fetching full server manifest for checksum comparison…")
    # IMPORTANT: checksum planning requires a full server manifest. If we pass
    # `since`, unchanged server rows are omitted and look local-only, causing
    # needless re-sends even when checksums match. Incremental sync can only be
    # reintroduced after the local snapshot stores checksums per resource.
    manifest = client.manifest()
    if deck_name and not deck_names:
        deck_names = [deck_name]
    if deck_names is not None:
        if progress:
            progress(f"Scoping server manifest to {len(deck_names)} Kelma deck(s)…")
        manifest = _scope_server_manifest_to_decks(client, manifest, deck_names, progress=progress)
    # Tombstones and local delete detection are valid only against the exact
    # routing scope that produced the previous snapshot. A route change resets
    # the baseline instead of deleting data in either direction.
    snapshot = sync_state.load_state(col)
    current_scope = sorted(set(deck_names or ([deck_name] if deck_name else [])))
    previous_scope = snapshot.get("scope")
    snapshot_version = snapshot.get("version", 0)
    scope_matches = (
        isinstance(snapshot_version, int) and snapshot_version >= 2 and previous_scope == current_scope
    )
    if scope_matches:
        manifest = dict(manifest)
        manifest["tombstones"] = _tombstones_for_snapshot(manifest, snapshot)
    else:
        manifest = dict(manifest)
        manifest["tombstones"] = []
        if progress and any(snapshot.get(key) for key in ("notes", "cards", "notetypes", "decks")):
            progress("Deletes: routing scope changed (or old snapshot found); resetting deletion baseline without deleting data")

    if progress:
        progress(
            f"Server manifest: {len(manifest.get('notes', []))} notes, "
            f"{len(manifest.get('cards', []))} cards, {len(manifest.get('notetypes', []))} notetypes, "
            f"{len(manifest.get('decks', []))} decks, {len(manifest.get('reviews', []))} reviews, "
            f"{len(manifest.get('media', []))} media"
        )
        progress("Phase 2/10: applying scoped server tombstones…")
    tombstones = apply_tombstones(col, manifest)
    if progress:
        progress(f"Tombstones complete: applied {tombstones.applied}")
        progress("Phase 3/10: previous scoped snapshot loaded")

    # Repair local duplicate generated cards before building manifests. This
    # prevents invalid duplicate cards/blank-GUID duplicate notes from being
    # counted or pushed forever.
    anki_local.repair_duplicate_cards(col, deck_names=deck_names, progress=progress)
    if progress:
        progress("Phase 4/10: building local key snapshot…")
    local_note_manifest = anki_local.note_manifest(col, deck_names=deck_names, progress=progress)
    if progress:
        progress(f"Snapshot: {len(local_note_manifest)} local notes")
    local_card_manifest = anki_local.card_manifest(col, deck_names=deck_names)
    if progress:
        progress(f"Snapshot: {len(local_card_manifest)} local cards")
    # Remember which logical cards existed before any upstream notes or
    # notetypes are applied. Anki generates cards for pulled notes with a fresh
    # local mod timestamp; those generated cards are not local edits and the
    # corresponding server records must remain authoritative this pass.
    local_card_keys_before_pulls = {
        key
        for item in local_card_manifest
        if (key := _card_logical_key(item))
    }
    server_card_keys = {
        key
        for item in manifest.get("cards", [])
        if (key := _card_logical_key(item))
    }
    server_authoritative_card_keys = (
        server_card_keys - local_card_keys_before_pulls
    )
    used_notetype_ids = {int(n["notetype_id"]) for n in local_note_manifest}
    server_used_notetype_ids = _server_used_notetype_ids(
        client, manifest, progress=progress
    )
    # Include only types used by local or server notes. This excludes unrelated
    # stock scaffolding and unused server types that do not round-trip through
    # Anki until content actually references them.
    compared_notetype_ids = used_notetype_ids | server_used_notetype_ids
    local_notetype_manifest = anki_local.notetype_manifest(
        col, notetype_ids=compared_notetype_ids
    )
    if progress:
        progress(f"Snapshot: {len(local_notetype_manifest)} local notetypes")
    local_deck_manifest = anki_local.deck_manifest(col, deck_names=deck_names)
    if progress:
        progress(f"Snapshot: {len(local_deck_manifest)} local decks")
    # Deletion detection must consider all locally existing notetypes, even
    # unused ones. Publishing/comparison above remains scoped to used types so
    # stock scaffolding is never uploaded.
    all_local_notetype_ids = {
        str(item["notetype_id"]) for item in anki_local.notetype_manifest(col)
    }
    local_keys = {
        "notes": {x["guid"] for x in local_note_manifest},
        "cards": {str(x["card_id"]) for x in local_card_manifest},
        "notetypes": all_local_notetype_ids,
        "decks": {x["name"] for x in local_deck_manifest},
    }
    snapshot_is_empty = not any(
        snapshot.get(kind) for kind in ("notes", "cards", "notetypes", "decks")
    )
    fresh_restore = bool(
        deck_names is None
        and snapshot_is_empty
        and (manifest.get("notes") or manifest.get("cards"))
        and _safe_fresh_or_interrupted_restore(
            local_note_manifest, local_card_manifest, manifest
        )
    )
    if fresh_restore and progress:
        progress(
            "Fresh KelmaDesktop collection detected: restoring the server copy "
            "without treating unused stock structure as local edits"
        )
    if progress:
        progress("Phase 5/10: detecting local deletes…")
    local_deletes = (
        sync_state.compute_local_deletes(snapshot, local_keys) if scope_matches else {}
    )
    # Never echo an incoming server deletion back as a newly detected local
    # deletion. A note tombstone also removes its generated local cards.
    applied_by_server = {
        "notes": tombstones.applied_resources.get("note", set()),
        "cards": tombstones.applied_resources.get("card", set()),
        "notetypes": tombstones.applied_resources.get("notetype", set()),
        "decks": tombstones.applied_resources.get("deck", set()),
    }
    local_deletes = {
        resource: sorted(set(values) - applied_by_server.get(resource, set()))
        for resource, values in local_deletes.items()
        if set(values) - applied_by_server.get(resource, set())
    }
    local_deletes = _limit_deletes_to_scoped_server(local_deletes, manifest)
    delete_total = sum(len(values) for values in local_deletes.values())
    content_deletes = len(local_deletes.get("notes", [])) + len(local_deletes.get("cards", []))
    known_content = len(snapshot.get("notes", [])) + len(snapshot.get("cards", []))
    structural_deletes = bool(local_deletes.get("decks") or local_deletes.get("notetypes"))
    disproportionate = content_deletes > max(10, int(known_content * 0.10))
    needs_approval = delete_total > 100 or structural_deletes or disproportionate
    if needs_approval and not allow_large_deletes:
        raise DeletionSafetyError(local_deletes)
    if local_deletes:
        _push_local_deletes(col, client, local_deletes, progress=progress)
        if progress:
            progress("Deletes changed server state; refreshing manifest…")
        manifest = client.manifest()
        if deck_names is not None:
            manifest = _scope_server_manifest_to_decks(client, manifest, deck_names, progress=progress)
    elif progress:
        progress("Deletes: none")

    result = ContentSyncResult(tombstones=tombstones, local_deletes=local_deletes)

    try:
        if progress:
            progress("Phase 6/10: syncing decks…")
        result.decks = sync_decks_once(
            col,
            client,
            manifest,
            progress=progress,
            deck_names=deck_names,
            prefer_server=fresh_restore,
            newest_wins=newest_wins,
            accept_server_timestamp_truncation=accept_server_timestamp_truncation,
        )
    except DeckSyncConflict as e:
        raise ContentSyncConflict("deck", e.conflicts) from e
    try:
        if progress:
            progress("Phase 7/10: syncing notetypes…")
        result.notetypes = sync_notetypes_once(
            col,
            client,
            manifest,
            apply_pulls=True,
            progress=progress,
            notetype_ids=compared_notetype_ids,
            prefer_server=fresh_restore,
            newest_wins=newest_wins,
            accept_server_timestamp_truncation=accept_server_timestamp_truncation,
        )
    except NotetypeSyncConflict as e:
        raise ContentSyncConflict("notetype", e.conflicts) from e
    try:
        if progress:
            progress("Phase 8/10: syncing notes…")
        result.notes = sync_notes_once(
            col,
            client,
            since=since,
            apply_pulls=apply_note_pulls,
            deck_name=deck_name,
            deck_names=deck_names,
            server_manifest=manifest,
            newest_wins=newest_wins,
            accept_server_timestamp_truncation=accept_server_timestamp_truncation,
            progress=progress,
        )
    except NoteSyncConflict as e:
        raise ContentSyncConflict("note", e.conflicts) from e
    if progress:
        progress("Phase 9/10: syncing cards…")
    try:
        result.cards = sync_cards_once(
            col,
            client,
            manifest,
            progress=progress,
            deck_names=deck_names,
            prefer_server=fresh_restore,
            newest_wins=newest_wins,
            accept_server_timestamp_truncation=accept_server_timestamp_truncation,
            server_authoritative_keys=server_authoritative_card_keys,
            scheduling_mode=scheduling_mode,
        )
    except CardSyncConflict as e:
        raise ContentSyncConflict("card", e.conflicts) from e
    try:
        if progress:
            progress("Phase 10/10: syncing full review history and daily limits…")
        result.reviews = sync_reviews_once(
            col,
            client,
            manifest,
            deck_names=deck_names,
            clear_pending_usn=newest_wins,
            progress=progress,
        )
    except ReviewSyncConflict as e:
        raise ContentSyncConflict("review history", e.conflicts) from e
    if progress:
        progress("Final phase: syncing media…")
    result.media = sync_media_once(
        col,
        client,
        manifest,
        progress=progress,
        deck_names=deck_names or ([deck_name] if deck_name else None),
    )
    result.server_time = result.notes.server_time or manifest.get("server_time", "")

    # Save the post-sync collection, not the pre-pull keys. This is essential on
    # a fresh restore: otherwise the next sync sees every restored row as new
    # and the deletion baseline remains empty.
    if progress:
        progress("Refreshing post-sync snapshot…")
    final_notes = anki_local.note_manifest(
        col, deck_names=deck_names, progress=progress
    )
    final_cards = anki_local.card_manifest(col, deck_names=deck_names)
    final_notetype_ids = {int(note["notetype_id"]) for note in final_notes}
    final_notetypes = anki_local.notetype_manifest(
        col, notetype_ids=final_notetype_ids
    )
    all_final_notetype_ids = {
        str(item["notetype_id"]) for item in anki_local.notetype_manifest(col)
    }
    server_notetype_ids = {
        str(item.get("notetype_id")) for item in manifest.get("notetypes", [])
    }
    final_decks = anki_local.deck_manifest(col, deck_names=deck_names)
    final_keys = {
        "notes": {str(item["guid"]) for item in final_notes},
        "cards": {str(item["card_id"]) for item in final_cards},
        # Track used/published types plus existing types known by this server;
        # omit unrelated stock types so they can never become server deletes.
        "notetypes": (
            {str(item["notetype_id"]) for item in final_notetypes}
            | (all_final_notetype_ids & server_notetype_ids)
        ),
        "decks": {str(item["name"]) for item in final_decks},
    }
    if progress:
        progress("Saving sync snapshot…")
    final_server_manifest = client.manifest()
    if deck_names is not None:
        final_server_manifest = _scope_server_manifest_to_decks(
            client, final_server_manifest, deck_names, progress=progress
        )
    card_schedule_resets = {
        f"{item.get('note_guid', '')}:{int(item.get('ord', 0) or 0)}": {
            "throughReviewId": int(item.get("schedule_reset_through_review_id", 0) or 0),
            "modifiedAt": item.get("schedule_reset_modified_at", ""),
        }
        for item in final_server_manifest.get("cards", [])
        if item.get("note_guid")
    }
    final_local_cards_by_key = {
        f"{item.get('note_guid', '')}:{int(item.get('ord', 0) or 0)}": item
        for item in final_cards
    }
    card_due_date_overrides = {
        key: {
            "dueAtMillis": int(item.get("due_date_override_ms", 0) or 0),
            "modifiedAt": item.get("due_date_override_modified_at", ""),
            "ankiDue": int(
                (final_local_cards_by_key.get(key, {}).get("scheduling") or {}).get("due", 0) or 0
            ),
            "maxReviewId": int(
                final_local_cards_by_key.get(key, {}).get("max_review_id", 0) or 0
            ),
            "maxManualReviewId": int(
                final_local_cards_by_key.get(key, {}).get("max_manual_review_id", 0) or 0
            ),
        }
        for item in final_server_manifest.get("cards", [])
        if item.get("note_guid")
        for key in [f"{item.get('note_guid', '')}:{int(item.get('ord', 0) or 0)}"]
    }
    new_state = sync_state.build_state(
        notes=sorted(final_keys["notes"]),
        cards=sorted(final_keys["cards"]),
        notetypes=sorted(final_keys["notetypes"]),
        decks=sorted(final_keys["decks"]),
        scope=current_scope,
        card_schedule_resets=card_schedule_resets,
        card_due_date_overrides=card_due_date_overrides,
        completed_at=final_server_manifest.get("server_time", ""),
    )
    sync_state.save_state(col, new_state)

    if progress:
        progress("Sync complete.")
    return result
