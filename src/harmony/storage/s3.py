"""Object storage over the S3 API (OBJECT_STORE=s3): MinIO in Compose, CI and on Render.

boto3 is synchronous, so each call runs in a worker thread. A botocore client is
thread-safe once built, so one client (from its own boto3 session; the default session
isn't safe to share) serves the whole process. The bucket itself is created by the
MinIO image's `infra/minio/init.sh`, not here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from harmony.storage.base import ObjectKey, ObjectNotFound

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

# HEAD responses carry no error body, so a missing key shows up as a bare "404".
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

_CLIENT_CONFIG = Config(
    signature_version="s3v4",
    # MinIO serves buckets on the endpoint's path, not as DNS subdomains.
    s3={"addressing_style": "path"},
    retries={"mode": "standard", "max_attempts": 3},
    connect_timeout=5,
    read_timeout=30,
    # Matches asyncio's default executor (at most 32 threads), so concurrent calls
    # don't churn connections when the pool's default of 10 runs out.
    max_pool_connections=32,
)


class S3ObjectStore:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def connect(
        cls,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key_id: str | None,
        secret_access_key: str | None,
    ) -> S3ObjectStore:
        """A store on `bucket`. Unset credentials fall back to boto3's default chain.

        Makes no network call: a wrong endpoint or credentials surface on first use.
        """
        # `client` overloads every AWS service; only the s3 stubs are installed, so strict
        # pyright sees the other overloads as unknown. The s3 overload is fully typed.
        client: S3Client = boto3.session.Session().client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=_CLIENT_CONFIG,
        )
        return cls(client, bucket)

    @property
    def bucket(self) -> str:
        return self._bucket

    async def put(self, key: ObjectKey, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key.path,
            Body=data,
            ContentType=content_type,
        )

    async def get(self, key: ObjectKey) -> bytes:
        return await asyncio.to_thread(self._get, key)

    async def delete(self, key: ObjectKey) -> None:
        # S3 answers 204 whether or not the key existed.
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key.path)

    async def exists(self, key: ObjectKey) -> bool:
        return await asyncio.to_thread(self._exists, key)

    def close(self) -> None:
        """Close the client's connection pool (process shutdown)."""
        self._client.close()

    def _get(self, key: ObjectKey) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key.path)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFound(key.path) from exc
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def _exists(self, key: ObjectKey) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key.path)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return False
            raise
        return True


def _error_code(exc: ClientError) -> str | None:
    return exc.response.get("Error", {}).get("Code")
