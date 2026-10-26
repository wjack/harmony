# P1 (engine) and P2 (desktop app): build spec

Builds on P0 (see [P0_PLAN.md](P0_PLAN.md)). The design is [ARCHITECTURE.md](ARCHITECTURE.md) (§n references point there), and the UI follows [UI_REFERENCE.md](UI_REFERENCE.md). P0's conventions still apply: tool paths, secrets, shared-file ownership, test isolation and style. Where this spec and the architecture disagree, the architecture wins; flag the conflict.

**Revised for the unstructured revision** ([REVISION_UNSTRUCTURED.md](REVISION_UNSTRUCTURED.md), folded into ARCHITECTURE.md §18). The tasks below (C1, E1–E5, U1–U4) built P1 and P2 first. The rework that brought the code to the revised design was split by owner (R-agents, R-domain-api, R-temporal, R-scenario, R-ui-settings, R-ui-chat, R-docs), and [P1_P2_CONTRACT.md](P1_P2_CONTRACT.md) holds the current shapes and the file ownership. This spec describes what each area must do under the revision.

**Spend.** Everything here runs on `ScriptedModel` by default. Only the coordinator runs live calls (`MODEL_MODE=live`), within the $200 dev budget. No task agent sets `MODEL_MODE=live`.

## Exit criteria

**P1:**
- `harmony run harmony-demo --story suggested` plays the whole intended story (§11.5) headless on `ScriptedModel` and prints transcripts. Specifically:
  - the outreach approval card is approved;
  - Ridgeline's question reaches Alex inline;
  - Northlake's consult fails the sharing check and reaches Helen, routed with a reason;
  - Helen approves;
  - Alex gives the go-ahead, and both threads close with a summary on each side.
- The same command with `--live` works on real models. The coordinator runs it 5 times and records how often each step of the story happens, the spend and the latency per turn.
- Firewall property tests, release tests, routing tests and prompt-shape tests (§15) pass.

**P2:**
- With API, worker and web running on `ScriptedModel`, a person can do both sides of the suggested request in the browser:
  - as Alex: send, approve, answer, give the go-ahead;
  - as Helen: approve the routed item;
  - see who needs a human in the network view.
- A Playwright smoke test does the same in CI.

## Shared contract (task C1, first)

One agent writes the contract that every later task codes against. It's thin but complete: signatures, Pydantic models, Protocols and stubs that raise `NotImplementedError` or return 501. [P1_P2_CONTRACT.md](P1_P2_CONTRACT.md) is its current form.

### Schema (`0001_initial`)

Bring the schema to §4. Nothing is deployed, so changes fold into `migrations/versions/0001_initial.py` rather than a new migration, and local dev databases are recreated. Beyond P0's tables:
- **`rep_transcript`:** `(world_id, thread_id, side, seq)` PK; columns `role` and `content_text` (raw wire JSON, text).
- **`chat_messages.content_text`:** raw wire JSON, text.
- **`cards`:** approval cards in a chat. Columns:
  - `conversation_id`, `turn_id`, `tool_use_id`;
  - `kind` (`outreach`);
  - `payload` (json);
  - `status` (`pending` | `approved` | `rejected` | `superseded`);
  - `resolution` (json), `version`.
- **`network_requests`:** `card_id`, `brief` (free text), `recipients` (json), `version`.
- **`threads`:** `turn_count`, `version`.
- **`trusted_organizations`:** per §4.
- **`consults`:** the draft fields, the sharing check, the released text and who released it, per §4.
- **`rep_sessions.close_summary`.**

`alembic check` reports no drift.

### Domain services (`src/harmony/domain/`)

One module per aggregate. Each function takes a `UnitOfWork` and emits `entity.snapshot` events:

| Module | Functions |
|---|---|
| `conversations.py` | `start`, `append_member_message`, `append_assistant_content`, `chat_items(conv)` (the UI view model, below) |
| `cards.py` | `create`, `resolve` |
| `requests.py` | `create_from_card`, `status(request)` |
| `threads.py` | `open`, `append_message`, `set_state` |
| `consults.py` | `open`, `release`, `mark_needs_human`, `activity` (what the answerer looked at) |
| `escalations.py` | `open`, `add_draft`, `resolve`, `reassign` |
| `routing.py` | `apply_route` |
| `notes.py` | (internal notes) |

