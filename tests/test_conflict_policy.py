from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma.kelma_sync_v2.conflict_policy import (
    modified_timestamp,
    newest_side,
    server_wins_anki_timestamp_truncation,
)


class ConflictPolicyTest(unittest.TestCase):
    def test_server_source_time_wins(self) -> None:
        local = {"modified_at": "2026-07-14T10:00:00Z"}
        server = {
            "client_modified_at": "2026-07-14T11:00:00Z",
            "modified_at": "2026-07-14T12:00:00Z",
        }
        self.assertEqual(newest_side(local, server), "server")

    def test_client_time_is_preferred_over_server_receipt_time(self) -> None:
        local = {"modified_at": "2026-07-14T11:00:00Z"}
        server = {
            "client_modified_at": "2026-07-14T10:00:00Z",
            "modified_at": "2026-07-14T12:00:00Z",
        }
        self.assertEqual(newest_side(local, server), "local")

    def test_old_manifest_falls_back_to_server_time(self) -> None:
        local = {"modified_at": "2026-07-14T10:00:00Z"}
        server = {"modified_at": "2026-07-14T11:00:00Z"}
        self.assertEqual(newest_side(local, server), "server")

    def test_invalid_client_time_falls_back_to_server_time(self) -> None:
        item = {
            "client_modified_at": "0001-01-01T00:00:00Z",
            "modified_at": "2026-07-14T11:00:00Z",
        }
        self.assertGreater(modified_timestamp(item), 0)

    def test_offsetless_legacy_time_is_always_utc(self) -> None:
        naive = {"modified_at": "2026-07-14T10:00:00"}
        utc = {"modified_at": "2026-07-14T10:00:00Z"}
        offset = {"modified_at": "2026-07-14T06:00:00-04:00"}
        self.assertEqual(modified_timestamp(naive), modified_timestamp(utc))
        self.assertEqual(modified_timestamp(offset), modified_timestamp(utc))

    def test_future_local_clock_cannot_overwrite_server(self) -> None:
        local = {"modified_at": "2026-07-14T16:00:00Z"}
        server = {
            "client_modified_at": "2026-07-14T11:59:00Z",
            "modified_at": "2026-07-14T11:59:30Z",
        }
        self.assertEqual(
            newest_side(local, server, utc_now="2026-07-14T12:00:00Z"),
            "server",
        )

    def test_future_server_source_uses_trusted_receipt_time(self) -> None:
        local = {"modified_at": "2026-07-14T11:00:00Z"}
        server = {
            "client_modified_at": "2026-07-14T16:00:00Z",
            "modified_at": "2026-07-14T11:30:00Z",
        }
        self.assertEqual(
            newest_side(local, server, utc_now="2026-07-14T12:00:00Z"),
            "server",
        )

    def test_anki_timestamp_truncation_can_be_recognized_without_weakening_ties(self) -> None:
        local = {"modified_at": "2026-07-14T10:00:00Z"}
        server = {"client_modified_at": "2026-07-14T10:00:00.210442Z"}
        self.assertTrue(server_wins_anki_timestamp_truncation(local, server))
        self.assertFalse(server_wins_anki_timestamp_truncation(local, local))
        self.assertFalse(server_wins_anki_timestamp_truncation(server, local))

    def test_tied_unknown_or_subsecond_times_remain_ambiguous(self) -> None:
        stamp = {"modified_at": "2026-07-14T10:00:00Z"}
        subsecond = {"modified_at": "2026-07-14T10:00:00.900Z"}
        self.assertIsNone(newest_side(stamp, stamp))
        self.assertIsNone(newest_side(stamp, subsecond))
        self.assertIsNone(newest_side({}, stamp))


if __name__ == "__main__":
    unittest.main()
