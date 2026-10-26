"""The per-test database fixtures: each test gets its own clone of the template."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.config import Settings
from tests.conftest import ClonedDatabase

_SCRATCH = "t1_isolation_probe"


async def test_engine_is_bound_to_this_tests_clone(
    database: ClonedDatabase, db_engine: AsyncEngine, template_database: str
) -> None:
    async with db_engine.connect() as conn:
        name = (await conn.execute(text("select current_database()"))).scalar_one()
    assert name == database.name
    assert name != template_database
    assert name.startswith("harmony_test_")


async def test_a_clone_can_be_written(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with db_sessionmaker() as session, session.begin():
        await session.execute(text(f"create table {_SCRATCH} (id int primary key)"))
        await session.execute(text(f"insert into {_SCRATCH} values (1)"))
    async with db_sessionmaker() as session:
        count = (await session.execute(text(f"select count(*) from {_SCRATCH}"))).scalar_one()
    assert count == 1


async def test_writes_never_leak_into_the_next_clone(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        found = (await conn.execute(text(f"select to_regclass('{_SCRATCH}')"))).scalar_one()
    assert found is None


def test_app_settings_point_at_the_clone(app_settings: Settings, database: ClonedDatabase) -> None:
    assert app_settings.asyncpg_dsn() == database.dsn
    assert app_settings.sqlalchemy_url() == database.url
