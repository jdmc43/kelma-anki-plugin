"""Deletion propagation between the master and a service shadow.

Package import only adds/updates notes — it never deletes — so deletions must be
replayed separately. The obvious source is Anki's `graves` table (it records every
note/card deletion as its object id), and that's what this module used to key on.

But grave oids are *ids*, and ids are NOT stable across the master and a shadow:
the shadow's copy of a note (matched by GUID) routinely carries a different note/
card id than the master's — because the server/mobile assigns its own ids and the
apkg importer renumbers on seed. So an id-keyed replay either no-ops (the src's
deleted id doesn't exist in dst, so real deletions silently never sync) or, worse,
collides with an *unrelated* dst object that happens to share that id and deletes
the wrong note. It also can't recover a deleted object's GUID (it's already gone),
so there's no way to translate the grave back to the stable identity.

So we don't use graves. Instead we diff **note GUIDs** — the one identity that is
stable across collections — against a snapshot of the GUIDs that were present on
*both* sides at the last converged sync (see `shadowsync`). A GUID that was in that
snapshot and is now missing from one side was genuinely deleted there, and is
removed from the other side. A safety cap refuses to act if the diff is
implausibly large (a corrupt/stale snapshot, or a whole deck un-routed), so a bad
snapshot can never cascade into mass deletion.
"""

from __future__ import annotations

from anki.collection import Collection


def _all_dids(col: Collection, deck_names: list[str]) -> list[int]:
    """Deck ids for the named decks and their subdecks, within `col`."""
    out: set[int] = set()
    for name in deck_names:
        prefix = name + "::"
        for d in col.decks.all_names_and_ids():
            if d.name == name or d.name.startswith(prefix):
                out.add(d.id)
    return list(out)


def routed_guids(col: Collection, deck_names: list[str]) -> set[str]:
    """Distinct note GUIDs that have a card in one of the routed decks, within
    `col`. This is the stable cross-collection identity set we diff on."""
    dids = _all_dids(col, deck_names)
    if not dids:
        return set()
    ph = ",".join("?" * len(dids))
    return set(
        col.db.list(
            f"select distinct guid from notes where id in "
            f"(select nid from cards where did in ({ph}))",
            *dids,
        )
    )


def remove_guids(
    col: Collection,
    guids: set[str],
    deck_names: list[str],
    cap_frac: float = 0.25,
    cap_min: int = 100,
) -> tuple[int, str | None]:
    """Delete notes in `col`'s routed decks whose GUID is in `guids`.

    Returns `(removed, skip_reason)`. Safety-capped: if the number of notes that
    would be removed exceeds `max(cap_min, cap_frac * routed_note_count)`, nothing
    is deleted and a human-readable reason is returned instead — so a corrupt or
    stale snapshot (or a deck the user just un-routed) can never wipe the server.
    """
    if not guids:
        return 0, None
    dids = _all_dids(col, deck_names)
    if not dids:
        return 0, None
    ph = ",".join("?" * len(dids))
    # (note_id, guid) for every note in the routed decks — filter in Python to
    # avoid a multi-thousand-variable SQL IN clause on the GUID set.
    rows = col.db.all(
        f"select n.id, n.guid from notes n where n.id in "
        f"(select nid from cards where did in ({ph}))",
        *dids,
    )
    target = [nid for nid, guid in rows if guid in guids]
    if not target:
        return 0, None
    cap = max(cap_min, int(len(rows) * cap_frac))
    if len(target) > cap:
        return 0, (
            f"{len(target)} deletions exceed safety cap {cap} "
            f"(of {len(rows)} routed notes) — snapshot looks stale, skipping"
        )
    col.remove_notes(target)
    return len(target), None
