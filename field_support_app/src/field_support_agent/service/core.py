import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from field_support_agent.domain import (
    ConflictError,
    Event,
    ForbiddenError,
    Issue,
    IssueStatus,
    Message,
    NotFoundError,
    OutboxItem,
    Solution,
    TransitionReason,
    ValidationError,
    require_transition,
)
from field_support_agent.storage import CoreDatabase


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CoreService:
    MAX_TEXT_LENGTH = 1_000_000

    def __init__(self, database: CoreDatabase, clock: Clock = _utc_now):
        self.database = database
        self._clock = clock
        self.database.recover_in_flight_outbox(self._now())

    def create_issue(self, reporter_id: str, description: Optional[str] = None) -> Issue:
        reporter = self._required(reporter_id, "reporter_id", 256)
        body = self._optional_text(description, "description")
        now = self._now()
        with self.database.transaction() as connection:
            issue_id = self._new_root_id(connection, now)
            connection.execute(
                """INSERT INTO issues
                   (issue_id, root_issue_id, reporter_id, status, rollup_status, created_at, updated_at)
                   VALUES (?, ?, ?, 'open', 'open', ?, ?)""",
                (issue_id, issue_id, reporter, now, now),
            )
            self._event(connection, issue_id, "IssueCreated", reporter, "local", {}, now)
            if body is not None:
                self._message(connection, issue_id, reporter, "reporter", "local", body, now)
            self._event(connection, issue_id, "SnapshotRequested", "core", "local", {"trigger": "issue_created"}, now)
        return self._get_issue(issue_id)

    def create_subissue(
        self, selected_issue_id: str, reporter_id: str, description: Optional[str] = None
    ) -> Issue:
        selected_id = self._required(selected_issue_id, "selected_issue_id", 128)
        reporter = self._required(reporter_id, "reporter_id", 256)
        body = self._optional_text(description, "description")
        now = self._now()
        with self.database.transaction() as connection:
            selected = connection.execute("SELECT * FROM issues WHERE issue_id = ?", (selected_id,)).fetchone()
            if selected is None:
                raise NotFoundError("issue not found: {}".format(selected_id))
            root_id = selected["root_issue_id"]
            next_sequence = connection.execute(
                "SELECT COALESCE(MAX(sub_sequence), 0) + 1 FROM issues WHERE root_issue_id = ?",
                (root_id,),
            ).fetchone()[0]
            issue_id = "{}-S{:03d}".format(root_id, next_sequence)
            connection.execute(
                """INSERT INTO issues
                   (issue_id, root_issue_id, parent_issue_id, sub_sequence, reporter_id,
                    status, rollup_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'open', 'open', ?, ?)""",
                (issue_id, root_id, selected_id, next_sequence, reporter, now, now),
            )
            connection.execute(
                "UPDATE issues SET rollup_status = 'open', updated_at = ? WHERE issue_id = ?",
                (now, root_id),
            )
            self._event(
                connection,
                issue_id,
                "IssueReopened",
                reporter,
                "local",
                {"root_issue_id": root_id, "selected_issue_id": selected_id, "sub_sequence": next_sequence},
                now,
            )
            if body is not None:
                self._message(connection, issue_id, reporter, "reporter", "local", body, now)
            self._event(connection, issue_id, "SnapshotRequested", "core", "local", {"trigger": "subissue_created"}, now)
        return self._get_issue(issue_id)

    def append_message(
        self, issue_id: str, actor_id: str, role: str, content: str, channel: str = "local"
    ) -> Message:
        issue_key = self._required(issue_id, "issue_id", 128)
        actor = self._required(actor_id, "actor_id", 256)
        message_role = self._choice(role, "role", {"reporter", "assistant", "engineer", "system"})
        message_channel = self._choice(channel, "channel", {"local", "feishu", "system"})
        body = self._required(content, "content", self.MAX_TEXT_LENGTH)
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if message_channel == "local" and message_role == "reporter" and not issue["local_input_enabled"]:
                raise ConflictError("local input is disabled after human handoff")
            return self._message(connection, issue_key, actor, message_role, message_channel, body, now)

    def request_handoff(self, issue_id: str, actor_id: str) -> Issue:
        issue_key = self._required(issue_id, "issue_id", 128)
        actor = self._required(actor_id, "actor_id", 256)
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if issue["status"] == IssueStatus.CLOSED.value:
                raise ConflictError("closed issue cannot be handed off")
            if issue["handoff_state"] != "none":
                raise ConflictError("handoff already requested")
            connection.execute(
                """UPDATE issues SET handoff_state = 'queued', local_input_enabled = 0, updated_at = ?
                   WHERE issue_id = ?""",
                (now, issue_key),
            )
            self._event(connection, issue_key, "HandoffRequested", actor, "local", {}, now)
        return self._get_issue(issue_key)

    def mark_handoff_delivered(self, issue_id: str, actor_id: str = "gateway") -> Issue:
        issue_key = self._required(issue_id, "issue_id", 128)
        actor = self._required(actor_id, "actor_id", 256)
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if issue["handoff_state"] not in {"queued", "delivering"}:
                raise ConflictError("handoff is not waiting for delivery")
            connection.execute(
                "UPDATE issues SET handoff_state = 'delivered', updated_at = ? WHERE issue_id = ?",
                (now, issue_key),
            )
            self._event(connection, issue_key, "HandoffDelivered", actor, "system", {}, now)
        return self._get_issue(issue_key)

    def submit_solution(
        self,
        issue_id: str,
        submitted_by: str,
        content: str,
        verification_method: Optional[str] = None,
        source: str = "local",
    ) -> Solution:
        issue_key = self._required(issue_id, "issue_id", 128)
        submitter = self._required(submitted_by, "submitted_by", 256)
        body = self._required(content, "content", self.MAX_TEXT_LENGTH)
        verification = self._optional_text(verification_method, "verification_method")
        solution_source = self._choice(source, "source", {"local", "feishu", "system"})
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if issue["status"] == IssueStatus.CLOSED.value:
                raise ConflictError("closed issue cannot receive a solution")
            require_transition(
                IssueStatus(issue["status"]),
                IssueStatus.PENDING_VERIFICATION,
                TransitionReason.SOLUTION_SUBMITTED,
            )
            version = issue["latest_solution_version"] + 1
            solution_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO solutions
                   (solution_id, issue_id, version, submitted_by, content, verification_method, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (solution_id, issue_key, version, submitter, body, verification, solution_source, now),
            )
            connection.execute(
                """UPDATE issues SET status = 'pending_verification', latest_solution_version = ?, updated_at = ?
                   WHERE issue_id = ?""",
                (version, now, issue_key),
            )
            self._recalculate_rollup(connection, issue["root_issue_id"], now)
            self._event(
                connection,
                issue_key,
                "SolutionSubmitted",
                submitter,
                solution_source,
                {"solution_id": solution_id, "version": version, "verification_method": verification},
                now,
            )
        return Solution(solution_id, issue_key, version, submitter, body, verification, solution_source, now)

    def import_solution(
        self,
        issue_id: str,
        version: int,
        submitted_by: str,
        content: str,
        verification_method: Optional[str] = None,
    ) -> Solution:
        """Import the Gateway's authoritative solution version idempotently."""
        issue_key = self._required(issue_id, "issue_id", 128)
        submitter = self._required(submitted_by, "submitted_by", 256)
        body = self._required(content, "content", self.MAX_TEXT_LENGTH)
        verification = self._optional_text(verification_method, "verification_method")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValidationError("version must be a positive integer")
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            existing = connection.execute(
                "SELECT * FROM solutions WHERE issue_id = ? AND version = ?", (issue_key, version)
            ).fetchone()
            if existing is not None:
                return Solution(**dict(existing))
            if issue["status"] == IssueStatus.CLOSED.value:
                raise ConflictError("closed issue cannot receive a solution")
            if version <= issue["latest_solution_version"]:
                raise ConflictError("solution version is stale")
            solution_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO solutions
                   (solution_id, issue_id, version, submitted_by, content, verification_method, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'feishu', ?)""",
                (solution_id, issue_key, version, submitter, body, verification, now),
            )
            connection.execute(
                """UPDATE issues SET status = 'pending_verification', latest_solution_version = ?, updated_at = ?
                   WHERE issue_id = ?""",
                (version, now, issue_key),
            )
            self._recalculate_rollup(connection, issue["root_issue_id"], now)
            self._event(
                connection,
                issue_key,
                "SolutionSubmitted",
                submitter,
                "feishu",
                {"solution_id": solution_id, "version": version, "verification_method": verification},
                now,
            )
        return Solution(solution_id, issue_key, version, submitter, body, verification, "feishu", now)

    def report_verification_failure(self, issue_id: str, reporter_id: str, observation: str) -> Issue:
        issue_key = self._required(issue_id, "issue_id", 128)
        reporter = self._required(reporter_id, "reporter_id", 256)
        failure = self._required(observation, "observation", self.MAX_TEXT_LENGTH)
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if issue["reporter_id"] != reporter:
                raise ForbiddenError("only this report's reporter may report verification failure")
            if issue["status"] != IssueStatus.PENDING_VERIFICATION.value:
                raise ConflictError("issue is not pending verification")
            require_transition(
                IssueStatus(issue["status"]), IssueStatus.OPEN, TransitionReason.VERIFICATION_FAILED
            )
            connection.execute(
                "UPDATE issues SET status = 'open', updated_at = ? WHERE issue_id = ?", (now, issue_key)
            )
            self._recalculate_rollup(connection, issue["root_issue_id"], now)
            self._event(
                connection,
                issue_key,
                "IssueReopened",
                reporter,
                "local",
                {"reason": "verification_failed", "observation": failure},
                now,
            )
        return self._get_issue(issue_key)

    def confirm_solution(self, issue_id: str, reporter_id: str, expected_solution_version: int) -> Issue:
        issue_key = self._required(issue_id, "issue_id", 128)
        reporter = self._required(reporter_id, "reporter_id", 256)
        if isinstance(expected_solution_version, bool) or not isinstance(expected_solution_version, int):
            raise ValidationError("expected_solution_version must be an integer")
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if issue["reporter_id"] != reporter:
                raise ForbiddenError("only this report's reporter may confirm the solution")
            if issue["status"] != IssueStatus.PENDING_VERIFICATION.value:
                raise ConflictError("issue is not pending verification")
            if issue["latest_solution_version"] != expected_solution_version:
                raise ConflictError("solution version is stale")
            require_transition(
                IssueStatus(issue["status"]), IssueStatus.CLOSED, TransitionReason.REPORTER_CONFIRMED
            )
            connection.execute(
                "UPDATE issues SET status = 'closed', updated_at = ? WHERE issue_id = ?", (now, issue_key)
            )
            self._recalculate_rollup(connection, issue["root_issue_id"], now)
            self._event(
                connection,
                issue_key,
                "ReporterConfirmed",
                reporter,
                "local",
                {"solution_version": expected_solution_version},
                now,
            )
        return self._get_issue(issue_key)

    def confirm_ai_resolution(self, issue_id: str, reporter_id: str) -> Issue:
        issue_key = self._required(issue_id, "issue_id", 128)
        reporter = self._required(reporter_id, "reporter_id", 256)
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            if issue["reporter_id"] != reporter:
                raise ForbiddenError("only this report's reporter may confirm the solution")
            if issue["status"] != IssueStatus.OPEN.value:
                raise ConflictError("only an open issue may confirm an AI resolution")
            if issue["handoff_state"] != "none":
                raise ConflictError("AI resolution cannot be confirmed after human handoff")
            latest = connection.execute(
                """SELECT content FROM messages
                   WHERE issue_id = ? AND role = 'assistant' AND actor_id = 'codex'
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (issue_key,),
            ).fetchone()
            if latest is None:
                raise ConflictError("issue has no Codex conclusion to confirm")

            require_transition(
                IssueStatus.OPEN,
                IssueStatus.PENDING_VERIFICATION,
                TransitionReason.SOLUTION_SUBMITTED,
            )
            require_transition(
                IssueStatus.PENDING_VERIFICATION,
                IssueStatus.CLOSED,
                TransitionReason.REPORTER_CONFIRMED,
            )
            version = issue["latest_solution_version"] + 1
            solution_id = str(uuid.uuid4())
            verification = "现场人员确认 AI 建议已解决问题"
            connection.execute(
                """INSERT INTO solutions
                   (solution_id, issue_id, version, submitted_by, content, verification_method, source, created_at)
                   VALUES (?, ?, ?, 'codex', ?, ?, 'system', ?)""",
                (solution_id, issue_key, version, latest["content"], verification, now),
            )
            connection.execute(
                """UPDATE issues SET status = 'closed', latest_solution_version = ?, updated_at = ?
                   WHERE issue_id = ?""",
                (version, now, issue_key),
            )
            self._recalculate_rollup(connection, issue["root_issue_id"], now)
            self._event(
                connection,
                issue_key,
                "SolutionSubmitted",
                "codex",
                "system",
                {"solution_id": solution_id, "version": version, "verification_method": verification},
                now,
            )
            self._event(
                connection,
                issue_key,
                "ReporterConfirmed",
                reporter,
                "local",
                {"solution_version": version},
                now,
            )
        return self._get_issue(issue_key)

    def get_issue(self, issue_id: str) -> Issue:
        return self._get_issue(self._required(issue_id, "issue_id", 128))

    def list_issues(self) -> List[Issue]:
        return self.database.list_issues()

    def delete_issues(self, issue_ids: List[str]) -> List[str]:
        if not isinstance(issue_ids, list) or not issue_ids or len(issue_ids) > 1000:
            raise ValidationError("issue_ids must contain between 1 and 1000 items")
        requested = {self._required(item, "issue_id", 128) for item in issue_ids}
        with self.database.transaction() as connection:
            placeholders = ",".join("?" for _ in requested)
            rows = connection.execute(
                "SELECT issue_id, root_issue_id FROM issues WHERE issue_id IN ({})".format(placeholders),
                tuple(requested),
            ).fetchall()
            found = {row["issue_id"] for row in rows}
            missing = requested - found
            if missing:
                raise NotFoundError("issue not found: {}".format(sorted(missing)[0]))

            targets = set(found)
            selected_roots = {row["issue_id"] for row in rows if row["issue_id"] == row["root_issue_id"]}
            if selected_roots:
                root_placeholders = ",".join("?" for _ in selected_roots)
                targets.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT issue_id FROM issues WHERE root_issue_id IN ({})".format(root_placeholders),
                        tuple(selected_roots),
                    ).fetchall()
                )

            while True:
                target_placeholders = ",".join("?" for _ in targets)
                descendants = {
                    row[0]
                    for row in connection.execute(
                        "SELECT issue_id FROM issues WHERE parent_issue_id IN ({})".format(
                            target_placeholders
                        ),
                        tuple(targets),
                    ).fetchall()
                }
                expanded = targets | descendants
                if expanded == targets:
                    break
                targets = expanded

            target_placeholders = ",".join("?" for _ in targets)
            target_values = tuple(targets)
            affected_roots = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT root_issue_id FROM issues WHERE issue_id IN ({})".format(
                        target_placeholders
                    ),
                    target_values,
                ).fetchall()
            }
            for table in ("outbox", "events", "messages", "solutions"):
                connection.execute(
                    "DELETE FROM {} WHERE issue_id IN ({})".format(table, target_placeholders),
                    target_values,
                )
            if selected_roots:
                root_placeholders = ",".join("?" for _ in selected_roots)
                connection.execute(
                    "DELETE FROM codex_sessions WHERE root_issue_id IN ({})".format(root_placeholders),
                    tuple(selected_roots),
                )
            connection.execute(
                "DELETE FROM issues WHERE issue_id IN ({}) AND issue_id <> root_issue_id".format(
                    target_placeholders
                ),
                target_values,
            )
            connection.execute(
                "DELETE FROM issues WHERE issue_id IN ({})".format(target_placeholders), target_values
            )
            now = self._now()
            for root_id in affected_roots - selected_roots:
                self._recalculate_rollup(connection, root_id, now)
        self.database.compact()
        return sorted(targets)

    def issue_summary(self, issue_id: str) -> str:
        issue_key = self._required(issue_id, "issue_id", 128)
        messages = self.database.list_messages(issue_key)
        for message in messages:
            if message.role == "reporter":
                return message.content[:200]
        return "等待描述问题"

    def timeline(self, issue_id: str) -> Dict[str, Any]:
        issue = self.get_issue(issue_id)
        return {
            "issue": issue,
            "messages": self.database.list_messages(issue.issue_id),
            "solutions": self.database.list_solutions(issue.issue_id),
            "events": self.database.list_events(issue.issue_id),
        }

    def codex_thread(self, issue_id: str) -> Optional[str]:
        issue = self.get_issue(issue_id)
        return self.database.get_codex_thread(issue.root_issue_id)

    def forget_codex_thread(self, thread_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM codex_sessions WHERE thread_id = ?", (thread_id,))

    def save_codex_thread(self, issue_id: str, thread_id: str, *, rebuilt: bool = False) -> None:
        issue_key = self._required(issue_id, "issue_id", 128)
        thread_key = self._required(thread_id, "thread_id", 256)
        now = self._now()
        with self.database.transaction() as connection:
            issue = self._issue_row(connection, issue_key)
            root_id = issue["root_issue_id"]
            existing = connection.execute(
                "SELECT thread_id FROM codex_sessions WHERE root_issue_id = ?", (root_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO codex_sessions (root_issue_id, thread_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (root_id, thread_key, now, now),
                )
            else:
                connection.execute(
                    "UPDATE codex_sessions SET thread_id = ?, updated_at = ? WHERE root_issue_id = ?",
                    (thread_key, now, root_id),
                )
            self._event(
                connection,
                issue_key,
                "CodexConversationRebuilt" if rebuilt or existing is not None else "CodexConversationStarted",
                "core",
                "system",
                {"root_issue_id": root_id},
                now,
            )

    def claim_outbox(self, limit: int = 100) -> List[OutboxItem]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        now = self._now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT outbox_id FROM outbox
                   WHERE status = 'pending' AND available_at <= ?
                   ORDER BY created_at, rowid LIMIT ?""",
                (now, limit),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    "UPDATE outbox SET status = 'in_flight', attempts = attempts + 1, updated_at = ? "
                    "WHERE outbox_id IN ({})".format(placeholders),
                    (now, *ids),
                )
                claimed_rows = connection.execute(
                    "SELECT * FROM outbox WHERE outbox_id IN ({})".format(placeholders), ids
                ).fetchall()
            else:
                claimed_rows = []
        by_id = {row["outbox_id"]: self.database._outbox(row) for row in claimed_rows}
        return [by_id[item_id] for item_id in ids]

    def acknowledge_outbox(self, outbox_id: str) -> None:
        key = self._required(outbox_id, "outbox_id", 128)
        now = self._now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status = 'sent', updated_at = ?, last_error = NULL
                   WHERE outbox_id = ? AND status = 'in_flight'""",
                (now, key),
            )
            if cursor.rowcount != 1:
                raise ConflictError("outbox item is not in flight")

    def retry_outbox(self, outbox_id: str, available_at: str, error: str) -> None:
        key = self._required(outbox_id, "outbox_id", 128)
        available = self._required(available_at, "available_at", 64)
        self._validate_timestamp(available, "available_at")
        failure = self._required(error, "error", 4096)
        now = self._now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status = 'pending', available_at = ?, updated_at = ?, last_error = ?
                   WHERE outbox_id = ? AND status = 'in_flight'""",
                (available, now, failure, key),
            )
            if cursor.rowcount != 1:
                raise ConflictError("outbox item is not in flight")

    def _message(
        self,
        connection: sqlite3.Connection,
        issue_id: str,
        actor_id: str,
        role: str,
        channel: str,
        content: str,
        now: str,
    ) -> Message:
        message_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO messages (message_id, issue_id, actor_id, role, channel, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message_id, issue_id, actor_id, role, channel, content, now),
        )
        self._event(
            connection,
            issue_id,
            "MessageAppended",
            actor_id,
            channel,
            {"message_id": message_id, "role": role, "content": content},
            now,
        )
        return Message(message_id, issue_id, actor_id, role, channel, content, now)

    def _event(
        self,
        connection: sqlite3.Connection,
        issue_id: str,
        kind: str,
        actor_id: str,
        channel: str,
        payload: Dict[str, Any],
        now: str,
    ) -> str:
        event_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        connection.execute(
            """INSERT INTO events
               (schema_version, event_id, issue_id, kind, actor_id, channel, occurred_at, payload_json)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, issue_id, kind, actor_id, channel, now, payload_json),
        )
        envelope = {
            "schema_version": 1,
            "event_id": event_id,
            "issue_id": issue_id,
            "kind": kind,
            "actor_id": actor_id,
            "channel": channel,
            "occurred_at": now,
            "payload": payload,
        }
        connection.execute(
            """INSERT INTO outbox
               (outbox_id, event_id, issue_id, kind, payload_json, status, attempts,
                available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                event_id,
                issue_id,
                kind,
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                now,
                now,
                now,
            ),
        )
        return event_id

    @staticmethod
    def _issue_row(connection: sqlite3.Connection, issue_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        if row is None:
            raise NotFoundError("issue not found: {}".format(issue_id))
        return row

    def _get_issue(self, issue_id: str) -> Issue:
        issue = self.database.get_issue(issue_id)
        if issue is None:
            raise NotFoundError("issue not found: {}".format(issue_id))
        return issue

    @staticmethod
    def _recalculate_rollup(connection: sqlite3.Connection, root_id: str, now: str) -> None:
        statuses = [
            row[0]
            for row in connection.execute("SELECT status FROM issues WHERE root_issue_id = ?", (root_id,)).fetchall()
        ]
        if IssueStatus.OPEN.value in statuses:
            rollup = IssueStatus.OPEN.value
        elif IssueStatus.PENDING_VERIFICATION.value in statuses:
            rollup = IssueStatus.PENDING_VERIFICATION.value
        else:
            rollup = IssueStatus.CLOSED.value
        connection.execute(
            "UPDATE issues SET rollup_status = ?, updated_at = ? WHERE issue_id = ?", (rollup, now, root_id)
        )

    def _new_root_id(self, connection: sqlite3.Connection, now: str) -> str:
        date = now[:10].replace("-", "")
        for _ in range(20):
            issue_id = "ISS-{}-{}".format(date, uuid.uuid4().hex[:8].upper())
            if connection.execute("SELECT 1 FROM issues WHERE issue_id = ?", (issue_id,)).fetchone() is None:
                return issue_id
        raise RuntimeError("could not allocate a unique issue id")

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _validate_timestamp(value: str, field: str) -> None:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValidationError("{} must be an RFC3339 timestamp".format(field)) from exc
        if parsed.tzinfo is None:
            raise ValidationError("{} must include a timezone".format(field))

    @staticmethod
    def _required(value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("{} must be a non-empty string".format(field))
        cleaned = value.strip()
        if len(cleaned) > maximum:
            raise ValidationError("{} is too long".format(field))
        return cleaned

    def _optional_text(self, value: Optional[str], field: str) -> Optional[str]:
        if value is None:
            return None
        return self._required(value, field, self.MAX_TEXT_LENGTH)

    @staticmethod
    def _choice(value: Any, field: str, choices: set) -> str:
        if not isinstance(value, str) or value not in choices:
            raise ValidationError("{} must be one of {}".format(field, sorted(choices)))
        return value
