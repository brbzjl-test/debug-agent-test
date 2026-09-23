"""On-demand Codex app-server transport. Only assistant text reaches the UI."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import PurePath


ACTIVITY_STAGES = frozenset({
    "capturing", "preparing", "starting", "waiting", "analyzing", "checking",
    "reading_logs", "reading_code", "responding", "saving", "retrying", "limited",
})


class AppServerRequestError(RuntimeError):
    def __init__(self, method, message):
        super().__init__(message)
        self.method = method


def command_stage(item):
    """Describe an observed read operation without exposing paths, commands or output."""
    actions = item.get("commandActions", [])
    paths = [PurePath(a["path"]) for a in actions if a.get("path") and a.get("type") == "read"]
    if any(".log" in p.suffixes or "logs" in p.parts for p in paths):
        return "reading_logs"
    if any(p.suffix in {".py", ".cpp", ".cc", ".h", ".hpp", ".c", ".js", ".ts", ".rs", ".sh"} for p in paths):
        return "reading_code"
    return "checking"


class AppServerStream:
    def __init__(self, command, environment, timeout):
        self.command = command
        self.environment = environment
        self.timeout = timeout

    def run(self, prompt, thread_id, cwd, model, effort, on_update, on_activity=None):
        events = []
        messages = {}
        phases = {}
        response = ""
        activity = on_activity or (lambda stage: None)
        activity("starting")
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=errors, env=self.environment, start_new_session=True,
            )
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + self.timeout
            buffer = b""

            def send(value):
                proc.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
                proc.stdin.flush()

            def receive():
                nonlocal buffer
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex analysis timed out")
                while b"\n" not in buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("Codex analysis timed out")
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        errors.flush()
                        errors.seek(0)
                        detail = errors.read().decode("utf-8", errors="replace").strip()[-2000:]
                        # Diagnostics stay in the private analysis record and server logs.
                        raise RuntimeError(
                            "Codex app-server disconnected before completing the turn"
                            + (": " + detail if detail else "")
                        )
                    buffer += chunk
                line, buffer = buffer.split(b"\n", 1)
                return json.loads(line)

            request_id = 0

            def rpc(method, params):
                nonlocal request_id
                request_id += 1
                send({"id": request_id, "method": method, "params": params})
                while True:
                    event = receive()
                    if event.get("id") == request_id and "method" not in event:
                        if "error" in event:
                            raise AppServerRequestError(method, event["error"].get("message", "Codex request failed"))
                        return event.get("result", {})
                    handle(event)

            def handle(event):
                nonlocal response
                method = event.get("method", "")
                if "id" in event and method:
                    # No approval, tool execution, or permission grant is delegated to the host.
                    send({"id": event["id"], "error": {
                        "code": -32601, "message": "Unsupported in read-only field support",
                    }})
                    return
                params = event.get("params", {})
                if params.get("threadId") != thread_id:
                    return
                if method == "error" and params.get("willRetry"):
                    activity("retrying")
                elif method == "turn/started":
                    activity("waiting")
                elif method.startswith("item/reasoning/"):
                    # Use only the occurrence of work, never reasoning text or summaries.
                    activity("analyzing")
                elif method == "item/commandExecution/outputDelta":
                    activity(None)
                if method in {"item/started", "item/completed"}:
                    item = params.get("item", {})
                    item_type = item.get("type")
                    if item_type == "commandExecution":
                        activity(command_stage(item) if method == "item/started" else "waiting")
                    elif item_type in {"reasoning", "contextCompaction", "plan"}:
                        activity("analyzing")
                    if item.get("type") != "agentMessage":
                        return
                    item_id = item["id"]
                    phases[item_id] = item.get("phase")
                    if item.get("phase") == "commentary":
                        activity("analyzing")
                        return
                    activity("responding")
                    if method == "item/completed":
                        messages[item_id] = item.get("text", "")
                        response = messages[item_id]
                        events.append({"type": "item.completed", "item": {
                            "type": "agent_message", "text": response,
                        }})
                        on_update(response)
                elif method == "item/agentMessage/delta":
                    item_id = params["itemId"]
                    if phases.get(item_id) == "commentary":
                        activity("analyzing")
                        return
                    activity("responding")
                    messages[item_id] = messages.get(item_id, "") + params.get("delta", "")
                    on_update(messages[item_id])

            try:
                rpc("initialize", {"clientInfo": {"name": "field_support_agent", "version": "0.1.0"}})
                send({"method": "initialized", "params": {}})
                params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "read-only"}
                if model:
                    params["model"] = model
                if thread_id:
                    params["threadId"] = thread_id
                opened = rpc("thread/resume" if thread_id else "thread/start", params)
                if opened.get("sandbox", {}).get("type") != "readOnly" or opened.get("approvalPolicy") != "never":
                    raise RuntimeError("Codex did not confirm the required read-only policy")
                thread_id = opened["thread"]["id"]
                events.append({"type": "thread.started", "thread_id": thread_id})
                turn_params = {
                    "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                    "cwd": cwd, "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly"},
                }
                if effort:
                    turn_params["effort"] = effort
                if model:
                    turn_params["model"] = model
                activity("waiting")
                turn = rpc("turn/start", turn_params)["turn"]
                while True:
                    event = receive()
                    handle(event)
                    params = event.get("params", {})
                    if (event.get("method") == "turn/completed"
                            and params.get("threadId") == thread_id
                            and params.get("turn", {}).get("id") == turn["id"]):
                        completed = params["turn"]
                        if completed.get("status") != "completed":
                            error = completed.get("error") or {}
                            raise RuntimeError(error.get("message", "Codex turn did not complete"))
                        # A completed message is authoritative; incomplete deltas are never saved as a result.
                        activity("saving")
                        return response, events, thread_id
            finally:
                selector.close()
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        # macOS may reject killpg once the group leader has just exited.
                        if proc.poll() is None:
                            proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    proc.wait()
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                proc.stdout.close()
