"""`call_model_checkpointed`: reuse, storage round trips and settlement.

No test reaches the network: paid-client behaviour runs `AnthropicModel` against an
httpx2 `MockTransport` (a fake key, a fake host), and every guard that writes uses a
temp ledger.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import anthropic
import httpx2
import pytest
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db import repo
from harmony.db.models import ModelCall
from harmony.llm import (
    CallKey,
    HeartbeatCallback,
    ModelRequest,
    ModelResult,
    ModelRole,
    StreamEventCallback,
)
from harmony.llm.anthropic_model import AnthropicModel
from harmony.llm.budget import BudgetGuard, FreeSpend
from harmony.llm.checkpoint import (
    GuardMismatch,
    call_model_checkpointed,
    load_call,
    metered_call,
)
from harmony.llm.errors import BadRequest, ModelCallError
from harmony.llm.ledger import SpendLedger
from harmony.llm.pricing import OPUS_5_5, call_cost
from harmony.llm.requests import BETA_FALLBACK_DEFAULT, build_request
from harmony.llm.scripted import SCRIPTED_SIGNATURE_PREFIX, ScriptedModel
from harmony.llm.wire import JsonObject, canonical_json

CASSETTES = Path(__file__).parents[1] / "fixtures" / "cassettes"
USER: JsonObject = {"role": "user", "content": "Can you share the pilot dates?"}
SEND_MESSAGE: JsonObject = {
    "name": "send_message",
    "description": "Send a message in the thread.",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
}

type Sessionmaker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def world_id(db_sessionmaker: Sessionmaker) -> UUID:
    world = uuid4()
    async with db_sessionmaker() as session, session.begin():
        repo.add_world(
            session,
            world_id=world,
            scenario="llm-unit",
            mode="live",
            build_sha="test",
            budget_lineage=uuid4(),
        )
    return world


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "spend_ledger.jsonl"
    path.write_text('{"run": "smoke", "model": "claude-opus-5-5", "cost_usd": 0.06}\n')
    return path


@pytest.fixture
def guard(ledger_path: Path) -> BudgetGuard:
    return BudgetGuard(
        ledger=SpendLedger(ledger_path),
        cap_usd=Decimal(200),
        thresholds=(Decimal(50), Decimal(100), Decimal(200)),
        env="test",
    )


def request_for(
    world: UUID,
    *,
    role: ModelRole = "representative",
    thread: str | None = "nl-hl",
    turn_id: UUID | None = None,
) -> ModelRequest:
    key = CallKey(
        world_id=world,
        scenario="llm-unit",
        org="northlake",
        role=role,
        thread=thread,
        turn=1,
        turn_id=turn_id or uuid4(),
        step_idx=0,
        attempt=1,
    )
    return build_request(
        key,
        tier="featured",
        system="You represent Northlake.",
        messages=[USER],
        max_tokens=1_000,
        tools=[SEND_MESSAGE] if role == "representative" else (),
        model_override=OPUS_5_5,
    )


def ledger_calls(path: Path) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [line for line in lines if line.get("project") == "harmony" and line["kind"] == "call"]


async def stored_rows(sessionmaker: Sessionmaker) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(ModelCall)) or 0


class Billable:
    """Stands in for a paid client: replays cassettes, but isn't a `ScriptedModel`."""

    def __init__(self, *, tag: str = "", gate: Callable[[], Awaitable[None]] | None = None):
        self._inner = ScriptedModel.from_paths(CASSETTES, strict_contract=False)
        self._tag = tag
        self._gate = gate
        self.calls = 0

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        self.calls += 1
        if self._gate is not None:
            await self._gate()
        result = await self._inner.call(
            request, on_stream_event=on_stream_event, heartbeat=heartbeat
        )
        if not self._tag:
            return result
        message = json.loads(result.response_text)
        message["id"] = self._tag
        return result.model_copy(update={"response_text": canonical_json(message)})


class Exploding:
    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        raise AssertionError("a stored call must not be made again")


# --- Reuse ------------------------------------------------------------------------------


