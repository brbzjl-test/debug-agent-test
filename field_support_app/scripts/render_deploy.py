#!/usr/bin/env python3
"""Validate site configuration and render static deployment templates."""

from __future__ import annotations

import argparse
import grp
import pwd
import shlex
import sys
from pathlib import Path

from field_support_agent.config import ConfigError, load_config


def _systemd_quote(value: str) -> str:
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def _render(template: Path, destination: Path, replacements: dict[str, str]) -> None:
    content = template.read_text(encoding="utf-8")
    for key, value in replacements.items():
        content = content.replace("@{}@".format(key), value)
    if "@" in content:
        unresolved = [word for word in content.split() if word.startswith("@")]
        if unresolved:
            raise ValueError("unresolved template placeholder: {}".format(unresolved[0]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成现场调试助手部署配置")
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.install_root.is_absolute() or not args.config.is_absolute():
        print("安装目录和配置路径必须是绝对路径", file=sys.stderr)
        return 2
    if args.install_root in {Path("/"), Path("/usr"), Path("/opt"), Path("/etc")}:
        print("安装目录不能是系统顶级目录", file=sys.stderr)
        return 2
    try:
        account = pwd.getpwnam(args.user)
    except KeyError:
        print("运行账号不存在：{}".format(args.user), file=sys.stderr)
        return 2
    if account.pw_uid == 0:
        print("Core 禁止使用 root 账号运行", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("配置无效：{}".format(exc), file=sys.stderr)
        return 2
    for repository in config.business_repositories:
        if not repository.local_path.is_dir():
            print("业务仓库路径不存在：{}".format(repository.local_path), file=sys.stderr)
            return 2

    replacements = {
        "INSTALL_ROOT": str(args.install_root),
        "CONFIG_PATH": str(args.config),
        "SERVICE_USER": account.pw_name,
        "SERVICE_GROUP": grp.getgrgid(account.pw_gid).gr_name,
        "CODEX_HOME_PATH": _systemd_quote(str(Path(account.pw_dir) / ".codex")),
    }
    deploy_root = args.app_root / "deploy"
    _render(
        deploy_root / "systemd/field-support-core.service.in",
        args.output / "field-support-core.service",
        replacements,
    )
    _render(
        deploy_root / "systemd/field-support-autostart.service.in",
        args.output / "field-support-autostart.service",
        replacements,
    )
    readonly_lines = "\n".join(
        "BindReadOnlyPaths={}".format(_systemd_quote(str(repository.local_path)))
        for repository in config.business_repositories
    )
    _render(
        deploy_root / "systemd/repositories.conf.in",
        args.output / "repositories.conf",
        {"READ_ONLY_REPOSITORIES": readonly_lines},
    )
    _render(
        deploy_root / "xdg/field-support-float.desktop.in",
        args.output / "field-support-float.desktop",
        {"INSTALL_ROOT": str(args.install_root)},
    )
    (args.output / "install.env").write_text(
        "SERVICE_USER={}\nSERVICE_GROUP={}\nSERVICE_HOME={}\nINSTALL_ROOT={}\nCONFIG_PATH={}\n".format(
            shlex.quote(account.pw_name),
            shlex.quote(grp.getgrgid(account.pw_gid).gr_name),
            shlex.quote(account.pw_dir),
            shlex.quote(str(args.install_root)),
            shlex.quote(str(args.config)),
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
