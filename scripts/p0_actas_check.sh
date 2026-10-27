#!/usr/bin/env bash
# P0 act-as and access check (P0_PLAN.md T4): drives a running API the way a browser
# does, with curl and one cookie jar per visitor, and prints PASS or FAIL per check.
# Exits non-zero if any check fails.
#
#   HARMONY_BASE_URL   the API's origin (default http://localhost:8000)
#   HARMONY_PASSCODE   the master passcode, when the API runs with ACCESS_CODE set: each
#                      visitor's jar passes the gate first, as the passcode page does
#
# The database may be empty: POST /api/session/start seeds a world lazily. Needs curl and
# python3 (for reading JSON). Nothing here needs ADMIN_SECRET, and nothing is wiped.
set -uo pipefail

BASE="${HARMONY_BASE_URL:-http://localhost:8000}"
BASE="${BASE%/}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failed=0
results=()

record() { # status name [detail]
  local line
  line=$(printf '%-4s  %s%s' "$1" "$2" "${3:+  ($3)}")
  results+=("$line")
  echo "$line"
  [[ $1 == FAIL ]] && failed=1
  return 0
}

# unlock JAR: give the visitor the gate's cookie, once. The passcode goes into the body
# from the environment, never onto a command line. A failure leaves the jar locked, so
# the check that needed it fails on its 401.
unlock() {
  local jar=$1 status
  [[ -n ${HARMONY_PASSCODE:-} && ! -e $work/$jar.unlocked ]] || return 0
  status=$(python3 -I -c 'import json, os
print(json.dumps({"passcode": os.environ["HARMONY_PASSCODE"], "next": "/"}))' |
    curl -sS -o /dev/null -w '%{http_code}' -b "$work/$jar.jar" -c "$work/$jar.jar" \
      -H 'Content-Type: application/json' --data @- "$BASE/api/gate") || true
  if [[ $status == 200 ]]; then
    touch "$work/$jar.unlocked"
  else
    echo "unlock ($jar): POST /api/gate returned $status" >&2
  fi
}

# call JAR METHOD PATH [JSON]: the response body goes to $work/body, its headers to
# $work/headers, and the status code to stdout.
call() {
  local jar=$1 method=$2 path=$3 body=${4:-}
  unlock "$jar"
  local args=(-sS -o "$work/body" -D "$work/headers" -w '%{http_code}'
    -b "$work/$jar.jar" -c "$work/$jar.jar" -X "$method")
  if [[ -n $body ]]; then
    args+=(-H 'Content-Type: application/json' --data "$body")
  fi
  rm -f "$work/body" "$work/headers"
  # On a connection failure curl still prints 000 for the status.
  curl "${args[@]}" "$BASE/api$path" || true
}

# json EXPR: evaluate a Python expression over the last body, bound to `d`.
json() {
  python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(eval(sys.argv[2], {"d": d}))
except Exception as exc:
    print(f"<unreadable: {exc}>")
' "$work/body" "$1"
}

cursor() { # the X-Harmony-Cursor header of the last response
  tr -d '\r' <"$work/headers" | awk -F': *' 'tolower($1) == "x-harmony-cursor" {print $2}'
}

member_id() { # JAR SLUG: the member's ID in that visitor's world
  local status
  status=$(call "$1" GET /members)
  [[ $status == 200 ]] || return 1
  json "next((m['id'] for m in d['featured'] + d['all'] if m['slug'] == '$2'), '')"
}

act_as() { # JAR SLUG: prints the status code
  local id
  id=$(member_id "$1" "$2") || { echo "members-failed"; return; }
  call "$1" POST /session/act-as "{\"member_id\": \"$id\"}"
}

inbox_ids() { # JAR: the acting member's thread IDs, space-separated
  local status
  status=$(call "$1" GET /threads)
  [[ $status == 200 ]] || { echo "status-$status"; return; }
  json "' '.join(sorted(t['id'] for t in d))"
}

echo "p0_actas_check against $BASE"

# --- 0. Without a session, nothing resolves ---------------------------------------------
status=$(call anon GET /session)
if [[ $status == 401 ]]; then
  record PASS "0. no cookie: GET /api/session is 401"
else
  record FAIL "0. no cookie: GET /api/session is 401" "got $status"
fi

# --- 1. start creates a world ------------------------------------------------------------
world=""
status=$(call a POST /session/start)
if [[ $status == 200 ]]; then
  world=$(json "d['world_id']")
  start_cursor=$(cursor)
  facts=$(json "(d['world']['status'], d['member']['slug'], d['member']['org']['slug'])")
  status=$(call a GET /session)
  same=$(json "d['world_id']")
  if [[ $facts == "('live', 'alex', 'henderson')" && $status == 200 && $same == "$world" && -n $start_cursor ]]; then
    record PASS "1. start creates a world" "world $world, cursor $start_cursor"
  else
    record FAIL "1. start creates a world" "$facts; GET /session $status -> $same; cursor '$start_cursor'"
  fi
else
  record FAIL "1. start creates a world" "POST /api/session/start returned $status: $(head -c 300 "$work/body")"
fi

