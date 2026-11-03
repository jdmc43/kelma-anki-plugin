"""Profile-backed cloud accounts with strictly separate collections.

Each Anki profile is one local account and owns one collection. An account is
assigned to exactly one provider: AnkiWeb or KelmaSync. The registry contains no
credentials; AnkiWeb authentication remains in Anki's profile and Kelma tokens
remain in that profile's add-on config.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ANKIWEB = "ankiweb"
KELMA = "kelma"
PROVIDERS = (ANKIWEB, KELMA)
REGISTRY_VERSION = 1


@dataclass(frozen=True)
class Account:
    profile: str
    provider: str | None


def _registry_path(base: str | os.PathLike[str]) -> Path:
    return Path(base) / "kelma-accounts.json"


def load_registry(base: str | os.PathLike[str]) -> dict[str, str]:
    try:
        raw: Any = json.loads(_registry_path(base).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != REGISTRY_VERSION:
        return {}
    records = raw.get("accounts")
    if not isinstance(records, dict):
        return {}
    return {
        str(profile): str(provider)
        for profile, provider in records.items()
        if str(profile).strip() and provider in PROVIDERS
    }


def save_registry(base: str | os.PathLike[str], records: dict[str, str]) -> None:
    path = _registry_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": REGISTRY_VERSION,
        "accounts": {
            str(profile): provider
            for profile, provider in sorted(records.items())
            if str(profile).strip() and provider in PROVIDERS
        },
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".kelma-accounts-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
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


def accounts(base: str | os.PathLike[str], profiles: list[str]) -> list[Account]:
    records = load_registry(base)
    return [Account(profile=name, provider=records.get(name)) for name in sorted(profiles)]


def provider_for(base: str | os.PathLike[str], profile: str) -> str | None:
    return load_registry(base).get(profile)


def set_provider(base: str | os.PathLike[str], profile: str, provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("Account provider must be ankiweb or kelma")
    if not profile.strip():
        raise ValueError("Account profile is blank")
    records = load_registry(base)
    records[profile] = provider
    save_registry(base, records)


def collection_path(base: str | os.PathLike[str], profile: str) -> Path:
    return Path(base) / profile / "collection.anki2"
