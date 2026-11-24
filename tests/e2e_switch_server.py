#!/usr/bin/env python3
"""End-to-end test: one profile switching between two KelmaSync servers.

Runs two instances of the open-source KelmaSync server (github.com/jdmc43/kelma-sync),
each with its own database, creates an account on each the way a server's host
would, and checks with Anki's Python runtime that a collection:

- uploads completely to a newly chosen server,
- never deletes anything on either server because of the switch,
- comes back together when switched back (union of both servers), and
- ignores a stale snapshot from the other server even without the switch helper.

    ANKI_PY="$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python"
    KELMA_SYNC_SERVER_REPO=../kelma-sync \\
    DATABASE_URL_A=postgres://kelma:kelma@localhost:5433/kelma_a?sslmode=disable \\
    DATABASE_URL_B=postgres://kelma:kelma@localhost:5433/kelma_b?sslmode=disable \\
    PYTHONPATH=src "$ANKI_PY" tests/e2e_switch_server.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = Path(os.environ.get("KELMA_SYNC_SERVER_REPO", ROOT.parent / "kelma-sync")).resolve()
PORTS = (int(os.environ.get("E2E_PORT_A", "18091")), int(os.environ.get("E2E_PORT_B", "18092")))
DATABASES = (
    os.environ.get("DATABASE_URL_A", "postgres://kelma:kelma@localhost:5433/kelma_a?sslmode=disable"),
    os.environ.get("DATABASE_URL_B", "postgres://kelma:kelma@localhost:5433/kelma_b?sslmode=disable"),
)

sys.path.insert(0, str(ROOT / "src"))

from anki.collection import Collection  # noqa: E402
from kelma import sync_server  # noqa: E402
from kelma.kelma_sync_v2.client import V2Client  # noqa: E402
from kelma.kelma_sync_v2.content_sync import sync_content_once  # noqa: E402


def wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"server did not open port {port}")


def start_servers(tmp: Path) -> list[subprocess.Popen]:
    binary = os.environ.get("KELMA_SYNC_BIN")
    if not binary:
        binary = str(tmp / "kelma-sync")
        subprocess.run(["go", "build", "-o", binary, "./cmd/server"], cwd=SERVER_ROOT, check=True, timeout=180)
    procs = []
    for index, (port, database) in enumerate(zip(PORTS, DATABASES)):
        env = os.environ.copy()
        env.update({
            "PORT": str(port),
            "DATABASE_URL": database,
            "MIGRATIONS_DIR": str(SERVER_ROOT / "migrations"),
            "MEDIA_DIR": str(tmp / f"media-{index}"),
            "KELMA_AUTH_MODE": "local",
        })
        procs.append(subprocess.Popen([binary], cwd=SERVER_ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for port in PORTS:
        wait_port(port)
    return procs


def account(url: str) -> V2Client:
    """An account created by the server's host (here: its register endpoint)."""
    client = V2Client(url)
    username = f"switch-{int(time.time() * 1000)}@example.org"
    client.register(username, "correct horse battery")
    client.login(username, "correct horse battery", "Anki switch e2e")
    return client


def sync(col: Collection, client: V2Client):
    return sync_content_once(
        col, client, deck_names=None, newest_wins=True,
        accept_server_timestamp_truncation=True, scheduling_mode="server_fsrs",
    )


def server_fronts(client: V2Client) -> set[str]:
    return {
        client.get_note(item["guid"])["fields"][0]
        for item in client.manifest().get("notes", [])
    }


def local_fronts(col: Collection) -> set[str]:
    return {col.get_note(nid).fields[0] for nid in col.find_notes("")}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kelma-switch-e2e-"))
    procs = start_servers(tmp)
    url_a, url_b = (f"http://127.0.0.1:{port}" for port in PORTS)
    profile = tmp / "profile"
    profile.mkdir()
    col = Collection(str(profile / "collection.anki2"))
    try:
        for url in (url_a, url_b):
            sync_server.check(url)
        deck = col.decks.id("Switch E2E")
        for front in ("uno", "dos", "tres"):
            note = col.new_note(col.models.by_name("Basic"))
            note["Front"], note["Back"] = front, front.upper()
            col.add_note(note, deck)

        print("== profile on server A ==")
        client_a = account(url_a)
        sync(col, client_a)
        assert server_fronts(client_a) == {"uno", "dos", "tres"}

        print("== switch the profile to server B ==")
        reset = sync_server.reset_server_state(profile, col)
        assert "kelma_sync_v2_state.json" in reset, reset
        client_b = account(url_b)
        first_b = sync(col, client_b)
        assert server_fronts(client_b) == {"uno", "dos", "tres"}, "everything uploads to the new server"
        assert server_fronts(client_a) == {"uno", "dos", "tres"}, "server A is untouched"
        assert local_fronts(col) == {"uno", "dos", "tres"}
        again = sync_content_once(col, client_b, since=first_b.server_time, deck_names=None, newest_wins=True,
                                  accept_server_timestamp_truncation=True)
        assert again.notes.pushed == again.notes.pulled == again.cards.pushed == 0, again

        print("== a deletion on server B stays on server B ==")
        col.remove_notes(col.find_notes("dos"))
        sync(col, client_b)
        assert server_fronts(client_b) == {"uno", "tres"}
        assert server_fronts(client_a) == {"uno", "dos", "tres"}

        print("== switching back merges: nothing is deleted on either server ==")
        sync_server.reset_server_state(profile, col)
        sync(col, client_a)
        assert local_fronts(col) == {"uno", "dos", "tres"}, "server A's note comes back"
        assert server_fronts(client_a) == {"uno", "dos", "tres"}
        assert server_fronts(client_b) == {"uno", "tres"}

        print("== a stale snapshot from the other server is ignored ==")
        # Point the profile at B without the switch helper (e.g. an edited
        # config file). B holds a deletion of "dos"; A's snapshot lists "dos".
        sync(col, client_b)
        assert "dos" in local_fronts(col), "B's deletion must not apply through A's snapshot"
        assert server_fronts(client_b) == {"uno", "dos", "tres"}
        assert server_fronts(client_a) == {"uno", "dos", "tres"}
        print("OK: per-profile server switching")
        return 0
    finally:
        col.close()
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
