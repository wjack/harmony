"""The request contract, checked over recorded requests at no cost (§15 prompt-shape tests).

Per request:
- message order: the first message is `user`; the last is not `assistant` (no
  prefill); every `role: "system"` message follows a user message (or an assistant
  message ending in server-tool use) and is last or followed by an assistant message,
  and carries text only;
- per model: no sampling parameters anywhere; no forced `tool_choice`; no thinking
  budget; Opus 5.5 never disables thinking; Haiku 5.5 sends no `fallbacks` and
  disables thinking only at effort `high` or lower;
- every beta feature used carries its beta header;
- representatives send `disable_parallel_tool_use`; `eager_input_streaming` only on
  client tools.

Across requests: consecutive requests of one conversation (a representative's
(thread, side), or an internal chat) keep `model`, `system`, `tools`, `thinking` and
`output_config` byte-identical, and each request's `messages` is a byte-prefix
extension of the previous one in canonical JSON. Cache markers are compared too: the
builders use top-level automatic caching, so a moving marker is itself a defect.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from harmony.errors import HarmonyError
from harmony.llm import ModelRequest
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5
from harmony.llm.requests import (
    BETA_DISPLAY_UPDATES,
    BETA_FALLBACK_DEFAULT,
    BETA_THINKING_BINDING,
    HAIKU_DISABLE_THINKING_EFFORTS,
    SUPPORTED_MODELS,
    is_client_tool,
)
from harmony.llm.wire import JsonObject, as_list, as_object, as_str, canonical_json, objects

BETA_FALLBACK_LIST: Final = "server-side-fallback-2026-06-01"
SAMPLING_PARAMS: Final = ("temperature", "top_p", "top_k")
FORCED_TOOL_CHOICES: Final = frozenset({"any", "tool"})
# Fields that, if changed between turns, invalidate the cached prefix and the thinking
# blocks bound to it.
PREFIX_FIELDS: Final = ("model", "system", "tools", "thinking", "output_config")


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    message: str
    index: int = 0  # position of the offending request in the checked sequence

    def __str__(self) -> str:
        return f"[{self.index}] {self.rule}: {self.message}"


class ContractViolation(HarmonyError):
    def __init__(self, violations: Sequence[Violation]) -> None:
        super().__init__("request contract violated:\n" + "\n".join(map(str, violations)))
        self.violations = tuple(violations)


# --- One request ----------------------------------------------------------------------


def check_request(request: ModelRequest, *, index: int = 0) -> list[Violation]:
    body = request.body
    betas = set(request.betas)
    found: list[Violation] = []

    def flag(rule: str, message: str) -> None:
        found.append(Violation(rule, message, index))

    model = as_str(body.get("model"))
    if model not in SUPPORTED_MODELS:
        flag("supported-model", f"model {model!r} has no request builder")

    for path in _sampling_paths(body):
        flag("no-sampling", f"sampling parameter {path} is not allowed on any model")

    tool_choice = as_object(body.get("tool_choice"))
    if tool_choice is not None and tool_choice.get("type") in FORCED_TOOL_CHOICES:
        flag("no-forced-tool-choice", f"tool_choice type {tool_choice.get('type')!r}")
    tools = objects(body.get("tools"))
    single_tool = tool_choice is not None and tool_choice.get("disable_parallel_tool_use") is True
    if request.key.role == "representative" and tools and not single_tool:
        flag("representative-single-tool", "representatives must disable parallel tool use")
    for tool in tools:
        if "eager_input_streaming" in tool and not is_client_tool(tool):
            flag("eager-client-tools-only", f"eager_input_streaming on server tool {tool!r}")

    thinking = as_object(body.get("thinking")) or {}
    thinking_type = thinking.get("type")
    effort = as_str((as_object(body.get("output_config")) or {}).get("effort"))
    if thinking_type == "enabled" or "budget_tokens" in thinking:
        flag("no-thinking-budget", "thinking budgets are rejected; use effort")
    if model == OPUS_5_5 and thinking_type == "disabled":
        flag("opus-thinking-on", "Opus 5.5 rejects disabled thinking")
    if model == HAIKU_5_5:
        if "fallbacks" in body:
            flag("haiku-no-fallbacks", "Haiku 5.5 has no server-side fallback")
        if (
            thinking_type == "disabled"
            and (effort or "medium") not in HAIKU_DISABLE_THINKING_EFFORTS
        ):
            flag("haiku-disabled-thinking-effort", f"disabled thinking at effort {effort!r}")
    if thinking_type == "disabled" and "block_binding" in thinking:
        flag("block-binding-adaptive-only", "block_binding needs adaptive thinking")

    fallbacks = body.get("fallbacks")
    required_betas = {
        BETA_FALLBACK_DEFAULT: fallbacks == "default",
        BETA_FALLBACK_LIST: isinstance(fallbacks, list),
        BETA_DISPLAY_UPDATES: thinking.get("display") == "updates",
        BETA_THINKING_BINDING: "block_binding" in thinking,
    }
    for beta, used in required_betas.items():
        if used and beta not in betas:
            flag("beta-header", f"feature in use without beta {beta}")

    found.extend(Violation(rule, message, index) for rule, message in _message_order(body))
    return found


def _sampling_paths(body: JsonObject) -> list[str]:
    paths = [name for name in SAMPLING_PARAMS if name in body]
    for i, hop in enumerate(objects(body.get("fallbacks"))):
        paths += [f"fallbacks[{i}].{name}" for name in SAMPLING_PARAMS if name in hop]
    return paths


def _message_order(body: JsonObject) -> list[tuple[str, str]]:
    raw = as_list(body.get("messages"))
    if raw is None or not raw:
        return [("messages", "messages must be a non-empty list")]
    messages = objects(raw)
    if len(messages) != len(raw):
        return [("messages", "every message must be an object")]
    found: list[tuple[str, str]] = []
    if messages[0].get("role") != "user":
        found.append(("first-message-user", "the first message must be a user message"))
    if messages[-1].get("role") == "assistant":
        found.append(("no-prefill", "the last message must not be an assistant prefill"))
    for i, message in enumerate(messages):
        role = message.get("role")
        if role not in ("user", "assistant", "system"):
            found.append(("message-role", f"messages[{i}] has role {role!r}"))
        if role != "system" or _is_effort_marker(message):
            continue
        before = messages[i - 1] if i > 0 else None
        after = messages[i + 1] if i + 1 < len(messages) else None
        if before is None or not (
            before.get("role") == "user" or _ends_with_server_tool_use(before)
        ):
            found.append(("system-placement", f"messages[{i}] must follow a user message"))
        if after is not None and after.get("role") != "assistant":
            found.append(
                ("system-placement", f"messages[{i}] must be last or followed by an assistant")
            )
        if not _is_text_only(message.get("content")):
            found.append(("system-text-only", f"messages[{i}] must carry text only"))
    return found


def _is_effort_marker(message: JsonObject) -> bool:
    # A per-message effort change (`content: []` + `output_config`) may sit anywhere.
    return message.get("content") == [] and "output_config" in message


def _ends_with_server_tool_use(message: JsonObject) -> bool:
    blocks = objects(message.get("content"))
    return (
        message.get("role") == "assistant"
        and bool(blocks)
        and blocks[-1].get("type") == "server_tool_use"
    )


def _is_text_only(content: JsonValue) -> bool:
    if isinstance(content, str):
        return True
    blocks = as_list(content)
    return blocks is not None and all(
        isinstance(block, dict) and block.get("type") == "text" for block in blocks
    )


# --- Consecutive requests of one conversation -------------------------------------------


def conversation_key(request: ModelRequest) -> Hashable | None:
    """Which requests must extend each other: a representative's (thread, side) or an
    internal chat (its conversation id in `thread`). Single calls have none."""
    key = request.key
    if key.role not in ("representative", "internal") or key.thread is None:
        return None
    world: UUID = key.world_id
    return (world, key.org, key.role, key.thread)


def check_prefix(
    previous: ModelRequest, current: ModelRequest, *, index: int = 0
) -> list[Violation]:
    found: list[Violation] = []
    for field in PREFIX_FIELDS:
        before, after = previous.body.get(field), current.body.get(field)
        if canonical_json(before) != canonical_json(after):
            found.append(Violation("prefix-extension", f"`{field}` changed between turns", index))
    old = objects(previous.body.get("messages"))
    new = objects(current.body.get("messages"))
    old_text = canonical_json(previous.body.get("messages"))
    new_text = canonical_json(current.body.get("messages"))
    # "[a,b]" -> "[a,b": the new list must start with exactly the old elements.
    if not new_text.startswith(old_text[:-1]):
        diverged = next(
            (
                i
                for i, message in enumerate(old)
                if i >= len(new) or canonical_json(message) != canonical_json(new[i])
            ),
            len(old),
        )
        found.append(
            Violation(
                "prefix-extension",
                f"messages diverge at index {diverged} (previous request had {len(old)})",
                index,
            )
        )
    return found


def check_requests(
    requests: Iterable[ModelRequest],
    *,
    key: Callable[[ModelRequest], Hashable | None] = conversation_key,
) -> list[Violation]:
    """Every per-request rule, plus the prefix rule between consecutive requests that
    share a conversation key, in recording order."""
    found: list[Violation] = []
    last: dict[Hashable, ModelRequest] = {}
    for index, request in enumerate(requests):
        found.extend(check_request(request, index=index))
        conversation = key(request)
        if conversation is None:
            continue
        previous = last.get(conversation)
        if previous is not None:
            found.extend(check_prefix(previous, request, index=index))
        last[conversation] = request
    return found


def assert_contract(requests: Iterable[ModelRequest]) -> None:
    violations = check_requests(requests)
    if violations:
        raise ContractViolation(violations)


__all__ = [
    "ContractViolation",
    "Violation",
    "assert_contract",
    "check_prefix",
    "check_request",
    "check_requests",
    "conversation_key",
]
