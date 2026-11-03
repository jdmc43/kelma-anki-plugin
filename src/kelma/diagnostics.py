"""Sync diagnostics: compare the master collection against each service shadow.

After a sync, a service's shadow is the local mirror of that service's server,
so comparing the master against the shadow — by card/note id, which the package
importer preserves — shows exactly where the desktop and the server disagree:

* cards present in the master but not the shadow  -> on the desktop, not the server
* cards present in the shadow but not the master  -> on the server, not the desktop
* cards in both but with a different mod/usn       -> the same card, diverged

Plus each collection's `col` row (mod / scm / ls / usn) and grave count, which
tells us the sync state itself. Everything is rendered as plain text the user
can copy and paste back for analysis. The dialog is read-only — it opens each
shadow briefly and closes it; it never writes.
"""

from __future__ import annotations

import time
from concurrent.futures import Future

from anki.collection import Collection
from aqt import mw
from aqt.qt import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)
from aqt.utils import tooltip

from . import config, consts, paths

# Cap per-deck id lists so a badly-diverged deck can't produce a megabyte of text.
_MAX_IDS = 40


def _deck_dids(col: Collection, name: str) -> list[int]:
    """Deck ids for `name` and its subdecks, resolved within `col` (shadow deck
    ids differ from the master's, so this must be per-collection)."""
    prefix = name + "::"
    return [
        d.id
        for d in col.decks.all_names_and_ids()
        if d.name == name or d.name.startswith(prefix)
    ]


def _col_meta(col: Collection) -> dict:
    row = col.db.first("select crt, mod, scm, ls, usn from col") or (0, 0, 0, 0, 0)
    crt, mod, scm, ls, usn = row
    return {
        "crt": crt,
        "mod": mod,
        "scm": scm,
        "ls": ls,
        "usn": usn,
        "graves": col.db.scalar("select count(*) from graves") or 0,
        "cards": col.db.scalar("select count(*) from cards") or 0,
        "notes": col.db.scalar("select count(*) from notes") or 0,
        "pending_cards": col.db.scalar("select count(*) from cards where usn=-1") or 0,
        "pending_notes": col.db.scalar("select count(*) from notes where usn=-1") or 0,
    }


def _deck_card_state(col: Collection, name: str) -> dict[int, tuple[int, int]]:
    """{card_id: (mod, usn)} for a deck and its subdecks, within `col`."""
    dids = _deck_dids(col, name)
    if not dids:
        return {}
    ph = ",".join("?" * len(dids))
    rows = col.db.all(
        f"select id, mod, usn from cards where did in ({ph})", *dids
    )
    return {cid: (mod, usn) for cid, mod, usn in rows}


def _deck_notetypes(col: Collection, name: str) -> list[tuple]:
    """Notetypes used by the deck's notes: (id, name, mtime_secs, usn, n_fields).

    If the same notetype name appears with different ids on master vs shadow, the
    importer has been duplicating it (schema mismatch -> remap), which forces new
    note/card ids on every reconcile — the churn cause."""
    dids = _deck_dids(col, name)
    if not dids:
        return []
    ph = ",".join("?" * len(dids))
    rows = col.db.all(
        f"select nt.id, nt.name, nt.mtime_secs, nt.usn, "
        f"(select count(*) from fields f where f.ntid = nt.id) "
        f"from notetypes nt where nt.id in ("
        f"  select distinct n.mid from notes n where n.id in ("
        f"    select nid from cards where did in ({ph})))"
        f" order by nt.name, nt.id",
        *dids,
    )
    return [tuple(r) for r in rows]


