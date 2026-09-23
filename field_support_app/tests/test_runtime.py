import json
import tempfile
import unittest
from pathlib import Path

from field_support_agent.analysis import AnalysisResult
from field_support_agent.service import CoreService, RuntimeService
from field_support_agent.storage import CoreDatabase


class FakeSnapshot:
    def __init__(self, path):
        self.manifest_path = str(path)


class FakeCollector:
    def __init__(self, root):
        self.root = root
        self.state_dir = root
        self.calls = []

    def capture(self, issue_id, trigger):
        self.calls.append((issue_id, trigger))
        path = self.root / (issue_id + ".json")
        path.write_text("{}", encoding="utf-8")
        return FakeSnapshot(path)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.thread_id = "thread-root"

    def start_conversation(self, issue_id, prompt, manifest, *, on_update=None, on_activity=None):
        self.calls.append(("start", issue_id, prompt, manifest))
        return AnalysisResult(issue_id, True, "只读分析结果", (), thread_id=self.thread_id)

    def resume_conversation(self, thread_id, issue_id, root_issue_id, prompt, manifest, *, recurrence=False, on_update=None, on_activity=None):
        self.calls.append(("resume", thread_id, issue_id, root_issue_id, prompt, manifest, recurrence))
        return AnalysisResult(issue_id, True, "继续分析结果", (), thread_id=thread_id)


class MissingSessionRunner(FakeRunner):
    def resume_conversation(self, thread_id, issue_id, root_issue_id, prompt, manifest, *, recurrence=False, on_update=None, on_activity=None):
        self.calls.append(("missing", thread_id, issue_id))
        return AnalysisResult(
            issue_id,
            False,
            "",
            (),
            error="session not found",
            thread_id=thread_id,
            session_missing=True,
        )

    def start_conversation(self, issue_id, prompt, manifest, *, on_update=None, on_activity=None):
        self.calls.append(("rebuild", issue_id, prompt, manifest))
        return AnalysisResult(issue_id, True, "重建后的分析", (), thread_id="thread-rebuilt")


class BusySessionRunner(FakeRunner):
    def resume_conversation(self, thread_id, issue_id, root_issue_id, prompt, manifest, **kwargs):
        self.calls.append(("resume", thread_id, issue_id))
        return AnalysisResult(
            issue_id, False, "", (), thread_id=thread_id,
            error="thread {} already has an active writer".format(thread_id), session_busy=True,
        )


class FakeGateway:
    def __init__(self):
        self.handoffs = []
        self.solution = None
        self.verification_failures = []

    def handoff(self, payload, idempotency_key):
        self.handoffs.append((payload, idempotency_key))
        return {"handoff_status": "queued"}

    def sync(self, issue_id, after_seq=0):
        return {
            "issue": {"issue_id": issue_id, "handoff_status": "solution_available" if self.solution else "delivered"},
            "latest_solution": self.solution,
            "events": [],
            "cursor": after_seq + 1,
        }

    def verification_failed(self, issue_id, solution_version, reporter_id, observation):
        self.verification_failures.append((issue_id, solution_version, reporter_id, observation))
        self.solution = None
        return {"handoff_status": "delivered"}


