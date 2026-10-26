# P1–P2 shared contract (unstructured revision)

This is the contract every rework task codes against after [REVISION_UNSTRUCTURED.md](REVISION_UNSTRUCTURED.md) ("R§n" below). It lists the shapes, names and signatures that cross task boundaries, gives each one line of meaning, and says who owns which file. The code is the source of truth, and everything below exists in the tree: the rework tasks have landed, and the engine runs end to end (`tests/e2e/test_story_scripted.py` plays R§10 against Temporal). Plain "§n" refers to [ARCHITECTURE.md](ARCHITECTURE.md), which R§ overrides wherever they disagree.

The `Owner:` lines in module docstrings date from the P1 build (E1–E5, U1–U4) and are historical. The [file ownership](#file-ownership) table below is what counts.

## Ground rules

- **Don't impose structure on the agents (R§1).** Representatives talk in free text. Code enforces only plumbing: identity, turn-taking, the firewall, and human review where the org's mode asks for it. If a task thinks it needs a structured field or a code rule that R§ doesn't name, it doesn't add one. It raises the question with the coordinator.
- **Removed concepts are gone, not hidden.** That means offers, signing authority, approval limits, the approver role, holding messages, grants and release scopes, knowledge visibility, structured profiles and briefs, inbound filters, routing learning and `subcontract`. Delete the code, the tests, the fixtures and the UI for them; don't stub them or leave them unused. The grep test in R§11 (`tests/unit/test_removed_concepts.py`) scans `src/`, `web/src/`, `migrations/`, `scenarios/` and `tests/` by case-insensitive substring, so a kept meaning that shares a word is reworded (thread access, not "grant"); only `directory_visibility` is exempt.
- **Change a signature only with its owner.** A task that needs a contract change asks the coordinator. It never edits another task's file.
- **Models are frozen Pydantic v2** (`extra="forbid"` on inputs and payloads). There are no money types: prices and terms are message text.
- **IDs are minted by the caller:**
  - `ids.side_effect_id(turn_id, step_idx, tool_use_id)` in activities;
  - `workflow.uuid4()` in workflows;
  - `uuid4()` in the API.

  Domain functions never mint IDs, and every write is idempotent on its ID.
- **The API never calls the model.** It starts, signals or updates workflows. Model activities run on `harmony-model`.
- **`web/src/api/schema.d.ts` is generated** by `make gen-api`. Nobody hand-edits it. Whoever changes an API or event model runs `make gen-api` in the same change.
- **Local dev databases are recreated.** `0001_initial` was rewritten in place (R§8), so `make migrate` doesn't upgrade an existing dev database. Drop the `harmony` database and recreate it.

## Database

Everything is in `migrations/versions/0001_initial.py`. On a fresh database `make migrate` applies it, and `alembic check` reports no drift. No task writes a migration. A task that needs a column asks the coordinator, who folds it into `0001`.

**Dropped** (R§8):
- Tables: `offers`, `grants` and `routing_decisions`.
- `members.approval_limit_minor`.
- `orgs.currency` and `orgs.signing_key`.
- `rep_policy`: counterparty overrides, inbound filters, holding template and delay, the auto-release allowlist and the auto-countersign limit.
- `rep_settings.card` and `default_scope`.
- `knowledge_items.visibility` and `documents.visibility`.
- `escalations.scope` and `offer_id`.
- `thread_messages.offer_id`.
- `threads.holding_sent_at` and the sub-request columns.
- `network_requests`: the structured brief, `authority`, `screening_summary` and the parent thread.
- `screenings`: `must_haves`, `questions` and `self_reported`.
- `rep_sessions.exposed`.

**Changed or added:**
- `rep_settings(org_id, version, public_summary, instructions, created_by_member_id)`: immutable and versioned. `orgs.current_settings_version` points at the current row, and a representative freezes the version it started with.
- `rep_policy(org_id, autonomy, sharing_description, directory_visibility, routing_prompt, default_assignee_id, version)`: read live at every decision.
- `trusted_organizations(org_id, counterparty_org_id, autonomy?, sharing_description, updated_by_member_id, updated_at, created_at, version)`.
  - PK `(world_id, org_id, counterparty_org_id)`, with CHECK `not_self` (`org_id <> counterparty_org_id`).
  - `autonomy` NULL means "Same as default".
  - Snapshot entity `trusted_organization`. Its id is `uuid5(org_id, str(counterparty_org_id))`.
- `members(…, role, title, profile)`: `role` is `admin` | `member` (CHECK), and `profile` is prose that the answerer reads when routing.
- `network_requests(…, brief text, recipients jsonb, status, card_id, version)`.
- `screenings(search_id, org_id, requester_org_id, fit, reason)`.
- `rep_sessions(thread_id, side, system_text, tools_text, model, close_summary?)`: `close_summary` is the side's own summary and is never sent to the counterparty.
- `consults`:
  - the question, `status` and `dedupe_key`;
  - the draft fields: `answer`, `needs_human`, `needs_human_reason`, `route` and `rationale`;
  - `answer_turn_id`, which keys the answerer's `model_calls`: its tool calls are what it looked at;
  - `sharing_check` (`{within_sharing, reason}`, nullable);
  - `released_text`, `released_by` (`auto` | `member`), `released_by_member_id` and `released_at`.
- `escalations(…, kind, consult_id?, status, dedupe_key, assignees, priority, current_draft_rev, resolution, resolved_by_member_id, resolved_at)`.
- `escalation_drafts(escalation_id, draft_rev, answer, route, rationale, turn_id)`: a consult's draft revision, or the held message of an `outbound_review`.
- `agent_turns(id, role, org_id, thread_id?, conversation_id?, side?, cost_usd, latency_ms, completed_at)` is unchanged from P0 and now written (`domain/agent_turns.record`): one row per finished internal, representative or screener turn, and one per draft rev of a consult's answerer and sharing check.

**Literals** (`db/models/choices.py`):
- `Autonomy` = `ask` | `auto` | `skip`.
- `ThreadState` = `active` | `awaiting_principal` | `closed` | `expired`.
- `ThreadMessageKind` = `message` | `close`.
- `Provenance` = `agent` | `agent_approved` | `human`.
- `ConsultStatus` = `answering` | `released` | `needs_human` | `declined`.
- `EscalationKind` = `consult` | `outbound_review`.
- `ReleasedBy` = `auto` | `member`.
- `Fit` = `strong` | `possible` | `no` | `unknown`.
- `CardKind` = `outreach`.
- `ModelRole` adds `sharing_check`.
- `MemberRole` = `admin` | `member` (R§12 drops `approver`).

## Events (`src/harmony/events/schemas.py`)

- `EntityType` / `ENTITY_TYPES`: every `entity.snapshot` type.
  - The `Snapshotable` models (`db.models.SNAPSHOT_ENTITIES`): `org`, `member`, `rep_settings`, `rep_policy`, `trusted_organization`, `conversation`, `card`, `network_request`, `thread`, `consult`, `escalation`, `knowledge_item` and `document`.
  - The append-only types (`APPEND_ONLY_ENTITY_TYPES`): `thread_message`, `internal_note` and `escalation_draft`. These are snapshotted once at version 1.
- `ThreadAccessChanged.added`: true when a member gained an access reason, false when they lost one. It was `granted`.
- `DemoNeedsHuman` (`demo.needs_human`, `{org_slug, member_slug, entity_id}`) and `StreamChatDelta` (`stream.chat_delta`) are unchanged. The web client types the delta from the generated schema (`EventOf<"stream.chat_delta">`); the realtime dispatcher leaves it to the chat view, which appends it to the streaming turn.
- **A deleted row** gets one last snapshot at `version + 1` whose `data` is the row's keys plus `deleted: true`. Removing a trusted organization is the one case.

Snapshot scopes:
- Thread rows go to each party org's thread scope.
- Org-private rows (consults, escalations, drafts, notes) go to the owning org's thread scope.
- Settings, policy, trusted organizations and member profile edits go to `EventScope.org(org)`.
- Chat rows go to `EventScope.member(owner, org)`.

## Cassettes (`src/harmony/agents/keys.py`)

A cassette entry matches `CallKey(scenario, org, role, thread, turn, step_idx)`. Keys come from slugs and ordinals, never minted UUIDs. Seeded rows have the same `uuid5` ids in every world, and tools name orgs by slug, so tool inputs in cassettes are literal. The `{{label}}` reference machinery is gone.

| Role | org | thread | turn | step |
|---|---|---|---|---|
| `internal` | the chat's org | `chat_key(member, n)` | the chat's turn number | step within the turn; a turn resumed after a card keeps counting |
| `representative` | that side's org | `thread_key(...)` | that side's own turn number | step within the turn |
| `answerer` | the consulting org | the thread key | the k-th consult of (thread, side) | `answerer_step(draft_rev, step)` = `(draft_rev - 1) * ANSWERER_STEPS + step` (`ANSWERER_STEPS` = 8): one block of steps per draft rev |
| `sharing_check` | the consulting org | the thread key | the k-th consult of (thread, side) | `draft_rev - 1` |
| `screener` | the screened org | `search_key(chat key)` (`*` matches any search) | the origin chat turn | the search's index in that turn |

Checkpoint turn ids:
- turns use the workflow-minted `turn_id`;
- screener calls use `screen_turn_id(search_id, org_id)`;
- answerer calls use `consult_turn_id(consult_id)`;
- sharing checks use `sharing_turn_id(consult_id)`.

`step_idx` equals the cassette step.

## Agent layer (`src/harmony/agents/`)

### Shared models (`agents/models.py`)

- **Briefs and titles.**
  - `Brief`: free text, 1..`BRIEF_MAX_CHARS` (8000). It is the requester representative's whole mandate.
  - `brief_title(brief)`: the first line, at most `BRIEF_TITLE_CHARS` (80). It titles requests, threads and inbox items.
  - `first_line(text, limit)`: the helper behind it.
- **Status literals.**
  - `RequestStatus` = `active` | `closed`.
  - `EscalationStatus` = `open` | `resolved` | `cancelled`.
  - `ParticipantStatus` = `screened` | `messaged` | `replied` | `needs_human` | `closed` (R§9; `expired` shows as `closed`).
- **People, routing and context.**
  - `MemberRef(id, slug, name)`.
  - `Route(assignees: member slugs, reason)`.
  - `ActivityLine(tool, label, detail?)`: one thing internal Claude did with a tool, as the reviewer reads it.
  - `ContextPart(kind, label, text)`, where `ContextPartKind` = `public_summary` | `instructions` | `sharing` | `brief` | `counterparty`. These are the parts of a representative's frozen prompt (R§2).
- **Escalations.**
  - `EscalationAction` = `approve` | `decline` | `take_over`. Approve with `text` is "Edit". Redraft and reassign are separate Updates.
  - `EscalationResolution(escalation_id, action, text?, draft_rev?, member_id, org_id)`: the `resolve_escalation` Update's argument and the stored resolution. `org_id` is the acting member's org, so the validator can refuse another org's member.
- **Cards.**
  - `ScreenedCounts`.
  - `OutreachRecipient(org_id, fit?, reason)`.
  - `OutreachCardPayload(kind="outreach", brief, recipients, screened?, search_ids)`. `CardPayload` = `OutreachCardPayload`.
  - `OutreachEdits(recipients?: org ids, brief?)`.
  - `CardResolution(card_id, decision: approve | reject, member_id, edits?, feedback?)`.
- **Chat items.** The `ChatItem` union (on `type`): `user_text`, `assistant_text`, `thinking_summary`, `tool_activity`, `outreach_card`, `request_status` and `inline_question`. There are no compare-offers or accept cards.

### Consult (`agents/consult.py` models) and release (`agents/release.py`)

- `ConsultDraft(answer, needs_human, needs_human_reason?, route?, rationale)`: the answerer's structured output (`CONSULT_OUTPUT_SCHEMA`). There are no sources, offer or suggested actions; what the answerer looked at is recorded from its tool calls.
- `SharingCheck(within_sharing, reason)`: the sharing check's output (`SHARING_CHECK_OUTPUT_SCHEMA`), stored as `consults.sharing_check`. It sees only the applicable sharing descriptions, the question and the draft.
- Answerer outcomes:
  - `DraftReady(draft, turn_id, step_idx)` or `AnswerFailed(reason: refusal | invalid, detail)`;
  - then `Answered(consult_id, result_text)` or `Escalated(consult_id, escalation_id, draft_rev?, assignee_ids, reason)`.
- `release.mode_for(policy, trusted?) -> Autonomy`: the counterparty's own mode if set, else the default.
- `context.sharing_parts(policy, trusted?, counterparty_name) -> list[str]`: the default description, plus the trusted organization's extra one if there is one, each quoted as a prompt part.
- `release.needs_sharing_check(mode, draft)`: only *Auto* runs the check, and only on a draft that doesn't need a human.
- `release.decide(mode, draft, sharing?) -> Release(answer) | NeedsHuman(reason, detail)`. It applies the R§4.4 table in this order: `needs_human` first, then *Auto* (a failed check needs a human), then *Ask* (always a person). *Auto* requires the verdict. A check that couldn't run is stored as `SharingCheck(within_sharing=False, reason=...)` and passed in.
- `release.why_needed(consult) -> str`: "Why this needs you", read back from the consult row.
- The calls, in `agents/consult.py`:
  - `answer(sessionmaker, model, *, world_id, consult_id, draft_rev=1, instruction?, attempt, model_override?, cassette_thread?, consult_turn?) -> DraftReady | AnswerFailed`: a tool loop of at most `ANSWERER_STEPS` calls over `ANSWERER_TOOLS`, ending in the structured draft (R§4.1). Its prompt (`answer_inputs`) is read live: the sharing descriptions, instructions, member profiles, routing prompt, internal notes (and a redraft's instruction), the thread quoted, and the question. It holds no records: the answerer looks them up.
  - `check_sharing(sessionmaker, model, *, world_id, consult_id, draft, draft_rev=1, attempt, model_override?, cassette_thread?, consult_turn?) -> SharingCheck`: one call that sees only the applicable sharing descriptions, the question and the draft. A refusal or an invalid verdict returns a failed check whose reason says it couldn't run.
  - `settle(uow, *, consult_id, outcome, draft_rev=1, sharing?) -> Answered | Escalated`: records the draft (`consults.record_draft`) and verdict (`consults.record_sharing_check`), applies `release.decide` with live policy, and releases (`consults.release`, `released_by="auto"`) or opens the consult's one escalation (`escalation_id_for(consult_id)` = `uuid5(consult_id, "escalation")`) and routes it. Idempotent; a redraft only adds its draft rev to the open escalation.
  - `result_text(action, text) -> str`: the `consult` tool result for any settlement. An approval is `"Your side answered:\n"` plus the text quoted (`context.quote_released`), and that string is what `consults.released_text` stores; a decline quotes what the side may say, if anything.
  - `settled_result(consult) -> str | None`: the result a settled consult owes, for a retried settle. A settled consult is never reused: the same question asked again is a new consult (`consults.open` dedupes only open ones, storing a repeat under `<dedupe_key>#<consult_id>`), so it is answered from the records and policy as they are then.

### Runtime (`agents/runtime.py`)

- `run_turn(ctx, executor, model, *, transcript, on_text=None) -> TurnOutcome`: unchanged. At most `MAX_STEPS` (8).
- `PerformativeName` = `send_message` | `consult` | `close`: the representative tools that can stop a turn before the model does. `consult` stops it until the answer is released, `close` stops it for good, and `send_message` stops it only when *Ask* holds the message. Otherwise a turn ends when the model stops calling tools (`end_turn`).
- `TurnStop` = `performative` | `end_turn` | `awaiting_card` | `max_steps` | `refusal`.
- The `TurnEffect` union (on `kind`):
  - `message_sent(message_id, message_kind)`;
  - `consult_opened(consult_id, deduplicated)`;
  - `message_held(escalation_id, message_kind)`: an *Ask* `outbound_review`;
  - `card_raised(card_id, card_kind, tool_use_id)`;
  - `search_ran(search_id, screened)`.

  The offer, acceptance and sub-request effects are gone.
- `ToolExecution(result, is_error, ends_turn, owed_result, effect)` and `OwedResult(tool_use_id, content?, is_error)`: unchanged.

### Tools (`agents/tools/`)

- **Representative** (`REP_TOOLS`, inputs `REP_INPUTS`): `send_message{text}`, `consult{question}` and `close{summary}`. They are identical for every representative. A turn may send several messages (R§5).
  - Under *Ask*, `send_message` is held as an `outbound_review` escalation whose draft is the message. Its id is `uuid5(message_id, "outbound_review")`, where `message_id` is the call's side-effect id, and its dedupe key is `outbound:{message_id}`. The turn stops, and the result is owed once a person decides. A `close` is never held: no words cross but its empty marker, and the summary stays with the closer's side.
  - `close` stores its side's summary (`threads.store_close_summary`) and appends a `close` thread message with an empty body, so the summary never reaches the counterparty.
  - A bare text reply maps to `send_message` (`TEXT_REPLY_ID`).
  - The mode is read live, under the thread's row lock, at each `send_message`. A call whose side effect an earlier attempt of the turn applied (a message, a held item or a consult under its side-effect id) returns that effect again instead of deciding anew, so a retried turn never sends a held message or holds a sent one.
- **Internal Claude** (`INTERNAL_TOOLS`, inputs `INTERNAL_INPUTS`):
  - `search_org_knowledge{query, limit?}`: hits are named `kn:<uuid>` (knowledge items) and `doc:<uuid>` (documents);
  - `read_document{id}`: takes a `kn:` or `doc:` id, or a bare id;
  - `search_network{need}`;
  - `send_network_request{brief, recipients: [{org: slug, reason}]}`, which raises the outreach card. Each recipient carries its best fit across all of this chat's searches, and the card's screened counts count each org once;
  - `get_request_status{request_id?}`.
- **Answerer** (`ANSWERER_TOOLS`): `search_org_knowledge` and `read_document`, over its own org's records only.

### Context (`agents/context.py`)

- `build_rep_context(session, *, world_id, thread_id, side, model) -> RepContext` renders only the R§2 parts: the counterparty, the public summary, the instructions, the sharing descriptions that apply (`sharing_parts(policy, trusted?, *, counterparty_name)`) and, on the requester side, the brief. Each part is in its own tag with `<` escaped.
- `context_parts(system_text) -> list[ContextPart]`: reads the parts back from the frozen bytes, for "What our representative saw".
- `delivered_notices(...) -> list[DeliveredNotice]` and `received_results(...) -> set[str]`: the notices and result texts the side's stored transcript carries, for the same drawer.
- `NoticeKind` = `settings` | `mode` | `sharing` | `world_date` | `closed`. These are code-written notices.
  - `settings`: the org changed its prompt settings, and the thread keeps its frozen version (`domain/settings.SETTINGS_NOTICE`).
  - `mode`: the mode for this counterparty changed (`domain/settings.mode_notice`).
  - `sharing`: the sharing descriptions that apply changed. The text is the new descriptions (`domain/settings.sharing_notice`).
  - `closed`: the counterparty closed, and this side may write its summary in one last turn (`context.closed_notice(counterparty_name)`).
- `queue_notice`, `flush_notices`, `wrap_counterparty`, `quote_released`, `world_date_notice`, `build_internal_context`, `load_rep_session` and `freeze_rep_session` keep their signatures.
- `RepContext` has no record of what was exposed: records reach a representative only inside released answers, which arrive as tool results.

### Discovery (`agents/discovery.py`)

- `DiscoveryStrategy.search(need: str, *, world_id, requester_org_id, search_id, origin) -> Shortlist`.
- Models:
  - `Verdict(fit, reason)` (`SCREENER_OUTPUT_SCHEMA`);
  - `ShortlistEntry(org_id, org_slug, org_name, rank, fit, reason, exploration)`;
  - `NearMiss(org_id, org_slug, org_name, reason)`;
  - `Shortlist(search_id, screened, entries, near_misses, note)`;
  - `ReviewStats(reviews, average_rating)`.
- `AgentScreen` screens each org on its public summary, with no inbound filters, regions or must-haves. Each verdict is stored as a `screenings` row, with its `agent_turns` row (`screen_turn_id`, costed to the requester's org), in one unit of work.

## Domain services (`src/harmony/domain/`)

Writers take a `UnitOfWork` and queue their snapshots. Readers take an `AsyncSession`.

Errors (`domain/errors.py`):
- `CardNotPending` (409);
- `EscalationNotOpen` (409, with `resolved_by`);
- `ConsultNotOpen` (409);
- `NotInOrg(member_id, org_id)` (403);
- `NotAdmin` (403);
- `InvalidRoute` (422);
- `SettingsConflict` (409): a stale settings version, or trusting an organization twice.

Activities turn them into non-retryable `ApplicationError`s typed by class name.

- **`requests.py`.**
  - `create_from_card(uow, card, resolution, *, request_id)`: takes the brief from `edits.brief`, else the card's.
  - `set_status`, `recipients_of`, `threads_of` and `request_status`.
  - `status(session, request, *, waiting_of=None) -> RequestStatusView`. A recipient the card proposed but the member left out (no thread) is listed as `screened`, with its fit and reason from the card.
  - `participant(thread, *, needs_human, replied) -> ParticipantStatus`.
  - `RequestStatusView(id, title, status, brief, conversation_id, created_at, screened, recipients, version)`.
  - `RecipientStatus(org, fit, reason, participant, thread_id, thread_state, waiting_on, latest_line, close_summary, last_activity_at)`. `latest_line` is the thread's newest message (either side) after its sender's name, so internal Claude and the member can tell who said it. `close_summary` is the requester side's own summary.
- **`threads.py`.**
  - `open`, `set_state`, `parties`, `side_of`, `org_of`, `other`, `cassette_key` and `locked`.
  - `add_access(uow, thread, *, member_id, org_id, reason="routed", shared_by_member_id?, question?) -> bool`: one access reason for one member, idempotent, announced with `thread.access_changed`.
  - `append_message(uow, thread_id, *, message_id, from_org_id, kind, body, provenance, turn_id?, author_member_id?, approved_by_member_id?)`: `turn_count` counts turns, not messages.
  - `store_close_summary(uow, thread_id, side, summary)`: the side's own summary on its `rep_sessions` row. The first write wins; a side with no session raises `ValueError`.
  - `TERMINAL_STATES` = `closed`, `expired`.
  - `MAX_TURNS` = 16.
- **`consults.py`.**
  - `dedupe_key`, `open`, `mark_needs_human`, `decline`, `get`, `for_thread` and `ordinal`.
  - `record_draft(uow, consult_id, draft?, *, answer_turn_id)` and `record_sharing_check(uow, consult_id, check)`: the draft fields (or that there was no draft) and the verdict the release decision was made on. The first write wins.
  - `release(uow, consult_id, *, text, released_by, member_id?)`: `text` is the tool result (`consult.result_text`). A member release names the member.
  - `activity(session, *, turn_id, steps?) -> list[ActivityLine]`: what the answerer looked at, from its `model_calls`. Every rev shares the checkpoint turn, so `steps=keys.answerer_steps(rev)` keeps one rev's calls; the workspace lists each draft's own.
- **`escalations.py`.**
  - `open(uow, *, escalation_id, org_id, kind, dedupe_key, thread_id?, side?, consult_id?, priority=0) -> (escalation, created)`.
  - `add_draft`, `assign`, `resolve`, `cancel`, `set_status`, `drafts`, `current_draft`, `route_of`, `held_message_id`, `assignee_ids` and `scope_of`.
  - `reassign(uow, escalation_id, *, to_member_ids, member_id)`: no reason, nothing recorded for learning.
- **`routing.py`.** `apply_route(uow, escalation, route?) -> list[member_id]`: validates slugs against members and falls back to the default assignee. Nothing is recorded for learning; the draft keeps the route, which is how a reviewer tells an item Claude routed from one that fell back.
- **`settings.py`.** The Customize page's writes (R§3). Each is admin-only (`NotAdmin`), takes the org's `rep_policy` row lock, and checks a version (`SettingsConflict`).
  - `update(uow, *, org_id, member_id, policy_version, edit: SettingsEdit)`. `SettingsEdit` has optional `public_summary`, `instructions`, `sharing_description`, `autonomy`, `directory_visibility`, `routing_prompt`, `default_assignee_id` and `member_profiles: {member id: profile}`, and only the fields set change. Prose writes the next `rep_settings` version (live threads get a `settings` notice). Policy fields change `rep_policy` in place and bump its version, and a live thread whose mode or sharing descriptions changed gets a `mode` or `sharing` notice. Profiles change `members.profile` (an `org`-scope `member` snapshot each) with no notice and no version bump. A null for a required field, or a default assignee or profile for someone outside the org, raises `ValueError`.
  - `add_trusted`, `update_trusted(…, version, edit: TrustedEdit)` and `remove_trusted(…, version)`, with the same notices. An unknown trusted organization raises `LookupError`.
  - `trusted_organizations(session, world_id, org_id)`, `sharing_notice(parts)`, `mode_notice(mode)` and `SETTINGS_NOTICE`.
- **`agent_turns.py`.** `record(uow, *, turn_id, role, org_id, latency_ms, thread_id?, conversation_id?, side?, calls_turn_id?, steps?)` upserts the turn's row, costed from its checkpointed `model_calls` (within `steps` when given), so a retry rewrites the same total. `rev_turn_id(checkpoint_turn_id, draft_rev)` = `uuid5(checkpoint, f"rev/{draft_rev}")` keys one draft rev of a consult's answerer or sharing check.
- **`conversations.py`.** Unchanged, except that `tool_label(name, tool_input) -> (label, detail?)` is public and `chat_items` builds only the R§9 items.
- **`cards.py`, `notes.py` and `clock.py`:** unchanged.

## Temporal

Workflow IDs (`payloads.py`):
- `conversation_workflow_id` → `w:{world}:conv:{id}`;
- `request_workflow_id` → `w:{world}:req:{id}`;
- `thread_workflow_id` → `w:{world}:thread:{id}`.

Update validators reject with an `ApplicationError` of one of these types:
- `CARD_NOT_PENDING_ERROR` (409);
- `ESCALATION_NOT_OPEN_ERROR` (409);
- `NOT_IN_ORG_ERROR` (403);
- `INVALID_RESOLUTION_ERROR` (422);
- `NOT_FOUND_ERROR` (404).

`payloads.update_failure(ActivityError)` re-raises an activity's typed cause, so the API sees the same types.

### `ConversationWorkflow` (`workflows/conversation.py`)

- `run(ConversationInput(world_id, conversation_id, org_id, owner_member_id, cassette_thread, carried, turns_per_run)) -> ConversationResult(turns)`.
- Signal `member_message(MemberMessage(message_id, member_id, text, sent_at))`. While a card is pending, the message rejects the card with the text as feedback.
- Signal `network_update(NetworkUpdate(request_id, kind))`, where `kind` is `closed`: every thread of the request has ended. It wakes an internal turn, which summarizes in prose.
  - An inline question is no update and never reaches the model: the escalation's own commits (open, assign, resolve, cancel) touch the request's conversation, so the chat refetches.
- Update `resolve_card(CardResolution) -> CardResolved(card_id, status, request_id?)`. The validator rejects a card that isn't the pending one, and feedback on an approval.
- Query `pending() -> ConversationPending(queued_messages, network_updates, turn_in_flight, pending_card_id, turns)`.

### `NetworkRequestWorkflow` (`workflows/network_request.py`)

- `run(NetworkRequestInput(world_id, request_id, requester_org_id, conversation_id?, recipient_org_ids, max_active_threads=8)) -> NetworkRequestResult(status)`.
  - Opens one thread per recipient through a sliding window.
  - Once every thread has ended, it sets the request `closed` and sends `notify_conversation(kind="closed")`.
  - There is no fan-in, deadline or acceptance.
- Signal `thread_changed(ThreadChanged(thread_id, responder_org_id, state))`.
- Query `status() -> RequestProgress(request_id, status, threads: ThreadProgressRef(thread_id, responder_org_id, state)[])`.
- A scenario's seeded open requests (`threads.yaml` `requests`, such as the adversary's) get the same workflow when their world is created or reset: `worlds.service.RequestStarter.start(world_id, request_ids)`, implemented by `worlds/requests_temporal.TemporalRequestStarter`. A start that fails is logged, and the visitor still gets the world.

### `ThreadWorkflow` (`workflows/thread.py`)

`run(ThreadInput(world_id, thread_id, request_id, requester_org_id, responder_org_id, conversation_id?, cassette_thread, max_turns=16, deadline_at?)) -> ThreadResult(state, turns)`.

The requester's representative opens. A side's turn is a tool loop (R§5). When it ends with `end_turn`, the other side moves, receiving every message sent since its last turn. Then:
- **`consult`.** The thread goes `awaiting_principal`, and `answer_consult` runs.
  - Released: the same side's next turn opens with the answer as the owed result.
  - Escalated: the workflow waits for `resolve_escalation`.
  - There is no holding message or timer.
- **A held message (*Ask*).** The thread goes `awaiting_principal`.
  - Approve, or approve with edits: the message is sent, the counterparty moves next, and the owed result opens the holding side's next turn.
  - Decline: nothing is sent, and the same side moves again with the owed refusal.
  - Take over: the member's text goes out with `human` provenance, and the counterparty moves next.
- **`close`.** The thread becomes `closed` for both sides. The other side gets a `closed` notice and one last turn, in which only `close` (its own summary) takes effect.
- **Bounds.** `max_steps` hands the move to the other side, like `end_turn`. A refusal closes the thread with no summary. The thread ends `expired` at `max_turns` (both sides' turns together) or at `deadline_at`, which bounds every wait, a person's included. These bounds are R§5's turn cap and idle timeout; there is no separate idle timer.

Handlers:
- Update `resolve_escalation(EscalationResolution) -> EscalationResolved(escalation_id, action, message_id?)`. The validator rejects:
  - with `ESCALATION_NOT_OPEN_ERROR` an escalation that isn't open on this thread;
  - with `NOT_IN_ORG_ERROR` a `resolution.org_id` that isn't the escalation's org;
  - with `INVALID_RESOLUTION_ERROR` a `take_over` without text, a consult `approve` with neither text nor a draft, or a `draft_rev` that hasn't been written yet.

  A consult resolution is pinned to a draft rev: the one it names, else the newest one written.
- Update `reassign(Reassignment(escalation_id, to_member_id, by_member_id)) -> Reassigned(escalation_id, assignees)`. It moves the item and its routed thread access, and records nothing for learning.
- Update `redraft(Redraft(escalation_id, instruction, by_member_id)) -> RedraftStarted(escalation_id, draft_rev)`: open `consult` escalations only. A rev that ends up unwritten (the answerer's call failed or returned no draft) is reported by `NetworkActivities.report_redraft_failed` as `RedraftFailed` (`escalation.redraft_failed`, `{escalation_id, draft_rev}`, in the escalation's scope), so the panel stops waiting for it at once.
- Query `state() -> ThreadProgress(thread_id, state, side_to_move, requester_turns, responder_turns, open_escalation_ids, owed, last_stop)`.
- The `accepted` signal is gone.

### Activities

`AgentActivities` runs on `harmony-model` (`activities/agents.py`):
- `internal_turn(InternalTurnInput) -> InternalTurnResult`.
- Each internal or representative turn writes its `agent_turns` row once `run_turn` returns. A turn paused on a card is written again when it resumes and ends.
- `rep_turn(RepTurnInput(world_id, thread_id, side, org_id, cassette_thread, turn_id, turn, owed, deliver_after_seq)) -> RepTurnResult(turn_id, stop, next_step, effects, owed?, refusal_category?, delivered_through_seq)`.
- `answer_consult(AnswerConsultInput(world_id, consult_id, thread_id, side, org_id, cassette_thread, consult_turn, draft_rev, instruction?))` returns one of:
  - `ConsultReleased(consult_id, owed)`;
  - `ConsultEscalated(consult_id, escalation_id, draft_rev?, assignee_ids)`.

  It runs the answerer's tool loop, then the sharing check (role `sharing_check`) when the first draft would be released under *Auto*, then `consult.settle`, which applies `release.decide` and, when a person is needed, `routing.apply_route`. A redraft is never checked: a person releases it. The settle unit of work also records the rev's `agent_turns` rows.

`NetworkActivities` runs on `harmony-main` (`activities/network.py`):
- `apply_card_resolution(CardResolutionInput) -> CardResolutionApplied`.
- `start_network_request(StartRequestInput)`.
- `open_thread(OpenThreadInput) -> ThreadInput`.
- `apply_resolution(ApplyResolutionInput(world_id, thread_id, resolution, message_id)) -> ResolutionApplied(resolved, owed?, side, sent)`.
- `reassign_escalation(ReassignInput(world_id, reassignment)) -> Reassigned`.
- `set_thread_state(ThreadStateInput)`, `set_request_status(RequestStatusInput)` and `notify_conversation(NotifyConversationInput)`.

`post_holding`, `holding_delay`, `record_acceptance`, `accept_delegated` and `countersign` are deleted, and so is their registration in `worker.main_activities`.

## API (`src/harmony/api/routes/`)

Every route below is registered in `create_app` (`P1_P2_ROUTERS`), and `tests/unit/test_contract_routes.py` proves the table. Every route is implemented.

Errors are `ApiProblem{detail, code, resolved_by?}`, where `code` is one of:
- `not_found`;
- `invalid`;
- `not_admin`;
- `card_not_pending`;
- `escalation_not_open`;
- `not_in_org`;
- `world_retired`.

A 401 keeps FastAPI's `{detail}`. Routes marked "cursor" carry `X-Harmony-Cursor`. The `/api/offers` route and the grant, permission and knowledge-visibility endpoints are gone.

| Method and path | Request → response | Semantics |
|---|---|---|
| `GET /api/conversations` | → `ConversationSummary[]` (cursor) | The sidebar Recents. |
| `POST /api/conversations` | `{text}` → `ConversationCreated{id, message_id}` | Start a chat. |
| `GET /api/conversations/{id}` | → `ConversationDetail{id, title, owner, items: ChatItem[], pending_card_id, queued_message_ids, turn_in_flight, version}` (cursor) | The chat view. Owner only. |
| `POST /api/conversations/{id}/messages` | `{text}` → `MessagePosted{message_id, outcome: queued \| card_rejected, card_id}` | A member message. A pending card is rejected with the text as feedback. |
| `POST /api/cards/{card_id}/approve` | `{recipients?: uuid[] (min 1), brief?: string}` → `CardResolved{card_id, status, request_id?}` | Approve the outreach card, with the member's brief and a subset of the proposed recipients. Errors: 409 `card_not_pending`; 422 `invalid` (recipients outside the proposed ones); 404. |
| `POST /api/cards/{card_id}/reject` | `{feedback}` → `CardResolved` | Reject the card with feedback for Claude. |
| `GET /api/requests/{id}` | → `RequestStatusView` (cursor) | Per-recipient status, latest line and own-side close summary. |
| `GET /api/inbox` | → `Inbox{needs_you, with_your_team, claude_handling, shared_with_you, done}` (cursor) | Each section is a list of `InboxItem{thread_id, title, side, counterparty, thread_state, side_to_move, escalation: InboxEscalation{id, kind, status, question, reason, assignees, priority, created_at, version}?, shared_by?, shared_question?, access_reasons, last_activity_at}`. `reason` is "Why this needs you". `side_to_move` is whose turn it is; while the thread is `awaiting_principal`, it is the side checking with its team. A live thread with an item open for a teammate (not the member) is `with_your_team`. A thread whose items are all resolved stays under `claude_handling` (or `shared_with_you`) until it closes or expires; only those are `done`. There are no routing stats. |
| `POST /api/escalations/{id}/resolve` | `{action: approve \| decline \| take_over, text? (≤ 20000), draft_rev?}` → `EscalationResolved` | Approve & send (`text` is "Edit"), Decline (`text` is the reason), or Take over (needs `text`). Errors: 409 `escalation_not_open` (with `resolved_by`); 403 `not_in_org`; 422 `invalid`; 404. |
| `POST /api/escalations/{id}/reassign` | `{member_id}` → `Reassigned` | Move the item. The target must be a member of the escalation's org. |
| `POST /api/escalations/{id}/redraft` | `{instruction}` → `RedraftStarted` | "Ask Claude to redraft" (consult items only). |
| `GET /api/threads/{id}/workspace` | → `ThreadWorkspace` (cursor) | Own side only; details below the table. |
| `POST /api/threads/{id}/notes` | `{body}` → `NoteView` | Internal note. An `@slug` mention shares the thread. |
| `GET /api/orgs/{slug}/harmony` | → `HarmonySettingsView` (cursor) | Any member of the org; fields below the table. |
| `PUT /api/orgs/{slug}/harmony` | `HarmonySettingsUpdate` → `HarmonySettingsView` | Admins only (403 `not_admin`). The update and its notices are described below the table. |
| `POST /api/orgs/{slug}/trusted-organizations` | `TrustedOrganizationCreate{counterparty_org_id, autonomy?, sharing_description=""}` → `TrustedOrganizationView` | Admins only. Errors: 409 `invalid` if the org is already trusted; 422 `invalid` for self or an org outside the world. |
| `PUT /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}` | `TrustedOrganizationUpdate{version, autonomy?, sharing_description?}` → `TrustedOrganizationView` | Admins only. `autonomy: null` means "Same as default". A stale `version` is 409. Live threads with that counterparty get `mode` / `sharing` notices on change. An org that isn't trusted is 404. |
| `DELETE /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}?version=` | → 204 | Admins only. A stale `version` is 409; an org that isn't trusted is 404. Same notices. |
| `GET /api/network?request=<id?>` | → `NetworkView` (cursor) | The network graph; details below the table. |
| `GET /api/demo/story` | → `DemoStory{scenario, world_date, acting_as, suggested_message?, suggested_recipients, steps}` | The walkthrough beat. |

**`ThreadWorkspace`** is `{thread_id, side, consults, escalations, notes, representative, close_summary, version}`:
- `consults`: `ConsultView{id, question, status, answer, needs_human, needs_human_reason, sharing_check, released_text, released_by, released_by_member, created_at, released_at, version}`.
- `escalations`: `EscalationView{id, kind, status, consult_id, reason, assignees, priority, current_draft_rev, drafts: EscalationDraftView{draft_rev, answer, route, rationale, activity: ActivityLine[], created_at}[], resolution, resolved_by, resolved_at, created_at, version}`.
- `representative`: `RepresentativeContext{parts: ContextPart[], notices: DeliveredNotice[], released: ReleasedAnswerView[], frozen_at}`. It is "What our representative saw", and is null before the side's first turn. `notices` (`{kind, text, delivered_at}`) and `released` list only what a stored call of that side carried.

**`HarmonySettingsView`** is `{org, settings_version, public_summary, instructions, sharing_description, autonomy, trusted_organizations, directory_visibility, routing_prompt, default_assignee, members, policy_version, editable}`:
- `trusted_organizations`: `TrustedOrganizationView{counterparty, autonomy?, sharing_description, updated_by?, updated_at, version}`.
- `members`: `MemberProfileView{member, role, title, profile}`.

**`PUT /api/orgs/{slug}/harmony`** takes `HarmonySettingsUpdate`, in which every field except `policy_version` is optional: `{policy_version, public_summary?, instructions?, sharing_description?, autonomy?, directory_visibility?, routing_prompt?, default_assignee_id?, member_profiles?: {member id: profile}}`.
- `public_summary` and `instructions` write a new `rep_settings` version. Live threads keep their version and get a `settings` notice.
- Policy fields apply live. A live thread whose mode or applicable sharing descriptions changed gets a `mode` or `sharing` notice.
- `member_profiles` sets the named members' profiles (R§12: admins edit them here). Only the members it names change; it bumps no version and sends no notice, because the answerer reads profiles live.
- A stale `policy_version` is 409 `invalid`. A default assignee or profile for someone outside the org, or a null for a field that can't be cleared, is 422 `invalid`. Only `default_assignee_id` can be cleared (routing then falls back to an admin).

**`NetworkView`** is `{world_id, requests: NetworkRequestRef[], request_id, nodes, edges, counts}`:
- `nodes`: `NetworkNode{org, kind, featured, role, participant?, fit?, waiting_on?, suggested}`.
- `edges`: `NetworkEdge{thread_id, from_org_id, to_org_id, state, latest_line?, turn_count?, close_summary?, last_activity_at}`. `close_summary` is set only for the acting member's own org's side.
- `counts`: `NetworkCounts{orgs, threads, active, closed}`.
- There are no offer labels.

P0's routes are unchanged. Their response titles use `brief_title`.

## File ownership

Every file maps to one owner. To change a file you don't own, ask the coordinator. Each task also owns the tests and fixtures for its own files. A test of a deleted concept is deleted, not skipped.

| Files | Owner |
|---|---|
| `src/harmony/db/models/**`, `migrations/versions/0001_initial.py` | Contract (frozen) |
| `src/harmony/agents/models.py`; the model classes and output schemas in `agents/consult.py` | Contract (frozen) |
| Tool names, descriptions, input schemas and input models in `agents/tools/rep.py` and `agents/tools/internal.py` | Contract (frozen) |
| `src/harmony/agents/runtime.py`: `PerformativeName`, `TurnStop` and the `TurnEffect` union | Contract (frozen) |
| `src/harmony/temporal/workflows/payloads.py`, and the workflow handler signatures (names, arguments, results) | Contract (frozen) |
| API request and response models in `src/harmony/api/routes/*.py`, `routes/problems.py` and `api/app.py` | Contract (frozen) |
| `src/harmony/events/schemas.py`, `web/src/api/schema.d.ts` (generated), `docs/P1_P2_CONTRACT.md`, `tests/unit/test_contract_routes.py` and `tests/unit/test_schema.py` | Contract (frozen) |
| `src/harmony/agents/**` except the contract pieces above, including `release.py`, `context.py`, `discovery.py`, `keys.py`, `transcript.py`, the tool executors, `prompts/*.md` and the answerer and sharing-check calls in `consult.py` | **R-agents** |
| `src/harmony/domain/**`, `src/harmony/api/**` route bodies, `src/harmony/seeds/**` (the loader; `seeds/schema.py` shapes are coordinated with R-scenario) | **R-domain-api** |
| `src/harmony/temporal/**` (bodies, activity payloads, `worker.py`) | **R-temporal** |
| `scenarios/**`, `tests/fixtures/cassettes/**`, `src/harmony/storyrun/**`, `src/harmony/cli_run.py`, `docs/SCENARIO.md` | **R-scenario** |
| `web/src/customize/**`, `web/src/requests/**` | **R-ui-settings** |
| `web/src/chat/**`, `web/src/network/**`, `web/src/shell/**`, `web/src/demo/**`, and `web/src/fixtures/**` and `web/src/gallery/**` for those areas | **R-ui-chat** |
| `docs/ARCHITECTURE.md`, `docs/UI_REFERENCE.md`, `docs/P1_P2_PLAN.md` | **R-docs** |
| Not listed: `src/harmony/{auth,llm,events/sse.py,events/stream.py,db/uow.py,...}` (P0 plumbing), `web/src/{app,api/*.ts,realtime,theme,scaffold}/**` | Frozen. A task that needs a change there makes the smallest one and reports it. |

## Deviations and decisions

These are the contract's own choices where R§ is silent, recorded so every task makes the same ones.

- **`network_update` kind** is `closed` alone. `closed` replaces `fan_in`: with no offers there's nothing to fan in, so internal Claude wakes once every thread has ended and summarizes in prose (R§10.5).
- **Release by mode** is in `agents/release.py`. The sharing check runs only in *Auto*, and only on a draft the answerer would release. *Ask* never runs it, and *Skip* skips it (R§4.4).
- **"Why this needs you"** isn't stored. `release.why_needed` derives it from the consult row: no draft, the answerer's reason, the failed check's reason, or *Ask*. A held message uses the fixed `HELD_MESSAGE_REASON`.
- **`sharing` notice.** The representative's frozen prompt holds the sharing descriptions (R§2), so a change that applies to a live thread is delivered as a notice, the same way a `mode` change is.
- **Reassign carries no reason.** R§6 says it records nothing.
- **`EscalationResolution.org_id` and `NotInOrg` (403)** stand in for the old signing-authority check: a member may resolve only their own org's items.
- **`approver` is dropped (R§12).** Roles are `admin` and `member`, ordered that way in the member directory. Maya, Henderson's PI, is an admin in the demo scenario.
- **Profiles are edited with the settings (R§12).** `PUT /api/orgs/{slug}/harmony` takes `member_profiles`, so the one admin-only write path covers them; the Team pane edits them in the same draft.
- **Prose and profiles are last-writer-wins.** Only `rep_policy` fields bump `policy_version`. A `rep_settings` version is append-only and a profile is read live, so two admins editing them at once both land, the later one on top.
- **The request title** is derived from the brief's first line (`brief_title`). There's no title column.
- **The answerer's cassette steps** are blocked per draft rev (`answerer_step`), because the answerer is now a tool loop.
- **`MemberProfileView`** is the settings page's member row. The name is distinct from `auth.views.MemberView`, so the generated client has no `harmony__…` names.
- **The trusted-organization list** has no GET route of its own. It is read through `HarmonySettingsView.trusted_organizations`.
- **Left-out recipients stay on the request's status.** A recipient the card proposed but the member left out has no thread, and it shows as `screened`, with the card's fit and reason, so the status still accounts for everyone Claude suggested.
- **Seeded open requests start with their world.** A scenario's open requests (the Vantage adversary in `harmony-demo`) start their `NetworkRequestWorkflow` when a world is created or reset, through `RequestStarter`, as if their outreach had just been approved. In live mode those turns cost money in every world.
- **One `agent_turns` row per draft rev.** A consult's answerer and sharing check run every rev under one checkpoint turn, so each rev's row is keyed `rev_turn_id(checkpoint, rev)` and costed from that rev's steps. Its latency is the attempt that finished it.
- **Story rules are used in order.** `story.yaml`'s `humans` rules are used in order, each for one escalation, because one org may face several decisions with the same counterparty (a question, then a go-ahead). An escalation asked again keeps its rule. `harmony run` decides only the story request's escalations, so the adversary's escalation stays open for a person.
- **SSE thread access is cached per stream.** `events/sse.py`'s `ThreadAccessCache` (`access_ttl_s`, 30 s) resolves a viewer's thread access once per batch and caches it. An entry is dropped when a `thread.access_changed` event for its thread goes by, and expires after the TTL regardless. This P0 plumbing was renamed from its grant-era names so the grep test passes; its behavior is unchanged.

