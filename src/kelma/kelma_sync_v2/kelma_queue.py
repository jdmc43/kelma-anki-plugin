"""Make Anki's daily counts follow Kelma Review's queue rules.

Kelma Review fills a deck's day like this (ReviewQueueOrdering.kt): new cards
are allotted first and count against the review limit, then reviews take what
is left; every limit on the path from the top deck applies; a card is held back
when a sibling of its note is already in today's allotment (new and review
siblings are buried by default). Anki always fills reviews first and gives new
cards the rest, so the same deck can show 0 new + 100 due in Anki but 40 new +
60 due in Kelma Review.

What counts as studied today also follows Kelma Review: it is read from the
review history (a card's first review ever is a new card, a review-type answer
counts once per card), so reviews done in Kelma Review or on another device
count as soon as they are synced. Anki's own "studied today" counters only grow
for answers given in this Anki, and a parent deck's counters include its
subdecks. Siblings of cards studied today are held back.

Anki can show the same numbers: with "new cards ignore review limit" and
"apply all parent limits" on, each deck's "today only" limits are set to
Anki's own counter plus what Kelma Review allots, so what remains is exactly
the allotment. Those "today only" values are local (Kelma Review and KelmaSync
ignore them) and expire by themselves the next day; the add-on sets them again
after every sync and when a profile opens or the day changes.
"""
from __future__ import annotations

from collections import defaultdict
import time
from typing import Any

LEARN_AHEAD_SECONDS = 20 * 60


def _path(name: str) -> list[str]:
    parts = name.split("::")
    return ["::".join(parts[: index + 1]) for index in range(len(parts))]


def _in_subtree(name: str, root: str) -> bool:
    return name == root or name.startswith(root + "::")


class _Snapshot:
    """Everything the allotment needs, read once from the collection."""

    def __init__(self, col) -> None:
        self.today = int(col.sched.today)
        self.now = int(time.time())
        self.decks = {
            int(deck["id"]): deck for deck in col.decks.all() if not deck.get("dyn")
        }
        self.name_of = {did: str(deck["name"]) for did, deck in self.decks.items()}
        self.by_name = {name: did for did, name in self.name_of.items()}
        self.limits: dict[str, tuple[int, int]] = {}
        # Anki's own counters (this Anki's answers only, subdecks included).
        self.counters: dict[str, tuple[int, int]] = {}
        self.preset_bury: dict[str, tuple[bool, bool]] = {}
        for did, deck in self.decks.items():
            name = self.name_of[did]
            conf = col.decks.config_dict_for_deck_id(did)
            new_limit = deck.get("newLimit")
            review_limit = deck.get("reviewLimit")
            self.limits[name] = (
                int(conf["new"]["perDay"] if new_limit is None else new_limit),
                int(conf["rev"]["perDay"] if review_limit is None else review_limit),
            )
            new_today = deck.get("newToday") or [0, 0]
            rev_today = deck.get("revToday") or [0, 0]
            self.counters[name] = (
                int(new_today[1]) if int(new_today[0]) == self.today else 0,
                int(rev_today[1]) if int(rev_today[0]) == self.today else 0,
            )
            self.preset_bury[name] = (bool(conf["new"].get("bury")), bool(conf["rev"].get("bury")))
        self.cards_by_deck: dict[str, list[tuple]] = defaultdict(list)
        for cid, guid, ord_, did, queue, due in col.db.all(
            "SELECT c.id, n.guid, c.ord, c.did, c.queue, c.due FROM cards c "
            "JOIN notes n ON n.id = c.nid WHERE c.queue >= 0 AND c.odid = 0"
        ):
            name = self.name_of.get(int(did))
            if name is not None:
                self.cards_by_deck[name].append((int(cid), guid, int(ord_), int(queue), int(due)))
        self._read_studied_today(col)

    def _read_studied_today(self, col) -> None:
        """Today's studied cards per deck from the review history, as Kelma Review counts them."""
        start_ms = (int(col.sched.day_cutoff) - 86400) * 1000
        new: dict[str, set[int]] = defaultdict(set)
        review: dict[str, set[int]] = defaultdict(set)
        self.studied_ords: dict[str, set[int]] = defaultdict(set)
        for cid, guid, ord_, did, odid, typ, first in col.db.all(
            "SELECT r.cid, n.guid, c.ord, c.did, c.odid, r.type, "
            "r.id = (SELECT min(id) FROM revlog WHERE cid = r.cid) "
            "FROM revlog r JOIN cards c ON c.id = r.cid JOIN notes n ON n.id = c.nid "
            "WHERE r.id >= ? ORDER BY r.id",
            start_ms,
        ):
            self.studied_ords[guid].add(int(ord_))
            name = self.name_of.get(int(odid or did))
            if name is None:
                continue
            if first:
                new[name].add(int(cid))
            elif int(typ) == 1:
                review[name].add(int(cid))
        self.studied = {
            name: (len(new.get(name, ())), len(review.get(name, ())))
            for name in self.by_name
        }

    def subtree_studied(self, root: str) -> tuple[int, int]:
        new = review = 0
        for name, (studied_new, studied_review) in self.studied.items():
            if _in_subtree(name, root):
                new += studied_new
                review += studied_review
        # Anki's counter covers subdecks too; it can only be higher when the
        # history is missing rows (e.g. answers whose cards were deleted).
        counter_new, counter_review = self.counters.get(root, (0, 0))
        return max(new, counter_new), max(review, counter_review)


