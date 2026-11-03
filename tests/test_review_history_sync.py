from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import ModuleType
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

anki = ModuleType("anki")
anki_collection = ModuleType("anki.collection")
anki_collection.Collection = object
anki.collection = anki_collection
sys.modules.setdefault("anki", anki)
sys.modules.setdefault("anki.collection", anki_collection)

from kelma.kelma_sync_v2.checksum_rs import review_checksum
from kelma.kelma_sync_v2.review_sync import _record_checksum, sync_reviews_once


class FakeDB:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE notes(id INTEGER PRIMARY KEY, guid TEXT);
            CREATE TABLE decks(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE cards(
                id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, odid INTEGER, ord INTEGER
            );
            CREATE TABLE revlog(
                id INTEGER PRIMARY KEY, cid INTEGER, usn INTEGER, ease INTEGER,
                ivl INTEGER, lastIvl INTEGER, factor INTEGER, time INTEGER, type INTEGER
            );
            INSERT INTO notes VALUES (10, 'g1');
            INSERT INTO decks VALUES (20, 'Deck');
            INSERT INTO cards VALUES (30, 10, 20, 0, 0);
            INSERT INTO revlog VALUES (1001, 30, -1, 3, 10, 1, 2500, 4000, 1);
        """)

    def all(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchall()

    def first(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchone()

    def scalar(self, sql: str, *args):
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None

    def execute(self, sql: str, *args):
        result = self.conn.execute(sql, args)
        self.conn.commit()
        return result

    def list(self, sql: str, *args):
        return [row[0] for row in self.conn.execute(sql, args)]


class FakeDecks:
    def __init__(self) -> None:
        self.deck = {
            "id": 20,
            "name": "Deck",
            "mod": 123,
            "usn": 0,
            "newToday": [5, 0],
            "revToday": [5, 0],
            "lrnToday": [5, 0],
            "timeToday": [5, 0],
        }

    def all(self):
        return [self.deck]

    def by_name(self, name: str):
        return self.deck if name == "Deck" else None

    def update(self, deck, preserve_usn=False):
        self.deck = deck


class FakeCollection:
    def __init__(self) -> None:
        self.db = FakeDB()
        self.decks = FakeDecks()
        self.crt = 1_700_000_000

    def save(self) -> None:
        pass


class FakeClient:
    def __init__(self, remote_review: dict) -> None:
        self.remote_review = remote_review
        self.pushes: list[dict] = []

    def batch_pull(self, **request):
        assert request == {"reviews": [1002]}
        return {"reviews": [self.remote_review]}

    def batch_push(self, payload):
        self.pushes.append(payload)
        return {
            "accepted": {
                "reviews": len(payload.get("reviews", [])),
                "study_days": len(payload.get("study_days", [])),
            },
            "conflicts": {"reviews": []},
        }


class ReviewHistorySyncTest(unittest.TestCase):
    def test_card_id_and_deck_do_not_change_review_identity(self) -> None:
        first = {
            "review_id": 1001, "source_card_id": 10, "note_guid": "g",
            "card_ord": 0, "deck_name": "A", "ease": 3, "interval": 10,
            "last_interval": 1, "factor": 2500, "taken_millis": 4000,
            "review_kind": 1,
        }
        relayed = {**first, "source_card_id": 99, "deck_name": "B"}
        self.assertEqual(_record_checksum(first), _record_checksum(relayed))

    def test_server_identity_is_retained_after_local_card_deletion(self) -> None:
        col = FakeCollection()
        checksum = review_checksum("g1", 0, 3, 10, 1, 2500, 4000, 1)
        col.db.execute("DELETE FROM cards")
        col.db.execute("DELETE FROM notes")
        result = sync_reviews_once(
            col,
            FakeClient({}),
            {"reviews": [{"review_id": 1001, "checksum": checksum}], "study_days": []},
            clear_pending_usn=True,
        )
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.conflicts, [])

    def test_dual_sync_marks_downloaded_history_pending_for_ankiweb(self) -> None:
        col = FakeCollection()
        remote = {
            "review_id": 1002,
            "source_card_id": 999,
            "note_guid": "g1",
            "card_ord": 0,
            "deck_name": "Deck",
            "ease": 3,
            "interval": 5,
            "last_interval": 1,
            "factor": 2500,
            "taken_millis": 1000,
            "review_kind": 1,
        }
        checksum = review_checksum("g1", 0, 3, 5, 1, 2500, 1000, 1)
        sync_reviews_once(
            col,
            FakeClient(remote),
            {"reviews": [{"review_id": 1002, "checksum": checksum}], "study_days": []},
            clear_pending_usn=False,
        )
        self.assertEqual(col.db.scalar("SELECT usn FROM revlog WHERE id=1002"), -1)

    def test_dual_sync_marks_daily_counters_pending_for_ankiweb(self) -> None:
        col = FakeCollection()
        checksum = review_checksum("g1", 0, 3, 10, 1, 2500, 4000, 1)
        epoch_day = col.crt // 86400 + 5
        result = sync_reviews_once(
            col,
            FakeClient({}),
            {
                "reviews": [{"review_id": 1001, "checksum": checksum}],
                "study_days": [{
                    "day": epoch_day,
                    "deck_name": "Deck",
                    "new_studied": 20,
                    "review_studied": 50,
                    "learning_studied": 0,
                    "milliseconds_studied": 1200,
                }],
            },
            clear_pending_usn=False,
            mark_study_days_pending=True,
        )
        self.assertEqual(result.study_days_marked_pending, 1)
        self.assertEqual(col.decks.deck["usn"], -1)

    def test_orphan_source_id_never_attaches_to_an_unrelated_card(self) -> None:
        col = FakeCollection()
        remote = {
            "review_id": 1002,
            "source_card_id": 30,
            "note_guid": "deleted-guid",
            "card_ord": 0,
            "deck_name": "Deleted",
            "ease": 3,
            "interval": 5,
            "last_interval": 1,
            "factor": 2500,
            "taken_millis": 1000,
            "review_kind": 1,
        }
        checksum = review_checksum("deleted-guid", 0, 3, 5, 1, 2500, 1000, 1)
        sync_reviews_once(
            col,
            FakeClient(remote),
            {"reviews": [{"review_id": 1002, "checksum": checksum}], "study_days": []},
            clear_pending_usn=True,
        )
        self.assertLess(col.db.scalar("SELECT cid FROM revlog WHERE id=1002"), 0)

    def test_history_union_maps_card_id_and_applies_daily_limit(self) -> None:
        col = FakeCollection()
        remote = {
            "review_id": 1002,
            "source_card_id": 999,
            "note_guid": "g1",
            "card_ord": 0,
            "deck_name": "Deck",
            "ease": 2,
            "interval": -600,
            "last_interval": 0,
            "factor": 2500,
            "taken_millis": 3000,
            "review_kind": 0,
        }
        remote_checksum = review_checksum(
            "g1", 0, 2, -600, 0, 2500, 3000, 0
        )
        epoch_day = col.crt // 86400 + 5
        manifest = {
            "reviews": [{"review_id": 1002, "checksum": remote_checksum}],
            "study_days": [{
                "day": epoch_day,
                "deck_name": "Deck",
                "new_studied": 20,
                "review_studied": 62,
                "learning_studied": 0,
                "milliseconds_studied": 123456,
            }],
        }
        client = FakeClient(remote)

        result = sync_reviews_once(
            col,
            client,
            manifest,
            clear_pending_usn=True,
        )

        self.assertEqual((result.pushed, result.pulled), (1, 1))
        pulled = col.db.first("SELECT cid,usn,ease FROM revlog WHERE id=1002")
        self.assertEqual(pulled, (30, 0, 2))
        self.assertEqual(col.db.scalar("SELECT usn FROM revlog WHERE id=1001"), 0)
        self.assertEqual(col.decks.deck["newToday"], [5, 20])
        self.assertEqual(col.decks.deck["revToday"], [5, 62])
        self.assertEqual(col.decks.deck["timeToday"], [5, 123456])
        review_pushes = [p for p in client.pushes if p.get("reviews")]
        self.assertEqual(len(review_pushes), 1)
        self.assertEqual(review_pushes[0]["reviews"][0]["review_id"], 1001)


if __name__ == "__main__":
    unittest.main()
