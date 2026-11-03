from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from anki.collection import Collection

from . import anki_apply, anki_local, sync_state
from .client import V2Client
from .conflict_policy import newest_side, server_wins_anki_timestamp_truncation

_BATCH_SIZE = 3000


@dataclass
class CardSyncResult:
    pushed: int = 0
    pulled: int = 0
    skipped: int = 0
    study_state_pulled_card_ids: list[int] = field(default_factory=list)
    schedule_reset_pulled_card_ids: list[int] = field(default_factory=list)
    due_date_pulled_card_ids: list[int] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class CardSyncConflict(RuntimeError):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(conflicts)} card conflict(s)")
        self.conflicts = conflicts


def _logical_key(entry: dict) -> str:
    return entry.get("logical_key") or f"{entry.get('note_guid', '')}:{int(entry.get('ord', 0) or 0)}"


def _study_state(entry: dict) -> str:
    return "suspended" if entry.get("study_state") == "suspended" else "active"


def _study_state_side(entry: dict, *, server: bool = False) -> dict:
    timestamp = (
        entry.get("study_state_modified_at", "")
        if server
        else entry.get("study_state_client_modified_at", entry.get("modified_at", ""))
    )
    return {"client_modified_at": timestamp, "modified_at": timestamp}


def _timestamp_millis(value: str) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (OverflowError, TypeError, ValueError):
        return 0


def _iso_from_millis(value: int) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _due_date_side(entry: dict, *, server: bool = False) -> dict:
    timestamp = (
        entry.get("due_date_override_modified_at", "")
        if server
        else entry.get("due_date_override_client_modified_at", "")
    )
    return {"client_modified_at": timestamp, "modified_at": timestamp}


