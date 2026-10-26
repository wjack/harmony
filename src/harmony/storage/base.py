"""The object store interface. Browsers never reach the store; the API proxies, after
checking world and org access (ARCHITECTURE.md §13).

Implementations (T10): `storage/localfs.py` (unit tests, local dev) and
`storage/s3.py` (MinIO in Compose, CI and Render).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

# Object names are relative paths whose segments start with a letter or digit: no
# empty, `.`, `..` or hidden segments and no leading slash, so a key can never escape
# its world/org prefix.
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ObjectNotFound(KeyError):
    """No object under the key."""


@dataclass(frozen=True, slots=True)
class ObjectKey:
    world_id: UUID
    org_id: UUID
    name: str

    def __post_init__(self) -> None:
        if not all(_SEGMENT.fullmatch(segment) for segment in self.name.split("/")):
            raise ValueError(f"invalid object name: {self.name!r}")

    @property
    def path(self) -> str:
        """The storage path: `worlds/<world_id>/orgs/<org_id>/<name>`."""
        return f"worlds/{self.world_id}/orgs/{self.org_id}/{self.name}"


class ObjectStore(Protocol):
    async def put(self, key: ObjectKey, data: bytes, *, content_type: str) -> None:
        """Create or overwrite the object."""
        ...

    async def get(self, key: ObjectKey) -> bytes:
        """The object's bytes. Raises `ObjectNotFound`."""
        ...

    async def delete(self, key: ObjectKey) -> None:
        """Remove the object. Deleting a missing object is not an error."""
        ...

    async def exists(self, key: ObjectKey) -> bool: ...
