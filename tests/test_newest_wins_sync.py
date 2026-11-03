from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

# The policy modules only need Collection for annotations. Keep these unit tests
# independent of a full Anki build/runtime.
anki = ModuleType("anki")
anki_collection = ModuleType("anki.collection")
anki_collection.Collection = object
anki.collection = anki_collection
sys.modules.setdefault("anki", anki)
sys.modules.setdefault("anki.collection", anki_collection)

from kelma.kelma_sync_v2 import (
    anki_apply,
    anki_local,
    card_sync,
    deck_sync,
    note_sync,
    notetype_sync,
)


class FakeClient:
    def __init__(self, pulled: dict | None = None) -> None:
        self.pulled = pulled or {"notes": [], "cards": []}
        self.pull_requests: list[dict] = []
        self.push_requests: list[dict] = []

    def batch_pull(self, **request):
        self.pull_requests.append(request)
        return self.pulled

    def get_notetype(self, notetype_id):
        return next(
            item
            for item in self.pulled.get("notetypes", [])
            if int(item["notetype_id"]) == int(notetype_id)
        )

    def batch_push(self, payload):
        self.push_requests.append(payload)
        return {
            "accepted": {
                "notes": len(payload.get("notes", [])),
                "cards": len(payload.get("cards", [])),
            },
            "conflicts": {"notes": []},
        }


