# Kelma cloud accounts (Anki add-on)

Adds profile-backed cloud accounts to Anki. Every account is assigned to exactly
one provider—**AnkiWeb** or **KelmaSync**—and owns a separate Anki collection.
Normal synchronization never copies changes between accounts.

## Source and license

This repository is distributed under the GNU Affero General Public License
v3.0; see [`LICENSE`](LICENSE).

## Account model

An Anki profile is the local boundary for an account:

```text
Anki profile / account A                 Anki profile / account B
┌────────────────────────┐               ┌────────────────────────┐
│ collection.anki2       │               │ collection.anki2       │
│ collection.media/      │               │ collection.media/      │
│ provider: AnkiWeb      │               │ provider: KelmaSync    │
└───────────┬────────────┘               └───────────┬────────────┘
            │ native sync                            │ KelmaSync v2
            ▼                                        ▼
         AnkiWeb                                  KelmaSync
```

- Collections, media, sync cursors, and credentials stay profile-scoped.
- AnkiWeb authentication remains owned by the Anki profile.
- Kelma bearer tokens are stored in a mode-`0600` file inside that profile.
- The shared account registry stores only profile names and provider types; it
  never stores passwords, tokens, or collection content.
- Profiles are AnkiWeb accounts unless assigned to KelmaSync, so existing
  profiles keep syncing with AnkiWeb exactly as before.
- A Kelma account blocks native AnkiWeb auto-sync. An AnkiWeb account never runs
  KelmaSync during normal sync.

## Getting started

1. Install the add-on and restart Anki. It adds a **Kelma** profile for your
   Kelma account (once; an existing profile assigned to KelmaSync is reused).
2. Click **Sync**. The popup has an **AnkiWeb | Kelma** toggle: choose
   **Kelma** to switch to the Kelma profile. If you are not signed in, the
   add-on asks you to sign in with your Kelma account (or create one at
   kelma.tech), then syncs your Kelma decks. Kelma decks show a green ✓, or
   `+added` / `~changed` until the next sync.
3. Choose **AnkiWeb** in the same toggle to return to your last AnkiWeb profile.
   **Tools → Kelma → Switch to … account** does the same.

Open **Tools → Kelma → Accounts and collections…** to assign profiles, switch
accounts, or open Anki's profile manager to add/remove profiles.

## Explicit deck transfer

Accounts remain separate by default. Choose **Tools → Kelma → Transfer decks
between accounts…**, select one deck in each account, then choose:

- **Insert** — copy a deck that does not exist in the destination. Its name,
  notes, cards, review history, scheduling, deck configuration, and media are
  retained. The source remains unchanged.
- **Overwrite** — replace the selected destination deck with the preferred
  account's version. Destination cards are removed first; a backup is created.
- **Merge** — designate the two selected deck namespaces as the same deck.
  One-sided notes/cards are unioned into both accounts. Differing notes enter a
  conflict table where the version retained in both accounts is selected;
  newest versions are suggested, while ties require an explicit choice.
  Scheduling converges by GUID/template identity and newest card modification.

Merge creates backups of both collections. Operations include subdecks and
media but never contact either cloud automatically. Sync each affected account
explicitly afterward when ready. Automatic deletion propagation remains off.

## Provider behavior

### AnkiWeb account

The Sync button runs Anki's native collection/media sync. KelmaSync credentials
or collections are never consulted.

### KelmaSync account

The Sync button runs KelmaSync v2 against the entire profile collection. It does
not run AnkiWeb before or afterward. KelmaSync's independent card study state
maps to Anki suspension (`queue = -1`); buried queues remain local. Schedule
reset maps to Anki's native Schedule as New operation while preserving revlog
history. Manual due-date overrides map to native Set Due Date; manual revlog and
post-sync baselines distinguish explicit changes from normal scheduling, and a
later rated review or reset clears the override.

Notes, cards, immutable review history, notetypes, decks, media, tombstones,
study state, reset cutoffs, and manual due dates use the existing KelmaSync v2
newest-wins pipeline. Passwords are never stored.

**Same due dates as Kelma Review.** KelmaSync schedules every card with FSRS-6
from its review history. The add-on places those schedules on the account's
study days (time zone and day-start hour from the study-day policy), exactly as
Kelma Review does: a review card is due at the start of the study day "study day
of the last review + interval", and learning steps that cross into a later study
day become due that day. Anki's "next day starts at" follows the account, and
Anki's deck presets get the account's FSRS parameters, desired retention,
learning steps and maximum interval, so reviews done in Anki schedule the same
way. After uploading reviews, a sync waits for the server to schedule them and
pulls the result in the same pass. Cards placed by older versions on UTC days
are moved once (and again if the study-day policy changes).

**Fast repeat syncs.** The last full server manifest is kept next to the
collection (`kelma_server_manifest.json.gz`); later syncs ask only for what
changed since then (a full refresh happens at least daily). HTTP connections are
reused, and deck/notetype records that another client serialised differently are
compared in normalised form instead of being pulled again on every sync.

Large media restores use the same prepared-TAR protocol as Kelma Kotlin: 128 MiB
/ 10,000-file batches, up to five concurrent preparations in first-ready order,
and exactly one streaming TAR GET at a time. Interrupted one-shot jobs are
replaced with only unfinished entries. Ordinary updates use exactly 64 bounded
authenticated proxy workers. Files are size-checked, staged, and atomically
published without buffering an archive or collection in memory. **Delete and
redownload media…** removes files already backed by the current KelmaSync
account, retains local-only files, and immediately runs the complete resumable pull.

## Migration from older dual-sync versions

Older releases (1.0.131 and earlier) used one master collection, kept a hidden
copy of it per service, and reconciled it with both AnkiWeb and KelmaSync. That
machinery is gone: there are no hidden copies, no AnkiWeb comparison, and no
per-deck routing. This release does not attempt to split an already mixed
collection automatically, because doing so would require guessing ownership.

On first launch:

1. Existing Kelma credentials are moved from global add-on config into the
   currently open profile.
2. The add-on adds a **Kelma** profile; switch to it from the Sync button and
   sign in to pull your Kelma decks into it.
3. Profiles not assigned to KelmaSync are AnkiWeb accounts and resume Anki's
   native sync. Reassign them in **Accounts and collections…** if needed.
4. Use explicit Merge only for content you intentionally want to copy.

The hidden copies the old releases kept next to each collection are no longer
used. When a profile still has them, the add-on offers once to delete them;
answering No keeps them and it does not ask again. They can also be deleted by
hand from the profile folder: `kelma_shadow_kelmasync.anki2` and
`kelma_shadow_ankiweb.anki2`, each with its `.media` folder and `.media.db2`
file. The profile's own collection and media are never touched.

## Install (development)

```bash
cd kelma-anki-plugin
./build.sh dev
# Fully quit Anki (⌘Q on macOS), then relaunch.
```

Package for distribution:

```bash
./build.sh
# creates dist/kelma.ankiaddon
```

The server-backed end-to-end test lives here—not in the server repository—and
uses the sibling `../kelma_sync_2` checkout by default:

```bash
ANKI_PY="$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python"
PYTHONPATH=src "$ANKI_PY" tests/e2e_sync_server.py
```

Set `KELMA_SYNC_SERVER_REPO` to test against a server checkout elsewhere.

## Requirements

- Modern Anki with the Rust backend (approximately 23.10 or newer).
- One Anki profile per cloud account.
- KelmaSync v2 access for Kelma accounts.
