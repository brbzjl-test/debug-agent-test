import hashlib
import hmac
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from field_support_gateway.adapters import LarkCliBaseAdapter, LarkCliFeishuAdapter
from field_support_gateway.auth import HmacCallbackVerifier
from field_support_gateway.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ValidationError,
)
from field_support_gateway.server import GatewayHTTPServer
from field_support_gateway.service import GatewayService
from field_support_gateway.store import GatewayStore
from field_support_gateway.worker import OutboxWorker


class FakeFeishuAdapter:
    def __init__(self):
        self.calls = []
        self.updates = []

    def send_handoff(self, handoff):
        self.calls.append(handoff)
        message_id = "message-" + handoff["issue_id"]
        return {
            "chat_id": "chat-1",
            "message_id": message_id,
            "topic_id": handoff.get("_topic_root_message_id") or message_id,
        }

    def update_issue_card(self, message_id, issue_id, state, solution, observation=None):
        self.updates.append((message_id, issue_id, state, solution, observation))


class FakeBaseAdapter:
    def __init__(self):
        self.calls = []

    def apply_event(self, event):
        self.calls.append(event)
        return {"record_id": "rec-1"}


class FailingFeishuAdapter:
    def send_handoff(self, handoff):
        raise RuntimeError("Feishu unavailable")


class GatewayTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = GatewayStore(str(Path(self.tempdir.name) / "gateway.db"))
        self.store.register_device("station-1", "device-secret")
        self.store.register_device("station-2", "other-secret")
        self.store.register_engineer("engineer-1")
        self.feishu = FakeFeishuAdapter()
        self.base = FakeBaseAdapter()
        self.secret = "callback-secret"
        self.service = GatewayService(
            self.store,
            HmacCallbackVerifier(self.secret),
            self.feishu,
            self.base,
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    @staticmethod
    def handoff_payload():
        return {
            "issue_id": "ISS-20260916-00000001",
            "reporter_id": "site-user",
            "summary": "device missing",
            "evidence": [{"snapshot_id": "SNP-1"}],
        }

    def create_handoff(self):
        return self.service.handoff(
            "Bearer device-secret", "handoff-key-1", self.handoff_payload()
        )

    def signed(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        return {
            "X-Field-Support-Timestamp": timestamp,
            "X-Field-Support-Signature": signature,
        }, body

    def test_handoff_is_idempotent_and_conflicts_on_changed_content(self):
        first = self.create_handoff()
        second = self.create_handoff()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(1, self.store.count_rows("issues"))
        self.assertEqual(1, self.store.count_rows("handoffs"))
        self.assertEqual(1, self.store.count_rows("delivery_outbox"))
        changed = self.handoff_payload()
        changed["summary"] = "different"
        with self.assertRaises(ConflictError):
            self.service.handoff("Bearer device-secret", "handoff-key-1", changed)

    def test_sync_always_returns_materialized_state(self):
        self.create_handoff()
        initial = self.service.sync(
            "Bearer device-secret", "ISS-20260916-00000001", 999999
        )
        self.assertEqual("open", initial["issue"]["status"])
        self.assertEqual([], initial["events"])
        self.assertGreater(initial["cursor"], 0)
        with self.assertRaises(AuthorizationError):
            self.service.sync(
                "Bearer other-secret", "ISS-20260916-00000001", 0
            )

    def test_solution_requires_actual_solution_but_not_verification(self):
        self.create_handoff()
        empty_payload = {
            "event_id": "card-empty",
            "action": "submit_solution",
            "operator": {"open_id": "engineer-1"},
            "value": {
                "issue_id": "ISS-20260916-00000001",
                "actual_solution": "  ",
            },
        }
        headers, body = self.signed(empty_payload)
        with self.assertRaises(ValidationError):
            self.service.card_callback(headers, body)

        payload = {
            "event_id": "card-1",
            "action": "submit_solution",
            "operator": {"open_id": "engineer-1"},
            "value": {
                "issue_id": "ISS-20260916-00000001",
                "solution_version": 1,
                "actual_solution": "reconnected cable",
            },
        }
        headers, body = self.signed(payload)
        result = self.service.card_callback(headers, body)
        replay = self.service.card_callback(headers, body)
        self.assertEqual("pending_verification", result["status"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, self.store.count_rows("solutions"))
        synced = self.service.sync(
            "Bearer device-secret", "ISS-20260916-00000001", 0
        )
        self.assertIsNone(synced["latest_solution"]["verification_method"])

    def test_solution_engineer_must_be_authorized_and_version_must_advance(self):
        self.create_handoff()
        unauthorized = {
            "event_id": "card-x",
            "action": "submit_solution",
            "operator": {"open_id": "unknown"},
            "value": {
                "issue_id": "ISS-20260916-00000001",
                "actual_solution": "restart",
            },
        }
        headers, body = self.signed(unauthorized)
        with self.assertRaises(AuthorizationError):
            self.service.card_callback(headers, body)

        wrong_version = {
            "event_id": "card-v2",
            "action": "submit_solution",
            "operator": {"open_id": "engineer-1"},
            "value": {
                "issue_id": "ISS-20260916-00000001",
                "solution_version": 2,
                "actual_solution": "restart",
            },
        }
        headers, body = self.signed(wrong_version)
        with self.assertRaises(ConflictError):
            self.service.card_callback(headers, body)

    def test_reporter_confirmation_closes_gateway_issue_idempotently(self):
        self.create_handoff()
        self.service.dispatch_one_handoff()
        self.store.submit_solution(
            "card-confirm", "engineer-1", "ISS-20260916-00000001", "restart", None, 1
        )
        first = self.service.confirm_solution(
            "Bearer device-secret",
            "confirm-key",
            "ISS-20260916-00000001",
            {"solution_version": 1, "reporter_id": "site-user"},
        )
        replay = self.service.confirm_solution(
            "Bearer device-secret",
            "confirm-key",
            "ISS-20260916-00000001",
            {"solution_version": 1, "reporter_id": "site-user"},
        )
        self.assertEqual("closed", first["status"])
        self.assertTrue(replay["replayed"])
        synced = self.service.sync("Bearer device-secret", "ISS-20260916-00000001", 0)
        self.assertEqual("closed", synced["issue"]["status"])
        self.assertEqual("closed", self.feishu.updates[-1][2])

    def test_verification_failure_reopens_issue_and_engineer_card(self):
        self.create_handoff()
        self.service.dispatch_one_handoff()
        self.store.submit_solution(
            "card-failure", "engineer-1", "ISS-20260916-00000001", "restart", None, 1
        )
        result = self.service.report_verification_failure(
            "Bearer device-secret",
            "failure-key",
            "ISS-20260916-00000001",
            {
                "solution_version": 1,
                "reporter_id": "site-user",
                "observation": "restart did not restore the device",
            },
        )
        self.assertEqual("open", result["status"])
        self.assertEqual("awaiting_engineer", self.feishu.updates[-1][2])
        self.assertEqual("restart did not restore the device", self.feishu.updates[-1][4])

    def test_event_callback_tracks_engineer_claim_and_deduplicates(self):
        self.create_handoff()
        payload = {
            "event_id": "event-claim-1",
            "kind": "EngineerClaimed",
            "operator": {"open_id": "engineer-1"},
            "value": {"issue_id": "ISS-20260916-00000001"},
        }
        headers, body = self.signed(payload)
        first = self.service.event_callback(headers, body)
        second = self.service.event_callback(headers, body)
        self.assertTrue(first["accepted"])
        self.assertTrue(second["replayed"])
        synced = self.service.sync(
            "Bearer device-secret", "ISS-20260916-00000001", 0
        )
        self.assertEqual("handling", synced["issue"]["handoff_status"])

    def test_callback_signature_is_required_and_case_insensitive(self):
        payload = {
            "event_id": "event-1",
            "operator": {"open_id": "engineer-1"},
        }
        body = json.dumps(payload).encode()
        with self.assertRaises(AuthenticationError):
            self.service.callback_verifier.verify({}, body)
        headers, signed_body = self.signed(payload)
        lower_headers = {key.lower(): value for key, value in headers.items()}
        verified = self.service.callback_verifier.verify(lower_headers, signed_body)
        self.assertEqual("event-1", verified.event_id)

    def test_delivery_and_base_outboxes_are_event_driven(self):
        self.create_handoff()
        self.assertTrue(self.service.dispatch_one_handoff())
        self.assertEqual(1, len(self.feishu.calls))
        synced = self.service.sync(
            "Bearer device-secret", "ISS-20260916-00000001", 0
        )
        self.assertEqual("delivered", synced["issue"]["handoff_status"])
        projected = 0
        while self.service.dispatch_one_base_event():
            projected += 1
        self.assertEqual(self.store.count_rows("events"), projected)
        self.assertEqual(projected, len(self.base.calls))

    def test_subissue_handoff_reuses_root_issue_topic(self):
        root_payload = self.handoff_payload()
        root_payload["root_issue_id"] = root_payload["issue_id"]
        self.service.handoff("Bearer device-secret", "root-key", root_payload)
        self.assertTrue(self.service.dispatch_one_handoff())

        sub_payload = {
            "issue_id": "ISS-20260916-00000001-S001",
            "root_issue_id": "ISS-20260916-00000001",
            "reporter_id": "site-user",
            "summary": "device missing again",
        }
        self.service.handoff("Bearer device-secret", "sub-key", sub_payload)
        self.assertTrue(self.service.dispatch_one_handoff())

        root = self.store.sync_issue("station-1", root_payload["issue_id"], 0)["issue"]
        sub = self.store.sync_issue("station-1", sub_payload["issue_id"], 0)["issue"]
        self.assertEqual(root["topic_id"], sub["topic_id"])
        self.assertNotEqual(root["message_id"], sub["message_id"])
        self.assertEqual(root["topic_id"], self.feishu.calls[-1]["_topic_root_message_id"])

    def test_lark_cli_adapter_uses_argument_list_and_environment_credentials(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 0, stdout='{"data":{"message_id":"m-1"}}', stderr=""
            )

        adapter = LarkCliFeishuAdapter(
            "server-profile", "support-chat", runner=runner
        )
        result = adapter.send_handoff(self.handoff_payload())
        command, kwargs = calls[0]
        self.assertIsInstance(command, list)
        self.assertIn("+messages-send", command)
        self.assertEqual("interactive", command[command.index("--msg-type") + 1])
        card = json.loads(command[command.index("--content") + 1])
        self.assertEqual("2.0", card["schema"])
        form = card["body"]["elements"][1]
        self.assertEqual("form", form["tag"])
        self.assertTrue(form["elements"][0]["required"])
        self.assertIn("--idempotency-key", command)
        self.assertNotIn("device-secret", command)
        self.assertEqual("m-1", result["message_id"])
        self.assertTrue(kwargs["check"])

        adapter.update_issue_card(
            "m-1",
            "ISS-1",
            "waiting_verification",
            {"version": 1, "actual_solution": "restart"},
        )
        update_command = calls[-1][0]
        self.assertIn("PATCH", update_command)
        updated = json.loads(update_command[update_command.index("--data") + 1])
        self.assertNotIn("提交解决方案", updated["content"])

        reply_payload = self.handoff_payload()
        reply_payload["_topic_root_message_id"] = "m-root"
        adapter.send_handoff(reply_payload)
        reply_command = calls[-1][0]
        self.assertIn("+messages-reply", reply_command)
        self.assertIn("--reply-in-thread", reply_command)
        self.assertEqual("m-root", reply_command[reply_command.index("--message-id") + 1])

    def test_lark_card_event_resolves_issue_from_message_binding(self):
        self.create_handoff()
        self.assertTrue(self.service.dispatch_one_handoff())
        message_id = self.store.sync_issue("station-1", "ISS-20260916-00000001", 0)["issue"]["message_id"]
        result = self.service.card_action_event(
            {
                "type": "card.action.trigger",
                "event_id": "card-live-1",
                "operator_id": "engineer-1",
                "message_id": message_id,
                "action_tag": "button",
                "form_value": json.dumps({"actual_solution": "重新插入设备线"}),
            }
        )
        self.assertEqual("pending_verification", result["status"])

    def test_lark_cli_base_adapter_searches_before_upsert(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if "+record-search" in command:
                output = '{"data":{"record_id_list":["rec-1"]}}'
            else:
                output = '{"data":{"record_id":"rec-1"}}'
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        adapter = LarkCliBaseAdapter(
            "server-profile", "base-token", "table-id", runner=runner
        )
        result = adapter.apply_event(
            {"issue_id": "ISS-1", "kind": "HandoffRequested", "seq": 1}
        )
        self.assertIn("+record-search", calls[0])
        self.assertIn("+record-upsert", calls[1])
        self.assertEqual("rec-1", calls[1][calls[1].index("--record-id") + 1])
        self.assertEqual("rec-1", result["data"]["record_id"])
        written = json.loads(calls[1][calls[1].index("--json") + 1])
        self.assertEqual("ISS-1", written["问题编号"])
        self.assertEqual("open", written["状态"])

    def test_worker_projects_base_when_feishu_delivery_fails(self):
        base = FakeBaseAdapter()
        service = GatewayService(
            self.store,
            HmacCallbackVerifier(self.secret),
            FailingFeishuAdapter(),
            base,
        )
        service.handoff(
            "Bearer device-secret", "worker-key", self.handoff_payload()
        )
        worker = OutboxWorker(service, retry_seconds=0.05)
        worker.start()
        deadline = time.time() + 1.0
        while not base.calls and time.time() < deadline:
            time.sleep(0.01)
        worker.stop()
        self.assertEqual(1, len(base.calls))


class HTTPIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = GatewayStore(str(Path(self.tempdir.name) / "http.db"))
        self.store.register_device("station-1", "device-secret")
        self.store.register_engineer("engineer-1")
        self.secret = "callback-secret"
        service = GatewayService(self.store, HmacCallbackVerifier(self.secret))
        self.server = GatewayHTTPServer(("127.0.0.1", 0), service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tempdir.cleanup()

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers or {}
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_http_handoff_sync_and_card_callback(self):
        handoff = {
            "issue_id": "ISS-HTTP-1",
            "reporter_id": "reporter",
            "summary": "process crashed",
        }
        status, result = self.request(
            "POST",
            "/v1/handoffs",
            handoff,
            {
                "Authorization": "Bearer device-secret",
                "Idempotency-Key": "http-key",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(202, status)
        self.assertEqual("queued", result["handoff_status"])

        _, synced = self.request(
            "GET",
            "/v1/issues/ISS-HTTP-1/sync?after_seq=999",
            headers={"Authorization": "Bearer device-secret"},
        )
        self.assertEqual("open", synced["issue"]["status"])
        self.assertEqual([], synced["events"])

        card = {
            "event_id": "http-card-1",
            "action": "submit_solution",
            "operator": {"open_id": "engineer-1"},
            "value": {
                "issue_id": "ISS-HTTP-1",
                "actual_solution": "restarted service",
            },
        }
        body = json.dumps(card).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        request = urllib.request.Request(
            self.base_url + "/v1/feishu/card-actions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Field-Support-Timestamp": timestamp,
                "X-Field-Support-Signature": signature,
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read())
        self.assertEqual("pending_verification", result["status"])

        status, reopened = self.request(
            "POST",
            "/v1/issues/ISS-HTTP-1/verification-failure",
            {
                "solution_version": 1,
                "reporter_id": "reporter",
                "observation": "service still fails",
            },
            {
                "Authorization": "Bearer device-secret",
                "Idempotency-Key": "http-failure-1",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("open", reopened["status"])

        self.store.submit_solution(
            "http-card-2", "engineer-1", "ISS-HTTP-1", "replace cable", None, 2
        )

        status, confirmed = self.request(
            "POST",
            "/v1/issues/ISS-HTTP-1/confirm",
            {"solution_version": 2, "reporter_id": "reporter"},
            {
                "Authorization": "Bearer device-secret",
                "Idempotency-Key": "http-confirm-1",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("closed", confirmed["status"])

        _, final = self.request(
            "GET",
            "/v1/issues/ISS-HTTP-1/sync?after_seq=999",
            headers={"Authorization": "Bearer device-secret"},
        )
        self.assertEqual("replace cable", final["latest_solution"]["actual_solution"])
        self.assertEqual("closed", final["issue"]["status"])


if __name__ == "__main__":
    unittest.main()
