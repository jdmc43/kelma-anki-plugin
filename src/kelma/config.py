"""Global UI preferences plus profile-scoped cloud account credentials."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from aqt import mw

from . import accounts, consts

# The add-on's package/dir name (e.g. "kelma" or a numeric AnkiWeb id).
ADDON = __name__.split(".")[0]

_ACCOUNT_KEYS = {
    "v2_url",
    "v2_username",
    "v2_token",
    "v2_client_id",
    "v2_client_label",
    "v2_last_server_time",
    "v2_allow_large_deletes",
    "v2_scheduling_mode",
}
_ACCOUNT_DEFAULTS: dict[str, Any] = {
    "v2_url": consts.DEFAULT_V2_URL,
    "v2_username": "",
    "v2_token": "",
    "v2_client_id": "",
    "v2_client_label": "Anki plugin",
    "v2_last_server_time": "",
    "v2_allow_large_deletes": False,
    "v2_scheduling_mode": "server_fsrs",
}
_ACCOUNT_CONFIG_FILENAME = "kelma-cloud-account.json"

# Settings of the retired dual-sync releases (hidden per-service collection
# copies, v1 host-key logins, per-deck routing). Nothing reads them any more;
# `save` removes them so old host keys don't linger in the add-on config.
_LEGACY_KEYS = (
    "kelmasync_url",
    "kelmasync_hkey",
    "kelmasync_user",
    "kelmasync_path",
    "ankiweb_hkey",
    "ankiweb_user",
    "sync_media",
    "backup_before_sync",
    "deck_routing",
    "v2_routing_initialized",
    "v2_unrouted_decks_local",
)


def _profile_account_path() -> Path | None:
    try:
        return Path(mw.pm.profileFolder()) / _ACCOUNT_CONFIG_FILENAME
    except (AttributeError, TypeError):
        return None


def _read_profile_account() -> dict[str, Any] | None:
    path = _profile_account_path()
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    values = raw.get("values")
    return values if isinstance(values, dict) else None


def _write_profile_account(values: dict[str, Any]) -> None:
    path = _profile_account_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".kelma-cloud-account-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "values": values}, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _running_in_kelma_desktop() -> bool:
    """Detect Desktop at runtime without changing the shared profile config."""
    try:
        from aqt import _kelma_bundled
    except (ImportError, AttributeError):
        return False
    return bool(getattr(_kelma_bundled, "IS_KELMA_DESKTOP", False))


def get() -> dict[str, Any]:
    cfg = mw.addonManager.getConfig(ADDON) or {}
    profile_values = _read_profile_account()
    if profile_values is not None:
        cfg.update({key: profile_values[key] for key in _ACCOUNT_KEYS if key in profile_values})
    else:
        legacy_owner = str(cfg.get("v2_legacy_profile") or "")
        current_profile = str(getattr(getattr(mw, "pm", None), "name", "") or "")
        if legacy_owner and legacy_owner != current_profile:
            cfg.update(_ACCOUNT_DEFAULTS)
    cfg.setdefault("enabled", True)
    cfg.setdefault("wrap_sync_button", True)
    cfg.setdefault("block_native_sync", True)
    cfg.setdefault("features", {})
    # KelmaSync-only mode: every profile is a KelmaSync account and native sync
    # is never used. Off by default in Anki; KelmaDesktop also enforces this at
    # runtime so shared Anki settings cannot re-enable native sync there.
    cfg.setdefault("kelmasync_only", False)
    cfg.setdefault("v2_url", _ACCOUNT_DEFAULTS["v2_url"])
    current_v2_url = str(cfg.get("v2_url") or "").rstrip("/")
    if not current_v2_url or current_v2_url in consts.LEGACY_V2_URLS:
        # Both hostnames reach the same service/token database. Migrate the URL
        # without discarding the saved token, so existing clients do not need to
        # sign in again.
        cfg["v2_url"] = consts.DEFAULT_V2_URL
    cfg.setdefault("v2_username", "")
    cfg.setdefault("v2_token", "")
    cfg.setdefault("v2_client_id", "")
    cfg.setdefault(
        "v2_client_label",
        "KelmaDesktop" if _running_in_kelma_desktop() else "Anki plugin",
    )
    cfg.setdefault("v2_last_server_time", "")
    cfg.setdefault("v2_allow_large_deletes", False)
    cfg.setdefault("v2_scheduling_mode", "server_fsrs")
    if cfg.get("v2_scheduling_mode") not in ("server_fsrs", "local_anki"):
        cfg["v2_scheduling_mode"] = "server_fsrs"
    return cfg


def kelmasync_only() -> bool:
    # KelmaDesktop and regular Anki can share one profile/add-ons directory.
    # Force Desktop-only mode in memory without persisting it into the
    # standalone Anki plugin's configuration.
    return _running_in_kelma_desktop() or bool(
        get().get("kelmasync_only", False)
    )


def account_provider() -> str | None:
    """KelmaSync for profiles assigned to it; every other profile is AnkiWeb."""
    if kelmasync_only():
        return accounts.KELMA
    try:
        return accounts.effective_provider(
            accounts.load_registry(mw.pm.base), mw.pm.name
        )
    except (AttributeError, TypeError):
        return None


def get_global(key: str, default: Any = None) -> Any:
    """A non-credential add-on setting shared by all profiles."""
    return (mw.addonManager.getConfig(ADDON) or {}).get(key, default)


def set_global(key: str, value: Any) -> None:
    if key in _ACCOUNT_KEYS:
        raise ValueError(f"{key} is a per-profile account setting")
    cfg = dict(mw.addonManager.getConfig(ADDON) or {})
    if cfg.get(key) == value:
        return
    cfg[key] = value
    mw.addonManager.writeConfig(ADDON, cfg)


def ui_services() -> tuple[str, ...]:
    """Only the provider assigned to the current profile is visible."""
    provider = account_provider()
    return (provider,) if provider in accounts.PROVIDERS else ()


def save(cfg: dict[str, Any]) -> None:
    account_values = {
        key: cfg.get(key, _ACCOUNT_DEFAULTS[key])
        for key in _ACCOUNT_KEYS
    }
    _write_profile_account(account_values)
    global_cfg = dict(cfg)
    current_profile = str(getattr(getattr(mw, "pm", None), "name", "") or "")
    global_cfg["v2_legacy_profile"] = current_profile
    for key, default in _ACCOUNT_DEFAULTS.items():
        global_cfg[key] = default
    for key in _LEGACY_KEYS:
        global_cfg.pop(key, None)
    mw.addonManager.writeConfig(ADDON, global_cfg)


def migrate_account_config() -> None:
    """Move legacy global Kelma credentials into the current profile once."""
    if _read_profile_account() is None:
        save(get())


def has_native_ankiweb_auth() -> bool:
    """Whether this Anki profile is logged into native collection sync."""
    if kelmasync_only():
        return False
    try:
        return bool(mw and mw.pm and mw.pm.sync_auth())
    except Exception:  # noqa: BLE001 - profile may be opening/closing
        return False


def has_credentials(service: str) -> bool:
    if service != account_provider():
        return False
    if service == consts.KELMA:
        return bool(get().get("v2_token"))
    # AnkiWeb accounts use Anki's native profile login.
    return has_native_ankiweb_auth()


def services_for_deck(deck_name: str) -> tuple[str, ...]:
    """Every deck belongs only to the current profile's provider."""
    provider = account_provider()
    if provider in accounts.PROVIDERS:
        return (provider,)
    return ()


