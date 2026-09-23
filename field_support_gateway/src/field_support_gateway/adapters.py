"""Explicit external-system adapter interfaces and lark-cli implementations."""

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Protocol


class FeishuAdapter(Protocol):
    def send_handoff(self, handoff: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one handoff and return chat/topic binding metadata."""

    def update_issue_card(
        self,
        message_id: str,
        issue_id: str,
        state: str,
        solution: Optional[Mapping[str, Any]],
        observation: Optional[str] = None,
    ) -> None:
        """Replace the issue card after a workflow transition."""


class BaseAdapter(Protocol):
    def apply_event(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        """Project one gateway event into Base."""


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class LarkCliFeishuAdapter:
    profile: str
    support_chat_id: str
    binary: str = "lark-cli"
    timeout_seconds: int = 30
    runner: Runner = subprocess.run

    def send_handoff(self, handoff: Mapping[str, Any]) -> Mapping[str, Any]:
        content = json.dumps(_solution_card(handoff), ensure_ascii=False, separators=(",", ":"))
        topic_root = str(handoff.get("_topic_root_message_id") or "")
        command = [self.binary, "--profile", self.profile, "im"]
        if topic_root:
            command.extend(
                [
                    "+messages-reply",
                    "--message-id",
                    topic_root,
                    "--reply-in-thread",
                ]
            )
        else:
            command.extend(
                ["+messages-send", "--chat-id", self.support_chat_id]
            )
        command.extend(
            [
                "--as",
                "bot",
                "--msg-type",
                "interactive",
                "--content",
                content,
                "--idempotency-key",
                str(handoff.get("_idempotency_key", handoff["issue_id"])),
                "--format",
                "json",
            ]
        )
        completed = self.runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )
        result = json.loads(completed.stdout or "{}")
        message_id = _find_value(result, "message_id")
        if not message_id:
            raise RuntimeError("lark-cli response did not contain message_id")
        return {
            "chat_id": self.support_chat_id,
            "message_id": message_id,
            "topic_id": topic_root or message_id,
            "raw": result,
        }

    def update_issue_card(
        self,
        message_id: str,
        issue_id: str,
        state: str,
        solution: Optional[Mapping[str, Any]],
        observation: Optional[str] = None,
    ) -> None:
        card = _state_card(issue_id, state, solution or {}, observation)
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        command = [
            self.binary,
            "--profile",
            self.profile,
            "api",
            "PATCH",
            "/open-apis/im/v1/messages/{}".format(message_id),
            "--as",
            "bot",
            "--data",
            json.dumps({"content": content}, ensure_ascii=False, separators=(",", ":")),
            "--format",
            "json",
        ]
        self.runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )


def _solution_card(handoff: Mapping[str, Any]) -> Mapping[str, Any]:
    issue_id = str(handoff["issue_id"])
    summary = str(handoff.get("summary", ""))
    reporter = str(handoff.get("reporter_id", ""))
    evidence_count = len(handoff.get("evidence", []))
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": "{} 等待工程师处理".format(issue_id)},
        },
        "header": {
            "title": {"tag": "plain_text", "content": "现场问题处理"},
            "subtitle": {"tag": "plain_text", "content": issue_id},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "notice_colorful"},
            "text_tag_list": [
                {"tag": "text_tag", "text": {"tag": "plain_text", "content": "待处理"}, "color": "blue"}
            ],
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "large",
            "padding": "12px 12px 20px 12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": "**现场现象**\n{}\n\n<font color='grey'>上报人：{} · 已收集 {} 项证据</font>".format(
                        _escape_markdown(summary), _escape_markdown(reporter), evidence_count
                    ),
                },
                {
                    "tag": "form",
                    "name": "solution_form",
                    "direction": "vertical",
                    "vertical_spacing": "medium",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "actual_solution",
                            "required": True,
                            "input_type": "multiline_text",
                            "rows": 4,
                            "max_length": 1000,
                            "label": {"tag": "plain_text", "content": "实际解决方案"},
                            "placeholder": {"tag": "plain_text", "content": "填写已经执行的处理，例如重新插线、重启进程或提交记录"},
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
            ],
        },
    }


def _state_card(
    issue_id: str,
    state: str,
    solution: Mapping[str, Any],
    observation: Optional[str],
) -> Mapping[str, Any]:
    version = solution.get("version", "-")
    actual_solution = _escape_markdown(str(solution.get("actual_solution", "")))
    if state == "waiting_verification":
        title = "方案已提交，等待现场验证"
        template = "orange"
        content = "**解决方案 v{}**\n{}\n\n<font color='grey'>现场确认未解决后，本卡片会重新开放方案表单。</font>".format(
            version, actual_solution
        )
        elements = [{"tag": "markdown", "content": content}]
    elif state == "closed":
        title = "现场问题已解决"
        template = "green"
        elements = [
            {
                "tag": "markdown",
                "content": "**最终解决方案 v{}**\n{}\n\n<font color='grey'>现场人员已确认恢复。</font>".format(
                    version, actual_solution
                ),
            }
        ]
    elif state == "awaiting_engineer":
        title = "现场验证未通过"
        template = "red"
        elements = [
            {
                "tag": "markdown",
                "content": "**验证失败现象**\n{}\n\n**上一版方案 v{}**\n{}".format(
                    _escape_markdown(observation or ""), version, actual_solution
                ),
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
                        "label": {"tag": "plain_text", "content": "新的实际解决方案"},
                    },
                    {
                        "tag": "button",
                        "name": "submit_solution",
                        "text": {"tag": "plain_text", "content": "提交新方案"},
                        "type": "primary_filled",
                        "width": "fill",
                        "form_action_type": "submit",
                    },
                ],
            },
        ]
    else:
        raise ValueError("unsupported issue card state")
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": title}},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": issue_id},
            "template": template,
        },
        "body": {"elements": elements},
    }


def _escape_markdown(value: str) -> str:
    for source, replacement in (("&", "&#38;"), ("<", "&#60;"), (">", "&#62;"), ("*", "&#42;")):
        value = value.replace(source, replacement)
    return value


@dataclass
class LarkCliBaseAdapter:
    profile: str
    app_token: str
    table_id: str
    binary: str = "lark-cli"
    timeout_seconds: int = 30
    runner: Runner = subprocess.run

    def apply_event(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        fields = json.dumps(_base_fields(event), ensure_ascii=False, separators=(",", ":"))
        search_command = [
            self.binary,
            "--profile",
            self.profile,
            "base",
            "+record-search",
            "--as",
            "bot",
            "--base-token",
            self.app_token,
            "--table-id",
            self.table_id,
            "--keyword",
            str(event["issue_id"]),
            "--search-field",
            "问题编号",
            "--field-id",
            "问题编号",
            "--limit",
            "1",
            "--format",
            "json",
        ]
        searched = self.runner(
            search_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )
        search_result = json.loads(searched.stdout or "{}")
        record_id = _find_record_id(search_result)
        upsert_command = [
            self.binary,
            "--profile",
            self.profile,
            "base",
            "+record-upsert",
            "--as",
            "bot",
            "--base-token",
            self.app_token,
            "--table-id",
            self.table_id,
            "--json",
            fields,
            "--format",
            "json",
        ]
        if record_id is not None:
            upsert_command.extend(["--record-id", record_id])
        completed = self.runner(
            upsert_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )
        return json.loads(completed.stdout or "{}")


def _base_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    issue_id = str(event["issue_id"])
    kind = str(event["kind"])
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    timestamp = _base_datetime(str(event.get("occurred_at", "")))
    fields: dict[str, Any] = {"问题编号": issue_id, "最近活动时间": timestamp}
    if kind == "HandoffRequested":
        summary = str(payload.get("summary", "现场问题待处理"))
        fields.update(
            {
                "问题标题": summary[:80],
                "现象描述": summary,
                "上报原消息": summary,
                "状态": "open",
                "上报时间": timestamp,
                "上报类型": "基于旧问题上报" if "-S" in issue_id else "新问题",
            }
        )
        if "-S" in issue_id:
            fields["父问题编号"] = issue_id.split("-S", 1)[0]
    elif kind == "SolutionSubmitted":
        fields.update(
            {
                "状态": "待验证",
                "解决方案": str(payload.get("actual_solution", "")),
                "验证步骤": str(payload.get("verification_method") or "现场确认是否恢复"),
            }
        )
        actor_id = str(event.get("actor_id", ""))
        if actor_id.startswith("ou_"):
            fields["负责人"] = [{"id": actor_id}]
    elif kind == "ReporterConfirmed":
        fields.update({"状态": "已解决", "现场确认时间": timestamp})
    return fields


def _base_datetime(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _find_value(value: Any, key: str) -> Optional[str]:
    if isinstance(value, dict):
        if key in value:
            return str(value[key])
        for child in value.values():
            found = _find_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_value(child, key)
            if found is not None:
                return found
    return None


def _find_record_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        direct = value.get("record_id")
        if isinstance(direct, str) and direct:
            return direct
        identifiers = value.get("record_id_list")
        if isinstance(identifiers, list) and identifiers and isinstance(identifiers[0], str):
            return identifiers[0]
        for child in value.values():
            found = _find_record_id(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_record_id(child)
            if found is not None:
                return found
    return None
