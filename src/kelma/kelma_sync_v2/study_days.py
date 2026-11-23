"""Place KelmaSync FSRS schedules on the account's study days, like Kelma Review.

The server's FSRS projection stores the exact due time (`due_at_millis`) and an
interval in days. Kelma Review makes a review card due at the start of the
study day `study_day(last review) + interval`, where study days follow the
account's study-day policy (time zone and day-start hour). Interday learning
steps become due at the start of their study day; steps within the same study
day keep their exact time. Anki counts days from its own "today", so a Kelma
study day maps to an Anki day number by a constant offset taken at sync time.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

DAY_MS = 86_400_000


def _zone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone or no tz database (Windows)
        return datetime.now().astimezone().tzinfo or timezone.utc


class StudyDayMapper:
    def __init__(self, policy: dict[str, Any], anki_today: int, now_ms: int) -> None:
        self.timezone_id = str(policy.get("timezone_id") or "UTC")
        self.day_start_hour = min(23, max(0, int(policy.get("day_start_hour", 4) or 0)))
        self._zone = _zone(self.timezone_id)
        self.offset = int(anki_today) - self.study_day(now_ms)

    @property
    def key(self) -> str:
        return f"{self.timezone_id}|{self.day_start_hour}"

    def study_day(self, epoch_ms: int) -> int:
        local = datetime.fromtimestamp(epoch_ms / 1000, self._zone)
        day: date = local.date()
        if local.hour < self.day_start_hour:
            day -= timedelta(days=1)
        return day.toordinal()

    def anki_day(self, epoch_ms: int) -> int:
        return self.study_day(epoch_ms) + self.offset

    def anki_due(self, scheduling: dict[str, Any]) -> tuple[int, int] | None:
        """(queue, due) for a server FSRS schedule, or None if it has no FSRS times."""
        projection = scheduling.get("_kelma_fsrs")
        if not isinstance(projection, dict):
            return None
        due_at = int(projection.get("due_at_millis", 0) or 0)
        if due_at <= 0:
            return None
        queue = int(scheduling.get("queue", 0) or 0)
        if queue == 2:
            interval = int(scheduling.get("ivl", 0) or 0)
            last_review = due_at - interval * DAY_MS
            return 2, self.study_day(last_review) + interval + self.offset
        if queue in (1, 3):
            last_review = int(projection.get("through_review_id", 0) or 0)
            if last_review > 0 and self.study_day(due_at) > self.study_day(last_review):
                return 3, self.anki_day(due_at)
            return 1, due_at // 1000
        return None
