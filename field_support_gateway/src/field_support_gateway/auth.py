"""Device and callback authentication."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Mapping, Protocol

from .errors import AuthenticationError, ValidationError


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifiedCallback:
    event_id: str
    principal_id: str


class CallbackVerifier(Protocol):
    def verify(self, headers: Mapping[str, str], body: bytes) -> VerifiedCallback:
        """Verify callback authenticity and return trusted envelope identity."""


class HmacCallbackVerifier:
    """Deployment-neutral HMAC verifier used behind a Feishu ingress adapter.

    A production ingress can replace this class with Feishu's native verifier.
    The canonical signature is HMAC-SHA256(secret, timestamp + "." + body).
    """

    def __init__(self, secret: str, max_age_seconds: int = 300) -> None:
        if not secret:
            raise ValueError("callback secret must not be empty")
        self._secret = secret.encode("utf-8")
        self._max_age_seconds = max_age_seconds

    def verify(self, headers: Mapping[str, str], body: bytes) -> VerifiedCallback:
        normalized_headers = {str(key).lower(): value for key, value in headers.items()}
        timestamp = normalized_headers.get("x-field-support-timestamp", "")
        signature = normalized_headers.get("x-field-support-signature", "")
        if not timestamp or not signature:
            raise AuthenticationError("missing callback signature headers")
        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise AuthenticationError("invalid callback timestamp") from exc
        if abs(int(time.time()) - sent_at) > self._max_age_seconds:
            raise AuthenticationError("expired callback timestamp")
        expected = hmac.new(
            self._secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("invalid callback signature")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("callback body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("callback body must be a JSON object")
        event_id = str(payload.get("event_id", "")).strip()
        operator = payload.get("operator") or {}
        if not isinstance(operator, dict):
            raise ValidationError("callback operator must be an object")
        principal_id = str(operator.get("open_id", "")).strip()
        if not event_id:
            raise ValidationError("callback event_id is required")
        if not principal_id:
            raise ValidationError("callback operator.open_id is required")
        return VerifiedCallback(event_id=event_id, principal_id=principal_id)