class NewestWinsSyncTest(unittest.TestCase):
    def test_old_notetype_fields_are_hydrated_for_current_anki(self) -> None:
        models = SimpleNamespace(
            new=lambda name: {"name": name, "flds": [], "tmpls": [], "req": []},
            new_field=lambda name: {
                "name": name,
                "ord": None,
                "sticky": False,
                "description": "",
            },
            new_template=lambda name: {
                "name": name,
                "ord": None,
                "qfmt": "",
                "afmt": "",
                "bqfmt": "",
            },
        )
        definition = anki_apply._anki_compatible_notetype_definition(
            SimpleNamespace(models=models),
            {
                "flds": [{"name": "Front", "ord": 0}],
                "tmpls": [{"name": "Card 1", "ord": 0, "qfmt": "{{Front}}"}],
            },
            None,
            "Legacy",
        )

        self.assertFalse(definition["flds"][0]["sticky"])
        self.assertEqual(definition["flds"][0]["description"], "")
        self.assertEqual(definition["tmpls"][0]["bqfmt"], "")

    def test_anki_stock_kind_metadata_is_not_a_notetype_conflict(self) -> None:
        shared = {"flds": [{"name": "Front"}], "tmpls": []}
        local = {
            "name": "Basic",
            "definition": {**shared, "originalStockKind": 1},
        }
        server = {"name": "Basic", "definition": shared}
        self.assertTrue(
            notetype_sync._canonically_equivalent_notetypes(local, server)
        )

    def test_hydrated_stock_kind_notetype_is_skipped_after_pull(self) -> None:
        stamp = "2026-07-14T10:00:00Z"
        local_manifest = [{
            "notetype_id": 1,
            "checksum": "local",
            "modified_at": stamp,
        }]
        server_manifest = {"notetypes": [{
            "notetype_id": 1,
            "checksum": "server",
            "client_modified_at": stamp,
        }]}
        shared = {"flds": [{"name": "Front"}], "tmpls": []}
        local_record = {
            "name": "Basic",
            "definition": {**shared, "originalStockKind": 1},
        }
        server_record = {
            "notetype_id": 1,
            "name": "Basic",
            "definition": shared,
        }
        client = FakeClient(pulled={"notetypes": [server_record]})
        with (
            patch.object(
                anki_local, "notetype_manifest", return_value=local_manifest
            ),
            patch.object(
                anki_local, "notetype_record", return_value=local_record
            ),
            patch.object(anki_apply, "apply_server_notetype") as apply_server,
        ):
            result = notetype_sync.sync_notetypes_once(
                object(), client, server_manifest=server_manifest,
                newest_wins=True,
                accept_server_timestamp_truncation=True,
            )
        self.assertEqual(result.skipped, 1)
        apply_server.assert_not_called()

    def test_anki_mod_is_serialized_as_explicit_utc(self) -> None:
        self.assertEqual(
            anki_local.iso_from_anki_mod(1784044800),
            "2026-07-14T16:00:00Z",
        )

    def test_future_server_source_is_not_written_into_local_mod(self) -> None:
        record = {
            "client_modified_at": "2026-07-14T16:00:00Z",
            "modified_at": "2026-07-14T12:00:00Z",
        }
        expected = int(
            datetime.fromisoformat("2026-07-14T12:00:00+00:00").timestamp()
        )
        self.assertEqual(anki_apply._source_modified_seconds(record), expected)

    def test_server_newer_note_is_pulled_without_conflict(self) -> None:
        local = [{
            "guid": "g1", "checksum": "local",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server = {
            "server_time": "2026-07-14T12:00:00Z",
            "notes": [{
                "guid": "g1", "checksum": "server",
                "client_modified_at": "2026-07-14T11:00:00Z",
                "modified_at": "2026-07-14T12:00:00Z",
            }],
        }
        client = FakeClient(pulled={"notes": [{
            "guid": "g1", "fields": ["new"], "tags": [],
            "client_modified_at": "2026-07-14T11:00:00Z",
        }]})
        applied: list[str] = []
        with (
            patch.object(anki_local, "note_manifest", return_value=local),
            patch.object(anki_apply, "apply_note", side_effect=lambda _col, rec: applied.append(rec["guid"])),
        ):
            result = note_sync.sync_notes_once(
                object(), client, server_manifest=server, newest_wins=True
            )
        self.assertEqual(result.pulled, 1)
        self.assertEqual(applied, ["g1"])
        self.assertEqual(client.pull_requests, [{"notes": ["g1"]}])
        self.assertEqual(client.push_requests, [])

    def test_local_newer_note_uses_server_checksum_as_write_base(self) -> None:
        local = [{
            "guid": "g1", "checksum": "local",
            "modified_at": "2026-07-14T11:00:00Z",
        }]
        server = {"notes": [{
            "guid": "g1", "checksum": "server",
            "client_modified_at": "2026-07-14T10:00:00Z",
        }]}
        local_record = {
            "guid": "g1", "notetype_id": 1, "fields": ["new"], "tags": [],
            "client_modified_at": "2026-07-14T11:00:00Z",
        }
        client = FakeClient()
        with (
            patch.object(anki_local, "note_manifest", return_value=local),
            patch.object(anki_local, "note_record", return_value=local_record),
        ):
            result = note_sync.sync_notes_once(
                object(), client, server_manifest=server, newest_wins=True
            )
        self.assertEqual(result.pushed, 1)
        payload = client.push_requests[0]["notes"][0]
        self.assertEqual(payload["base_checksum"], "server")

    def test_freshly_pulled_subsecond_note_does_not_prompt_again(self) -> None:
        local = [{
            "guid": "g1", "checksum": "local",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server = {"notes": [{
            "guid": "g1", "checksum": "server",
            "client_modified_at": "2026-07-14T10:00:00.210442Z",
        }]}
        client = FakeClient(pulled={"notes": [{
            "guid": "g1", "fields": ["server"], "tags": [],
            "client_modified_at": "2026-07-14T10:00:00.210442Z",
        }]})
        with (
            patch.object(anki_local, "note_manifest", return_value=local),
            patch.object(
                anki_local,
                "note_record",
                return_value={"fields": ["local"], "tags": []},
            ),
            patch.object(anki_apply, "apply_note"),
        ):
            result = note_sync.sync_notes_once(
                object(),
                client,
                server_manifest=server,
                newest_wins=True,
                accept_server_timestamp_truncation=True,
            )
        self.assertEqual(result.pulled, 1)
        self.assertEqual(result.conflicts, [])

    def test_tied_note_times_remain_a_real_conflict(self) -> None:
        stamp = "2026-07-14T10:00:00Z"
        local = [{"guid": "g1", "checksum": "local", "modified_at": stamp}]
        server = {"notes": [{
            "guid": "g1", "checksum": "server", "client_modified_at": stamp,
        }]}
        client = FakeClient()
        with patch.object(anki_local, "note_manifest", return_value=local):
            with self.assertRaises(note_sync.NoteSyncConflict):
                note_sync.sync_notes_once(
                    object(), client, server_manifest=server, newest_wins=True
                )

    def test_unicode_canonical_order_does_not_create_a_note_conflict(self) -> None:
        local = [{
            "guid": "g1", "checksum": "local",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server = {
            "server_time": "2026-07-14T10:00:01Z",
            "notes": [{
                "guid": "g1", "checksum": "server",
                "client_modified_at": "2026-07-14T10:00:00.834Z",
            }],
        }
        # The only difference is canonical combining-mark order:
        # fatha+shadda vs shadda+fatha.
        local_record = {
            "guid": "g1", "notetype_id": 1,
            "fields": ["base\u064e\u0651"], "tags": ["tag"],
            "client_modified_at": "2026-07-14T10:00:00Z",
        }
        server_record = {
            "guid": "g1", "notetype_id": 1,
            "fields": ["base\u0651\u064e"], "tags": ["tag"],
            "client_modified_at": "2026-07-14T10:00:00.834Z",
        }
        client = FakeClient(pulled={"notes": [server_record]})
        with (
            patch.object(anki_local, "note_manifest", return_value=local),
            patch.object(anki_local, "note_record", return_value=local_record),
        ):
            result = note_sync.sync_notes_once(
                object(), client, server_manifest=server, newest_wins=True,
                accept_server_timestamp_truncation=True,
            )
        self.assertEqual(result.pushed, 1)
        self.assertEqual(client.pull_requests, [{"notes": ["g1"]}])
        self.assertEqual(
            client.push_requests[0]["notes"][0]["base_checksum"], "server"
        )

    def test_server_newer_deck_and_notetype_are_pulled(self) -> None:
        deck_local = [{
            "name": "Deck", "checksum": "local",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        deck_server = {"decks": [{
            "name": "Deck", "checksum": "server",
            "client_modified_at": "2026-07-14T11:00:00Z",
        }]}
        notetype_local = [{
            "notetype_id": 1, "checksum": "local",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        notetype_server = {"notetypes": [{
            "notetype_id": 1, "checksum": "server",
            "client_modified_at": "2026-07-14T11:00:00Z",
        }]}
        client = FakeClient()
        with (
            patch.object(anki_local, "deck_manifest", return_value=deck_local),
            patch.object(anki_apply, "apply_server_deck") as apply_deck,
        ):
            decks = deck_sync.sync_decks_once(
                object(), client, server_manifest=deck_server, newest_wins=True
            )
        with (
            patch.object(anki_local, "notetype_manifest", return_value=notetype_local),
            patch.object(anki_apply, "apply_server_notetype") as apply_notetype,
        ):
            notetypes = notetype_sync.sync_notetypes_once(
                object(), client, server_manifest=notetype_server,
                newest_wins=True,
            )
        self.assertEqual(decks.pulled, 1)
        self.assertEqual(notetypes.pulled, 1)
        apply_deck.assert_called_once_with(ANY, client, "Deck")
        apply_notetype.assert_called_once_with(ANY, client, 1)

    def test_suspended_state_pushes_independently_in_local_scheduler_mode(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "study_state": "suspended",
            "study_state_client_modified_at": "2026-07-14T12:00:00Z",
            "modified_at": "2026-07-14T12:00:00Z",
        }]
        server = {"cards": [{
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "study_state": "active",
            "study_state_client_modified_at": "2026-07-14T11:00:00Z",
            "study_state_modified_at": "2026-07-14T11:00:01Z",
        }]}
        record = {
            "card_id": 1, "note_guid": "g1", "deck_name": "Deck", "ord": 0,
            "scheduling": {"queue": -1}, "study_state": "suspended",
            "study_state_client_modified_at": "2026-07-14T12:00:00Z",
            "client_modified_at": "2026-07-14T12:00:00Z",
        }
        client = FakeClient()
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(anki_local, "card_record", return_value=record),
        ):
            result = card_sync.sync_cards_once(
                object(), client, server_manifest=server, scheduling_mode="local_anki"
            )

        self.assertEqual(result.pushed, 1)
        pushed = client.push_requests[0]["cards"][0]
        self.assertEqual(pushed["study_state"], "suspended")
        self.assertEqual(pushed["study_state_client_modified_at"], "2026-07-14T12:00:00Z")

    def test_server_schedule_reset_is_applied_and_reported_for_ankiweb(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "schedule_reset_through_review_id": 0,
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server_card = {
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "schedule_reset_through_review_id": 200,
            "schedule_reset_modified_at": "2026-07-14T11:00:00Z",
            "modified_at": "2026-07-14T11:00:00Z",
        }
        client = FakeClient(pulled={"cards": [server_card]})
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(card_sync.sync_state, "load_state", return_value={
                "cardScheduleResets": {"g1:0": {"throughReviewId": 100}}
            }),
            patch.object(anki_apply, "apply_card", return_value=1) as apply,
        ):
            result = card_sync.sync_cards_once(
                object(), client, server_manifest={"cards": [server_card]},
                scheduling_mode="local_anki",
            )

        self.assertEqual(result.schedule_reset_pulled_card_ids, [1])
        self.assertTrue(apply.call_args.kwargs["apply_schedule_reset"])

    def test_applying_due_date_uses_anki_native_scheduler(self) -> None:
        class FakeDB:
            def first(self, *_args):
                return (1,)

            def execute(self, *_args):
                return None

        class FakeScheduler:
            today = 5

            def __init__(self) -> None:
                self.calls = []

            def set_due_date(self, card_ids, days):
                self.calls.append((card_ids, days))

        scheduler = FakeScheduler()
        collection = SimpleNamespace(
            crt=10 * 86400,
            db=FakeDB(),
            sched=scheduler,
            decks=SimpleNamespace(id=lambda _name: 1),
        )
        due_at = 20 * 86_400_000

        applied = anki_apply.apply_card(
            collection,
            {
                "note_guid": "g1", "ord": 0, "deck_name": "Deck",
                "due_date_override_ms": due_at,
            },
            apply_scheduling=False,
            apply_study_state=False,
            apply_due_date_override=True,
        )

        self.assertEqual(applied, 1)
        self.assertEqual(scheduler.calls, [([1], "5")])

    def test_manual_revlog_is_the_authoritative_local_due_date_signal(self) -> None:
        old_review_id = card_sync._timestamp_millis("2026-07-14T00:00:00Z")
        manual_id = card_sync._timestamp_millis("2026-07-15T00:00:00.123Z")
        due_at, modified_at = card_sync._local_due_date_state(
            SimpleNamespace(crt=1_700_000_000),
            {
                "scheduling": {"type": 2, "queue": 2, "due": 10},
                "modified_at": "2026-07-14T11:59:59Z",
                "max_review_id": old_review_id,
                "max_manual_review_id": manual_id,
            },
            {
                "dueAtMillis": 0,
                "modifiedAt": "2026-07-14T10:00:00Z",
                "ankiDue": 5,
                "maxReviewId": old_review_id,
                "maxManualReviewId": 0,
            },
            card_sync._timestamp_millis("2026-07-14T12:00:00Z"),
        )

        self.assertEqual(due_at, (1_700_000_000 // 86400 + 10) * 86_400_000)
        self.assertEqual(modified_at, "2026-07-15T00:00:00.123000Z")

    def test_rated_review_after_manual_due_clears_the_override(self) -> None:
        old_review_id = card_sync._timestamp_millis("2026-07-14T00:00:00Z")
        manual_id = card_sync._timestamp_millis("2026-07-15T00:00:00.123Z")
        review_id = card_sync._timestamp_millis("2026-07-16T00:00:00.456Z")
        due_at, modified_at = card_sync._local_due_date_state(
            SimpleNamespace(crt=1_700_000_000),
            {
                "scheduling": {"type": 2, "queue": 2, "due": 20},
                "modified_at": "2026-07-16T00:00:01Z",
                "max_review_id": review_id,
                "max_manual_review_id": manual_id,
            },
            {
                "dueAtMillis": card_sync._timestamp_millis("2026-07-20T00:00:00Z"),
                "modifiedAt": "2026-07-15T00:00:00.123Z",
                "ankiDue": 10,
                "maxReviewId": old_review_id,
                "maxManualReviewId": manual_id,
            },
            card_sync._timestamp_millis("2026-07-15T12:00:00Z"),
        )

        self.assertEqual(due_at, 0)
        self.assertEqual(modified_at, "2026-07-16T00:00:00.456000Z")

    def test_unrelated_card_mod_does_not_create_a_due_override(self) -> None:
        state = card_sync._local_due_date_state(
            SimpleNamespace(crt=1_700_000_000),
            {
                "scheduling": {"type": 2, "queue": 2, "due": 10},
                "modified_at": "2026-07-16T12:00:00Z",
                "max_review_id": 100,
                "max_manual_review_id": 0,
            },
            {
                "dueAtMillis": 0,
                "modifiedAt": "2026-07-15T00:00:00Z",
                "ankiDue": 10,
                "maxReviewId": 100,
                "maxManualReviewId": 0,
            },
            card_sync._timestamp_millis("2026-07-15T12:00:00Z"),
        )

        self.assertEqual(state, (0, "2026-07-15T00:00:00Z"))

    def test_server_due_date_is_applied_and_reported_for_ankiweb(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "scheduling": {"type": 2, "queue": 2, "due": 100},
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server_card = {
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "due_date_override_ms": 1784678400000,
            "due_date_override_modified_at": "2026-07-14T11:00:00Z",
        }
        client = FakeClient(pulled={"cards": [server_card]})
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(card_sync.sync_state, "load_state", return_value={}),
            patch.object(anki_apply, "apply_card", return_value=1) as apply,
        ):
            result = card_sync.sync_cards_once(
                SimpleNamespace(crt=0), client,
                server_manifest={"cards": [server_card]},
                scheduling_mode="local_anki",
            )

        self.assertEqual(result.due_date_pulled_card_ids, [1])
        self.assertTrue(apply.call_args.kwargs["apply_due_date_override"])

    def test_local_manual_due_date_is_pushed_independently(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "note_guid": "g1", "deck_name": "Deck", "ord": 0,
            "scheduling": {"type": 2, "queue": 2, "due": 10},
            "modified_at": "2026-07-14T12:00:00Z",
            "client_modified_at": "2026-07-14T12:00:00Z",
            "max_review_id": 1784023200000,
        }]
        server_card = {
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "due_date_override_ms": 0,
            "due_date_override_modified_at": "2026-07-14T10:00:00Z",
        }
        client = FakeClient()
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(anki_local, "card_record", return_value=local[0]),
            patch.object(card_sync.sync_state, "load_state", return_value={
                "completedAt": "2026-07-14T11:00:00Z",
            }),
        ):
            card_sync.sync_cards_once(
                SimpleNamespace(crt=1_700_000_000), client,
                server_manifest={"cards": [server_card]},
                scheduling_mode="local_anki",
            )

        pushed = client.push_requests[0]["cards"][0]
        expected = (1_700_000_000 // 86400 + 10) * 86_400_000
        self.assertEqual(pushed["due_date_override_ms"], expected)
        self.assertEqual(
            pushed["due_date_override_client_modified_at"],
            "2026-07-14T12:00:00Z",
        )

    def test_server_reset_does_not_erase_later_local_reviews(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "schedule_reset_through_review_id": 0, "max_review_id": 300,
            "modified_at": "2026-07-14T12:00:00Z",
        }]
        server_card = {
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "schedule_reset_through_review_id": 200,
            "schedule_reset_modified_at": "2026-07-14T11:00:00Z",
            "modified_at": "2026-07-14T11:00:00Z",
        }
        client = FakeClient()
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(card_sync.sync_state, "load_state", return_value={}),
            patch.object(anki_apply, "apply_card") as apply,
        ):
            result = card_sync.sync_cards_once(
                object(), client, server_manifest={"cards": [server_card]},
                scheduling_mode="local_anki",
            )

        self.assertEqual(result.schedule_reset_pulled_card_ids, [])
        apply.assert_not_called()

    def test_local_schedule_reset_is_pushed_independently(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "note_guid": "g1", "deck_name": "Deck", "ord": 0,
            "scheduling": {}, "client_modified_at": "2026-07-14T11:00:00Z",
            "schedule_reset_through_review_id": 200,
            "schedule_reset_client_modified_at": "2026-07-14T11:00:00Z",
        }]
        server_card = {
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "schedule_reset_through_review_id": 100,
            "modified_at": "2026-07-14T10:00:00Z",
        }
        client = FakeClient()
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(anki_local, "card_record", return_value=local[0]),
            patch.object(card_sync.sync_state, "load_state", return_value={
                "cardScheduleResets": {"g1:0": {"throughReviewId": 100}}
            }),
        ):
            card_sync.sync_cards_once(
                object(), client, server_manifest={"cards": [server_card]},
                scheduling_mode="local_anki",
            )

        pushed = client.push_requests[0]["cards"][0]
        self.assertEqual(pushed["schedule_reset_through_review_id"], 200)

    def test_server_suspension_is_reported_for_ankiweb_publish(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "study_state": "active",
            "study_state_client_modified_at": "2026-07-14T10:00:00Z",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server_card = {
            "logical_key": "g1:0", "card_id": 2, "checksum": "same",
            "study_state": "suspended",
            "study_state_client_modified_at": "2026-07-14T11:00:00Z",
            "study_state_modified_at": "2026-07-14T11:00:01Z",
        }
        client = FakeClient(pulled={"cards": [server_card]})
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(anki_apply, "apply_card", return_value=1),
        ):
            result = card_sync.sync_cards_once(
                object(), client, server_manifest={"cards": [server_card]},
                scheduling_mode="local_anki",
            )

        self.assertEqual(result.study_state_pulled_card_ids, [1])

    def test_server_newer_structural_card_is_pulled(self) -> None:
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "local",
            "modified_at": "2026-07-14T10:00:00Z",
        }]
        server = {"cards": [{
            "logical_key": "g1:0", "card_id": 2, "checksum": "server",
            "client_modified_at": "2026-07-14T11:00:00Z",
        }]}
        client = FakeClient(pulled={"cards": [{"card_id": 2}]})
        applied: list[int] = []
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(
                anki_apply,
                "apply_card",
                side_effect=lambda _col, rec, **_kwargs: applied.append(rec["card_id"]),
            ),
        ):
            result = card_sync.sync_cards_once(
                object(), client, server_manifest=server, newest_wins=True
            )
        self.assertEqual(result.pulled, 1)
        self.assertEqual(applied, [2])

    def test_card_generated_by_upstream_note_pull_cannot_overwrite_server(self) -> None:
        # Anki gives a newly generated local card the current time (12:00), but
        # the corresponding upstream card was uploaded earlier (11:00). The
        # current local timestamp is generation bookkeeping, not a local edit.
        local = [{
            "logical_key": "g1:0", "card_id": 1, "checksum": "same",
            "modified_at": "2026-07-14T12:00:00Z",
        }]
        server = {
            "server_time": "2026-07-14T12:00:01Z",
            "cards": [{
                "logical_key": "g1:0", "card_id": 2, "checksum": "same",
                "client_modified_at": "2026-07-14T11:00:00Z",
            }],
        }
        client = FakeClient(pulled={"cards": [{"card_id": 2}]})
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(anki_apply, "apply_card") as apply_card,
        ):
            result = card_sync.sync_cards_once(
                object(),
                client,
                server_manifest=server,
                newest_wins=True,
                server_authoritative_keys={"g1:0"},
            )
        self.assertEqual(result.pulled, 1)
        self.assertEqual(result.pushed, 0)
        self.assertEqual(client.push_requests, [])
        apply_card.assert_called_once_with(
            ANY, {"card_id": 2}, apply_scheduling=True, apply_study_state=True,
            apply_schedule_reset=False, apply_due_date_override=False,
        )

    def test_interrupted_note_pull_restores_pristine_generated_card(self) -> None:
        generated_at = 1_784_044_800
        local = [{
            "logical_key": "g1:0",
            "card_id": generated_at * 1000,
            "note_id": generated_at * 1000 + 1,
            "deck_id": 1,
            "deck_name": "Default",
            "checksum": "local-default",
            "modified_at": "2026-07-14T16:00:00Z",
            "scheduling": {
                "type": 0, "queue": 0, "ivl": 0, "reps": 0, "lapses": 0,
            },
        }]
        server = {
            "server_time": "2026-07-14T16:00:01Z",
            "cards": [{
                "logical_key": "g1:0", "card_id": 2,
                "deck_name": "arabic", "checksum": "server-arabic",
                "client_modified_at": "2026-07-14T11:00:00Z",
            }],
        }
        client = FakeClient(pulled={"cards": [{"card_id": 2}]})
        with (
            patch.object(anki_local, "card_manifest", return_value=local),
            patch.object(anki_apply, "apply_card") as apply_card,
        ):
            result = card_sync.sync_cards_once(
                object(), client, server_manifest=server, newest_wins=True
            )
        self.assertEqual(result.pulled, 1)
        self.assertEqual(result.pushed, 0)
        self.assertEqual(client.push_requests, [])
        apply_card.assert_called_once_with(
            ANY, {"card_id": 2}, apply_scheduling=True, apply_study_state=True,
            apply_schedule_reset=False, apply_due_date_override=False,
        )


class DesktopModeTest(unittest.TestCase):
    def test_desktop_runtime_overrides_shared_false_config(self) -> None:
        aqt = ModuleType("aqt")
        aqt.mw = SimpleNamespace(
            addonManager=SimpleNamespace(
                getConfig=lambda _addon: {
                    "kelmasync_only": False,
                    "ankiweb_hkey": "stale-shared-profile-key",
                },
                writeConfig=lambda *_args: None,
            )
        )
        aqt._kelma_bundled = SimpleNamespace(IS_KELMA_DESKTOP=True)
        old = sys.modules.get("aqt")
        sys.modules["aqt"] = aqt
        try:
            sys.modules.pop("kelma.config", None)
            from kelma import config, consts
            self.assertTrue(config.kelmasync_only())
            self.assertEqual(config.ui_services(), (consts.KELMA,))
            self.assertFalse(config.has_credentials(consts.ANKIWEB))
        finally:
            sys.modules.pop("kelma.config", None)
            if old is None:
                sys.modules.pop("aqt", None)
            else:
                sys.modules["aqt"] = old


if __name__ == "__main__":
    unittest.main()