### Agent layer (`src/harmony/agents/`)

| Module | Contents |
|---|---|
| `runtime.py` | `run_turn(ctx: TurnContext, executor: ToolExecutor, model: ModelClient) -> TurnOutcome`. This is the checkpointed loop (§5). |
| `context.py` | `build_rep_context(...)`, `freeze_rep_session(...)`, `flush_notices(...)`, `context_parts(...)` (what the representative saw) |
| `transcript.py` | Load and append raw wire JSON for rep sessions and conversations |
| `prompts/` | Templates for each role (`internal.md`, `representative.md`, `consult.md`, `screener.md`, `synth.md`) and a renderer. The templates are plain text with `{placeholders}`, versioned in git. |
| `tools/internal.py`, `tools/rep.py` | JSON schemas plus executors |
| `consult.py` | The answerer's tool loop and the sharing check; the `ConsultDraft` and `SharingCheck` models |
| `release.py` | `mode_for`, `needs_sharing_check`, `decide(mode, draft, sharing) -> Release \| NeedsHuman(reason, detail)`, `why_needed` |
| `discovery.py` | `DiscoveryStrategy` Protocol and `AgentScreen` |
| `synth.py` | The synthesizer, for P3. Stub it in P1. |

### Temporal (`temporal/workflows/` and `temporal/activities/`)

