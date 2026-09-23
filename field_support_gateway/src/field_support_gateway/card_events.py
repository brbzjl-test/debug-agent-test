"""Server-side Feishu card event consumer using lark-cli's long connection."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import logging
from typing import IO, Optional

from .service import GatewayService


LOGGER = logging.getLogger(__name__)


class LarkCardEventConsumer:
    def __init__(
        self,
        service: GatewayService,
        profile: str,
        *,
        binary: str = "lark-cli",
    ) -> None:
        self.service = service
        self.profile = profile
        self.binary = binary
        self._process: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    def start(self) -> None:
        if self._process is not None:
            return
        command = [
            self.binary,
            "--profile",
            self.profile,
            "event",
            "consume",
            "card.action.trigger",
            "--as",
            "bot",
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stderr_thread = threading.Thread(target=self._read_stderr, args=(self._process.stderr,), daemon=True)
        self._stderr_thread.start()
        self._thread = threading.Thread(target=self._read, args=(self._process.stdout,), daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            return_code = self._process.poll()
            self.stop()
            raise RuntimeError("Feishu card event consumer did not become ready (exit={})".format(return_code))

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        self._thread = None
        self._stderr_thread = None
        self._process = None

    def _read(self, stream: IO[str]) -> None:
        for line in stream:
            if self._stop.is_set():
                break
            try:
                payload = json.loads(line)
                if payload.get("type") == "card.action.trigger":
                    self.service.card_action_event(payload)
            except Exception:
                # Invalid or unauthorized events remain rejected by the service.
                continue

    def _read_stderr(self, stream: IO[str]) -> None:
        for line in stream:
            message = line.rstrip()
            if message.startswith("[event] ready event_key=card.action.trigger"):
                self._ready.set()
            elif message:
                LOGGER.info("lark event consumer: %s", message)
