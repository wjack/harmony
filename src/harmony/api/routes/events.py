"""Events routes, mounted under `/api` by `create_app`.

- GET /api/events: the acting member's live view of one world, as Server-Sent Events.
  Durable events carry `id: <seq>`, so the browser's automatic reconnect resumes with
  `Last-Event-ID`; the first connection passes the REST snapshot's cursor as `?after=`
  and the snapshot's session epoch as `?epoch=`.

The stream ends whenever the session's epoch moves (act-as, reset, a new build's fork).
The browser then reconnects to the same URL by itself; `?epoch=` lets the server refuse
that reconnect (409) instead of resuming a stream for a member or world the client's
cache no longer matches, and the client re-snapshots.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sse_starlette import EventSourceResponse

from harmony.api.deps import EventBusDep, SessionmakerDep
from harmony.auth.session import Actor, current_actor
from harmony.events.cursor import current_cursor
from harmony.events.sse import EventStream, SseOptions, Viewer

router = APIRouter(tags=["events"])


def get_sse_options() -> SseOptions:
    """Stream timings. A dependency, so tests shorten them through `dependency_overrides`."""
    return SseOptions()


_STREAM_RESPONSE: dict[str, Any] = {
    "description": (
        "A `text/event-stream`. Each message's `data` is one `EventEnvelope` as JSON. "
        "Durable events also carry `id: <seq>`; stream events (`stream.*`) carry no id. "
        "A message with only an `id` moves Last-Event-ID past events this viewer may not "
        "see. Comments are heartbeats. The server ends the stream when the session's "
        "acting member changes (after sending `session.member_changed`) and after "
        "`world.retired`."
    ),
    "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/EventEnvelope"}}},
}


@router.get(
    "/events",
    response_class=EventSourceResponse,
    summary="Stream world events (SSE)",
    responses={
        200: _STREAM_RESPONSE,
        400: {"description": "Last-Event-ID is not a seq."},
        404: {"description": "The world no longer exists."},
        409: {
            "description": "The cursor is ahead of this world, or `epoch` is not the "
            "session's current epoch: resubscribe from a fresh snapshot."
        },
        503: {"description": "The event bus is not running."},
    },
)
async def stream_events(
    actor: Annotated[Actor, Depends(current_actor)],
    bus: EventBusDep,
    sessionmaker: SessionmakerDep,
    options: Annotated[SseOptions, Depends(get_sse_options)],
    after: Annotated[
        int | None,
        Query(ge=0, description="Resume after this seq: the snapshot's X-Harmony-Cursor."),
    ] = None,
    epoch: Annotated[
        int | None,
        Query(
            ge=0,
            description="The session epoch of the snapshot the client opens from; a "
            "stream for any other epoch is refused with 409.",
        ),
    ] = None,
    lens: Annotated[
        Literal["network"] | None,
        Query(
            description="`network`: the network view's demo lens, which sees every event "
            "in the world (the view is a demo tool, opened from the demo bar)."
        ),
    ] = None,
    last_event_id: Annotated[
        str | None,
        Header(description="Sent by EventSource on reconnect; wins over `after`."),
    ] = None,
) -> EventSourceResponse:
    if epoch is not None and epoch != actor.epoch:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the session changed since this stream's snapshot; resubscribe from a fresh one",
        )
    requested = _resume_point(last_event_id, after)
    async with sessionmaker() as session:
        try:
            latest = await current_cursor(session, actor.world_id)
        except LookupError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "world not found") from None
    # Seqs never go backwards in a world, so a cursor ahead of it came from another world
    # (a reset since the snapshot): resuming from it would silently skip events.
    if requested is not None and requested > latest:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cursor is ahead of this world; resubscribe from a fresh snapshot cursor",
        )
    viewer = Viewer(
        session_id=actor.session_id,
        epoch=actor.epoch,
        member_id=actor.member_id,
        org_id=actor.org_id,
        role=actor.role,
        # The network view's demo lens sees every event, as its REST read does.
        lens=lens == "network",
    )
    stream = EventStream(
        sessionmaker=sessionmaker,
        bus=bus,
        world_id=actor.world_id,
        viewer=viewer,
        # Without a cursor the stream tails from now.
        after=latest if requested is None else requested,
        options=options,
    )
    return EventSourceResponse(
        stream.frames(), ping=options.heartbeat_s, send_timeout=options.send_timeout_s
    )


def _resume_point(last_event_id: str | None, after: int | None) -> int | None:
    if last_event_id is None or not last_event_id.strip():
        return after
    try:
        seq = int(last_event_id)
    except ValueError:
        seq = -1
    if seq < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Last-Event-ID must be a seq")
    return seq
