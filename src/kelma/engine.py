"""Runs KelmaSync and AnkiWeb as separate, sequential sync operations.

Both run inside one background task, but each is reported distinctly — the
progress label switches from "KelmaSync: …" to "AnkiWeb: …" with its own bar —
rather than a single merged "dual sync". One service failing does not abort the
other; a combined summary (and any errors) is shown at the end.
"""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Callable, Optional

from aqt import mw, gui_hooks
from aqt.qt import QTimer, qconnect
from aqt.utils import showWarning, tooltip

from . import config, consts
from .progress import Reporter
from .shadowsync import is_check_db_error, repair, sync_service

_running = False
_last_backup = 0.0  # session throttle so rapid repeat syncs don't re-backup
_BACKUP_INTERVAL = 300


def dual_sync(
    on_done: Optional[Callable[[], None]] = None, only: Optional[str] = None
) -> None:
    """Sync every active service (or just `only`), each reported on its own bar."""
    global _running
    if _running:
        return

    deck_names = [d.name for d in mw.col.decks.all_names_and_ids()]
    active = config.active_services(deck_names)
    order = [s for s in (consts.KELMA, consts.ANKIWEB) if s in active]
    if only is not None:
        order = [s for s in order if s == only]

    if not order:
        what = consts.SERVICE_LABEL.get(only, "any service") if only else "anything"
        services = " or ".join(consts.SERVICE_LABEL[s] for s in config.ui_services())
        showWarning(
            f"Kelma: nothing to sync to {what}.\n\n"
            "Open Tools → Kelma → Settings to log in and route at least one deck "
            f"to {services}."
        )
        if on_done:
            on_done()
        return

    cfg = config.get()
    global _last_backup
    if cfg["backup_before_sync"] and time.time() - _last_backup > _BACKUP_INTERVAL:
        try:
            mw.create_backup_now()
            _last_backup = time.time()
        except Exception:  # noqa: BLE001 - backup is best-effort
            pass

    _running = True
    gui_hooks.sync_will_start()
    reporter = Reporter()
    # Concrete first label (no generic "starting"); sync_service overwrites it
    # immediately with the precise step it's on.
    reporter.set_total(1)
    reporter.message(f"{consts.SERVICE_LABEL[order[0]]}: checking account…")

    # Paint the dialog from the main thread so the label always reflects the
    # step currently in progress, even one that's slow or blocking.
    heartbeat = QTimer(mw)
    heartbeat.setInterval(120)
    qconnect(heartbeat.timeout, reporter.render)

    def one_pass() -> list[dict]:
        results = []
        for service in order:
            label = consts.SERVICE_LABEL[service]
            try:
                results.append(sync_service(service, reporter))
            except Exception as err:  # noqa: BLE001 - keep going to the next service
                # Auto-recover from "Please use the Check Database action": run it
                # ourselves and retry once, instead of failing at the user.
                if is_check_db_error(err):
                    reporter.message(f"{label}: repairing database…")
                    repair(service)
                    try:
                        results.append(sync_service(service, reporter))
                        continue
                    except Exception as err2:  # noqa: BLE001
                        err = err2
                reporter.message(f"{label}: failed")
                results.append(
                    {"service": service, "label": label, "error": str(err)}
                )
        return results

    def _pulled_anything(results: list[dict]) -> bool:
        return any(
            r.get("pulled_cards")
            or r.get("rescheduled_local")
            or r.get("deleted_local")
            for r in results
        )

    def task() -> list[dict]:
        results = one_pass()
        # A change on one service's server only reaches the master when THAT
        # service syncs. A service synced earlier in the pass (e.g. KelmaSync,
        # which runs before AnkiWeb) therefore misses a change AnkiWeb just
        # pulled in, and it wouldn't reach KelmaMobile until the next sync — the
        # "sync twice" problem. So if the pass pulled anything into the master and
        # there's more than one service, run a second pass to push those changes
        # out to the others now. Two passes cover propagation between services;
        # cap there so a server that keeps changing can't loop us forever.
        if len(order) > 1 and _pulled_anything(results):
            reporter.message("Propagating cross-service changes…")
            results = one_pass()
        return results

    def done(fut: "Future[list[dict]]") -> None:
        global _running
        _running = False
        heartbeat.stop()
        heartbeat.deleteLater()
        try:
            mw.col.models._clear_cache()
        except Exception:  # noqa: BLE001
            pass
        gui_hooks.sync_did_finish()
        mw.reset()
        try:
            results = fut.result()
        except Exception as err:  # noqa: BLE001 - unexpected, outside per-service
            showWarning(f"Kelma sync failed:\n{err}")
            if on_done:
                on_done()
            return
        errors = [r for r in results if "error" in r]
        if errors:
            detail = "\n\n".join(f"{r['label']}:\n{r['error']}" for r in errors)
            showWarning(f"Some Kelma syncs failed:\n\n{detail}")
        tooltip(_summary(results), parent=mw)
        if on_done:
            on_done()

    mw.taskman.with_progress(
        task, done, label=reporter.step, title="Kelma sync", immediate=True
    )
    heartbeat.start()


def _summary(results: list[dict]) -> str:
    parts = []
    for r in results:
        label = r.get("label", consts.SERVICE_LABEL.get(r.get("service"), "?"))
        if "error" in r:
            parts.append(f"{label}: failed")
        elif "skipped" in r:
            parts.append(f"{label}: skipped")
        else:
            deleted = r.get("deleted_remote", 0) + r.get("deleted_local", 0)
            extra = f" −{deleted}" if deleted else ""
            resched = r.get("rescheduled_remote", 0) + r.get("rescheduled_local", 0)
            sched = f" ⟳{resched}" if resched else ""
            tag = "" if r.get("path") != consts.PATH_LEGACY else " (legacy)"
            parts.append(
                f"{label}{tag}: {r['decks']} decks "
                f"(↑{r['pushed_cards']} ↓{r['pulled_cards']}{extra}{sched})"
            )
    return "Sync complete — " + ", ".join(parts)
