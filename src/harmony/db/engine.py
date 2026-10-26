"""The async SQLAlchemy engine and session factory.

One engine per process (API or worker), sized by the connection budget in
`harmony.config`. LISTEN/NOTIFY connections are separate asyncpg connections on
`Settings.asyncpg_dsn()` and never come from this pool.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harmony.config import Settings

# Recycle before typical managed-Postgres idle timeouts so a quiet process never
# hands out a connection the server already closed.
_POOL_RECYCLE_S = 1800


def create_engine(
    url: str,
    *,
    pool_size: int = 8,
    max_overflow: int = 4,
    application_name: str = "harmony",
) -> AsyncEngine:
    """An asyncpg-backed engine for `url` (a `postgresql+asyncpg://` URL)."""
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_S,
        connect_args={"server_settings": {"application_name": application_name}},
    )


def engine_from_settings(settings: Settings, *, application_name: str = "harmony") -> AsyncEngine:
    """The process engine on DATABASE_URL, sized from the settings' pool budget.

    `application_name` shows up in `pg_stat_activity`, which is how the connection
    budget is audited; use `harmony-api` / `harmony-worker`.
    """
    return create_engine(
        settings.sqlalchemy_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        application_name=application_name,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions that keep loaded attributes after commit (responses are built post-commit)."""
    return async_sessionmaker(engine, expire_on_commit=False)
