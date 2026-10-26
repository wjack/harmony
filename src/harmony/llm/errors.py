"""Errors raised by the model layer.

Temporal matches non-retryable errors by the raised class's *name* (see
`harmony.errors`), so these names are part of the contract with
`temporal/policies.py`: `BadRequest`, `BudgetExhausted` and `LedgerUnavailable` are
non-retryable; `ModelCallError` itself is retryable.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from pydantic import JsonValue

from harmony.errors import HarmonyError


class ModelCallError(HarmonyError):
    """A model call failed. Retryable: rate limits, overload, 5xx, dropped streams.

    `partial_usage` is the usage billed before the failure (`message_start` merged
    with the last `message_delta`), or None when nothing was billed. The checkpoint
    settles it against the budget before re-raising, so failed calls still count.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_usage: Mapping[str, JsonValue] | None = None,
        served_model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_usage = dict(partial_usage) if partial_usage is not None else None
        self.served_model = served_model


class BadRequest(ModelCallError):
    """The same request will fail again: a 4xx other than 408/409/429, a request no
    builder supports, or a cassette with no entry for the call. Non-retryable."""


class BudgetExhausted(HarmonyError):
    """A reservation would exceed a spend cap. Non-retryable."""

    def __init__(
        self,
        message: str,
        *,
        budget: str,
        cap_usd: Decimal,
        committed_usd: Decimal,
        requested_usd: Decimal,
    ) -> None:
        super().__init__(message)
        self.budget = budget
        self.cap_usd = cap_usd
        self.committed_usd = committed_usd
        self.requested_usd = requested_usd


class LedgerUnavailable(HarmonyError):
    """The spend ledger is missing, unreadable, unwritable, truncated or corrupt.

    Live calls fail closed on it: the guard never creates the ledger and never
    guesses at a total it cannot read. Non-retryable.
    """
