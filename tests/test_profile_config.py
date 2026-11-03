from __future__ import annotations

import importlib
import sys
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


class FakeAddonManager:
    def __init__(self) -> None:
        self.config = {
            "v2_token": "legacy-a-token",
            "v2_username": "a@example.com",
        }

    def getConfig(self, _addon):
        return dict(self.config)

    def writeConfig(self, _addon, values):
        self.config = dict(values)


class ProfileConfigIsolationTest(unittest.TestCase):
    def test_kelma_credentials_are_isolated_by_profile(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            manager = FakeAddonManager()
            pm = SimpleNamespace(
                name="Account A",
                profileFolder=lambda: str(Path(base) / pm.name),
            )
            aqt = ModuleType("aqt")
            aqt.mw = SimpleNamespace(addonManager=manager, pm=pm)
            previous = sys.modules.get("aqt")
            sys.modules["aqt"] = aqt
            try:
                sys.modules.pop("kelma.config", None)
                config = importlib.import_module("kelma.config")
                config.migrate_account_config()
                self.assertEqual(config.get()["v2_token"], "legacy-a-token")
                self.assertEqual(manager.config["v2_token"], "")

                pm.name = "Account B"
                self.assertEqual(config.get()["v2_token"], "")
                second = config.get()
                second["v2_token"] = "b-token"
                second["v2_username"] = "b@example.com"
                config.save(second)

                pm.name = "Account A"
                self.assertEqual(config.get()["v2_token"], "legacy-a-token")
                self.assertEqual(config.get()["v2_username"], "a@example.com")
                pm.name = "Account B"
                self.assertEqual(config.get()["v2_token"], "b-token")
            finally:
                if previous is None:
                    sys.modules.pop("aqt", None)
                else:
                    sys.modules["aqt"] = previous


if __name__ == "__main__":
    unittest.main()
