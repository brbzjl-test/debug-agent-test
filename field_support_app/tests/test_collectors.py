import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from field_support_agent.collectors.base import redact
from field_support_agent.collectors.bundle import build_evidence_bundle
from field_support_agent.collectors.business_status import BusinessStatusCollector, ProcessInfo
from field_support_agent.collectors.snapshot import SnapshotCollector


class SnapshotCollectorTest(unittest.TestCase):
    def test_business_status_matches_process_command_or_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            running = root / "running"
            stopped = root / "stopped"
            running.mkdir()
            stopped.mkdir()
            collector = BusinessStatusCollector(
                [
                    {"name": "running", "local_path": str(running)},
                    {"name": "stopped", "local_path": str(stopped)},
                ],
                process_provider=lambda: [
                    ProcessInfo(41, "zsh", str(running)),
                    ProcessInfo(42, "python app.py", str(running), 3661),
                ],
            )

            report = collector.collect().to_dict()

            self.assertTrue(report["available"])
            self.assertEqual({"total": 2, "running": 1}, report["summary"])
            self.assertEqual("running", report["programs"][0]["state"])
            self.assertEqual([42], report["programs"][0]["pids"])
            self.assertEqual([{"pid": 42, "elapsed_seconds": 3661}], report["programs"][0]["processes"])
            self.assertEqual("not_running", report["programs"][1]["state"])

    def test_business_status_decodes_macos_lsof_paths(self):
        encoded = "/Users/brb/\\xe9\\xa1\\xb9\\xe7\\x9b\\xae/business"
        self.assertEqual("/Users/brb/项目/business", BusinessStatusCollector._decode_lsof_path(encoded))

    def test_business_status_parses_process_elapsed_time(self):
        self.assertEqual(45, BusinessStatusCollector._parse_elapsed("00:45"))
        self.assertEqual(3723, BusinessStatusCollector._parse_elapsed("01:02:03"))
        self.assertEqual(176523, BusinessStatusCollector._parse_elapsed("2-01:02:03"))
        self.assertIsNone(BusinessStatusCollector._parse_elapsed("unknown"))

    def test_business_status_reports_unknown_when_process_scan_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            collector = BusinessStatusCollector(
                [{"name": "business", "local_path": str(root)}],
                process_provider=lambda: (_ for _ in ()).throw(OSError("unavailable")),
            )

            report = collector.collect().to_dict()

            self.assertFalse(report["available"])
            self.assertEqual("unknown", report["programs"][0]["state"])

    def test_snapshot_collects_git_and_manifest_without_modifying_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            before = (repo / "README.md").read_bytes()

            collector = SnapshotCollector(
                root / "state",
                [{"name": "demo", "git_url": "git@example/demo.git", "local_path": str(repo)}],
                command_timeout_seconds=2,
            )
            report = collector.capture("ISS-20260915-12345678", "issue_created")

            manifest = json.loads(Path(report.manifest_path).read_text(encoding="utf-8"))
            names = {item["name"] for item in manifest["evidence"]}
            self.assertIn("git_demo_status", names)
            self.assertEqual(before, (repo / "README.md").read_bytes())

    def test_snapshot_collects_logs_from_configured_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            log_root = root / "runtime" / "device"
            log_root.mkdir(parents=True)
            log_file = log_root / "business.log"
            log_file.write_text("device started\n", encoding="utf-8")

            collector = SnapshotCollector(
                root / "state",
                [{"name": "demo", "git_url": "git@example/demo.git", "local_path": str(repo)}],
                log_paths=[log_root],
                command_timeout_seconds=2,
            )
            report = collector.capture("ISS-20260915-87654321", "issue_created")

            matches = [item for item in report.evidence if item.source == str(log_file.resolve())]
            self.assertEqual(1, len(matches))
            self.assertTrue(matches[0].ok)

    def test_redacts_common_credentials(self):
        value = redact("Authorization: Bearer-secret\napp_secret=abc123")
        self.assertNotIn("Bearer-secret", value)
        self.assertNotIn("abc123", value)

    def test_evidence_bundle_contains_manifest_conversation_and_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            collector = SnapshotCollector(
                root / "state",
                [{"name": "demo", "git_url": "git@example/demo.git", "local_path": str(repo)}],
                command_timeout_seconds=2,
            )
            report = collector.capture("ISS-1", "human_handoff")

            bundle = build_evidence_bundle(
                report,
                "ISS-1",
                "现象：设备没有响应",
                [{"role": "reporter", "content": "设备没有响应"}],
            )

            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                self.assertIn("摘要.md", names)
                self.assertIn("manifest.json", names)
                self.assertIn("conversation.json", names)
                self.assertIn("SHA256SUMS.txt", names)
                self.assertTrue(any(name.startswith("evidence/") for name in names))
                manifest = json.loads(archive.read("manifest.json"))
                included = [item for item in manifest["evidence"] if item.get("archive_path")]
                self.assertTrue(included)
                self.assertTrue(all("output_file" not in item for item in included))


if __name__ == "__main__":
    unittest.main()
