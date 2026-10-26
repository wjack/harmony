"""Process settings, read from the environment and the repo's `.env.local`.

Environment variables win over `.env.local`; the file is optional (Render and CI set
real environment variables). Never log a `Settings` object's secret fields: they are
`SecretStr` so an accidental repr prints `**********`.

Connection budget (ARCHITECTURE.md §8). Render's basic_1gb Postgres allows 100
connections. Per process we open `db_pool_size + db_max_overflow` pooled connections
plus one dedicated connection outside the pool (the LISTEN connection in the API, the
stream-notify connection in the worker):

    web    : 8 + 4 + 1 LISTEN  = 13
    worker : 8 + 4 + 1 notify  = 13
    deploy overlap (old + new instances of both)    x2 = 52
    pre-deploy migrations (alembic, web and worker)      2
                                                        --
                                                        54, leaving headroom
                                                        under 100 for psql and
                                                        Render's own connections.

Raise the pool only together with this table.
"""

from __future__ import annotations

import subprocess
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env.local"

AppEnv = Literal["local", "ci", "prod"]
ModelMode = Literal["scripted", "live"]
ObjectStoreKind = Literal["localfs", "s3"]

LOCAL_DATABASE_URL = "postgresql://harmony:harmony@localhost:5432/harmony"
LOCAL_MODEL_OVERRIDE = "claude-haiku-5-5"
# Used only when APP_ENV is local or ci and SESSION_SECRET is unset; prod refuses it.
_INSECURE_DEV_SESSION_SECRET = "harmony-insecure-dev-session-secret"
_MIN_PROD_SECRET_LEN = 32


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _empty_to_none(value: object) -> object:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
        validate_by_name=True,
        validate_by_alias=True,
    )

    # --- Database -------------------------------------------------------------------
    # Plain `postgresql://` URLs, as Render provides them; use `sqlalchemy_url()` or
    # `asyncpg_dsn()` to get a driver-specific form. DATABASE_DIRECT_URL bypasses any
    # transaction-mode pooler: LISTEN and migrations must use it. Unset means "same as
    # DATABASE_URL".
    database_url: SecretStr = SecretStr(LOCAL_DATABASE_URL)
    database_direct_url: SecretStr | None = None
    db_pool_size: int = Field(default=8, ge=1)
    db_max_overflow: int = Field(default=4, ge=0)

    # --- Temporal -------------------------------------------------------------------
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Set on Temporal Cloud only; its presence switches the client to TLS + API key.
    temporal_api_key: SecretStr | None = None

    # --- Model ----------------------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    model_mode: ModelMode = "scripted"
    # Every role runs on this model when set. Defaults to Haiku 5.5 when APP_ENV=local;
    # set MODEL_OVERRIDE=none to run the production model mix locally.
    model_override: str | None = None
    # ScriptedModel's cassettes (MODEL_MODE=scripted): a directory of YAML files. The
    # image ships this directory (the only part of tests/ in it); a scripted call
    # without a cassette entry fails as a 400.
    cassette_dir: Path = REPO_ROOT / "tests" / "fixtures" / "cassettes"

    # --- Spend ----------------------------------------------------------------------
    # Absolute path to the shared dev ledger. Required for live calls when local; the
    # budget guard refuses to start (and never creates the file) when it is missing.
    spend_ledger_path: Path | None = None
    spend_project: str = "harmony"
    dev_budget_usd: Decimal = Decimal(200)
    alert_thresholds: Annotated[tuple[Decimal, ...], NoDecode] = (
        Decimal(50),
        Decimal(100),
        Decimal(200),
    )
    # Hosted demo caps (§14), in the `budgets` table; they apply only to live calls
    # with APP_ENV=prod (`budget_pg.HostedCaps`). Per session lineage, so Reset never
    # refills it; the daily cap is still open, and unset means none.
    lineage_budget_usd: Decimal = Field(default=Decimal(6), gt=0)
    daily_budget_usd: Decimal | None = Field(default=None, gt=0)

    # --- App ------------------------------------------------------------------------
    app_env: AppEnv = "local"
    session_secret: SecretStr | None = None
    # Unlocks live (paid) runs in the hosted demo; unset means live runs are off.
    access_code: SecretStr | None = None
    # Guards POST /api/admin/wipe; unset means the endpoint always refuses.
    admin_secret: SecretStr | None = None
    build_sha: str = Field(
        default_factory=_git_head,
        validation_alias=AliasChoices("BUILD_SHA", "RENDER_GIT_COMMIT"),
    )
    scenario_anchor: date = date(2026, 10, 26)
    web_dist_dir: Path = REPO_ROOT / "web" / "dist"

    # --- Object storage -------------------------------------------------------------
    object_store: ObjectStoreKind = "localfs"
    localfs_root: Path = REPO_ROOT / ".data" / "objects"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "harmony"
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None

    @field_validator(
        "database_direct_url",
        "temporal_api_key",
        "anthropic_api_key",
        "spend_ledger_path",
        "daily_budget_usd",
        "session_secret",
        "access_code",
        "admin_secret",
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return _empty_to_none(value)

    @field_validator("model_override", mode="before")
    @classmethod
    def _parse_model_override(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in {"", "none"}:
            return None
        return value

    @field_validator("alert_thresholds", mode="before")
    @classmethod
    def _parse_thresholds(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(Decimal(part.strip()) for part in value.split(",") if part.strip())
        return value

    @field_validator("alert_thresholds")
    @classmethod
    def _sorted_thresholds(cls, value: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _apply_env_defaults(self) -> Settings:
        if "model_override" not in self.model_fields_set and self.app_env == "local":
            self.model_override = LOCAL_MODEL_OVERRIDE
        if self.app_env == "prod":
            secret = self.session_secret.get_secret_value() if self.session_secret else ""
            if len(secret) < _MIN_PROD_SECRET_LEN:
                raise ValueError(
                    f"SESSION_SECRET must be set to at least {_MIN_PROD_SECRET_LEN} "
                    "characters when APP_ENV=prod"
                )
        if self.spend_ledger_path is not None and not self.spend_ledger_path.is_absolute():
            raise ValueError("SPEND_LEDGER_PATH must be an absolute path")
        return self

    # --- Derived values -------------------------------------------------------------

    def session_secret_value(self) -> str:
        """The cookie-signing secret (a fixed dev value outside prod when unset)."""
        if self.session_secret is not None:
            return self.session_secret.get_secret_value()
        return _INSECURE_DEV_SESSION_SECRET

    def sqlalchemy_url(self, *, direct: bool = False) -> str:
        """The database URL with the asyncpg driver, for `create_async_engine`."""
        url = make_url(self._raw_database_url(direct=direct)).set(drivername="postgresql+asyncpg")
        return url.render_as_string(hide_password=False)

    def asyncpg_dsn(self, *, direct: bool = True) -> str:
        """A plain `postgresql://` DSN for `asyncpg.connect` (LISTEN/NOTIFY connections)."""
        url = make_url(self._raw_database_url(direct=direct)).set(drivername="postgresql")
        return url.render_as_string(hide_password=False)

    def _raw_database_url(self, *, direct: bool) -> str:
        if direct and self.database_direct_url is not None:
            return self.database_direct_url.get_secret_value()
        return self.database_url.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read once. Tests build `Settings(...)` directly."""
    return Settings()
