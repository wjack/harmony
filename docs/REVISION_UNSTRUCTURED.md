# Revision: let the agents work unstructured

**Folded in (2026-10-26):** this revision is now part of [ARCHITECTURE.md](ARCHITECTURE.md) (recorded in §18), [UI_REFERENCE.md](UI_REFERENCE.md) and [P1_P2_PLAN.md](P1_P2_PLAN.md), which describe the system as it now is. It stays here as the record of the user's direction.

Status: approved direction from the user, 2026-10-25. This document overrides `ARCHITECTURE.md`, `SCENARIO.md`, `UI_REFERENCE.md`, `P1_P2_PLAN.md` and `P1_P2_CONTRACT.md` wherever they disagree, until those are updated to match.

## 1. The principle

Harmony must not impose structure on how the agents work. Representatives talk in free text, decide for themselves what to say and when, and source information from their own side when they need it. Code enforces only plumbing (identity, turn-taking, the firewall, human review where the org's mode asks for it). Structure the user rejected must be gone from the **database schema, the API and the engine**, not only hidden in the UI.

Removed outright:

1. **Structured offers and signing authority.** No offers table, no offer envelope (amount, currency, expiry), no `send_offer` / `accept_offer` tools, no accept card, no compare-offers card, no countersigning, no approval limits, no brief authority caps, no aggregate-commitment checks. Prices, terms and agreement are just messages.
2. **Holding messages.** No holding template, no holding delay, no code-sent "I'm checking with the team" message, no holding timer. A representative that wants to tell the counterparty it is checking simply says so.
3. **Standing permissions and file sharing.** No grants table, no release scopes (*this thread / always for X / always*), no revocation, no Standing permissions page, no knowledge visibility (`public` / `representative` / `internal`), no "Files and knowledge" settings, no default scope, no auto-release allowlist. The representative never holds copies of org records.
4. **Structured public profile.** No card JSON with services / ask-us-for / won't-do / turnaround / location. One free-text **summary**.
5. **Inbound filters** ("won't do" auto-declines). The representative decides from its instructions.
6. **Routing learning.** No corrections, no routing-decision history, no "% reassigned", no corrections fed back into the routing prompt.
7. **Structured briefs and search.** The brief is free text written by internal Claude and approved by the member. `search_network` takes a free-text need. The screener returns a fit and a one-line reason only.
8. **`subcontract`** and the sub-request machinery.
9. **The one-performative-per-turn rule.**

## 2. What the representative knows

The representative's system prompt (frozen per thread side at its first turn, as today) holds:
- the org's **public summary** and the representative's **instructions** (prose);
- the **sharing description** that applies to this counterparty: the org's default description, plus the counterparty's own description if it is a trusted organization (§3);
- on the requester side, the **brief** the member approved (free text);
- the counterparty's network-verified identity (unchanged).

Nothing else. No knowledge items, documents or other org records are ever rendered into a representative request. Information from its own side arrives only as a **released consult answer** (the `consult` tool result). The context inspector ("What our representative saw") shows exactly these parts plus the released answers.

## 3. Sharing descriptions and trusted organizations

An org describes, in prose, **what kinds of information its representative may pass on**. It does not list files. Records change over time, so internal Claude fetches the current information when it is needed.

- `rep_policy.sharing_description`: applies to every counterparty. Example (Northlake): "Our external academic price list, what is open on the public instrument calendar, sample-prep and air-free handling requirements, and typical turnaround. Not: other clients or their bookings, internal reserve slots, or discounts."
- **Trusted organizations** (renamed from "counterparty overrides"): a table of counterparties the org treats differently. Each row has an optional mode override (*Ask* / *Auto* / *Skip*; empty means the org default) and an optional extra sharing description for that organization ("Also share with them: …").
- Policy is read live at every decision (unchanged), so edits apply at the next consult.

## 4. Consult: internal Claude sources the information

`consult(question)` stays the representative's only way to its own side, and it still blocks the representative's turn until an answer is released.

1. **Internal Claude answers with tools.** The answerer is internal Claude for that org: full org context plus its read tools (`search_org_knowledge`, `read_document`, and any future org tools), run as a short tool loop, ending in a structured draft. It reads the current records at that moment. The answerer is told the counterparty, the question (quoted, untrusted), the conversation so far, and the sharing description that applies to this counterparty.
2. **The draft**: `answer` (text for the representative), `needs_human` + `needs_human_reason`, `route` (assignees + reason, used only when a person is needed), `rationale`. No sources list, no offer, no suggested actions. What the answerer looked at is recorded from its tool calls and shown to the reviewer.
3. **Sharing check** (new, prompt-based, replaces the declared-sources and n-gram release policy). A separate small model call sees only the applicable sharing description(s), the question and the draft answer (never the org records), and returns `within_sharing: bool` + `reason`. Haiku 5.5 at low effort (Haiku 5.5 for every role under the development `MODEL_OVERRIDE` too); built through `llm/requests.py`; checkpointed like every model call.
4. **Release decision by mode** (for this counterparty):

   | Mode | A consult answer goes straight back to the representative when… | Outbound messages |
   |---|---|---|
   | **Ask** | never: a teammate reviews every answer | Every message is held for a teammate (`outbound_review`) |
   | **Auto** (default) | the answerer says no human is needed **and** the sharing check passes | Sent |
   | **Skip** | the answerer says no human is needed (no sharing check) | Sent |

   Anything else becomes an inbox item routed by prompt (§6), with the reason (the answerer's, or the sharing check's).
5. **A teammate's approval releases that answer to that one thread.** No scopes. To make something shareable from now on, the teammate edits the sharing description (the inbox offers a link: "Edit what you share with Henderson Lab").
6. Commitments: the answerer must set `needs_human` for anything that commits the org (accepting work, agreeing a price outside the published list, waiving a fee, booking a reserve slot) unless the org's instructions plainly allow it. This is prompt policy, not code.

## 5. Representative tools and turns

Tools, identical for every representative: `send_message(text)`, `consult(question)`, `close(summary)`.

- A turn is a normal tool loop. The representative may send several messages, and the turn ends when the model stops calling tools or calls `close`. Tools run one at a time (`disable_parallel_tool_use` stays: it keeps transcripts deterministic).
  - Round 2 (FEEDBACK_R2_PLAN decision 1): it consults before it sends. Once it has sent a message in a turn, a `consult` is refused with `CONSULT_AFTER_SEND`, and it consults at the start of its next turn.
- `close(summary)` ends the thread for both sides. The summary is for the closer's own org only (stored per side, never sent to the counterparty). The other side's representative gets a code-written notice and may write its own summary in one last turn.
  - Round 2 (docs/design/REOPEN.md): a member of either side who can see the thread can reopen it, at most twice and within REOPEN.md's hand-back rules, which starts a new run of the same thread. For threads that start after round 2, the close tool's text no longer says the conversation ends "for good".
- Turn cap, idle timeout and the blocking consult stay as safety plumbing.
- Messages carry provenance `agent`, `agent_approved` (released or edited by a teammate) or `human` (a teammate took over). No `template`.

## 6. Routing

- Routing stays prompt-based: the org's routing prompt plus member profiles (prose) go to the answerer, which proposes assignees and a reason; code validates them against members and falls back to the default assignee. Admins see everything.
- **Reassign** stays as a plain inbox action: it moves the item and its thread access to another member. It records nothing for learning.
- Removed: approval limits, qualifying-member insertion, routing-decision history, corrections.

## 7. Requests, briefs and the requester side

- Internal Claude's tools: `search_org_knowledge`, `read_document`, `search_network(need)`, `send_network_request(brief, recipients)` (the approval card), `get_request_status`.
- The outreach card shows the free-text brief (editable) and the recipients with their screening fit. Approving sends it. The brief is the requester representative's mandate, so the member approving it is what releases its contents.
- When a counterparty proposes terms, the requester's representative consults its own side like any other question. Henderson's answerer routes "Northlake proposes $1,410 with results by Nov 6; go ahead?" to Alex, and it appears inline in Alex's chat (the same path as Ridgeline's seal-material question). Alex's answer is released and the representative replies.
- The request status card shows each recipient's state (§9) and the latest line or closing summary. Internal Claude summarizes progress in prose; there is no comparison card.

