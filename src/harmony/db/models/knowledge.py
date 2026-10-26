"""What an org knows: its own records, read only by its internal Claude's tools (§2, §4)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, Index, UniqueConstraint
from sqlalchemy import text as sql_text  # `Document.text` would shadow `text`
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import Base, CreatedAt, VersionedEntity, world_fk


class Document(CreatedAt, VersionedEntity, Base):
    """A file in object storage, with its text extracted at upload and frozen."""

    __tablename__ = "documents"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("uploaded_by_member_id", "members"),
        UniqueConstraint("world_id", "org_id", "slug"),
        Index(None, "world_id", "org_id"),
    )
    entity_type = "document"

    org_id: Mapped[UUID]
    # The seed's natural key; NULL for uploads.
    slug: Mapped[str | None]
    title: Mapped[str]
    # The `ObjectKey.name` under worlds/<world>/orgs/<org>/.
    object_name: Mapped[str]
    mime: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(server_default=sql_text("''"))
    uploaded_by_member_id: Mapped[UUID | None]


class KnowledgeItem(CreatedAt, VersionedEntity, Base):
    """One piece of org knowledge: a price list, a template, a schedule, a note.

    Never rendered into a representative's request: information reaches the other side
    only as a released consult answer (§2).
    """

    __tablename__ = "knowledge_items"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("doc_id", "documents"),
        world_fk("author_member_id", "members"),
        UniqueConstraint("world_id", "org_id", "slug"),
        Index(None, "world_id", "org_id"),
    )
    entity_type = "knowledge_item"

    org_id: Mapped[UUID]
    slug: Mapped[str | None]
    # price_list, template, schedule, past_job, note, ... (free text)
    kind: Mapped[str] = mapped_column(server_default=sql_text("'note'"))
    title: Mapped[str]
    body: Mapped[str]
    doc_id: Mapped[UUID | None]
    # Who wrote it, when a member did; the answerer may suggest asking them (§6).
    author_member_id: Mapped[UUID | None]
