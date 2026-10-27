"""secret_scan.sh also counts the provider credentials .env.local and terraform.tfvars
hold (deploy runbook steps 2 and 3): a Render API key and a signed JWT, beside the
Anthropic key test_deploy_scripts.py covers. The shapes are built by concatenation, so
this file holds none of them itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "secret_scan.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)

RENDER_KEY = "rnd_" + "Q7xK2mZp" * 4
JWT = "eyJ" + "hbGciOiJFUzI1NiJ9" + ".eyJ" + "zdWIiOiJoYXJtb255In0" + "." + "s1Gn4tUr3_-x" * 3


def _scan(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env, cwd=repo, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.mark.parametrize("secret", [RENDER_KEY, JWT], ids=["render", "jwt"])
def test_secret_scan_counts_provider_credentials_and_never_prints_them(
    tmp_path: Path, secret: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "infra.tf").write_text(f'api_key = "{secret}"\n')
    _git(repo, "add", ".")

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "secret_scan.sh: infra.tf: 1 added line(s) hold a key-shaped string"
    ]
    assert secret not in result.stdout + result.stderr


def test_secret_scan_passes_the_render_fixture(tmp_path: Path) -> None:
    """test_deploy_scripts.py's stand-in Render key isn't key-shaped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "fixture.py").write_text('RENDER_KEY = "rnd_fixture-render-key-0001"\n')
    _git(repo, "add", ".")

    assert _scan(repo).returncode == 0
