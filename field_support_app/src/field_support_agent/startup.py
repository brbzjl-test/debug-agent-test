from __future__ import annotations

import os
import tempfile
from pathlib import Path

from field_support_agent.domain import ValidationError


def autostart_enabled(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == "on"
    except FileNotFoundError:
        return False


class StartupPreference:
    def __init__(self, path: Path) -> None:
        self.path = path

    def status(self) -> dict:
        return {"available": True, "enabled": autostart_enabled(self.path)}

    def set_enabled(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValidationError("自启动设置必须为布尔值")
        descriptor, temporary = tempfile.mkstemp(prefix="autostart-", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("on\n" if enabled else "off\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.status()
