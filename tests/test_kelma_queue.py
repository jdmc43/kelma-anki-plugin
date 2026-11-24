from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def _counts(col, name: str) -> tuple[int, int, int]:
    def find(node, prefix=""):
        for child in node.children:
            full = f"{prefix}::{child.name}" if prefix else child.name
            if full == name:
                return child
            found = find(child, full)
            if found is not None:
                return found
        return None

    node = find(col.sched.deck_due_tree())
    return node.new_count, node.learn_count, node.review_count


class KelmaQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        from anki.collection import Collection

        self.folder = tempfile.TemporaryDirectory()
        self.col = Collection(str(Path(self.folder.name) / "collection.anki2"))

    def tearDown(self) -> None:
        self.col.close()
        self.folder.cleanup()

    def _deck(self, name: str, new_limit: int | None = None, review_limit: int | None = None) -> int:
        did = self.col.decks.id(name)
        deck = self.col.decks.get(did)
        if new_limit is not None:
            deck["newLimit"] = new_limit
        if review_limit is not None:
            deck["reviewLimit"] = review_limit
        self.col.decks.save(deck)
        return did

    def _notes(self, did: int, count: int, *, reviews: bool, model: str = "Basic") -> None:
        notetype = self.col.models.by_name(model)
        for index in range(count):
            note = self.col.new_note(notetype)
            note["Front"] = f"{did}-{index}-{model}"
            self.col.add_note(note, did)
            if reviews:
                self.col.db.execute(
                    "UPDATE cards SET type=2, queue=2, due=?, ivl=5 WHERE nid=?",
                    self.col.sched.today - 1, note.id,
                )
                for cid in self.col.db.list("SELECT id FROM cards WHERE nid=?", note.id):
                    self._revlog(cid, days_ago=10, kind=0)  # learned earlier

    _revlog_seq = 0

    def _revlog(self, cid: int, *, days_ago: int, kind: int) -> None:
        """A review-history row, as sync pulls it from another device."""
        KelmaQueueTest._revlog_seq += 1
        rid = (self.col.sched.day_cutoff - 86400 * (days_ago + 1) + 3600) * 1000 + KelmaQueueTest._revlog_seq
        self.col.db.execute(
            "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
            "VALUES (?, ?, 0, 3, 5, 1, 2500, 1000, ?)",
            rid, cid, kind,
        )

    def test_new_cards_are_allotted_before_reviews(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        did = self._deck("Mes Mots", new_limit=40, review_limit=100)
        self._notes(did, 50, reviews=False)
        self._notes(did, 120, reviews=True)
        self.assertEqual(_counts(self.col, "Mes Mots"), (0, 0, 100))  # Anki's own order
        kelma_queue.apply(self.col)
        self.assertEqual(_counts(self.col, "Mes Mots"), (40, 0, 60))
        snapshot = kelma_queue._Snapshot(self.col)
        self.assertEqual(kelma_queue.allot(snapshot, "Mes Mots"), (40, 0, 60))

    def test_due_siblings_count_once(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        did = self._deck("Words", new_limit=0, review_limit=200)
        self._notes(did, 10, reviews=True, model="Basic (and reversed card)")
        kelma_queue.apply(self.col)
        self.assertEqual(kelma_queue.allot(kelma_queue._Snapshot(self.col), "Words"), (0, 0, 10))
        self.assertEqual(_counts(self.col, "Words"), (0, 0, 10))

    def test_parent_limits_apply_to_subdecks(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        self._deck("Parent", new_limit=5, review_limit=30)
        child = self._deck("Parent::Child", new_limit=20, review_limit=200)
        self._notes(child, 10, reviews=False)
        self._notes(child, 40, reviews=True)
        kelma_queue.apply(self.col)
        snapshot = kelma_queue._Snapshot(self.col)
        for name in ("Parent", "Parent::Child"):
            self.assertEqual(_counts(self.col, name), kelma_queue.allot(snapshot, name), name)
        self.assertEqual(_counts(self.col, "Parent::Child"), (5, 0, 25))

    def test_cards_studied_on_another_device_count_against_today(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        did = self._deck("arabic", new_limit=4, review_limit=12)
        self._notes(did, 3, reviews=False)
        self._notes(did, 20, reviews=True)
        # Synced from Kelma Review: 2 new cards learned and 8 reviews done today.
        for cid in self.col.db.list("SELECT id FROM cards WHERE did=? AND queue=0 LIMIT 2", did):
            self._revlog(cid, days_ago=0, kind=0)
            self.col.db.execute("UPDATE cards SET type=2, queue=2, due=?, ivl=3 WHERE id=?", self.col.sched.today + 3, cid)
        for cid in self.col.db.list("SELECT id FROM cards WHERE did=? AND queue=2 AND due<=? LIMIT 8", did, self.col.sched.today):
            self._revlog(cid, days_ago=0, kind=1)
            self.col.db.execute("UPDATE cards SET due=? WHERE id=?", self.col.sched.today + 5, cid)
        self.assertEqual(_counts(self.col, "arabic"), (0, 0, 12))  # Anki alone knows nothing of them
        kelma_queue.apply(self.col)
        # Kelma Review: new 4 − 2 = 2 left, but only 1 new card remains; reviews 12 − 8 − 2 − 1 = 1.
        self.assertEqual(kelma_queue.allot(kelma_queue._Snapshot(self.col), "arabic"), (1, 0, 1))
        self.assertEqual(_counts(self.col, "arabic"), (1, 0, 1))

    def test_finished_deck_shows_nothing_left(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        did = self._deck("arabic", new_limit=0, review_limit=5)
        self._notes(did, 12, reviews=True)
        for cid in self.col.db.list("SELECT id FROM cards WHERE did=? LIMIT 5", did):
            self._revlog(cid, days_ago=0, kind=1)
            self.col.db.execute("UPDATE cards SET due=? WHERE id=?", self.col.sched.today + 4, cid)
        kelma_queue.apply(self.col)
        self.assertEqual(_counts(self.col, "arabic"), (0, 0, 0))

    def test_studying_a_subdeck_in_anki_is_counted_once(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        self._deck("P", new_limit=0, review_limit=30)
        child = self._deck("P::C", new_limit=0, review_limit=200)
        self._notes(child, 40, reviews=True)
        self.col.decks.select(child)
        for _ in range(3):
            self.col.sched.answerCard(self.col.sched.getCard(), 3)
        # Anki raises the subdeck's and the parent's counters for each answer.
        self.assertEqual(self.col.decks.get(child)["revToday"][1], 3)
        kelma_queue.apply(self.col)
        self.assertEqual(_counts(self.col, "P"), (0, 0, 27))
        self.assertEqual(_counts(self.col, "P::C"), (0, 0, 27))

    def test_options_match_kelma_review_and_reapplying_changes_nothing(self) -> None:
        from kelma.kelma_sync_v2 import kelma_queue

        did = self._deck("Deck", new_limit=10, review_limit=50)
        self._notes(did, 5, reviews=True)
        kelma_queue.apply(self.col)
        conf = self.col.decks.config_dict_for_deck_id(did)
        self.assertTrue(self.col.get_config("newCardsIgnoreReviewLimit"))
        self.assertTrue(self.col.get_config("applyAllParentLimits"))
        self.assertTrue(conf["new"]["bury"] and conf["rev"]["bury"])
        self.assertEqual(kelma_queue.apply(self.col), 0)


if __name__ == "__main__":
    unittest.main()
