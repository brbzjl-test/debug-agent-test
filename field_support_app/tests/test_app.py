import argparse
import errno
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from field_support_agent.app import serve


class AppTests(unittest.TestCase):
    def test_desktop_port_conflict_has_actionable_message_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "business"
            repository.mkdir()
            config = root / "config.yaml"
            config.write_text(
                "business_repositories:\n"
                "  - name: business\n"
                "    git_url: git@example/business.git\n"
                "    local_path: {}\n".format(repository),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config=config,
                state_dir=root / "state",
                api_port=0,
                ui_port=8765,
                desktop=True,
                browser=False,
                no_codex=True,
            )
            output = io.StringIO()
            conflict = OSError(errno.EADDRINUSE, "Address already in use")

            with patch("field_support_agent.app.run_desktop_shell", side_effect=conflict), redirect_stderr(output):
                result = serve(args)

            self.assertEqual(2, result)
            self.assertIn("端口 8765 已被占用", output.getvalue())
            self.assertIn("可能已有一个现场调试助手在运行", output.getvalue())


if __name__ == "__main__":
    unittest.main()
