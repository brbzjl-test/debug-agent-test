import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import bridge
from base_sync import BaseSync, records
from doc_sync import IssueDocSync, entry_marker, issue_marker
from test_bridge import event


class IssueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge.Store(self.temp.name, {"engineer_users": ["ou_engineer"]})
        self.count = 0

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def send(self, text, sender="ou_owner"):
        self.count += 1
        message = event("om_test_" + str(self.count), text, sender_id=sender)
        self.store.enqueue(message)
        return self.store.db.execute("SELECT * FROM inbox WHERE message_id=?", (message["message_id"],)).fetchone()

    def report(self, title="电机启动失败"):
        row = self.send("/report " + title)
        return row, self.store.issue(row["issue_id"])

    def test_plain_chat_and_new_menu_do_not_create_issues(self):
        undecided = self.send("程序报错了")
        self.assertEqual(undecided["status"], "done")
        self.assertIsNone(self.store.next_message())
        self.send("/chat")
        ordinary = self.send("解释一下日志")
        self.assertEqual(ordinary["status"], "pending")
        self.assertIsNone(ordinary["issue_id"])
        self.send("/new")
        self.assertEqual(self.send("新现象")["status"], "done")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM issues").fetchone()[0], 0)

    def test_id_is_created_before_chat_and_sub_ids_have_separate_contexts(self):
        root = self.send("/report")
        self.assertEqual(root["status"], "done")
        self.assertEqual(self.store.issue(root["issue_id"])["description"], "")
        first = self.send("初次现象")
        self.store.finish(first, "初次回答", "parent-thread")
        child = self.send("/sub " + root["issue_id"])
        self.assertEqual(child["issue_id"], root["issue_id"] + "-S001")
        self.assertEqual(child["status"], "done")
        item = self.store.issue(child["issue_id"])
        self.assertEqual((item["parent_id"], item["status"], item["thread_id"], item["description"]),
                         (root["issue_id"], "open", None, ""))
        follow = self.send("本次现象")
        self.assertIsNone(self.store.session(follow))
        self.assertIn("旧问题资料", self.store.analysis_message(follow))
        self.assertIn("初次现象", self.store.analysis_message(follow))
        self.store.finish(follow, "本次回答", "child-thread")
        self.assertEqual(self.store.session(self.send("本次补充")), "child-thread")
        self.send("/use " + root["issue_id"])
        self.assertEqual(self.store.session(self.send("回到初次")), "parent-thread")
        sibling = self.send("/sub " + root["issue_id"])
        self.assertEqual(sibling["issue_id"], root["issue_id"] + "-S002")
        self.assertIsNone(self.store.session(sibling))

    def test_closing_child_leaves_parent_and_sibling_unchanged(self):
        root, _ = self.report()
        first = self.send("/sub " + root["issue_id"])
        second = self.send("/sub " + root["issue_id"])
        self.store.propose(self.store.issue(first["issue_id"]), "方案", "验证", "Codex", "child-plan")
        self.send("/confirm " + first["issue_id"])
        self.assertEqual(self.store.issue(first["issue_id"])["status"], "已解决")
        self.assertEqual(self.store.issue(root["issue_id"])["status"], "open")
        self.assertEqual(self.store.issue(second["issue_id"])["status"], "open")
        # Re-reporting an already resolved child remains possible.
        next_report = self.send("/sub " + first["issue_id"])
        self.assertEqual(self.store.issue(next_report["issue_id"])["parent_id"], first["issue_id"])

    def test_parent_selector_waits_then_creates_one_child_from_allowed_option(self):
        root, _ = self.report()
        self.send("/sub")
        waiting = self.send("选择期间误发的补充")
        self.assertIsNone(waiting["issue_id"])
        self.assertEqual(waiting["status"], "done")
        action = self.store.db.execute("SELECT * FROM card_actions WHERE command='/sub-select'").fetchone()
        callback = {"type": "card.action.trigger", "action_value": json.dumps({"bridge_action": action["token"]}),
                    "operator_id": "ou_owner", "event_id": "ev-sub1", "message_id": "om_selector", "chat_id": "oc_one",
                    "option": "not-an-offered-id"}
        self.assertFalse(self.store.enqueue_callback(callback))
        callback["option"] = root["issue_id"]
        self.assertTrue(self.store.enqueue_callback(callback))
        callback["event_id"] = "ev-sub2"
        self.assertFalse(self.store.enqueue_callback(callback))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM issues WHERE parent_id=?", (root["issue_id"],)).fetchone()[0], 1)
        self.assertEqual(self.send("本轮日志")["issue_id"], root["issue_id"] + "-S001")

    def test_other_reporter_cannot_select_private_parent(self):
        root, _ = self.report()
        result = self.send("/sub " + root["issue_id"], "ou_other")
        self.assertIn("只能访问", result["answer"])
        self.assertEqual(self.store.issue(root["issue_id"])["child_count"], 0)

    def test_sub_counter_and_selection_survive_restart_with_null_base(self):
        root, _ = self.report()
        self.send("/sub " + root["issue_id"])
        self.send("/chat")
        self.store.db.close()
        self.store = bridge.Store(self.temp.name, {"base": None})
        self.assertEqual(self.send("重启后的普通问题")["status"], "pending")
        result = self.send("/sub " + root["issue_id"])
        self.assertEqual(result["issue_id"], root["issue_id"] + "-S002")
        self.assertIn("父问题", self.store.issue_detail(self.store.issue(result["issue_id"])))

    def test_every_explicit_report_has_new_id_and_open_status(self):
        first, a = self.report()
        second, b = self.report()
        self.assertNotEqual(a["issue_id"], b["issue_id"])
        self.assertEqual((a["status"], b["status"]), ("open", "open"))
        self.assertIsNone(a["thread_id"])
        self.assertFalse(self.store.enqueue(json.loads(first["event_json"])))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM issues").fetchone()[0], 2)

    def test_queued_turns_bind_to_issue_at_arrival_not_execution_time(self):
        first, a = self.report("故障 A")
        follow = self.send("A 的补充")
        second, b = self.report("故障 B")
        self.store.finish(first, "A 回答", "thread-a")
        self.store.finish(second, "B 回答", "thread-b")
        self.assertEqual(self.store.session(follow), "thread-a")
        self.send("/use " + a["issue_id"])
        self.assertEqual(self.store.session(self.send("继续 A")), "thread-a")
        self.assertNotEqual(a["issue_id"], b["issue_id"])

    def test_only_reporter_can_confirm_pending_issue_and_open_cannot_close(self):
        row, item = self.report()
        self.send("/confirm " + item["issue_id"])
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "open")
        self.store.propose(item, "使用正确端口配置", "验证启动完成且状态反馈正常", "Codex", "answer1")
        self.send("/confirm " + item["issue_id"], "ou_engineer")
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "待验证")
        self.send("/confirm " + item["issue_id"])
        item = self.store.issue(item["issue_id"])
        self.assertEqual(item["status"], "已解决")
        self.assertEqual(item["confirmed_by"], "ou_owner")
        self.assertIsNotNone(item["confirmed_at"])
        self.assertFalse(self.store.propose(item, "迟到的 AI 方案", "验证步骤", "Codex", "late"))

    def test_failed_verification_reopens_same_issue_new_occurrence_needs_report(self):
        row, item = self.report()
        self.store.propose(item, "方案", "验证", "Codex", "p1")
        self.send("/reopen " + item["issue_id"])
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "open")
        self.store.propose(item, "新方案", "新验证", "Codex", "p2")
        self.send("/confirm " + item["issue_id"])
        result = self.send("又出问题了")
        self.assertEqual(result["status"], "done")
        self.assertIn("重新选择", result["answer"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM issues").fetchone()[0], 1)

    def test_incomplete_solution_does_not_advance_status(self):
        _, item = self.report()
        with self.assertRaises(ValueError):
            self.store.propose(item, "猜测端口错误", "", "Codex", "p1")
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "open")

    def test_only_configured_engineer_can_submit_manual_solution(self):
        _, item = self.report()
        command = "/verify " + item["issue_id"] + " 修复方案 | 验证步骤"
        self.send(command)
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "open")
        self.send(command, "ou_engineer")
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "待验证")

    def test_old_confirmation_card_cannot_confirm_a_new_solution(self):
        _, item = self.report()
        self.store.propose(item, "方案一", "验证一", "Codex", "p1")
        old = self.store.issue(item["issue_id"])["verification_version"]
        self.send("/reopen " + item["issue_id"])
        self.store.propose(item, "方案二", "验证二", "Codex", "p2")
        result = self.send("/confirm " + item["issue_id"] + " " + str(old))
        self.assertIn("过期", result["answer"])
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "待验证")

    def test_card_scope_and_double_click_do_not_create_extra_reports(self):
        self.send("/help")
        action = self.store.db.execute("SELECT * FROM card_actions WHERE command='/report'").fetchone()
        callback = {"type": "card.action.trigger", "action_value": json.dumps({"bridge_action": action["token"]}),
                    "operator_id": "ou_other", "event_id": "ev1", "message_id": "om_card", "chat_id": "oc_one"}
        self.assertFalse(self.store.enqueue_callback(callback))
        callback["operator_id"] = "ou_owner"
        self.assertTrue(self.store.enqueue_callback(callback))
        callback["event_id"] = "ev2"
        self.assertFalse(self.store.enqueue_callback(callback))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM issues").fetchone()[0], 1)

    def test_group_card_action_preserves_its_topic_context(self):
        topic = event("om_topic", "/help", chat_type="group", chat_id="oc_group", topic_id="om_topic")
        self.store.enqueue(topic)
        action = self.store.db.execute("SELECT * FROM card_actions WHERE command='/report' ORDER BY rowid DESC LIMIT 1").fetchone()
        callback = {"type": "card.action.trigger", "action_value": json.dumps({"bridge_action": action["token"]}),
                    "operator_id": "ou_owner", "event_id": "ev-topic", "message_id": "om_card", "chat_id": "oc_group"}
        self.assertTrue(self.store.enqueue_callback(callback))
        item = self.store.active(topic)
        self.assertIsNotNone(item)
        self.assertEqual(self.store.key(topic), self.store.key({"chat_id": "oc_group", "sender_id": "ou_owner", "topic_id": "om_topic"}))

    def test_migration_preserves_ordinary_history_without_inventing_issues(self):
        with tempfile.TemporaryDirectory() as state:
            old = bridge.MessageStore(state)
            old.enqueue(event())
            old.finish(event(), "legacy answer", "old-thread")
            old.db.close()
            new = bridge.Store(state)
            self.assertEqual(new.db.execute("SELECT count(*) FROM issues").fetchone()[0], 0)
            self.assertEqual(new.session(event()), "old-thread")
            new.db.close()


