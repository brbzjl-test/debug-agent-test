#!/usr/bin/env python3
"""Run the local Core service without adding a runtime dependency."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
from pathlib import Path

from field_support_agent.api.server import LocalAPIServer
from field_support_agent.config import ConfigError, load_config
from field_support_agent.app import _runtime_components, build_runtime
from field_support_agent.settings import SettingsStore
from field_support_agent.management import ManagementAccess
from field_support_agent.startup import StartupPreference
import logging

LOGGER = logging.getLogger("field-support")

def _write_private(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="现场调试助手 Core 服务")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--startup-preference", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("配置无效：{}".format(exc), file=sys.stderr)
        return 2

    missing = [str(item.local_path) for item in config.business_repositories if not item.local_path.is_dir()]
    if missing:
        print("业务仓库路径不存在：{}".format(", ".join(missing)), file=sys.stderr)
        return 2

    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(args.state_dir), 0o700)
    os.chmod(str(args.runtime_dir), 0o700)

    token_path = args.runtime_dir / "api.token"
    metadata_path = args.runtime_dir / "api.json"
    token = secrets.token_urlsafe(48)
    _write_private(token_path, token + "\n")

    settings = SettingsStore(args.state_dir / "settings.json", config)
    database, runtime = build_runtime(settings.runtime(), args.state_dir, enable_codex=True)

    def apply_settings(value: dict) -> None:
        collector, runner, gateway = _runtime_components(value, args.state_dir, True)
        runtime.configure(collector, runner, gateway)
        if gateway is not None and getattr(gateway, "site_name", ""):
            try:
                chat = gateway.ensure_support_chat()
            except Exception as exc:
                LOGGER.warning("auto site support chat failed: %s", exc)
                return
            if chat and chat != value["feishu"]["support_chat_id"]:
                updated = settings.runtime()
                updated["feishu"]["support_chat_id"] = chat
                settings.update(updated)

    server = LocalAPIServer(
        runtime,
        port=args.port,
        session_token=token,
        settings=settings,
        management_access=ManagementAccess(settings.management_password),
        on_settings=apply_settings,
        startup_preference=StartupPreference(args.startup_preference) if args.startup_preference else None,
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        server.start()
        host, port = server.address
        _write_private(
            metadata_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "host": host,
                    "port": port,
                    "token_file": str(token_path),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        print("field-support-core ready on {}:{}".format(host, port), flush=True)
        stop.wait()
    finally:
        server.close()
        runtime.close(wait=True)
        database.close()
        for path in (metadata_path, token_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
