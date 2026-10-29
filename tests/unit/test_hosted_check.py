"""scripts/hosted_check.py's own logic, against an in-process fake of the gate contract
(LIVE_DEMO_PLAN 3.2). The real runs are against the hosted app (section 6, step 10)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).parents[2]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hosted_check", ROOT / "scripts" / "hosted_check.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while decorating
    spec.loader.exec_module(module)
    return module


hosted_check = _load_script()

PASSCODE = "fixture-passcode-0001"
ADMIN = "fixture-admin-secret"
NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


class FakeGate:
    """The gate's contract, and just enough of the session routes behind it."""

    def __init__(
        self, *, fail_open: str | None = None, throttled: bool = False, live: bool = True
    ) -> None:
        self.fail_open = fail_open  # a path the gate wrongly lets through
        self.throttled = throttled  # this network is past the per-client unlock limit
        self.live = live  # live models run (off on compose-smoke: APP_ENV=ci, scripted)
        self.alex = str(uuid4())
        self.helen = str(uuid4())
        self.acted_as: list[str] = []
        self.posted: list[str] = []

    def member(self, member_id: str) -> dict[str, object]:
        slug, org = ("alex", "henderson") if member_id == self.alex else ("helen", "northlake")
        return {"id": member_id, "slug": slug, "org": {"slug": org}}

    def session(self, member_id: str) -> httpx.Response:
        view = {"world_id": str(uuid4()), "epoch": 2, "member": self.member(member_id)}
        return httpx.Response(200, json=view, headers={"X-Harmony-Cursor": "42"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        cookies = dict(
            pair.split("=", 1) for pair in request.headers.get("cookie", "").split("; ") if pair
        )
        if path == "/api/healthz":
            return httpx.Response(200, json={"status": "ok", "build_sha": "abc", "app_env": "prod"})
        if path == "/api/admin/live" and request.headers.get("x-admin-secret") == ADMIN:
            return httpx.Response(200, json={"paused": True, "provider_blocked_until": None})
        if path == "/api/gate" and request.method == "GET":
            return httpx.Response(200, html='<input type="password">', headers=NO_STORE)
        if path == "/api/gate" and self.throttled:
            problem = {"code": "too_many_attempts"}
            return httpx.Response(429, json=problem, headers={**NO_STORE, "Retry-After": "900"})
        if path == "/api/gate":
            if json.loads(request.content)["passcode"] != PASSCODE:
                return httpx.Response(401, json={"code": "passcode_invalid"}, headers=NO_STORE)
            # Secure, as outside APP_ENV=local, while the fake origin is plain http.
            cookie = "harmony_gate=g1; HttpOnly; Secure; SameSite=Lax; Path=/"
            return httpx.Response(200, json={"next": "/"}, headers={"Set-Cookie": cookie})
        if cookies.get("harmony_gate") != "g1" and path != self.fail_open:
            if path.startswith("/api/"):
                return httpx.Response(401, json={"code": "passcode_required"}, headers=NO_STORE)
            page = '<input type="password">' if request.method == "GET" else ""
            return httpx.Response(401, html=page, headers=NO_STORE)
        if path == "/api/session/start":
            response = self.session(self.helen)
            response.headers["Set-Cookie"] = "harmony_session=s1; HttpOnly; Secure; Path=/"
            return response
        if cookies.get("harmony_session") != "s1":
            return httpx.Response(401, json={"detail": "no session"})
        if path == "/api/session":
            return self.session(self.alex)
        if path == "/api/members":
            everyone = [self.member(self.helen), self.member(self.alex)]
            return httpx.Response(200, json={"featured": everyone, "all": []})
        if path == "/api/session/act-as":
            member_id = json.loads(request.content)["member_id"]
            self.acted_as.append(member_id)
            return self.session(member_id)
        if path == "/api/budget":
            return httpx.Response(200, json={"live": self.live, "state": "ok"})
        if path == "/api/conversations" and request.method == "POST":
            self.posted.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"id": str(uuid4())})
        return httpx.Response(404, json={"code": "not_found"})


