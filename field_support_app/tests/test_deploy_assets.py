import sys
import tempfile
import unittest
import os
import subprocess
import pwd
from pathlib import Path


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


class DeployAssetsTest(unittest.TestCase):
    def read(self, relative: str) -> str:
        path = ROOT / relative
        self.assertTrue(path.is_file(), relative)
        return path.read_text(encoding="utf-8")

    def test_system_service_is_boot_scoped_non_root_and_hardened(self):
        service = self.read("deploy/systemd/field-support-core.service.in")
        self.assertIn("User=@SERVICE_USER@", service)
        self.assertNotIn("User=root", service)
        self.assertIn("WantedBy=multi-user.target", service)
        self.assertIn("After=local-fs.target", service)
        self.assertNotIn("network-online.target", service)
        for setting in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=read-only"):
            self.assertIn(setting, service)

    def test_business_repositories_are_rendered_read_only(self):
        template = self.read("deploy/systemd/repositories.conf.in")
        renderer = self.read("scripts/render_deploy.py")
        self.assertIn("@READ_ONLY_REPOSITORIES@", template)
        self.assertIn("BindReadOnlyPaths=", renderer)
        self.assertNotIn("BindPaths=", renderer)

    def test_ui_is_xdg_autostart_and_requires_pyside(self):
        desktop = self.read("deploy/xdg/field-support-float.desktop.in")
        runner = self.read("scripts/run_ui.py")
        installer = self.read("scripts/install.sh")
        project = self.read("pyproject.toml")
        self.assertIn("X-GNOME-Autostart-enabled=true", desktop)
        self.assertIn("PySide6.QtWebEngineWidgets", runner)
        self.assertIn("run_web.py", runner)
        self.assertIn("api.json", runner)
        self.assertIn("token_file", runner)
        self.assertIn("core_url=core_url", runner)
        self.assertIn("[desktop]", installer)
        self.assertIn('desktop = ["PySide6', project)
        self.assertNotIn("pip install -r", installer)

    def test_installer_can_disable_both_autostart_entries(self):
        installer = self.read("scripts/install.sh")
        self.assertIn('AUTOSTART="keep"', installer)
        self.assertIn('--autostart) AUTOSTART=', installer)
        self.assertIn('PREFERENCE_PATH="${STATE_DIR}/autostart.mode"', installer)
        self.assertIn('if [[ "${AUTOSTART}" != "keep" || ! -f "${PREFERENCE_PATH}" ]]', installer)
        self.assertIn('systemctl enable field-support-autostart.service', installer)
        self.assertIn('systemctl disable field-support-core.service', installer)
        self.assertIn('systemctl restart field-support-autostart.service', installer)
        self.assertIn('run_ui_login.py', self.read("deploy/xdg/field-support-float.desktop.in"))
        self.assertIn('field-support-autostart.enabled', self.read("scripts/run_ui_login.py"))
        self.assertIn('field-support-autostart.enabled', self.read("scripts/startup_core.py"))

    def test_no_periodic_or_remote_idle_services_are_installed(self):
        deploy_text = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "deploy").rglob("*") if path.is_file()
        ).lower()
        self.assertNotIn("ontimer=", deploy_text)
        self.assertNotIn("feishu", deploy_text)
        self.assertNotIn("base polling", deploy_text)
        self.assertFalse(list((ROOT / "deploy").rglob("*.timer")))

    def test_healthcheck_is_one_shot_only(self):
        healthcheck = self.read("scripts/healthcheck.py")
        service = self.read("deploy/systemd/field-support-core.service.in")
        self.assertIn("ExecStartPost=", service)
        self.assertIn("deadline =", healthcheck)
        self.assertIn("time.monotonic() >= deadline", healthcheck)
        self.assertNotIn("WatchdogSec", service)

    def test_local_development_scripts_do_not_write_project_state(self):
        core = self.read("scripts/dev_core.sh")
        self.assertIn("XDG_STATE_HOME", core)
        self.assertIn("XDG_RUNTIME_DIR", core)
        self.assertNotIn("/.dev-state", core)

    def test_renderer_uses_current_non_root_user_and_read_only_repository(self):
        if os.geteuid() == 0:
            self.skipTest("test requires a non-root account")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = temporary / "business repo"
            repository.mkdir()
            config = temporary / "config.yaml"
            config.write_text(
                "business_repositories:\n"
                "  - name: mock\n"
                "    git_url: ssh://example/mock.git\n"
                "    local_path: {}\n".format(repository),
                encoding="utf-8",
            )
            output = temporary / "rendered"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(SRC)
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/render_deploy.py"),
                    "--app-root",
                    str(ROOT),
                    "--install-root",
                    "/opt/field-support-agent",
                    "--config",
                    str(config),
                    "--user",
                    pwd.getpwuid(os.geteuid()).pw_name,
                    "--output",
                    str(output),
                ],
                check=True,
                env=environment,
            )
            service = (output / "field-support-core.service").read_text(encoding="utf-8")
            autostart = (output / "field-support-autostart.service").read_text(encoding="utf-8")
            repositories = (output / "repositories.conf").read_text(encoding="utf-8")
            install_environment = (output / "install.env").read_text(encoding="utf-8")
            self.assertIn("User={}".format(pwd.getpwuid(os.geteuid()).pw_name), service)
            self.assertIn("startup_core.py", autostart)
            self.assertNotIn("@INSTALL_ROOT@", autostart)
            self.assertIn('BindReadOnlyPaths="{}"'.format(repository), repositories)
            self.assertIn("SERVICE_GROUP=", install_environment)
            self.assertIn('ReadWritePaths="{}/.codex"'.format(pwd.getpwuid(os.geteuid()).pw_dir), service)
            self.assertNotIn('@CODEX_HOME_PATH@', service)


if __name__ == "__main__":
    unittest.main()