async def test_a_stored_call_is_reused_without_calling(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    model = ScriptedModel.from_paths(CASSETTES)
    request = request_for(world_id)
    first = await call_model_checkpointed(db_sessionmaker, model, FreeSpend(), request)
    assert (first.reused, model.call_count) == (False, 1)
    assert first.request_text == canonical_json(request.body)

    # A retried activity (a fresh process, so a fresh client) finds the row.
    again = await call_model_checkpointed(db_sessionmaker, Exploding(), FreeSpend(), request)
    assert again.reused
    assert (again.response_text, again.served_model) == (first.response_text, first.served_model)
    assert await stored_rows(db_sessionmaker) == 1


async def test_a_stored_call_belongs_to_its_world(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    request = request_for(world_id)
    await call_model_checkpointed(
        db_sessionmaker, ScriptedModel.from_paths(CASSETTES), FreeSpend(), request
    )
    elsewhere = request.model_copy(
        update={"key": request.key.model_copy(update={"world_id": uuid4()})}
    )
    with pytest.raises(ValueError, match="belongs to world"):
        await call_model_checkpointed(db_sessionmaker, Exploding(), FreeSpend(), elsewhere)


async def test_empty_thinking_keeps_its_signature_through_store_and_load(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    request = request_for(world_id)
    call = await call_model_checkpointed(
        db_sessionmaker, ScriptedModel.from_paths(CASSETTES), FreeSpend(), request
    )
    loaded = await load_call(db_sessionmaker, request.key.turn_id, 0)
    assert loaded is not None
    assert loaded.response_text == call.response_text  # byte for byte
    thinking = loaded.message()["content"][0]
    assert thinking["type"] == "thinking"
    assert thinking["thinking"] == ""
    assert thinking["signature"].startswith(SCRIPTED_SIGNATURE_PREFIX)
    assert loaded.stop_reason == "tool_use"
    assert loaded.cost_usd == 0


async def test_concurrent_attempts_keep_the_first_stored_response(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    arrived: list[None] = []
    both_in = asyncio.Event()

    async def gate() -> None:
        arrived.append(None)
        if len(arrived) == 2:
            both_in.set()
        await both_in.wait()

    request = request_for(world_id)
    first, second = Billable(tag="msg_first", gate=gate), Billable(tag="msg_second", gate=gate)
    a, b = await asyncio.gather(
        call_model_checkpointed(db_sessionmaker, first, guard, request),
        call_model_checkpointed(db_sessionmaker, second, guard, request),
    )
    assert sorted([a.reused, b.reused]) == [False, True]
    assert a.response_text == b.response_text
    assert await stored_rows(db_sessionmaker) == 1
    # Both calls were made, so both were billed.
    assert len(ledger_calls(ledger_path)) == 2
    assert guard.in_flight_usd == 0


# --- Spend guard pairing -----------------------------------------------------------------


async def test_clients_must_run_with_their_guard(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    with pytest.raises(GuardMismatch):
        await call_model_checkpointed(
            db_sessionmaker, ScriptedModel.from_paths(CASSETTES), guard, request_for(world_id)
        )
    with pytest.raises(GuardMismatch):
        await call_model_checkpointed(
            db_sessionmaker, Billable(), FreeSpend(), request_for(world_id)
        )
    assert ledger_calls(ledger_path) == []


async def test_a_paid_call_is_settled_at_its_priced_cost(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    request = request_for(world_id)
    call = await call_model_checkpointed(db_sessionmaker, Billable(), guard, request)
    expected = call_cost(call.usage, requested_model=OPUS_5_5, served_model=OPUS_5_5)
    assert call.cost_usd == expected > 0
    [line] = ledger_calls(ledger_path)
    assert line["cost_usd"] == float(expected)
    assert line["turn_id"] == str(request.key.turn_id)
    assert guard.in_flight_usd == 0


# --- Failures are settled from what they billed ------------------------------------------


async def test_settlement_happens_even_when_storing_fails(
    guard: BudgetGuard, ledger_path: Path
) -> None:
    async def store(result: ModelResult, cost: Decimal) -> None:
        raise RuntimeError("database went away")

    with pytest.raises(RuntimeError):
        await metered_call(Billable(), guard, request_for(uuid4()), store)
    [line] = ledger_calls(ledger_path)
    assert line["cost_usd"] > 0  # pyright: ignore[reportOperatorIssue]
    assert "partial" not in line
    assert guard.in_flight_usd == 0


async def test_a_cancelled_call_settles_what_streamed_before_it(
    guard: BudgetGuard, ledger_path: Path
) -> None:
    started = asyncio.Event()

    class Hangs:
        async def call(
            self,
            request: ModelRequest,
            *,
            on_stream_event: StreamEventCallback | None = None,
            heartbeat: HeartbeatCallback | None = None,
        ) -> ModelResult:
            assert on_stream_event is not None
            await on_stream_event(_message_start(input_tokens=100_000))
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def store(result: ModelResult, cost: Decimal) -> None:
        return None

    task = asyncio.create_task(metered_call(Hangs(), guard, request_for(uuid4()), store))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)  # the shielded settlement finishes in the background
    [line] = ledger_calls(ledger_path)
    assert line["partial"] is True
    # 100K input at $4; the output it generated was never reported, so max_tokens
    # (1,000) of it at $20.
    assert line["cost_usd"] == 0.42
    assert guard.in_flight_usd == 0


# --- AnthropicModel over a mock transport ------------------------------------------------


def _message_start(*, input_tokens: int, model: str = OPUS_5_5) -> dict[str, JsonValue]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 1,
            },
        },
    }


def _sse(*events: Mapping[str, JsonValue]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


EMPTY_THINKING_STREAM = _sse(
    _message_start(input_tokens=120),
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "EqQBCkYIBxgCKkBmock"},
    },
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "text_delta", "text": "pong"},
    },
    {"type": "content_block_stop", "index": 1},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 42},
    },
    {"type": "message_stop"},
)