## 8. Data model

Nothing is deployed and nothing is committed, so fold these into `migrations/versions/0001_initial.py`. The schema should read as designed, with no drop-and-recreate history. `alembic check` must show no drift, and local dev databases are recreated.

**Drop**
- Tables: `offers`, `grants`, `routing_decisions`.
- `members.approval_limit_minor`.
- `rep_policy`: `counterparty_overrides`, `inbound_filters`, `holding_template`, `holding_delay_s`, `auto_release_allowlist`, `auto_countersign_limit_minor`.
- `rep_settings`: `card` (JSON) and `default_scope`.
- `knowledge_items.visibility`, `documents.visibility`. These tables remain the org's own records, read only by internal Claude's tools.
- `escalations.scope`. Escalation kinds become `consult` and `outbound_review`.
- `thread_messages.offer_id`. Message kinds become `message` and `close`. Provenance drops `template`.
- Thread states `binding`, `declined`, `awaiting_requester_decision` and `awaiting_subrequest`. States become `active`, `awaiting_principal`, `closed` and `expired` (round 2: neither of the last two is final, since Reopen returns a thread to `active`, and the UI shows both as "Closed").
- `network_requests`: `authority`, `brief_constraints` and the structured brief fields. Sub-request columns go too.
- Enums: `Visibility`, `GrantScope`, `GrantSubject`, `RevokeMode`, `OfferStatus`, `AcceptanceMode`, plus `GrantedBy` if nothing else uses it.
- Screening: per-must-have results and `self_reported`.

