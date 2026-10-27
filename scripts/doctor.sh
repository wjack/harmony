#!/usr/bin/env bash
# Print tool versions and check local prerequisites. Run via `make doctor`, which puts
# Homebrew and OrbStack on PATH. Never prints secret values.
set -uo pipefail
cd "$(dirname "$0")/.."

failed=0

row() { printf '  %-12s %s\n' "$1" "$2"; }

# GNU stat first: BSD stat rejects -c without printing, while GNU's -f would print the
# file system's details before failing.
file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }

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
  mode=$(file_mode .env.local)
  row ".env.local" "present, mode $mode"
  [[ $mode == 600 ]] || { row "" "expected mode 600: chmod 600 .env.local"; failed=1; }
else
  row ".env.local" "MISSING: copy .env.local.example, then run make env-local"
  failed=1
fi
# Terraform state and tfvars hold production secrets in plaintext: owner-only, always.
for file in infra/terraform/terraform.tfvars infra/terraform/terraform.tfstate*; do
  [[ -f $file ]] || continue
  mode=$(file_mode "$file")
  if ((8#$mode & 8#077)); then
    row "${file##*/}" "mode $mode, open to group or others: chmod 600 $file"
    failed=1
  else
    row "${file##*/}" "mode $mode"
  fi
done
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
