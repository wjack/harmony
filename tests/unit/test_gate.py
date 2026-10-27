"""The passcode gate without a database (LIVE_DEMO_PLAN 3.2): the client key, `next`,
the page, the cookie, unlock and its limits, request bodies and transport headers.

Every gated test builds a fresh app, so no limiter state crosses tests. The default-deny
route walk and the world-creation limits need a database: tests/integration/test_gate.py.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from base64 import b64encode
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from itsdangerous import TimestampSigner, URLSafeTimedSerializer
from pydantic import SecretStr
from starlette.types import Message, Receive, Scope, Send

from harmony.api import gate_limits
from harmony.api.app import create_app
from harmony.api.deps import AppServices
from harmony.api.gate import (
    GATE_COOKIE,
    BodyLimit,
    PasscodeGate,
    client_key,
    client_network,
    key_digest,
    safe_next,
)
from harmony.api.gate_limits import MAX_TRACKED, FailurePacer, UnlockLimiter, WorldLimits
from harmony.api.gate_page import PAGE
from harmony.api.same_origin import REFUSAL
from harmony.config import Settings
from harmony.seeds.loader import load_scenario
from harmony.worlds.service import TooManyWorlds
from tests.conftest import prod_settings, settings_without_dotenv

PASSCODE = "correct horse battery"
SECRET = "s" * 40
ADMIN = "gate-admin-secret"
CLIENT = "198.51.100.7"
PASSCODE_REQUIRED = {"detail": "passcode required", "code": "passcode_required"}
FOURTEEN_DAYS_S = 14 * 24 * 3600


def _settings(dist: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "ci",
        "session_secret": SecretStr(SECRET),
        "access_code": SecretStr(PASSCODE),
        "admin_secret": SecretStr(ADMIN),
        "client_ip_header": "x-forwarded-for",
        "web_dist_dir": dist,
    }
    return settings_without_dotenv(**(values | overrides))


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>")
    (dist / "assets" / "app-1a2b3c.js").write_text("console.log(1)")
    return dist


@pytest.fixture
def fast_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pacing between wrong passcodes, for tests about something else."""
    monkeypatch.setattr(gate_limits, "FAILURE_SLOT_S", 0.0)


@pytest.fixture
async def client(dist: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(create_app(_settings(dist))) as c:
        yield c


def _client(app: FastAPI) -> httpx.AsyncClient:
    # https, so a Secure cookie is sent back.
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="https://harmony.test")


async def _unlock(
    c: httpx.AsyncClient, passcode: str = PASSCODE, *, ip: str | None = None, **body: Any
) -> httpx.Response:
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await c.post("/api/gate", json={"passcode": passcode, **body}, headers=headers)


def _http_scope(headers: Sequence[tuple[str, str]]) -> Scope:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(name.encode(), value.encode()) for name, value in headers],
    }


# --- The client key -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([("x-forwarded-for", CLIENT)], CLIENT),
        # The rightmost entry is the hop Render's proxy appends; the rest is forgeable.
        ([("x-forwarded-for", f"6.6.6.6, {CLIENT}")], CLIENT),
        ([("x-forwarded-for", "6.6.6.6"), ("x-forwarded-for", CLIENT)], CLIENT),
        # An IPv6 client holds a whole /64, so the /64 is its key.
        ([("x-forwarded-for", "2001:db8:1:2::1")], "2001:db8:1:2::/64"),
        ([("x-forwarded-for", "2001:db8:1:2:aaaa:bbbb:cccc:dddd")], "2001:db8:1:2::/64"),
        ([("x-forwarded-for", "2001:db8:1:3::1")], "2001:db8:1:3::/64"),
        ([("x-forwarded-for", f"::ffff:{CLIENT}")], CLIENT),
        ([], None),
        ([("x-forwarded-for", "")], None),
        ([("x-forwarded-for", "garbage")], None),
        ([("x-forwarded-for", f"{CLIENT}:443")], None),
        ([("x-forwarded-for", f"{CLIENT}, 10.0.0.1")], None),
        ([("true-client-ip", CLIENT)], None),
    ],
)
def test_the_client_key_is_the_rightmost_forwarded_address(
    headers: list[tuple[str, str]], expected: str | None, tmp_path: Path
) -> None:
    assert client_key(_http_scope(headers), _settings(tmp_path)) == expected


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "fc00::1",
        "fd12::1",
        "::1",
        "fe80::1",
        "::ffff:10.0.0.1",
    ],
)
def test_private_loopback_link_local_and_cgnat_addresses_are_unknown(
    address: str, tmp_path: Path
) -> None:
    assert client_key(_http_scope([("x-forwarded-for", address)]), _settings(tmp_path)) is None


