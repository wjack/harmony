"""`agents.runtime.run_turn`: the checkpointed tool loop (§5), on `ScriptedModel` with
its strict request contract (message order, system placement, byte-prefix extension
between consecutive requests of one conversation, `llm/contract.py`) plus the API's
tool_use/tool_result pairing (`tests.unit._turns.tool_pairing`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID, uuid5

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from harmony.agents.runtime import (
    INTERRUPTED,
    NOT_RUN,
    CardRaised,
    ConsultOpened,
    OwedResult,
    TurnIncomplete,
    echo_content,
    opening_blocks,
    run_turn,
    text_block,
    tool_calls,
    tool_result_block,
)
from harmony.agents.transcript import ConversationTranscript, TranscriptConflict, WireMessage
from harmony.llm import ModelRequest
from harmony.llm.contract import check_requests
from harmony.llm.scripted import ScriptedBlock, ScriptedModel
from harmony.llm.wire import JsonObject, canonical_json
from tests.unit._turns import (
    NAMESPACE,
    Checkpointed,
    Executor,
    MemoryTranscripts,
    cassette,
    say,
    tool_pairing,
    turn_context,
    use,
)

WORLD = UUID(int=11)
NOTICE = "Notice from Northlake Characterization Core: stop using the public calendar."


def turn_id(n: int, label: str = "") -> UUID:
    return uuid5(NAMESPACE, f"turn/{label}{n}")


def opening(text: str = "Can you run XRD on 12 powders by Nov 6?") -> list[JsonObject]:
    return [text_block(text)]


def roles(messages: list[WireMessage]) -> list[str]:
    return [message.role for message in messages]


def sent_messages(model: Checkpointed, index: int = -1) -> list[JsonValue]:
    messages = model.requests[index].body["messages"]
    assert isinstance(messages, list)
    return messages


def unpaired(requests: Sequence[ModelRequest]) -> list[str]:
    """Pairing problems across every recorded request."""
    found: list[str] = []
    for index, request in enumerate(requests):
        messages = request.body["messages"]
        assert isinstance(messages, list)
        found += [
            f"[{index}] {p}" for p in tool_pairing([m for m in messages if isinstance(m, dict)])
        ]
    return found


def conversation(label: str = "c") -> ConversationTranscript:
    return ConversationTranscript(world_id=WORLD, conversation_id=uuid5(NAMESPACE, label))


# --- One turn ------------------------------------------------------------------------


async def test_close_ends_the_turn_and_owes_its_result() -> None:
    model = Checkpointed(
        ScriptedModel(cassette({(1, 0): [use("close", "toolu_a", summary="Booked Nov 4.")]}))
    )
    store, executor = MemoryTranscripts(), Executor()
    ctx = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening())

    outcome = await run_turn(ctx, executor, model, transcript=store)

    assert outcome.stop == "performative"
    assert outcome.performative == "close"
    assert outcome.owed == OwedResult(tool_use_id="toolu_a", content="Closed.")
    assert (outcome.next_step, outcome.calls) == (1, 1)
    assert executor.executed == [("close", 0)]
    stored = store.messages(ctx.transcript)
    assert roles(stored) == ["user", "assistant"]
    assert stored[0].content() == list[JsonValue](opening())
    body = model.requests[0].body
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


async def test_a_turn_sends_several_messages_and_ends_when_the_model_stops() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [use("send_message", "toolu_1", text="Yes, we can run XRD.")],
                    (1, 1): [use("send_message", "toolu_2", text="Results by Nov 6.")],
                    (1, 2): [say("Both sent; waiting for Henderson.")],
                }
            ),
            chunk_chars=5,
        )
    )
    store, executor, shown = MemoryTranscripts(), Executor(), list[str]()
    ctx = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening())

    outcome = await run_turn(ctx, executor, model, transcript=store, on_text=shown.append)

    assert (outcome.stop, outcome.performative, outcome.owed) == ("end_turn", None, None)
    assert [effect.kind for effect in outcome.effects] == ["message_sent", "message_sent"]
    assert executor.executed == [("send_message", 0), ("send_message", 1)]
    # The closing text is the model's own: proposed as a reply, refused (the turn has
    # sent), and never shown live.
    assert executor.replies == [("Both sent; waiting for Henderson.", 2)]
    assert "".join(shown) == "Yes, we can run XRD.\n\nResults by Nov 6."
    stored = store.messages(ctx.transcript)
    assert roles(stored) == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert stored[2].content() == [tool_result_block("toolu_1", "Sent.")]
    assert check_requests(model.requests) == []


async def test_text_alongside_consult_is_dropped() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [
                        say("Sure, Thursday works for Voltaic's slot too."),
                        use("consult", "toolu_c", question="Can we book Nov 4?"),
                    ]
                }
            )
        )
    )
    executor = Executor()
    ctx = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening())
    shown: list[str] = []

    outcome = await run_turn(
        ctx, executor, model, transcript=MemoryTranscripts(), on_text=shown.append
    )

    assert outcome.performative == "consult"
    assert outcome.owed == OwedResult(tool_use_id="toolu_c", content=None)
    assert isinstance(outcome.effects[0], ConsultOpened)
    # The text never reaches the counterparty: no text reply, no send_message, and
    # nothing shown live.
    assert executor.replies == []
    assert executor.executed == [("consult", 0)]
    assert shown == []


async def test_a_representative_streams_only_what_it_sends() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [say("Let me answer."), use("send_message", "toolu_s", text="Yes.")],
                    (1, 1): [say("Done.")],
                    (2, 0): [say("We can do Nov 4.")],
                }
            ),
            chunk_chars=3,
        )
    )
    store, shown = MemoryTranscripts(), list[str]()
    first = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening())
    await run_turn(first, Executor(), model, transcript=store, on_text=shown.append)
    assert "".join(shown) == "Yes."

    shown.clear()
    second = turn_context(
        world_id=WORLD, turn=2, turn_id=turn_id(2), opening=[text_block("Thursday?")]
    )
    await run_turn(second, Executor(), model, transcript=store, on_text=shown.append)
    # A bare reply is shown once its response has ended without a tool call.
    assert "".join(shown) == "We can do Nov 4."


async def test_a_bare_text_reply_maps_to_send_message() -> None:
    model = Checkpointed(ScriptedModel(cassette({(1, 0): [say("We can do Nov 4.")]})))
    executor = Executor()
    ctx = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening())

    outcome = await run_turn(ctx, executor, model, transcript=MemoryTranscripts())

    assert (outcome.stop, outcome.performative, outcome.owed) == ("end_turn", None, None)
    assert [effect.kind for effect in outcome.effects] == ["message_sent"]
    assert executor.replies == [("We can do Nov 4.", 0)]


async def test_internal_claude_ends_on_a_reply_with_its_text() -> None:
    conversation = ConversationTranscript(world_id=WORLD, conversation_id=uuid5(NAMESPACE, "c"))
    model = Checkpointed(
        ScriptedModel(cassette({(1, 0): [say("Here is the shortlist.")]}, role="internal"))
    )
    executor, store = Executor(role="internal"), MemoryTranscripts()
    member = uuid5(NAMESPACE, "alex")
    ctx = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Find me an XRD lab."),
        role="internal",
        transcript=conversation,
        author=member,
    )

    outcome = await run_turn(ctx, executor, model, transcript=store)

    assert (outcome.stop, outcome.final_text) == ("end_turn", "Here is the shortlist.")
    assert store.authors[f"c:{conversation.conversation_id}"] == [member, None]
    assert "tool_choice" not in model.requests[0].body or model.requests[0].body["tool_choice"] == {
        "type": "auto"
    }


async def test_tool_results_go_back_and_notices_follow_the_opening() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [say("Let me check."), use("lookup", "toolu_1")],
                    (1, 1): [use("consult", "toolu_2", question="Is Nov 4 open?")],
                }
            )
        )
    )
    store, executor = MemoryTranscripts(), Executor()
    ctx = turn_context(
        world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening(), notices=NOTICE
    )

    outcome = await run_turn(ctx, executor, model, transcript=store)

    assert outcome.next_step == 2
    stored = store.messages(ctx.transcript)
    assert roles(stored) == ["user", "system", "assistant", "user", "assistant"]
    assert stored[1].content() == [{"type": "text", "text": NOTICE}]
    assert stored[3].content() == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "lookup ok"}
    ]
    # The second request is the first plus the response and the results: notices are
    # sent once, right after the opening.
    assert len(model.requests) == 2
    assert check_requests(model.requests) == []
    assert canonical_json(sent_messages(model)) == canonical_json(
        [message.as_json() for message in stored[:4]]
    )


async def test_calls_after_the_ending_call_are_not_run() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [
                        use("lookup", "toolu_1"),
                        use("send_network_request", "toolu_2"),
                        use("lookup", "toolu_3"),
                    ]
                },
                role="internal",
            )
        )
    )
    store = MemoryTranscripts()
    executor = Executor(role="internal", cards=["send_network_request"])
    conversation = ConversationTranscript(world_id=WORLD, conversation_id=uuid5(NAMESPACE, "c"))
    ctx = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Ask the XRD labs."),
        role="internal",
        transcript=conversation,
    )

    outcome = await run_turn(ctx, executor, model, transcript=store)

    assert outcome.stop == "awaiting_card"
    assert outcome.owed == OwedResult(tool_use_id="toolu_2", content=None)
    assert isinstance(outcome.effects[-1], CardRaised)
    assert executor.executed == [("lookup", 0), ("send_network_request", 0)]
    stored = store.messages(conversation)
    # The known results are stored at the stop; the card's comes with the resume.
    assert roles(stored) == ["user", "assistant", "user"]
    assert stored[2].content() == [
        tool_result_block("toolu_1", "lookup ok"),
        tool_result_block("toolu_3", NOT_RUN.format(name="send_network_request"), is_error=True),
    ]


async def test_a_card_pauses_the_turn_and_resumes_at_the_next_step() -> None:
    conversation = ConversationTranscript(world_id=WORLD, conversation_id=uuid5(NAMESPACE, "c"))
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [say("Here is the brief."), use("send_network_request", "toolu_card")],
                    (1, 1): [say("Sent to Northlake and Ridgeline.")],
                },
                role="internal",
            )
        )
    )
    store = MemoryTranscripts()
    executor = Executor(role="internal", cards=["send_network_request"])
    first = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Send it."),
        role="internal",
        transcript=conversation,
        notices="Today's date: Monday, October 26, 2026.",
    )

    paused = await run_turn(first, executor, model, transcript=store)
    assert (paused.stop, paused.next_step) == ("awaiting_card", 1)

    resumed = first.model_copy(
        update={
            "opening": [tool_result_block("toolu_card", "Approved by Alex.")],
            "first_step": paused.next_step,
            "notices": None,
        }
    )
    done = await run_turn(resumed, executor, model, transcript=store)

    assert (done.stop, done.next_step, done.final_text) == (
        "end_turn",
        2,
        "Sent to Northlake and Ridgeline.",
    )
    assert [r.key.step_idx for r in model.requests] == [0, 1]
    assert check_requests(model.requests) == []
    assert roles(store.messages(conversation)) == [
        "user",
        "system",
        "assistant",
        "user",
        "assistant",
    ]


async def test_a_card_on_the_last_step_is_still_answered() -> None:
    steps: dict[tuple[int, int], list[ScriptedBlock]] = {
        (1, 0): [use("search_network", "toolu_s0")],
        (1, 1): [use("search_network", "toolu_s1")],
        (1, 2): [use("send_network_request", "toolu_card")],
        (1, 3): [say("Sent to Northlake and Ridgeline.")],
    }
    model = Checkpointed(ScriptedModel(cassette(steps, role="internal")))
    store, chat = MemoryTranscripts(), conversation()
    executor = Executor(role="internal", cards=["send_network_request"])
    first = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Send it."),
        role="internal",
        transcript=chat,
        max_steps=3,
    )

    paused = await run_turn(first, executor, model, transcript=store)
    assert (paused.stop, paused.next_step) == ("awaiting_card", 3)

    resumed = first.model_copy(
        update={
            "opening": [tool_result_block("toolu_card", "Approved by Alex.")],
            "first_step": paused.next_step,
        }
    )
    done = await run_turn(resumed, executor, model, transcript=store)

    # The resumed turn makes its one call past the cap, so the card's result is stored
    # and answered instead of left dangling.
    assert (done.stop, done.next_step, done.calls) == ("end_turn", 4, 1)
    assert done.final_text == "Sent to Northlake and Ridgeline."
    stored = store.messages(chat)
    assert stored[-2].content() == [tool_result_block("toolu_card", "Approved by Alex.")]
    assert check_requests(model.requests) == []
    assert unpaired(model.requests) == []
    assert tool_pairing([m.as_json() for m in stored]) == []


async def test_max_steps_stops_with_the_last_results_stored() -> None:
    steps: dict[tuple[int, int], list[ScriptedBlock]] = {
        (1, step): [use("lookup", f"toolu_{step}")] for step in range(3)
    }
    model = Checkpointed(ScriptedModel(cassette(steps)))
    store = MemoryTranscripts()
    ctx = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening(), max_steps=3)

    outcome = await run_turn(ctx, Executor(), model, transcript=store)

    assert (outcome.stop, outcome.next_step, outcome.calls) == ("max_steps", 3, 3)
    stored = store.messages(ctx.transcript)
    assert roles(stored) == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert stored[-1].content() == [tool_result_block("toolu_2", "lookup ok")]


async def test_a_refusal_keeps_the_input_but_not_the_notices() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {(1, 0): []},
                stop={(1, 0): ("refusal", {"category": "cyber"})},
            )
        )
    )
    store = MemoryTranscripts()
    ctx = turn_context(
        world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening(), notices=NOTICE
    )

    outcome = await run_turn(ctx, Executor(), model, transcript=store)

    assert (outcome.stop, outcome.refusal_category, outcome.next_step) == ("refusal", "cyber", 0)
    # A stored system message must be followed by an assistant turn, so the notices of
    # a refused call are dropped; the input is consumed.
    assert roles(store.messages(ctx.transcript)) == ["user"]


@pytest.mark.parametrize("reason", ["max_tokens", "pause_turn"])
async def test_an_incomplete_response_consumes_its_input_only(reason: str) -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {(1, 0): [use("send_message", "toolu_x", text="Half a")]},
                stop={(1, 0): (reason, None)},
            )
        )
    )
    store = MemoryTranscripts()
    ctx = turn_context(
        world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening(), notices=NOTICE
    )

    with pytest.raises(TurnIncomplete) as raised:
        await run_turn(ctx, Executor(), model, transcript=store)
    # A retry replays the same response: same error, nothing stored twice.
    with pytest.raises(TurnIncomplete):
        await run_turn(ctx.model_copy(update={"attempt": 2}), Executor(), model, transcript=store)

    assert raised.value.stop_reason == reason
    stored = store.messages(ctx.transcript)
    assert roles(stored) == ["user"]
    assert stored[0].content() == list[JsonValue](opening())


async def test_an_incomplete_response_after_a_tool_step_keeps_its_results() -> None:
    chat = conversation()
    steps = {
        (1, 0): [use("search_network", "toolu_s0")],
        (1, 1): [say("Here is a long ans")],
        (2, 0): [say("Two labs fit.")],
    }
    model = Checkpointed(
        ScriptedModel(cassette(steps, role="internal", stop={(1, 1): ("max_tokens", None)}))
    )
    store, executor = MemoryTranscripts(), Executor(role="internal")
    first = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Find an XRD lab."),
        role="internal",
        transcript=chat,
    )

    with pytest.raises(TurnIncomplete):
        await run_turn(first, executor, model, transcript=store)
    assert roles(store.messages(chat)) == ["user", "assistant", "user"]
    assert store.messages(chat)[-1].content() == [
        tool_result_block("toolu_s0", "search_network ok")
    ]

    second = turn_context(
        world_id=WORLD,
        turn=2,
        turn_id=turn_id(2),
        opening=opening("Any luck?"),
        role="internal",
        transcript=chat,
    )
    outcome = await run_turn(second, executor, model, transcript=store)

    assert outcome.final_text == "Two labs fit."
    assert check_requests(model.requests) == []
    assert unpaired(model.requests) == []


async def test_a_failed_call_consumes_its_input_and_a_retry_completes_the_turn() -> None:
    chat = conversation()
    ctx = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Find an XRD lab."),
        role="internal",
        transcript=chat,
        notices="Today's date: Monday, October 26, 2026.",
    )
    steps = {(1, 0): [use("search_network", "toolu_s0")], (1, 1): [say("Two labs fit.")]}
    model = Checkpointed(
        ScriptedModel(cassette(steps, role="internal")),
        fail_once=[(ctx.turn_id, 0), (ctx.turn_id, 1)],
    )
    store, executor = MemoryTranscripts(), Executor(role="internal")

    with pytest.raises(RuntimeError):
        await run_turn(ctx, executor, model, transcript=store)
    # The member's message is kept, the notices are not (nothing may follow them).
    assert roles(store.messages(chat)) == ["user"]
    with pytest.raises(RuntimeError):
        await run_turn(ctx.model_copy(update={"attempt": 2}), executor, model, transcript=store)
    # The search ran; its result is stored, so a failed turn can't strand the call.
    assert roles(store.messages(chat)) == ["user", "system", "assistant", "user"]

    outcome = await run_turn(
        ctx.model_copy(update={"attempt": 3}), executor, model, transcript=store
    )

    assert outcome.final_text == "Two labs fit."
    assert roles(store.messages(chat)) == ["user", "system", "assistant", "user", "assistant"]
    # The rerun processes the stored response again (executors are idempotent).
    assert executor.executed == [("search_network", 0)] * 2
    assert check_requests(model.requests) == []
    assert unpaired(model.requests) == []


async def test_a_new_turn_answers_the_calls_an_interrupted_turn_left_open() -> None:
    chat = conversation()
    steps = {(1, 0): [use("search_network", "toolu_s0")], (2, 0): [say("Let me search again.")]}
    model = Checkpointed(ScriptedModel(cassette(steps, role="internal")))
    store = MemoryTranscripts()
    first = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Find an XRD lab."),
        role="internal",
        transcript=chat,
    )
    # The tool's infrastructure fails on every attempt: the response is stored, its
    # result never is.
    with pytest.raises(RuntimeError):
        await run_turn(
            first, Executor(role="internal", fail_once=["search_network"]), model, transcript=store
        )
    assert roles(store.messages(chat)) == ["user", "assistant"]

    second = turn_context(
        world_id=WORLD,
        turn=2,
        turn_id=turn_id(2),
        opening=opening("Any luck?"),
        role="internal",
        transcript=chat,
    )
    outcome = await run_turn(second, Executor(role="internal"), model, transcript=store)

    assert outcome.stop == "end_turn"
    assert store.messages(chat)[2].content() == [
        tool_result_block("toolu_s0", INTERRUPTED, is_error=True),
        text_block("Any luck?"),
    ]
    assert check_requests(model.requests) == []
    assert unpaired(model.requests) == []


# --- Retries -------------------------------------------------------------------------


async def test_a_retried_turn_reuses_its_calls_and_never_duplicates() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [use("lookup", "toolu_1")],
                    (1, 1): [use("consult", "toolu_2", question="Is Nov 4 open?")],
                }
            )
        )
    )
    store = MemoryTranscripts()
    ctx = turn_context(
        world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening(), notices=NOTICE
    )
    flaky = Executor(fail_once=["lookup"])

    with pytest.raises(RuntimeError):
        await run_turn(ctx, flaky, model, transcript=store)
    # The first call and its response were stored before the tool failed.
    assert roles(store.messages(ctx.transcript)) == ["user", "system", "assistant"]

    retried = ctx.model_copy(update={"attempt": 2})
    outcome = await run_turn(retried, flaky, model, transcript=store)
    again = await run_turn(retried, flaky, model, transcript=store)

    assert outcome.stop == again.stop == "performative"
    # One real call per step; a rerun processes the stored response again instead of
    # calling, and the executor (idempotent) sees the call again.
    assert len(model.requests) == 2
    assert flaky.executed.count(("consult", 1)) == 2
    assert roles(store.messages(ctx.transcript)) == [
        "user",
        "system",
        "assistant",
        "user",
        "assistant",
    ]


async def test_a_call_stored_before_the_append_is_replayed_into_the_transcript() -> None:
    entries = cassette({(1, 0): [use("consult", "toolu_1", question="Is Nov 4 open?")]})
    model = Checkpointed(ScriptedModel(entries))
    ctx = turn_context(
        world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening(), notices=NOTICE
    )
    # An attempt whose model call was checkpointed but whose append never happened.
    await run_turn(ctx, Executor(), model, transcript=MemoryTranscripts())
    store = MemoryTranscripts()

    outcome = await run_turn(ctx, Executor(), model, transcript=store)

    assert outcome.stop == "performative"
    assert len(model.requests) == 1
    stored = store.messages(ctx.transcript)
    assert canonical_json([m.as_json() for m in stored[:2]]) == canonical_json(sent_messages(model))


async def test_a_turn_whose_messages_are_not_the_tail_is_refused() -> None:
    store = MemoryTranscripts()
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [use("consult", "toolu_1", question="Is Nov 4 open?")],
                    (2, 0): [use("send_message", "toolu_2", text="Nov 4 works.")],
                    (2, 1): [say("Sent.")],
                }
            )
        )
    )
    first = turn_context(world_id=WORLD, turn=1, turn_id=turn_id(1), opening=opening())
    await run_turn(first, Executor(), model, transcript=store)
    second = turn_context(
        world_id=WORLD,
        turn=2,
        turn_id=turn_id(2),
        opening=[tool_result_block("toolu_1", "Yes, Nov 4 is open.")],
    )
    await run_turn(second, Executor(), model, transcript=store)

    with pytest.raises(TranscriptConflict):
        await run_turn(
            first.model_copy(update={"first_step": 1}), Executor(), model, transcript=store
        )


async def test_text_from_later_steps_streams_as_a_new_paragraph() -> None:
    model = Checkpointed(
        ScriptedModel(
            cassette(
                {
                    (1, 0): [say("Searching."), use("search_network", "toolu_1")],
                    (1, 1): [say("Found two labs.")],
                },
                role="internal",
            ),
            chunk_chars=4,
        )
    )
    conversation = ConversationTranscript(world_id=WORLD, conversation_id=uuid5(NAMESPACE, "c"))
    ctx = turn_context(
        world_id=WORLD,
        turn=1,
        turn_id=turn_id(1),
        opening=opening("Find an XRD lab."),
        role="internal",
        transcript=conversation,
    )
    shown: list[str] = []

    await run_turn(
        ctx, Executor(role="internal"), model, transcript=MemoryTranscripts(), on_text=shown.append
    )

    assert "".join(shown) == "Searching.\n\nFound two labs."


# --- Wire helpers --------------------------------------------------------------------


def test_opening_blocks_put_owed_results_first_and_refuse_pending_ones() -> None:
    owed = OwedResult(tool_use_id="toolu_1", content="Sent.")
    assert opening_blocks([owed], ["Hello"]) == [
        tool_result_block("toolu_1", "Sent."),
        text_block("Hello"),
    ]
    with pytest.raises(ValueError, match="pending"):
        opening_blocks([OwedResult(tool_use_id="toolu_2", content=None)], [])


def test_a_result_owed_to_no_call_opens_as_text_after_the_tool_results() -> None:
    """A held bare-text reply had no tool call, so its result can't be a tool_result."""
    held = OwedResult(tool_use_id="", content="Not sent: your side didn't approve it.")
    owed = OwedResult(tool_use_id="toolu_1", content="Sent.")
    assert opening_blocks([held, owed], ["Hello"]) == [
        tool_result_block("toolu_1", "Sent."),
        text_block("Not sent: your side didn't approve it."),
        text_block("Hello"),
    ]


