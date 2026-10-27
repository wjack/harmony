"""Saved workflow histories replay against today's workflow code (ARCHITECTURE §7,
"Deploys"), so a change that would break runs in flight on a worker fails here first.

tests/fixtures/histories/ holds one file per workflow run (`tests/_histories.py`):
- `r2/`: recorded by the round-2 feedback build once the Edit engine (EDIT.md) removed
  `redraft` and `revise`, from its workflow tests and its scripted story. The earlier
  recordings (`p0_p2/`, `live/`) went with them: those runs would take the removed
  paths, and no deployed world can resume a run that old (FEEDBACK_R2_PLAN §5).

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

from tests._histories import HISTORIES, REPLAYED, REPLAYED_TYPES, load, workflow_type

RECORDINGS = ("r2",)


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
