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
# once the user has approved the deploy and its spend. They, and `plan -out`, are also
# refused while infra/terraform differs from origin/main, and `apply <plan>` takes only
# a plan this script saved from origin/main's current Terraform. Apply the plan the user
# saw, not a fresh one: a non-interactive shell can't answer Terraform's prompt, and
# -auto-approve would apply unseen changes.
#
#   scripts/tf.sh plan -out=deploy.tfplan     # show the user its summary, never `show -json`
#   HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply deploy.tfplan
#   rm infra/terraform/deploy.tfplan          # it holds every secret in plaintext
set -euo pipefail
set +x
umask 077

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${HARMONY_ENV_FILE:-$repo_root/.env.local}"
tf_dir="$repo_root/infra/terraform"
export PATH="$PATH:/opt/homebrew/bin"
terraform="${TERRAFORM:-terraform}"
unmerged_ok=${HARMONY_TF_ALLOW_UNMERGED:-0}

# The saved plan this run writes (`plan -out=<file>`) or applies (`apply <file>`), as
# given: relative to infra/terraform, like Terraform's own -chdir.
plan_file=""
case "${1:-}" in
  plan)
    previous=""
    for arg in "${@:2}"; do
      case $arg in
        -out=* | --out=*) plan_file=${arg#*=} ;;
      esac
      [[ $previous == -out || $previous == --out ]] && plan_file=$arg
      previous=$arg
    done
    ;;
  apply)
    last=${!#}
    (($# > 1)) && [[ $last != -* ]] && plan_file=$last
    ;;
esac

# Terraform applies the files on disk, or the configuration a saved plan holds; no PR or
# CI run has seen either unless it is main's. Refuses a tree whose infra/terraform
# differs from origin/main: edited, on another branch, or with untracked files. Ignored
# files (terraform.tfvars, the state, saved plans) don't count. Fetch first.
require_main() {
  [[ $unmerged_ok == 1 ]] && return 0
  if ! git -C "$repo_root" rev-parse --verify --quiet origin/main >/dev/null; then
    echo "tf.sh: refusing 'terraform $1': no origin/main to compare infra/terraform with." >&2
    exit 2
  fi
  if ! git -C "$repo_root" diff --quiet origin/main -- infra/terraform ||
    [[ -n $(git -C "$repo_root" ls-files --others --exclude-standard -- infra/terraform) ]]; then
    echo "tf.sh: refusing 'terraform $1': infra/terraform differs from origin/main." >&2
    echo "tf.sh: run 'git fetch origin main' and plan and apply from main's Terraform;" >&2
    echo "tf.sh: HARMONY_TF_ALLOW_UNMERGED=1 overrides, with the user's approval." >&2
    exit 2
  fi
}

# Where `plan -out` notes the origin/main Terraform and the plan file it saved: inside
# .git, so it is never committed and never counts as an untracked file.
plan_record() {
  echo "$(git -C "$repo_root" rev-parse --absolute-git-dir)/harmony-tfplans/$(basename "$plan_file")"
}

# origin/main's infra/terraform tree and the saved plan's contents, as git object IDs.
plan_fingerprint() {
  local path=$plan_file
  [[ $path == /* ]] || path="$tf_dir/$path"
  printf '%s %s\n' \
    "$(git -C "$repo_root" rev-parse --verify --quiet origin/main:infra/terraform)" \
    "$(git -C "$repo_root" hash-object --no-filters -- "$path" 2>/dev/null)"
}

case "${1:-}" in
  apply | destroy)
    if [[ ${HARMONY_TF_ALLOW_APPLY:-0} != 1 ]]; then
      echo "tf.sh: refusing 'terraform $1' without the user's approval." >&2
      echo "tf.sh: set HARMONY_TF_ALLOW_APPLY=1 once the user has approved the deploy." >&2
      exit 2
    fi
    require_main "$1"
    # A saved plan holds the configuration it was made from, which may be a branch's;
    # the tree on disk now says nothing about it.
    if [[ -n $plan_file && $unmerged_ok != 1 ]]; then
      record="$(plan_record)"
      if [[ ! -f $record || $(cat "$record") != "$(plan_fingerprint)" ]]; then
        echo "tf.sh: refusing 'terraform apply $plan_file': tf.sh didn't save it from" >&2
        echo "tf.sh: origin/main's current Terraform. Plan again: scripts/tf.sh plan -out=$plan_file" >&2
        exit 2
      fi
    fi
    ;;
  plan)
    [[ -z $plan_file ]] || require_main "plan -out"
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
# So does a saved plan (`plan -out=deploy.tfplan`), which holds every variable and
# generated secret in plaintext: delete it once applied.
protect_state() {
  local file
  for file in "$tf_dir"/terraform.tfstate* "$tf_dir"/*.tfplan "$tf_dir"/tfplan; do
    [[ -f $file ]] && chmod 600 "$file"
  done
  return 0
}

status=0
"$terraform" -chdir="$tf_dir" "$@" || status=$?
protect_state
if [[ $status == 0 && -n $plan_file && $unmerged_ok != 1 ]]; then
  record="$(plan_record)"
  case $1 in
    plan) mkdir -p "$(dirname "$record")" && plan_fingerprint >"$record" ;;
    apply) rm -f "$record" ;; # Terraform won't apply a plan twice anyway
  esac
fi
exit "$status"
