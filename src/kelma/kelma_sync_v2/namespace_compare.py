"""Aggregate pure deck-path mismatches by exact logical-card membership."""
from __future__ import annotations

import hashlib
from typing import Any


def prepare_deck_namespace_comparison(
    manifests: dict[str, dict[str, Any]],
) -> int:
    """Replace one path mismatch with one synthetic deck comparison row.

    Pairing is based on a SHA-256 of the complete set of ``(note_guid, ord)``
    logical card keys in each deck. Every source must contain exactly one deck
    with that membership. Cards are tagged with a shared comparison namespace,
    so the full-path difference does not become one difference per card.

    Empty deck shells at one of the matched paths are removed from comparison.
    Returns the number of synthetic namespace rows added per source.
    """
    if not manifests:
        return 0

    deck_items: dict[str, dict[str, dict[str, Any]]] = {}
    memberships: dict[str, dict[str, set[str]]] = {}
    actual_names: dict[str, dict[str, str]] = {}
    signatures: dict[str, dict[str, list[str]]] = {}
    for source, manifest in manifests.items():
        deck_items[source] = {
            str(item.get("name", "")).casefold(): item
            for item in manifest.get("decks", [])
            if item.get("name")
        }
        memberships[source] = {}
        actual_names[source] = {}
        for card in manifest.get("cards", []):
            deck_name = str(card.get("deck_name", ""))
            logical = str(
                card.get("logical_key")
                or f"{card.get('note_guid', '')}:{int(card.get('ord', 0) or 0)}"
            )
            if deck_name and logical:
                folded = deck_name.casefold()
                memberships[source].setdefault(folded, set()).add(logical)
                actual_names[source][folded] = deck_name
        signatures[source] = {}
        for folded, keys in memberships[source].items():
            signature = hashlib.sha256(
                "\n".join(sorted(keys)).encode("utf-8")
            ).hexdigest()
            signatures[source].setdefault(signature, []).append(folded)

    common_signatures = set.intersection(
        *(set(source_signatures) for source_signatures in signatures.values())
    )
    removed: dict[str, set[str]] = {source: set() for source in manifests}
    synthetic: dict[str, list[dict[str, Any]]] = {
        source: [] for source in manifests
    }
    namespace_count = 0
    for signature in sorted(common_signatures):
        matched = {
            source: source_signatures[signature]
            for source, source_signatures in signatures.items()
        }
        if any(len(names) != 1 for names in matched.values()):
            continue
        folded_names = {source: names[0] for source, names in matched.items()}
        if len(set(folded_names.values())) == 1:
            continue
        items = {
            source: deck_items[source].get(folded)
            for source, folded in folded_names.items()
        }
        if any(item is None for item in items.values()):
            continue

        first_source = next(iter(manifests))
        card_count = len(memberships[first_source][folded_names[first_source]])
        group_paths = {
            actual_names[source][folded]
            for source, folded in folded_names.items()
        }
        all_group_paths = {name.casefold() for name in group_paths}
        for source, manifest in manifests.items():
            folded = folded_names[source]
            item = dict(items[source] or {})
            item["namespace_group"] = signature
            item["namespace_name"] = actual_names[source][folded]
            item["namespace_card_count"] = card_count
            item["namespace_paths"] = sorted(group_paths, key=str.casefold)
            synthetic[source].append(item)
            removed[source].add(folded)
            for card in manifest.get("cards", []):
                if str(card.get("deck_name", "")).casefold() == folded:
                    card["comparison_namespace_group"] = signature
            for path in all_group_paths:
                if not memberships[source].get(path):
                    removed[source].add(path)
        namespace_count += 1

    if namespace_count:
        for source, manifest in manifests.items():
            manifest["decks"] = [
                item
                for item in manifest.get("decks", [])
                if str(item.get("name", "")).casefold() not in removed[source]
            ] + synthetic[source]
    return namespace_count
