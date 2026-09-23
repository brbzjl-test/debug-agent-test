from __future__ import annotations

import argparse
import errno
import signal
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional, Sequence

from field_support_agent.analysis import ReadOnlyCodexRunner
from field_support_agent.api import LocalAPIServer
from field_support_agent.collectors import SnapshotCollector
from field_support_agent.config import load_config
from field_support_agent.integrations import DirectFeishuClient, LarkCliFeishuClient
from field_support_agent.management import ManagementAccess
from field_support_agent.service import CoreService, RuntimeService
from field_support_agent.settings import SettingsStore
from field_support_agent.storage import CoreDatabase
from field_support_agent.ui.preview import preview_server
from field_support_agent.ui.shell import run_desktop_shell
import logging

LOGGER = logging.getLogger("field-support")

def _runtime_components(settings: dict, state_dir: Path, enable_codex: bool):
    repositories = settings["business"]["repositories"]
    topology_value = settings["business"].get("ros_topology_file", "")
    topology = Path(topology_value) if topology_value else None
    log_paths = [Path(item) for item in settings["business"].get("log_paths", [])]
    collector = SnapshotCollector(state_dir, repositories, topology, log_paths=log_paths)
    runner = None
    if enable_codex:
        codex = settings["codex"]
        runner = ReadOnlyCodexRunner(
            [Path(item["local_path"]) for item in repositories],
            state_dir,
            codex_binary=codex["binary"],
            codex_home=Path(codex["home"]),
            model=codex["model"],
            reasoning_effort=codex["reasoning_effort"],
            timeout_seconds=float(codex["timeout_seconds"]),
        )
    feishu = settings["feishu"]
    gateway = None
    if (
        feishu.get("connection_mode") == "lark_cli_profile"
        and feishu.get("lark_cli_binary")
        and feishu.get("lark_profile")
    ):
        gateway = LarkCliFeishuClient(
            feishu["lark_cli_binary"],
            feishu["lark_profile"],
            feishu["support_chat_id"],
            state_dir / "feishu-state.json",
            base_app_token=feishu["base_app_token"],
            base_table_id=feishu["base_table_id"],
            site_name=feishu["site_name"],
            device_name=feishu.get("device_name", ""),
        )
    elif feishu["app_id"] and feishu["app_secret"]:
        gateway = DirectFeishuClient(
            feishu["app_id"],
            feishu["app_secret"],
            feishu["support_chat_id"],
            state_dir / "feishu-state.json",
            base_app_token=feishu["base_app_token"],
            base_table_id=feishu["base_table_id"],
            site_name=feishu["site_name"],
            device_name=feishu.get("device_name", ""),
        )
    return collector, runner, gateway


def build_runtime(settings: dict, state_dir: Path, enable_codex: bool) -> tuple[CoreDatabase, RuntimeService]:
    database = CoreDatabase(state_dir / "core.sqlite3")
    core = CoreService(database)
    collector, runner, gateway = _runtime_components(settings, state_dir, enable_codex)
    return database, RuntimeService(core, collector, runner, gateway)


def serve(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state_dir = args.state_dir.expanduser().resolve()
    settings = SettingsStore(state_dir / "settings.json", config)
    database, runtime = build_runtime(settings.runtime(), state_dir, not args.no_codex)

    def apply_settings(value: dict) -> None:
        collector, runner, gateway = _runtime_components(value, state_dir, not args.no_codex)
        runtime.configure(collector, runner, gateway)
        if gateway is not None and (gateway.site_name or gateway.device_name):
            try:
                chat = gateway.ensure_support_chat()
            except Exception as exc:
                LOGGER.warning("auto site support chat failed: %s", exc)
                return
            if chat and chat != value["feishu"].get("support_chat_id"):
                updated = settings.runtime()
                updated["feishu"]["support_chat_id"] = chat
                settings.update(updated)

    management_access = ManagementAccess(settings.management_password)
    api = LocalAPIServer(
        runtime,
        port=args.api_port,
        settings=settings,
        management_access=management_access,
        on_settings=apply_settings,
    )
    api.start()
    core_url = "http://{}:{}".format(*api.address)
    try:
        if args.desktop:
            run_desktop_shell(port=args.ui_port, core_url=core_url, session_token=api.session_token)
            return 0
        with preview_server(port=args.ui_port, core_url=core_url, session_token=api.session_token) as (_, url):
            print("现场调试助手：{}".format(url), flush=True)
            if args.browser:
                webbrowser.open(url)
            stopped = threading.Event()

            def stop(*_unused: object) -> None:
                stopped.set()

            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            stopped.wait()
        return 0
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(
            "启动失败：本机端口 {} 已被占用，可能已有一个现场调试助手在运行。\n"
            "请通过悬浮窗的关闭按钮退出旧实例，然后重新执行启动命令。".format(args.ui_port),
            file=sys.stderr,
        )
        return 2
    finally:
        api.close()
        runtime.close(wait=True)
        database.close()


def capture(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    repositories = [
        {"name": item.name, "git_url": item.git_url, "local_path": str(item.local_path)}
        for item in config.business_repositories
    ]
    topology = config.ros_topology.expected_topology_file if config.ros_topology else None
    report = SnapshotCollector(
        args.state_dir,
        repositories,
        topology,
        log_paths=config.log_paths,
    ).capture(args.issue_id, args.trigger)
    print(report.manifest_path)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Field support assistant")
    subparsers = result.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="run Core and the local Web UI")
    serve_parser.add_argument("--config", type=Path, required=True)
    serve_parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/field-support-agent")
    serve_parser.add_argument("--api-port", type=int, default=0)
    serve_parser.add_argument("--ui-port", type=int, default=8765)
    serve_parser.add_argument("--browser", action="store_true")
    serve_parser.add_argument("--desktop", action="store_true")
    serve_parser.add_argument("--no-codex", action="store_true")
    serve_parser.set_defaults(function=serve)

    capture_parser = subparsers.add_parser("snapshot", help="capture one read-only snapshot")
    capture_parser.add_argument("--config", type=Path, required=True)
    capture_parser.add_argument("--state-dir", type=Path, required=True)
    capture_parser.add_argument("--issue-id", required=True)
    capture_parser.add_argument("--trigger", default="manual")
    capture_parser.set_defaults(function=capture)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
