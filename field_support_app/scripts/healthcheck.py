#!/usr/bin/env python3
"""One-shot startup/manual health check; this is not a periodic monitor."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="检查现场调试助手 Core")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--wait", type=float, default=0.0)
    args = parser.parse_args()
    deadline = time.monotonic() + max(args.wait, 0.0)

    while True:
        try:
            metadata = json.loads((args.runtime_dir / "api.json").read_text(encoding="utf-8"))
            url = "http://{}:{}/health".format(metadata["host"], metadata["port"])
            with urllib.request.urlopen(url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and payload.get("status") == "ok":
                print("field-support-core healthy")
                return 0
        except (OSError, KeyError, ValueError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            print("field-support-core health check failed", file=sys.stderr)
            return 1
        time.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(main())
