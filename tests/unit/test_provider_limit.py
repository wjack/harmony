"""The provider's own spend limit (LIVE_DEMO_PLAN 3.4), over a fake transport: nothing
leaves the process.

A 402 `billing_error`, or the workspace usage-limit message, is a non-retryable
`BudgetExhausted(kind="provider")` that blocks live calls locally until resumed;
other API errors keep their usual mapping.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import anthropic
import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import LiveControls
from harmony.llm import CallKey, ModelRequest, ModelResult
from harmony.llm.anthropic_model import AnthropicModel
from harmony.llm.budget import BudgetGuard
from harmony.llm.checkpoint import metered_call
from harmony.llm.errors import BadRequest, BudgetExhausted, ModelCallError
from harmony.llm.live_switch import LIVE_SWITCH_KEY, LiveSwitch, set_live
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


def _request() -> ModelRequest:
    key = CallKey(
        world_id=uuid4(),
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


async def _call(api: FakeAPI, guard: BudgetGuard) -> ModelResult:
    async def keep(result: ModelResult, cost: Decimal) -> ModelResult:
        return result

    return await metered_call(api.model(guard), guard, _request(), keep)


def _guard(sm: Sessionmaker) -> BudgetGuard:
    """The hosted worker's guard: the live switch, and no ledger."""
    return BudgetGuard(
        ledger=None,
        cap_usd=Decimal(200),
        thresholds=(Decimal(200),),
        env="prod",
        switch=LiveSwitch(sm),
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
async def test_the_provider_limit_blocks_live_calls_until_resumed(
    db_sessionmaker: Sessionmaker, status: int, error_type: str, message: str
) -> None:
    sm = db_sessionmaker
    guard = _guard(sm)
    api = FakeAPI(status, error_type, message)

    with pytest.raises(BudgetExhausted) as refused:
        await _call(api, guard)
    assert refused.value.kind == "provider"
    assert type(refused.value).__name__ in MODEL_NON_RETRYABLE
    assert api.requests == 1
    assert await _blocked_until(sm) is not None

    # Refused locally, before any request.
    with pytest.raises(BudgetExhausted) as blocked:
        await _call(api, guard)
    assert blocked.value.kind == "provider"
    assert api.requests == 1

    resumed = await set_live(sm, enabled=True)
    assert resumed.provider_blocked_until is None
    with pytest.raises(BudgetExhausted):
        await _call(api, guard)
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
    guard = _guard(sm)
    api = FakeAPI(status, error_type, message)

    with pytest.raises(ModelCallError) as failed:
        await _call(api, guard)
    assert type(failed.value) is error
    assert (type(failed.value).__name__ in MODEL_NON_RETRYABLE) == (error is BadRequest)
    assert await _blocked_until(sm) is None

    # Not blocked: the next call reaches the API.
    with pytest.raises(error):
        await _call(api, guard)
    assert api.requests == 2
