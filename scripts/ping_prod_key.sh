#!/usr/bin/env bash
# Check the production Anthropic key with the request shapes the hosted app sends
# (LIVE_DEMO_PLAN 3.8; deploy runbook step 7): `harmony llm ping --live --model opus`
# (a streamed featured internal turn, with both betas), then `--model haiku` (the
# screener). Two paid calls on the hosted key, about $0.01; run it only once the user
# has approved that spend.
#
#   scripts/ping_prod_key.sh
#
# The key is `anthropic_api_key` in infra/terraform/terraform.tfvars (HARMONY_TFVARS_FILE
# overrides the path). Python reads it in process and hands it to each ping as
# ANTHROPIC_API_KEY in the environment: it is never printed, traced or put on a command
# line. The calls run with APP_ENV=local and MODEL_OVERRIDE=none, and are recorded in the
# dev ledger under SPEND_PROJECT=harmony-hosted, so they don't count against the dev $200.
set -euo pipefail
set +x

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
tfvars="${HARMONY_TFVARS_FILE:-$repo_root/infra/terraform/terraform.tfvars}"
# Appended, so a `harmony` already on PATH comes first.
export PATH="$PATH:$repo_root/.venv/bin:/opt/homebrew/bin"
python="$(command -v python3)"

exec "$python" -I - "$tfvars" <<'PY'
"""Read anthropic_api_key from terraform.tfvars and ping with it, once per model."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

NAME = "anthropic_api_key"
# One HCL string assignment on its own line, with an optional trailing comment.
ASSIGNMENT = re.compile(rf'^\s*{NAME}\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*|//.*)?$')
ESCAPES = {'\\"': '"', "\\\\": "\\", "$${": "${", "%%{": "%{"}


def fail(message: str) -> NoReturn:
    print(f"ping_prod_key.sh: {message}", file=sys.stderr)
    sys.exit(2)


def read_key(tfvars: Path) -> str:
    try:
        lines = tfvars.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"can't read {tfvars}: {exc.strerror}")
    for line in lines:
        match = ASSIGNMENT.match(line)
        if match:
            key = re.sub(r'\\"|\\\\|\$\$\{|%%\{', lambda m: ESCAPES[m.group()], match[1])
            if not key.startswith("sk-ant-"):
                fail(f"{NAME} in {tfvars} is not an Anthropic key (sk-ant-...)")
            return key
    fail(f"{NAME} is not set in {tfvars}")


def main() -> None:
    tfvars = Path(sys.argv[1])
    key = read_key(tfvars)
    env = os.environ | {
        "ANTHROPIC_API_KEY": key,
        "APP_ENV": "local",
        "MODEL_OVERRIDE": "none",
        "SPEND_PROJECT": "harmony-hosted",
    }
    print(f"ping_prod_key.sh: using {NAME} from {tfvars}", flush=True)
    failed = False
    for model in ("opus", "haiku"):
        print(f"--- harmony llm ping --live --model {model}", flush=True)
        result = subprocess.run(
            ["harmony", "llm", "ping", "--live", "--model", model],
            env=env,
            stdin=subprocess.DEVNULL,
        )
        failed = failed or result.returncode != 0
    sys.exit(1 if failed else 0)


main()
PY
