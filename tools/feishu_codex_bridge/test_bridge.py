import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import bridge


def event(message_id="om_1", text="请分析启动失败", **extra):
    return dict({
        "type": "im.message.receive_v1", "event_id": "ev_" + message_id,
        "message_id": message_id, "chat_id": "oc_one", "chat_type": "p2p",
        "sender_id": "ou_owner", "message_type": "text", "content": text,
    }, **extra)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge.MessageStore(self.temp.name)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_duplicate_delivery_creates_one_job_and_one_ack(self):
        self.assertTrue(self.store.enqueue(event()))
        self.assertFalse(self.store.enqueue(event(event_id="redelivery")))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_sent_ack_and_answer_survive_restart_without_another_model_call(self):
        self.store.enqueue(event())
        self.store.mark_sent(self.store.next_reply())
        self.store.finish(event(), "已经定位到相关代码", "thread-a")
        first = dict(self.store.next_reply())
        self.store.db.close()
        self.store = bridge.MessageStore(self.temp.name)
        self.store.recover()
        self.assertIsNone(self.store.next_message())
        self.assertEqual(first, dict(self.store.next_reply()))
        self.assertEqual(self.store.session(event()), "thread-a")

    def test_crashed_analysis_is_retained_and_not_silently_replayed(self):
        self.store.enqueue(event())
        self.store.mark_running(self.store.next_message())
        self.store.recover()
        self.assertIsNone(self.store.next_message())
        row = self.store.db.execute("SELECT * FROM inbox").fetchone()
        self.assertIn("重新发送", row["answer"])
        self.assertEqual(json.loads(row["event_json"])["content"], event()["content"])

    def test_sessions_are_isolated_by_chat_and_sender(self):
        self.store.enqueue(event())
        self.store.finish(event(), "first", "thread-a")
        self.assertIsNone(self.store.session(event(sender_id="ou_other")))
        self.assertIsNone(self.store.session(event(chat_id="oc_other")))

    def test_sessions_are_isolated_by_topic_in_one_group(self):
        first = event(chat_type="group", chat_id="oc_group", topic_id="om_topic_a")
        second = event("om_2", chat_type="group", chat_id="oc_group", topic_id="om_topic_b")
        reply = event("om_3", chat_type="group", chat_id="oc_group", topic_id="om_topic_a")
        self.store.enqueue(first)
        self.store.finish(first, "first", "thread-a")
        self.assertIsNone(self.store.session(second))
        self.assertEqual(self.store.session(reply), "thread-a")

    def test_reset_is_ordered_behind_retrying_question(self):
        self.store.enqueue(event())
        self.store.enqueue(event("om_2", "/new"))
        first = self.store.next_message()
        self.store.mark_running(first)
        self.store.retry_model(first, "network unavailable")
        self.assertIsNone(self.store.next_message())
        self.store.finish(first, "answer", "thread-a")
        reset = self.store.next_message()
        self.assertEqual(reset["message_id"], "om_2")
        self.store.reset(reset)
        self.assertIsNone(self.store.session(event()))

    def test_send_retry_keeps_idempotency_key_and_answer_order(self):
        self.store.enqueue(event())
        self.store.finish(event(), "很长的回答" * 1000, "thread-a")
        ack = self.store.next_reply()
        self.store.retry_reply(ack, "connection dropped after send")
        self.assertIsNone(self.store.next_reply())
        self.store.db.execute("UPDATE outbox SET next_attempt=0")
        retried = self.store.next_reply()
        self.assertEqual(ack["idempotency_key"], retried["idempotency_key"])
        self.store.mark_sent(retried)
        self.assertEqual(self.store.next_reply()["part_key"], "answer:0")

    def test_unsupported_media_never_becomes_model_input(self):
        self.store.enqueue(event(message_type="image", content="img_secret"))
        self.assertIsNone(self.store.next_message())
        self.assertIn("只支持文字", self.store.next_reply()["text"])

    def test_status_and_help_do_not_wait_for_model(self):
        self.store.enqueue(event())
        self.store.enqueue(event("om_2", "/help"))
        self.store.enqueue(event("om_3", "/status"))
        rows = self.store.db.execute("SELECT status FROM inbox ORDER BY seq").fetchall()
        self.assertEqual([row[0] for row in rows], ["pending", "done", "done"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "allowed_users": ["ou_owner"],
            "allowed_group_chats": ["oc_group"],
            "bot_names": ["小臂生产助手"],
        }

    def test_plain_text_looking_like_json_is_preserved(self):
        content = '{"text":"这是日志原文"}'
        self.assertEqual(bridge.normalize_event(event(text=content), self.config)["content"], content)

    def test_other_users_and_unrelated_events_are_ignored(self):
        self.assertIsNone(bridge.normalize_event(event(sender_id="ou_other"), self.config))
        self.assertIsNone(bridge.normalize_event(event(type="message.sent"), self.config))
        self.assertIsNone(bridge.normalize_event([], self.config))

    def test_group_requires_both_allowlist_and_explicit_prefix(self):
        group = event(chat_type="group", chat_id="oc_group")
        self.assertIsNone(bridge.normalize_event(group, self.config))
        group["content"] = "/codex 分析当前仓库"
        self.assertEqual(bridge.normalize_event(group, self.config)["content"], "分析当前仓库")
        group["content"] = "@小臂生产助手 /codex /report"
        self.assertEqual(bridge.normalize_event(group, self.config)["content"], "/report")
        group["content"] = "@小臂生产助手 我有个问题"
        self.assertEqual(bridge.normalize_event(group, self.config)["content"], "我有个问题")
        group["content"] = "@_user_1 /report"
        self.assertEqual(bridge.normalize_event(group, self.config)["content"], "/report")
        group["content"] = "请复制 /codex /report"
        self.assertIsNone(bridge.normalize_event(group, self.config))
        group["chat_id"] = "oc_unapproved"
        self.assertIsNone(bridge.normalize_event(group, self.config))

    def test_group_topic_uses_root_id_and_falls_back_to_first_message(self):
        root = event(chat_type="group", chat_id="oc_group", message_id="om_root", root_id="om_root", content="/codex /report")
        reply = event("om_reply", chat_type="group", chat_id="oc_group", root_id="om_root", parent_id="om_bot", content="/codex 日志补充")
        first = bridge.normalize_event(root, self.config)
        follow = bridge.normalize_event(reply, self.config)
        self.assertEqual((first["topic_id"], follow["topic_id"]), ("om_root", "om_root"))
        no_root = event("om_new", chat_type="group", chat_id="oc_group", content="/codex /report")
        self.assertEqual(bridge.normalize_event(no_root, self.config)["topic_id"], "om_new")

    def test_large_unicode_reply_is_lossless_and_within_byte_budget(self):
        text = "机器人🦾\n" * 8000
        chunks = bridge.split_reply(text)
        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(part.encode()) <= 10000 for part in chunks))

    def test_sandbox_and_external_tool_disables_apply_before_resume(self):
        config = {"codex_cli": "codex", "workdir": "/repo with spaces", "reasoning_effort": "medium"}
        args = bridge.codex_base(config, ["node_repl", "computer-use"])
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertIn("mcp_servers.computer-use.enabled=false", args)
        self.assertIn("mcp_servers.node_repl.enabled=false", args)
        self.assertIn("--ignore-rules", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_single_instance_lock_does_not_trust_stale_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            with bridge.acquire_lock(state):
                with self.assertRaises(RuntimeError):
                    bridge.acquire_lock(state)
                self.assertIsNotNone(bridge.running_pid(state))
            self.assertIsNone(bridge.running_pid(state))

    def test_bridge_can_be_constructed_before_asyncio_run_on_python39(self):
        with tempfile.TemporaryDirectory() as state:
            app = bridge.Bridge({"state_dir": state}, [])
            async def stop_immediately():
                app.stop_event.set()
            app.listen = stop_immediately
            app.listen_cards = stop_immediately
            asyncio.run(app.run())
            self.assertEqual(json.loads((Path(state) / "health.json").read_text())["connection"], "stopped")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_turns_then_reset_then_fresh_turn_through_reply_queue(self):
        with tempfile.TemporaryDirectory() as state:
            app = bridge.Bridge({"state_dir": state, "lark_cli": "lark-cli", "lark_profile": "test"}, [])
            def answer(text):
                return json.dumps({"answer": text, "resolution_proposed": False, "solution": "", "verification_steps": ""})
            app.codex.ask = AsyncMock(side_effect=[("thread-1", answer("一")), ("thread-1", answer("二")), ("thread-2", answer("三"))])
            for i, text in enumerate(("/report 问题一", "追问", "/report", "新问题")):
                app.store.enqueue(event("om_" + str(i), text))
            with patch.object(bridge, "run_cli", new_callable=AsyncMock) as send:
                tasks = [asyncio.create_task(app.analyze()), asyncio.create_task(app.send())]
                try:
                    for _ in range(100):
                        pending = app.store.db.execute("SELECT count(*) FROM inbox WHERE status!='done'").fetchone()[0]
                        unsent = app.store.db.execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]
                        if not pending and not unsent:
                            break
                        await asyncio.sleep(0.02)
                    self.assertEqual((pending, unsent), (0, 0))
                    self.assertEqual([call.args[1] for call in app.codex.ask.call_args_list], [None, "thread-1", None])
                    self.assertEqual(send.await_count, 9)  # three receipts, four final replies, two issue cards
                    self.assertTrue(all("--as" in call.args[0] and "bot" in call.args[0] for call in send.call_args_list))
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    app.store.db.close()

    async def test_subprocess_treats_shell_syntax_as_data(self):
        value = '中文 `do-not-execute` $(echo nope) "quotes"\nsecond line'
        result = await bridge.run_cli([sys.executable, "-c", "import sys; print(sys.stdin.read(), end='')"], stdin=value)
        self.assertEqual(result, value)


if __name__ == "__main__":
    unittest.main()
