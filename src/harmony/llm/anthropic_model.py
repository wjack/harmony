"""The live model client: the Anthropic SDK's beta client, streaming every call.

- `AsyncAnthropic(max_retries=1)`; Temporal owns retrying. The SDK timeout is kept
  under half the model activity's `start_to_close` (600 s), so both SDK attempts fit
  in one activity attempt.
- Every call streams, so the activity can heartbeat while it runs (a background
  ticker covers long silent thinking) and so a failure can be settled from the usage
  billed so far.
- A stream that ends without `message_stop` (the connection closed mid-response, no
  error event) is a failed, retryable call: the SDK's accumulated snapshot is a
  fragment whose usage never reported its output tokens.
- The final message is stored exactly as accumulated: no `exclude_none`, no
  `exclude_defaults`, so an empty thinking block keeps its empty text and signature.
- `stop_reason == "refusal"` is checked before anything reads the content; the
  category is logged and returned. A served model that differs from the requested
  one (a server-side fallback) is logged, not raised.

Only this module imports the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Final, cast

import anthropic
from anthropic.types.beta import BetaMessageParam
from pydantic import JsonValue

from harmony.config import Settings
from harmony.llm import (
    HeartbeatCallback,
    ModelRequest,
    ModelResult,
    StreamEventCallback,
)
from harmony.llm.errors import BadRequest, ModelCallError
from harmony.llm.usage import StreamUsageTracker
from harmony.llm.wire import JsonObject, as_int, as_str

log = logging.getLogger(__name__)

SDK_TIMEOUT_S: Final = 280.0
SDK_CONNECT_TIMEOUT_S: Final = 10.0
HEARTBEAT_INTERVAL_S: Final = 10.0
# Raw Messages API events forwarded as-is; the SDK's derived events are dropped and
# its enriched stop events are reduced to their raw shape.
_RAW_EVENT_TYPES: Final = frozenset(
    {"message_start", "message_delta", "content_block_start", "content_block_delta"}
)
# Statuses where the same request will fail again.
_NON_RETRYABLE_STATUS: Final = frozenset({400, 401, 403, 404, 413, 422})
_NON_RETRYABLE_ERROR_TYPES: Final = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
    }
)


class AnthropicModel:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._client = client
        self._heartbeat_interval = heartbeat_interval_s

    @classmethod
    def from_settings(cls, settings: Settings) -> AnthropicModel:
        if settings.anthropic_api_key is None:
            raise BadRequest("ANTHROPIC_API_KEY is not set; live calls need it")
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=1,
            timeout=anthropic.Timeout(SDK_TIMEOUT_S, connect=SDK_CONNECT_TIMEOUT_S),
        )
        return cls(client)

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        body = dict(request.body)
        model = as_str(body.pop("model", None))
        max_tokens = as_int(body.pop("max_tokens", None))
        messages = body.pop("messages", None)
        if model is None or max_tokens is None or not isinstance(messages, list):
            raise BadRequest("request body needs model, max_tokens and messages")
        tracker = StreamUsageTracker()
        ticker = asyncio.create_task(self._tick(heartbeat)) if heartbeat is not None else None
        try:
            async with self._client.beta.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=cast(Iterable[BetaMessageParam], messages),
                betas=list(request.betas) if request.betas else anthropic.omit,
                # Everything else goes on the wire exactly as the builder made it.
                extra_body=body,
            ) as stream:
                completed = False
                async for event in stream:
                    raw = _raw_event(event)
                    if raw is None:
                        continue
                    completed = completed or raw.get("type") == "message_stop"
                    tracker.observe(raw)
                    if heartbeat is not None:
                        heartbeat()
                    if on_stream_event is not None:
                        await on_stream_event(raw)
                if not completed:
                    raise ModelCallError(
                        "the response stream ended before message_stop",
                        partial_usage=tracker.usage,
                        served_model=tracker.served_model,
                    )
                final = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise _status_error(exc, tracker) from exc
        except anthropic.APIConnectionError as exc:
            raise ModelCallError(
                f"connection to the API failed: {exc}",
                partial_usage=tracker.usage,
                served_model=tracker.served_model,
            ) from exc
        except anthropic.AnthropicError as exc:
            raise ModelCallError(
                f"model call failed: {exc}",
                partial_usage=tracker.usage,
                served_model=tracker.served_model,
            ) from exc
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

        # Refusal first: nothing below reads content.
        refusal_category: str | None = None
        if final.stop_reason == "refusal":
            refusal_category = final.stop_details.category if final.stop_details else None
            log.warning(
                "model refused (category=%s) turn=%s step=%s",
                refusal_category,
                request.key.turn_id,
                request.key.step_idx,
            )
        if final.model != model:
            log.info("served by %s instead of %s (server-side fallback)", final.model, model)
        response_text = final.to_json(
            indent=None,
            use_api_names=True,
            exclude_unset=True,
            exclude_none=False,
            exclude_defaults=False,
        )
        usage = cast(JsonObject, json.loads(final.usage.to_json(indent=None, exclude_unset=True)))
        return ModelResult(
            response_text=response_text,
            served_model=final.model,
            stop_reason=final.stop_reason,
            usage=usage,
            refusal_category=refusal_category,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def _tick(self, heartbeat: HeartbeatCallback) -> None:
        # Adaptive thinking with omitted display can stream nothing for minutes.
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            heartbeat()


def _raw_event(event: object) -> Mapping[str, JsonValue] | None:
    kind = getattr(event, "type", None)
    if kind in _RAW_EVENT_TYPES:
        to_dict = getattr(event, "to_dict")  # noqa: B009 - SDK models expose to_dict
        return cast(Mapping[str, JsonValue], to_dict(mode="json"))
    if kind == "content_block_stop":
        return {"type": "content_block_stop", "index": cast(int, getattr(event, "index"))}  # noqa: B009
    if kind == "message_stop":
        return {"type": "message_stop"}
    return None


def _status_error(exc: anthropic.APIStatusError, tracker: StreamUsageTracker) -> ModelCallError:
    error_type = _error_type(exc.body)
    message = f"API error {exc.status_code} ({error_type or 'unknown'}): {exc.message}"
    if exc.status_code in _NON_RETRYABLE_STATUS or error_type in _NON_RETRYABLE_ERROR_TYPES:
        return BadRequest(message, partial_usage=tracker.usage, served_model=tracker.served_model)
    return ModelCallError(message, partial_usage=tracker.usage, served_model=tracker.served_model)


def _error_type(body: object) -> str | None:
    if not isinstance(body, Mapping):
        return None
    error = cast(Mapping[str, object], body).get("error")
    if isinstance(error, Mapping):
        kind = cast(Mapping[str, object], error).get("type")
        return kind if isinstance(kind, str) else None
    return None


__all__ = ["HEARTBEAT_INTERVAL_S", "SDK_TIMEOUT_S", "AnthropicModel"]
