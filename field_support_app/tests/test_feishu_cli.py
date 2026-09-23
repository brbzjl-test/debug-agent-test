import tempfile
import unittest
from pathlib import Path

from field_support_agent.integrations.feishu_cli import LarkCliFeishuClient, _find_value


class LarkCliFeishuClientTests(unittest.TestCase):
    def test_constructor_does_not_start_long_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            client = LarkCliFeishuClient("lark-cli", "bot", "oc_support", Path(temp) / "state.json")
            self.assertIsNone(client._process)

    def test_card_callback_saves_solution_for_matching_message(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            client = LarkCliFeishuClient("lark-cli", "bot", "oc_support", state_path)
            client._state["issues"]["ISS-1"] = {
                "message_id": "om_card",
                "handoff_status": "delivered",
                "solution": None,
                "sequence": 0,
            }
            updates = []
            client._update_card = lambda message_id, card: updates.append((message_id, card))
            client._handle_event(
                {
                    "type": "card.action.trigger",
                    "chat_id": "oc_support",
                    "message_id": "om_card",
                    "operator_id": "ou_engineer",
                    "form_value": {
                        "actual_solution": "重新插线并重启程序",
                        "verification_method": "确认图像恢复",
                    },
                }
            )

            synced = client.sync("ISS-1")
            self.assertEqual("solution_available", synced["issue"]["handoff_status"])
            self.assertEqual("重新插线并重启程序", synced["latest_solution"]["actual_solution"])
            self.assertEqual("om_card", updates[0][0])
            self.assertNotIn("form", [element["tag"] for element in updates[0][1]["body"]["elements"]])
            self.assertTrue(state_path.exists())

    def test_verification_failure_reopens_original_card_form(self):
        with tempfile.TemporaryDirectory() as temp:
            client = LarkCliFeishuClient("lark-cli", "bot", "oc_support", Path(temp) / "state.json")
            client._state["issues"]["ISS-1"] = {
                "message_id": "om_card",
                "handoff_status": "solution_available",
                "card_summary": "设备没有响应",
                "evidence_count": 3,
                "solution": {"version": 1, "actual_solution": "重新插线", "verification_method": "确认恢复"},
                "sequence": 1,
            }
            updates = []
            client._update_card = lambda message_id, card: updates.append((message_id, card))

            client.verification_failed("ISS-1", 1, "reporter", "重新插线后仍无响应")

            self.assertEqual("delivered", client.sync("ISS-1")["issue"]["handoff_status"])
            tags = [element["tag"] for element in updates[0][1]["body"]["elements"]]
            self.assertIn("form", tags)
            self.assertIn("重新插线后仍无响应", updates[0][1]["body"]["elements"][0]["content"])

    def test_find_value_searches_nested_cli_response(self):
        result = {"data": {"message": {"message_id": "om_123"}}}
        self.assertEqual("om_123", _find_value(result, "message_id"))

    def test_handoff_attaches_zip_as_thread_reply_to_card(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "ISS-1_evidence.zip"
            bundle.write_bytes(b"zip")
            client = LarkCliFeishuClient("lark-cli", "bot", "oc_support", root / "state.json")
            calls = []
            client._ensure_started = lambda: None

            def run_json(arguments, timeout=30, cwd=None):
                calls.append((arguments, timeout, cwd))
                message_id = "om_file" if "--file" in arguments else "om_card"
                return {"data": {"message_id": message_id}}

            client._run_json = run_json
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
            self.assertEqual(2, len(calls))
            self.assertIn("+messages-reply", calls[1][0])
            self.assertIn("--reply-in-thread", calls[1][0])
            self.assertEqual("om_card", calls[1][0][calls[1][0].index("--message-id") + 1])
            self.assertIn("--file", calls[1][0])
            self.assertEqual(root, calls[1][2])

            client.handoff(
                {"issue_id": "ISS-1", "evidence": [], "evidence_bundle": str(bundle)},
                "handoff:ISS-1",
            )
            self.assertEqual(2, len(calls))

    def test_subissue_card_replies_inside_root_issue_topic(self):
        with tempfile.TemporaryDirectory() as temp:
            client = LarkCliFeishuClient("lark-cli", "bot", "oc_support", Path(temp) / "state.json")
            calls = []
            message_ids = iter(["om_root", "om_sub"])
            client._ensure_started = lambda: None

            def run_json(arguments, timeout=30, cwd=None):
                calls.append(arguments)
                return {"data": {"message_id": next(message_ids)}}

            client._run_json = run_json
            root = client.handoff(
                {"issue_id": "ISS-1", "root_issue_id": "ISS-1", "summary": "first", "evidence": []},
                "handoff:ISS-1",
            )
            sub = client.handoff(
                {
                    "issue_id": "ISS-1-S001",
                    "root_issue_id": "ISS-1",
                    "summary": "again",
                    "evidence": [],
                },
                "handoff:ISS-1-S001",
            )

            self.assertEqual("om_root", root["topic_id"])
            self.assertEqual("om_root", sub["topic_id"])
            self.assertIn("+messages-send", calls[0])
            self.assertIn("+messages-reply", calls[1])
            self.assertIn("--reply-in-thread", calls[1])
            self.assertEqual("om_root", calls[1][calls[1].index("--message-id") + 1])

    def test_restart_restores_pending_solution_card_state(self):
        with tempfile.TemporaryDirectory() as temp:
            client = LarkCliFeishuClient("lark-cli", "bot", "oc_support", Path(temp) / "state.json")
            client._state["issues"]["ISS-1"] = {
                "message_id": "om_card",
                "evidence_message_id": "om_file",
                "handoff_status": "delivered",
                "card_state": "awaiting_engineer",
                "solution": {"version": 1, "actual_solution": "重新插线", "verification_method": "确认恢复"},
                "sequence": 1,
            }
            updates = []
            client._ensure_started = lambda: None
            client._update_card = lambda message_id, card: updates.append((message_id, card))

            result = client.handoff(
                {"issue_id": "ISS-1", "issue_status": "pending_verification", "evidence": []},
                "handoff:ISS-1",
            )
            client.sync("ISS-1")

            self.assertEqual("solution_available", result["handoff_status"])
            self.assertEqual("waiting_verification", client._state["issues"]["ISS-1"]["card_state"])
            self.assertEqual("om_card", updates[0][0])


if __name__ == "__main__":
    unittest.main()
