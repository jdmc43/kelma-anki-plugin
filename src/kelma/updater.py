"""Checksum-verified update checks for the standalone Kelma Anki add-on."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import time
import urllib.request
from concurrent.futures import Future
from typing import Any

from aqt import mw
from aqt.qt import QTimer
from aqt.utils import askUser, showInfo, showWarning, tooltip

from . import config, consts

_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_checking = False


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value.strip())
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def _fetch_manifest() -> dict[str, Any]:
    request = urllib.request.Request(
        consts.UPDATE_MANIFEST_URL,
        headers={
            "User-Agent": f"Kelma Anki add-on/{consts.KELMA_CLIENT_VERSION}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("update manifest is unexpectedly large")
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise RuntimeError("unsupported Kelma update manifest")
    return data


def _addon_update(manifest: dict[str, Any]) -> dict[str, Any]:
    item = manifest.get("addon")
    if not isinstance(item, dict):
        raise RuntimeError("add-on update metadata is missing")
    version = str(item.get("version") or "")
    url = str(item.get("url") or "")
    sha256 = str(item.get("sha256") or "").lower()
    if not version or not url.startswith("https://") or not _SHA256_RE.fullmatch(sha256):
        raise RuntimeError("invalid add-on update metadata")
    return {
        "version": version,
        "url": url,
        "sha256": sha256,
        "size": int(item.get("size") or 0),
    }


def _record_check() -> None:
    cfg = config.get()
    cfg["update_last_check"] = int(time.time())
    config.save(cfg)


def _download_addon(item: dict[str, Any]) -> bytes:
    request = urllib.request.Request(
        item["url"],
        headers={"User-Agent": f"Kelma Anki add-on/{consts.KELMA_CLIENT_VERSION}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise RuntimeError("add-on update is unexpectedly large")
    actual = hashlib.sha256(data).hexdigest()
    if actual != item["sha256"]:
        raise RuntimeError(
            f"add-on checksum mismatch (expected {item['sha256']}, got {actual})"
        )
    return data


def _install_download(future: Future) -> None:
    try:
        data = future.result()
        result = mw.addonManager.install(io.BytesIO(data), force_enable=True)
        error = getattr(result, "errmsg", "")
        if error:
            raise RuntimeError(f"Anki rejected the add-on package: {error}")
    except Exception as err:  # noqa: BLE001
        showWarning(f"Could not install the Kelma update:\n\n{err}", parent=mw)
        return
    showInfo(
        "Kelma was updated successfully. Restart Anki before syncing again.",
        parent=mw,
    )


def _prompt_and_download(item: dict[str, Any]) -> None:
    if not askUser(
        f"Kelma add-on {item['version']} is available.\n\n"
        f"Installed version: {consts.KELMA_CLIENT_VERSION}\n"
        f"Download size: {item['size'] / (1024 * 1024):.1f} MB\n\n"
        "Download, verify, and install it now? Anki must be restarted afterward.",
        parent=mw,
    ):
        return
    mw.taskman.run_in_background(lambda: _download_addon(item), _install_download)


def check_for_update(*, manual: bool = False) -> None:
    """Check Desktop core or standalone add-on updates without blocking the UI."""
    global _checking
    if config.kelmasync_only():
        try:
            from aqt._kelma_update import check_for_kelma_update

            check_for_kelma_update(mw, manual=manual)
        except Exception as err:  # noqa: BLE001
            if manual:
                showWarning(f"Could not start the Kelma update check:\n\n{err}", parent=mw)
        return
    if _checking:
        if manual:
            tooltip("A Kelma update check is already running.", parent=mw)
        return
    _checking = True

    def done(future: Future) -> None:
        global _checking
        _checking = False
        _record_check()
        try:
            item = _addon_update(future.result())
        except Exception as err:  # noqa: BLE001
            if manual:
                showWarning(f"Could not check for Kelma updates:\n\n{err}", parent=mw)
            else:
                print(f"Kelma update check failed: {err}")
            return
        if _version_tuple(item["version"]) <= _version_tuple(
            consts.KELMA_CLIENT_VERSION
        ):
            if manual:
                tooltip(
                    f"Kelma {consts.KELMA_CLIENT_VERSION} is up to date.", parent=mw
                )
            return
        _prompt_and_download(item)

    mw.taskman.run_in_background(_fetch_manifest, done)


def schedule_automatic_check() -> None:
    cfg = config.get()
    # KelmaDesktop core owns its daily check; avoid a duplicate add-on timer.
    if config.kelmasync_only():
        return
    if cfg.get("check_for_updates", True) is False:
        return
    last = int(cfg.get("update_last_check") or 0)
    if time.time() - last < _CHECK_INTERVAL_SECONDS:
        return
    QTimer.singleShot(5000, lambda: check_for_update(manual=False))