def _client(gate: FakeGate) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://fake.test", transport=httpx.MockTransport(gate))


async def test_the_walk_passes_against_the_gate_contract() -> None:
    report = hosted_check.Report()
    async with _client(FakeGate()) as client:
        await hosted_check.check_walk(client, report)
    assert report.failures == 0


async def test_the_walk_catches_a_route_the_gate_lets_through() -> None:
    report = hosted_check.Report()
    async with _client(FakeGate(fail_open="/requests")) as client:
        await hosted_check.check_walk(client, report)
    assert report.failures == 2  # GET and HEAD


async def test_entering_unlocks_starts_and_acts_as_alex_with_secure_cookies_over_http() -> None:
    gate = FakeGate()
    report = hosted_check.Report()
    async with _client(gate) as client:
        visitor = hosted_check.Visitor(client)
        desk = await hosted_check.enter(visitor, PASSCODE, report, wrong_first=True)
    assert report.failures == 0
    assert gate.acted_as == [gate.alex]
    assert (desk.member_id, desk.epoch, desk.cursor) == (gate.alex, 2, "42")
    assert visitor.cookies == {"harmony_gate": "g1", "harmony_session": "s1"}


async def test_a_refused_passcode_stops_the_run() -> None:
    report = hosted_check.Report()
    async with _client(FakeGate()) as client:
        with pytest.raises(hosted_check.Stop):
            await hosted_check.enter(
                hosted_check.Visitor(client), "not-the-passcode", report, wrong_first=False
            )
    assert report.failures == 1


async def test_a_throttled_unlock_says_it_is_the_per_client_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = hosted_check.Report()
    async with _client(FakeGate(throttled=True)) as client:
        with pytest.raises(hosted_check.Stop):
            await hosted_check.enter(
                hosted_check.Visitor(client), PASSCODE, report, wrong_first=False
            )
    assert report.failures == 1
    assert (
        "FAIL  the passcode unlocks (status 429: this network hit the gate's per-client "
        "limit; retry after 900 s)" in capsys.readouterr().out
    )


def test_the_default_run_requires_the_build_it_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without `--build-sha`, any SHA would pass, a stale deploy's included."""
    monkeypatch.delenv("HARMONY_BASE_URL", raising=False)
    with pytest.raises(SystemExit) as refused:
        hosted_check.parse_args(["--base-url", "http://fake.test"])
    assert refused.value.code == 2
    assert "--build-sha" in capsys.readouterr().err
    args = hosted_check.parse_args(["--base-url", "http://fake.test", "--build-sha", "abc"])
    assert (args.build_sha, args.app_env) == ("abc", "prod")
    # Modes other than the default run don't read healthz.
    assert hosted_check.parse_args(["--base-url", "http://fake.test", "--rotation"]).rotation


@pytest.mark.parametrize(("build_sha", "failures"), [("abc", 0), ("def", 1)])
async def test_healthz_must_report_the_build_sha_given(build_sha: str, failures: int) -> None:
    report = hosted_check.Report()
    args = argparse.Namespace(app_env="prod", build_sha=build_sha)
    async with _client(FakeGate()) as client:
        await hosted_check.check_health(client, report, args)
    assert report.failures == failures


def _rotation_args(tmp_path: Path) -> argparse.Namespace:
    jar = tmp_path / "jar.json"
    hosted_check.save_jar(jar, {"harmony_gate": "g0", "harmony_session": "s1"})
    return argparse.Namespace(previous_jar=jar)


async def test_the_rotation_drill_refuses_a_passcode_that_wasnt_rotated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARMONY_PASSCODE", PASSCODE)
    monkeypatch.setenv("HARMONY_PREVIOUS_PASSCODE", PASSCODE)
    async with _client(FakeGate()) as client:
        with pytest.raises(SystemExit, match="wasn't rotated") as refused:
            await hosted_check.rotation(client, hosted_check.Report(), _rotation_args(tmp_path))
    # Both ways to rotate: -replace changes nothing while terraform.tfvars sets the passcode.
    message = str(refused.value.code)
    assert "change it there and run `HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply`" in message
    assert "-replace=random_password.access_code" in message


