#!/usr/bin/env bash
# Run the Playwright end-to-end suite (frontend/tests/e2e) the same way in CI
# and on a developer machine.
#
# Usage:
#   frontend/scripts/e2e.sh [playwright test args...]
#   frontend/scripts/e2e.sh --docker [playwright test args...]
#
# Host mode (default; what CI runs):
#   Needs Node 22 with `npm ci` done in frontend/, Playwright's Chromium
#   (`npx playwright install --with-deps chromium`) and a Python 3.12
#   interpreter with backend/requirements.txt installed. playwright.config.ts
#   starts the FastAPI backend on :8200 (tests/e2e/start-backend.sh picks the
#   interpreter: $ARKIVE_E2E_PYTHON, backend/.venv, .venv, then python3) and
#   the SvelteKit dev server on :5173.
#
# Docker mode (--docker):
#   Runs everything inside mcr.microsoft.com/playwright:v<version>-jammy, where
#   <version> is the @playwright/test version locked in package-lock.json.
#   Python 3.12 is provisioned with uv. node_modules and the Python venv live in
#   named Docker volumes, so the host checkout only gains .playwright/ (test
#   results + HTML report) and .svelte-kit/, chowned back to the calling user.
#
# Examples:
#   frontend/scripts/e2e.sh --docker                       # whole suite
#   frontend/scripts/e2e.sh --docker --project=dashboard   # one project
#   frontend/scripts/e2e.sh --docker --repeat-each=2 --retries=0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${FRONTEND_DIR}/.." && pwd)"

UV_VERSION="0.13.0"
VOLUME_PREFIX="${ARKIVE_E2E_VOLUME_PREFIX:-arkive-e2e}"

playwright_version() {
  # Version of @playwright/test resolved in the lockfile.
  sed -n '/"node_modules\/@playwright\/test": {/,/}/s/.*"version": "\([^"]*\)".*/\1/p' \
    "${FRONTEND_DIR}/package-lock.json" | head -n 1
}

run_host() {
  cd "${FRONTEND_DIR}"
  exec npx playwright test "$@"
}

run_docker() {
  local version image
  version="$(playwright_version)"
  if [ -z "${version}" ]; then
    echo "e2e.sh: could not read the @playwright/test version from package-lock.json" >&2
    exit 1
  fi
  image="mcr.microsoft.com/playwright:v${version}-jammy"
  echo "e2e.sh: running in ${image}"

  local tty_flags=()
  if [ -t 0 ] && [ -t 1 ]; then
    tty_flags=(-it)
  fi

  exec docker run --rm --init --ipc=host ${tty_flags[@]+"${tty_flags[@]}"} \
    -v "${REPO_ROOT}:/work" \
    -v "${VOLUME_PREFIX}-node-modules:/work/frontend/node_modules" \
    -v "${VOLUME_PREFIX}-cache:/cache" \
    -e CI="${CI:-}" \
    -e HOST_UID="$(id -u)" \
    -e HOST_GID="$(id -g)" \
    -w /work/frontend \
    "${image}" \
    bash /work/frontend/scripts/e2e.sh --inside-container "$@"
}

run_inside_container() {
  cd "${FRONTEND_DIR}"
  export PATH="/cache/bin:${PATH}"
  export UV_CACHE_DIR=/cache/uv
  export UV_PYTHON_INSTALL_DIR=/cache/python

  # Python 3.12 + backend requirements (jammy ships Python 3.10).
  if ! command -v uv >/dev/null 2>&1 || [ "$(uv --version | awk '{print $2}')" != "${UV_VERSION}" ]; then
    echo "e2e.sh: installing uv ${UV_VERSION}"
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
      | env UV_INSTALL_DIR=/cache/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
  fi
  local req_hash venv
  req_hash="$(sha256sum < "${REPO_ROOT}/backend/requirements.txt" | cut -c1-16)"
  venv="/cache/venv-${req_hash}"
  if [ ! -x "${venv}/bin/python" ]; then
    echo "e2e.sh: creating Python 3.12 venv for backend requirements"
    uv venv --quiet --python 3.12 "${venv}"
    uv pip install --quiet --python "${venv}/bin/python" -r "${REPO_ROOT}/backend/requirements.txt"
  fi
  export ARKIVE_E2E_PYTHON="${venv}/bin/python"

  # Frontend dependencies (Linux/glibc build, kept out of the host checkout).
  local lock_hash
  lock_hash="$(sha256sum package-lock.json | cut -c1-16)"
  if [ "$(cat node_modules/.e2e-lock-hash 2>/dev/null || true)" != "${lock_hash}" ]; then
    echo "e2e.sh: npm ci"
    find node_modules -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
    npm ci --no-audit --no-fund
    echo "${lock_hash}" > node_modules/.e2e-lock-hash
  fi

  local status=0
  npx playwright test "$@" || status=$?

  if [ -n "${HOST_UID:-}" ]; then
    chown -R "${HOST_UID}:${HOST_GID:-${HOST_UID}}" \
      .playwright .svelte-kit 2>/dev/null || true
  fi
  exit "${status}"
}

case "${1:-}" in
  --docker)
    shift
    run_docker "$@"
    ;;
  --inside-container)
    shift
    run_inside_container "$@"
    ;;
  *)
    run_host "$@"
    ;;
esac