class MockAPI:
    """Serves canned Messages API responses and records what was sent."""

    def __init__(self, status: int, body: bytes, content_type: str = "text/event-stream"):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(
            self.status, headers={"content-type": self.content_type}, content=self.body
        )

    def model(self) -> AnthropicModel:
        client = anthropic.AsyncAnthropic(
            api_key="test-key-not-real",
            base_url="http://anthropic.invalid",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )
        return AnthropicModel(client, heartbeat_interval_s=3600)


async def test_anthropic_model_keeps_an_empty_thinking_signature(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    api = MockAPI(200, EMPTY_THINKING_STREAM)
    request = request_for(world_id, role="internal", thread=None)
    events: list[Mapping[str, JsonValue]] = []
    beats: list[None] = []

    async def on_event(event: Mapping[str, JsonValue]) -> None:
        events.append(event)

    call = await call_model_checkpointed(
        db_sessionmaker,
        api.model(),
        guard,
        request,
        on_stream_event=on_event,
        heartbeat=lambda: beats.append(None),
    )

    # What went over the (mock) wire: the builder's body, streamed, with its betas.
    [sent] = api.requests
    assert sent.url.path == "/v1/messages"
    assert BETA_FALLBACK_DEFAULT in sent.headers["anthropic-beta"]
    wire = json.loads(sent.content)
    assert wire["stream"] is True
    for field in ("model", "max_tokens", "system", "messages", "thinking", "output_config"):
        assert wire[field] == request.body[field]
    assert wire["fallbacks"] == "default"

    loaded = await load_call(db_sessionmaker, request.key.turn_id, 0)
    assert loaded is not None
    thinking, text = loaded.message()["content"]
    assert thinking == {"type": "thinking", "thinking": "", "signature": "EqQBCkYIBxgCKkBmock"}
    assert text["text"] == "pong"
    assert loaded.stop_reason == "end_turn"
    assert loaded.usage["input_tokens"] == 120
    assert loaded.usage["output_tokens"] == 42

    # 120 x $4 + 42 x $20 per million
    assert call.cost_usd == Decimal("0.00132")
    [line] = ledger_calls(ledger_path)
    assert (line["cost_usd"], line["output_tokens"]) == (0.00132, 42)
    assert events[0]["type"] == "message_start"
    assert len(beats) == len(events) == 9


async def test_a_stream_that_fails_midway_settles_its_partial_usage(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    body = _sse(
        _message_start(input_tokens=250_000),
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    api = MockAPI(200, body)
    request = request_for(world_id, role="internal", thread=None)
    with pytest.raises(ModelCallError) as caught:
        await call_model_checkpointed(db_sessionmaker, api.model(), guard, request)
    assert not isinstance(caught.value, BadRequest)  # retryable
    assert caught.value.partial_usage is not None
    [line] = ledger_calls(ledger_path)
    assert line["partial"] is True
    assert line["cost_usd"] == 1.02  # 250K input at $4, plus max_tokens (1,000) output at $20
    assert await stored_rows(db_sessionmaker) == 0
    assert guard.in_flight_usd == 0


def _text_block_with(text: str) -> list[dict[str, JsonValue]]:
    return [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
    ]


OVERLOADED: dict[str, JsonValue] = {
    "type": "error",
    "error": {"type": "overloaded_error", "message": "Overloaded"},
}


async def test_output_streamed_before_a_break_is_never_billed_as_one_token(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    """`message_start` reports `output_tokens: 1`; the text generated after it is billed
    by the API but only reported at the end, which this stream never reached."""
    body = _sse(_message_start(input_tokens=120), *_text_block_with("word " * 700), OVERLOADED)
    request = request_for(world_id, role="internal", thread=None)
    with pytest.raises(ModelCallError):
        await call_model_checkpointed(db_sessionmaker, MockAPI(200, body).model(), guard, request)
    [line] = ledger_calls(ledger_path)
    assert line["partial"] is True
    # 120 input at $4, plus max_tokens (1,000) output at $20 per million: an upper
    # bound on the ~700 tokens it streamed, where the reported usage says 1.
    assert line["cost_usd"] == 0.02048
    assert guard.in_flight_usd == 0


async def test_a_break_after_the_usage_report_settles_the_reported_usage(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    body = _sse(
        _message_start(input_tokens=120),
        *_text_block_with("pong"),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
        OVERLOADED,
    )
    request = request_for(world_id, role="internal", thread=None)
    with pytest.raises(ModelCallError):
        await call_model_checkpointed(db_sessionmaker, MockAPI(200, body).model(), guard, request)
    [line] = ledger_calls(ledger_path)
    assert (line["partial"], line["cost_usd"]) == (True, 0.00132)  # 120 x $4 + 42 x $20


async def test_a_rejected_request_bills_nothing_and_is_not_retryable(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    api = MockAPI(400, json.dumps(error).encode(), content_type="application/json")
    request = request_for(world_id, role="internal", thread=None)
    with pytest.raises(BadRequest):
        await call_model_checkpointed(db_sessionmaker, api.model(), guard, request)
    assert ledger_calls(ledger_path) == []
    assert guard.in_flight_usd == 0


async def test_a_stream_that_ends_before_message_stop_is_a_failed_call(
    db_sessionmaker: Sessionmaker, world_id: UUID, guard: BudgetGuard, ledger_path: Path
) -> None:
    """A connection closed mid-response (no error event, no `message_stop`): the
    accumulated snapshot is a fragment, so it is neither stored nor priced as complete."""
    body = _sse(
        _message_start(input_tokens=120), *_text_block_with("Yes, we can meet Nov 6 at $1,1")
    )
    request = request_for(world_id, role="internal", thread=None)
    with pytest.raises(ModelCallError) as caught:
        await call_model_checkpointed(db_sessionmaker, MockAPI(200, body).model(), guard, request)
    assert not isinstance(caught.value, BadRequest)  # retryable
    assert await stored_rows(db_sessionmaker) == 0
    [line] = ledger_calls(ledger_path)
    assert (line["partial"], line["cost_usd"]) == (True, 0.02048)
    assert guard.in_flight_usd == 0