def _deck_sched(col: Collection, name: str) -> dict[tuple[str, int], tuple]:
    """{(note_guid, card_ord): (ivl, reps, type, queue, mod)} for the deck.

    Keyed by the cross-collection-stable identity. ivl/reps/type/queue are
    collection-independent (unlike `due`, which is crt-relative), so comparing
    them across master and shadow shows whether scheduling actually agrees."""
    dids = _deck_dids(col, name)
    if not dids:
        return {}
    ph = ",".join("?" * len(dids))
    out: dict[tuple[str, int], tuple] = {}
    for guid, ord_, ivl, reps, ctype, queue, mod in col.db.all(
        f"select n.guid, c.ord, c.ivl, c.reps, c.type, c.queue, c.mod "
        f"from cards c join notes n on c.nid = n.id where c.did in ({ph})",
        *dids,
    ):
        out[(guid, ord_)] = (ivl, reps, ctype, queue, mod)
    return out


def _sched_mismatch_line(m: dict, s: dict) -> str:
    """Count matched cards whose scheduling disagrees (ignoring crt-relative due)."""
    keys = set(m) & set(s)
    diff = [k for k in keys if m[k][:4] != s[k][:4]]  # ivl, reps, type, queue
    if not diff:
        return "    scheduling: in sync for all matched cards\n"
    sample = diff[:5]
    detail = "; ".join(
        f"{k[0][:8]}/ord{k[1]}: m(ivl={m[k][0]},reps={m[k][1]}) "
        f"s(ivl={s[k][0]},reps={s[k][1]})"
        for k in sample
    )
    more = "" if len(diff) <= 5 else f" … (+{len(diff) - 5})"
    return f"    scheduling OUT OF SYNC: {len(diff)} matched cards — {detail}{more}\n"


def _deck_note_guids(col: Collection, name: str) -> set[str]:
    """Distinct note GUIDs that have a card in `name` (or a subdeck), within `col`.

    GUIDs are content-stable across collections (the importer preserves them),
    so comparing GUID sets distinguishes pure id-churn (same GUIDs, different
    card ids — a non-idempotent reconcile) from duplicate-origin content
    (different GUIDs — the deck was created independently on two devices)."""
    dids = _deck_dids(col, name)
    if not dids:
        return set()
    ph = ",".join("?" * len(dids))
    rows = col.db.list(
        f"select distinct guid from notes where id in "
        f"(select nid from cards where did in ({ph}))",
        *dids,
    )
    return set(rows)


def _fmt_ts(ms_or_s: int) -> str:
    """col.mod/scm/ls are ms; usn is not a time. Best-effort human time for the
    big ones; raw value always shown alongside by the caller."""
    if not ms_or_s:
        return "0"
    # ls/mod/scm are stored in ms in modern Anki.
    secs = ms_or_s / 1000 if ms_or_s > 10_000_000_000 else ms_or_s
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(secs))
    except (ValueError, OSError):
        return str(ms_or_s)


def _ids(values) -> str:
    vals = sorted(values)
    shown = ", ".join(str(v) for v in vals[:_MAX_IDS])
    if len(vals) > _MAX_IDS:
        shown += f", … (+{len(vals) - _MAX_IDS} more)"
    return shown or "-"


def _fmt_notetypes(ntm: list[tuple], nts: list[tuple]) -> str:
    """Side-by-side notetypes used by the deck. A name present on both sides with
    different ids = a duplicated notetype (the remap that churns ids)."""
    def fmt(rows):
        if not rows:
            return "      (none)"
        return "\n".join(
            f"      id={r[0]} name={r[1]!r} mtime={r[2]} usn={r[3]} fields={r[4]}"
            for r in rows
        )

    names_m = {r[1] for r in ntm}
    names_s = {r[1] for r in nts}
    ids_by_name_m = {r[1]: r[0] for r in ntm}
    ids_by_name_s = {r[1]: r[0] for r in nts}
    mismatched = [
        n for n in names_m & names_s if ids_by_name_m[n] != ids_by_name_s[n]
    ]
    flag = ""
    if mismatched:
        flag = (
            "\n    ⚠ notetype id MISMATCH for same name(s) "
            f"{mismatched} — importer is duplicating/remapping (churn cause)"
        )
    return (
        "    notetypes (master):\n"
        + fmt(ntm)
        + "\n    notetypes (shadow):\n"
        + fmt(nts)
        + flag
        + "\n"
    )


