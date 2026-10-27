"""Saved workflow histories replay against today's workflow code (ARCHITECTURE §7,
"Deploys"), so a change that would break runs in flight on a worker fails here first.

tests/fixtures/histories/ holds one file per workflow run (`tests/_histories.py`):
- `p0_p2/`: recorded by the code at ceea81b, before the live phase, from its workflow
  tests and its scripted story. They replay through the live phase's workflow changes:
  failure reasons, internal turns on their own queue, cards sent without review,
  `revise`, the note on a missing summary. One recording isn't kept: a thread whose
  requester's first turn sent nothing, which now ends `expired` (3.13 E3) where it used
  to hand the move to the responder, so its commands differ. Nothing was deployed, so
  no such run is in flight anywhere (LIVE_DEMO_PLAN §4, integration step 2).
- `live/`: recorded by the live phase's code, the same way.

A change that fails here needs a `workflow.patched` guard for runs in flight, or, while
nothing is deployed, the histories recorded again (HARMONY_SAVE_HISTORIES).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from harmony.temporal.workflows.payloads import ConversationCarry
from tests._histories import HISTORIES, REPLAYED, REPLAYED_TYPES, load, workflow_type

RECORDINGS = ("p0_p2", "live")


async def _each(paths: Sequence[Path]) -> AsyncIterator[WorkflowHistory]:
    for path in paths:
        yield load(path)


@pytest.mark.parametrize("recording", RECORDINGS)
async def test_every_saved_history_replays(recording: str) -> None:
    paths = sorted((HISTORIES / recording).glob("*.json"))
    # Every replayed workflow has runs here, so an emptied recording can't pass.
    assert {workflow_type(load(path)) for path in paths} == REPLAYED_TYPES
    replayer = Replayer(workflows=list(REPLAYED), data_converter=pydantic_data_converter)
    failures: list[str] = []
    async with replayer.workflow_replay_iterator(_each(paths)) as results:
        index = 0
        async for result in results:
            if result.replay_failure is not None:
                failures.append(f"{paths[index].name}: {result.replay_failure}")
            index += 1
    assert index == len(paths)
    assert not failures, "\n".join(failures)


def test_a_carry_from_before_failure_reasons_still_loads() -> None:
    """What lets the `p0_p2` conversations replay: their carry says only whether the
    last turn failed."""
    assert ConversationCarry.model_validate({"turn_failed": False}).turn_failure is None
    failed = ConversationCarry.model_validate({"turn_failed": True, "turns": 3})
    assert (failed.turn_failure, failed.turns) == ("model_error", 3)
