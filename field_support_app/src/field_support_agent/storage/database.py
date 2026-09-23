import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from field_support_agent.domain import Event, Issue, IssueStatus, Message, OutboxItem, Solution


SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    root_issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    parent_issue_id TEXT REFERENCES issues(issue_id),
    sub_sequence INTEGER,
    reporter_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'pending_verification', 'closed')),
    rollup_status TEXT NOT NULL CHECK(rollup_status IN ('open', 'pending_verification', 'closed')),
    latest_solution_version INTEGER NOT NULL DEFAULT 0,
    local_input_enabled INTEGER NOT NULL DEFAULT 1 CHECK(local_input_enabled IN (0, 1)),
    handoff_state TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((issue_id = root_issue_id AND sub_sequence IS NULL) OR
          (issue_id <> root_issue_id AND sub_sequence IS NOT NULL AND sub_sequence > 0)),
    UNIQUE(root_issue_id, sub_sequence)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    channel TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solutions (
    solution_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    version INTEGER NOT NULL CHECK(version > 0),
    submitted_by TEXT NOT NULL,
    content TEXT NOT NULL,
    verification_method TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(issue_id, version)
);

CREATE TABLE IF NOT EXISTS codex_sessions (
    root_issue_id TEXT PRIMARY KEY REFERENCES issues(issue_id),
    thread_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'in_flight', 'sent')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_issues_root ON issues(root_issue_id, sub_sequence);
CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_issue ON messages(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, available_at, created_at);
"""


class CoreDatabase:
    """One serialized SQLite connection used as the Core's only writer."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(SCHEMA)
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise RuntimeError("SQLite foreign keys could not be enabled")
            schema_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version == 0:
                self._connection.execute("PRAGMA user_version = 1")
            elif schema_version != 1:
                raise RuntimeError("unsupported database schema version: {}".format(schema_version))
        if self.path != Path(":memory:"):
            os.chmod(str(self.path), 0o600)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def compact(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("VACUUM")

    def journal_mode(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def get_issue(self, issue_id: str) -> Optional[Issue]:
        with self._lock:
            row = self._connection.execute(
                """SELECT i.*, root.rollup_status AS root_rollup_status
                   FROM issues i JOIN issues root ON root.issue_id = i.root_issue_id
                   WHERE i.issue_id = ?""",
                (issue_id,),
            ).fetchone()
        return self._issue(row) if row else None

    def list_issues(self) -> List[Issue]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT i.*, root.rollup_status AS root_rollup_status
                   FROM issues i JOIN issues root ON root.issue_id = i.root_issue_id
                   ORDER BY i.created_at DESC, i.issue_id DESC"""
            ).fetchall()
        return [self._issue(row) for row in rows]

    def list_events(self, issue_id: str) -> List[Event]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE issue_id = ? ORDER BY sequence", (issue_id,)
            ).fetchall()
        return [self._event(row) for row in rows]

    def list_messages(self, issue_id: str) -> List[Message]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE issue_id = ? ORDER BY created_at, rowid", (issue_id,)
            ).fetchall()
        return [Message(**dict(row)) for row in rows]

    def list_solutions(self, issue_id: str) -> List[Solution]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM solutions WHERE issue_id = ? ORDER BY version", (issue_id,)
            ).fetchall()
        return [Solution(**dict(row)) for row in rows]

    def get_codex_thread(self, root_issue_id: str) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT thread_id FROM codex_sessions WHERE root_issue_id = ?", (root_issue_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def list_outbox(self, status: Optional[str] = None) -> List[OutboxItem]:
        query = "SELECT * FROM outbox"
        params = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at, rowid"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._outbox(row) for row in rows]

    def recover_in_flight_outbox(self, recovered_at: str) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE outbox
                   SET status = 'pending', available_at = ?, updated_at = ?,
                       last_error = 'recovered after core restart'
                   WHERE status = 'in_flight'""",
                (recovered_at, recovered_at),
            )
            return cursor.rowcount

    @staticmethod
    def _issue(row: sqlite3.Row) -> Issue:
        return Issue(
            issue_id=row["issue_id"],
            root_issue_id=row["root_issue_id"],
            parent_issue_id=row["parent_issue_id"],
            sub_sequence=row["sub_sequence"],
            reporter_id=row["reporter_id"],
            status=IssueStatus(row["status"]),
            root_rollup_status=IssueStatus(row["root_rollup_status"]),
            latest_solution_version=row["latest_solution_version"],
            local_input_enabled=bool(row["local_input_enabled"]),
            handoff_state=row["handoff_state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(
            sequence=row["sequence"],
            schema_version=row["schema_version"],
            event_id=row["event_id"],
            issue_id=row["issue_id"],
            kind=row["kind"],
            actor_id=row["actor_id"],
            channel=row["channel"],
            occurred_at=row["occurred_at"],
            payload=json.loads(row["payload_json"]),
        )

    @staticmethod
    def _outbox(row: sqlite3.Row) -> OutboxItem:
        return OutboxItem(
            outbox_id=row["outbox_id"],
            event_id=row["event_id"],
            issue_id=row["issue_id"],
            kind=row["kind"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            available_at=row["available_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )
