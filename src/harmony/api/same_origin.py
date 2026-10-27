"""Refuse state-changing `/api` requests that a third-party page made (login CSRF).

The SPA and the API share one origin (ARCHITECTURE.md §3), so every write the SPA
makes is same-origin. Another site can still auto-submit a plain HTML form to, say,
`POST /api/session/start`: `SameSite=Lax` keeps the visitor's cookie off that POST,
but the browser stores the response's `Set-Cookie` (a top-level navigation), which
silently swaps the visitor onto a brand-new session and world.

Browsers label requests, and a page cannot forge the labels:
- `Sec-Fetch-Site` on every request (every current browser): writes are accepted only
  when it is `same-origin` (or `none`, a user-initiated navigation);
- otherwise `Origin`, which browsers send on cross-origin writes: it must name this
  host (`null`, an opaque origin, never does).

A request with neither label did not come from a modern browser page (curl, tests,
scripts), so it carries no ambient cookie of a visitor who did not mean to send it.
Safe methods (GET, HEAD, OPTIONS) change nothing and are never checked.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_FETCH_SITES: Final = frozenset({"same-origin", "none"})
REFUSAL: Final = "cross-site requests may not change state; use the Harmony app itself"


def is_same_origin(headers: Headers) -> bool:
    """Whether a state-changing request's browser labels say it came from this origin."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() in ALLOWED_FETCH_SITES
    origin = headers.get("origin")
    if origin is None:
        return True
    host = headers.get("host")
    return host is not None and urlsplit(origin.strip()).netloc.lower() == host.strip().lower()


def cross_site_refusal() -> JSONResponse:
    """The 403 for a write whose browser labels name another site."""
    return JSONResponse({"detail": REFUSAL}, status_code=403)


class SameOriginWrites:
    """ASGI middleware: a 403 for a cross-site write under `prefix`."""

    def __init__(self, app: ASGIApp, *, prefix: str = "/api") -> None:
        self._app = app
        self._prefix = prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refused = (
            scope["type"] == "http"
            and self._guarded(scope)
            and not is_same_origin(Headers(scope=scope))
        )
        if refused:
            await cross_site_refusal()(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _guarded(self, scope: Scope) -> bool:
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        under = path == self._prefix or path.startswith(f"{self._prefix}/")
        return under and method not in SAFE_METHODS


__all__ = ["SameOriginWrites", "cross_site_refusal", "is_same_origin"]
