"""The deploy scripts keep secrets off command lines and out of their output, and keep
the files that hold them private (LIVE_DEMO_PLAN 3.8, L6).

Each script runs for real, against temporary files and stub programs first on PATH:
nothing here reaches Terraform, Render, Temporal Cloud or the Anthropic API.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(
    script: Path, *args: str, env: Mapping[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _stub(bin_dir: Path, name: str, source: str) -> Path:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(source)
    path.chmod(0o755)
    return path


def _base_env(**extra: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
    return env | extra


def _copy_into(tree: Path, *names: str) -> None:
    """Copy scripts into a throwaway repo layout, so whatever they touch relative to
    their own location (infra/terraform, .env.local) is inside `tree`."""
    (tree / "scripts").mkdir(parents=True, exist_ok=True)
    (tree / "infra" / "terraform").mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(SCRIPTS / name, tree / "scripts" / name)


# --- tfvars_from_env.sh ---------------------------------------------------------------

RENDER_KEY = "rnd_fixture-render-key-0001"
OWNER_FROM_ENV = "tea-fixture-owner-from-env"
TEMPORAL_KEY = 'tc-"quoted"\\back-${interp}-%{directive}'


def _env_file(path: Path) -> Path:
    path.write_text(
        "# provider credentials\n"
        f"RENDER_API_KEY={RENDER_KEY}\n"
        f'export RENDER_OWNER_ID="{OWNER_FROM_ENV}"\n'
        f"TEMPORAL_CLOUD_API_KEY='{TEMPORAL_KEY}'\n"
        "TEMPORAL_CLOUD_ACCOUNT_ID=\n"
        "ANTHROPIC_API_KEY=sk-ant-not-copied\n"
    )
    return path


def test_tfvars_from_env_adds_only_missing_keys_and_prints_no_value(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path / ".env.local")
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('render_owner_id = "tea-already-set"')  # no trailing newline
    tfvars.chmod(0o644)
    env = _base_env(HARMONY_ENV_FILE=str(env_file), HARMONY_TFVARS_FILE=str(tfvars))

    result = _run(SCRIPTS / "tfvars_from_env.sh", env=env)

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    for value in (RENDER_KEY, OWNER_FROM_ENV, TEMPORAL_KEY, "tea-already-set", "sk-ant"):
        assert value not in output
    assert "added render_api_key, temporal_cloud_api_key" in result.stdout
    assert "left as they were: render_owner_id" in result.stdout
    assert "not set: temporal_account_id" in result.stdout
    assert _mode(tfvars) == 0o600
    assert tfvars.read_text().splitlines() == [
        'render_owner_id = "tea-already-set"',
        f'render_api_key = "{RENDER_KEY}"',
        'temporal_cloud_api_key = "tc-\\"quoted\\"\\\\back-$${interp}-%%{directive}"',
    ]

    again = _run(SCRIPTS / "tfvars_from_env.sh", env=env)
    assert again.returncode == 0, again.stderr
    assert "added nothing" in again.stdout
    assert len(tfvars.read_text().splitlines()) == 3


def test_tfvars_from_env_creates_a_private_file(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path / ".env.local")
    tfvars = tmp_path / "terraform.tfvars"
    env = _base_env(HARMONY_ENV_FILE=str(env_file), HARMONY_TFVARS_FILE=str(tfvars))

    result = _run(SCRIPTS / "tfvars_from_env.sh", env=env)

    assert result.returncode == 0, result.stderr
    assert _mode(tfvars) == 0o600
    assert "render_owner_id" in tfvars.read_text()


# --- ping_prod_key.sh -----------------------------------------------------------------

PROD_KEY = 'sk-ant-fixture-"quoted"-prod-0123'
PROD_KEY_PLAIN = "sk-ant-fixture-plain-prod-0123"
HARMONY_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$STUB_DIR/argv.log"
{
  printf 'key=%s\\n' "${ANTHROPIC_API_KEY-<unset>}"
  printf 'app_env=%s override=%s project=%s mode=%s\\n' \\
    "$APP_ENV" "$MODEL_OVERRIDE" "$SPEND_PROJECT" "${MODEL_MODE-<unset>}"
} >>"$STUB_DIR/env.log"
"""


