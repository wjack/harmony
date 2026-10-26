#!/usr/bin/env bash
# Print tool versions and check local prerequisites. Run via `make doctor`, which puts
# Homebrew and OrbStack on PATH. Never prints secret values.
set -uo pipefail
cd "$(dirname "$0")/.."

failed=0

row() { printf '  %-12s %s\n' "$1" "$2"; }

check() {
  local name=$1
  shift
  local out
  if out=$("$@" 2>/dev/null | head -n1) && [[ -n $out ]]; then
    row "$name" "$out"
  else
    row "$name" "MISSING or not working: $*"
    failed=1
  fi
}

echo "Tools"
check uv uv --version
check python uv run --quiet python --version
check node node --version
check pnpm pnpm --version
check temporal temporal --version
check docker docker version --format 'server {{.Server.Version}}'
check compose docker compose version --short
check terraform terraform version
check gh gh --version

echo "Project"
if [[ -f .env.local ]]; then
  mode=$(stat -f '%Lp' .env.local 2>/dev/null || stat -c '%a' .env.local)
  row ".env.local" "present, mode $mode"
  [[ $mode == 600 ]] || { row "" "expected mode 600: chmod 600 .env.local"; failed=1; }
else
  row ".env.local" "MISSING: copy .env.local.example, then run make env-local"
  failed=1
fi
uv run --quiet python - <<'EOF' || failed=1
from harmony.config import get_settings

s = get_settings()
ledger = s.spend_ledger_path
print(f"  {'app_env':<12} {s.app_env}")
print(f"  {'model_mode':<12} {s.model_mode} (override: {s.model_override})")
state = "exists" if ledger and ledger.is_file() else "MISSING"
print(f"  {'ledger':<12} {ledger} ({state})")
EOF

if docker compose ps --format '{{.Service}} {{.Health}}' >/dev/null 2>&1; then
  echo "Compose"
  docker compose ps --format '{{printf "%-12s" .Service}} {{.State}} {{.Health}}' | sed 's/^/  /'
fi

exit $failed
