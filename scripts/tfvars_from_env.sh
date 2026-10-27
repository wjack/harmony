#!/usr/bin/env bash
# Copy the Terraform providers' credentials from .env.local into terraform.tfvars, so
# Terraform needs nothing from the environment (LIVE_DEMO_PLAN 3.8):
#
#   RENDER_API_KEY             -> render_api_key
#   RENDER_OWNER_ID            -> render_owner_id
#   TEMPORAL_CLOUD_API_KEY     -> temporal_cloud_api_key
#   TEMPORAL_CLOUD_ACCOUNT_ID  -> temporal_account_id
#
# It only adds a variable terraform.tfvars doesn't set yet: an existing line is never
# changed, even when .env.local holds a different value. Values are never printed,
# traced or put on a command line; only names are reported. The file stays at mode 600.
#
#   scripts/tfvars_from_env.sh
#
# HARMONY_ENV_FILE and HARMONY_TFVARS_FILE override the two paths.
set -euo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${HARMONY_ENV_FILE:-$repo_root/.env.local}"
tfvars="${HARMONY_TFVARS_FILE:-$repo_root/infra/terraform/terraform.tfvars}"

# Prints the Terraform variable a .env.local key is copied to, or nothing to skip it.
variable_for() {
  case $1 in
    RENDER_API_KEY) echo render_api_key ;;
    RENDER_OWNER_ID) echo render_owner_id ;;
    TEMPORAL_CLOUD_API_KEY) echo temporal_cloud_api_key ;;
    TEMPORAL_CLOUD_ACCOUNT_ID) echo temporal_account_id ;;
  esac
}

# Whether terraform.tfvars already assigns the variable (commented lines don't count).
assigned() {
  [[ -f $tfvars ]] && grep -Eq "^[[:space:]]*$1[[:space:]]*=" "$tfvars"
}

# The value as an HCL string body: backslashes and quotes escaped, and template
# sequences (${ and %{) doubled so Terraform reads them literally.
hcl_escape() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//\$\{/\$\$\{}
  value=${value//%\{/%%\{}
  printf '%s' "$value"
}

if [[ ! -f $env_file ]]; then
  echo "tfvars_from_env.sh: $env_file not found" >&2
  exit 1
fi
if [[ ! -f $tfvars ]]; then
  : >"$tfvars"
fi
chmod 600 "$tfvars"
# Appended lines must start on a line of their own.
if [[ -s $tfvars && -n $(tail -c 1 "$tfvars") ]]; then
  printf '\n' >>"$tfvars"
fi

assignment='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
double_quoted='^"(.*)"$'
single_quoted="^'(.*)'$"
added="" kept="" seen=""
while IFS= read -r line || [[ -n $line ]]; do
  line=${line%$'\r'}
  [[ $line =~ $assignment ]] || continue
  key=${BASH_REMATCH[2]}
  value=${BASH_REMATCH[3]}
  name="$(variable_for "$key")"
  [[ -n $name ]] || continue
  if [[ $value =~ $double_quoted || $value =~ $single_quoted ]]; then
    value=${BASH_REMATCH[1]}
  fi
  [[ -n $value ]] || continue
  # A repeated key in .env.local: the first value counts, as for a variable already set.
  [[ " $seen " == *" $name "* ]] && continue
  seen+=" $name"
  if assigned "$name"; then
    kept+="${kept:+, }$name"
    continue
  fi
  # printf is a builtin, so the value never appears in a process's arguments.
  printf '%s = "%s"\n' "$name" "$(hcl_escape "$value")" >>"$tfvars"
  added+="${added:+, }$name"
done <"$env_file"

missing=""
for name in render_api_key render_owner_id temporal_cloud_api_key temporal_account_id; do
  [[ " $seen " == *" $name "* ]] || assigned "$name" || missing+="${missing:+, }$name"
done

echo "tfvars_from_env.sh: added ${added:-nothing} to $tfvars"
[[ -z $kept ]] || echo "tfvars_from_env.sh: left as they were: $kept"
[[ -z $missing ]] || echo "tfvars_from_env.sh: not in $env_file, and not set: $missing"
