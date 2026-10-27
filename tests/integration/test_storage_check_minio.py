"""`harmony storage check` against the Compose MinIO, as the runbook runs it on Render.

Opt-in, like test_storage_minio.py:
    docker compose --profile storage up -d --build minio
    uv run pytest -m minio tests/integration/test_storage_check_minio.py
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from harmony import cli_storage
from tests.conftest import settings_without_dotenv
from tests.integration.test_storage_minio import ACCESS_KEY, BUCKET, ENDPOINT, SECRET_KEY

pytestmark = pytest.mark.minio


def test_the_check_passes_against_minio(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        httpx.get(f"{ENDPOINT}/minio/health/live", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(
            f"MinIO is not up at {ENDPOINT}: docker compose --profile storage up -d --build minio"
        )
    settings = settings_without_dotenv(
        object_store="s3",
        s3_endpoint_url=ENDPOINT,
        s3_bucket=BUCKET,
        s3_region="us-east-1",
        s3_access_key_id=ACCESS_KEY,
        s3_secret_access_key=SECRET_KEY,
    )
    monkeypatch.setattr(cli_storage, "get_settings", lambda: settings)

    result = CliRunner().invoke(cli_storage.app, ["check"])

    assert result.exit_code == 0, result.output
    assert result.output.count("PASS") == 3, result.output
