"""Propagate per-card scheduling between the master and a service shadow.

Why this exists: the reconcile copies decks with Anki's apkg importer, which
matches cards by (note, template ordinal) and *skips* any card that already
exists — its source carries a literal `// TODO: could update existing card`. So
when you review a card on one device, the note content still merges by GUID, but
the card's scheduling state (queue / due / interval / ease / reps / lapses /
FSRS data) is never carried to the matching card on the other side. Reviews
silently don't sync.

This module fills that gap. For the given decks it pairs cards across the two
collections by (note GUID, template ordinal) — the stable identity, since the
importer assigns each collection its own card ids — and, newest-mtime-wins,
copies the scheduling columns from src to dst, marking the dst card pending
(usn=-1) so the shadow's native sync uploads it.

Date correctness: review and day-learn `due`/`odue` values are stored as days
since each collection's creation (`crt`). When the two collections were created
at different times they must be shifted by the crt delta — exactly what the
importer does for newly-added cards. When the crt values match (the common case
for one account), the delta is 0 and this is a plain copy. Cards sitting in a
filtered deck (`odid != 0`) on either side are left untouched: their odue
bookkeeping is fragile and filtered decks are transient.
"""

from __future__ import annotations

import time

from anki.collection import Collection


def _deck_dids(col: Collection, name: str) -> list[int]:
    prefix = name + "::"
    return [
        d.id
        for d in col.decks.all_names_and_ids()
        if d.name == name or d.name.startswith(prefix)
    ]


def _all_dids(col: Collection, names: list[str]) -> list[int]:
    out: set[int] = set()
    for name in names:
        out.update(_deck_dids(col, name))
    return list(out)


def _shift_due(due: int, queue: int, ctype: int, delta_days: int) -> int:
    """Adjust a due value for the crt delta. Date-based dues (review / day-learn,
    in days since crt) shift; position-based (new) and intraday-learn (epoch
    seconds) dues are absolute and don't."""
    if delta_days == 0:
        return due
    if queue in (2, 3):  # review, day (re)learn — days since crt
        return due + delta_days
    if queue < 0 and ctype == 2:  # suspended/buried review — days since crt
        return due + delta_days
    return due


def _shift_odue(odue: int, delta_days: int) -> int:
    """odue holds a saved review due (days since crt) for a (re)learning card; 0
    otherwise. Shift only the non-zero (date-based) case."""
    if delta_days == 0 or odue == 0:
        return odue
    return odue + delta_days


def sync_scheduling(src: Collection, dst: Collection, deck_names: list[str]) -> int:
    """Copy newer scheduling between decks with the same names."""
    return sync_scheduling_between_decks(src, dst, deck_names, deck_names)


def sync_scheduling_between_decks(
    src: Collection,
    dst: Collection,
    src_deck_names: list[str],
    dst_deck_names: list[str],
) -> int:
    """Copy newer scheduling across explicitly designated deck namespaces.

    Cards are paired by note GUID and template ordinal, so source and target
    deck names may differ.
    """
    if not src_deck_names or not dst_deck_names:
        return 0
    src_dids = _all_dids(src, src_deck_names)
    dst_dids = _all_dids(dst, dst_deck_names)
    if not src_dids or not dst_dids:
        return 0

    src_crt = src.db.scalar("select crt from col") or 0
    dst_crt = dst.db.scalar("select crt from col") or 0
    # Round to the nearest whole day, don't floor: collection crt sits at the
    # rollover hour, but two crt dates can straddle a DST change, so the raw
    # second delta is a whole number of days ± up to an hour. Flooring would be
    # off by one in one direction (e.g. -905 on push but +904 on pull), shifting
    # pulled review cards a day wrong. Rounding is exact and symmetric here.
    delta_days = round((int(src_crt) - int(dst_crt)) / 86400)

    dph = ",".join("?" * len(dst_dids))
    # (guid, ord) -> (card_id, mod, odid)
    dst_index: dict[tuple[str, int], tuple[int, int, int]] = {}
    for cid, guid, ord_, mod, odid in dst.db.all(
        f"select c.id, n.guid, c.ord, c.mod, c.odid from cards c "
        f"join notes n on c.nid = n.id where c.did in ({dph})",
        *dst_dids,
    ):
        dst_index[(guid, ord_)] = (cid, mod, odid)

    sph = ",".join("?" * len(src_dids))
    updates: list[tuple] = []
    for (
        guid,
        ord_,
        smod,
        stype,
        squeue,
        sdue,
        sivl,
        sfactor,
        sreps,
        slapses,
        sleft,
        sodue,
        sodid,
        sflags,
        sdata,
    ) in src.db.all(
        f"select n.guid, c.ord, c.mod, c.type, c.queue, c.due, c.ivl, c.factor, "
        f"c.reps, c.lapses, c.left, c.odue, c.odid, c.flags, c.data "
        f"from cards c join notes n on c.nid = n.id where c.did in ({sph})",
        *src_dids,
    ):
        entry = dst_index.get((guid, ord_))
        if entry is None:
            continue  # only on src — the apkg import already adds it
        dcid, dmod, dodid = entry
        if smod <= dmod:
            continue  # dst is newer or equal — don't clobber
        if sodid or dodid:
            continue  # filtered deck on either side — leave its odue alone
        updates.append(
            (
                stype,
                squeue,
                _shift_due(sdue, squeue, stype, delta_days),
                sivl,
                sfactor,
                sreps,
                slapses,
                sleft,
                _shift_odue(sodue, delta_days),
                sflags,
                sdata,
                smod,
                dcid,
            )
        )

    if not updates:
        return 0

    dst.db.executemany(
        "update cards set type=?, queue=?, due=?, ivl=?, factor=?, reps=?, "
        "lapses=?, left=?, odue=?, flags=?, data=?, mod=?, usn=-1 where id=?",
        updates,
    )
    # Mark the collection changed so the shadow's native sync uploads the cards.
    dst.db.execute("update col set mod=?", int(time.time() * 1000))
    return len(updates)
