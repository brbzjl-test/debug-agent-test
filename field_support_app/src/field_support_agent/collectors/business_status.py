from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .base import utc_now


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command: str
    cwd: Optional[str] = None
    elapsed_seconds: Optional[int] = None


@dataclass(frozen=True)
class ProcessRuntime:
    pid: int
    elapsed_seconds: Optional[int]


@dataclass(frozen=True)
class ProgramStatus:
    name: str
    local_path: str
    state: str
    process_count: int
    pids: tuple[int, ...]
    processes: tuple[ProcessRuntime, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["pids"] = list(self.pids)
        value["processes"] = [asdict(item) for item in self.processes]
        return value


@dataclass(frozen=True)
class BusinessStatusReport:
    checked_at: str
    available: bool
    programs: tuple[ProgramStatus, ...]

    def to_dict(self) -> dict:
        running = sum(item.state == "running" for item in self.programs)
        return {
            "checked_at": self.checked_at,
            "available": self.available,
            "detection": "repository_path",
            "summary": {"total": len(self.programs), "running": running},
            "programs": [item.to_dict() for item in self.programs],
        }


class BusinessStatusCollector:
    """Find processes that execute code from a configured repository."""

    def __init__(
        self,
        repositories: Sequence[Mapping[str, str]],
        *,
        process_provider: Optional[Callable[[], Sequence[ProcessInfo]]] = None,
    ) -> None:
        self.repositories = tuple(repositories)
        self.process_provider = process_provider or self._processes

    def collect(self) -> BusinessStatusReport:
        try:
            processes = tuple(self.process_provider())
            available = True
        except (OSError, subprocess.SubprocessError, ValueError):
            processes = ()
            available = False

        programs = []
        for repository in self.repositories:
            path = Path(str(repository["local_path"])).expanduser().resolve()
            matches = tuple(process for process in processes if self._matches(path, process)) if available else ()
            matches = tuple(sorted(matches, key=lambda process: process.pid))
            programs.append(
                ProgramStatus(
                    name=str(repository["name"]),
                    local_path=str(path),
                    state="running" if matches else ("not_running" if available else "unknown"),
                    process_count=len(matches),
                    pids=tuple(process.pid for process in matches),
                    processes=tuple(ProcessRuntime(process.pid, process.elapsed_seconds) for process in matches),
                )
            )
        return BusinessStatusReport(utc_now(), available, tuple(programs))

    @staticmethod
    def _matches(repository: Path, process: ProcessInfo) -> bool:
        path_text = str(repository)
        if path_text in process.command:
            return True
        if not process.cwd:
            return False
        try:
            cwd = Path(process.cwd).resolve()
            cwd.relative_to(repository)
        except (OSError, ValueError):
            return False

        try:
            argv = shlex.split(process.command)
        except ValueError:
            return False
        if not argv:
            return False
        executable = Path(argv[0]).name.lstrip("-").lower()
        if executable in {"ros2", "roslaunch", "rosrun"}:
            return True
        if executable.startswith("python") and "-m" in argv[1:]:
            return True

        source_suffixes = {
            "python": (".py",),
            "python3": (".py",),
            "node": (".js", ".mjs", ".cjs"),
        }
        suffixes = source_suffixes.get(executable, ())
        for argument in argv[1:]:
            if not argument.lower().endswith(suffixes):
                continue
            source = Path(argument)
            source = source if source.is_absolute() else cwd / source
            try:
                source.resolve().relative_to(repository)
                return True
            except (OSError, ValueError):
                continue
        return False

    @classmethod
    def _processes(cls) -> Sequence[ProcessInfo]:
        executable = shutil.which("ps")
        if executable is None:
            raise OSError("ps is unavailable")
        result = subprocess.run(
            [executable, "-axo", "pid=,etime=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            raise subprocess.SubprocessError("ps failed")

        rows = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if not fields or not fields[0].isdigit():
                continue
            elapsed = cls._parse_elapsed(fields[1]) if len(fields) > 1 else None
            command = fields[2] if len(fields) > 2 else ""
            rows.append(ProcessInfo(int(fields[0]), command, elapsed_seconds=elapsed))

        cwd_by_pid = cls._cwd_by_pid(tuple(item.pid for item in rows))
        return tuple(
            ProcessInfo(item.pid, item.command, cwd_by_pid.get(item.pid), item.elapsed_seconds)
            for item in rows
        )

    @staticmethod
    def _parse_elapsed(value: str) -> Optional[int]:
        try:
            day_parts = value.strip().split("-", maxsplit=1)
            days = int(day_parts[0]) if len(day_parts) == 2 else 0
            clock = day_parts[-1].split(":")
            if len(clock) == 3:
                hours, minutes, seconds = (int(part) for part in clock)
            elif len(clock) == 2:
                hours = 0
                minutes, seconds = (int(part) for part in clock)
            else:
                return None
            if min(days, hours, minutes, seconds) < 0 or minutes >= 60 or seconds >= 60:
                return None
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _cwd_by_pid(pids: Sequence[int]) -> dict[int, str]:
        if platform.system() == "Linux":
            result = {}
            for pid in pids:
                try:
                    result[pid] = os.readlink("/proc/{}/cwd".format(pid))
                except OSError:
                    continue
            return result

        if platform.system() != "Darwin":
            return {}
        executable = shutil.which("lsof")
        if executable is None:
            return {}
        try:
            completed = subprocess.run(
                [executable, "-d", "cwd", "-Fpn"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        result = {}
        current_pid = None
        for line in completed.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current_pid = int(line[1:])
            elif line.startswith("n") and current_pid is not None:
                result[current_pid] = BusinessStatusCollector._decode_lsof_path(line[1:])
        return result

    @staticmethod
    def _decode_lsof_path(value: str) -> str:
        raw = bytearray()
        index = 0
        while index < len(value):
            if value[index : index + 2] == "\\x" and index + 4 <= len(value):
                try:
                    raw.append(int(value[index + 2 : index + 4], 16))
                    index += 4
                    continue
                except ValueError:
                    pass
            raw.extend(value[index].encode("utf-8"))
            index += 1
        return raw.decode("utf-8", errors="replace")
