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
- The provider's own spend limit (a 402 `billing_error`, or the workspace usage-limit
  message) is `BudgetExhausted(kind="provider")`, never retried; `on_provider_limit`
  (the guard's `block_provider`) then stops hosted calls before they reach the API
  (3.4). The message match is deliberately narrow, and a listed risk: a reworded
  message falls back to its status's ordinary mapping.

Only this module imports the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from decimal import Decimal
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
from harmony.llm.errors import BadRequest, BudgetExhausted, ModelCallError
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
# The provider's spend limit: a billing error, or the Console's workspace usage limit.
_BILLING_STATUS: Final = 402
_BILLING_ERROR_TYPE: Final = "billing_error"
_USAGE_LIMIT_MESSAGE: Final = re.compile(
    r"you have reached your specified (?:workspace )?api usage limits?", re.IGNORECASE
)

ProviderLimitHook = Callable[[], Awaitable[None]]


class AnthropicModel:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        on_provider_limit: ProviderLimitHook | None = None,
    ) -> None:
        self._client = client
        self._heartbeat_interval = heartbeat_interval_s
        self._on_provider_limit = on_provider_limit

    @classmethod
    def from_settings(
        cls, settings: Settings, *, on_provider_limit: ProviderLimitHook | None = None
    ) -> AnthropicModel:
        if settings.anthropic_api_key is None:
            raise BadRequest("ANTHROPIC_API_KEY is not set; live calls need it")
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=1,
            timeout=anthropic.Timeout(SDK_TIMEOUT_S, connect=SDK_CONNECT_TIMEOUT_S),
        )
        return cls(client, on_provider_limit=on_provider_limit)

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
            if _is_provider_limit(exc):
                await self._provider_limited()
                raise _provider_limit_error(exc) from exc
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

    async def _provider_limited(self) -> None:
        if self._on_provider_limit is None:
            return
        try:
            await self._on_provider_limit()
        except Exception:
            # The call still fails as `provider`; only the local block is lost, and the
            # next call meets the same provider error and tries again.
            log.exception("could not record the provider's spend-limit block")

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


def _is_provider_limit(exc: anthropic.APIStatusError) -> bool:
    if exc.status_code == _BILLING_STATUS or _error_type(exc.body) == _BILLING_ERROR_TYPE:
        return True
    return _USAGE_LIMIT_MESSAGE.search(_error_message(exc)) is not None


def _provider_limit_error(exc: anthropic.APIStatusError) -> BudgetExhausted:
    log.error("the provider's spend limit refused a call: %s %s", exc.status_code, exc.message)
    zero = Decimal(0)
    return BudgetExhausted(
        f"the provider's spend limit refused the call (API error {exc.status_code}): {exc.message}",
        kind="provider",
        budget="provider",
        cap_usd=zero,
        committed_usd=zero,
        requested_usd=zero,
    )


def _status_error(exc: anthropic.APIStatusError, tracker: StreamUsageTracker) -> ModelCallError:
    error_type = _error_type(exc.body)
    message = f"API error {exc.status_code} ({error_type or 'unknown'}): {exc.message}"
    if exc.status_code in _NON_RETRYABLE_STATUS or error_type in _NON_RETRYABLE_ERROR_TYPES:
        return BadRequest(message, partial_usage=tracker.usage, served_model=tracker.served_model)
    return ModelCallError(message, partial_usage=tracker.usage, served_model=tracker.served_model)


def _error_type(body: object) -> str | None:
    return _error_field(body, "type")


def _error_message(exc: anthropic.APIStatusError) -> str:
    return _error_field(exc.body, "message") or exc.message


def _error_field(body: object, name: str) -> str | None:
    if not isinstance(body, Mapping):
        return None
    error = cast(Mapping[str, object], body).get("error")
    if isinstance(error, Mapping):
        value = cast(Mapping[str, object], error).get(name)
        return value if isinstance(value, str) else None
    return None


__all__ = ["HEARTBEAT_INTERVAL_S", "SDK_TIMEOUT_S", "AnthropicModel"]
