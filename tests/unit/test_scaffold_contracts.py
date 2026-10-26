"""The small pieces of the shared interfaces that carry real logic."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harmony.auth.session import read_session_id, sign_session_id
from harmony.events.schemas import (
    EventEnvelope,
    EventScope,
    HarmonyEvent,
    StreamDelta,
    WorldCreated,
)
from harmony.storage.base import ObjectKey


def test_scopes_require_their_ids() -> None:
    with pytest.raises(ValidationError, match="thread_id"):
        EventScope(kind="thread", org_id=uuid4())
    with pytest.raises(ValidationError, match="member_id"):
        EventScope(kind="member")
    assert EventScope.thread(uuid4(), uuid4()).kind == "thread"
    assert EventScope.demo().org_id is None


def test_events_round_trip_through_the_discriminated_union() -> None:
    world_id, org_id, thread_id, turn_id = uuid4(), uuid4(), uuid4(), uuid4()
    delta = StreamDelta(org_id=org_id, thread_id=thread_id, turn_id=turn_id, attempt=1, text="Hi")
    envelope = EventEnvelope(
        world_id=world_id,
        seq=None,
        created_at=None,
        scope=EventScope.thread(org_id, thread_id),
        event=delta,
    )
    assert EventEnvelope.model_validate_json(envelope.model_dump_json()) == envelope

    parsed = TypeAdapter(HarmonyEvent).validate_python(
        {"type": "world.created", "scenario": "harmony-demo", "mode": "live", "build_sha": "x"}
    )
    assert isinstance(parsed, WorldCreated)


def test_events_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(HarmonyEvent).validate_python({"type": "world.retired", "surprise": 1})


@pytest.mark.parametrize("name", ["", "../x", "/abs", "a//b", "a/./b", "a/..", ".hidden"])
def test_object_keys_reject_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="invalid object name"):
        ObjectKey(world_id=uuid4(), org_id=uuid4(), name=name)


def test_object_key_path_is_world_then_org() -> None:
    world_id, org_id = uuid4(), uuid4()
    key = ObjectKey(world_id=world_id, org_id=org_id, name="docs/report-v2.pdf")
    assert key.path == f"worlds/{world_id}/orgs/{org_id}/docs/report-v2.pdf"


def test_session_cookie_round_trips_and_rejects_tampering() -> None:
    session_id = uuid4()
    token = sign_session_id(session_id, "secret-a")
    assert read_session_id(token, "secret-a") == session_id
    assert read_session_id(token, "secret-b") is None
    other_payload = sign_session_id(uuid4(), "secret-a").split(".")[0]
    forged = other_payload + "." + token.split(".")[1]
    assert read_session_id(forged, "secret-a") is None
    assert read_session_id("garbage", "secret-a") is None
