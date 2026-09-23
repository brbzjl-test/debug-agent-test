from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)((?:access|refresh|api|app)[_-]?token\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)((?:app|client)[_-]?secret\s*[:=]\s*)(\S+)"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact(text: str) -> str:
    result = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED PRIVATE KEY]", result)
    return result


@dataclass(frozen=True)
class EvidenceResult:
    name: str
    source: str
    started_at: str
    finished_at: str
    ok: bool
    exit_code: Optional[int]
    duration_ms: int
    output_file: Optional[str]
    sha256: Optional[str]
    missing_reason: Optional[str]
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class CommandCollector:
    """Runs fixed argv commands without a shell and stores bounded output."""

    def __init__(self, output_dir: Path, timeout_seconds: float = 5.0, max_bytes: int = 512_000):
        self.output_dir = output_dir
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        output_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> EvidenceResult:
        started = utc_now()
        start_ns = __import__("time").monotonic_ns()
        executable = shutil.which(argv[0])
        if executable is None:
            return self._missing(name, "command", started, start_ns, f"command not found: {argv[0]}")

        command = [executable, *argv[1:]]
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                env=dict(env) if env else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout_seconds,
                check=False,
            )
            raw = proc.stdout or b""
            truncated = len(raw) > self.max_bytes
            raw = raw[: self.max_bytes]
            text = redact(raw.decode("utf-8", errors="replace"))
            path, digest = self._write(name, text)
            return EvidenceResult(
                name=name,
                source="argv:" + json.dumps(list(argv), ensure_ascii=False),
                started_at=started,
                finished_at=utc_now(),
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                duration_ms=self._elapsed_ms(start_ns),
                output_file=str(path),
                sha256=digest,
                missing_reason=None if proc.returncode == 0 else f"command exited with {proc.returncode}",
                truncated=truncated,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or b"")[: self.max_bytes]
            text = redact(partial.decode("utf-8", errors="replace"))
            path, digest = self._write(name, text)
            return EvidenceResult(
                name=name,
                source="argv:" + json.dumps(list(argv), ensure_ascii=False),
                started_at=started,
                finished_at=utc_now(),
                ok=False,
                exit_code=None,
                duration_ms=self._elapsed_ms(start_ns),
                output_file=str(path),
                sha256=digest,
                missing_reason=f"timed out after {self.timeout_seconds}s",
                truncated=len(partial) >= self.max_bytes,
            )
        except OSError as exc:
            return self._missing(name, "command", started, start_ns, str(exc))

    def _write(self, name: str, text: str) -> tuple[Path, str]:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "evidence"
        path = self.output_dir / f"{safe_name}.txt"
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, digest

    def _missing(self, name: str, source: str, started: str, start_ns: int, reason: str) -> EvidenceResult:
        return EvidenceResult(
            name=name,
            source=source,
            started_at=started,
            finished_at=utc_now(),
            ok=False,
            exit_code=None,
            duration_ms=self._elapsed_ms(start_ns),
            output_file=None,
            sha256=None,
            missing_reason=reason,
        )

    @staticmethod
    def _elapsed_ms(start_ns: int) -> int:
        return max(0, (__import__("time").monotonic_ns() - start_ns) // 1_000_000)


def read_bounded_files(
    roots: Iterable[Path],
    output_dir: Path,
    *,
    patterns: Sequence[str],
    max_files: int = 30,
    tail_bytes: int = 128_000,
) -> list[EvidenceResult]:
    results: list[EvidenceResult] = []
    candidates: list[Path] = []
    for root in roots:
        resolved_root = root.expanduser().resolve()
        if not resolved_root.exists():
            continue
        for pattern in patterns:
            candidates.extend(path for path in resolved_root.glob(pattern) if path.is_file())

    unique = sorted(set(candidates), key=lambda path: path.stat().st_mtime, reverse=True)[:max_files]
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(unique):
        started = utc_now()
        start_ns = __import__("time").monotonic_ns()
        try:
            with source.open("rb") as handle:
                size = source.stat().st_size
                if size > tail_bytes:
                    handle.seek(-tail_bytes, os.SEEK_END)
                raw = handle.read(tail_bytes)
            target = output_dir / f"log_{index:03d}_{source.name}"
            target.write_text(redact(raw.decode("utf-8", errors="replace")), encoding="utf-8")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            results.append(
                EvidenceResult(
                    name=f"log:{source.name}",
                    source=str(source),
                    started_at=started,
                    finished_at=utc_now(),
                    ok=True,
                    exit_code=None,
                    duration_ms=CommandCollector._elapsed_ms(start_ns),
                    output_file=str(target),
                    sha256=digest,
                    missing_reason=None,
                    truncated=size > tail_bytes,
                )
            )
        except OSError as exc:
            results.append(
                EvidenceResult(
                    name=f"log:{source.name}",
                    source=str(source),
                    started_at=started,
                    finished_at=utc_now(),
                    ok=False,
                    exit_code=None,
                    duration_ms=CommandCollector._elapsed_ms(start_ns),
                    output_file=None,
                    sha256=None,
                    missing_reason=str(exc),
                )
            )
    return results

