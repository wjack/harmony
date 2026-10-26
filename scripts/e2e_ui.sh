#!/usr/bin/env bash
# The suggested story in a browser (web/e2e/story.spec.ts, Playwright) on ScriptedModel.
# Run by `make e2e-ui`.
#
# By default it builds and starts the containerized app (compose.ci.yaml) through
# compose_smoke.sh, runs only this check there, and tears the stack down. CI's
# compose-smoke job runs the same check among the others.
#
#   E2E_BASE_URL=http://127.0.0.1:5173   drive an app that is already running instead.
#                                        It must run with MODEL_MODE=scripted: the test
#                                        follows the cassettes, and a live model costs.
#
# Playwright's browser is installed once with
#   pnpm -C web exec playwright install --only-shell chromium
set -euo pipefail
cd "$(dirname "$0")/.."

# make puts pnpm on PATH itself; a direct run on macOS needs it too.
export PATH="$PATH:/opt/homebrew/bin"

if [[ -n ${E2E_BASE_URL:-} ]]; then
  exec pnpm -C web exec playwright test
fi
SMOKE_ONLY=ui exec bash scripts/compose_smoke.sh
