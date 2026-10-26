"""Outbound network requests and the discovery screenings behind them (§6, §7)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Index, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import (
    Base,
    CreatedAt,
    JsonArray,
    VersionedEntity,
    WorldScoped,
    one_of,
    world_fk,
)
from harmony.db.models.choices import Fit


class NetworkRequest(CreatedAt, VersionedEntity, Base):
    """A request an org sends out, with the brief its member approved.

    Its workflow is `w:{world}:req:{id}` (§7). `status` is free text
    (`agents.models.RequestStatus`).
    """

    __tablename__ = "network_requests"
    __table_args__ = (
        world_fk("requester_org_id", "orgs"),
        world_fk("conversation_id", "conversations"),
        world_fk("created_by_member_id", "members"),
        world_fk("card_id", "cards"),
        Index(None, "world_id", "requester_org_id"),
    )
    entity_type = "network_request"

    requester_org_id: Mapped[UUID]
    # The chat the request came from, if any.
    conversation_id: Mapped[UUID | None]
    created_by_member_id: Mapped[UUID | None]
    # The approved outreach card.
    card_id: Mapped[UUID | None]
    # The free-text brief the member approved: the requester representative's mandate.
    brief: Mapped[str]
    # Approved recipients, with fit and reason as shown on the card.
    recipients: Mapped[JsonArray] = mapped_column(server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(server_default=text("'active'"))


class Screening(CreatedAt, WorldScoped, Base):
    """One screener verdict for one org in one search: a fit and a one-line reason. The
    first write wins (§6.1).

    `search_id` identifies one `search_network` call (deterministic per tool use).
    """

    __tablename__ = "screenings"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("requester_org_id", "orgs"),
        one_of("fit", Fit),
    )

    search_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    requester_org_id: Mapped[UUID]
    fit: Mapped[Fit]
    reason: Mapped[str] = mapped_column(server_default=text("''"))
