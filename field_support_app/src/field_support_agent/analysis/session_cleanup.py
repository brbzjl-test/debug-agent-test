"""Delete only explicitly associated Codex sessions through its supported API."""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import tempfile
import time
from uuid import UUID


def delete_sessions(command, environment, thread_ids, on_deleted, timeout=10):
    ids = list(dict.fromkeys(thread_ids))
    for thread_id in ids:
        if str(UUID(thread_id)) != thread_id:
            raise ValueError("invalid Codex session UUID")
    if not ids:
        return
    with tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=stderr, env=environment) as proc:
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            buffer = b""
            deadline = time.monotonic() + timeout

            def request(request_id, method, params):
                nonlocal buffer
                proc.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
                proc.stdin.flush()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Codex session cleanup timed out")
                    if b"\n" not in buffer:
                        if not selector.select(remaining):
                            raise TimeoutError("Codex session cleanup timed out")
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            raise RuntimeError("Codex session cleanup disconnected")
                        buffer += chunk
                        continue
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == request_id and "method" not in message:
                        return message

            try:
                initialized = request(1, "initialize", {"clientInfo": {
                    "name": "field_support_cleanup", "version": "0.1.0",
                }})
                if "error" in initialized:
                    raise RuntimeError(initialized["error"].get("message", "Codex initialization failed"))
                proc.stdin.write(b'{"method":"initialized","params":{}}\n')
                proc.stdin.flush()
                for number, thread_id in enumerate(ids, 2):
                    result = request(number, "thread/delete", {"threadId": thread_id})
                    if "error" in result:
                        message = result["error"].get("message", "Codex deletion failed")
                        # Retry after a partial deletion, including an app crash before local commit.
                        if message.lower() != "no rollout found for thread id " + thread_id:
                            raise RuntimeError(message)
                    on_deleted(thread_id)
            finally:
                selector.close()
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
