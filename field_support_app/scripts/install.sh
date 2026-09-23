#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "用法: sudo $0 --user <非root现场账号> --config <绝对路径> [--prefix /opt/field-support-agent] [--autostart keep|on|off]" >&2
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "安装 systemd system service 需要 root 权限，请使用 sudo。" >&2
  exit 2
fi

SERVICE_USER=""
CONFIG_PATH=""
INSTALL_ROOT="/opt/field-support-agent"
AUTOSTART="keep"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --config) CONFIG_PATH="${2:-}"; shift 2 ;;
    --prefix) INSTALL_ROOT="${2:-}"; shift 2 ;;
    --autostart) AUTOSTART="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
if [[ -z "${SERVICE_USER}" || -z "${CONFIG_PATH}" || "${CONFIG_PATH}" != /* || "${INSTALL_ROOT}" != /* ]]; then
  usage
  exit 2
fi
if [[ "${AUTOSTART}" != "keep" && "${AUTOSTART}" != "on" && "${AUTOSTART}" != "off" ]]; then
  usage
  exit 2
fi
case "${INSTALL_ROOT}" in
  /|/usr|/opt|/etc) echo "--prefix 不能是系统顶级目录。" >&2; exit 2 ;;
esac

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT
export PYTHONPATH="${SOURCE_ROOT}/src"
python3 "${SOURCE_ROOT}/scripts/render_deploy.py" \
  --app-root "${SOURCE_ROOT}" --install-root "${INSTALL_ROOT}" --config "${CONFIG_PATH}" \
  --user "${SERVICE_USER}" --output "${BUILD_DIR}"
# shellcheck disable=SC1090
source "${BUILD_DIR}/install.env"

install -d -m 0755 "${INSTALL_ROOT}"
rm -rf "${INSTALL_ROOT}/src" "${INSTALL_ROOT}/scripts" "${INSTALL_ROOT}/deploy"
cp -R "${SOURCE_ROOT}/src" "${SOURCE_ROOT}/scripts" "${SOURCE_ROOT}/deploy" "${INSTALL_ROOT}/"
install -m 0644 "${SOURCE_ROOT}/pyproject.toml" "${SOURCE_ROOT}/README.md" "${INSTALL_ROOT}/"
python3 -m venv "${INSTALL_ROOT}/.venv"
"${INSTALL_ROOT}/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"${INSTALL_ROOT}/.venv/bin/python" -m pip install "${INSTALL_ROOT}[desktop]"
chown -R root:root "${INSTALL_ROOT}"
find "${INSTALL_ROOT}/scripts" -type f -name '*.sh' -exec chmod 0755 {} +
find "${INSTALL_ROOT}/scripts" -type f -name '*.py' -exec chmod 0755 {} +

install -m 0644 "${BUILD_DIR}/field-support-core.service" /etc/systemd/system/field-support-core.service
install -m 0644 "${BUILD_DIR}/field-support-autostart.service" /etc/systemd/system/field-support-autostart.service
install -d -m 0755 /etc/systemd/system/field-support-core.service.d
install -m 0644 "${BUILD_DIR}/repositories.conf" /etc/systemd/system/field-support-core.service.d/repositories.conf

# Codex persists authentication and sessions here, including under ProtectHome.
if [[ ! -d "${SERVICE_HOME}/.codex" ]]; then
  install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 "${SERVICE_HOME}/.codex"
fi

AUTOSTART_DIR="${SERVICE_HOME}/.config/autostart"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 "${AUTOSTART_DIR}"
install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0644 \
  "${BUILD_DIR}/field-support-float.desktop" "${AUTOSTART_DIR}/field-support-float.desktop"

STATE_DIR="/var/lib/field-support"
PREFERENCE_PATH="${STATE_DIR}/autostart.mode"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 "${STATE_DIR}"
if [[ "${AUTOSTART}" != "keep" || ! -f "${PREFERENCE_PATH}" ]]; then
  if [[ "${AUTOSTART}" == "keep" ]]; then
    AUTOSTART="on"
  fi
  printf '%s\n' "${AUTOSTART}" > "${PREFERENCE_PATH}"
  chown "${SERVICE_USER}:${SERVICE_GROUP}" "${PREFERENCE_PATH}"
  chmod 0600 "${PREFERENCE_PATH}"
fi

systemctl daemon-reload
systemctl disable field-support-core.service
systemctl stop field-support-core.service
systemctl enable field-support-autostart.service
systemctl restart field-support-autostart.service

runuser -u "${SERVICE_USER}" -- "${INSTALL_ROOT}/.venv/bin/python" -c \
  'import PySide6.QtWebEngineWidgets' >/dev/null
if [[ "$(<"${PREFERENCE_PATH}")" == "on" ]]; then
  echo "安装完成。Core 已启动；浮窗将在 ${SERVICE_USER} 下次图形登录时启动。"
else
  echo "安装完成。Core 和浮窗下次不会自启动；请手动启动 Core 和所需界面。"
fi
