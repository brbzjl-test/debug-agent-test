from __future__ import annotations

import argparse

from .preview import serve_preview


def main() -> None:
    parser = argparse.ArgumentParser(description="现场调试助手 UI")
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器预览 Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    if not args.browser:
        try:
            from .shell import run_desktop_shell

            run_desktop_shell(host=args.host, port=args.port)
            return
        except ImportError:
            print("未安装 PySide6/QtWebEngine，已切换到浏览器预览。")

    serve_preview(host=args.host, port=args.port, open_browser=True)


if __name__ == "__main__":
    main()
