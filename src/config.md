# Kelma cloud accounts — configuration

Use **Tools → Kelma → Accounts and collections…** instead of editing raw config.

Each Anki profile is one account with a separate collection and exactly one
provider:

- `ankiweb`: the profile uses native AnkiWeb sync only.
- `kelma`: the profile uses KelmaSync v2 only; native AnkiWeb auto-sync is
  blocked for that profile.

The provider registry is stored at `kelma-accounts.json` in Anki's base data
directory. It contains profile names and provider types only—never credentials.
KelmaSync URL, username, bearer token, client id, cursor, scheduler mode, and
delete approval are stored separately in `kelma-cloud-account.json` inside the
profile directory with mode `0600`.

An unconfigured profile does not sync. Deck data moves between accounts only
through **Transfer decks between accounts…**. Insert copies a missing deck,
Overwrite replaces a target from a preferred account, and Merge resolves two
designated deck namespaces into both collections. None contacts either cloud.

Legacy routing and shadow-sync keys may remain in global add-on config after an
upgrade, but normal account sync no longer uses them. Existing global Kelma v2
credentials are migrated into the profile that is open on first launch.
