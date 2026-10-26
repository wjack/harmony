"""The P1–P2 contract (docs/P1_P2_CONTRACT.md): every route is declared and typed.

`ROUTES` must list every route the P1–P2 routers declare, so a route added without a
contract line fails here. Each route documents its errors as `ApiProblem` bodies, and
its behaviour is tested with its domain elsewhere.

Also pins the small contract invariants that aren't routes: the snapshot entity types,
the `demo.needs_human` event, and the worker registration.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

from harmony.api.app import P1_P2_ROUTERS, create_app
from harmony.db.models import SNAPSHOT_ENTITIES
from harmony.events.schemas import (
    APPEND_ONLY_ENTITY_TYPES,
    ENTITY_TYPES,
    DemoNeedsHuman,
    DemoNotice,
    EventScope,
    HarmonyEvent,
)
from harmony.temporal.worker import main_workflows
from harmony.temporal.workflows.conversation import ConversationWorkflow
from harmony.temporal.workflows.network_request import NetworkRequestWorkflow
from harmony.temporal.workflows.thread import ThreadWorkflow
from tests.conftest import settings_without_dotenv

# (method, path template) of every P1–P2 route.
ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/conversations"),
    ("POST", "/api/conversations"),
    ("GET", "/api/conversations/{conversation_id}"),
    ("POST", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/cards/{card_id}/approve"),
    ("POST", "/api/cards/{card_id}/reject"),
    ("GET", "/api/requests/{request_id}"),
    ("GET", "/api/inbox"),
    ("POST", "/api/escalations/{escalation_id}/resolve"),
    ("POST", "/api/escalations/{escalation_id}/reassign"),
    ("POST", "/api/escalations/{escalation_id}/redraft"),
    ("GET", "/api/threads/{thread_id}/workspace"),
    ("POST", "/api/threads/{thread_id}/notes"),
    ("GET", "/api/orgs/{slug}/harmony"),
    ("PUT", "/api/orgs/{slug}/harmony"),
    ("POST", "/api/orgs/{slug}/trusted-organizations"),
    ("PUT", "/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}"),
    ("DELETE", "/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}"),
    ("GET", "/api/network"),
    ("GET", "/api/demo/story"),
]


def _app() -> FastAPI:
    return create_app(settings_without_dotenv(app_env="ci"))


def test_the_table_lists_every_p1_p2_route() -> None:
    declared = {
        (method, "/api" + route.path)
        for module in P1_P2_ROUTERS
        for route in module.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert declared == set(ROUTES)


def test_openapi_documents_every_error_as_a_problem_body() -> None:
    """401 keeps FastAPI's body and 422 its validation errors; every other error a
    route documents is an `ApiProblem`."""
    schema = _app().openapi()
    assert "ApiProblem" in schema["components"]["schemas"]
    for method, template in ROUTES:
        responses = schema["paths"][template][method.lower()]["responses"]
        assert "401" in responses, (method, template)
        for code, response in responses.items():
            if not code.startswith(("4", "5")) or code in {"401", "422"}:
                continue
            ref = response["content"]["application/json"]["schema"]["$ref"]
            assert ref == "#/components/schemas/ApiProblem", (method, template, code)


def test_a_request_is_only_ever_active_or_closed() -> None:
    """Nothing withdraws a request (no route, no signal), so no view may show a
    status nothing can set."""
    schemas = _app().openapi()["components"]["schemas"]
    for view in ("NetworkRequestRef", "RequestStatusView"):
        assert schemas[view]["properties"]["status"]["enum"] == ["active", "closed"], view


async def test_routes_still_require_a_session() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/api/inbox")
    assert response.status_code == 401


def test_entity_types_cover_every_snapshot_model() -> None:
    assert set(SNAPSHOT_ENTITIES) | set(APPEND_ONLY_ENTITY_TYPES) == set(ENTITY_TYPES)
    assert not set(SNAPSHOT_ENTITIES) & set(APPEND_ONLY_ENTITY_TYPES)
    assert len(ENTITY_TYPES) == len(set(ENTITY_TYPES))


def test_needs_human_is_a_demo_notice_that_names_who() -> None:
    escalation_id = uuid4()
    event = TypeAdapter(HarmonyEvent).validate_python(
        {
            "type": "demo.needs_human",
            "org_slug": "brightline",
            "member_slug": "priya",
            "entity_id": str(escalation_id),
        }
    )
    assert isinstance(event, DemoNeedsHuman)
    # The demo scope (UnitOfWork, SSE) accepts only `DemoNotice`s.
    assert isinstance(event, DemoNotice)
    assert event.kind == "needs_human"
    assert EventScope.demo().kind == "demo"
    with pytest.raises(ValidationError, match="member_slug"):
        DemoNeedsHuman(org_slug="brightline", entity_id=escalation_id)


def test_the_worker_registers_the_p1_workflows() -> None:
    assert {ConversationWorkflow, NetworkRequestWorkflow, ThreadWorkflow} <= set(main_workflows())
