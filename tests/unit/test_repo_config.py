"""Static checks on the repo's run and deploy configuration (not on Python code)."""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from honcho.environ import parse_procfile
from honcho.manager import KILL_WAIT
from pydantic import SecretStr

from harmony.config import Settings
from harmony.temporal.worker import scripted_model
from tests.conftest import prod_settings, settings_without_dotenv

ROOT = Path(__file__).parents[2]

# The time a cancelled model call needs to settle its ledger line and the worker to exit.
SETTLE_MARGIN_S = 2.0


def _flags(argv: list[str], *names: str) -> dict[str, float]:
    parser = argparse.ArgumentParser(add_help=False)
    for name in names:
        parser.add_argument(name, type=float)
    known, _ = parser.parse_known_args(argv)
    return {name: value for name, value in vars(known).items() if value is not None}


def test_the_dev_worker_settles_in_flight_calls_before_it_is_killed() -> None:
    """A reload (watchfiles: SIGINT, then SIGKILL after `--sigint-timeout`) or a stop
    (honcho: SIGTERM, then SIGKILL after KILL_WAIT) must leave the worker time to
    cancel its activities after its graceful shutdown, so a paid call in flight is
    settled into the ledger instead of vanishing with the process."""
    procfile = parse_procfile((ROOT / "Procfile.dev").read_text())
    argv = shlex.split(procfile.processes["worker"])
    assert argv[0] == "watchfiles"
    watch = _flags(argv[1:], "--sigint-timeout")
    target = next(arg for arg in argv[1:] if arg.startswith("harmony worker"))
    worker = _flags(shlex.split(target)[2:], "--graceful-shutdown")

    sigint_timeout = watch.get("sigint_timeout", 5.0)  # watchfiles' default
    graceful = worker.get("graceful_shutdown", 90.0)  # `harmony worker`'s default
    assert graceful + SETTLE_MARGIN_S <= min(sigint_timeout, KILL_WAIT)


def _published_ports(compose: dict[str, Any]) -> list[tuple[str, str]]:
    ports: list[tuple[str, str]] = []
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            if isinstance(port, dict):  # long syntax
                port = f"{port.get('host_ip', '0.0.0.0')}:{port.get('published', '')}"
            ports.append((name, str(port)))
    return ports


def test_dev_compose_publishes_every_port_on_loopback_only() -> None:
    """Temporal has no auth and the Postgres/MinIO passwords are in the repo: a port
    published on 0.0.0.0 (Compose's default) is open to the whole network."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    ports = _published_ports(compose)
    assert {name for name, _ in ports} >= {"postgres", "temporal", "minio"}
    for name, port in ports:
        assert port.startswith("127.0.0.1:"), f"{name} publishes {port!r} beyond loopback"


def _terraform_block(source: str, header: str) -> str:
    start = source.index(header)
    end = source.index("\n}\n", start)
    return source[start:end]


def test_every_render_service_carries_its_own_env() -> None:
    """Each service gets its env through `env_vars`; an env group would be one more
    resource whose link Render applies separately from the service's deploy."""
    source = (ROOT / "infra" / "terraform" / "modules" / "render" / "main.tf").read_text()
    assert "render_env_group" not in source
    for header in (
        'resource "render_web_service" "web"',
        'resource "render_background_worker" "worker"',
    ):
        assert re.search(r"^\s*env_vars\s*=", _terraform_block(source, header), re.M), header


def test_the_tfvars_example_holds_names_only() -> None:
    """terraform.tfvars.example is committed: every assignment in it is empty or
    commented out, so copying it can't carry a real value along."""
    example = (ROOT / "infra" / "terraform" / "terraform.tfvars.example").read_text()
    assignments = re.findall(r"^\s*(\w+)\s*=\s*(.*)$", example, re.M)
    assert assignments == [("anthropic_api_key", '""')]


def test_every_render_service_migrates_before_its_new_code_starts() -> None:
    """Render deploys web and the worker independently; a worker that started without
    migrating could run new activity code against the old schema."""
    source = (ROOT / "infra" / "terraform" / "modules" / "render" / "main.tf").read_text()
    for header in (
        'resource "render_web_service" "web"',
        'resource "render_background_worker" "worker"',
    ):
        block = _terraform_block(source, header)
        assert re.search(r'^\s*pre_deploy_command\s*=\s*"alembic upgrade head"$', block, re.M), (
            header
        )
    # ...and the two runs are serialized.
    assert "pg_advisory_lock" in (ROOT / "migrations" / "env.py").read_text()


SECRET_BEARING = [
    ".env.prod",  # scripts/tf.sh's HARMONY_ENV_FILE override
    ".env.production.local",
    "infra/terraform/tfplan",  # `scripts/tf.sh plan -out=tfplan`: variables and state
    "infra/terraform/prod.tfplan",
    "infra/terraform/terraform.tfvars.json",  # auto-loaded by Terraform
    "infra/terraform/secrets.auto.tfvars.json",
    "infra/terraform/terraform.tfvars",
    "infra/terraform/terraform.tfstate",
    "infra/terraform/crash.20261025.log",
    ".terraformrc",  # may hold registry credentials
]
SHAREABLE = [".env.local.example", "infra/terraform/terraform.tfvars.example"]


