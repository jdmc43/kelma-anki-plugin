"""Pure helpers for validating independent comparison snapshots."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def count_matching_items_missing_from_source(
    manifests: dict[str, dict[str, Any]],
    *,
    missing_source: str,
    agreement_sources: tuple[str, str],
    resources: Iterable[str],
    key_for: Callable[[str, dict[str, Any]], str],
    fingerprint_for: Callable[[str, dict[str, Any] | None], Any],
) -> int:
    """Count absent items whose two other source fingerprints agree exactly."""
    left_source, right_source = agreement_sources
    total = 0
    for resource in resources:
        maps = {
            source: {
                key_for(resource, item): item
                for item in manifest.get(resource, [])
            }
            for source, manifest in manifests.items()
        }
        left = maps[left_source]
        right = maps[right_source]
        missing = maps[missing_source]
        for key in set(left) & set(right):
            if key in missing:
                continue
            if fingerprint_for(resource, left[key]) == fingerprint_for(
                resource, right[key]
            ):
                total += 1
    return total
