"""Post-deploy checks of the hosted app, over HTTP only (LIVE_DEMO_PLAN section 6,
steps 10, 12 and 13). It never makes a model call: the one chat message it sends
(`--refused-turn`) goes only while live models are paused.

Secrets come from the environment, filled in process from Terraform's outputs and
never echoed:

    HARMONY_BASE_URL="$(scripts/tf.sh output -raw web_url)" \\
    HARMONY_PASSCODE="$(scripts/tf.sh output -raw access_code)" \\
    HARMONY_ADMIN_SECRET="$(scripts/tf.sh output -raw admin_secret)" \\
      uv run python scripts/hosted_check.py --build-sha "$(git rev-parse origin/main)"

    (or one of the modes below in place of --build-sha)

Against compose-smoke (APP_ENV=ci, built from the checkout):

    HARMONY_PASSCODE=... uv run python scripts/hosted_check.py \\
      --base-url http://localhost:18000 --app-env ci --build-sha "$(git rev-parse HEAD)"

    HARMONY_PASSCODE           the master passcode (every mode but --client-key)
    HARMONY_ADMIN_SECRET       --reset-retires, --refused-turn and --client-key
    HARMONY_PREVIOUS_PASSCODE  --rotation: the passcode before the rotation. Capture it
                               before the rotation's apply, which replaces what
                               `scripts/tf.sh output -raw access_code` returns:

    export HARMONY_PREVIOUS_PASSCODE="$(scripts/tf.sh output -raw access_code)"

Modes, one per run:

    (none)            /api/healthz reports --app-env (default prod) and --build-sha,
                      which this run requires: the commit that must be live; then
                      the no-cookie walk; a wrong passcode, then an unlock; session
                      start; act as Alex at Henderson (never Northlake or Vantage, which
                      would start the adversary); an event stream that must show
                      `: ready` and two heartbeats about 15 s apart (no proxy buffering);
                      and a resume with Last-Event-ID
    --across-deploy   holds a stream while the operator redeploys, then expects a clean
                      reconnect, or a 409 and a re-snapshot
    --reset-retires   Reset must hand the old world to a retire workflow on Temporal,
                      whose worker marks it retired (the workflow then waits out the
                      row TTL before deleting the rows)
    --refused-turn    with live models paused, a chat turn as Alex fails as `paused`
                      and the global spend doesn't move. Only where hosted caps apply
                      (the hosted app, or a local live stack with HOSTED_CAPS_IN_DEV=1):
                      scripted turns never read the live switch, so on compose-smoke it
                      stops at once, saying so
    --rotation        the previous cookie jar (--previous-jar) and passcode get 401;
                      the new passcode unlocks
    --client-key      prints GET /api/admin/client-key: which header carries your IP

Cookies stay in memory. `--save-jar FILE` writes the visitor's cookies to FILE (mode
600) after the default run, for a later `--rotation --previous-jar FILE`; delete it
after the drill. FILE must be outside the repository, where `git add` can't pick up a
working gate cookie. Prints PASS or FAIL per check and exits 1 on any failure.

The gate limits each client network: 5 unlocks per 15 minutes, counting successes, and
5 new sessions per UTC day (every mode but --rotation and --client-key starts one). A
check refused with 429 says so, with the server's Retry-After.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from harmony.api.gate import GATE_COOKIE
from harmony.auth.session import SESSION_COOKIE
from harmony.events.schemas import EventEnvelope, SessionMemberChanged
from harmony.events.sse import READY_COMMENT, SseFrame, decode_sse

HEARTBEAT_S: Final = 15.0  # SseOptions.heartbeat_s
# A heartbeat gap outside this window means a proxy held the stream back.
HEARTBEAT_WINDOW_S: Final = (10.0, 20.0)
READY_WAIT_S: Final = 10.0
PING_PREFIX: Final = "ping"  # sse-starlette's heartbeat comment
ADMIN_HEADER: Final = "X-Admin-Secret"
CURSOR_HEADER: Final = "X-Harmony-Cursor"
# Who the checks act as: a featured member whose world starts nothing on its own.
ACT_AS: Final = ("alex", "henderson")
ROOT: Final = Path(__file__).resolve().parents[1]

# The no-cookie walk: every one of these must get the gate's 401.
LOCKED_API: Final = (
    ("GET", "/api/session"),
    ("GET", "/api/events"),
    ("GET", "/api/docs"),
    ("GET", "/api/openapi.json"),
    ("GET", "/api/no-such-route"),
    ("POST", "/api/session/start"),
    ("POST", "/api/worlds/reset"),
    ("POST", "/api/admin/live"),  # no admin secret
)
LOCKED_PAGES: Final = ("/", "/requests", "/network", "/assets/index.js")


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" ({detail})" if detail and not ok else ""))
        self.failures += not ok
        return ok

    def note(self, line: str) -> None:
        print(f"      {line}")


class Stop(Exception):
    """A check failed that the rest of the run depends on."""


def env_secret(name: str, hint: str = "from `scripts/tf.sh output -raw ...`") -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"hosted_check.py: set {name} ({hint})")
    return value


def status_of(response: httpx.Response) -> str:
    """A response's status for a FAIL line; a 429 says it is the gate's per-client limit,
    so it doesn't read as a broken gate."""
    if response.status_code != 429:
        return f"status {response.status_code}"
    wait = response.headers.get("retry-after", "?")
    return f"status 429: this network hit the gate's per-client limit; retry after {wait} s"


