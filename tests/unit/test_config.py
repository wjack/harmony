from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from harmony.config import Settings
from tests.conftest import prod_settings, settings_without_dotenv

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
    "ACCESS_CODE",
    "DAILY_BUDGET_USD",
    "GLOBAL_BUDGET_USD",
    "CLIENT_DAILY_BUDGET_USD",
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
        prod_settings(session_secret=None)
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        prod_settings(session_secret=SecretStr("short"))
    assert prod_settings(session_secret=SecretStr("x" * 32)).app_env == "prod"


def test_prod_settings_load() -> None:
    settings = prod_settings()
    assert (settings.app_env, settings.model_mode, settings.model_override) == (
        "prod",
        "live",
        None,
    )
    assert settings.access_code is not None


@pytest.mark.parametrize(
    "code",
    [
        None,
        "x" * 15,
        " correct horse battery",
        "correct horse battery ",
        "correct horse batterý",
        "correct\thorse battery",
    ],
    ids=["missing", "15-chars", "leading-space", "trailing-space", "non-ascii", "tab"],
)
def test_prod_refuses_a_weak_or_malformed_access_code(code: str | None) -> None:
    with pytest.raises(ValidationError, match="ACCESS_CODE"):
        prod_settings(access_code=SecretStr(code) if code is not None else None)


def test_prod_accepts_a_sixteen_character_access_code() -> None:
    assert prod_settings(access_code=SecretStr("x" * 16)).access_code is not None


def test_prod_refuses_scripted_models() -> None:
    with pytest.raises(ValidationError, match="MODEL_MODE"):
        prod_settings(model_mode="scripted")


def test_prod_may_pin_every_role_to_a_known_model() -> None:
    assert prod_settings(model_override="claude-haiku-5-5").model_override == "claude-haiku-5-5"
    assert prod_settings(model_override="none").model_override is None


def test_worlds_pin_to_the_build_and_the_model_override() -> None:
    # A MODEL_OVERRIDE change ships without a new build SHA; it must still fork worlds.
    assert prod_settings(build_sha="abc123").world_build == "abc123"
    pinned = prod_settings(build_sha="abc123", model_override="claude-haiku-5-5")
    assert pinned.world_build == "abc123+claude-haiku-5-5"


def test_prod_refuses_an_unknown_model_override() -> None:
    with pytest.raises(ValidationError, match="MODEL_OVERRIDE"):
        prod_settings(model_override="claude-haiku-5")


def test_a_refusal_never_prints_a_secret_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # In prod every setting arrives as a raw string, and the S3 secret, the last field,
    # would fill the tail of a printed input dict in Render's deploy logs.
    s3_secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
    secrets = {
        "SESSION_SECRET": "s" * 40,
        "ACCESS_CODE": "correct horse battery",
        "ADMIN_SECRET": "fixture-admin-secret-0123456789ab",
        "DATABASE_URL": "postgresql://harmony:db-password-fixture@db.invalid/harmony",
        "S3_SECRET_ACCESS_KEY": s3_secret,
    }
    env = secrets | {
        "APP_ENV": "prod",
        "MODEL_MODE": "live",
        "MODEL_OVERRIDE": "claude-haiku-5",
        "DAILY_BUDGET_USD": "60",
        "GLOBAL_BUDGET_USD": "250",
        "CLIENT_DAILY_BUDGET_USD": "25",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as refused:
        make()
    text = str(refused.value)
    assert "MODEL_OVERRIDE must be unset or one of" in text
    for secret in secrets.values():
        assert not any(secret[i : i + 8] in text for i in range(len(secret) - 7)), secret


@pytest.mark.parametrize(
    ("field", "env_name"),
    [
        ("daily_budget_usd", "DAILY_BUDGET_USD"),
        ("global_budget_usd", "GLOBAL_BUDGET_USD"),
        ("client_daily_budget_usd", "CLIENT_DAILY_BUDGET_USD"),
    ],
)
def test_prod_refuses_a_missing_cap(field: str, env_name: str) -> None:
    with pytest.raises(ValidationError, match=env_name):
        prod_settings(**{field: None})


@pytest.mark.parametrize("app_env", ["local", "ci"])
def test_local_and_ci_defaults_are_unchanged(app_env: str) -> None:
    settings = make(app_env=app_env)
    assert settings.access_code is None
    assert settings.model_mode == "scripted"
    assert settings.model_override == ("claude-haiku-5-5" if app_env == "local" else None)
    assert (settings.daily_budget_usd, settings.global_budget_usd) == (None, None)
    assert settings.client_daily_budget_usd is None
    assert settings.lineage_budget_usd == Decimal(10)
    assert settings.client_ip_header == "x-forwarded-for"
    assert not settings.hosted_caps_in_dev
    assert (
        settings.worker_model_concurrency,
        settings.worker_model_rate,
        settings.worker_chat_concurrency,
    ) == (4, 2.0, 2)


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
