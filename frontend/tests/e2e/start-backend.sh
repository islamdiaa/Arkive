#!/bin/bash
# Kill stale uvicorn on port 8200, wait for port to be free, start fresh
set -euo pipefail

pkill -9 -f '[u]vicorn.*8200' 2>/dev/null || true
for i in $(seq 1 40); do
  grep -q ':2008 00000000:0000 0A' /proc/net/tcp 2>/dev/null || break
  sleep 0.25
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPO_ROOT="$(cd "${FRONTEND_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
PROFILES_DIR="${REPO_ROOT}/profiles"
CONFIG_DIR="${ARKIVE_CONFIG_DIR:-/tmp/arkive-playwright-$$}"

# If the directory exists but has permission issues (e.g. root-owned from Docker),
# fall back to a fresh temp directory.
if [ -d "${CONFIG_DIR}" ] && ! touch "${CONFIG_DIR}/.writetest" 2>/dev/null; then
  CONFIG_DIR="/tmp/arkive-playwright-$$"
fi
rm -f "${CONFIG_DIR}/.writetest" 2>/dev/null || true

mkdir -p "${CONFIG_DIR}/logs"
# Ensure deterministic E2E state between runs.
find "${CONFIG_DIR}" -maxdepth 1 -type f -name 'arkive.db*' -delete 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"

PYTHON_BIN=""
for candidate in \
  "${ARKIVE_E2E_PYTHON:-}" \
  "${BACKEND_DIR}/.venv/bin/python" \
  "${REPO_ROOT}/.venv/bin/python" \
  "$(command -v python3)"; do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
    PYTHON_BIN="${candidate}"
    break
  fi
done

cd "${BACKEND_DIR}"
exec env ARKIVE_DEV_MODE="${ARKIVE_DEV_MODE:-1}" ARKIVE_CONFIG_DIR="${CONFIG_DIR}" ARKIVE_PROFILES_DIR="${PROFILES_DIR}" PYTHONPATH=. \
  "${PYTHON_BIN}" -m uvicorn app.main:app --host 127.0.0.1 --port 8200
