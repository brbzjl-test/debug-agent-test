#!/usr/bin/env python3
"""Open the floating UI at login only when autostart is enabled."""

from pathlib import Path

from run_ui import main


if __name__ == "__main__" and Path("/run/field-support-autostart.enabled").is_file():
    raise SystemExit(main())
