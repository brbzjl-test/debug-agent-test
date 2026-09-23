#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${FIELD_SUPPORT_CONFIG:-${APP_ROOT}/config.example.yaml}"
STATE_DIR="${FIELD_SUPPORT_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/field-support-dev}"
RUNTIME_DIR="${FIELD_SUPPORT_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/field-support-dev-${UID}}"
export PYTHONPATH="${APP_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON:-python3}" "${APP_ROOT}/scripts/run_core.py" \
  --config "${CONFIG_PATH}" --state-dir "${STATE_DIR}" --runtime-dir "${RUNTIME_DIR}" "$@"
