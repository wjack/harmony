# P1–P2 shared contract (unstructured revision)

This is the contract every rework task codes against after [REVISION_UNSTRUCTURED.md](REVISION_UNSTRUCTURED.md) ("R§n" below). It lists the shapes, names and signatures that cross task boundaries, gives each one line of meaning, and says who owns which file. The code is the source of truth, and everything below exists in the tree: the rework tasks have landed, and the engine runs end to end (`tests/e2e/test_story_scripted.py` plays R§10 against Temporal). Plain "§n" refers to [ARCHITECTURE.md](ARCHITECTURE.md), which R§ overrides wherever they disagree.

The `Owner:` lines in module docstrings date from the P1 build (E1–E5, U1–U4) and are historical. The [file ownership](#file-ownership) table below is what counts.

The live demo ([LIVE_DEMO_PLAN.md](LIVE_DEMO_PLAN.md), "3.n" there) adds to this contract. Its shapes are in the [Live phase](#live-phase) section, and the sections above it are updated where it changes them. Feedback round 2 ([FEEDBACK_R2_PLAN.md](FEEDBACK_R2_PLAN.md); designs [EDIT.md](design/EDIT.md) and [REOPEN.md](design/REOPEN.md)) adds Edit (the `steer` Update), Reopen and the rest of its shapes; they are in the [Round 2](#round-2) section at the end, and the sections above are updated where it changes them.

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
- **Dev databases upgrade in place.** `0001_initial` was rewritten in place once (R§8), and is now fixed: the live phase's schema is the `0002_live_demo` migration and round 2's are `0003_edit_guidance` and `0004_reopen`, so `make migrate` takes a dev database at `0001` to head.

## Database

`migrations/versions/0001_initial.py` is the P1–P2 schema, `0002_live_demo.py` adds the live phase's (see [Live phase](#live-phase)), and `0003_edit_guidance.py` and `0004_reopen.py` add round 2's (see [Round 2](#round-2)). On a fresh database, or one at `0001`, `make migrate` applies them; `alembic check` reports no drift, and `alembic downgrade 0001 && alembic upgrade head` round-trips. No task writes a migration. A task that needs a column asks the coordinator, who folds it into `0002`.

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
- `ThreadState` = `active` | `awaiting_principal` | `closed` | `expired`. Every surface shows both terminal states as Closed, with the reason from `EndReason` (round 2).
- `ThreadMessageKind` = `message` | `close`.
- `Provenance` = `agent` | `agent_approved` | `human`.
- `ConsultStatus` = `answering` | `released` | `needs_human` | `declined`.
- `EscalationKind` = `consult` | `outbound_review`.
- `ReleasedBy` = `auto` | `member`.
- `Fit` = `strong` | `possible` | `no` | `unknown`.
- `CardKind` = `outreach`.
- `ModelRole` adds `sharing_check`.
- `MemberRole` = `admin` | `member` (R§12 drops `approver`). Every member of the demo cast is an admin (round 2, E1); tests make a non-admin in their fixtures.
- Round 2: `EndReason`, `ReopenBlock`, `SteerKind`, `SteerStatus`, `SteerGuidanceOutcome`, `SteerDraftOutcome`, `SteerFailure`, and `NoticeKind` gains `reopened` (see [Round 2](#round-2)).

## Events (`src/harmony/events/schemas.py`)

- `EntityType` / `ENTITY_TYPES`: every `entity.snapshot` type.
  - The `Snapshotable` models (`db.models.SNAPSHOT_ENTITIES`): `org`, `member`, `rep_settings`, `rep_policy`, `trusted_organization`, `conversation`, `card`, `network_request`, `thread`, `consult`, `escalation`, `knowledge_item` and `document`.
  - The append-only types (`APPEND_ONLY_ENTITY_TYPES`): `thread_message`, `internal_note`, `escalation_draft` and `rep_guidance`. These are snapshotted once at version 1.
  - Round 2 adds `steer` (versioned) and `rep_guidance`. Both are what an org tells its own Claude, so the network view's stream never carries them (`LENS_HIDDEN_ENTITY_TYPES`).
- `ThreadAccessChanged.added`: true when a member gained an access reason, false when they lost one. It was `granted`.
- `DemoNeedsHuman` (`demo.needs_human`, `{org_slug, member_slug, entity_id}`) and `StreamChatDelta` (`stream.chat_delta`) are unchanged. The web client types the delta from the generated schema (`EventOf<"stream.chat_delta">`); the realtime dispatcher leaves it to the chat view, which appends it to the streaming turn.
- **A deleted row** gets one last snapshot at `version + 1` whose `data` is the row's keys plus `deleted: true`. Removing a trusted organization is the one case.

Snapshot scopes:
- Thread rows go to each party org's thread scope.
- Org-private rows (consults, escalations, drafts, comments, steers, guidance) go to the owning org's thread scope.
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
| `steer` (round 2) | the steering org | the thread key | the steer's `seq` on that side | 0 for the guidance, 1 for the held message's rewrite |

Checkpoint turn ids:
- turns use the workflow-minted `turn_id`;
- screener calls use `screen_turn_id(search_id, org_id)`;
- answerer calls use `consult_turn_id(consult_id)`;
- sharing checks use `sharing_turn_id(consult_id)`;
- a steer's two calls use `steer_guidance_turn_id(steer_id)` and `steer_rewrite_turn_id(steer_id)`.

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
  - `ParticipantStatus` = `screened` | `messaged` | `replied` | `needs_human` | `closed` (R§9; `expired` shows as `closed`). A reopened thread reads `messaged`, `replied` or `needs_human` again, from its live state.
- **People, routing and context.**
  - `MemberRef(id, slug, name)`.
  - `Route(assignees: RouteAssignee(member: slug, why)[])`. A before-validator maps the old stored shape (`assignees: [slug]`, one `reason`) to each slug with that reason as its why.
  - `ActivityLine(tool, label, detail?)`: one thing internal Claude did with a tool, as the reviewer reads it.
  - `ContextPart(kind, label, text)`, where `ContextPartKind` = `public_summary` | `instructions` | `sharing` | `brief` | `ask` | `counterparty`. These are the parts of a representative's frozen prompt (R§2).
- **Escalations.**
  - `EscalationAction` = `approve` | `decline` | `take_over`. Approve with `text` sends the member's wording of the draft. Telling Claude what to change is the `steer` Update (round 2), which replaced Redraft and `revise`; reassign is its own Update.
  - `EscalationResolution(escalation_id, action, text?, draft_rev?, member_id, org_id)`: the `resolve_escalation` Update's argument and the stored resolution. `org_id` is the acting member's org, so the validator can refuse another org's member.
- **Cards.**
  - `ScreenedCounts`.
  - `OutreachRecipient(org_id, fit?, reason, ask?, title?, suggested?)`.
  - `OutreachCardPayload(kind="outreach", brief, recipients, screened?, search_ids, title?, auto_send_recipients?)`. `CardPayload` = `OutreachCardPayload`.
  - `OutreachEdits(recipients?: org ids, brief?, asks?: {org id: ask})`.
  - `CardResolution(card_id, decision: approve | reject, member_id, edits?, feedback?, auto=false, rejected_with?)`. `rejected_with` (`CardRejectedWith` = `decline` | `message`) says how a member rejected it: the card's Decline, or a message typed while it waited.
- **Chat items.** The `ChatItem` union (on `type`): `user_text`, `assistant_text`, `thinking_summary`, `tool_activity`, `outreach_card`, `request_status` and `inline_question`. There are no compare-offers or accept cards.

### Consult (`agents/consult.py` models) and release (`agents/release.py`)

- `ConsultDraft(answer, needs_human, needs_human_reason?, route?, rationale, short_question?)`: the answerer's structured output (`CONSULT_OUTPUT_SCHEMA`). There are no sources, offer or suggested actions; what the answerer looked at is recorded from its tool calls.
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
  - `answer(sessionmaker, model, *, world_id, consult_id, draft_rev=1, instruction?, attempt, model_override?, cassette_thread?, consult_turn?) -> DraftReady | AnswerFailed`: a tool loop of at most `ANSWERER_STEPS` calls over `ANSWERER_TOOLS`, ending in the structured draft (R§4.1). Its prompt (`answer_inputs`) is read live: the sharing descriptions, instructions, member profiles, routing prompt, the current guidance for this side (quoted, and labelled as never widening what may be shared; "None." when there is none), a steer's instruction when it redrafts, the thread quoted, and the question. No comment reaches it. It holds no records: the answerer looks them up.
  - `check_sharing(sessionmaker, model, *, world_id, consult_id, draft, draft_rev=1, attempt, model_override?, cassette_thread?, consult_turn?) -> SharingCheck`: one call that sees only the applicable sharing descriptions, the question and the draft. A refusal or an invalid verdict returns a failed check whose reason says it couldn't run.
  - `settle(uow, *, consult_id, outcome, draft_rev=1, sharing?) -> Answered | Escalated`: records the draft (`consults.record_draft`) and verdict (`consults.record_sharing_check`), applies `release.decide` with live policy, and releases (`consults.release`, `released_by="auto"`) or opens the consult's one escalation (`escalation_id_for(consult_id)` = `uuid5(consult_id, "escalation")`) and routes it. Idempotent; a steer's redraft only adds its draft rev to the open escalation.
  - `result_text(action, text) -> str`: the `consult` tool result for any settlement. An approval is `"Your side answered:\n"` plus the text quoted (`context.quote_released`), and that string is what `consults.released_text` stores; a decline quotes what the side may say, if anything.
  - `settled_result(consult) -> str | None`: the result a settled consult owes, for a retried settle. A settled consult is never reused: the same question asked again is a new consult (`consults.open` dedupes only open ones, storing a repeat under `<dedupe_key>#<consult_id>`), so it is answered from the records and policy as they are then.

### Runtime (`agents/runtime.py`)

- `run_turn(ctx, executor, model, *, transcript, on_text=None) -> TurnOutcome`: unchanged. At most `MAX_STEPS` (8).
- `PerformativeName` = `send_message` | `consult` | `close`: the representative tools that can stop a turn before the model does. `consult` stops it until the answer is released, `close` ends the conversation, and `send_message` stops it only when *Ask* holds the message (or, round 2, an Edit or a guided bare reply holds it). Otherwise a turn ends when the model stops calling tools (`end_turn`).
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

- **Representative** (`REP_TOOLS`, inputs `REP_INPUTS`): `send_message{text}`, `consult{question}` and `close{summary}`. They are identical for every representative. A turn may send several messages and may consult before it sends (R§5). Once it has sent, `consult` is refused with `CONSULT_AFTER_SEND`, which tells it to end the turn, not to close, and to ask at the start of its next turn.
  - Under *Ask*, `send_message` is held as an `outbound_review` escalation whose draft is the message. Its id is `uuid5(message_id, "outbound_review")`, where `message_id` is the call's side-effect id, and its dedupe key is `outbound:{message_id}`. The turn stops, and the result is owed once a person decides. A `close` is never held: no words cross but its empty marker, and the summary stays with the closer's side.
  - `close` stores its side's summary (`threads.store_close_summary`) and appends a `close` thread message with an empty body, so the summary never reaches the counterparty.
  - A bare text reply maps to `send_message` (`TEXT_REPLY_ID`).
  - The mode is read live, under the thread's row lock, at each `send_message`. A call whose side effect an earlier attempt of the turn applied (a message, a held item or a consult under its side-effect id) returns that effect again instead of deciding anew, so a retried turn never sends a held message or holds a sent one.
- **Internal Claude** (`INTERNAL_TOOLS`, inputs `INTERNAL_INPUTS`):
  - `search_org_knowledge{query, limit?}`: hits are named `kn:<uuid>` (knowledge items) and `doc:<uuid>` (documents);
  - `read_document{id}`: takes a `kn:` or `doc:` id, or a bare id;
  - `search_network{need}`;
  - `send_network_request{brief, title?, recipients: [{org: slug, reason, ask?, title?, suggested?}]}`, which raises the outreach card. The new fields have defaults until the cassettes carry them (see [Live phase](#live-phase)). Each recipient carries its best fit across all of this chat's searches, and the card's screened counts count each org once;
  - `get_request_status{request_id?}`.
- **Answerer** (`ANSWERER_TOOLS`): `search_org_knowledge` and `read_document`, over its own org's records only.

### Context (`agents/context.py`)

- `build_rep_context(session, *, world_id, thread_id, side, model) -> RepContext` renders only the R§2 parts: the counterparty, the public summary, the instructions, the sharing descriptions that apply (`sharing_parts(policy, trusted?, *, counterparty_name)`) and, on the requester side, the brief. Each part is in its own tag with `<` escaped.
- `context_parts(system_text) -> list[ContextPart]`: reads the parts back from the frozen bytes, for "What our representative saw".
- `delivered_notices(...) -> list[DeliveredNotice]` and `received_results(...) -> set[str]`: the notices and result texts the side's stored transcript carries, for the same drawer.
- `NoticeKind` = `settings` | `mode` | `sharing` | `world_date` | `closed` | `reopened`. These are code-written notices.
  - `settings`: the org changed its prompt settings, and the thread keeps its frozen version (`domain/settings.SETTINGS_NOTICE`).
  - `mode`: the mode for this counterparty changed (`domain/settings.mode_notice`).
  - `sharing`: the sharing descriptions that apply changed. The text is the new descriptions (`domain/settings.sharing_notice`).
  - `closed`: the counterparty closed, and this side may write its summary in one last turn (`context.closed_notice(counterparty_name)`).
  - `reopened` (round 2): the conversation was reopened, worded for the side that reopened it and for the other side. `begin_reopen` drops stale undelivered `closed` and `reopened` notices before it queues these.
- Guidance is never a notice (round 2): `guidance_block` renders a version as a code-written lead plus the text in `<guidance>`, the user-message block a turn opens with once, and `delivered_guidance` reads back which versions reached the representative.
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
  - `create_from_card(uow, card, resolution, *, request_id)`: takes the brief from `edits.brief`, else the card's, and the recipients from `chosen_recipients` with the edited asks (see [Live phase](#live-phase)).
  - `set_status`, `recipients_of`, `threads_of` and `request_status`. Round 2 adds `reopen` (a closed request back to active, round + 1) and `end_round(uow, request_id, *, from_workflow)`, the one locked path that closes a request.
  - `status(session, request, *, waiting_of=None) -> RequestStatusView`. A recipient the card proposed but the member left out (no thread) is listed as `screened`, with its fit and reason from the card.
  - `participant(thread, *, needs_human, replied) -> ParticipantStatus`.
  - `RequestStatusView(id, title, status, brief, conversation_id, created_at, screened, recipients, version)`.
  - `RecipientStatus(org, fit, reason, sent, participant, thread_id, thread_state, waiting_on, latest_line, close_summary, stop_reason?, last_activity_at)`. `latest_line` is the thread's newest message (either side) after its sender's name, so internal Claude and the member can tell who said it. `close_summary` is the requester side's own summary.
- **`threads.py`.**
  - `open`, `set_state`, `parties`, `side_of`, `org_of`, `other`, `cassette_key` and `locked`. `set_state` also takes `end_reason` and `closed_by` and, on the move into a terminal state, stores them with `ended_at`.
  - `add_access(uow, thread, *, member_id, org_id, reason="routed", shared_by_member_id?, question?) -> bool`: one access reason for one member, idempotent, announced with `thread.access_changed`.
  - `append_message(uow, thread_id, *, message_id, from_org_id, kind, body, provenance, turn_id?, author_member_id?, approved_by_member_id?)`: `turn_count` counts turns, not messages.
  - `store_close_summary(uow, thread_id, side, summary)`: the side's own summary on its `rep_sessions` row. The first write wins; a side with no session raises `ValueError`.
  - `TERMINAL_STATES` = `closed`, `expired`. A terminal thread leaves its state only through a reopen (`reopens.begin`, round 2).
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
- **`conversations.py`.** `tool_label(name, tool_input) -> (label, detail?)` is public and `chat_items` builds only the R§9 items. The send-mode helpers are in [Live phase](#live-phase).
- **`cards.py`.** `create(…, auto=False)`, and `awaiting_member()`, the one predicate for a card awaiting the member (see [Live phase](#live-phase)).
- **`notes.py` and `clock.py`:** unchanged. Notes are the request view's comments, which no Claude reads (round 2).
- **`steers.py`, `reopens.py`** (round 2): see [Round 2](#round-2).

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
- `NOT_FOUND_ERROR` (404);
- round 2's `STEER_RUNNING_ERROR` ("SteerRunning", 409 `steer_running`, `details=[{by_member_id}]`), `THREAD_CLOSED_ERROR` ("ThreadClosed", 409 `thread_closed`) and `STEER_CONFLICT_ERROR` ("SteerConflict", 422 `invalid`).

`payloads.update_failure(ActivityError)` re-raises an activity's typed cause, so the API sees the same types.

### `ConversationWorkflow` (`workflows/conversation.py`)

- `run(ConversationInput(world_id, conversation_id, org_id, owner_member_id, cassette_thread, carried, turns_per_run)) -> ConversationResult(turns)`.
- Signal `member_message(MemberMessage(message_id, member_id, text, sent_at))`. While a card is pending, the message rejects the card with the text as feedback.
- Signal `network_update(NetworkUpdate(request_id, kind, round=1))`, where `kind` is `closed`: every thread of the request has ended. It wakes an internal turn, which summarizes in prose. `round` counts the request's rounds (round 2's Reopen), and the conversation takes each (request, round) once.
  - An inline question is no update and never reaches the model: the escalation's own commits (open, assign, resolve, cancel) touch the request's conversation, so the chat refetches.
- Update `resolve_card(CardResolution) -> CardResolved(card_id, status, request_id?)`. The validator rejects a card that isn't the pending one, and feedback on an approval.
- Query `pending() -> ConversationPending(queued_messages, network_updates, turn_in_flight, pending_card_id, turns)`.

### `NetworkRequestWorkflow` (`workflows/network_request.py`)

- `run(NetworkRequestInput(world_id, request_id, requester_org_id, conversation_id?, recipient_org_ids, max_active_threads=8)) -> NetworkRequestResult(status)`.
  - Opens one thread per recipient through a sliding window.
  - It follows each thread's first run only. Once every first run has ended, it calls `end_round(from_workflow=True)`, which closes the request unless a reopened thread is still live; when it closed it, the workflow sends `notify_conversation(kind="closed", round)`. Otherwise the reopened thread's own tail closes it later.
  - There is no fan-in, deadline or acceptance.
- Signal `thread_changed(ThreadChanged(thread_id, responder_org_id, state))`.
- Query `status() -> RequestProgress(request_id, status, threads: ThreadProgressRef(thread_id, responder_org_id, state)[])`.
- A scenario's seeded open requests (`threads.yaml` `requests`, such as the adversary's) get the same workflow when their world is created or reset: `worlds.service.RequestStarter.start(world_id, request_ids)`, implemented by `worlds/requests_temporal.TemporalRequestStarter`. A start that fails is logged, and the visitor still gets the world.

### `ThreadWorkflow` (`workflows/thread.py`)

`run(ThreadInput(world_id, thread_id, request_id, requester_org_id, responder_org_id, conversation_id?, cassette_thread, max_turns=16, deadline_at?, run=1, carry?, reopen?, guidance={}, steer_seq={})) -> ThreadResult(state, turns)`. A reopened run is a new execution under the same id (round 2).

The requester's representative opens. A side's turn is a tool loop (R§5). When it ends with `end_turn`, the other side moves, receiving every message sent since its last turn. Then:
- **`consult`.** The thread goes `awaiting_principal`, and `answer_consult` runs.
  - Released: the same side's next turn opens with the answer as the owed result.
  - Escalated: the workflow waits for `resolve_escalation`.
  - There is no holding message or timer.
- **A held message (*Ask*).** The thread goes `awaiting_principal`.
  - Approve, or approve with edits: the message is sent, the counterparty moves next, and the owed result opens the holding side's next turn.
  - Decline: nothing is sent, and the same side moves again with the owed refusal.
  - Take over: the member's text goes out with `human` provenance, and the counterparty moves next.
- **`close`.** The thread becomes `closed` for both sides, `end_reason` `closed`. The other side gets a `closed` notice and one last turn, in which only `close` (its own summary) takes effect.
- **Bounds.** `max_steps` hands the move to the other side, like `end_turn`. A refusal closes the thread with no summary (`end_reason` `stopped`). The thread ends `expired` at `max_turns` (both sides' turns together, `turn_limit`) or at `deadline_at` (`deadline`), which bounds every wait, a person's included. These bounds are R§5's turn cap and idle timeout; there is no separate idle timer.

Handlers:
- Update `resolve_escalation(EscalationResolution) -> EscalationResolved(escalation_id, action, message_id?)`. The validator rejects:
  - with `ESCALATION_NOT_OPEN_ERROR` an escalation that isn't open on this thread;
  - with `NOT_IN_ORG_ERROR` a `resolution.org_id` that isn't the escalation's org;
  - with `INVALID_RESOLUTION_ERROR` a `take_over` without text, a consult `approve` with neither text nor a draft, or a `draft_rev` that hasn't been written yet.

  A consult resolution is pinned to a draft rev: the one it names, else the newest one written.
- Update `reassign(Reassignment(escalation_id, to_member_id, by_member_id)) -> Reassigned(escalation_id, assignees)`. It moves the item and its routed thread access, and records nothing for learning.
- Update `steer(SteerRequest) -> SteerStarted` (round 2) replaces `redraft`; `RedraftFailed` and `escalation.redraft_failed` are gone. See [Round 2](#round-2).
- Query `state() -> ThreadProgress(thread_id, state, side_to_move, requester_turns, responder_turns, open_escalation_ids, owed, last_stop)`.
- The `accepted` signal is gone.

### Activities

`AgentActivities` runs on `harmony-model` (`activities/agents.py`):
- `internal_turn(InternalTurnInput) -> InternalTurnResult`.
- Each internal or representative turn writes its `agent_turns` row once `run_turn` returns. A turn paused on a card is written again when it resumes and ends.
- `rep_turn(RepTurnInput(world_id, thread_id, side, org_id, cassette_thread, turn_id, turn, owed, deliver_after_seq, guidance_version?, steer_seq=0, reopened=false, run_start_seq=0)) -> RepTurnResult(turn_id, stop, next_step, effects, owed?, refusal_category?, delivered_through_seq)`.
- `answer_consult(AnswerConsultInput(world_id, consult_id, thread_id, side, org_id, cassette_thread, consult_turn, draft_rev, instruction?))` returns one of:
  - `ConsultReleased(consult_id, owed)`;
  - `ConsultEscalated(consult_id, escalation_id, draft_rev?, assignee_ids)`.

  It runs the answerer's tool loop, then the sharing check (role `sharing_check`) when the first draft would be released under *Auto*, then `consult.settle`, which applies `release.decide` and, when a person is needed, `routing.apply_route`. A steer's redraft is never checked: a person releases it.
- `write_guidance(SteerPartInput) -> GuidanceWritten` and `rewrite_held(SteerPartInput) -> int | None` (round 2, role `steer`). The settle unit of work also records the rev's `agent_turns` rows.

`NetworkActivities` runs on `harmony-main` (`activities/network.py`):
- `apply_card_resolution(CardResolutionInput) -> CardResolutionApplied`.
- `start_network_request(StartRequestInput)`.
- `open_thread(OpenThreadInput) -> ThreadInput`.
- `apply_resolution(ApplyResolutionInput(world_id, thread_id, resolution, message_id)) -> ResolutionApplied(resolved, owed?, side, sent)`.
- `reassign_escalation(ReassignInput(world_id, reassignment)) -> Reassigned`.
- `set_thread_state(ThreadStateInput)` and `notify_conversation(NotifyConversationInput)`. `ThreadStateInput` gains `end_reason`, `closed_by` and `last_turn` (round 2).
- `settled_card`, `stored_resolution` (the retry paths) and `note_unsummarized`.
- Round 2 adds `record_steer`, `settle_steer`, `record_reopen`, `begin_reopen`, `finish_run` and `end_round`. `set_request_status` and `report_redraft_failed` are gone.

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
- `world_retired`;
- the live phase's `passcode_required`, `passcode_invalid`, `too_many_attempts` and `payload_too_large` (see [Live phase](#live-phase));
- round 2's `member_changed`, `steer_running`, `thread_closed`, `thread_open`, `reopen_in_progress`, `closing_in_progress` and `not_reopenable` (see [Round 2](#round-2)). `ApiProblem` gains `by`, `member`, `reopened_by` and `block` for them;
- `nothing_to_retry`: a Retry with no step Claude declined waiting on it.

A 401 keeps FastAPI's `{detail}`. Routes marked "cursor" carry `X-Harmony-Cursor`. The `/api/offers` route and the grant, permission and knowledge-visibility endpoints are gone.

| Method and path | Request → response | Semantics |
|---|---|---|
| `GET /api/conversations` | → `ConversationSummary[]` (cursor) | The sidebar Recents. |
| `POST /api/conversations` | `{text, send_without_confirmation?: bool \| null}` → `ConversationCreated{id, message_id}` | Start a chat, with its send mode (null follows the org's default). |
| `GET /api/conversations/{id}` | → `ConversationDetail{id, title, owner, items: ChatItem[], pending_card_id, queued_message_ids, turn_in_flight, turn_failure, refusal?, send_mode, version}` (cursor) | The chat view. Owner only. `refusal{turn_id, category, retryable}` is set while Claude's last turn ended in a refusal. |
| `PATCH /api/conversations/{id}` | `ConversationUpdate{send_without_confirmation: bool \| null}` → `SendMode{override, org_default, effective}` | The chat's send mode (Live phase). Owner only (404 otherwise); 409 `world_retired`. |
| `POST /api/conversations/{id}/messages` | `{text}` → `MessagePosted{message_id, outcome: queued \| card_rejected, card_id}` | A member message. A pending card is rejected with the text as feedback. |
| `POST /api/conversations/{id}/retry` | `{text?}` (1..20000) → `MessagesRetried{message_ids}` | Send the messages Claude declined again, or `text` (the member's edit of them), as a new turn. Owner only (404 otherwise). 409 `nothing_to_retry` unless the last turn was refused at its first call and nothing has happened since. |
| `POST /api/cards/{card_id}/approve` | `{recipients?: uuid[] (min 1), brief?: string, asks?: {org id: string}}` → `CardResolved{card_id, status, request_id?}` | Approve the outreach card, with the member's brief, asks and a subset of the proposed recipients. The web always sends `recipients`. Errors: 409 `card_not_pending` (also for an `auto` card); 422 `invalid` (recipients or asks outside the proposed ones); 404. |
| `POST /api/cards/{card_id}/reject` | `{feedback?}` (1..4000) → `CardResolved` | The card's Decline (`rejected_with: decline`), with an optional reason that only Claude in this chat reads. |
| `GET /api/requests/{id}` | → `RequestStatusView` (cursor) | Per-recipient status, latest line and own-side close summary. |
| `GET /api/inbox` | → `Inbox{needs_you, with_your_team, claude_handling, shared_with_you, done}` (cursor) | Each section is a list of `InboxItem{thread_id, title, side, counterparty, thread_state, stop_reason?, side_to_move, escalation: InboxEscalation{id, kind, status, question, short_question?, reason, assignees, priority, created_at, version}?, shared_by?, shared_question?, access_reasons, last_activity_at}`. `title` is `threads.thread_title`. `reason` is "Why this needs you". `side_to_move` is whose turn it is; while the thread is `awaiting_principal`, it is the side checking with its team. A live thread with an item open for a teammate (not the member) is `with_your_team`. A thread whose items are all resolved stays under `claude_handling` (or `shared_with_you`) until it closes or expires; only those are `done`. There are no routing stats. |
| `POST /api/escalations/{id}/resolve` | `{action: approve \| decline \| take_over, text? (≤ 20000), draft_rev?}` → `EscalationResolved` | Approve and send (`text` is the member's wording of the draft), Decline (`text` is the reason), or Take over (needs `text`). `draft_rev` pins the draft the member saw, held messages included. `revise` is refused (422). Errors: 409 `escalation_not_open` (with `resolved_by`); 403 `not_in_org`; 422 `invalid`; 404. |
| `POST /api/escalations/{id}/reassign` | `{member_id}` → `Reassigned` | Move the item. The target must be a member of the escalation's org. |
| `POST /api/escalations/{id}/retry` | `{draft_text?, question?}` (1..20000, 1..2000) → `AnswerUsed{escalation_id, draft_rev}` | Run the step Claude declined on a question again, as its next draft rev: the answerer (on `question`, the member's rewording, which replaces the stored question), or the sharing check on the current draft (or on `draft_text`, the member's edit of it). `EscalationView.refusal{step, category, draft_rev}` says which. Errors: 409 `nothing_to_retry` unless Claude declined the step the item waits on; 409 `escalation_not_open`; 422 `invalid` for a held message, or `question` when the check is the declined step; 404. |
| `GET /api/threads/{id}/workspace` | → `ThreadWorkspace` (cursor) | Own side only; details below the table. |
| `POST /api/threads/{id}/notes` | `{body, as_member_id}` → `NoteView` | A comment for the team, which no Claude reads. An `@slug` mention shares the thread. Errors: 409 `member_changed` (with `member`: the tab's member is stale after a switch elsewhere), checked first; 404. |
| `POST /api/threads/{id}/steers` | `AddSteer` → 202 `SteerStarted` | Edit: tell this side's Claude what to do differently (round 2). |
| `POST /api/threads/{id}/reopen` | `ReopenRequest` → 202 `ReopenStarted` | Reopen a closed conversation (round 2). |
| `GET /api/orgs/{slug}/harmony` | → `HarmonySettingsView` (cursor) | Any member of the org; fields below the table. |
| `PUT /api/orgs/{slug}/harmony` | `HarmonySettingsUpdate` → `HarmonySettingsView` | Admins only (403 `not_admin`). The update and its notices are described below the table. |
| `POST /api/orgs/{slug}/trusted-organizations` | `TrustedOrganizationCreate{counterparty_org_id, autonomy?, sharing_description=""}` → `TrustedOrganizationView` | Admins only. Errors: 409 `invalid` if the org is already trusted; 422 `invalid` for self or an org outside the world. |
| `PUT /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}` | `TrustedOrganizationUpdate{version, autonomy?, sharing_description?}` → `TrustedOrganizationView` | Admins only. `autonomy: null` means "Same as default". A stale `version` is 409. Live threads with that counterparty get `mode` / `sharing` notices on change. An org that isn't trusted is 404. |
| `DELETE /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}?version=` | → 204 | Admins only. A stale `version` is 409; an org that isn't trusted is 404. Same notices. |
| `GET /api/network?request=<id?>` | → `NetworkView` (cursor) | The network graph; details below the table. |
| `GET /api/demo/story` | → `DemoStory{scenario, world_date, acting_as, suggested_message?, suggested_recipients, steps}` | The walkthrough beat. |
| `GET /api/demo/whose?chat=\|thread=` | → `DemoWhose{kind, orgs: {org, people: {member_id, name}[]}[]}` | Who can open a chat or thread the acting member can't, for the not-found page (round 2). |

**`ThreadWorkspace`** is `{thread_id, side, consults, escalations, notes, representative, close_summary, steers, guidance, earlier_summaries, version}` (round 2 adds the last three; see [Round 2](#round-2)):
- `consults`: `ConsultView{id, question, status, answer, needs_human, needs_human_reason, sharing_check, released_text, released_by, released_by_member, created_at, released_at, version}`.
- `escalations`: `EscalationView{id, kind, status, consult_id, reason, assignees, priority, current_draft_rev, drafts: EscalationDraftView{draft_rev, answer, short_question?, route: RouteView{assignees: {slug, why}[]}?, rationale, activity: ActivityLine[], rewritten_by: RewrittenBy{steer_id, author}?, created_at}[], resolution, resolved_by, resolved_at, created_at, version}`. A held message's `reason` is rev 1's own (an Edit's hold, a guided bare reply, or *Ask*).
- `representative`: `RepresentativeContext{parts: ContextPart[], notices: DeliveredNotice[], released: ReleasedAnswerView[], guidance: DeliveredGuidance[], frozen_at}`. It is "What our representative saw", and is null before the side's first turn. `notices` (`{kind, text, delivered_at}`), `released` and `guidance` list only what a stored call of that side carried.

**`HarmonySettingsView`** is `{org, settings_version, public_summary, instructions, sharing_description, autonomy, trusted_organizations, directory_visibility, routing_prompt, default_assignee, send_without_confirmation, members, policy_version, editable}`:
- `trusted_organizations`: `TrustedOrganizationView{counterparty, autonomy?, sharing_description, updated_by?, updated_at, version}`.
- `members`: `MemberProfileView{member, role, title, profile}`.

**`PUT /api/orgs/{slug}/harmony`** takes `HarmonySettingsUpdate`, in which every field except `policy_version` is optional: `{policy_version, public_summary?, instructions?, sharing_description?, autonomy?, directory_visibility?, routing_prompt?, default_assignee_id?, send_without_confirmation?, member_profiles?: {member id: profile}}`.
- `public_summary` and `instructions` write a new `rep_settings` version. Live threads keep their version and get a `settings` notice.
- Policy fields apply live. A live thread whose mode or applicable sharing descriptions changed gets a `mode` or `sharing` notice. `send_without_confirmation` is a policy field that no representative's prompt holds, so it sends no notice.
- `member_profiles` sets the named members' profiles (R§12: admins edit them here). Only the members it names change; it bumps no version and sends no notice, because the answerer reads profiles live.
- A stale `policy_version` is 409 `invalid`. A default assignee or profile for someone outside the org, or a null for a field that can't be cleared, is 422 `invalid`. Only `default_assignee_id` can be cleared (routing then falls back to an admin).

**`NetworkView`** is `{world_id, requests: NetworkRequestRef[], request_id, nodes, edges, counts}`:
- `nodes`: `NetworkNode{org, kind, featured, role, participant?, fit?, waiting_on?, suggested}`.
- `edges`: `NetworkEdge{thread_id, from_org_id, to_org_id, state, stop_reason?, end_reason?, closed_by?, latest_line?, turn_count?, close_summary?, last_activity_at}`. `close_summary` is set only for the acting member's own org's side, and `closed_by` only for a party to the thread.
- `counts`: `NetworkCounts{orgs, threads, active, closed}`.
- There are no offer labels.

P0's routes are unchanged, except `ThreadSummary` (`GET /api/threads`, `GET /api/threads/{id}`), whose `title` is `threads.thread_title` and which gains `stop_reason?` and round 2's `run`, `end_reason`, `ended_at`, `closed_by` and `run_finished_at`; `ThreadDetail`, which gains `reopen` and `runs` (see [Round 2](#round-2)); and `SessionView`, which gains `internal_model` (see [Live phase](#live-phase)).

## File ownership

Every file maps to one owner. To change a file you don't own, ask the coordinator. In the live phase, LIVE_DEMO_PLAN §4 assigned the files (L0–L8), and in round 2 FEEDBACK_R2_PLAN §2.1 did; their owners take precedence over the R-task names below. Each task also owns the tests and fixtures for its own files. A test of a deleted concept is deleted, not skipped.

| Files | Owner |
|---|---|
| `src/harmony/db/models/**`, `migrations/versions/0001_initial.py`, `migrations/versions/0002_live_demo.py` | Contract (frozen) |
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
- **Titles are written by internal Claude and stored** (3.13 E7): `network_requests.title` for the requester's member, and `threads.title` from each recipient's title, which both sides see. Rows without one fall back to `brief_title` (the requester) and the opening line or "Request from X" (a responder). See [Live phase](#live-phase).
- **The answerer's cassette steps** are blocked per draft rev (`answerer_step`), because the answerer is now a tool loop.
- **`MemberProfileView`** is the settings page's member row. The name is distinct from `auth.views.MemberView`, so the generated client has no `harmony__…` names.
- **The trusted-organization list** has no GET route of its own. It is read through `HarmonySettingsView.trusted_organizations`.
- **Left-out recipients stay on the request's status.** A recipient the card proposed but the member left out has no thread, and it shows as `screened`, with the card's fit and reason, so the status still accounts for everyone Claude suggested.
- **Seeded open requests start with their world.** A scenario's open requests (the Vantage adversary in `harmony-demo`) start their `NetworkRequestWorkflow` when a world is created or reset, through `RequestStarter`, as if their outreach had just been approved. In live mode those turns cost money in every world.
- **One `agent_turns` row per draft rev.** A consult's answerer and sharing check run every rev under one checkpoint turn, so each rev's row is keyed `rev_turn_id(checkpoint, rev)` and costed from that rev's steps. Its latency is the attempt that finished it.
- **Story rules are used in order.** `story.yaml`'s `humans` rules are used in order, each for one escalation, because one org may face several decisions with the same counterparty (a question, then a go-ahead). An escalation asked again keeps its rule. `harmony run` decides only the story request's escalations, so the adversary's escalation stays open for a person.
- **SSE thread access is cached per stream.** `events/sse.py`'s `ThreadAccessCache` (`access_ttl_s`, 30 s) resolves a viewer's thread access once per batch and caches it. An entry is dropped when a `thread.access_changed` event for its thread goes by, and expires after the TTL regardless. This P0 plumbing was renamed from its grant-era names so the grep test passes; its behavior is unchanged.


## Live phase

The live demo's shapes ([LIVE_DEMO_PLAN.md](LIVE_DEMO_PLAN.md), "3.n" below), fixed by its L0 task before the parallel tasks started and updated at integration to what was built. The routes are in `LIVE_ROUTERS` (`api/app.py`), and `tests/unit/test_contract_routes.py` lists them (`LIVE_ROUTES`) and proves that every error they document is an `ApiProblem`. Every live route is built; none answers 501.

**The gate (3.2, `api/gate.py`).**
- `GATE_COOKIE` = `harmony_gate`; `MAX_BODY_BYTES` = 256 KiB (`BodyLimit`).
- `PasscodeGate(app, *, settings)` is the outermost middleware and `BodyLimit(app, *, max_bytes=MAX_BODY_BYTES)` sits just inside it. Both are pure ASGI. The gate is on whenever `ACCESS_CODE` is set.
- `client_key(scope, settings) -> str | None`: the visitor's network from `CLIENT_IP_HEADER` (an IPv4 address, or the /64 of an IPv6 address), or None when it is unknown (missing, unparseable, or a private, loopback, link-local or CGNAT address). An unknown key gets no per-client limit and no client spend key. `client_key_hash(scope, settings)` is its HMAC under `SESSION_SECRET`, cut to 32 hex characters.
- The gate's state (`Gate`: the passcode check, the cookie and the limits in `api/gate_limits.py`) lives on the `PasscodeGate` instance that `create_app` builds, not on `app.state`. Routes reach it through the request scope (`GateDep`, None while the gate is off).
- The cookie is `URLSafeTimedSerializer(SESSION_SECRET, salt="harmony.gate")` over an HMAC fingerprint of the passcode, valid 14 days; `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` outside `APP_ENV=local`. A new passcode invalidates every cookie.
- Unlock limits, per known client key: 5 attempts per 15 minutes, counted before the comparison; then 429 with `Retry-After` for 15, 30, then 60 minutes; a second attempt in flight gets 429 with `Retry-After: 1`. Each wrong passcode waits for a 0.3 s slot behind one global lock, and more than 100 failures in 15 minutes logs `gate_failures_spike`. Every attempt logs `gate_attempt` with its outcome, the header source and a 12-hex key digest, never the IP or the passcode.
- Headers: while the gate is on, every response carries `X-Robots-Tag: noindex`, gated refusals `Cache-Control: no-store`, `/assets` `private, max-age=31536000, immutable`, and the SPA's HTML `private, no-cache` with `Vary: Cookie`. With `APP_ENV=prod` every response carries `Strict-Transport-Security` (except a 500 written by Starlette's `ServerErrorMiddleware`, which sits outside the gate), and `/api/docs` and `/api/openapi.json` are off.
- Websocket scopes without the cookie are closed with 1008; `lifespan` passes.

| Method and path | Request → response | Semantics |
|---|---|---|
| `GET /api/gate` | → the passcode page (HTML) | Open without the cookie. A constant page: nothing from the request is written into it. |
| `POST /api/gate` | `GateUnlock{passcode, next="/"}` → `GateUnlocked{next}` and the cookie | Open without the cookie. `next` is the validated same-origin path (exactly one leading `/`, no backslash, no control character; anything else becomes `/`). The passcode is trimmed and matched case-sensitively. Errors: 401 `passcode_invalid`; 411 (neither `Content-Length` nor chunked, code `invalid`); 413 `payload_too_large` (over 4 KB, chunked bodies included); 429 `too_many_attempts` with `Retry-After` (seconds). With the gate off it answers 200 `{next}` and sets no cookie. |
| `POST /api/gate/lock` | → 204 | Clears the cookie ("Lock Harmony"). With the gate off it answers 204 too, and the web then shows the passcode page, which any passcode leaves. |

Without the cookie, every other `/api/*` path, and any method but `GET` and `HEAD` anywhere, gets 401 `{detail: "passcode required", code: "passcode_required"}`; any other `GET` or `HEAD` gets the passcode page with status 401. The exceptions are `GET` and `HEAD /api/healthz` and `/api/admin/*` with a valid `X-Admin-Secret` (a wrong or missing secret without the cookie gets the same 401). A body over `MAX_BODY_BYTES` gets 413 `payload_too_large`.

**World creation (3.3, `worlds/service.py`).**
- `POST /api/session/start?world=network` answers 404 `not_found` whether or not the gate is on (P3 is deferred).
- While the gate is on: 5 new sessions per known client key and 200 overall per UTC day, then 429 `too_many_attempts`; 20 Resets or wiped-world re-creates per visitor session per hour, then 429. Gate off, nothing is limited. `POST /api/session/start` and `POST /api/worlds/reset` document these.
- A new session stores `sessions.client_key_hash`, whether or not the gate is on; it is None for an unknown key.
- **Nothing model-driven starts at world creation.** A scenario's seeded open request starts the first time the visitor acts as a member of its requester or a recipient org (for harmony-demo, Northlake or Vantage), once per world: guarded by its `world_workflows` row and a `pg_advisory_xact_lock` on `seeded_requests:{world_id}`. A failed start is logged and doesn't fail act-as. `RequestStarter.start(world_id, request_ids)` is unchanged.

**Settings and env names (`config.py`).**
- `ACCESS_CODE` is the master passcode. `CLIENT_IP_HEADER` is `x-forwarded-for` (the default; the rightmost entry), `true-client-ip` or `cf-connecting-ip`.
- Caps: `LINEAGE_BUDGET_USD` (default 10), `DAILY_BUDGET_USD`, `GLOBAL_BUDGET_USD` and `CLIENT_DAILY_BUDGET_USD` (unset means none, outside prod). `HOSTED_CAPS_IN_DEV` (false) applies the hosted caps to local live calls.
- Worker capacity: `WORKER_MODEL_CONCURRENCY` (4), `WORKER_MODEL_RATE` (2.0 starts per second) and `WORKER_CHAT_CONCURRENCY` (2).
- Prod refuses to boot unless `MODEL_MODE=live`, `MODEL_OVERRIDE` is unset, `ACCESS_CODE` is 16 or more printable ASCII characters with no whitespace at either end, and the daily, global and client caps are set.
- Tests and scripts read the passcode from `E2E_PASSCODE` (Playwright; `web/e2e/unlock.ts`) and `HARMONY_PASSCODE` (the shell and Python checks). `tests/conftest.py`'s `app_settings` pins `access_code=None` unless a test opts in, and `prod_settings(**overrides)` fills every field prod requires.

**Budgets and failures (3.4, 3.5).**
- `BudgetKind` = `session` | `client` | `daily` | `global` | `paused` | `provider` (`db/models/choices.py`): which cap refused a call. `BudgetExhausted.kind` (`llm/errors.py`) takes these values.
- `FailureReason` = `session_budget` | `daily_budget` | `demo_budget` | `paused` | `model_error` | `refused`: why a turn failed or a thread stopped. One enum serves every surface.
- `payloads.BUDGET_FAILURES` maps kind to reason: `session` → `session_budget`; `client` and `daily` → `daily_budget`; `global` and `provider` → `demo_budget`; `paused` → `paused`.
- Across the activity boundary, a refused reservation is a non-retryable `ApplicationError(type=BUDGET_EXHAUSTED_ERROR ("BudgetExhausted"), details=[BudgetExhaustedDetails(kind).model_dump()])`. The workflows read `kind` from the cause's details.
- A refused reservation publishes `demo.notice {kind: "budget", entity_id: null}` in the demo scope. `DemoNotice.kind` is free text, so the event schema doesn't change. The SPA refetches `GET /api/budget` on it.
- `threads.stop_reason` (a `FailureReason`, CHECK `ck_threads_stop_reason`) says why the system stopped a thread. It reaches `ThreadSummary.stop_reason`, `InboxItem.stop_reason`, `NetworkEdge.stop_reason` and `RecipientStatus.stop_reason`; it is status, not content, so `scoped` keeps it.
- `ConversationDetail.turn_failure: FailureReason | None` replaces `turn_failed`, as do `ConversationCarry.turn_failure` and `ConversationPending.turn_failure`. The workflows classify the cause: `BudgetExhausted` by its `kind`; `BadRequest`, `TurnIncomplete` and `ModelCallError` after its retries give `model_error`; an internal turn that stops on `refusal` gives `refused`. The next member message clears it. A thread whose turn is refused closes with `stop_reason` `refused`.
- `ModelCallError` (429, 529, 5xx, a broken stream) is retried 6 times from 5 s, doubling, at most 60 s (`policies.model_retry_policy`).
- `live_controls(key PK, paused, provider_blocked_until?, updated_at)` is the live switch: one row, key `live`, inserted by `0002`. `budget_pg.ensure_hosted_rows(sessionmaker, settings)` runs in the API lifespan and at worker start: it inserts the switch row if missing and upserts the `global` row and today's `daily:` row with the configured caps. `sessions.client_key_hash` replaces `access_code_hash`, and `budget_pg.access_key` is gone.
- **Hosted caps** (`HostedCaps`) apply to live calls in prod, or locally with `HOSTED_CAPS_IN_DEV`. `hosted_budget_keys` gives `lineage:<id>`, `client:<hash>:<date>` (when the session has a hash), `daily:<date>` and `global`, creating missing lineage, client and daily rows; the `global` row comes only from boot, and a missing one refuses as `global`. `HostedBudgets.reserve` reads the switch first (paused, a missing row, or an active provider block refuse before any budget row is locked), then locks every key in key order. When several caps refuse, the widest is named: `paused`, `provider`, `global`, `session`, `daily`, `client`.
- **Provider limit.** `AnthropicModel` maps a 402 `billing_error`, or the workspace usage-limit message, to a non-retryable `BudgetExhausted(kind="provider")` and calls its `on_provider_limit` hook; the worker passes the guard's `block_provider`, which sets `provider_blocked_until` 10 minutes ahead. Resume clears it.
- `EXHAUSTED_BELOW_USD` is $1.10, not the plan's $1.00: it must cover `worst_case_cost` of the Opus answerer at 16,000 `max_tokens` with its fallback hop at a 20,000-token input ($1.08). `remaining_usd` is the cap minus settled spend; in-flight reservations aren't subtracted.
- Settle and release retry three times on database errors, then leave the reservation `reserved` for `settle_stale`, which the worker runs at start and every 10 minutes while hosted caps are on. Each crossing of 50, 75, 90 and 100% of the global or a daily cap logs `hosted_spend_crossed` once, naming the day's top lineage and client hash.

| Method and path | Request → response | Semantics |
|---|---|---|
| `GET /api/budget` | → `BudgetView{live, state: BudgetState, session?: SessionBudget{cap_usd, spent_usd, reserved_usd, remaining_usd}, resets_at?}` | Session route (401 is FastAPI's). `BudgetState` = `ok` \| `session_exhausted` \| `daily_exhausted` \| `demo_exhausted` \| `paused`, by the same widest-first order as `reserve`; a provider block reads `demo_exhausted`. `resets_at` is set while `daily_exhausted`. Missing lineage and client rows count as full caps. With hosted caps off, `{live: false, state: "ok"}`. |
| `GET /api/admin/spend` | → `AdminSpendView{live: LiveState, today: BudgetTotals?, lineages_today, top_lineages: KeySpend[], top_clients: KeySpend[]}` | `BudgetTotals{cap_usd, spent_usd, reserved_usd}`; `KeySpend` adds `key`. |
| `POST /api/admin/live` | `LiveToggle{enabled}` → `LiveState{paused, provider_blocked_until?, global: BudgetTotals?}` | Pause or resume; resuming clears a provider block and never touches a cap. |
| `GET /api/admin/worlds/{world_id}` | → `AdminWorldView{status, retire_workflow?: running \| completed \| failed \| terminated}` | 404 for an unknown world. |
| `GET /api/admin/client-key` | → `ClientKeyView{source, key?, forwarded_for: string[], true_client_ip?, cf_connecting_ip?}` | The caller's own key, for the header check. |

Admin routes take `X-Admin-Secret`, compared with `hmac.compare_digest`. Without the gate's cookie a missing or wrong secret gets the gate's 401 `passcode_required`; with the cookie (or with the gate off), 403 `not_admin`. `POST /api/admin/wipe` (`routes/worlds.py`) answers the same `ApiProblem` 403 `not_admin`. With `ADMIN_SECRET` unset every operator route is refused. Money is a JSON number of US dollars. `retire_workflow` is read with Temporal's `describe()`: a retired world's workflow reads `running` for an hour after the world is marked retired (it waits out `DEFAULT_ROW_TTL` before deleting the rows), then `completed`.

**Send without review (3.10).**
- Storage: `rep_policy.send_without_confirmation` (the org default, false), `conversations.send_without_confirmation` (the chat's override; null follows the default) and `cards.auto` (a card raised in a chat that sends without review).
- `SendMode{override: bool?, org_default, effective}` (`domain/conversations.py`) is `ConversationDetail.send_mode` and the PATCH route's response. `PATCH /api/conversations/{id}` takes `ConversationUpdate{send_without_confirmation: bool | null}`. The key is required, and null means the org default. It is owner-only (404 otherwise) and 409 `world_retired` in a retired world, and it bumps `version` and tells the owner but leaves `last_activity_at` alone.
- `StartConversation.send_without_confirmation?` sets a new chat's override. `SettingsEdit`, `HarmonySettingsView` and `HarmonySettingsUpdate` carry the org default (admins only, no notice).
- Domain: `conversations.start(…, send_without_confirmation=None)`, `set_send_mode(uow, conversation_id, override)`, `send_mode(session, conversation) -> SendMode` and `effective_send_mode(session, conversation) -> bool` (read live; L8 calls it when `send_network_request` runs). `cards.create(…, auto=False)` stores the flag, so nothing sets the column on a returned row.
- **No member action touches an auto card.** `cards.awaiting_member()` (pending and not `auto`) is the one predicate for `cards.pending()`, the Recents `has_pending_card` query and superseding. `cards.resolve` raises `CardNotPending(card, "being sent without review")` unless `resolution.auto`, approve and reject answer 409 `card_not_pending`, and a typed message returns `queued`, not `card_rejected`.
- **Guards, in auto mode only** (`tools/internal.py`), each an error result that stores nothing: only rows marked `suggested` that a search in this chat screened `strong` or `possible` are sent (an unscreened or `no` suggested row refuses the whole call; unsuggested rows are listed but not sent); at most `AUTO_SEND_MAX_RECIPIENTS` = `MAX_ACTIVE_THREADS` (8) recipients; at most 3 auto requests per internal turn; no recipient with a live thread from another request in this chat. A retried call reuses the decision stored on its card. A second `send_network_request` in the same response gets `NOT_RUN_YET` ("Not run yet: requests go out one at a time…").
- The workflow settles an auto card with `CardResolution(…, member_id=owner, auto=True)` through `apply_card_resolution` with mirror options, with no Update, then starts the request and resumes the turn with "Sent without review (this chat's setting): request R to A and B." plus `AFTER_SEND` (round 2's card results, below). If the apply fails with a domain error, it rejects the card the same way and resumes with "Couldn't send: {reason}. Nothing went out." `InternalTurnResult.card_auto` (default false) and `CardRaised.auto` (default false) carry the decision out of the turn, and `OutreachCardPayload.auto_send_recipients` says who it goes to.
- On a new turn whose effective mode differs from the last one Claude was told in this chat, an appended system notice starting `NOTICE_PREFIX` ("Harmony: in this chat, `send_network_request` …") tells it (`temporal/activities/send_mode.py`). The system prompt and tool bytes are the same in either mode.
- `OutreachCardItem.auto` is true for an auto card in every status, and false otherwise.

**Recipients and the card (3.13 E2, E4).**
- `OutreachRecipient` and `CardRecipient` gain `ask?` (what this recipient alone is asked; only its representative sees it), `title?` (its thread's subject, which both sides see) and `suggested?` (internal Claude suggests sending to it; false lists it for the member's decision). `OutreachCardItem` gains `title?` (the request's name for the member).
- `requests.default_recipients(payload)`: the `suggested` rows, else the rows whose fit isn't `no` (cards from before suggestions), else all. The web's default ticks follow the same rule.
- `requests.chosen_recipients(payload, resolution)`: `edits.recipients` when given; else `auto_send_recipients` when `resolution.auto`; else `default_recipients(payload)`. `create_from_card` sends to them, with `OutreachEdits.asks` replacing the card's asks, and stores `title`, `ask` and `suggested` in `network_requests.recipients`.
- **The web always sends `recipients`** on approve. The server's default applies only to an approve with no edits, such as a headless one.
- **`CardRecipient.selected` means "in the request":** every listed row while the card awaits the member, and once it was rejected; the rows the request went to once it was approved (`chosen_recipients`); and an auto card's `auto_send_recipients` while it is still sending. A sent row's `ask` is the one the request stored (the edited one).
- **`RecipientStatus.sent`** is true for a row the request went to, and false for a row that comes only from the card (shown "Not sent").
- `ContextPartKind` gains `ask`, for the requester representative's `<ask>` prompt part (L8).

**Titles (3.13 E7, E13).**
- Storage: `network_requests.title` (the request's name for the requester's member; never shown to a recipient) and `threads.title` (set by `threads.open` from the recipient's `title` only).
- `threads.thread_title(thread, request, org_id, *, requester, opening)`: the thread's own title; else, on the requester's side, `requester_title(request)`; else, on a responder's side, `responder_title(requester=, opening=)`. It replaces `request_title`.
- `threads.requester_title(request)` = `request.title or brief_title(request.brief)`: `RequestStatusView.title`, and the requester's side and the network lens in `NetworkRequestRef.title`.
- `threads.responder_title(*, requester, opening)` = the requester's first message on the thread, else "Request from X".
- **A responder never sees `request.title`.** Its inbox, threads, network (`NetworkRequestRef.title` is its own thread's title) and workspace bodies name the thread by `thread.title` or the fallbacks.
- Seeds: `RequestSpec.title`, `OpenRequestSpec.title` and `RecipientSpec.title`, `ask` and `suggested`. The loader stores them, and a seeded thread's title is its request's title.

**Drafts and routes (3.13 E12).**
- `Route.assignees: RouteAssignee{member: slug, why}[]`. `why` is one short clause addressed to that member. The before-validator maps the old shape (`assignees: [slug]`, `reason`) to `{member, why: reason}`, so stored drafts and cassettes stay valid.
- `ConsultDraft.short_question?`: the decision in one line, for the reviewer. Drafts from before it are null. It is stored in `escalation_drafts.short_question`, and read as `EscalationDraftView.short_question` and `InboxEscalation.short_question` (the current draft's).
- `routing.named_slugs(assignees, slugs) -> {slug: why}`: loose matching, unknown slugs dropped, and the first why given wins.
- `EscalationDraftView.route` is `RouteView{assignees: RouteAssigneeView{slug, why}[]}`, as applied.
- `CONSULT_OUTPUT_SCHEMA` requires `short_question` and `route.assignees: [{member, why}]` (the route's `reason` is gone). The validators still accept the old shape from stored drafts.

**Revise (3.13 E3b), superseded by Edit in round 2.** `revise`, `context.quote_note` and the `<note>` part are gone: the resolve route refuses `revise` with 422, and a member tells Claude what to change on a held message with the `steer` Update, which rewrites the message in place and writes guidance (see [Round 2](#round-2)). `consult.held_result` keeps approve, decline and take over; an approval of a member's edited wording reads "Sent as your side edited it".

**Session (3.13 E6).** `SessionView.internal_model: InternalModelView{id, label, adaptive}` (`auth/views.py`) is what internal Claude runs on for the acting org: "Opus 5.5" or "Haiku 5.5" (else the model id) with `adaptive` true, or "Scripted" with `adaptive` false when `MODEL_MODE=scripted`. `session_view(…, settings)` and `session_response(…, settings)` take the settings.

**The representative's turn (3.13 E1, E1b, E3).**
- `close` is refused while the counterparty has sent no message in this run (`RepToolScope.run_start_seq`, round 2's Reopen); a closed thread still takes the summary-only close.
- An empty `end_turn` after this turn's own tool results ends the turn as `end_turn`; an empty first response, and `max_tokens` with empty content, still raise `TurnIncomplete`.
- `TurnContext.bare_text_sends` and `RepToolScope.bare_text_sends` (both default true) are false when a turn opens with the code-written `NOTHING_NEW` or `THREAD_ENDED` fallback; bare text then stays private and isn't streamed.
- `ThreadWorkflow` ends a thread `expired` after two consecutive turns, one per side, that sent no message, consulted no one and didn't close.
- A representative turn sends `stream.typing` every 5 s for its whole length; the web's typing rows expire after 8 s.

**Queues and models (3.1, 3.7).** `internal_turn` runs on `harmony-chat` (`WORKER_CHAT_CONCURRENCY` slots); `rep_turn` and `answer_consult` on `harmony-model` (`WORKER_MODEL_CONCURRENCY`, `WORKER_MODEL_RATE`). `run_worker` builds its `WorkerConfig` from settings. Background orgs' internal Claude runs on Haiku 5.5, and every org's chat streams. `harmony llm ping [--live] --model opus|haiku` sends the production shapes (streamed featured internal turn; the screener).

**Workflow versioning.** The live phase's and round 2's `ConversationWorkflow`, `NetworkRequestWorkflow` and `ThreadWorkflow` changes carry no `workflow.patched` guard: worlds fork per build, so no deployed workflow resumes on changed code. `tests/workflow/test_replay.py` replays saved histories (`tests/fixtures/histories/`) against today's code: `r2/` only (`RECORDINGS = ("r2",)`), recorded by this build's workflow tests and scripted story after `redraft` and `revise` were removed, and recorded again at integration; it includes a reopened run and a failed reopen attempt. The older `p0_p2/` and `live/` recordings were deleted, because they hold the removed Update and commands that no longer replay. The `ConversationCarry` shim for the old `turn_failed` flag is deleted too; `closed_request_ids` is still read (as round 1) from a carry made before rounds. `HARMONY_SAVE_HISTORIES=<dir>` makes the Temporal test fixtures save every run of these three workflows (`tests/_histories.py`), which is how a recording is made again.

**Defaults that keep old data valid.** Every new response field defaults where null or false is a true value (`title`, `ask`, `suggested`, `auto`, `short_question`, `stop_reason`, `why`). `SessionView.internal_model`, `ConversationDetail.send_mode`, `ConversationDetail.turn_failure` and `RecipientStatus.sent` are required. The tool inputs `SendNetworkRequestInput.title` and `RecipientInput.ask`, `title` and `suggested` are required (the cassettes carry them); each title is 1–60 characters after stripping whitespace. A call with two or more recipients is refused when the brief names a recipient org, or one recipient's ask or title names another (full `Org.name`, case-insensitive, word boundaries). Asks a member edits on the card aren't checked: they are the member's own words. `test_story_scripted` also plays the story with the answerers' cassettes as P0–P2 recorded them (`tests/fixtures/cassettes_p0_p2/`): routes naming member slugs with one shared `reason`, and no `short_question`. A send without `title`, `ask` or `suggested` is refused, so the chat's cassette is today's in both runs.

**`0002_live_demo`.** It drops `sessions.access_code_hash`; adds `sessions.client_key_hash`, `threads.stop_reason` (with its CHECK), `network_requests.title`, `threads.title`, `escalation_drafts.short_question`, `rep_policy.send_without_confirmation`, `conversations.send_without_confirmation` and `cards.auto`; and creates `live_controls` with its `live` row. Its downgrade reverses all of it.

**The card's heading (3.12).** User-facing text says "Harmony Network Request" (plural "Harmony Network Requests"). The `ToolCard` `<h3>` of the outreach card, the request status card and the auto card stays exactly "Harmony Network Request", so the region's accessible name never changes. The request's own `title` renders as the first line of the card body, outside the h3. Code identifiers and API paths keep their names.

**Web pieces for later owners.**
- `web/src/app/budget.ts`: `useBudget()` over `GET /api/budget` (query key `queryKeys.budget` = `["budget"]`), and the types `BudgetView` and `BudgetState`. It runs once there is a session, polls every 30 s while the tab is visible, and refetches on `demo.notice {kind: "budget"}`.
- `web/src/chat/failureCopy.ts`: `failureLine(reason: FailureReason, state: BudgetState): string` (the 3.5 copy), `budgetLine(state): string | null` (the demo bar's and composer's state line) and `useBudgetState()`, and the type `FailureReason`. The demo bar, Demo controls, chat, request view and network view all read their copy here.
- `web/src/shell/ui/ClampedText.tsx` is gone (round 2, decision 8): long briefs, asks and messages show in full.
- `web/src/test/shellHarness.tsx`: `NEW_WORLD_ID`, `Page`, `testRoutes`, `Gate`, `FakeServer`, `renderShell`, `renderLive`, `openAccountMenu`, `demoBar` and `watchFor`. Later owners may add exports but keep these signatures.
- `web/e2e/unlock.ts`: `unlock(page: Page): Promise<void>` posts `E2E_PASSCODE` to `/api/gate` through `page.request`, and fails on a non-200. `playwright.config.ts` refuses to start without `E2E_PASSCODE`, and `scripts/e2e_ui.sh` exports it.
- Lock handling (`web/src/api/client.ts`, `app/lock.ts`): a 401 whose code is `passcode_required` throws `PasscodeRequired` or calls the dispatcher's `lock()`, which stops retries, clears the query cache, alerts and toasts, posts `{type: "locked"}` to other tabs (they reload; it is the only message that still reloads a tab, round 2) and goes to `/api/gate?next=<path>`. Any other 401 still shows the Landing.
- `web/src/fixtures/index.ts`: `Method` includes `"patch"`.

## Round 2

Feedback round 2's shapes ([FEEDBACK_R2_PLAN.md](FEEDBACK_R2_PLAN.md) §1, which fixed the names; [EDIT.md](design/EDIT.md) and [REOPEN.md](design/REOPEN.md) for the behaviour), as built. Where the designs and the code differ, the code wins and the plan's status records it.

**Schema.**
- `0003_edit_guidance`: `steers(thread_id, org_id, side, seq, kind: edit | reopen, author_member_id, body, escalation_id?, draft_text?, draft_rev?, status: working | done | failed, guidance_outcome?, guidance_version?, draft_outcome?, failure_reason?, version)` and `rep_guidance(thread_id, org_id, side, version, text, steer_id, created_by_member_id)`, append-only (an empty `text` withdraws the guidance).
- `0004_reopen`: `threads.run` (1), `end_reason`, `ended_at`, `closed_by`, `run_finished_at`; `thread_runs(thread_id, run, started_at, deadline_at, max_turns, reopened_by_member_id?, reopened_by_org_id?, reopened_side?, steer_id?, start_seq, state?, end_reason?, closed_by?, stop_reason?, ended_at?, requester_summary?, responder_summary?, carry?, finished_at?)`; `network_requests.round` (1) and `workflow_done_at`. Both migrations are additive, with downgrades. Seeded terminal threads get `end_reason` and `ended_at` from the loader and no `thread_runs` row; `open_thread` writes run 1's row.
- Literals: `EndReason` = `closed` | `quiet` | `unsent` | `turn_limit` | `deadline` | `stopped`; `ReopenBlock` = `history` | `unfinished` | `not_asked` | `limit` | `refused` | `they_closed`; `SteerKind`, `SteerStatus`, `SteerGuidanceOutcome` (`updated` | `unchanged` | `rejected` | `failed`), `SteerDraftOutcome` (`rewritten` | `settled` | `failed`), `SteerFailure` (a `FailureReason`, `added_details`, `invalid`, `thread_closed`, `unchanged` or `reopen_failed`); `ModelRole` adds `steer`.

**Turn rules (decision 1, EDIT.md §3.6 and §3.7).** In `agents/tools/rep.py`:
- `CONSULT_AFTER_SEND`: "Not asked: your message already went out, so it's {counterparty}'s move. Don't close. End your turn now: stop calling tools. Ask your side at the start of your next turn."
- `NOT_REPLIED`: "{counterparty} hasn't replied yet. To wait for their reply, end your turn: stop calling tools. `close` ends the conversation." `CLOSE_TOOL` begins "End the conversation for both sides once it's settled or clearly going nowhere." Tool text is frozen per thread side, so threads started before this build keep their old wording.
- A steer recorded after a turn started (`steers.max_seq(...) > RepToolScope.steer_seq`): before the turn has sent, `send_message` is held with `STEERED_HOLD` ("Claude wrote this before your side's latest edit reached it."); after it has sent, a send is refused with `STEERED_NOT_SENT` and a close with `STEERED_NOT_CLOSED`. In a turn that opened with guidance (`RepToolScope.guided`), a bare text reply is held with `GUIDED_TEXT_HOLD` ("Claude answered in plain text just after your side's edit reached it, so a teammate checks it first."). Held items store their reason as rev 1's `needs_human_reason`. `CUT_OFF_CONSULT` and `CUT_OFF_HELD` are the results a consult or held message is owed when the run ends first.

**Edit (steer).**
- `POST /api/threads/{id}/steers` takes `AddSteer{client_key, as_member_id, body (1..STEER_BODY_MAX_CHARS = 4000), escalation_id?, draft_text? (≤ 20000)}` and answers 202 `SteerStarted{steer_id, escalation_id?, draft_rev?}` (`draft_rev` is the rev the waiting item is being rewritten as). Errors, in order: 409 `member_changed` (with `member`); 422 `invalid` for a blank body; 404 (the thread, or an `escalation_id` that isn't the actor's org's item on it); 403 `not_in_org`; 409 `steer_running` (with `by`), `thread_closed` and `world_retired`.
- The API runs the Update `steer(SteerRequest{steer_id, side, org_id, member_id, client_key, body, escalation_id?, draft_text?})` with Update id `SteerRequest.update_id` (`steer:{steer_id}:{body digest}`); `steers.steer_id_for(world, org, member, client_key)` makes a retried POST the same steer, and the route answers 422 `invalid` for a key already stored with other text. The validator refuses with `SteerRunning` (`details=[{by_member_id}]`) while that side's last steer runs, `ThreadClosed` on an ended thread or before a reopen begins, and `SteerConflict` (422) for a reused key with different text.
- The workflow: `record_steer` (`steers.record`, at the next per-side `seq`), then in parallel `write_guidance` and, while the item it names is still open, its rewrite: a consult draft is redrafted by `answer_consult` with the instruction, a held message by `rewrite_held` in place. Then `settle_steer`, which decides the status from the rows the parts wrote. The side's next turn waits for the steer, and an Edit never starts a turn.
- `write_guidance(SteerPartInput) -> GuidanceWritten`: role `steer`, step 0, from `prompts/steer_guidance.md`, with only members' words (the instruction and the current guidance) and code-written names. `parse_guidance` decides `rejected` (a refusal, unusable output, over `steers.GUIDANCE_MAX_CHARS` = 2000, or `steers.added_details`: a number, date, email, link or name none of those words hold), `unchanged`, or `updated` (`steers.add_guidance` writes the next version).
- `rewrite_held(SteerPartInput) -> int | None`: role `steer`, step 1, no tools, from `prompts/held_rewrite.md`; the new draft rev, which a person approves.
- `answer_inputs` reads `steers.current_guidance_quoted` (or "None.") under "# What your teammates told the representative"; the notes section and `_notes_text` are gone, so no comment reaches any model.
- **Reads.** `ThreadWorkspace.steers: SteerView{id, kind, author, body, created_at, status, escalation_id?, draft_rev?, guidance_outcome?, guidance_version?, draft_outcome?, failure_reason?, version}[]`; `ThreadWorkspace.guidance: GuidanceView{version, text, created_at, steer_id, by, delivered_at?}?` (the current version, and when its block reached the representative); `RepresentativeContext.guidance: DeliveredGuidance[]`; `EscalationDraftView.rewritten_by: RewrittenBy{steer_id, author}?`.
- **Events.** `steer` (versioned) and `rep_guidance` (append-only) snapshots go to the steering org's thread scope only (the steer's without `draft_text`), and never to the network view's stream (`LENS_HIDDEN_ENTITY_TYPES`). `escalation.redraft_failed` is gone.

**Reopen.**
- `POST /api/threads/{id}/reopen` takes `ReopenRequest{client_key, as_member_id, body (1..4000, trimmed)}` and answers 202 `ReopenStarted{steer_id, thread_id}`. Errors, in order: 409 `member_changed`; 404; 403 `not_in_org`; 409 `thread_open` (with `reopened_by: ReopenedBy{org, member?}`, `member` only for the same org), `reopen_in_progress`, `closing_in_progress`, `not_reopenable` (with `block`) and `world_retired`. The same member with the same key, or a different key and the same text, gets the first steer's 202.
- `temporal/reopen.start_reopen` records the steer (kind `reopen`, `working`) and starts `ThreadWorkflow` again under the same id with `ThreadInput{run, carry: ThreadCarry{turns, delivered, consults, owed}, reopen: ReopenInput{steer_id, side, org_id, member_id, body, seq, from_state}, guidance, steer_seq}`, a fresh `max_turns` (carried + 16) and deadline, and the memo `reopen_steer_id` (`MEMO_STEER`).
- The run: `record_reopen`, `write_guidance` (kind `reopen`), then `begin_reopen` only when the guidance was `updated` (otherwise `settle_steer` with the reason, and the run returns the old state), then the turn loop from the reopening side, then the tail: `end_round` for a run after the first (and `notify_conversation` with `NetworkUpdate.round` when it closed the request), and `finish_run`, always last.
- `domain/reopens.py`: `reopen_offer(thread, runs, side, *, requester_wrote, now) -> ReopenOffer{state: ReopenOfferState (allowed | wrapping_up | blocked), block?} | None`, `MAX_REOPENS` = 2, `WRAP_UP_GRACE` = 10 min, `begin`, `finish_run` and `end_line` (the reason the guidance writer is told).
- `ThreadStateInput` gains `end_reason`, `closed_by` and `last_turn`. The `closed` notice is queued by `set_thread_state` only when the call that ends the thread is a last turn's refusal (`last_turn` with `closed_by`); a `close` queues its own.
- `requests.end_round(uow, request_id, *, from_workflow) -> RoundEnd{closed, round, conversation_id}` closes a request only when its workflow is done (`workflow_done_at`) and no thread is live. The request workflow's tail calls it with `from_workflow=True`; `set_request_status` is gone.
- **Reads.** `ThreadSummary` gains `run`, `end_reason`, `ended_at`, `closed_by` and `run_finished_at`. `ThreadDetail` gains `reopen: ReopenOffer?` (per viewer) and `runs: ThreadRunView{run, started_at, reopened: RunReopened{org, side, by?, steer_id?}?, ended: RunEnded{state, end_reason, closed_by, stop_reason, at}?}[]` (the counterparty sees `by` and `steer_id` null). `ThreadWorkspace.earlier_summaries: EarlierSummary{run, text}[]` holds the actor's side's archived summaries. `NetworkEdge` gains `end_reason` and `closed_by` (parties only).
- **The conversation.** `ConversationWorkflow` takes each (request, round) once: `ConversationCarry.closed_request_rounds` replaces `closed_request_ids`, which is still read as round 1, and `undelivered_updates` carries `closed` updates a turn took but never stored.

**Card results (FEEDBACK_R2_PLAN §1.8, `agents/tools/internal.py`).** The `send_network_request` result once its card is settled:
- approved: "Approved and sent: request R to A and B." or, in a chat that sends without review, "Sent without review (this chat's setting): request R to …", each followed by `AFTER_SEND`: "Their progress shows on the request's card in this chat, and anything that needs {member} will come up here as a question; you'll be told here when every conversation has ended. There's nothing to check now: in a sentence, tell {member} it's out and stop.";
- the card's Decline: "Declined: {member} decided not to send this request[, and said: {reason}]" then "Nothing went out. Don't propose it again unless {member} asks.";
- a message typed while the card waited: "rejected: <message>";
- an auto card that couldn't be sent: "Couldn't send: {reason}. Nothing went out." (an error result).

`OutreachCardItem.rejected_with` is `decline` | `message`, or null: older rejected rows read `message`, and a card an automatic send rejected gives null.

**Tool rows (`domain/conversations.py`).** A tool row shows a present-tense label while it runs ("Searching the Harmony network…", "Searching your organization's knowledge", "Reading a document", "Checking on the request"), then the past-tense label with its outcome after "·": "1 strong, 2 possible of 14", "2 possible fits of 14", "No fits among 14"; "3 matches", "1 match", "No matches". The need or query is the row's detail, on a second line.

**Switching and not-found.**
- Every write from the request view (`/notes`, `/steers`, `/reopen`) carries `as_member_id`; a mismatch with the session's member is 409 `member_changed` with `member`, checked first, and writes nothing.
- `GET /api/demo/whose?chat=<id>` or `?thread=<id>` → `DemoWhose{kind: chat | thread, orgs: DemoWhoseOrg{org, people: DemoPerson{member_id, name}[]}[]}`: for ids in the session's own live world only. A chat names its owner; a thread its requester's then its responder's people who can open it (admins, then `thread_access` rows), at most 3 (`WHOSE_MAX_PEOPLE`). Any other id is 404, and neither or both parameters 422.
- Tabs share `{type: "member_changed", sessionId, epoch, memberId}` and `{type: "locked"}` on the `BroadcastChannel`; `dispatcher.onMemberChanged(listener)` is new.

**`harmony run`.** `--edit ORG:COUNTERPARTY:N:TEXT` (repeatable; TEXT may hold colons) has a member of ORG tell its Claude TEXT, an Edit, once ORG's thread with COUNTERPARTY has N messages. The report adds the engine checks `consults_after_send`, `closes_after_refused_consult` (in the same turn), `status_checks_after_send` and, with `--edit`, `guidance_followed`: each Edit's guidance and the first message its side sent after it, flagged when no guidance was written, nothing was sent, or the message mentions it.
