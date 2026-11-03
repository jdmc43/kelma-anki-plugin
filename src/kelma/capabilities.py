"""Decide whether to drive KelmaSync with the standard or legacy sync path.

The choice is the user's `kelmasync_path` setting; when that's "auto" we probe the
server. A real KelmaSync server can advertise itself at `GET /kelma/capabilities`
(JSON). If it reports `{"legacy": true}` — e.g. a deployment kept stock-compatible
for AnkiMobile clients — we use the legacy path. Anything else (including a plain
modern Anki sync server, or an unreachable probe) defaults to the standard path,
since the per-deck reconcile works against any modern Anki sync server.

Results are cached per URL for the session so we don't probe on every sync;
`clear_cache()` is called when the user changes settings.

Crucially, `resolve_path()` NEVER blocks a sync on the network. For "auto" it
returns the fast standard path immediately and probes the server in a background
thread, so a later sync can switch to legacy if the server asks for it. (A
blocking probe to an unreachable server used to stall the sync at "starting…".)
"""

from __future__ import annotations

import threading

from . import config, consts

_cache: dict[str, str] = {}
_probing: set[str] = set()


def resolve_path() -> str:
    """Return consts.PATH_STANDARD or consts.PATH_LEGACY for KelmaSync.

    Returns instantly: manual override, cached probe result, or — when unknown —
    the standard path while a background probe runs for next time.
    """
    cfg = config.get()
    mode = cfg.get("kelmasync_path", consts.PATH_AUTO)
    if mode in (consts.PATH_STANDARD, consts.PATH_LEGACY):
        return mode  # manual override

    url = cfg["kelmasync_url"]
    if url in _cache:
        return _cache[url]
    _start_probe(url)
    return consts.PATH_STANDARD


def _start_probe(url: str) -> None:
    if url in _cache or url in _probing:
        return
    _probing.add(url)

    def run() -> None:
        try:
            _cache[url] = _probe(url)
        finally:
            _probing.discard(url)

    threading.Thread(target=run, daemon=True).start()


def _probe(base_url: str) -> str:
    try:
        import requests  # bundled with Anki

        endpoint = base_url.rstrip("/") + "/kelma/capabilities"
        resp = requests.get(endpoint, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("legacy") is True:
                return consts.PATH_LEGACY
            return consts.PATH_STANDARD
    except Exception:  # noqa: BLE001 - probe is best-effort
        pass
    # No Kelma capabilities endpoint (or unreachable): the standard per-deck path
    # works against any modern Anki sync server, so prefer it.
    return consts.PATH_STANDARD


def clear_cache() -> None:
    _cache.clear()
