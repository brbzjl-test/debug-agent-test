from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class IssueStatus(str, Enum):
    OPEN = "open"
    PENDING_VERIFICATION = "pending_verification"
    CLOSED = "closed"


@dataclass(frozen=True)
class Issue:
    issue_id: str
    root_issue_id: str
    parent_issue_id: Optional[str]
    sub_sequence: Optional[int]
    reporter_id: str
    status: IssueStatus
    root_rollup_status: IssueStatus
    latest_solution_version: int
    local_input_enabled: bool
    handoff_state: str
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "root_issue_id": self.root_issue_id,
            "parent_issue_id": self.parent_issue_id,
            "sub_sequence": self.sub_sequence,
            "reporter_id": self.reporter_id,
            "status": self.status.value,
            "root_rollup_status": self.root_rollup_status.value,
            "latest_solution_version": self.latest_solution_version,
            "local_input_enabled": self.local_input_enabled,
            "handoff_state": self.handoff_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class Event:
    sequence: int
    schema_version: int
    event_id: str
    issue_id: str
    kind: str
    actor_id: str
    channel: str
    occurred_at: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "issue_id": self.issue_id,
            "kind": self.kind,
            "actor_id": self.actor_id,
            "channel": self.channel,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Message:
    message_id: str
    issue_id: str
    actor_id: str
    role: str
    channel: str
    content: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Solution:
    solution_id: str
    issue_id: str
    version: int
    submitted_by: str
    content: str
    verification_method: Optional[str]
    source: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class OutboxItem:
    outbox_id: str
    event_id: str
    issue_id: str
    kind: str
    payload: Dict[str, Any]
    status: str
    attempts: int
    available_at: str
    created_at: str
    updated_at: str
    last_error: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

