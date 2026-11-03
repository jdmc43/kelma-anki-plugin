from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

try:
    from anki.collection import Collection
    from kelma.account_transfer import (
        insert_deck,
        merge_conflicts,
        merge_decks,
        overwrite_deck,
    )
    from kelma.kelma_sync_v2.media_sync import delete_downloaded_media_for_redownload
except ModuleNotFoundError:
    Collection = None


@unittest.skipIf(Collection is None, "requires Anki's Python runtime")
class AccountTransferRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.current = Collection(str(root / "current.anki2"))
        self.other = Collection(str(root / "other.anki2"))

    def tearDown(self) -> None:
        self.current.close()
        self.other.close()
        self.temp.cleanup()

    def add(self, collection, deck: str, guid: str, front: str, modified: int) -> None:
        note = collection.new_note(collection.models.by_name("Basic"))
        note.guid = guid
        note["Front"] = front
        note["Back"] = "answer"
        collection.add_note(note, collection.decks.id(deck))
        collection.db.execute(
            "UPDATE notes SET mod=?,usn=0 WHERE id=?",
            modified,
            note.id,
        )

    def test_redownload_deletion_retains_only_local_media(self) -> None:
        note = self.current.new_note(self.current.models.by_name("Basic"))
        note["Front"] = '<img src="server.png"> [sound:local-only.mp3]'
        note["Back"] = "answer"
        self.current.add_note(note, self.current.decks.id("Media"))
        media = Path(self.current.media.dir())
        media.mkdir(parents=True, exist_ok=True)
        (media / "server.png").write_bytes(b"server")
        (media / "local-only.mp3").write_bytes(b"local")
        (media / "server-unreferenced.png").write_bytes(b"server-unreferenced")
        (media / "local-unreferenced.png").write_bytes(b"local-unreferenced")

        removed, removed_bytes = delete_downloaded_media_for_redownload(
            self.current,
            {
                "media": [
                    {"filename": "server.png", "size_bytes": 6},
                    {"filename": "server-unreferenced.png", "size_bytes": 19},
                ]
            },
        )

        self.assertEqual((removed, removed_bytes), (2, 25))
        self.assertFalse((media / "server.png").exists())
        self.assertFalse((media / "server-unreferenced.png").exists())
        self.assertTrue((media / "local-only.mp3").exists())
        self.assertTrue((media / "local-unreferenced.png").exists())

    def test_insert_overwrite_and_conflict_merge(self) -> None:
        self.add(self.current, "Insert Me", "insert-guid", "inserted", 10)
        result = insert_deck(self.current, self.other, "Insert Me")
        self.assertEqual(result["cards"], 1)
        self.assertEqual(len(self.other.find_cards('deck:"Insert Me"')), 1)

        self.add(self.current, "Preferred", "preferred-guid", "preferred", 20)
        self.add(self.other, "Replace Me", "old-guid", "old", 30)
        result = overwrite_deck(self.current, self.other, "Preferred", "Replace Me")
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(self.other.find_cards('deck:"Replace Me"')), 1)

        self.add(self.current, "Deck A", "shared-guid", "keep A", 100)
        self.add(self.current, "Deck A", "only-a", "only A", 100)
        self.add(self.other, "Deck B", "shared-guid", "keep B", 100)
        self.add(self.other, "Deck B", "only-b", "only B", 100)
        conflicts = merge_conflicts(
            self.current,
            self.other,
            "Deck A",
            "Deck B",
        )
        self.assertEqual(len(conflicts), 1)
        self.assertIsNone(conflicts[0].suggested)

        merge_decks(
            self.current,
            self.other,
            "Deck A",
            "Deck B",
            {"shared-guid": "current"},
        )
        self.assertEqual(len(self.current.find_cards('deck:"Deck A"')), 3)
        self.assertEqual(len(self.other.find_cards('deck:"Deck B"')), 3)
        for collection in (self.current, self.other):
            note_id = collection.db.scalar(
                "SELECT id FROM notes WHERE guid='shared-guid'"
            )
            self.assertEqual(collection.get_note(note_id)["Front"], "keep A")
        self.assertIsNone(self.other.decks.id_for_name("Deck A"))
        self.assertIsNone(self.current.decks.id_for_name("Deck B"))


if __name__ == "__main__":
    unittest.main()
