from __future__ import annotations

import math
from decimal import Decimal

import pytest
from pydantic import JsonValue

from harmony.llm.errors import BadRequest
from harmony.llm.pricing import (
    HAIKU_5_5,
    OPUS_4_8,
    OPUS_5,
    OPUS_5_5,
    PRICES,
    UNKNOWN_MODEL_PRICE,
    call_cost,
    estimate_input_tokens,
    price_of,
    round_usd,
    usage_counts,
    worst_case_cost,
)
from harmony.llm.wire import JsonObject, canonical_json

MTOK = 1_000_000


def cost(usage: JsonObject, model: str, served: str | None = None) -> Decimal:
    return call_cost(usage, requested_model=model, served_model=served or model)


# --- Golden prices ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        # $4 in, $20 out.
        (OPUS_5_5, {"input_tokens": MTOK, "output_tokens": MTOK}, "24"),
        # Cache read $0.20; writes $5 (5-minute) and $8 (1-hour).
        (OPUS_5_5, {"cache_read_input_tokens": MTOK}, "0.20"),
        (
            OPUS_5_5,
            {
                "cache_creation_input_tokens": 2 * MTOK,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": MTOK,
                    "ephemeral_1h_input_tokens": MTOK,
                },
            },
            "13",
        ),
        # A typical turn: 10K fresh, 50K cached, 4K + 1K written, 2K out.
        (
            OPUS_5_5,
            {
                "input_tokens": 10_000,
                "output_tokens": 2_000,
                "cache_read_input_tokens": 50_000,
                "cache_creation_input_tokens": 5_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 4_000,
                    "ephemeral_1h_input_tokens": 1_000,
                },
            },
            "0.118",
        ),
        # Without a TTL breakdown, writes are the 5-minute kind our builders use.
        (OPUS_5_5, {"cache_creation_input_tokens": MTOK}, "5"),
        # Opus 5 and Opus 4.8: $5 / $25, cache read $0.50, writes 1.25x and 2x.
        (OPUS_5, {"input_tokens": MTOK, "output_tokens": MTOK}, "30"),
        (OPUS_5, {"cache_read_input_tokens": MTOK}, "0.50"),
        (OPUS_4_8, {"input_tokens": MTOK, "output_tokens": MTOK}, "30"),
        (
            OPUS_4_8,
            {
                "cache_creation_input_tokens": 2 * MTOK,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": MTOK,
                    "ephemeral_1h_input_tokens": MTOK,
                },
            },
            "16.25",
        ),
        # Haiku 5.5 short context: $0.10 / $0.50; read 0.1x; writes 1.25x and 2x.
        (HAIKU_5_5, {"input_tokens": 50_000, "output_tokens": 1_000}, "0.0055"),
        (HAIKU_5_5, {"input_tokens": 50_000, "cache_read_input_tokens": 50_000}, "0.0055"),
        (
            HAIKU_5_5,
            {
                "cache_creation_input_tokens": 10_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 8_000,
                    "ephemeral_1h_input_tokens": 2_000,
                },
            },
            "0.0014",
        ),
    ],
)
def test_golden_prices(model: str, usage: JsonObject, expected: str) -> None:
    assert cost(usage, model) == Decimal(expected)


def test_haiku_steps_up_above_100k_prompt_tokens() -> None:
    # The step counts every input-side token: fresh, cached and written.
    at_limit: JsonObject = {
        "input_tokens": 60_000,
        "cache_read_input_tokens": 40_000,
        "output_tokens": 1_000,
    }
    over_limit: JsonObject = {**at_limit, "input_tokens": 60_001}
    # 60K x $0.10 + 40K x $0.01 + 1K x $0.50
    assert cost(at_limit, HAIKU_5_5) == Decimal("0.0069")
    # 60,001 x $0.50 + 40K x $0.05 + 1K x $2.50 = $0.0345005, rounded *up*.
    assert cost(over_limit, HAIKU_5_5) == Decimal("0.034501")

    written_over: JsonObject = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 100_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 100_000},
    }
    # 1 x $0.50 + 100K x $0.625 (1.25x the long-context input rate)
    assert cost(written_over, HAIKU_5_5) == round_usd(Decimal("0.0000005") + Decimal("0.0625"))


def test_a_fallback_iteration_is_priced_at_its_served_model() -> None:
    # Opus 5.5 was overloaded mid-request; Opus 5 served the response. Top-level usage
    # covers only the served attempt; iterations cover both.
    usage: JsonObject = {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "iterations": [
            {"type": "message", "model": OPUS_5_5, "input_tokens": 1_000, "output_tokens": 0},
            {
                "type": "fallback_message",
                "model": OPUS_5,
                "input_tokens": 1_000,
                "output_tokens": 500,
            },
        ],
    }
    # 1K x $4 on Opus 5.5, then 1K x $5 + 500 x $25 on Opus 5.
    assert cost(usage, OPUS_5_5, served=OPUS_5) == Decimal("0.0215")
    counts = usage_counts(usage)
    assert (counts.input_tokens, counts.output_tokens) == (2_000, 500)


def test_an_iteration_without_a_model_is_priced_at_the_dearer_model() -> None:
    usage: JsonObject = {
        "iterations": [{"type": "message", "input_tokens": MTOK}],
    }
    assert cost(usage, OPUS_5_5, served=OPUS_5) == Decimal("5")
    assert cost(usage, OPUS_5_5) == Decimal("4")


def test_web_search_and_fast_mode() -> None:
    usage: JsonObject = {
        "input_tokens": MTOK,
        "server_tool_use": {"web_search_requests": 3},
    }
    assert cost(usage, OPUS_5_5) == Decimal("4.03")
    # Fast mode doubles token prices, not search fees.
    assert cost({**usage, "speed": "fast"}, OPUS_5_5) == Decimal("8.03")


