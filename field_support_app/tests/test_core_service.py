import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from field_support_agent.domain import (
    ConflictError,
    ForbiddenError,
    IssueStatus,
    NotFoundError,
    TransitionReason,
    ValidationError,
    require_transition,
)
from field_support_agent.service import CoreService
from field_support_agent.storage import CoreDatabase


FIXED_TIME = datetime(2026, 9, 15, 8, 30, tzinfo=timezone.utc)


class CoreServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "core.sqlite3"
        self.database = CoreDatabase(self.database_path)
        self.service = CoreService(self.database, clock=lambda: FIXED_TIME)

    def tearDown(self):
        self.database.close()
        self.temporary_directory.cleanup()

    def test_database_enables_wal_and_foreign_keys(self):
        self.assertEqual("wal", self.database.journal_mode())
        with self.database.transaction() as connection:
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def test_create_issue_is_atomic_and_enqueues_every_event(self):
        issue = self.service.create_issue("site-user", "camera is missing")
        self.assertRegex(issue.issue_id, r"^ISS-20260915-[0-9A-F]{8}$")
        self.assertEqual(IssueStatus.OPEN, issue.status)
        timeline = self.service.timeline(issue.issue_id)
        self.assertEqual(["IssueCreated", "MessageAppended", "SnapshotRequested"], [e.kind for e in timeline["events"]])
        self.assertEqual(3, len(self.database.list_outbox("pending")))

    def test_subids_are_allocated_under_root_even_when_selected_from_subissue(self):
        root = self.service.create_issue("reporter")
        first = self.service.create_subissue(root.issue_id, "reporter-a")
        second = self.service.create_subissue(first.issue_id, "reporter-b")
        self.assertEqual(root.issue_id + "-S001", first.issue_id)
        self.assertEqual(root.issue_id + "-S002", second.issue_id)
        self.assertEqual(first.issue_id, second.parent_issue_id)
        self.assertEqual(root.issue_id, second.root_issue_id)

    def test_solution_confirmation_requires_current_reporter_and_latest_version(self):
        issue = self.service.create_issue("reporter")
        first = self.service.submit_solution(issue.issue_id, "engineer", "restart the process")
        second = self.service.submit_solution(issue.issue_id, "engineer", "replace the cable")
        self.assertEqual(1, first.version)
        self.assertEqual(2, second.version)
        with self.assertRaises(ForbiddenError):
            self.service.confirm_solution(issue.issue_id, "someone-else", 2)
        with self.assertRaises(ConflictError):
            self.service.confirm_solution(issue.issue_id, "reporter", 1)
        closed = self.service.confirm_solution(issue.issue_id, "reporter", 2)
        self.assertEqual(IssueStatus.CLOSED, closed.status)
        self.assertEqual(IssueStatus.CLOSED, closed.root_rollup_status)

    def test_reporter_can_close_open_issue_with_latest_codex_conclusion(self):
        issue = self.service.create_issue("reporter", "camera is missing")
        self.service.append_message(issue.issue_id, "codex", "assistant", "重新插线后重启相机服务", "system")

        closed = self.service.confirm_ai_resolution(issue.issue_id, "reporter")

        self.assertEqual(IssueStatus.CLOSED, closed.status)
        timeline = self.service.timeline(issue.issue_id)
        self.assertEqual("重新插线后重启相机服务", timeline["solutions"][-1].content)
        self.assertEqual("codex", timeline["solutions"][-1].submitted_by)
        self.assertEqual("system", timeline["solutions"][-1].source)
        self.assertEqual(["SolutionSubmitted", "ReporterConfirmed"], [item.kind for item in timeline["events"][-2:]])

    def test_ai_resolution_requires_codex_reply_and_no_human_handoff(self):
        issue = self.service.create_issue("reporter")
        self.service.append_message(issue.issue_id, "system", "assistant", "analysis unavailable", "system")
        with self.assertRaises(ConflictError):
            self.service.confirm_ai_resolution(issue.issue_id, "reporter")

        self.service.append_message(issue.issue_id, "codex", "assistant", "restart", "system")
        self.service.request_handoff(issue.issue_id, "reporter")
        with self.assertRaises(ConflictError):
            self.service.confirm_ai_resolution(issue.issue_id, "reporter")

    def test_verification_failure_requires_reporter_and_observation(self):
        issue = self.service.create_issue("reporter")
        self.service.submit_solution(issue.issue_id, "engineer", "restart")
        with self.assertRaises(ForbiddenError):
            self.service.report_verification_failure(issue.issue_id, "other", "still broken")
        reopened = self.service.report_verification_failure(issue.issue_id, "reporter", "still broken")
        self.assertEqual(IssueStatus.OPEN, reopened.status)
        self.assertEqual("verification_failed", self.service.timeline(issue.issue_id)["events"][-1].payload["reason"])

    def test_reoccurrence_preserves_closed_issue_and_changes_root_rollup(self):
        root = self.service.create_issue("first-reporter")
        self.service.submit_solution(root.issue_id, "engineer", "restart")
        self.service.confirm_solution(root.issue_id, "first-reporter", 1)
        subissue = self.service.create_subissue(root.issue_id, "second-reporter", "it happened again")
        refreshed_root = self.service.get_issue(root.issue_id)
        self.assertEqual(IssueStatus.CLOSED, refreshed_root.status)
        self.assertEqual(IssueStatus.OPEN, refreshed_root.root_rollup_status)
        self.service.submit_solution(subissue.issue_id, "engineer", "replace cable")
        self.service.confirm_solution(subissue.issue_id, "second-reporter", 1)
        self.assertEqual(IssueStatus.CLOSED, self.service.get_issue(root.issue_id).root_rollup_status)

    def test_handoff_disables_reporter_input(self):
        issue = self.service.create_issue("reporter")
        handed_off = self.service.request_handoff(issue.issue_id, "reporter")
        self.assertFalse(handed_off.local_input_enabled)
        self.assertEqual("queued", handed_off.handoff_state)
        with self.assertRaises(ConflictError):
            self.service.append_message(issue.issue_id, "reporter", "reporter", "more details")
        delivered = self.service.mark_handoff_delivered(issue.issue_id)
        self.assertEqual("delivered", delivered.handoff_state)

    def test_outbox_claim_ack_and_retry_are_serialized(self):
        self.service.create_issue("reporter")
        claimed = self.service.claim_outbox(2)
        self.assertEqual(2, len(claimed))
        self.assertTrue(all(item.attempts == 1 for item in claimed))
        self.service.acknowledge_outbox(claimed[0].outbox_id)
        self.service.retry_outbox(claimed[1].outbox_id, "2026-09-15T09:00:00Z", "offline")
        self.assertEqual("sent", self.database.list_outbox()[0].status)
        self.assertEqual("pending", self.database.list_outbox()[1].status)

    def test_concurrent_subissue_allocation_is_unique(self):
        root = self.service.create_issue("reporter")
        created = []
        errors = []

        def create(index):
            try:
                created.append(self.service.create_subissue(root.issue_id, "reporter-{}".format(index)).issue_id)
            except Exception as exc:  # pragma: no cover - assertion reports the exception
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(12, len(set(created)))
        self.assertEqual(root.issue_id + "-S012", sorted(created)[-1])

    def test_records_survive_database_reopen(self):
        issue = self.service.create_issue("reporter", "persistent report")
        self.service.save_codex_thread(issue.issue_id, "thread-persistent")
        self.database.close()
        self.database = CoreDatabase(self.database_path)
        self.service = CoreService(self.database, clock=lambda: FIXED_TIME)
        self.assertEqual("persistent report", self.service.timeline(issue.issue_id)["messages"][0].content)
        self.assertEqual("thread-persistent", self.service.codex_thread(issue.issue_id))

    def test_subissue_uses_root_codex_thread(self):
        root = self.service.create_issue("reporter")
        self.service.save_codex_thread(root.issue_id, "thread-shared")
        subissue = self.service.create_subissue(root.issue_id, "reporter")
        self.assertEqual("thread-shared", self.service.codex_thread(subissue.issue_id))

    def test_delete_subissue_removes_descendants_and_keeps_root(self):
        root = self.service.create_issue("reporter", "root")
        self.service.save_codex_thread(root.issue_id, "thread-shared")
        first = self.service.create_subissue(root.issue_id, "reporter", "first")
        second = self.service.create_subissue(first.issue_id, "reporter", "second")

        deleted = self.service.delete_issues([first.issue_id])

        self.assertEqual({first.issue_id, second.issue_id}, set(deleted))
        self.assertEqual(root.issue_id, self.service.get_issue(root.issue_id).issue_id)
        self.assertEqual("thread-shared", self.service.codex_thread(root.issue_id))
        with self.assertRaises(NotFoundError):
            self.service.get_issue(first.issue_id)

    def test_delete_root_removes_complete_issue_family_and_related_rows(self):
        root = self.service.create_issue("reporter", "root")
        subissue = self.service.create_subissue(root.issue_id, "reporter", "again")
        self.service.submit_solution(subissue.issue_id, "engineer", "restart", "run once")
        self.service.save_codex_thread(root.issue_id, "thread-shared")

        deleted = self.service.delete_issues([root.issue_id])

        self.assertEqual({root.issue_id, subissue.issue_id}, set(deleted))
        with self.database.transaction() as connection:
            for table in ("issues", "messages", "solutions", "events", "outbox", "codex_sessions"):
                self.assertEqual(0, connection.execute("SELECT count(*) FROM {}".format(table)).fetchone()[0])

    def test_outbox_in_flight_items_are_recovered_after_restart(self):
        self.service.create_issue("reporter")
        claimed_id = self.service.claim_outbox(1)[0].outbox_id
        self.database.close()
        self.database = CoreDatabase(self.database_path)
        self.service = CoreService(self.database, clock=lambda: FIXED_TIME)
        recovered = {item.outbox_id: item for item in self.database.list_outbox()}
        self.assertEqual("pending", recovered[claimed_id].status)
        self.assertEqual("recovered after core restart", recovered[claimed_id].last_error)

    def test_state_machine_forbids_direct_open_to_closed(self):
        with self.assertRaises(ConflictError):
            require_transition(IssueStatus.OPEN, IssueStatus.CLOSED, TransitionReason.REPORTER_CONFIRMED)

    def test_retry_outbox_requires_timezone_aware_timestamp(self):
        self.service.create_issue("reporter")
        claimed = self.service.claim_outbox(1)[0]
        with self.assertRaises(ValidationError):
            self.service.retry_outbox(claimed.outbox_id, "tomorrow", "offline")


if __name__ == "__main__":
    unittest.main()
