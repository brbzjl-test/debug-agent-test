from __future__ import annotations

import contextlib
import json
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, Optional
from urllib import error, request


ASSET_DIR = Path(__file__).with_name("web")


class _AssetServer(ThreadingHTTPServer):
    daemon_threads = True


def _handler(core_url: Optional[str] = None, session_token: Optional[str] = None) -> type[SimpleHTTPRequestHandler]:
    root = str(ASSET_DIR)

    class AssetHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/api/"):
                self._proxy()
                return
            super().do_GET()

        def do_POST(self) -> None:
            if self.path.startswith("/api/"):
                self._proxy()
                return
            self.send_error(404)

        def do_PUT(self) -> None:
            if self.path.startswith("/api/"):
                self._proxy()
                return
            self.send_error(404)

        def _proxy(self) -> None:
            if not core_url or not session_token:
                self._json_error(503, "Core service is not connected")
                return
            target = core_url.rstrip("/") + self.path[len("/api") :]
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            headers = {"Authorization": "Bearer " + session_token}
            management_token = self.headers.get("X-Management-Token")
            if management_token:
                headers["X-Management-Token"] = management_token
            if body is not None:
                headers["Content-Type"] = "application/json"
            upstream = request.Request(target, data=body, method=self.command, headers=headers)
            try:
                with request.urlopen(upstream, timeout=60) as response:
                    payload = response.read()
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            except error.HTTPError as exc:
                payload = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            except (error.URLError, TimeoutError) as exc:
                self._json_error(502, "Core service unavailable: {}".format(exc))
                return
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json_error(self, status: int, message: str) -> None:
            payload = json.dumps({"error": {"code": "core_unavailable", "message": message}}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return AssetHandler


@contextlib.contextmanager
def preview_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    core_url: Optional[str] = None,
    session_token: Optional[str] = None,
) -> Iterator[tuple[_AssetServer, str]]:
    server = _AssetServer((host, port), _handler(core_url, session_token))
    thread = threading.Thread(target=server.serve_forever, name="field-support-ui", daemon=True)
    thread.start()
    address, bound_port = server.server_address[:2]
    url = f"http://{address}:{bound_port}/index.html"
    try:
        yield server, url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def serve_preview(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    open_browser: bool = True,
    core_url: Optional[str] = None,
    session_token: Optional[str] = None,
) -> None:
    server = _AssetServer((host, port), _handler(core_url, session_token))
    address, bound_port = server.server_address[:2]
    suffix = "?demo=1" if not core_url else ""
    url = f"http://{address}:{bound_port}/index.html{suffix}"
    print(f"现场调试助手预览：{url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
