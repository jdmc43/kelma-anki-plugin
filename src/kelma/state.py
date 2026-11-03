"""Persistent sync state, kept in `kelma_state.json` next to the collection.

Two jobs:

1. **Change detection** — a cheap per-deck "fingerprint" (card count + newest
   card mod + newest note mod) lets us skip reconciling decks that haven't changed
   since the last sync, instead of re-exporting every routed deck every time.
2. **Last-sync info** — remembered per service for the Sync button's details.

The file is best-effort: a missing/corrupt file just means "everything looks
changed" (a full reconcile), never an error.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from anki.collection import Collection
from aqt import mw

from . import paths

Fingerprint = list  # [card_count, max_card_mod, max_note_mod]


def _path() -> str:
    return os.path.join(paths.profile_dir(), "kelma_state.json")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - any problem => empty state
        return {}


def save(state: dict) -> None:
    try:
        path = _path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as err:  # noqa: BLE001 - persistence is best-effort
        # Never crash the caller, but do NOT hide the failure: a silently
        # missing state file makes every card look like a pending local change.
        print(f"Kelma state save failed: {err}")


# -- per-service deck baselines ----------------------------------------------
def service_decks(state: dict, service: str) -> dict:
    return state.setdefault("decks", {}).setdefault(service, {})


# -- last-sync metadata ------------------------------------------------------
def mark_synced(state: dict, service: str, path: str, col: Collection | None = None) -> None:
    meta = state.setdefault("meta", {}).setdefault(service, {})
    meta["at"] = int(time.time())
    meta["path"] = path
    if col is not None:
        # A wall-clock cutoff is not enough: pulled cards preserve the source's
        # mod time, which can be newer than an old/stale sync timestamp. Capture
        # the exact per-deck collection state at convergence instead.
        meta["card_baseline"] = {
            str(int(did)): [int(max_id or 0), int(max_mod or 0)]
            for did, max_id, max_mod in col.db.all(
                "SELECT did, max(id), max(mod) FROM cards GROUP BY did"
            )
        }


def last_sync(service: str) -> Optional[dict]:
    return load().get("meta", {}).get(service)


# -- fingerprints ------------------------------------------------------------
def _deck_dids(col: Collection, name: str) -> list[int]:
    prefix = name + "::"
    return [
        d.id
        for d in col.decks.all_names_and_ids()
        if d.name == name or d.name.startswith(prefix)
    ]


def _fingerprint_dids(col: Collection, dids: list[int]) -> Fingerprint:
    if not dids:
        return [0, 0, 0]
    ph = ",".join("?" * len(dids))
    count = col.db.scalar(f"select count(*) from cards where did in ({ph})", *dids) or 0
    cmod = col.db.scalar(f"select max(mod) from cards where did in ({ph})", *dids) or 0
    nmod = (
        col.db.scalar(
            f"select max(n.mod) from notes n where n.id in "
            f"(select nid from cards where did in ({ph}))",
            *dids,
        )
        or 0
    )
    return [count, cmod, nmod]


def fingerprint(col: Collection, deck_name: str) -> Fingerprint:
    """Cheap change signature for one deck (including its subdecks)."""
    return _fingerprint_dids(col, _deck_dids(col, deck_name))


def _name_dids_map(col: Collection, deck_names: list[str]) -> dict[str, list[int]]:
    all_decks = [(d.name, d.id) for d in col.decks.all_names_and_ids()]
    out: dict[str, list[int]] = {}
    for name in deck_names:
        prefix = name + "::"
        out[name] = [
            did for dn, did in all_decks if dn == name or dn.startswith(prefix)
        ]
    return out


def pending_for_service(
    col: Collection, deck_names: list[str], service: str
) -> dict[str, tuple[int, int]]:
    """Per-deck card changes since this service's exact collection baseline."""
    pending = pending_by_did(col, service)
    out: dict[str, tuple[int, int]] = {}
    for name, dids in _name_dids_map(col, deck_names).items():
        out[name] = (
            sum(pending.get(d, (0, 0))[0] for d in dids),
            sum(pending.get(d, (0, 0))[1] for d in dids),
        )
    return out


