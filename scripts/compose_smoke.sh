#!/usr/bin/env bash
# Build the production image and smoke-test the containerized app (compose.ci.yaml):
# api + worker on throwaway Postgres and Temporal, MODEL_MODE=scripted, behind the master
# passcode gate (ACCESS_CODE). Prints PASS or FAIL per check, then always tears the stack
# down. Run by `make compose-smoke` and by CI's compose-smoke job.
#
# The gate is checked first, without a cookie: everything but the health check and the
# gate's own routes is refused. Every later check unlocks the way a browser does.
#
# The checks run on the host against the stack's published ports, which they find in
# HARMONY_BASE_URL, DATABASE_URL / DATABASE_DIRECT_URL and TEMPORAL_ADDRESS.
#
#   SMOKE_KEEP=1            leave the stack running afterwards (for debugging)
#   SMOKE_ALLOW_MISSING=1   report a check whose script doesn't exist yet as SKIP, not FAIL
#   SMOKE_ONLY=ui           run only the Playwright story (what `make e2e-ui` runs)
#
# The Playwright story needs web/node_modules and Playwright's Chromium on the host
# (scripts/e2e_ui.sh says how to install it).
set -euo pipefail
cd "$(dirname "$0")/.."

# make puts these on PATH itself; a direct run on macOS needs them too.
export PATH="$PATH:/opt/homebrew/bin:$HOME/.orbstack/bin"

export HARMONY_CI_API_PORT="${HARMONY_CI_API_PORT:-18000}"
export HARMONY_CI_PG_PORT="${HARMONY_CI_PG_PORT:-15432}"
export HARMONY_CI_TEMPORAL_PORT="${HARMONY_CI_TEMPORAL_PORT:-17233}"
BUILD_SHA="${BUILD_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || echo ci)}}"
export BUILD_SHA

compose=(docker compose -f compose.ci.yaml)

# Settings for the host-side checks. Exported values beat .env.local, which harmony.config
# still reads on a dev machine: point everything at the CI stack, keep the model scripted,
# send any accidental model call to the discard port, and keep the real ledger out of it.
db_url="postgresql://harmony:harmony@localhost:${HARMONY_CI_PG_PORT}/harmony"
# APP_ENV=ci sets the session cookie Secure; curl still sends it to http://localhost.
export HARMONY_BASE_URL="http://localhost:${HARMONY_CI_API_PORT}"
export DATABASE_URL="$db_url" DATABASE_DIRECT_URL="$db_url"
export TEMPORAL_ADDRESS="localhost:${HARMONY_CI_TEMPORAL_PORT}" TEMPORAL_NAMESPACE=default
export APP_ENV=ci MODEL_MODE=scripted OBJECT_STORE=localfs
export ANTHROPIC_BASE_URL="http://127.0.0.1:9"
export HARMONY_TEST_DB_PREFIX="${HARMONY_TEST_DB_PREFIX:-smoke}"
# compose.ci.yaml's ACCESS_CODE (test_repo_config keeps the two equal). Playwright's
# unlock() reads E2E_PASSCODE; p0_actas_check.sh and sse_resume_check.py read
# HARMONY_PASSCODE.
export E2E_PASSCODE=ci-only-passcode-0001
export HARMONY_PASSCODE="$E2E_PASSCODE"
smoke_tmp="$(mktemp -d)"
export SPEND_LEDGER_PATH="$smoke_tmp/spend_ledger.jsonl"
# The smoke checks' own visitor, unlocked once the stack is up.
jar="$smoke_tmp/visitor.jar"

results=()
failed=0

record() { # name status [detail]
  results+=("$(printf '%-4s  %s%s' "$2" "$1" "${3:+  ($3)}")")
  [[ $2 == FAIL ]] && failed=1
  return 0
}

run_check() { # name required-file command...
  local name=$1 file=$2
  shift 2
  if [[ -n $file && ! -e $file ]]; then
    if [[ ${SMOKE_ALLOW_MISSING:-0} == 1 ]]; then
      record "$name" SKIP "$file does not exist yet"
    else
      record "$name" FAIL "$file does not exist"
    fi
    return 0
  fi
  echo "--- $name"
  if "$@"; then record "$name" PASS; else record "$name" FAIL; fi
}

