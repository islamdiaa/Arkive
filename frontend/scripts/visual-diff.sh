#!/usr/bin/env bash
# Compare two frontend builds pixel by pixel (frontend/tests/visual).
#
# Usage:
#   frontend/scripts/visual-diff.sh [--docker] <baseline-build-dir> [candidate-build-dir]
#
# Screenshots every route in demo mode, in both themes at desktop and phone
# widths, plus a few opened forms and dialogs, from the baseline build, then
# from the candidate build (default: frontend/build), and fails on any pixel
# that differs (VISUAL_THRESHOLD=0.01 ignores anti-aliasing noise of a level
# or two). Native <select> option lists don't show in screenshots, so their
# computed style is compared too. Diffs land in frontend/.playwright/visual/report.
#
# A baseline build of another ref, e.g. before a CSS or dependency change:
#   git worktree add /tmp/arkive-base origin/main
#   (cd /tmp/arkive-base/frontend && npm ci && npm run build)
#   frontend/scripts/visual-diff.sh /tmp/arkive-base/frontend/build
#
# Host mode needs Playwright's Chromium (`npx playwright install chromium`).
# --docker runs in mcr.microsoft.com/playwright:v<version>-jammy without
# network access, using the host's frontend/node_modules; the container name
# is $ARKIVE_VISUAL_CONTAINER (default arkive-visual-diff).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="tests/visual/playwright.config.ts"

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

compare() {
  local baseline="$1" candidate="$2"
  cd "${FRONTEND_DIR}"
  rm -rf .playwright/visual
  echo "visual-diff: recording ${baseline}"
  VISUAL_BUILD_DIR="${baseline}" npx playwright test -c "${CONFIG}" --update-snapshots=all --reporter=dot
  echo "visual-diff: comparing ${candidate}"
  VISUAL_BUILD_DIR="${candidate}" npx playwright test -c "${CONFIG}"
}

docker_mode=0
if [ "${1:-}" = "--docker" ]; then
  docker_mode=1
  shift
fi
[ $# -ge 1 ] && [ $# -le 2 ] || usage

baseline="$(cd "$1" && pwd)"
candidate="$(cd "${2:-${FRONTEND_DIR}/build}" && pwd)"
for dir in "${baseline}" "${candidate}"; do
  [ -f "${dir}/index.html" ] || { echo "visual-diff: no index.html in ${dir}" >&2; exit 1; }
done

if [ "${docker_mode}" = 0 ]; then
  compare "${baseline}" "${candidate}"
  exit
fi

version="$(sed -n '/"node_modules\/@playwright\/test": {/,/}/s/.*"version": "\([^"]*\)".*/\1/p' \
  "${FRONTEND_DIR}/package-lock.json" | head -n 1)"
exec docker run --rm --init --ipc=host --network none \
  --name "${ARKIVE_VISUAL_CONTAINER:-arkive-visual-diff}" \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e VISUAL_THRESHOLD -e VISUAL_WORKERS \
  -v "${FRONTEND_DIR}:/work/frontend" \
  -v "${baseline}:/visual/baseline:ro" \
  -v "${candidate}:/visual/candidate:ro" \
  -w /work/frontend \
  "mcr.microsoft.com/playwright:v${version}-jammy" \
  bash scripts/visual-diff.sh /visual/baseline /visual/candidate
