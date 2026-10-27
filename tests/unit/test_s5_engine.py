"""Section 5 live-run engine fixes that need a guard of their own, over a fake transport:
nothing leaves the process."""

from __future__ import annotations

import json
from uuid import uuid4

import anthropic
import httpx2
import pytest

from harmony.llm import CallKey
from harmony.llm.anthropic_model import AnthropicModel
from harmony.llm.errors import ModelCallError
from harmony.llm.pricing import HAIKU_5_5
from harmony.llm.requests import build_request
from harmony.temporal.policies import MODEL_NON_RETRYABLE

# The model's tool input, which the SDK can't parse: an unquoted string.
BAD_INPUT = '{"text": Yes, Nov 4 works.}'


def _sse(*events: dict[str, object]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _malformed_tool_call(request: httpx2.Request) -> httpx2.Response:
    body = _sse(
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": HAIKU_5_5,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "send_message",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": BAD_INPUT},
        },
    )
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


async def test_malformed_tool_json_is_a_retryable_model_error() -> None:
    """Bug 4: the SDK's `ValueError` became a non-retryable domain error that failed the
    turn. It is now a `ModelCallError`, retried within the model policy's attempts, and
    the model's text isn't carried into the error Temporal records."""
    client = anthropic.AsyncAnthropic(
        api_key="test-key-not-real",
        base_url="http://anthropic.invalid",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(_malformed_tool_call)),
    )
    key = CallKey(
        world_id=uuid4(),
        scenario="llm-unit",
        org="northlake",
        role="representative",
        thread="t1",
        turn=1,
        turn_id=uuid4(),
        step_idx=0,
        attempt=1,
    )
    request = build_request(
        key,
        tier="featured",
        system="You are Claude for Northlake.",
        messages=[{"role": "user", "content": "Can you do Nov 4?"}],
        max_tokens=1_000,
        model_override=HAIKU_5_5,
    )

    with pytest.raises(ModelCallError) as failed:
        await AnthropicModel(client, heartbeat_interval_s=3600).call(request)

    assert str(failed.value) == "the model wrote tool input that isn't valid JSON"
    assert "ModelCallError" not in MODEL_NON_RETRYABLE
    assert failed.value.__cause__ is None
    assert failed.value.partial_usage is not None