def _ignored(path: str) -> bool:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("needs git and the repo's checkout")
    result = subprocess.run(
        [git, "-C", str(ROOT), "check-ignore", "--no-index", "-q", path],
        check=False,
        capture_output=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def test_the_env_example_names_real_settings_with_valid_values() -> None:
    """.env.local is copied from the example, and Settings ignores a key it doesn't
    know, so a misspelt name there would silently do nothing."""
    example = ROOT / ".env.local.example"
    app_settings = example.read_text().split("# --- App settings", 1)[1]
    names = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", app_settings, re.M))
    known = {name.upper() for name in Settings.model_fields} | {"BUILD_SHA"}
    assert names
    assert names <= known, names - known
    Settings(_env_file=example)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("path", SECRET_BEARING)
def test_files_that_carry_production_secrets_are_ignored(path: str) -> None:
    assert _ignored(path), f"{path} could be committed"


@pytest.mark.parametrize("path", SHAREABLE)
def test_example_files_stay_committable(path: str) -> None:
    assert not _ignored(path)


def _in_docker_context(path: str) -> bool:
    """Whether .dockerignore lets `path` into the build context: the last pattern that
    matches the path or one of its parent directories decides (Docker's rule)."""
    included = True
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exception = line.startswith("!")
        pattern = line.removeprefix("!").strip("/")
        parts = path.split("/")
        candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            included = exception
    return included


def test_the_image_ships_the_cassettes_compose_smoke_plays() -> None:
    """CI's compose-smoke runs the image with MODEL_MODE=scripted, which reads
    CASSETTE_DIR, by default the repo's tests/fixtures/cassettes, so the image must carry
    them. (Production never plays them: prod refuses scripted mode.)"""
    cassettes = Settings.model_fields["cassette_dir"].default
    assert cassettes == ROOT / "tests" / "fixtures" / "cassettes"
    files = sorted(path.relative_to(ROOT).as_posix() for path in cassettes.glob("*.yaml"))
    assert files
    for file in files:
        assert _in_docker_context(file), f"{file} is not in the image's build context"
    assert not _in_docker_context("tests/unit/test_repo_config.py")
    assert not _in_docker_context("tests/fixtures/cassettes/.env.local")

    copies = [
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("COPY")
    ]
    assert any("./tests/fixtures/cassettes/" in line and "--parents" in line for line in copies)


def _cmd(dockerfile: str) -> list[str]:
    line = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    return shlex.split(line.removeprefix("CMD "))


def test_the_image_trusts_forwarded_headers_from_private_addresses_only() -> None:
    """With `--forwarded-allow-ips='*'` any client could set request.client, and with
    it the access log's address, by sending its own X-Forwarded-For."""
    argv = _cmd((ROOT / "Dockerfile").read_text())
    assert "--proxy-headers" in argv
    allowed = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--forwarded-allow-ips="))
    networks = [ipaddress.ip_network(entry) for entry in allowed.split(",")]
    assert networks
    assert all(network.is_private for network in networks), allowed


def test_the_image_runs_one_api_process() -> None:
    """The gate's limits live in process (one web process), and uvicorn forks
    $WEB_CONCURRENCY workers unless --workers is given."""
    argv = _cmd((ROOT / "Dockerfile").read_text())
    assert argv[argv.index("--workers") + 1] == "1"


def _compose_smoke_value(name: str) -> str:
    script = (ROOT / "scripts" / "compose_smoke.sh").read_text()
    match = re.search(rf"^export {name}=(\S+)$", script, re.M)
    assert match, f"compose_smoke.sh doesn't export {name}"
    return match[1]


def test_compose_smoke_unlocks_with_the_ci_stacks_passcode() -> None:
    """compose.ci.yaml sets the master passcode; compose_smoke.sh hands the same value
    to Playwright (E2E_PASSCODE) and to the shell and Python checks (HARMONY_PASSCODE)."""
    # A regex, not yaml: compose.ci.yaml uses Compose's own tags (!override, !reset).
    stack = (ROOT / "compose.ci.yaml").read_text()
    passcodes = re.findall(r"^\s+ACCESS_CODE:\s*(\S+)$", stack, re.M)
    assert len(passcodes) == 1, passcodes
    passcode = passcodes[0]
    assert _compose_smoke_value("E2E_PASSCODE") == passcode
    assert _compose_smoke_value("HARMONY_PASSCODE") == '"$E2E_PASSCODE"'
    # A passcode production would accept, so CI's gate runs as the hosted one does.
    prod_settings(access_code=SecretStr(passcode))


def test_a_deployed_scripted_worker_without_cassettes_refuses_to_start(tmp_path: Path) -> None:
    # Prod refuses MODEL_MODE=scripted outright (test_config), so CI is the deployed case.
    settings = settings_without_dotenv(
        app_env="ci", session_secret="x" * 40, cassette_dir=tmp_path / "missing"
    )
    with pytest.raises(RuntimeError, match="no cassettes"):
        scripted_model(settings)
    # Locally it only warns, so a fresh checkout's worker still starts.
    assert scripted_model(settings.model_copy(update={"app_env": "local"})).call_count == 0
