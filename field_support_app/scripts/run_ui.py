#!/usr/bin/env python3
"""Start the desktop shell and fail clearly when its optional UI runtime is absent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="启动现场调试助手浮窗")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/run/field-support"))
    args = parser.parse_args()

    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        print(
            "无法启动桌面浮窗：未安装 PySide6 和 QtWebEngine。\n"
            "系统不会自动安装依赖。可运行 scripts/run_web.py 使用本机浏览器。",
            file=sys.stderr,
        )
        return 2

    os.environ.setdefault("FIELD_SUPPORT_UI_DEMO", "0")
    from field_support_agent.ui.shell import run_desktop_shell

    try:
        metadata = json.loads((args.runtime_dir / "api.json").read_text(encoding="utf-8"))
        token = Path(metadata["token_file"]).read_text(encoding="utf-8").strip()
        core_url = "http://{}:{}".format(metadata["host"], metadata["port"])
    except (OSError, KeyError, ValueError) as exc:
        print("无法连接本机 Core：{}".format(exc), file=sys.stderr)
        return 3
    run_desktop_shell(
        host=args.host,
        port=args.port,
        core_url=core_url,
        session_token=token,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