def _ping(
    tmp_path: Path, tfvars_text: str, *args: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stubs = tmp_path / "bin"
    _stub(stubs, "harmony", HARMONY_STUB)
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text(tfvars_text)
    env = _base_env(
        HARMONY_TFVARS_FILE=str(tfvars),
        STUB_DIR=str(stubs),
        PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
        # Should the stub ever be bypassed, the real client fails locally, unpaid.
        ANTHROPIC_BASE_URL="http://127.0.0.1:9",
    )
    env.pop("MODEL_MODE", None)
    assert shutil.which("harmony", path=env["PATH"]) == str(stubs / "harmony")
    return _run(SCRIPTS / "ping_prod_key.sh", *args, env=env), stubs


def test_ping_prod_key_hands_the_key_over_in_the_environment_only(tmp_path: Path) -> None:
    escaped = PROD_KEY.replace('"', '\\"')
    result, stubs = _ping(
        tmp_path,
        'access_code = "not this one"\n'
        f'anthropic_api_key = "{escaped}"  # the production workspace\n',
    )

    assert result.returncode == 0, result.stderr
    assert PROD_KEY not in result.stdout + result.stderr
    assert "sk-ant" not in result.stdout + result.stderr
    argv = (stubs / "argv.log").read_text()
    assert argv.splitlines() == [
        "llm ping --live --model haiku-featured",
        "llm ping --live --model haiku",
    ]
    assert PROD_KEY not in argv
    assert (stubs / "env.log").read_text().splitlines() == [
        f"key={PROD_KEY}",
        "app_env=local override=none project=harmony-hosted mode=<unset>",
    ] * 2


def test_ping_prod_key_pings_the_featured_turn_on_opus_for_the_production_mix(
    tmp_path: Path,
) -> None:
    result, stubs = _ping(tmp_path, f'anthropic_api_key = "{PROD_KEY_PLAIN}"\n', "--production-mix")

    assert result.returncode == 0, result.stderr
    assert (stubs / "argv.log").read_text().splitlines() == [
        "llm ping --live --model opus",
        "llm ping --live --model haiku",
    ]


def test_ping_prod_key_refuses_an_unknown_argument(tmp_path: Path) -> None:
    result, stubs = _ping(tmp_path, f'anthropic_api_key = "{PROD_KEY_PLAIN}"\n', "--opus")

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not (stubs / "argv.log").exists()


@pytest.mark.parametrize(
    ("tfvars_text", "message"),
    [
        ('access_code = "x"\n', "anthropic_api_key is not set"),
        ('anthropic_api_key = "rnd_wrong-kind-of-key"\n', "is not an Anthropic key"),
    ],
)
def test_ping_prod_key_refuses_without_a_key(
    tmp_path: Path, tfvars_text: str, message: str
) -> None:
    result, stubs = _ping(tmp_path, tfvars_text)

    assert result.returncode == 2
    assert message in result.stderr
    assert "rnd_wrong" not in result.stdout + result.stderr
    assert not (stubs / "argv.log").exists()


# --- hosted_admin.sh ------------------------------------------------------------------

ADMIN_SECRET = "fixture-admin-secret-0123456789abcdef"
# Plays curl against the admin routes: writes the body to -o, prints the status for -w.
CURL_STUB = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
opts = {args[i]: args[i + 1] for i in range(len(args) - 1) if args[i] in ("-o", "-X", "--data")}
header_files = [args[i + 1][1:] for i in range(len(args) - 1)
                if args[i] == "-H" and args[i + 1].startswith("@")]
headers = "".join(Path(name).read_text() for name in header_files)
stub = Path(os.environ["STUB_DIR"])
state = stub / "paused"
path = args[-1].split("/api", 1)[1]
if path == "/admin/live":
    state.write_text(json.dumps(not json.loads(opts["--data"])["enabled"]))
paused = json.loads(state.read_text()) if state.exists() else False
live = {"paused": paused, "provider_blocked_until": None, "global": None}
bodies = {
    "/admin/live": live,
    "/admin/spend": {"live": live, "today": None, "lineages_today": 0,
                     "top_lineages": [], "top_clients": []},
    "/admin/wipe": {"retired_world_ids": ["a", "b"]},
}
with (stub / "curl.log").open("a") as log:
    log.write(json.dumps({
        "argv": args,
        "header_files": header_files,
        "admin_header": headers == "X-Admin-Secret: " + os.environ["EXPECTED_SECRET"] + "\\n",
    }) + "\\n")
Path(opts["-o"]).write_text(json.dumps(bodies[path]))
print(200, end="")
"""


class CurlCall(TypedDict):
    argv: list[str]
    header_files: list[str]
    admin_header: bool


TERRAFORM_STUB = """#!/usr/bin/env bash
case "$*" in
  *"output -raw web_url") printf 'https://harmony.example.test' ;;
  *"output -raw admin_secret") printf '%s' "$EXPECTED_SECRET" ;;
  *) exit 1 ;;