def problem_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def locked(response: httpx.Response, *, api: bool) -> str | None:
    """Why `response` isn't the gate's refusal, or None when it is."""
    if response.status_code != 401:
        return f"status {response.status_code}"
    if "no-store" not in response.headers.get("cache-control", ""):
        return "no Cache-Control: no-store"
    if "noindex" not in response.headers.get("x-robots-tag", ""):
        return "no X-Robots-Tag: noindex"
    if api and problem_code(response) != "passcode_required":
        return f"code {problem_code(response)!r}"
    if not api and response.request.method == "GET" and 'type="password"' not in response.text:
        return "not the passcode page"
    return None


def heartbeat_gap(times: list[float]) -> str | None:
    """Why two heartbeats' arrival times show a buffered stream, or None."""
    if len(times) < 2:
        return f"{len(times)} heartbeat(s) arrived"
    gap = times[1] - times[0]
    low, high = HEARTBEAT_WINDOW_S
    return None if low <= gap <= high else f"{gap:.1f} s apart"


def jar_path(value: str) -> Path:
    """`--save-jar`: a path outside the repository. The jar holds a gate cookie that
    unlocks the hosted app until the next rotation."""
    path = Path(value).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise argparse.ArgumentTypeError(
            f"{value} is inside the repository, where `git add` could commit a working "
            'gate cookie; use a path outside it, such as "$TMPDIR/harmony.cookies.json"'
        )
    return path


def save_jar(path: Path, cookies: Mapping[str, str]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an existing file keeps its mode through O_CREAT
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(dict(cookies), file)


def load_jar(path: Path) -> dict[str, str]:
    cookies = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cookies, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in cookies.items()
    ):
        raise ValueError(f"{path} is not a cookie jar from --save-jar")
    return dict(cookies)


