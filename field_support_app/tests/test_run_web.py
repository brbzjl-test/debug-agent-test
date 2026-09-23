import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("run_web", ROOT / "scripts/run_web.py")
run_web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_web)
from field_support_agent.ui.preview import preview_server


class RunWebTest(unittest.TestCase):
    def test_uses_running_core_and_binds_browser_to_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            token_path = runtime / "api.token"
            token_path.write_text("test-token\n", encoding="utf-8")
            (runtime / "api.json").write_text(
                json.dumps({"host": "127.0.0.1", "port": 8766, "token_file": str(token_path)}),
                encoding="utf-8",
            )
            with patch.object(run_web, "serve_preview") as serve:
                result = run_web.main(["--runtime-dir", str(runtime), "--no-browser", "--port", "8767"])
            self.assertEqual(result, 0)
            serve.assert_called_once_with(
                host="127.0.0.1",
                port=8767,
                open_browser=False,
                core_url="http://127.0.0.1:8766",
                session_token="test-token",
            )

    def test_fails_clearly_before_core_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(run_web, "serve_preview") as serve:
                result = run_web.main(["--runtime-dir", directory])
            self.assertEqual(result, 3)
            serve.assert_not_called()

    def test_web_api_proxies_to_core_with_session_token(self):
        received = []

        class CoreHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                body = b'{"issues":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        core = ThreadingHTTPServer(("127.0.0.1", 0), CoreHandler)
        thread = threading.Thread(target=core.serve_forever, daemon=True)
        thread.start()
        try:
            with preview_server(
                core_url="http://127.0.0.1:{}".format(core.server_port),
                session_token="test-token",
            ) as (_, url):
                with urllib.request.urlopen(url.replace("/index.html", "/api/issues"), timeout=2) as response:
                    self.assertEqual(response.read(), b'{"issues":[]}')
            self.assertEqual(received, [("/issues", "Bearer test-token")])
        finally:
            core.shutdown()
            core.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
