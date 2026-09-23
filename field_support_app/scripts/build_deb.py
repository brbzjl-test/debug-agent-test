#!/usr/bin/env python3
"""Build the architecture-independent Ubuntu installer package."""

from __future__ import annotations

import argparse
import io
import tarfile
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        from pip._vendor import tomli as tomllib


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("src", "scripts", "deploy")
SOURCE_FILES = ("pyproject.toml", "README.md", "DEPLOY_UBUNTU.md", "config.example.yaml")
DEPENDENCIES = (
    "python3 (>= 3.9)", "python3-venv", "python3-pip", "git", "curl", "ca-certificates",
    "procps", "iproute2", "usbutils", "pciutils", "lsof", "ripgrep",
    "libgl1", "libegl1", "libnss3", "libxkbcommon-x11-0", "libxcb-cursor0",
    "libxcb-icccm4", "libxcb-image0", "libxcb-keysyms1", "libxcb-render-util0",
    "libxcb-xinerama0", "libxcb-xkb1", "libx11-xcb1", "libasound2t64 | libasound2",
    "fonts-noto-cjk",
)


def _tar_entry(archive: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    archive.addfile(info, io.BytesIO(content))


def _directory(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    archive.addfile(info)


def _tar_gzip(files: dict[str, tuple[bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        directories = set()
        for name in files:
            path = Path(name)
            directories.update(str(parent) for parent in path.parents if str(parent) != ".")
        for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
            _directory(archive, directory)
        for name, (content, mode) in sorted(files.items()):
            _tar_entry(archive, name, content, mode)
    return buffer.getvalue()


def _ar_member(handle, name: str, content: bytes) -> None:
    archive_name = name + "/"
    if len(archive_name) > 16:
        raise ValueError("Debian archive member name is too long: {}".format(name))
    header = (
        archive_name.ljust(16)
        + "0".ljust(12)
        + "0".ljust(6)
        + "0".ljust(6)
        + format(0o100644, "o").ljust(8)
        + str(len(content)).ljust(10)
        + "`\n"
    ).encode("ascii")
    handle.write(header)
    handle.write(content)
    if len(content) % 2:
        handle.write(b"\n")


def _data_files() -> dict[str, tuple[bytes, int]]:
    files = {}
    for directory in SOURCE_DIRS:
        for source in (SOURCE_ROOT / directory).rglob("*"):
            if not source.is_file() or source.is_symlink() or "__pycache__" in source.parts:
                continue
            if source.suffix == ".pyc" or source.name == ".DS_Store":
                continue
            target = "usr/share/field-support-agent/{}".format(source.relative_to(SOURCE_ROOT))
            files[target] = (source.read_bytes(), 0o644)
    for filename in SOURCE_FILES:
        files["usr/share/field-support-agent/" + filename] = ((SOURCE_ROOT / filename).read_bytes(), 0o644)
    launcher = (
        "#!/bin/sh\n"
        "exec /usr/bin/python3 /usr/share/field-support-agent/scripts/setup_package.py \"$@\"\n"
    )
    files["usr/bin/field-support-setup"] = (launcher.encode("utf-8"), 0o755)
    return files


def _control_files(version: str, installed_size: int) -> dict[str, tuple[bytes, int]]:
    control = (
        "Package: field-support-agent\n"
        "Version: {}\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Architecture: all\n"
        "Maintainer: Field Support Team <field-support@localhost>\n"
        "Installed-Size: {}\n"
        "Depends: {}\n"
        "Description: Local field issue collection and analysis assistant\n"
        " Installs a setup command for configuring one business repository,\n"
        " a local systemd service, and an optional desktop floating window.\n"
    ).format(version, installed_size, ", ".join(DEPENDENCIES))
    postinst = (
        "#!/bin/sh\nset -e\n"
        "if [ \"$1\" = configure ]; then\n"
        "  if [ -f /etc/field-support-agent/service-user ]; then\n"
        "    echo 'Run sudo field-support-setup --reuse to apply this package update.'\n"
        "  else\n"
        "    echo 'Run sudo field-support-setup --repo /absolute/path/to/business-repository.'\n"
        "  fi\n"
        "fi\n"
    )
    prerm = (
        "#!/bin/sh\nset -e\n"
        "if [ \"$1\" = remove ] && [ -f /etc/field-support-agent/service-user ]; then\n"
        "  service_user=$(cat /etc/field-support-agent/service-user)\n"
        "  /bin/bash /usr/share/field-support-agent/scripts/uninstall.sh --user \"$service_user\"\n"
        "fi\n"
    )
    postrm = (
        "#!/bin/sh\nset -e\n"
        "if [ \"$1\" = purge ]; then\n"
        "  rm -f /etc/field-support-agent/config.yaml /etc/field-support-agent/service-user\n"
        "  rmdir /etc/field-support-agent 2>/dev/null || true\n"
        "fi\n"
    )
    return {
        "control": (control.encode("utf-8"), 0o644),
        "postinst": (postinst.encode("utf-8"), 0o755),
        "prerm": (prerm.encode("utf-8"), 0o755),
        "postrm": (postrm.encode("utf-8"), 0o755),
    }


def build(output: Path) -> Path:
    version = tomllib.loads((SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    data_files = _data_files()
    installed_size = (sum(len(content) for content, _ in data_files.values()) + 1023) // 1024
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "field-support-agent_{}_all.deb".format(version)
    with destination.open("wb") as handle:
        handle.write(b"!<arch>\n")
        _ar_member(handle, "debian-binary", b"2.0\n")
        _ar_member(handle, "control.tar.gz", _tar_gzip(_control_files(version, installed_size)))
        _ar_member(handle, "data.tar.gz", _tar_gzip(data_files))
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="构建现场调试助手 Ubuntu .deb 安装包")
    parser.add_argument("--output", type=Path, default=SOURCE_ROOT.parent / "dist")
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
