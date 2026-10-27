"""Saved workflow histories (tests/fixtures/histories/), which `test_replay` replays
against today's workflow code, and the recorder that saves them.

Each file is one run of a P1–P2 workflow, `{"workflow_id": ..., "history": ...}`, with
the history in Temporal's JSON form (`WorkflowHistory.to_json`), less what names the
machine that recorded it: its worker identity, which sticky task queue names carry too,
reads `RECORDER`, and stack traces (absolute paths) are dropped. Replay reads neither.

The Temporal fixtures record every workflow their client starts. When
HARMONY_SAVE_HISTORIES names a directory, each test saves there every run of each
P1–P2 workflow it started, a continue-as-new chain run by run:

    HARMONY_SAVE_HISTORIES=tests/fixtures/histories/<recording> uv run pytest \\
        tests/workflow/test_e4_workflows.py \\
        "tests/e2e/test_story_scripted.py::test_the_suggested_story_plays_and_every_firewall_holds[today]"
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Final, cast

from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    StartWorkflowInput,
    WorkflowHandle,
    WorkflowHistory,
)

from harmony.temporal.workflows.conversation import ConversationWorkflow
from harmony.temporal.workflows.network_request import NetworkRequestWorkflow
from harmony.temporal.workflows.thread import ThreadWorkflow

HISTORIES: Final = Path(__file__).parent / "fixtures" / "histories"
SAVE_HISTORIES_ENV: Final = "HARMONY_SAVE_HISTORIES"
RECORDER: Final = "harmony-test"
# The workflows whose histories are saved and replayed: the test stand-ins and the P0
# workflows (hello, retirement) aren't.
REPLAYED: Final = (ThreadWorkflow, ConversationWorkflow, NetworkRequestWorkflow)
REPLAYED_TYPES: Final = frozenset(workflow.__name__ for workflow in REPLAYED)


def load(path: Path) -> WorkflowHistory:
    saved = json.loads(path.read_text(encoding="utf-8"))
    return WorkflowHistory.from_json(saved["workflow_id"], saved["history"])


def workflow_type(history: WorkflowHistory) -> str:
    started = history.events[0].workflow_execution_started_event_attributes
    return started.workflow_type.name


class StartedWorkflows(Interceptor):
    """Records every workflow its client starts (signal-with-start included), so a test
    can save their histories."""

    def __init__(self) -> None:
        self.handles: list[WorkflowHandle[Any, Any]] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _Recorder(next, self.handles)

    async def save(self, client: Client, test: str) -> None:
        """Save every run of each recorded P1–P2 workflow to HARMONY_SAVE_HISTORIES, if
        it is set; `test` names the files."""
        directory = os.environ.get(SAVE_HISTORIES_ENV)
        if directory:
            _write(Path(directory), test, await self._runs(client), client.identity)

    async def _runs(self, client: Client) -> list[tuple[str, WorkflowHistory]]:
        """Each recorded P1–P2 workflow's runs, in order, with a name for each."""
        runs: list[tuple[str, WorkflowHistory]] = []
        seen: set[str] = set()
        for index, handle in enumerate(self.handles):
            # A signal-with-start returns no first run: the run it signalled starts the
            # chain as far as this test is concerned.
            run_id = handle.first_execution_run_id or handle.result_run_id
            part = 0
            while run_id is not None and run_id not in seen:
                seen.add(run_id)
                history = await client.get_workflow_handle(handle.id, run_id=run_id).fetch_history()
                kind = workflow_type(history)
                if kind not in REPLAYED_TYPES:
                    break
                runs.append((f"{index:02d}_{kind}_run{part}", history))
                run_id = _next_run(history)
                part += 1
        return runs


class _Recorder(OutboundInterceptor):
    def __init__(self, next: OutboundInterceptor, handles: list[WorkflowHandle[Any, Any]]):
        super().__init__(next)
        self._handles = handles

    async def start_workflow(self, input: StartWorkflowInput) -> WorkflowHandle[Any, Any]:
        handle = await super().start_workflow(input)
        self._handles.append(handle)
        return handle


def _write(
    directory: Path, test: str, runs: list[tuple[str, WorkflowHistory]], identity: str
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", test).strip("_")
    for name, history in runs:
        recorded = json.loads(history.to_json().replace(identity, RECORDER))
        body = {"workflow_id": history.workflow_id, "history": _without_stack_traces(recorded)}
        path = directory / f"{prefix}__{name}.json"
        path.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")


def _without_stack_traces(value: Any) -> Any:
    if isinstance(value, dict):
        fields = cast(dict[str, Any], value)
        return {k: _without_stack_traces(v) for k, v in fields.items() if k != "stackTrace"}
    if isinstance(value, list):
        return [_without_stack_traces(item) for item in cast(list[Any], value)]
    return value


def _next_run(history: WorkflowHistory) -> str | None:
    last = history.events[-1]
    if not last.HasField("workflow_execution_continued_as_new_event_attributes"):
        return None
    return last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
