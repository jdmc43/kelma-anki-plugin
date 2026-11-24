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
