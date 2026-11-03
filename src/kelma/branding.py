"""Kelma visual branding — the star logo and accent color, gated on the
`brand_logo` / `brand_theme` feature toggles so users can turn them off.

- `brand_logo`  shows the blue-star logo in the add-on's UI surfaces.
- `brand_theme` swaps the neutral gold accent for Kelma green and applies the
  Kelma name/styling.
"""

from __future__ import annotations

import os

from aqt.qt import QIcon, QPixmap, Qt

from . import features

_ASSETS = os.path.join(os.path.dirname(__file__), "assets")
_STAR = os.path.join(_ASSETS, "kelma_star.png")

# Kelma green (theme on) vs. the neutral gold used when the theme is off.
ACCENT_GREEN = "#5cbf77"
ACCENT_GOLD = "#d9b25a"


def logo_enabled() -> bool:
    return features.enabled("brand_logo")


def theme_enabled() -> bool:
    return features.enabled("brand_theme")


def accent() -> str:
    """Hex accent color for clickable/brand cues."""
    return ACCENT_GREEN if theme_enabled() else ACCENT_GOLD


def accent_rgba(alpha: float) -> str:
    h = accent().lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def star_pixmap(height: int) -> QPixmap:
    p = QPixmap(_STAR)
    if p.isNull():
        return p
    return p.scaledToHeight(int(height), Qt.TransformationMode.SmoothTransformation)


def star_icon() -> QIcon:
    return QIcon(_STAR)
