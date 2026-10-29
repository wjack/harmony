"""The master passcode gate (LIVE_DEMO_PLAN 3.2): `PasscodeGate`, `BodyLimit`, the
client key, the cookie, and the gate's own routes.

- GET /api/gate: the passcode page (self-contained HTML, never the SPA)
- POST /api/gate {passcode, next}: unlock; sets the `harmony_gate` cookie
- POST /api/gate/lock: clear the cookie ("Lock Harmony")

`PasscodeGate` is a pure-ASGI, default-deny middleware, registered outermost, so it
covers `/assets`, the SPA, the docs and any route added later; `BodyLimit` sits just
inside it and refuses oversized bodies. The gate is on whenever `ACCESS_CODE` is set.

While it is on, the gate's state (`Gate`: the passcode check, the cookie, and the
limits in `gate_limits.py`) lives on the middleware, which `create_app` builds once per
app, and reaches routes through the request scope (`GateDep`). With the gate off,
`GateDep` is None and no limit runs. The page itself is in `gate_page.py`.

Owner: L1.
"""

from __future__ import annotations

import hmac
import logging
import unicodedata
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Annotated, Final, cast
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from harmony.api.deps import SettingsDep
from harmony.api.gate_limits import FailurePacer, UnlockLimiter, WorldLimits
from harmony.api.gate_page import NO_STORE, page
from harmony.api.routes.problems import (
    RETRY_AFTER,
    ApiProblem,
    ProblemCode,
    problem,
    problem_entries,
)
from harmony.api.same_origin import cross_site_refusal, is_same_origin
from harmony.config import Settings

log = logging.getLogger(__name__)

# The gate's cookie, apart from the visitor session's: the gate comes first.
GATE_COOKIE: Final = "harmony_gate"
GATE_COOKIE_MAX_AGE_S: Final = 14 * 24 * 3600
# What `BodyLimit` lets through; the largest legitimate body (a 20,000-character
# message) is about 120 KB as JSON.
MAX_BODY_BYTES: Final = 256 * 1024
# An unlock body: a passcode and a path.
UNLOCK_MAX_BYTES: Final = 4 * 1024
MAX_PASSCODE_CHARS: Final = 256

UNLOCK_PATH: Final = "/api/gate"
# What passes without the cookie (besides admin routes with the admin secret).
_OPEN: Final = frozenset(
    {
        ("GET", "/api/healthz"),
        ("HEAD", "/api/healthz"),
        ("GET", UNLOCK_PATH),
        ("POST", UNLOCK_PATH),
    }
)
_ADMIN_PREFIX: Final = "/api/admin/"
_SCOPE_KEY: Final = "harmony.gate"
_SALT: Final = "harmony.gate"
_HSTS: Final = "max-age=31536000"

router = APIRouter(tags=["gate"])


# --- The client key -------------------------------------------------------------------

# A proxy's address in the header's position would make every visitor share one key.
_UNKNOWN_NETWORKS: Final = tuple(
    ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
        "::1/128",
        "fe80::/10",
    )
)
_IPV6_CLIENT_PREFIX: Final = 64
# The wider network a client key is in, which shares one allowance of unlock attempts:
# a routed IPv6 /48 holds 65,536 /64 keys, and tunnel brokers hand them out free.
_IPV4_NETWORK_PREFIX: Final = 24
_IPV6_NETWORK_PREFIX: Final = 48


def client_key(scope: Scope, settings: Settings) -> str | None:
    """The visitor's network from `CLIENT_IP_HEADER`: an IPv4 address, or the /64 an
    IPv6 address is in. None when it is missing, unparseable or private (an unknown
    client gets no per-client limit or spend key).

    `x-forwarded-for` gives the rightmost entry, the hop Render's proxy appends; a
    client can forge every entry to its left.
    """
    values = Headers(scope=scope).getlist(settings.client_ip_header)
    if not values:
        return None
    if settings.client_ip_header == "x-forwarded-for":
        raw = ",".join(values).rsplit(",", 1)[-1]
    else:
        raw = values[-1]
    try:
        address: IPv4Address | IPv6Address = ip_address(raw.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in _UNKNOWN_NETWORKS):
        return None
    if isinstance(address, IPv6Address):
        # One IPv6 host holds a whole /64 and can rotate addresses within it, so a
        # per-address key would hand it a fresh allowance per address.
        return str(ip_network(f"{address}/{_IPV6_CLIENT_PREFIX}", strict=False))
    return str(address)


