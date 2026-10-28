"""The P1–P2 contract (docs/P1_P2_CONTRACT.md): every route is declared and typed.

`ROUTES` must list every route the P1–P2 routers declare, and `LIVE_ROUTES` every
route of the live phase's, so a route added without a contract line fails here. Each
route documents its errors as `ApiProblem` bodies, and its behaviour is tested with its
domain elsewhere.

Also pins the small contract invariants that aren't routes: the snapshot entity types,
the `demo.needs_human` event, and the worker registration.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

from harmony.api.app import LIVE_ROUTERS, P1_P2_ROUTERS, create_app
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
    ("PATCH", "/api/conversations/{conversation_id}"),
    ("POST", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/cards/{card_id}/approve"),
    ("POST", "/api/cards/{card_id}/reject"),
    ("GET", "/api/requests/{request_id}"),
    ("GET", "/api/inbox"),
    ("POST", "/api/escalations/{escalation_id}/resolve"),
    ("POST", "/api/escalations/{escalation_id}/reassign"),
    ("GET", "/api/threads/{thread_id}/workspace"),
    ("POST", "/api/threads/{thread_id}/notes"),
    ("POST", "/api/threads/{thread_id}/steers"),
    ("GET", "/api/orgs/{slug}/harmony"),
    ("PUT", "/api/orgs/{slug}/harmony"),
    ("POST", "/api/orgs/{slug}/trusted-organizations"),
    ("PUT", "/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}"),
    ("DELETE", "/api/orgs/{slug}/trusted-organizations/{counterparty_org_id}"),
    ("GET", "/api/network"),
    ("GET", "/api/demo/story"),
    ("GET", "/api/demo/whose"),
    ("GET", "/api/demo/waiting"),
    ("PUT", "/api/demo/approvals"),
]

# Round 2's new routes (docs/FEEDBACK_R2_PLAN.md §1.3), with the JSON body each takes,
# its success status, the body it answers and the problems it documents. Reopen is on
# P0's threads router, so `ROUTES` doesn't list it.
R2_ROUTES: dict[tuple[str, str], tuple[str | None, str, str, set[str]]] = {
    ("POST", "/api/threads/{thread_id}/steers"): (
        "AddSteer",
        "202",
        "SteerStarted",
        {"403", "404", "409"},
    ),
    ("POST", "/api/threads/{thread_id}/reopen"): (
        "ReopenRequest",
        "202",
        "ReopenStarted",
        {"403", "404", "409"},
    ),
    ("GET", "/api/demo/whose"): (None, "200", "DemoWhose", {"404"}),
}

# Every live-phase route (the "Live phase" section), with the JSON body it takes, its
# success status and the JSON body it answers: the models L0 fixed while these routes
# were stubs answering 501. `GET /api/gate` answers the passcode page, lock no body.
LIVE_ROUTES: dict[tuple[str, str], tuple[str | None, str, str | None]] = {
    ("GET", "/api/gate"): (None, "200", None),
    ("POST", "/api/gate"): ("GateUnlock", "200", "GateUnlocked"),
    ("POST", "/api/gate/lock"): (None, "204", None),
    ("GET", "/api/budget"): (None, "200", "BudgetView"),
    ("GET", "/api/admin/spend"): (None, "200", "AdminSpendView"),
    ("POST", "/api/admin/live"): ("LiveToggle", "200", "LiveState"),
    ("GET", "/api/admin/worlds/{world_id}"): (None, "200", "AdminWorldView"),
    ("GET", "/api/admin/client-key"): (None, "200", "ClientKeyView"),
}
# Live routes behind a visitor session: their 401 is the session's, as on P1–P2 routes.
# Every other live 401 is the gate's `passcode_required` problem.
SESSION_LIVE_ROUTES = {("GET", "/api/budget")}


def _app() -> FastAPI:
    return create_app(settings_without_dotenv(app_env="ci"))


def _declared(routers: tuple[Any, ...]) -> set[tuple[str, str]]:
    return {
        (method, "/api" + route.path)
        for module in routers
        for route in module.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


def test_the_table_lists_every_p1_p2_route() -> None:
    assert _declared(P1_P2_ROUTERS) == set(ROUTES)


def test_the_table_lists_every_live_route() -> None:
    assert _declared(LIVE_ROUTERS) == set(LIVE_ROUTES)


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


def test_live_routes_document_every_error_as_a_problem_body() -> None:
    schema = _app().openapi()
    for method, template in LIVE_ROUTES:
        responses = schema["paths"][template][method.lower()]["responses"]
        for code, response in responses.items():
            if not code.startswith(("4", "5")) or code == "422":
                continue
            if code == "401" and (method, template) in SESSION_LIVE_ROUTES:
                continue
            ref = response["content"]["application/json"]["schema"]["$ref"]
            assert ref == "#/components/schemas/ApiProblem", (method, template, code)


def _json_schema(content: dict[str, Any] | None) -> str | None:
    """The component a request or response body's JSON refers to, if it has one."""
    schema: dict[str, Any] = (content or {}).get("application/json", {}).get("schema", {})
    ref: str | None = schema.get("$ref")
    return ref.removeprefix("#/components/schemas/") if ref else None


def test_live_routes_answer_with_the_typed_bodies_the_contract_fixed() -> None:
    """The stubs' typed shapes are the built routes' own, and no stub's 501 is left."""
    schema = _app().openapi()
    for (method, template), (takes, success, answers) in LIVE_ROUTES.items():
        operation = schema["paths"][template][method.lower()]
        where = (method, template)
        assert _json_schema(operation.get("requestBody", {}).get("content")) == takes, where
        responses: dict[str, Any] = operation["responses"]
        assert [code for code in responses if code.startswith("2")] == [success], where
        assert _json_schema(responses[success].get("content")) == answers, where
        assert "501" not in responses, where


def test_round_2_routes_answer_with_the_typed_bodies_the_contract_fixed() -> None:
    schema = _app().openapi()
    for (method, template), (takes, success, answers, errors) in R2_ROUTES.items():
        operation = schema["paths"][template][method.lower()]
        where = (method, template)
        assert _json_schema(operation.get("requestBody", {}).get("content")) == takes, where
        responses: dict[str, Any] = operation["responses"]
        assert [code for code in responses if code.startswith("2")] == [success], where
        assert _json_schema(responses[success].get("content")) == answers, where
        problems = {code for code in responses if code.startswith(("4", "5"))}
        assert problems - {"401", "422"} == errors, where
        for code in errors:
            assert _json_schema(responses[code]["content"]) == "ApiProblem", (where, code)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_the_health_check_answers_get_and_head(method: str) -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.request(method, "/api/healthz")
    assert response.status_code == 200


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
