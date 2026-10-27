#!/usr/bin/env bash
# Run Terraform in infra/terraform with credentials from .env.local. Values are never
# printed, traced or put on a command line; only key names are reported.
#
#   scripts/tf.sh init
#   scripts/tf.sh validate
#   scripts/tf.sh plan                   # reads the real Render and Temporal Cloud accounts
#   scripts/tf.sh output -raw web_url    # sensitive outputs only into a variable or a pipe
#
# Only these .env.local keys reach Terraform's environment:
#   RENDER_API_KEY, RENDER_OWNER_ID, TEMPORAL_CLOUD_API_KEY
#                               as is; the providers read them directly
#   PROD_ANTHROPIC_API_KEY      as TF_VAR_anthropic_api_key (the production workspace's
#                               key, never the development ANTHROPIC_API_KEY)
#   TEMPORAL_CLOUD_ACCOUNT_ID   as TF_VAR_temporal_account_id
#   TF_VAR_<name>               as is
# Blank values count as unset. HARMONY_ENV_FILE overrides the file's path.
#
# Precedence: Terraform reads TF_VAR_* below terraform.tfvars, so a variable set in
# terraform.tfvars (the production key, provider credentials, the passcode) wins over
# its .env.local mapping here, and the provider variables set there win over the
# environment the providers would read.
#
# The state holds every secret in plaintext (the model key, the passcode, the admin and
# session secrets, the Temporal key, the MinIO password, the database URL). Terraform runs
# under umask 077, and every terraform.tfstate* file is set to mode 600 afterwards;
# scripts/doctor.sh flags one that isn't.
#
# `apply` and `destroy` are refused unless HARMONY_TF_ALLOW_APPLY=1, which is set only
# once the user has approved the deploy and its spend.
set -euo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${HARMONY_ENV_FILE:-$repo_root/.env.local}"
tf_dir="$repo_root/infra/terraform"
export PATH="$PATH:/opt/homebrew/bin"
terraform="${TERRAFORM:-terraform}"

case "${1:-}" in
  apply | destroy)
    if [[ ${HARMONY_TF_ALLOW_APPLY:-0} != 1 ]]; then
      echo "tf.sh: refusing 'terraform $1' without the user's approval." >&2
      echo "tf.sh: set HARMONY_TF_ALLOW_APPLY=1 once the user has approved the deploy." >&2
      exit 2
    fi
    ;;
esac

# Prints the environment name a .env.local key is exported as, or nothing to skip it.
export_name() {
  case $1 in
    RENDER_API_KEY | RENDER_OWNER_ID | TEMPORAL_CLOUD_API_KEY) echo "$1" ;;
    PROD_ANTHROPIC_API_KEY) echo TF_VAR_anthropic_api_key ;;
    TEMPORAL_CLOUD_ACCOUNT_ID) echo TF_VAR_temporal_account_id ;;
    TF_VAR_?*) echo "$1" ;;
  esac
}

load_env_file() {
  local assignment='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
  local double_quoted='^"(.*)"$' single_quoted="^'(.*)'$"
  local line key value name loaded=""
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    [[ $line =~ $assignment ]] || continue
    key=${BASH_REMATCH[2]}
    value=${BASH_REMATCH[3]}
    name="$(export_name "$key")"
    [[ -n $name ]] || continue
    if [[ $value =~ $double_quoted || $value =~ $single_quoted ]]; then
      value=${BASH_REMATCH[1]}
    fi
    [[ -n $value ]] || continue
    export "$name=$value"
    loaded+="${loaded:+, }$key"
  done <"$env_file"
  echo "tf.sh: loaded ${loaded:-no keys} from $env_file" >&2
}

if [[ -f $env_file ]]; then
  load_env_file
else
  echo "tf.sh: $env_file not found; running without credentials" >&2
fi

# State written before umask 077, or by Terraform run some other way, gets 600 too.
protect_state() {
  local file
  for file in "$tf_dir"/terraform.tfstate*; do
    [[ -f $file ]] && chmod 600 "$file"
  done
  return 0
}

status=0
"$terraform" -chdir="$tf_dir" "$@" || status=$?
protect_state
exit "$status"
