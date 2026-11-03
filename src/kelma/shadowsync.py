"""Two-way sync of a single service's shadow collection, plus reconciliation
with the master collection the user studies in.

Speed model: we never reconcile deck-by-deck. Each sync first fingerprints the
whole collection in two grouped SQL queries (fast), diffs against the last-synced
baseline to find which decks changed, then moves *all* changed decks in a single
batched apkg export/import (one round-trip per direction, or zero if nothing
changed) — instead of one round-trip per deck.

KelmaSync compatibility mode is tracked for reporting, but every mode uses the
same routed, change-detected reconciliation. AnkiWeb is always legacy.
"""

from __future__ import annotations

import time

from anki.collection import Collection
from anki.sync import SyncAuth
from aqt import mw

from . import auth as auth_mod
from . import capabilities, config, consts, deletions, paths, state
from .progress import Reporter
from .reconcile import reconcile_decks
from .scheduling import sync_scheduling


def _master_deck_names() -> list[str]:
    return [d.name for d in mw.col.decks.all_names_and_ids()]


def _reconcile(src: Collection, dst: Collection, decks: list[str], media: bool) -> int:
    """Reconcile, but if the export trips Anki's id sanity check (future-dated ids,
    typically pulled in from the server), renumber the SOURCE's invalid ids in
    place and retry just the export — no re-download."""
    try:
        return reconcile_decks(src, dst, decks, with_media=media)
    except Exception as err:  # noqa: BLE001
        if not is_check_db_error(err):
            raise
        src.fix_integrity()  # renumbers future-dated ids on the export source
        return reconcile_decks(src, dst, decks, with_media=media)


def is_check_db_error(err: Exception) -> bool:
    """True for Anki's "Please use the Check Database action" / inconsistent-db
    errors (e.g. future-dated ids from a skewed clock tripping the export check)."""
    return "check database" in str(err).lower()


def repair(service: str) -> None:
    """Run Check Database (fix_integrity) on the master and the service shadow.

    This renumbers out-of-range/future-dated ids and rebuilds caches, clearing the
    condition that makes the export/sync sanity check fail — so we can retry
    instead of dumping the user at a manual "Check Database" dialog.
    """
    try:
        mw.col.fix_integrity()
    except Exception:  # noqa: BLE001 - best-effort repair
        pass
    if paths.shadow_exists(service):
        try:
            shadow = Collection(paths.shadow_path(service))
            try:
                shadow.fix_integrity()
            finally:
                shadow.close()
        except Exception:  # noqa: BLE001
            pass


def _await_media_sync(col: Collection) -> None:
    """Block until the background media sync started by `sync_collection`/
    `sync_media` finishes. This is essential: media sync runs on a background
    thread and the caller closes the collection right after — closing while it's
    still transferring ABORTS it, so a large media library never fully uploads.
    `media_sync_status()` raises if the sync errored, surfacing failures instead
    of silently dropping media."""
    while True:
        if not col.media_sync_status().active:
            return
        time.sleep(0.3)


def _run_native_sync(col: Collection, auth: SyncAuth, sync_media: bool) -> Collection:
    """Perform a normal sync; if the server requires a full sync, do it. Also
    waits for the (background) media sync to finish before returning.

    Returns the collection to keep using (re-opened after a full sync).
    """
    out = col.sync_collection(auth, sync_media)

    if out.required == out.NO_CHANGES:
        # A normal/no-change sync already kicked off media in the background
        # (sync_media=True) — wait for it before the caller closes the collection.
        if sync_media:
            _await_media_sync(col)
        return col

    # The meta step may hand back a load-balanced endpoint (AnkiWeb does). The
    # full upload/download MUST go there or its response lacks the size header
    # ("missing original size"). Use it locally without touching the profile's
    # real sync URL.
    if out.new_endpoint:
        auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)

    media_usn = out.server_media_usn if sync_media else None
    if out.required == out.FULL_UPLOAD:
        upload = True
    elif out.required in (out.FULL_DOWNLOAD, out.FULL_SYNC):
        # Server is canonical for a shadow; download, then reconcile will push
        # the master's routed decks back up on the (incremental) sync that follows.
        upload = False
    else:
        # NORMAL_SYNC: collection changes applied, media started in background.
        if sync_media:
            _await_media_sync(col)
        return col

    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=media_usn, upload=upload)
    # Reopen the SAME backend (as Anki does) — not a new Collection, which would
    # be a second handle on the file and lock it ("Anki already open").
    col.reopen(after_full_sync=True)
    # A full sync does NOT start media (see rslib sync_collection_inner: it skips
    # the background media sync when a full sync is required). Start it now and
    # wait, or media is never transferred on the first sync of a new shadow.
    if sync_media:
        col.sync_media(auth)
        _await_media_sync(col)
    return col


