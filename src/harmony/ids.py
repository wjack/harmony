"""IDs for rows an activity creates (ARCHITECTURE.md §4).

A side effect of a model turn gets `uuid5(turn_id, step_idx, tool_use_id)`: the turn
ID is minted once in workflow code, and the model call at (turn, step) is checkpointed,
so a retried activity derives the same ID and collides with the row it wrote before
instead of duplicating it. (Seed rows use `harmony.seeds.ids`.)

`turn_id_for` names a turn by what it is (world, thread, producing org, turn number),
so every run that takes the same turn, not only every attempt of one run, shares its
checkpoints and side-effect IDs.
"""

from __future__ import annotations

from uuid import UUID, uuid5


def side_effect_id(turn_id: UUID, step_idx: int, tool_use_id: str) -> UUID:
    return uuid5(turn_id, f"{step_idx}/{tool_use_id}")


def turn_id_for(world_id: UUID, thread_id: UUID, org_id: UUID, turn: int) -> UUID:
    return uuid5(world_id, f"turn/{thread_id}/{org_id}/{turn}")