def allot(snapshot: _Snapshot, root: str, *, bury: bool = True) -> tuple[int, int, int]:
    """Kelma Review's (new, learning, review) counts for `root` and its subdecks."""
    return _allot(snapshot, root, bury=bury)[:3]


def _allot(snapshot: _Snapshot, root: str, *, bury: bool = True) -> tuple[int, int, int, int]:
    """(new, learning, review, interday learning) for `root`."""
    names = [name for name in snapshot.by_name if _in_subtree(name, root)]
    limit_decks = set(_path(root)) | {deck for name in names for deck in _path(name)}
    remaining_new: dict[str, int] = {}
    remaining_review: dict[str, int] = {}
    for deck in limit_decks:
        if deck not in snapshot.limits:
            continue
        new_limit, review_limit = snapshot.limits[deck]
        studied_new, studied_review = snapshot.subtree_studied(deck)
        remaining_new[deck] = max(0, new_limit - studied_new)
        remaining_review[deck] = max(0, review_limit - studied_review - studied_new)
    cards = [(name, card) for name in names for card in snapshot.cards_by_deck.get(name, [])]
    new_cards = sorted(
        (item for item in cards if item[1][3] == 0),
        key=lambda item: (item[0].lower(), item[1][4], item[1][0]),
    )
    reviews = sorted(
        (item for item in cards if item[1][3] == 2 and item[1][4] <= snapshot.today),
        key=lambda item: (item[1][4], item[1][0]),
    )
    intraday = sum(
        1
        for _name, (_cid, _guid, _ord, queue, due) in cards
        if queue == 1 and due <= snapshot.now + LEARN_AHEAD_SECONDS
    )
    interday = sum(
        1 for _name, (_cid, _guid, _ord, queue, due) in cards if queue == 3 and due <= snapshot.today
    )
    # Siblings of cards studied today are held back, as in Kelma Review.
    seen: dict[str, set[int]] = defaultdict(set)
    if bury:
        for guid, ords in snapshot.studied_ords.items():
            seen[guid] = set(ords)
    allotted_new = allotted_review = 0
    for name, (_cid, guid, ord_, _queue, _due) in new_cards:
        if bury and seen[guid] - {ord_}:
            continue
        path = [deck for deck in _path(name) if deck in remaining_new]
        if all(remaining_new[deck] > 0 and remaining_review[deck] > 0 for deck in path):
            for deck in path:
                remaining_new[deck] -= 1
                remaining_review[deck] -= 1
            allotted_new += 1
            seen[guid].add(ord_)
    for name, (_cid, guid, ord_, _queue, _due) in reviews:
        if bury and seen[guid] - {ord_}:
            continue
        path = [deck for deck in _path(name) if deck in remaining_review]
        if all(remaining_review[deck] > 0 for deck in path):
            for deck in path:
                remaining_review[deck] -= 1
            allotted_review += 1
            seen[guid].add(ord_)
    return allotted_new, intraday + interday, allotted_review, interday


def apply(col) -> int:
    """Set Anki's options and today's limits so its counts match Kelma Review.

    Returns the number of decks whose "today only" limits changed.
    """
    if not col.get_config("newCardsIgnoreReviewLimit", False):
        col.set_config("newCardsIgnoreReviewLimit", True)
    if not col.get_config("applyAllParentLimits", False):
        col.set_config("applyAllParentLimits", True)
    for conf in col.decks.all_config():
        # Kelma Review buries new and review siblings by default.
        if not conf["new"].get("bury") or not conf["rev"].get("bury"):
            conf["new"]["bury"] = True
            conf["rev"]["bury"] = True
            col.decks.update_config(conf)
    snapshot = _Snapshot(col)
    changed = 0
    for name, did in snapshot.by_name.items():
        allotted_new, _learning, allotted_review, interday = _allot(snapshot, name)
        counter_new, counter_review = snapshot.counters[name]
        # Anki subtracts the deck's own counter from each limit, so the limit
        # is that counter plus the allotment. Interday learning cards use up
        # Anki's review limit (not Kelma Review's), so they get room as well.
        wanted: dict[str, Any] = {
            "newLimitToday": {"limit": counter_new + allotted_new, "today": snapshot.today},
            "reviewLimitToday": {
                "limit": counter_review + allotted_review + interday,
                "today": snapshot.today,
            },
        }
        deck = col.decks.get(did)
        if deck is None or all(deck.get(key) == value for key, value in wanted.items()):
            continue
        deck.update(wanted)
        col.decks.save(deck)
        changed += 1
    return changed
