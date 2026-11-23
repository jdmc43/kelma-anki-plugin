from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anki.collection import Collection

from .client import V2Client, V2Conflict
from .conflict_policy import newest_side, server_wins_anki_timestamp_truncation
from . import anki_apply, anki_local
from .checksum_rs import deck_checksum

_PREFETCH_CHUNK = 500


@dataclass
class DeckSyncResult:
    pushed: int = 0
    pulled: int = 0
    skipped: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class DeckSyncConflict(RuntimeError):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(conflicts)} deck conflict(s)")
        self.conflicts = conflicts


def sync_decks_once(
    col: Collection,
    client: V2Client,
    server_manifest: dict[str, Any] | None = None,
    progress=None,
    deck_names: list[str] | None = None,
    prefer_server: bool = False,
    newest_wins: bool = False,
    accept_server_timestamp_truncation: bool = False,
) -> DeckSyncResult:
    if progress:
        progress("Decks: building local deck manifest…")
    local = {x["name"]: x for x in anki_local.deck_manifest(col, deck_names=deck_names)}
    if server_manifest is None:
        server_manifest = client.manifest()
    server = {x["name"]: x for x in server_manifest.get("decks", [])}
    result = DeckSyncResult()
    names = sorted(set(local) | set(server))
    total = len(names)
    # One batch request for every server deck we may need, instead of one GET
    # per deck. It also lets an equivalent copy be recognised: the server may
    # store a config another client serialised differently (null vs absent
    # keys, Anki's daily bookkeeping), which would otherwise be pulled again
    # on every sync without ever converging.
    records = _prefetch_server_decks(client, [
        name for name in names
        if name in server
        and (name not in local or local[name].get("checksum") != server[name].get("checksum"))
    ])

    def same(name: str) -> bool:
        l, s = local.get(name), server.get(name)
        if not l or not s:
            return False
        if l.get("checksum") == s.get("checksum"):
            return True
        record = records.get(name)
        if record is None:
            return False
        if deck_checksum(
            anki_local.normalize_deck_config(record.get("config") or {})
        ) == l.get("checksum"):
            return True
        return _applying_changes_nothing(col, name, record.get("config") or {})

    def pull(name: str) -> None:
        record = records.get(name)
        if record is not None:
            anki_apply.apply_deck(col, record)
        else:
            anki_apply.apply_server_deck(col, client, name)
    if progress:
        progress(f"Decks: syncing {total} decks…")
    for idx, name in enumerate(names, 1):
        if progress:
            progress(f"Decks {idx}/{total} · pushed {result.pushed}, pulled {result.pulled}, skipped {result.skipped}, conflicts {len(result.conflicts)}")
        l = local.get(name)
        s = server.get(name)
        if prefer_server:
            # Fresh KelmaDesktop collections contain stock local structure but
            # no user content. Restore server decks without presenting those
            # unused defaults as competing edits, and never publish local-only
            # stock decks during the first restore.
            if s and l and same(name):
                result.skipped += 1
            elif s:
                pull(name)
                result.pulled += 1
            else:
                result.skipped += 1
            continue
        if l and s and same(name):
            result.skipped += 1
            continue
        if l and not s:
            if name == "Default" and not _deck_has_cards(col, name):
                # Anki creates this empty deck in every new collection. It is
                # local scaffolding, not a user-created server deck.
                result.skipped += 1
                continue
            _push_deck(col, client, name, base_checksum="")
            result.pushed += 1
            continue
        if s and not l:
            pull(name)
            result.pulled += 1
            continue
        if l and s:
            winner = (
                newest_side(
                    l, s, utc_now=server_manifest.get("server_time")
                )
                if newest_wins
                else None
            )
            if (
                winner is None
                and accept_server_timestamp_truncation
                and server_wins_anki_timestamp_truncation(l, s)
            ):
                winner = "server"
            if winner == "server":
                pull(name)
                result.pulled += 1
            elif winner == "local":
                try:
                    _push_deck(
                        col, client, name,
                        base_checksum=str(s.get("checksum") or ""),
                    )
                    result.pushed += 1
                except V2Conflict as conflict:
                    result.conflicts.append({
                        "name": name,
                        "server": conflict.server or s,
                        "client": conflict.client or l,
                    })
            else:
                result.conflicts.append({"name": name, "server": s, "client": l})
    if result.conflicts:
        if progress:
            progress(f"Decks: {len(result.conflicts)} conflict(s)")
        raise DeckSyncConflict(result.conflicts)
    if progress:
        progress(f"Decks complete: pushed {result.pushed}, pulled {result.pulled}, skipped {result.skipped}")
    return result


def _applying_changes_nothing(col: Collection, name: str, server_config: dict[str, Any]) -> bool:
    """Whether applying the server config would leave the local deck as it is.

    Kelma Review and the website create decks with few or no Anki settings, so
    their checksums never match Anki's full deck dict even though there is
    nothing to pull. Explicit nulls count (a limit cleared elsewhere must still
    be applied); keys the server does not store are left alone by apply_deck.
    """
    record = anki_local.deck_record(col, name)
    if record is None:
        return False
    local_config = record["config"]
    return all(
        local_config.get(key) == value
        for key, value in server_config.items()
        if key not in anki_local.DECK_BOOKKEEPING_KEYS
    )


def _prefetch_server_decks(client: V2Client, names: list[str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for start in range(0, len(names), _PREFETCH_CHUNK):
        try:
            response = client.batch_pull(decks=names[start:start + _PREFETCH_CHUNK]) or {}
        except Exception:  # noqa: BLE001 - fall back to per-deck requests
            return records
        for record in response.get("decks", []) or []:
            if record.get("name"):
                records[str(record["name"])] = record
    return records


def _deck_has_cards(col: Collection, name: str) -> bool:
    deck = col.decks.by_name(name)
    if not deck:
        return False
    deck_id = int(deck["id"])
    return bool(
        col.db.scalar(
            "select 1 from cards where did = ? or odid = ? limit 1",
            deck_id,
            deck_id,
        )
    )


def _push_deck(col: Collection, client: V2Client, name: str, *, base_checksum: str, force: bool = False) -> dict[str, Any]:
    rec = anki_local.deck_record(col, name)
    if rec is None:
        raise ValueError(f"local deck not found: {name}")
    return client.put_deck(
        name,
        config=rec["config"],
        client_modified_at=rec["client_modified_at"],
        base_checksum=base_checksum,
        force=force,
    )
