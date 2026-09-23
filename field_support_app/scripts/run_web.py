#!/usr/bin/env python3
"""Serve the real local UI in a browser using the installed Core."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from field_support_agent.ui.preview import serve_preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在本机浏览器打开现场调试助手")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/run/field-support"))
    parser.add_argument("--no-browser", action="store_true", help="只输出网址，不自动打开浏览器")
    args = parser.parse_args(argv)

    try:
        metadata = json.loads((args.runtime_dir / "api.json").read_text(encoding="utf-8"))
        token = Path(metadata["token_file"]).read_text(encoding="utf-8").strip()
        core_url = "http://{}:{}".format(metadata["host"], metadata["port"])
        if not token or metadata["host"] != "127.0.0.1":
            raise ValueError("Core 地址或会话凭证无效")
    except (OSError, KeyError, ValueError) as exc:
        print("无法连接本机 Core：{}。请先启动 field-support-core.service。".format(exc), file=sys.stderr)
        return 3

    try:
        serve_preview(
            host="127.0.0.1",
            port=args.port,
            open_browser=not args.no_browser,
            core_url=core_url,
            session_token=token,
        )
    except OSError as exc:
        print("无法启动网页服务：{}。可通过 --port 指定其他端口。".format(exc), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
