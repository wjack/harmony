"""Reading the API's `usage` objects, from final messages and from stream events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from harmony.llm.wire import JsonObject, as_int, as_object, as_str, objects


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Billable token counts for one sampling iteration (or a whole call)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def cache_creation_input_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def prompt_tokens(self) -> int:
        """Every input-side token; Haiku 5.5's rate card is chosen by this."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @classmethod
    def from_usage(cls, usage: Mapping[str, JsonValue]) -> TokenCounts:
        written = _count(usage, "cache_creation_input_tokens")
        breakdown = as_object(usage.get("cache_creation"))
        if breakdown is None:
            # No TTL breakdown: the default (and only TTL our builders use) is 5 minutes.
            write_5m, write_1h = written, 0
        else:
            write_5m = _count(breakdown, "ephemeral_5m_input_tokens")
            write_1h = _count(breakdown, "ephemeral_1h_input_tokens")
            # Writes the breakdown doesn't attribute are priced at the dearer TTL.
            write_1h += max(0, written - write_5m - write_1h)
        return cls(
            input_tokens=_count(usage, "input_tokens"),
            output_tokens=_count(usage, "output_tokens"),
            cache_read_input_tokens=_count(usage, "cache_read_input_tokens"),
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
        )


@dataclass(frozen=True, slots=True)
class Iteration:
    """One `usage.iterations` entry: `message`, `fallback_message`, `advisor_message`
    or `compaction`. `model` is None when the API leaves it out."""

    kind: str
    model: str | None
    tokens: TokenCounts


@dataclass(frozen=True, slots=True)
class ParsedUsage:
    total: TokenCounts
    # Present when the API reports per-attempt usage (fallbacks, compaction, server
    # tool loops). It is then the source of truth: top-level usage covers only the
    # attempt that produced the returned message.
    iterations: tuple[Iteration, ...] | None
    web_searches: int
    speed: str | None

    @classmethod
    def parse(cls, usage: Mapping[str, JsonValue]) -> ParsedUsage:
        raw_iterations = usage.get("iterations")
        iterations = (
            tuple(
                Iteration(
                    kind=as_str(entry.get("type")) or "message",
                    model=as_str(entry.get("model")),
                    tokens=TokenCounts.from_usage(entry),
                )
                for entry in objects(raw_iterations)
            )
            if raw_iterations is not None
            else None
        )
        server_tools = as_object(usage.get("server_tool_use")) or {}
        return cls(
            total=TokenCounts.from_usage(usage),
            iterations=iterations or None,
            web_searches=_count(server_tools, "web_search_requests"),
            speed=as_str(usage.get("speed")),
        )


class StreamUsageTracker:
    """Follows raw stream events and keeps the usage billed so far.

    `message_start` carries the input-side usage; each `message_delta` carries
    cumulative totals that overwrite it (fields it omits keep their earlier value).
    A `fallback` content block names the model that took over.

    Output tokens are only reported by `message_delta`, at the end of the stream:
    until then `message_start`'s `output_tokens` is a placeholder (typically 1), so
    usage read from a stream that broke off earlier undercounts what was generated
    (`output_reported` is False).
    """

    def __init__(self) -> None:
        self._usage: JsonObject | None = None
        self._model: str | None = None
        self._output_reported = False

    @property
    def usage(self) -> JsonObject | None:
        return dict(self._usage) if self._usage is not None else None

    @property
    def served_model(self) -> str | None:
        return self._model

    @property
    def output_reported(self) -> bool:
        """Whether `usage` includes the output tokens generated (a `message_delta` was seen)."""
        return self._output_reported

    def observe(self, event: Mapping[str, JsonValue]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = as_object(event.get("message")) or {}
            self._usage = dict(as_object(message.get("usage")) or {})
            self._model = as_str(message.get("model")) or self._model
        elif kind == "message_delta":
            delta_usage = as_object(event.get("usage")) or {}
            merged = dict(self._usage or {})
            merged.update({key: value for key, value in delta_usage.items() if value is not None})
            self._usage = merged
            self._output_reported = True
        elif kind == "content_block_start":
            block = as_object(event.get("content_block")) or {}
            if block.get("type") == "fallback":
                target = as_object(block.get("to")) or {}
                self._model = as_str(target.get("model")) or self._model


def _count(source: Mapping[str, JsonValue], key: str) -> int:
    return max(0, as_int(source.get(key)) or 0)
