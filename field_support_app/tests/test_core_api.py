import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from field_support_agent.api import LocalAPIServer
from field_support_agent.management import ManagementAccess
from field_support_agent.service import CoreService
from field_support_agent.startup import StartupPreference
from field_support_agent.storage import CoreDatabase


class APITests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = CoreDatabase(Path(self.temporary_directory.name) / "api.sqlite3")
        self.management = ManagementAccess(lambda: "secure-password")
        self.startup = StartupPreference(Path(self.temporary_directory.name) / "autostart.mode")
        self.api = LocalAPIServer(
            CoreService(self.database), management_access=self.management, startup_preference=self.startup
        )
        self.api.start()
        host, port = self.api.address
        self.base_url = "http://{}:{}".format(host, port)

    def tearDown(self):
        self.api.close()
        self.database.close()
        self.temporary_directory.cleanup()

    def request(self, method, path, body=None, token=True, headers=None):
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + self.api.session_token
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_server_binds_loopback_and_generates_token(self):
        self.assertEqual("127.0.0.1", self.api.address[0])
        self.assertGreaterEqual(len(self.api.session_token), 32)
        status, body = self.request("GET", "/health", token=False)
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_startup_switch_requires_session_and_persists_without_stopping_core(self):
        self.assertEqual(401, self.request("PUT", "/v1/startup", {"enabled": False}, token=False)[0])
        self.assertEqual(400, self.request("PUT", "/v1/startup", {"enabled": "off"})[0])
        status, state = self.request("PUT", "/v1/startup", {"enabled": False})
        self.assertEqual(200, status)
        self.assertEqual({"available": True, "enabled": False}, state)
        self.assertEqual("off", self.startup.path.read_text(encoding="utf-8").strip())
        self.assertEqual(200, self.request("GET", "/health", token=False)[0])
        self.assertEqual({"available": True, "enabled": False}, self.request("GET", "/v1/startup")[1])
        self.assertEqual(True, self.request("PUT", "/v1/startup", {"enabled": True})[1]["enabled"])

    def test_api_requires_token_and_rejects_unknown_fields(self):
        status, body = self.request("GET", "/v1/issues", token=False)
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["error"]["code"])
        status, body = self.request(
            "POST", "/v1/issues", {"reporter_id": "site-user", "write_access": True}
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_complete_api_lifecycle(self):
        status, created = self.request(
            "POST", "/v1/issues", {"reporter_id": "site-user", "description": "device missing"}
        )
        self.assertEqual(201, status)
        issue_id = created["issue"]["issue_id"]
        status, listed = self.request("GET", "/v1/issues")
        self.assertEqual(200, status)
        self.assertEqual("device missing", listed["issues"][0]["summary"])
        status, solution = self.request(
            "POST",
            "/v1/issues/{}/solutions".format(issue_id),
            {"submitted_by": "engineer", "content": "reconnect cable", "source": "feishu"},
        )
        self.assertEqual(201, status)
        self.assertEqual(1, solution["solution"]["version"])
        status, confirmed = self.request(
            "POST",
            "/v1/issues/{}/confirm".format(issue_id),
            {"reporter_id": "site-user", "expected_solution_version": 1},
        )
        self.assertEqual(200, status)
        self.assertEqual("closed", confirmed["issue"]["status"])
        status, timeline = self.request("GET", "/v1/issues/{}/timeline".format(issue_id))
        self.assertEqual(200, status)
        self.assertEqual("device missing", timeline["messages"][0]["content"])

    def test_reporter_confirms_latest_ai_conclusion(self):
        status, created = self.request(
            "POST", "/v1/issues", {"reporter_id": "site-user", "description": "camera missing"}
        )
        self.assertEqual(201, status)
        issue_id = created["issue"]["issue_id"]
        status, _ = self.request(
            "POST",
            "/v1/issues/{}/messages".format(issue_id),
            {"actor_id": "codex", "role": "assistant", "content": "reconnect camera", "channel": "system"},
        )
        self.assertEqual(201, status)

        status, confirmed = self.request(
            "POST",
            "/v1/issues/{}/confirm-ai-resolution".format(issue_id),
            {"reporter_id": "site-user"},
        )

        self.assertEqual(200, status)
        self.assertEqual("closed", confirmed["issue"]["status"])
        status, timeline = self.request("GET", "/v1/issues/{}/timeline".format(issue_id))
        self.assertEqual("reconnect camera", timeline["solutions"][-1]["content"])

    def test_management_password_protects_permanent_issue_deletion(self):
        _, created = self.request(
            "POST", "/v1/issues", {"reporter_id": "site-user", "description": "remove me"}
        )
        issue_id = created["issue"]["issue_id"]
        status, state = self.request("GET", "/v1/management/status")
        self.assertEqual(200, status)
        self.assertTrue(state["configured"])

        status, unlocked = self.request(
            "POST", "/v1/management/session", {"password": "secure-password"}
        )
        self.assertEqual(200, status)
        self.assertFalse(unlocked["password_created"])
        management_headers = {"X-Management-Token": unlocked["management_token"]}

        status, deleted = self.request(
            "POST",
            "/v1/management/issues/delete",
            {"issue_ids": [issue_id]},
            headers=management_headers,
        )
        self.assertEqual(200, status)
        self.assertEqual([issue_id], deleted["deleted_issue_ids"])
        self.assertEqual(404, self.request("GET", "/v1/issues/{}".format(issue_id))[0])

    def test_management_deletion_rejects_missing_or_wrong_session(self):
        issue_id = self.request("POST", "/v1/issues", {"reporter_id": "site-user"})[1]["issue"]["issue_id"]
        self.request("POST", "/v1/management/session", {"password": "secure-password"})
        status, body = self.request(
            "POST", "/v1/management/issues/delete", {"issue_ids": [issue_id]}
        )
        self.assertEqual(403, status)
        self.assertEqual("forbidden", body["error"]["code"])


if __name__ == "__main__":
    unittest.main()
