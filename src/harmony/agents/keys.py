"""ScriptedModel cassette keys: which (org, role, thread, turn, step) each call uses.

A cassette entry matches `CallKey(scenario, org, role, thread, turn, step_idx)`
(`llm/scripted.py`). Keys must be identical in every world of a scenario, so they are
built from slugs and per-world ordinals, never from minted UUIDs. Roles are
`llm.ModelRole`; the plan's "consult" role is `answerer`.

Per role (org / thread / turn / step):
- `internal`: the chat's org / `chat_key(member, n)` / the chat's turn number / the step
  within the turn.
- `representative`: the side's org / `thread_key(...)` / that side's turn number / the
  step within the turn.
- `answerer`: the consulting org / `thread_key(...)` / that side's consult number /
  `answerer_step(draft_rev, step)`.
- `sharing_check`: the consulting org / `thread_key(...)` / that side's consult number /
  draft rev - 1.
- `screener`: the screened org / `search_key(origin)` / the origin's turn number / the
  index of the search in that turn.

Details:
- **Conversations.** `chat:<member slug>:<n>`, where n is the owner's n-th conversation
  in the world (1-based, by `created_at`). Turn n is the n-th `internal_turn` of that
  chat. A turn paused on a card resumes with the same turn number and turn_id, and
  its steps keep counting (`TurnContext.first_step`), so the step after approval is
  `step: 2` when the card was raised at step 1.
- **Threads.** A seeded thread uses its seed slug. Any other thread is
  `<requester slug>.<responder slug>`, plus `.<k>` for the k-th thread (k >= 2, by
  `created_at`) between the same pair in the world. Dots never occur in slugs, so the
  two forms can't collide. Each side counts its own turns from 1; a consult answer
  arrives at the start of that side's next turn.
- **Consults.** The answerer for the k-th consult of a (thread, side) is turn k. It is a
  short tool loop, so each draft rev gets its own block of `ANSWERER_STEPS` steps: a
  redraft never reuses an earlier rev's step. The sharing check of rev r is step r - 1.
- **Searches.** Each `search_network` call in an internal turn screens every candidate
  once: org = the candidate, thread = `search:<chat key>`, turn = that chat turn,
  step = the 0-based index of this `search_network` call among the turn's searches.
  `thread: "*"` lets one entry answer for an org in any search.
- **Checkpoints.** Model calls are checkpointed on `(turn_id, step_idx)`, with
  `step_idx` equal to the cassette step. Turns use the workflow-minted turn_id; screener
  calls use `screen_turn_id(search_id, org_id)`, answerer calls `consult_turn_id`, and
  sharing checks `sharing_turn_id`.
- **Ids in tool inputs.** Seeded rows have the same ids in every world (`seeds.ids`),
  and tools name organizations by slug, so a cassette's tool inputs are literal.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid5

CHAT_PREFIX: Final = "chat"
SEARCH_PREFIX: Final = "search"
# The most steps one draft's answerer tool loop may take.
ANSWERER_STEPS: Final = 8


def chat_key(member_slug: str, n: int) -> str:
    """`chat:alex:1`: the owner's n-th conversation (1-based)."""
    if n < 1:
        raise ValueError(f"conversation ordinal must be >= 1, got {n}")
    return f"{CHAT_PREFIX}:{member_slug}:{n}"


def thread_key(
    requester_slug: str, responder_slug: str, k: int = 1, *, seeded_slug: str | None = None
) -> str:
    """A thread's cassette key: its seed slug, else `henderson.northlake[.k]`."""
    if seeded_slug is not None:
        return seeded_slug
    if k < 1:
        raise ValueError(f"thread ordinal must be >= 1, got {k}")
    base = f"{requester_slug}.{responder_slug}"
    return base if k == 1 else f"{base}.{k}"


def search_key(origin_key: str) -> str:
    """`search:chat:alex:1`: the screener calls of a search run from `origin_key`."""
    return f"{SEARCH_PREFIX}:{origin_key}"


def screen_turn_id(search_id: UUID, org_id: UUID) -> UUID:
    """The checkpoint turn_id of one screener call; a retried search reuses it."""
    return uuid5(search_id, f"screen/{org_id}")


def consult_turn_id(consult_id: UUID) -> UUID:
    """The checkpoint turn_id of a consult's answerer calls (`answerer_step`)."""
    return uuid5(consult_id, "answer")


def sharing_turn_id(consult_id: UUID) -> UUID:
    """The checkpoint turn_id of a consult's sharing checks (step = draft rev - 1)."""
    return uuid5(consult_id, "sharing")


def answerer_step(draft_rev: int, step: int) -> int:
    """The cassette step (and checkpoint step_idx) of `step` in draft rev `draft_rev`'s
    tool loop."""
    if draft_rev < 1 or not 0 <= step < ANSWERER_STEPS:
        raise ValueError(f"no answerer step {step} of draft rev {draft_rev}")
    return (draft_rev - 1) * ANSWERER_STEPS + step


def answerer_steps(draft_rev: int) -> range:
    """Every cassette step (and checkpoint step_idx) of draft rev `draft_rev`'s tool loop."""
    first = answerer_step(draft_rev, 0)
    return range(first, first + ANSWERER_STEPS)


__all__ = [
    "ANSWERER_STEPS",
    "answerer_step",
    "answerer_steps",
    "chat_key",
    "consult_turn_id",
    "screen_turn_id",
    "search_key",
    "sharing_turn_id",
    "thread_key",
]
