"""Storage breakdown dialog: per-deck cards / notes / media size, with the
ability to untrack or delete decks from either cloud."""

from __future__ import annotations

from concurrent.futures import Future

from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QColor,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    Qt,
    QVBoxLayout,
)
from aqt.utils import askUser, tooltip

from . import config, consts, deckbadges, storage

_COLOR = {consts.KELMA: "#16a34a", consts.ANKIWEB: "#2563eb"}


def _human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class StorageDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Kelma — Storage breakdown")
        self.resize(740, 600)
        self.rows: list[dict] = []
        self.total_media = 0

        layout = QVBoxLayout(self)
        self.info = QLabel("Analyzing storage…")
        layout.addWidget(self.info)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self.filter_combo = QComboBox()
        for mode, label in [
            ("all", "All"),
            ("kelma", "KelmaSync"),
            ("ankiweb", "AnkiWeb"),
            ("both", "Both (either cloud)"),
        ]:
            self.filter_combo.addItem(label, mode)
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_combo)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Deck", "Cards", "Notes", "Media", "Kelma", "AnkiWeb"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 6):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        layout.addWidget(self.table)

        layout.addWidget(
            QLabel(
                "Select deck(s), then act per cloud. <b>Untrack</b> stops syncing "
                "them to that cloud. <b>Delete</b> also removes them from that "
                "cloud on the next sync. Your main collection is never affected."
            )
        )

        actions = QHBoxLayout()
        for label, service in [
            ("Untrack from KelmaSync", consts.KELMA),
            ("Untrack from AnkiWeb", consts.ANKIWEB),
        ]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, s=service: self._untrack(s))
            actions.addWidget(b)
        actions.addStretch()
        for label, service in [
            ("Delete from KelmaSync", consts.KELMA),
            ("Delete from AnkiWeb", consts.ANKIWEB),
        ]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, s=service: self._delete(s))
            actions.addWidget(b)
        layout.addLayout(actions)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

        self._load()

    # -- data -----------------------------------------------------------------
    def _load(self) -> None:
        def task() -> dict:
            return storage.deck_breakdown(mw.col)

        def done(fut: "Future[dict]") -> None:
            result = fut.result()
            self.rows = result["rows"]
            self.total_media = result["total_media"]
            self._populate()

        mw.taskman.with_progress(task, done, parent=self, label="Analyzing storage…")

    def _populate(self) -> None:
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            name = row["name"]
            services = config.services_for_deck(name)

            def cell(text, align_right=False):
                it = QTableWidgetItem(text)
                if align_right:
                    it.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                return it

            self.table.setItem(r, 0, cell(name))
            self.table.setItem(r, 1, cell(f"{row['cards']:,}", True))
            self.table.setItem(r, 2, cell(f"{row['notes']:,}", True))
            self.table.setItem(r, 3, cell(_human(row["media_bytes"]), True))
            for col, service in ((4, consts.KELMA), (5, consts.ANKIWEB)):
                routed = service in services
                it = QTableWidgetItem("✓" if routed else "–")
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if routed:
                    it.setForeground(QColor(_COLOR[service]))
                self.table.setItem(r, col, it)
        self.info.setText(
            f"{len(self.rows)} decks · total media {_human(self.total_media)}  "
            "(per-deck media may double-count files shared between decks)"
        )
        self._apply_filter()

    def _apply_filter(self, *_args) -> None:
        mode = self.filter_combo.currentData()
        for r, row in enumerate(self.rows):
            services = config.services_for_deck(row["name"])
            k = consts.KELMA in services
            w = consts.ANKIWEB in services
            show = (
                mode == "all"
                or (mode == "kelma" and k)
                or (mode == "ankiweb" and w)
                or (mode == "both" and (k or w))
            )
            self.table.setRowHidden(r, not show)

    def _selected_names(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.table.item(r, 0).text() for r in rows]

    # -- actions --------------------------------------------------------------
    def _set_routing(self, service: str, names: list[str]) -> None:
        cfg = config.get()
        routing = cfg["deck_routing"]
        for name in names:
            current = list(config.services_for_deck(name))
            if service in current:
                current.remove(service)
            routing[name] = current
        config.save(cfg)

    def _untrack(self, service: str) -> None:
        names = self._selected_names()
        if not names:
            tooltip("Select one or more decks first.", parent=self)
            return
        self._set_routing(service, names)
        tooltip(
            f"Untracked {len(names)} deck(s) from {consts.SERVICE_LABEL[service]}.",
            parent=self,
        )
        self._populate()

    def _delete(self, service: str) -> None:
        names = self._selected_names()
        if not names:
            tooltip("Select one or more decks first.", parent=self)
            return
        label = consts.SERVICE_LABEL[service]
        if not askUser(
            f"Delete {len(names)} deck(s) from {label}?\n\n"
            f"This removes them from {label} (and its local shadow) on the next "
            "sync, and untracks them. Your main collection is NOT affected.",
            parent=self,
            title="Kelma",
        ):
            return

        def task() -> int:
            return storage.delete_from_cloud(service, names)

        def done(fut: "Future[int]") -> None:
            removed = fut.result()
            self._set_routing(service, names)
            deckbadges.invalidate_sizes()  # main-page GB recomputes (now smaller)
            tooltip(
                f"Removed {removed:,} cards (and freed their media) from {label}'s "
                f"copy — uploads on the next {label} sync.",
                parent=self,
            )
            self._load()  # re-analyze so the breakdown reflects the new sizes

        mw.taskman.with_progress(
            task, done, parent=self, label=f"Removing from {label}…"
        )
