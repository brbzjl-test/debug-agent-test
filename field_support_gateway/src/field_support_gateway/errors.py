"""Domain errors translated to stable HTTP responses."""


class GatewayError(Exception):
    status_code = 400
    code = "gateway_error"


class AuthenticationError(GatewayError):
    status_code = 401
    code = "authentication_failed"


class AuthorizationError(GatewayError):
    status_code = 403
    code = "authorization_failed"


class NotFoundError(GatewayError):
    status_code = 404
    code = "not_found"


class ConflictError(GatewayError):
    status_code = 409
    code = "conflict"


class ValidationError(GatewayError):
    status_code = 422
    code = "validation_failed"