class FakeBase:
    def __init__(self):
        self.tables = {"tbl_issues": {}, "tbl_history": {}}
        self.counter = 0
        self.lose_create_response = False
        self.patches = []

    async def __call__(self, args):
        command = args[args.index("base") + 1]
        table = args[args.index("--table-id") + 1]
        if command == "+record-list":
            cond = json.loads(args[args.index("--filter-json") + 1])["conditions"][0]
            found = [r for r in self.tables[table].values() if r["fields"].get(cond[0]) == cond[2]]
            fields = [cond[0]]
            return json.dumps({"fields": fields, "record_id_list": [r['record_id'] for r in found],
                               "data": [[r['fields'].get(f) for f in fields] for r in found], "has_more": False})
        if command == "+record-get":
            rid = args[args.index("--record-id") + 1]
            fields = list(self.tables[table][rid]['fields'])
            return json.dumps({"fields": fields, "record_id_list": [rid],
                               "data": [[self.tables[table][rid]['fields'][f] for f in fields]]})
        if command == "+record-upsert":
            fields = json.loads(args[args.index("--json") + 1])
            self.patches.append(dict(fields))
            if "--record-id" in args:
                rid = args[args.index("--record-id") + 1]
                self.tables[table][rid]["fields"].update(fields)
            else:
                self.counter += 1
                rid = "rec_" + str(self.counter)
                self.tables[table][rid] = {"record_id": rid, "fields": fields}
                if self.lose_create_response:
                    self.lose_create_response = False
                    raise RuntimeError("response lost after creation")
            return json.dumps({"record": self.tables[table][rid]})
        raise AssertionError(command)