def _anki_due_at_millis(col: Collection, entry: dict) -> int:
    scheduling = entry.get("scheduling") or {}
    queue = int(scheduling.get("queue", 0) or 0)
    if queue not in (2, 3):
        return 0
    collection_day = int(scheduling.get("due", 0) or 0)
    return (int(col.crt) // 86400 + collection_day) * 86_400_000


def _local_due_date_state(
    col: Collection,
    entry: dict,
    baseline: dict,
    completed_at_ms: int,
) -> tuple[int, str]:
    scheduling = entry.get("scheduling") or {}
    current_due = int(scheduling.get("due", 0) or 0)
    modified_at = str(entry.get("modified_at", ""))
    modified_ms = _timestamp_millis(modified_at)
    max_review_id = int(entry.get("max_review_id", 0) or 0)
    max_manual_id = int(entry.get("max_manual_review_id", 0) or 0)
    baseline_due = int(baseline.get("dueAtMillis", 0) or 0)
    baseline_modified = str(baseline.get("modifiedAt", ""))
    baseline_anki_due = int(baseline.get("ankiDue", 0) or 0)
    baseline_review_id = int(baseline.get("maxReviewId", 0) or 0)
    baseline_manual_id = int(baseline.get("maxManualReviewId", 0) or 0)
    is_reset = (
        int(scheduling.get("type", 0) or 0) == 0
        and int(scheduling.get("queue", 0) or 0) == 0
    )

    rated_after_baseline = max_review_id > max(baseline_review_id, completed_at_ms)
    manual_after_baseline = max_manual_id > max(baseline_manual_id, completed_at_ms)
    if rated_after_baseline and max_review_id > max_manual_id:
        return 0, _iso_from_millis(max_review_id)
    if manual_after_baseline and max_manual_id > max_review_id and not is_reset:
        return _anki_due_at_millis(col, entry), _iso_from_millis(max_manual_id)
    if baseline:
        if current_due == baseline_anki_due:
            return baseline_due, baseline_modified
        # Older Anki versions may change due without a manual revlog row.
        if not is_reset and modified_ms > completed_at_ms and modified_ms > max_review_id:
            return _anki_due_at_millis(col, entry), modified_at
        return 0, _iso_from_millis(max(max_review_id, modified_ms)) or baseline_modified
    if completed_at_ms and not is_reset and modified_ms > completed_at_ms and modified_ms > max_review_id:
        return _anki_due_at_millis(col, entry), modified_at
    return 0, ""


def _looks_like_interrupted_server_pull(local: dict, server: dict) -> bool:
    """Detect a pristine Default card generated while an upstream pull aborted.

    Applying a new server note makes Anki generate a card in deck id 1 with a
    current timestamp. If a later note conflict aborts before card sync, the
    next pass must not treat that generated timestamp as an intentional move
    that outranks the existing server card.
    """
    if int(local.get("deck_id", 0) or 0) != 1:
        return False
    if str(server.get("deck_name", "")).casefold() == str(
        local.get("deck_name", "")
    ).casefold():
        return False
    scheduling = local.get("scheduling") or {}
    if any(
        int(scheduling.get(field, 0) or 0) != 0
        for field in ("type", "queue", "ivl", "reps", "lapses")
    ):
        return False
    note_id = int(local.get("note_id", 0) or 0)
    card_id = int(local.get("card_id", 0) or 0)
    try:
        modified = int(
            datetime.fromisoformat(
                str(local.get("modified_at", "")).replace("Z", "+00:00")
            ).timestamp()
        )
    except (TypeError, ValueError):
        return False
    return (
        note_id > 0
        and card_id > 0
        and abs(note_id // 1000 - modified) <= 1
        and abs(card_id // 1000 - modified) <= 1
    )


def sync_cards_once(
    col: Collection,
    client: V2Client,
    server_manifest: dict | None = None,
    progress=None,
    deck_names: list[str] | None = None,
    prefer_server: bool = False,
    newest_wins: bool = False,
    accept_server_timestamp_truncation: bool = False,
    server_authoritative_keys: set[str] | None = None,
    scheduling_mode: str = "server_fsrs",
) -> CardSyncResult:
    use_server_scheduling = scheduling_mode != "local_anki"
    if progress:
        progress("Cards: building local card manifest…")
    local = {_logical_key(x): x for x in anki_local.card_manifest(col, deck_names=deck_names)}
    try:
        saved_state = sync_state.load_state(col)
        reset_baseline = saved_state.get("cardScheduleResets", {})
        due_date_baseline = saved_state.get("cardDueDateOverrides", {})
        completed_at_ms = _timestamp_millis(saved_state.get("completedAt", ""))
    except (AttributeError, OSError):
        reset_baseline = {}
        due_date_baseline = {}
        completed_at_ms = 0
    if server_manifest is None:
        server_manifest = client.manifest()
    server = {_logical_key(x): x for x in server_manifest.get("cards", [])}
    server_authoritative_keys = server_authoritative_keys or set()
    utc_now = server_manifest.get("server_time")
    result = CardSyncResult()
    keys = sorted(set(local) | set(server))
    total = len(keys)
    if progress:
        progress(f"Cards: planning {total} cards by logical identity…")
    local_only: list[int] = []
    server_pull_ids: list[int] = []  # card_ids to batch-pull
    local_state_ids: set[int] = set()
    server_state_ids: set[int] = set()
    local_reset_ids: set[int] = set()
    server_reset_ids: set[int] = set()
    local_due_date_ids: set[int] = set()
    server_due_date_ids: set[int] = set()
    for idx, key in enumerate(keys, 1):
        if progress and (idx == 1 or idx == total or idx % _BATCH_SIZE == 0):
            progress(f"Cards plan {idx}/{total} · to push {len(local_only)}, to pull {len(server_pull_ids)}, in sync {result.skipped}, conflicts {len(result.conflicts)}")
        l = local.get(key)
        s = server.get(key)
        local_reset = 0
        if l:
            baseline = reset_baseline.get(key) or {}
            local_reset = max(
                int(l.get("schedule_reset_through_review_id", 0) or 0),
                int(baseline.get("throughReviewId", 0) or 0),
            )
            l["effective_schedule_reset_through_review_id"] = local_reset
        server_reset = int((s or {}).get("schedule_reset_through_review_id", 0) or 0)
        if l and s and local_reset != server_reset:
            if local_reset > server_reset:
                local_reset_ids.add(int(l["card_id"]))
            elif int(l.get("max_review_id", 0) or 0) <= server_reset:
                server_reset_ids.add(int(s["card_id"]))
        elif l and not s and local_reset > 0:
            local_reset_ids.add(int(l["card_id"]))
        elif s and not l and server_reset > 0:
            server_reset_ids.add(int(s["card_id"]))
        local_due_date = 0
        if l:
            local_due_date, local_due_modified = _local_due_date_state(
                col, l, due_date_baseline.get(key) or {}, completed_at_ms,
            )
            l["effective_due_date_override_ms"] = local_due_date
            l["due_date_override_client_modified_at"] = local_due_modified
        server_due_date = int((s or {}).get("due_date_override_ms", 0) or 0)
        if l and s and local_due_date != server_due_date:
            local_due_side = _due_date_side(l)
            server_due_side = _due_date_side(s, server=True)
            due_winner = newest_side(local_due_side, server_due_side, utc_now=utc_now)
            if due_winner is None:
                local_stamp = _timestamp_millis(local_due_side["modified_at"])
                server_stamp = _timestamp_millis(server_due_side["modified_at"])
                if local_stamp <= 0 < server_stamp:
                    due_winner = "server"
                elif server_stamp <= 0 < local_stamp:
                    due_winner = "local"
            if due_winner == "local":
                local_due_date_ids.add(int(l["card_id"]))
            elif due_winner == "server":
                server_due_date_ids.add(int(s["card_id"]))
        elif l and not s and local_due_date > 0:
            local_due_date_ids.add(int(l["card_id"]))
        elif s and not l and server_due_date > 0:
            server_due_date_ids.add(int(s["card_id"]))
        if l and s and _study_state(l) != _study_state(s):
            state_winner = newest_side(
                _study_state_side(l), _study_state_side(s, server=True), utc_now=utc_now,
            )
            if state_winner == "local":
                local_state_ids.add(int(l["card_id"]))
            elif state_winner == "server":
                server_state_ids.add(int(s["card_id"]))
        elif l and not s:
            local_state_ids.add(int(l["card_id"]))
        elif s and not l:
            server_state_ids.add(int(s["card_id"]))
        if prefer_server:
            # Pulling notes into a fresh collection generates local cards in the
            # stock Default deck before card records are restored. They are not
            # competing edits: apply the server card to set its real deck and
            # scheduling, and ignore generated cards absent from the server.
            if s:
                server_pull_ids.append(int(s["card_id"]))
            else:
                result.skipped += 1
            continue
        if key in server_authoritative_keys:
            # This card did not exist locally before upstream notes/notetypes
            # were pulled. Anki just generated a pristine card with the current
            # wall-clock mod time; that is not a local edit and must never
            # overwrite the upstream card's deck or scheduling.
            if s:
                server_pull_ids.append(int(s["card_id"]))
            else:
                result.skipped += 1
            continue
        if l and s:
            if l.get("checksum") != s.get("checksum"):
                if newest_wins and _looks_like_interrupted_server_pull(l, s):
                    server_pull_ids.append(int(s["card_id"]))
                    continue
                # KelmaSync-only clients have two canonical sources, so a
                # structural move with a clear timestamp direction can use the same newest-wins
                # rule as scheduling. Ties/unknowns remain explicit conflicts.
                winner = (
                    newest_side(l, s, utc_now=utc_now) if newest_wins else None
                )
                if (
                    winner is None
                    and accept_server_timestamp_truncation
                    and server_wins_anki_timestamp_truncation(l, s)
                ):
                    winner = "server"
                if winner == "server":
                    server_pull_ids.append(int(s["card_id"]))
                elif winner == "local":
                    local_only.append(int(l["card_id"]))
                else:
                    result.conflicts.append({"card_id": int(l["card_id"]), "server": s, "client": l})
                continue
            # Same structure: scheduling is newest-wins by UTC source time when
            # this client follows server FSRS. In local Anki mode, keep rslib's
            # local scheduling and ignore server projection-only card updates.
            if not use_server_scheduling:
                result.skipped += 1
                continue
            winner = newest_side(l, s, utc_now=utc_now)
            if winner == "local":
                local_only.append(int(l["card_id"]))  # push local scheduling
            elif winner == "server":
                server_pull_ids.append(int(s["card_id"]))
            else:
                result.skipped += 1
            continue
        if l and not s:
            local_only.append(int(l["card_id"]))
            continue
        if s:
            # Server-only card: batch-pull and apply if the card exists locally.
            server_pull_ids.append(int(s["card_id"]))

    local_state_keys = {
        _logical_key(item)
        for item in local.values()
        if int(item.get("card_id", 0) or 0) in local_state_ids
    }
    # Batch-pull server cards instead of one HTTP request per card.
    all_server_pull_ids = list(dict.fromkeys(
        server_pull_ids + sorted(server_state_ids) + sorted(server_reset_ids) +
        sorted(server_due_date_ids)
    ))
    if all_server_pull_ids:
        if progress:
            progress(f"Cards: pulling {len(all_server_pull_ids)} server cards in {_BATCH_SIZE}-item batches…")
        for start in range(0, len(all_server_pull_ids), _BATCH_SIZE):
            chunk = all_server_pull_ids[start:start + _BATCH_SIZE]
            resp = client.batch_pull(cards=chunk)
            for record in resp.get("cards", []):
                try:
                    server_id = int(record.get("card_id", 0) or 0)
                    local_won_state = _logical_key(record) in local_state_keys
                    apply_state = not local_won_state and (
                        server_id in server_state_ids
                        or _study_state(local.get(_logical_key(record), {})) == _study_state(record)
                    )
                    apply_reset = server_id in server_reset_ids
                    apply_due_date = server_id in server_due_date_ids
                    applied_id = anki_apply.apply_card(
                        col,
                        record,
                        apply_scheduling=use_server_scheduling,
                        apply_study_state=apply_state,
                        apply_schedule_reset=apply_reset,
                        apply_due_date_override=apply_due_date,
                    )
                    if apply_state and server_id in server_state_ids and applied_id:
                        result.study_state_pulled_card_ids.append(int(applied_id))
                    if apply_reset and applied_id:
                        result.schedule_reset_pulled_card_ids.append(int(applied_id))
                    if apply_due_date and applied_id:
                        result.due_date_pulled_card_ids.append(int(applied_id))
                    result.pulled += 1
                except Exception:
                    result.skipped += 1
            if progress:
                progress(
                    f"Cards: pulled {min(start + _BATCH_SIZE, len(all_server_pull_ids))}/"
                    f"{len(all_server_pull_ids)}…"
                )
    all_local_ids = list(dict.fromkeys(
        local_only + sorted(local_state_ids) + sorted(local_reset_ids) +
        sorted(local_due_date_ids)
    ))
    if all_local_ids:
        if progress:
            progress(f"Cards: pushing {len(all_local_ids)} cards in {_BATCH_SIZE}-item batches…")
        total_batches = (len(all_local_ids) + _BATCH_SIZE - 1) // _BATCH_SIZE
        for batch_idx, start in enumerate(range(0, len(all_local_ids), _BATCH_SIZE), 1):
            chunk = all_local_ids[start:start + _BATCH_SIZE]
            if progress:
                progress(f"Cards: sending batch {batch_idx}/{total_batches} ({len(chunk)} cards)…")
            payload_cards = []
            for cid in chunk:
                rec = anki_local.card_record(col, cid)
                if rec:
                    payload = {
                        "card_id": cid,
                        "note_guid": rec["note_guid"],
                        "deck_name": rec["deck_name"],
                        "ord": rec["ord"],
                        "scheduling": rec["scheduling"],
                        "client_modified_at": rec["client_modified_at"],
                    }
                    if cid in local_state_ids:
                        payload["study_state"] = rec["study_state"]
                        payload["study_state_client_modified_at"] = rec[
                            "study_state_client_modified_at"
                        ]
                    local_entry = local.get(_logical_key(rec), {})
                    if cid in local_reset_ids:
                        payload["schedule_reset_through_review_id"] = int(
                            local_entry.get("effective_schedule_reset_through_review_id", 0) or 0
                        )
                        payload["schedule_reset_client_modified_at"] = rec[
                            "schedule_reset_client_modified_at"
                        ]
                    if cid in local_due_date_ids:
                        payload["due_date_override_ms"] = int(
                            local_entry.get("effective_due_date_override_ms", 0) or 0
                        )
                        payload["due_date_override_client_modified_at"] = local_entry.get(
                            "due_date_override_client_modified_at", rec["client_modified_at"]
                        )
                    payload_cards.append(payload)
            resp = client.batch_push({"notes": [], "cards": payload_cards, "notetypes": [], "decks": []})
            result.pushed += int((resp.get("accepted") or {}).get("cards", 0))
            if progress:
                progress(
                    f"Cards batch {batch_idx}/{total_batches} complete · "
                    f"{min(start + _BATCH_SIZE, len(all_local_ids))}/{len(all_local_ids)} sent · "
                    f"pushed {result.pushed}"
                )

    if result.conflicts:
        if progress:
            progress(f"Cards: {len(result.conflicts)} conflict(s)")
        raise CardSyncConflict(result.conflicts)
    if progress:
        progress(f"Cards complete: pushed {result.pushed}, pulled {result.pulled}, skipped {result.skipped}")
    return result
