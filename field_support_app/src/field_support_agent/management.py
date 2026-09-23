from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Callable, Dict, Tuple


class ManagementAccessError(ValueError):
    pass


class ManagementAccess:
    """Short-lived management sessions backed by the local app setting."""

    SESSION_TTL_SECONDS = 30 * 60

    def __init__(self, password_provider: Callable[[], str]) -> None:
        self._password_provider = password_provider
        self._lock = threading.RLock()
        self._sessions: Dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self._password_provider())

    def unlock(self, password: str) -> Tuple[str, bool, int]:
        configured = self._password_provider()
        if not configured:
            raise ManagementAccessError("请先在设置页配置工程模式密码")
        if not isinstance(password, str) or not hmac.compare_digest(password, configured):
            raise ManagementAccessError("管理密码错误")
        with self._lock:
            token = secrets.token_urlsafe(32)
            self._sessions[token] = time.monotonic() + self.SESSION_TTL_SECONDS
            self._purge_expired()
        return token, False, self.SESSION_TTL_SECONDS

    def require(self, token: str) -> None:
        with self._lock:
            self._purge_expired()
            if token not in self._sessions:
                raise PermissionError("管理模式已退出或超时，请重新输入密码")
            self._sessions[token] = time.monotonic() + self.SESSION_TTL_SECONDS

    def lock(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        self._sessions = {token: expiry for token, expiry in self._sessions.items() if expiry > now}
