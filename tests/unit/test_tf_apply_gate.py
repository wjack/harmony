"""tf.sh applies only main's Terraform (deploy runbook step 9 and Rotation): a working
tree whose infra/terraform differs from origin/main is refused before Terraform runs,
`apply <plan>` takes only a plan tf.sh saved from origin/main's current Terraform, and
a saved plan, which holds every secret in plaintext, is kept at mode 600.

The script runs for real in a throwaway git repository with a stub `terraform`:
nothing here reaches Render or Temporal Cloud.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[2] / "scripts"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)

TERRAFORM_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" >"$STUB_DIR/args"
dir=${1#-chdir=}
: >"$dir/deploy.tfplan"
chmod 644 "$dir/deploy.tfplan"
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip


def _repo(tmp_path: Path) -> Path:
    """A repo whose main, as last fetched, holds the Terraform now on disk."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "tf.sh", repo / "scripts" / "tf.sh")
    terraform_dir = repo / "infra" / "terraform"
    terraform_dir.mkdir(parents=True)
    (terraform_dir / "main.tf").write_text('locals {\n  a = "main"\n}\n')
    (repo / ".gitignore").write_text("*.tfvars\n*.tfplan\n*.tfstate\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _tf(repo: Path, tmp_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    stub = stubs / "terraform"
    stub.write_text(TERRAFORM_STUB)
    stub.chmod(0o755)
    (stubs / "args").unlink(missing_ok=True)
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
    env |= {
        "TERRAFORM": str(stub),
        "STUB_DIR": str(stubs),
        "HARMONY_ENV_FILE": str(tmp_path / "no.env"),
        "HARMONY_TF_ALLOW_APPLY": "1",
        **extra,
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "tf.sh"), *args],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def _plan(repo: Path, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return _tf(repo, tmp_path, "plan", "-out=deploy.tfplan", **extra)


def _apply(repo: Path, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return _tf(repo, tmp_path, "apply", "deploy.tfplan", **extra)


def _ran(tmp_path: Path) -> bool:
    return (tmp_path / "bin" / "args").exists()


def test_apply_runs_main_s_terraform_and_keeps_the_saved_plan_private(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Ignored files are the user's own: they never count as a difference.
    (repo / "infra" / "terraform" / "terraform.tfvars").write_text('access_code = "x"\n')

    planned = _plan(repo, tmp_path)
    plan = repo / "infra" / "terraform" / "deploy.tfplan"
    assert planned.returncode == 0, planned.stderr
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600

    result = _apply(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bin" / "args").read_text().splitlines()[1:] == ["apply", "deploy.tfplan"]
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600


@pytest.mark.parametrize("change", ["edited", "untracked", "committed elsewhere"])
def test_plan_and_apply_refuse_terraform_that_main_does_not_have(
    tmp_path: Path, change: str
) -> None:
    repo = _repo(tmp_path)
    terraform_dir = repo / "infra" / "terraform"
    if change == "untracked":
        (terraform_dir / "extra.tf").write_text("locals {}\n")
    else:
        (terraform_dir / "main.tf").write_text('locals {\n  a = "branch"\n}\n')
        if change == "committed elsewhere":
            _git(repo, "commit", "-qam", "branch")

    for result in (_plan(repo, tmp_path), _apply(repo, tmp_path)):
        assert result.returncode == 2
        assert "infra/terraform differs from origin/main" in result.stderr
        assert not _ran(tmp_path)  # Terraform never ran

    assert _plan(repo, tmp_path, HARMONY_TF_ALLOW_UNMERGED="1").returncode == 0
    assert _apply(repo, tmp_path, HARMONY_TF_ALLOW_UNMERGED="1").returncode == 0


def test_a_plan_saved_on_a_branch_is_refused_once_main_is_checked_out(tmp_path: Path) -> None:
    """Terraform applies the configuration inside the plan, not the files on disk."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-qb", "branch")
    (repo / "infra" / "terraform" / "main.tf").write_text('locals {\n  a = "branch"\n}\n')
    _git(repo, "commit", "-qam", "branch")
    assert _plan(repo, tmp_path).returncode == 2
    # Saved some other way: Terraform itself, or tf.sh before this check.
    _tf(repo, tmp_path, "plan", "-out=deploy.tfplan", HARMONY_TF_ALLOW_UNMERGED="1")
    _git(repo, "checkout", "-q", "-")

    result = _apply(repo, tmp_path)

    assert result.returncode == 2
    assert "didn't save it from" in result.stderr
    assert not _ran(tmp_path)


@pytest.mark.parametrize("change", ["main moved on", "plan replaced", "plan applied"])
def test_apply_refuses_a_plan_that_is_not_main_s_latest(tmp_path: Path, change: str) -> None:
    repo = _repo(tmp_path)
    assert _plan(repo, tmp_path).returncode == 0
    if change == "main moved on":
        (repo / "infra" / "terraform" / "main.tf").write_text('locals {\n  a = "next"\n}\n')
        _git(repo, "commit", "-qam", "next")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    elif change == "plan replaced":
        (repo / "infra" / "terraform" / "deploy.tfplan").write_bytes(b"another plan")
    else:
        assert _apply(repo, tmp_path).returncode == 0

    result = _apply(repo, tmp_path)

    assert result.returncode == 2
    assert "Plan again: scripts/tf.sh plan -out=deploy.tfplan" in result.stderr
    assert not _ran(tmp_path)


def test_apply_refuses_without_a_fetched_main(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    result = _apply(repo, tmp_path)

    assert result.returncode == 2
    assert "no origin/main" in result.stderr
    assert not _ran(tmp_path)
