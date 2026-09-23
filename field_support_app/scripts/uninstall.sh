#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "卸载 systemd system service 需要 root 权限，请使用 sudo。" >&2
  exit 2
fi

SERVICE_USER=""
INSTALL_ROOT="/opt/field-support-agent"
PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --prefix) INSTALL_ROOT="${2:-}"; shift 2 ;;
    --purge-data) PURGE=1; shift ;;
    *) echo "用法: sudo $0 --user <现场账号> [--prefix PATH] [--purge-data]" >&2; exit 2 ;;
  esac
done
if [[ -z "${SERVICE_USER}" || "${INSTALL_ROOT}" != /* || "${SERVICE_USER}" == "root" ]]; then
  echo "必须提供非 root 的 --user 和绝对 --prefix。" >&2
  exit 2
fi
case "${INSTALL_ROOT}" in
  /|/usr|/opt|/etc) echo "--prefix 不能是系统顶级目录。" >&2; exit 2 ;;
esac
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
if [[ -z "${SERVICE_HOME}" ]]; then
  echo "运行账号不存在：${SERVICE_USER}" >&2
  exit 2
fi

systemctl disable --now field-support-core.service 2>/dev/null || true
systemctl disable --now field-support-autostart.service 2>/dev/null || true
rm -f /etc/systemd/system/field-support-core.service
rm -f /etc/systemd/system/field-support-autostart.service
rm -rf /etc/systemd/system/field-support-core.service.d
rm -f "${SERVICE_HOME}/.config/autostart/field-support-float.desktop"
rm -rf "${INSTALL_ROOT}"
if [[ "${PURGE}" -eq 1 ]]; then
  rm -rf /var/lib/field-support
else
  echo "保留问题数据：/var/lib/field-support（使用 --purge-data 才会删除）。"
fi
systemctl daemon-reload
echo "卸载完成。"
