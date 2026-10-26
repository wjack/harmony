"""S3ObjectStore against the Compose MinIO, which is built from source (infra/minio).

Opt-in:
    docker compose --profile storage up -d --build minio
    uv run pytest -m minio tests/integration/test_storage_minio.py

Skips when MinIO isn't reachable. Credentials are Compose's local defaults (or the
MINIO_ROOT_USER / MINIO_ROOT_PASSWORD overrides Compose itself reads).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import boto3
import httpx
import pytest

from harmony.storage import ObjectKey, ObjectNotFound, create_object_store
from harmony.storage.s3 import S3ObjectStore
from tests.conftest import settings_without_dotenv

pytestmark = pytest.mark.minio

ENDPOINT = os.environ.get("HARMONY_TEST_MINIO_URL", "http://127.0.0.1:9000")
BUCKET = "harmony"
ACCESS_KEY = os.environ.get("MINIO_ROOT_USER", "harmony")
SECRET_KEY = os.environ.get("MINIO_ROOT_PASSWORD", "harmony-local-only")


@pytest.fixture
async def minio_store() -> AsyncIterator[S3ObjectStore]:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            (await client.get(f"{ENDPOINT}/minio/health/live")).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(
            f"MinIO is not up at {ENDPOINT}: docker compose --profile storage up -d --build minio"
        )
    settings = settings_without_dotenv(
        object_store="s3",
        s3_endpoint_url=ENDPOINT,
        s3_bucket=BUCKET,
        s3_region="us-east-1",
        s3_access_key_id=ACCESS_KEY,
        s3_secret_access_key=SECRET_KEY,
    )
    store = create_object_store(settings)
    assert isinstance(store, S3ObjectStore)
    yield store
    store.close()


def _key(name: str = "documents/quote.pdf") -> ObjectKey:
    # Fresh world and org ids, so concurrent runs never share objects.
    return ObjectKey(world_id=uuid4(), org_id=uuid4(), name=name)


async def test_put_get_delete_round_trip(minio_store: S3ObjectStore) -> None:
    key = _key()
    try:
        await minio_store.put(key, b"%PDF-1.7 quote", content_type="application/pdf")
        assert await minio_store.exists(key)
        assert await minio_store.get(key) == b"%PDF-1.7 quote"

        await minio_store.put(key, b"%PDF-1.7 revised", content_type="application/pdf")
        assert await minio_store.get(key) == b"%PDF-1.7 revised"

        await minio_store.delete(key)
        assert not await minio_store.exists(key)
        with pytest.raises(ObjectNotFound):
            await minio_store.get(key)
        await minio_store.delete(key)
    finally:
        await minio_store.delete(key)


async def test_objects_use_the_world_org_key_and_keep_their_content_type(
    minio_store: S3ObjectStore,
) -> None:
    key = _key("price-list.csv")
    raw = boto3.session.Session().client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )
    try:
        await minio_store.put(key, b"sku,price\n", content_type="text/csv")

        head = raw.head_object(
            Bucket=BUCKET, Key=f"worlds/{key.world_id}/orgs/{key.org_id}/price-list.csv"
        )
        assert head["ContentType"] == "text/csv"
        assert head["ContentLength"] == len(b"sku,price\n")
    finally:
        await minio_store.delete(key)
        raw.close()
