import json
import stat
import tempfile
import unittest
from pathlib import Path

from field_support_agent.config import AppConfig, BusinessRepository
from field_support_agent.settings import SettingsError, SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def _store(self, root: Path) -> SettingsStore:
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        config = AppConfig((BusinessRepository("business", "git@example/repo.git", repo),))
        return SettingsStore(root / "state" / "settings.json", config)

    def test_secret_is_private_and_never_returned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            value = store.runtime()
            value["feishu"]["app_id"] = "cli_test"
            value["feishu"]["app_secret"] = "super-secret"
            public = store.update(value)

            self.assertNotIn("app_secret", public["feishu"])
            self.assertTrue(public["feishu"]["app_secret_configured"])
            self.assertEqual(0o600, stat.S_IMODE(store.path.stat().st_mode))
            self.assertEqual("super-secret", json.loads(store.path.read_text())["feishu"]["app_secret"])

    def test_codex_model_and_effort_can_follow_local_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            self.assertEqual("", store.runtime()["codex"]["model"])
            self.assertEqual("", store.runtime()["codex"]["reasoning_effort"])
            saved = store.update(store.runtime())
            self.assertEqual("", saved["codex"]["model"])
            self.assertEqual("", saved["codex"]["reasoning_effort"])

    def test_log_paths_and_device_name_are_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            value = store.runtime()
            value["business"]["log_paths"] = ["/tmp/singel_workstation_runtime/A2机器人宝维塔NCU上下料"]
            value["feishu"]["device_name"] = "A2机器人宝维塔NCU上下料"
            store.update(value)
            runtime = store.runtime()
            self.assertEqual(
                [str(Path(item).resolve()) for item in value["business"]["log_paths"]],
                runtime["business"]["log_paths"],
            )
            self.assertEqual("A2机器人宝维塔NCU上下料", runtime["feishu"]["device_name"])

    def test_blank_secret_keeps_existing_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            value = store.runtime()
            value["feishu"].update({"app_id": "cli_test", "app_secret": "secret"})
            public = store.update(value)
            public["feishu"].pop("app_secret_configured")
            store.update(public)
            self.assertEqual("secret", store.runtime()["feishu"]["app_secret"])

    def test_invalid_business_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            value = store.runtime()
            value["business"]["repositories"][0]["local_path"] = str(root / "missing")
            with self.assertRaises(SettingsError):
                store.update(value)

    def test_lark_cli_profile_does_not_require_app_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            value = store.runtime()
            value["feishu"].update(
                {
                    "connection_mode": "lark_cli_profile",
                    "lark_cli_binary": "/opt/homebrew/bin/lark-cli",
                    "lark_profile": "inventory-bot",
                    "support_chat_id": "oc_support",
                }
            )
            public = store.update(value)

            self.assertEqual("lark_cli_profile", public["feishu"]["connection_mode"])
            self.assertEqual("inventory-bot", public["feishu"]["lark_profile"])
            self.assertFalse(public["feishu"]["app_secret_configured"])

    def test_management_password_is_stored_locally_and_hidden_from_public_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            value = store.runtime()
            value["management"]["password"] = "1234"
            public = store.update(value)

            self.assertTrue(public["management"]["password_configured"])
            self.assertNotIn("password", public["management"])
            self.assertEqual("1234", store.management_password())
            self.assertEqual("1234", json.loads(store.path.read_text())["management"]["password"])


if __name__ == "__main__":
    unittest.main()
