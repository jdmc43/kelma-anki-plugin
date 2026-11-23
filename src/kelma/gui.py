"""Kelma menu, profile account dialogs, and the Sync button integration.

Each Anki profile syncs with exactly one provider. A KelmaSync profile runs a
two-way local ↔ KelmaSync sync of its own collection; AnkiWeb profiles keep
Anki's native sync. Nothing here copies, compares, or publishes between the two.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from aqt import gui_hooks, mw
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QCursor,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QKeySequence,
    QLineEdit,
    QMenu,
    QPushButton,
    pyqtSignal,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTimer,
    Qt,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from aqt.utils import tooltip, askUser

from . import accounts, branding, config, consts, deckbadges, features, legacy_copies, paths, state, updater

_orig_sync = None
_V2_ACTIVE_ACTION: str | None = None


def _v2_active_message() -> str | None:
    if _V2_ACTIVE_ACTION == "sync":
        return "A KelmaSync sync is already running. Please wait for its completion toast, then compare."
    if _V2_ACTIVE_ACTION == "compare":
        return "A KelmaSync compare is already running. Please wait for it to finish."
    if _V2_ACTIVE_ACTION == "reset":
        return "Local data is being reset for a fresh KelmaSync restore."
    if _V2_ACTIVE_ACTION == "media-reset":
        return "Local KelmaSync media is being deleted and redownloaded."
    if _V2_ACTIVE_ACTION == "merge":
        return "Account collections are being merged. Wait for completion."
    return None


def _fmt_gb(nbytes: int) -> str:
    return f"{nbytes / (1024 ** 3):.2f} GB"


def _esc(text: str) -> str:
    """HTML-escape note field content for safe display in QTextBrowser."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def _strip_html(text: str) -> str:
    """Strip HTML tags from an Anki note field, leaving plain text.

    Anki fields are HTML (e.g. ``<div dir="rtl">مرحبا</div>``). Escaping that
    buries the text under literal ``&lt;div&gt;`` — so we strip tags first and
    diff/display the plain text. Whitespace is collapsed; ``<br>``/``<p>``
    become newlines before stripping so line breaks survive.
    """
    import re
    # Convert block-level tags to newlines before stripping.
    text = re.sub(r"<(?:br|/p|/div)\s*>", "\n", text, flags=re.IGNORECASE)
    # Remove all remaining tags.
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities.
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse runs of spaces (but preserve newlines).
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(lines).strip()


def _render_field_diff(local: str, server: str) -> tuple[str, str]:
    """Render two field values as HTML with inline character-level diff.

    HTML is stripped to plain text first (Anki fields are HTML), so the diff is
    on actual content, not markup. Uses ``difflib.SequenceMatcher`` to highlight
    exact spans that differ. Returns ``(local_html, server_html)``.
    """
    local = _strip_html(local)
    server = _strip_html(server)
    if local == server:
        return _esc(local), _esc(server)

    sm = difflib.SequenceMatcher(None, local, server)
    local_parts: list[str] = []
    server_parts: list[str] = []
    for tag, l1, l2, s1, s2 in sm.get_opcodes():
        l_chunk = local[l1:l2]
        s_chunk = server[s1:s2]
        if tag == "equal":
            local_parts.append(_esc(l_chunk))
            server_parts.append(_esc(s_chunk))
        else:
            # Differing spans: highlight with a bright inline mark so the
            # exact change is visible even within a long field.
            if l_chunk:
                local_parts.append(
                    f"<mark style='background:#8b3a3a; color:#fff; "
                    f"border-radius:2px;'>{_esc(l_chunk)}</mark>"
                )
            if s_chunk:
                server_parts.append(
                    f"<mark style='background:#3a6b8b; color:#fff; "
                    f"border-radius:2px;'>{_esc(s_chunk)}</mark>"
                )
    local_html = "".join(local_parts) or "<i style='color:#555'>(empty)</i>"
    server_html = "".join(server_parts) or "<i style='color:#555'>(empty)</i>"
    return local_html, server_html


# -----------------------------------------------------------------------------
# Profile-backed account separation
# -----------------------------------------------------------------------------
_ACCOUNT_LABELS = {
    accounts.ANKIWEB: "AnkiWeb",
    accounts.KELMA: "KelmaSync",
}


def _current_account_provider() -> str | None:
    if config.kelmasync_only():
        return accounts.KELMA
    return accounts.provider_for(mw.pm.base, mw.pm.name)


def _current_account_label() -> str:
    provider = _current_account_provider()
    return _ACCOUNT_LABELS.get(provider, "Not configured")


