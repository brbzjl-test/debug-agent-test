#!/usr/bin/env python3
"""Configure an installed Debian package for one local desktop account."""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path("/usr/share/field-support-agent")
CONFIG_DIR = Path("/etc/field-support-agent")
CONFIG_PATH = CONFIG_DIR / "config.yaml"
SERVICE_USER_PATH = CONFIG_DIR / "service-user"


def _git_origin(repository: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _config_text(name: str, git_url: str, repository: Path) -> str:
    return (
        "business_repositories:\n"
        "  - name: {!r}\n"
        "    git_url: {!r}\n"
        "    local_path: {!r}\n"
    ).format(name, git_url, str(repository))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置现场调试助手的业务仓库和运行账号")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", type=Path, help="已有业务 Git 仓库的绝对路径")
    source.add_argument("--reuse", action="store_true", help="使用上次的配置更新安装")
    parser.add_argument("--user", help="桌面登录账号；使用 sudo 时默认取 SUDO_USER")
    parser.add_argument("--git-url", help="仓库没有 origin 时指定 Git 地址")
    parser.add_argument("--name", help="业务仓库显示名称")
    parser.add_argument("--autostart", choices=("keep", "on", "off"), default="keep")
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("请使用 sudo 运行 field-support-setup。", file=sys.stderr)
        return 2

    if args.reuse:
        if args.git_url or args.name:
            parser.error("--reuse 不能与 --git-url 或 --name 同时使用")
        try:
            service_user = SERVICE_USER_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            print("找不到上次的运行账号，请使用 --repo 重新配置。", file=sys.stderr)
            return 2
        if not CONFIG_PATH.is_file():
            print("找不到业务仓库配置，请使用 --repo 重新配置。", file=sys.stderr)
            return 2
    else:
        service_user = args.user or os.environ.get("SUDO_USER", "")
        if not service_user or service_user == "root":
            print("请通过 sudo 从桌面账号运行，或指定 --user。", file=sys.stderr)
            return 2
        try:
            if pwd.getpwnam(service_user).pw_uid == 0:
                raise KeyError(service_user)
        except KeyError:
            print("运行账号不存在或不能使用 root：{}".format(service_user), file=sys.stderr)
            return 2
        repository = args.repo.expanduser().resolve()
        if not repository.is_dir():
            print("业务仓库目录不存在：{}".format(repository), file=sys.stderr)
            return 2
        git_url = args.git_url or _git_origin(repository)
        if not git_url:
            print("仓库没有 origin，请补充 --git-url。", file=sys.stderr)
            return 2
        name = args.name or repository.name
        if not name or any("\n" in value or "\r" in value for value in (name, git_url, str(repository))):
            print("仓库名称或 Git 地址无效。", file=sys.stderr)
            return 2
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(_config_text(name, git_url, repository), encoding="utf-8")
        os.chmod(CONFIG_PATH, 0o644)
        SERVICE_USER_PATH.write_text(service_user + "\n", encoding="utf-8")
        os.chmod(SERVICE_USER_PATH, 0o600)

    if args.reuse:
        try:
            if pwd.getpwnam(service_user).pw_uid == 0:
                raise KeyError(service_user)
        except KeyError:
            print("运行账号不存在或不能使用 root：{}".format(service_user), file=sys.stderr)
            return 2

    command = [
        "bash", str(PACKAGE_ROOT / "scripts/install.sh"),
        "--user", service_user,
        "--config", str(CONFIG_PATH),
        "--autostart", args.autostart,
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    print("业务仓库已配置。请在运行账号下登录 Codex，并在应用设置页配置模型与飞书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
