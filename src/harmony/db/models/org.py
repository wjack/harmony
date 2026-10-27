"""Orgs, their members, communities, and the representative's settings, policy and
trusted organizations (§9)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import (
    Base,
    CreatedAt,
    Entity,
    Snapshotable,
    Versioned,
    VersionedEntity,
    WorldScoped,
    one_of,
    world_fk,
)
from harmony.db.models.choices import Autonomy, DirectoryVisibility, MemberRole


class Org(CreatedAt, VersionedEntity, Base):
    """A network participant. Every org is both a requester and a responder."""

    __tablename__ = "orgs"
    __table_args__ = (UniqueConstraint("world_id", "slug"),)
    entity_type = "org"

    slug: Mapped[str]
    name: Mapped[str]
    # The network view's category: characterization, synthesis, fabrication, ...
    kind: Mapped[str]
    # Featured orgs are hand-written, stream tokens and run on Opus (§5, §11.7).
    featured: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The `rep_settings.version` new thread sides pin. No foreign key: settings rows
    # reference the org, and a cycle would need deferred constraints for one integer.
    current_settings_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class Member(CreatedAt, VersionedEntity, Base):
    """A person in an org; these are the switchable accounts (§11.3)."""

    __tablename__ = "members"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        UniqueConstraint("world_id", "slug"),
        Index(None, "world_id", "org_id"),
        one_of("role", MemberRole),
    )
    entity_type = "member"

    org_id: Mapped[UUID]
    # Stable across worlds (seeded), so a reset keeps the visitor's account.
    slug: Mapped[str]
    name: Mapped[str]
    title: Mapped[str | None]
    role: Mapped[MemberRole]
    # What they handle, in prose; the router reads it (§10).
    profile: Mapped[str] = mapped_column(server_default=text("''"))


class Community(CreatedAt, Entity, Base):
    """A trusted circle such as a university or a consortium (§17.7)."""

    __tablename__ = "communities"
    __table_args__ = (UniqueConstraint("world_id", "slug"),)

    slug: Mapped[str]
    name: Mapped[str]
    kind: Mapped[str]


class OrgCommunity(CreatedAt, WorldScoped, Base):
    """Membership of an org in a community, verified by the network."""

    __tablename__ = "org_communities"
    __table_args__ = (
        world_fk("community_id", "communities"),
        world_fk("org_id", "orgs"),
        Index(None, "world_id", "org_id"),
    )

    community_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    verified: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class RepSettings(CreatedAt, Snapshotable, Base):
    """One version of an org's prompt-facing settings (§9). Rows are immutable.

    A thread side's system prompt is frozen from the version current at its first turn;
    `threads` pins it per side. Its snapshot id is the org's id.
    """

    __tablename__ = "rep_settings"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("created_by_member_id", "members"),
    )
    entity_type = "rep_settings"

    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What the org does, in prose: what discovery screens and counterparties read.
    public_summary: Mapped[str]
    # The representative's private prompt, in prose.
    instructions: Mapped[str]
    created_by_member_id: Mapped[UUID | None]

    def snapshot_id(self) -> UUID:
        return self.org_id

    def snapshot_version(self) -> int:
        return self.version


class RepPolicy(Versioned, Base):
    """An org's live policy, read at every decision point (§3, §9). One row per org."""

    __tablename__ = "rep_policy"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("default_assignee_id", "members"),
        one_of("autonomy", Autonomy),
        one_of("directory_visibility", DirectoryVisibility),
    )
    entity_type = "rep_policy"

    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    # The default mode; a trusted organization may have its own.
    autonomy: Mapped[Autonomy] = mapped_column(server_default=text("'auto'"))
    # What kinds of information the representative may pass on to any counterparty, in
    # prose. The sharing check reads it; it never lists records.
    sharing_description: Mapped[str] = mapped_column(server_default=text("''"))
    directory_visibility: Mapped[DirectoryVisibility] = mapped_column(
        server_default=text("'public'")
    )
    routing_prompt: Mapped[str] = mapped_column(server_default=text("''"))
    default_assignee_id: Mapped[UUID | None]
    # Internal Claude's requests go out without a member's review (3.10); a chat may
    # override it (`conversations.send_without_confirmation`).
    send_without_confirmation: Mapped[bool] = mapped_column(server_default=text("false"))

    def snapshot_id(self) -> UUID:
        return self.org_id


class TrustedOrganization(CreatedAt, Versioned, Base):
    """A counterparty an org treats differently (§3): its own mode, and what else the
    representative may tell it. Live policy, like `rep_policy`."""

    __tablename__ = "trusted_organizations"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("counterparty_org_id", "orgs"),
        world_fk("updated_by_member_id", "members"),
        one_of("autonomy", Autonomy),
        CheckConstraint("org_id <> counterparty_org_id", name="not_self"),
    )
    entity_type = "trusted_organization"

    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    counterparty_org_id: Mapped[UUID] = mapped_column(primary_key=True)
    # NULL: the org's default mode.
    autonomy: Mapped[Autonomy | None]
    # Shared with this counterparty on top of `rep_policy.sharing_description`.
    sharing_description: Mapped[str] = mapped_column(server_default=text("''"))
    updated_by_member_id: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    def snapshot_id(self) -> UUID:
        # Unique per (org, counterparty): the lens sees every org's rows.
        return uuid5(self.org_id, str(self.counterparty_org_id))
