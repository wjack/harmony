"""The API's retirer: Temporal when reachable, inline when Temporal is down."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from temporalio.service import RPCError, RPCStatusCode

from harmony.worlds.retirer_temporal import FallbackRetirer, temporal_unreachable


class _Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.retired: list[UUID] = []

    async def retire(self, world_id: UUID) -> None:
        self.retired.append(world_id)
        if self.error is not None:
            raise self.error


UNREACHABLE = [
    RuntimeError("Failed client connect: Server connection error: tcp connect error"),
    RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b""),
    RPCError("deadline", RPCStatusCode.DEADLINE_EXCEEDED, b""),
]


@pytest.mark.parametrize("error", UNREACHABLE, ids=["lazy-connect", "unavailable", "deadline"])
async def test_an_unreachable_temporal_falls_back_to_retiring_inline(error: Exception) -> None:
    primary, fallback = _Recorder(error), _Recorder()
    world_id = uuid4()
    await FallbackRetirer(primary, fallback).retire(world_id)
    assert primary.retired == [world_id]
    assert fallback.retired == [world_id]


@pytest.mark.parametrize(
    "error",
    [
        RPCError("denied", RPCStatusCode.PERMISSION_DENIED, b""),
        RuntimeError("something else broke"),
        ValueError("bad input"),
    ],
)
async def test_other_failures_propagate_without_a_fallback(error: Exception) -> None:
    primary, fallback = _Recorder(error), _Recorder()
    with pytest.raises(type(error)):
        await FallbackRetirer(primary, fallback).retire(uuid4())
    assert fallback.retired == []


async def test_a_reachable_temporal_is_the_only_retirer() -> None:
    primary, fallback = _Recorder(), _Recorder()
    await FallbackRetirer(primary, fallback).retire(uuid4())
    assert len(primary.retired) == 1
    assert fallback.retired == []


def test_unreachable_is_recognised_only_for_connection_failures() -> None:
    assert all(temporal_unreachable(error) for error in UNREACHABLE)
    assert not temporal_unreachable(RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
