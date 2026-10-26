"""Reviews between orgs: what discovery's reputation reads (§6.1)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, Index, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import Base, CreatedAt, Entity, world_fk


class Review(CreatedAt, Entity, Base):
    """A review of one org by another."""

    __tablename__ = "reviews"
    __table_args__ = (
        world_fk("about_org_id", "orgs"),
        world_fk("by_org_id", "orgs"),
        Index(None, "world_id", "about_org_id"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
    )

    about_org_id: Mapped[UUID]
    by_org_id: Mapped[UUID]
    rating: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str]
