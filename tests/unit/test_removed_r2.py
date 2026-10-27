"""FEEDBACK_R2_PLAN.md §3 step 6: what round 2 removed stays gone from the code, the web
client, the migrations, the scenarios and the tests.

Matching is by exact substring. The route tests that assert a removal
(`tests/integration/test_e4_routes.py`: `/redraft` answers 405, `revise` 422) name what
they check, so they are left out, as is this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).parents[2]
SCANNED: Final = ("src", "web/src", "migrations", "scenarios", "tests")
SKIPPED_DIRS: Final = frozenset({"__pycache__", "node_modules", ".pytest_cache"})
LEFT_OUT: Final = frozenset(
    {Path(__file__).resolve(), (ROOT / "tests/integration/test_e4_routes.py").resolve()}
)

REMOVED: Final = (
    # Redraft and send back, superseded by Edit (EDIT.md).
    "/redraft",
    '"revise"',
    "redraft_failed",
    "RedraftFailed",
    "useRedraftEscalation",
    "quote_note",
    "retry_steer",
    # No Claude reads comments (E3).
    "_notes_text",
    "Internal notes on this conversation",
    # The clamps, the docked outreach card and its modes (decision 8, E5).
    "ClampedText",
    "MESSAGE_LINES",
    "EDIT_ROWS",
    "ExpandingTextarea",
    "OutreachApproval",
    "AskUserCard",
    # The request view's retired parts (E5, decision 11).
    "Why it came to you",
    "Claude's reasoning",
    "asked the team",
    "Restore Claude's version",
    "Draft 2 of 2",
    "View more",
)
# Expired is internal: the web says Closed, with a reason (REOPEN.md §2.1).
REMOVED_IN_WEB: Final = ('"Expired"',)


def _text_files() -> list[Path]:
    files: list[Path] = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if (
                path.is_file()
                and not SKIPPED_DIRS.intersection(path.parts)
                and path.resolve() not in LEFT_OUT
            ):
                files.append(path)
    return files


def test_nothing_round_2_removed_is_referenced() -> None:
    hits: list[str] = []
    scanned = 0
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # images and other binary fixtures
        scanned += 1
        removed = REMOVED + REMOVED_IN_WEB if path.is_relative_to(ROOT / "web") else REMOVED
        for number, line in enumerate(text.splitlines(), start=1):
            hits.extend(
                f"{path.relative_to(ROOT)}:{number}: {term}" for term in removed if term in line
            )
    assert scanned > 300, "the scan found too few files; is ROOT right?"
    assert hits == []
