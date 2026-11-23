from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma import legacy_copies


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class LegacyCopiesTest(unittest.TestCase):
    def test_finds_measures_and_deletes_only_the_old_copies(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            profile = Path(base)
            # The profile's own collection must never be touched.
            _write(profile / "collection.anki2", 10)
            _write(profile / "collection.media" / "a.jpg", 10)
            _write(profile / "collection.media.db2", 10)
            # Look-alikes that are not copies of ours.
            _write(profile / "kelma_shadow_other.anki2", 10)
            _write(profile / "kelma_state.json", 10)
            # The old dual-sync copies.
            _write(profile / "kelma_shadow_kelmasync.anki2", 100)
            _write(profile / "kelma_shadow_kelmasync.anki2-wal", 5)
            _write(profile / "kelma_shadow_kelmasync.media" / "b.mp3", 200)
            _write(profile / "kelma_shadow_kelmasync.media" / "sub" / "c.png", 300)
            _write(profile / "kelma_shadow_kelmasync.media.db2", 7)
            _write(profile / "kelma_shadow_ankiweb.anki2", 50)

            found = legacy_copies.find(str(profile))
            self.assertEqual(
                sorted(os.path.basename(p) for p in found),
                [
                    "kelma_shadow_ankiweb.anki2",
                    "kelma_shadow_kelmasync.anki2",
                    "kelma_shadow_kelmasync.anki2-wal",
                    "kelma_shadow_kelmasync.media",
                    "kelma_shadow_kelmasync.media.db2",
                ],
            )
            self.assertEqual(legacy_copies.total_bytes(found), 100 + 5 + 200 + 300 + 7 + 50)

            self.assertEqual(legacy_copies.delete(found), [])
            self.assertEqual(legacy_copies.find(str(profile)), [])
            remaining = sorted(
                str(p.relative_to(profile)) for p in profile.rglob("*") if p.is_file()
            )
            self.assertEqual(
                remaining,
                [
                    "collection.anki2",
                    "collection.media.db2",
                    os.path.join("collection.media", "a.jpg"),
                    "kelma_shadow_other.anki2",
                    "kelma_state.json",
                ],
            )

    def test_symlinked_copies_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            profile = Path(base) / "profile"
            elsewhere = Path(base) / "elsewhere"
            _write(elsewhere / "keep.anki2", 10)
            _write(elsewhere / "media" / "keep.jpg", 10)
            profile.mkdir()
            os.symlink(elsewhere / "keep.anki2", profile / "kelma_shadow_kelmasync.anki2")
            os.symlink(elsewhere / "media", profile / "kelma_shadow_kelmasync.media")
            self.assertEqual(legacy_copies.find(str(profile)), [])

    def test_empty_profile_has_nothing_to_delete(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(legacy_copies.find(base), [])
            self.assertEqual(legacy_copies.total_bytes([]), 0)
            self.assertEqual(legacy_copies.delete([]), [])


if __name__ == "__main__":
    unittest.main()
