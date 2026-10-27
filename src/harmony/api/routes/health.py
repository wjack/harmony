"""Liveness: `GET|HEAD /api/healthz`. Cheap and dependency-free, so Render's health
check never fails because Postgres or Temporal is slow. It passes the passcode gate
without a cookie (Render's check sends none), and keeps `build_sha`, which the deploy
checks read to prove which build is live."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from harmony.api.deps import SettingsDep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    build_sha: str
    app_env: str


# One route per method: a single `api_route` for both would give them one operation id.
@router.get("/healthz")
@router.head("/healthz")
async def healthz(settings: SettingsDep) -> Health:
    return Health(status="ok", build_sha=settings.build_sha, app_env=settings.app_env)
