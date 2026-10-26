#!/usr/bin/env bash
# The P0 exit check (P0_PLAN.md T13), run by `make p0-exit`:
#
#   1. `make up`, `make migrate`, `make seed` (a new harmony-demo world);
#   2. the API and worker in the background with MODEL_MODE=scripted;
#   3. p0_actas_check.sh, sse_resume_check.py and `harmony dev hello` against them, and
#      the representative-turn e2e (which runs its own app on a test database);
#   4. `harmony spend` on a temporary ledger with simulated threshold crossings, and on
#      the real ledger, read-only (scripts/p0_spend_check.py);
#   5. PASS or FAIL per check; the background API and worker are always stopped.
#
# The Compose services stay up afterwards, as after `make up`. Nothing calls a model:
# MODEL_MODE=scripted throughout, and ANTHROPIC_BASE_URL points at the discard port.
#
#   P0_EXIT_API_PORT=8100   the background API's port (8000 is left to `make dev`)
#   P0_EXIT_KEEP_LOGS=1     keep the API and worker logs after a pass
set -euo pipefail
cd "$(dirname "$0")/.."

# make puts these on PATH itself; a direct run on macOS needs them too.
export PATH="$PATH:/opt/homebrew/bin:$HOME/.orbstack/bin"

# Each background job gets its own process group, so stopping it stops `uv run`'s child.
set -m

port="${P0_EXIT_API_PORT:-8100}"
export HARMONY_BASE_URL="http://127.0.0.1:${port}"
export MODEL_MODE=scripted ANTHROPIC_BASE_URL="http://127.0.0.1:9"
export HARMONY_TEST_DB_PREFIX="${HARMONY_TEST_DB_PREFIX:-p0exit}"

work="$(mktemp -d "${TMPDIR:-/tmp}/harmony-p0-exit.XXXXXX")"
api_pid=""
worker_pid=""
results=()
failed=0

record() { # name status [detail]
  results+=("$(printf '%-4s  %s%s' "$2" "$1" "${3:+  ($3)}")")
  [[ $2 == FAIL ]] && failed=1
  return 0
}

check() { # name command...
  local name=$1
  shift
  echo
  echo "--- $name"
  if "$@"; then record "$name" PASS; else record "$name" FAIL; fi
}

stop() { # pid
  local pid=$1
  [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
}

summary() {
  echo
  echo "P0 exit check:"
  printf '  %s\n' "${results[@]}"
  if ((failed)); then echo "P0 exit: FAIL"; else echo "P0 exit: PASS"; fi
}

teardown() {
  local status=$?
  stop "$api_pid"
  stop "$worker_pid"
  if ((failed || status)); then
    for log in api worker; do
      if [[ -s $work/$log.log ]]; then
        echo
        echo "--- $log log (last 60 lines)"
        tail -n 60 "$work/$log.log"
      fi
    done
  fi
  if ((failed || status)) || [[ ${P0_EXIT_KEEP_LOGS:-0} == 1 ]]; then
    echo "logs: $work"
  else
    rm -rf "$work"
  fi
}
trap teardown EXIT

wait_for_api() {
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "$HARMONY_BASE_URL/api/healthz" >/dev/null 2>&1; then
      curl -fsS --max-time 2 "$HARMONY_BASE_URL/api/healthz"
      echo
      return 0
    fi
    kill -0 "$api_pid" 2>/dev/null || { echo "the API exited"; return 1; }
    sleep 1
  done
  echo "the API did not answer /api/healthz within 60s"
  return 1
}

hello_world() {
  local output
  output="$(uv run --frozen harmony dev hello --world "$world_id" --ticks 3 --interval 0.2 --wait 60)" ||
    return 1
  echo "$output"
  [[ $output == *"hello.tick seqs: "* ]]
}

processes_running() {
  kill -0 "$api_pid" 2>/dev/null || { echo "the API exited"; return 1; }
  kill -0 "$worker_pid" 2>/dev/null || { echo "the worker exited"; return 1; }
}

# --- 1. services, schema, a world -------------------------------------------------------

if curl -fsS --max-time 2 "$HARMONY_BASE_URL/api/healthz" >/dev/null 2>&1; then
  echo "p0-exit: something already answers on port $port; set P0_EXIT_API_PORT" >&2
  record "port $port is free" FAIL
  summary
  exit 1
fi

check "make up" make --no-print-directory up
check "make migrate" make --no-print-directory migrate
echo
echo "--- make seed"
world_id=""
if make --no-print-directory seed | tee "$work/seed.log"; then
  world_id="$(awk '$1 == "world" {print $2; exit}' "$work/seed.log")"
fi
if [[ -n $world_id ]]; then record "make seed" PASS; else record "make seed" FAIL; fi
if ((failed)); then
  summary
  exit 1
fi

# --- 2. the API and worker ---------------------------------------------------------------

echo
echo "--- start the API (port $port) and the worker, MODEL_MODE=scripted"
uv run --frozen uvicorn harmony.api.app:create_app --factory \
  --host 127.0.0.1 --port "$port" >"$work/api.log" 2>&1 &
api_pid=$!
uv run --frozen harmony worker --graceful-shutdown 5 >"$work/worker.log" 2>&1 &
worker_pid=$!

check "API healthy" wait_for_api
if ((failed)); then
  summary
  exit 1
fi

# --- 3. the P0 checks --------------------------------------------------------------------

check "act-as and access (p0_actas_check.sh)" bash scripts/p0_actas_check.sh
check "SSE resume (sse_resume_check.py)" uv run --frozen python scripts/sse_resume_check.py
check "HelloWorkflow on the background worker (harmony dev hello)" hello_world
check "representative turn e2e" uv run --frozen pytest tests/e2e/test_rep_turn_stream.py -q
check "spend alerts and the real ledger (p0_spend_check.py)" \
  uv run --frozen python scripts/p0_spend_check.py
check "API and worker still running" processes_running

summary
exit "$failed"