class AccountDialog(QDialog):
    """Assign one cloud provider to each Anki profile/collection."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent or mw)
        self.setWindowTitle("Cloud accounts")
        self.resize(620, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(_brand_header("Separate cloud accounts"))
        help_text = QLabel(
            "Each Anki profile is one account with its own collection. Assign it "
            "to either AnkiWeb or KelmaSync. Normal sync only contacts that "
            "account; decks move between accounts only through explicit Transfer operations."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        profiles = list(mw.pm.profiles())
        records = accounts.load_registry(mw.pm.base)
        self.table = QTableWidget(len(profiles), 3)
        self.table.setHorizontalHeaderLabels(["Account / Anki profile", "Cloud", "Collection"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._profiles = profiles
        self._selectors: dict[str, QComboBox] = {}
        for row, profile in enumerate(profiles):
            label = profile + (" (current)" if profile == mw.pm.name else "")
            item = QTableWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, profile)
            self.table.setItem(row, 0, item)
            selector = QComboBox()
            selector.addItem("Not configured", None)
            selector.addItem("AnkiWeb", accounts.ANKIWEB)
            selector.addItem("KelmaSync", accounts.KELMA)
            selected = selector.findData(records.get(profile))
            selector.setCurrentIndex(max(0, selected))
            self.table.setCellWidget(row, 1, selector)
            self._selectors[profile] = selector
            path = accounts.collection_path(mw.pm.base, profile)
            self.table.setItem(row, 2, QTableWidgetItem("Available" if path.exists() else "Not created"))
        if mw.pm.name in profiles:
            self.table.selectRow(profiles.index(mw.pm.name))
        layout.addWidget(self.table)

        actions = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        switch = QPushButton("Switch to selected")
        switch.clicked.connect(self._switch)
        merge = QPushButton("Transfer or merge decks…")
        merge.clicked.connect(self._merge)
        profiles_button = QPushButton("Add or remove profiles…")
        profiles_button.clicked.connect(self._profiles_manager)
        actions.addWidget(save)
        actions.addWidget(switch)
        actions.addWidget(merge)
        actions.addWidget(profiles_button)
        layout.addLayout(actions)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def _selected_profile(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _save(self) -> None:
        records = {
            profile: selector.currentData()
            for profile, selector in self._selectors.items()
            if selector.currentData() in accounts.PROVIDERS
        }
        accounts.save_registry(mw.pm.base, records)
        tooltip("Cloud account assignments saved.", parent=self)

    def _switch(self) -> None:
        if _V2_ACTIVE_ACTION:
            tooltip(_v2_active_message() or "Wait for synchronization to finish.", parent=self)
            return
        profile = self._selected_profile()
        if not profile:
            return
        self._save()
        if profile == mw.pm.name:
            tooltip("That account is already open.", parent=self)
            return
        if not accounts.provider_for(mw.pm.base, profile):
            tooltip("Assign the selected account to AnkiWeb or KelmaSync first.", parent=self)
            return
        self.accept()

        def open_profile() -> None:
            mw.pm.openProfile(profile)
            mw.loadProfile()

        mw.unloadProfile(open_profile)

    def _merge(self) -> None:
        if _V2_ACTIVE_ACTION:
            tooltip(_v2_active_message() or "Wait for synchronization to finish.", parent=self)
            return
        profile = self._selected_profile()
        if not profile or profile == mw.pm.name:
            tooltip("Select a different configured account to merge.", parent=self)
            return
        self._save()
        self.accept()
        _merge_profile_into_current(profile)

    def _profiles_manager(self) -> None:
        if _V2_ACTIVE_ACTION:
            tooltip(_v2_active_message() or "Wait for synchronization to finish.", parent=self)
            return
        self._save()
        self.accept()
        mw.unloadProfileAndShowProfileManager()


def _merge_profile_into_current(source_profile: str | None = None) -> None:
    global _V2_ACTIVE_ACTION
    if _V2_ACTIVE_ACTION:
        tooltip(_v2_active_message() or "Wait for synchronization to finish.")
        return
    candidates = [
        account.profile
        for account in accounts.accounts(mw.pm.base, list(mw.pm.profiles()))
        if account.profile != mw.pm.name
        and account.provider in accounts.PROVIDERS
        and accounts.collection_path(mw.pm.base, account.profile).exists()
    ]
    if not candidates:
        tooltip("Create and configure another account before transferring decks.")
        return
    if source_profile is not None and source_profile not in candidates:
        tooltip("The selected account collection is unavailable.")
        return

    from .account_transfer import AccountTransferDialog

    _V2_ACTIVE_ACTION = "merge"
    try:
        AccountTransferDialog(mw, preselected_profile=source_profile).exec()
    finally:
        _V2_ACTIVE_ACTION = None


# -----------------------------------------------------------------------------
# Sync button integration
# -----------------------------------------------------------------------------
def _brand_header(text: str) -> QWidget:
    """A menu/dialog header row: the Kelma star logo (if enabled) + a title,
    tinted with the Kelma accent when the theme toggle is on."""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(14, 0, 14, 6)
    row.setSpacing(8)
    if branding.logo_enabled():
        icon = QLabel()
        icon.setPixmap(branding.star_pixmap(18))
        row.addWidget(icon)
    if branding.theme_enabled():
        title = QLabel(f'<b style="color:{branding.accent()};">{text}</b>')
    else:
        title = QLabel(f"<b>{text}</b>")
    row.addWidget(title)
    row.addStretch(1)
    return w


def _wrapped_sync() -> None:
    cfg = config.get()
    if config.kelmasync_only():
        _v2_sync_menu()
        return
    if not cfg["enabled"] or not cfg["wrap_sync_button"]:
        if _orig_sync:
            return _orig_sync()
        return
    _v2_sync_menu()


def _install_sync_hook() -> None:
    global _orig_sync
    if _orig_sync is not None:
        return
    _orig_sync = mw.on_sync_button_clicked
    mw.on_sync_button_clicked = _wrapped_sync
    mw.onSync = _wrapped_sync  # legacy alias used by some callers


_orig_collection_sync = None


def _install_native_sync_guard() -> None:
    """Allow Anki's native sync only for profiles assigned to AnkiWeb.

    `_sync_collection_and_media` is the single chokepoint for Anki's native sync —
    auto-sync on open/close, the Y shortcut, and the (unwrapped) Sync button all
    route through it. For KelmaSync and unassigned profiles we make it a no-op
    (still calling the continuation so open/close proceeds), so their
    collections never reach whichever AnkiWeb login happens to be present.
    """
    global _orig_collection_sync
    if _orig_collection_sync is not None:
        return
    _orig_collection_sync = mw._sync_collection_and_media

    def guarded(after_sync):
        cfg = config.get()
        provider = _current_account_provider()
        if config.kelmasync_only() or (
            cfg.get("enabled", True)
            and cfg.get("block_native_sync", True)
            and provider != accounts.ANKIWEB
        ):
            # Kelma and unconfigured accounts must never auto-sync their
            # collection into whichever AnkiWeb login happens to be present.
            after_sync()
        else:
            _orig_collection_sync(after_sync)

    mw._sync_collection_and_media = guarded


# -----------------------------------------------------------------------------
# KelmaSync v2
# -----------------------------------------------------------------------------
def _ensure_v2_vendor() -> None:
    """Expose the vendored .kelma_sync_v2 package as top-level kelma_sync_v2.

    We intentionally do NOT add the addon directory to sys.path, because add-on
    module names could shadow Python stdlib modules.
    """
    if "kelma_sync_v2" in sys.modules:
        return
    from . import kelma_sync_v2 as vendored
    sys.modules["kelma_sync_v2"] = vendored


class V2LoginDialog(QDialog):
    """Simple KelmaSync sign-in dialog with inline error feedback."""

    def __init__(self, parent, client, cfg: dict, endpoint: str) -> None:
        super().__init__(parent)
        self._client = client
        self.auth_out = None
        self.username_value = ""
        self.client_label = "KelmaDesktop" if config.kelmasync_only() else "Anki plugin"

        self.setWindowTitle("Sign in to Kelma Desktop")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(8)

        title = QLabel("Sign in with Kelma Immersion")
        font = title.font()
        font.setPointSize(15)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        account_text = (
            'Use the same email and password as your '
            '<a href="https://kelma.tech">Kelma Immersion account</a>.'
        )
        if not config.kelmasync_only():
            account_text += " This is separate from your AnkiWeb login."
        account_help = QLabel(account_text)
        account_help.setOpenExternalLinks(True)
        account_help.setWordWrap(True)
        layout.addWidget(account_help)
        layout.addSpacing(6)

        layout.addWidget(QLabel("Kelma Immersion email"))
        self.email = QLineEdit(str(cfg.get("v2_username", "")))
        self.email.setPlaceholderText("you@example.com")
        layout.addWidget(self.email)

        layout.addWidget(QLabel("Password"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password)

        self.show_password = QCheckBox("Show password")
        self.show_password.toggled.connect(
            lambda shown: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(self.show_password)

        self.error = QLabel("")
        self.error.setStyleSheet("color: #b91c1c;")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        layout.addSpacing(4)
        buttons = QDialogButtonBox()
        self.sign_in = buttons.addButton("Sign In to Kelma", QDialogButtonBox.ButtonRole.AcceptRole)
        self.sign_in.setDefault(True)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.sign_in.clicked.connect(self._sign_in)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

        (self.password if self.email.text().strip() else self.email).setFocus()

    def _show_error(self, message: str) -> None:
        clean = re.sub(r"^.*?:\s*", "", str(message)).strip()
        self.error.setText(
            clean
            or "Sign-in failed. Use the email and password for your Kelma Immersion account."
        )
        self.error.show()
        self.password.selectAll()
        self.password.setFocus()

    def _sign_in(self) -> None:
        username = self.email.text().strip()
        password = self.password.text()
        if not username or not password:
            self._show_error("Enter both your email and password.")
            return
        self.error.hide()
        self.sign_in.setEnabled(False)
        QApplication.processEvents()
        try:
            self.auth_out = self._client.login(username, password, self.client_label)
        except Exception as err:  # noqa: BLE001
            self.sign_in.setEnabled(True)
            self._show_error(str(err))
            return
        self.username_value = username
        self.accept()


def _v2_client_or_login():
    """Return a V2Client, showing one polished sign-in dialog if needed."""
    try:
        _ensure_v2_vendor()
        from kelma_sync_v2.client import V2Client
    except Exception as err:  # noqa: BLE001
        tooltip(f"KelmaSync client package is not installed: {err}")
        return None

    cfg = config.get()
    endpoint = cfg.get("v2_url") or consts.DEFAULT_V2_URL
    token = cfg.get("v2_token") or ""
    # Login delegates to Kelma Immersion and may include a live account-status
    # check, so do not use the old 12-second UI timeout.
    client = V2Client(endpoint, token=token, timeout=30)
    if token:
        return client

    dialog = V2LoginDialog(mw, client, cfg, endpoint)
    if dialog.exec() != QDialog.DialogCode.Accepted or dialog.auth_out is None:
        return None

    cfg["v2_url"] = endpoint
    cfg["v2_username"] = dialog.username_value
    cfg["v2_token"] = dialog.auth_out.token
    cfg["v2_client_id"] = dialog.auth_out.client_id
    cfg["v2_client_label"] = dialog.client_label
    config.save(cfg)
    tooltip("Signed in to KelmaSync.")
    return client


class V2FullDiffDialog(QDialog):
    """Compare this profile's collection with KelmaSync and resolve differences."""

    _progress_signal = pyqtSignal(str)

    def __init__(self, parent=None, *, reconcile_mode: bool = False) -> None:
        super().__init__(parent)
        self._reconcile_mode = reconcile_mode
        self.setWindowTitle(
            "Resolve local / KelmaSync differences" if reconcile_mode else "KelmaSync compare"
        )
        self.resize(1000, 640)
        self._client = None

        layout = QVBoxLayout(self)
        if reconcile_mode:
            intro_text = (
                "An ambiguous concurrent difference needs your choice. "
                "Keep the local collection or the KelmaSync version."
            )
            intro = QLabel(intro_text)
            intro.setWordWrap(True)
            layout.addWidget(intro)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Resource:"))
        self.resource_combo = QComboBox()
        self.resource_combo.addItems(["Notes", "Notetypes", "Decks", "Cards"])
        self.resource_combo.currentIndexChanged.connect(self._populate)
        top_row.addWidget(self.resource_combo)
        top_row.addStretch()

        self.btn_accept_all = QPushButton("Use KelmaSync for all")
        self.btn_accept_all.clicked.connect(self._accept_all)
        top_row.addWidget(self.btn_accept_all)
        local_source = "Local collection"
        self.btn_force_all = QPushButton(f"Use {local_source} for all")
        self.btn_force_all.clicked.connect(self._force_all)
        top_row.addWidget(self.btn_force_all)
        layout.addLayout(top_row)

        self.status_label = QLabel("Loading…")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Key", "Deck", "Status", local_source, "KelmaSync", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._show_row_detail)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.continue_button = close_button
        if reconcile_mode:
            close_button.setText("Finish review")
        buttons.rejected.connect(self.accept if reconcile_mode else self.reject)
        layout.addWidget(buttons)

        self._diff = None
        self._progress_signal.connect(self._on_progress)
        self._load()

    def _on_progress(self, text: str) -> None:
        self.status_label.setText(text)

    def _load(self) -> None:
        """Start comparison without blocking/re-entering Qt's UI thread."""
        global _V2_ACTIVE_ACTION
        blocked = _v2_active_message()
        if blocked:
            self._on_progress(f"⚠ {blocked}")
            self.btn_accept_all.setEnabled(False)
            self.btn_force_all.setEnabled(False)
            return
        try:
            self._on_progress("Connecting…")
            client = _v2_client_or_login()
        except Exception as err:  # noqa: BLE001
            self._on_progress(f"⚠ Login error: {err}")
            return
        if client is None:
            self._on_progress("Not signed in. Use Kelma → KelmaSync login/server… to sign in.")
            return
        # A KelmaSync profile syncs its whole collection.
        deck_names = None

        self._client = client
        _V2_ACTIVE_ACTION = "compare"
        self.btn_accept_all.setEnabled(False)
        self.btn_force_all.setEnabled(False)
        self.continue_button.setEnabled(False)
        self._on_progress("Scope: all decks")

        def progress(text: str) -> None:
            self._progress_signal.emit(text)

        def work():
            import time as _t
            from types import SimpleNamespace
            from kelma_sync_v2 import anki_local
            from kelma_sync_v2.content_sync import _scope_server_manifest_to_decks
            from kelma_sync_v2.full_diff import _diff_keyed

            progress("Contacting server…")
            t0 = _t.time()
            server = client.manifest()
            server = _scope_server_manifest_to_decks(
                client, server, deck_names, progress=progress
            )
            progress(
                f"Server: {len(server.get('notes', []))} notes, "
                f"{len(server.get('cards', []))} cards, "
                f"{len(server.get('notetypes', []))} notetypes, "
                f"{len(server.get('decks', []))} decks ({_t.time()-t0:.1f}s)"
            )
            local = anki_local.local_manifest(
                mw.col, deck_names=deck_names, progress=progress
            )
            comparison_ntids = {
                int(nt["notetype_id"]) for nt in local.get("notetypes", [])
            }
            comparison_ntids.update(
                int(note["notetype_id"])
                for note in server.get("notes", [])
                if note.get("notetype_id") is not None
            )
            server["notetypes"] = [
                nt for nt in server.get("notetypes", [])
                if int(nt.get("notetype_id", 0)) in comparison_ntids
            ]
            progress("Comparing notes…")
            notes = _diff_keyed(
                local.get("notes", []), server.get("notes", []), "guid"
            )
            progress("Comparing cards…")
            cards = _diff_keyed(
                local.get("cards", []), server.get("cards", []), "logical_key"
            )
            progress("Comparing notetypes…")
            notetypes = _diff_keyed(
                local.get("notetypes", []),
                server.get("notetypes", []),
                "notetype_id",
            )
            progress("Comparing decks…")
            decks = _diff_keyed(
                local.get("decks", []), server.get("decks", []), "name"
            )
            first_changed = 0
            for idx, entries in enumerate((notes, notetypes, decks, cards)):
                if any(entry.status != "in-sync" for entry in entries):
                    first_changed = idx
                    break
            total_changed = sum(
                1
                for entries in (notes, notetypes, decks, cards)
                for entry in entries
                if entry.status != "in-sync"
            )
            return SimpleNamespace(
                notes=notes,
                cards=cards,
                notetypes=notetypes,
                decks=decks,
                server_time=server.get("server_time", ""),
                first_changed=first_changed,
                total_changed=total_changed,
            )

        def done(future: Future) -> None:
            global _V2_ACTIVE_ACTION
            if _V2_ACTIVE_ACTION == "compare":
                _V2_ACTIVE_ACTION = None
            try:
                self.btn_accept_all.setEnabled(True)
                self.btn_force_all.setEnabled(True)
                self.continue_button.setEnabled(True)
            except RuntimeError:
                # The user closed the dialog while its worker was finishing.
                return
            try:
                loaded = future.result()
            except Exception as err:  # noqa: BLE001
                self._on_progress(f"⚠ Compare failed: {err}")
                tooltip(f"KelmaSync compare failed: {err}")
                return
            self._diff = loaded
            self.resource_combo.setCurrentIndex(loaded.first_changed)
            if loaded.total_changed == 0:
                try:
                    _mark_service_synced_for_badges(consts.KELMA)
                    self._on_progress("Everything matches KelmaSync — badges reset.")
                except Exception:  # noqa: BLE001
                    pass
            else:
                self._on_progress("Building table…")
            try:
                self._populate()
            except Exception as err:  # noqa: BLE001
                self._on_progress(f"⚠ Failed to render: {err}")

        mw.taskman.run_in_background(work, done, uses_collection=True)

    def _resource_key(self) -> str:
        return {0: "notes", 1: "notetypes", 2: "decks", 3: "cards"}.get(
            self.resource_combo.currentIndex(), "notes"
        )

    def _current_entries(self) -> list:
        if self._diff is None:
            return []
        key = self._resource_key()
        return getattr(self._diff, key, [])

    def _changed_entries(self) -> list:
        return [e for e in self._current_entries() if e.status != "in-sync"]

    def _populate(self) -> None:
        if self._diff is None:
            return
        entries = self._current_entries()
        changed = self._changed_entries()
        total = len(entries)
        # Only render changed rows (text-only). Rendering thousands of per-row
        # button widgets freezes Qt; instead select rows and use the top
        # Accept/Force buttons.
        self._rows = changed
        self.status_label.setText(
            f"{total} total · {len(changed)} changed — select rows, then choose which source wins"
        )
        # Cap rendered rows so a huge diff (e.g. thousands of local-only cards)
        # doesn't freeze Qt. The batch actions still operate on all changed.
        cap = 2000
        shown = changed[:cap]
        deck_map = _build_deck_map(mw.col, self._resource_key())
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(shown))
            for i, entry in enumerate(shown):
                self.table.setItem(i, 0, QTableWidgetItem(entry.key[:60]))
                self.table.setItem(i, 1, QTableWidgetItem(deck_map.get(entry.key, "—")))
                self.table.setItem(i, 2, QTableWidgetItem(entry.status))
                self.table.setItem(i, 3, QTableWidgetItem(_diff_local_preview(entry)))
                self.table.setItem(i, 4, QTableWidgetItem(_diff_server_preview(entry)))
        finally:
            self.table.setUpdatesEnabled(True)
        if len(changed) > cap:
            self.status_label.setText(
                f"{total} total · {len(changed)} changed (showing first {cap}) — "
                f"use a source button above to resolve everything"
            )

    def _selected_entries(self) -> list:
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        return [self._rows[r] for r in sorted(rows) if 0 <= r < len(self._rows)]

    def _show_row_detail(self, row: int, _col: int = 0) -> None:
        if not hasattr(self, "_rows") or row < 0 or row >= len(self._rows):
            return
        entry = self._rows[row]
        try:
            html = _v2_entry_detail_html(mw.col, self._client, entry)
        except Exception as err:  # noqa: BLE001
            html = f"<h3>Failed to build diff</h3><pre>{_esc(str(err))}</pre>"
        dlg = QDialog(self)
        dlg.setWindowTitle(f"KelmaSync diff: {entry.key}")
        dlg.resize(920, 680)
        layout = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(html)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _resolve_entries(self, entries: list, mode: str) -> None:
        """Resolve a batch of entries in the background, reloading once at end."""
        if not entries:
            tooltip("No changed rows selected.")
            return
        client = self._client
        self.status_label.setText(f"Resolving {len(entries)} item(s)…")
        self.btn_accept_all.setEnabled(False)
        self.btn_force_all.setEnabled(False)
        self.continue_button.setEnabled(False)

        def work():
            from kelma_sync_v2 import anki_apply, anki_local
            done = 0
            # Batch by resource type to avoid thousands of individual HTTP requests.
            guids = []
            card_ids = []
            notetype_ids = []
            deck_names_list = []
            for entry in entries:
                if mode == "accept":
                    if entry.resource == "guid":
                        guids.append(entry.key)
                    elif entry.resource == "notetype_id":
                        notetype_ids.append(int(entry.key))
                    elif entry.resource == "name":
                        deck_names_list.append(entry.key)
                    elif entry.resource in {"card_id", "logical_key"}:
                        cid = int((entry.server or {}).get("card_id") or entry.key)
                        card_ids.append(cid)
                else:  # force (push local to server)
                    # Batch-push local records in one operation per type.
                    pass
            # Batch-pull server resources.
            if guids or card_ids or notetype_ids or deck_names_list:
                resp = client.batch_pull(
                    notes=guids, cards=card_ids,
                    notetypes=notetype_ids, decks=deck_names_list,
                )
                for record in resp.get("notes", []):
                    try: anki_apply.apply_note(mw.col, record); done += 1
                    except Exception: pass
                for record in resp.get("cards", []):
                    try: anki_apply.apply_card(mw.col, record); done += 1
                    except Exception: pass
                for record in resp.get("notetypes", []):
                    try: anki_apply.apply_notetype(mw.col, record); done += 1
                    except Exception: pass
                for record in resp.get("decks", []):
                    try: anki_apply.apply_deck(mw.col, record); done += 1
                    except Exception: pass
            # Batch-push local resources for force mode.
            if mode != "accept":
                payload = {"notes": [], "cards": [], "notetypes": [], "decks": []}
                for entry in entries:
                    if entry.resource == "guid":
                        rec = anki_local.note_record(mw.col, entry.key)
                        if rec:
                            payload["notes"].append({"guid": entry.key, "notetype_id": rec["notetype_id"], "fields": rec["fields"], "tags": rec["tags"], "client_modified_at": rec["client_modified_at"], "base_checksum": ""})
                    elif entry.resource == "notetype_id":
                        rec = anki_local.notetype_record(mw.col, int(entry.key))
                        if rec:
                            payload["notetypes"].append({"notetype_id": int(entry.key), "name": rec["name"], "definition": rec["definition"], "client_modified_at": rec["client_modified_at"], "base_checksum": ""})
                    elif entry.resource == "name":
                        rec = anki_local.deck_record(mw.col, entry.key)
                        if rec:
                            payload["decks"].append({"name": entry.key, "config": rec["config"], "client_modified_at": rec["client_modified_at"], "base_checksum": ""})
                    elif entry.resource in {"card_id", "logical_key"}:
                        cid = int((entry.local or {}).get("card_id") or entry.key)
                        rec = anki_local.card_record(mw.col, cid)
                        if rec:
                            payload["cards"].append({
                                "card_id": cid,
                                "note_guid": rec["note_guid"],
                                "deck_name": rec["deck_name"],
                                "ord": rec["ord"],
                                "scheduling": rec["scheduling"],
                                "study_state": rec["study_state"],
                                "study_state_client_modified_at": rec[
                                    "study_state_client_modified_at"
                                ],
                                "client_modified_at": rec["client_modified_at"],
                            })
                # Push in 3000-item batches per resource type.
                for kind in ("notetypes", "decks", "notes", "cards"):
                    records = payload[kind]
                    for start in range(0, len(records), 3000):
                        chunk_payload = {"notes": [], "cards": [], "notetypes": [], "decks": []}
                        chunk_payload[kind] = records[start:start + 3000]
                        client.batch_push(chunk_payload, force=True)
                done += len(entries)
            elif mode == "accept":
                pass  # done already counted above
            return done

        def done_cb(future: Future) -> None:
            self.btn_accept_all.setEnabled(True)
            self.btn_force_all.setEnabled(True)
            self.continue_button.setEnabled(True)
            try:
                n = future.result()
            except Exception as err:  # noqa: BLE001
                self.status_label.setText(f"Resolve failed: {err}")
                return
            tooltip(f"Resolved {n} item(s).")
            # The resolved items now match KelmaSync (either side won — both
            # converged). Reset the badge baseline so pulled/pushed data does
            # not appear as +N pending local changes.
            try:
                _mark_service_synced_for_badges(consts.KELMA)
            except Exception:  # noqa: BLE001
                pass
            self._load()  # reload once

        mw.taskman.run_in_background(work, done_cb, uses_collection=True)

    def _accept_all(self) -> None:
        sel = self._selected_entries()
        self._resolve_entries(sel if sel else self._changed_entries(), "accept")

    def _force_all(self) -> None:
        sel = self._selected_entries()
        self._resolve_entries(sel if sel else self._changed_entries(), "force")