async def test_the_rotation_drill_says_when_to_capture_the_previous_passcode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARMONY_PASSCODE", PASSCODE)
    monkeypatch.delenv("HARMONY_PREVIOUS_PASSCODE", raising=False)
    async with _client(FakeGate()) as client:
        with pytest.raises(SystemExit, match="before the rotation's apply"):
            await hosted_check.rotation(client, hosted_check.Report(), _rotation_args(tmp_path))


async def test_the_rotation_drill_passes_once_the_old_passcode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARMONY_PASSCODE", PASSCODE)
    monkeypatch.setenv("HARMONY_PREVIOUS_PASSCODE", "fixture-passcode-0000")
    report = hosted_check.Report()
    async with _client(FakeGate()) as client:
        await hosted_check.rotation(client, report, _rotation_args(tmp_path))
    assert report.failures == 0


@pytest.mark.parametrize(
    ("times", "verdict"),
    [
        ([100.0, 115.2], None),
        ([100.0, 100.1], "0.1 s apart"),
        ([100.0, 131.0], "31.0 s apart"),
        ([100.0], "1 heartbeat(s) arrived"),
    ],
)
def test_heartbeats_bunched_together_mean_a_buffering_proxy(
    times: list[float], verdict: str | None
) -> None:
    assert hosted_check.heartbeat_gap(times) == verdict


@pytest.mark.parametrize(
    ("view", "retired"),
    [
        ({"status": "retired", "retire_workflow": "running"}, True),  # sleeping out the TTL
        ({"status": "retired", "retire_workflow": "completed"}, True),
        ({"status": "retired", "retire_workflow": None}, False),  # retired inline
        ({"status": "retired", "retire_workflow": "failed"}, False),
        ({"status": "retiring", "retire_workflow": "running"}, False),
    ],
)
def test_the_world_counts_as_retired_by_the_worker_only_through_its_workflow(
    view: dict[str, object], retired: bool
) -> None:
    assert hosted_check.retired_by_worker(view) is retired


def test_a_saved_jar_is_private_and_reads_back(tmp_path: Path) -> None:
    jar = tmp_path / "jar.json"
    jar.write_text("{}")
    jar.chmod(0o644)

    hosted_check.save_jar(jar, {"harmony_gate": "g1", "harmony_session": "s1"})

    assert stat.S_IMODE(jar.stat().st_mode) == 0o600
    assert hosted_check.load_jar(jar) == {"harmony_gate": "g1", "harmony_session": "s1"}
    jar.write_text('["not", "a", "jar"]')
    with pytest.raises(ValueError, match="not a cookie jar"):
        hosted_check.load_jar(jar)


async def test_a_refused_turn_needs_live_models_and_says_so_without_posting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Compose-smoke runs scripted models: nothing reads the live switch there, so the
    # turn would play and the check could only time out.
    monkeypatch.setenv("HARMONY_PASSCODE", PASSCODE)
    monkeypatch.setenv("HARMONY_ADMIN_SECRET", ADMIN)
    gate = FakeGate(live=False)
    report = hosted_check.Report()
    async with _client(gate) as client:
        with pytest.raises(hosted_check.Stop):
            await hosted_check.refused_turn(client, report, argparse.Namespace(turn_wait=60.0))
    assert gate.posted == []
    out = capsys.readouterr().out
    assert "FAIL  live models run here (GET /api/budget: status 200, live=False)" in out
    assert "MODEL_MODE=live" in out


@pytest.mark.parametrize("inside", ["jar.json", "scripts/jar.json", "."])
def test_a_saved_jar_must_be_outside_the_repository(inside: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="inside the repository"):
        hosted_check.jar_path(str(ROOT / inside))


def test_a_saved_jar_outside_the_repository_is_taken(tmp_path: Path) -> None:
    assert (
        hosted_check.jar_path(str(tmp_path / "harmony.cookies.json"))
        == (tmp_path / "harmony.cookies.json").resolve()
    )
