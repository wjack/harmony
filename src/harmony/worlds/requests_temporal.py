"""The production `RequestStarter`: starts a new world's seeded open requests (§11.6).

A scenario may seed requests that are open when the world is created, as if their
outreach had just been approved (threads.yaml's `requests`, such as the adversary's).
Each gets its `NetworkRequestWorkflow`, the same start an approved outreach card makes.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from harmony.temporal.activities.network import start_request_workflow


class TemporalRequestStarter:
    def __init__(self, client: Client, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._client = client
        self._sessionmaker = sessionmaker

    async def start(self, world_id: UUID, request_ids: Sequence[UUID]) -> None:
        for request_id in request_ids:
            await start_request_workflow(
                self._client, self._sessionmaker, world_id=world_id, request_id=request_id
            )


__all__ = ["TemporalRequestStarter"]
