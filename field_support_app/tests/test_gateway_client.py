import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from field_support_agent.integrations import GatewayClient


class GatewayClientTests(unittest.TestCase):
    def test_handoff_and_sync_use_device_bearer_token(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.command, self.path, self.headers, json.loads(body)))
                self._reply({"handoff_status": "queued"})

            def do_GET(self):
                received.append((self.command, self.path, self.headers, None))
                self._reply({"issue": {"handoff_status": "delivered"}, "events": [], "cursor": 2})

            def _reply(self, value):
                encoded = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = GatewayClient("http://127.0.0.1:{}".format(server.server_address[1]), "secret-token")
            client.handoff({"issue_id": "ISS-1"}, "key-1")
            client.sync("ISS-1", 1)
            client.confirm("ISS-1", 2, "reporter")
            client.verification_failed("ISS-1", 2, "reporter", "重启后仍无响应")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual("Bearer secret-token", received[0][2]["Authorization"])
        self.assertEqual("key-1", received[0][2]["Idempotency-Key"])
        self.assertEqual("/v1/issues/ISS-1/sync?after_seq=1", received[1][1])
        self.assertEqual("/v1/issues/ISS-1/confirm", received[2][1])
        self.assertEqual({"solution_version": 2, "reporter_id": "reporter"}, received[2][3])
        self.assertEqual("/v1/issues/ISS-1/verification-failure", received[3][1])
        self.assertEqual(
            {"solution_version": 2, "reporter_id": "reporter", "observation": "重启后仍无响应"},
            received[3][3],
        )


if __name__ == "__main__":
    unittest.main()
