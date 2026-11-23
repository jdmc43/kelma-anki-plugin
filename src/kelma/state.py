"""Persistent sync state, kept in `kelma_state.json` next to the collection.

After each successful sync the add-on records, per service, when it synced and
the exact per-deck card state at that moment. The deck badges compare the
collection against that baseline to show `+added` / `~changed` counts.

The file is best-effort: a missing/corrupt file just means "everything looks
changed" until the next sync, never an error.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from anki.collection import Collection

from . import paths

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


