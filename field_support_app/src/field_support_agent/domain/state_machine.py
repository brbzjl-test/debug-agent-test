from enum import Enum

from .errors import ConflictError
from .models import IssueStatus


class TransitionReason(str, Enum):
    SOLUTION_SUBMITTED = "solution_submitted"
    VERIFICATION_FAILED = "verification_failed"
    REPORTER_CONFIRMED = "reporter_confirmed"


_ALLOWED = {
    (IssueStatus.OPEN, IssueStatus.PENDING_VERIFICATION, TransitionReason.SOLUTION_SUBMITTED),
    (
        IssueStatus.PENDING_VERIFICATION,
        IssueStatus.PENDING_VERIFICATION,
        TransitionReason.SOLUTION_SUBMITTED,
    ),
    (IssueStatus.PENDING_VERIFICATION, IssueStatus.OPEN, TransitionReason.VERIFICATION_FAILED),
    (IssueStatus.PENDING_VERIFICATION, IssueStatus.CLOSED, TransitionReason.REPORTER_CONFIRMED),
}


def require_transition(current: IssueStatus, target: IssueStatus, reason: TransitionReason) -> None:
    """Enforce the complete P0 status transition table.

    A recurrence creates a new sub-issue, so it is intentionally absent here.
    """

    if (current, target, reason) not in _ALLOWED:
        raise ConflictError(
            "status transition {} -> {} is not allowed for {}".format(current.value, target.value, reason.value)
        )

