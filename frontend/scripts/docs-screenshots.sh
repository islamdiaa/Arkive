#!/usr/bin/env bash
# Regenerate the screenshots in docs/images (frontend/tests/docs-screenshots).
#
# Usage:
#   frontend/scripts/docs-screenshots.sh [--docker] [--no-build]
#
# Builds the frontend (npm run build, unless --no-build), serves the static
# build and screenshots pages in demo mode (?demo=true: the sample data in
# src/lib/api/mock.ts, no backend) with a fixed clock, viewport, locale and
# time zone. The PNGs are written to docs/images, overwriting the old ones;
# review them (git diff, an image viewer) before committing. Pages that use
# them: README.md and docs/*.md.
#
# Needs `npm ci` done in frontend/. Host mode also needs Playwright's Chromium
# (`npx playwright install chromium`). --docker runs Playwright in
# mcr.microsoft.com/playwright:v<version>-jammy without network access, using
# the host's frontend/node_modules; the container name is
# $ARKIVE_DOCS_SCREENSHOTS_CONTAINER (default arkive-docs-screenshots).
# Pixels depend on the browser build and fonts, so use --docker for images
# you commit: reruns then give identical files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${FRONTEND_DIR}/.." && pwd)"
CONFIG="tests/docs-screenshots/playwright.config.ts"

usage() {
  sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

docker_mode=0
build=1
inside=0
while [ $# -gt 0 ]; do
  case "$1" in
    --docker) docker_mode=1 ;;
    --no-build) build=0 ;;
    --inside-container) inside=1 ;;
    *) usage ;;
  esac
  shift
done

cd "${FRONTEND_DIR}"

if [ "${inside}" = 1 ]; then
  exec npx playwright test -c "${CONFIG}"
fi

if [ "${build}" = 1 ]; then
  echo "docs-screenshots: building the frontend"
  npm run build
fi
[ -f build/index.html ] || { echo "docs-screenshots: no build/index.html; run without --no-build" >&2; exit 1; }

if [ "${docker_mode}" = 0 ]; then
  exec npx playwright test -c "${CONFIG}"
fi

version="$(sed -n '/"node_modules\/@playwright\/test": {/,/}/s/.*"version": "\([^"]*\)".*/\1/p' \
  package-lock.json | head -n 1)"
[ -n "${version}" ] || { echo "docs-screenshots: no @playwright/test version in package-lock.json" >&2; exit 1; }
exec docker run --rm --init --ipc=host --network none \
  --name "${ARKIVE_DOCS_SCREENSHOTS_CONTAINER:-arkive-docs-screenshots}" \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e DOCS_SCREENSHOTS_WORKERS \
  -v "${REPO_ROOT}:/work" \
  -w /work/frontend \
  "mcr.microsoft.com/playwright:v${version}-jammy" \
  bash scripts/docs-screenshots.sh --inside-container