**Workflows.** IDs per §7, with the inputs, signals, Updates and queries named:
- **`ConversationWorkflow`:**
  - signals `member_message(text)` and `network_update(request_id, kind)`, where `kind` is `closed` (an inline question refreshes the chat from its escalation's commit, not through the workflow);
  - Update `resolve_card(card_id, decision, edits|feedback)`;
  - query `pending()`.
- **`NetworkRequestWorkflow`:**
  - signal `thread_changed(thread_id, state)`;
  - query `status()`.
- **`ThreadWorkflow`:**
  - Updates `resolve_escalation(escalation_id, action, text?, draft_rev?)`, `reassign(escalation_id, member_id)` and `redraft(escalation_id, instruction)`;
  - query `state()`.

**Activities:**
- on `harmony-model`: `internal_turn`, `rep_turn`, `answer_consult` (the answerer's loop, then the sharing check);
- on `harmony-main`: `apply_card_resolution`, `start_network_request`, `open_thread`, `apply_resolution`, `reassign_escalation`, `set_thread_state`, `set_request_status`, `notify_conversation`.

### API (`api/routes/`)

Pydantic models for every request and response, so `make gen-api` regenerates the web types. Endpoints:

| Method and path | Purpose |
|---|---|
| `GET /api/conversations` | Sidebar recents |
| `POST /api/conversations {text}` | Start a chat; returns `{id}` |
| `GET /api/conversations/{id}` | `{items: ChatItem[], cursor}` |
| `POST /api/conversations/{id}/messages {text}` | A member message. If a card is pending, it rejects the card with this text as feedback, Claude Code style. |
| `POST /api/cards/{id}/approve {recipients?, brief?}` | Approve the outreach card, with the member's brief and a subset of the proposed recipients |
| `POST /api/cards/{id}/reject {feedback}` | Reject a card with feedback |
| `GET /api/requests/{id}` | Per-recipient status, latest line and our side's close summary |
| `GET /api/inbox` | `{needs_you, with_your_team, claude_handling, shared_with_you, done}` for the acting member |
| `GET /api/threads/{id}/workspace` | Own side only: consults, escalations with their drafts and what Claude looked at, notes, "What our representative saw" and the close summary |
| `POST /api/escalations/{id}/resolve {action, text?, draft_rev?}` | Approve & send (with `text`, Edit), Decline or Take over |
| `POST /api/escalations/{id}/reassign {member_id}` | Reassign an inbox item |
| `POST /api/escalations/{id}/redraft {instruction}` | Ask Claude to redraft |
| `POST /api/threads/{id}/notes {body}` | Add an internal note |
| `GET /api/orgs/{slug}/harmony` | Public summary, instructions, sharing description, autonomy, trusted organizations, directory visibility, routing prompt, default assignee, and members with role and profile |
| `PUT /api/orgs/{slug}/harmony` | Admin only; policy is live, prompt settings are versioned |
| `POST`, `PUT`, `DELETE /api/orgs/{slug}/trusted-organizations[/{counterparty_org_id}]` | Admin only; add, edit or remove a trusted organization |
| `GET /api/network?request=<id?>` | `{nodes: [{org, participant, waiting_on?: {member, since}, suggested}], edges: [{thread, state, latest_line}], counts}` |
| `GET /api/demo/story` | `{suggested_message, suggested_recipients: [slug]}`, with dates templated from the world clock |

**`ChatItem`** is a discriminated union:
- `user_text`, `assistant_text` (markdown), `thinking_summary`;
- `tool_activity` (label, detail, status);
- `outreach_card` (card_id, status, brief, recipients with fit and reason, screened counts);
- `request_status` (request_id);
- `inline_question` (escalation_id, question, from org).

Every response that a stream follows carries `X-Harmony-Cursor`.

### Events

- Every new aggregate goes into `entity.snapshot`, `trusted_organization` included.
- Add `demo.needs_human` `{org_slug, member_slug, entity_id}` with no payload, for cross-account alerts.
- Token deltas for conversations reuse `stream.delta`, keyed by conversation.

### ScriptedModel cassettes

The cassette format already keys on (scenario, org, role, thread, turn, step). The role names are `internal`, `representative`, `answerer`, `sharing_check` and `screener`, plus the convention for conversation turns. The answerer is a tool loop, so its steps are blocked per draft rev; [P1_P2_CONTRACT.md](P1_P2_CONTRACT.md) has the key scheme.

**C1 acceptance:**
- `make migrate`, `make lint typecheck test` and `make gen-api` pass.
- Every new route returns 501 with a typed body.
- `docs/P1_P2_CONTRACT.md` lists every Protocol and endpoint with one line on its semantics.

## P1 tasks (parallel after C1)

### E1 — Runtime, context, transcripts, notices

Owns `agents/runtime.py`, `context.py`, `transcript.py` and `prompts/*`.

The coordinator has already drafted the prompts in `src/harmony/agents/prompts/`: `representative.md`, `side_requester.md`, `side_responder.md`, `consult.md`, `internal.md` and `screener.md`. Keep their substance. Wire the placeholders, add the renderer and tests, and report any change you make to the wording.

**`run_turn`:**
- It loops over `call_model_checkpointed(turn_id, step_idx, request)` (P0).
- It appends the response bytes to the transcript and executes tool calls through the executor.
- Tool results go back as a `user` message with `tool_result` blocks.
- The turn ends when the model stops calling tools, or when a tool stops it: `consult` until the answer is released, `close` for good, and `send_message` only when *Ask* holds the message. A turn may send several messages.
- Maximum 8 steps per turn.
- `disable_parallel_tool_use` is set on representatives.

**Context:**
- The rep session's system prompt and tools are rendered and frozen at the side's first turn.
- The system prompt contains:
  - the public summary;
  - the instructions;
  - the sharing description(s) that apply to this counterparty;
  - the brief (requester side);
  - the counterparty's verified identity;
  - the rules about the delimiter and identity.
- No knowledge item or document is ever rendered into it.
- Counterparty bodies are wrapped in a code delimiter, and identity-shaped lines are escaped.
- Pending notices (`settings`, `mode`, `sharing`, `world_date`, `closed`) are flushed as one system message after the user message.
- "What our representative saw" is read back from the frozen bytes (`context_parts`), plus the notices and released answers a stored call carried (`delivered_notices`, `received_results`).

**Internal Claude's prompt:**
- org context, the member's name and role, the world date (as a notice);
- the instruction to use `search_network` before `send_network_request`.

**Tests:**
- Hypothesis firewall properties over the serialized request bytes (§15);
- prefix-extension and system-placement checks via P0's `llm/contract.py`;
- delimiter escaping;
- the notices flush.

### E2 — Tools, consult, sharing check, release, routing

Owns `agents/tools/*`, `agents/consult.py`, `release.py`, `domain/routing.py`, `domain/consults.py` and `domain/escalations.py`.

**Representative tools** (§5): `send_message(text)`, `consult(question)` and `close(summary)`, the same for every org. Under *Ask*, `send_message` and `close` are held as an `outbound_review`. `close` stores the summary on the closer's side only.

**Internal tools:** `search_org_knowledge`, `read_document`, `search_network(need)` (calls E3), `send_network_request(brief, recipients)` (creates a pending card; the turn pauses), `get_request_status`.

**The consult answerer:**
- internal Claude for the consulting org, run as a short tool loop over `search_org_knowledge` and `read_document`, ending in a structured draft via `output_config.format`;
- its inputs are the org's context, member profiles, the routing prompt, notes, the thread as quoted data, the question (quoted), the counterparty and the sharing description(s) that apply;
- validate the output, and route to the default assignee on refusal or invalid output.

**The sharing check** (§2.1): one small call that sees only the applicable sharing description(s), the question and the draft, never the org's records, and returns `within_sharing` and a reason. It runs in *Auto* on drafts that don't need a person. A check that can't run counts as failed.

**Release by mode** (§2.3): *Ask* never releases without a person; *Auto* releases when no person is needed and the sharing check passes; *Skip* releases when no person is needed. A member's approval releases the answer to that one thread.

**Routing** (§10):
- `apply_route` validates assignees against members and falls back to the default assignee;
- it writes `thread_access`;
- reassign moves the item and its access, and records nothing else.

**Tests:** the §15 release and routing tests.

### E3 — Discovery

Owns `agents/discovery.py` and `prompts/screener.md`.

- **`AgentScreen`:** SQL hard filters (directory visibility and communities), then one screener call per org, in parallel (concurrency capped), on that org's public summary and review statistics. Each verdict is a fit and a one-line reason. Verdicts go to `screenings`, first write wins.
- **Ranking** in code: fit tier, then reputation, plus one exploration slot.
- **The shortlist** handed to internal Claude has the fit, the rank, and quoted, truncated reasons.

**Tests:**
- ranking determinism;
- a malicious summary can't change another org's verdict (scripted);
- a retry re-screens only missing orgs.

### E4 — Workflows, activities, API actions

Owns `temporal/workflows/{conversation,network_request,thread}.py`, `temporal/activities/*`, and the implementations of C1's routes in `api/routes/{conversations,cards,requests,inbox,escalations,threads,orgs,network,demo}.py`.

**`ConversationWorkflow`** (§7):
- It drains pending work into one turn, with member messages first.
- A pending card pauses the turn:
  - the `internal_turn` activity returns `AwaitingCard`;
  - the workflow waits for the `resolve_card` Update;
  - then `internal_turn` continues with the tool result: "approved; request R sent to …" or "rejected: <feedback>".
- An inline question sends `network_update`. Only `closed`, sent once every thread of the request has ended, wakes a model turn, with a placeholder user turn plus a system note. Internal Claude then summarizes in prose.
- It continues-as-new when idle.

**`NetworkRequestWorkflow`:**
- starts one `ThreadWorkflow` per recipient, through a sliding window;
- once every thread has ended, sets the request `closed` and notifies the conversation.

**`ThreadWorkflow`:**
- The requester's representative opens.
- A side's turn is a tool loop. When it ends, the other side moves, receiving every message sent since its last turn.
- `consult` runs `answer_consult`:
  - if released, the same side's next turn opens with the answer as the tool result;
  - otherwise it opens an escalation and waits for the Update.
- Under *Ask*, a held message waits for the Update the same way.
- `close` ends the thread for both sides. The other side gets a `closed` notice and one last turn.
- 16-turn cap, and a deadline that bounds every wait, a person's included. The thread ends `expired` at either.

**Tests:** time-skipping workflow tests with stubbed activities (§15), plus one e2e of the full story on `ScriptedModel` against Compose Temporal.

### E5 — Scenario content, story, cassettes, `harmony run`

Owns `scenarios/harmony-demo/**`, `tests/fixtures/cassettes/story_*.yaml` and `cli_run.py`.

**Content comes from [SCENARIO.md](SCENARIO.md).** Write it as YAML: the featured orgs in full, the 10 background orgs, and `story.yaml` with the suggested message and recipients. Keep the facts the story depends on exactly as written there:
- Henderson's records have no seal material;
- Northlake's public calendar starts Nov 10, and Raj's reserve block (Nov 4) is not covered by Northlake's sharing description;
- the prices;
- the sharing descriptions;
- the routing prompts.

**Cassettes** for the whole story (every role, every turn), so `harmony run harmony-demo --story suggested` and the P2 UI run end to end for free.

**`harmony run <scenario> [--story suggested] [--live] [--as alex]`:**
- seeds a world;
- starts the chat with the suggested message;
- auto-approves cards and auto-resolves escalations with the drafts, unless `--interactive`;
- prints each transcript and a spend summary (`harmony spend --new-alerts`).

## P2 tasks (parallel after C1; on `ScriptedModel`)

Every UI task follows UI_REFERENCE.md: tokens, fonts (Source Serif 4, Inter, JetBrains Mono via Google Fonts), spacing and copy. Each task owns its directory under `web/src/`.

### U1 — Theme and shell (`web/src/theme`, `web/src/shell`, `web/src/demo`)

- **Tokens:** CSS variables per UI_REFERENCE §1, with light mode; dark mode only if it falls out of the tokens.
- **Sidebar** (§2): "Claude" wordmark, nav, Requests badge, Network, Recents from `/api/conversations`, collapse to a rail.
- **Account menu** (§5), with the Switch account group (from `/api/members`), Demo controls and Reset demo.
- **Demo bar:** acting account, world date, budget, Reset, cross-account chips from `demo.needs_human`.
- **Demo controls modal** (§9) and the not-available dialog with `useUnavailable()`.
- **Toasts.**
- **Routes and layout** per §12.

### U2 — Chat (`web/src/chat`)

- **New chat** (§3): greeting by time of day and first name, composer, `+` menu (unavailable items), model label "Opus 5.5", the suggestion chip from `/api/demo/story`.
- **Chat view** (§4):
  - user pills and serif assistant markdown;
  - streaming from `stream.delta` with the spark indicator;
  - tool activity lines;
  - the action row.
- **Cards:**
  - the **outreach approval** as the ask-user card above the composer: the free-text brief (editable) and the recipients with their fit; options Send to all, Send to a subset, Edit brief…, and a "Something else" free-text row that rejects with feedback;
  - the request status card (per-recipient status with the latest line or our side's closing summary, live);
  - the inline question card (answer box → resolve).

### U3 — Requests inbox (`web/src/requests`)

The three-pane inbox per UI_REFERENCE §7:
- **List:** groups Needs you, Claude is handling, Shared with you, Done.
- **Thread view:** transcript, consult and release rows, internal notes.
- **Escalation panel:**
  - the question (or the held message under *Ask*) and "Why this needs you";
  - the routing reason and Reassign (member picker);
  - the editable draft answer;
  - what Claude looked at (activity lines);
  - Approve & send, Decline, Take over, and "Tell Claude what to do instead" (redraft);
  - a link to edit what the org shares with that organization.
- **Properties rail.**
- **"What our representative saw" drawer:** the parts of the frozen prompt, the notices sent since, and the released answers.
- Share with a teammate.

### U4 — Network view and Harmony settings (`web/src/network`, `web/src/customize`)

**Network view** (§8 and ARCHITECTURE §11.6):
- React Flow on a dotted canvas, filling it at a readable zoom;
- org nodes with status pills (Screened, Messaged, Replied, Needs human, Closed), and suggested-recipient rings;
- animated edges labeled with the latest line;
- a side panel with threads, our own close summaries and "Act as <member>";
- a header with the request picker, counts and a world switch.

**Customize → Harmony** (§6):
- Public profile: the summary and directory visibility;
- Instructions;
- Sharing: the default sharing description and the trusted organizations, each with a mode select ("Same as default", Ask, Auto, Skip) and an "Also share with them" description;
- Autonomy: the default mode, each option explained by when a person gets involved;
- Team & routing: the default assignee, the routing prompt, and members with role and profile;
- admin-only editing.

### P2 integration

- A Playwright smoke test of the suggested request on `ScriptedModel` against the containerized app.
- Vitest for the dispatcher-driven pieces (cards update live, badges, act-as reset).
- Screenshots of the main screens saved to `docs/screenshots/`. The `final-*` set walks the suggested story after the unstructured rework; shots of removed UI are gone.

## Order of work

1. C1 (one agent).
2. E1–E5 and U1–U4 in parallel, in two workflows (engine and UI), each under 10 agents.
3. Integration:
   - wire and fix until `harmony run … --story suggested` passes on `ScriptedModel`;
   - P2 integration;
   - a review-and-fix pass (lenses: firewall and release, workflows and idempotency, realtime and access, UI fidelity).
4. Live checks: the coordinator runs the story on real models, tunes prompts, and records `--live` runs for fallbacks.
