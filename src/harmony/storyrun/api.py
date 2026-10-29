"""A typed client for the routes `harmony run` drives, as one browser session.

Responses parse into the routes' own response models, so a contract change fails here
loudly instead of drifting. `in_process` serves the app over `httpx.ASGITransport`
without its lifespan: no LISTEN bus (nothing here reads `/api/events`) and no orphan
sweep, with the caller's Temporal client and retirer. With `ACCESS_CODE` set it unlocks
the passcode gate first, as a visitor would.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Receive, Scope, Send
from temporalio.client import Client

from harmony.api.app import API_PREFIX, create_app
from harmony.api.gate import GateUnlock, GateUnlocked
from harmony.api.routes.cards import ApproveCard, RejectCard
from harmony.api.routes.conversations import (
    ConversationCreated,
    ConversationDetail,
    StartConversation,
)
from harmony.api.routes.demo import DemoApprovals
from harmony.api.routes.escalations import ResolveEscalation
from harmony.api.routes.inbox import Inbox
from harmony.api.routes.threads import ReopenRequest, ReopenStarted, ThreadDetail
from harmony.api.routes.workspace import AddSteer, ThreadWorkspace
from harmony.auth.views import MemberDirectory, SessionView
from harmony.config import Settings
from harmony.domain.requests import RequestStatusView
from harmony.temporal.workflows.payloads import CardResolved, EscalationResolved, SteerStarted
from harmony.worlds.retirer import Retirer

log = logging.getLogger(__name__)

# Any https origin works in process; https so a non-local env's Secure cookie is sent.
BASE_URL = "https://harmony.test"
TIMEOUT_S = 60.0


class ApiError(RuntimeError):
    """A route answered with an error, or with a body its model doesn't accept."""

    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"{method} {API_PREFIX}{path}: {status} {detail}")
        self.status = status
        self.detail = detail


class HarmonyApi:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    # --- Session ----------------------------------------------------------------------

    async def start_session(self, scenario: str) -> SessionView:
        return await self._call(SessionView, "POST", "/session/start", {"scenario": scenario})

    async def act_as(self, member_id: UUID) -> SessionView:
        return await self._call(
            SessionView, "POST", "/session/act-as", {"member_id": str(member_id)}
        )

    async def members(self) -> MemberDirectory:
        return await self._call(MemberDirectory, "GET", "/members")

    # --- Chat -------------------------------------------------------------------------

    async def unlock(self, passcode: str) -> GateUnlocked:
        return await self._call(GateUnlocked, "POST", "/gate", GateUnlock(passcode=passcode))

    async def start_conversation(
        self, text: str, *, send_without_confirmation: bool | None = None
    ) -> ConversationCreated:
        """A new chat; `send_without_confirmation` sets its override (None follows the
        org's default)."""
        body = StartConversation(text=text, send_without_confirmation=send_without_confirmation)
        return await self._call(ConversationCreated, "POST", "/conversations", body)

    async def set_skip_approvals(self, skip: bool) -> DemoApprovals:
        """Demo controls' Skip approvals for the session's world."""
        return await self._call(DemoApprovals, "PUT", "/demo/approvals", DemoApprovals(skip=skip))

    async def conversation(self, conversation_id: UUID) -> ConversationDetail:
        return await self._call(ConversationDetail, "GET", f"/conversations/{conversation_id}")

    async def approve_card(self, card_id: UUID, body: ApproveCard) -> CardResolved:
        return await self._call(CardResolved, "POST", f"/cards/{card_id}/approve", body)

    async def reject_card(self, card_id: UUID, feedback: str | None) -> CardResolved:
        body = RejectCard(feedback=feedback)
        return await self._call(CardResolved, "POST", f"/cards/{card_id}/reject", body)

    async def request(self, request_id: UUID) -> RequestStatusView:
        return await self._call(RequestStatusView, "GET", f"/requests/{request_id}")

    # --- Inbox and threads ------------------------------------------------------------

    async def inbox(self) -> Inbox:
        return await self._call(Inbox, "GET", "/inbox")

    async def workspace(self, thread_id: UUID) -> ThreadWorkspace:
        return await self._call(ThreadWorkspace, "GET", f"/threads/{thread_id}/workspace")

    async def resolve(self, escalation_id: UUID, body: ResolveEscalation) -> EscalationResolved:
        return await self._call(
            EscalationResolved, "POST", f"/escalations/{escalation_id}/resolve", body
        )

    async def thread(self, thread_id: UUID) -> ThreadDetail:
        return await self._call(ThreadDetail, "GET", f"/threads/{thread_id}")

    async def steer(self, thread_id: UUID, body: AddSteer) -> SteerStarted:
        return await self._call(SteerStarted, "POST", f"/threads/{thread_id}/steers", body)

    async def reopen(self, thread_id: UUID, body: ReopenRequest) -> ReopenStarted:
        return await self._call(ReopenStarted, "POST", f"/threads/{thread_id}/reopen", body)

    # --- Transport --------------------------------------------------------------------

    async def _call[M: BaseModel](
        self, model: type[M], method: str, path: str, body: BaseModel | dict[str, Any] | None = None
    ) -> M:
        payload = (
            body.model_dump(mode="json", exclude_none=True) if isinstance(body, BaseModel) else body
        )
        response = await self._http.request(method, API_PREFIX + path, json=payload)
        if response.is_error:
            raise ApiError(method, path, response.status_code, _detail(response))
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ApiError(method, path, response.status_code, f"unexpected body: {exc}") from exc


class _Problem(BaseModel):
    """What error bodies share: `ApiProblem` and FastAPI's own both carry `detail`."""

    detail: str


def _detail(response: httpx.Response) -> str:
    try:
        return _Problem.model_validate_json(response.content).detail
    except ValidationError:
        return response.text[:500]


@asynccontextmanager
async def in_process(
    settings: Settings, *, engine: AsyncEngine, temporal: Client, retirer: Retirer
) -> AsyncGenerator[HarmonyApi]:
    """The app served in this process, as one visitor with its own cookie jar.

    A route that raises answers 500, as it would behind a server, so the run ends with
    its report; the traceback is logged, as a server would log it.
    """
    app = create_app(settings, engine=engine, temporal=temporal, retirer=retirer)

    async def serve(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await app(scope, receive, send)
        except Exception:
            log.exception("%s %s failed", scope.get("method"), scope.get("path"))
            raise

    transport = httpx.ASGITransport(app=serve, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL, timeout=TIMEOUT_S) as http:
        api = HarmonyApi(http)
        if settings.access_code is not None:
            await api.unlock(settings.access_code.get_secret_value())
        yield api
