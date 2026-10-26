"""Deterministic seed IDs (ARCHITECTURE.md §4).

A seeded row's ID is `uuid5(SCENARIO_NS, "<scenario>/<kind>/<natural key>")`, so the
same scenario row has the same ID in every world. Composite `(world_id, id)` keys keep
the worlds apart, and a recording made in one world replays into another unchanged.

Natural keys are slugs, joined with `/` for rows whose slug is unique only within a
parent: `knowledge_item` keys are `<org>/<slug>`, `thread_message` keys are
`<thread>/<n>`.
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

# Fixed forever: changing it would change every seeded ID.
SCENARIO_NS: Final = uuid5(NAMESPACE_URL, "https://harmony.invalid/scenarios")

SeedKind = Literal[
    "org",
    "member",
    "community",
    "knowledge_item",
    "network_request",
    "thread",
    "thread_message",
]


def seed_id(scenario: str, kind: SeedKind, key: str) -> UUID:
    """The ID of scenario row `kind`/`key`; identical in every world of `scenario`."""
    return uuid5(SCENARIO_NS, f"{scenario}/{kind}/{key}")
