"""Scripted model calls for harmony-demo integration tests: one cassette entry per
(role, turn, step), on any thread."""

from __future__ import annotations

from collections.abc import Sequence

from harmony.llm import ModelRole
from harmony.llm.scripted import (
    ANY_THREAD,
    CassetteEntry,
    CassetteKey,
    CassetteMatch,
    ScriptedBlock,
)
from tests.integration._e2_world import SCENARIO


def entry(
    org: str,
    role: ModelRole,
    turn: int,
    step: int,
    content: Sequence[ScriptedBlock],
    *,
    refused: bool = False,
) -> dict[CassetteKey, CassetteEntry]:
    """`content` as the response to `org`'s `role` call at (turn, step), or a refusal."""
    scripted = CassetteEntry.model_validate(
        {
            "match": CassetteMatch(org=org, role=role, thread=ANY_THREAD, turn=turn, step=step),
            "content": list(content),
            "stop_reason": "refusal" if refused else None,
            "stop_details": {"category": "cyber"} if refused else None,
        }
    )
    return {(SCENARIO, org, role, ANY_THREAD, turn, step): scripted}