**Add or change**
- `rep_settings.public_summary` (text).
- `rep_policy.sharing_description` (text).
- `trusted_organizations`: PK `(world_id, org_id, counterparty_org_id)`; columns `autonomy` (nullable, `Autonomy`), `sharing_description` (text), `updated_by_member_id`, plus timestamps and a version for optimistic writes; world-scoped FKs as elsewhere; CHECK `org_id <> counterparty_org_id`.
- `network_requests.brief` (text).
- Per thread side: `close_summary` (text, nullable).
- Consult rows keep the draft fields above, plus `sharing_check` (`within_sharing`, `reason`), `released_text`, `released_by` / `released_by_member_id`, and the answerer's tool-call log reference.
- The domain, events, API models, the generated web client (`make gen-api`) and `P1_P2_CONTRACT.md` follow the schema. Remove `/offers` routes, grant and permission endpoints, and knowledge-visibility endpoints. Add trusted-organization CRUD. Settings writes stay admin-only.

## 9. UI

- **Customize → Harmony** sections:
  - **Public profile**: one Summary textarea and directory visibility.
  - **Instructions**.
  - **Sharing**: the default description, plus the **Trusted organizations** list (add organization; each row has a mode select of "Same as default", Ask, Auto or Skip, and an "Also share with them" textarea).
  - **Autonomy**: the default mode. The user found the modes confusing, so explain them in plain words, by when a person gets involved, with this copy or close to it:
    - **Ask**: "A teammate approves every answer and message before it goes to the other organization."
    - **Auto** (recommended): "Claude handles the conversation. A teammate sees anything beyond what you share, and any decision that needs a person."
    - **Skip**: "Claude handles everything it can. A teammate sees only what Claude says needs a person. For partners you trust fully."

    Next to the default, say that trusted organizations can have their own mode. On each trusted organization row, the mode select defaults to "Same as default".
  - **Team and routing**: default assignee, routing prompt, and members with role and profile prose.

  Removed: Files and knowledge, Standing permissions, Offers and authority, Holding message, and Claude's routing (learning).
