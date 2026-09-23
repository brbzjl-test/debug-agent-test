class DomainError(Exception):
    """Base class for errors safe to expose through the local API."""

    code = "domain_error"


class ValidationError(DomainError):
    code = "validation_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class ForbiddenError(DomainError):
    code = "forbidden"

