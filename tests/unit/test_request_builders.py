from __future__ import annotations

import itertools
from typing import get_args
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from harmony.llm import CallKey, ModelRequest, ModelRole
from harmony.llm.contract import (
    BETA_FALLBACK_LIST,
    ContractViolation,
    assert_contract,
    check_prefix,
    check_request,
    check_requests,
)
from harmony.llm.errors import BadRequest
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5
from harmony.llm.requests import (
    BETA_DISPLAY_UPDATES,
    BETA_FALLBACK_DEFAULT,
    BETA_THINKING_BINDING,
    ROLE_PROFILES,
    Tier,
    build_request,
    resolve_model,
)
from harmony.llm.wire import JsonObject, as_object, objects

ROLES: tuple[ModelRole, ...] = get_args(ModelRole)
TIERS: tuple[Tier, ...] = get_args(Tier)
WORLD = UUID(int=1)

CLIENT_TOOL: JsonObject = {
    "name": "send_message",
    "description": "Send a message in the thread.",
    "input_schema": {"type": "object", "properties": {"body": {"type": "string"}}},
}
SERVER_TOOL: JsonObject = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
USER: JsonObject = {"role": "user", "content": "Can you share the pilot dates?"}


def key(role: ModelRole = "representative", *, thread: str | None = "nl-hl") -> CallKey:
    return CallKey(
        world_id=WORLD,
        scenario="test",
        org="northlake",
        role=role,
        thread=thread,
        turn=1,
        turn_id=uuid4(),
        step_idx=0,
        attempt=1,
    )


def build(
    role: ModelRole = "representative",
    tier: Tier = "featured",
    *,
    override: str | None = None,
    messages: list[JsonObject] | None = None,
    **options: object,
) -> ModelRequest:
    return build_request(
        key(role),
        tier=tier,
        system="You represent Northlake.",
        messages=messages or [USER],
        max_tokens=2_000,
        model_override=override,
        **options,  # pyright: ignore[reportArgumentType]
    )


def thinking(request: ModelRequest) -> JsonObject:
    return as_object(request.body["thinking"]) or {}


def tool_choice(request: ModelRequest) -> JsonObject:
    return as_object(request.body.get("tool_choice")) or {}


ALL_BUILDS = [
    pytest.param(role, tier, override, id=f"{role}-{tier}-{override or 'prod'}")
    for role, tier, override in itertools.product(ROLES, TIERS, (None, OPUS_5_5))
]


# --- Rules for every model -----------------------------------------------------------


@pytest.mark.parametrize(("role", "tier", "override"), ALL_BUILDS)
def test_every_built_request_satisfies_the_contract(
    role: ModelRole, tier: Tier, override: str | None
) -> None:
    for stream_to_ui in (False, True):
        request = build(
            role,
            tier,
            override=override,
            tools=[CLIENT_TOOL, SERVER_TOOL],
            stream_to_ui=stream_to_ui,
            prefix_mismatch="error",
        )
        assert check_request(request) == []
        body = request.body
        assert body["model"] == resolve_model(role, tier, override)
        assert not {"temperature", "top_p", "top_k"} & body.keys()
        assert tool_choice(request)["type"] == "auto"
        effort = (as_object(body["output_config"]) or {})["effort"]
        assert effort == ROLE_PROFILES[(role, tier)].effort
        assert list(body)[:4] == ["model", "max_tokens", "system", "messages"]
        assert body["cache_control"] == {"type": "ephemeral"}


def test_representatives_disable_parallel_tool_use() -> None:
    assert tool_choice(build("representative", tools=[CLIENT_TOOL])) == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }
    assert tool_choice(build("internal", tools=[CLIENT_TOOL])) == {"type": "auto"}
    assert "tool_choice" not in build("representative").body


def test_eager_input_streaming_only_on_client_tools_when_streaming() -> None:
    streamed = objects(build(tools=[CLIENT_TOOL, SERVER_TOOL], stream_to_ui=True).body["tools"])
    assert streamed[0]["eager_input_streaming"] is True
    assert "eager_input_streaming" not in streamed[1]
    quiet = objects(build(tools=[CLIENT_TOOL], stream_to_ui=False).body["tools"])
    assert "eager_input_streaming" not in quiet[0]


def test_effort_and_structured_output() -> None:
    schema: JsonObject = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    request = build("synthesizer", effort="max", output_schema=schema)
    assert request.body["output_config"] == {
        "effort": "max",
        "format": {"type": "json_schema", "schema": schema},
    }


def test_max_tokens_and_model_are_checked() -> None:
    with pytest.raises(BadRequest):
        build_request(key(), tier="featured", system=None, messages=[USER], max_tokens=0)
    with pytest.raises(BadRequest):
        build_request(key(), tier="featured", system=None, messages=[USER], max_tokens=128_001)
    with pytest.raises(BadRequest, match="no request builder"):
        build(override="claude-opus-5")


# --- Opus 5.5 --------------------------------------------------------------------------


