from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .streaming import AppServerRequestError, AppServerStream
from .session_cleanup import delete_sessions


class SecurityViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisResult:
    issue_id: str
    ok: bool
    response: str
    events: tuple[dict, ...]
    error: Optional[str] = None
    thread_id: Optional[str] = None
    session_missing: bool = False
    session_busy: bool = False


class ReadOnlyCodexRunner:
    """Runs Codex with read-only sandboxing and verifies source trees are unchanged."""

    def __init__(
        self,
        repositories: Sequence[Path],
        state_dir: Path,
        *,
        codex_binary: str = "codex",
        codex_home: Optional[Path] = None,
        model: Optional[str] = None,
        reasoning_effort: str = "low",
        timeout_seconds: float = 300.0,
    ) -> None:
        if not repositories:
            raise ValueError("at least one repository is required")
        if reasoning_effort not in {"", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("unsupported Codex reasoning effort")
        self.repositories = tuple(path.expanduser().resolve(strict=True) for path in repositories)
        self.state_dir = state_dir.expanduser().resolve()
        self.codex_binary = shutil.which(codex_binary) or codex_binary
        self.codex_home = codex_home.expanduser().resolve() if codex_home else None
        self.model = model.strip() if model and model.strip() else None
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    def start_conversation(self, issue_id: str, prompt: str, snapshot_manifest: Path,
                           *, on_update: Optional[Callable[[str], None]] = None,
                           on_activity: Optional[Callable[[Optional[str]], None]] = None) -> AnalysisResult:
        return self._run(issue_id, self._build_prompt(issue_id, prompt, snapshot_manifest), None, on_update, on_activity)

    def resume_conversation(
        self,
        thread_id: str,
        issue_id: str,
        root_issue_id: str,
        prompt: str,
        snapshot_manifest: Path,
        *,
        recurrence: bool = False,
        on_update: Optional[Callable[[str], None]] = None,
        on_activity: Optional[Callable[[Optional[str]], None]] = None,
    ) -> AnalysisResult:
        followup = self._build_followup_prompt(
            issue_id, root_issue_id, prompt, snapshot_manifest, recurrence=recurrence
        )
        return self._run(issue_id, followup, thread_id, on_update, on_activity)

    def analyze(self, issue_id: str, prompt: str, snapshot_manifest: Path) -> AnalysisResult:
        """Compatibility entry point for callers that start a new conversation."""
        return self.start_conversation(issue_id, prompt, snapshot_manifest)

    def delete_conversations(self, thread_ids, on_deleted) -> None:
        """Management operation; never exposed as a model tool."""
        command = self._command()
        command = command[:command.index("exec")] + ["app-server", "--listen", "stdio://"]
        delete_sessions(command, self._minimal_environment(), thread_ids, on_deleted)

    def _run(self, issue_id: str, prompt: str, thread_id: Optional[str],
             on_update: Optional[Callable[[str], None]] = None,
             on_activity: Optional[Callable[[Optional[str]], None]] = None) -> AnalysisResult:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if on_activity:
            on_activity("preparing")
        before = {str(path): self._fingerprint(path) for path in self.repositories}
        issue_dir = self.state_dir / "issues" / issue_id / "analysis"
        issue_dir.mkdir(parents=True, exist_ok=True)

        if on_update is not None and not self._has_external_rules():
            return self._run_streaming(issue_id, prompt, thread_id, on_update, before, issue_dir, on_activity)

        command = self._command(thread_id)
        env = self._minimal_environment()
        if on_activity:
            on_activity("limited")
        try:
            proc = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self._assert_unchanged(before)
            return AnalysisResult(
                issue_id=issue_id,
                ok=False,
                response="",
                events=(),
                error="Codex analysis timed out",
                thread_id=thread_id,
            )

        self._assert_unchanged(before)
        events = self._parse_jsonl(proc.stdout)
        response = self._last_agent_message(events)
        error = None if proc.returncode == 0 else (proc.stderr.strip() or f"Codex exited with {proc.returncode}")
        result_thread_id = self._thread_id(events) or thread_id
        result = AnalysisResult(
            issue_id=issue_id,
            ok=proc.returncode == 0,
            response=response,
            events=tuple(events),
            error=error,
            thread_id=result_thread_id,
            session_missing=self._session_missing(error),
            session_busy=bool(thread_id and self._session_busy(error)),
        )
        result_path = issue_dir / "latest.json"
        result_path.write_text(
            json.dumps(
                {
                    "issue_id": issue_id,
                    "ok": result.ok,
                    "response": result.response,
                    "events": events,
                    "error": error,
                    "thread_id": result.thread_id,
                    "session_missing": result.session_missing,
                    "session_busy": result.session_busy,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return result

    def _has_external_rules(self) -> bool:
        # app-server has no --ignore-rules equivalent. Preserve the existing exec
        # isolation when local execpolicy files could allow commands outside the sandbox.
        home = self.codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        roots = {home / "rules", Path.home() / ".agents" / "rules"}
        for repository in self.repositories:
            for parent in (repository, *repository.parents):
                roots.update((parent / ".codex" / "rules", parent / ".agents" / "rules"))
        return any(path.is_file() for root in roots for path in root.glob("**/*.rules"))

    def _run_streaming(self, issue_id, prompt, thread_id, on_update, before, issue_dir, on_activity=None):
        command = self._command()
        command = command[:command.index("exec")] + ["app-server", "--listen", "stdio://"]
        response, events, error = "", [], None
        session_busy = False
        try:
            response, events, thread_id = AppServerStream(
                command, self._minimal_environment(), self.timeout_seconds,
            ).run(prompt, thread_id, str(self.repositories[0]), self.model, self.reasoning_effort, on_update, on_activity)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            error = str(exc)
            # Identify a rejected resume so the UI can explain why analysis did not start.
            session_busy = (isinstance(exc, AppServerRequestError)
                            and exc.method == "thread/resume" and self._session_busy(error))
        finally:
            self._assert_unchanged(before)
        result = AnalysisResult(
            issue_id, error is None, response, tuple(events), error,
            thread_id=thread_id, session_missing=self._session_missing(error),
            session_busy=session_busy,
        )
        (issue_dir / "latest.json").write_text(json.dumps({
            "issue_id": issue_id, "ok": result.ok, "response": response, "events": events,
            "error": error, "thread_id": thread_id, "session_missing": result.session_missing,
            "session_busy": result.session_busy,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _command(self, thread_id: Optional[str] = None) -> list[str]:
        command = [
            self.codex_binary,
            "-a",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(self.repositories[0]),
        ]
        if self.model:
            command.extend(("--model", self.model))
        command.extend([
            "--disable",
            "plugins",
            "--disable",
            "apps",
            "--disable",
            "skill_search",
            "--disable",
            "multi_agent",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "hooks",
            "-c",
            "mcp_servers={}",
            "-c",
            'network_access="disabled"',
        ])
        if self.reasoning_effort:
            command.extend(("-c", 'model_reasoning_effort="{}"'.format(self.reasoning_effort)))
        command.append("exec")
        if thread_id:
            command.extend(("resume", "--ignore-rules", "--json", thread_id, "-"))
        else:
            command.extend(("--ignore-rules", "--json", "-"))
        return command

    def _build_prompt(self, issue_id: str, user_prompt: str, manifest: Path) -> str:
        manifest = manifest.expanduser().resolve(strict=True)
        repos = "\n".join(f"- {path}" for path in self.repositories)
        return f"""你是现场调试分析助手。问题 ID：{issue_id}。

最高规则：严禁修改、创建、删除或重命名任何业务代码、配置和脚本；严禁执行 Git 写操作、部署、重启、ROS 发布或设备控制。即使用户明确要求也只能给出由人员执行的步骤。只能读取证据和代码并分析。

业务仓库（只读）：
{repos}

Snapshot manifest：{manifest}

回复对象是非技术现场人员。请先在内部核对日志、Snapshot 和代码，再用简单、明确的中文回答。
固定输出格式：
结论：用 1 到 2 句话说明目前最可能的情况；证据不足时直接说“暂时无法确定”。
请按顺序操作：最多 5 步，每步只写一个现场人员能执行的具体动作。优先使用检查线缆、确认指示灯、切换界面、重新尝试等直观操作。除非没有替代办法，不要要求输入终端命令。
完成后告诉我：明确要求现场反馈哪一个可观察结果。
需要工程师时：说明需要转人工，并用一句话说明原因。

不要输出长篇技术分析、内部文件路径、代码行号或大段日志。不要让现场人员修改代码、配置或 Git。不要把推断写成确定事实。

现场描述：
{user_prompt}
"""

    def _build_followup_prompt(
        self,
        issue_id: str,
        root_issue_id: str,
        user_prompt: str,
        manifest: Path,
        *,
        recurrence: bool,
    ) -> str:
        resolved_manifest = manifest.expanduser().resolve(strict=True)
        recurrence_note = (
            "这是关联问题的一次新的复发记录。可以参考历史，但不得默认本次根因与上次相同。\n"
            if recurrence
            else ""
        )
        return f"""当前问题记录：{issue_id}
所属主问题：{root_issue_id}
{recurrence_note}
现场新增信息：
{user_prompt}

本轮 Snapshot：{resolved_manifest}

请结合本对话已有的现场描述、分析和证据，比较本轮状态并更新判断。
继续遵守最高规则：只能读取，严禁修改业务代码、配置或 Git，严禁执行部署、重启、ROS 发布或设备控制。
回复必须面向非技术现场人员，保持简短，并按“结论 / 请按顺序操作 / 完成后告诉我 / 需要工程师时”输出。操作最多 5 步，每步只能包含一个具体动作。
"""

    def _minimal_environment(self) -> dict[str, str]:
        allowed = ("PATH", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        if self.codex_home:
            env["CODEX_HOME"] = str(self.codex_home)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["PAGER"] = "cat"
        return env

    def _assert_unchanged(self, before: Mapping[str, str]) -> None:
        after = {str(path): self._fingerprint(path) for path in self.repositories}
        changed = [path for path, digest in before.items() if after.get(path) != digest]
        if changed:
            raise SecurityViolation("business repository changed during analysis: " + ", ".join(changed))

    @staticmethod
    def _fingerprint(repository: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(repository.rglob("*")):
            if ".git" in path.parts or not path.is_file():
                continue
            relative = path.relative_to(repository).as_posix().encode("utf-8")
            stat = path.stat()
            digest.update(relative)
            digest.update(str(stat.st_size).encode("ascii"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        for argv in (
            ("git", "rev-parse", "HEAD"),
            ("git", "symbolic-ref", "-q", "HEAD"),
            ("git", "status", "--porcelain=v2", "--untracked-files=all"),
            ("git", "diff", "--binary"),
            ("git", "diff", "--cached", "--binary"),
        ):
            completed = subprocess.run(
                argv,
                cwd=repository,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            digest.update(" ".join(argv).encode("utf-8"))
            digest.update(str(completed.returncode).encode("ascii"))
            digest.update(completed.stdout)
        return digest.hexdigest()

    @staticmethod
    def _parse_jsonl(output: str) -> list[dict]:
        events: list[dict] = []
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    @staticmethod
    def _last_agent_message(events: Sequence[dict]) -> str:
        for event in reversed(events):
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    return str(item.get("text", ""))
            if event.get("type") == "message" and event.get("role") == "assistant":
                return str(event.get("content", ""))
        return ""

    @staticmethod
    def _thread_id(events: Sequence[dict]) -> Optional[str]:
        for event in events:
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        return None

    @staticmethod
    def _session_missing(error: Optional[str]) -> bool:
        if not error:
            return False
        lowered = error.lower()
        markers = ("session not found", "unknown session", "rollout not found", "no rollout", "failed to find session")
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _session_busy(error: Optional[str]) -> bool:
        return bool(error and "already has an active writer" in error.lower())
