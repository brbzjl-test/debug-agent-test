"""SQLite event store and materialized gateway state."""

import contextlib
import datetime as dt
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .auth import token_digest
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class GatewayStore:
    def __init__(self, path: str) -> None:
        db_path = Path(path)
        if path != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _configure(self) -> None:
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self._conn.execute("PRAGMA database_list").fetchone()[2] != "":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engineers (
                    engineer_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id TEXT PRIMARY KEY,
                    root_issue_id TEXT,
                    reporter_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','pending_verification','closed')),
                    handoff_status TEXT NOT NULL,
                    current_solution_version INTEGER NOT NULL DEFAULT 0,
                    chat_id TEXT,
                    message_id TEXT,
                    topic_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                    kind TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_issue_seq ON events(issue_id, seq);
                CREATE TABLE IF NOT EXISTS handoffs (
                    idempotency_key TEXT PRIMARY KEY,
                    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS solutions (
                    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                    version INTEGER NOT NULL,
                    engineer_id TEXT NOT NULL REFERENCES engineers(engineer_id),
                    actual_solution TEXT NOT NULL,
                    verification_method TEXT,
                    submitted_at TEXT NOT NULL,
                    callback_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(issue_id, version)
                );
                CREATE TABLE IF NOT EXISTS callback_receipts (
                    callback_id TEXT PRIMARY KEY,
                    callback_type TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS base_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(issues)").fetchall()
            }
            if "root_issue_id" not in columns:
                self._conn.execute("ALTER TABLE issues ADD COLUMN root_issue_id TEXT")
            if "message_id" not in columns:
                self._conn.execute("ALTER TABLE issues ADD COLUMN message_id TEXT")
            rows = self._conn.execute(
                "SELECT issue_id, root_issue_id, message_id, topic_id FROM issues"
            ).fetchall()
            for row in rows:
                issue_id = str(row["issue_id"])
                root_issue_id = str(row["root_issue_id"] or "")
                if not root_issue_id:
                    marker = issue_id.rfind("-S")
                    root_issue_id = issue_id[:marker] if marker > 0 else issue_id
                message_id = str(row["message_id"] or row["topic_id"] or "")
                self._conn.execute(
                    "UPDATE issues SET root_issue_id=?, message_id=? WHERE issue_id=?",
                    (root_issue_id, message_id or None, issue_id),
                )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def register_device(self, device_id: str, token: str) -> None:
        if not device_id.strip() or not token:
            raise ValidationError("device_id and token are required")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO devices(device_id, token_digest, enabled, created_at)
                   VALUES(?, ?, 1, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     token_digest=excluded.token_digest, enabled=1""",
                (device_id, token_digest(token), now),
            )

    def authenticate_device(self, token: str) -> str:
        digest = token_digest(token)
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id, token_digest FROM devices WHERE enabled=1"
            ).fetchall()
        for row in rows:
            if secrets.compare_digest(row["token_digest"], digest):
                return str(row["device_id"])
        raise AuthenticationError("invalid device token")

    def register_engineer(self, engineer_id: str) -> None:
        if not engineer_id.strip():
            raise ValidationError("engineer_id is required")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO engineers(engineer_id, enabled, created_at)
                   VALUES(?, 1, ?)
                   ON CONFLICT(engineer_id) DO UPDATE SET enabled=1""",
                (engineer_id, utc_now()),
            )

    def require_engineer(self, engineer_id: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM engineers WHERE engineer_id=?", (engineer_id,)
            ).fetchone()
        if row is None or not row["enabled"]:
            raise AuthorizationError("callback actor is not an authorized engineer")

    def create_handoff(
        self,
        device_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        issue_id = str(payload.get("issue_id", "")).strip()
        root_issue_id = str(payload.get("root_issue_id") or issue_id).strip()
        reporter_id = str(payload.get("reporter_id", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        if not issue_id or not root_issue_id or not reporter_id or not summary:
            raise ValidationError("issue_id, root_issue_id, reporter_id and summary are required")
        if not idempotency_key.strip():
            raise ValidationError("Idempotency-Key is required")
        request_json = canonical_json(dict(payload))
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT request_hash, response_json FROM handoffs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ConflictError("idempotency key was used with different content")
                response = json.loads(existing["response_json"])
                response["replayed"] = True
                return response

            issue = conn.execute(
                "SELECT device_id, reporter_id, root_issue_id FROM issues WHERE issue_id=?",
                (issue_id,),
            ).fetchone()
            if issue is None:
                conn.execute(
                    """INSERT INTO issues(
                         issue_id, root_issue_id, reporter_id, device_id, status, handoff_status,
                         current_solution_version, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, 'open', 'queued', 0, ?, ?)""",
                    (issue_id, root_issue_id, reporter_id, device_id, now, now),
                )
            elif (
                issue["device_id"] != device_id
                or issue["reporter_id"] != reporter_id
                or str(issue["root_issue_id"] or issue_id) != root_issue_id
            ):
                raise ConflictError("issue identity conflicts with existing gateway state")
            else:
                conn.execute(
                    "UPDATE issues SET handoff_status='queued', updated_at=? WHERE issue_id=?",
                    (now, issue_id),
                )

            event = self._append_event_tx(
                conn,
                issue_id,
                "HandoffRequested",
                device_id,
                "device",
                dict(payload),
                now,
            )
            conn.execute(
                """INSERT INTO delivery_outbox(
                     issue_id, idempotency_key, payload_json, state, created_at, updated_at
                   ) VALUES(?, ?, ?, 'pending', ?, ?)""",
                (issue_id, idempotency_key, request_json, now, now),
            )
            response = {
                "issue_id": issue_id,
                "status": "open",
                "handoff_status": "queued",
                "event_seq": event["seq"],
                "replayed": False,
            }
            conn.execute(
                "INSERT INTO handoffs VALUES(?, ?, ?, ?, ?)",
                (idempotency_key, issue_id, request_hash, canonical_json(response), now),
            )
            return response

    def sync_issue(self, device_id: str, issue_id: str, after_seq: int) -> Dict[str, Any]:
        with self._lock:
            issue = self._conn.execute(
                "SELECT * FROM issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if issue is None:
                raise NotFoundError("issue does not exist")
            if issue["device_id"] != device_id:
                raise AuthorizationError("issue belongs to another device")
            rows = self._conn.execute(
                "SELECT * FROM events WHERE issue_id=? AND seq>? ORDER BY seq LIMIT 1000",
                (issue_id, max(0, after_seq)),
            ).fetchall()
            solution = self._conn.execute(
                """SELECT issue_id, version, engineer_id, actual_solution,
                          verification_method, submitted_at
                   FROM solutions WHERE issue_id=? ORDER BY version DESC LIMIT 1""",
                (issue_id,),
            ).fetchone()
            latest_seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS value FROM events WHERE issue_id=?",
                (issue_id,),
            ).fetchone()["value"]
        return {
            "issue": self._issue_dict(issue),
            "latest_solution": dict(solution) if solution is not None else None,
            "events": [self._event_dict(row) for row in rows],
            "cursor": int(latest_seq),
        }

    def submit_solution(
        self,
        callback_id: str,
        engineer_id: str,
        issue_id: str,
        actual_solution: str,
        verification_method: Optional[str],
        requested_version: Optional[int],
    ) -> Dict[str, Any]:
        actual_solution = actual_solution.strip()
        if not actual_solution:
            raise ValidationError("actual_solution must not be empty")
        self.require_engineer(engineer_id)
        now = utc_now()
        with self.transaction() as conn:
            receipt = conn.execute(
                "SELECT response_json FROM callback_receipts WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if receipt is not None:
                response = json.loads(receipt["response_json"])
                response["replayed"] = True
                return response
            issue = conn.execute(
                "SELECT * FROM issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if issue is None:
                raise NotFoundError("issue does not exist")
            next_version = int(issue["current_solution_version"]) + 1
            if requested_version is not None and requested_version != next_version:
                raise ConflictError("solution_version is not the next version")
            conn.execute(
                """INSERT INTO solutions(
                     issue_id, version, engineer_id, actual_solution,
                     verification_method, submitted_at, callback_id
                   ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    issue_id,
                    next_version,
                    engineer_id,
                    actual_solution,
                    verification_method.strip() if verification_method else None,
                    now,
                    callback_id,
                ),
            )
            conn.execute(
                """UPDATE issues SET status='pending_verification',
                     handoff_status='solution_available', current_solution_version=?,
                     updated_at=? WHERE issue_id=?""",
                (next_version, now, issue_id),
            )
            event = self._append_event_tx(
                conn,
                issue_id,
                "SolutionSubmitted",
                engineer_id,
                "feishu_card",
                {
                    "solution_version": next_version,
                    "actual_solution": actual_solution,
                    "verification_method": verification_method.strip()
                    if verification_method
                    else None,
                },
                now,
            )
            response = {
                "issue_id": issue_id,
                "status": "pending_verification",
                "solution_version": next_version,
                "event_seq": event["seq"],
                "replayed": False,
            }
            conn.execute(
                "INSERT INTO callback_receipts VALUES(?, 'card', ?, ?)",
                (callback_id, canonical_json(response), now),
            )
            return response

    def issue_id_for_message(self, message_id: str) -> str:
        if not message_id:
            raise ValidationError("message_id is required")
        with self._lock:
            row = self._conn.execute(
                "SELECT issue_id FROM issues WHERE message_id=?", (message_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("card message is not bound to an issue")
        return str(row["issue_id"])

    def confirm_solution(
        self,
        device_id: str,
        issue_id: str,
        solution_version: int,
        reporter_id: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if not idempotency_key.strip():
            raise ValidationError("Idempotency-Key is required")
        now = utc_now()
        callback_id = "device:" + idempotency_key
        with self.transaction() as conn:
            receipt = conn.execute(
                "SELECT response_json FROM callback_receipts WHERE callback_id=?", (callback_id,)
            ).fetchone()
            if receipt is not None:
                response = json.loads(receipt["response_json"])
                response["replayed"] = True
                return response
            issue = conn.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
            if issue is None:
                raise NotFoundError("issue does not exist")
            if issue["device_id"] != device_id or issue["reporter_id"] != reporter_id:
                raise AuthorizationError("issue identity does not match device report")
            if issue["status"] != "pending_verification":
                raise ConflictError("issue is not pending verification")
            if int(issue["current_solution_version"]) != solution_version:
                raise ConflictError("solution version is stale")
            conn.execute(
                "UPDATE issues SET status='closed', handoff_status='closed', updated_at=? WHERE issue_id=?",
                (now, issue_id),
            )
            event = self._append_event_tx(
                conn,
                issue_id,
                "ReporterConfirmed",
                reporter_id,
                "device",
                {"solution_version": solution_version},
                now,
            )
            response = {
                "issue_id": issue_id,
                "status": "closed",
                "event_seq": event["seq"],
                "replayed": False,
            }
            conn.execute(
                "INSERT INTO callback_receipts VALUES(?, 'device_confirm', ?, ?)",
                (callback_id, canonical_json(response), now),
            )
            return response

    def report_verification_failure(
        self,
        device_id: str,
        issue_id: str,
        solution_version: int,
        reporter_id: str,
        observation: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        observation = observation.strip()
        if not observation:
            raise ValidationError("observation must not be empty")
        if not idempotency_key.strip():
            raise ValidationError("Idempotency-Key is required")
        now = utc_now()
        callback_id = "device-failure:" + idempotency_key
        with self.transaction() as conn:
            receipt = conn.execute(
                "SELECT response_json FROM callback_receipts WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if receipt is not None:
                response = json.loads(receipt["response_json"])
                response["replayed"] = True
                return response
            issue = conn.execute(
                "SELECT * FROM issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if issue is None:
                raise NotFoundError("issue does not exist")
            if issue["device_id"] != device_id or issue["reporter_id"] != reporter_id:
                raise AuthorizationError("issue identity does not match device report")
            if issue["status"] != "pending_verification":
                raise ConflictError("issue is not pending verification")
            if int(issue["current_solution_version"]) != solution_version:
                raise ConflictError("solution version is stale")
            conn.execute(
                "UPDATE issues SET status='open', handoff_status='delivered', updated_at=? WHERE issue_id=?",
                (now, issue_id),
            )
            event = self._append_event_tx(
                conn,
                issue_id,
                "VerificationFailed",
                reporter_id,
                "device",
                {"solution_version": solution_version, "observation": observation},
                now,
            )
            response = {
                "issue_id": issue_id,
                "status": "open",
                "handoff_status": "delivered",
                "event_seq": event["seq"],
                "replayed": False,
            }
            conn.execute(
                "INSERT INTO callback_receipts VALUES(?, 'device_failure', ?, ?)",
                (callback_id, canonical_json(response), now),
            )
            return response

    def issue_card_context(self, issue_id: str) -> Dict[str, Any]:
        with self._lock:
            issue = self._conn.execute(
                "SELECT message_id FROM issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if issue is None:
                raise NotFoundError("issue does not exist")
            solution = self._conn.execute(
                """SELECT version, engineer_id, actual_solution, verification_method,
                          submitted_at
                   FROM solutions WHERE issue_id=? ORDER BY version DESC LIMIT 1""",
                (issue_id,),
            ).fetchone()
        message_id = str(issue["message_id"] or "")
        if not message_id:
            raise ConflictError("issue is not bound to a Feishu card")
        return {
            "message_id": message_id,
            "latest_solution": dict(solution) if solution is not None else None,
        }

    def topic_root_for(self, root_issue_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                """SELECT topic_id FROM issues
                   WHERE root_issue_id=? AND topic_id IS NOT NULL AND topic_id!=''
                   ORDER BY created_at, issue_id LIMIT 1""",
                (root_issue_id,),
            ).fetchone()
        return str(row["topic_id"]) if row is not None else None

    def record_feishu_event(
        self,
        callback_id: str,
        engineer_id: str,
        issue_id: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        allowed = {
            "EngineerClaimed": "handling",
            "FeishuMessageCaptured": None,
            "TopicBound": "delivered",
        }
        if kind not in allowed:
            raise ValidationError("unsupported Feishu event kind")
        self.require_engineer(engineer_id)
        now = utc_now()
        with self.transaction() as conn:
            receipt = conn.execute(
                "SELECT response_json FROM callback_receipts WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if receipt is not None:
                response = json.loads(receipt["response_json"])
                response["replayed"] = True
                return response
            issue = conn.execute(
                "SELECT issue_id FROM issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if issue is None:
                raise NotFoundError("issue does not exist")
            handoff_status = allowed[kind]
            if handoff_status:
                conn.execute(
                    "UPDATE issues SET handoff_status=?, updated_at=? WHERE issue_id=?",
                    (handoff_status, now, issue_id),
                )
            event = self._append_event_tx(
                conn, issue_id, kind, engineer_id, "feishu_event", dict(payload), now
            )
            response = {
                "issue_id": issue_id,
                "accepted": True,
                "event_seq": event["seq"],
                "replayed": False,
            }
            conn.execute(
                "INSERT INTO callback_receipts VALUES(?, 'event', ?, ?)",
                (callback_id, canonical_json(response), now),
            )
            return response

    def pending_delivery(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM delivery_outbox WHERE state='pending' ORDER BY outbox_id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def complete_delivery(
        self, outbox_id: int, binding: Mapping[str, Any]
    ) -> Dict[str, Any]:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("delivery outbox item does not exist")
            issue_id = str(row["issue_id"])
            conn.execute(
                """UPDATE delivery_outbox SET state='done', attempts=attempts+1,
                     last_error=NULL, updated_at=? WHERE outbox_id=?""",
                (now, outbox_id),
            )
            conn.execute(
                """UPDATE issues SET handoff_status='delivered', chat_id=?, message_id=?, topic_id=?,
                     updated_at=? WHERE issue_id=?""",
                (
                    binding.get("chat_id"),
                    binding.get("message_id") or binding.get("topic_id"),
                    binding.get("topic_id") or binding.get("message_id"),
                    now,
                    issue_id,
                ),
            )
            return self._append_event_tx(
                conn,
                issue_id,
                "HandoffDelivered",
                "gateway",
                "system",
                dict(binding),
                now,
            )

    def fail_delivery(self, outbox_id: int, error: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE delivery_outbox SET attempts=attempts+1, last_error=?,
                     updated_at=? WHERE outbox_id=?""",
                (error[:1000], utc_now(), outbox_id),
            )

    def pending_base_event(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM base_outbox WHERE state='pending' ORDER BY outbox_id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def complete_base_event(self, outbox_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE base_outbox SET state='done', attempts=attempts+1,
                     last_error=NULL, updated_at=? WHERE outbox_id=?""",
                (utc_now(), outbox_id),
            )

    def fail_base_event(self, outbox_id: int, error: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE base_outbox SET attempts=attempts+1, last_error=?,
                     updated_at=? WHERE outbox_id=?""",
                (error[:1000], utc_now(), outbox_id),
            )

    def count_rows(self, table: str) -> int:
        allowed = {
            "issues",
            "events",
            "handoffs",
            "solutions",
            "callback_receipts",
            "delivery_outbox",
            "base_outbox",
        }
        if table not in allowed:
            raise ValueError("unsupported table")
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])

    def _append_event_tx(
        self,
        conn: sqlite3.Connection,
        issue_id: str,
        kind: str,
        actor_id: str,
        source: str,
        payload: Mapping[str, Any],
        occurred_at: str,
    ) -> Dict[str, Any]:
        event_id = str(uuid.uuid4())
        received_at = utc_now()
        cursor = conn.execute(
            """INSERT INTO events(
                 event_id, issue_id, kind, actor_id, source,
                 occurred_at, received_at, payload_json
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                issue_id,
                kind,
                actor_id,
                source,
                occurred_at,
                received_at,
                canonical_json(dict(payload)),
            ),
        )
        seq = int(cursor.lastrowid)
        base_payload = {
            "seq": seq,
            "event_id": event_id,
            "issue_id": issue_id,
            "kind": kind,
            "actor_id": actor_id,
            "source": source,
            "occurred_at": occurred_at,
            "payload": dict(payload),
        }
        conn.execute(
            """INSERT INTO base_outbox(
                 event_id, issue_id, kind, payload_json, state, created_at, updated_at
               ) VALUES(?, ?, ?, ?, 'pending', ?, ?)""",
            (event_id, issue_id, kind, canonical_json(base_payload), received_at, received_at),
        )
        return base_payload

    @staticmethod
    def _issue_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "issue_id": row["issue_id"],
            "root_issue_id": row["root_issue_id"],
            "reporter_id": row["reporter_id"],
            "device_id": row["device_id"],
            "status": row["status"],
            "handoff_status": row["handoff_status"],
            "current_solution_version": row["current_solution_version"],
            "chat_id": row["chat_id"],
            "message_id": row["message_id"],
            "topic_id": row["topic_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "issue_id": row["issue_id"],
            "kind": row["kind"],
            "actor_id": row["actor_id"],
            "source": row["source"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "payload": json.loads(row["payload_json"]),
        }
