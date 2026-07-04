#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

cd "${ROOT_DIR}"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/python" -m pip install -r "${ROOT_DIR}/requirements.txt"
fi

echo "Starting funding-demo on http://${HOST}:${PORT}"
exec "${VENV_DIR}/bin/python" -m uvicorn web_app_core:app --host "${HOST}" --port "${PORT}" --proxy-headers
