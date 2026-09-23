from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


LOGGER = logging.getLogger(__name__)


class DirectFeishuClient:
    """On-demand Feishu SDK connection used only after human handoff."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        support_chat_id: str,
        state_path: Path,
        *,
        base_app_token: str = "",
        base_table_id: str = "",
        site_name: str = "",
        device_name: str = "",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.support_chat_id = support_chat_id
        self.base_app_token = base_app_token
        self.base_table_id = base_table_id
        self.site_name = site_name
        self.device_name = device_name
        self.state_path = state_path
        self._lock = threading.RLock()
        self._handoff_lock = threading.Lock()
        self._state = self._load_state()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._channel: Any = None

    def handoff(self, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        with self._handoff_lock:
            return self._handoff(payload, idempotency_key)

    def ensure_support_chat(self) -> str:
        """Resolve the site support chat by name, creating it (with the bot as a member) when missing."""
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
        existing = _feishu_json(
            "https://open.feishu.cn/open-apis/im/v1/chats?page_size=100",
            _tenant_access_token(self.app_id, self.app_secret),
            {},
            "GET",
        )
        items = ((existing.get("data") or {}).get("items") or [])
        for chat in items:
            if chat.get("name") == name:
                chat_id = str(chat["chat_id"])
                self.support_chat_id = chat_id
                with self._lock:
                    self._state["site"] = {"name": name, "chat_id": chat_id}
                    self._write_state()
                return chat_id
        created = _feishu_json(
            "https://open.feishu.cn/open-apis/im/v1/chats?user_id_type=open_id",
            _tenant_access_token(self.app_id, self.app_secret),
            {"name": name},
        )
        data = created.get("data") or {}
        chat_id = str(data.get("chat_id", ""))
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
            card = _solution_card(payload)
            options = None
            if topic_root_message_id:
                options = {
                    "uuid": idempotency_key,
                    "reply_to": topic_root_message_id,
                    "reply_in_thread": True,
                }
            result = self._run(
                self._channel.send(self.support_chat_id, {"card": card}, options),
                timeout=30,
            )
            if not result.success or not result.message_id:
                raise RuntimeError("飞书问题卡片发送失败：{}".format(result.error or "missing message id"))
            message_id = result.message_id
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
            result = self._run(
                self._channel.send(
                    self.support_chat_id,
                    {"file": {"source": str(bundle_path), "file_name": bundle_path.name}},
                    {
                        "uuid": "evidence:{}".format(issue_id),
                        "reply_to": topic_root_message_id,
                        "reply_in_thread": True,
                    },
                ),
                timeout=60,
            )
            if not result.success or not result.message_id:
                raise RuntimeError("飞书证据包发送失败：{}".format(result.error or "missing message id"))
            existing["evidence_message_id"] = result.message_id
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
            self._ensure_started()
            result = self._run(
                self._channel.update_card(
                    str(item["message_id"]),
                    _waiting_verification_card(issue_id, item["solution"]),
                ),
                timeout=30,
            )
            if not result.success:
                raise RuntimeError("飞书问题卡片更新失败：{}".format(result.error or "unknown error"))
            with self._lock:
                self._state["issues"][issue_id]["card_state"] = "waiting_verification"
                self._write_state()
            item["card_state"] = "waiting_verification"
        status = item.get("handoff_status", "queued")
        return {
            "issue": {"issue_id": issue_id, "handoff_status": status},
            "latest_solution": item.get("solution"),
            "events": [],
            "cursor": max(after_seq, int(item.get("sequence", 0))),
        }

    def confirm(self, issue_id: str, solution_version: int, reporter_id: str) -> Mapping[str, Any]:
        with self._lock:
            current = dict(self._state["issues"].get(issue_id) or {})
        message_id = str(current.get("message_id", ""))
        if message_id:
            self._ensure_started()
            result = self._run(
                self._channel.update_card(
                    message_id,
                    _closed_card(issue_id, current.get("solution") or {}),
                ),
                timeout=30,
            )
            if not result.success:
                raise RuntimeError("飞书问题卡片关闭失败：{}".format(result.error or "unknown error"))
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
        self._ensure_started()
        card = _solution_card(
            {
                "issue_id": issue_id,
                "card_summary": current.get("card_summary", ""),
                "evidence_count": current.get("evidence_count", 0),
                "previous_solution": solution,
                "failure_observation": observation,
            }
        )
        result = self._run(self._channel.update_card(message_id, card), timeout=30)
        if not result.success:
            raise RuntimeError("飞书问题卡片重新开放失败：{}".format(result.error or "unknown error"))
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
            loop = self._loop
            thread = self._thread
            channel = self._channel
            self._loop = None
            self._thread = None
            self._channel = None
        if loop is None or thread is None:
            return
        if channel is not None:
            try:
                asyncio.run_coroutine_threadsafe(channel.disconnect(), loop).result(timeout=10)
            except Exception:
                LOGGER.exception("failed to disconnect Feishu channel")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._channel is not None:
                return
            ready = threading.Event()
            failure: list[BaseException] = []

            def run_loop() -> None:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    from lark_oapi.channel import Events, FeishuChannel

                    channel = FeishuChannel(app_id=self.app_id, app_secret=self.app_secret)
                    channel.on(Events.CARD_ACTION, self._on_card_action)
                    with self._lock:
                        self._loop = loop
                        self._channel = channel
                    ready.set()
                    loop.run_forever()
                except BaseException as exc:
                    failure.append(exc)
                    ready.set()

            thread = threading.Thread(target=run_loop, name="field-support-feishu", daemon=True)
            self._thread = thread
            thread.start()
        if not ready.wait(timeout=10):
            raise RuntimeError("飞书事件连接初始化超时")
        if failure:
            raise RuntimeError("飞书 SDK 初始化失败") from failure[0]
        self._run(self._channel.connect_until_ready(timeout=20), timeout=25)

    def _run(self, coroutine, timeout: float):
        with self._lock:
            loop = self._loop
        if loop is None:
            raise RuntimeError("飞书事件连接未启动")
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=timeout)

    async def _on_card_action(self, event: Any) -> None:
        message_id = str(getattr(event, "message_id", ""))
        form = _find_form_values(getattr(event, "action", None), getattr(event, "raw", {}))
        solution = str(form.get("actual_solution", "")).strip()
        verification = str(form.get("verification_method", "")).strip()
        if not solution or not verification:
            return
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
                "engineer_id": str(getattr(getattr(event, "operator", None), "open_id", "")),
                "actual_solution": solution,
                "verification_method": verification,
            }
            item["handoff_status"] = "solution_available"
            item["sequence"] = int(item.get("sequence", 0)) + 1
            self._write_state()
            solution_payload = dict(item["solution"])
        update = await self._channel.update_card(
            message_id,
            _waiting_verification_card(issue_id, solution_payload),
        )
        if not update.success:
            LOGGER.error("failed to hide Feishu solution form for %s: %s", issue_id, update.error)
        else:
            with self._lock:
                self._state["issues"][issue_id]["card_state"] = "waiting_verification"
                self._write_state()
        await asyncio.to_thread(
            self._project_base,
            issue_id,
            "待验证",
            {"actual_solution": solution, "verification_method": verification},
        )

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

    def _project_base(self, issue_id: str, status: str, payload: Mapping[str, Any]) -> None:
        if not self.base_app_token or not self.base_table_id:
            return
        try:
            token = _tenant_access_token(self.app_id, self.app_secret)
            base = "https://open.feishu.cn/open-apis/bitable/v1/apps/{}/tables/{}".format(
                urllib.parse.quote(self.base_app_token), urllib.parse.quote(self.base_table_id)
            )
            _ensure_site_field(base, token)
            fields = {
                "问题编号": issue_id,
                "现场": self.site_name,
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
            searched = _feishu_json(
                base + "/records/search",
                token,
                {
                    "filter": {
                        "conjunction": "and",
                        "conditions": [{"field_name": "问题编号", "operator": "is", "value": [issue_id]}],
                    },
                    "page_size": 1,
                },
            )
            items = ((searched.get("data") or {}).get("items") or [])
            if items and items[0].get("record_id"):
                _feishu_json(base + "/records/" + urllib.parse.quote(items[0]["record_id"]), token, {"fields": fields}, "PUT")
            else:
                _feishu_json(base + "/records", token, {"fields": fields})
        except Exception:
            LOGGER.exception("failed to project issue %s to Feishu Base", issue_id)


def _ensure_site_field(base: str, token: str) -> None:
    fields = _feishu_json(
        base + "/fields?page_size=100",
        token,
        {},
        "GET",
    )
    if any(f.get("field_name") == "现场" for f in ((fields.get("data") or {}).get("items") or [])):
        return
    _feishu_json(
        base + "/fields",
        token,
        {"field_name": "现场", "type": 1, "property": {"uuid": None}},
    )


def _solution_card(handoff: Mapping[str, Any]) -> Dict[str, Any]:
    issue_id = str(handoff["issue_id"])
    summary = _compact_summary(str(handoff.get("card_summary") or handoff.get("summary", "")))
    evidence_count = int(handoff.get("evidence_count", len(handoff.get("evidence", []))))
    previous_solution = handoff.get("previous_solution") or {}
    failure_observation = str(handoff.get("failure_observation", "")).strip()
    summary_content = "**现场摘要**\n{}\n\n已收集 {} 项证据，完整 ZIP 证据包已附在本问题话题中。".format(
        summary, evidence_count
    )
    if failure_observation:
        summary_content += "\n\n**现场验证未通过**\n{}".format(failure_observation)
    if previous_solution:
        summary_content += "\n\n<font color='grey'>上一版方案（v{}）：{}</font>".format(
            previous_solution.get("version", "-"),
            str(previous_solution.get("actual_solution", "")),
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": "{} 等待工程师处理".format(issue_id)}},
        "header": {
            "title": {"tag": "plain_text", "content": "现场问题处理"},
            "subtitle": {"tag": "plain_text", "content": issue_id},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": summary_content,
                },
                {
                    "tag": "form",
                    "name": "solution_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "actual_solution",
                            "required": True,
                            "input_type": "multiline_text",
                            "rows": 4,
                            "label": {"tag": "plain_text", "content": "实际解决方案"},
                        },
                        {
                            "tag": "input",
                            "name": "verification_method",
                            "required": True,
                            "input_type": "multiline_text",
                            "rows": 2,
                            "label": {"tag": "plain_text", "content": "现场验证方法"},
                        },
                        {
                            "tag": "button",
                            "name": "submit_solution",
                            "text": {"tag": "plain_text", "content": "提交解决方案"},
                            "type": "primary_filled",
                            "width": "fill",
                            "form_action_type": "submit",
                        },
                    ],
                },
            ]
        },
    }


def _waiting_verification_card(issue_id: str, solution: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": "{} 等待现场验证".format(issue_id)}},
        "header": {
            "title": {"tag": "plain_text", "content": "方案已提交，等待现场验证"},
            "subtitle": {"tag": "plain_text", "content": issue_id},
            "template": "orange",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "**解决方案 v{}**\n{}\n\n**现场验证方法**\n{}\n\n<font color='grey'>现场确认未解决后，本卡片会重新开放方案表单。</font>".format(
                        solution.get("version", "-"),
                        solution.get("actual_solution", ""),
                        solution.get("verification_method", "现场确认是否恢复"),
                    ),
                }
            ]
        },
    }


def _closed_card(issue_id: str, solution: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": "{} 已解决".format(issue_id)}},
        "header": {
            "title": {"tag": "plain_text", "content": "现场问题已解决"},
            "subtitle": {"tag": "plain_text", "content": issue_id},
            "template": "green",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "**最终解决方案 v{}**\n{}\n\n<font color='grey'>现场人员已确认恢复。</font>".format(
                        solution.get("version", "-"),
                        solution.get("actual_solution", ""),
                    ),
                }
            ]
        },
    }


def _compact_summary(value: str) -> str:
    compact = " ".join(value.split()) or "现场问题待工程师处理"
    return compact if len(compact) <= 100 else compact[:97].rstrip() + "..."


def _find_form_values(action: Any, raw: Any) -> Dict[str, Any]:
    candidates = [getattr(action, "value", None), raw]

    def visit(value: Any) -> Optional[Dict[str, Any]]:
        if isinstance(value, dict):
            if "actual_solution" in value or "verification_method" in value:
                return value
            for child in value.values():
                found = visit(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found is not None:
                    return found
        elif isinstance(value, str) and value.startswith("{"):
            try:
                return visit(json.loads(value))
            except json.JSONDecodeError:
                return None
        return None

    for candidate in candidates:
        found = visit(candidate)
        if found is not None:
            return found
    return {}


def _tenant_access_token(app_id: str, app_secret: str) -> str:
    result = _feishu_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        "",
        {"app_id": app_id, "app_secret": app_secret},
    )
    token = result.get("tenant_access_token")
    if not token:
        raise RuntimeError(str(result.get("msg") or "飞书凭证验证失败"))
    return str(token)


def _feishu_json(url: str, token: str, body: Mapping[str, Any], method: str = "POST") -> Dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if int(result.get("code", 0)) != 0:
        raise RuntimeError(str(result.get("msg") or "飞书 API 调用失败"))
    return result
