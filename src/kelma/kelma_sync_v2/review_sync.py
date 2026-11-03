"""Append-only Anki review-history and portable daily-counter sync."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from anki.collection import Collection

from . import anki_local
from .checksum_rs import review_checksum
from .client import V2Client

_BATCH_SIZE = 3000
_DAILY_FIELDS = (
    ("newToday", "new_studied"),
    ("revToday", "review_studied"),
    ("lrnToday", "learning_studied"),
    ("timeToday", "milliseconds_studied"),
)


@dataclass
class ReviewSyncResult:
    pushed: int = 0
    pulled: int = 0
    skipped: int = 0
    study_days_pushed: int = 0
    study_days_applied: int = 0
    study_days_marked_pending: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class ReviewSyncConflict(RuntimeError):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(conflicts)} review-history conflict(s)")
        self.conflicts = conflicts


def _chunks(values: list[Any], size: int = _BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _review_rows(
    col: Collection, deck_names: list[str] | None = None
) -> list[tuple[Any, ...]]:
    where = ""
    params: list[int] = []
    if deck_names is not None:
        dids = anki_local._deck_ids_for_names(col, deck_names)
        if not dids:
            return []
        placeholders = ",".join("?" for _ in dids)
        where = f"WHERE c.did IN ({placeholders}) OR c.odid IN ({placeholders})"
        params = [*dids, *dids]
    return list(col.db.all(
        f"""
        SELECT r.id, r.cid, r.ease, r.ivl, r.lastIvl, r.factor, r.time,
               r.type, COALESCE(n.guid, ''), COALESCE(c.ord, 0),
               COALESCE(d.name, '')
        FROM revlog r
        LEFT JOIN cards c ON c.id = r.cid
        LEFT JOIN notes n ON n.id = c.nid
        LEFT JOIN decks d ON d.id = CASE WHEN c.odid != 0 THEN c.odid ELSE c.did END
        {where}
        ORDER BY r.id
        """,
        *params,
    ))


def _row_record(row: tuple[Any, ...]) -> dict[str, Any]:
    (
        review_id, source_card_id, ease, interval, last_interval, factor,
        taken_millis, review_kind, note_guid, card_ord, deck_name,
    ) = row
    return {
        "review_id": int(review_id),
        "source_card_id": int(source_card_id),
        "note_guid": str(note_guid or ""),
        "card_ord": int(card_ord or 0),
        "deck_name": str(deck_name or ""),
        "ease": int(ease or 0),
        "interval": int(interval or 0),
        "last_interval": int(last_interval or 0),
        "factor": int(factor or 0),
        "taken_millis": int(taken_millis or 0),
        "review_kind": int(review_kind or 0),
    }


def _record_checksum(record: dict[str, Any]) -> str:
    return review_checksum(
        str(record.get("note_guid") or ""),
        int(record.get("card_ord", 0) or 0),
        int(record.get("ease", 0) or 0),
        int(record.get("interval", 0) or 0),
        int(record.get("last_interval", 0) or 0),
        int(record.get("factor", 0) or 0),
        int(record.get("taken_millis", 0) or 0),
        int(record.get("review_kind", 0) or 0),
    )


def local_review_records(
    col: Collection, deck_names: list[str] | None = None
) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for row in _review_rows(col, deck_names=deck_names):
        record = _row_record(row)
        record["checksum"] = _record_checksum(record)
        records[record["review_id"]] = record
    return records


def _review_card_map(col: Collection) -> tuple[dict[tuple[str, int], int], set[int]]:
    rows = col.db.all(
        "SELECT n.guid, c.ord, c.id FROM cards c JOIN notes n ON n.id=c.nid"
    )
    by_identity = {(str(guid), int(card_ord)): int(cid) for guid, card_ord, cid in rows}
    return by_identity, set(by_identity.values())


def _apply_review(
    col: Collection,
    record: dict[str, Any],
    card_ids: dict[tuple[str, int], int],
    occupied_card_ids: set[int],
    *,
    incoming_usn: int = 0,
) -> bool:
    review_id = int(record.get("review_id", 0) or 0)
    if review_id <= 0:
        return False

    note_guid = str(record.get("note_guid") or "")
    card_ord = int(record.get("card_ord", 0) or 0)
    cid = card_ids.get((note_guid, card_ord), 0) if note_guid else 0
    if not cid:
        source_cid = int(record.get("source_card_id", 0) or 0)
        if source_cid and source_cid not in occupied_card_ids:
            cid = source_cid
        else:
            # Preserve orphaned review history without attaching it to an
            # unrelated local card whose timestamp id happened to collide.
            basis = source_cid or review_id
            cid = -abs(basis)

    col.db.execute(
        """
        INSERT OR IGNORE INTO revlog
          (id,cid,usn,ease,ivl,lastIvl,factor,time,type)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        review_id,
        cid,
        incoming_usn,
        int(record.get("ease", 0) or 0),
        int(record.get("interval", 0) or 0),
        int(record.get("last_interval", 0) or 0),
        int(record.get("factor", 0) or 0),
        int(record.get("taken_millis", 0) or 0),
        int(record.get("review_kind", 0) or 0),
    )
    return True


