"""S3ObjectStore request shapes and error mapping, offline (botocore's Stubber).

The round trip against a real server is tests/integration/test_storage_minio.py.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from harmony.storage import ObjectKey, ObjectNotFound, create_object_store
from harmony.storage.s3 import S3ObjectStore
from tests.conftest import settings_without_dotenv

BUCKET = "harmony"


@pytest.fixture
def stubbed() -> Iterator[tuple[S3ObjectStore, Stubber]]:
    # The Stubber answers before any request is signed or sent; the endpoint is the
    # discard port so a missed stub fails locally.
    client = boto3.session.Session().client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url="http://127.0.0.1:9",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        yield S3ObjectStore(client, BUCKET), stubber
        stubber.assert_no_pending_responses()


def _key() -> ObjectKey:
    return ObjectKey(world_id=uuid4(), org_id=uuid4(), name="documents/quote.pdf")


async def test_put_writes_the_key_path_with_its_content_type(
    stubbed: tuple[S3ObjectStore, Stubber],
) -> None:
    store, stubber = stubbed
    key = _key()
    stubber.add_response(
        "put_object",
        {},
        {"Bucket": BUCKET, "Key": key.path, "Body": b"pdf", "ContentType": "application/pdf"},
    )

    await store.put(key, b"pdf", content_type="application/pdf")


async def test_get_reads_the_body(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stubber = stubbed
    key = _key()
    stubber.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"pdf"), 3)},
        {"Bucket": BUCKET, "Key": key.path},
    )

    assert await store.get(key) == b"pdf"


async def test_get_maps_no_such_key_to_object_not_found(
    stubbed: tuple[S3ObjectStore, Stubber],
) -> None:
    store, stubber = stubbed
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with pytest.raises(ObjectNotFound):
        await store.get(_key())


async def test_exists_maps_a_bare_404_to_false(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stubber = stubbed
    key = _key()
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    stubber.add_response("head_object", {}, {"Bucket": BUCKET, "Key": key.path})

    assert not await store.exists(key)
    assert await store.exists(key)


async def test_other_errors_propagate(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stubber = stubbed
    stubber.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    stubber.add_client_error("head_object", service_error_code="403", http_status_code=403)

    with pytest.raises(ClientError, match="AccessDenied"):
        await store.get(_key())
    with pytest.raises(ClientError):
        await store.exists(_key())


async def test_delete_sends_the_key_path(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stubber = stubbed
    key = _key()
    stubber.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": key.path})

    await store.delete(key)


def test_factory_builds_an_s3_store_without_touching_the_network() -> None:
    settings = settings_without_dotenv(
        object_store="s3",
        s3_endpoint_url="http://127.0.0.1:9",
        s3_bucket="harmony-test",
        s3_access_key_id="test",
        s3_secret_access_key="test",
    )

    store = create_object_store(settings)

    assert isinstance(store, S3ObjectStore)
    assert store.bucket == "harmony-test"
    store.close()