@dataclass
class Visitor:
    """One browser. Its cookies go out by hand: outside APP_ENV=local they are Secure,
    and httpx's own jar would keep them off a plain-http (Compose) origin."""

    client: httpx.AsyncClient
    cookies: dict[str, str] = field(default_factory=dict[str, str])

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return headers

    async def send(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        response = await self.client.request(
            method, path, json=body, headers=self._headers(headers)
        )
        self.client.cookies.clear()
        self.cookies.update(response.cookies)
        return response

    @asynccontextmanager
    async def stream(
        self, params: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[httpx.Response]:
        async with self.client.stream(
            "GET", "/api/events", params=params, headers=self._headers(headers)
        ) as response:
            yield response

    async def unlock(self, passcode: str) -> httpx.Response:
        return await self.send("POST", "/api/gate", body={"passcode": passcode, "next": "/"})


@dataclass(frozen=True, slots=True)
class Desk:
    """The visitor's desk as the last snapshot showed it."""

    member_id: str
    epoch: int
    cursor: str


def is_ready(frame: SseFrame) -> bool:
    return frame.comment == READY_COMMENT


def is_ping(frame: SseFrame) -> bool:
    return frame.comment is not None and frame.comment.startswith(PING_PREFIX)


class Watch:
    """One open stream, read the way EventSource reads it: the last id (for
    Last-Event-ID), every id, and when each heartbeat arrived."""

    def __init__(self, response: httpx.Response, last_id: str) -> None:
        self._frames = decode_sse(response.aiter_lines())
        self.last_id = last_id
        self.ids: list[int] = []
        self.pings: list[float] = []

    async def until(self, done: Callable[[SseFrame], bool], wait_s: float, what: str) -> None:
        """Read until `done`; a timeout or the stream's end is a `Stop`."""
        try:
            ended = not await self.read(done, wait_s)
        except TimeoutError:
            raise Stop(f"{what}: not within {wait_s:.0f} s") from None
        if ended:
            raise Stop(f"{what}: the server ended the stream first")

    async def read(self, done: Callable[[SseFrame], bool], wait_s: float) -> bool:
        """Read until `done` (True) or the stream's end (False), for at most `wait_s`."""
        async with asyncio.timeout(wait_s):
            async for frame in self._frames:
                if frame.id is not None:
                    self.last_id = frame.id
                    self.ids.append(int(frame.id))
                if is_ping(frame):
                    self.pings.append(time.monotonic())
                if done(frame):
                    return True
        return False


# --- Steps shared by the modes --------------------------------------------------------


async def check_health(client: httpx.AsyncClient, report: Report, args: argparse.Namespace) -> None:
    get = await client.get("/api/healthz")
    head = await client.head("/api/healthz")
    report.check(
        "GET and HEAD /api/healthz are open",
        get.status_code == 200 and head.status_code == 200,
        f"GET {get.status_code}, HEAD {head.status_code}",
    )
    if get.status_code != 200:
        return
    health = get.json()
    report.check(
        f"healthz reports app_env={args.app_env}",
        health.get("app_env") == args.app_env,
        f"app_env={health.get('app_env')}",
    )
    sha = health.get("build_sha")
    report.check(
        "healthz reports the build",
        sha == args.build_sha,
        f"build_sha={sha}, expected {args.build_sha}",
    )


async def check_walk(client: httpx.AsyncClient, report: Report) -> None:
    """Without a cookie only the health check and the gate's own page answer."""
    stranger = Visitor(client)
    for method, path in LOCKED_API:
        why = locked(await stranger.send(method, path), api=True)
        report.check(f"no cookie: {method} {path} is locked", why is None, why or "")
    wrong_admin = await stranger.send(
        "GET", "/api/admin/spend", headers={ADMIN_HEADER: secrets.token_urlsafe(24)}
    )
    why = locked(wrong_admin, api=True)
    report.check("a wrong admin secret: GET /api/admin/spend is locked", why is None, why or "")
    for method in ("GET", "HEAD"):
        for path in LOCKED_PAGES:
            why = locked(await stranger.send(method, path), api=False)
            report.check(f"no cookie: {method} {path} is locked", why is None, why or "")
    page = await stranger.send("GET", "/api/gate")
    report.check(
        "GET /api/gate is the passcode page",
        page.status_code == 200 and 'type="password"' in page.text,
        f"status {page.status_code}",
    )


async def enter(visitor: Visitor, passcode: str, report: Report, *, wrong_first: bool) -> Desk:
    """Unlock, start a session and act as Alex: what a visitor's first minute does."""
    if wrong_first:
        wrong = await visitor.unlock(secrets.token_urlsafe(18))
        report.check(
            "a wrong passcode is refused",
            wrong.status_code == 401 and problem_code(wrong) == "passcode_invalid",
            f"{status_of(wrong)}, code {problem_code(wrong)!r}",
        )
    unlocked = await visitor.unlock(passcode)
    if not report.check(
        "the passcode unlocks",
        unlocked.status_code == 200 and GATE_COOKIE in visitor.cookies,
        status_of(unlocked),
    ):
        raise Stop
    started = await visitor.send("POST", "/api/session/start")
    if not report.check(
        "start a session",
        started.status_code == 200 and SESSION_COOKIE in visitor.cookies,
        status_of(started),
    ):
        raise Stop
    members = await visitor.send("GET", "/api/members")
    listed: list[dict[str, Any]] = (
        members.json()["featured"] + members.json()["all"] if members.status_code == 200 else []
    )
    member_id = next((m["id"] for m in listed if (m["slug"], m["org"]["slug"]) == ACT_AS), None)
    if not report.check("find Alex at Henderson", member_id is not None, f"{members.status_code}"):
        raise Stop
    acted = await visitor.send("POST", "/api/session/act-as", body={"member_id": member_id})
    view = acted.json() if acted.status_code == 200 else {}
    acting = (view.get("member", {}).get("slug"), view.get("member", {}).get("org", {}).get("slug"))
    if not report.check("act as Alex at Henderson", acting == ACT_AS, f"{acted.status_code}"):
        raise Stop
    return desk_of(acted)


def desk_of(response: httpx.Response) -> Desk:
    """The desk a session snapshot (`SessionView` and its cursor header) shows."""
    view = response.json()
    return Desk(view["member"]["id"], view["epoch"], response.headers.get(CURSOR_HEADER, "0"))


def stream_params(desk: Desk) -> dict[str, str]:
    return {"after": desk.cursor, "epoch": str(desk.epoch)}


# --- Modes ----------------------------------------------------------------------------


async def default_run(client: httpx.AsyncClient, report: Report, args: argparse.Namespace) -> None:
    await check_health(client, report, args)
    await check_walk(client, report)
    visitor = Visitor(client)
    desk = await enter(visitor, env_secret("HARMONY_PASSCODE"), report, wrong_first=True)
    if args.save_jar:
        save_jar(args.save_jar, visitor.cookies)
        report.note(f"saved the visitor's cookies to {args.save_jar} (mode 600)")

    async with visitor.stream(stream_params(desk)) as response:
        if not report.check("open the event stream", response.status_code == 200):
            raise Stop
        watch = Watch(response, desk.cursor)
        await watch.until(is_ready, READY_WAIT_S, "`: ready`")
        report.check("the stream says `: ready` at once", True)
        report.note(f"waiting for two heartbeats (about {2 * HEARTBEAT_S:.0f} s)")
        await watch.until(lambda _: len(watch.pings) >= 2, 3 * HEARTBEAT_S, "two heartbeats")
        why = heartbeat_gap(watch.pings)
        report.check("heartbeats arrive about 15 s apart (nothing buffers)", why is None, why or "")
    resume_from = watch.last_id

    # Disconnected: this act-as commits an event the next connection must deliver.
    acted = await visitor.send("POST", "/api/session/act-as", body={"member_id": desk.member_id})
    if not report.check("act as Alex again", acted.status_code == 200, f"{acted.status_code}"):
        raise Stop
    epoch = desk_of(acted).epoch

    def missed(frame: SseFrame) -> bool:
        if frame.data is None:
            return False
        event = EventEnvelope.model_validate_json(frame.data).event
        return isinstance(event, SessionMemberChanged) and event.epoch == epoch

    headers = {"Last-Event-ID": resume_from}
    async with visitor.stream({"epoch": str(epoch)}, headers) as response:
        if not report.check("reconnect with Last-Event-ID", response.status_code == 200):
            raise Stop
        resumed = Watch(response, resume_from)
        await resumed.until(missed, READY_WAIT_S, "the event committed while disconnected")
    report.check(
        "the event committed while disconnected arrives on resume, after Last-Event-ID",
        all(seq > int(resume_from) for seq in resumed.ids),
        f"Last-Event-ID {resume_from}, received ids {resumed.ids}",
    )


async def reopen(visitor: Visitor, desk: Desk, last_id: str, report: Report, wait_s: float) -> None:
    """Reconnect as EventSource does after a drop, retrying while the service restarts;
    on a 409, re-snapshot and reopen from the snapshot's cursor, as the app does."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            async with visitor.stream(stream_params(desk), {"Last-Event-ID": last_id}) as response:
                status = response.status_code
                if status == 200:
                    await Watch(response, last_id).until(is_ready, READY_WAIT_S, "`: ready`")
                    report.check("a clean reconnect with Last-Event-ID", True)
                    return
        except httpx.TransportError:
            status = 0
        if status not in (0, 502, 503, 504) or time.monotonic() > deadline:
            break
        await asyncio.sleep(5)
    if not report.check("reconnect after the deploy: 200, or 409", status == 409, f"{status}"):
        return
    snapshot = await visitor.send("GET", "/api/session")
    if not report.check("a 409, then a fresh snapshot", snapshot.status_code == 200):
        return
    async with visitor.stream(stream_params(desk_of(snapshot))) as response:
        ok = response.status_code == 200
        if ok:
            await Watch(response, "0").until(is_ready, READY_WAIT_S, "`: ready`")
        report.check("the stream reopens from the snapshot's cursor", ok, f"{response.status_code}")


async def across_deploy(
    client: httpx.AsyncClient, report: Report, args: argparse.Namespace
) -> None:
    visitor = Visitor(client)
    desk = await enter(visitor, env_secret("HARMONY_PASSCODE"), report, wrong_first=False)
    async with visitor.stream(stream_params(desk)) as response:
        if not report.check("open the event stream", response.status_code == 200):
            raise Stop
        watch = Watch(response, desk.cursor)
        await watch.until(is_ready, READY_WAIT_S, "`: ready`")
        report.note(f"holding the stream: redeploy now (waiting up to {args.deploy_wait:.0f} s)")
        try:
            await watch.read(lambda _: False, args.deploy_wait)
        except TimeoutError:
            raise Stop(f"no deploy ended the stream within {args.deploy_wait:.0f} s") from None
        except httpx.TransportError as exc:
            report.note(f"the stream dropped: {type(exc).__name__}")
    report.check("the stream ends with the deploy", True)
    await reopen(visitor, desk, watch.last_id, report, args.reconnect_wait)


async def reset_retires(
    client: httpx.AsyncClient, report: Report, args: argparse.Namespace
) -> None:
    admin = {ADMIN_HEADER: env_secret("HARMONY_ADMIN_SECRET")}
    visitor = Visitor(client)
    await enter(visitor, env_secret("HARMONY_PASSCODE"), report, wrong_first=False)
    reset = await visitor.send("POST", "/api/worlds/reset")
    if not report.check("Reset", reset.status_code == 200, f"status {reset.status_code}"):
        raise Stop
    result = reset.json()
    old, status = result["previous_world_id"], result["previous_world_status"]
    if not report.check(
        "Reset hands the old world to a retire workflow",
        status == "retiring",
        f"previous_world_status={status}"
        + (
            ": retirement ran inline, so the API couldn't start the workflow on Temporal"
            " (check the web service's TEMPORAL_* env and its logs)"
            if status == "retired"
            else ""
        ),
    ):
        return
    deadline = time.monotonic() + args.retire_wait
    seen: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await visitor.send("GET", f"/api/admin/worlds/{old}", headers=admin)
        seen = response.json() if response.status_code == 200 else {"status": response.status_code}
        if retired_by_worker(seen) or seen.get("retire_workflow") in ("failed", "terminated"):
            break
        await asyncio.sleep(2)
    report.check(
        "the worker retires it (status retired, by a running or completed retire workflow)",
        retired_by_worker(seen),
        f"after {args.retire_wait:.0f} s: {seen}",
    )


def retired_by_worker(view: Mapping[str, Any]) -> bool:
    """Whether the worker's retire workflow, not an inline fallback, retired the world.
    The workflow keeps running after marking it retired: it sleeps out the row TTL (an
    hour in the API) before deleting the rows, so `running` is the usual answer here."""
    return view.get("status") == "retired" and view.get("retire_workflow") in (
        "running",
        "completed",
    )


async def spend(visitor: Visitor, admin: Mapping[str, str]) -> dict[str, Any]:
    response = await visitor.send("GET", "/api/admin/spend", headers=admin)
    if response.status_code != 200:
        raise Stop(f"GET /api/admin/spend returned {response.status_code}")
    return response.json()


async def refused_turn(client: httpx.AsyncClient, report: Report, args: argparse.Namespace) -> None:
    admin = {ADMIN_HEADER: env_secret("HARMONY_ADMIN_SECRET")}
    visitor = Visitor(client)
    before = await spend(visitor, admin)
    # The only guard between this check and a paid turn: never post unless paused.
    if not report.check(
        "live models are paused",
        before["live"]["paused"] is True,
        "run scripts/hosted_admin.sh pause first",
    ):
        raise Stop
    await enter(visitor, env_secret("HARMONY_PASSCODE"), report, wrong_first=False)
    budget = await visitor.send("GET", "/api/budget")
    live = budget.json().get("live") if budget.status_code == 200 else None
    if not report.check(
        "hosted caps apply here", live is True, f"GET /api/budget: {status_of(budget)}, live={live}"
    ):
        report.note(
            "scripted turns and calls without hosted caps never read the live switch, so "
            "the pause isn't enforced on this stack; run --refused-turn on the hosted app, "
            "or locally with MODEL_MODE=live and HOSTED_CAPS_IN_DEV=1"
        )
        raise Stop
    posted = await visitor.send(
        "POST", "/api/conversations", body={"text": "hosted_check: a turn while paused"}
    )
    if not report.check("post a chat message as Alex", posted.status_code == 200):
        raise Stop
    conversation = posted.json()["id"]
    failure = None
    deadline = time.monotonic() + args.turn_wait
    while failure is None and time.monotonic() < deadline:
        await asyncio.sleep(1)
        detail = await visitor.send("GET", f"/api/conversations/{conversation}")
        failure = detail.json().get("turn_failure") if detail.status_code == 200 else None
    report.check("the turn fails as `paused`", failure == "paused", f"turn_failure={failure}")
    after = await spend(visitor, admin)
    spent = [(view["live"].get("global") or {}).get("spent_usd") for view in (before, after)]
    report.check("global spend is unchanged", spent[0] == spent[1], f"{spent[0]} -> {spent[1]}")


async def rotation(client: httpx.AsyncClient, report: Report, args: argparse.Namespace) -> None:
    if args.previous_jar is None:
        sys.exit("hosted_check.py: --rotation needs --previous-jar FILE (from --save-jar)")
    previous = env_secret(
        "HARMONY_PREVIOUS_PASSCODE",
        "the passcode before the rotation, captured with `scripts/tf.sh output -raw "
        "access_code` before the rotation's apply",
    )
    passcode = env_secret("HARMONY_PASSCODE")
    if previous == passcode:
        sys.exit(
            "hosted_check.py: HARMONY_PREVIOUS_PASSCODE equals HARMONY_PASSCODE, so the "
            "passcode wasn't rotated. If terraform.tfvars sets access_code, change it there "
            "and run `HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply` (a -replace changes "
            "nothing while it is set). Otherwise rotate the generated passcode with "
            "`HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply "
            "-replace=random_password.access_code` (a -replace given only to `plan` does "
            "nothing). Capture the previous passcode before the apply."
        )
    old = Visitor(client, load_jar(args.previous_jar))
    why = locked(await old.send("GET", "/api/session"), api=True)
    report.check("the previous cookie jar is locked out", why is None, why or "")
    refused = await Visitor(client).unlock(previous)
    report.check(
        "the previous passcode is refused",
        refused.status_code == 401 and problem_code(refused) == "passcode_invalid",
        status_of(refused),
    )
    unlocked = await old.unlock(passcode)
    report.check(
        "the new passcode unlocks",
        unlocked.status_code == 200 and GATE_COOKIE in old.cookies,
        status_of(unlocked),
    )
    back = await old.send("GET", "/api/session")
    report.check("the visitor is back in their own session", back.status_code == 200)


async def client_key(client: httpx.AsyncClient, report: Report, args: argparse.Namespace) -> None:
    response = await Visitor(client).send(
        "GET", "/api/admin/client-key", headers={ADMIN_HEADER: env_secret("HARMONY_ADMIN_SECRET")}
    )
    if report.check("GET /api/admin/client-key", response.status_code == 200):
        print(json.dumps(response.json(), indent=2))


MODES: Final = {
    "across_deploy": across_deploy,
    "reset_retires": reset_retires,
    "refused_turn": refused_turn,
    "rotation": rotation,
    "client_key": client_key,
}


async def run(args: argparse.Namespace) -> int:
    report = Report()
    mode = next((MODES[name] for name in MODES if getattr(args, name)), default_run)
    print(f"hosted_check against {args.base_url}")
    timeout = httpx.Timeout(30.0, read=None)  # stream reads are bounded by Watch
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        try:
            await mode(client, report, args)
        except Stop as stop:
            if stop.args:
                report.check(str(stop.args[0]), False)
        except (TimeoutError, httpx.HTTPError) as exc:
            report.check("the app kept answering", False, repr(exc))
    return 1 if report.failures else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-deploy checks of the hosted app.")
    parser.add_argument("--base-url", default=os.environ.get("HARMONY_BASE_URL"))
    modes = parser.add_mutually_exclusive_group()
    for name in MODES:
        modes.add_argument(f"--{name.replace('_', '-')}", action="store_true")
    parser.add_argument("--app-env", default="prod", help="what /api/healthz must report")
    parser.add_argument(
        "--build-sha", help="the default run: the commit /api/healthz must report (required)"
    )
    parser.add_argument(
        "--save-jar", type=jar_path, help="default run: keep the cookies here (outside the repo)"
    )
    parser.add_argument("--previous-jar", type=Path, help="--rotation: the saved cookies")
    parser.add_argument("--deploy-wait", type=float, default=1200.0)
    parser.add_argument("--reconnect-wait", type=float, default=300.0)
    parser.add_argument("--retire-wait", type=float, default=120.0)
    parser.add_argument("--turn-wait", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("set HARMONY_BASE_URL or pass --base-url")
    if not any(getattr(args, name) for name in MODES) and not args.build_sha:
        # Any SHA would pass otherwise, a stale deploy's included.
        parser.error(
            'the default run checks the live build: pass --build-sha "$(git rev-parse '
            'origin/main)", the commit that must be live'
        )
    args.base_url = args.base_url.rstrip("/")
    return args


def main() -> None:
    sys.exit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
