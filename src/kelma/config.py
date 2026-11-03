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
    "kelmasync_url",
    "kelmasync_hkey",
    "kelmasync_user",
    "kelmasync_path",
    "ankiweb_hkey",
    "ankiweb_user",
    "sync_media",
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
    "kelmasync_url": consts.DEFAULT_KELMA_URL,
    "kelmasync_hkey": "",
    "kelmasync_user": "",
    "kelmasync_path": consts.PATH_AUTO,
    "ankiweb_hkey": "",
    "ankiweb_user": "",
    "sync_media": True,
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
    cfg.setdefault("kelmasync_url", consts.DEFAULT_KELMA_URL)
    cfg.setdefault("kelmasync_hkey", "")
    cfg.setdefault("kelmasync_user", "")
    cfg.setdefault("kelmasync_path", consts.PATH_AUTO)
    cfg.setdefault("ankiweb_hkey", "")
    cfg.setdefault("ankiweb_user", "")
    cfg.setdefault("sync_media", True)
    cfg.setdefault("wrap_sync_button", True)
    cfg.setdefault("block_native_sync", True)
    cfg.setdefault("backup_before_sync", True)
    cfg.setdefault("deck_routing", {})
    cfg.setdefault("features", {})
    # KelmaSync-only mode: hide every AnkiWeb surface (account, sync options,
    # routing column). Off by default — the standalone plugin is dual-sync; the
    # KelmaDesktop also enforces this at runtime so shared Anki settings cannot
    # re-enable native sync inside the Desktop process.
    cfg.setdefault("kelmasync_only", False)
    # KelmaSync v2 experimental REST client config. Kept separate from v1 hkey
    # auth so the existing sync path remains untouched.
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
    # A fresh Anki add-on install must not interpret an empty routing map as
    # "publish every Anki deck to KelmaSync". Only an explicit routing map (or
    # the new persisted flag) counts as initialized; an old sync timestamp does
    # not make the unsafe implicit-all scope explicit. Such profiles go through
    # the AnkiWeb → KelmaSync → deck-picker flow once after upgrading.
    if not cfg.get("v2_routing_initialized", False):
        cfg["v2_routing_initialized"] = bool(cfg.get("deck_routing"))
    cfg.setdefault("v2_unrouted_decks_local", False)
    return cfg


def kelmasync_only() -> bool:
    # KelmaDesktop and regular Anki can share one profile/add-ons directory.
    # Force Desktop-only routing in memory without persisting it into the
    # standalone Anki plugin's configuration.
    return _running_in_kelma_desktop() or bool(
        get().get("kelmasync_only", False)
    )


def account_provider() -> str | None:
    if kelmasync_only():
        return accounts.KELMA
    try:
        return accounts.provider_for(mw.pm.base, mw.pm.name)
    except (AttributeError, TypeError):
        return None


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
    mw.addonManager.writeConfig(ADDON, global_cfg)


def migrate_account_config() -> None:
    """Move legacy global Kelma credentials into the current profile once."""
    if _read_profile_account() is None:
        save(get())


def set_value(key: str, value: Any) -> None:
    cfg = get()
    cfg[key] = value
    save(cfg)


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
    cfg = get()
    if service == consts.KELMA:
        # KelmaSync v2 stores a bearer token. The legacy host key may remain on
        # upgraded profiles, but fresh Desktop/Mobile-era logins only have
        # v2_token and must still show badges/account state.
        return bool(cfg.get("v2_token") or cfg.get("kelmasync_hkey"))
    # v2 uses Anki's native profile authentication. Fresh Windows installs do
    # not populate the obsolete add-on ankiweb_hkey field.
    return has_native_ankiweb_auth() or bool(cfg["ankiweb_hkey"])


def _normalize(services: Any) -> tuple[str, ...]:
    if not isinstance(services, (list, tuple)):
        return ()
    return tuple(s for s in consts.SERVICES if s in services)


def v2_routing_initialized() -> bool:
    return bool(get().get("v2_routing_initialized", False))


def services_for_deck(deck_name: str) -> tuple[str, ...]:
    """Every deck belongs only to the current profile's provider."""
    provider = account_provider()
    if provider in accounts.PROVIDERS:
        return (provider,)
    return ()


def legacy_services_for_deck(deck_name: str) -> tuple[str, ...]:
    """Read old per-deck routing for migration/diagnostic compatibility."""
    cfg = get()
    routing: dict[str, Any] = cfg["deck_routing"]
    if deck_name in routing:
        services = _normalize(routing[deck_name])
    else:
        services = ()
        parts = deck_name.split("::")
        for i in range(len(parts) - 1, 0, -1):
            ancestor = "::".join(parts[:i])
            if ancestor in routing:
                services = _normalize(routing[ancestor])
                break
        else:
            if not cfg.get("v2_unrouted_decks_local", False):
                services = consts.DEFAULT_SERVICES
    if has_native_ankiweb_auth() and consts.ANKIWEB not in services:
        services = tuple(
            service for service in consts.SERVICES
            if service in services or service == consts.ANKIWEB
        )
    return services


def decks_for_service(service: str, all_deck_names: list[str]) -> list[str]:
    return [n for n in all_deck_names if service in services_for_deck(n)]


def active_services(all_deck_names: list[str]) -> tuple[str, ...]:
    """Services to actually sync: enabled, have credentials, and at least one
    deck routes to them."""
    if not get()["enabled"]:
        return ()
    out = []
    for s in consts.SERVICES:
        if has_credentials(s) and decks_for_service(s, all_deck_names):
            out.append(s)
    return tuple(out)
