"""Object storage (ARCHITECTURE.md §13): the `ObjectStore` interface and its factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harmony.storage.base import ObjectKey, ObjectNotFound, ObjectStore

if TYPE_CHECKING:
    from harmony.config import Settings

__all__ = ["ObjectKey", "ObjectNotFound", "ObjectStore", "create_object_store"]


def create_object_store(settings: Settings) -> ObjectStore:
    """The store OBJECT_STORE selects. boto3 is imported only when S3 is selected."""
    match settings.object_store:
        case "localfs":
            from harmony.storage.localfs import LocalFsObjectStore

            return LocalFsObjectStore(settings.localfs_root)
        case "s3":
            from harmony.storage.s3 import S3ObjectStore

            secret = settings.s3_secret_access_key
            return S3ObjectStore.connect(
                bucket=settings.s3_bucket,
                endpoint_url=settings.s3_endpoint_url,
                region=settings.s3_region,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=secret.get_secret_value() if secret is not None else None,
            )
