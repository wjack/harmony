"""Internal Claude's token stream (`events/stream.ChatStream`) and who receives it
(`events/sse.chat_stream_view`): the conversation's owner and the network lens only."""

from __future__ import annotations

from uuid import UUID, uuid4

from harmony.events.bus import StreamMessage
from harmony.events.schemas import StreamChatDelta
from harmony.events.sse import Viewer, chat_stream_view
from harmony.events.stream import MAX_DELTA_CHARS, ChatStream

WORLD, ORG, OWNER, CONVERSATION, TURN = (uuid4() for _ in range(5))


class Collect:
    def __init__(self) -> None:
        self.messages: list[StreamMessage] = []

    async def publish(self, message: StreamMessage) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        return None


def _stream(publisher: Collect, *, attempt: int = 1, enabled: bool = True) -> ChatStream:
    return ChatStream(
        publisher,
        world_id=WORLD,
        org_id=ORG,
        member_id=OWNER,
        conversation_id=CONVERSATION,
        turn_id=TURN,
        attempt=attempt,
        enabled=enabled,
        # Long enough that only the exit flush runs.
        interval_s=60,
    )


async def test_a_chat_turn_streams_coalesced_deltas_to_its_owner() -> None:
    publisher = Collect()
    async with _stream(publisher, attempt=2) as out:
        out.push("Screening ")
        out.push("the network…")
    (message,) = publisher.messages
    assert message.world_id == WORLD
    # No typing or reset events: a higher `attempt` alone tells the client to restart.
    assert message.event == StreamChatDelta(
        org_id=ORG,
        member_id=OWNER,
        conversation_id=CONVERSATION,
        turn_id=TURN,
        attempt=2,
        text="Screening the network…",
    )


async def test_long_output_is_split_and_a_disabled_stream_sends_nothing() -> None:
    publisher = Collect()
    async with _stream(publisher) as out:
        out.push("x" * (MAX_DELTA_CHARS + 1))
    texts = [m.event.text for m in publisher.messages if isinstance(m.event, StreamChatDelta)]
    assert [len(text) for text in texts] == [MAX_DELTA_CHARS, 1]

    quiet = Collect()
    async with _stream(quiet, enabled=False) as out:
        out.push("unwatched")
    assert quiet.messages == []


def _viewer(member_id: UUID, *, lens: bool = False) -> Viewer:
    return Viewer(
        session_id=uuid4(), epoch=1, member_id=member_id, org_id=ORG, role="admin", lens=lens
    )


def test_only_the_owner_and_the_lens_see_a_chat() -> None:
    delta = StreamChatDelta(
        org_id=ORG,
        member_id=OWNER,
        conversation_id=CONVERSATION,
        turn_id=TURN,
        attempt=1,
        text="hi",
    )
    assert chat_stream_view(delta, _viewer(OWNER)) == delta
    # Not even an admin of the same org.
    assert chat_stream_view(delta, _viewer(uuid4())) is None
    assert chat_stream_view(delta, _viewer(uuid4(), lens=True)) == delta
