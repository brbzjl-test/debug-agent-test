"""Domain types and errors for issue lifecycle management."""

from .errors import ConflictError, DomainError, ForbiddenError, NotFoundError, ValidationError
from .models import Event, Issue, IssueStatus, Message, OutboxItem, Solution
from .state_machine import TransitionReason, require_transition

__all__ = [
    "ConflictError",
    "DomainError",
    "Event",
    "ForbiddenError",
    "Issue",
    "IssueStatus",
    "Message",
    "NotFoundError",
    "OutboxItem",
    "Solution",
    "TransitionReason",
    "ValidationError",
    "require_transition",
]
