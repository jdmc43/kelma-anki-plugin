"""Sync credentials: building SyncAuth and logging in to each service."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Optional

from aqt import mw
from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    Qt,
    QVBoxLayout,
)
from aqt.utils import showWarning, tooltip
from anki.sync import SyncAuth

from . import config, consts


def build_auth(service: str) -> Optional[SyncAuth]:
    cfg = config.get()
    if service == consts.KELMA:
        if not cfg["kelmasync_hkey"]:
            return None
        return SyncAuth(
            hkey=cfg["kelmasync_hkey"],
            endpoint=cfg["kelmasync_url"] or None,
        )
    if not cfg["ankiweb_hkey"]:
        return None
    # AnkiWeb is always the real AnkiWeb (no custom endpoint).
    return SyncAuth(hkey=cfg["ankiweb_hkey"])


class _LoginDialog(QDialog):
    def __init__(self, parent, service: str) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(f"Log in to {consts.SERVICE_LABEL[service]}")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.user = QLineEdit()
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        # Only KelmaSync has a configurable URL; AnkiWeb is always real AnkiWeb.
        self.url = QLineEdit(config.get()["kelmasync_url"])
        if service == consts.KELMA:
            form.addRow("Server URL", self.url)
        form.addRow("Username/email", self.user)
        form.addRow("Password", self.pw)
        layout.addLayout(form)

        # Account creation is web-based — link out to the branded sign-up page
        # (Kelma Immersion for KelmaSync, ankiweb.net for AnkiWeb) rather than
        # registering from inside the client.
        site = "Kelma Immersion" if service == consts.KELMA else "AnkiWeb"
        signup = QLabel(
            f'No account? <a href="{consts.SIGNUP_URL[service]}">'
            f"Create one on {site}</a>."
        )
        signup.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        signup.setOpenExternalLinks(True)  # opens in the default browser
        layout.addWidget(signup)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def login(service: str, on_success=None) -> None:
    dlg = _LoginDialog(mw, service)
    if not dlg.exec():
        return
    username = dlg.user.text().strip()
    password = dlg.pw.text()
    endpoint = (dlg.url.text().strip() or None) if service == consts.KELMA else None
    if not username or not password:
        showWarning("Username and password are required.")
        return

    def task() -> SyncAuth:
        return mw.col.sync_login(username, password, endpoint)

    def on_done(fut: "Future[SyncAuth]") -> None:
        try:
            auth = fut.result()
        except Exception as err:  # noqa: BLE001 - surface any login failure
            showWarning(f"Login failed: {err}")
            return
        cfg = config.get()
        if service == consts.KELMA:
            cfg["kelmasync_hkey"] = auth.hkey
            cfg["kelmasync_user"] = username
            if endpoint:
                cfg["kelmasync_url"] = endpoint
        else:
            cfg["ankiweb_hkey"] = auth.hkey
            cfg["ankiweb_user"] = username
        config.save(cfg)
        tooltip(f"Logged in to {consts.SERVICE_LABEL[service]}.", parent=mw)
        if on_success:
            on_success()

    mw.taskman.with_progress(
        task, on_done, label=f"Logging in to {consts.SERVICE_LABEL[service]}…"
    )