class FakeDocs:
    def __init__(self):
        self.blocks = []
        self.documents = {}
        self.counter = 0
        self.lose_update_response = False

    async def __call__(self, args):
        service = "base" if "base" in args else "docs"
        command = args[args.index(service) + 1]
        if (service, command) == ("base", "+base-block-list"):
            return json.dumps({"data": {"blocks": self.blocks}})
        if (service, command) == ("base", "+base-block-create"):
            kind = args[args.index("--type") + 1]
            self.counter += 1
            block = {"id": ("bfl_" if kind == "folder" else "ldx_") + str(self.counter),
                     "type": kind, "name": args[args.index("--name") + 1]}
            if "--parent-id" in args:
                block["parent_id"] = args[args.index("--parent-id") + 1]
            if kind == "docx":
                token = "doc_" + str(self.counter)
                block["docx_token"] = token
                self.documents[token] = {"content": "", "revision_id": 1}
            self.blocks.append(block)
            return json.dumps({"data": {"block": block}})
        token = args[args.index("--doc") + 1]
        if (service, command) == ("docs", "+fetch"):
            return json.dumps({"data": {"document": dict(self.documents[token])}})
        if (service, command) == ("docs", "+update"):
            document = self.documents[token]
            document["content"] += args[args.index("--content") + 1]
            document["revision_id"] += 1
            if self.lose_update_response:
                self.lose_update_response = False
                raise RuntimeError("response lost after append")
            return json.dumps({"data": {"document": dict(document)}})
        raise AssertionError((service, command))


class DocumentSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "state_dir": self.temp.name, "lark_cli": "lark-cli", "lark_profile": "test",
            "issue_docs": {"folder_name": "问题详情"},
            "base": {"base_token": "base", "url": "https://example.test/base/base"},
        }
        self.store = bridge.Store(self.temp.name, self.config)
        self.fake = FakeDocs()
        self.sync = IssueDocSync(self.config, self.store, self.fake)
        self.store.enqueue(event(text="/report 现象"))
        self.item = self.store.active(event())

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_creates_one_document_and_syncs_full_timeline(self):
        await self.sync.once()
        item = self.store.issue(self.item["issue_id"])
        self.assertEqual(item["doc_url"], "https://example.test/docx/doc_2")
        self.assertEqual(item["doc_initialized"], 1)
        content = self.fake.documents[item["doc_token"]]["content"]
        self.assertEqual(content.count(issue_marker(item["issue_id"])), 1)
        entries = self.store.db.execute("SELECT * FROM issue_entries WHERE issue_id=?", (item["issue_id"],)).fetchall()
        self.assertTrue(all(entry["doc_synced_at"] for entry in entries))
        self.assertTrue(all(content.count(entry_marker(entry["entry_id"])) == 1 for entry in entries))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE part_key='doc-ready'").fetchone()[0], 1)
        await self.sync.once()
        self.assertEqual(self.fake.documents[item["doc_token"]]["content"], content)

    async def test_response_loss_after_append_does_not_duplicate_entry(self):
        await self.sync.once()
        with self.store.db:
            self.store.entry(self.item["issue_id"], "retry-entry", "AI分析", "Codex", "需要保留一次")
        self.fake.lose_update_response = True
        await self.sync.once()
        with self.store.db:
            self.store.db.execute("UPDATE issues SET doc_retry_at=0 WHERE issue_id=?", (self.item["issue_id"],))
        await self.sync.once()
        item = self.store.issue(self.item["issue_id"])
        content = self.fake.documents[item["doc_token"]]["content"]
        self.assertEqual(content.count(entry_marker("retry-entry")), 1)
        synced = self.store.db.execute("SELECT doc_synced_at FROM issue_entries WHERE entry_id='retry-entry'").fetchone()[0]
        self.assertIsNotNone(synced)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"state_dir": self.temp.name, "lark_cli": "lark-cli", "lark_profile": "test",
                       "base": {"base_token": "base", "issues_table_id": "tbl_issues", "history_table_id": "tbl_history", "url": "https://example.test"}}
        self.store = bridge.Store(self.temp.name, self.config)
        self.fake = FakeBase()
        self.sync = BaseSync(self.config, self.store, self.fake)
        self.store.enqueue(event(text="/report 现象"))
        self.item = self.store.active(event())

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_lost_create_response_is_recovered_by_business_id(self):
        self.fake.lose_create_response = True
        with self.assertRaises(RuntimeError):
            await self.sync.sync_issue(self.item)
        await self.sync.sync_issue(self.store.issue(self.item["issue_id"]))
        self.assertEqual(len(self.fake.tables["tbl_issues"]), 1)

    async def test_first_cloud_record_is_open_even_when_locally_pending(self):
        self.store.propose(self.item, "方案", "验证", "Codex", "p1")
        await self.sync.sync_issue(self.store.issue(self.item["issue_id"]))
        self.assertEqual(self.fake.patches[0]["状态"], "open")
        self.assertEqual(self.fake.patches[-1]["状态"], "待验证")

    async def test_sub_report_is_a_distinct_cloud_row_with_parent_and_history(self):
        await self.sync.sync_issue(self.item)
        self.store.enqueue(event("om_sub", "/sub " + self.item["issue_id"]))
        child = self.store.active(event())
        await self.sync.sync_issue(child)
        child = self.store.issue(child["issue_id"])
        self.assertEqual(len(self.fake.tables["tbl_issues"]), 2)
        fields = self.fake.tables["tbl_issues"][child["record_id"]]["fields"]
        self.assertEqual(fields["父问题编号"], self.item["issue_id"])
        self.assertEqual(fields["上报类型"], "基于旧问题上报")
        self.assertEqual(fields["状态"], "open")
        self.assertEqual(fields["现象描述"], "")
        for entry in self.store.db.execute("SELECT * FROM issue_entries WHERE issue_id=?", (child["issue_id"],)).fetchall():
            await self.sync.sync_entry(entry)
        self.assertTrue(all(r["fields"]["所属问题"] == [{"id": child["record_id"]}] for r in self.fake.tables["tbl_history"].values()))

    async def test_human_fields_preserved_and_remote_close_cannot_bypass_reporter(self):
        await self.sync.sync_issue(self.item)
        item = self.store.issue(self.item["issue_id"])
        remote = self.fake.tables["tbl_issues"][item["record_id"]]["fields"]
        remote.update({"负责人": [{"id": "ou_engineer"}], "根因与复盘": "人工记录", "状态": "已解决"})
        await self.sync.sync_issue(item)
        self.assertEqual(remote["状态"], "open")
        self.assertEqual(remote["根因与复盘"], "人工记录")
        self.assertEqual(remote["负责人"], [{"id": "ou_engineer"}])

    async def test_sheet_solution_moves_pending_but_stale_sheet_cannot_undo_failed_verification(self):
        await self.sync.sync_issue(self.item)
        item = self.store.issue(self.item["issue_id"])
        remote = self.fake.tables["tbl_issues"][item["record_id"]]["fields"]
        remote.update({"状态": "待验证", "解决方案": "开发修复", "验证步骤": "业务验证"})
        await self.sync.sync_issue(item)
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "待验证")
        self.store.enqueue(event("om_failed", "/reopen " + item["issue_id"]))
        await self.sync.sync_issue(self.store.issue(item["issue_id"]))
        self.assertEqual(self.store.issue(item["issue_id"])["status"], "open")
        self.assertEqual(remote["状态"], "open")

    async def test_long_history_is_complete_and_retries_do_not_duplicate_parts(self):
        await self.sync.sync_issue(self.item)
        content = "完整记录🦾" * 6000
        with self.store.db:
            self.store.entry(self.item["issue_id"], "long-entry", "AI分析", "Codex", content)
        entry = self.store.db.execute("SELECT * FROM issue_entries WHERE entry_id='long-entry'").fetchone()
        await self.sync.sync_entry(entry)
        await self.sync.sync_entry(entry)
        rows = list(self.fake.tables["tbl_history"].values())
        self.assertEqual("".join(r["fields"]["内容"] for r in rows), content)
        self.assertEqual(len(rows), 3)

    async def test_stale_ai_proposal_in_sheet_cannot_undo_site_rejection(self):
        self.store.propose(self.item, "AI 方案", "AI 验证", "Codex", "ai1")
        await self.sync.sync_issue(self.store.issue(self.item["issue_id"]))
        self.store.enqueue(event("om_failed_ai", "/reopen " + self.item["issue_id"]))
        await self.sync.sync_issue(self.store.issue(self.item["issue_id"]))
        self.assertEqual(self.store.issue(self.item["issue_id"])["status"], "open")


if __name__ == "__main__":
    unittest.main()
