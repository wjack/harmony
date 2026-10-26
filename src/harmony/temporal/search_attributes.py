"""Typed search attributes. Every world workflow start sets `WORLD_ID` (and `ORG_ID`
when it belongs to one org); retirement lists `WorldId = x` to find what to terminate.

Registered in Compose (`--search-attribute`), in `tests/conftest.py`'s local test
server, and on Temporal Cloud by Terraform.

Safe to import from workflow code: it touches nothing but `temporalio.common`.
"""

from __future__ import annotations

from uuid import UUID

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

WORLD_ID = SearchAttributeKey.for_keyword("WorldId")
ORG_ID = SearchAttributeKey.for_keyword("OrgId")

ALL = (WORLD_ID, ORG_ID)


def world_search_attributes(world_id: UUID, org_id: UUID | None = None) -> TypedSearchAttributes:
    """The attributes for a workflow in `world_id` (and `org_id`), for every start:
    top-level, signal-with-start and children (§7)."""
    pairs = [SearchAttributePair(WORLD_ID, str(world_id))]
    if org_id is not None:
        pairs.append(SearchAttributePair(ORG_ID, str(org_id)))
    return TypedSearchAttributes(pairs)


def running_in_world_query(world_id: UUID) -> str:
    """The List Workflow Executions query retirement loops on. `world_id` is a UUID, so
    quoting is safe."""
    return f"{WORLD_ID.name} = '{world_id}' AND ExecutionStatus = 'Running'"
