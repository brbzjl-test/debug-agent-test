from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, Optional

from field_support_agent.analysis import ReadOnlyCodexRunner
from field_support_agent.analysis.streaming import ACTIVITY_STAGES
from field_support_agent.collectors import SnapshotCollector, SnapshotReport, build_evidence_bundle
from field_support_agent.domain import ConflictError, Issue, Message, ValidationError
from field_support_agent.integrations import GatewayClient

from .core import CoreService


LOGGER = logging.getLogger(__name__)


class RuntimeService:
    """Adds on-demand snapshot and analysis workers around the durable Core."""

    def __init__(
        self,
        core: CoreService,
        snapshot_collector: SnapshotCollector,
        codex_runner: Optional[ReadOnlyCodexRunner] = None,
        gateway_client: Optional[GatewayClient] = None,
        *,
        workers: int = 2,
    ) -> None:
        self.core = core
        self.snapshot_collector = snapshot_collector
        self.codex_runner = codex_runner
        self.gateway_client = gateway_client
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="field-support-worker")
        self._lock = threading.RLock()
        self._snapshots: Dict[str, SnapshotReport] = {}
        self._jobs: set[Future[Any]] = set()
        self._gateway_cursors: Dict[str, int] = {}
        self._analysis_locks: Dict[str, threading.Lock] = {}
        self._analysis_progress: Dict[str, dict] = {}
        self._handoff_jobs: set[str] = set()
        self._handoff_attempts: Dict[str, int] = {}
        self._handoff_retry_at: Dict[str, float] = {}
        self._deleted_issue_ids: set[str] = set()
        if self.gateway_client is not None:
            self._queue_pending_handoffs()

    def close(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        if self.gateway_client is not None and hasattr(self.gateway_client, "close"):
            self.gateway_client.close()

    def configure(self, snapshot_collector, codex_runner=None, gateway_client=None) -> None:
        with self._lock:
            previous_gateway = self.gateway_client
            self.snapshot_collector = snapshot_collector
            self.codex_runner = codex_runner
            self.gateway_client = gateway_client
        if previous_gateway is not None and previous_gateway is not gateway_client and hasattr(previous_gateway, "close"):
            previous_gateway.close()
        if gateway_client is not None:
            self._queue_pending_handoffs()

    def create_issue(self, reporter_id: str, description: Optional[str] = None) -> Issue:
        issue = self.core.create_issue(reporter_id, description)
        self._submit(self._capture, issue.issue_id, "issue_created")
        if description:
            self._submit(self._capture_and_analyze, issue.issue_id, description)
        return issue

    def create_subissue(
        self, selected_issue_id: str, reporter_id: str, description: Optional[str] = None
    ) -> Issue:
        issue = self.core.create_subissue(selected_issue_id, reporter_id, description)
        self._submit(self._capture, issue.issue_id, "subissue_created")
        if description:
            self._submit(self._capture_and_analyze, issue.issue_id, description)
        return issue

    def append_message(
        self, issue_id: str, actor_id: str, role: str, content: str, channel: str = "local"
    ) -> Message:
        message = self.core.append_message(issue_id, actor_id, role, content, channel)
        if role == "reporter" and channel == "local":
            self._submit(self._capture_and_analyze, issue_id, content)
        return message

    def latest_snapshot(self, issue_id: str) -> Optional[SnapshotReport]:
        with self._lock:
            return self._snapshots.get(issue_id)

    def request_handoff(self, issue_id: str, actor_id: str) -> Issue:
        issue = self.core.request_handoff(issue_id, actor_id)
        self._queue_handoff(issue_id)
        return issue

    def timeline(self, issue_id: str) -> Dict[str, Any]:
        issue = self.core.get_issue(issue_id)
        if self.gateway_client is not None and issue.handoff_state != "none":
            if issue.handoff_state in {"queued", "delivering"}:
                self._queue_handoff(issue_id)
            self._sync_gateway(issue_id)
        with self._lock:
            timeline = self.core.timeline(issue_id)
            progress = self._analysis_progress.get(issue_id)
            timeline["analysis"] = None
            if progress and timeline["issue"].local_input_enabled:
                now = time.monotonic()
                timeline["analysis"] = {
                    "status": progress["status"], "content": progress["content"], "stage": progress["stage"],
                    "elapsed_seconds": max(0, int(now - progress["started_at"])),
                    "idle_seconds": max(0, int(now - progress["last_activity_at"])),
                }
            return timeline

    def confirm_solution(self, issue_id: str, reporter_id: str, expected_solution_version: int) -> Issue:
        issue = self.core.confirm_solution(issue_id, reporter_id, expected_solution_version)
        if self.gateway_client is not None:
            self._submit(
                self.gateway_client.confirm,
                issue_id,
                expected_solution_version,
                reporter_id,
            )
        return issue

    def confirm_ai_resolution(self, issue_id: str, reporter_id: str) -> Issue:
        return self.core.confirm_ai_resolution(issue_id, reporter_id)

    def report_verification_failure(self, issue_id: str, reporter_id: str, observation: str) -> Issue:
        previous = self.core.get_issue(issue_id)
        issue = self.core.report_verification_failure(issue_id, reporter_id, observation)
        if self.gateway_client is not None:
            self._submit(
                self.gateway_client.verification_failed,
                issue_id,
                previous.latest_solution_version,
                reporter_id,
                observation,
            )
        return issue

    def delete_issues(self, issue_ids):
        if (not isinstance(issue_ids, list) or not 1 <= len(issue_ids) <= 1000
                or any(not isinstance(item, str) or not item.strip() for item in issue_ids)):
            raise ValidationError("请选择要删除的问题记录")
        root_ids = {self.core.get_issue(item).root_issue_id for item in issue_ids}
        with ExitStack() as stack:
            for root_id in sorted(root_ids):
                lock = self._analysis_lock(root_id)
                if not lock.acquire(blocking=False):
                    raise ConflictError("所选问题正在分析，请等待分析结束后再删除")
                stack.callback(lock.release)
            return self._delete_issues_locked(issue_ids, root_ids)

    def _delete_issues_locked(self, issue_ids, root_ids):
        existing = {item.issue_id: item for item in self.core.list_issues()}
        for issue_id in issue_ids:
            if issue_id not in existing:
                self.core.get_issue(issue_id)
        targets = set(issue_ids)
        selected_roots = {
            item.issue_id
            for item in existing.values()
            if item.issue_id in targets and item.issue_id == item.root_issue_id
        }
        targets.update(
            item.issue_id for item in existing.values() if item.root_issue_id in selected_roots
        )
        while True:
            descendants = {
                item.issue_id for item in existing.values() if item.parent_issue_id in targets
            }
            expanded = targets | descendants
            if expanded == targets:
                break
            targets = expanded
        # A subissue shares the root session. Delete the shared Codex copy and
        # rebuild from surviving local history on the next analysis.
        thread_ids = {self.core.codex_thread(root_id) for root_id in root_ids} - {None}
        if thread_ids:
            with self._lock:
                runner = self.codex_runner
            if runner is None:
                raise ConflictError("无法清理关联的 Codex 会话，请启用 Codex 后重试删除")
            try:
                runner.delete_conversations(sorted(thread_ids), self.core.forget_codex_thread)
            except Exception as exc:
                LOGGER.warning("Codex session cleanup failed: %s", exc)
                raise ConflictError("Codex 会话清理失败，本地问题记录已保留，请检查 Codex 配置后重试") from exc
        with self._lock:
            self._deleted_issue_ids.update(targets)
        deleted = self.core.delete_issues(issue_ids)
        state_dir = self.snapshot_collector.state_dir
        for issue_id in deleted:
            shutil.rmtree(state_dir / "issues" / issue_id, ignore_errors=True)
        with self._lock:
            for issue_id in deleted:
                self._snapshots.pop(issue_id, None)
                self._analysis_progress.pop(issue_id, None)
        gateway = self.gateway_client
        if gateway is not None and hasattr(gateway, "forget_issues"):
            gateway.forget_issues(deleted, selected_roots)
        else:
            self._forget_local_feishu_state(state_dir / "feishu-state.json", deleted, selected_roots)
        return deleted

    @staticmethod
    def _forget_local_feishu_state(path: Path, issue_ids, root_issue_ids) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        issues = value.get("issues")
        topics = value.get("topics")
        if isinstance(issues, dict):
            for issue_id in issue_ids:
                issues.pop(issue_id, None)
        if isinstance(topics, dict):
            for root_issue_id in root_issue_ids:
                topics.pop(root_issue_id, None)
        temporary = path.with_suffix(path.suffix + ".tmp")
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(path))
            os.chmod(str(path), 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def wait_for_idle(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            jobs = tuple(self._jobs)
        for job in jobs:
            job.result(timeout=timeout)

    def _submit(self, function, *args: Any) -> None:
        future = self._executor.submit(function, *args)
        with self._lock:
            self._jobs.add(future)
        future.add_done_callback(self._job_done)

    def _job_done(self, future: Future[Any]) -> None:
        with self._lock:
            self._jobs.discard(future)
        try:
            future.result()
        except Exception:
            LOGGER.exception("field-support background job failed")

    def _queue_pending_handoffs(self) -> None:
        for issue in self.core.list_issues():
            if issue.handoff_state != "none" and issue.status.value != "closed":
                self._queue_handoff(issue.issue_id)

    def _queue_handoff(self, issue_id: str) -> None:
        with self._lock:
            if self.gateway_client is None or issue_id in self._handoff_jobs:
                return
            if time.monotonic() < self._handoff_retry_at.get(issue_id, 0):
                return
            self._handoff_jobs.add(issue_id)
            future = self._executor.submit(self._send_handoff, issue_id)
            self._jobs.add(future)
        future.add_done_callback(lambda completed: self._handoff_done(issue_id, completed))

    def _handoff_done(self, issue_id: str, future: Future[Any]) -> None:
        with self._lock:
            self._jobs.discard(future)
            self._handoff_jobs.discard(issue_id)
        try:
            future.result()
        except Exception as exc:
            with self._lock:
                attempts = self._handoff_attempts.get(issue_id, 0) + 1
                self._handoff_attempts[issue_id] = attempts
                self._handoff_retry_at[issue_id] = time.monotonic() + min(60, 5 * (2 ** (attempts - 1)))
            LOGGER.warning("Feishu handoff pending for %s: %s", issue_id, exc)
        else:
            with self._lock:
                self._handoff_attempts.pop(issue_id, None)
                self._handoff_retry_at.pop(issue_id, None)

    def _capture(self, issue_id: str, trigger: str) -> SnapshotReport:
        with self._lock:
            if issue_id in self._deleted_issue_ids:
                raise RuntimeError("issue was deleted during snapshot collection")
            collector = self.snapshot_collector
        report = collector.capture(issue_id, trigger)
        with self._lock:
            if issue_id in self._deleted_issue_ids:
                shutil.rmtree(collector.state_dir / "issues" / issue_id, ignore_errors=True)
                raise RuntimeError("issue was deleted during snapshot collection")
            self._snapshots[issue_id] = report
        return report

    def _capture_and_analyze(self, issue_id: str, content: str) -> None:
        issue = self.core.get_issue(issue_id)
        analysis_lock = self._analysis_lock(issue.root_issue_id)
        with analysis_lock:
            with self._lock:
                if issue_id in self._deleted_issue_ids:
                    return
                now = time.monotonic()
                self._analysis_progress[issue_id] = {
                    "status": "running", "content": "", "stage": "capturing",
                    "started_at": now, "last_activity_at": now,
                }
            try:
                self._analyze_locked(issue_id, content)
            except Exception:
                LOGGER.exception("Codex analysis failed for %s", issue_id)
                with self._lock:
                    if issue_id not in self._deleted_issue_ids:
                        self._append_analysis_failure(issue_id)
            finally:
                with self._lock:
                    self._analysis_progress.pop(issue_id, None)

    def _update_analysis(self, issue_id: str, content: str) -> None:
        with self._lock:
            progress = self._analysis_progress.get(issue_id)
            if progress is not None and issue_id not in self._deleted_issue_ids:
                if content and content != progress["content"]:
                    progress["last_activity_at"] = time.monotonic()
                    progress["stage"] = "responding"
                progress["content"] = content

    def _update_activity(self, issue_id: str, stage: Optional[str]) -> None:
        if stage is not None and stage not in ACTIVITY_STAGES:
            return
        with self._lock:
            progress = self._analysis_progress.get(issue_id)
            if progress is not None and issue_id not in self._deleted_issue_ids:
                progress["last_activity_at"] = time.monotonic()
                if stage is not None:
                    progress["stage"] = stage

    def _analyze_locked(self, issue_id: str, content: str) -> None:
        issue = self.core.get_issue(issue_id)
        thread_id = self.core.codex_thread(issue_id)
        reporter_messages = [
            item for item in self.core.timeline(issue_id)["messages"] if item.role == "reporter"
        ]
        if issue.parent_issue_id:
            trigger = "recurrence_observation"
        elif thread_id or len(reporter_messages) > 1:
            trigger = "followup_observation"
        else:
            trigger = "first_observation"
        report = self._capture(issue_id, trigger)
        with self._lock:
            runner = self.codex_runner
        if runner is None:
            return

        rebuilt = False
        if thread_id:
            result = runner.resume_conversation(
                thread_id,
                issue.issue_id,
                issue.root_issue_id,
                content,
                Path(report.manifest_path),
                recurrence=issue.parent_issue_id is not None,
                on_update=lambda text: self._update_analysis(issue_id, text),
                on_activity=lambda stage: self._update_activity(issue_id, stage),
            )
            if result.session_missing and not result.session_busy:
                rebuilt = True
                self._update_analysis(issue_id, "")
                result = runner.start_conversation(
                    issue.issue_id,
                    self._conversation_seed(issue.issue_id, content),
                    Path(report.manifest_path),
                    on_update=lambda text: self._update_analysis(issue_id, text),
                    on_activity=lambda stage: self._update_activity(issue_id, stage),
                )
        else:
            result = runner.start_conversation(
                issue.issue_id,
                self._conversation_seed(issue.issue_id, content),
                Path(report.manifest_path),
                on_update=lambda text: self._update_analysis(issue_id, text),
                on_activity=lambda stage: self._update_activity(issue_id, stage),
            )

        if result.session_busy:
            LOGGER.warning("Codex session is occupied for %s: %s", issue_id, result.error)
            self._append_analysis_failure(
                issue_id,
                "当前问题的 Codex 会话正被其他窗口或进程占用，本次分析未启动。现场信息已保存，请释放会话占用后再试。",
            )
            return
        if result.ok and result.thread_id and (thread_id is None or rebuilt):
            self.core.save_codex_thread(issue.issue_id, result.thread_id, rebuilt=rebuilt)
        if not result.ok or not result.response or (thread_id is None or rebuilt) and not result.thread_id:
            LOGGER.warning("Codex analysis failed for %s: %s", issue_id, result.error)
            self._append_analysis_failure(issue_id)
            return
        refreshed = self.core.get_issue(issue_id)
        if not refreshed.local_input_enabled:
            return
        self.core.append_message(issue_id, "codex", "assistant", result.response, "system")

    def _analysis_lock(self, root_issue_id: str) -> threading.Lock:
        with self._lock:
            return self._analysis_locks.setdefault(root_issue_id, threading.Lock())

    def _conversation_seed(self, issue_id: str, latest_content: str) -> str:
        current = self.core.get_issue(issue_id)
        related = [
            item for item in self.core.list_issues() if item.root_issue_id == current.root_issue_id
        ]
        related.sort(key=lambda item: (item.created_at, item.issue_id))
        role_labels = {
            "reporter": "现场人员",
            "assistant": "Codex",
            "engineer": "工程师",
            "system": "系统",
        }
        lines = [
            "这是主问题 {} 的持续诊断记录。".format(current.root_issue_id),
            "以下是本地保存的完整问题上下文；请以当前问题 {} 的最新现场信息为重点。".format(issue_id),
        ]
        for item in related:
            lines.append("\n问题记录：{}{}".format(
                item.issue_id,
                "（复发记录）" if item.parent_issue_id else "（主问题）",
            ))
            timeline = self.core.timeline(item.issue_id)
            for message in timeline["messages"]:
                lines.append("{}：{}".format(role_labels.get(message.role, message.role), message.content))
            for solution in timeline["solutions"]:
                lines.append("工程师解决方案：{}".format(solution.content))
                if solution.verification_method:
                    lines.append("验证方法：{}".format(solution.verification_method))
        if not any("现场人员：" in line for line in lines):
            lines.append("现场人员：{}".format(latest_content))
        if current.parent_issue_id:
            lines.append("\n这是一次新的复发。可以参考历史，但不能默认根因与上次相同。")
        return "\n".join(lines)

    def _append_analysis_failure(self, issue_id: str, message: Optional[str] = None) -> None:
        issue = self.core.get_issue(issue_id)
        if issue.local_input_enabled:
            self.core.append_message(
                issue_id,
                "system",
                "assistant",
                message or "现场信息已经保存，但本次分析没有完成。请稍后再试；如果问题影响继续操作，请转给工程师处理。",
                "system",
            )

    def _send_handoff(self, issue_id: str) -> None:
        with self._lock:
            gateway = self.gateway_client
        if gateway is None:
            return
        report = self.latest_snapshot(issue_id) or self._capture(issue_id, "human_handoff")
        timeline = self.core.timeline(issue_id)
        issue = timeline["issue"]
        reporter_messages = [item.content for item in timeline["messages"] if item.role == "reporter"]
        summary = reporter_messages[-1] if reporter_messages else "现场问题待工程师处理"
        card_summary = self._card_summary(timeline["messages"])
        conversation = [
            {
                "actor_id": item.actor_id,
                "role": item.role,
                "channel": item.channel,
                "content": item.content,
                "created_at": item.created_at,
            }
            for item in timeline["messages"]
        ]
        evidence_bundle = build_evidence_bundle(report, issue.issue_id, card_summary, conversation)
        evidence = [
            {
                "name": item.name,
                "ok": item.ok,
                "source": item.source,
                "sha256": item.sha256,
                "output_file": item.output_file,
            }
            for item in getattr(report, "evidence", ())
        ]
        gateway.handoff(
            {
                "issue_id": issue.issue_id,
                "root_issue_id": issue.root_issue_id,
                "parent_issue_id": issue.parent_issue_id,
                "issue_status": issue.status.value,
                "reporter_id": issue.reporter_id,
                "summary": summary,
                "card_summary": card_summary,
                "snapshot_id": getattr(report, "snapshot_id", None),
                "snapshot_manifest": report.manifest_path,
                "evidence_bundle": str(evidence_bundle),
                "evidence": evidence,
            },
            "handoff:{}".format(issue.issue_id),
        )
        self._sync_gateway(issue_id, gateway)

    @staticmethod
    def _card_summary(messages) -> str:
        reporter = next((item.content for item in reversed(messages) if item.role == "reporter"), "现场问题待处理")
        analysis = next((item.content for item in reversed(messages) if item.role == "assistant"), "")
        value = "现象：{}".format(" ".join(reporter.split()))
        if analysis:
            value += "；AI：{}".format(" ".join(analysis.split()))
        return value if len(value) <= 100 else value[:97].rstrip() + "..."

    def _sync_gateway(self, issue_id: str, gateway=None) -> None:
        with self._lock:
            gateway = gateway or self.gateway_client
        if gateway is None:
            return
        try:
            payload = gateway.sync(issue_id, self._gateway_cursors.get(issue_id, 0))
            self._gateway_cursors[issue_id] = int(payload.get("cursor", 0))
            remote_issue = payload.get("issue") or {}
            local_issue = self.core.get_issue(issue_id)
            if remote_issue.get("handoff_status") in {"delivered", "handling", "solution_available"}:
                if local_issue.handoff_state in {"queued", "delivering"}:
                    self.core.mark_handoff_delivered(issue_id)
            solution = payload.get("latest_solution")
            if isinstance(solution, dict):
                version = int(solution.get("version", 0))
                if version > self.core.get_issue(issue_id).latest_solution_version:
                    self.core.import_solution(
                        issue_id,
                        version,
                        str(solution.get("engineer_id", "engineer")),
                        str(solution.get("actual_solution", "")),
                        solution.get("verification_method"),
                    )
        except Exception as exc:
            LOGGER.info("gateway sync pending for %s: %s", issue_id, exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)
