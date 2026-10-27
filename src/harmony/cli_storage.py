"""`harmony storage check`: put, get and delete a probe object through the store
OBJECT_STORE selects, the deploy runbook's check of MinIO from Render Shell.

Prints PASS or FAIL per step and exits 1 on any failure. The probe sits under the nil
world and org ids, which no world has, so it never lands in a world's objects.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import typer

from harmony.config import Settings, get_settings
from harmony.storage import ObjectKey, ObjectStore, create_object_store

app = typer.Typer(add_completion=False, help="Object storage utilities.")

_NIL = UUID(int=0)


@dataclass(frozen=True, slots=True)
class StepResult:
    name: str
    error: str | None  # None when the step passed


def probe_key() -> ObjectKey:
    return ObjectKey(world_id=_NIL, org_id=_NIL, name=f"storage-check/{uuid4().hex}")


async def check_store(store: ObjectStore, key: ObjectKey) -> list[StepResult]:
    """Put `key`, read it back and delete it. Once the put has passed, the delete runs
    whatever the get found, so a failed check leaves nothing behind."""
    data = secrets.token_bytes(32)
    results: list[StepResult] = []

    async def step(name: str, action: Callable[[], Awaitable[bool]]) -> bool:
        try:
            ok = await action()
        except Exception as exc:  # a store failure is this check's answer, not a crash
            results.append(StepResult(name, f"{type(exc).__name__}: {exc}"))
            return False
        results.append(StepResult(name, None if ok else "unexpected result"))
        return ok

    async def put() -> bool:
        await store.put(key, data, content_type="application/octet-stream")
        return True

    async def get() -> bool:
        return await store.get(key) == data

    async def delete() -> bool:
        await store.delete(key)
        return not await store.exists(key)

    if await step("put the probe", put):
        await step("get it back unchanged", get)
        await step("delete it", delete)
    return results


def _location(settings: Settings) -> str:
    match settings.object_store:
        case "localfs":
            return f"localfs at {settings.localfs_root}"
        case "s3":
            return f"s3 bucket {settings.s3_bucket}"


@app.callback()
def storage() -> None:
    """Object storage utilities."""


@app.command()
def check() -> None:
    """Put, get and delete a probe object through the configured store."""
    settings = get_settings()
    key = probe_key()
    typer.echo(f"{_location(settings)}, probe {key.path}")
    results = asyncio.run(check_store(create_object_store(settings), key))
    for result in results:
        detail = f" ({result.error})" if result.error else ""
        typer.echo(f"{'FAIL' if result.error else 'PASS'}  {result.name}{detail}")
    if any(result.error for result in results):
        raise typer.Exit(code=1)
