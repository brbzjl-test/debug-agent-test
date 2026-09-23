from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping

from field_support_agent.config import AppConfig
from field_support_agent.integrations import verify_lark_profile


class SettingsError(ValueError):
    pass


class SettingsStore:
    """Private local settings managed by the loopback UI."""

    VERSION = 1
    REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}

    def __init__(self, path: Path, initial_config: AppConfig) -> None:
        self.path = path.expanduser().resolve()
        self._settings = self._defaults(initial_config)
        if self.path.exists():
            self._settings = self._validate(self._read(), existing=self._settings)

    def runtime(self) -> Dict[str, Any]:
        return deepcopy(self._settings)

    def public(self) -> Dict[str, Any]:
        value = self.runtime()
        secret = value["feishu"].pop("app_secret", "")
        value["feishu"]["app_secret_configured"] = bool(secret)
        password = value["management"].pop("password", "")
        value["management"]["password_configured"] = bool(password)
        return value

    def management_password(self) -> str:
        return str(self._settings["management"]["password"])

    def update(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        settings = self._validate(value, existing=self._settings)
        self._write(settings)
        self._settings = settings
        return self.public()

    def verify_feishu(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        feishu = dict(self._settings["feishu"])
        feishu.update({key: item for key, item in value.items() if item})
        mode = str(feishu.get("connection_mode", "credentials"))
        if mode == "lark_cli_profile":
            binary = str(feishu.get("lark_cli_binary", "")).strip()
            profile = str(feishu.get("lark_profile", "")).strip()
            if not binary or not profile:
                raise SettingsError("请填写 lark-cli 程序位置和 profile")
            try:
                return verify_lark_profile(binary, profile)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                raise SettingsError(str(exc)) from exc
        app_id = str(feishu.get("app_id", "")).strip()
        app_secret = str(feishu.get("app_secret", "")).strip()
        if not app_id or not app_secret:
            raise SettingsError("请填写飞书 App ID 和 App Secret")
        request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise SettingsError("无法连接飞书开放平台") from exc
        if int(result.get("code", -1)) != 0 or not result.get("tenant_access_token"):
            raise SettingsError(str(result.get("msg") or "飞书凭证验证失败"))
        return {"verified": True, "app_id": app_id, "expire_seconds": int(result.get("expire", 0))}

    def _defaults(self, config: AppConfig) -> Dict[str, Any]:
        codex_binary = shutil.which("codex") or "codex"
        return {
            "version": self.VERSION,
            "business": {
                "repositories": [
                    {"name": item.name, "git_url": item.git_url, "local_path": str(item.local_path)}
                    for item in config.business_repositories
                ],
                "ros_topology_file": str(config.ros_topology.expected_topology_file) if config.ros_topology else "",
                "log_paths": [str(item) for item in config.log_paths],
            },
            "codex": {
                "binary": codex_binary,
                "home": str(Path.home() / ".codex"),
                "model": "",
                "reasoning_effort": "",
                "timeout_seconds": 300,
            },
            "feishu": {
                "device_name": "",
                "site_name": "",
                "connection_mode": "credentials",
                "app_id": "",
                "app_secret": "",
                "lark_cli_binary": shutil.which("lark-cli") or "",
                "lark_profile": "",
                "support_chat_id": "",
                "base_app_token": "",
                "base_table_id": "",
            },
            "management": {"password": ""},
        }

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError("设置文件无法读取") from exc
        if not isinstance(value, dict):
            raise SettingsError("设置文件必须是 JSON 对象")
        return value

    def _validate(self, value: Mapping[str, Any], existing: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise SettingsError("设置必须是对象")
        allowed = {"version", "business", "codex", "feishu", "management"}
        if set(value) - allowed:
            raise SettingsError("设置包含未知字段")

        business = value.get("business")
        codex = value.get("codex")
        feishu_input = value.get("feishu")
        management_input = value.get("management", existing.get("management", {"password": ""}))
        if not isinstance(business, Mapping) or not isinstance(codex, Mapping) or not isinstance(feishu_input, Mapping):
            raise SettingsError("业务代码、Codex 和飞书设置不能为空")
        if not isinstance(management_input, Mapping):
            raise SettingsError("工程模式设置无效")

        repositories = business.get("repositories")
        if not isinstance(repositories, list) or not repositories:
            raise SettingsError("至少需要配置一个业务仓库")
        normalized_repositories = []
        seen_paths = set()
        for item in repositories:
            if not isinstance(item, Mapping) or set(item) != {"name", "git_url", "local_path"}:
                raise SettingsError("业务仓库字段不完整")
            name = str(item["name"]).strip()
            git_url = str(item["git_url"]).strip()
            local_path = Path(str(item["local_path"])).expanduser()
            if not name or not git_url or not local_path.is_absolute() or not local_path.is_dir():
                raise SettingsError("业务仓库名称、Git 地址或本地路径无效")
            resolved = str(local_path.resolve())
            if resolved in seen_paths:
                raise SettingsError("业务仓库本地路径不能重复")
            seen_paths.add(resolved)
            normalized_repositories.append({"name": name, "git_url": git_url, "local_path": resolved})

        topology = str(business.get("ros_topology_file", "")).strip()
        if topology and not Path(topology).expanduser().is_absolute():
            raise SettingsError("ROS 拓扑文件必须使用绝对路径")
        log_paths = []
        raw_log_paths = business.get("log_paths", "")
        if isinstance(raw_log_paths, str):
            raw_log_paths = [line for line in raw_log_paths.splitlines()]
        if not isinstance(raw_log_paths, list):
            raise SettingsError("业务日志路径无效")
        for raw_path in raw_log_paths:
            candidate = str(raw_path).strip()
            if not candidate:
                continue
            expanded = Path(candidate).expanduser()
            if not expanded.is_absolute():
                raise SettingsError("业务日志路径必须使用绝对路径")
            log_paths.append(str(expanded.resolve()))

        binary = str(codex.get("binary", "")).strip()
        home = Path(str(codex.get("home", ""))).expanduser()
        model = str(codex.get("model", "")).strip()
        effort = str(codex.get("reasoning_effort", "")).strip()
        try:
            timeout = int(codex.get("timeout_seconds", 300))
        except (TypeError, ValueError) as exc:
            raise SettingsError("Codex 超时时间必须是整数") from exc
        if not binary or not home.is_absolute():
            raise SettingsError("Codex 位置和配置目录不能为空")
        if effort and effort not in self.REASONING_EFFORTS:
            raise SettingsError("Codex 推理强度无效")
        if timeout < 30 or timeout > 1800:
            raise SettingsError("Codex 超时时间必须在 30 到 1800 秒之间")

        previous_secret = str(existing.get("feishu", {}).get("app_secret", ""))
        app_secret = str(feishu_input.get("app_secret", "")).strip() or previous_secret
        connection_mode = str(feishu_input.get("connection_mode", "credentials")).strip()
        app_id = str(feishu_input.get("app_id", "")).strip()
        lark_cli_binary = str(feishu_input.get("lark_cli_binary", "")).strip()
        lark_profile = str(feishu_input.get("lark_profile", "")).strip()
        support_chat_id = str(feishu_input.get("support_chat_id", "")).strip()
        site_name = str(feishu_input.get("site_name", "")).strip()
        if len(site_name) > 64:
            raise SettingsError("现场名称不能超过 64 个字符")
        device_name = str(feishu_input.get("device_name", "")).strip()
        if len(device_name) > 64:
            raise SettingsError("设备名称不能超过 64 个字符")
        base_app_token = str(feishu_input.get("base_app_token", "")).strip()
        base_table_id = str(feishu_input.get("base_table_id", "")).strip()
        if connection_mode not in {"credentials", "lark_cli_profile"}:
            raise SettingsError("飞书连接方式无效")
        if connection_mode == "credentials":
            if app_id and not app_secret:
                raise SettingsError("配置飞书 App ID 时必须填写 App Secret")
            if app_secret and not app_id:
                raise SettingsError("配置飞书 App Secret 时必须填写 App ID")
        elif not lark_cli_binary or not lark_profile:
            raise SettingsError("lark-cli profile 模式需要程序位置和 profile")
        if bool(base_app_token) != bool(base_table_id):
            raise SettingsError("多维表格 App Token 和数据表 ID 必须同时填写")

        previous_password = str(existing.get("management", {}).get("password", ""))
        management_password = str(management_input.get("password", "")).strip() or previous_password
        if management_password and not 4 <= len(management_password) <= 128:
            raise SettingsError("工程模式密码需要 4 到 128 个字符")

        return {
            "version": self.VERSION,
            "business": {
                "repositories": normalized_repositories,
                "ros_topology_file": topology,
                "log_paths": log_paths,
            },
            "codex": {
                "binary": binary,
                "home": str(home.resolve()),
                "model": model,
                "reasoning_effort": effort,
                "timeout_seconds": timeout,
            },
            "feishu": {
                "device_name": device_name,
                "site_name": site_name,
                "connection_mode": connection_mode,
                "app_id": app_id,
                "app_secret": app_secret,
                "lark_cli_binary": lark_cli_binary,
                "lark_profile": lark_profile,
                "support_chat_id": support_chat_id,
                "base_app_token": base_app_token,
                "base_table_id": base_table_id,
            },
            "management": {"password": management_password},
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.path.parent), 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.path))
            os.chmod(str(self.path), 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
