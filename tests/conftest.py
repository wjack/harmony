"""Shared fixtures.

Databases: one migrated template per session, and a fresh `CREATE DATABASE … TEMPLATE`
clone per test, on the server in DATABASE_DIRECT_URL (`make up`). Set
HARMONY_TEST_DB_PREFIX (for example to your task id) when several pytest runs share
one Postgres, so their template names never collide.

Temporal: `temporal_env` is a local dev server with the WorldId/OrgId search
attributes (list queries work); `temporal_env_time_skipping` is the
time-skipping test server, which has no List API. Neither touches Compose's Temporal.
Both record the workflows their client starts, and save their histories when
HARMONY_SAVE_HISTORIES is set (`tests/_histories.py`).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

# Guards, set before anything imports harmony.config: no test may reach the Anthropic
# API or write the real spend ledger. The discard port makes an accidental client call
# fail immediately and locally.
os.environ["MODEL_MODE"] = "scripted"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
os.environ["SPEND_LEDGER_PATH"] = str(
    Path(tempfile.mkdtemp(prefix="harmony-test-")) / "spend_ledger.jsonl"
)
# Typer forces Rich's terminal styling whenever GITHUB_ACTIONS (or FORCE_COLOR) is set,
# which splits option names in --help output with ANSI codes; the tests read plain text.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from harmony.config import REPO_ROOT, Settings, get_settings
from harmony.db.engine import create_engine, create_sessionmaker
from harmony.temporal import search_attributes
from tests._histories import StartedWorkflows


def settings_without_dotenv(**values: Any) -> Settings:
    """Settings from the process environment and `values` only, ignoring .env.local."""
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def prod_settings(**overrides: Any) -> Settings:
    """APP_ENV=prod settings with every field prod requires (`Settings._check_prod`)."""
    values: dict[str, Any] = {
        "app_env": "prod",
        "model_mode": "live",
        "model_override": None,
        "session_secret": SecretStr("s" * 40),
        "access_code": SecretStr("correct horse battery"),
        "daily_budget_usd": Decimal(60),
        "global_budget_usd": Decimal(250),
        "client_daily_budget_usd": Decimal(25),
    }
    return settings_without_dotenv(**(values | overrides))


@dataclass(frozen=True, slots=True)
class ClonedDatabase:
    name: str
    url: str  # postgresql+asyncpg://, for SQLAlchemy
    dsn: str  # postgresql://, for asyncpg.connect (LISTEN/NOTIFY)


def _db_prefix() -> str:
    raw = os.environ.get("HARMONY_TEST_DB_PREFIX", "local")
    return "harmony_test_" + re.sub(r"[^a-z0-9_]", "_", raw.lower())[:24]


def _server_url() -> str:
    return get_settings().sqlalchemy_url(direct=True)


def _url_for(database: str, *, driver: str) -> str:
    url = make_url(_server_url()).set(drivername=driver, database=database)
    return url.render_as_string(hide_password=False)


def _clone(name: str) -> ClonedDatabase:
    return ClonedDatabase(
        name=name,
        url=_url_for(name, driver="postgresql+asyncpg"),
        dsn=_url_for(name, driver="postgresql"),
    )


async def _admin(*statements: str) -> None:
    conn = await asyncpg.connect(_url_for("postgres", driver="postgresql"))
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


def _in_fresh_thread[T](fn: Callable[[], T]) -> T:
    # Alembic's env.py calls asyncio.run, which needs a thread with no running loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def _drop(name: str) -> str:
    return f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'


@pytest.fixture(scope="session")
def template_database() -> Iterator[str]:
    """A database migrated to head, used only as a CREATE DATABASE template."""
    name = f"{_db_prefix()}_tpl"

    def build() -> None:
        asyncio.run(_admin(_drop(name), f'CREATE DATABASE "{name}"'))
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.attributes["database_url"] = _url_for(name, driver="postgresql+asyncpg")
        config.attributes["configure_logging"] = False
        command.upgrade(config, "head")
        # Cloning fails while anyone is connected to the template; forbid connections.
        asyncio.run(_admin(f'ALTER DATABASE "{name}" WITH ALLOW_CONNECTIONS false'))

    _in_fresh_thread(build)
    yield name
    _in_fresh_thread(lambda: asyncio.run(_admin(_drop(name))))


@pytest.fixture
async def database(template_database: str) -> AsyncIterator[ClonedDatabase]:
    """A fresh, migrated database for this test, dropped afterwards."""
    clone = _clone(f"{_db_prefix()}_{uuid4().hex[:12]}")
    await _admin(f'CREATE DATABASE "{clone.name}" TEMPLATE "{template_database}"')
    try:
        yield clone
    finally:
        await _admin(_drop(clone.name))


@pytest.fixture
async def db_engine(database: ClonedDatabase) -> AsyncIterator[AsyncEngine]:
    """An engine on this test's database, with room for concurrency tests."""
    engine = create_engine(
        database.url, pool_size=5, max_overflow=25, application_name="harmony-test"
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def db_sessionmaker(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(db_engine)


@pytest.fixture
def app_settings(database: ClonedDatabase) -> Settings:
    """Settings pointed at this test's database, for `create_app(settings, engine=...)`.

    The gate stays off (no `ACCESS_CODE`, whatever .env.local says); a test that
    exercises it sets one on its own copy."""
    return get_settings().model_copy(
        update={
            "database_url": SecretStr(database.dsn),
            "database_direct_url": SecretStr(database.dsn),
            "access_code": None,
        }
    )


def _temporal_cli() -> str:
    return shutil.which("temporal") or "/opt/homebrew/bin/temporal"


@pytest.fixture
async def temporal_env(request: pytest.FixtureRequest) -> AsyncIterator[WorkflowEnvironment]:
    """A local Temporal dev server (the installed CLI) with WorldId and OrgId."""
    started = StartedWorkflows()
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        interceptors=[started],
        search_attributes=search_attributes.ALL,
        dev_server_existing_path=_temporal_cli(),
        dev_server_log_level="error",
    )
    async with env:
        yield env
        await started.save(env.client, request.node.name)


@pytest.fixture
async def temporal_env_time_skipping(
    request: pytest.FixtureRequest,
) -> AsyncIterator[WorkflowEnvironment]:
    """The time-skipping test server. The SDK downloads its binary on first use."""
    started = StartedWorkflows()
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter, interceptors=[started]
    )
    async with env:
        yield env
        await started.save(env.client, request.node.name)