def test_opus_thinks_adaptively_and_falls_back_server_side() -> None:
    request = build("representative", "featured")
    assert request.body["model"] == OPUS_5_5
    assert thinking(request) == {"type": "adaptive"}
    assert request.body["fallbacks"] == "default"
    assert request.betas == (BETA_FALLBACK_DEFAULT,)


def test_opus_streams_thinking_updates_with_its_beta() -> None:
    request = build("internal", stream_to_ui=True)
    assert thinking(request) == {"type": "adaptive", "display": "updates"}
    assert set(request.betas) == {BETA_FALLBACK_DEFAULT, BETA_DISPLAY_UPDATES}


def test_block_binding_carries_its_beta() -> None:
    request = build("representative", prefix_mismatch="error")
    assert thinking(request)["block_binding"] == {"prefix_mismatch_behavior": "error"}
    assert BETA_THINKING_BINDING in request.betas


def test_the_dev_override_never_disables_thinking() -> None:
    # The screener wants thinking off on Haiku; on Opus 5.5 that would be a 400.
    request = build("screener", override=OPUS_5_5)
    assert request.body["model"] == OPUS_5_5
    assert thinking(request) == {"type": "adaptive"}
    assert request.body["fallbacks"] == "default"


# --- Haiku 5.5 -------------------------------------------------------------------------


def test_haiku_has_no_fallbacks_and_disables_thinking_only_up_to_high() -> None:
    screener = build("screener")
    assert screener.body["model"] == HAIKU_5_5
    assert "fallbacks" not in screener.body
    assert thinking(screener) == {"type": "disabled"}
    assert screener.betas == ()
    for effort in ("low", "medium", "high"):
        assert thinking(build("screener", effort=effort)) == {"type": "disabled"}
    for effort in ("xhigh", "max"):
        assert thinking(build("screener", effort=effort)) == {"type": "adaptive"}


def test_haiku_background_roles_keep_adaptive_thinking() -> None:
    for role in ("representative", "answerer"):
        request = build(role, "background")
        assert request.body["model"] == HAIKU_5_5
        assert thinking(request) == {"type": "adaptive"}
        assert "fallbacks" not in request.body
        # Haiku never streams thinking updates.
        assert "display" not in thinking(build(role, "background", stream_to_ui=True))


# --- The contract checker catches what the API would reject --------------------------


def broken(request: ModelRequest, **changes: JsonValue) -> ModelRequest:
    return request.model_copy(update={"body": {**request.body, **changes}})


def rules(request: ModelRequest) -> set[str]:
    return {violation.rule for violation in check_request(request)}


@pytest.mark.parametrize(
    ("base_role", "changes", "rule"),
    [
        ("representative", {"temperature": 0.2}, "no-sampling"),
        (
            "representative",
            {"fallbacks": [{"model": "claude-opus-5", "top_p": 0.9}]},
            "no-sampling",
        ),
        ("representative", {"tool_choice": {"type": "any"}}, "no-forced-tool-choice"),
        ("internal", {"tool_choice": {"type": "tool", "name": "x"}}, "no-forced-tool-choice"),
        ("representative", {"tool_choice": {"type": "auto"}}, "representative-single-tool"),
        ("internal", {"thinking": {"type": "disabled"}}, "opus-thinking-on"),
        (
            "internal",
            {"thinking": {"type": "enabled", "budget_tokens": 1024}},
            "no-thinking-budget",
        ),
        ("internal", {"thinking": {"type": "adaptive", "display": "updates"}}, "beta-header"),
        ("internal", {"fallbacks": [{"model": "claude-opus-5"}]}, "beta-header"),
        ("internal", {"model": "claude-sonnet-9"}, "supported-model"),
        ("screener", {"fallbacks": "default"}, "haiku-no-fallbacks"),
        ("screener", {"output_config": {"effort": "max"}}, "haiku-disabled-thinking-effort"),
        (
            "screener",
            {
                "thinking": {
                    "type": "disabled",
                    "block_binding": {"prefix_mismatch_behavior": "error"},
                }
            },
            "block-binding-adaptive-only",
        ),
        (
            "internal",
            {"tools": [{**SERVER_TOOL, "eager_input_streaming": True}]},
            "eager-client-tools-only",
        ),
    ],
)
def test_per_model_violations_are_caught(
    base_role: ModelRole, changes: dict[str, JsonValue], rule: str
) -> None:
    request = build(base_role, tools=[CLIENT_TOOL])
    assert rule in rules(broken(request, **changes))


def test_a_fallback_list_needs_its_own_beta() -> None:
    request = broken(build("internal"), fallbacks=[{"model": "claude-opus-5"}])
    assert rules(request) == {"beta-header"}
    fixed = request.model_copy(update={"betas": (*request.betas, BETA_FALLBACK_LIST)})
    assert rules(fixed) == set()


