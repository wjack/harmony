"""The retry policy for model activities (3.5) and where they run (3.7)."""

from __future__ import annotations

from datetime import timedelta

from harmony.temporal.policies import (
    CHAT_TASK_QUEUE,
    MODEL_NON_RETRYABLE,
    MODEL_TASK_QUEUE,
    WORLD_RETIRED,
    chat_activity_options,
    model_activity_options,
    model_retry_policy,
)


def test_a_failed_model_call_gets_six_attempts_backing_off_to_a_minute() -> None:
    policy = model_retry_policy()
    assert policy.maximum_attempts == 6
    assert policy.initial_interval == timedelta(seconds=5)
    assert policy.backoff_coefficient == 2.0
    assert policy.maximum_interval == timedelta(seconds=60)


def test_what_never_succeeds_on_a_retry_stops_at_once() -> None:
    assert MODEL_NON_RETRYABLE == (
        "BadRequest",
        "BadRequestError",
        "BudgetExhausted",
        "LedgerUnavailable",
        "GuardMismatch",
        "TurnIncomplete",
        "TranscriptConflict",
        WORLD_RETIRED,
    )
    assert model_retry_policy().non_retryable_error_types == list(MODEL_NON_RETRYABLE)


def test_internal_turns_run_on_their_own_queue_with_the_model_policy() -> None:
    chat, model = chat_activity_options(), model_activity_options()
    assert (chat.get("task_queue"), model.get("task_queue")) == (CHAT_TASK_QUEUE, MODEL_TASK_QUEUE)
    assert chat.get("retry_policy") == model.get("retry_policy") == model_retry_policy()
    assert chat.get("start_to_close_timeout") == model.get("start_to_close_timeout")
    assert chat.get("heartbeat_timeout") == model.get("heartbeat_timeout")
