"""scripts/render_deploys.sh reads and triggers Render deploys with the API key kept in
process, and deploys only an approved, exact commit (deploy runbook steps 9b, 12 and
Rotation). It runs against a local stub of the Render API: nothing reaches Render.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "render_deploys.sh"
KEY = "rnd_" + "stubStubStub" * 2
COMMIT = "84e2cae7add474be986eb70aa6039b9e604e1322"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class RenderStub:
    """GET /v1/services?name=, GET and POST /v1/services/{id}/deploys."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None, Any]] = []
        self.statuses = {"web": "live", "worker": "live", "minio": "live"}
        self.reject_with: int | None = None

    def handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _reply(self, code: int, body: object) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, body: Any = None) -> None:
                stub.requests.append((self.command, self.path, self.headers["Authorization"], body))
                if stub.reject_with is not None:
                    self._reply(stub.reject_with, {"id": "e", "message": "unauthorized"})
                    return
                path, _, query = self.path.partition("?")
                if path == "/v1/services":
                    name = query.split("name=", 1)[1].split("&", 1)[0]
                    rows = [{"cursor": "c", "service": {"id": f"srv-{name}", "name": name}}]
                    rows.append({"cursor": "d", "service": {"id": "srv-x", "name": name + "-old"}})
                    self._reply(200, rows)
                    return
                short = path.split("/")[3].removeprefix("srv-harmony-")
                deploy = {
                    "id": "dep-1",
                    "status": stub.statuses[short],
                    "commit": {"id": COMMIT},
                    "trigger": "new_commit",
                    "finishedAt": "2026-10-27T07:00:00Z",
                }
                if self.command == "GET":
                    self._reply(200, [{"cursor": "c", "deploy": deploy}])
                else:
                    self._reply(
                        201, deploy | {"id": "dep-2", "status": "created", "trigger": "api"}
                    )

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self._handle(json.loads(self.rfile.read(length)))

        return Handler


@pytest.fixture
def render() -> Iterator[tuple[RenderStub, str]]:
    stub = RenderStub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


def _run(tmp_path: Path, url: str, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / ".env.local"
    # A blank template line above the real key, as .env.local.example ships it.
    env_file.write_text(f'RENDER_API_KEY=\nRENDER_API_KEY="{KEY}"\nRENDER_OWNER_ID=tea-stub\n')
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
    env |= {"HARMONY_ENV_FILE": str(env_file), "RENDER_API_URL": url, **extra}
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert KEY not in result.stdout + result.stderr
    return result


def test_status_reads_each_service_with_the_key_in_a_header(
    tmp_path: Path, render: tuple[RenderStub, str]
) -> None:
    stub, url = render

    result = _run(tmp_path, url, "status")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"harmony-{name}: live, commit {COMMIT}, trigger new_commit, 2026-10-27T07:00:00Z"
        for name in ("web", "worker", "minio")
    ]
    assert {auth for _, _, auth, _ in stub.requests} == {f"Bearer {KEY}"}
    assert all("ownerId=tea-stub" in path for _, path, _, _ in stub.requests[::2])


def test_status_fails_when_a_pre_deploy_failed(
    tmp_path: Path, render: tuple[RenderStub, str]
) -> None:
    stub, url = render
    stub.statuses["worker"] = "pre_deploy_failed"

    result = _run(tmp_path, url, "status")

    assert result.returncode == 1
    assert "harmony-worker: pre_deploy_failed" in result.stdout


def test_deploy_needs_the_user_s_approval_and_an_exact_commit(
    tmp_path: Path, render: tuple[RenderStub, str]
) -> None:
    stub, url = render

    refused = _run(tmp_path, url, "deploy", "worker", COMMIT)
    short = _run(tmp_path, url, "deploy", "worker", COMMIT[:12], HARMONY_RENDER_ALLOW_DEPLOY="1")

    assert refused.returncode == 2
    assert "without the user's approval" in refused.stderr
    assert short.returncode == 2
    assert stub.requests == []


def test_deploy_posts_the_approved_commit(tmp_path: Path, render: tuple[RenderStub, str]) -> None:
    stub, url = render

    result = _run(tmp_path, url, "deploy", "worker", COMMIT, HARMONY_RENDER_ALLOW_DEPLOY="1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("harmony-worker: deploy dep-2 created")
    method, path, _, body = stub.requests[-1]
    assert (method, path, body) == ("POST", "/v1/services/srv-harmony-worker/deploys", {
        "commitId": COMMIT
    })  # fmt: skip


def test_an_api_error_names_the_status_but_not_the_key(
    tmp_path: Path, render: tuple[RenderStub, str]
) -> None:
    stub, url = render
    stub.reject_with = 401

    result = _run(tmp_path, url, "status")

    assert result.returncode == 1
    assert result.stderr.strip() == "render_deploys.sh: GET /services returned 401 unauthorized"


def test_the_key_never_goes_to_a_plaintext_remote(tmp_path: Path) -> None:
    result = _run(tmp_path, "http://api.render.com.example/v1", "status")

    assert result.returncode == 2
    assert "must be https" in result.stderr