SYSTEM_NOTE: JsonObject = {"role": "system", "content": "The member changed the settings."}
ASSISTANT: JsonObject = {"role": "assistant", "content": [{"type": "text", "text": "Sure."}]}
SERVER_TOOL_TURN: JsonObject = {
    "role": "assistant",
    "content": [
        {"type": "text", "text": "Searching."},
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}},
    ],
}


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([USER, SYSTEM_NOTE], set[str]()),
        ([USER, SYSTEM_NOTE, ASSISTANT, USER], set[str]()),
        ([USER, SERVER_TOOL_TURN, SYSTEM_NOTE], set[str]()),
        # The per-message effort marker may sit anywhere.
        (
            [
                USER,
                ASSISTANT,
                {"role": "system", "content": [], "output_config": {"effort": "low"}},
                USER,
            ],
            set[str](),
        ),
        ([SYSTEM_NOTE, USER], {"first-message-user", "system-placement"}),
        ([USER, ASSISTANT, SYSTEM_NOTE], {"system-placement"}),
        ([USER, SYSTEM_NOTE, USER], {"system-placement"}),
        (
            [USER, {"role": "system", "content": [{"type": "image", "source": {}}]}],
            {"system-text-only"},
        ),
        ([USER, ASSISTANT], {"no-prefill"}),
        ([USER, {"role": "tool", "content": "x"}], {"message-role"}),
        ([], {"messages"}),
    ],
)
def test_message_order_rules(messages: list[JsonObject], expected: set[str]) -> None:
    request = broken(build("internal"), messages=list[JsonValue](messages))
    assert rules(request) == expected


# --- Byte-prefix extension across turns ---------------------------------------------------


def test_consecutive_turns_must_extend_the_previous_request() -> None:
    first = build("representative", tools=[CLIENT_TOOL])
    second = build(
        "representative",
        tools=[CLIENT_TOOL],
        messages=[USER, ASSISTANT, {"role": "user", "content": "Thanks!"}],
    )
    assert check_prefix(first, second) == []

    edited = build(
        "representative",
        tools=[CLIENT_TOOL],
        messages=[{"role": "user", "content": "Can you share the pilot date?"}, ASSISTANT, USER],
    )
    [violation] = check_prefix(first, edited)
    assert "diverge at index 0" in violation.message

    new_system = build_request(
        key(),
        tier="featured",
        system="You represent Northlake. Today is Tuesday.",
        messages=[USER, ASSISTANT, USER],
        max_tokens=2_000,
        tools=[CLIENT_TOOL],
    )
    assert {v.message for v in check_prefix(first, new_system)} == {
        "`system` changed between turns"
    }

    streamed = build(
        "representative", tools=[CLIENT_TOOL], messages=[USER, ASSISTANT, USER], stream_to_ui=True
    )
    changed = {v.message for v in check_prefix(first, streamed)}
    assert {"`tools` changed between turns", "`thinking` changed between turns"} <= changed


def test_check_requests_follows_each_conversation_separately() -> None:
    def rep(thread: str, messages: list[JsonObject]) -> ModelRequest:
        return build_request(
            key("representative", thread=thread),
            tier="featured",
            system="You represent Northlake.",
            messages=messages,
            max_tokens=2_000,
        )

    a1 = rep("a", [USER])
    b1 = rep("b", [{"role": "user", "content": "Different thread."}])
    a2 = rep("a", [USER, ASSISTANT, USER])
    one_off = build_request(
        key("screener", thread=None), tier="featured", system=None, messages=[USER], max_tokens=50
    )
    assert check_requests([a1, b1, one_off, a2]) == []
    assert_contract([a1, b1, one_off, a2])

    rewritten = rep("a", [{"role": "user", "content": "Rewritten history."}, ASSISTANT, USER])
    violations = check_requests([a1, b1, rewritten])
    assert [(v.rule, v.index) for v in violations] == [("prefix-extension", 2)]
    with pytest.raises(ContractViolation) as caught:
        assert_contract([a1, b1, rewritten])
    assert caught.value.violations == tuple(violations)


# --- harmony llm ping (dry run only; --live is never run in tests) ---------------------


def test_ping_dry_run_builds_a_valid_request_without_sending() -> None:
    import json
    import subprocess
    import sys

    from typer.testing import CliRunner

    from harmony import cli_llm

    request = cli_llm.ping_request()
    assert check_request(request) == []
    assert request.body["model"] == OPUS_5_5
    assert request.key.role == "internal"

    result = CliRunner().invoke(cli_llm.app, ["ping"])
    assert result.exit_code == 0, result.output
    body_line, betas_line, summary = result.output.splitlines()
    assert json.loads(body_line)["max_tokens"] == cli_llm.PING_MAX_TOKENS
    assert betas_line == f"betas: {BETA_FALLBACK_DEFAULT}"
    assert summary.startswith("dry run: would reserve up to $")

    # The dry run never loads the SDK (checked in a clean interpreter).
    probe = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from harmony import cli_llm\n"
        "assert CliRunner().invoke(cli_llm.app, ['ping']).exit_code == 0\n"
        "assert 'anthropic' not in sys.modules, 'dry run imported the SDK'\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=60)