@pytest.mark.parametrize(
    ("key", "network"),
    [
        (CLIENT, "198.51.100.0/24"),
        ("2001:db8:1:2::/64", "2001:db8:1::/48"),
        ("2001:db8:1:ffff::/64", "2001:db8:1::/48"),
    ],
)
def test_a_client_keys_network_is_its_ipv4_24_or_ipv6_48(key: str, network: str) -> None:
    assert client_network(key) == network


@pytest.mark.parametrize("source", ["true-client-ip", "cf-connecting-ip"])
def test_the_client_key_source_is_a_setting(source: str, tmp_path: Path) -> None:
    settings = _settings(tmp_path, client_ip_header=source)
    scope = _http_scope([("x-forwarded-for", "203.0.113.1"), (source, CLIENT)])
    assert client_key(scope, settings) == CLIENT
    assert client_key(_http_scope([("x-forwarded-for", CLIENT)]), settings) is None


# --- `next` ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "//evil.com",
        "/\\evil.com",
        "https://evil.com",
        "/%09/evil.com",
        "/%0a/evil.com",
        "/%5C%5Cevil.com",
        "/%2F/evil.com",
        "/\t/evil.com",
        "evil.com",
        "",
    ],
)
def test_next_must_be_a_path_on_this_origin(value: str) -> None:
    assert safe_next(value) == "/"


@pytest.mark.parametrize("value", ["/", "/requests/abc?tab=details#top", "/chat/%E2%9C%93"])
def test_a_same_origin_next_is_kept(value: str) -> None:
    assert safe_next(value) == value


async def test_unlock_answers_with_the_checked_next(client: httpx.AsyncClient) -> None:
    response = await _unlock(client, next="//evil.com")
    assert response.json() == {"next": "/"}
    response = await _unlock(client, next="/requests/abc?tab=details")
    assert response.json() == {"next": "/requests/abc?tab=details"}


# --- The page -------------------------------------------------------------------------


def _cast_and_orgs() -> set[str]:
    names: set[str] = set()
    for org in load_scenario("harmony-demo").orgs:
        names |= {org.name, org.slug}
        for member in org.members:
            # Honorifics ("Dr.", "Prof.") are not names.
            names |= {member.name, member.slug, *(w for w in member.name.split() if "." not in w)}
    return names


def test_the_page_names_nobody_and_loads_nothing_from_the_bundle() -> None:
    names = _cast_and_orgs()
    assert {"Helen", "Northlake Characterization Core", "vantage"} <= names
    for name in names:
        assert not re.search(rf"\b{re.escape(name)}\b", PAGE, re.IGNORECASE), name
    assert "/assets" not in PAGE
    assert "<h1>Harmony</h1>" in PAGE
    assert PAGE.count('type="password"') == 1
    assert ">Open</button>" in PAGE


