"""Current-account sync badges and storage size on the main deck list."""

from __future__ import annotations

import os
import re
import time

from anki.media import media_paths_from_col_path
from aqt import mw, gui_hooks

from . import config, consts, features, paths, state

_installed = False

# Per-cloud size: exact authenticated server usage for KelmaSync, and local
# collection/media bytes for native AnkiWeb. Computed in the background and
# cached so deck-list rendering never performs network or filesystem work.
_SIZE_TTL = 300
_size_cache: dict[str, tuple[float, int]] = {}
_size_running: set[str] = set()

# Kelma = green, AnkiWeb = blue.
_COLOR = {consts.KELMA: "#16a34a", consts.ANKIWEB: "#2563eb"}

# Matches a deck-name link in the deck-browser tree, capturing the deck id.
_LINK_RE = re.compile(r"""(<a class="deck[^>]*pycmd\('open:(\d+)'\)">[^<]*</a>)""")
_STYLE = (
    "<style>"
    ".kelma-badges:not(:empty){margin-right:8px;white-space:nowrap;}"
    ".kelma-badge{font-size:11px;border:1px solid;border-radius:4px;"
    "padding:0 4px;margin-right:3px;vertical-align:middle;}"
    ".kelma-badge.synced{opacity:.55;}"
    ".kelma-sizes{padding:4px 10px 2px;font-size:12px;opacity:.9;}"
    "</style>"
)


def _badges(did: int, name: str, pending: dict) -> str:
    services = config.services_for_deck(name)
    spans = []
    for s in config.ui_services():
        if s not in services or not config.has_credentials(s):
            continue
        added, changed = pending[s].get(did, (0, 0))
        cloud = consts.SERVICE_LABEL[s]
        if added or changed:
            text = " ".join(
                p for p in (f"+{added}" if added else "", f"~{changed}" if changed else "") if p
            )
            detail = ", ".join(
                p for p in (f"{added} added" if added else "", f"{changed} changed" if changed else "") if p
            )
            title = f"{cloud}: {detail}"
            cls = "kelma-badge"
        else:
            text = "✓"
            title = f"{cloud}: in sync"
            cls = "kelma-badge synced"
        color = _COLOR[s]
        spans.append(
            f'<span class="{cls}" style="color:{color};border-color:{color}" '
            f'title="{title}">{text}</span>'
        )
    return f'<span class="kelma-badges">{"".join(spans)}</span>'


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _compute_size(shadow_path: str, fallback_col_path: str = "") -> int:
    # v2 has no shadow collection: measure the real collection + media instead
    # of reporting a permanent 0.00 GB from a file that never exists.
    if not os.path.exists(shadow_path) and fallback_col_path:
        shadow_path = fallback_col_path
    total = 0
    try:
        total += os.path.getsize(shadow_path)
    except OSError:
        pass
    media_folder, media_db = media_paths_from_col_path(shadow_path)
    if os.path.isdir(media_folder):
        total += _dir_size(media_folder)
    try:
        total += os.path.getsize(media_db)
    except OSError:
        pass
    return total


def _refresh_deck_list() -> None:
    try:
        mw.deckBrowser.refresh()
    except Exception:  # noqa: BLE001
        pass


def _ensure_size(service: str) -> None:
    entry = _size_cache.get(service)
    if entry and time.time() - entry[0] < _SIZE_TTL:
        return
    if service in _size_running:
        return
    _size_running.add(service)
    shadow_path = paths.shadow_path(service)  # resolve on the main thread
    col_path = str(getattr(mw.col, "path", "") or "")
    cfg = config.get()
    endpoint = str(cfg.get("v2_url") or consts.DEFAULT_V2_URL)
    token = str(cfg.get("v2_token") or "")

    def work() -> int:
        if service == consts.KELMA and token:
            # This badge describes cloud storage, not the size of the entire
            # local Anki profile. Ask KelmaSync for its exact account-wide total
            # (media blobs + canonical content rows).
            from .kelma_sync_v2.client import V2Client

            return int(
                V2Client(endpoint, token=token, timeout=20)
                .usage()
                .get("used_bytes", 0)
            )
        if service == consts.ANKIWEB:
            # Native AnkiWeb sync covers the real collection, not a v1 shadow.
            return _compute_size("", col_path)
        return _compute_size(shadow_path, col_path)

    def done(future) -> None:
        try:
            size = int(future.result())
        except Exception as err:  # noqa: BLE001
            print(f"Kelma badge size refresh failed ({service}): {err}")
        else:
            _size_cache[service] = (time.time(), size)
            _refresh_deck_list()
        finally:
            _size_running.discard(service)

    mw.taskman.run_in_background(work, done, uses_collection=False)


def invalidate_sizes() -> None:
    """Force the per-cloud size to recompute on next render (e.g. after a delete)."""
    _size_cache.clear()


def _size_str(service: str) -> str:
    _ensure_size(service)
    entry = _size_cache.get(service)
    if entry is None:
        return "…"
    return f"{entry[1] / (1024 ** 3):.2f} GB"


def _sizes_html(totals: dict) -> str:
    parts = []
    for s in config.ui_services():
        if not config.has_credentials(s):
            continue
        parts.append(
            f'<span style="color:{_COLOR[s]}">{consts.SERVICE_LABEL[s]}: '
            f"{totals[s]:,} cards · {_size_str(s)}</span>"
        )
    return (
        '<div class="kelma-sizes">' + " &nbsp;·&nbsp; ".join(parts) + "</div>"
        if parts
        else ""
    )


def _on_render(deck_browser, content) -> None:
    try:
        if not features.enabled("deck_badges"):
            return
        if not any(config.has_credentials(s) for s in config.ui_services()):
            return
        pending = {s: state.pending_by_did(mw.col, s) for s in config.ui_services()}
        names = {d.id: d.name for d in mw.col.decks.all_names_and_ids()}

        # Per-cloud "size" = total cards across the decks routed to that cloud.
        did_counts = {
            did: cnt
            for did, cnt in mw.col.db.all(
                "select did, count(*) from cards group by did"
            )
        }
        totals = {s: 0 for s in config.ui_services()}
        for did, name in names.items():
            cnt = did_counts.get(did, 0)
            for s in config.services_for_deck(name):
                if config.has_credentials(s):
                    totals[s] += cnt

        def repl(m: "re.Match") -> str:
            name = names.get(int(m.group(2)))
            if name is None:
                return m.group(1)
            return _badges(int(m.group(2)), name, pending) + m.group(1)

        tree = _LINK_RE.sub(repl, content.tree)
        content.tree = _STYLE + _sizes_html(totals) + tree
    except Exception as err:  # noqa: BLE001 - never break the deck list over a badge
        import traceback
        print(f"Kelma deckbadges _on_render error: {err}\n{traceback.format_exc()}")


def setup() -> None:
    global _installed
    if _installed:
        return
    gui_hooks.deck_browser_will_render_content.append(_on_render)
    _installed = True
    # KelmaDesktop renders the deck browser (moveToState in loadCollection)
    # BEFORE profile_did_open fires, so the first paint happens without our
    # hook. Re-render once so badges + sizes show immediately on startup.
    try:
        if mw.state == "deckBrowser":
            mw.deckBrowser.refresh()
    except Exception:  # noqa: BLE001
        pass
