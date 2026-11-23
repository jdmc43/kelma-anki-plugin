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


    def test_unassigned_profiles_are_ankiweb(self) -> None:
        records = {"Kelma": accounts.KELMA, "Old": accounts.ANKIWEB}
        self.assertEqual(accounts.effective_provider(records, "Kelma"), accounts.KELMA)
        self.assertEqual(accounts.effective_provider(records, "Old"), accounts.ANKIWEB)
        self.assertEqual(accounts.effective_provider(records, "User 1"), accounts.ANKIWEB)

    def test_kelma_profile_prefers_the_one_named_kelma(self) -> None:
        profiles = ["Arabic", "kelma", "User 1"]
        self.assertEqual(
            accounts.kelma_profile({"Arabic": accounts.KELMA, "kelma": accounts.KELMA}, profiles),
            "kelma",
        )
        self.assertEqual(accounts.kelma_profile({"Arabic": accounts.KELMA}, profiles), "Arabic")
        # A profile merely named Kelma is not the Kelma account until assigned.
        self.assertIsNone(accounts.kelma_profile({}, profiles))

    def test_ankiweb_profile_returns_to_the_last_one_used(self) -> None:
        records = {"Kelma": accounts.KELMA, "Spanish": accounts.ANKIWEB}
        profiles = ["Kelma", "Spanish", "User 1"]
        self.assertEqual(accounts.ankiweb_profile(records, profiles, "User 1"), "User 1")
        self.assertEqual(accounts.ankiweb_profile(records, profiles, "Kelma"), "Spanish")
        self.assertEqual(accounts.ankiweb_profile(records, profiles, None), "Spanish")
        self.assertIsNone(accounts.ankiweb_profile(records, ["Kelma"], None))

    def test_unused_profile_name_ignores_case(self) -> None:
        self.assertEqual(accounts.unused_profile_name(["User 1"], "Kelma"), "Kelma")
        self.assertEqual(accounts.unused_profile_name(["kelma"], "Kelma"), "Kelma 2")
        self.assertEqual(accounts.unused_profile_name(["Kelma", "KELMA 2"], "Kelma"), "Kelma 3")


if __name__ == "__main__":
    unittest.main()
