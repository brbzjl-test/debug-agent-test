#!/usr/bin/env python3
"""Boot-only gate for the fixed Core systemd unit."""

import argparse
import os
import subprocess
from pathlib import Path

from field_support_agent.startup import autostart_enabled

ACTIVE_THIS_BOOT = Path("/run/field-support-autostart.enabled")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preference", type=Path, required=True)
    args = parser.parse_args()
    if autostart_enabled(args.preference):
        ACTIVE_THIS_BOOT.touch()
        os.chmod(ACTIVE_THIS_BOOT, 0o644)
        subprocess.run(["/usr/bin/systemctl", "start", "field-support-core.service"], check=True)
    else:
        ACTIVE_THIS_BOOT.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
