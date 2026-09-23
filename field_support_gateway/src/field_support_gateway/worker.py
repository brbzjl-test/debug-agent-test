"""Event-driven delivery worker for durable local Outboxes."""

import threading
from typing import Optional

from .service import GatewayService


class OutboxWorker:
    def __init__(self, service: GatewayService, retry_seconds: float = 5.0) -> None:
        self._service = service
        self._retry_seconds = retry_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="field-support-outbox", daemon=True
        )
        self._thread.start()
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._retry_seconds + 1.0))
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            failed = False
            while not self._stop.is_set():
                made_progress = False
                try:
                    made_progress = self._service.dispatch_one_handoff()
                except Exception:
                    failed = True
                try:
                    made_progress = self._service.dispatch_one_base_event() or made_progress
                except Exception:
                    failed = True
                if failed:
                    break
                if not made_progress:
                    break
            if failed and not self._stop.wait(self._retry_seconds):
                self._wake.set()
