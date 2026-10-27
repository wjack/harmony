"""The declarative base every model inherits, and the mixins that shape world-scoped rows.

The naming convention gives every constraint a deterministic name, which Alembic needs
for autogenerate, `alembic check` and clean downgrades.

World-scoped keys (ARCHITECTURE.md §4): every world-scoped table's primary key starts
with `world_id`, and every foreign key to a world-scoped table includes it, so a
cross-world reference cannot be stored. Use `world_fk` for those foreign keys.

Models declare no `relationship()`s. Composite keys that share `world_id` make
relationships noisy (overlapping foreign-key columns), and async sessions cannot lazy
load anyway; queries go through `harmony.db.repo` with an explicit world filter.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal, TypedDict, get_args
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Deferral(TypedDict):
    deferrable: bool
    initially: str


# Foreign keys are DEFERRABLE INITIALLY DEFERRED. Models declare no relationships, so
# the ORM flushes unrelated tables in class-name order, not dependency order; checking
# at commit lets one flush insert a parent and its children in any order.
# `UnitOfWork.commit` runs the checks early (`SET CONSTRAINTS ALL IMMEDIATE`), before it
# allocates event seqs, so no FK check takes a lock while the seq row is held.
# Cascades and SET NULL are never deferred.
DEFERRED: Final[Deferral] = {"deferrable": True, "initially": "DEFERRED"}

# Dollar amounts (spend, budgets): six decimals keeps per-token prices exact.
USD = Numeric(14, 6)

type JsonObject = dict[str, Any]
type JsonArray = list[Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        UUID: postgresql.UUID(as_uuid=True),
        str: Text(),
        # Enumerated columns are text plus a CHECK (`one_of`), never a native enum,
        # so adding a value is a plain constraint swap.
        Literal: Text(),
        datetime: DateTime(timezone=True),
        date: Date(),
        Decimal: USD,
        JsonObject: postgresql.JSONB(),
        JsonArray: postgresql.JSONB(),
    }


# --- Column helpers -----------------------------------------------------------------


def one_of(column: str, literal: Any) -> CheckConstraint:
    """A CHECK that `column` holds one of the values of a `Literal[...]` type.

    Deriving the list from the Literal keeps the Python type and the database in step.
    """
    values = ", ".join(f"'{value}'" for value in get_args(literal))
    return CheckConstraint(f"{column} IN ({values})", name=column)


def deferred_fk(target: str, *, ondelete: str | None = None) -> ForeignKey:
    """A single-column foreign key, checked at commit (see `DEFERRED`)."""
    return ForeignKey(target, ondelete=ondelete, **DEFERRED)


def world_fk(
    columns: str | tuple[str, ...],
    table: str,
    *,
    name: str | None = None,
    cycle: bool = False,
) -> ForeignKeyConstraint:
    """A foreign key `(world_id, *columns)` to `table (world_id, id)`, checked at commit.

    For a table whose key is not `(world_id, id)`, pass `local:remote` column pairs,
    e.g. `("org_id:org_id", "settings_version:version")`. `cycle=True` creates the
    constraint after both tables (`ALTER TABLE`): use it on the one edge that closes a
    reference cycle.
    """
    pairs = [columns] if isinstance(columns, str) else list(columns)
    local = ["world_id"]
    remote = [f"{table}.world_id"]
    for pair in pairs:
        mine, _, theirs = pair.partition(":")
        local.append(mine)
        remote.append(f"{table}.{theirs or 'id'}")
    return ForeignKeyConstraint(local, remote, name=name, use_alter=cycle, **DEFERRED)


# --- Mixins -------------------------------------------------------------------------


class WorldScoped:
    """A row that lives in one world. Its primary key starts with `world_id`.

    Deleting the `worlds` row cascades to every world-scoped row; `repo.delete_world_rows`
    removes a world's contents but keeps the `worlds` row as a tombstone.
    """

    world_id: Mapped[UUID] = mapped_column(
        deferred_fk("worlds.id", ondelete="CASCADE"), primary_key=True, sort_order=-100
    )


class CreatedAt:
    """`created_at`, set by the database at insert (transaction start time)."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), sort_order=90)


class Entity(WorldScoped):
    """A world-scoped row keyed `(world_id, id)`.

    IDs are always minted by the caller: `uuid5` for seeds and activity side effects,
    `workflow.uuid4()` in workflow code. There is no default, so a forgotten
    deterministic ID fails loudly instead of duplicating on retry.
    """

    id: Mapped[UUID] = mapped_column(primary_key=True, sort_order=-90)


class Snapshotable(WorldScoped):
    """A row the web client caches from `entity.snapshot` events (applied by version).

    `UnitOfWork.add_snapshot(row, scope)` builds the event after the flush, so the
    version it carries is the one the row was committed with.
    """

    # The snapshot's `entity_type`; unique across models (`SNAPSHOT_ENTITIES`).
    entity_type: ClassVar[str]
    # Columns the snapshot leaves out, so their text never reaches a browser.
    snapshot_omits: ClassVar[frozenset[str]] = frozenset()

    def snapshot_id(self) -> UUID:
        """The snapshot's `entity_id`: the row's id, or its org's for per-org rows."""
        raise NotImplementedError

    def snapshot_version(self) -> int:
        raise NotImplementedError


class Versioned(Snapshotable):
    """A mutable snapshot entity whose `version` bumps on every ORM flush that updates it.

    `version` is SQLAlchemy's `version_id_col`: each ORM UPDATE sets
    `version = version + 1` guarded by `WHERE version = <loaded>`, so a concurrent
    writer raises `StaleDataError` instead of silently overwriting. Core `update()`
    statements bypass this and must bump `version` themselves.
    """

    version: Mapped[int] = mapped_column(Integer, server_default=text("1"), sort_order=95)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.version}

    def snapshot_version(self) -> int:
        return self.version


class VersionedEntity(Entity, Versioned):
    """A `(world_id, id)` entity with a version: the common case."""

    def snapshot_id(self) -> UUID:
        return self.id
