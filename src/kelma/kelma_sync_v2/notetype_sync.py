"""Notetype sync orchestration for KelmaSync v2.

Notetypes must sync before notes so pulled notes have a local schema to attach
to. Conflicts are explicit and stop note sync.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from anki.collection import Collection

from .client import V2Client, V2Conflict
from .conflict_policy import newest_side, server_wins_anki_timestamp_truncation
from . import anki_apply, anki_local
from .checksum_rs import notetype_checksum

_PREFETCH_CHUNK = 200


@dataclass
class NotetypeSyncResult:
    pushed: int = 0
    pulled: int = 0
    skipped: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class NotetypeSyncConflict(RuntimeError):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(conflicts)} notetype conflict(s)")
        self.conflicts = conflicts


def _same_notetype_name(local_name: str, server_name: str) -> bool:
    """Equal, or Anki's de-duplicated copy of it ("Basic" → "Basic-23fda").

    Anki keeps notetype names unique: applying a server "Basic" next to an
    existing local "Basic" renames it with a 5-hex suffix, which must not look
    like a difference on every later sync.
    """
    return local_name == server_name or bool(
        re.fullmatch(re.escape(server_name) + r"-[0-9a-f]{5}", local_name)
    )


def _canonically_equivalent_notetypes(
    local: dict[str, Any] | None,
    server: dict[str, Any] | None,
) -> bool:
    """Whether two notetype records differ only in client bookkeeping."""
    if not local or not server:
        return False
    if not _same_notetype_name(str(local.get("name") or ""), str(server.get("name") or "")):
        return False
    # Normalise both copies the same way: Anki injects originalStockKind and
    # recomputes req, and other clients may have stored raw ids/mod/usn.
    local_definition = anki_local.normalize_notetype_definition(local.get("definition") or {})
    server_definition = anki_local.normalize_notetype_definition(server.get("definition") or {})
    local_definition.pop("name", None)
    server_definition.pop("name", None)
    return local_definition == server_definition


def sync_notetypes_once(
    col: Collection,
    client: V2Client,
    server_manifest: dict[str, Any] | None = None,
    *,
    apply_pulls: bool = True,
    progress=None,
    notetype_ids: set[int] | None = None,
    prefer_server: bool = False,
    newest_wins: bool = False,
    accept_server_timestamp_truncation: bool = False,
    equivalent: dict[str, list[str]] | None = None,
) -> NotetypeSyncResult:
    if progress:
        progress("Notetypes: building local notetype manifest…")
    local = {str(x["notetype_id"]): x for x in anki_local.notetype_manifest(col, notetype_ids=notetype_ids)}
    if server_manifest is None:
        server_manifest = client.manifest()
    server = {
        str(x["notetype_id"]): x
        for x in server_manifest.get("notetypes", [])
        if notetype_ids is None or int(x["notetype_id"]) in notetype_ids
    }
    result = NotetypeSyncResult()

    keys = sorted(set(local) | set(server))
    total = len(keys)
    # Batch-fetch the server notetypes we may need (one request instead of one
    # GET each) so equivalent copies are recognised before newest-wins would
    # pull them again on every sync.
    known = equivalent if equivalent is not None else {}

    def pair(key: str) -> list[str]:
        return [str(local[key].get("checksum") or ""), str(server[key].get("checksum") or "")]

    records = _prefetch_server_notetypes(client, [
        int(key) for key in keys
        if key in server
        and (key not in local or local[key].get("checksum") != server[key].get("checksum"))
        and not (key in local and known.get(key) == pair(key))
    ])

    def same(key: str) -> bool:
        l, s = local.get(key), server.get(key)
        if not l or not s:
            return False
        if l.get("checksum") == s.get("checksum"):
            return True
        if known.get(key) == pair(key):
            return True
        record = records.get(int(key))
        if record is None:
            return False
        name = str(record.get("name") or "")
        if notetype_checksum(
            name, anki_local.normalize_notetype_definition(record.get("definition") or {})
        ) == l.get("checksum") or _canonically_equivalent_notetypes(
            anki_local.notetype_record(col, int(key)), record
        ):
            known[key] = pair(key)
            return True
        known.pop(key, None)
        return False

    def pull(ntid: int) -> None:
        record = records.get(ntid)
        if record is not None:
            anki_apply.apply_notetype(col, record)
        else:
            anki_apply.apply_server_notetype(col, client, ntid)
    if progress:
        progress(f"Notetypes: syncing {total} notetypes…")
    for idx, key in enumerate(keys, 1):
        if progress:
            progress(f"Notetypes {idx}/{total} · pushed {result.pushed}, pulled {result.pulled}, skipped {result.skipped}, conflicts {len(result.conflicts)}")
        l = local.get(key)
        s = server.get(key)
        ntid = int(key)
        if prefer_server:
            # A fresh Anki collection ships unused stock notetypes. They are
            # scaffolding, not local edits: restore every server notetype and
            # leave local-only stock definitions unpublished.
            if s and l and same(key):
                result.skipped += 1
            elif s and apply_pulls:
                pull(ntid)
                result.pulled += 1
            else:
                result.skipped += 1
            continue
        if l and s and same(key):
            result.skipped += 1
            continue
        if l and not s:
            _push_notetype(col, client, ntid, base_checksum="")
            result.pushed += 1
            continue
        if s and not l:
            if apply_pulls:
                pull(ntid)
                result.pulled += 1
            else:
                result.skipped += 1
            continue
        if l and s:
            winner = (
                newest_side(
                    l, s, utc_now=server_manifest.get("server_time")
                )
                if newest_wins
                else None
            )
            if winner is None:
                local_record = anki_local.notetype_record(col, ntid)
                server_record = records.get(ntid) or client.get_notetype(ntid)
                if _canonically_equivalent_notetypes(local_record, server_record):
                    result.skipped += 1
                    continue
                if (
                    accept_server_timestamp_truncation
                    and server_wins_anki_timestamp_truncation(l, s)
                ):
                    winner = "server"
            if winner == "server" and apply_pulls:
                pull(ntid)
                result.pulled += 1
            elif winner == "server":
                result.skipped += 1
            elif winner == "local":
                try:
                    _push_notetype(
                        col, client, ntid,
                        base_checksum=str(s.get("checksum") or ""),
                    )
                    result.pushed += 1
                except V2Conflict as conflict:
                    result.conflicts.append({
                        "notetype_id": ntid,
                        "server": conflict.server or s,
                        "client": conflict.client or l,
                    })
            else:
                result.conflicts.append({"notetype_id": ntid, "server": s, "client": l})
    if result.conflicts:
        if progress:
            progress(f"Notetypes: {len(result.conflicts)} conflict(s)")
        raise NotetypeSyncConflict(result.conflicts)
    if progress:
        progress(f"Notetypes complete: pushed {result.pushed}, pulled {result.pulled}, skipped {result.skipped}")
    return result


def _prefetch_server_notetypes(client: V2Client, ids: list[int]) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), _PREFETCH_CHUNK):
        try:
            response = client.batch_pull(notetypes=ids[start:start + _PREFETCH_CHUNK]) or {}
        except Exception:  # noqa: BLE001 - fall back to per-notetype requests
            return records
        for record in response.get("notetypes", []) or []:
            try:
                records[int(record.get("notetype_id"))] = record
            except (TypeError, ValueError):
                continue
    return records


def force_local_notetype(col: Collection, client: V2Client, notetype_id: int) -> dict[str, Any]:
    return _push_notetype(col, client, notetype_id, base_checksum="", force=True)


def accept_server_notetype(col: Collection, client: V2Client, notetype_id: int) -> int:
    return anki_apply.apply_server_notetype(col, client, notetype_id)


def _push_notetype(
    col: Collection,
    client: V2Client,
    notetype_id: int,
    *,
    base_checksum: str,
    force: bool = False,
) -> dict[str, Any]:
    rec = anki_local.notetype_record(col, notetype_id)
    if rec is None:
        raise ValueError(f"local notetype not found: {notetype_id}")
    return client.put_notetype(
        notetype_id,
        name=rec["name"],
        definition=rec["definition"],
        client_modified_at=rec["client_modified_at"],
        base_checksum=base_checksum,
        force=force,
    )
