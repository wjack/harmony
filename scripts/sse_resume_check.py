"""Check that SSE resumes after a disconnect with no gaps (a P0 exit criterion).

Against a running API (`make dev`, or the compose stack) and the database it uses:

1. starts a visitor session in the network world (`POST /api/session/start?world=network`,
   keeping the cookie);
2. opens `GET /api/events?after=0&world=network&lens=network` and learns the world from
   its first event (the demo lens, honoured only in the network world, sees every
   event, so the seqs it receives must be contiguous);
3. writes events into that world through the UnitOfWork and reads them live;
4. drops the connection, writes more, and reconnects as EventSource does: the same
   URL plus `Last-Event-ID`;
5. writes more while connected, then checks every seq from 1 arrived exactly once,
   in order.

Prints PASS or FAIL per check and exits 1 on any failure. The events are `hello.tick`s
written with DATABASE_URL from the environment or `.env.local`. No model calls.

    uv run python scripts/sse_resume_check.py [--base-url http://localhost:8000]

The base URL defaults to HARMONY_BASE_URL, as for p0_actas_check.sh.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.auth.session import SESSION_COOKIE
from harmony.config import get_settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.db.uow import unit_of_work
from harmony.events.schemas import EventEnvelope, EventScope, HelloTick
from harmony.events.sse import SseFrame, decode_sse

EVENTS_PATH = "/api/events"
WORLD_PARAMS = {"world": "network"}
STREAM_PARAMS = {"after": "0", **WORLD_PARAMS, "lens": "network"}
DEFAULT_BASE_URL = "http://localhost:8000"


@dataclass
class Stream:
    """What the client has seen so far, across connections."""

    seqs: list[int] = field(default_factory=list[int])
    last_event_id: str | None = None
    world_id: UUID | None = None

    async def read_until(
        self, frames: AsyncIterator[SseFrame], done: Callable[[EventEnvelope], bool], wait_s: float
    ) -> None:
        async with asyncio.timeout(wait_s):
            async for frame in frames:
                if frame.id is not None:
                    self.last_event_id = frame.id
                if frame.data is None:
                    continue
                envelope = frame.envelope()
                self.world_id = self.world_id or envelope.world_id
                if envelope.seq is not None:
                    self.seqs.append(envelope.seq)
                if done(envelope):
                    return
        raise RuntimeError("the server ended the stream")


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" ({detail})" if detail and not ok else ""))
        self.failures += not ok
        return ok


def last_tick(label: str, count: int) -> Callable[[EventEnvelope], bool]:
    def done(envelope: EventEnvelope) -> bool:
        event = envelope.event
        return isinstance(event, HelloTick) and event.workflow_id == label and event.n == count

    return done


async def write_ticks(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID, label: str, count: int
) -> list[int]:
    async with unit_of_work(sessionmaker, world_id) as uow:
        for n in range(1, count + 1):
            uow.add_event(HelloTick(workflow_id=label, n=n, of=count), EventScope.world())
        return list(await uow.commit())


async def run(base_url: str, count: int, wait_s: float) -> int:
    report = Report()
    engine = engine_from_settings(get_settings(), application_name="harmony-sse-check")
    sessionmaker = create_sessionmaker(engine)
    run_id = f"sse-resume-check-{uuid4().hex[:8]}"
    stream = Stream()
    try:
        timeout = httpx.Timeout(wait_s, read=None)  # reads are bounded by read_until
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            started = await client.post("/api/session/start", params=WORLD_PARAMS)
            token = started.cookies.get(SESSION_COOKIE)
            if not report.check(
                "start a session",
                started.is_success and token is not None,
                f"{started.status_code}",
            ):
                return 1
            # Outside APP_ENV=local the cookie is Secure, and httpx's jar keeps a Secure
            # cookie off plain http (the compose stack); send it the way curl and
            # browsers on localhost do.
            client.cookies.clear()
            client.headers["Cookie"] = f"{SESSION_COOKIE}={token}"

            async with client.stream("GET", EVENTS_PATH, params=STREAM_PARAMS) as response:
                if not report.check("open the stream", response.status_code == 200):
                    return 1
                frames = decode_sse(response.aiter_lines())
                await stream.read_until(frames, lambda _: True, wait_s)
                world_id = stream.world_id
                assert world_id is not None
                live = await write_ticks(sessionmaker, world_id, f"{run_id}-live", count)
                await stream.read_until(frames, last_tick(f"{run_id}-live", count), wait_s)
                report.check("events arrive while connected", set(live) <= set(stream.seqs))
            # The connection is gone; these commits happen while nobody listens for us.
            missed = await write_ticks(sessionmaker, world_id, f"{run_id}-missed", count)

            headers = {"Last-Event-ID": stream.last_event_id or "0"}
            async with client.stream(
                "GET", EVENTS_PATH, params=STREAM_PARAMS, headers=headers
            ) as response:
                if not report.check("reconnect with Last-Event-ID", response.status_code == 200):
                    return 1
                frames = decode_sse(response.aiter_lines())
                await stream.read_until(frames, last_tick(f"{run_id}-missed", count), wait_s)
                after = await write_ticks(sessionmaker, world_id, f"{run_id}-after", count)
                await stream.read_until(frames, last_tick(f"{run_id}-after", count), wait_s)

        report.check(
            "events committed while disconnected arrive after reconnecting",
            set(missed) <= set(stream.seqs),
        )
        report.check("events keep arriving after reconnecting", set(after) <= set(stream.seqs))
        expected = list(range(1, after[-1] + 1))
        report.check(
            "every seq from 1 arrived exactly once, in order",
            stream.seqs == expected,
            f"received {len(stream.seqs)} seqs, expected 1..{after[-1]}",
        )
    except (TimeoutError, RuntimeError, httpx.HTTPError) as exc:
        report.check("the stream kept delivering", False, repr(exc))
    finally:
        await engine.dispose()
    return 1 if report.failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check that SSE resumes after a disconnect with no gaps."
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("HARMONY_BASE_URL") or DEFAULT_BASE_URL
    )
    parser.add_argument("--events", type=int, default=5, help="events per phase")
    parser.add_argument("--wait", type=float, default=10.0, help="seconds per read phase")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base_url, args.events, args.wait)))


if __name__ == "__main__":
    main()