- **Requests inbox / escalation panel**: the question, "Why this needs you" (the answerer's or sharing check's reason), the draft answer, and what internal Claude looked at (activity lines). Actions are Approve & send, Edit, Redraft (with instruction), Decline and Reassign, plus the link to edit sharing for that organization. No release-scope chips and no offer line.
- **Chat**:
  - The suggestion chip and composer stay.
  - The outreach ask-user card shows the free-text brief and recipients with fit.
  - The request status card shows live statuses and latest lines.
  - Inline question cards cover consults routed to the member.
  - No compare-offers or accept cards.
- **Network view**:
  - Participant statuses are *Screened*, *Messaged*, *Replied*, *Needs human* (with "Act as …") and *Closed* (with the closer's summary visible only to its own org; otherwise "Closed").
  - Suggested recipients stay ringed.
  - No offer edge labels.
  - Fix the layout so the graph fills the canvas at a readable zoom and the side panel doesn't dominate.
- Every control for a removed concept is gone, not "not available".

## 10. Demo story (replaces ARCHITECTURE §11.5 beats)

1. Alex sends the suggested message. Internal Claude searches the network and drafts the brief. The outreach card lists Northlake, Ridgeline and Calder (Calder screened as a weak fit) and Alex approves.
2. Ridgeline's representative asks which seal material the holder needs. Henderson's representative consults, Henderson's internal Claude can't find it, and the question reaches Alex inline. Alex answers "Kalrez, please. The powders may sit under argon for a few weeks." The answer is released and Ridgeline quotes $325 in a message.
3. Northlake's representative consults: "Can we run XRD on 12 air-sensitive powders before Nov 6? The public calendar is full until Nov 10."
   - Northlake's internal Claude reads the calendar and Raj's reserve-block note through its tools and drafts a yes using the Wed Nov 4 block.
   - The sharing check fails, because reserve slots aren't in Northlake's sharing description, so the item goes to Helen with that reason.
   - Helen approves, and Northlake's representative offers $1,410 with results by Nov 5, in prose.
4. Henderson's representative consults its own side about going ahead with both. The question reaches Alex, who says yes to both. Both representatives confirm in messages: Northlake's instructions let it confirm list-price bookings, and Helen already approved the slot. Each closes with a summary.
5. Alex's internal Claude summarizes: $1,735 in total, inside his budget. The network view shows both threads Closed, and Calder Screened.
6. "What our representative saw" shows Northlake's representative never saw Raj's note, only the released answer.
7. The adversary (Vantage) still probes Northlake for Thursday bookings and forges a verified-sender block. The representative refuses or consults, and the sharing check catches any client detail.

The network sim ("big run") uses the same protocol. Its end screen counts closed conversations and the consults that reached a person (human touches per closed conversation).

## 11. Tests that must hold

- **Firewall property** (Hypothesis, on serialized request bytes): no knowledge item or document body ever appears in a representative request except inside a released consult answer.
- In *Auto*, a draft failing the sharing check never reaches the representative without a teammate's approval. In *Ask*, nothing does. In *Skip*, a draft with `needs_human` doesn't.
- A trusted organization's mode and sharing description apply to that counterparty only.
- `close` stores the summary on the closer's side only, and the counterparty never receives it.
- Grep-level: no references to offers, grants, holding, countersign, approval limits, visibility, inbound filters or routing corrections remain in `src/`, `web/src/`, `migrations/`, `scenarios/` or `tests/`, except this document and historical notes.

## 12. Decisions on open questions (2026-10-26, from the contract and docs tasks)

- **Member roles:** drop `approver`; roles are `admin` (settings writes, sees every inbox item) and `member`. Prof. Maya Henderson becomes `admin` of Henderson Lab.
- **Member profiles are editable:** the Team and routing pane edits each member's profile prose, so the settings API needs a write path for it (admin-only), with tests.
- **Closing a thread** renders as a "Closed" row in each side's transcript; the closer's summary shows only to the closer's org.
- **Sharing description changes mid-thread** reach a live representative as a code-written `sharing` notice (no re-base). This is intended.
- **The sharing check** runs only in *Auto*, and only on drafts the answerer didn't already send to a person. This is intended.
- **Go-ahead questions** on the requester side are one inline question per thread. They aren't combined.
- **Reviews and ratings** stay as discovery data.
- **docs/P0_PLAN.md** is historical: add a note at its top saying the revision supersedes its descriptions of visibility, grants, offers and holding.

## 13. Direction (2026-10-26, from the user): the demo is live, not scripted

- **The hosted demo runs on live models.** Anyone with the link can type anything into any preconfigured account and see what really happens. The §10 story is only the walkthrough's suggestion, not a script the app plays.
- **`ScriptedModel` and the cassettes are test fixtures only** (pytest, CI, Playwright). They are not a demo mode, and the hosted app has no scripted fallback for visitors. The scripted story's lack of reaction to the visitor's choices is expected and isn't a defect to fix in scripted mode.
- **Live-in-browser is the next build phase, after this run.** It covers the live model switch, the access gate, the in-app caps and the budget-exhausted state. Reviews in this run may note gaps there, but fixes in this run shouldn't build it.
- **The big run (P3) is deferred.** Its controls keep opening the not-available dialog.
- **Development and live testing run every role on Haiku 5.5** (user, 2026-10-26), through the local `MODEL_OVERRIDE` default (`claude-haiku-5-5`). This supersedes any brief or plan that says testing runs on Opus. The hosted demo keeps the production mix (Opus 5.5 for featured orgs), checked once with `MODEL_OVERRIDE=none` before deploy.
- **Requests can go out without confirmation** (user, 2026-10-26): a configurable requester-side setting, the equivalent of Claude Code's "dangerously skip permissions". When it's on, internal Claude sends requests (possibly several) without the outreach card: nobody confirms what is asked or who is asked. It matters once the network carries thousands of requests. Proposed shape: an org default an admin sets in Customize → Harmony, plus a per-chat override in the composer. It's off by default, the UI marks it clearly, and the spend caps still bound it. It changes only the send step; go-ahead consults still follow the org's autonomy mode.
- **User-facing name: "Harmony Network Request"** (user, 2026-10-26). It replaces "Network request" everywhere a person reads it: card titles, the network view's picker, activity lines and tool labels, and internal Claude's own wording in chat. That avoids confusion with the technical term "network request". Code identifiers (`network_requests`, `NetworkRequestWorkflow`, the API paths) keep their names.
- **Request view redesign** (user, 2026-10-26, approved from a mockup). It replaces the three-column inbox:
  - Requests are listed in the left sidebar like Claude's recent chats: "Needs you" first, then recent requests. The middle list column goes. /requests stays as a full searchable list, like Claude's Chats page.
  - A request opens full width as a readable, chat-style conversation. The header has a short generated title, the counterparty and a status pill. A Details button opens properties and activity in a right panel.
  - A decision is an inline card at the end of the transcript. It shows a short question, why it was routed to you (with Reassign), the full draft reply as readable text and what Claude looked at. There is no nested scroll box.
  - The actions live in the composer: "Tell Claude what to change…" plus Decline, Edit and Approve and send. The internal-note input merges into the same composer.
- **One master passcode protects the whole system** (user, 2026-10-26). Without it, a visitor sees only a passcode screen: no accounts, no data, no API and no event stream. With it, everything is open, and anyone can type anything into any preconfigured account. The spend caps still apply.
