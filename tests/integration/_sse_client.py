"""Test helpers: read an ASGI app's SSE streams in-process, frame by frame.

httpx's `ASGITransport` collects the whole response body before returning, which never
happens for an event stream. `StreamingASGITransport` runs the app in a task and hands
the body over chunk by chunk; closing the response sends the app `http.disconnect`, as
a browser closing its EventSource would. It does not run the app's lifespan, so start
the event bus yourself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.types import ASGIApp, Message

from harmony.events.schemas import EventEnvelope
from harmony.events.sse import READY_COMMENT, SseFrame, decode_sse


class _BodyStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: asyncio.Queue[bytes | None],
        disconnected: asyncio.Event,
        app: asyncio.Task[None],
    ) -> None:
        self._chunks = chunks
        self._disconnected = disconnected
        self._app = app

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (chunk := await self._chunks.get()) is not None:
            yield chunk

    async def aclose(self) -> None:
        self._disconnected.set()
        try:
            async with asyncio.timeout(5):
                await asyncio.shield(self._app)
        except TimeoutError:
            self._app.cancel()
            raise AssertionError("the app did not stop after the client disconnected") from None


class StreamingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }
        loop = asyncio.get_running_loop()
        started: asyncio.Future[tuple[int, list[tuple[bytes, bytes]]]] = loop.create_future()
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue()  # None ends the body
        disconnected = asyncio.Event()
        request_sent = False

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                started.set_result((message["status"], list(message.get("headers", []))))
            elif message["type"] == "http.response.body":
                if chunk := message.get("body", b""):
                    chunks.put_nowait(bytes(chunk))
                if not message.get("more_body", False):
                    chunks.put_nowait(None)

        async def run() -> None:
            try:
                await self._app(scope, receive, send)
            except Exception as exc:
                if started.done():
                    raise
                started.set_exception(exc)  # raised by handle_async_request instead
            finally:
                chunks.put_nowait(None)

        app = asyncio.create_task(run())
        status, headers = await started
        return httpx.Response(
            status,
            headers=headers,
            stream=_BodyStream(chunks, disconnected, app),
            request=request,
        )


class SseReader:
    """Frames of one open event stream, read with timeouts.

    A background task pumps frames into a queue, so a timed-out wait leaves the stream
    readable (cancelling a read inside the decoder would end it).
    """

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.last_event_id: str | None = None
        self._frames: asyncio.Queue[SseFrame | None] = asyncio.Queue()
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for frame in decode_sse(self.response.aiter_lines()):
                self._frames.put_nowait(frame)
        finally:
            self._frames.put_nowait(None)

    async def next_frame(self, wait_s: float = 5.0) -> SseFrame | None:
        """The next frame, or None once the server has ended the stream."""
        async with asyncio.timeout(wait_s):
            frame = await self._frames.get()
        if frame is None:
            self._frames.put_nowait(None)  # stay ended for later calls
        elif frame.id is not None:
            self.last_event_id = frame.id
        return frame

    async def next_envelope(self, wait_s: float = 5.0) -> EventEnvelope:
        """The next frame with data, skipping heartbeats and id-only frames."""
        async with asyncio.timeout(wait_s):
            while True:
                frame = await self.next_frame(wait_s)
                if frame is None:
                    raise AssertionError("the stream ended")
                if frame.data is not None:
                    return frame.envelope()

    async def envelopes_until(
        self, done: Callable[[EventEnvelope], bool], wait_s: float = 5.0
    ) -> list[EventEnvelope]:
        """Every envelope up to and including the first for which `done` is true."""
        received: list[EventEnvelope] = []
        async with asyncio.timeout(wait_s):
            while True:
                envelope = await self.next_envelope(wait_s)
                received.append(envelope)
                if done(envelope):
                    return received

    async def until_end(self, wait_s: float = 5.0) -> list[SseFrame]:
        """Every frame until the server ends the stream."""
        frames: list[SseFrame] = []
        async with asyncio.timeout(wait_s):
            while (frame := await self.next_frame(wait_s)) is not None:
                frames.append(frame)
        return frames

    async def aclose(self) -> None:
        self._pump.cancel()
        await self.response.aclose()


@asynccontextmanager
async def open_stream(
    client: httpx.AsyncClient,
    *,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
) -> AsyncGenerator[SseReader]:
    """GET /api/events and keep it open for the `async with` block."""
    async with client.stream("GET", "/api/events", params=params, headers=headers) as response:
        if response.status_code != 200:
            raise AssertionError(f"{response.status_code}: {(await response.aread())!r}")
        reader = SseReader(response)
        try:
            # The stream is subscribed to the bus once it says so; from then on nothing
            # published is missed.
            assert await reader.next_frame() == SseFrame(comment=READY_COMMENT)
            yield reader
        finally:
            await reader.aclose()
