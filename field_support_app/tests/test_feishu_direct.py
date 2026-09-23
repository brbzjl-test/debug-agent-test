import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from field_support_agent.integrations.feishu_direct import (
    DirectFeishuClient,
    _compact_summary,
    _find_form_values,
    _solution_card,
)


class DirectFeishuClientTests(unittest.TestCase):
    def test_constructor_does_not_open_long_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            client = DirectFeishuClient("cli_test", "secret", "oc_chat", Path(temp) / "state.json")
            self.assertIsNone(client._thread)
            self.assertIsNone(client._channel)

    def test_device_name_is_used_when_creating_support_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            client = DirectFeishuClient(
                "cli_test",
                "secret",
                "",
                Path(temp) / "state.json",
                site_name="上海工厂",
                device_name="A2机器人",
            )
            calls = []

            def fake_json(url, token, body, method="POST"):
                calls.append((url, body, method))
                if method == "GET":
                    return {"data": {"items": []}}
                return {"code": 0, "data": {"chat_id": "oc_created"}}

            with patch("field_support_agent.integrations.feishu_direct._tenant_access_token", return_value="tenant"), patch(
                "field_support_agent.integrations.feishu_direct._feishu_json", side_effect=fake_json
            ):
                self.assertEqual("oc_created", client.ensure_support_chat())

            self.assertEqual("现场支持·上海工厂·A2机器人", calls[1][1]["name"])
            self.assertEqual("oc_created", client.support_chat_id)

    def test_solution_card_requires_solution_and_verification(self):
        card = _solution_card({"issue_id": "ISS-1", "card_summary": "设备无响应", "evidence": []})
        form = card["body"]["elements"][1]
        inputs = {item.get("name"): item for item in form["elements"]}
        self.assertTrue(inputs["actual_solution"]["required"])
        self.assertTrue(inputs["verification_method"]["required"])
        self.assertEqual("submit", inputs["submit_solution"]["form_action_type"])
        self.assertIn("完整 ZIP 证据包已附在本问题话题中", card["body"]["elements"][0]["content"])

    def test_card_summary_never_exceeds_one_hundred_characters(self):
        self.assertEqual(100, len(_compact_summary("故" * 120)))
        self.assertTrue(_compact_summary("故" * 120).endswith("..."))

    def test_handoff_attaches_zip_as_thread_reply_to_card(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ISS-1_evidence.zip"
            bundle.write_bytes(b"zip")
            client = DirectFeishuClient("cli_test", "secret", "oc_chat", root / "state.json")
            calls = []

            class Channel:
                async def send(self, to, message, opts=None):
                    calls.append((to, message, opts))
                    return SimpleNamespace(success=True, message_id="om_file" if "file" in message else "om_card", error=None)

            client._channel = Channel()
            client._ensure_started = lambda: None
            client._run = lambda coroutine, timeout: asyncio.run(coroutine)

            result = client.handoff(
                {
                    "issue_id": "ISS-1",
                    "summary": "设备无响应",
                    "card_summary": "现象：设备无响应",
                    "evidence": [],
                    "evidence_bundle": str(bundle),
                },
                "handoff:ISS-1",
            )

            self.assertEqual("om_card", result["message_id"])
            self.assertEqual("om_file", result["evidence_message_id"])
            self.assertEqual("om_card", calls[1][2]["reply_to"])
            self.assertTrue(calls[1][2]["reply_in_thread"])

    def test_subissue_card_replies_inside_root_issue_topic(self):
        with tempfile.TemporaryDirectory() as temp:
            client = DirectFeishuClient("cli_test", "secret", "oc_chat", Path(temp) / "state.json")
            calls = []
            message_ids = iter(["om_root", "om_sub"])

            class Channel:
                async def send(self, to, message, opts=None):
                    calls.append((to, message, opts))
                    return SimpleNamespace(success=True, message_id=next(message_ids), error=None)

            client._channel = Channel()
            client._ensure_started = lambda: None
            client._run = lambda coroutine, timeout: asyncio.run(coroutine)

            root = client.handoff(
                {"issue_id": "ISS-1", "root_issue_id": "ISS-1", "summary": "first", "evidence": []},
                "handoff:ISS-1",
            )
            sub = client.handoff(
                {
                    "issue_id": "ISS-1-S001",
                    "root_issue_id": "ISS-1",
                    "parent_issue_id": "ISS-1",
                    "summary": "again",
                    "evidence": [],
                },
                "handoff:ISS-1-S001",
            )

            self.assertEqual("om_root", root["topic_id"])
            self.assertEqual("om_root", sub["topic_id"])
            self.assertIsNone(calls[0][2])
            self.assertEqual("om_root", calls[1][2]["reply_to"])
            self.assertTrue(calls[1][2]["reply_in_thread"])
            self.assertEqual("om_sub", client._state["issues"]["ISS-1-S001"]["message_id"])

    def test_first_subissue_handoff_owns_family_topic(self):
        with tempfile.TemporaryDirectory() as temp:
            client = DirectFeishuClient("cli_test", "secret", "oc_chat", Path(temp) / "state.json")

            class Channel:
                async def send(self, to, message, opts=None):
                    return SimpleNamespace(success=True, message_id="om_sub", error=None)

            client._channel = Channel()
            client._ensure_started = lambda: None
            client._run = lambda coroutine, timeout: asyncio.run(coroutine)
            result = client.handoff(
                {
                    "issue_id": "ISS-1-S001",
                    "root_issue_id": "ISS-1",
                    "summary": "again",
                    "evidence": [],
                },
                "handoff:ISS-1-S001",
            )
            self.assertEqual("om_sub", result["topic_id"])
            self.assertEqual("om_sub", client._state["topics"]["ISS-1"])

    def test_card_action_persists_solution_for_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            client = DirectFeishuClient("cli_test", "secret", "oc_chat", path)
            client._state["issues"]["ISS-1"] = {
                "handoff_status": "delivered",
                "message_id": "om_message",
                "solution": None,
                "confirmed": False,
            }
            updates = []

            class Channel:
                async def update_card(self, message_id, card):
                    updates.append((message_id, card))
                    return SimpleNamespace(success=True, error=None)

            client._channel = Channel()
            event = SimpleNamespace(
                message_id="om_message",
                operator=SimpleNamespace(open_id="ou_engineer"),
                action=SimpleNamespace(value={}),
                raw={
                    "event": {
                        "action": {
                            "form_value": {
                                "actual_solution": "重新插线并重启进程",
                                "verification_method": "确认遥操恢复",
                            }
                        }
                    }
                },
            )
            asyncio.run(client._on_card_action(event))
            result = client.sync("ISS-1")
            self.assertEqual("solution_available", result["issue"]["handoff_status"])
            self.assertEqual("重新插线并重启进程", result["latest_solution"]["actual_solution"])
            self.assertEqual("确认遥操恢复", result["latest_solution"]["verification_method"])
            self.assertEqual("om_message", updates[0][0])
            self.assertNotIn("form", [element["tag"] for element in updates[0][1]["body"]["elements"]])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_verification_failure_reopens_original_card_form(self):
        with tempfile.TemporaryDirectory() as temp:
            client = DirectFeishuClient("cli_test", "secret", "oc_chat", Path(temp) / "state.json")
            client._state["issues"]["ISS-1"] = {
                "message_id": "om_card",
                "handoff_status": "solution_available",
                "card_summary": "设备没有响应",
                "evidence_count": 3,
                "solution": {"version": 1, "actual_solution": "重新插线", "verification_method": "确认恢复"},
                "sequence": 1,
            }
            updates = []

            class Channel:
                async def update_card(self, message_id, card):
                    updates.append((message_id, card))
                    return SimpleNamespace(success=True, error=None)

            client._channel = Channel()
            client._ensure_started = lambda: None
            client._run = lambda coroutine, timeout: asyncio.run(coroutine)

            client.verification_failed("ISS-1", 1, "reporter", "重新插线后仍无响应")

            self.assertEqual("delivered", client.sync("ISS-1")["issue"]["handoff_status"])
            tags = [element["tag"] for element in updates[0][1]["body"]["elements"]]
            self.assertIn("form", tags)
            self.assertIn("重新插线后仍无响应", updates[0][1]["body"]["elements"][0]["content"])

    def test_nested_form_values_are_normalized(self):
        result = _find_form_values(
            SimpleNamespace(value={}),
            {"event": {"action": {"form_value": json.dumps({"actual_solution": "x", "verification_method": "y"})}}},
        )
        self.assertEqual({"actual_solution": "x", "verification_method": "y"}, result)


if __name__ == "__main__":
    unittest.main()
