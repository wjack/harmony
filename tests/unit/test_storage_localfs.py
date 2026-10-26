"""LocalFsObjectStore: the ObjectStore contract on a temporary directory."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harmony.storage import ObjectKey, ObjectNotFound, ObjectStore, create_object_store
from harmony.storage.localfs import LocalFsObjectStore
from tests.conftest import settings_without_dotenv


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "objects"


@pytest.fixture
def store(root: Path) -> ObjectStore:
    return LocalFsObjectStore(root)


def _key(
    name: str = "documents/quote.pdf",
    *,
    world_id: UUID | None = None,
    org_id: UUID | None = None,
) -> ObjectKey:
    return ObjectKey(world_id=world_id or uuid4(), org_id=org_id or uuid4(), name=name)


def _files_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


async def test_put_then_get_returns_the_bytes(store: ObjectStore) -> None:
    key = _key()
    await store.put(key, b"%PDF-1.7 quote", content_type="application/pdf")

    assert await store.exists(key)
    assert await store.get(key) == b"%PDF-1.7 quote"


async def test_put_overwrites(store: ObjectStore) -> None:
    key = _key()
    await store.put(key, b"first", content_type="text/plain")
    await store.put(key, b"second", content_type="text/plain")

    assert await store.get(key) == b"second"


async def test_empty_objects_round_trip(store: ObjectStore) -> None:
    key = _key("empty.txt")
    await store.put(key, b"", content_type="text/plain")

    assert await store.exists(key)
    assert await store.get(key) == b""


async def test_missing_object(store: ObjectStore) -> None:
    key = _key()

    assert not await store.exists(key)
    with pytest.raises(ObjectNotFound):
        await store.get(key)


async def test_delete_removes_the_object_and_tolerates_missing_ones(store: ObjectStore) -> None:
    key = _key()
    await store.put(key, b"bytes", content_type="text/plain")

    await store.delete(key)
    await store.delete(key)

    assert not await store.exists(key)
    with pytest.raises(ObjectNotFound):
        await store.get(key)


async def test_objects_live_at_their_key_path_with_no_temp_files_left(
    store: ObjectStore, root: Path
) -> None:
    key = _key("documents/quote.pdf")
    await store.put(key, b"bytes", content_type="application/pdf")
    await store.put(key, b"bytes again", content_type="application/pdf")

    assert _files_under(root) == [
        root / "worlds" / str(key.world_id) / "orgs" / str(key.org_id) / "documents" / "quote.pdf"
    ]


async def test_world_and_org_scope_objects_with_the_same_name(store: ObjectStore) -> None:
    world_id, org_a, org_b = uuid4(), uuid4(), uuid4()
    in_a = _key("price-list.csv", world_id=world_id, org_id=org_a)
    in_b = _key("price-list.csv", world_id=world_id, org_id=org_b)
    in_other_world = _key("price-list.csv", world_id=uuid4(), org_id=org_a)
    await store.put(in_a, b"a", content_type="text/csv")
    await store.put(in_b, b"b", content_type="text/csv")

    assert await store.get(in_a) == b"a"
    assert await store.get(in_b) == b"b"
    assert not await store.exists(in_other_world)


async def test_a_prefix_directory_is_not_an_object(store: ObjectStore) -> None:
    world_id, org_id = uuid4(), uuid4()
    nested = _key("documents/quote.pdf", world_id=world_id, org_id=org_id)
    prefix = _key("documents", world_id=world_id, org_id=org_id)
    under_a_file = _key("documents/quote.pdf/page-1", world_id=world_id, org_id=org_id)
    await store.put(nested, b"bytes", content_type="application/pdf")

    assert not await store.exists(prefix)
    assert not await store.exists(under_a_file)
    with pytest.raises(ObjectNotFound):
        await store.get(prefix)
    with pytest.raises(ObjectNotFound):
        await store.get(under_a_file)
    await store.delete(prefix)
    await store.delete(under_a_file)
    assert await store.get(nested) == b"bytes"


async def test_concurrent_puts_leave_one_whole_object(store: ObjectStore, root: Path) -> None:
    key = _key()
    payloads = [bytes([n]) * 256_000 for n in range(16)]

    await asyncio.gather(
        *(store.put(key, data, content_type="application/octet-stream") for data in payloads)
    )

    assert await store.get(key) in payloads
    assert len(_files_under(root)) == 1


async def test_a_failed_put_keeps_the_old_object_and_no_temp_file(
    store: ObjectStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _key()
    await store.put(key, b"old", content_type="text/plain")

    def failing_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk full"):
        await store.put(key, b"new", content_type="text/plain")

    assert await store.get(key) == b"old"
    assert len(_files_under(root)) == 1


def test_factory_builds_a_local_store_on_the_configured_root(tmp_path: Path) -> None:
    settings = settings_without_dotenv(object_store="localfs", localfs_root=tmp_path)

    store = create_object_store(settings)

    assert isinstance(store, LocalFsObjectStore)
    assert store.root == tmp_path
