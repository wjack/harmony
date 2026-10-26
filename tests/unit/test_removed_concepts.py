"""REVISION_UNSTRUCTURED.md §11, grep level: the concepts the revision removed are gone
from the code, the web client, the migrations, the scenarios and the tests.

Matching is by substring, case-insensitive, so `send_offer`, `ThreadVisibility` and
`approval_limit` count as much as prose does. Directory visibility (who can find an
org) is the one kept concept that shares a word. This file names the concepts, so it is
the only file left out.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).parents[2]
SCANNED: Final = ("src", "web/src", "migrations", "scenarios", "tests")
SKIPPED_DIRS: Final = frozenset({"__pycache__", "node_modules", ".pytest_cache"})

REMOVED: Final = re.compile(
    r"offer|grant|holding|countersign|approval[ _-]?limit|visibility|inbound[ _-]?filter"
    r"|routing[ _-]?correction|subcontract|approver|revo[kc]|sub[ _-]?request|release[ _-]?scope"
    r"|allowlist|default[ _-]?scope|counterparty[ _-]?override",
    re.IGNORECASE,
)
KEPT: Final = re.compile(r"directory[ _-]?visibility", re.IGNORECASE)


def _text_files() -> list[Path]:
    files: list[Path] = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if path.is_file() and not SKIPPED_DIRS.intersection(path.parts):
                files.append(path)
    return files


def test_no_removed_concept_is_referenced() -> None:
    this_file = Path(__file__).resolve()
    hits: list[str] = []
    scanned = 0
    for path in _text_files():
        if path.resolve() == this_file:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # images and other binary fixtures
        scanned += 1
        for number, line in enumerate(text.splitlines(), start=1):
            if REMOVED.search(KEPT.sub("", line)):
                hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert scanned > 300, "the scan found too few files; is ROOT right?"
    assert hits == []


def test_the_pattern_catches_code_names_and_spares_directory_visibility() -> None:
    for caught in (
        "send_offer",
        "ThreadVisibility",
        "approval_limit_usd",
        "Approver",
        "grants",
        "revoke_access",
        "a hard revocation",
        "subrequest_id",
        "sub-requests",
        "release_scope",
        "auto_release_allowlist",
        "DefaultScope",
        "counterparty_override",
    ):
        assert REMOVED.search(KEPT.sub("", caught)), caught
    for kept in ("directory_visibility", "DirectoryVisibility", "directory visibility"):
        assert not REMOVED.search(KEPT.sub("", kept)), kept
