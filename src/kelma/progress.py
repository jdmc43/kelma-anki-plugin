"""Sync progress state + rendering.

The background sync task only *sets* plain attributes on a Reporter (the current
step label, the bar position). The engine paints the dialog from a main-thread
QTimer calling `Reporter.render()`. Because painting is decoupled from the
background thread, the dialog always shows the step currently in progress — even
if that step is slow or blocks — so a stall names exactly what it's stuck on
(never a generic "starting").

During a native server sync the Reporter can "watch" the collection being synced
and surface Anki's live backend progress (transfer %, media counts).
"""

from __future__ import annotations

from aqt import mw


def _format_sync(progress, prefix: str) -> str | None:
    kind = progress.WhichOneof("value")
    if kind == "full_sync":
        fs = progress.full_sync
        if fs.total:
            pct = int(fs.transferred * 100 / fs.total)
            return f"{prefix}: transferring {pct}% ({fs.transferred}/{fs.total})"
        return f"{prefix}: transferring…"
    if kind == "media_sync":
        m = progress.media_sync
        bits = []
        if m.checked:
            bits.append(f"checked {m.checked}")
        if m.added:
            bits.append(f"added {m.added}")
        if m.removed:
            bits.append(f"removed {m.removed}")
        return f"{prefix}: media — " + (", ".join(bits) if bits else "syncing…")
    if kind == "normal_sync":
        return f"{prefix}: {progress.normal_sync.stage or 'syncing…'}"
    return None


class Reporter:
    def __init__(self) -> None:
        self.step = ""
        self.value = 0
        self.total = 0
        self._live_col = None
        self._live_prefix = ""

    # -- written by the background task (plain attribute writes) --------------
    def set_total(self, total: int) -> None:
        self.total = max(int(total), 0)
        self.value = 0

    def advance(self, label: str) -> None:
        if self.total:
            self.value = min(self.value + 1, self.total)
        self.step = label

    def tick(self) -> None:
        """Advance the bar one step without changing the label (for skipped work)."""
        if self.total:
            self.value = min(self.value + 1, self.total)

    def message(self, label: str) -> None:
        self.step = label

    def watch(self, col, prefix: str) -> None:
        """While set, render() prefers the collection's live backend progress."""
        self._live_prefix = prefix
        self._live_col = col

    def unwatch(self) -> None:
        self._live_col = None

    # -- read on the main thread ----------------------------------------------
    def current_label(self) -> str:
        col = self._live_col
        if col is not None:
            try:
                msg = _format_sync(col.latest_progress(), self._live_prefix)
                if msg:
                    return msg
            except Exception:  # noqa: BLE001 - backend may be mid-reopen
                pass
        return self.step or "Kelma sync…"

    def render(self) -> None:
        """Paint the progress dialog. Call only on the main thread (QTimer)."""
        try:
            mw.progress.update(
                label=self.current_label(), value=self.value, max=self.total
            )
        except Exception:  # noqa: BLE001 - dialog may be gone
            pass
