#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${FIELD_SUPPORT_PYTHON:-${APP_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="${PYTHON:-python3}"
  export PYTHONPATH="${APP_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
fi
exec "${PYTHON_BIN}" -m field_support_agent.ui --browser "$@"
