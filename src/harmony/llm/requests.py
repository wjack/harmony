"""Request builders: one Messages API body per call, built for the *resolved* model.

In development `MODEL_OVERRIDE` runs every role on Opus 5.5, so the rules key on the
model actually sent, not on the role (ARCHITECTURE.md §5). `llm/contract.py` checks
the same rules over recorded requests.

Every model:
- no sampling parameters, and no forced `tool_choice` (always `auto`; representatives
  add `disable_parallel_tool_use`);
- an explicit effort per role (`output_config.effort`);
- client tools get `eager_input_streaming` when the turn streams to the UI;
- one cache breakpoint on the frozen system prompt, plus top-level automatic caching
  for the growing tail.

Opus 5.5: `thinking: {type: "adaptive"}`, never disabled or budgeted (both 400), with
`display: "updates"` and its beta when the turn streams to the UI; server-side
`fallbacks: "default"` and its beta.

Haiku 5.5: no `fallbacks` (it has no server-side fallback); thinking `disabled` only
when the role wants it off and effort is `high` or lower, otherwise adaptive.

Because the system prompt, tools and thinking settings are part of the cached prefix
(and of what thinking blocks are bound to), a (thread, side) must keep `tier`,
`stream_to_ui` and `prefix_mismatch` fixed for its whole life.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

from pydantic import JsonValue

from harmony.llm import CallKey, ModelRequest, ModelRole
from harmony.llm.errors import BadRequest
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5
from harmony.llm.wire import JsonObject

Effort = Literal["low", "medium", "high", "xhigh", "max"]
# Featured orgs run on Opus; background orgs on Haiku (§14).
Tier = Literal["featured", "background"]
# `thinking.block_binding.prefix_mismatch_behavior`: dev sends "error" so a broken
# prefix fails loudly (§2.2).
PrefixMismatch = Literal["error", "drop_block"]

BETA_FALLBACK_DEFAULT: Final = "server-side-fallback-2026-07-01"
BETA_DISPLAY_UPDATES: Final = "thinking-display-updates-2026-08-18"
BETA_THINKING_BINDING: Final = "thinking-binding-controls-2026-08-01"

# Haiku 5.5 accepts `thinking: {type: "disabled"}` only at these efforts.
HAIKU_DISABLE_THINKING_EFFORTS: Final = frozenset[str](("low", "medium", "high"))
MAX_OUTPUT_TOKENS: Final = 128_000
EFFORTS: Final = frozenset[str](get_args(Effort))


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """A role's production model and effort (§5). `thinking_off` is a preference the
    builder honours only where the resolved model allows it."""

    model: str
    effort: Effort
    thinking_off: bool = False


ROLE_PROFILES: Final[Mapping[tuple[ModelRole, Tier], RoleProfile]] = MappingProxyType(
    {
        ("internal", "featured"): RoleProfile(OPUS_5_5, "medium"),
        # A background org's chat is the visitor's side cast, not the story (3.1).
        ("internal", "background"): RoleProfile(HAIKU_5_5, "medium"),
        ("representative", "featured"): RoleProfile(OPUS_5_5, "medium"),
        ("representative", "background"): RoleProfile(HAIKU_5_5, "low"),
        ("answerer", "featured"): RoleProfile(OPUS_5_5, "medium"),
        ("answerer", "background"): RoleProfile(HAIKU_5_5, "medium"),
        # Single no-tools calls the size of the answerer's (EDIT.md §7).
        ("steer", "featured"): RoleProfile(OPUS_5_5, "medium"),
        ("steer", "background"): RoleProfile(HAIKU_5_5, "medium"),
        ("sharing_check", "featured"): RoleProfile(HAIKU_5_5, "low"),
        ("sharing_check", "background"): RoleProfile(HAIKU_5_5, "low"),
        ("synthesizer", "featured"): RoleProfile(OPUS_5_5, "high"),
        ("synthesizer", "background"): RoleProfile(OPUS_5_5, "high"),
        ("screener", "featured"): RoleProfile(HAIKU_5_5, "low", thinking_off=True),
        ("screener", "background"): RoleProfile(HAIKU_5_5, "low", thinking_off=True),
    }
)


def resolve_model(role: ModelRole, tier: Tier, model_override: str | None) -> str:
    """The model a call is sent to: `MODEL_OVERRIDE` when set, else the role's model."""
    return model_override or ROLE_PROFILES[(role, tier)].model


@dataclass(frozen=True, slots=True)
class _Plan:
    """What the per-model rules decide for one call."""

    model: str
    role: ModelRole
    effort: Effort
    thinking_off: bool
    stream_to_ui: bool
    prefix_mismatch: PrefixMismatch | None


@dataclass(frozen=True, slots=True)
class _ModelParts:
    thinking: JsonObject
    fallbacks: JsonValue | None
    betas: frozenset[str]


def _opus_5_5(plan: _Plan) -> _ModelParts:
    thinking: JsonObject = {"type": "adaptive"}
    betas = {BETA_FALLBACK_DEFAULT}
    if plan.stream_to_ui:
        thinking["display"] = "updates"
        betas.add(BETA_DISPLAY_UPDATES)
    if plan.prefix_mismatch is not None:
        thinking["block_binding"] = {"prefix_mismatch_behavior": plan.prefix_mismatch}
        betas.add(BETA_THINKING_BINDING)
    return _ModelParts(thinking=thinking, fallbacks="default", betas=frozenset(betas))