def client_network(key: str) -> str:
    """The wider network a `client_key` is in: an IPv4 /24, or an IPv6 /48."""
    network = ip_network(key, strict=False)
    prefix = _IPV4_NETWORK_PREFIX if network.version == 4 else _IPV6_NETWORK_PREFIX
    return str(network.supernet(new_prefix=prefix))


def key_digest(key: str, settings: Settings) -> str:
    """HMAC-SHA256(SESSION_SECRET, key) in hex: a client key that is never stored."""
    secret = settings.session_secret_value().encode()
    return hmac.new(secret, key.encode(), sha256).hexdigest()


# --- `next` ---------------------------------------------------------------------------


def safe_next(value: str) -> str:
    """`value` if it is a path on this origin, else "/": exactly one leading slash and
    no backslash or control character, before or after percent-decoding."""
    for candidate in (value, unquote(value)):
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or any(unicodedata.category(char) == "Cc" for char in candidate)
        ):
            return "/"
    return value


def too_many_attempts(retry_after_s: int, detail: str) -> JSONResponse:
    """A 429 `too_many_attempts` with `Retry-After` (seconds)."""
    return _problem_response(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "too_many_attempts",
        detail,
        headers={RETRY_AFTER: str(retry_after_s)},
    )


def _problem_response(
    status_code: int, code: ProblemCode, detail: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        ApiProblem(detail=detail, code=code).model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=NO_STORE | (headers or {}),
    )


# --- The gate's state -----------------------------------------------------------------


def _derive(secret: bytes, purpose: str) -> bytes:
    return hmac.new(secret, purpose.encode(), sha256).digest()


def _mac(key: bytes, value: str) -> bytes:
    # A JSON string may hold a lone surrogate, which strict UTF-8 refuses.
    return hmac.new(key, value.encode("utf-8", "surrogatepass"), sha256).digest()