def test_an_unpriced_served_model_costs_the_dearest_rate_never_zero() -> None:
    usage: JsonObject = {"input_tokens": MTOK, "output_tokens": MTOK}
    unknown = cost(usage, OPUS_5_5, served="claude-opus-9")
    assert unknown == Decimal("30")
    assert all(unknown >= cost(usage, model) for model in PRICES)
    assert UNKNOWN_MODEL_PRICE.card.output == max(
        card.output for price in PRICES.values() for card in price.cards
    )


def test_an_unpriced_requested_model_is_refused() -> None:
    with pytest.raises(BadRequest):
        price_of("claude-sonnet-9")
    with pytest.raises(BadRequest):
        worst_case_cost({"model": "claude-sonnet-9", "max_tokens": 10, "messages": []})


def test_costs_round_up_to_the_micro_dollar() -> None:
    assert round_usd(Decimal("0.0000001")) == Decimal("0.000001")
    assert round_usd(Decimal("1.0000000")) == Decimal("1.000000")
    assert cost({"input_tokens": 1}, OPUS_5_5) == Decimal("0.000004")
    assert cost({"input_tokens": 1}, HAIKU_5_5) == Decimal("0.000001")  # $0.0000001


def test_malformed_counts_are_not_negative_or_bools() -> None:
    usage: JsonObject = {"input_tokens": -5, "output_tokens": True, "cache_read_input_tokens": "9"}
    assert cost(usage, OPUS_5_5) == Decimal(0)


# --- Worst case ---------------------------------------------------------------------------


def _body(model: str, max_tokens: int, **extra: JsonValue) -> JsonObject:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "hello " * 200}],
        **extra,
    }


def test_worst_case_covers_the_primary_attempt_and_the_default_fallback() -> None:
    body = _body(OPUS_5_5, 4_000, fallbacks="default")
    tokens = math.ceil(len(canonical_json(body).encode()) / 2)
    assert estimate_input_tokens(body) == tokens
    primary = Decimal(tokens) * 8 + Decimal(4_000) * 20  # 1-hour write is the dearest input
    fallback = Decimal(tokens) * 10 + Decimal(4_000) * 25  # Opus 5 / 4.8
    assert worst_case_cost(body) == round_usd((primary + fallback) / MTOK)

    without_fallback = _body(OPUS_5_5, 4_000)
    assert worst_case_cost(without_fallback) < worst_case_cost(body)


def test_worst_case_bounds_any_bill_the_request_can_produce() -> None:
    body = _body(OPUS_5_5, 4_000, fallbacks="default")
    tokens = estimate_input_tokens(body)
    # The dearest real outcome: the primary writes the whole prompt to the 1-hour
    # cache and runs to max_tokens, then the fallback does the same.
    billed: JsonObject = {
        "iterations": [
            {
                "type": "message",
                "model": OPUS_5_5,
                "cache_creation_input_tokens": tokens,
                "cache_creation": {"ephemeral_1h_input_tokens": tokens},
                "output_tokens": 4_000,
            },
            {
                "type": "fallback_message",
                "model": OPUS_4_8,
                "cache_creation_input_tokens": tokens,
                "cache_creation": {"ephemeral_1h_input_tokens": tokens},
                "output_tokens": 4_000,
            },
        ]
    }
    assert cost(billed, OPUS_5_5, served=OPUS_4_8) <= worst_case_cost(body)


def test_haiku_worst_case_uses_the_long_context_card() -> None:
    body = _body(HAIKU_5_5, 2_000)
    tokens = estimate_input_tokens(body)
    expected = (Decimal(tokens) * Decimal("1.00") + Decimal(2_000) * Decimal("2.50")) / MTOK
    assert worst_case_cost(body) == round_usd(expected)


def test_worst_case_adds_every_hop_of_an_explicit_fallback_list() -> None:
    body = _body(
        OPUS_5_5,
        1_000,
        fallbacks=[{"model": OPUS_5, "max_tokens": 2_000}, {"model": OPUS_4_8}],
    )
    tokens = estimate_input_tokens(body)
    expected = (
        Decimal(tokens) * 8
        + Decimal(1_000) * 20
        + Decimal(tokens) * 10
        + Decimal(2_000) * 25
        + Decimal(tokens) * 10
        + Decimal(1_000) * 25
    ) / MTOK
    assert worst_case_cost(body) == round_usd(expected)


def _opus_attempt(body: JsonObject, max_tokens: int) -> Decimal:
    return (Decimal(estimate_input_tokens(body)) * 8 + Decimal(max_tokens) * 20) / MTOK


def test_worst_case_reserves_web_searches_and_fast_mode() -> None:
    search: JsonObject = {"type": "web_search_20250305", "name": "web_search"}
    bounded = _body(OPUS_5_5, 100, tools=[{**search, "max_uses": 5}])
    assert worst_case_cost(bounded) == round_usd(_opus_attempt(bounded, 100) + Decimal("0.05"))
    # A search tool without max_uses is reserved at 20 searches.
    unbounded = _body(OPUS_5_5, 100, tools=[search])
    assert worst_case_cost(unbounded) == round_usd(_opus_attempt(unbounded, 100) + Decimal("0.20"))
    fast = _body(OPUS_5_5, 100, speed="fast")
    assert worst_case_cost(fast) == round_usd(_opus_attempt(fast, 100) * 2)


def test_worst_case_rejects_a_body_without_model_or_max_tokens() -> None:
    with pytest.raises(BadRequest):
        worst_case_cost({"messages": []})
    with pytest.raises(BadRequest):
        worst_case_cost({"model": OPUS_5_5, "max_tokens": True})
