import importlib.util
import io
import os
import pwd
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from field_support_agent.config import parse_config


ROOT = Path(__file__).parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup_package = load_script("setup_package")


class DebianPackageTests(unittest.TestCase):
    def test_setup_generates_absolute_config_from_git_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "business repo"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", "ssh://example/repo.git"],
                check=True,
            )
            self.assertEqual("ssh://example/repo.git", setup_package._git_origin(repository))
            config = parse_config(setup_package._config_text("business", "ssh://example/repo.git", repository))
            self.assertEqual(repository, config.business_repositories[0].local_path)

    def test_setup_command_writes_config_and_invokes_installer(self):
        if os.geteuid() == 0:
            self.skipTest("requires a non-root test account")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "business repo"
            repository.mkdir()
            config_dir = root / "etc"
            user = pwd.getpwuid(os.geteuid()).pw_name
            with patch.object(setup_package, "CONFIG_DIR", config_dir), \
                 patch.object(setup_package, "CONFIG_PATH", config_dir / "config.yaml"), \
                 patch.object(setup_package, "SERVICE_USER_PATH", config_dir / "service-user"), \
                 patch.object(setup_package, "PACKAGE_ROOT", root / "package"), \
                 patch.object(setup_package.os, "geteuid", return_value=0), \
                 patch.object(setup_package.subprocess, "run") as run, \
                 patch.dict(os.environ, {"SUDO_USER": user}):
                result = setup_package.main(["--repo", str(repository), "--git-url", "ssh://example/repo.git"])
                self.assertEqual(0, result)
                config = parse_config((config_dir / "config.yaml").read_text(encoding="utf-8"))
                self.assertEqual(repository.resolve(), config.business_repositories[0].local_path)
                self.assertEqual("ssh://example/repo.git", config.business_repositories[0].git_url)
                self.assertEqual(user, (config_dir / "service-user").read_text(encoding="utf-8").strip())
                self.assertEqual("keep", run.call_args.args[0][-1])
                self.assertEqual(0, setup_package.main(["--reuse"]))
                self.assertEqual(2, run.call_count)

    def test_deb_contains_setup_command_and_service_templates(self):
        try:
            builder = load_script("build_deb")
        except ImportError:
            self.skipTest("TOML parser is unavailable in this Python runtime")
        with tempfile.TemporaryDirectory() as directory:
            package = builder.build(Path(directory))
            self.assertEqual("field-support-agent_0.1.0_all.deb", package.name)
            data = package.read_bytes()
            self.assertTrue(data.startswith(b"!<arch>\n"))
            members = {}
            offset = 8
            while offset < len(data):
                header = data[offset : offset + 60]
                self.assertEqual(b"`\n", header[58:60])
                name = header[:16].decode("ascii").strip().rstrip("/")
                size = int(header[48:58].decode("ascii").strip())
                members[name] = data[offset + 60 : offset + 60 + size]
                offset += 60 + size + size % 2
            self.assertEqual(["debian-binary", "control.tar.gz", "data.tar.gz"], list(members))
            self.assertEqual(b"2.0\n", members["debian-binary"])
            with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as archive:
                control = archive.extractfile("control").read().decode("utf-8")
            self.assertIn("Architecture: all", control)
            with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("usr/bin/field-support-setup", names)
            self.assertIn("usr/share/field-support-agent/scripts/install.sh", names)
            self.assertIn("usr/share/field-support-agent/deploy/systemd/field-support-autostart.service.in", names)


if __name__ == "__main__":
    unittest.main()
