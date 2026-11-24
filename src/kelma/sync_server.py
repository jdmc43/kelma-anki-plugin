"""Which KelmaSync server a profile syncs with, and switching it safely.

Every Anki profile keeps its own server URL and sign-in (in the profile's
`kelma-cloud-account.json`), so one profile can sync with Kelma's server and
another with a self-hosted one. Accounts on a self-hosted server are created
however its host chooses; the add-on only signs in.

Switching a profile to another server signs it out and forgets everything that
described the old server: the "changes since" checkpoint, the cached server
manifest, the last-sync snapshot used to detect deletions, and the deck-badge
baseline. The collection itself stays as it is; the next sync merges it with
whatever the account on the new server already holds.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import consts

# Files next to the collection (profile folder) and in its media folder that
# only make sense for the server that produced them.
_PROFILE_FILES = ("kelma_server_manifest.json.gz",)
_MEDIA_FILES = ("kelma_sync_v2_state.json",)
# Collection settings recorded from one server's account.
_COLLECTION_KEYS = (
    "kelmaReviewsVerified",
    "kelmaSchedulerProfile",
    "kelmaStudyDayAlignment",
)
# Account values that belong to one server.
_ACCOUNT_RESET = {
    "v2_username": "",
    "v2_token": "",
    "v2_client_id": "",
    "v2_last_server_time": "",
    "v2_cursor_url": "",
    "v2_allow_large_deletes": False,
}


class ServerCheckError(Exception):
    """The address does not answer like a KelmaSync server."""


def normalize(text: str) -> str:
    """A canonical server URL, or ValueError with a message for the user."""
    value = str(text or "").strip()
    if not value:
        raise ValueError("Enter the server's address, for example https://sync.example.org.")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("The address must start with https:// (or http:// on your own network).")
    if not parts.hostname:
        raise ValueError("That address has no server name.")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("Enter only the server's address, without ?, # or a login.")
    try:
        port = parts.port
    except ValueError as err:
        raise ValueError("The port number is not valid.") from err
    host = parts.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port and not (parts.scheme == "https" and port == 443) and not (
        parts.scheme == "http" and port == 80
    ):
        netloc += f":{port}"
    path = parts.path.rstrip("/")
    url = f"{parts.scheme}://{netloc}{path}"
    return consts.DEFAULT_V2_URL if url in consts.LEGACY_V2_URLS else url


def is_default(url: str) -> bool:
    try:
        return normalize(url) == consts.DEFAULT_V2_URL
    except ValueError:
        return False


def host(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return parts.netloc or str(url or "")


def label(url: str) -> str:
    """'Kelma' for the default server, otherwise its host name."""
    return "Kelma" if is_default(url) else host(url)


def is_local(url: str) -> bool:
    name = (urlsplit(str(url or "")).hostname or "").lower()
    if name == "localhost" or name.endswith(".local") or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def is_unencrypted(url: str) -> bool:
    return urlsplit(str(url or "")).scheme == "http"


def check(url: str, timeout: float = 10.0) -> None:
    """Raise ServerCheckError unless `url` answers KelmaSync's health check."""
    try:
        request = Request(
            normalize(url) + "/health",
            headers={"Accept": "application/json", "User-Agent": "Kelma Anki add-on"},
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-chosen server
            status = int(getattr(response, "status", 200))
            body = response.read(4096)
    except ValueError as err:
        raise ServerCheckError(str(err)) from err
    except Exception as err:  # noqa: BLE001 - network errors of every kind
        reason = getattr(err, "reason", None) or err
        if "SSL" in type(reason).__name__ or "SSL" in str(reason):
            raise ServerCheckError(
                "The server did not answer over https://. If it only offers http:// "
                "(for example on your own network), type the address with http:// in front."
            ) from err
        raise ServerCheckError(f"Could not reach the server ({reason}).") from err
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ServerCheckError(
            "The server answered, but not like a KelmaSync server. Check the address."
        )


def switched_account(cfg: dict[str, Any], new_url: str) -> dict[str, Any]:
    """Account values for `new_url`: signed out, with no sync checkpoint."""
    updated = dict(cfg)
    updated.update(_ACCOUNT_RESET)
    updated["v2_url"] = normalize(new_url)
    return updated


def reset_server_state(profile_dir: str | os.PathLike[str], col=None) -> list[str]:
    """Forget what the profile knew about its previous server.

    Returns a short description of everything that was reset.
    """
    reset: list[str] = []
    folder = Path(profile_dir)
    for name in _PROFILE_FILES:
        if _remove(folder / name):
            reset.append(name)
    if col is not None:
        try:
            media_dir = Path(col.media.dir())
        except Exception:  # noqa: BLE001 - collection closing
            media_dir = None
        if media_dir is not None:
            for name in _MEDIA_FILES:
                if _remove(media_dir / name):
                    reset.append(name)
        for key in _COLLECTION_KEYS:
            try:
                if col.get_config(key, None) is not None:
                    col.remove_config(key)
                    reset.append(key)
            except Exception:  # noqa: BLE001 - settings are optional
                pass
    if _forget_badge_baseline(folder / "kelma_state.json"):
        reset.append("deck badge baseline")
    return reset


def _remove(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _forget_badge_baseline(path: Path) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    syncs = state.get("meta") if isinstance(state, dict) else None
    if not isinstance(syncs, dict) or consts.KELMA not in syncs:
        return False
    syncs.pop(consts.KELMA)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)
    return True
