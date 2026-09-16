#!/usr/bin/env python3
"""Deterministic business-process simulator for field-support testing."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Sequence


SCENARIOS = (
    "normal",
    "device_missing",
    "software_conflict",
    "intermittent",
    "dependency_error",
    "process_crash",
    "silent_hang",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ContextFilter(logging.Filter):
    def __init__(self, run_id: str, scenario: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.scenario = scenario

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        record.scenario = self.scenario
        record.component = getattr(record, "component", "business_app")
        record.error_code = getattr(record, "error_code", "NONE")
        return True


class MaxLevelFilter(logging.Filter):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.maximum


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value = {
            "timestamp": utc_now(),
            "level": record.levelname,
            "pid": record.process,
            "run_id": record.run_id,
            "component": record.component,
            "scenario": record.scenario,
            "error_code": record.error_code,
            "message": record.getMessage(),
        }
        if record.exc_info:
            value["traceback"] = self.formatException(record.exc_info)
        return json.dumps(value, ensure_ascii=False)


def build_logger(log_dir: Path, run_id: str, scenario: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"debug-agent-test.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    context = ContextFilter(run_id, scenario)

    formatter = JsonFormatter()
    file_handler = RotatingFileHandler(
        log_dir / "business.log",
        maxBytes=256_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(context)
    stdout_handler.addFilter(MaxLevelFilter(logging.WARNING))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(context)

    logger.handlers[:] = [file_handler, stdout_handler, stderr_handler]
    return logger


class Simulator:
    def __init__(
        self,
        scenario: str,
        duration: float,
        interval: float,
        log_dir: Path,
        runtime_dir: Path,
        run_id: Optional[str] = None,
    ) -> None:
        self.scenario = scenario
        self.duration = max(0.0, duration)
        self.interval = max(0.05, interval)
        self.run_id = run_id or uuid.uuid4().hex
        self.runtime_dir = runtime_dir
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logger = build_logger(log_dir, self.run_id, scenario)
        self.started = time.monotonic()
        self.stop_requested = False
        self._last_heartbeat: Optional[str] = None

    def request_stop(self, *_args: object) -> None:
        self.stop_requested = True

    def write_state(self, state: str, error_code: str = "NONE", detail: str = "") -> None:
        value = {
            "timestamp": utc_now(),
            "pid": os.getpid(),
            "run_id": self.run_id,
            "component": "business_app",
            "scenario": self.scenario,
            "state": state,
            "error_code": error_code,
            "detail": detail,
            "last_heartbeat": self._last_heartbeat,
        }
        target = self.runtime_dir / "status.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def log(self, level: int, message: str, *, component: str = "business_app", error_code: str = "NONE") -> None:
        self.logger.log(level, message, extra={"component": component, "error_code": error_code})

    def heartbeat(self, message: str = "service heartbeat healthy") -> None:
        self._last_heartbeat = utc_now()
        self.log(logging.INFO, message, component="health")
        self.write_state("running")

    def wait(self, seconds: Optional[float] = None) -> None:
        deadline = time.monotonic() + (self.interval if seconds is None else max(0.0, seconds))
        while not self.stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def run(self) -> int:
        self.log(logging.INFO, "business process starting", component="startup")
        self.write_state("starting")
        handler = self._handlers()[self.scenario]
        try:
            code = handler()
        except Exception as exc:
            self.logger.exception(
                "unhandled process exception",
                extra={"component": "runtime", "error_code": "E_PROCESS_CRASH"},
            )
            self.write_state("crashed", "E_PROCESS_CRASH", str(exc))
            traceback.print_exc(file=sys.stderr)
            return 70
        if self.stop_requested:
            self.log(logging.INFO, "shutdown requested", component="lifecycle")
        return code

    def _handlers(self):
        return {
            "normal": self._normal,
            "device_missing": self._device_missing,
            "software_conflict": self._software_conflict,
            "intermittent": self._intermittent,
            "dependency_error": self._dependency_error,
            "process_crash": self._process_crash,
            "silent_hang": self._silent_hang,
        }

    def _normal(self) -> int:
        while not self.stop_requested and self.elapsed() < self.duration:
            self.heartbeat()
            self.wait()
        self.write_state("stopped")
        self.log(logging.INFO, "business process stopped cleanly", component="lifecycle")
        return 0

    def _device_missing(self) -> int:
        self.log(
            logging.ERROR,
            "required camera device CAM-LEFT was not detected",
            component="device_manager",
            error_code="E_DEVICE_NOT_FOUND",
        )
        self.write_state("failed", "E_DEVICE_NOT_FOUND", "CAM-LEFT missing")
        return 20

    def _software_conflict(self) -> int:
        conflict_at = min(self.duration * 0.25, self.interval)
        while not self.stop_requested and self.elapsed() < self.duration:
            if self.elapsed() >= conflict_at:
                self.log(
                    logging.ERROR,
                    "keyboard command rejected: teleop and inference both claim control",
                    component="control_router",
                    error_code="E_CONTROL_OWNER_CONFLICT",
                )
                self.write_state("degraded", "E_CONTROL_OWNER_CONFLICT", "teleop,inference")
            else:
                self.heartbeat("control mode awaiting owner")
            self.wait()
        return 23

    def _intermittent(self) -> int:
        fault_start = self.duration / 3
        fault_end = self.duration * 2 / 3
        reported_fault = False
        reported_recovery = False
        while not self.stop_requested and self.elapsed() < self.duration:
            elapsed = self.elapsed()
            if fault_start <= elapsed < fault_end:
                if not reported_fault:
                    self.log(
                        logging.ERROR,
                        "device response timed out; retrying",
                        component="device_manager",
                        error_code="E_DEVICE_TIMEOUT",
                    )
                    reported_fault = True
                self.write_state("degraded", "E_DEVICE_TIMEOUT", "temporary timeout")
            else:
                if reported_fault and not reported_recovery:
                    self.log(logging.INFO, "device communication recovered", component="device_manager")
                    reported_recovery = True
                self.heartbeat()
            self.wait()
        self.write_state("stopped")
        return 0

    def _dependency_error(self) -> int:
        try:
            raise ModuleNotFoundError("No module named 'mock_vendor_sdk'")
        except ModuleNotFoundError as exc:
            self.logger.exception(
                "runtime dependency could not be loaded",
                extra={"component": "startup", "error_code": "E_DEPENDENCY_MISSING"},
            )
            self.write_state("failed", "E_DEPENDENCY_MISSING", str(exc))
            return 21

    def _process_crash(self) -> int:
        self.heartbeat("startup completed before crash")
        raise RuntimeError("simulated worker crash while processing frame 42")

    def _silent_hang(self) -> int:
        self.heartbeat("last heartbeat before simulated hang")
        self.write_state("hung", "E_HEARTBEAT_STALE", "process alive, heartbeat stopped")
        remaining = max(0.0, self.duration - self.elapsed())
        self.wait(remaining)
        return 24


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    simulator = Simulator(
        scenario=args.scenario,
        duration=args.duration,
        interval=args.interval,
        log_dir=args.log_dir,
        runtime_dir=args.runtime_dir,
        run_id=args.run_id,
    )
    signal.signal(signal.SIGINT, simulator.request_stop)
    signal.signal(signal.SIGTERM, simulator.request_stop)
    return simulator.run()


if __name__ == "__main__":
    raise SystemExit(main())

