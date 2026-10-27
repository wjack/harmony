#!/usr/bin/env bash
# Read and trigger the hosted services' Render deploys (LIVE_DEMO_PLAN section 6: step 9b,
# step 12's redeploy, and Rotation's deploys) without the Render API key ever reaching a
# command line, a trace or the output.
#
#   scripts/render_deploys.sh status
#       each service's latest deploy: status, full commit SHA, trigger; exits 1 if one failed
#   HARMONY_RENDER_ALLOW_DEPLOY=1 scripts/render_deploys.sh deploy web|worker|minio <commit>
#       deploys that exact commit (40 hex characters), never "whatever main holds": the
#       live one (healthz's build_sha) after an env change, or a merged one CI passed
#
# `deploy` is refused unless HARMONY_RENDER_ALLOW_DEPLOY=1, which is set only once the
# user has approved that deploy. The key is RENDER_API_KEY (and the workspace
# RENDER_OWNER_ID), from the environment or else read in process from .env.local
# (HARMONY_ENV_FILE overrides the path). Python sends it as a request header; only
# service names, deploy states and commit SHAs are printed. RENDER_API_URL overrides the
# API (https only, except for a local test server).
set -euo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
export HARMONY_ENV_FILE="${HARMONY_ENV_FILE:-$repo_root/.env.local}"

usage() {
  echo "usage: scripts/render_deploys.sh status | deploy web|worker|minio <commit sha>" >&2
  exit 2
}

case ${1:-} in
  status) (($# == 1)) || usage ;;
  deploy)
    (($# == 3)) || usage
    [[ $2 == web || $2 == worker || $2 == minio ]] || usage
    [[ $3 =~ ^[0-9a-f]{40}$ ]] || {
      echo "render_deploys.sh: give the full 40-character commit SHA to deploy" >&2
      exit 2
    }
    if [[ ${HARMONY_RENDER_ALLOW_DEPLOY:-0} != 1 ]]; then
      echo "render_deploys.sh: refusing to deploy without the user's approval." >&2
      echo "render_deploys.sh: set HARMONY_RENDER_ALLOW_DEPLOY=1 once the user has approved it." >&2
      exit 2
    fi
    ;;
  *) usage ;;
esac

exec python3 -I - "$@" <<'PY'
"""Render API calls for the deploy runbook; the key stays in this process."""

from __future__ import annotations  # macOS's /usr/bin/python3 is 3.9

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "harmony"
SERVICES = ("web", "worker", "minio")
FAILED = {"build_failed", "update_failed", "pre_deploy_failed", "canceled"}
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def fail(message: str, code: int = 1) -> None:
    print(f"render_deploys.sh: {message}", file=sys.stderr)
    sys.exit(code)


def from_env_file(name: str) -> str | None:
    """`name`'s value in .env.local, parsed as scripts/tf.sh does: quotes stripped, blank
    values skipped, and the last assignment wins."""
    try:
        with open(os.environ["HARMONY_ENV_FILE"], encoding="utf-8") as env_file:
            lines = env_file.read().splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        match = ASSIGNMENT.match(line.rstrip("\r"))
        if match and match[1] == name:
            value = match[2]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found = value or found
    return found


def setting(name: str) -> str | None:
    return os.environ.get(name) or from_env_file(name)


def api_base() -> str:
    base = os.environ.get("RENDER_API_URL", "https://api.render.com/v1").rstrip("/")
    parts = urllib.parse.urlsplit(base)
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    if parts.scheme != "https" and not local:
        fail("RENDER_API_URL must be https (or a local test server)", 2)
    return base


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect fails the call instead of carrying the key to another URL."""

    def redirect_request(self, *args: object) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirects)
KEY = setting("RENDER_API_KEY")
if not KEY:
    fail("RENDER_API_KEY is not set, in the environment or in the env file", 2)
OWNER = setting("RENDER_OWNER_ID")
BASE = api_base()


def call(method: str, path: str, body: dict[str, str] | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_unredirected_header("Authorization", f"Bearer {KEY}")
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}").get("message", "")
        except ValueError:
            detail = ""
        fail(f"{method} {path.split('?')[0]} returned {error.code} {detail}".rstrip())
    except urllib.error.URLError as error:
        fail(f"{method} {path.split('?')[0]} failed: {error.reason}")
    return json.loads(raw) if raw else None


def service_id(short: str) -> str:
    name = f"{PREFIX}-{short}"
    query = {"name": name, "limit": "100"}
    if OWNER:
        query["ownerId"] = OWNER
    rows = call("GET", "/services?" + urllib.parse.urlencode(query)) or []
    ids = [row["service"]["id"] for row in rows if row["service"]["name"] == name]
    if len(ids) != 1:
        fail(f"expected one service named {name}, found {len(ids)}")
    return ids[0]


def describe(deploy: dict) -> str:
    commit = (deploy.get("commit") or {}).get("id") or "?"
    when = deploy.get("finishedAt") or deploy.get("createdAt") or "?"
    return f"{deploy.get('status')}, commit {commit}, trigger {deploy.get('trigger')}, {when}"


def status() -> None:
    failed = False
    for short in SERVICES:
        rows = call("GET", f"/services/{service_id(short)}/deploys?limit=1") or []
        if not rows:
            print(f"{PREFIX}-{short}: no deploys")
            continue
        deploy = rows[0]["deploy"]
        failed = failed or deploy.get("status") in FAILED
        print(f"{PREFIX}-{short}: {describe(deploy)}")
    sys.exit(1 if failed else 0)


def deploy(short: str, commit: str) -> None:
    created = call("POST", f"/services/{service_id(short)}/deploys", {"commitId": commit})
    if isinstance(created, dict) and "id" in created:
        print(f"{PREFIX}-{short}: deploy {created['id']} {describe(created)}")
    else:
        print(f"{PREFIX}-{short}: deploy of {commit} queued")


if sys.argv[1] == "status":
    status()
else:
    deploy(sys.argv[2], sys.argv[3])
PY
