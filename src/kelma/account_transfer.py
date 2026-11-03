"""Explicit deck transfer operations between profile-backed accounts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any

from anki.collection import Collection
from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from aqt.utils import askUser, tooltip

from . import accounts
from .reconcile import reconcile_deck
from .scheduling import sync_scheduling_between_decks


@dataclass(frozen=True)
class NoteVersion:
    guid: str
    fields: tuple[str, ...]
    tags: tuple[str, ...]
    modified: int

    @property
    def preview(self) -> str:
        text = re.sub(r"<[^>]+>", " ", self.fields[0] if self.fields else "")
        return " ".join(text.split())[:80] or "(empty note)"


@dataclass(frozen=True)
class MergeConflict:
    guid: str
    current: NoteVersion
    other: NoteVersion
    suggested: str | None


def deck_names(col: Collection) -> list[str]:
    return sorted((deck.name for deck in col.decks.all_names_and_ids()), key=str.casefold)


def _deck_ids(col: Collection, root: str) -> list[int]:
    prefix = root + "::"
    return [
        int(deck.id)
        for deck in col.decks.all_names_and_ids()
        if deck.name == root or deck.name.startswith(prefix)
    ]


def _card_ids(col: Collection, root: str) -> list[int]:
    dids = _deck_ids(col, root)
    if not dids:
        return []
    marks = ",".join("?" for _ in dids)
    return [int(row[0]) for row in col.db.all(f"SELECT id FROM cards WHERE did IN ({marks})", *dids)]


def note_versions(col: Collection, root: str) -> dict[str, NoteVersion]:
    dids = _deck_ids(col, root)
    if not dids:
        return {}
    marks = ",".join("?" for _ in dids)
    rows = col.db.all(
        f"SELECT DISTINCT n.id,n.guid,n.flds,n.tags,n.mod FROM notes n "
        f"JOIN cards c ON c.nid=n.id WHERE c.did IN ({marks}) ORDER BY n.id",
        *dids,
    )
    versions: dict[str, NoteVersion] = {}
    for _nid, guid, fields, tags, modified in rows:
        key = str(guid or "")
        if not key:
            raise ValueError(f"Deck '{root}' contains a note with an empty GUID; repair it before merging.")
        if key in versions:
            raise ValueError(f"Deck '{root}' contains duplicate note GUID {key!r}; repair it before merging.")
        versions[key] = NoteVersion(
            guid=key,
            fields=tuple(str(fields or "").split("\x1f")),
            tags=tuple(str(tags or "").split()),
            modified=int(modified or 0),
        )
    return versions


def merge_conflicts(current: Collection, other: Collection, current_deck: str, other_deck: str) -> list[MergeConflict]:
    left = note_versions(current, current_deck)
    right = note_versions(other, other_deck)
    conflicts = []
    for guid in sorted(set(left) & set(right)):
        a, b = left[guid], right[guid]
        if (a.fields, a.tags) == (b.fields, b.tags):
            continue
        suggested = "current" if a.modified > b.modified else "other" if b.modified > a.modified else None
        conflicts.append(MergeConflict(guid, a, b, suggested))
    return conflicts


def _mapped_name(source_name: str, source_root: str, target_root: str) -> str:
    return target_root + source_name[len(source_root):]


def _copy_deck(src: Collection, dst: Collection, source_root: str, target_root: str) -> tuple[int, int]:
    if source_root != target_root and dst.decks.id_for_name(source_root) is not None:
        raise ValueError(
            f"Destination already has '{source_root}'. Rename it or designate that deck as the target."
        )
    source_dids = {int(deck.id): deck.name for deck in src.decks.all_names_and_ids()}
    rows = []
    ids = _deck_ids(src, source_root)
    if ids:
        marks = ",".join("?" for _ in ids)
        rows = src.db.all(
            f"SELECT n.guid,c.ord,c.did FROM cards c JOIN notes n ON n.id=c.nid "
            f"WHERE c.did IN ({marks})",
            *ids,
        )
    # Independent accounts often have equivalent stock notetypes with different
    # IDs. Merging their schemas would append duplicate fields. Keep both model
    # definitions and resolve matching note content explicitly instead.
    allowed_target_dids = set(_deck_ids(dst, target_root))
    existing_cards = {
        (str(guid), int(ord_)): (int(cid), int(did))
        for cid, guid, ord_, did in dst.db.all(
            "SELECT c.id,n.guid,c.ord,c.did FROM cards c JOIN notes n ON n.id=c.nid"
        )
    }
    for guid, ordinal, _did in rows:
        existing = existing_cards.get((str(guid), int(ordinal)))
        if existing and existing[1] not in allowed_target_dids:
            raise ValueError(
                f"A matching card already belongs to another destination deck (GUID {guid!r}). "
                "Designate that deck as the merge/overwrite target instead."
            )

    imported = reconcile_deck(
        src, dst, source_root, with_media=True, merge_notetypes=False,
    )

    destination_cards = {
        (str(guid), int(ord_)): int(cid)
        for cid, guid, ord_ in dst.db.all(
            "SELECT c.id,n.guid,c.ord FROM cards c JOIN notes n ON n.id=c.nid"
        )
    }
    moves = []
    for guid, ordinal, did in rows:
        card_id = destination_cards.get((str(guid), int(ordinal)))
        source_name = source_dids.get(int(did), source_root)
        if card_id is not None:
            moves.append((int(dst.decks.id(_mapped_name(source_name, source_root, target_root))), card_id))
    if moves:
        dst.db.executemany("UPDATE cards SET did=?,usn=-1 WHERE id=?", moves)
        dst.db.execute("UPDATE col SET mod=?", int(time.time() * 1000))

    if source_root != target_root:
        prefix = source_root + "::"
        candidates = [
            deck for deck in dst.decks.all_names_and_ids()
            if deck.name == source_root or deck.name.startswith(prefix)
        ]
        for deck in sorted(candidates, key=lambda item: item.name.count("::"), reverse=True):
            if not dst.db.scalar("SELECT 1 FROM cards WHERE did=? LIMIT 1", int(deck.id)):
                dst.decks.remove([deck.id])

    scheduled = sync_scheduling_between_decks(src, dst, [source_root], [target_root])
    return imported, scheduled


def _delete_deck(col: Collection, root: str) -> int:
    cards = _card_ids(col, root)
    if cards:
        col.remove_cards_and_orphaned_notes(cards)
    for deck in sorted(
        [d for d in col.decks.all_names_and_ids() if d.name == root or d.name.startswith(root + "::")],
        key=lambda item: item.name.count("::"),
        reverse=True,
    ):
        if not col.db.scalar("SELECT 1 FROM cards WHERE did=? LIMIT 1", int(deck.id)):
            col.decks.remove([deck.id])
    return len(cards)


def _backup(col: Collection, base: str | Path, profile: str) -> None:
    folder = accounts.profile_dir(base, profile) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    col.create_backup(backup_folder=str(folder), force=True, wait_for_completion=True)


def insert_deck(src: Collection, dst: Collection, source_root: str) -> dict[str, int]:
    if dst.decks.id_for_name(source_root) is not None:
        raise ValueError(f"'{source_root}' already exists in the destination; use Overwrite or Merge.")
    imported, scheduled = _copy_deck(src, dst, source_root, source_root)
    return {"cards": imported, "scheduled": scheduled, "deleted": 0}


def overwrite_deck(src: Collection, dst: Collection, source_root: str, target_root: str) -> dict[str, int]:
    if dst.decks.id_for_name(target_root) is None:
        raise ValueError(f"Destination deck '{target_root}' does not exist; use Insert.")
    if source_root != target_root and dst.decks.id_for_name(source_root) is not None:
        raise ValueError(f"Destination also contains '{source_root}', so this mapping is ambiguous.")
    preferred = note_versions(src, source_root)
    deleted = _delete_deck(dst, target_root)
    imported, scheduled = _copy_deck(src, dst, source_root, target_root)
    resolved = _apply_versions(dst, target_root, preferred)
    return {
        "cards": imported,
        "scheduled": scheduled,
        "deleted": deleted,
        "resolved": resolved,
    }


def _apply_versions(col: Collection, root: str, versions: dict[str, NoteVersion]) -> int:
    dids = _deck_ids(col, root)
    if not dids or not versions:
        return 0
    marks = ",".join("?" for _ in dids)
    note_ids = {
        str(guid): int(nid)
        for nid, guid in col.db.all(
            f"SELECT DISTINCT n.id,n.guid FROM notes n JOIN cards c ON c.nid=n.id "
            f"WHERE c.did IN ({marks})",
            *dids,
        )
    }
    changed = 0
    modified = int(time.time())
    for guid, version in versions.items():
        nid = note_ids.get(guid)
        if nid is None:
            continue
        note = col.get_note(nid)
        count = len(note.fields)
        note.fields = list(version.fields[:count]) + [""] * max(0, count - len(version.fields))
        note.tags = list(version.tags)
        col.update_note(note)
        col.db.execute("UPDATE notes SET mod=?,usn=-1 WHERE id=?", modified, nid)
        changed += 1
    return changed


def merge_decks(
    current: Collection,
    other: Collection,
    current_root: str,
    other_root: str,
    choices: dict[str, str],
) -> dict[str, int]:
    current_before = note_versions(current, current_root)
    other_before = note_versions(other, other_root)
    imported_a, scheduled_a = _copy_deck(current, other, current_root, other_root)
    imported_b, scheduled_b = _copy_deck(other, current, other_root, current_root)

    selected: dict[str, NoteVersion] = {}
    for guid in set(current_before) & set(other_before):
        side = choices.get(guid)
        if side == "current":
            selected[guid] = current_before[guid]
        elif side == "other":
            selected[guid] = other_before[guid]
    resolved = _apply_versions(current, current_root, selected)
    _apply_versions(other, other_root, selected)
    return {
        "cards": imported_a + imported_b,
        "scheduled": scheduled_a + scheduled_b,
        "deleted": 0,
        "resolved": resolved,
    }


class ConflictResolutionDialog(QDialog):
    def __init__(self, parent, conflicts: list[MergeConflict], current_name: str, other_name: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resolve deck merge conflicts")
        self.resize(900, 500)
        outer = QVBoxLayout(self)
        hint = QLabel(
            "Choose the version to keep in both accounts. Newest is preselected; tied modifications require a choice."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self.table = QTableWidget(len(conflicts), 5)
        self.table.setHorizontalHeaderLabels(["Note", current_name, other_name, "Modified", "Keep"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._conflicts = conflicts
        self._choices: list[QComboBox] = []
        for row, conflict in enumerate(conflicts):
            self.table.setItem(row, 0, QTableWidgetItem(conflict.guid or "(empty GUID)"))
            self.table.setItem(row, 1, QTableWidgetItem(conflict.current.preview))
            self.table.setItem(row, 2, QTableWidgetItem(conflict.other.preview))
            self.table.setItem(row, 3, QTableWidgetItem(f"{conflict.current.modified} / {conflict.other.modified}"))
            choice = QComboBox()
            choice.addItem("Choose…", None)
            choice.addItem(current_name, "current")
            choice.addItem(other_name, "other")
            if conflict.suggested:
                choice.setCurrentIndex(choice.findData(conflict.suggested))
            self.table.setCellWidget(row, 4, choice)
            self._choices.append(choice)
        outer.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_complete)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _accept_if_complete(self) -> None:
        if any(choice.currentData() not in ("current", "other") for choice in self._choices):
            tooltip("Choose which version to keep for every conflict.", parent=self)
            return
        self.accept()

    def choices(self) -> dict[str, str]:
        return {
            conflict.guid: str(choice.currentData())
            for conflict, choice in zip(self._conflicts, self._choices)
        }


class AccountTransferDialog(QDialog):
    def __init__(self, parent, preselected_profile: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transfer decks between accounts")
        self.resize(680, 360)
        outer = QVBoxLayout(self)
        intro = QLabel(
            "Insert copies a missing deck. Overwrite replaces a target deck with the preferred version. "
            "Merge designates two decks as the same and resolves differing notes into both accounts."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)
        form = QGridLayout()
        form.addWidget(QLabel("Other account:"), 0, 0)
        self.profile = QComboBox()
        profiles = [
            profile
            for profile in mw.pm.profiles()
            if profile != mw.pm.name
            and accounts.provider_for(mw.pm.base, profile) in accounts.PROVIDERS
            and accounts.collection_path(mw.pm.base, profile).exists()
        ]
        for profile in profiles:
            self.profile.addItem(profile, profile)
        if preselected_profile:
            index = self.profile.findData(preselected_profile)
            if index >= 0:
                self.profile.setCurrentIndex(index)
        self.profile.currentIndexChanged.connect(self._load_other_decks)
        form.addWidget(self.profile, 0, 1)
        self.current_deck_label = QLabel(f"Deck in {mw.pm.name}:")
        form.addWidget(self.current_deck_label, 1, 0)
        self.current_deck = QComboBox()
        self.current_deck.addItems(deck_names(mw.col))
        form.addWidget(self.current_deck, 1, 1)
        self.other_deck_label = QLabel("Deck in other account:")
        form.addWidget(self.other_deck_label, 2, 0)
        self.other_deck = QComboBox()
        form.addWidget(self.other_deck, 2, 1)
        form.addWidget(QLabel("Operation:"), 3, 0)
        self.operation = QComboBox()
        self.operation.addItem("Insert", "insert")
        self.operation.addItem("Overwrite", "overwrite")
        self.operation.addItem("Merge", "merge")
        self.operation.currentIndexChanged.connect(self._operation_changed)
        form.addWidget(self.operation, 3, 1)
        self.preferred_label = QLabel("Preferred/source version:")
        form.addWidget(self.preferred_label, 4, 0)
        self.preferred = QComboBox()
        self.preferred.addItem(mw.pm.name, "current")
        self.preferred.addItem("Other account", "other")
        self.preferred.currentIndexChanged.connect(self._operation_changed)
        form.addWidget(self.preferred, 4, 1)
        outer.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.run_button = buttons.addButton("Continue…", QDialogButtonBox.ButtonRole.ActionRole)
        self.run_button.clicked.connect(self._run)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._load_other_decks()
        self._operation_changed()

    def _load_other_decks(self) -> None:
        self.other_deck.clear()
        profile = self.profile.currentData()
        if not profile:
            return
        other = Collection(str(accounts.collection_path(mw.pm.base, str(profile))))
        try:
            self.other_deck.addItems(deck_names(other))
        finally:
            other.close()

    def _operation_changed(self) -> None:
        operation = self.operation.currentData()
        is_insert = operation == "insert"
        is_merge = operation == "merge"
        source_is_current = self.preferred.currentData() == "current"
        self.preferred.setEnabled(not is_merge)

        # Insert has no target-deck selection: the selected source deck is
        # copied under its existing name and must be absent from the target.
        # Show only the selector belonging to the chosen source account.
        show_current = not is_insert or source_is_current
        show_other = not is_insert or not source_is_current
        self.current_deck_label.setVisible(show_current)
        self.current_deck.setVisible(show_current)
        self.other_deck_label.setVisible(show_other)
        self.other_deck.setVisible(show_other)
        if is_insert:
            self.current_deck_label.setText(f"Deck to insert from {mw.pm.name}:")
            self.other_deck_label.setText("Deck to insert from other account:")
            self.preferred_label.setText("Source account:")
        else:
            self.current_deck_label.setText(f"Deck in {mw.pm.name}:")
            self.other_deck_label.setText("Deck in other account:")
            self.preferred_label.setText("Preferred/source version:")

    def _run(self) -> None:
        profile = self.profile.currentData()
        current_root = self.current_deck.currentText()
        other_root = self.other_deck.currentText()
        operation = self.operation.currentData()
        preferred = self.preferred.currentData()
        if operation == "insert":
            has_required_decks = bool(
                current_root if preferred == "current" else other_root
            )
        else:
            has_required_decks = bool(current_root and other_root)
        if not profile or not has_required_decks:
            tooltip("Select both accounts and the required decks.", parent=self)
            return

        choices: dict[str, str] = {}
        if operation == "merge":
            other = Collection(str(accounts.collection_path(mw.pm.base, str(profile))))
            try:
                conflicts = merge_conflicts(mw.col, other, current_root, other_root)
            finally:
                other.close()
            if conflicts:
                resolver = ConflictResolutionDialog(self, conflicts, mw.pm.name, str(profile))
                if resolver.exec() != QDialog.DialogCode.Accepted:
                    return
                choices = resolver.choices()

        description = {
            "insert": f"Insert the preferred deck into the other account without changing the source?",
            "overwrite": "Permanently replace the target deck? A backup will be created first.",
            "merge": "Merge these designated decks into both accounts? Backups will be created first.",
        }[str(operation)]
        if not askUser(description + "\n\nCloud sync will not run automatically.", parent=self):
            return

        self.run_button.setEnabled(False)
        self.status.setText("Creating backups and transferring deck data…")
        path = accounts.collection_path(mw.pm.base, str(profile))
        base = mw.pm.base
        current_profile = mw.pm.name

        def work() -> dict[str, int]:
            other = Collection(str(path))
            try:
                if operation == "insert":
                    if preferred == "current":
                        _backup(other, base, str(profile))
                        return insert_deck(mw.col, other, current_root)
                    _backup(mw.col, base, current_profile)
                    return insert_deck(other, mw.col, other_root)
                if operation == "overwrite":
                    if preferred == "current":
                        _backup(other, base, str(profile))
                        return overwrite_deck(mw.col, other, current_root, other_root)
                    _backup(mw.col, base, current_profile)
                    return overwrite_deck(other, mw.col, other_root, current_root)
                _backup(mw.col, base, current_profile)
                _backup(other, base, str(profile))
                return merge_decks(mw.col, other, current_root, other_root, choices)
            finally:
                other.close()

        def done(future: Any) -> None:
            self.run_button.setEnabled(True)
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001
                self.status.setText(f"Transfer failed: {error}")
                return
            mw.reset()
            self.status.setText(
                f"Complete: {result.get('cards', 0)} cards considered, "
                f"{result.get('deleted', 0)} replaced, {result.get('scheduled', 0)} schedules reconciled. "
                "Sync each account explicitly when ready."
            )

        mw.taskman.run_in_background(work, done, uses_collection=True)