# --- 2. Alex sees only the Henderson-visible thread --------------------------------------
history=""
status=$(call a GET /threads)
if [[ $status == 200 ]]; then
  summary=$(json "[(t['requester']['slug'], t['responder']['slug'], t['access_reason']) for t in d]")
  if [[ $summary == "[('henderson', 'northlake', 'routed')]" && -n $(cursor) ]]; then
    history=$(json "d[0]['id']")
    record PASS "2. Alex sees only the Henderson-visible thread" "$history"
  else
    record FAIL "2. Alex sees only the Henderson-visible thread" "inbox $summary, cursor '$(cursor)'"
  fi
else
  record FAIL "2. Alex sees only the Henderson-visible thread" "GET /api/threads returned $status"
fi

# --- 3. Helen sees the routed thread -----------------------------------------------------
status=$(act_as a helen)
if [[ $status == 200 && $(json "(d['member']['slug'], d['epoch'])") == "('helen', 1)" ]]; then
  status=$(call a GET /threads)
  reasons=$(json "next((t['access_reasons'] for t in d if t['id'] == '$history'), None)")
  detail=$(call a GET "/threads/$history")
  if [[ -n $history && $reasons == *routed* && $detail == 200 ]]; then
    record PASS "3. Helen sees the routed thread" "reasons $reasons"
  else
    record FAIL "3. Helen sees the routed thread" "reasons $reasons, GET /api/threads/{id} $detail"
  fi
else
  record FAIL "3. Helen sees the routed thread" "act-as helen returned $status: $(head -c 300 "$work/body")"
fi

# --- 4. Another org's member doesn't see it, and the thread is a 404 ---------------------
# Every demo member is an admin (FEEDBACK_R2_PLAN.md E1), so the one who can't open the
# thread is at an org that isn't on it: Jordan, at Vantage.
status=$(act_as a jordan)
if [[ $status == 200 && $(json "d['member']['slug']") == jordan ]]; then
  ids=$(inbox_ids a)
  detail=$(call a GET "/threads/$history")
  body=$(json "d")
  if [[ -n $history && $ids != status-* && $ids != *"$history"* && $detail == 404 ]]; then
    record PASS "4. Jordan doesn't see it; GET /api/threads/{id} is 404" "$body"
  else
    record FAIL "4. Jordan doesn't see it; GET /api/threads/{id} is 404" "inbox '$ids', GET $detail"
  fi
else
  record FAIL "4. Jordan doesn't see it; GET /api/threads/{id} is 404" "act-as jordan returned $status"
fi

# --- 5. act-as with a member from another world is 403 -----------------------------------
status=$(call b POST /session/start '{"scenario": "minimal"}')
foreign=""
[[ $status == 200 ]] && foreign=$(json "d['member']['id']")
if [[ -n $foreign ]]; then
  status=$(call a POST /session/act-as "{\"member_id\": \"$foreign\"}")
  random=$(call a POST /session/act-as '{"member_id": "00000000-0000-4000-8000-000000000000"}')
  call a GET /session >/dev/null
  still=$(json "(d['member']['slug'], d['world_id'])")
  if [[ $status == 403 && $random == 403 && $still == "('jordan', '$world')" ]]; then
    record PASS "5. act-as with another world's member is 403"
  else
    record FAIL "5. act-as with another world's member is 403" "got $status (unknown ID $random); now $still"
  fi
else
  record FAIL "5. act-as with another world's member is 403" "second visitor's start returned $status"
fi

# --- 6. reset changes the world; the old one retires; slugs still resolve -----------------
# Reset starts the story over, so the session acts as the story's member (Alex) again.
status=$(call a POST /worlds/reset)
if [[ $status == 200 ]]; then
  new=$(json "d['session']['world_id']")
  previous=$(json "(d['previous_world_id'], d['previous_world_status'])")
  slug=$(json "d['session']['member']['slug']")
  call a GET /session >/dev/null
  current=$(json "d['world_id']")
  helen=$(act_as a helen)
  ids=$(inbox_ids a)
  if [[ $new != "$world" && $current == "$new" && $slug == alex && $helen == 200 &&
    ($previous == "('$world', 'retiring')" || $previous == "('$world', 'retired')") &&
    $ids == *"$history"* ]]; then
    record PASS "6. reset forks a new world; old one retiring/retired; slugs resolve" \
      "$world -> $new, previous $previous"
  else
    record FAIL "6. reset forks a new world; old one retiring/retired; slugs resolve" \
      "new $new (session $current), previous $previous, member $slug, act-as helen $helen, inbox '$ids'"
  fi
else
  record FAIL "6. reset forks a new world; old one retiring/retired; slugs resolve" \
    "POST /api/worlds/reset returned $status: $(head -c 300 "$work/body")"
fi

# --- 7. the admin wipe needs the secret ----------------------------------------------------
status=$(call a POST /admin/wipe)
if [[ $status == 403 ]]; then
  record PASS "7. POST /api/admin/wipe without the secret is 403"
else
  record FAIL "7. POST /api/admin/wipe without the secret is 403" "got $status"
fi

echo
echo "p0_actas_check summary:"
printf '%s\n' "${results[@]}"
exit "$failed"