def _meta_block(label: str, meta: dict, note: str = "") -> str:
    suffix = f"  ({note})" if note else ""
    return (
        f"-- {label} --{suffix}\n"
        f"  cards={meta['cards']:,}  notes={meta['notes']:,}  "
        f"graves={meta['graves']:,}\n"
        f"  pending(usn=-1): cards={meta['pending_cards']:,} "
        f"notes={meta['pending_notes']:,}\n"
        f"  crt={meta['crt']} ({_fmt_ts(meta['crt'])})\n"
        f"  mod={meta['mod']} ({_fmt_ts(meta['mod'])})  "
        f"scm={meta['scm']} ({_fmt_ts(meta['scm'])})\n"
        f"  ls={meta['ls']} ({_fmt_ts(meta['ls'])})  usn={meta['usn']}\n"
    )


def _compare_deck(
    name: str, m: dict, s: dict, gm: set[str], gs: set[str]
) -> str:
    only_m = set(m) - set(s)
    only_s = set(s) - set(m)
    both = set(m) & set(s)
    mod_diff = [cid for cid in both if m[cid][0] != s[cid][0]]
    mod_diff_set = set(mod_diff)
    usn_diff = [
        cid for cid in both if m[cid][1] != s[cid][1] and cid not in mod_diff_set
    ]

    # Content is judged by GUIDs (stable across collections); card ids are not —
    # the importer assigns each collection its own, so matched-by-GUID notes still
    # land on different card ids and show up in only_m/only_s. So a large only_m/
    # only_s alongside a large shared-GUID set means id churn, NOT differing
    # content: report the two signals separately instead of conflating them.
    guid_both = gm & gs
    guid_only_m = gm - gs
    guid_only_s = gs - gm

    verdicts: list[str] = []
    if guid_only_m or guid_only_s:
        if guid_only_m and guid_only_s:
            tail = "duplicate content created independently on two devices (de-dupe)"
        else:
            tail = "a note is present on one side and missing from the other"
        verdicts.append(
            f"    NOTES: {len(guid_both)} shared by GUID, "
            f"{len(guid_only_m)} only on master, {len(guid_only_s)} only on server "
            f"— {tail}."
        )
    if (only_m or only_s) and guid_both:
        verdicts.append(
            f"    CARD IDS: {len(only_m)} on master / {len(only_s)} on server don't "
            f"match across collections though {len(guid_both)} GUIDs are shared — "
            "reconcile is regenerating card ids (churn; inflates graves)."
        )

    if not only_m and not only_s and not mod_diff and not usn_diff:
        return f'  deck "{name}": in sync ({len(m)} cards)\n'

    lines = [
        f'  deck "{name}": master {len(m)} cards, shadow {len(s)} cards; '
        f"note GUIDs master={len(gm)} shadow={len(gs)} shared={len(guid_both)}"
    ]
    lines.extend(verdicts)
    if only_m:
        lines.append(f"    only in master ({len(only_m)}): {_ids(only_m)}")
    if only_s:
        lines.append(f"    only in shadow/server ({len(only_s)}): {_ids(only_s)}")
    if mod_diff:
        sample = sorted(mod_diff)[:5]
        detail = "; ".join(
            f"{cid}: m.mod={m[cid][0]} s.mod={s[cid][0]}" for cid in sample
        )
        more = "" if len(mod_diff) <= 5 else f" … (+{len(mod_diff) - 5})"
        lines.append(f"    mod differs ({len(mod_diff)}): {detail}{more}")
    if usn_diff:
        lines.append(f"    usn differs only ({len(usn_diff)}): {_ids(usn_diff)}")
    return "\n".join(lines) + "\n"