teardown() {
  local status=$?
  if ((failed || status)); then
    echo "--- stack logs (last 200 lines per service)"
    "${compose[@]}" logs --no-color --tail 200 || true
  fi
  if [[ ${SMOKE_KEEP:-0} == 1 ]]; then
    echo "SMOKE_KEEP=1: the stack is still up; stop it with: ${compose[*]} down -v"
  else
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$smoke_tmp"
}
trap teardown EXIT

check_healthz() {
  local body
  body="$(curl -fsS --max-time 5 "$HARMONY_BASE_URL/api/healthz")" || return 1
  echo "$body"
  [[ $body == *'"status":"ok"'* && $body == *"\"build_sha\":\"$BUILD_SHA\""* ]]
}

# gated METHOD PATH [CURL ARGS...]: without a cookie the gate must refuse the request with
# 401, `no-store` and `noindex`: JSON `passcode_required` under /api, the passcode page
# (one password field) for any other GET.
gated() {
  local method=$1 path=$2 headers="$smoke_tmp/headers" body="$smoke_tmp/body" status
  shift 2
  status="$(curl -sS --max-time 5 -o "$body" -D "$headers" -w '%{http_code}' \
    -X "$method" "$@" "$HARMONY_BASE_URL$path")" || return 1
  if [[ $status != 401 ]]; then
    echo "$method $path: $status, not 401"
    return 1
  fi
  grep -qi '^cache-control:.*no-store' "$headers" || { echo "$method $path: not no-store"; return 1; }
  grep -qi '^x-robots-tag:.*noindex' "$headers" || { echo "$method $path: not noindex"; return 1; }
  if [[ $path == /api/* ]]; then
    grep -q '"code":"passcode_required"' "$body" || { echo "$method $path: not passcode_required"; return 1; }
  elif [[ $method == GET ]]; then
    grep -qi 'type="password"' "$body" || { echo "$method $path: not the passcode page"; return 1; }
  fi
  echo "$method $path: 401"
}

# The gate from a fresh visitor: only the health check and the gate's own routes are
# open, and the admin routes take the secret instead of the cookie.
check_gate_walk() {
  local path status
  for path in /api/session /api/events /api/docs /api/openapi.json /api/nope /api/budget \
    /assets/x.js / /requests/abc /network; do
    gated GET "$path" -H 'Accept: text/html,application/json' || return 1
  done
  gated POST /api/session/start || return 1
  gated POST /api/admin/live -H 'Content-Type: application/json' --data '{"enabled": false}' || return 1
  gated GET /api/admin/spend -H 'X-Admin-Secret: not-the-secret' || return 1
  # `--head`, not `-X HEAD`: with -X curl still waits for the Content-Length body a HEAD
  # response never sends, and times out.
  status="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
    "$HARMONY_BASE_URL/api/healthz")" || return 1
  [[ $status == 200 ]] || { echo "GET /api/healthz: $status"; return 1; }
  status="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' --head \
    "$HARMONY_BASE_URL/api/healthz")" || return 1
  [[ $status == 200 ]] || { echo "HEAD /api/healthz: $status"; return 1; }
  status="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
    "$HARMONY_BASE_URL/api/gate")" || return 1
  [[ $status == 200 ]] || { echo "GET /api/gate: $status"; return 1; }
  # compose.ci.yaml's throwaway admin secret: the one credential besides the cookie.
  status="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
    -H 'X-Admin-Secret: ci-only-admin-secret' "$HARMONY_BASE_URL/api/admin/spend")" || return 1
  [[ $status == 200 ]] || { echo "GET /api/admin/spend with the secret: $status"; return 1; }
  echo "healthz open to GET and HEAD; the gate page and the admin secret pass"
}

# Unlock the smoke checks' cookie jar. The passcode travels in the request body, built
# from the environment, never on a command line.
check_unlock() {
  local status
  status="$(python3 -I -c 'import json, os; print(json.dumps({"passcode": os.environ["HARMONY_PASSCODE"], "next": "/"}))' |
    curl -sS --max-time 5 -o "$smoke_tmp/body" -w '%{http_code}' -c "$jar" \
      -H 'Content-Type: application/json' --data @- "$HARMONY_BASE_URL/api/gate")"
  [[ $status == 200 ]] || { echo "POST /api/gate: $status"; return 1; }
  grep -q $'\tharmony_gate\t' "$jar" || { echo "no harmony_gate cookie"; return 1; }
  echo "unlocked: $(cat "$smoke_tmp/body")"
}

# The SPA is served from the image behind the gate: index.html `private, no-cache` and
# varying on the cookie, hashed assets `private` and immutable.
check_spa() {
  local headers="$smoke_tmp/headers" body asset
  body="$(curl -fsS --max-time 5 -b "$jar" -D "$headers" "$HARMONY_BASE_URL/")" || return 1
  if [[ $body == *'Harmony API is running'* ]]; then
    echo "got the API's placeholder page: the image has no web build"
    return 1
  fi
  grep -qi '^cache-control: private, no-cache' "$headers" || { echo "index.html is not private, no-cache"; return 1; }
  grep -qi '^vary:.*cookie' "$headers" || { echo "index.html doesn't vary on Cookie"; return 1; }
  [[ $body =~ (/assets/[^\"]+) ]] || { echo "index.html references no /assets/ file"; return 1; }
  asset=${BASH_REMATCH[1]}
  curl -fsS --max-time 5 -b "$jar" -o /dev/null -D "$headers" "$HARMONY_BASE_URL$asset" || return 1
  grep -qi '^cache-control: private,.*immutable' "$headers" || { echo "$asset is not private and immutable"; return 1; }
  echo "index.html private, no-cache, Vary: Cookie; $asset private, immutable"
}

# The suggested story in a browser, against the image's SPA and API (web/e2e).
check_ui_story() {
  E2E_BASE_URL="$HARMONY_BASE_URL" pnpm -C web exec playwright test
}

check_worker_running() {
  "${compose[@]}" ps --status running --services | grep -x worker >/dev/null
}

# The containerized worker loads the cassettes it plays in scripted mode (the host-side
# e2e check below uses the repo's copy, not the image's).
check_image_cassettes() {
  "${compose[@]}" exec -T worker python -c '
from harmony.config import get_settings
from harmony.llm.scripted import load_cassettes
from harmony.temporal.worker import scripted_model
settings = get_settings()
scripted_model(settings)  # refuses outside APP_ENV=local when the directory is missing
entries = load_cassettes(settings.cassette_dir)
assert entries, f"no cassette entries in {settings.cassette_dir}"
print(f"{len(entries)} cassette entries in {settings.cassette_dir}")
'
}

summary() {
  echo
  echo "compose smoke:"
  printf '  %s\n' "${results[@]}"
}

echo "--- build (BUILD_SHA=$BUILD_SHA)"
"${compose[@]}" build
echo "--- up"
if "${compose[@]}" up -d --wait --wait-timeout 180; then
  record "stack up (migrate, api healthy, worker running)" PASS
else
  record "stack up (migrate, api healthy, worker running)" FAIL
  summary
  exit 1
fi

if [[ ${SMOKE_ONLY:-} != ui ]]; then
  run_check "GET /api/healthz" "" check_healthz
  run_check "the gate, without a cookie" "" check_gate_walk
  run_check "unlock with the passcode" "" check_unlock
  run_check "SPA served from the image" "" check_spa
  run_check "cassettes in the worker image" "" check_image_cassettes
  run_check "act-as and access (p0_actas_check.sh)" scripts/p0_actas_check.sh \
    bash scripts/p0_actas_check.sh
  run_check "SSE resume (sse_resume_check.py)" scripts/sse_resume_check.py \
    uv run python scripts/sse_resume_check.py
  # -m "not minio" selects the e2e tests whether or not they carry the compose marker.
  run_check "representative turn e2e" tests/e2e/test_rep_turn_stream.py \
    uv run pytest tests/e2e/test_rep_turn_stream.py -m "not minio" -q
fi
run_check "suggested story in a browser (Playwright)" web/e2e/story.spec.ts check_ui_story
run_check "worker still running" "" check_worker_running

summary
exit "$failed"
