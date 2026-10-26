"""Liveness: `GET /api/healthz`. Cheap and dependency-free, so Render's health check
never fails because Postgres or Temporal is slow."""

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


@router.get("/healthz")
async def healthz(settings: SettingsDep) -> Health:
    return Health(status="ok", build_sha=settings.build_sha, app_env=settings.app_env)