def _inline(tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", PAGE, re.DOTALL)
    assert match is not None, tag
    return match.group(1)


def _hash(source: str) -> str:
    return "'sha256-" + b64encode(sha256(source.encode()).digest()).decode() + "'"


def _assert_page_headers(response: httpx.Response) -> None:
    csp = response.headers["content-security-policy"]
    assert f"script-src {_hash(_inline('script'))};" in csp
    assert f"style-src {_hash(_inline('style'))};" in csp
    for directive in (
        "default-src 'none'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ):
        assert directive in csp
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-robots-tag"] == "noindex"


async def test_the_page_is_served_with_its_policy(client: httpx.AsyncClient) -> None:
    page = await client.get("/api/gate")
    assert page.status_code == 200
    assert page.text == PAGE
    _assert_page_headers(page)

    locked = await client.get("/requests/abc")
    assert locked.status_code == 401
    assert locked.text == PAGE
    _assert_page_headers(locked)
    head = await client.head("/requests/abc")
    assert head.status_code == 401
    assert head.headers["content-type"].startswith("text/html")


async def test_nothing_from_the_request_is_written_into_the_page(
    client: httpx.AsyncClient,
) -> None:
    hostile = '"</script><script>alert(1)</script>'
    for path in (f"/{hostile}", f"/api/gate?next={hostile}"):
        response = await client.get(path, headers={"Referer": hostile, "User-Agent": hostile})
        assert response.text == PAGE
        assert hostile not in response.text


# --- The cookie -----------------------------------------------------------------------


def _fingerprint(passcode: str = PASSCODE) -> str:
    message = f"harmony.gate:{passcode}".encode()
    return hmac.new(SECRET.encode(), message, sha256).hexdigest()[:32]


def _minted(*, age_s: int = 0, passcode: str = PASSCODE) -> str:
    """A cookie value as 3.2 specifies it, signed `age_s` seconds ago."""

    class _Signed(TimestampSigner):
        def get_timestamp(self) -> int:
            return int(time.time()) - age_s

    serializer = URLSafeTimedSerializer(SECRET, salt="harmony.gate", signer=_Signed)
    return serializer.dumps({"f": _fingerprint(passcode)})


async def _status_with(c: httpx.AsyncClient, cookie: str) -> int:
    c.cookies.clear()
    c.cookies.set(GATE_COOKIE, cookie, domain="harmony.test")
    return (await c.get("/api/openapi.json")).status_code


async def test_the_cookie_holds_a_signed_fingerprint_of_the_passcode(
    client: httpx.AsyncClient,
) -> None:
    assert await _status_with(client, _minted()) == 200
    assert await _status_with(client, _minted(age_s=FOURTEEN_DAYS_S - 60)) == 200
    assert await _status_with(client, _minted(age_s=FOURTEEN_DAYS_S + 60)) == 401
    assert await _status_with(client, _minted(passcode="another passcode here")) == 401
    tampered = _minted()
    tampered = tampered[:-2] + ("A" if tampered[-2] != "A" else "B") + tampered[-1]
    assert await _status_with(client, tampered) == 401
    assert await _status_with(client, "") == 401
    assert await _status_with(client, "not-a-cookie") == 401


async def test_a_cookie_from_another_passcode_is_refused(dist: Path) -> None:
    async with _client(create_app(_settings(dist))) as a:
        unlocked = await _unlock(a)
        cookie = unlocked.cookies[GATE_COOKIE]
        assert await _status_with(a, cookie) == 200
    rotated = _settings(dist, access_code=SecretStr("a different passcode"))
    async with _client(create_app(rotated)) as b:
        assert await _status_with(b, cookie) == 401


# --- Unlock ---------------------------------------------------------------------------


def _cookie_attributes(response: httpx.Response) -> dict[str, str]:
    [header] = [v for v in response.headers.get_list("set-cookie") if v.startswith(GATE_COOKIE)]
    attributes: dict[str, str] = {}
    for part in header.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attributes[name.lower()] = value
    return attributes


async def test_the_right_passcode_unlocks(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/openapi.json")).status_code == 401
    response = await _unlock(client, next="/requests/abc")
    assert response.status_code == 200
    assert response.json() == {"next": "/requests/abc"}
    assert response.headers["cache-control"] == "no-store"
    attributes = _cookie_attributes(response)
    assert attributes["max-age"] == str(FOURTEEN_DAYS_S)
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/"
    assert {"httponly", "secure"} <= set(attributes)
    assert (await client.get("/api/openapi.json")).status_code == 200


async def test_the_cookie_is_not_secure_locally(dist: Path) -> None:
    async with _client(create_app(_settings(dist, app_env="local"))) as c:
        response = await _unlock(c)
    assert "secure" not in _cookie_attributes(response)


@pytest.mark.usefixtures("fast_failures")
async def test_the_passcode_is_trimmed_and_case_sensitive(client: httpx.AsyncClient) -> None:
    assert (await _unlock(client, f"  {PASSCODE}\n")).status_code == 200
    client.cookies.clear()
    wrong = await _unlock(client, PASSCODE.upper())
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "passcode_invalid"
    assert wrong.json()["detail"] == "That passcode isn't right."
    assert GATE_COOKIE not in wrong.cookies
    # Over 256 characters is a wrong attempt, however it starts.
    assert (await _unlock(client, PASSCODE + " " * 300)).status_code == 401


@pytest.mark.usefixtures("fast_failures")
async def test_the_sixth_attempt_from_one_client_in_15_minutes_is_throttled(
    client: httpx.AsyncClient,
) -> None:
    for _ in range(5):
        assert (await _unlock(client, "wrong", ip=CLIENT)).status_code == 401
    throttled = await _unlock(client, PASSCODE, ip=CLIENT)
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "too_many_attempts"
    assert 0 < int(throttled.headers["retry-after"]) <= 15 * 60
    # Another client is unaffected.
    assert (await _unlock(client, PASSCODE, ip="198.51.100.8")).status_code == 200


async def test_concurrent_guesses_from_one_client_are_compared_at_most_five_times(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="harmony.api.gate")
    responses = await asyncio.gather(*(_unlock(client, "wrong", ip=CLIENT) for _ in range(50)))
    statuses = [response.status_code for response in responses]
    assert statuses.count(401) <= 5
    assert statuses.count(429) == 50 - statuses.count(401)
    compared = [r for r in caplog.records if "gate_attempt outcome=wrong" in r.getMessage()]
    assert len(compared) == statuses.count(401)


@pytest.mark.usefixtures("fast_failures")
async def test_a_success_clears_the_count(client: httpx.AsyncClient) -> None:
    for _ in range(4):
        assert (await _unlock(client, "wrong", ip=CLIENT)).status_code == 401
    assert (await _unlock(client, PASSCODE, ip=CLIENT)).status_code == 200
    for _ in range(5):
        assert (await _unlock(client, "wrong", ip=CLIENT)).status_code == 401


async def test_a_right_passcode_does_not_wait_behind_wrong_ones(
    client: httpx.AsyncClient,
) -> None:
    # Five clients' failures queue for 300 ms slots behind one lock.
    failures = [
        asyncio.create_task(_unlock(client, "wrong", ip=f"198.51.100.{20 + i}")) for i in range(5)
    ]
    await asyncio.sleep(0.05)
    started = time.monotonic()
    right = await _unlock(client, PASSCODE, ip="198.51.100.9")
    elapsed = time.monotonic() - started
    assert right.status_code == 200
    assert elapsed < 0.25, elapsed
    assert not all(task.done() for task in failures)
    assert [r.status_code for r in await asyncio.gather(*failures)] == [401] * 5


async def test_a_flood_of_wrong_passcodes_never_holds_a_typos_correction(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong passcode settles its key once compared, so the visitor's correction goes
    through while the typo waits for its slot. Beyond `MAX_WAITING` waiting failures, one
    is answered at once: a flood from many keys holds a bounded number of requests."""
    monkeypatch.setattr(gate_limits, "FAILURE_SLOT_S", 0.5)
    monkeypatch.setattr(gate_limits, "MAX_WAITING", 3)
    queued = [
        asyncio.create_task(_unlock(client, "wrong", ip=f"198.51.100.{20 + i}")) for i in range(2)
    ]
    await asyncio.sleep(0.02)
    typo = asyncio.create_task(_unlock(client, "wrong", ip=CLIENT))
    await asyncio.sleep(0.02)

    started = time.monotonic()
    corrected = await _unlock(client, PASSCODE, ip=CLIENT)
    assert corrected.status_code == 200
    assert time.monotonic() - started < 0.25
    assert not typo.done()

    started = time.monotonic()
    flood = await asyncio.gather(
        *(_unlock(client, "wrong", ip=f"2001:db8:0:{i:x}::1") for i in range(30))
    )
    assert [r.status_code for r in flood] == [401] * 30
    assert time.monotonic() - started < 0.4
    assert [r.status_code for r in await asyncio.gather(typo, *queued)] == [401] * 3


@pytest.mark.usefixtures("fast_failures")
async def test_the_keys_of_one_network_share_its_allowance(client: httpx.AsyncClient) -> None:
    """A routed IPv6 /48 holds 65,536 /64 keys: each brings no fresh allowance."""
    statuses = [
        (await _unlock(client, "wrong", ip=f"2001:db8:0:{i:x}::1")).status_code for i in range(52)
    ]
    assert statuses == [401] * 50 + [429] * 2
    # A right passcode from that network waits too; another network is unaffected.
    assert (await _unlock(client, PASSCODE, ip="2001:db8:0:ffff::1")).status_code == 429
    assert (await _unlock(client, PASSCODE, ip="2001:db8:1::1")).status_code == 200


@pytest.mark.usefixtures("fast_failures")
@pytest.mark.parametrize("ip", ["10.0.0.1", None])
async def test_an_unknown_client_is_never_throttled_per_client(
    client: httpx.AsyncClient, ip: str | None
) -> None:
    for _ in range(8):
        assert (await _unlock(client, "wrong", ip=ip)).status_code == 401
    assert (await _unlock(client, PASSCODE, ip=ip)).status_code == 200


async def test_cross_site_attempts_are_refused_without_counting_against_the_client(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture, dist: Path
) -> None:
    # A third-party page can make its visitor's browser POST here (a no-cors fetch or
    # an auto-submitted form); the browser labels it, and it is never compared.
    caplog.set_level(logging.INFO, logger="harmony.api.gate")
    cross_site = {
        "X-Forwarded-For": CLIENT,
        "Sec-Fetch-Site": "cross-site",
        "Origin": "https://evil.example",
    }
    for _ in range(6):
        refused = await client.post("/api/gate", content=b"", headers=cross_site)
        assert refused.status_code == 403
        assert refused.json() == {"detail": REFUSAL}
        assert refused.headers["cache-control"] == "no-store"
    same_site = {"X-Forwarded-For": CLIENT, "Sec-Fetch-Site": "same-origin"}
    unlocked = await client.post("/api/gate", json={"passcode": PASSCODE}, headers=same_site)
    assert unlocked.status_code == 200
    digest = key_digest(CLIENT, _settings(dist))[:12]
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("gate_")]
    assert outcomes == [
        f"gate_attempt outcome=cross_site source=x-forwarded-for key={digest}"
    ] * 6 + [f"gate_attempt outcome=ok source=x-forwarded-for key={digest}"]


@pytest.mark.usefixtures("fast_failures")
async def test_a_forged_leftmost_forwarded_address_does_not_change_the_key(
    client: httpx.AsyncClient,
) -> None:
    for i in range(5):
        response = await _unlock(client, "wrong", ip=f"203.0.113.{i}, {CLIENT}")
        assert response.status_code == 401
    assert (await _unlock(client, PASSCODE, ip=f"192.0.2.99, {CLIENT}")).status_code == 429


@pytest.mark.usefixtures("fast_failures")
async def test_the_key_comes_from_true_client_ip_when_configured(dist: Path) -> None:
    app = create_app(_settings(dist, client_ip_header="true-client-ip"))
    async with _client(app) as c:
        for i in range(5):
            headers = {"True-Client-IP": CLIENT, "X-Forwarded-For": f"198.51.100.{30 + i}"}
            response = await c.post("/api/gate", json={"passcode": "wrong"}, headers=headers)
            assert response.status_code == 401
        headers = {"True-Client-IP": CLIENT, "X-Forwarded-For": "198.51.100.99"}
        throttled = await c.post("/api/gate", json={"passcode": PASSCODE}, headers=headers)
        assert throttled.status_code == 429
        other = {"True-Client-IP": "198.51.100.8", "X-Forwarded-For": CLIENT}
        assert (await c.post("/api/gate", json={"passcode": PASSCODE}, headers=other)).is_success


@pytest.mark.usefixtures("fast_failures")
async def test_attempts_are_logged_by_key_hash_only(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture, dist: Path
) -> None:
    caplog.set_level(logging.INFO, logger="harmony.api.gate")
    attempt = "my-secret-guess"
    await _unlock(client, attempt, ip=CLIENT)
    await _unlock(client, PASSCODE, ip=CLIENT)
    await _unlock(client, attempt)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("gate_attempt")]
    digest = key_digest(CLIENT, _settings(dist))[:12]
    assert lines == [
        f"gate_attempt outcome=wrong source=x-forwarded-for key={digest}",
        f"gate_attempt outcome=ok source=x-forwarded-for key={digest}",
        "gate_attempt outcome=wrong source=x-forwarded-for key=unknown",
    ]
    for record in caplog.records:
        text = record.getMessage() + repr(record.args)
        assert CLIENT not in text
        assert attempt not in text
        assert PASSCODE not in text


# --- Request bodies -------------------------------------------------------------------


async def _raw(
    app: Any, method: str, path: str, *, headers: Sequence[tuple[str, str]], chunks: list[bytes]
) -> tuple[int, dict[str, str], bytes]:
    """One request straight to the ASGI app, so `Content-Length` can be left out."""
    incoming: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.encode(), value.encode()) for name, value in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("harmony.test", 443),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    head = {name.decode(): value.decode() for name, value in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], head, body


async def test_unlock_needs_a_length_and_a_small_body(dist: Path) -> None:
    app = create_app(_settings(dist))
    body = json.dumps({"passcode": PASSCODE}).encode()
    json_type = ("content-type", "application/json")
    status, _, raw = await _raw(app, "POST", "/api/gate", headers=[json_type], chunks=[body])
    assert status == 411
    assert json.loads(raw)["code"] == "invalid"

    async with _client(app) as c:
        big = await c.post(
            "/api/gate", content=b"x" * 5 * 1024, headers={"Content-Type": "application/json"}
        )
        assert big.status_code == 413
        assert big.json()["code"] == "payload_too_large"

        async def chunked() -> AsyncIterator[bytes]:
            yield b'{"passcode": "'
            for _ in range(5):
                yield b"x" * 1024
            yield b'"}'

        cut = await c.post(
            "/api/gate", content=chunked(), headers={"Content-Type": "application/json"}
        )
        assert cut.status_code == 413
        assert cut.json()["code"] == "payload_too_large"
        # A small chunked body is read as usual.

        async def small() -> AsyncIterator[bytes]:
            yield json.dumps({"passcode": PASSCODE}).encode()

        assert (
            await c.post("/api/gate", content=small(), headers={"Content-Type": "application/json"})
        ).status_code == 200


async def test_other_routes_refuse_a_body_over_256_kb(client: httpx.AsyncClient) -> None:
    await _unlock(client)
    json_type = {"Content-Type": "application/json"}
    declared = await client.post("/api/session/start", content=b" " * 300 * 1024, headers=json_type)
    assert declared.status_code == 413
    assert declared.json() == {
        "detail": "the request body is too large",
        "code": "payload_too_large",
    }

    async def chunked() -> AsyncIterator[bytes]:
        for _ in range(300):
            yield b" " * 1024

    streamed = await client.post("/api/session/start", content=chunked(), headers=json_type)
    assert streamed.status_code == 413
    assert streamed.json()["code"] == "payload_too_large"


async def test_a_20000_character_non_ascii_message_fits() -> None:
    received: list[bytes] = []

    async def echo(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        received.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # ASCII-escaped JSON is the largest a 20,000-character message gets: 6 bytes a char.
    payload = json.dumps({"text": "研" * 20_000}, ensure_ascii=True).encode()
    assert len(payload) > 100_000
    halves = [payload[: len(payload) // 2], payload[len(payload) // 2 :]]
    status, _, _ = await _raw(BodyLimit(echo), "POST", "/x", headers=[], chunks=halves)
    assert status == 200
    assert received == [payload]


# --- What the gate refuses ------------------------------------------------------------


async def test_the_event_stream_is_refused_before_any_database_work(dist: Path) -> None:
    app = create_app(_settings(dist))

    def no_database() -> Any:
        raise AssertionError("a locked request opened a database session")

    services: AppServices = app.state.services
    app.state.services = replace(services, sessionmaker=no_database)
    async with _client(app) as c:
        response = await c.get("/api/events", params={"after": 0})
    assert response.status_code == 401
    assert response.json() == PASSCODE_REQUIRED
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"


async def test_websockets_are_closed_and_lifespan_passes(dist: Path) -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    gate = PasscodeGate(inner, settings=_settings(dist))
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    socket: Scope = {"type": "websocket", "path": "/api/events", "headers": []}
    await gate(socket, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    await gate({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


async def test_lock_clears_the_cookie(client: httpx.AsyncClient) -> None:
    await _unlock(client)
    assert (await client.get("/api/openapi.json")).status_code == 200
    locked = await client.post("/api/gate/lock")
    assert locked.status_code == 204
    assert locked.headers["cache-control"] == "no-store"
    assert _cookie_attributes(locked)["max-age"] == "0"
    assert GATE_COOKIE not in client.cookies
    after = await client.get("/api/session")
    assert after.status_code == 401
    assert after.json() == PASSCODE_REQUIRED
    # Locking needs the cookie, like everything else.
    assert (await client.post("/api/gate/lock")).json() == PASSCODE_REQUIRED


# --- Transport headers ----------------------------------------------------------------


async def test_prod_sends_hsts_on_every_response_and_has_no_docs(dist: Path) -> None:
    app = create_app(prod_settings(web_dist_dir=dist, client_ip_header="x-forwarded-for"))
    async with _client(app) as c:
        responses = [
            await c.get("/api/healthz"),
            await c.get("/api/session"),
            await c.get("/requests/abc"),
            await c.get("/api/gate"),
            await _unlock(c),
            await c.get("/requests/abc"),
            await c.get("/api/docs"),
            await c.get("/api/openapi.json"),
        ]
    for response in responses:
        hsts = response.headers.get("strict-transport-security")
        assert hsts == "max-age=31536000", response.url
    assert [r.status_code for r in responses] == [200, 401, 401, 200, 200, 200, 404, 404]


async def test_no_hsts_outside_prod(client: httpx.AsyncClient) -> None:
    assert "strict-transport-security" not in (await client.get("/api/healthz")).headers


async def test_gated_spa_files_are_private(client: httpx.AsyncClient) -> None:
    await _unlock(client)
    spa = await client.get("/requests/abc")
    assert spa.status_code == 200
    assert "<title>spa</title>" in spa.text
    assert spa.headers["cache-control"] == "private, no-cache"
    assert spa.headers["vary"] == "Cookie"
    asset = await client.get("/assets/app-1a2b3c.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "private, max-age=31536000, immutable"


# --- The gate off ---------------------------------------------------------------------


async def test_without_a_passcode_nothing_is_gated(dist: Path) -> None:
    async with _client(create_app(_settings(dist, access_code=None))) as c:
        assert (await c.get("/requests/abc")).text == "<!doctype html><title>spa</title>"
        assert (await c.get("/api/openapi.json")).status_code == 200
        for _ in range(8):
            unlocked = await _unlock(c, "anything", ip=CLIENT, next="//evil.com")
            assert unlocked.status_code == 200
            assert unlocked.json() == {"next": "/"}
            assert GATE_COOKIE not in unlocked.cookies
        assert "x-robots-tag" not in (await c.get("/api/healthz")).headers


# --- The limiters ---------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _attempt(limiter: UnlockLimiter, key: str = "k", network: str = "n") -> int | None:
    wait = limiter.admit(key, network)
    if wait is None:
        limiter.settle(key, network, success=False)
    return wait


def test_the_unlock_limiter_backs_off_up_to_an_hour() -> None:
    clock = _Clock()
    limiter = UnlockLimiter(clock=clock)
    for block_s in (15 * 60, 30 * 60, 60 * 60, 60 * 60):
        assert [_attempt(limiter) for _ in range(5)] == [None] * 5
        assert _attempt(limiter) == block_s
        clock.now += block_s - 1
        assert _attempt(limiter) == 1
        clock.now += 1
    assert _attempt(limiter, "other") is None


def test_a_lockout_long_after_the_last_block_starts_over_at_15_minutes() -> None:
    clock = _Clock()
    limiter = UnlockLimiter(clock=clock)
    for block_s in (15 * 60, 30 * 60):
        assert [_attempt(limiter) for _ in range(5)] == [None] * 5
        assert _attempt(limiter) == block_s
        clock.now += block_s
    # A day after the last block ended, the backoff starts over.
    clock.now += 24 * 3600
    assert [_attempt(limiter) for _ in range(5)] == [None] * 5
    assert _attempt(limiter) == 15 * 60


def test_the_unlock_window_slides() -> None:
    clock = _Clock()
    limiter = UnlockLimiter(clock=clock)
    for _ in range(4):
        assert _attempt(limiter) is None
    clock.now += 15 * 60
    assert [_attempt(limiter) for _ in range(5)] == [None] * 5


def test_one_unlock_in_flight_per_client() -> None:
    limiter = UnlockLimiter()
    assert limiter.admit("k", "n") is None
    assert limiter.admit("k", "n") == 1
    assert limiter.admit("other", "n") is None
    limiter.settle("k", "n", success=True)
    assert limiter.admit("k", "n") is None


def test_a_network_allows_its_attempts_per_window_and_a_success_gives_one_back() -> None:
    clock = _Clock()
    limiter = UnlockLimiter(network_attempts=3, clock=clock)
    assert [_attempt(limiter, f"k{i}") for i in range(4)] == [None] * 3 + [15 * 60]
    clock.now += 60
    assert _attempt(limiter, "k9", "m") is None
    # The window is fixed: room comes when it ends, not a window after the last attempt.
    assert _attempt(limiter, "k9") == 14 * 60
    clock.now += 14 * 60
    assert limiter.admit("k9", "n") is None
    limiter.settle("k9", "n", success=True)
    assert [_attempt(limiter, f"j{i}") for i in range(4)] == [None] * 3 + [15 * 60]


def test_a_full_table_of_live_clients_leaves_a_new_one_to_its_networks_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="harmony.api.gate_limits")
    clock = _Clock()
    limiter = UnlockLimiter(max_tracked=3, network_attempts=7, clock=clock)
    for key in ("a", "b", "c"):
        assert _attempt(limiter, key) is None
    # Every tracked client is live: a new one goes untracked as a client, so whoever
    # filled the table can't lock out every new visitor, but its network's allowance
    # still bounds it.
    assert [_attempt(limiter, "d") for _ in range(5)] == [None] * 4 + [15 * 60]
    assert sum("gate_clients_full" in r.getMessage() for r in caplog.records) == 1
    # Only that network: a newcomer from another one goes ahead.
    assert _attempt(limiter, "e", "m") is None
    # Once the window passes, idle clients make room, oldest first, and the network
    # starts over.
    clock.now += 15 * 60
    assert [_attempt(limiter, "d") for _ in range(6)] == [None] * 5 + [15 * 60]


def test_a_full_table_of_live_networks_refuses_a_new_one_until_one_goes_idle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="harmony.api.gate_limits")
    clock = _Clock()
    limiter = UnlockLimiter(max_tracked=2, clock=clock)
    assert _attempt(limiter, "a", "n1") is None
    assert _attempt(limiter, "b", "n2") is None
    clock.now += 60
    # Untracked, a new network would have no bound at all.
    assert _attempt(limiter, "c", "n3") == 14 * 60
    assert sum("gate_networks_full" in r.getMessage() for r in caplog.records) == 1
    # The tracked networks keep working.
    assert _attempt(limiter, "a", "n1") is None
    clock.now += 15 * 60
    assert _attempt(limiter, "c", "n3") is None


def test_many_distinct_clients_cost_little_per_attempt() -> None:
    # One unlock per /64, each from its own /48 and answered at once (411/422), past
    # both tables' caps: an attempt must not walk a whole table.
    clock = _Clock()
    limiter = UnlockLimiter(clock=clock)
    started = time.process_time()
    waits = []
    for i in range(3 * MAX_TRACKED // 2):
        clock.now += 0.001
        waits.append(_attempt(limiter, f"2001:db8:{i:x}::/64", f"2001:db8:{i:x}::/48"))
    elapsed = time.process_time() - started
    assert waits[:MAX_TRACKED] == [None] * MAX_TRACKED
    # Sweeping every tracked entry per attempt took over 20 s of CPU here.
    assert elapsed < 2, elapsed


async def test_a_spike_of_failures_is_logged_once_per_window(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate_limits, "FAILURE_SLOT_S", 0.0)
    caplog.set_level(logging.WARNING, logger="harmony.api.gate_limits")
    clock = _Clock()
    pacer = FailurePacer(spike=3, clock=clock)

    def spikes() -> int:
        return sum("gate_failures_spike" in r.getMessage() for r in caplog.records)

    for _ in range(6):
        await pacer.wait()
    assert spikes() == 1
    clock.now += 15 * 60
    for _ in range(4):
        await pacer.wait()
    assert spikes() == 2


async def test_at_most_max_waiting_failures_wait_for_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_limits, "FAILURE_SLOT_S", 0.05)
    monkeypatch.setattr(gate_limits, "MAX_WAITING", 3)
    pacer = FailurePacer()
    waits = [asyncio.create_task(pacer.wait()) for _ in range(10)]
    await asyncio.sleep(0)
    assert pacer.waiting == 3
    assert sum(task.done() for task in waits) == 7
    await asyncio.gather(*waits)
    assert pacer.waiting == 0


def test_new_sessions_are_limited_per_client_and_per_day() -> None:
    today = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)
    current = [today]
    limits = WorldLimits(per_client=5, daily=8, now=lambda: current[0])
    for _ in range(5):
        limits.new_session("a")
    with pytest.raises(TooManyWorlds) as refused:
        limits.new_session("a")
    assert refused.value.retry_after_s == 6 * 3600
    limits.new_session("b")
    limits.new_session(None)
    limits.new_session(None)
    with pytest.raises(TooManyWorlds):
        limits.new_session(None)
    current[0] = today + timedelta(hours=6)
    limits.new_session("a")


def test_recreates_track_a_bounded_number_of_sessions() -> None:
    clock = _Clock()
    limits = WorldLimits(recreates=2, max_tracked=2, clock=clock)
    first, second, third = uuid4(), uuid4(), uuid4()
    limits.recreate(first)
    limits.recreate(second)
    # Both tracked sessions are live, so a third goes untracked rather than growing
    # the table.
    for _ in range(3):
        limits.recreate(third)
    limits.recreate(first)
    with pytest.raises(TooManyWorlds):
        limits.recreate(first)
    clock.now += 3600
    # Stale sessions make room for the third, which is now counted.
    limits.recreate(third)
    limits.recreate(third)
    with pytest.raises(TooManyWorlds):
        limits.recreate(third)


def test_recreates_are_limited_per_session() -> None:
    clock = _Clock()
    limits = WorldLimits(recreates=20, clock=clock)
    session, other = uuid4(), uuid4()
    for _ in range(20):
        limits.recreate(session)
    with pytest.raises(TooManyWorlds) as refused:
        limits.recreate(session)
    assert refused.value.retry_after_s == 3600
    limits.recreate(other)
    clock.now += 3600
    limits.recreate(session)