def collect_report(deck_filter: str | None) -> str:
    """Build the full text report. `deck_filter` is a single deck name, or None
    for every routed deck. Runs off the UI thread."""
    out: list[str] = []
    out.append("=== Kelma sync diagnostics ===")
    out.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append("")

    master = mw.col
    out.append(_meta_block("master collection", _col_meta(master)))

    master_names = [d.name for d in master.decks.all_names_and_ids()]

    for service in consts.SERVICES:
        label = consts.SERVICE_LABEL[service]
        if not paths.shadow_exists(service):
            out.append(f"-- {label} shadow --\n  (no shadow yet — not synced)\n")
            continue

        try:
            shadow = Collection(paths.shadow_path(service))
        except Exception as err:  # noqa: BLE001
            out.append(f"-- {label} shadow --\n  (could not open: {err})\n")
            continue
        try:
            out.append(
                _meta_block(
                    f"{label} shadow",
                    _col_meta(shadow),
                    note="mirrors the server as of the last sync",
                )
            )

            routed = config.decks_for_service(service, master_names)
            if deck_filter is not None:
                routed = [d for d in routed if d == deck_filter]
            if not routed:
                which = (
                    f'"{deck_filter}" is not routed to {label}'
                    if deck_filter
                    else f"no decks routed to {label}"
                )
                out.append(f"  ({which})\n")
                continue

            out.append(f"PER-DECK — master vs {label} shadow:")
            for name in sorted(routed, key=str.lower):
                m = _deck_card_state(master, name)
                s = _deck_card_state(shadow, name)
                gm = _deck_note_guids(master, name)
                gs = _deck_note_guids(shadow, name)
                out.append(_compare_deck(name, m, s, gm, gs))
                out.append(
                    _sched_mismatch_line(
                        _deck_sched(master, name), _deck_sched(shadow, name)
                    )
                )
                # Notetype detail only for diverged decks (where the cause matters).
                if set(m) ^ set(s):
                    out.append(
                        _fmt_notetypes(
                            _deck_notetypes(master, name),
                            _deck_notetypes(shadow, name),
                        )
                    )
        finally:
            try:
                shadow.close()
            except Exception:  # noqa: BLE001
                pass

    return "\n".join(out)


class DiagnosticsDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Kelma — Sync diagnostics")
        self.resize(820, 640)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Compares your collection against each service's local shadow "
                "(which mirrors that server as of the last sync). Run it right "
                "after a sync, then <b>Copy</b> the report."
            )
        )

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Deck:"))
        self.deck_combo = QComboBox()
        self.deck_combo.addItem("All routed decks", None)
        names = [d.name for d in mw.col.decks.all_names_and_ids()]
        routed = sorted(
            {
                n
                for s in consts.SERVICES
                for n in config.decks_for_service(s, names)
            },
            key=str.lower,
        )
        for name in routed:
            self.deck_combo.addItem(name, name)
        bar.addWidget(self.deck_combo, 1)
        run = QPushButton("Run")
        run.clicked.connect(self._run)
        bar.addWidget(run)
        layout.addLayout(bar)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.output.font()
        font.setFamily("Menlo, Consolas, monospace")
        self.output.setFont(font)
        self.output.setPlainText("Press Run to generate the report.")
        layout.addWidget(self.output, 1)

        actions = QHBoxLayout()
        copy = QPushButton("Copy to clipboard")
        copy.clicked.connect(self._copy)
        actions.addWidget(copy)
        actions.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

    def _run(self) -> None:
        deck = self.deck_combo.currentData()
        self.output.setPlainText("Collecting…")

        def task() -> str:
            return collect_report(deck)

        def done(fut: "Future[str]") -> None:
            try:
                self.output.setPlainText(fut.result())
            except Exception as err:  # noqa: BLE001
                self.output.setPlainText(f"Failed to collect diagnostics:\n{err}")

        mw.taskman.with_progress(
            task, done, parent=self, label="Collecting sync diagnostics…"
        )

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.output.toPlainText())
        tooltip("Diagnostics copied to clipboard.", parent=self)