def _counter_pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return int(value[0] or 0), int(value[1] or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def local_study_days(
    col: Collection, deck_names: list[str] | None = None
) -> list[dict[str, Any]]:
    allowed = set(deck_names or [])

    def in_scope(name: str) -> bool:
        return deck_names is None or name in allowed or any(
            name.startswith(parent + "::") for parent in allowed
        )

    crt_day = int(col.crt) // 86400
    out: list[dict[str, Any]] = []
    for deck in col.decks.all():
        name = str(deck.get("name") or "")
        if not name or not in_scope(name):
            continue
        pairs = {
            wire: _counter_pair(deck.get(local))
            for local, wire in _DAILY_FIELDS
        }
        valid = [pair for pair in pairs.values() if pair is not None]
        if not valid:
            continue
        local_day = max(pair[0] for pair in valid)
        record: dict[str, Any] = {
            "day": crt_day + local_day,
            "deck_name": name,
        }
        for _local, wire in _DAILY_FIELDS:
            pair = pairs[wire]
            record[wire] = pair[1] if pair and pair[0] == local_day else 0
        out.append(record)
    return out


def _study_day_key(record: dict[str, Any]) -> tuple[int, str]:
    return int(record.get("day", 0) or 0), str(record.get("deck_name") or "")


def _merge_study_day(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> dict[str, Any]:
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)
    if _study_day_key(left) != _study_day_key(right):
        return dict(max((left, right), key=lambda item: _study_day_key(item)[0]))
    merged = dict(left)
    for _local, wire in _DAILY_FIELDS:
        merged[wire] = max(
            int(left.get(wire, 0) or 0), int(right.get(wire, 0) or 0)
        )
    return merged


def _apply_study_day(
    col: Collection,
    record: dict[str, Any],
    *,
    mark_pending_for_ankiweb: bool = False,
) -> tuple[bool, bool]:
    name = str(record.get("deck_name") or "")
    if not name:
        return False, False
    deck = col.decks.by_name(name)
    if not deck:
        return False, False
    local_day = int(record.get("day", 0) or 0) - int(col.crt) // 86400

    changed = False
    for local, wire in _DAILY_FIELDS:
        incoming = int(record.get(wire, 0) or 0)
        current = _counter_pair(deck.get(local))
        if current != (local_day, incoming):
            deck[local] = [local_day, incoming]
            changed = True
    already_pending = int(deck.get("usn", 0) or 0) == -1
    needs_write = changed or (mark_pending_for_ankiweb and not already_pending)
    if not needs_write:
        return False, mark_pending_for_ankiweb

    if mark_pending_for_ankiweb:
        # Native AnkiWeb sync only sends deck counters when the deck is pending.
        # The three-way plugin records a signature after successful publication
        # so unchanged counters are not re-marked on every comparison.
        deck["mod"] = int(time.time())
        deck["usn"] = -1
    else:
        # Kelma-only clients should not turn synchronized bookkeeping into a
        # structural deck edit.
        deck["mod"] = deck.get("mod", 0)
        deck["usn"] = deck.get("usn", 0)
    col.decks.update(deck, preserve_usn=True)
    return changed, mark_pending_for_ankiweb


def _clear_review_usns(col: Collection, review_ids: list[int]) -> None:
    for chunk in _chunks(review_ids):
        placeholders = ",".join("?" for _ in chunk)
        col.db.execute(
            f"UPDATE revlog SET usn=0 WHERE usn=-1 AND id IN ({placeholders})",
            *chunk,
        )


def sync_reviews_once(
    col: Collection,
    client: V2Client,
    server_manifest: dict[str, Any],
    *,
    deck_names: list[str] | None = None,
    clear_pending_usn: bool = False,
    mark_study_days_pending: bool = False,
    progress=None,
) -> ReviewSyncResult:
    """Union local/server revlogs, then converge portable same-day counters."""
    result = ReviewSyncResult()
    # Presence is the protocol capability marker. This lets upgraded clients
    # remain read-compatible while a self-hosted server is still being updated.
    if "reviews" not in server_manifest:
        if progress:
            progress("Reviews: server does not advertise review-history sync yet")
        return result

    if progress:
        progress("Reviews: building local append-only history manifest…")
    local = local_review_records(col, deck_names=deck_names)
    server = {
        int(item.get("review_id", 0) or 0): item
        for item in server_manifest.get("reviews", [])
        if int(item.get("review_id", 0) or 0) > 0
    }

    shared = sorted(set(local) & set(server))
    for review_id in shared:
        server_checksum = str(server[review_id].get("checksum") or "")
        # Anki keeps revlog rows after card deletion. Such an orphan no longer
        # has a local GUID, so it cannot reproduce the portable checksum; the
        # server's previously-known immutable identity remains authoritative.
        local_is_orphan = not str(local[review_id].get("note_guid") or "")
        if (
            not local_is_orphan
            and server_checksum
            and server_checksum != local[review_id]["checksum"]
        ):
            result.conflicts.append({
                "review_id": review_id,
                "server": server[review_id],
                "client": local[review_id],
            })
        else:
            result.skipped += 1
    if result.conflicts:
        raise ReviewSyncConflict(result.conflicts)

    pull_ids = sorted(set(server) - set(local))
    if pull_ids and progress:
        progress(f"Reviews: pulling {len(pull_ids)} missing history row(s)…")
    card_ids, occupied_card_ids = _review_card_map(col)
    incoming_usn = 0 if clear_pending_usn else -1
    for chunk in _chunks(pull_ids):
        response = client.batch_pull(reviews=chunk)
        for record in response.get("reviews", []):
            if _apply_review(
                col,
                record,
                card_ids,
                occupied_card_ids,
                incoming_usn=incoming_usn,
            ):
                result.pulled += 1
        if progress:
            progress(f"Reviews: downloaded {result.pulled}/{len(pull_ids)} history row(s)")
    # Make downloaded history durable before a potentially large first upload.
    # This also lets the three-way Anki plugin immediately publish pulled rows
    # to AnkiWeb (incoming_usn=-1) even if the Kelma backfill later fails.
    if result.pulled:
        col.save()

    push_ids = sorted(set(local) - set(server))
    if push_ids and progress:
        progress(f"Reviews: uploading {len(push_ids)} missing history row(s)…")
    accepted_ids: list[int] = list(shared) if clear_pending_usn else []
    for chunk in _chunks(push_ids):
        payload = [
            {key: value for key, value in local[review_id].items() if key != "checksum"}
            for review_id in chunk
        ]
        response = client.batch_push({
            "notes": [], "cards": [], "reviews": payload, "study_days": [],
            "notetypes": [], "decks": [],
        })
        conflicts = (response.get("conflicts") or {}).get("reviews") or []
        if conflicts:
            raise ReviewSyncConflict(list(conflicts))
        accepted = int((response.get("accepted") or {}).get("reviews", 0) or 0)
        if accepted != len(chunk):
            raise RuntimeError(
                f"server accepted {accepted}/{len(chunk)} review-history rows"
            )
        result.pushed += accepted
        if progress:
            progress(f"Reviews: uploaded {result.pushed}/{len(push_ids)} history row(s)")
        if clear_pending_usn:
            accepted_ids.extend(chunk)
    if accepted_ids:
        _clear_review_usns(col, accepted_ids)

    # Daily limits use deck counters, not revlog rows. Publish their portable
    # epoch-day form and merge the server's monotonic snapshots locally.
    local_days = local_study_days(col, deck_names=deck_names)
    meaningful_days = [
        record for record in local_days
        if any(int(record.get(wire, 0) or 0) != 0 for _local, wire in _DAILY_FIELDS)
    ]
    for chunk in _chunks(meaningful_days, 1000):
        response = client.batch_push({
            "notes": [], "cards": [], "reviews": [], "study_days": chunk,
            "notetypes": [], "decks": [],
        })
        accepted = int((response.get("accepted") or {}).get("study_days", 0) or 0)
        if accepted != len(chunk):
            raise RuntimeError(
                f"server accepted {accepted}/{len(chunk)} daily study snapshots"
            )
        result.study_days_pushed += accepted

    combined: dict[tuple[int, str], dict[str, Any]] = {}
    for record in [*server_manifest.get("study_days", []), *local_days]:
        key = _study_day_key(record)
        if not key[1]:
            continue
        combined[key] = _merge_study_day(combined.get(key), record)
    latest_by_deck: dict[str, dict[str, Any]] = {}
    for record in combined.values():
        name = str(record.get("deck_name") or "")
        current = latest_by_deck.get(name)
        latest_by_deck[name] = _merge_study_day(current, record)
    for record in latest_by_deck.values():
        applied, marked_pending = _apply_study_day(
            col,
            record,
            mark_pending_for_ankiweb=mark_study_days_pending,
        )
        if applied:
            result.study_days_applied += 1
        if marked_pending:
            result.study_days_marked_pending += 1
    if result.study_days_applied or result.study_days_marked_pending:
        col.save()

    if progress:
        progress(
            f"Reviews complete: pushed {result.pushed}, pulled {result.pulled}, "
            f"in sync {result.skipped}; daily counters applied {result.study_days_applied}, "
            f"queued for AnkiWeb {result.study_days_marked_pending}"
        )
    return result
