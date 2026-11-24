# Kelma cloud accounts — configuration

Use **Tools → Kelma → Accounts and collections…** instead of editing raw config.

Each Anki profile is one account with a separate collection and exactly one
provider:

- `ankiweb` (the default for every profile not assigned to KelmaSync): the
  profile uses native AnkiWeb sync only.
- `kelma`: the profile uses KelmaSync v2 only; native AnkiWeb auto-sync is
  blocked for that profile. The add-on creates a **Kelma** profile for this on
  first launch, and the Sync button's AnkiWeb | Kelma toggle switches to it.

The provider registry is stored at `kelma-accounts.json` in Anki's base data
directory. It contains profile names and provider types only—never credentials.
KelmaSync server URL, username, bearer token, client id, cursor (and the server
it came from), scheduler mode, and delete approval are stored separately in
`kelma-cloud-account.json` inside the profile directory with mode `0600`, so each
profile can use a different KelmaSync server. Change it with **Sync server…**,
which signs the profile out and resets what it knew about the old server.

Deck data moves between accounts only
through **Transfer decks between accounts…**. Insert copies a missing deck,
Overwrite replaces a target from a preferred account, and Merge resolves two
designated deck namespaces into both collections. None contacts either cloud.

Global settings:

- `enabled`: when `false`, the Sync button falls back to Anki's own sync.
- `wrap_sync_button`: when `true`, the Sync button opens the account sync menu.
- `block_native_sync`: when `true`, native sync (including auto-sync on open and
  close) never runs for KelmaSync profiles.
- `kelma_profile_created`, `last_ankiweb_profile`: bookkeeping for the Kelma
  profile and the AnkiWeb profile the toggle returns to.
- `check_for_updates`: check kelma.tech for add-on updates.
- `v2_url`: KelmaSync server for profiles that have not chosen one.
- `features`: display modifications toggled under **Display modifications**.

Existing global Kelma v2 credentials are migrated into the profile that is open
on first launch. Settings left over from the retired dual-sync releases (hidden
collection copies, per-deck routing, v1 host keys) are removed the next time
the add-on saves its configuration.