def pending_by_did(col: Collection, service: str) -> dict[int, tuple[int, int]]:
    """Per-deck-id changes since the last exact synchronized collection state."""
    meta = last_sync(service) or {}
    raw_baseline = meta.get("card_baseline") or {}
    baseline = {
        int(did): (int(values[0]), int(values[1]))
        for did, values in raw_baseline.items()
        if isinstance(values, list) and len(values) >= 2
    }
    if baseline:
        values_sql = ",".join("(?,?,?)" for _ in baseline)
        args = [value for did, marks in baseline.items() for value in (did, marks[0], marks[1])]
        out = {
            int(did): (int(added or 0), int(changed or 0))
            for did, added, changed in col.db.all(
                f"""
                WITH baseline(did, max_id, max_mod) AS (VALUES {values_sql})
                SELECT c.did,
                       sum(CASE WHEN c.id > b.max_id THEN 1 ELSE 0 END),
                       sum(CASE WHEN c.id <= b.max_id AND c.mod > b.max_mod THEN 1 ELSE 0 END)
                FROM cards c JOIN baseline b ON b.did = c.did
                GROUP BY c.did
                """,
                *args,
            )
        }
        marks = ",".join("?" for _ in baseline)
        for did, count in col.db.all(
            f"SELECT did, count(*) FROM cards WHERE did NOT IN ({marks}) GROUP BY did",
            *baseline.keys(),
        ):
            out[int(did)] = (int(count), 0)
        return out

    # Backward-compatible fallback until the first successful sync migrates an
    # old state file to exact per-deck baselines.
    last_sec = int(meta.get("at") or 0)
    last_ms = last_sec * 1000
    out: dict[int, list[int]] = {}
    for did, cnt in col.db.all(
        "select did, count(*) from cards where id > ? group by did", last_ms
    ):
        out[int(did)] = [int(cnt), 0]
    for did, cnt in col.db.all(
        "select did, count(*) from cards where mod > ? and id <= ? group by did",
        last_sec, last_ms,
    ):
        out.setdefault(int(did), [0, 0])[1] = int(cnt)
    return {did: (values[0], values[1]) for did, values in out.items()}


def pending_deletions(col: Collection) -> int:
    """Count note+card deletions tracked in graves (collection-wide; graves don't
    record which deck a deleted card belonged to)."""
    from anki.consts import REM_CARD, REM_NOTE

    return (
        col.db.scalar(
            "select count(*) from graves where type in (?, ?)", REM_NOTE, REM_CARD
        )
        or 0
    )


def fingerprints_for(col: Collection, deck_names: list[str]) -> dict[str, Fingerprint]:
    """Fingerprint many decks at once with just two grouped queries.

    Returns {deck_name: [card_count, max_card_mod, max_note_mod]}, each value
    aggregated over the deck and its subdecks. This is the fast path used to find
    which decks changed without scanning per deck.
    """
    cmap: dict[int, tuple] = {}
    for did, cnt, cmod in col.db.all(
        "select did, count(*), coalesce(max(mod), 0) from cards group by did"
    ):
        cmap[did] = (cnt, cmod)
    nmap: dict[int, int] = {}
    for did, nmod in col.db.all(
        "select c.did, coalesce(max(n.mod), 0) from cards c "
        "join notes n on c.nid = n.id group by c.did"
    ):
        nmap[did] = nmod

    all_decks = [(d.name, d.id) for d in col.decks.all_names_and_ids()]
    out: dict[str, Fingerprint] = {}
    for name in deck_names:
        prefix = name + "::"
        count = cmod = nmod = 0
        for dn, did in all_decks:
            if dn == name or dn.startswith(prefix):
                c = cmap.get(did)
                if c:
                    count += c[0]
                    if c[1] > cmod:
                        cmod = c[1]
                nm = nmap.get(did, 0)
                if nm > nmod:
                    nmod = nm
        out[name] = [count, cmod, nmod]
    return out