esac
"""


def _admin(
    tmp_path: Path, *args: str, from_terraform: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[CurlCall]]:
    stubs = tmp_path / "bin"
    _stub(stubs, "curl", CURL_STUB)
    _copy_into(tmp_path / "repo", "hosted_admin.sh", "tf.sh")
    env = _base_env(
        STUB_DIR=str(stubs),
        EXPECTED_SECRET=ADMIN_SECRET,
        PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
        HARMONY_ENV_FILE=str(tmp_path / "no.env"),
    )
    if from_terraform:
        env["TERRAFORM"] = str(_stub(tmp_path / "tf-bin", "terraform", TERRAFORM_STUB))
    else:
        env |= {"HARMONY_BASE_URL": "http://admin.test/", "HARMONY_ADMIN_SECRET": ADMIN_SECRET}
    result = _run(tmp_path / "repo" / "scripts" / "hosted_admin.sh", *args, env=env)
    log = stubs / "curl.log"
    calls: list[CurlCall] = (
        [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    )
    return result, calls


def _assert_secret_kept_off_argv(
    result: subprocess.CompletedProcess[str], calls: list[CurlCall]
) -> None:
    assert calls
    for call in calls:
        assert not any(ADMIN_SECRET in arg for arg in call["argv"])
        assert call["admin_header"] is True
        for name in call["header_files"]:
            # The private temporary directory is gone once the script exits.
            assert not Path(name).parent.exists()
    assert ADMIN_SECRET not in result.stdout + result.stderr


def test_hosted_admin_pause_sends_the_secret_from_a_file_it_then_deletes(tmp_path: Path) -> None:
    result, calls = _admin(tmp_path, "pause")

    assert result.returncode == 0, result.stderr
    assert "live models are paused at http://admin.test" in result.stdout
    assert [(c["argv"][c["argv"].index("-X") + 1], c["argv"][-1]) for c in calls] == [
        ("POST", "http://admin.test/api/admin/live"),
        ("GET", "http://admin.test/api/admin/spend"),
    ]
    _assert_secret_kept_off_argv(result, calls)


def test_hosted_admin_reads_terraform_outputs_in_process(tmp_path: Path) -> None:
    result, calls = _admin(tmp_path, "status", from_terraform=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["live"]["paused"] is False
    assert calls[0]["argv"][-1] == "https://harmony.example.test/api/admin/spend"
    _assert_secret_kept_off_argv(result, calls)


def test_hosted_admin_wipe_needs_yes(tmp_path: Path) -> None:
    refused, calls = _admin(tmp_path, "wipe")
    assert refused.returncode == 2
    assert not calls

    wiped, calls = _admin(tmp_path, "wipe", "--yes")
    assert wiped.returncode == 0, wiped.stderr
    assert "retired 2 worlds" in wiped.stdout
    _assert_secret_kept_off_argv(wiped, calls)


# --- tf.sh and doctor.sh: state files stay private ------------------------------------

TERRAFORM_STATE_STUB = """#!/usr/bin/env bash
umask >"$STUB_DIR/umask"
dir=${1#-chdir=}
: >"$dir/terraform.tfstate.backup"
exit 3
"""


def test_tf_sh_runs_terraform_under_umask_077_and_locks_down_state(tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    _copy_into(tree, "tf.sh")
    state = tree / "infra" / "terraform" / "terraform.tfstate"
    state.write_text("{}")
    state.chmod(0o644)
    stubs = tmp_path / "bin"
    terraform = _stub(stubs, "terraform", TERRAFORM_STATE_STUB)
    env = _base_env(
        TERRAFORM=str(terraform), STUB_DIR=str(stubs), HARMONY_ENV_FILE=str(tmp_path / "no.env")
    )

    result = _run(tree / "scripts" / "tf.sh", "validate", env=env)

    assert result.returncode == 3  # Terraform's own status comes through
    assert (stubs / "umask").read_text().strip() == "0077"
    assert _mode(state) == 0o600
    assert _mode(tree / "infra" / "terraform" / "terraform.tfstate.backup") == 0o600


def test_doctor_flags_state_open_to_others(tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    _copy_into(tree, "doctor.sh")
    terraform_dir = tree / "infra" / "terraform"
    (terraform_dir / "terraform.tfstate").write_text("{}")
    (terraform_dir / "terraform.tfstate").chmod(0o644)
    (terraform_dir / "terraform.tfstate.backup").write_text("{}")
    (terraform_dir / "terraform.tfstate.backup").chmod(0o600)
    # Only the base system: the tool checks fail fast and nothing outside `tree` runs.
    env = _base_env(PATH="/usr/bin:/bin")

    result = _run(tree / "scripts" / "doctor.sh", env=env)

    assert result.returncode == 1
    rows = {line.split()[0]: line for line in result.stdout.splitlines() if "tfstate" in line}
    assert "mode 644, open to group or others" in rows["terraform.tfstate"]
    assert rows["terraform.tfstate.backup"].split(None, 1)[1] == "mode 600"


# --- secret_scan.sh: deploy runbook step 2 --------------------------------------------

# The repo's own `sk-ant-` strings: fixtures, prefix checks and the docs naming them.
SK_ANT_FILES = (
    "docs/LIVE_DEMO_PLAN.md",
    "infra/terraform/tests/hosted.tftest.hcl",
    "infra/terraform/variables.tf",
    "scripts/ping_prod_key.sh",
    "scripts/secret_scan.sh",
    "tests/unit/test_deploy_scripts.py",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)


def _scan(repo: Path) -> subprocess.CompletedProcess[str]:
    return _run(SCRIPTS / "secret_scan.sh", env=_base_env(), cwd=repo)


def test_secret_scan_passes_the_repos_fixtures_and_prefix_checks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name in SK_ANT_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "sk-ant-" in text, name
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")

    result = _scan(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no key-shaped string" in result.stdout


def test_secret_scan_counts_a_key_by_file_and_never_prints_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # Built here, so this file holds no key-shaped string of its own.
    key = "sk-ant-" + "api03-" + "Ab3_x-9Z" * 12
    (repo / "notes.md").write_text(f"pasted by mistake: {key}\nsk-ant-test-only\n")
    (repo / "infra").mkdir()
    (repo / "infra" / "main.tf").write_text(f'key = "{key}"\nother = "{key}"\n')
    _git(repo, "add", ".")

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "secret_scan.sh: infra/main.tf: 2 added line(s) hold a key-shaped string",
        "secret_scan.sh: notes.md: 1 added line(s) hold a key-shaped string",
    ]
    assert "sk-ant" not in result.stdout + result.stderr


def test_secret_scan_ignores_a_key_being_removed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    key = "sk-ant-" + "api03-" + "Ab3_x-9Z" * 12
    (repo / "notes.md").write_text(f"{key}\n")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "k",
    )
    (repo / "notes.md").write_text("gone\n")
    _git(repo, "add", ".")

    result = _scan(repo)

    assert result.returncode == 0, result.stdout
