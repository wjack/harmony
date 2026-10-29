"""Why a model activity failed, carried across the activity boundary (3.5).

An exception an activity doesn't catch reaches the workflow as an `ApplicationError`
with only its class name and message. A refused reservation is therefore re-raised
with what refused it in its details (`budget_refused`), and the workflows read
it back (`failure_reason`) into the `FailureReason` the chat and the thread show.

Workflow code imports this module inside `workflow.unsafe.imports_passed_through()`.
"""

from __future__ import annotations

from pydantic import ValidationError
from temporalio.exceptions import ActivityError, ApplicationError

from harmony.db.models.choices import BudgetKind, FailureReason
from harmony.llm.errors import BudgetExhausted
from harmony.temporal.workflows.payloads import (
    BUDGET_EXHAUSTED_ERROR,
    BUDGET_FAILURES,
    BudgetExhaustedDetails,
)

# A refusal whose details can't be read (a worker from before 3.5) is still a refusal:
# the visitor is told the demo's budget is used up rather than shown a model error.
_UNREADABLE_KIND: BudgetKind = "global"


def budget_refused(error: BudgetExhausted) -> ApplicationError:
    """The non-retryable error a model activity raises for a refused reservation."""
    return ApplicationError(
        str(error),
        BudgetExhaustedDetails(kind=error.kind).model_dump(),
        type=BUDGET_EXHAUSTED_ERROR,
        non_retryable=True,
    )


def failure_reason(error: ActivityError) -> FailureReason:
    """Why a model activity failed for good: what refused a refused reservation, and
    `model_error` for anything else (a 400, a call that failed on every retry, an
    incomplete turn)."""
    cause = error.cause
    if isinstance(cause, ApplicationError) and cause.type == BUDGET_EXHAUSTED_ERROR:
        return BUDGET_FAILURES[_refusing_kind(cause)]
    return "model_error"


def _refusing_kind(cause: ApplicationError) -> BudgetKind:
    if not cause.details:
        return _UNREADABLE_KIND
    try:
        return BudgetExhaustedDetails.model_validate(cause.details[0]).kind
    except ValidationError:
        return _UNREADABLE_KIND


__all__ = ["budget_refused", "failure_reason"]
