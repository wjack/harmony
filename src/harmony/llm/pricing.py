"""Per-model prices, call costs, and the worst-case estimate a call reserves.

Prices are USD per million tokens, from the Claude API reference (2026-10):
- Opus 5.5: $4 / $20; cache reads $0.20 (0.05x); writes $5 (5-minute), $8 (1-hour).
- Haiku 5.5: $0.10 / $0.50 when the prompt is 100K tokens or fewer, $0.50 / $2.50
  above; cache reads 0.1x input, writes 1.25x (5-minute) and 2x (1-hour).
- Opus 5 and Opus 4.8, the server-side fallback targets for Opus 5.5: $5 / $25;
  cache reads $0.50; writes 1.25x and 2x.
- Web search: $10 per 1,000 searches.

Money-safety rules:
- Costs are Decimal, rounded *up* to the micro-dollar.
- A served model with no price (a new fallback target, say) is priced at the dearest
  known rate, never at $0. An unknown *requested* model is refused before the call.
- The worst case a call reserves covers every attempt the request can bill: the
  requested model at `max_tokens`, plus its server-side fallback.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Final

from pydantic import JsonValue

from harmony.llm.errors import BadRequest
from harmony.llm.usage import Iteration, ParsedUsage, TokenCounts
from harmony.llm.wire import as_int, as_list, as_str, canonical_json, objects

log = logging.getLogger(__name__)

OPUS_5_5: Final = "claude-opus-5-5"
HAIKU_5_5: Final = "claude-haiku-5-5"
OPUS_5: Final = "claude-opus-5"
OPUS_4_8: Final = "claude-opus-4-8"

WEB_SEARCH_USD: Final = Decimal("0.01")
# Fast mode bills 2x the standard rate (documented for Opus 5.5: $8 / $40).
FAST_MODE_MULTIPLIER: Final = Decimal(2)
# The models `fallbacks: "default"` can hand an Opus 5.5 request to (one hop).
DEFAULT_FALLBACK_TARGETS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {OPUS_5_5: (OPUS_5, OPUS_4_8)}
)
# A web-search tool without `max_uses` is reserved at this many searches.
UNBOUNDED_WEB_SEARCHES: Final = 20
# Conservative bytes-per-token for estimating a request's input before sending it:
# the 5.x tokenizers average about 3 characters of English per token, and JSON
# framing only adds bytes, so 2 overestimates.
_BYTES_PER_TOKEN: Final = 2

_MTOK = Decimal(1_000_000)
_MICRO = Decimal("0.000001")


def round_usd(amount: Decimal) -> Decimal:
    """Round up to the micro-dollar (`model_calls.cost_usd` is Numeric(14, 6))."""
    return amount.quantize(_MICRO, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class RateCard:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal

    @classmethod
    def standard(cls, input_usd: str, output_usd: str, *, cache_read: str) -> RateCard:
        """A card whose cache writes are the standard 1.25x (5-minute) and 2x (1-hour)."""
        base = Decimal(input_usd)
        return cls(
            input=base,
            output=Decimal(output_usd),
            cache_read=Decimal(cache_read),
            cache_write_5m=base * Decimal("1.25"),
            cache_write_1h=base * 2,
        )

    @property
    def dearest_input(self) -> Decimal:
        return max(self.input, self.cache_read, self.cache_write_5m, self.cache_write_1h)

    def cost(self, tokens: TokenCounts) -> Decimal:
        return (
            tokens.input_tokens * self.input
            + tokens.output_tokens * self.output
            + tokens.cache_read_input_tokens * self.cache_read
            + tokens.cache_write_5m_tokens * self.cache_write_5m
            + tokens.cache_write_1h_tokens * self.cache_write_1h
        ) / _MTOK


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model: str
    card: RateCard
    # A second card for long prompts (Haiku 5.5), used when the prompt's input-side
    # tokens exceed `long_context_above`.
    long_context_card: RateCard | None = None
    long_context_above: int = 0

    @property
    def cards(self) -> tuple[RateCard, ...]:
        return (
            (self.card,) if self.long_context_card is None else (self.card, self.long_context_card)
        )

    def card_for(self, tokens: TokenCounts) -> RateCard:
        if self.long_context_card is not None and tokens.prompt_tokens > self.long_context_above:
            return self.long_context_card
        return self.card

    def cost(self, tokens: TokenCounts) -> Decimal:
        return self.card_for(tokens).cost(tokens)


_HAIKU_SHORT = RateCard(
    input=Decimal("0.10"),
    output=Decimal("0.50"),
    cache_read=Decimal("0.010"),
    cache_write_5m=Decimal("0.125"),
    cache_write_1h=Decimal("0.20"),
)
_HAIKU_LONG = RateCard(
    input=Decimal("0.50"),
    output=Decimal("2.50"),
    cache_read=Decimal("0.050"),
    cache_write_5m=Decimal("0.625"),
    cache_write_1h=Decimal("1.00"),
)

PRICES: Final[Mapping[str, ModelPrice]] = MappingProxyType(
    {
        OPUS_5_5: ModelPrice(OPUS_5_5, RateCard.standard("4", "20", cache_read="0.20")),
        HAIKU_5_5: ModelPrice(
            HAIKU_5_5, _HAIKU_SHORT, long_context_card=_HAIKU_LONG, long_context_above=100_000
        ),
        OPUS_5: ModelPrice(OPUS_5, RateCard.standard("5", "25", cache_read="0.50")),
        OPUS_4_8: ModelPrice(OPUS_4_8, RateCard.standard("5", "25", cache_read="0.50")),
    }
)


def _dearest(cards: Iterable[RateCard]) -> RateCard:
    cards = tuple(cards)
    return RateCard(
        input=max(card.input for card in cards),
        output=max(card.output for card in cards),
        cache_read=max(card.cache_read for card in cards),
        cache_write_5m=max(card.cache_write_5m for card in cards),
        cache_write_1h=max(card.cache_write_1h for card in cards),
    )


# What an unpriced served model is charged: every component at the dearest known rate.
UNKNOWN_MODEL_PRICE: Final = ModelPrice(
    "unknown", _dearest(card for price in PRICES.values() for card in price.cards)
)


def price_of(model: str) -> ModelPrice:
    """The price of a model we may *send*; refuses models with no price."""
    price = PRICES.get(model)
    if price is None:
        raise BadRequest(f"no price for model {model!r}; refusing to send an unpriceable call")
    return price


def _price_for_billing(model: str | None) -> ModelPrice:
    price = PRICES.get(model) if model is not None else None
    if price is None:
        log.warning("no price for served model %r; charging the dearest known rate", model)
        return UNKNOWN_MODEL_PRICE
    return price


# --- Settled cost -------------------------------------------------------------------


def call_cost(
    usage: Mapping[str, JsonValue], *, requested_model: str, served_model: str
) -> Decimal:
    """What a call billed, from its raw `usage` object.

    With `usage.iterations`, each attempt is priced at its own model (a fallback
    attempt at the fallback model). An iteration that doesn't name its model is
    priced at the dearer of the requested and served models.
    """
    parsed = ParsedUsage.parse(usage)
    if parsed.iterations is not None:
        tokens_usd = sum(
            (_iteration_cost(it, requested_model, served_model) for it in parsed.iterations),
            Decimal(0),
        )
    else:
        tokens_usd = _price_for_billing(served_model).cost(parsed.total)
    if parsed.speed == "fast":
        tokens_usd *= FAST_MODE_MULTIPLIER
    return round_usd(tokens_usd + parsed.web_searches * WEB_SEARCH_USD)


def _iteration_cost(iteration: Iteration, requested_model: str, served_model: str) -> Decimal:
    if iteration.model is not None:
        return _price_for_billing(iteration.model).cost(iteration.tokens)
    return max(
        _price_for_billing(model).cost(iteration.tokens)
        for model in {requested_model, served_model}
    )


# --- Worst case, reserved before the call ---------------------------------------------


def estimate_input_tokens(body: Mapping[str, JsonValue]) -> int:
    return math.ceil(len(canonical_json(body).encode("utf-8")) / _BYTES_PER_TOKEN)


def worst_case_cost(body: Mapping[str, JsonValue]) -> Decimal:
    """The most a request can bill: every input token at the dearest input-side rate
    and `max_tokens` of output, on the requested model and on its fallback.

    Raises `BadRequest` for a model with no price, so an unpriceable call is never sent.
    """
    model = as_str(body.get("model"))
    max_tokens = as_int(body.get("max_tokens"))
    if model is None or max_tokens is None or max_tokens < 0:
        raise BadRequest("request body needs a string `model` and a non-negative `max_tokens`")
    input_tokens = estimate_input_tokens(body)
    total = _attempt_worst_case(price_of(model), input_tokens, max_tokens)

    fallbacks = body.get("fallbacks")
    if fallbacks == "default":
        targets = DEFAULT_FALLBACK_TARGETS.get(model)
        hop_prices = (
            [_price_for_billing(target) for target in targets] if targets else [UNKNOWN_MODEL_PRICE]
        )
        total += max(_attempt_worst_case(price, input_tokens, max_tokens) for price in hop_prices)
    elif fallbacks is not None:
        # The array form tries each entry in turn, so every hop can bill.
        for entry in objects(fallbacks):
            hop_tokens = as_int(entry.get("max_tokens")) or max_tokens
            hop_price = _price_for_billing(as_str(entry.get("model")))
            total += _attempt_worst_case(hop_price, input_tokens, hop_tokens)

    if body.get("speed") == "fast":
        total *= FAST_MODE_MULTIPLIER
    return round_usd(total + _web_search_budget(body) * WEB_SEARCH_USD)


def _attempt_worst_case(price: ModelPrice, input_tokens: int, output_tokens: int) -> Decimal:
    return max(
        (input_tokens * card.dearest_input + output_tokens * card.output) / _MTOK
        for card in price.cards
    )


def _web_search_budget(body: Mapping[str, JsonValue]) -> int:
    searches = 0
    for tool in objects(as_list(body.get("tools"))):
        tool_type = as_str(tool.get("type")) or ""
        if tool_type.startswith("web_search"):
            searches += as_int(tool.get("max_uses")) or UNBOUNDED_WEB_SEARCHES
    return searches


def usage_counts(usage: Mapping[str, JsonValue]) -> TokenCounts:
    """Token totals for a ledger line: the sum over iterations when present."""
    parsed = ParsedUsage.parse(usage)
    if parsed.iterations is None:
        return parsed.total
    return TokenCounts(
        input_tokens=sum(it.tokens.input_tokens for it in parsed.iterations),
        output_tokens=sum(it.tokens.output_tokens for it in parsed.iterations),
        cache_read_input_tokens=sum(it.tokens.cache_read_input_tokens for it in parsed.iterations),
        cache_write_5m_tokens=sum(it.tokens.cache_write_5m_tokens for it in parsed.iterations),
        cache_write_1h_tokens=sum(it.tokens.cache_write_1h_tokens for it in parsed.iterations),
    )


def web_searches(usage: Mapping[str, JsonValue]) -> int:
    return ParsedUsage.parse(usage).web_searches


__all__ = [
    "DEFAULT_FALLBACK_TARGETS",
    "HAIKU_5_5",
    "OPUS_4_8",
    "OPUS_5",
    "OPUS_5_5",
    "PRICES",
    "UNKNOWN_MODEL_PRICE",
    "WEB_SEARCH_USD",
    "ModelPrice",
    "RateCard",
    "call_cost",
    "estimate_input_tokens",
    "price_of",
    "round_usd",
    "usage_counts",
    "web_searches",
    "worst_case_cost",
]
