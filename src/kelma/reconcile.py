"""Cross-collection deck reconciliation.

Reconciling = copying the cards/notes/scheduling/media of a set of decks from one
collection into another, newest-wins, using Anki's own package import/export.
`update_notes = IF_NEWER` gives GUID-based, mtime-based merge for free, so we don't
hand-roll a merge engine.
"""

from __future__ import annotations

import os
import tempfile

from anki.collection import (
    Collection,
    CardIdsLimit,
    ExportAnkiPackageOptions,
    ImportAnkiPackageOptions,
    ImportAnkiPackageRequest,
)
from anki import import_export_pb2 as ie

# IF_NEWER: only overwrite an existing note/notetype when the incoming one is newer.
IF_NEWER = ie.ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_IF_NEWER


def _card_ids_for_decks(col: Collection, deck_names: list[str]) -> list[int]:
    present = [n for n in deck_names if col.decks.id_for_name(n) is not None]
    if not present:
        return []
    query = " OR ".join(f'deck:"{name}"' for name in present)
    return list(col.find_cards(query))


def _export_import(
    src: Collection,
    dst: Collection,
    card_ids: list[int],
    with_media: bool,
    merge_notetypes: bool,
) -> None:
    tmpdir = tempfile.mkdtemp(prefix="kelma_reconcile_")
    pkg = os.path.join(tmpdir, "deck.apkg")
    try:
        src.export_anki_package(
            out_path=pkg,
            options=ExportAnkiPackageOptions(
                with_scheduling=True,
                with_deck_configs=True,
                with_media=with_media,
                legacy=False,
            ),
            limit=CardIdsLimit(card_ids=card_ids),
        )
        dst.import_anki_package(
            ImportAnkiPackageRequest(
                package_path=pkg,
                options=ImportAnkiPackageOptions(
                    merge_notetypes=merge_notetypes,
                    update_notes=IF_NEWER,
                    update_notetypes=IF_NEWER,
                    with_scheduling=True,
                    with_deck_configs=True,
                ),
            )
        )
    finally:
        try:
            os.remove(pkg)
            os.rmdir(tmpdir)
        except OSError:
            pass


def reconcile_decks(
    src: Collection,
    dst: Collection,
    deck_names: list[str],
    with_media: bool = True,
    *,
    merge_notetypes: bool = True,
) -> int:
    """Batched reconcile: copy all requested decks' content in one apkg.

    Returns the number of cards considered (0 if nothing to do).
    """
    card_ids = _card_ids_for_decks(src, deck_names)
    if not card_ids:
        return 0
    _export_import(src, dst, card_ids, with_media, merge_notetypes)
    return len(card_ids)


def reconcile_deck(
    src: Collection,
    dst: Collection,
    deck_name: str,
    with_media: bool = True,
    *,
    merge_notetypes: bool = True,
) -> int:
    """Standard (granular) reconcile of a single deck. Returns cards copied."""
    card_ids = _card_ids_for_decks(src, [deck_name])
    if not card_ids:
        return 0
    _export_import(src, dst, card_ids, with_media, merge_notetypes)
    return len(card_ids)
