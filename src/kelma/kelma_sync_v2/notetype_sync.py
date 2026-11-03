"""Notetype sync orchestration for KelmaSync v2.

Notetypes must sync before notes so pulled notes have a local schema to attach
to. Conflicts are explicit and stop note sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anki.collection import Collection

from .client import V2Client, V2Conflict
from .conflict_policy import newest_side, server_wins_anki_timestamp_truncation
from . import anki_apply, anki_local


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


def _canonically_equivalent_notetypes(
    local: dict[str, Any] | None,
    server: dict[str, Any] | None,
) -> bool:
    """Ignore Anki's local-only stock-notetype classification metadata."""
    if not local or not server or local.get("name") != server.get("name"):
        return False
    local_definition = dict(local.get("definition") or {})
    server_definition = dict(server.get("definition") or {})
    # Current Anki injects this property while decoding an older definition.
    # It does not affect fields, templates, rendering, or scheduling and is not
    # consistently serialized by older Anki/Kelma clients.
    local_definition.pop("originalStockKind", None)
    server_definition.pop("originalStockKind", None)
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
            if s and l and l.get("checksum") == s.get("checksum"):
                result.skipped += 1
            elif s and apply_pulls:
                anki_apply.apply_server_notetype(col, client, ntid)
                result.pulled += 1
            else:
                result.skipped += 1
            continue
        if l and s and l.get("checksum") == s.get("checksum"):
            result.skipped += 1
            continue
        if l and not s:
            _push_notetype(col, client, ntid, base_checksum="")
            result.pushed += 1
            continue
        if s and not l:
            if apply_pulls:
                anki_apply.apply_server_notetype(col, client, ntid)
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
                server_record = client.get_notetype(ntid)
                if _canonically_equivalent_notetypes(local_record, server_record):
                    result.skipped += 1
                    continue
                if (
                    accept_server_timestamp_truncation
                    and server_wins_anki_timestamp_truncation(l, s)
                ):
                    winner = "server"
            if winner == "server" and apply_pulls:
                anki_apply.apply_server_notetype(col, client, ntid)
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
