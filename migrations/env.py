"""Alembic environment: async, against harmony's metadata.

The URL comes from `config.attributes["database_url"]` when a caller (the test
fixtures) sets one, else from settings: DATABASE_DIRECT_URL, because DDL must not go
through a transaction-mode pooler.

Online runs use `asyncio.run`, so call Alembic's command API from a thread without a
running event loop (see `tests/conftest.py`).

Online runs hold a Postgres advisory lock while they migrate: on Render the web and
worker services both run `alembic upgrade head` before deploying, independently, and
the second must wait for the first and then find nothing left to do.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from harmony.config import get_settings
from harmony.db.models import Base

config = context.config
target_metadata = Base.metadata

# pg_advisory_lock key for migration runs ("harmony" in ASCII).
MIGRATION_LOCK_KEY = 0x6861726D6F6E79

# The CLI gets alembic.ini's logging; callers that own logging (pytest) opt out.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    url: object = config.attributes.get("database_url")
    if isinstance(url, str):
        return url
    return get_settings().sqlalchemy_url(direct=True)


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        literal_binds=connection is None,
        transaction_per_migration=True,
    )


def _run_sync(connection: Connection) -> None:
    # Session-level: held across the per-migration transactions, released below (or
    # when the connection closes, if a migration fails).
    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
    connection.commit()
    try:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.rollback()
        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
        connection.commit()


async def _run_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    _configure(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
