"""`harmony storage check` against the local filesystem store and a failing store."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from harmony import cli_storage
from harmony.cli_storage import check_store, probe_key
from harmony.storage import ObjectKey
from harmony.storage.localfs import LocalFsObjectStore
from tests.conftest import settings_without_dotenv


def test_the_check_passes_against_localfs_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_without_dotenv(object_store="localfs", localfs_root=tmp_path)
    monkeypatch.setattr(cli_storage, "get_settings", lambda: settings)

    result = CliRunner().invoke(cli_storage.app, ["check"])

    assert result.exit_code == 0, result.output
    assert result.output.count("PASS") == 3
    assert "FAIL" not in result.output
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_the_probe_stays_out_of_every_world() -> None:
    key = probe_key()
    assert key.path.startswith(
        "worlds/00000000-0000-0000-0000-000000000000/orgs/00000000-0000-0000-0000-000000000000/"
    )
    assert probe_key() != key


class _CorruptingStore(LocalFsObjectStore):
    """Hands back other bytes than it was given."""

    async def get(self, key: ObjectKey) -> bytes:
        return b"not what was put"


async def test_a_failed_read_still_deletes_the_probe(tmp_path: Path) -> None:
    store = _CorruptingStore(tmp_path)
    key = probe_key()

    results = await check_store(store, key)

    assert [(result.name, result.error is None) for result in results] == [
        ("put the probe", True),
        ("get it back unchanged", False),
        ("delete it", True),
    ]
    assert not await store.exists(key)


class _UnreachableStore(LocalFsObjectStore):
    async def put(self, key: ObjectKey, data: bytes, *, content_type: str) -> None:
        raise ConnectionError("no route to the store")


async def test_a_failed_put_stops_the_check(tmp_path: Path) -> None:
    results = await check_store(_UnreachableStore(tmp_path), probe_key())

    assert len(results) == 1
    assert results[0].error == "ConnectionError: no route to the store"


def test_a_failed_check_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_without_dotenv(object_store="localfs", localfs_root=tmp_path)
    monkeypatch.setattr(cli_storage, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_storage, "create_object_store", lambda _: _UnreachableStore(tmp_path))

    result = CliRunner().invoke(cli_storage.app, ["check"])

    assert result.exit_code == 1
    assert "FAIL  put the probe (ConnectionError: no route to the store)" in result.output
