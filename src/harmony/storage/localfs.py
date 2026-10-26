"""Object storage on the local filesystem (OBJECT_STORE=localfs): unit tests and local dev.

Objects live at `<root>/<ObjectKey.path>`, the same layout as the S3 keys. A put writes
a temp file in the target directory and renames it into place, so readers never see a
partial object and concurrent puts to one key leave one whole winner. Temp names start
with a dot, which no object name segment can, so they never collide with objects.

Unlike S3, a filesystem can't hold objects `a` and `a/b` at once: the second put raises
`OSError`. Harmony's object names never nest that way.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from harmony.storage.base import ObjectKey, ObjectNotFound


class LocalFsObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    async def put(self, key: ObjectKey, data: bytes, *, content_type: str) -> None:
        # A filesystem keeps no content type; the caller's row (documents.mime) has it.
        await asyncio.to_thread(_write_atomically, self._path(key), data)

    async def get(self, key: ObjectKey) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise ObjectNotFound(key.path) from exc

    async def delete(self, key: ObjectKey) -> None:
        await asyncio.to_thread(_delete_file, self._path(key))

    async def exists(self, key: ObjectKey) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    def _path(self, key: ObjectKey) -> Path:
        return self._root / key.path


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _delete_file(path: Path) -> None:
    # A directory here is the prefix of other objects, not an object: leave it alone.
    if path.is_file():
        path.unlink(missing_ok=True)
