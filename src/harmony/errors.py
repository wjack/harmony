"""Domain errors shared across layers.

Temporal matches non-retryable errors by class name (an activity's uncaught exception
becomes an `ApplicationError` whose `type` is the class name), so these names are part
of the contract with `temporal/policies.py`: don't rename them casually.
"""

from __future__ import annotations

from uuid import UUID


class HarmonyError(Exception):
    """Base class for Harmony's own errors."""


class WorldRetired(HarmonyError):
    """The world is not `live` (it is retiring or retired): refuse the write or the start.

    Raised by activities' status guard and by the UnitOfWork's commit-time re-check.
    Non-retryable in Temporal.
    """

    def __init__(self, world_id: UUID) -> None:
        super().__init__(f"world {world_id} is not live")
        self.world_id = world_id
