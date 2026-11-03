from __future__ import annotations

import json
import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma import accounts


class AccountRegistryTest(unittest.TestCase):
    def test_profiles_are_distinct_accounts_with_distinct_collections(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            accounts.set_provider(base, "Anki account", accounts.ANKIWEB)
            accounts.set_provider(base, "Kelma account", accounts.KELMA)

            records = accounts.accounts(base, ["Kelma account", "Anki account"])

            self.assertEqual(
                records,
                [
                    accounts.Account("Anki account", accounts.ANKIWEB),
                    accounts.Account("Kelma account", accounts.KELMA),
                ],
            )
            self.assertNotEqual(
                accounts.collection_path(base, "Anki account"),
                accounts.collection_path(base, "Kelma account"),
            )

    def test_registry_rejects_unknown_providers_and_ignores_malformed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "kelma-accounts.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accounts": {
                            "Anki": "ankiweb",
                            "Bad": "shared",
                            "": "kelma",
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(accounts.load_registry(base), {"Anki": "ankiweb"})
            with self.assertRaises(ValueError):
                accounts.set_provider(base, "Bad", "shared")

    def test_saving_replaces_registry_atomically_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            accounts.save_registry(base, {"User": accounts.KELMA})
            path = Path(base) / "kelma-accounts.json"
            payload = path.read_text(encoding="utf-8")

            self.assertEqual(accounts.provider_for(base, "User"), accounts.KELMA)
            self.assertNotIn("token", payload.casefold())
            self.assertNotIn("password", payload.casefold())
            self.assertEqual(list(Path(base).glob(".kelma-accounts-*")), [])


if __name__ == "__main__":
    unittest.main()