def _diff_local_preview(entry) -> str:
    l = entry.local
    if l is None:
        return "(missing)"
    return _entry_preview(l)


def _build_deck_map(col, resource_key: str) -> dict[str, str]:
    """Build a {diff key: deck_name} map for notes/cards/decks."""
    out: dict[str, str] = {}
    try:
        if resource_key in {"notes", "guid"}:
            rows = col.db.all(
                "SELECT DISTINCT n.guid, d.name "
                "FROM notes n JOIN cards c ON c.nid = n.id "
                "JOIN decks d ON d.id = c.did WHERE n.guid != ''"
            )
            for guid, name in rows:
                out[str(guid)] = str(name)
        elif resource_key in {"cards", "card_id", "logical_key"}:
            rows = col.db.all(
                "SELECT n.guid, c.ord, d.name FROM cards c "
                "JOIN notes n ON n.id = c.nid JOIN decks d ON d.id = c.did "
                "WHERE n.guid != ''"
            )
            for guid, ord_, name in rows:
                out[f"{guid}:{int(ord_ or 0)}"] = str(name)
        elif resource_key in {"decks", "name"}:
            for d in col.decks.all_names_and_ids():
                out[str(d.name)] = str(d.name)
    except Exception:  # noqa: BLE001
        pass
    return out