class Gate:
    """One app's gate while `ACCESS_CODE` is set: the passcode check, the cookie, and
    the limits, all in process (Render runs one web instance)."""

    def __init__(self, settings: Settings, access_code: str) -> None:
        secret = settings.session_secret_value().encode()
        self.settings = settings
        self.unlocks = UnlockLimiter()
        self.failures = FailurePacer()
        self.worlds = WorldLimits()
        # Digests, so neither code's length is observable in the comparison.
        self._compare_key = _derive(secret, "harmony.gate.passcode")
        self._expected = _mac(self._compare_key, access_code)
        admin = settings.admin_secret.get_secret_value() if settings.admin_secret else None
        self._admin_key = _derive(secret, "harmony.gate.admin")
        self._admin = _mac(self._admin_key, admin) if admin else None
        self._fingerprint = hmac.new(
            secret, f"harmony.gate:{access_code}".encode(), sha256
        ).hexdigest()[:32]
        self._serializer = URLSafeTimedSerializer(settings.session_secret_value(), salt=_SALT)

    def matches(self, attempt: str) -> bool:
        """Whether `attempt`, trimmed, is the passcode (case-sensitive)."""
        if len(attempt) > MAX_PASSCODE_CHARS:
            return False
        return hmac.compare_digest(_mac(self._compare_key, attempt.strip()), self._expected)

    def is_admin(self, secret: str | None) -> bool:
        if self._admin is None or secret is None:
            return False
        return hmac.compare_digest(_mac(self._admin_key, secret), self._admin)

    def mint_cookie(self) -> str:
        """A `harmony_gate` value: the passcode's fingerprint, signed and timestamped.
        A new passcode changes the fingerprint, so rotation locks every cookie out."""
        return self._serializer.dumps({"f": self._fingerprint})

    def admits(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        try:
            data: object = self._serializer.loads(cookie, max_age=GATE_COOKIE_MAX_AGE_S)
        except BadSignature:
            return False
        if not isinstance(data, dict):
            return False
        fingerprint = cast(dict[str, object], data).get("f")
        return isinstance(fingerprint, str) and hmac.compare_digest(fingerprint, self._fingerprint)

    def set_cookie(self, response: Response) -> None:
        response.set_cookie(
            GATE_COOKIE,
            self.mint_cookie(),
            max_age=GATE_COOKIE_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            secure=self.settings.app_env != "local",
            path="/",
        )


def gate_of(request: Request) -> Gate | None:
    """The app's `Gate` while it is on; None without `ACCESS_CODE`."""
    return cast(Gate | None, request.scope.get(_SCOPE_KEY))


GateDep = Annotated[Gate | None, Depends(gate_of)]


# --- Request bodies -------------------------------------------------------------------


class _BodyTooLarge(Exception):
    """Raised from `receive` once a body passes its cap."""


class _BodyCap:
    """Counts the bytes an app reads; past `max_bytes`, `receive` raises and whatever
    response the app makes of that becomes a 413 `payload_too_large`."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._read = 0
        self._exceeded = False
        self._started = False
        self._replaced = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def capped_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self._read += len(message.get("body", b""))
                if self._read > self._max_bytes:
                    self._exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            if self._replaced:
                return
            if message["type"] == "http.response.start" and not self._started:
                self._started = True
                if self._exceeded:
                    self._replaced = True
                    await _too_large()(scope, receive, send)
                    return
            await send(message)

        try:
            await self._app(scope, capped_receive, guarded_send)
        except _BodyTooLarge:
            if self._started:
                raise
            await _too_large()(scope, receive, send)


def _too_large() -> JSONResponse:
    return _problem_response(
        status.HTTP_413_CONTENT_TOO_LARGE, "payload_too_large", "the request body is too large"
    )


def _content_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is None or not value.isdigit():
        return None
    return int(value)


def _chunked(scope: Scope) -> bool:
    return "chunked" in Headers(scope=scope).get("transfer-encoding", "").lower()


class BodyLimit:
    """ASGI middleware: a 413 `payload_too_large` for a body over `max_bytes`, from
    `Content-Length` or by counting what `receive` returns (chunked bodies too)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        length = _content_length(scope)
        if length is not None and length > self._max_bytes:
            await _too_large()(scope, receive, send)
            return
        await _BodyCap(self._app, self._max_bytes)(scope, receive, send)


# --- The middleware -------------------------------------------------------------------


class PasscodeGate:
    """ASGI middleware: without a valid `harmony_gate` cookie, only the health check,
    the gate's own routes and admin routes with the admin secret pass."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        code = settings.access_code.get_secret_value() if settings.access_code else None
        self._gate = Gate(settings, code) if code else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "http":
            send = self._with_headers(send)
        gate = self._gate
        if gate is None:
            await self._app(scope, receive, send)
            return
        scope = {**scope, _SCOPE_KEY: gate}
        method, path = scope.get("method", "GET"), scope["path"]
        if (method, path) == ("POST", UNLOCK_PATH):
            await self._unlock(gate, scope, receive, send)
        elif self._admitted(gate, scope, method, path):
            await self._app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
        elif path == "/api" or path.startswith("/api/") or method not in ("GET", "HEAD"):
            response = _problem_response(
                status.HTTP_401_UNAUTHORIZED, "passcode_required", "passcode required"
            )
            await response(scope, receive, send)
        else:
            await page(status.HTTP_401_UNAUTHORIZED)(scope, receive, send)

    def _admitted(self, gate: Gate, scope: Scope, method: str, path: str) -> bool:
        if scope["type"] == "http" and (method, path) in _OPEN:
            return True
        connection = HTTPConnection(scope)
        if gate.admits(connection.cookies.get(GATE_COOKIE)):
            return True
        # The kill switch stays reachable while unlocks are throttled.
        return path.startswith(_ADMIN_PREFIX) and gate.is_admin(
            connection.headers.get("x-admin-secret")
        )

    def _with_headers(self, send: Send) -> Send:
        hsts = self._settings.app_env == "prod"
        gated = self._gate is not None
        if not (hsts or gated):
            return send

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if hsts:
                    headers.setdefault("strict-transport-security", _HSTS)
                if gated:
                    headers.setdefault("x-robots-tag", "noindex")
            await send(message)

        return send_with_headers

    async def _unlock(self, gate: Gate, scope: Scope, receive: Receive, send: Send) -> None:
        """POST /api/gate, in the order of 3.2: the per-client throttle before any of
        the body is read, then `Content-Length`, then the body capped at 4 KB; the
        route compares the passcode. A wrong one is settled, then paced
        (`FailurePacer`) before its 401 is sent: the attempt was counted at admit, so
        the key's next attempt (the visitor's correction) need not wait for this one.

        A cross-site attempt is refused before the throttle: it is never compared, so
        counting it would only let a third-party page lock out its visitor's network.
        """
        key = client_key(scope, self._settings)
        if not is_same_origin(Headers(scope=scope)):
            self._log_attempt("cross_site", key)
            response = cross_site_refusal()
            response.headers.update(NO_STORE)
            await response(scope, receive, send)
            return
        # An unknown key gets no per-client or per-network limit, only the global pacing.
        limited = (key, client_network(key)) if key is not None else None
        wait_s = gate.unlocks.admit(*limited) if limited is not None else None
        if wait_s is not None:
            self._log_attempt("throttled", key)
            await too_many_attempts(wait_s, "too many attempts")(scope, receive, send)
            return
        statuses: list[int] = []
        settled = False

        def settle(outcome: str) -> None:
            nonlocal settled
            if limited is not None and not settled:
                gate.unlocks.settle(*limited, success=outcome == "ok")
            settled = True

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])
                MutableHeaders(scope=message)["cache-control"] = "no-store"
                if message["status"] == status.HTTP_401_UNAUTHORIZED:
                    settle("wrong")
                    await gate.failures.wait()
            await send(message)

        try:
            length = _content_length(scope)
            if length is None and not _chunked(scope):
                response = _problem_response(
                    status.HTTP_411_LENGTH_REQUIRED, "invalid", "Content-Length is required"
                )
                await response(scope, receive, observed_send)
            elif length is not None and length > UNLOCK_MAX_BYTES:
                await _too_large()(scope, receive, observed_send)
            else:
                await _BodyCap(self._app, UNLOCK_MAX_BYTES)(scope, receive, observed_send)
        finally:
            outcome = _OUTCOMES.get(statuses[0], "error") if statuses else "error"
            settle(outcome)
            self._log_attempt(outcome, key)

    def _log_attempt(self, outcome: str, key: str | None) -> None:
        digest = key_digest(key, self._settings)[:12] if key is not None else "unknown"
        log.info(
            "gate_attempt outcome=%s source=%s key=%s",
            outcome,
            self._settings.client_ip_header,
            digest,
        )


_OUTCOMES: Final = {
    status.HTTP_200_OK: "ok",
    status.HTTP_401_UNAUTHORIZED: "wrong",
    status.HTTP_411_LENGTH_REQUIRED: "no_length",
    status.HTTP_413_CONTENT_TOO_LARGE: "too_large",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "malformed",
}


# --- Routes ---------------------------------------------------------------------------


class GateUnlock(BaseModel):
    """The unlock attempt. `next` is where to go after; the server re-checks it."""

    model_config = ConfigDict(extra="forbid")

    passcode: str
    next: str = "/"


class GateUnlocked(BaseModel):
    model_config = ConfigDict(frozen=True)

    # The validated same-origin path the page navigates to.
    next: str


@router.get("/gate", response_class=HTMLResponse)
async def gate_page() -> HTMLResponse:
    """The passcode page, a constant: nothing from the request is written into it."""
    return page(status.HTTP_200_OK)


@router.post(
    "/gate",
    response_model=GateUnlocked,
    responses=problem_entries(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_411_LENGTH_REQUIRED,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def unlock(body: GateUnlock, response: Response, gate: GateDep) -> GateUnlocked:
    """Check the passcode; on success set the `harmony_gate` cookie. 401
    `passcode_invalid` for a wrong one, 429 `too_many_attempts` when throttled."""
    target = safe_next(body.next)
    if gate is None:
        # Nothing is locked, so there is nothing to unlock.
        return GateUnlocked(next=target)
    if not gate.matches(body.passcode):
        # Paced by the gate middleware once the key is settled (`_unlock`).
        raise problem(
            status.HTTP_401_UNAUTHORIZED, "passcode_invalid", "That passcode isn't right."
        )
    gate.set_cookie(response)
    return GateUnlocked(next=target)


@router.post(
    "/gate/lock",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_entries(status.HTTP_401_UNAUTHORIZED),
)
async def lock(response: Response, settings: SettingsDep) -> None:
    """Clear the `harmony_gate` cookie."""
    response.headers.update(NO_STORE)
    response.delete_cookie(
        GATE_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "local",
    )


__all__ = [
    "GATE_COOKIE",
    "GATE_COOKIE_MAX_AGE_S",
    "MAX_BODY_BYTES",
    "BodyLimit",
    "Gate",
    "GateDep",
    "GateUnlock",
    "GateUnlocked",
    "PasscodeGate",
    "client_key",
    "client_network",
    "gate_of",
    "key_digest",
    "router",
    "safe_next",
    "too_many_attempts",
]
