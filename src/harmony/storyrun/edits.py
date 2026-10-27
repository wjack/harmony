"""`harmony run --edit ORG:COUNTERPARTY:N:TEXT`: a member of ORG tells its Claude TEXT
(an Edit, EDIT.md) on ORG's thread with COUNTERPARTY once that thread has N messages.
With `storyrun.checks`' `guidance_followed`, it is EDIT.md §9's opt-in live check: does
the side's next message follow the guidance without mentioning it?"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class PlannedEdit:
    org: str
    counterparty: str
    after_messages: int
    text: str
    # Posted with every try, so however often the player retries it is one steer.
    client_key: UUID = field(default_factory=uuid4)

    def describe(self) -> str:
        return f"{self.org}:{self.counterparty} after {self.after_messages} messages"


def parse_edit(value: str, org_slugs: frozenset[str]) -> PlannedEdit:
    """`ORG:COUNTERPARTY:N:TEXT`, with both orgs in the scenario. Raises `ValueError`
    saying what is wrong. TEXT may hold colons."""
    parts = value.split(":", 3)
    if len(parts) != 4:
        raise ValueError(f"{value!r} isn't ORG:COUNTERPARTY:N:TEXT")
    org, counterparty, count, text = parts
    for slug in (org, counterparty):
        if slug not in org_slugs:
            raise ValueError(f"no org {slug!r} (the scenario has {', '.join(sorted(org_slugs))})")
    if org == counterparty:
        raise ValueError(f"{org} can't be its own counterparty")
    if not count.isdigit():
        raise ValueError(f"N is a number of messages, not {count!r}")
    if not text.strip():
        raise ValueError("TEXT is empty")
    return PlannedEdit(org, counterparty, int(count), text.strip())


__all__ = ["PlannedEdit", "parse_edit"]
