from __future__ import annotations

import json
import platform
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .base import CommandCollector, EvidenceResult, read_bounded_files, utc_now


@dataclass(frozen=True)
class SnapshotReport:
    snapshot_id: str
    issue_id: str
    trigger: str
    started_at: str
    finished_at: str
    evidence: tuple[EvidenceResult, ...]
    manifest_path: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data


class SnapshotCollector:
    """Collects best-effort evidence without mutating configured business paths."""

    def __init__(
        self,
        state_dir: Path,
        repositories: Sequence[Mapping[str, str]],
        expected_ros_topology: Optional[Path] = None,
        *,
        log_paths: Sequence[Path] = (),
        command_timeout_seconds: float = 5.0,
    ) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.repositories = tuple(self._validated_repository(item) for item in repositories)
        self.expected_ros_topology = expected_ros_topology.expanduser().resolve() if expected_ros_topology else None
        self.log_paths = tuple(Path(item).expanduser().resolve() for item in log_paths)
        self.command_timeout_seconds = command_timeout_seconds

    @staticmethod
    def _validated_repository(item: Mapping[str, str]) -> dict[str, str]:
        required = {"name", "git_url", "local_path"}
        missing = required.difference(item)
        if missing:
            raise ValueError(f"repository is missing: {', '.join(sorted(missing))}")
        path = Path(item["local_path"]).expanduser()
        if not path.is_absolute():
            raise ValueError("repository local_path must be absolute")
        return {"name": item["name"], "git_url": item["git_url"], "local_path": str(path.resolve())}

    def capture(self, issue_id: str, trigger: str) -> SnapshotReport:
        if not issue_id or "/" in issue_id or ".." in issue_id:
            raise ValueError("invalid issue_id")
        snapshot_id = f"SNP-{uuid.uuid4().hex.upper()[:12]}"
        started_at = utc_now()
        output_dir = self.state_dir / "issues" / issue_id / "snapshots" / snapshot_id
        evidence_dir = output_dir / "evidence"
        command = CommandCollector(evidence_dir, timeout_seconds=self.command_timeout_seconds)
        results: list[EvidenceResult] = []

        for name, argv in self._system_commands():
            results.append(command.run(name, argv))

        for repository in self.repositories:
            results.extend(self._collect_repository(command, repository))

        log_roots = [Path(item["local_path"]) for item in self.repositories]
        log_roots.extend(self.log_paths)
        ros_log = Path.home() / ".ros" / "log"
        if ros_log.exists():
            log_roots.append(ros_log)
        results.extend(
            read_bounded_files(
                log_roots,
                evidence_dir / "logs",
                patterns=("*.log", "logs/*.log", "log/*.log", "**/latest/*.log"),
            )
        )

        if shutil.which("ros2"):
            for name, argv in self._ros_commands():
                results.append(command.run(name, argv))

        if self.expected_ros_topology:
            results.append(self._copy_expected_topology(self.expected_ros_topology, evidence_dir))

        finished_at = utc_now()
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = output_dir / "manifest.json"
        manifest = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "issue_id": issue_id,
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": finished_at,
            "platform": platform.platform(),
            "repositories": self.repositories,
            "evidence": [item.to_dict() for item in results],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return SnapshotReport(
            snapshot_id=snapshot_id,
            issue_id=issue_id,
            trigger=trigger,
            started_at=started_at,
            finished_at=finished_at,
            evidence=tuple(results),
            manifest_path=str(manifest_path),
        )

    @staticmethod
    def _system_commands() -> list[tuple[str, list[str]]]:
        commands = [
            ("uname", ["uname", "-a"]),
            ("uptime", ["uptime"]),
            ("processes", ["ps", "-axo", "pid,ppid,etime,state,%cpu,%mem,command"]),
            ("disk", ["df", "-h"]),
        ]
        if platform.system() == "Darwin":
            commands.extend(
                [
                    ("network_interfaces", ["ifconfig"]),
                    ("network_sockets", ["netstat", "-an"]),
                    ("usb_devices", ["system_profiler", "SPUSBDataType"]),
                ]
            )
        else:
            commands.extend(
                [
                    ("memory", ["free", "-h"]),
                    ("network_interfaces", ["ip", "-details", "address"]),
                    ("network_sockets", ["ss", "-lntup"]),
                    ("usb_devices", ["lsusb"]),
                    ("pci_devices", ["lspci", "-nn"]),
                    ("kernel_errors", ["journalctl", "-k", "-p", "warning", "-n", "200", "--no-pager"]),
                ]
            )
        return commands

    @staticmethod
    def _ros_commands() -> list[tuple[str, list[str]]]:
        return [
            ("ros_nodes", ["ros2", "node", "list"]),
            ("ros_topics", ["ros2", "topic", "list", "-t"]),
            ("ros_services", ["ros2", "service", "list", "-t"]),
            ("ros_actions", ["ros2", "action", "list", "-t"]),
        ]

    @staticmethod
    def _collect_repository(command: CommandCollector, repository: Mapping[str, str]) -> list[EvidenceResult]:
        name = repository["name"]
        path = Path(repository["local_path"])
        if not path.exists():
            return [command._missing(f"git_{name}", str(path), utc_now(), __import__("time").monotonic_ns(), "repository path not found")]
        return [
            command.run(f"git_{name}_remote", ["git", "remote", "-v"], cwd=path),
            command.run(f"git_{name}_head", ["git", "rev-parse", "HEAD"], cwd=path),
            command.run(f"git_{name}_branch", ["git", "branch", "--show-current"], cwd=path),
            command.run(f"git_{name}_status", ["git", "status", "--short", "--branch"], cwd=path),
            command.run(f"git_{name}_diff_stat", ["git", "diff", "--stat"], cwd=path),
            command.run(f"git_{name}_recent", ["git", "log", "-5", "--date=iso", "--pretty=format:%H%x09%ad%x09%an%x09%s"], cwd=path),
        ]

    @staticmethod
    def _copy_expected_topology(source: Path, output_dir: Path) -> EvidenceResult:
        started = utc_now()
        start_ns = __import__("time").monotonic_ns()
        if not source.is_file():
            return CommandCollector(output_dir)._missing("expected_ros_topology", str(source), started, start_ns, "file not found")
        raw = source.read_bytes()[:512_000]
        target = output_dir / "expected_ros_topology.yaml"
        target.write_bytes(raw)
        digest = __import__("hashlib").sha256(raw).hexdigest()
        return EvidenceResult(
            name="expected_ros_topology",
            source=str(source),
            started_at=started,
            finished_at=utc_now(),
            ok=True,
            exit_code=None,
            duration_ms=CommandCollector._elapsed_ms(start_ns),
            output_file=str(target),
            sha256=digest,
            missing_reason=None,
            truncated=source.stat().st_size > len(raw),
        )