def _changed(fps: dict, dstate: dict, key: str, routed: list[str], use_fp: bool) -> list[str]:
    """Decks whose `key` ('m' master / 's' shadow) fingerprint differs from the
    stored baseline. The fallback path exists for older callers/tests."""
    if not use_fp:
        return list(routed)
    return [d for d in routed if fps.get(d) != dstate.get(d, {}).get(key)]


def _propagate_deletions(
    reporter, dst: Collection, gone: set[str], routed: list[str], label: str, where: str
) -> int:
    """Remove notes deleted on the other side (GUIDs that were converged last sync
    and are now `gone`) from `dst`. GUID-keyed, so it works even though card/note
    ids differ across collections; safety-capped against a stale snapshot."""
    if not gone:
        reporter.tick()  # keep the phase count stable, keep the current label
        return 0
    reporter.advance(f"{label}: removing {where} deletions…")
    removed, why = deletions.remove_guids(dst, gone, routed)
    if why:
        reporter.message(f"{label}: skipped deletions — {why}")
    return removed


def _describe(decks: list[str]) -> str:
    if len(decks) <= 3:
        return ", ".join(decks)
    return f"{len(decks)} decks"


def sync_service(service: str, reporter: Reporter) -> dict:
    """Run a full reconcile+sync cycle for one service. Returns a small summary."""
    label = consts.SERVICE_LABEL[service]
    reporter.set_total(1)

    reporter.message(f"{label}: checking account…")
    auth = auth_mod.build_auth(service)
    if auth is None:
        reporter.advance(f"{label}: not logged in")
        return {"service": service, "label": label, "skipped": "no credentials"}

    cfg = config.get()
    sync_media = bool(cfg["sync_media"])
    reporter.message(f"{label}: reading deck routing…")
    routed = config.decks_for_service(service, _master_deck_names())

    # Always use fingerprint change-detection (push only the decks the DESKTOP
    # changed; pull only the decks the SERVER changed). This is mandatory for a
    # server shared with another writing client (AnkiMobile / KelmaMobile):
    # re-reconciling an unchanged deck re-marks all its cards usn=-1 in the shadow
    # and re-uploads them, overwriting whatever the mobile client pushed for that
    # deck in the meantime. Change-detection only moves what genuinely changed on
    # this side, so concurrent mobile edits to other decks are never clobbered.
    # The standard/legacy path is still tracked for the summary. resolve_path()
    # never blocks (it probes in the background).
    reporter.message(f"{label}: detecting server type…")
    path = capabilities.resolve_path() if service == consts.KELMA else consts.PATH_LEGACY
    use_fp = True

    st = state.load()
    dstate = state.service_decks(st, service)
    # GUIDs that were present on BOTH sides at the last converged sync. A GUID in
    # here that's now missing from one side was deleted there (see _propagate_
    # deletions); the intersection is deliberate — a note that only ever existed
    # on one side must never be read as a deletion.
    prev_guids = set(dstate.get("_guids", []))

    is_new = not paths.shadow_exists(service)
    # Phases: [seed] check-push, push, del, sync, check-pull, pull, del.
    total = (1 if is_new else 0) + 7

    reporter.message(f"{label}: opening local copy…")
    shadow = Collection(paths.shadow_path(service))
    reporter.set_total(total)
    try:
        if is_new:
            reporter.advance(f"{label}: seeding from server…")
            reporter.watch(shadow, f"{label} (seed)")
            try:
                shadow = _run_native_sync(shadow, auth, sync_media)
            finally:
                reporter.unwatch()

        # --- push: master -> shadow (changed decks only) ---------------------
        reporter.advance(f"{label}: checking {len(routed)} decks…")
        mfps = state.fingerprints_for(mw.col, routed)
        spre = state.fingerprints_for(shadow, routed)
        to_push = set(_changed(mfps, dstate, "m", routed, use_fp))
        # Force-push any deck the master holds more cards for than the shadow does.
        # Change-detection compares each side against its OWN prior baseline, so a
        # note added on the desktop before this service's baseline was captured
        # leaves the deck fingerprint equal to its baseline forever — it reads as
        # "unchanged" and is never pushed, staying desktop-only (one card short on
        # the server and every other client). A straight count comparison against
        # the shadow catches that straggler regardless of the fingerprint; once it
        # pushes, the counts match and this stops firing.
        to_push |= {d for d in routed if mfps[d][0] > spre[d][0]}
        to_push = sorted(to_push)
        if to_push:
            reporter.advance(f"{label}: pushing {_describe(to_push)}…")
            pushed = _reconcile(mw.col, shadow, to_push, sync_media)
        else:
            reporter.advance(f"{label}: nothing to push")
            pushed = 0
        # The apkg import adds new cards but skips ones that already exist, so
        # reviews/scheduling on existing cards don't cross over. Carry them — for
        # ALL routed decks, not just content-changed ones. A review's mtime is
        # already folded into the fingerprint baseline, so a deck whose only
        # change is a past review reads as "unchanged" and would never get its
        # scheduling backlog cleared. The pass is cheap and idempotent.
        sched_pushed = sync_scheduling(mw.col, shadow, routed)
        # Deletions: a GUID converged last sync but now gone from the master was
        # deleted on the desktop → remove it from the shadow so the native sync
        # carries the deletion to the server (and other clients). GUID-keyed, so
        # it's immune to the card/note-id divergence between the two collections.
        master_guids = deletions.routed_guids(mw.col, routed)
        del_pushed = _propagate_deletions(
            reporter, shadow, prev_guids - master_guids, routed, label, "desktop"
        )

        # --- two-way sync the shadow with its server -------------------------
        reporter.advance(f"{label}: syncing with server…")
        reporter.watch(shadow, label)
        try:
            shadow = _run_native_sync(shadow, auth, sync_media)
        finally:
            reporter.unwatch()

        # --- pull: shadow -> master (changed decks only) ---------------------
        # The server (mirrored in the shadow) may hold decks the master doesn't
        # yet — a fresh install, or a deck created on another client (KelmaMobile,
        # the web app). Those must be imported here, so the pull set is the routed
        # decks PLUS any server-only deck. `routed` (master-side) still drives the
        # push and deletion logic above.
        reporter.advance(f"{label}: checking server changes…")
        master_names = set(_master_deck_names())
        server_only = [
            d.name
            for d in shadow.decks.all_names_and_ids()
            if d.name != "Default" and d.name not in master_names
        ]
        pull_decks = sorted(set(routed) | set(server_only))
        sfps = state.fingerprints_for(shadow, pull_decks)
        mnow = state.fingerprints_for(mw.col, pull_decks)
        to_pull = set(_changed(sfps, dstate, "s", pull_decks, use_fp))
        # Mirror of the push-side straggler fix: pull any deck the shadow (server)
        # now holds more cards for than the master does (covers server-only decks,
        # whose master count is 0).
        to_pull |= {d for d in pull_decks if sfps[d][0] > mnow[d][0]}
        to_pull = sorted(to_pull)
        if to_pull:
            reporter.advance(f"{label}: pulling {_describe(to_pull)}…")
            pulled = _reconcile(shadow, mw.col, to_pull, sync_media)
        else:
            reporter.advance(f"{label}: nothing to pull")
            pulled = 0
        # Carry the server's reviews onto the master's existing cards, for all
        # pull decks (the import skips them; see the push side).
        sched_pulled = sync_scheduling(shadow, mw.col, pull_decks)
        # Deletions the other way: a GUID converged last sync but now gone from the
        # shadow (which mirrors the server after the native sync above) was deleted
        # on the server → remove it from the master.
        shadow_guids = deletions.routed_guids(shadow, pull_decks)
        del_pulled = _propagate_deletions(
            reporter, mw.col, prev_guids - shadow_guids, pull_decks, label, "server"
        )

        # --- record converged baseline for next time -------------------------
        # Over the union of push + pull decks, so freshly-imported server decks get
        # a baseline (and aren't re-detected as changed next sync).
        base_decks = sorted(set(routed) | set(pull_decks))
        final_m = state.fingerprints_for(mw.col, base_decks)
        final_s = state.fingerprints_for(shadow, base_decks)
        for name in base_decks:
            dstate[name] = {"m": final_m[name], "s": final_s[name]}
        # Snapshot the GUIDs now present on BOTH sides — the converged set that the
        # next sync diffs against to detect deletions. Recompute post-delete so a
        # note removed this cycle isn't re-flagged next time.
        dstate["_guids"] = sorted(
            deletions.routed_guids(mw.col, base_decks)
            & deletions.routed_guids(shadow, base_decks)
        )
    finally:
        try:
            shadow.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass

    state.mark_synced(st, service, path, mw.col)
    state.save(st)

    return {
        "service": service,
        "label": label,
        "path": path,
        "decks": len(routed),
        "pushed_cards": pushed,
        "pulled_cards": pulled,
        "deleted_remote": del_pushed,
        "deleted_local": del_pulled,
        "rescheduled_remote": sched_pushed,
        "rescheduled_local": sched_pulled,
    }
