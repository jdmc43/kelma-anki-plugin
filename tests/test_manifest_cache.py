from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma.kelma_sync_v2 import manifest_cache


def _keys(manifest: dict, resource: str) -> dict:
    key_of = manifest_cache._KEY_OF[resource]
    return {key_of(row): row for row in manifest.get(resource, [])}


BASE = {
    "server_time": "2026-11-23T10:00:00Z",
    "capabilities": ["a"],
    "notes": [
        {"guid": "n1", "checksum": "c1", "modified_at": "2026-11-20T00:00:00Z"},
        {"guid": "n2", "checksum": "c2", "modified_at": "2026-11-20T00:00:00Z"},
    ],
    "cards": [
        {"card_id": 1, "note_guid": "n1", "ord": 0, "checksum": "k1", "modified_at": "2026-11-20T00:00:00Z"},
        {"card_id": 2, "note_guid": "n2", "ord": 0, "checksum": "k2", "modified_at": "2026-11-20T00:00:00Z"},
    ],
    "reviews": [{"review_id": 100, "checksum": "r", "modified_at": "2026-11-20T00:00:00Z"}],
    "review_retractions": [],
    "study_days": [{"day": 1, "deck_name": "D", "new_studied": 1, "modified_at": "2026-11-20T00:00:00Z"}],
    "notetypes": [],
    "decks": [{"name": "D", "checksum": "d", "modified_at": "2026-11-20T00:00:00Z"}],
    "media": [{"filename": "a.jpg", "size_bytes": 1, "modified_at": "2026-11-20T00:00:00Z"}],
    "tombstones": [
        {"id": "t-old", "type": "media", "resource_id": "b.jpg", "deleted_at": "2026-11-21T00:00:00Z"},
    ],
}


class ManifestMergeTest(unittest.TestCase):
    def test_changed_rows_replace_and_new_rows_append(self) -> None:
        delta = {
            "server_time": "2026-11-23T11:00:00Z",
            "capabilities": ["a", "b"],
            "notes": [{"guid": "n1", "checksum": "c1b", "modified_at": "2026-11-23T10:30:00Z"}],
            "cards": [{"card_id": 3, "note_guid": "n3", "ord": 0, "checksum": "k3", "modified_at": "2026-11-23T10:30:00Z"}],
            "reviews": [{"review_id": 101, "checksum": "r2", "modified_at": "2026-11-23T10:30:00Z"}],
            "review_retractions": [{"review_id": 100, "modified_at": "2026-11-23T10:31:00Z"}],
            "study_days": [{"day": 1, "deck_name": "D", "new_studied": 5, "modified_at": "2026-11-23T10:30:00Z"}],
            "tombstones": [],
        }
        merged = manifest_cache.merge(BASE, delta)
        self.assertEqual(merged["server_time"], "2026-11-23T11:00:00Z")
        self.assertEqual(merged["capabilities"], ["a", "b"])
        self.assertEqual(_keys(merged, "notes")["n1"]["checksum"], "c1b")
        self.assertEqual(set(_keys(merged, "cards")), {"1", "2", "3"})
        self.assertEqual(set(_keys(merged, "reviews")), {"100", "101"})
        self.assertEqual(set(_keys(merged, "review_retractions")), {"100"})
        self.assertEqual(merged["study_days"][0]["new_studied"], 5)
        self.assertEqual(len(merged["study_days"]), 1)
        # The base manifest is not modified.
        self.assertEqual(_keys(BASE, "notes")["n1"]["checksum"], "c1")

    def test_a_deleted_note_takes_its_cards_with_it(self) -> None:
        delta = {
            "server_time": "2026-11-23T11:00:00Z",
            "tombstones": [{"id": "t1", "type": "note", "resource_id": "n2", "deleted_at": "2026-11-23T10:30:00Z"}],
        }
        merged = manifest_cache.merge(BASE, delta)
        self.assertEqual(set(_keys(merged, "notes")), {"n1"})
        self.assertEqual(set(_keys(merged, "cards")), {"1"})
        self.assertIn("t1", _keys(merged, "tombstones"))

    def test_a_row_recreated_after_its_deletion_stays(self) -> None:
        delta = {
            "server_time": "2026-11-23T11:00:00Z",
            "decks": [{"name": "D", "checksum": "d2", "modified_at": "2026-11-23T10:45:00Z"}],
            "tombstones": [{"id": "t2", "type": "deck", "resource_id": "D", "deleted_at": "2026-11-23T10:30:00Z"}],
        }
        merged = manifest_cache.merge(BASE, delta)
        self.assertEqual(_keys(merged, "decks")["D"]["checksum"], "d2")

    def test_deleted_before_a_recreation_in_the_same_delta_is_removed(self) -> None:
        delta = {
            "server_time": "2026-11-23T11:00:00Z",
            "decks": [{"name": "D", "checksum": "d2", "modified_at": "2026-11-23T10:15:00Z"}],
            "tombstones": [{"id": "t3", "type": "deck", "resource_id": "D", "deleted_at": "2026-11-23T10:30:00Z"}],
        }
        self.assertNotIn("D", _keys(manifest_cache.merge(BASE, delta), "decks"))

    def test_uploading_media_again_clears_its_tombstone(self) -> None:
        delta = {
            "server_time": "2026-11-23T11:00:00Z",
            "media": [{"filename": "b.jpg", "size_bytes": 2, "modified_at": "2026-11-23T10:30:00Z"}],
        }
        merged = manifest_cache.merge(BASE, delta)
        self.assertIn("b.jpg", _keys(merged, "media"))
        self.assertNotIn("t-old", _keys(merged, "tombstones"))


