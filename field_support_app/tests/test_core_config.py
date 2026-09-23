import tempfile
import unittest
from pathlib import Path

from field_support_agent.config import ConfigError, load_config, parse_config


VALID = """
business_repositories:
  - name: debug-agent-test
    git_url: git@github.com:brbzjl-test/debug-agent-test.git
    local_path: /Users/brb/项目/debug-agent-test
ros_topology:
  expected_topology_file: /tmp/topology.yaml
"""


class ConfigTests(unittest.TestCase):
    def test_parses_minimum_contract(self):
        config = parse_config(VALID)
        self.assertEqual("debug-agent-test", config.business_repositories[0].name)
        self.assertEqual(Path("/Users/brb/项目/debug-agent-test"), config.business_repositories[0].local_path)
        self.assertEqual(Path("/tmp/topology.yaml"), config.ros_topology.expected_topology_file)

    def test_ros_topology_is_optional(self):
        config = parse_config("business_repositories:\n  - name: app\n    git_url: ssh://app\n    local_path: /opt/app\n")
        self.assertIsNone(config.ros_topology)

    def test_parses_configured_log_paths(self):
        config = parse_config(
            "business_repositories:\n"
            "  - name: app\n"
            "    git_url: ssh://app\n"
            "    local_path: /opt/app\n"
            "log_paths:\n"
            "  - /tmp/singel_workstation_runtime/A2机器人宝维塔NCU上下料\n"
        )
        self.assertEqual(
            (Path("/tmp/singel_workstation_runtime/A2机器人宝维塔NCU上下料"),),
            config.log_paths,
        )

    def test_rejects_unknown_top_level_field(self):
        with self.assertRaisesRegex(ConfigError, "unknown top-level"):
            parse_config(VALID + "model_tools:\n  - shell: true\n")

    def test_rejects_unknown_repository_field_and_relative_path(self):
        with self.assertRaisesRegex(ConfigError, "unknown repository"):
            parse_config(
                "business_repositories:\n  - name: app\n    git_url: ssh://app\n    local_path: /opt/app\n    writable: true\n"
            )
        with self.assertRaisesRegex(ConfigError, "must be absolute"):
            parse_config(
                "business_repositories:\n  - name: app\n    git_url: ssh://app\n    local_path: relative/app\n"
            )

    def test_load_config_reports_io_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "cannot read"):
                load_config(Path(directory) / "missing.yaml")


if __name__ == "__main__":
    unittest.main()
