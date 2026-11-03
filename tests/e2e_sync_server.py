#!/usr/bin/env python3
"""End-to-end Anki add-on sync test against a local KelmaSync server.

Run with Anki's Python runtime. ``KELMA_SYNC_SERVER_REPO`` may override the
sibling ``../kelma_sync_2`` server checkout used for building and migrations.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = Path(
    os.environ.get("KELMA_SYNC_SERVER_REPO", ROOT.parent / "kelma_sync_2")
).resolve()
PORT = int(os.environ.get("E2E_PORT", "18082"))
BASE_URL = f"http://localhost:{PORT}"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://kelma:kelma@localhost:5433/kelma_sync_2_test?sslmode=disable",
)

sys.path.insert(0, str(ROOT / "src"))

from anki.collection import Collection  # noqa: E402
from kelma.kelma_sync_v2.client import V2Client  # noqa: E402
from kelma.kelma_sync_v2.content_sync import sync_content_once  # noqa: E402
from kelma.kelma_sync_v2 import sync_state  # noqa: E402


def wait_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"server did not open port {port}")


def start_server() -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update({
        "PORT": str(PORT),
        "DATABASE_URL": DATABASE_URL,
        "MIGRATIONS_DIR": str(SERVER_ROOT / "migrations"),
        "KELMA_AUTH_MODE": "local",
    })
    bin_path = Path(tempfile.gettempdir()) / "kelma-sync2-anki-e2e"
    subprocess.run(
        ["go", "build", "-o", str(bin_path), "./cmd/server"],
        cwd=SERVER_ROOT,
        check=True,
        timeout=30,
    )
    return subprocess.Popen(
        [str(bin_path)],
        cwd=SERVER_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def make_collection(path: Path, *, with_note: bool = True) -> Collection:
    col = Collection(str(path))
    deck_id = col.decks.id("Kelma E2E Test")
    ignored_deck_id = col.decks.id("Ignored Local Deck")
    nt = col.models.new("Kelma E2E Basic")
    col.models.add_field(nt, col.models.new_field("Front"))
    col.models.add_field(nt, col.models.new_field("Back"))
    tmpl = col.models.new_template("Card 1")
    tmpl["qfmt"] = "{{Front}}"
    tmpl["afmt"] = "{{FrontSide}}<hr id=answer>{{Back}}"
    col.models.add_template(nt, tmpl)
    col.models.add(nt)
    if with_note:
        media_dir = Path(col.media.dir())
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "kelma-e2e.png").write_bytes(b"kelma fake png bytes")
        note = col.new_note(nt)
        note.fields[0] = '<img src="kelma-e2e.png"> front e2e'
        note.fields[1] = "back e2e"
        note.tags = ["kelma-e2e"]
        col.add_note(note, deck_id)
        ignored = col.new_note(nt)
        ignored.fields[0] = "ignored front"
        ignored.fields[1] = "ignored back"
        ignored.tags = ["ignored"]
        col.add_note(ignored, ignored_deck_id)
    col.save()
    return col


def main() -> int:
    print("== ensuring postgres ==")
    subprocess.run(
        ["docker", "compose", "up", "-d", "postgres"],
        cwd=SERVER_ROOT,
        check=True,
        timeout=30,
    )

    proc = start_server()
    tmp = Path(tempfile.mkdtemp(prefix="kelma-anki-e2e-"))
    try:
        wait_port(PORT)
        print("server ok")

        col_path = tmp / "collection-a.anki2"
        col = make_collection(col_path, with_note=True)
        col_b = None
        col_fresh = None
        try:
            client = V2Client(BASE_URL)
            username = f"anki-e2e-{int(time.time()*1000)}@example.com"
            password = "test-password"
            client.register(username, password)
            client.login(username, password, "Anki E2E")

            scope = ["Kelma E2E Test"]
            print("== sync scoped content ==")
            result = sync_content_once(col, client, deck_names=scope)
            assert result.decks.pushed >= 1, result
            assert result.notetypes.pushed >= 1, result
            assert result.notes.pushed >= 1, result
            assert result.cards.pushed >= 1, result
            print("sync ok", result)

            print("== second sync is checksum-noop ==")
            result2 = sync_content_once(col, client, since=result.server_time, deck_names=scope)
            assert result2.decks.pushed == 0, result2
            assert result2.notetypes.pushed == 0, result2
            assert result2.notes.pushed == 0, result2
            assert result2.cards.pushed == 0, result2
            assert result2.media.uploaded == 0, result2
            print("noop sync ok", result2)

            print("== verify server note ==")
            manifest = client.manifest()
            notes = manifest.get("notes", [])
            assert len(notes) == 1, manifest
            guid = notes[0]["guid"]
            server_note = client.get_note(guid)
            assert server_note["fields"] == ['<img src="kelma-e2e.png"> front e2e', "back e2e"], server_note
            assert len(manifest.get("decks", [])) >= 1, manifest
            assert not any(d.get("name") == "Ignored Local Deck" for d in manifest.get("decks", [])), manifest
            assert len(manifest.get("cards", [])) >= 1, manifest
            assert any(m.get("filename") == "kelma-e2e.png" for m in client.manifest().get("media", []))

            print("== fresh unscoped KelmaDesktop restore ==")
            fresh_path = tmp / "collection-fresh.anki2"
            col_fresh = Collection(str(fresh_path))
            assert col_fresh.db.scalar("SELECT COUNT(*) FROM notes") == 0
            assert col_fresh.db.scalar("SELECT COUNT(*) FROM cards") == 0
            fresh_client = V2Client(BASE_URL, token=client.token)
            restored = sync_content_once(col_fresh, fresh_client, deck_names=None)
            assert restored.decks.pushed == 0, restored
            assert restored.notetypes.pushed == 0, restored
            assert restored.notes.pulled == 1, restored
            assert restored.cards.pulled == 1, restored
            assert restored.media.downloaded == 1, restored
            assert col_fresh.db.scalar("SELECT COUNT(*) FROM notes") == 1
            assert col_fresh.db.scalar("SELECT COUNT(*) FROM cards") == 1
            assert (Path(col_fresh.media.dir()) / "kelma-e2e.png").read_bytes() == b"kelma fake png bytes"
            restored_state = sync_state.load_state(col_fresh)
            assert len(restored_state.get("notes", [])) == 1, restored_state
            assert len(restored_state.get("cards", [])) == 1, restored_state

            print("== restored collection converges without reconciliation ==")
            restored2 = sync_content_once(col_fresh, fresh_client, deck_names=None)
            assert restored2.decks.pushed == 0, restored2
            assert restored2.notetypes.pushed == 0, restored2
            assert restored2.notes.pushed == 0 and restored2.notes.pulled == 0, restored2
            assert restored2.cards.pushed == 0 and restored2.cards.pulled == 0, restored2
            assert restored2.media.uploaded == 0 and restored2.media.downloaded == 0, restored2
            col_fresh.close()
            col_fresh = None

            print("== server tombstone deletes note on second client ==")
            # Simulate a second device cloned from the first synced collection.
            col.close()
            col = None
            col_b_path = tmp / "collection-b.anki2"
            shutil.copy(col_path, col_b_path)
            col_b = Collection(str(col_b_path))
            assert col_b.db.scalar("SELECT COUNT(*) FROM notes WHERE guid = ?", guid) == 1
            client_b = V2Client(BASE_URL, token=client.token)
            synced_b = sync_content_once(col_b, client_b, deck_names=scope)
            assert synced_b.media.downloaded == 1, synced_b
            media_path = Path(col_b.media.dir()) / "kelma-e2e.png"
            assert media_path.read_bytes() == b"kelma fake png bytes"
            client.delete_note(guid)
            deleted = sync_content_once(col_b, client_b, deck_names=scope)
            assert deleted.tombstones.applied >= 1, deleted
            assert col_b.db.scalar("SELECT COUNT(*) FROM notes WHERE guid = ?", guid) == 0

            print("== local dependent delete converges after server note tombstone ==")
            # This server note was already tombstoned above. Deleting the stale
            # local copy should remove any remaining server child card without
            # echoing the incoming note tombstone back as a local note delete.
            col_path2 = tmp / "collection-c.anki2"
            shutil.copy(col_path, col_path2)
            # Also copy the media dir so the sync state snapshot is present.
            media_src = col_path.parent / "collection-a.media"
            media_dst = col_path2.parent / "collection-c.media"
            if media_src.exists():
                shutil.copytree(media_src, media_dst, dirs_exist_ok=True)
            col_c = Collection(str(col_path2))
            assert col_c.db.scalar("SELECT COUNT(*) FROM notes WHERE guid = ?", guid) == 1
            row = col_c.db.first("SELECT id FROM notes WHERE guid = ?", guid)
            col_c.remove_notes([int(row[0])])
            assert col_c.db.scalar("SELECT COUNT(*) FROM notes WHERE guid = ?", guid) == 0
            client_c = V2Client(BASE_URL, token=client.token)
            deleted_local = sync_content_once(col_c, client_c, deck_names=scope)
            assert "notes" not in deleted_local.local_deletes, deleted_local.local_deletes
            assert "cards" in deleted_local.local_deletes, deleted_local.local_deletes
            # Server should no longer have the note.
            assert len(client_c.manifest().get("notes", [])) == 0
            # And a tombstone should exist for it.
            assert any(
                t.get("type") == "note" and t.get("resource_id") == guid
                for t in client_c.manifest().get("tombstones", [])
            )
            col_c.close()

            print("ANKI E2E OK")
        finally:
            if col is not None:
                col.close()
            if col_b is not None:
                col_b.close()
            if col_fresh is not None:
                col_fresh.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.stdout:
            out = proc.stdout.read()
            if out:
                print("\n== server log ==")
                print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
