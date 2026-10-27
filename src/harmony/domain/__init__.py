"""Domain services: one module per aggregate (P1_P2_PLAN.md "Domain services").

Every writing function takes a `UnitOfWork` (one world, one transaction) and queues the
`entity.snapshot` events its changes need, with the scopes below; the caller commits.
Reads take an `AsyncSession`. IDs are minted by the caller (`ids.side_effect_id` in
activities, `workflow.uuid4()` in workflows, uuid4 in the API), never here.

Snapshot scopes (§8, §10): a thread's rows go out per party org with
`EventScope.thread(org, thread)`, and org-private rows (consults, escalations, drafts,
notes) only to the owning org's thread scope; chat rows (conversation, card) go to the
owner with `EventScope.member(owner, org)`; requests to the requester org's thread
scopes plus the origin conversation's owner; settings, policy and trusted
organizations to `EventScope.org(org)`. Entity type names are
`events.schemas.EntityType`: the `Snapshotable` models (`UnitOfWork.add_snapshot`) plus
the append-only `thread_message`, `internal_note` and `escalation_draft` (version 1,
through `UnitOfWork.add_event`). Chat messages are never snapshotted on their own;
their conversation's snapshot tells clients to refetch.

Modules: `conversations`, `cards`, `requests`, `threads`, `consults`, `escalations`,
`routing`, `notes`, `steers` (members' instructions to their side's Claude and the
guidance written from them, org-private like notes), `settings`, `agent_turns` (the
per-turn cost and latency rows, with no events); `clock` is the world clock; `errors`
holds the domain errors.
"""
