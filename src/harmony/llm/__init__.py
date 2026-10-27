"""The model client interface. The API never calls a model; activities do (§3, §5).

Implementations: `llm/anthropic_model.py` (the real beta client, live mode only) and
`llm/scripted.py` (`ScriptedModel`, cassettes, $0). Callers never use a client
directly: they go through `llm/checkpoint.call_model_checkpointed`, which reuses a
stored `model_calls` row for the same (turn_id, step_idx) and wraps the call in a
spend guard (`llm/budget.spend_guard`: `BudgetGuard` live, `FreeSpend` scripted).
Requests come from `llm/requests.build_request`, which applies the per-model rules.

This package's `__init__` imports only pydantic, so `harmony spend` stays light; only
`llm/anthropic_model.py` imports the Anthropic SDK.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

# The roles in ARCHITECTURE.md §5: `answerer` is the consult answerer and router,
# `sharing_check` judges a draft answer against the org's sharing description(s), and
# `steer` turns a member's Edit into guidance and rewrites a held message (EDIT.md §7).
ModelRole = Literal[
    "internal", "representative", "answerer", "sharing_check", "synthesizer", "screener", "steer"
]

# Receives each raw Messages API stream event (`message_start`, `content_block_delta`,
# ...) as parsed JSON, in order. Awaited inline, so keep it fast (enqueue and return).
StreamEventCallback = Callable[[Mapping[str, JsonValue]], Awaitable[None]]
# Called periodically while a call is in flight; activities pass `activity.heartbeat`.
HeartbeatCallback = Callable[[], None]


class CallKey(BaseModel):
    """Identifies one model call.

    (`turn_id`, `step_idx`) is the checkpoint key in `model_calls`. The other fields key
    `ScriptedModel` cassettes (scenario, org, role, thread, turn, step) and fill the
    spend-ledger line.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    world_id: UUID
    scenario: str
    org: str = Field(description="Org slug.")
    role: ModelRole
    thread: str | None = Field(
        description="Seeded thread slug, else str(thread_id); None outside threads."
    )
    turn: int = Field(ge=1, description="1-based turn number within the thread or chat.")
    turn_id: UUID
    step_idx: int = Field(ge=0, description="0-based step within the turn's tool loop.")
    attempt: int = Field(ge=1, description="The Temporal activity attempt.")


class ModelRequest(BaseModel):
    """One Messages API call, already built for the resolved model (`llm/requests.py`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: CallKey
    body: dict[str, JsonValue] = Field(
        description="The exact request body: model (after MODEL_OVERRIDE), max_tokens, "
        "system, messages, tools, thinking, output_config, ..."
    )
    betas: tuple[str, ...] = ()


class ModelResult(BaseModel):
    """The final message of one call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    response_text: str = Field(
        description="The raw final message JSON exactly as received (no exclude_none or "
        "exclude_defaults), so empty thinking blocks keep their signatures. Stored as "
        "text in model_calls.response_text."
    )
    served_model: str = Field(description="response.model, which may differ on fallback.")
    stop_reason: str | None
    usage: dict[str, JsonValue] = Field(
        description="The raw usage object, including `iterations` when present."
    )
    refusal_category: str | None = Field(
        default=None,
        description="stop_details.category when stop_reason is 'refusal' (informational; "
        "may be None even on a refusal).",
    )


class ModelClient(Protocol):
    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        """Make one call and return its final message.

        - Streams internally; `on_stream_event` sees every raw stream event.
        - No retries beyond the SDK's `max_retries=1`; Temporal owns retrying.
        - `stop_reason == "refusal"` is returned, not raised; callers check it before
          reading content.
        - If the call fails after billing started, the raised error carries the partial
          usage from the last `message_delta` (`llm/errors.py`), so the caller can
          settle what was spent before re-raising.
        """
        ...


__all__ = [
    "CallKey",
    "HeartbeatCallback",
    "ModelClient",
    "ModelRequest",
    "ModelResult",
    "ModelRole",
    "StreamEventCallback",
]