class RuntimeTests(unittest.TestCase):
    def test_busy_session_only_reports_reason_and_keeps_original_conversation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            core = CoreService(database)
            issue = core.create_issue("reporter", "最初的现象")
            core.append_message(issue.issue_id, "codex", "assistant", "之前的分析", "system")
            core.save_codex_thread(issue.issue_id, "thread-original")
            runner = BusySessionRunner()
            runtime = RuntimeService(core, FakeCollector(root), runner, workers=1)
            try:
                with self.assertLogs('field_support_agent.service.runtime', level='WARNING'):
                    runtime.append_message(issue.issue_id, "reporter", "reporter", "补充现象")
                    runtime.wait_for_idle(timeout=3)
                timeline = runtime.timeline(issue.issue_id)
                self.assertEqual([("resume", "thread-original", issue.issue_id)], runner.calls)
                self.assertEqual("thread-original", core.codex_thread(issue.issue_id))
                self.assertEqual(1, len(core.list_issues()))
                self.assertIsNone(timeline["analysis"])
                messages = [message.content for message in timeline["messages"]]
                self.assertIn("之前的分析", messages)
                self.assertIn("补充现象", messages)
                self.assertIn("其他窗口或进程占用", messages[-1])
                self.assertIn("本次分析未启动", messages[-1])
            finally:
                runtime.close()
                database.close()

    def test_management_delete_removes_issue_database_and_local_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            runtime = RuntimeService(CoreService(database), FakeCollector(root))
            try:
                issue = runtime.core.create_issue("reporter", "remove this")
                snapshot_dir = root / "issues" / issue.issue_id / "snapshots" / "SNP-TEST"
                snapshot_dir.mkdir(parents=True)
                (snapshot_dir / "manifest.json").write_text("{}", encoding="utf-8")
                (root / "feishu-state.json").write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "issues": {issue.issue_id: {"message_id": "om_test"}},
                            "topics": {issue.issue_id: "omt_test"},
                        }
                    ),
                    encoding="utf-8",
                )

                deleted = runtime.delete_issues([issue.issue_id])

                self.assertEqual([issue.issue_id], deleted)
                self.assertFalse((root / "issues" / issue.issue_id).exists())
                self.assertEqual([], runtime.list_issues())
                feishu_state = json.loads((root / "feishu-state.json").read_text(encoding="utf-8"))
                self.assertEqual({}, feishu_state["issues"])
                self.assertEqual({}, feishu_state["topics"])
            finally:
                runtime.close()
                database.close()

    def test_issue_and_message_trigger_snapshot_and_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            collector = FakeCollector(root)
            runner = FakeRunner()
            runtime = RuntimeService(CoreService(database), collector, runner)
            try:
                issue = runtime.create_issue("reporter")
                runtime.append_message(issue.issue_id, "reporter", "reporter", "设备没有反应")
                runtime.wait_for_idle(timeout=3)
                timeline = runtime.timeline(issue.issue_id)
                self.assertEqual(["issue_created", "first_observation"], [call[1] for call in collector.calls])
                self.assertEqual(1, len(runner.calls))
                self.assertEqual("start", runner.calls[0][0])
                self.assertEqual("thread-root", runtime.core.codex_thread(issue.issue_id))
                self.assertEqual("只读分析结果", timeline["messages"][-1].content)
            finally:
                runtime.close()
                database.close()

    def test_followup_and_subissue_share_root_codex_conversation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            collector = FakeCollector(root)
            runner = FakeRunner()
            runtime = RuntimeService(CoreService(database), collector, runner, workers=1)
            try:
                issue = runtime.create_issue("reporter")
                runtime.append_message(issue.issue_id, "reporter", "reporter", "第一次现象")
                runtime.wait_for_idle(timeout=3)
                runtime.append_message(issue.issue_id, "reporter", "reporter", "补充现象")
                runtime.wait_for_idle(timeout=3)
                subissue = runtime.create_subissue(issue.issue_id, "reporter")
                runtime.append_message(subissue.issue_id, "reporter", "reporter", "问题再次发生")
                runtime.wait_for_idle(timeout=3)

                self.assertEqual(["start", "resume", "resume"], [call[0] for call in runner.calls])
                self.assertEqual("thread-root", runner.calls[1][1])
                self.assertEqual("thread-root", runner.calls[2][1])
                self.assertTrue(runner.calls[2][-1])
                self.assertEqual("thread-root", runtime.core.codex_thread(subissue.issue_id))
                self.assertEqual(
                    ["issue_created", "first_observation", "followup_observation", "subissue_created", "recurrence_observation"],
                    [call[1] for call in collector.calls],
                )
            finally:
                runtime.close()
                database.close()

    def test_missing_codex_session_is_rebuilt_from_local_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            collector = FakeCollector(root)
            core = CoreService(database)
            issue = core.create_issue("reporter", "最初的现象")
            core.append_message(issue.issue_id, "codex", "assistant", "之前的分析", "system")
            core.save_codex_thread(issue.issue_id, "thread-missing")
            runner = MissingSessionRunner()
            runtime = RuntimeService(core, collector, runner, workers=1)
            try:
                runtime.append_message(issue.issue_id, "reporter", "reporter", "现在再次出现")
                runtime.wait_for_idle(timeout=3)
                self.assertEqual(["missing", "rebuild"], [call[0] for call in runner.calls])
                self.assertIn("最初的现象", runner.calls[1][2])
                self.assertIn("之前的分析", runner.calls[1][2])
                self.assertIn("现在再次出现", runner.calls[1][2])
                self.assertEqual("thread-rebuilt", core.codex_thread(issue.issue_id))
                self.assertEqual("重建后的分析", core.timeline(issue.issue_id)["messages"][-1].content)
            finally:
                runtime.close()
                database.close()

    def test_handoff_sends_snapshot_and_imports_gateway_solution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            collector = FakeCollector(root)
            gateway = FakeGateway()
            runtime = RuntimeService(CoreService(database), collector, None, gateway)
            try:
                issue = runtime.create_issue("reporter")
                runtime.append_message(issue.issue_id, "reporter", "reporter", "设备掉线")
                runtime.wait_for_idle(timeout=3)
                handed_off = runtime.request_handoff(issue.issue_id, "reporter")
                self.assertEqual("queued", handed_off.handoff_state)
                runtime.wait_for_idle(timeout=3)
                self.assertEqual(1, len(gateway.handoffs))
                self.assertEqual("设备掉线", gateway.handoffs[0][0]["summary"])
                self.assertEqual(issue.root_issue_id, gateway.handoffs[0][0]["root_issue_id"])
                self.assertEqual("delivered", runtime.timeline(issue.issue_id)["issue"].handoff_state)

                gateway.solution = {
                    "version": 3,
                    "engineer_id": "engineer-1",
                    "actual_solution": "重新插线并重启进程",
                    "verification_method": "现场操作确认",
                }
                timeline = runtime.timeline(issue.issue_id)
                self.assertEqual("pending_verification", timeline["issue"].status.value)
                self.assertEqual(3, timeline["solutions"][-1].version)
                self.assertEqual("重新插线并重启进程", timeline["solutions"][-1].content)

                reopened = runtime.report_verification_failure(
                    issue.issue_id, "reporter", "重启后仍然掉线"
                )
                self.assertEqual("open", reopened.status.value)
                runtime.wait_for_idle(timeout=3)
                self.assertEqual(
                    [(issue.issue_id, 3, "reporter", "重启后仍然掉线")],
                    gateway.verification_failures,
                )
            finally:
                runtime.close()
                database.close()

    def test_handoff_prevents_late_analysis_from_reaching_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            collector = FakeCollector(root)
            runtime = RuntimeService(CoreService(database), collector, FakeRunner(), workers=1)
            try:
                issue = runtime.create_issue("reporter")
                runtime.wait_for_idle(timeout=3)
                runtime.request_handoff(issue.issue_id, "reporter")
                runtime.append_message  # keep the public contract visible for static type tools
                runtime._capture_and_analyze(issue.issue_id, "late")
                self.assertEqual([], runtime.timeline(issue.issue_id)["messages"])
            finally:
                runtime.close()
                database.close()

    def test_configuring_gateway_delivers_handoff_queued_while_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = CoreDatabase(root / "db.sqlite3")
            collector = FakeCollector(root)
            runtime = RuntimeService(CoreService(database), collector, None, None, workers=1)
            try:
                issue = runtime.create_issue("reporter")
                runtime.append_message(issue.issue_id, "reporter", "reporter", "相机没有图像")
                runtime.wait_for_idle(timeout=3)
                runtime.request_handoff(issue.issue_id, "reporter")
                self.assertEqual("queued", runtime.core.get_issue(issue.issue_id).handoff_state)

                gateway = FakeGateway()
                runtime.configure(collector, None, gateway)
                runtime.wait_for_idle(timeout=3)

                self.assertEqual(1, len(gateway.handoffs))
                self.assertEqual(issue.issue_id, gateway.handoffs[0][0]["issue_id"])
                self.assertEqual("delivered", runtime.timeline(issue.issue_id)["issue"].handoff_state)
            finally:
                runtime.close()
                database.close()


if __name__ == "__main__":
    unittest.main()
