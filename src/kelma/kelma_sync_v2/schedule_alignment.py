"""Re-place cards that already hold a server FSRS schedule on the study days.

Older add-on versions converted FSRS due dates through UTC days, so a review
done in the evening (or before the study day's start hour) landed a day later
than in Kelma Review. This pass fixes cards already in the collection, once per
study-day policy. It touches only cards whose review state is exactly the
server's projection and changes their `due` (and day-learning queue), not their
modification time, so nothing looks like a local edit to the next sync.
"""
from __future__ import annotations

from typing import Any

from .study_days import StudyDayMapper

_BATCH = 3000
CONFIG_KEY = "kelmaStudyDayAlignment"


def _same_review_state(local: tuple, scheduling: dict[str, Any]) -> bool:
    _cid, typ, _queue, _due, ivl, reps, lapses, data, _odid = local
    return (int(typ), int(ivl), int(reps), int(lapses), str(data or "")) == (
        int(scheduling.get("type", 0) or 0),
        int(scheduling.get("ivl", 0) or 0),
        int(scheduling.get("reps", 0) or 0),
        int(scheduling.get("lapses", 0) or 0),
        str(scheduling.get("data", "") or ""),
    )


def realign_projected_cards(
    col,
    client,
    server_manifest: dict[str, Any],
    mapper: StudyDayMapper,
    progress=None,
) -> int:
    local = {
        f"{guid}:{int(ord_)}": (cid, typ, queue, due, ivl, reps, lapses, data, odid)
        for cid, guid, ord_, typ, queue, due, ivl, reps, lapses, data, odid in col.db.all(
            "SELECT c.id, n.guid, c.ord, c.type, c.queue, c.due, c.ivl, c.reps, c.lapses, c.data, c.odid "
            "FROM cards c JOIN notes n ON n.id = c.nid"
        )
    }
    ids = [int(card["card_id"]) for card in server_manifest.get("cards", []) if card.get("card_id") is not None]
    updates: list[tuple[int, int, int]] = []
    for start in range(0, len(ids), _BATCH):
        if progress:
            progress(f"Study days: checking {min(start + _BATCH, len(ids))}/{len(ids)} cards…")
        response = client.batch_pull(cards=ids[start:start + _BATCH]) or {}
        for record in response.get("cards", []) or []:
            scheduling = record.get("scheduling") or {}
            aligned = mapper.anki_due(scheduling)
            if aligned is None:
                continue
            row = local.get(f"{record.get('note_guid', '')}:{int(record.get('ord', 0) or 0)}")
            if row is None or row[8] or not _same_review_state(row, scheduling):
                continue
            cid, queue, due = row[0], int(row[2]), int(row[3])
            new_queue, new_due = aligned
            if queue < 0:
                if new_queue != 2:
                    continue  # suspended/buried learning card: leave it as it is
                new_queue = queue  # keep it suspended/buried
            if (queue, due) != (new_queue, new_due):
                updates.append((new_queue, new_due, cid))
    if updates:
        col.db.executemany("UPDATE cards SET queue=?, due=? WHERE id=?", updates)
    return len(updates)
