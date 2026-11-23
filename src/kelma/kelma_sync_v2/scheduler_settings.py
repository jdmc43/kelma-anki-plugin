"""Make Anki's own scheduler use the account's FSRS profile.

KelmaSync schedules every card with FSRS-6 from its review history, and Kelma
Review shows those dates. Reviews done in Anki get the server's schedule at the
next sync; until then Anki computes the next interval itself. Giving Anki the
same FSRS parameters, desired retention, learning steps and maximum interval
keeps its answer buttons and due dates in line with Kelma Review meanwhile.
Applied to every deck preset of the (KelmaSync-only) profile, and again only
when the account profile changes.
"""
from __future__ import annotations

import json
from typing import Any

CONFIG_KEY = "kelmaSchedulerProfile"
_FSRS6_PARAMETER_COUNT = 21


def apply_profile(col, response: dict[str, Any]) -> bool:
    """Apply the account FSRS profile to Anki; True when something changed."""
    profile = (response or {}).get("profile") or {}
    parameters = profile.get("parameters")
    if not isinstance(parameters, list) or len(parameters) != _FSRS6_PARAMETER_COUNT:
        return False
    key = str(profile.get("config_hash") or json.dumps(profile, sort_keys=True))
    if col.get_config(CONFIG_KEY, "") == key and col.get_config("fsrs", False):
        return False
    learning = [float(seconds) / 60 for seconds in profile.get("learning_steps_seconds") or [60, 600]]
    relearning = [float(seconds) / 60 for seconds in profile.get("relearning_steps_seconds") or [600]]
    for conf in col.decks.all_config():
        conf["fsrsParams6"] = [float(value) for value in parameters]
        conf["desiredRetention"] = float(profile.get("desired_retention", 0.9) or 0.9)
        conf["new"]["delays"] = learning or [1.0, 10.0]
        conf["lapse"]["delays"] = relearning or [10.0]
        conf["rev"]["maxIvl"] = int(profile.get("maximum_interval", 36500) or 36500)
        col.decks.update_config(conf)
    col.set_config("fsrs", True)
    col.set_config(CONFIG_KEY, key)
    return True
