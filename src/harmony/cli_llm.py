"""`harmony llm`: model client utilities.

`harmony llm ping --model opus|haiku-featured|haiku` builds one small request in a
production shape,
checks it against the request contract and prints it with its worst-case reservation,
without sending anything. The shapes are the ones whose betas and fields a workspace
can reject (3.1):
- `opus`: a featured internal turn streamed to the UI, so `thinking.display` is
  `updates`, both Opus betas are sent, and its one client tool streams eagerly;
- `haiku-featured`: the same turn on Haiku 5.5, as prod sends it while MODEL_OVERRIDE
  pins every role to Haiku;
- `haiku`: the screener, with thinking disabled and a JSON-schema `output_config.format`.

`--live` sends it: one small call through `BudgetGuard` (so a missing ledger refuses
before any client exists), recorded in the ledger. Only the coordinator runs `--live`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Annotated, Final, Literal, NoReturn

import typer

from harmony.config import Settings, get_settings
from harmony.errors import HarmonyError
from harmony.llm import CallKey, ModelRequest, ModelResult
from harmony.llm.contract import check_request
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5, worst_case_cost
from harmony.llm.requests import build_request
from harmony.llm.wire import JsonObject, as_object, as_str, canonical_json, objects

app = typer.Typer(add_completion=False, help="Model client utilities.")

PingModel = Literal["opus", "haiku-featured", "haiku"]
PING_MODELS: Final[dict[PingModel, str]] = {
    "opus": OPUS_5_5,
    "haiku-featured": HAIKU_5_5,
    "haiku": HAIKU_5_5,
}
PING_MAX_TOKENS = 1024
PING_PROMPT = "Reply with the single word: pong"
# The streamed Opus shape needs a client tool; the prompt doesn't call for it.
PING_TOOL: Final[JsonObject] = {
    "name": "record_note",
    "description": "Record a short note. Not needed to reply.",
    "input_schema": {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
        "additionalProperties": False,
    },
}
PING_OUTPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


@app.callback()
def _group() -> None:  # pyright: ignore[reportUnusedFunction]
    """Model client utilities."""


@app.command()
def ping(
    model: Annotated[
        PingModel,
        typer.Option(
            "--model",
            help="opus: a streamed featured internal turn; haiku-featured: that turn on "
            "Haiku 5.5; haiku: a screener call.",
        ),
    ],
    live: Annotated[
        bool,
        typer.Option("--live", help="Send the request: one real, paid call (about $0.01)."),
    ] = False,
) -> None:
    """Build (and with --live, send) one small request in a production shape."""
    request = ping_request(model)
    violations = check_request(request)
    if violations:
        _fail("ping request breaks the contract: " + "; ".join(map(str, violations)))
    estimate = worst_case_cost(request.body)
    if not live:
        typer.echo(canonical_json(request.body))
        typer.echo(f"betas: {', '.join(request.betas) or 'none'}")
        typer.echo(
            f"dry run: would reserve up to ${estimate} on {request.body['model']}; --live sends it"
        )
        return
    settings = get_settings()
    try:
        result, cost = asyncio.run(_send(settings, request))
    except HarmonyError as exc:
        _fail(f"{type(exc).__name__}: {exc}")
    typer.echo(f"served by {result.served_model}, stop_reason={result.stop_reason}")
    typer.echo(f"reply: {reply_text(result.response_text)!r}")
    typer.echo(f"cost ${cost} (reserved up to ${estimate})")
    _report_spend(settings)


def ping_request(model: PingModel) -> ModelRequest:
    messages: list[JsonObject] = [{"role": "user", "content": PING_PROMPT}]
    if model != "haiku":
        return build_request(
            _ping_key("internal"),
            tier="featured",
            system=None,
            messages=messages,
            max_tokens=PING_MAX_TOKENS,
            tools=[PING_TOOL],
            stream_to_ui=True,
            effort="low",
            model_override=PING_MODELS[model],
        )
    return build_request(
        _ping_key("screener"),
        tier="featured",
        system=None,
        messages=messages,
        max_tokens=PING_MAX_TOKENS,
        output_schema=PING_OUTPUT_SCHEMA,
        model_override=PING_MODELS[model],
    )


def _ping_key(role: Literal["internal", "screener"]) -> CallKey:
    return CallKey(
        world_id=uuid.UUID(int=0),
        scenario="ping",
        org="harmony",
        role=role,
        thread=None,
        turn=1,
        turn_id=uuid.uuid4(),
        step_idx=0,
        attempt=1,
    )


def reply_text(response_text: str) -> str:
    message = as_object(json.loads(response_text)) or {}
    blocks = objects(message.get("content"))
    return "".join(as_str(b.get("text")) or "" for b in blocks if b.get("type") == "text").strip()


async def _send(settings: Settings, request: ModelRequest) -> tuple[ModelResult, Decimal]:
    # Imported here so the dry run never loads the SDK or the database layer.
    from harmony.llm.anthropic_model import AnthropicModel
    from harmony.llm.budget import BudgetGuard
    from harmony.llm.checkpoint import metered_call

    # The guard first: without a usable ledger, no client is ever built.
    guard = BudgetGuard.from_settings(settings)
    client = AnthropicModel.from_settings(settings, on_provider_limit=guard.block_provider)

    async def keep(result: ModelResult, cost: Decimal) -> tuple[ModelResult, Decimal]:
        return result, cost

    try:
        return await metered_call(client, guard, request, keep)
    finally:
        await client.aclose()


def _report_spend(settings: Settings) -> None:
    if settings.spend_ledger_path is None:
        return
    from harmony.cli_spend import alert_line, report_lines
    from harmony.llm.ledger import SpendLedger

    summary = SpendLedger(settings.spend_ledger_path, project=settings.spend_project).read()
    typer.echo(report_lines(summary, settings.dev_budget_usd)[0])
    for alert in summary.unacked:
        typer.echo(alert_line(alert))


def _fail(message: str) -> NoReturn:
    typer.echo(f"harmony llm: {message}", err=True)
    raise typer.Exit(code=2)


__all__ = ["PING_MODELS", "PingModel", "app", "ping_request", "reply_text"]