def test_echo_drops_blocks_before_the_last_fallback() -> None:
    message: dict[str, JsonValue] = {
        "content": [
            {"type": "text", "text": "partial"},
            {"type": "fallback", "model": "claude-opus-5-5"},
            {"type": "tool_use", "id": "toolu_1", "name": "send_message", "input": {}},
        ]
    }
    assert [block["type"] for block in echo_content(message)] == ["fallback", "tool_use"]
    assert [call.name for call in tool_calls(message)] == ["send_message"]


# --- Property: any sequence of turns keeps the request contract ------------------------

# How a representative's turn ends: a performative, a bare reply, or a message followed
# by text of its own.
Ending = Literal["consult", "close", "reply", "send"]
# How a turn's ending step fails, if it does.
Failure = Literal["none", "incomplete", "model_error", "tool_error"]
TURN_PLAN = st.tuples(
    st.booleans(),  # notices pending
    st.integers(min_value=0, max_value=2),  # lookup steps before the end
    st.sampled_from(["consult", "close", "reply", "send"]),
    st.booleans(),  # text alongside the ending call
    st.sampled_from(["none", "none", "incomplete", "model_error", "tool_error"]),
)
type Plan = tuple[bool, int, Ending, bool, Failure]
_ENDING_CALLS: dict[Ending, tuple[str, str]] = {
    "consult": ("consult", "question"),
    "close": ("close", "summary"),
    "send": ("send_message", "text"),
}


