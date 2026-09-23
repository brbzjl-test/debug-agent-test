from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .feishu_direct import _closed_card, _solution_card, _waiting_verification_card


LOGGER = logging.getLogger(__name__)


class LarkCliFeishuClient:
    """On-demand Feishu connection backed by an existing lark-cli profile."""

    def __init__(
        self,
        binary: str,
        profile: str,
        support_chat_id: str,
        state_path: Path,
        *,
        base_app_token: str = "",
        base_table_id: str = "",
        site_name: str = "",
        device_name: str = "",
    ) -> None:
        self.binary = binary
        self.profile = profile
        self.support_chat_id = support_chat_id
        self.base_app_token = base_app_token
        self.base_table_id = base_table_id
        self.site_name = site_name
        self.device_name = device_name
        self.state_path = state_path
        self._lock = threading.RLock()
        self._handoff_lock = threading.Lock()
        self._state = self._load_state()
        self._process: Optional[subprocess.Popen[str]] = None
        self._ready: Optional[threading.Event] = None
        self._stderr_tail: list[str] = []

    def handoff(self, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        with self._handoff_lock:
            return self._handoff(payload, idempotency_key)

    def ensure_support_chat(self) -> str:
        site = self.site_name.strip()
        device = self.device_name.strip()
        if not site and not device:
            return self.support_chat_id
        name = "现场支持·" + "·".join(item for item in (site, device) if item)
        with self._lock:
            cached = self._state.setdefault("site", {}).get("chat_id", "")
            if cached and self._state["site"].get("name") == name:
                self.support_chat_id = cached
                return cached
        listed = self._run_json(
            ["api", "GET", "/open-apis/im/v1/chats?page_size=100", "--as", "bot", "--format", "json"]
        )
        for chat in (listed.get("data", {}).get("items") or []):
            if chat.get("name") == name:
                chat_id = str(chat["chat_id"])
                self.support_chat_id = chat_id
                with self._lock:
                    self._state["site"] = {"name": name, "chat_id": chat_id}
                    self._write_state()
                return chat_id
        created = self._run_json(
            [
                "api",
                "POST",
                "/open-apis/im/v1/chats?user_id_type=open_id",
                "--as",
                "bot",
                "--data",
                json.dumps({"name": name}, ensure_ascii=False),
                "--format",
                "json",
            ]
        )
        chat_id = str((created.get("data") or {}).get("chat_id", ""))
        if not chat_id:
            raise RuntimeError("自动创建现场支持群失败")
        self.support_chat_id = chat_id
        with self._lock:
            self._state["site"] = {"name": name, "chat_id": chat_id}
            self._write_state()
        return chat_id

    def _handoff(self, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        issue_id = str(payload["issue_id"])
        root_issue_id = str(payload.get("root_issue_id") or issue_id)
        bundle_path = Path(str(payload.get("evidence_bundle", ""))).expanduser()
        with self._lock:
            existing = dict(self._state["issues"].get(issue_id) or {})
            topics = self._state.setdefault("topics", {})
            topic_root_message_id = str(
                topics.get(root_issue_id)
                or existing.get("topic_root_message_id")
                or ""
            )

        self._ensure_started()
        message_id = str(existing.get("message_id", ""))
        if not message_id:
            card_content = json.dumps(
                _solution_card(payload), ensure_ascii=False, separators=(",", ":")
            )
            if topic_root_message_id:
                arguments = [
                    "im",
                    "+messages-reply",
                    "--message-id",
                    topic_root_message_id,
                    "--reply-in-thread",
                    "--msg-type",
                    "interactive",
                    "--content",
                    card_content,
                    "--idempotency-key",
                    idempotency_key,
                    "--as",
                    "bot",
                    "--format",
                    "json",
                ]
            else:
                arguments = [
                    "im",
                    "+messages-send",
                    "--chat-id",
                    self.support_chat_id,
                    "--msg-type",
                    "interactive",
                    "--content",
                    card_content,
                    "--idempotency-key",
                    idempotency_key,
                    "--as",
                    "bot",
                    "--format",
                    "json",
                ]
            result = self._run_json(arguments, timeout=30)
            message_id = str(_find_value(result, "message_id") or "")
            if not message_id:
                raise RuntimeError("飞书问题卡片发送失败：未返回 message_id")
            if not topic_root_message_id:
                topic_root_message_id = message_id
            existing.update(
                handoff_status="delivering",
                message_id=message_id,
                root_issue_id=root_issue_id,
                topic_root_message_id=topic_root_message_id,
                card_state="awaiting_engineer",
                card_summary=str(payload.get("card_summary") or payload.get("summary", "")),
                evidence_count=len(payload.get("evidence", [])),
                solution=existing.get("solution"),
                confirmed=bool(existing.get("confirmed")),
            )
            with self._lock:
                self._state.setdefault("topics", {})[root_issue_id] = topic_root_message_id
                self._state["issues"][issue_id] = existing
                self._write_state()

        if not topic_root_message_id:
            topic_root_message_id = message_id
        existing["root_issue_id"] = root_issue_id
        existing["topic_root_message_id"] = topic_root_message_id
        with self._lock:
            self._state.setdefault("topics", {}).setdefault(root_issue_id, topic_root_message_id)

        if bundle_path.name and not existing.get("evidence_message_id"):
            if not bundle_path.is_file():
                raise RuntimeError("证据包不存在：{}".format(bundle_path))
            evidence_result = self._run_json(
                [
                    "im",
                    "+messages-reply",
                    "--message-id",
                    topic_root_message_id,
                    "--reply-in-thread",
                    "--file",
                    bundle_path.name,
                    "--idempotency-key",
                    "evidence:{}".format(issue_id),
                    "--as",
                    "bot",
                    "--format",
                    "json",
                ],
                timeout=60,
                cwd=bundle_path.parent,
            )
            evidence_message_id = str(_find_value(evidence_result, "message_id") or "")
            if not evidence_message_id:
                raise RuntimeError("飞书证据包发送失败：未返回 message_id")
            existing["evidence_message_id"] = evidence_message_id
        if existing.get("handoff_status") not in {"solution_available", "closed"}:
            if payload.get("issue_status") == "pending_verification" and existing.get("solution"):
                existing["handoff_status"] = "solution_available"
            else:
                existing["handoff_status"] = "delivered"
        with self._lock:
            self._state["issues"][issue_id] = existing
            self._write_state()
        self._project_base(issue_id, "open", payload)
        return {
            "handoff_status": existing["handoff_status"],
            "message_id": message_id,
            "topic_id": topic_root_message_id,
            "evidence_message_id": existing.get("evidence_message_id"),
        }

    def sync(self, issue_id: str, after_seq: int = 0) -> Mapping[str, Any]:
        with self._lock:
            item = dict(self._state["issues"].get(issue_id) or {})
        if (
            item.get("handoff_status") == "solution_available"
            and item.get("card_state") != "waiting_verification"
            and item.get("message_id")
            and item.get("solution")
        ):
            self._update_card(
                str(item["message_id"]),
                _waiting_verification_card(issue_id, item["solution"]),
            )
            with self._lock:
                self._state["issues"][issue_id]["card_state"] = "waiting_verification"
                self._write_state()
            item["card_state"] = "waiting_verification"
        return {
            "issue": {"issue_id": issue_id, "handoff_status": item.get("handoff_status", "queued")},
            "latest_solution": item.get("solution"),
            "events": [],
            "cursor": max(after_seq, int(item.get("sequence", 0))),
        }

    def confirm(self, issue_id: str, solution_version: int, reporter_id: str) -> Mapping[str, Any]:
        with self._lock:
            current = dict(self._state["issues"].get(issue_id) or {})
        message_id = str(current.get("message_id", ""))
        if message_id:
            self._update_card(message_id, _closed_card(issue_id, current.get("solution") or {}))
        with self._lock:
            item = self._state["issues"].get(issue_id)
            if item is not None:
                item["confirmed"] = True
                item["handoff_status"] = "closed"
                item["card_state"] = "closed"
                item["sequence"] = int(item.get("sequence", 0)) + 1
                self._write_state()
        self._project_base(issue_id, "已解决", {"reporter_id": reporter_id})
        return {"issue_id": issue_id, "status": "closed", "solution_version": solution_version}

    def verification_failed(
        self,
        issue_id: str,
        solution_version: int,
        reporter_id: str,
        observation: str,
    ) -> Mapping[str, Any]:
        with self._lock:
            current = dict(self._state["issues"].get(issue_id) or {})
        solution = current.get("solution") or {}
        if int(solution.get("version", 0)) != solution_version:
            raise RuntimeError("现场反馈对应的解决方案版本已过期")
        message_id = str(current.get("message_id", ""))
        if not message_id:
            raise RuntimeError("未找到飞书问题卡片")
        self._update_card(
            message_id,
            _solution_card(
                {
                    "issue_id": issue_id,
                    "card_summary": current.get("card_summary", ""),
                    "evidence_count": current.get("evidence_count", 0),
                    "previous_solution": solution,
                    "failure_observation": observation,
                }
            ),
        )
        with self._lock:
            item = self._state["issues"][issue_id]
            item["handoff_status"] = "delivered"
            item["card_state"] = "awaiting_engineer"
            item["last_failure_observation"] = observation
            item["sequence"] = int(item.get("sequence", 0)) + 1
            self._write_state()
        self._project_base(issue_id, "open", {"summary": observation})
        return {"issue_id": issue_id, "handoff_status": "delivered", "solution_version": solution_version}

    def forget_issues(self, issue_ids, root_issue_ids=()) -> None:
        with self._lock:
            issues = self._state.setdefault("issues", {})
            topics = self._state.setdefault("topics", {})
            for issue_id in issue_ids:
                issues.pop(issue_id, None)
            for root_issue_id in root_issue_ids:
                topics.pop(root_issue_id, None)
            self._write_state()

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOGGER.error("lark-cli card listener did not stop after SIGTERM")

    def _ensure_started(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            ready = threading.Event()
            self._ready = ready
            self._stderr_tail = []
            process = subprocess.Popen(
                self._command("event", "consume", "card.action.trigger", "--as", "bot"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._environment(),
            )
            self._process = process
            threading.Thread(target=self._read_events, args=(process,), name="field-support-lark-events", daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(process, ready), name="field-support-lark-status", daemon=True).start()
        if not ready.wait(timeout=20):
            self.close()
            detail = "".join(self._stderr_tail[-5:]).strip()
            raise RuntimeError("飞书卡片回调连接未就绪" + ("：" + detail if detail else ""))
        if process.poll() is not None:
            detail = "".join(self._stderr_tail[-5:]).strip()
            raise RuntimeError("飞书卡片回调连接启动失败" + ("：" + detail if detail else ""))

    def _read_events(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    self._handle_event(event)
            except Exception:
                LOGGER.exception("failed to process Feishu card event")

    def _read_stderr(self, process: subprocess.Popen[str], ready: threading.Event) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            with self._lock:
                self._stderr_tail.append(line)
                del self._stderr_tail[:-20]
            if "[event] ready event_key=card.action.trigger" in line:
                ready.set()

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        if event.get("type") != "card.action.trigger" or event.get("chat_id") != self.support_chat_id:
            return
        values = event.get("form_value") or {}
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except json.JSONDecodeError:
                return
        if not isinstance(values, Mapping):
            return
        solution = str(values.get("actual_solution", "")).strip()
        verification = str(values.get("verification_method", "")).strip()
        if not solution or not verification:
            return
        message_id = str(event.get("message_id", ""))
        with self._lock:
            issue_id = next(
                (key for key, item in self._state["issues"].items() if item.get("message_id") == message_id),
                "",
            )
            if not issue_id:
                return
            item = self._state["issues"][issue_id]
            if item.get("handoff_status") not in {"delivered", "handling"}:
                return
            version = int((item.get("solution") or {}).get("version", 0)) + 1
            item["solution"] = {
                "version": version,
                "engineer_id": str(event.get("operator_id", "")),
                "actual_solution": solution,
                "verification_method": verification,
            }
            item["handoff_status"] = "solution_available"
            item["sequence"] = int(item.get("sequence", 0)) + 1
            self._write_state()
            solution_payload = dict(item["solution"])
        self._update_card(message_id, _waiting_verification_card(issue_id, solution_payload))
        with self._lock:
            self._state["issues"][issue_id]["card_state"] = "waiting_verification"
            self._write_state()
        self._project_base(
            issue_id,
            "待验证",
            {"actual_solution": solution, "verification_method": verification},
        )

    def _project_base(self, issue_id: str, status: str, payload: Mapping[str, Any]) -> None:
        if not self.base_app_token or not self.base_table_id:
            return
        try:
            fields = {
                "问题编号": issue_id,
                "状态": status,
                "最近活动时间": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if status == "open":
                summary = str(payload.get("summary", "现场问题待处理"))
                fields.update({"问题标题": summary[:80], "现象描述": summary, "上报原消息": summary})
            elif status == "待验证":
                fields.update(
                    {
                        "解决方案": str(payload.get("actual_solution", "")),
                        "验证步骤": str(payload.get("verification_method", "")),
                    }
                )
            searched = self._base(
                "+record-list",
                "--filter-json",
                json.dumps({"logic": "and", "conditions": [["问题编号", "==", issue_id]]}, ensure_ascii=False),
                "--field-id",
                "问题编号",
                "--limit",
                "2",
            )
            items = _base_records(searched)
            if len(items) > 1:
                raise RuntimeError("多维表格中存在重复问题编号：" + issue_id)
            arguments = ["--json", json.dumps(fields, ensure_ascii=False)]
            if items:
                record_id = str(items[0].get("record_id") or items[0].get("id") or "")
                if not record_id:
                    raise RuntimeError("多维表格记录缺少 record_id")
                arguments.extend(["--record-id", record_id])
            self._base("+record-upsert", *arguments)
        except Exception:
            LOGGER.exception("failed to project issue %s to Feishu Base", issue_id)

    def _update_card(self, message_id: str, card: Mapping[str, Any]) -> None:
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        result = self._run_json(
            [
                "api",
                "PATCH",
                "/open-apis/im/v1/messages/{}".format(message_id),
                "--as",
                "bot",
                "--data",
                json.dumps({"content": content}, ensure_ascii=False, separators=(",", ":")),
                "--format",
                "json",
            ],
            timeout=30,
        )
        code = result.get("code", 0)
        if code not in {0, None}:
            raise RuntimeError("飞书问题卡片更新失败：{}".format(result.get("msg") or code))

    def _base(self, command: str, *arguments: str) -> Dict[str, Any]:
        return self._run_json(
            [
                "base",
                command,
                "--as",
                "bot",
                "--base-token",
                self.base_app_token,
                "--table-id",
                self.base_table_id,
                *arguments,
                "--format",
                "json",
            ]
        )

    def _run_json(
        self,
        arguments: Sequence[str],
        timeout: float = 30,
        cwd: Optional[Path] = None,
    ) -> Dict[str, Any]:
        result = subprocess.run(
            self._command(*arguments),
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=self._environment(),
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout)[-1200:].strip()
            raise RuntimeError("lark-cli 调用失败：" + detail)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("lark-cli 返回了无效 JSON") from exc
        if (
            not isinstance(value, dict)
            or value.get("ok") is False
            or value.get("code", 0) not in (0, None)
        ):
            raise RuntimeError("lark-cli 返回失败")
        return value

    def _command(self, *arguments: str) -> list[str]:
        return [self.binary, "--profile", self.profile, *arguments]

    @staticmethod
    def _environment() -> Dict[str, str]:
        value = dict(os.environ)
        value.update(LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        return value

    def _load_state(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("issues"), dict):
                value.setdefault("topics", {})
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 2, "issues": {}, "topics": {}}

    def _write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.state_path.parent), 0o700)
        temporary = self.state_path.with_suffix(".tmp")
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.state_path))
            os.chmod(str(self.state_path), 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def verify_lark_profile(binary: str, profile: str) -> Dict[str, Any]:
    client = LarkCliFeishuClient(binary, profile, "", Path(os.devnull))
    result = client._run_json(["auth", "status", "--json", "--verify"], timeout=30)
    identities = result.get("identities") or {}
    bot = identities.get("bot") or {}
    verified = result.get("verified") is True and bot.get("status") == "ready"
    if not verified:
        raise RuntimeError("lark-cli profile 的机器人身份未就绪")
    return {"verified": True, "identity": "bot", "profile": profile}


def _find_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        if value.get(key):
            return value[key]
        for item in value.values():
            found = _find_value(item, key)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, key)
            if found:
                return found
    return None


def _base_records(result: Mapping[str, Any]) -> list[Dict[str, Any]]:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else result
    if "record_id_list" in data and isinstance(data.get("data"), list):
        identifiers = data.get("record_id_list") or []
        return [{"record_id": record_id} for record_id in identifiers]
    items = data.get("records", data.get("items", []))
    return [dict(item) for item in items] if isinstance(items, list) else []
