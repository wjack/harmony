"""The provider's own spend limit (LIVE_DEMO_PLAN 3.4), over a fake transport: nothing
leaves the process.

A 402 `billing_error`, or the workspace usage-limit message, is a non-retryable
`BudgetExhausted(kind="provider")` that blocks hosted calls locally until resumed;
other API errors keep their usual mapping.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import anthropic
import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import Budget, LiveControls, World
from harmony.llm import CallKey, ModelRequest, ModelResult
from harmony.llm.anthropic_model import AnthropicModel
from harmony.llm.budget import BudgetGuard
from harmony.llm.budget_pg import (
    LIVE_SWITCH_KEY,
    HostedBudgets,
    HostedCaps,
    hosted_budget_keys,
    set_live,
)
from harmony.llm.checkpoint import metered_call
from harmony.llm.errors import BadRequest, BudgetExhausted, ModelCallError
from harmony.llm.pricing import OPUS_5_5
from harmony.llm.requests import build_request
from harmony.temporal.policies import MODEL_NON_RETRYABLE

type Sessionmaker = async_sessionmaker[AsyncSession]

USAGE_LIMIT = (
    "You have reached your specified workspace API usage limits. You will regain access "
    "on 2026-11-01 at 00:00 UTC."
)
UNKNOWN_BETA = "Unexpected value(s) `made-up-2026-01-01` for the `anthropic-beta` header."


class FakeAPI:
    """Answers every Messages request with one error, and counts the requests."""

    def __init__(self, status: int, error_type: str, message: str) -> None:
        self.status = status
        self.body = {"type": "error", "error": {"type": error_type, "message": message}}
        self.requests = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        return httpx2.Response(self.status, json=self.body)

    def model(self, guard: BudgetGuard) -> AnthropicModel:
        client = anthropic.AsyncAnthropic(
            api_key="test-key-not-real",
            base_url="http://anthropic.invalid",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )
        return AnthropicModel(
            client, heartbeat_interval_s=3600, on_provider_limit=guard.block_provider
        )


async def _hosted_world(sm: Sessionmaker) -> tuple[UUID, tuple[str, ...]]:
    world_id = uuid4()
    async with sm() as session, session.begin():
        session.add(
            World(
                id=world_id,
                scenario="llm-unit",
                mode="live",
                build_sha="test",
                budget_lineage=uuid4(),
            )
        )
    keys = await hosted_budget_keys(sm, HostedCaps(lineage_usd=Decimal(10)), world_id=world_id)
    return world_id, keys


def _request(world_id: UUID) -> ModelRequest:
    key = CallKey(
        world_id=world_id,
        scenario="llm-unit",
        org="northlake",
        role="internal",
        thread=None,
        turn=1,
        turn_id=uuid4(),
        step_idx=0,
        attempt=1,
    )
    return build_request(
        key,
        tier="featured",
        system="You are Northlake's internal assistant.",
        messages=[{"role": "user", "content": "Can you share the pilot dates?"}],
        max_tokens=1_000,
        model_override=OPUS_5_5,
    )


async def _call(
    api: FakeAPI, guard: BudgetGuard, world_id: UUID, keys: tuple[str, ...]
) -> ModelResult:
    async def keep(result: ModelResult, cost: Decimal) -> ModelResult:
        return result

    return await metered_call(api.model(guard), guard, _request(world_id), keep, budget_keys=keys)


def _guard(sm: Sessionmaker) -> BudgetGuard:
    """The hosted worker's guard: Postgres caps only."""
    return BudgetGuard(
        ledger=None,
        cap_usd=Decimal(200),
        thresholds=(Decimal(200),),
        env="prod",
        hosted=HostedBudgets(sm),
    )


async def _blocked_until(sm: Sessionmaker) -> object:
    async with sm() as session:
        return await session.scalar(
            select(LiveControls.provider_blocked_until).where(LiveControls.key == LIVE_SWITCH_KEY)
        )


@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (402, "billing_error", "Your credit balance is too low to access the API."),
        (400, "invalid_request_error", USAGE_LIMIT),
    ],
    ids=["billing-error", "usage-limit"],
)
async def test_the_provider_limit_blocks_hosted_calls_until_resumed(
    db_sessionmaker: Sessionmaker, status: int, error_type: str, message: str
) -> None:
    sm = db_sessionmaker
    world_id, keys = await _hosted_world(sm)
    guard = _guard(sm)
    api = FakeAPI(status, error_type, message)

    with pytest.raises(BudgetExhausted) as refused:
        await _call(api, guard, world_id, keys)
    assert refused.value.kind == "provider"
    assert type(refused.value).__name__ in MODEL_NON_RETRYABLE
    assert api.requests == 1
    assert await _blocked_until(sm) is not None
    # Nothing was billed, so the reservation was released.
    async with sm() as session:
        lineage = await session.get_one(Budget, keys[0])
    assert (lineage.spent_usd, lineage.reserved_usd) == (Decimal(0), Decimal(0))

    # Refused locally, before any request.
    with pytest.raises(BudgetExhausted) as blocked:
        await _call(api, guard, world_id, keys)
    assert blocked.value.kind == "provider"
    assert api.requests == 1

    resumed = await set_live(sm, enabled=True)
    assert resumed.provider_blocked_until is None
    with pytest.raises(BudgetExhausted):
        await _call(api, guard, world_id, keys)
    assert api.requests == 2


@pytest.mark.parametrize(
    ("status", "error_type", "message", "error"),
    [
        (
            429,
            "rate_limit_error",
            "Number of request tokens has exceeded your rate limit.",
            ModelCallError,
        ),
        (529, "overloaded_error", "Overloaded", ModelCallError),
        (400, "invalid_request_error", UNKNOWN_BETA, BadRequest),
    ],
    ids=["rate-limit", "overloaded", "unknown-beta"],
)
async def test_other_api_errors_keep_their_mapping(
    db_sessionmaker: Sessionmaker,
    status: int,
    error_type: str,
    message: str,
    error: type[ModelCallError],
) -> None:
    sm = db_sessionmaker
    world_id, keys = await _hosted_world(sm)
    guard = _guard(sm)
    api = FakeAPI(status, error_type, message)

    with pytest.raises(ModelCallError) as failed:
        await _call(api, guard, world_id, keys)
    assert type(failed.value) is error
    assert (type(failed.value).__name__ in MODEL_NON_RETRYABLE) == (error is BadRequest)
    assert await _blocked_until(sm) is None

    # Not blocked: the next call reaches the API.
    with pytest.raises(error):
        await _call(api, guard, world_id, keys)
    assert api.requests == 2
