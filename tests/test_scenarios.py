import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScenarioTests(unittest.TestCase):
    def run_scenario(self, scenario: str, duration: float = 0.25):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "app.py"),
                "--scenario",
                scenario,
                "--duration",
                str(duration),
                "--interval",
                "0.05",
                "--log-dir",
                str(root / "logs"),
                "--runtime-dir",
                str(root / "runtime"),
                "--run-id",
                "test-run",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
        status = json.loads((root / "runtime" / "status.json").read_text(encoding="utf-8"))
        log_text = (root / "logs" / "business.log").read_text(encoding="utf-8")
        return temp, proc, status, log_text

    def test_normal(self):
        temp, proc, status, log_text = self.run_scenario("normal")
        self.addCleanup(temp.cleanup)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(status["state"], "stopped")
        self.assertIn('"run_id": "test-run"', log_text)

    def test_failure_scenarios_have_stable_error_codes(self):
        expected = {
            "device_missing": (20, "E_DEVICE_NOT_FOUND"),
            "software_conflict": (23, "E_CONTROL_OWNER_CONFLICT"),
            "dependency_error": (21, "E_DEPENDENCY_MISSING"),
            "process_crash": (70, "E_PROCESS_CRASH"),
            "silent_hang": (24, "E_HEARTBEAT_STALE"),
        }
        for scenario, (return_code, error_code) in expected.items():
            with self.subTest(scenario=scenario):
                temp, proc, status, log_text = self.run_scenario(scenario)
                try:
                    self.assertEqual(proc.returncode, return_code)
                    self.assertIn(error_code, status["error_code"])
                    self.assertIn(error_code, log_text if scenario != "silent_hang" else json.dumps(status))
                finally:
                    temp.cleanup()

    def test_intermittent_fault_recovers_but_log_keeps_evidence(self):
        temp, proc, status, log_text = self.run_scenario("intermittent", duration=0.45)
        self.addCleanup(temp.cleanup)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(status["state"], "stopped")
        self.assertIn("E_DEVICE_TIMEOUT", log_text)
        self.assertIn("device communication recovered", log_text)


if __name__ == "__main__":
    unittest.main()