def _diff_server_preview(entry) -> str:
    s = entry.server
    if s is None:
        return "(missing)"
    return _entry_preview(s)


def _entry_preview(record: dict) -> str:
    """Short preview of a manifest entry's content."""
    if "guid" in record or "checksum" in record and "modified_at" in record and len(record) <= 4:
        # Manifest entry: show timestamp + checksum suffix
        ts = record.get("modified_at", "")
        cs = record.get("checksum", "")[:8]
        return f"{ts} · {cs}" if cs else str(ts)
    return str(record)[:120]


def _json_pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, default=str)


def _html_pre_diff(title: str, local_text: str, server_text: str) -> str:
    l_html, s_html = _render_field_diff(local_text, server_text)
    return (
        f"<h3>{_esc(title)}</h3>"
        "<table width='100%' cellspacing='0' cellpadding='8'>"
        "<tr><th align='left'>Local</th><th align='left'>Server</th></tr>"
        f"<tr><td valign='top' width='50%' style='background:#171717'>{l_html}</td>"
        f"<td valign='top' width='50%' style='background:#171717'>{s_html}</td></tr>"
        "</table>"
    )


def _v2_entry_detail_html(col, client, entry) -> str:
    from kelma_sync_v2 import anki_local

    kind = entry.resource
    title = f"{entry.status}: {entry.key}"
    local_name = "Local collection"
    publish_text = "The chosen result is kept between this local collection and KelmaSync."
    parts = [
        f"<h2>{_esc(title)}</h2>",
        f"<p><b>Choose the canonical source:</b> KelmaSync overwrites {local_name}; "
        f"{local_name} overwrites KelmaSync. {publish_text}</p>",
    ]

    if kind == "guid":
        local = anki_local.note_record(col, entry.key) if entry.local else None
        server = client.get_note(entry.key) if entry.server else None
        lf = (local or {}).get("fields") or []
        sf = (server or {}).get("fields") or []
        n = max(len(lf), len(sf))
        for i in range(n):
            parts.append(_html_pre_diff(f"Field {i + 1}", str(lf[i] if i < len(lf) else ""), str(sf[i] if i < len(sf) else "")))
        parts.append(_html_pre_diff("Tags", " ".join((local or {}).get("tags") or []), " ".join((server or {}).get("tags") or [])))
        parts.append(_html_pre_diff("Notetype", str((local or {}).get("notetype_id", "")), str((server or {}).get("notetype_id", ""))))
        return "".join(parts)

    if kind in {"card_id", "logical_key"}:
        local_cid = int((entry.local or {}).get("card_id") or entry.key)
        server_cid = int((entry.server or {}).get("card_id") or entry.key)
        local = anki_local.card_record(col, local_cid) if entry.local else None
        server = client.get_card(server_cid) if entry.server else None
        for field in ("note_guid", "deck_name", "ord"):
            parts.append(_html_pre_diff(field, str((local or {}).get(field, "")), str((server or {}).get(field, ""))))
        parts.append(_html_pre_diff("Scheduling", _json_pretty((local or {}).get("scheduling") or {}), _json_pretty((server or {}).get("scheduling") or {})))
        return "".join(parts)

    if kind == "notetype_id":
        ntid = int(entry.key)
        local = anki_local.notetype_record(col, ntid) if entry.local else None
        server = client.get_notetype(ntid) if entry.server else None
        parts.append(_html_pre_diff("Name", str((local or {}).get("name", "")), str((server or {}).get("name", ""))))
        parts.append(_html_pre_diff("Definition", _json_pretty((local or {}).get("definition") or {}), _json_pretty((server or {}).get("definition") or {})))
        return "".join(parts)

    if kind == "name":
        local = anki_local.deck_record(col, entry.key) if entry.local else None
        server = client.get_deck(entry.key) if entry.server else None
        parts.append(_html_pre_diff("Deck config", _json_pretty((local or {}).get("config") or {}), _json_pretty((server or {}).get("config") or {})))
        return "".join(parts)

    parts.append(_html_pre_diff("Raw manifest", _json_pretty(entry.local or {}), _json_pretty(entry.server or {})))
    return "".join(parts)


