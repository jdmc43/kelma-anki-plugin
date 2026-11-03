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
- An unconfigured profile cannot sync until it is explicitly assigned.
- A Kelma account blocks native AnkiWeb auto-sync. An AnkiWeb account never runs
  KelmaSync during normal sync.

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

Large media restores use the same prepared-TAR protocol as Kelma Kotlin: 128 MiB
/ 10,000-file batches, up to five concurrent preparations in first-ready order,
and exactly one streaming TAR GET at a time. Interrupted one-shot jobs are
replaced with only unfinished entries. Ordinary updates use exactly 64 bounded
authenticated proxy workers. Files are size-checked, staged, and atomically
published without buffering an archive or collection in memory. **Delete and
redownload media…** removes files already backed by the current KelmaSync
account, retains local-only files, and immediately runs the complete resumable pull.

## Migration from older dual-sync versions

Older releases used one master collection and automatically reconciled it with
both services. This release does not attempt to split an already mixed
collection automatically, because doing so would require guessing ownership.

On first launch:

1. Existing Kelma credentials are moved from global add-on config into the
   currently open profile.
2. Open **Accounts and collections…** and assign that profile to the provider
   whose collection it should represent.
3. Create another Anki profile for the other provider if needed.
4. Use explicit Merge only for content you intentionally want to copy.

No cloud sync runs from an unconfigured profile.

## Install (development)

```bash
cd /Users/voss/projects/kelma-anki-plugin
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
