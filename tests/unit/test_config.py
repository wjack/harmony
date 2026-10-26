from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from harmony.config import Settings
from tests.conftest import settings_without_dotenv

_ENV_KEYS = (
    "APP_ENV",
    "MODEL_OVERRIDE",
    "ALERT_THRESHOLDS",
    "SESSION_SECRET",
    "SPEND_LEDGER_PATH",
    "BUILD_SHA",
    "RENDER_GIT_COMMIT",
    "DATABASE_URL",
    "DATABASE_DIRECT_URL",
    "ADMIN_SECRET",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make(**values: Any) -> Settings:
    return settings_without_dotenv(**values)


def test_alert_thresholds_parse_from_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_THRESHOLDS", "100, 50,200")
    assert make().alert_thresholds == (Decimal(50), Decimal(100), Decimal(200))


def test_model_override_defaults_to_haiku_only_locally() -> None:
    assert make(app_env="local").model_override == "claude-haiku-5-5"
    assert make(app_env="ci").model_override is None


@pytest.mark.parametrize("value", ["none", "None", ""])
def test_model_override_can_be_disabled_locally(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MODEL_OVERRIDE", value)
    assert make(app_env="local").model_override is None


def test_database_urls_are_normalized_per_driver() -> None:
    settings = make(database_url=SecretStr("postgres://u:p%40ss@db:5432/app"))
    assert settings.sqlalchemy_url() == "postgresql+asyncpg://u:p%40ss@db:5432/app"
    assert settings.asyncpg_dsn() == "postgresql://u:p%40ss@db:5432/app"


def test_direct_url_falls_back_to_database_url_and_wins_when_set() -> None:
    pooled = SecretStr("postgresql://u:p@pooler:6432/app")
    assert make(database_url=pooled).asyncpg_dsn() == "postgresql://u:p@pooler:6432/app"
    settings = make(
        database_url=pooled, database_direct_url=SecretStr("postgresql://u:p@db:5432/app")
    )
    assert settings.asyncpg_dsn() == "postgresql://u:p@db:5432/app"
    assert settings.sqlalchemy_url() == "postgresql+asyncpg://u:p@pooler:6432/app"
    assert settings.sqlalchemy_url(direct=True) == "postgresql+asyncpg://u:p@db:5432/app"


def test_prod_requires_a_strong_session_secret() -> None:
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        make(app_env="prod")
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        make(app_env="prod", session_secret=SecretStr("short"))
    assert make(app_env="prod", session_secret=SecretStr("x" * 32)).app_env == "prod"


def test_local_has_a_dev_session_secret_when_unset() -> None:
    assert make(app_env="local").session_secret_value()


def test_blank_secrets_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_SECRET", "")
    monkeypatch.setenv("SESSION_SECRET", "  ")
    settings = make()
    assert settings.admin_secret is None
    assert settings.session_secret is None


def test_spend_ledger_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        make(spend_ledger_path=Path("spend_ledger.jsonl"))


def test_build_sha_comes_from_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    assert make().build_sha == "abc123"
    monkeypatch.setenv("BUILD_SHA", "def456")
    assert make().build_sha == "def456"


def test_tests_never_see_a_real_anthropic_key() -> None:
    assert Settings().anthropic_api_key is None
    assert Settings().model_mode == "scripted"