def feasible(plan: Plan) -> Plan:
    notices, tools, ending, chatty, failure = plan
    if failure == "tool_error" and ending == "reply":
        failure = "model_error"
    # An incomplete first call drops its notices, as a refusal does, which `check_prefix`
    # (consecutive requests) would flag although nothing replayed depends on them.
    if failure == "incomplete" and tools == 0:
        notices = False
    return notices, tools, ending, chatty, failure


@settings(max_examples=80, deadline=None)
@given(plan=st.lists(TURN_PLAN, min_size=1, max_size=5))
async def test_any_turn_sequence_keeps_the_request_contract(plan: list[Plan]) -> None:
    """Across turns with notices, tool steps, consults, closes, replies and several
    messages, and turns that fail at their ending step (an incomplete response, a failed
    model call, a failed tool): every request is a byte-prefix extension of the previous
    one, system messages sit only after a user message and before an assistant one,
    every tool call is answered exactly once, and the stored transcript is what was sent
    plus the last response."""
    turns = [feasible(p) for p in plan]
    turns.append((False, 0, "send", False, "none"))  # a final turn that succeeds
    steps: dict[tuple[int, int], list[ScriptedBlock]] = {}
    incomplete: dict[tuple[int, int], tuple[str, JsonObject | None]] = {}
    failing: list[tuple[UUID, int]] = []
    for turn, (_, tools, ending, chatty, failure) in enumerate(turns, start=1):
        for step in range(tools):
            steps[(turn, step)] = [use("lookup", f"toolu_{turn}_{step}")]
        last: list[ScriptedBlock] = [say(f"Thinking aloud {turn}.")] if chatty else []
        if ending == "reply":
            last = [say(f"Reply {turn}.")]
        else:
            name, field = _ENDING_CALLS[ending]
            last.append(use(name, f"toolu_{turn}_end", **{field: f"Message {turn}"}))
        steps[(turn, tools)] = last
        if ending == "send":
            steps[(turn, tools + 1)] = [say(f"Waiting for them {turn}.")]
        if failure == "incomplete":
            incomplete[(turn, tools)] = ("max_tokens", None)
        if failure == "model_error":
            failing.append((turn_id(turn, "p"), tools))
    model = Checkpointed(ScriptedModel(cassette(steps, stop=incomplete)), fail_once=failing)
    store = MemoryTranscripts()
    owed: OwedResult | None = None
    ref = None
    for turn, (notices, _, ending, _, failure) in enumerate(turns, start=1):
        if owed is not None and owed.content is None:
            owed = owed.model_copy(update={"content": f"Answer {turn}"})
        ctx = turn_context(
            world_id=WORLD,
            turn=turn,
            turn_id=turn_id(turn, "p"),
            opening=opening_blocks([owed] if owed else [], [f"Counterparty message {turn}"]),
            notices=f"Notice {turn}." if notices else None,
        )
        ref = ctx.transcript
        # `feasible` never fails a tool on a bare reply, which calls none.
        broken = [_ENDING_CALLS[ending][0]] if failure == "tool_error" and ending != "reply" else []
        executor = Executor(fail_once=broken)
        if failure != "none":
            # The workflow consumed the owed result with this turn's opening.
            owed = None
            expected = TurnIncomplete if failure == "incomplete" else RuntimeError
            with pytest.raises(expected):
                await run_turn(ctx, executor, model, transcript=store)
            continue
        outcome = await run_turn(ctx, executor, model, transcript=store)
        if ending in ("reply", "send"):
            assert (outcome.stop, outcome.owed) == ("end_turn", None)
            assert [effect.kind for effect in outcome.effects] == ["message_sent"]
        else:
            assert (outcome.stop, outcome.performative) == ("performative", ending)
        owed = outcome.owed

    assert check_requests(model.requests) == []
    assert unpaired(model.requests) == []
    assert ref is not None
    stored = [message.as_json() for message in store.messages(ref)]
    assert canonical_json(list[JsonValue](stored[:-1])) == canonical_json(sent_messages(model))
    assert stored[-1]["role"] == "assistant"
