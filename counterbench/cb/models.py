"""Per-model request settings, prices and token accounting."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# One ledger for every paid call, shared with Harmony: SPEND_LEDGER_PATH, else the folder that holds the
# repository (Harmony's default). run.py's --cap applies to its cumulative total.
LEDGER = Path(os.environ.get("SPEND_LEDGER_PATH") or ROOT.parents[1] / "spend_ledger.jsonl")

# Each model gets the request settings it requires: Haiku 4.5 takes a fixed thinking budget; the 5.5 models take
# an effort level (their thinking is adaptive and always on).
SETTINGS = {
    "claude-haiku-4-5": {"thinking": {"type": "enabled", "budget_tokens": 8000}},
    "claude-haiku-5-5": {"output_config": {"effort": "high"}},
    "claude-sonnet-5-5": {"output_config": {"effort": "high"}},
    "claude-opus-5-5": {"output_config": {"effort": "high"}},
}

# $ per million tokens: input, output, cache read. Cache writes cost 1.25x input.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
}


def settings(model: str) -> dict:
    if model not in SETTINGS:
        raise SystemExit(f"No settings for model {model!r}; add it to cb/models.py.")
    return SETTINGS[model]


def check_served_model(requested: str, response) -> None:
    """Fails loudly if the API served a different model (no fallbacks are configured)."""
    if response.model != requested and not response.model.startswith(requested):
        raise RuntimeError(f"Served model {response.model} differs from requested {requested}.")


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0

    def add(self, u) -> None:
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.calls += 1

    def cost(self) -> float:
        p_in, p_out, p_read = PRICES[self.model]
        return (self.input_tokens * p_in + self.cache_write_tokens * p_in * 1.25
                + self.cache_read_tokens * p_read + self.output_tokens * p_out) / 1e6

    def as_dict(self) -> dict:
        return {"model": self.model, "calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cache_write_tokens": self.cache_write_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cost_usd": round(self.cost(), 5)}
