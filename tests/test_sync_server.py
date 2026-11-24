from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


class NormalizeTest(unittest.TestCase):
    def test_addresses_are_canonical(self) -> None:
        from kelma import sync_server

        cases = {
            "sync.example.org": "https://sync.example.org",
            " HTTPS://Sync.Example.org/ ": "https://sync.example.org",
            "https://sync.example.org:443": "https://sync.example.org",
            "http://192.168.1.20:8080/": "http://192.168.1.20:8080",
            "https://example.org/kelma/": "https://example.org/kelma",
            "https://sync2.ankiai.tech": "https://sync2.kelma.tech",
        }
        for raw, expected in cases.items():
            self.assertEqual(sync_server.normalize(raw), expected, raw)

    def test_bad_addresses_explain_why(self) -> None:
        from kelma import sync_server

        for raw in ("", "ftp://example.org", "https://", "https://example.org/?a=1", "https://u:p@example.org",
                    "https://example.org:99999"):
            with self.assertRaises(ValueError, msg=raw):
                sync_server.normalize(raw)

    def test_labels_and_safety(self) -> None:
        from kelma import sync_server

        self.assertTrue(sync_server.is_default("sync2.kelma.tech/"))
        self.assertEqual(sync_server.label("https://sync2.kelma.tech"), "Kelma")
        self.assertEqual(sync_server.label("https://sync.example.org:8443"), "sync.example.org:8443")
        self.assertTrue(sync_server.is_local("http://localhost:8080"))
        self.assertTrue(sync_server.is_local("http://10.0.0.5"))
        self.assertTrue(sync_server.is_local("http://nas.local"))
        self.assertFalse(sync_server.is_local("http://sync.example.org"))
        self.assertTrue(sync_server.is_unencrypted("http://sync.example.org"))


class SwitchTest(unittest.TestCase):
    def test_switching_signs_out_and_drops_the_checkpoint(self) -> None:
        from kelma import sync_server

        cfg = {
            "v2_url": "https://sync2.kelma.tech", "v2_username": "me@example.com", "v2_token": "secret",
            "v2_client_id": "c1", "v2_last_server_time": "2026-11-24T00:00:00Z", "v2_cursor_url": "https://sync2.kelma.tech",
            "v2_allow_large_deletes": True, "v2_client_label": "Anki plugin", "v2_scheduling_mode": "server_fsrs",
        }
        switched = sync_server.switched_account(cfg, "sync.example.org")
        self.assertEqual(switched["v2_url"], "https://sync.example.org")
        for key in ("v2_username", "v2_token", "v2_client_id", "v2_last_server_time", "v2_cursor_url"):
            self.assertEqual(switched[key], "", key)
        self.assertFalse(switched["v2_allow_large_deletes"])
        self.assertEqual(switched["v2_client_label"], "Anki plugin")
        self.assertEqual(cfg["v2_token"], "secret")  # input untouched

    def test_reset_forgets_old_server_state_but_keeps_the_collection(self) -> None:
        from anki.collection import Collection
        from kelma import consts, sync_server

        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            col = Collection(str(profile / "collection.anki2"))
            try:
                note = col.new_note(col.models.by_name("Basic"))
                note["Front"] = "keep me"
                col.add_note(note, col.decks.id("Deck"))
                (profile / "kelma_server_manifest.json.gz").write_bytes(b"x")
                media_state = Path(col.media.dir()) / "kelma_sync_v2_state.json"
                media_state.write_text("{}")
                col.set_config("kelmaReviewsVerified", {"account": "a", "through": 5})
                col.set_config("kelmaSchedulerProfile", "hash")
                (profile / "kelma_state.json").write_text(json.dumps(
                    {"meta": {consts.KELMA: {"at": 1}, consts.ANKIWEB: {"at": 2}}}
                ))

                reset = sync_server.reset_server_state(profile, col)

                self.assertFalse((profile / "kelma_server_manifest.json.gz").exists())
                self.assertFalse(media_state.exists())
                self.assertIsNone(col.get_config("kelmaReviewsVerified", None))
                self.assertIsNone(col.get_config("kelmaSchedulerProfile", None))
                state = json.loads((profile / "kelma_state.json").read_text())
                self.assertEqual(state["meta"], {consts.ANKIWEB: {"at": 2}})
                self.assertIn("deck badge baseline", reset)
                self.assertEqual(col.note_count(), 1)
                self.assertEqual(sync_server.reset_server_state(profile, col), [])  # idempotent
            finally:
                col.close()


class SnapshotServerTest(unittest.TestCase):
    def test_snapshots_belong_to_one_server(self) -> None:
        from kelma.kelma_sync_v2 import sync_state

        snapshot = sync_state.build_state(["n1"], ["1"], [], ["Deck"], server="https://a.example")
        self.assertTrue(sync_state.same_server(snapshot, "https://a.example/"))
        self.assertFalse(sync_state.same_server(snapshot, "https://b.example"))
        # Written before servers could be switched: always the profile's server.
        legacy = {"version": 3, "notes": ["n1"]}
        self.assertTrue(sync_state.same_server(legacy, "https://b.example"))


if __name__ == "__main__":
    unittest.main()
