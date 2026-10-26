#!/usr/bin/env bash
# Regenerate the web client's types from FastAPI's OpenAPI document.
#
#   scripts/gen_api.sh           write web/src/api/schema.d.ts
#   scripts/gen_api.sh --check   exit 1 if the committed file is out of date
#
# The document comes from `create_app().openapi()`, so no server, database or Temporal
# is needed. Settings ignore .env.local and the build SHA is pinned, so the output is
# the same on every machine. The event payloads (`EventEnvelope` and every event model)
# are in components.schemas because `HarmonyApp.openapi()` adds them.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
web_dir="$repo_root/web"
target="$web_dir/src/api/schema.d.ts"

# Agent and GUI shells may not load the login profile; Homebrew is a fallback only, so
# tools already on PATH (CI's, or `make`'s) win.
export PATH="$PATH:/opt/homebrew/bin"

mode=write
case "${1:-}" in
  "") ;;
  --check) mode=check ;;
  *)
    echo "usage: $0 [--check]" >&2
    exit 2
    ;;
esac

if [[ ! -x "$web_dir/node_modules/.bin/openapi-typescript" ]]; then
  echo "gen_api: web dependencies are missing; run: pnpm -C web install --frozen-lockfile" >&2
  exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/harmony-gen-api.XXXXXX")
trap 'rm -rf "$work"' EXIT

# APP_ENV=local keeps prod-only validation out of the way; the document doesn't depend
# on any setting except the version, which is pinned here.
(
  cd "$repo_root"
  APP_ENV=local BUILD_SHA=openapi uv run --frozen --quiet python - "$work/openapi.json" <<'PY'
import json
import sys

from harmony.api.app import create_app
from harmony.config import Settings

settings = Settings(_env_file=None)
document = create_app(settings).openapi()
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(document, fh, indent=2)
    fh.write("\n")
PY
)

pnpm -C "$web_dir" exec openapi-typescript "$work/openapi.json" \
  --output "$work/schema.d.ts" >/dev/null

if [[ $mode == check ]]; then
  if ! diff -u "$target" "$work/schema.d.ts" >"$work/diff.txt"; then
    cat "$work/diff.txt"
    echo "gen_api: web/src/api/schema.d.ts is out of date; run: make gen-api" >&2
    exit 1
  fi
  echo "gen_api: web/src/api/schema.d.ts is up to date"
else
  mkdir -p "$(dirname "$target")"
  mv "$work/schema.d.ts" "$target"
  echo "gen_api: wrote ${target#"$repo_root"/}"
fi
