from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma.kelma_sync_v2.study_days import DAY_MS, StudyDayMapper

NY = ZoneInfo("America/New_York")
POLICY = {"timezone_id": "America/New_York", "day_start_hour": 4}


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=NY).timestamp() * 1000)


def review(last_review: str, interval: int) -> dict:
    due_at = ms(last_review) + interval * DAY_MS
    return {"type": 2, "queue": 2, "ivl": interval, "due": interval,
            "_kelma_fsrs": {"due_at_millis": due_at, "through_review_id": ms(last_review)}}


def learning(last_review: str, due: str) -> dict:
    return {"type": 1, "queue": 1, "ivl": 0, "due": ms(due) // 1000,
            "_kelma_fsrs": {"due_at_millis": ms(due), "through_review_id": ms(last_review)}}


class StudyDayMapperTest(unittest.TestCase):
    def setUp(self) -> None:
        # Anki's "today" is day 100 at noon on 23 November (study day 23 Nov).
        self.mapper = StudyDayMapper(POLICY, anki_today=100, now_ms=ms("2026-11-23T12:00:00"))

    def test_an_evening_review_is_due_on_its_study_day_not_the_utc_day(self) -> None:
        # 23:08 New York is already 2 November in UTC; Kelma Review counts from
        # study day 1 November, so +22 days is due today.
        self.assertEqual(self.mapper.anki_due(review("2026-11-01T23:08:00", 22)), (2, 100))

    def test_a_review_before_the_day_start_hour_belongs_to_the_previous_day(self) -> None:
        # 02:00 on 10 November is study day 9 November; +3 days → 12 November.
        self.assertEqual(self.mapper.anki_due(review("2026-11-10T02:00:00", 3)), (2, 100 - 11))

    def test_a_review_across_the_daylight_saving_change(self) -> None:
        mapper = StudyDayMapper(POLICY, anki_today=0, now_ms=ms("2026-03-08T12:00:00"))
        # 7 March 22:00 EST + 1 day is due on study day 8 March (today).
        self.assertEqual(mapper.anki_due(review("2026-03-07T22:00:00", 1)), (2, 0))

    def test_learning_steps_within_the_study_day_keep_their_exact_time(self) -> None:
        card = learning("2026-11-23T23:50:00", "2026-11-24T00:00:00")
        self.assertEqual(self.mapper.anki_due(card), (1, ms("2026-11-24T00:00:00") // 1000))

    def test_learning_steps_into_a_later_study_day_are_due_that_day(self) -> None:
        card = learning("2026-11-23T20:00:00", "2026-11-24T10:00:00")
        self.assertEqual(self.mapper.anki_due(card), (3, 101))

    def test_schedules_without_fsrs_times_are_left_to_the_crt_conversion(self) -> None:
        self.assertIsNone(self.mapper.anki_due({"type": 2, "queue": 2, "ivl": 5, "due": 40}))
        self.assertIsNone(self.mapper.anki_due({"type": 0, "queue": 0, "_kelma_fsrs": {"due_at_millis": 5}}))

    def test_an_unknown_time_zone_falls_back_to_local_time(self) -> None:
        mapper = StudyDayMapper({"timezone_id": "Not/AZone", "day_start_hour": 4}, 0, ms("2026-11-23T12:00:00"))
        self.assertEqual(mapper.anki_due(review("2026-11-20T12:00:00", 3))[0], 2)


class RealignmentTest(unittest.TestCase):
    def test_cards_holding_the_projection_move_without_a_local_edit(self) -> None:
        from anki.collection import Collection
        from kelma.kelma_sync_v2.schedule_alignment import realign_projected_cards

        with tempfile.TemporaryDirectory() as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                model = col.models.by_name("Basic")
                guids = []
                for front in ("a", "b", "c", "d"):
                    note = col.new_note(model)
                    note["Front"] = front
                    col.add_note(note, 1)
                    guids.append(note.guid)
                ids = col.db.list("SELECT c.id FROM cards c JOIN notes n ON n.id = c.nid ORDER BY n.id")
                mapper = StudyDayMapper(POLICY, col.sched.today, ms("2026-11-23T12:00:00"))
                projection = review("2026-11-01T23:08:00", 22)
                projection.update({"reps": 3, "lapses": 0, "data": '{"s":22.0,"d":5.0}'})
                today = col.sched.today
                # a: holds the projection, due one day late (old UTC conversion)
                # b: same, but suspended
                # c: reviewed in Anki since (different review state)
                # d: in a filtered deck
                for cid, queue, reps, odid in ((ids[0], 2, 3, 0), (ids[1], -1, 3, 0), (ids[2], 2, 4, 0), (ids[3], 2, 3, 5)):
                    col.db.execute(
                        "UPDATE cards SET type=2, queue=?, due=?, ivl=22, reps=?, lapses=0, data=?, odid=?, mod=111 WHERE id=?",
                        queue, today + 1, reps, projection["data"], odid, cid,
                    )

                class Client:
                    def batch_pull(self, cards):
                        return {"cards": [
                            {"card_id": cid, "note_guid": guid, "ord": 0, "scheduling": projection}
                            for cid, guid in zip(ids, guids)
                        ]}

                manifest = {"cards": [{"card_id": cid} for cid in ids]}
                moved = realign_projected_cards(col, Client(), manifest, mapper)
                rows = dict((cid, (queue, due, mod)) for cid, queue, due, mod in
                            col.db.all("SELECT id, queue, due, mod FROM cards"))
                self.assertEqual(moved, 2)
                self.assertEqual(rows[ids[0]], (2, today, 111))
                self.assertEqual(rows[ids[1]], (-1, today, 111))
                self.assertEqual(rows[ids[2]], (2, today + 1, 111))
                self.assertEqual(rows[ids[3]], (2, today + 1, 111))
            finally:
                col.close()


class SchedulerSettingsTest(unittest.TestCase):
    def test_the_account_fsrs_profile_is_applied_once(self) -> None:
        from anki.collection import Collection
        from kelma.kelma_sync_v2 import scheduler_settings

        profile = {"profile": {
            "parameters": [0.2 + i / 100 for i in range(21)],
            "desired_retention": 0.88,
            "learning_steps_seconds": [60, 600, 3600],
            "relearning_steps_seconds": [300],
            "maximum_interval": 1000,
            "config_hash": "h1",
        }}
        with tempfile.TemporaryDirectory() as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                self.assertTrue(scheduler_settings.apply_profile(col, profile))
                conf = col.decks.config_dict_for_deck_id(1)
                self.assertTrue(col.get_config("fsrs"))
                self.assertEqual(len(conf["fsrsParams6"]), 21)
                self.assertAlmostEqual(conf["desiredRetention"], 0.88)
                self.assertEqual(conf["new"]["delays"], [1.0, 10.0, 60.0])
                self.assertEqual(conf["lapse"]["delays"], [5.0])
                self.assertEqual(conf["rev"]["maxIvl"], 1000)
                self.assertFalse(scheduler_settings.apply_profile(col, profile))
                self.assertFalse(scheduler_settings.apply_profile(col, {"profile": {"parameters": [1, 2]}}))
            finally:
                col.close()


class ProjectionWaitTest(unittest.TestCase):
    def test_waits_for_the_server_to_schedule_uploaded_reviews(self) -> None:
        from unittest.mock import patch
        from kelma.kelma_sync_v2 import content_sync

        class Client:
            def __init__(self, counts):
                self.counts = list(counts)

            def scheduler_profile(self):
                pending = self.counts.pop(0) if self.counts else 0
                return {"projections": {"pending": pending, "running": 0}}

        with patch.object(content_sync.time, "sleep"):
            self.assertTrue(content_sync._wait_for_projections(Client([3, 1, 0])))
            busy = Client([5] * 1000)
            with patch.object(content_sync.time, "time", side_effect=[0, 0, 30]):
                self.assertFalse(content_sync._wait_for_projections(busy, timeout=20))

        class Broken:
            def scheduler_profile(self):
                raise RuntimeError("offline")

        self.assertFalse(content_sync._wait_for_projections(Broken()))


if __name__ == "__main__":
    unittest.main()
