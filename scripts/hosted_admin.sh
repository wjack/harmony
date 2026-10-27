#!/usr/bin/env bash
# Operator commands for the hosted demo (LIVE_DEMO_PLAN 3.4, 3.6). They need the admin
# secret only: no passcode, no unlock, no cookie jar.
#
#   scripts/hosted_admin.sh status       # GET /api/admin/spend
#   scripts/hosted_admin.sh pause        # live models off at once, then confirmed
#   scripts/hosted_admin.sh resume       # live models back on (clears a provider block)
#   scripts/hosted_admin.sh wipe --yes   # retire every world (POST /api/admin/wipe)
#
# The base URL and the secret come from `scripts/tf.sh output -raw web_url|admin_secret`,
# read in process. HARMONY_BASE_URL and HARMONY_ADMIN_SECRET override them, for a local
# or Compose API. The secret goes to curl as `-H @file`, from a private temporary
# directory removed on exit, so it never appears on a command line or in the output.
#
# If the API itself is down, pause in Render Shell on the worker or the web service,
# whichever is up: `harmony live pause` (and `harmony live resume` after). It writes the
# same switch through the app's own settings; the image has no psql.
set -euo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$PATH:/opt/homebrew/bin"

usage() {
  echo "usage: scripts/hosted_admin.sh status|pause|resume|wipe --yes" >&2
  exit 2
}

command=${1:-}
case $command in
  status | pause | resume) [[ $# == 1 ]] || usage ;;
  wipe) [[ $# == 2 && $2 == --yes ]] || usage ;;
  *) usage ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

tf_output() {
  "$repo_root/scripts/tf.sh" output -raw "$1"
}

base="${HARMONY_BASE_URL:-}"
[[ -n $base ]] || base="$(tf_output web_url)" || true
[[ -n $base ]] || { echo "hosted_admin.sh: no web_url in Terraform's outputs" >&2; exit 1; }
base="${base%/}"

secret="${HARMONY_ADMIN_SECRET:-}"
[[ -n $secret ]] || secret="$(tf_output admin_secret)" || true
[[ -n $secret ]] || { echo "hosted_admin.sh: no admin_secret in Terraform's outputs" >&2; exit 1; }
# printf is a builtin: the secret reaches the file without a process argument.
printf 'X-Admin-Secret: %s\n' "$secret" >"$work/admin.header"
unset secret

# call METHOD PATH [JSON]: the body goes to $work/body, the status code to stdout.
call() {
  local args=(-sS -o "$work/body" -w '%{http_code}' -X "$1" -H @"$work/admin.header")
  if [[ -n ${3:-} ]]; then
    args+=(-H 'Content-Type: application/json' --data "$3")
  fi
  rm -f "$work/body"
  curl "${args[@]}" "$base/api$2" || true
}

# expect STATUS GOT WHAT: fail with the response body unless the status matches.
expect() {
  [[ $2 == "$1" ]] && return 0
  echo "hosted_admin.sh: $3 returned $2" >&2
  [[ -f $work/body ]] && head -c 2000 "$work/body" >&2 && echo >&2
  exit 1
}

# json EXPR: a Python expression over the last body, bound to `d`.
json() {
  python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(eval(sys.argv[2], {"d": d}))
' "$work/body" "$1"
}

# set_live ENABLED PAUSED: switch, then confirm the switch through the spend view.
set_live() {
  local expected=$2
  expect 200 "$(call POST /admin/live "{\"enabled\": $1}")" "POST /api/admin/live"
  [[ $(json "d['paused']") == "$expected" ]] || {
    echo "hosted_admin.sh: the switch answered paused=$(json "d['paused']")" >&2
    exit 1
  }
  if [[ $1 == true && $(json "d['provider_blocked_until']") != None ]]; then
    echo "hosted_admin.sh: a provider block is still set" >&2
    exit 1
  fi
  expect 200 "$(call GET /admin/spend)" "GET /api/admin/spend"
  [[ $(json "d['live']['paused']") == "$expected" ]] || {
    echo "hosted_admin.sh: /api/admin/spend still reports paused=$(json "d['live']['paused']")" >&2
    exit 1
  }
}

case $command in
  status)
    expect 200 "$(call GET /admin/spend)" "GET /api/admin/spend"
    python3 -I -m json.tool "$work/body"
    ;;
  pause)
    set_live false True
    echo "hosted_admin.sh: live models are paused at $base"
    ;;
  resume)
    set_live true False
    echo "hosted_admin.sh: live models are on at $base"
    ;;
  wipe)
    expect 200 "$(call POST /admin/wipe)" "POST /api/admin/wipe"
    echo "hosted_admin.sh: retired $(json "len(d['retired_world_ids'])") worlds at $base"
    ;;
esac
