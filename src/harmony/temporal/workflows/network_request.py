"""`NetworkRequestWorkflow`: one approved request and its threads (§7).
ID `w:{world}:req:{request_id}`.

Started by `NetworkActivities.start_network_request` once an outreach card is approved.
It opens one thread per recipient (`NetworkActivities.open_thread`), each running its
own `ThreadWorkflow`, through a sliding window of `max_active_threads` threads that
haven't ended, and follows them through `thread_changed` signals. Once every thread is
`closed` or `expired`, the request is `closed` and the origin chat hears `closed`, the
one update that wakes internal Claude to tell the member how it went.

Owner: R-temporal.
"""

from __future__ import annotations

from uuid import UUID

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from harmony.agents.models import RequestStatus
    from harmony.db.models.choices import ThreadState
    from harmony.temporal.activities.network import (
        NetworkActivities,
        NotifyConversationInput,
        OpenThreadInput,
        RequestStatusInput,
    )
    from harmony.temporal.policies import mirror_activity_options
    from harmony.temporal.workflows.payloads import (
        NetworkRequestInput,
        NetworkRequestResult,
        NetworkUpdate,
        RequestProgress,
        ThreadChanged,
        ThreadProgressRef,
    )

TERMINAL: frozenset[ThreadState] = frozenset(("closed", "expired"))


@workflow.defn
class NetworkRequestWorkflow:
    @workflow.init
    def __init__(self, request: NetworkRequestInput) -> None:
        self._input = request
        self._status: RequestStatus = "active"
        self._threads: dict[UUID, ThreadChanged] = {}
        self._unopened: list[UUID] = list(dict.fromkeys(request.recipient_org_ids))

    @workflow.run
    async def run(self, request: NetworkRequestInput) -> NetworkRequestResult:
        while True:
            await self._open_threads()
            if not self._unopened and self._live() == 0:
                break
            await workflow.wait_condition(self._can_progress)
        await workflow.wait_condition(workflow.all_handlers_finished)
        self._status = "closed"
        await workflow.execute_activity_method(
            NetworkActivities.set_request_status,
            RequestStatusInput(
                world_id=request.world_id, request_id=request.request_id, status="closed"
            ),
            **mirror_activity_options(),
        )
        await self._notify_closed()
        return NetworkRequestResult(status=self._status)

    @workflow.signal
    def thread_changed(self, change: ThreadChanged) -> None:
        known = self._threads.get(change.thread_id)
        if known is not None and known.state in TERMINAL:
            return
        self._threads[change.thread_id] = change

    @workflow.query
    def status(self) -> RequestProgress:
        return RequestProgress(
            request_id=self._input.request_id,
            status=self._status,
            threads=[
                ThreadProgressRef(
                    thread_id=change.thread_id,
                    responder_org_id=change.responder_org_id,
                    state=change.state,
                )
                for change in self._threads.values()
            ],
        )

    async def _open_threads(self) -> None:
        request = self._input
        while self._unopened and self._live() < request.max_active_threads:
            org_id = self._unopened.pop(0)
            thread_id = workflow.uuid4()
            # Seen before the activity returns, so a fast first signal isn't overwritten.
            self._threads.setdefault(
                thread_id,
                ThreadChanged(thread_id=thread_id, responder_org_id=org_id, state="active"),
            )
            try:
                await workflow.execute_activity_method(
                    NetworkActivities.open_thread,
                    OpenThreadInput(
                        world_id=request.world_id,
                        request_id=request.request_id,
                        thread_id=thread_id,
                        responder_org_id=org_id,
                    ),
                    **mirror_activity_options(),
                )
            except ActivityError as failed:
                workflow.logger.warning("couldn't open a thread to %s: %s", org_id, failed)
                self._threads[thread_id] = ThreadChanged(
                    thread_id=thread_id, responder_org_id=org_id, state="closed"
                )

    def _live(self) -> int:
        return sum(1 for t in self._threads.values() if t.state not in TERMINAL)

    def _can_progress(self) -> bool:
        """A slot freed for the next recipient, or nothing is left to wait for."""
        live = self._live()
        if not self._unopened:
            return live == 0
        return live < self._input.max_active_threads

    async def _notify_closed(self) -> None:
        request = self._input
        if request.conversation_id is None:
            return
        await workflow.execute_activity_method(
            NetworkActivities.notify_conversation,
            NotifyConversationInput(
                world_id=request.world_id,
                conversation_id=request.conversation_id,
                update=NetworkUpdate(request_id=request.request_id, kind="closed"),
            ),
            **mirror_activity_options(),
        )


__all__ = ["NetworkRequestWorkflow"]
