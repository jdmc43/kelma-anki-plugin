"""Toggleable UI / visual modifications.

Everything the add-on changes about Anki's interface that a user might want to
turn off lives here as a registered feature. To add a new modification:

  1. append a `Feature(...)` to `FEATURES` below, and
  2. gate your code on `features.enabled("your_key")`.

It then appears automatically in **Tools → Kelma → Display modifications** as a
checkable item — no menu wiring needed. State is stored in the add-on config
under `features`, defaulting to each feature's `default`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    default: bool = True


# Registry of toggleable visual modifications. Add new ones here.
FEATURES: list[Feature] = [
    Feature(
        key="deck_badges",
        label="Deck sync badges",
        description="Show each deck's sync state on the deck list.",
        default=True,
    ),
    Feature(
        key="brand_logo",
        label="ForkiCards star logo",
        description="Show the ForkiCards blue-star logo in the sync menu, Tools menu, and settings.",
        default=True,
    ),
    Feature(
        key="brand_theme",
        label="Kelma theme & name",
        description="Use Kelma green accents and the Kelma name across the add-on's UI.",
        default=True,
    ),
]

_BY_KEY = {f.key: f for f in FEATURES}


def enabled(key: str) -> bool:
    feat = _BY_KEY.get(key)
    default = feat.default if feat else True
    return bool(config.get().get("features", {}).get(key, default))


def set_enabled(key: str, value: bool) -> None:
    cfg = config.get()
    cfg.setdefault("features", {})[key] = bool(value)
    config.save(cfg)
