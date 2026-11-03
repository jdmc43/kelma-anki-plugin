"""Profile-backed AnkiWeb/KelmaSync cloud accounts.

Each Anki profile owns one separate collection and syncs to one provider.
Cross-account changes move only through the explicit Merge action.
"""

from aqt import gui_hooks

_initialized = False


def _on_profile_open(*_args) -> None:
    global _initialized
    if _initialized:
        return
    try:
        from aqt import mw

        if mw is None or mw.col is None:
            return
        from .kelma import gui

        gui.setup()
        _initialized = True
    except Exception as err:  # noqa: BLE001 - never break Anki startup
        from aqt.utils import showWarning

        showWarning(f"KelmaSync failed to initialize:\n{err}")


# profile_did_open is the normal path. main_window_did_init is a fallback for
# packaged Desktop startup ordering, where add-ons may be loaded immediately
# after the profile-open hook has already fired.
gui_hooks.profile_did_open.append(_on_profile_open)
gui_hooks.main_window_did_init.append(_on_profile_open)