def _haiku_5_5(plan: _Plan) -> _ModelParts:
    if plan.thinking_off and plan.effort in HAIKU_DISABLE_THINKING_EFFORTS:
        # block_binding is rejected with disabled thinking; the history stays
        # append-only regardless.
        return _ModelParts(thinking={"type": "disabled"}, fallbacks=None, betas=frozenset())
    thinking: JsonObject = {"type": "adaptive"}
    betas: set[str] = set()
    if plan.prefix_mismatch is not None:
        thinking["block_binding"] = {"prefix_mismatch_behavior": plan.prefix_mismatch}
        betas.add(BETA_THINKING_BINDING)
    return _ModelParts(thinking=thinking, fallbacks=None, betas=frozenset(betas))


_BUILDERS: Final[Mapping[str, Callable[[_Plan], _ModelParts]]] = MappingProxyType(
    {OPUS_5_5: _opus_5_5, HAIKU_5_5: _haiku_5_5}
)
SUPPORTED_MODELS: Final = frozenset(_BUILDERS)


def is_client_tool(tool: Mapping[str, JsonValue]) -> bool:
    """User-defined tools have no `type`, or `custom`; server tools have their own."""
    return tool.get("type") in (None, "custom")


def build_request(
    key: CallKey,
    *,
    tier: Tier,
    system: str | None,
    messages: Sequence[JsonObject],
    max_tokens: int,
    tools: Sequence[JsonObject] = (),
    stream_to_ui: bool = False,
    output_schema: JsonObject | None = None,
    effort: Effort | None = None,
    model_override: str | None = None,
    prefix_mismatch: PrefixMismatch | None = None,
) -> ModelRequest:
    """Build the request for `key.role` at `tier`, for the resolved model.

    `system` is the frozen system prompt (exact bytes from `rep_sessions` for a
    representative). `messages` is the transcript as it will be sent, mid-conversation
    `role: "system"` notices included. `effort` overrides the role's default.
    """
    if not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
        raise BadRequest(f"max_tokens must be 1..{MAX_OUTPUT_TOKENS}, got {max_tokens}")
    profile = ROLE_PROFILES[(key.role, tier)]
    model = model_override or profile.model
    builder = _BUILDERS.get(model)
    if builder is None:
        supported = ", ".join(sorted(SUPPORTED_MODELS))
        raise BadRequest(f"no request builder for model {model!r} (supported: {supported})")
    plan = _Plan(
        model=model,
        role=key.role,
        effort=effort or profile.effort,
        thinking_off=profile.thinking_off,
        stream_to_ui=stream_to_ui,
        prefix_mismatch=prefix_mismatch,
    )
    parts = builder(plan)

    body: JsonObject = {"model": model, "max_tokens": max_tokens}
    if system:
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    body["messages"] = list[JsonValue](messages)
    if tools:
        body["tools"] = [_tool_entry(tool, eager=stream_to_ui) for tool in tools]
        tool_choice: JsonObject = {"type": "auto"}
        if key.role == "representative":
            # At most one tool call per turn: a consult must end the turn alone (§2.1).
            tool_choice["disable_parallel_tool_use"] = True
        body["tool_choice"] = tool_choice
    body["thinking"] = parts.thinking
    output_config: JsonObject = {"effort": plan.effort}
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    body["output_config"] = output_config
    if parts.fallbacks is not None:
        body["fallbacks"] = parts.fallbacks
    body["cache_control"] = {"type": "ephemeral"}
    return ModelRequest(key=key, body=body, betas=tuple(sorted(parts.betas)))


def production_body(body: Mapping[str, JsonValue], role: ModelRole, tier: Tier) -> JsonObject:
    """`body` as production would send it, for re-pricing a call made under
    `MODEL_OVERRIDE` (3.14): the role's production model and, where that model has one,
    its server-side fallback hop. Only what pricing reads changes; the messages and
    `max_tokens` stay as sent."""
    profile = ROLE_PROFILES[(role, tier)]
    parts = _BUILDERS[profile.model](
        _Plan(
            model=profile.model,
            role=role,
            effort=profile.effort,
            thinking_off=profile.thinking_off,
            stream_to_ui=False,
            prefix_mismatch=None,
        )
    )
    repriced: JsonObject = {key: value for key, value in body.items() if key != "fallbacks"}
    repriced["model"] = profile.model
    if parts.fallbacks is not None:
        repriced["fallbacks"] = parts.fallbacks
    return repriced


def _tool_entry(tool: JsonObject, *, eager: bool) -> JsonObject:
    entry = dict(tool)
    if eager and is_client_tool(entry):
        entry["eager_input_streaming"] = True
    return entry


__all__ = [
    "BETA_DISPLAY_UPDATES",
    "BETA_FALLBACK_DEFAULT",
    "BETA_THINKING_BINDING",
    "EFFORTS",
    "HAIKU_DISABLE_THINKING_EFFORTS",
    "ROLE_PROFILES",
    "SUPPORTED_MODELS",
    "Effort",
    "PrefixMismatch",
    "RoleProfile",
    "Tier",
    "build_request",
    "is_client_tool",
    "production_body",
    "resolve_model",
]
