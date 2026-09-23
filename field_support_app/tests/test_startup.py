import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from field_support_agent.startup import StartupPreference, autostart_enabled

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("startup_core", ROOT / "scripts/startup_core.py")
startup_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup_core)


class StartupPreferenceTests(unittest.TestCase):
    def test_setting_is_persisted_for_the_next_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            preference = Path(directory) / "autostart.mode"
            startup = StartupPreference(preference)
            self.assertFalse(autostart_enabled(preference))
            self.assertEqual({"available": True, "enabled": True}, startup.set_enabled(True))
            self.assertEqual("on", preference.read_text(encoding="utf-8").strip())
            self.assertEqual({"available": True, "enabled": False}, startup.set_enabled(False))
            self.assertEqual("off", preference.read_text(encoding="utf-8").strip())

    def test_boot_gate_latches_setting_for_this_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preference = StartupPreference(root / "autostart.mode")
            active = root / "active-this-boot"
            with patch.object(startup_core, "ACTIVE_THIS_BOOT", active), \
                 patch.object(sys, "argv", ["startup_core.py", "--preference", str(preference.path)]), \
                 patch.object(startup_core.subprocess, "run") as run:
                preference.set_enabled(True)
                self.assertEqual(0, startup_core.main())
                self.assertTrue(active.exists())
                run.assert_called_once_with(
                    ["/usr/bin/systemctl", "start", "field-support-core.service"], check=True
                )
                run.reset_mock()
                preference.set_enabled(False)
                self.assertTrue(active.exists())
                self.assertEqual(0, startup_core.main())
                self.assertFalse(active.exists())
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
