"""An org's own records (knowledge items and documents) as internal Claude's read tools
name them: `kn:<id>` and `doc:<id>` references, or a bare id.

Activity lines name what was read by its title. Titles come only from the org's own
records, so a reference to another org's record (which `read_document` refuses) never
shows that record's title.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Final
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db.models import Document, KnowledgeItem

READ_DOCUMENT: Final = "read_document"
_PREFIXES: Final = ("kn:", "doc:")


def record_id(ref: str) -> UUID | None:
    """The record a `kn:<id>` / `doc:<id>` reference (or a bare id) names."""
    text = ref.strip()
    for prefix in _PREFIXES:
        text = text.removeprefix(prefix)
    try:
        return UUID(text)
    except ValueError:
        return None


def read_ref(tool_input: JsonValue | None) -> UUID | None:
    """The record a `read_document` call's input names."""
    ref = tool_input.get("id") if isinstance(tool_input, dict) else None
    return record_id(ref) if isinstance(ref, str) else None


async def titles(
    session: AsyncSession, *, world_id: UUID, org_id: UUID, ids: Collection[UUID]
) -> dict[UUID, str]:
    """The titles of `org_id`'s records among `ids`."""
    if not ids:
        return {}
    wanted = list(ids)
    knowledge = await session.execute(
        select(KnowledgeItem.id, KnowledgeItem.title).where(
            KnowledgeItem.world_id == world_id,
            KnowledgeItem.org_id == org_id,
            KnowledgeItem.id.in_(wanted),
        )
    )
    documents = await session.execute(
        select(Document.id, Document.title).where(
            Document.world_id == world_id, Document.org_id == org_id, Document.id.in_(wanted)
        )
    )
    return {row[0]: row[1] for result in (knowledge, documents) for row in result}


def read_ids(calls: Iterable[tuple[str, JsonValue | None]]) -> set[UUID]:
    """The records named by the `read_document` calls among (tool name, input) pairs."""
    return {
        ref
        for name, tool_input in calls
        if name == READ_DOCUMENT and (ref := read_ref(tool_input)) is not None
    }


__all__ = ["READ_DOCUMENT", "read_ids", "read_ref", "record_id", "titles"]