class V2SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("KelmaSync settings")
        cfg = config.get()
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        layout.addLayout(grid)

        self.endpoint = QLineEdit(cfg.get("v2_url", consts.DEFAULT_V2_URL))
        self.username = QLineEdit(cfg.get("v2_username", ""))
        self.label = QLineEdit(cfg.get("v2_client_label", "Anki plugin"))
        self.last = QLabel(cfg.get("v2_last_server_time", "") or "(none)")
        self.token = QLabel("saved" if cfg.get("v2_token") else "not logged in")
        self.scheduling_mode = QComboBox()
        self.scheduling_mode.addItem("Server FSRS schedule", "server_fsrs")
        self.scheduling_mode.addItem("Local Anki schedule", "local_anki")
        mode = cfg.get("v2_scheduling_mode", "server_fsrs")
        self.scheduling_mode.setCurrentIndex(1 if mode == "local_anki" else 0)

        for row, (name, widget) in enumerate([
            ("Endpoint", self.endpoint),
            ("Kelma Immersion email", self.username),
            ("Client label", self.label),
            ("Token", self.token),
            ("Last server time", self.last),
            ("Scheduling source", self.scheduling_mode),
        ]):
            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(widget, row, 1)

        deletion_box = QGroupBox("Server deletion protection")
        deletion_layout = QVBoxLayout(deletion_box)
        deletion_help = QLabel(
            "More than 100 server deletions are blocked unless you explicitly approve one sync."
        )
        deletion_help.setWordWrap(True)
        deletion_layout.addWidget(deletion_help)
        self.allow_large_deletes = QCheckBox(
            "Allow more than 100 KelmaSync deletions on the next successful sync"
        )
        self.allow_large_deletes.setChecked(bool(cfg.get("v2_allow_large_deletes", False)))
        deletion_layout.addWidget(self.allow_large_deletes)
        layout.addWidget(deletion_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        cfg = config.get()
        cfg["v2_url"] = self.endpoint.text().strip() or consts.DEFAULT_V2_URL
        cfg["v2_username"] = self.username.text().strip()
        cfg["v2_client_label"] = self.label.text().strip() or "Anki plugin"
        cfg["v2_allow_large_deletes"] = self.allow_large_deletes.isChecked()
        cfg["v2_scheduling_mode"] = self.scheduling_mode.currentData() or "server_fsrs"
        config.save(cfg)
        tooltip("KelmaSync v2 settings saved.")
        self.accept()


def _mark_service_synced_for_badges(service: str) -> None:
    """Reset the pending-change badge after a confirmed pull/publish."""
    st = state.load()
    path = config.get().get("v2_url", "") if service == consts.KELMA else "AnkiWeb"
    state.mark_synced(st, service, path, mw.col)
    state.save(st)
    deckbadges.invalidate_sizes()
    # Always refresh — the deck browser may not be the current state during a
    # sync (a progress dialog is showing), but when the user returns to it,
    # the cached render would still show stale +N badges.
    try:
        mw.deckBrowser.refresh()
    except Exception:  # noqa: BLE001 - badge refresh is best-effort
        pass


def _v2_forget_login() -> None:
    cfg = config.get()
    cfg["v2_token"] = ""
    cfg["v2_client_id"] = ""
    cfg["v2_last_server_time"] = ""
    config.save(cfg)
    tooltip("KelmaSync v2 login/checkpoint cleared.")


class V2SyncProgressDialog(QDialog):
    _line = pyqtSignal(str)
    _done = pyqtSignal(str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("KelmaSync progress")
        self.resize(720, 420)
        self._started = datetime.now()
        layout = QVBoxLayout(self)
        self.status = QLabel("Starting sync…")
        layout.addWidget(self.status)
        self.log = QTextBrowser()
        layout.addWidget(self.log)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_btn.setEnabled(False)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._line.connect(self._append_line)
        self._done.connect(self._finish)

    def progress(self, text: str) -> None:
        self._line.emit(text)

    def complete(self, text: str, ok: bool = True) -> None:
        self._done.emit(text, ok)

    def _append_line(self, text: str) -> None:
        elapsed = (datetime.now() - self._started).total_seconds()
        line = f"{elapsed:6.1f}s  {text}"
        self.status.setText(text)
        self.log.append(line)
        QApplication.processEvents()

    def _finish(self, text: str, ok: bool) -> None:
        prefix = "✅" if ok else "⚠"
        self._append_line(f"{prefix} {text}")
        self.close_btn.setEnabled(True)
        # Refresh the deck browser so badges reflect the just-completed sync,
        # even if the progress dialog is still visible when the user closes it.
        try:
            mw.deckBrowser.refresh()
        except Exception:  # noqa: BLE001
            pass


def _kelma_desktop_sync_menu() -> None:
    """Sync menu for KelmaDesktop, where every profile is a KelmaSync account."""
    cfg = config.get()
    menu = QMenu(mw)
    menu.setStyleSheet("QMenu::item { padding-left: 18px; padding-right: 18px; padding-top: 5px; padding-bottom: 5px; } QMenu::item:selected { background-color: rgba(255,255,255,0.12); }")
    if branding.logo_enabled():
        menu.setIcon(branding.star_icon())

    container = QWidget()
    box = QVBoxLayout(container)
    box.setContentsMargins(0, 8, 0, 4)
    box.setSpacing(4)
    box.addWidget(_brand_header("KelmaSync"))
    container.setMinimumWidth(360)
    status = "logged in" if cfg.get("v2_token") else "not logged in"
    endpoint = cfg.get("v2_url") or consts.DEFAULT_V2_URL
    user = cfg.get("v2_username") or "(no username saved)"
    active = _v2_active_message()
    active_html = f"<br><span style='color:#d98'>⚠ {active}</span>" if active else ""
    status_label = QLabel(
        f"<b>{status}</b> · {user}<br>"
        f"<span style='color:#888'>{endpoint}</span>{active_html}"
    )
    status_wrap = QWidget()
    status_layout = QHBoxLayout(status_wrap)
    status_layout.setContentsMargins(18, 0, 18, 0)
    status_layout.addWidget(status_label)
    box.addWidget(status_wrap)
    wa = QWidgetAction(menu)
    wa.setDefaultWidget(container)
    menu.addAction(wa)
    menu.addSeparator()

    menu.setMinimumWidth(380)
    act_kelma = menu.addAction("Sync KelmaSync")
    act_compare = menu.addAction("Compare everything…")
    if _V2_ACTIVE_ACTION:
        act_kelma.setEnabled(False)
        act_compare.setEnabled(False)
    menu.addSeparator()
    act_v2_settings = menu.addAction("KelmaSync account/server…")
    act_forget = menu.addAction("Forget login")

    chosen = menu.exec(QCursor.pos())
    if chosen is None:
        return
    if chosen is act_kelma:
        _v2_sync_kelma()
    elif chosen is act_compare:
        V2FullDiffDialog(mw).exec()
    elif chosen is act_v2_settings:
        V2SettingsDialog(mw).exec()
    elif chosen is act_forget:
        _v2_forget_login()


def _v2_compare_or_onboard() -> None:
    provider = _current_account_provider()
    if config.kelmasync_only() or provider == accounts.KELMA:
        V2FullDiffDialog(mw).exec()
    elif provider == accounts.ANKIWEB:
        tooltip("Use Anki's native sync status for this AnkiWeb account, or Merge to compare collections.")
    else:
        AccountDialog(mw).exec()


def _sync_current_account() -> None:
    provider = _current_account_provider()
    if provider is None:
        AccountDialog(mw).exec()
    elif provider == accounts.KELMA:
        _v2_sync_kelma()
    elif _orig_sync:
        _orig_sync()


def _v2_sync_menu() -> None:
    """Sync only the current profile-backed account by default."""
    if _V2_ACTIVE_ACTION:
        tooltip(_v2_active_message() or "A sync operation is already running.")
        return
    if config.kelmasync_only():
        _kelma_desktop_sync_menu()
        return

    provider = _current_account_provider()
    menu = QMenu(mw)
    header = QWidget()
    layout = QVBoxLayout(header)
    layout.setContentsMargins(14, 8, 14, 8)
    layout.addWidget(_brand_header("Cloud account"))
    detail = QLabel(
        f"<b>{_esc(mw.pm.name)}</b> · {_esc(_current_account_label())}<br>"
        "<span style='color:#aaa'>This collection syncs only with its assigned cloud.</span>"
    )
    layout.addWidget(detail)
    widget_action = QWidgetAction(menu)
    widget_action.setDefaultWidget(header)
    menu.addAction(widget_action)
    menu.addSeparator()
    sync = menu.addAction(
        f"Sync {_ACCOUNT_LABELS.get(provider, 'current account')}"
        if provider else "Configure this account before syncing…"
    )
    redownload = (
        menu.addAction("Delete and redownload media…")
        if provider == accounts.KELMA
        else None
    )
    manage = menu.addAction("Accounts and collections…")
    transfer = menu.addAction("Transfer decks between accounts…")
    chosen = menu.exec(QCursor.pos())
    if chosen is sync:
        _sync_current_account()
    elif redownload is not None and chosen is redownload:
        _v2_delete_and_redownload_media()
    elif chosen is manage:
        AccountDialog(mw).exec()
    elif chosen is transfer:
        _merge_profile_into_current()


def run_kelma_desktop_sync() -> None:
    """Public bridge used by KelmaDesktop's core Sync button/shortcut."""
    _v2_sync_kelma()


def _v2_delete_and_redownload_media() -> None:
    """Delete referenced server-backed media and immediately restore it."""
    global _V2_ACTIVE_ACTION
    if _v2_active_message():
        tooltip(_v2_active_message() or "KelmaSync is busy.")
        return
    if not config.kelmasync_only() and _current_account_provider() != accounts.KELMA:
        tooltip("Media redownload is available only for KelmaSync accounts.")
        return
    client = _v2_client_or_login()
    if client is None:
        return
    if not askUser(
        "Delete and redownload KelmaSync media?\n\n"
        "This removes local media already backed by this KelmaSync account, "
        "then downloads fresh copies. Local-only files are retained.\n\n"
        "If the transfer is interrupted, run this action or Sync again to resume.",
        parent=mw,
    ):
        return

    from .kelma_sync_v2.media_sync import (
        delete_downloaded_media_for_redownload,
        sync_media_once,
    )

    _V2_ACTIVE_ACTION = "media-reset"
    dialog = V2SyncProgressDialog(mw)
    dialog.setWindowTitle("Redownload KelmaSync media")
    dialog.show()
    dialog.progress("Media redownload: fetching the server manifest…")
    deck_names = None  # a KelmaSync profile syncs its whole collection

    def work() -> dict[str, int]:
        manifest = client.manifest()
        removed, removed_bytes = delete_downloaded_media_for_redownload(
            mw.col,
            manifest,
            progress=dialog.progress,
            deck_names=deck_names,
        )
        media = sync_media_once(
            mw.col,
            client,
            server_manifest=manifest,
            progress=dialog.progress,
            deck_names=deck_names,
        )
        return {
            "removed": removed,
            "removed_bytes": removed_bytes,
            "downloaded": media.downloaded,
        }

    def done(future: Future) -> None:
        global _V2_ACTIVE_ACTION
        _V2_ACTIVE_ACTION = None
        try:
            result = future.result()
        except Exception as err:  # noqa: BLE001
            dialog.complete(
                f"Media redownload interrupted: {err}. Run Sync to resume completed work.",
                ok=False,
            )
            return
        deckbadges.invalidate_sizes()
        mw.reset()
        dialog.complete(
            f"Media redownload complete: deleted {result['removed']:,} local file(s) "
            f"({_fmt_gb(result['removed_bytes'])}), downloaded {result['downloaded']:,}.",
            ok=True,
        )

    mw.taskman.run_in_background(work, done, uses_collection=True)


def _v2_delete_and_restore_from_server() -> None:
    """Delete local Desktop content/media, preserve auth, then restore v2."""
    global _V2_ACTIVE_ACTION
    if _v2_active_message():
        tooltip(_v2_active_message() or "KelmaSync is busy.")
        return
    if not askUser(
        "Delete local data and restore from KelmaSync?\n\n"
        "This deletes all notes, cards, deck data, and media stored in KelmaDesktop, "
        "then downloads the server copy. Local reviews or edits that have not reached "
        "KelmaSync will be lost.\n\nA collection backup is created first. Your Kelma "
        "Immersion login is preserved.",
        parent=mw,
    ):
        return
    if _v2_client_or_login() is None:
        return

    _V2_ACTIVE_ACTION = "reset"
    dialog = V2SyncProgressDialog(mw)
    dialog.show()
    dialog.progress("Creating a collection backup before local reset…")

    def reset_work() -> dict[str, int]:
        mw.create_backup_now()
        col = mw.col
        note_ids = [int(value) for value in col.db.list("SELECT id FROM notes")]
        for start in range(0, len(note_ids), 1000):
            col.remove_notes(note_ids[start:start + 1000])

        # remove_notes normally removes every card; clean up any orphan rows.
        card_ids = [int(value) for value in col.db.list("SELECT id FROM cards")]
        if card_ids:
            col.remove_cards(card_ids)
        review_count = int(col.db.scalar("SELECT count(*) FROM revlog") or 0)
        col.db.execute("DELETE FROM revlog")

        deck_ids = [
            int(value)
            for value in col.db.list("SELECT id FROM decks WHERE id != 1")
        ]
        if deck_ids:
            col.decks.remove(deck_ids)

        media_dir = Path(col.media.dir())
        media_removed = 0
        if media_dir.exists():
            for path in media_dir.iterdir():
                if path.is_file():
                    path.unlink()
                    media_removed += 1
        # Native graves describe the reset, not deletions to publish elsewhere.
        col.db.execute("DELETE FROM graves")
        return {
            "notes": len(note_ids),
            "cards": len(card_ids),
            "reviews": review_count,
            "decks": len(deck_ids),
            "media": media_removed,
        }

    def reset_done(future: Future) -> None:
        global _V2_ACTIVE_ACTION
        try:
            removed = future.result()
        except Exception as err:  # noqa: BLE001
            _V2_ACTIVE_ACTION = None
            dialog.complete(f"Could not reset local KelmaDesktop data: {err}", ok=False)
            return
        cfg = config.get()
        cfg["v2_last_server_time"] = ""
        cfg["v2_allow_large_deletes"] = False
        config.save(cfg)
        _V2_ACTIVE_ACTION = None
        dialog.complete(
            "Local reset complete: "
            f"{removed['notes']} notes, {removed['cards']} remaining cards, "
            f"{removed['reviews']} reviews, {removed['decks']} decks, and "
            f"{removed['media']} files removed. "
            "Starting a fresh server restore…",
            ok=True,
        )
        _v2_sync_kelma()

    mw.taskman.run_in_background(reset_work, reset_done, uses_collection=True)


def _v2_sync_kelma() -> None:
    """Two-way sync of the current profile's collection with KelmaSync."""
    global _V2_ACTIVE_ACTION
    if not config.kelmasync_only() and _current_account_provider() != accounts.KELMA:
        tooltip("This collection is not assigned to a KelmaSync account.")
        return
    blocked = _v2_active_message()
    if blocked:
        tooltip(f"KelmaSync: {blocked}")
        return
    client = _v2_client_or_login()
    if client is None:
        return
    try:
        from kelma_sync_v2.content_sync import DeletionSafetyError, ContentSyncConflict, sync_content_once
    except Exception as err:  # noqa: BLE001
        tooltip(f"KelmaSync v2 package import failed: {err}")
        return

    cfg = config.get()
    since = cfg.get("v2_last_server_time") or None
    _V2_ACTIVE_ACTION = "sync"
    dlg = V2SyncProgressDialog(mw)
    dlg.show()
    dlg.progress(
        "Local ↔ KelmaSync queued for all decks. "
        "Waiting for the Anki collection worker…"
    )
    tooltip("KelmaSync: syncing local collection… progress window opened.")

    def _work():
        dlg.progress("Worker started.")
        return sync_content_once(
            mw.col,
            client,
            since=since,
            deck_names=None,
            allow_large_deletes=bool(cfg.get("v2_allow_large_deletes", False)),
            newest_wins=True,
            accept_server_timestamp_truncation=True,
            scheduling_mode=cfg.get("v2_scheduling_mode", "server_fsrs"),
            progress=dlg.progress,
        )

    def _done(future: Future) -> None:
        global _V2_ACTIVE_ACTION
        try:
            result = future.result()
        except DeletionSafetyError as safety:
            if _V2_ACTIVE_ACTION == "sync":
                _V2_ACTIVE_ACTION = None
            dlg.complete(
                f"KelmaSync deletion protection stopped the sync: {safety}. "
                "If the deletions are intentional, open Kelma → KelmaSync login/server "
                "and approve large deletions for one sync.",
                ok=False,
            )
            tooltip("KelmaSync blocked a large server deletion. Review deletion protection.")
            return
        except ContentSyncConflict as conflict:
            msg = f"{len(conflict.conflicts)} {conflict.resource} conflict(s). Choose which source wins."
            if _V2_ACTIVE_ACTION == "sync":
                _V2_ACTIVE_ACTION = None
            dlg.progress(msg)
            tooltip(f"KelmaSync: {msg}")
            V2FullDiffDialog(mw, reconcile_mode=True).exec()
            dlg.complete("Conflict review finished. Run sync once more to verify convergence.", ok=True)
            return
        except Exception as err:  # noqa: BLE001
            if _V2_ACTIVE_ACTION == "sync":
                _V2_ACTIVE_ACTION = None
            # Even on failure, pulled data is in the collection. Update the
            # badge baseline so it doesn't show as +N pending.
            try:
                _mark_service_synced_for_badges(consts.KELMA)
            except Exception:  # noqa: BLE001
                pass
            dlg.complete(f"KelmaSync failed: {err}", ok=False)
            tooltip(f"KelmaSync v2 sync failed: {err}")
            return
        cfg2 = config.get()
        cfg2["v2_last_server_time"] = result.server_time
        cfg2["v2_allow_large_deletes"] = False
        config.save(cfg2)
        # Pulled scheduling/content is a synchronized update, not a local edit.
        _mark_service_synced_for_badges(consts.KELMA)
        msg = (
            f"tombstones {result.tombstones.applied}, "
            f"decks {result.decks.pushed}/{result.decks.pulled}, "
            f"notetypes {result.notetypes.pushed}/{result.notetypes.pulled}, "
            f"notes {result.notes.pushed}/{result.notes.pulled}, "
            f"cards {result.cards.pushed}/{result.cards.pulled}, "
            f"reviews {result.reviews.pushed}/{result.reviews.pulled}, "
            f"daily limits {result.reviews.study_days_pushed}/{result.reviews.study_days_applied}, "
            f"media {result.media.uploaded}/{result.media.downloaded}."
        )
        if _V2_ACTIVE_ACTION == "sync":
            _V2_ACTIVE_ACTION = None
        dlg.complete(msg, ok=True)
        tooltip(f"KelmaSync complete: {msg}")

    try:
        mw.taskman.run_in_background(_work, _done, uses_collection=True)
    except Exception as err:  # noqa: BLE001
        _V2_ACTIVE_ACTION = None
        dlg.complete(f"Could not start KelmaSync sync: {err}", ok=False)


# -----------------------------------------------------------------------------
# Menu
# -----------------------------------------------------------------------------
def _build_menu() -> None:
    """Build the Tools → Kelma menu."""
    menu = QMenu("&Kelma", mw)
    menu.setStyleSheet("QMenu::item { padding-left: 18px; padding-right: 18px; padding-top: 5px; padding-bottom: 5px; } QMenu::item:selected { background-color: rgba(255,255,255,0.12); }")
    if branding.logo_enabled():
        menu.setIcon(branding.star_icon())
    mw.form.menuTools.addMenu(menu)

    if config.kelmasync_only():
        act_sync = QAction("Sync KelmaSync", mw)
        # KelmaDesktop core owns StandardKey.Save so the shortcut works even if
        # add-on initialization is delayed and never becomes ambiguous.
        act_sync.setToolTip("Sync KelmaSync (Ctrl+S / Command+S)")
        act_sync.triggered.connect(run_kelma_desktop_sync)
        menu.addAction(act_sync)

        act_restore = QAction("Delete local data and restore from server…", mw)
        act_restore.setToolTip(
            "Back up, delete local KelmaDesktop content/media, and restore KelmaSync"
        )
        act_restore.triggered.connect(_v2_delete_and_restore_from_server)
        menu.addAction(act_restore)

        act_media_restore = QAction("Delete and redownload media…", mw)
        act_media_restore.triggered.connect(_v2_delete_and_redownload_media)
        menu.addAction(act_media_restore)
    else:
        act_sync = QAction("Sync current account", mw)
        act_sync.setShortcut(QKeySequence.StandardKey.Save)
        act_sync.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_sync.setToolTip("Sync only this profile's assigned account")
        act_sync.triggered.connect(_sync_current_account)
        menu.addAction(act_sync)

        act_media_restore = QAction("Delete and redownload KelmaSync media…", mw)
        act_media_restore.triggered.connect(_v2_delete_and_redownload_media)
        menu.addAction(act_media_restore)

        act_accounts = QAction("Accounts and collections…", mw)
        act_accounts.triggered.connect(lambda: AccountDialog(mw).exec())
        menu.addAction(act_accounts)

        act_merge = QAction("Transfer decks between accounts…", mw)
        act_merge.triggered.connect(_merge_profile_into_current)
        menu.addAction(act_merge)

    act_compare = QAction("Compare current account…", mw)
    act_compare.triggered.connect(_v2_compare_or_onboard)
    menu.addAction(act_compare)

    menu.addSeparator()

    act_v2_settings = QAction("KelmaSync login/server…", mw)
    act_v2_settings.triggered.connect(lambda: V2SettingsDialog(mw).exec())
    menu.addAction(act_v2_settings)

    act_logout = QAction("Forget KelmaSync login", mw)
    act_logout.triggered.connect(_v2_forget_login)
    menu.addAction(act_logout)

    menu.addSeparator()
    act_updates = QAction("Check for Kelma updates…", mw)
    act_updates.triggered.connect(lambda: updater.check_for_update(manual=True))
    menu.addAction(act_updates)
    _build_display_menu(menu)


def _build_display_menu(parent: QMenu) -> None:
    """A checkable submenu, auto-populated from the features registry, for
    toggling visual modifications (deck badges, and future ones)."""
    sub = QMenu("Display modifications", mw)
    parent.addMenu(sub)
    for feat in features.FEATURES:
        act = QAction(feat.label, mw)
        act.setCheckable(True)
        act.setChecked(features.enabled(feat.key))
        act.setToolTip(feat.description)
        act.toggled.connect(
            lambda checked, k=feat.key: _toggle_feature(k, checked)
        )
        sub.addAction(act)


def _toggle_feature(key: str, checked: bool) -> None:
    features.set_enabled(key, checked)
    # Re-render the main screen so the change shows immediately.
    try:
        mw.reset()
    except Exception:  # noqa: BLE001
        pass


_legacy_checked_profiles: set[str] = set()


def _install_legacy_copy_check() -> None:
    """Check this profile now and every profile opened later in the session."""
    gui_hooks.profile_did_open.append(_schedule_legacy_copy_check)
    _schedule_legacy_copy_check()


def _schedule_legacy_copy_check() -> None:
    QTimer.singleShot(2000, _offer_legacy_copy_cleanup)


def _offer_legacy_copy_cleanup() -> None:
    """Offer once to delete the hidden copies left by dual-sync releases."""
    if mw is None or mw.col is None:
        return
    profile_dir = paths.profile_dir()
    if profile_dir in _legacy_checked_profiles:
        return
    _legacy_checked_profiles.add(profile_dir)
    found = legacy_copies.find(profile_dir)
    if not found or state.load().get("legacy_copies_declined"):
        return

    def measured(future: Future) -> None:
        if mw.col is None or paths.profile_dir() != profile_dir:
            return  # the profile changed meanwhile; ask next time it opens
        try:
            size_mb = future.result() / (1024 * 1024)
        except Exception:  # noqa: BLE001
            size_mb = 0.0
        if not askUser(
            "Older versions of the Kelma add-on kept hidden copies of this "
            f"collection in the profile folder ({size_mb:,.0f} MB). They are no "
            "longer used.\n\nDelete them? Your collection and media are not affected.",
            parent=mw,
            title="Kelma",
        ):
            st = state.load()
            st["legacy_copies_declined"] = True
            state.save(st)
            tooltip("Kept the old copies (kelma_shadow_* in the profile folder).")
            return

        def deleted(done: Future) -> None:
            try:
                failed = done.result()
            except Exception as err:  # noqa: BLE001
                tooltip(f"Could not delete the old copies: {err}")
                return
            if failed:
                tooltip(f"Could not delete {len(failed)} old copy file(s) in the profile folder.")
            else:
                tooltip(f"Deleted the old copies ({size_mb:,.0f} MB freed).")

        mw.taskman.run_in_background(
            lambda: legacy_copies.delete(found), deleted, uses_collection=False
        )

    mw.taskman.run_in_background(
        lambda: legacy_copies.total_bytes(found), measured, uses_collection=False
    )


def setup() -> None:
    """Entry point, called once after the profile/collection is ready."""
    # Each step is independent so a failure in one doesn't break the others.
    # Most importantly, deck badges must survive even if the menu/hook setup
    # throws — otherwise a GUI regression hides the green sync indicators.
    steps = [
        ("account credential migration", config.migrate_account_config),
        ("menu", _build_menu),
        ("sync hook", _install_sync_hook),
        ("deck badges", deckbadges.setup),
        ("update check", updater.schedule_automatic_check),
    ]
    # Kelma and unconfigured profile accounts must never fall through to native
    # AnkiWeb auto-sync. The guard dynamically allows native sync only for a
    # profile explicitly assigned to AnkiWeb.
    steps.insert(2, ("native AnkiWeb guard", _install_native_sync_guard))
    steps.append(("old dual-sync copies", _install_legacy_copy_check))

    for label, fn in steps:
        try:
            fn()
        except Exception as err:  # noqa: BLE001
            import traceback
            print(f"Kelma setup error ({label}): {err}\n{traceback.format_exc()}")