class ManifestCacheTest(unittest.TestCase):
    class Client:
        def __init__(self, full: dict, delta: dict) -> None:
            self.endpoint = "https://sync.example"
            self.token = "token-a"
            self.full, self.delta, self.calls = full, delta, []

        def manifest(self, since=None):
            self.calls.append(since)
            return dict(self.delta if since else self.full)

    def test_uses_the_cache_and_a_delta_then_refreshes_daily(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            col = SimpleNamespace(path=str(Path(folder) / "collection.anki2"))
            delta = {"server_time": "2026-11-23T11:00:00Z", "notes": [{"guid": "n9", "modified_at": "2026-11-23T10:30:00Z"}]}
            client = self.Client(BASE, delta)
            first = manifest_cache.fetch(col, client)
            self.assertEqual(client.calls, [None])
            manifest_cache.save(col, client, first)

            second = manifest_cache.fetch(col, client)
            self.assertEqual(client.calls, [None, BASE["server_time"]])
            self.assertIn("n9", _keys(second, "notes"))

            other_account = self.Client(BASE, delta)
            other_account.token = "token-b"
            manifest_cache.fetch(col, other_account)
            self.assertEqual(other_account.calls, [None])

            stale = dict(first, _kelma_full_at=time.time() - manifest_cache.FULL_REFRESH_SECONDS - 1)
            manifest_cache.save(col, client, stale)
            client.calls.clear()
            manifest_cache.fetch(col, client)
            self.assertEqual(client.calls, [None])

    def test_a_failed_delta_falls_back_to_a_full_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            col = SimpleNamespace(path=str(Path(folder) / "collection.anki2"))
            client = self.Client(BASE, {})
            manifest_cache.save(col, client, dict(BASE, _kelma_full_at=time.time()))

            def failing(since=None):
                client.calls.append(since)
                if since:
                    raise RuntimeError("offline")
                return dict(BASE)

            client.manifest = failing
            manifest_cache.fetch(col, client)
            self.assertEqual(client.calls, [BASE["server_time"], None])

    def test_an_unchanged_manifest_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            col = SimpleNamespace(path=str(Path(folder) / "collection.anki2"))
            client = self.Client(BASE, {"server_time": "2026-11-23T12:00:00Z"})
            manifest_cache.save(col, client, manifest_cache.fetch(col, client))
            path = Path(folder) / manifest_cache._FILENAME
            before = path.stat().st_mtime_ns
            quiet = manifest_cache.refresh(client, manifest_cache.fetch(col, client))
            self.assertEqual(quiet["_kelma_changes"], 0)
            manifest_cache.save(col, client, quiet)
            self.assertEqual(path.stat().st_mtime_ns, before)
            client.delta = {"server_time": "2026-11-23T13:00:00Z", "notes": [{"guid": "n5", "modified_at": "2026-11-23T12:30:00Z"}]}
            changed = manifest_cache.refresh(client, manifest_cache.fetch(col, client))
            self.assertGreater(changed["_kelma_changes"], 0)
            manifest_cache.save(col, client, changed)
            self.assertIn("n5", _keys(manifest_cache.fetch(col, self.Client(BASE, {"server_time": "z"})), "notes"))


if __name__ == "__main__":
    unittest.main()
