# Reopen engine: "Reopen with {org}"

Status: accepted (decision 15, docs/FEEDBACK_R2_PLAN.md §0) and **built** in the round-2 build. **Revision 2**, 2026-10-26, after the engine and abuse checks (§16), adjusted to the Edit design's revision 3 (docs/design/EDIT.md), which takes every change §15 asks of it.
- **As built**, where the code differs from this text (the code wins):
  - the offer function is `reopens.reopen_offer(thread, runs, side, *, requester_wrote, now)`, with the `ReopenOffer` and `ReopenOfferState` types (`src/harmony/domain/reopens.py`), not `reopens.offer`: a bare `offer` is a removed concept in `tests/unit/test_removed_concepts.py`, which spares only the Reopen offer's names. §3.2, §4.2 and §10.1 now use the built name;
  - `ThreadStateInput` also gained `closed_by` and `last_turn` (`src/harmony/temporal/activities/network.py`). When a `set_thread_state` call is what ends the thread with `last_turn` set (a refusal after something crossed), it queues the `closed` notice for `closed_by`'s counterparty; a `close` queues its own (§2.1);
  - `set_request_status` is gone altogether, not only from the request workflow's tail (§6.3): every close of a request goes through `end_round`.
- With D1 to D6 (§12) the user also decided two open questions: "for good" leaves the close tool's text for new threads (§0 item 9), and `POST /notes` takes `as_member_id` like this endpoint (§14).
- The names this design introduces are fixed in docs/FEEDBACK_R2_PLAN.md §1. Where the two differ, the plan wins.

The request: "Reopen probably has to open a little prompt to let you continue the discussion." Earlier in the same feedback round, the user said "this expired thing is not cool… Rather than expired, call it closed, and give the option to reopen", and "let's just make everyone admins for this demo".

This design covers the engine behind docs/mockups/round2/composer.html tabs 16 to 22 (Closed, the Reopen prompt, Reopening, Reopened, Reopen failed, the other side reopened, and when Reopen isn't offered), and the D · Closed and D · Reopen frames of docs/mockups/round2/turn_states.html. It builds on the Edit design, whose §3.9 is the seam.

**Citations.** `path:line` is relative to the repo root, at commit 0371669 (the base of this build), so they describe the code before this design was built. The few marked "as built" point into the built tree. A bare `:line` refers to the file named last before it. Anything that is a guess is called one.

---

## 0. In short

1. **Every finished conversation reads "Closed" with a short reason**, for example "Closed · Oct 29 · both sides went quiet". `expired` stays as an internal state (D1). A new `end_reason` column records why the conversation ended, and the web renders it.
2. **Reopen** sits on the latest Closed row. It opens a small inline prompt: the heading "Reopen with Ridgeline Fabrication", a text field, and Cancel and Reopen buttons. The prompt is blue like Edit, and the composer stays on Comment. The text is required (D6).
3. **Who can reopen.** Any member of either party who can see the thread; in the demo everyone is an admin. The other side doesn't have to agree. There are limits (D3):
   - at most **2 reopens** per conversation;
   - after the other side's Claude closes a run that your side reopened, or declines to continue, only the other side can reopen;
   - a responder can reopen only after the requester has written.
4. **The private part runs first.** `POST /api/threads/{id}/reopen` records the typed text as an Edit steer of kind `reopen` and starts a new run of the same `ThreadWorkflow` id. That run:
   1. has the org's internal Claude write the representative's guidance (`write_guidance`, the Edit path) while the thread **stays Closed**;
   2. only when the guidance was updated, reopens the thread in one transaction (`begin_reopen`): the row goes active, both representatives get a `reopened` notice, and the parent request reopens if it had closed;
   3. otherwise ends with nothing changed. The other side and the requester's chat never see a failed reopen.
5. **The new run** carries the turn numbers, delivered seq, consult counts and owed tool results forward. It gets a fresh 16 turns and a new 20 h deadline (D2), and the reopening side moves first. Its first turn opens with the new guidance block, and writes the next message under the org's usual review settings.
6. **What each representative is told.** A code-written `reopened` notice, different per side. Stale `closed` notices that never reached a representative are dropped first. The reopening side's representative also gets the guidance; it never sees the raw text or any comment.
7. **The parent request.** Runs after the first never talk to the request workflow. If the request had closed, it goes back to active with `round + 1` (D4). When the last live thread ends, the run's tail closes the request and the requester's chat hears that it "has ended again".
8. **History.** Each run's end stays as a Closed row with that side's archived closing summary. The counterparty sees "Henderson Lab reopened the conversation" and nothing else.
9. **Close wording.** For threads that start after this build, the `close` tool's description (`CLOSE_TOOL`) no longer says "for good"; tools are frozen per thread side, so running threads keep theirs. The `NOT_REPLIED` result, which isn't frozen, reads "`close` ends the conversation." from this build on.

**Build order.** Edit (docs/design/EDIT.md, migration `0003_edit_guidance`) first, then this design (migration `0004_reopen`). Both migrations are additive, because production has data. After building, reset local worlds: the build SHA in local dev is git HEAD (`src/harmony/config.py:154-157`), so a world kept across an uncommitted change would replay old histories on the new commands.

---

## 1. States today (observed)

### 1.1 The state machine

- `ThreadState` is `active | awaiting_principal | closed | expired` (`src/harmony/db/models/choices.py:58-59`).
- `TERMINAL_STATES = {closed, expired}` appears three times:
  - `src/harmony/domain/threads.py:72`;
  - `TERMINAL` in `src/harmony/temporal/workflows/thread.py:106`;
  - `TERMINAL` in `src/harmony/temporal/workflows/network_request.py:40`.
- `threads.set_state` says "A terminal thread never leaves its state" (`src/harmony/domain/threads.py:243-266`). It stores `stop_reason` only on the move into a terminal state.
- `MAX_TURNS = 16` (`src/harmony/domain/threads.py:71`; `ThreadInput.max_turns` default 16, `src/harmony/temporal/workflows/payloads.py:250-261`). This counts both sides' representative turns together (`src/harmony/temporal/workflows/thread.py:186`).
- The deadline is `deadline_at = request.created_at + REQUEST_LIFETIME` (20 h). It is set when the thread opens (`src/harmony/temporal/activities/network.py:87-90`, `:265`). It bounds every wait, a person's included (`src/harmony/temporal/workflows/thread.py:727-735`).

### 1.2 How a thread ends

In this table a bare `:line` is in `src/harmony/temporal/workflows/thread.py` unless the same cell names another file.

| Way | Where | State | What is recorded |
|---|---|---|---|
| The representative calls `close` | `src/harmony/agents/tools/rep.py:364-396`: it stores the summary, appends a `close` message, sets `closed` directly (:385), and queues the `closed` notice for the other side. The workflow then runs `_close` (`src/harmony/temporal/workflows/thread.py:449-451`, `:658-661`) | `closed` | close message; `rep_sessions.close_summary` (`src/harmony/domain/threads.py:269-289`, "first one written stands") |
| The other side's last turn, after a close | `src/harmony/temporal/workflows/thread.py:195-202`, only if the deadline hasn't passed; may call `close` again to store its own summary (`src/harmony/agents/tools/rep.py:370-373`) | stays `closed` | the other side's summary, or `note_unsummarized` (`src/harmony/temporal/activities/network.py:397-408`) |
| Representative refusal | `_after_turn` :458-465 → `_close(side, stop_reason="refused")`, so `closed_by` is the refusing side | `closed` | `stop_reason=refused` |
| Turn failed (budget, pause, model error) | `run` :191-193 | `closed` | `stop_reason` |
| Consult answer failed | `_consult` :503-506 | `closed` | `stop_reason` |
| Two silent turns | `_after_turn` :467-468 (`SILENT_TURNS_TO_EXPIRE = 2`, :110) | `expired` | nothing |
| A turn ends and no message has crossed | `_after_turn` :467-468 (`not self._crossed`) | `expired` | nothing |
| Turn cap | `run` :186-188 | `expired` | nothing |
| Deadline (top of loop, or while waiting on a person) | `run` :186-188; `_wait_resolution` :589-595 | `expired` | nothing |
| A refusal before anything crossed (`src/harmony/temporal/workflows/thread.py:461-464`) | `_end("closed", stop_reason="refused")` | `closed` | `stop_reason` |

**At the end of every run:**
- `set_thread_state` (`src/harmony/temporal/activities/network.py:370-394`) cancels open items (:391, `_cancel_open_items` :751-762).
- When this call is what ends the thread, it queues the `closed` notice (:393, `_queue_closed_notice` :765-777).
- Tasks are cancelled (`src/harmony/temporal/workflows/thread.py:205-207`).
- The terminal state reaches the request only once, at the end, through an external signal (`_set_state` :695-697; `_signal_request` :699-714, which catches `FailureError` when the request workflow has gone).

**Owed tool results that are dropped today:**
- The close branch (`src/harmony/temporal/workflows/thread.py:449-451`) never owes the closing side's `CLOSED` result (`src/harmony/agents/tools/rep.py:175`, `_closed` :457-462).
- The last turn's result is discarded (`src/harmony/temporal/workflows/thread.py:197-202`).
- A consult or held message cut off by the deadline owes nothing (`_wait_resolution` returns None, `src/harmony/temporal/workflows/thread.py:589-595`).

When a later turn finds an unanswered call, `_answering` fills in INTERRUPTED: "may have taken effect; check before you repeat it" (`src/harmony/agents/runtime.py:125-128`, `:479-494`). Today nothing ever reads these results again, so the loss doesn't matter. After a reopen it would (§4.9).

**Notices that never arrive.** `flush_notices` (`src/harmony/agents/context.py:466-498`) stamps the side's pending notices with the turn id and commits before the model call (`src/harmony/temporal/activities/agents.py:427-431`). A refused, incomplete or raising call stores its user messages "without the notices" (`src/harmony/agents/runtime.py:24-31`). The next flush delivers those again (`lost = await _undelivered(...)`, `src/harmony/agents/context.py:483`; `_undelivered` :886-912). So a `closed` notice ("…call `close` with it now", `closed_notice` :591-598) stamped for a last summary turn that failed is still waiting after the run ends. §5.3 drops it at a reopen.

### 1.3 Database evidence (the preview database `harmony_preview2`, read-only `psql`)

- Thread `7bbb3f46…` is `expired`:
  - 8 representative turns per side, which is 16, the turn cap;
  - 12 messages, no `close` message, no close summaries;
  - nothing stored says why it ended.

  Today a reader can't tell "turn limit" from "went quiet" or "deadline" without recounting.
- The seeded closed history threads (`6ba58693…`, `5c87c974…`, from `scenarios/harmony-demo/threads.yaml:14-17`, `:49-52`) are `closed` with:
  - no `deadline_at`;
  - no close message;
  - no `rep_sessions` rows.

### 1.4 Every reader of "expired"

**Backend**
- `src/harmony/domain/threads.py:72`
- `src/harmony/temporal/workflows/thread.py:106`
- `src/harmony/temporal/workflows/network_request.py:40`
- `src/harmony/domain/requests.py:17-22`: participant `closed` covers `closed` and `expired`.
- `src/harmony/api/routes/network.py:191-192`: counts.
- `src/harmony/seeds/loader.py:68`
- `src/harmony/storyrun/play.py:383-391`
- `src/harmony/storyrun/checks.py:412-438` (`_thread_ends`) and `:497-512` (`ended_at()`): they assume "a terminal thread never leaves its state".
- `src/harmony/agents/tools/rep.py:290-295`: the terminal refusal text interpolates the state name.

**Web**
- `web/src/requests/format.ts:96-98` (`isTerminal`) and `:111-123` (`stateLabel`, which returns "Expired" at :119-120).
- `web/src/requests/RequestView.tsx:265-272`; `EndedBanner` :354-379, with the copy "It ended without a close: both sides went quiet, or it reached its turn limit or deadline."
- `web/src/requests/live.ts:99` (`ENDED`).
- `web/src/network/status.ts:43` (EDGE), `:50` (`EXPIRED`), `:80` (`edgeLabel`), `:97-98` (`threadStatus`).
- `web/src/fixtures/requests.ts:735-749` (`expired()`), `web/src/fixtures/network.ts:135`.
- `web/src/api/schema.d.ts`.

**Tests**
- `web/src/network/network.test.ts:201`
- `web/src/requests/requests.test.tsx:343-347`
- `web/src/requests/format.test.ts:88`
- `tests/workflow/test_e4_workflows.py`: :541, :667-670, :689-692, :965-966, :1405-1406, :1725, :1771, :2530-2544.
- `tests/integration/test_e4_activities.py:578-580`, `:750`.
- `tests/integration/test_e2_consult_tools.py:1924`.
- `tests/unit/test_storyrun.py:375`, `:639`.

**Docs**
- `docs/ARCHITECTURE.md:344`; `:567` ("expired shows as Closed", already the intent).
- `docs/UI_REFERENCE.md:140`, `:157`.
- `docs/LIVE_DEMO_PLAN.md:594`.
- `docs/P1_P2_CONTRACT.md` (§11).

`src/harmony/events/sse.py:228` mentions "expired", but in an unrelated cache comment.

### 1.5 The no-reopen assumptions

- `src/harmony/agents/tools/rep.py:18` (docstring): "a closed thread can't be reopened".
- `CLOSE_TOOL` (:115-134) says "End the conversation for good".
- `NOT_REPLIED` (:177-180) says "`close` ends the conversation for good".
- `src/harmony/domain/requests.py:199-211`: "`closed` is final".
- `src/harmony/temporal/workflows/network_request.py:52-69`: the tail closes the request and notifies the chat once all known threads are terminal; `thread_changed` (:71-76) ignores any change after a terminal state.
- `src/harmony/temporal/workflows/conversation.py:158-164`: `network_update` dedupes by `request_id` alone, so a second "closed" would be dropped.
- `src/harmony/domain/settings.py:403-412`: `_live_threads` skips terminal threads, so settings, mode and sharing notices never reach a closed thread's representatives.
- `docs/LIVE_DEMO_PLAN.md:792`: the E1 reason "can't be reopened".

---

## 2. Decision D1: keep `expired` internal, show "Closed" everywhere

**Recommendation.** Keep `ThreadState` as it is and add `end_reason`. Every user-facing surface says **Closed**, plus a reason.

**Why.** Merging the two states would touch every reader in §1.4, the seeds, storyrun and about 15 tests, and the user would see no difference. Users only see the label and the reason. With the new column, the internal split doesn't matter anyway.

**Alternative.** Merge `expired` into `closed` with a data migration. This is cleaner long term, but noisier now.

### 2.1 `end_reason`

`EndReason = Literal["closed", "quiet", "unsent", "turn_limit", "deadline", "stopped"]` in `src/harmony/db/models/choices.py`.

| `end_reason` | Set by | Shown as (reason after "Closed · {date} ·") |
|---|---|---|
| `closed` | `src/harmony/agents/tools/rep.py _close` (`closed_by` = that side) | `closed_by` is ours: "Claude closed it"; theirs: "{Them} closed it"; null (seeded): no reason |
| `quiet` | `_after_turn` silent count | "both sides went quiet" |
| `unsent` | `_after_turn` with `not _crossed` | "nothing was sent" |
| `turn_limit` | `run` cap | "it reached its turn limit" |
| `deadline` | `run` deadline, `_wait_resolution` | "it reached its deadline" |
| `stopped` | any `stop_reason` (refusal, budget, pause, model error) | `stoppedLine(reason, budget)` (`web/src/chat/failureCopy.ts:61-74`), e.g. "The model service returned an error, so the conversation couldn't go on." |

`end_reason` is null only while a thread is open.

**Workflow changes in `src/harmony/temporal/workflows/thread.py`:**
- `_end(state, *, end_reason, stop_reason=None, closed_by=None)` makes `end_reason` required.
- `_close(closer, *, stop_reason=None)` passes `end_reason="stopped" if stop_reason else "closed"` and `closed_by=closer`.
- The cap and deadline check at :186 splits into `turn_limit` and `deadline`.
- :467-468 becomes `"unsent" if not self._crossed else "quiet"`.
- The refusal-before-crossing branch (`src/harmony/temporal/workflows/thread.py:461-464`) passes `closed_by=side`, so the refusal rule in §3.2 knows who declined. It still doesn't set `self._closed_by`, so no summary turn follows, as today.
- `ThreadStateInput` (`src/harmony/temporal/activities/network.py:161-171`) gains `end_reason` (as built, also `closed_by` and `last_turn`: a refusal after something crossed ends the thread with `last_turn` set, and that call queues the `closed` notice for the other side's last turn).
- `threads.set_state` gains `end_reason` and `closed_by`. On the move into a terminal state it stores them, together with `ended_at = now()`.
- `src/harmony/agents/tools/rep.py _close` (:385) passes `end_reason="closed", closed_by=scope.side`.

**Seeds, no backfill.** A world created under another build is forked fresh on the next visit and the old one retired (docs/ARCHITECTURE.md:394; `src/harmony/worlds/service.py:176-184`; `src/harmony/auth/session.py:171`). So after a deploy every thread a visitor can reach is either seeded or ran on the new code, and the migration needs no backfill. The seed loader (`src/harmony/seeds/loader.py:447-460`) sets `end_reason` (`closed` for a seeded `closed` thread, `quiet` for a seeded `expired` one) and `ended_at = last_activity_at` on seeded terminal threads, so they show "Closed · {date}".

### 2.2 What each surface shows

| Surface | Today | After |
|---|---|---|
| Request view header pill (`format.ts stateLabel`) | "Closed", "Expired" or "Stopped" | "Closed" |
| Inbox row (`format.ts rowStatus`, :130-137) | "Stopped", "Closed" or "Expired" | "Closed" |
| Request view body (`web/src/requests/RequestView.tsx:265-272`) | `EndedBanner` or `StoppedBanner` below the transcript | A **Closed row** in the transcript for each run that ended (§8). The latest one carries Reopen. Both banners go |
| Details (`web/src/requests/Details.tsx:202-204`) | "Expires {deadline}" | "Closes {deadline}" while open; "Closed {ended_at}" once ended; "Reopened by {name} · {date}" while a reopened run is live |
| Network edge (`web/src/network/status.ts`) | muted; "Closed: {stoppedLine}", the close summary, or "Expired" | muted; the own-side close summary when there is one, else "Closed: {reason}". `EXPIRED` is removed. `threadStatus` maps `expired` to `PARTICIPANT.closed` |
| Network counts (`src/harmony/api/routes/network.py:191-192`) | by `TERMINAL_STATES` | unchanged: a reopened thread counts as active again |
| Requester chat `RequestStatusCard` (`web/src/chat/cards/RequestStatusCard.tsx:45-58`) | "Closed" pill plus our close summary | unchanged while closed. After a reopen, the recipient's pill goes live again ("Messaged" or "Replied"), from participant status (`src/harmony/domain/requests.py:380-386`) |
| Inbox section (`src/harmony/api/routes/inbox.py:13`, :180) | ended threads go to Done | a reopened thread returns to the live sections, because its state is live |

A reopen that fails changes none of these (§4.3).

---

## 3. Who may reopen

### 3.1 Rule

- Any member who can see the thread (`src/harmony/auth/access.py:96`, `get_visible_thread`) and whose org is a party (`threads.side_of`, `src/harmony/domain/threads.py:374`) may reopen it, from either party. In the demo everyone is an admin, so that is every member of both orgs. Role isn't checked. A test-only non-admin with access can reopen, and one without access gets 404. The demo cast are all admins, so role behaviour is tested with a non-admin made in the test fixtures.
- The thread must be **reopenable** for the viewer's side (§3.2).
- **The other side doesn't have to agree** (D3).
  - Its representative gets a `reopened` notice and, normally, the reopening side's next message.
  - Its own review settings apply to its reply, as for any message.
  - Its representative can close again as soon as it has that message: the E1 close guard is satisfied, because the reopening side wrote in this run (§4.5).
- **What the other side's people can do.** Nobody has a "close" control in the API today (`api/routes` has no member close endpoint). While the reopened conversation waits on the reopening side, for example on a message held in its Ask review, the other side's people can Comment, or use Edit ("close this politely"), which takes effect at their representative's next turn. That turn comes only after the reopening side's message is sent, and the new 20 h deadline bounds the wait.

### 3.2 When Reopen is offered (`ThreadDetail.reopen`)

`reopens.reopen_offer(thread, runs, side, *, requester_wrote, now)` (new `src/harmony/domain/reopens.py`, with `MAX_REOPENS` and `WRAP_UP_GRACE`) is a pure function, computed per viewer. It returns `ReopenOffer {state: "allowed" | "wrapping_up" | "blocked", block: ReopenBlock | null}`, or null for an open thread. The checks run in this order:

| Check | Result | Copy on the Closed row (in place of the button) |
|---|---|---|
| No `thread_runs` row for the thread (seeded history) | `blocked` / `history` | none: no button |
| `run_finished_at` is null and `ended_at` is less than `WRAP_UP_GRACE` (10 min) ago | `wrapping_up` | Reopen disabled, hint "Claude is still wrapping up this conversation." |
| `threads.run − 1 ≥ MAX_REOPENS` (2) | `blocked` / `limit` | "This conversation was reopened twice, so it can't be reopened again." |
| `end_reason = stopped`, `stop_reason = refused`, `closed_by` is the other side | `blocked` / `refused` | "{Them}'s Claude declined to continue, so only {Them} can reopen it." |
| The current run was reopened by the viewer's side, `end_reason = closed` and `closed_by` is the other side | `blocked` / `they_closed` | "{Them} closed it after {Us} reopened it, so only {Them} can reopen it." |
| The viewer is the responder and the requester has no `message` on the thread, in any run | `blocked` / `not_asked` | "Only {Them} can reopen this conversation." |
| otherwise | `allowed` | Reopen |

- After `WRAP_UP_GRACE` with `run_finished_at` still null, the offer is `allowed` and the API decides precisely (§4.2 step 8): 409 `closing_in_progress` while the old run is still running, or 409 `not_reopenable` / `unfinished` when it died without finishing. So a dead run never leaves a button disabled forever, and the UI never states "can't be reopened" from a guess.
- **Budget.** While the budget isn't `ok`, Reopen is disabled with `budgetLine(state)` (`web/src/chat/failureCopy.ts:84`). The server doesn't check the budget first ("the UI never blocks on a guess", `web/src/chat/failureCopy.ts`). If the budget runs out in a race, the guidance call is refused, the attempt fails privately (§4.3), and nothing else changes.
- The reopen count, `end_reason`, `stop_reason` and `closed_by` are public facts on the `threads` row. Which org reopened the current run is already shown to both sides in the transcript (§8), so the per-side checks reveal nothing new.

---

## 4. The Temporal flow

### 4.1 Shape: an attempt under the same workflow id, private part first

```
Member presses Reopen
  POST /api/threads/{T}/reopen {client_key, as_member_id, body}
    API transaction (thread row locked):
      checks (§4.2) → steers.record(kind=reopen, status=working)  ← the attempt marker
      start_world_workflow(ThreadWorkflow.run, ThreadInput(run=n+1, carry, reopen=…),
                           FAIL, memo={reopen_steer_id})
    commit → 202 {steer_id}
  ThreadWorkflow, run n+1 (id w:{world}:thread:{T}); its state starts as the old terminal one:
    record_reopen            idempotent upsert of the steer row (covers a failed API commit)
    write_guidance(steer)    EDIT.md §3.3, kind=reopen; the thread is still Closed
      outcome ≠ updated, or raised → settle_steer(failed) → return.   Nothing else changed.
    begin_reopen             one transaction: run row, summaries archived, thread active,
                             stale notices dropped, reopened notices, steer done, request reopened
      raised → settle_steer(reopen_failed) → return
    loop from the reopening side (unchanged loop; fresh cap and deadline)
    tail: owe the closing results; cancel tasks; end_round (+ notify); finish_run(carry)
```

**Why a fresh start, not continue-as-new or an Update:**
- The old run has finished: its workflow returned `ThreadResult` (`src/harmony/temporal/workflows/thread.py:209`). An Update or signal has nothing to reach.
- `id_reuse_policy` defaults to `ALLOW_DUPLICATE` (temporalio 1.34.0, `client/_workflow.py:1063`, checked in the venv), so a closed id can be started again, as many times as attempts need.
- `start_world_workflow` (`src/harmony/temporal/start.py:70-125`):
  - refuses a non-live world (`WorldRetired`), which covers resets;
  - records the id in `world_workflows` idempotently;
  - gives the new run a fresh 24 h (`WORLD_RUN_TIMEOUT`, :42).

  That comfortably covers a new 20 h deadline. Continue-as-new would keep the old chain's 24 h execution timeout.
- It gains a `memo` parameter, passed to `client.start_workflow(memo=…)` (temporalio `client/_client.py:410`). The API reads it back with `handle.describe()` and `memo_value("reopen_steer_id")` (`client/_workflow.py:363`, `:1379`).
- The workflow id stays `thread_workflow_id(world, T)`, so every existing Update path works unchanged against the new run: `_update` in `src/harmony/api/routes/escalations.py:147-167` (as built), and Edit's steers.

**Why the private part first.** Edit refuses a closed thread anyway (EDIT.md §3.1, `THREAD_CLOSED_ERROR`), and a closed thread has no open items (`_cancel_open_items`), so nothing needs the row to be active while internal Claude writes the guidance. Running it first means a reopen that fails (budget spent, model error, a guidance check rejection) touches no shared row: no counterparty notice, no live pill, no new request round, no internal-Claude wake, and no private stop reason written to the shared thread row.

### 4.2 API: `POST /api/threads/{thread_id}/reopen`

Request: `ReopenRequest {client_key: uuid, as_member_id: uuid, body: str (1..4000, trimmed)}`.

- The web mints `client_key` once **when the prompt opens**, and keeps it until a 202 or Cancel, across 409s and network errors. After a 202 the prompt closes. A later prompt, including "Try again" after a failed attempt, mints a new key.
- Keeping the key across a 409 is safe here, unlike Edit's Update ids (EDIT.md §3.1): the only dedupe is the steer row, and a 409 writes nothing.
- `as_member_id` is the member the tab shows.

Response: `202 ReopenStarted {steer_id, thread_id}`. `steer_id = steers.steer_id_for(world, org, member, client_key)` (EDIT.md §2.1), so ids are server-minted and namespaced like an Edit's. There is no separate reopen id: the attempt is its steer.

The logic lives in a new `src/harmony/temporal/reopen.py` (`start_reopen(client, sessionmaker, actor, thread_id, request)`) so that integration tests can call it without HTTP. In one unit of work:

1. **Who.** If `actor.member_id != as_member_id`, return 409 `member_changed` with `member` (the session's current member) before any read or write. `current_actor` resolves the member from the `sessions` row on every request, and act-as is only a row update (`src/harmony/auth/session.py:1-11`, `:154-176`). Without this check, a tab still showing Alex after a switch in another tab would send Alex's text as the other org. Edit's `/steers` and `POST /notes` make the same check (EDIT.md §3.1, §8.1).
2. `get_visible_thread`, else 404. The actor's org must be a party, else 403 `not_in_org`. 422 `invalid` for a bad body.
3. `threads.locked(uow, T)`. This row lock serializes reopens of one thread: both sides at once, two tabs, or a double click.
4. **Idempotency.** If the steer `steer_id` exists, it must match (thread, kind `reopen`, author, body). Then return 202 with its ids, whatever its status. A failed attempt then shows as failed in the UI. A mismatch is 422 `invalid` (one client reusing a key with different text).
5. **Already open.** If the thread isn't terminal, the current run's row (`thread_runs[(T, run)]`) says who reopened it:
   - same member and the same body: return 202 with that run's `steer_id` (a second tab, or the prompt opened again after the first reopen landed);
   - otherwise return 409 `thread_open` with `reopened_by = {org, member}`, where `member` is set only for the actor's own org. It is null for a thread that was never closed.
6. **An attempt in flight.** If a `reopen` steer on this thread has status `working` (either org), the API checks the workflow with `describe()`:
   - **The attempt is live.** The execution is RUNNING and its memo's `reopen_steer_id` is that steer.
     - Same member and the same body: return 202 with that steer.
     - The same org otherwise: 409 `reopen_in_progress` with `reopened_by.member`.
     - The other org: 409 `reopen_in_progress` with no details, so a private attempt that may still fail reveals nothing.
   - **The attempt was abandoned.** Anything else means its run died without settling. The API settles that steer `failed` / `reopen_failed` in this transaction and continues.
7. **Rules.** Compute `reopens.reopen_offer` (§3.2). Then:
   - `blocked` → 409 `not_reopenable` with `block`;
   - `wrapping_up` → step 8;
   - `allowed` → step 9.
8. **The old run hasn't finished** (`run_finished_at` is null). `describe()`:
   - RUNNING → 409 `closing_in_progress`;
   - anything else → 409 `not_reopenable` / `unfinished`. The run ended without storing a carry, so its turn numbers and owed results can't be restored safely.
9. **Record the attempt.** `steers.record(kind="reopen", id=steer_id, side, org_id, author_member_id, body)` under the thread lock. It returns the steer's `seq` and emits the `steer` snapshot to the reopening org's thread scope only. The thread row is not touched.
10. **Build `ThreadInput`:**
    - the same ids and `cassette_thread`;
    - `run = n + 1`;
    - `carry = thread_runs[(T, n)].carry`;
    - `max_turns = carry.turns.requester + carry.turns.responder + MAX_TURNS`;
    - `deadline_at = now + REOPEN_LIFETIME` (20 h, the same value as `REQUEST_LIFETIME`);
    - `guidance = {side: latest rep_guidance version}` and `steer_seq = {side: max steers.seq}`, the latter including this steer (EDIT.md §3.9 asks for both);
    - `reopen = ReopenInput(steer_id, side, org_id, member_id, body, seq, from_state)`, where `from_state` is the current terminal state.
11. **Start.** `start_world_workflow(…, id_conflict_policy=FAIL, memo={"reopen_steer_id": str(steer_id)})`, inside the transaction, as `start_world_workflow` already does with the world row.
    - On `WorkflowAlreadyStartedError` (`temporalio/exceptions.py:53`), `describe()` and read the memo:
      - It is this steer: the commit of an earlier identical POST failed after its start (step 12). Return 202.
      - It is another attempt's steer: 409 `reopen_in_progress`, as in step 6.
      - It is absent, or it is the current run's own `steer_id`: the old run is in its last workflow task, after `finish_run`. Roll back, wait 300 ms and redo from step 3, up to 5 times. Then return 409 `closing_in_progress`.
    - On `WorldRetired`, return 409 `world_retired` (`src/harmony/api/routes/problems.py:122`).
12. **Commit, then return 202.** If the commit fails after the start succeeded, `record_reopen` (§4.3) inserts the steer from `ReopenInput`. A retried POST with the same key then finds the steer (step 4), or hits step 11's memo match.

**Why the body travels in the workflow input.** Edit already passes the instruction through the Update `steer` (EDIT.md §3.1). Keeping it in the input means `record_reopen` can write the `steers` row on its own, so "Your text is saved" is true even in the commit-failure case.

### 4.3 The attempt: guidance first, then the reopen

**Payloads** (`src/harmony/temporal/workflows/payloads.py`):

```python
class ThreadCarry(_Payload):
    turns: dict[Side, int]
    delivered: dict[Side, int]
    consults: dict[Side, int]
    owed: dict[Side, list[OwedResult]]  # never a pending (content None) result, §4.9


class ReopenInput(_Payload):
    steer_id: UUID
    side: Side
    org_id: UUID
    member_id: UUID
    body: str
    seq: int  # the steer's seq
    from_state: ThreadState  # closed | expired


class ThreadInput(_Payload):  # adds, all defaulted so run 1 is unchanged
    run: int = 1
    carry: ThreadCarry | None = None
    reopen: ReopenInput | None = None
    guidance: dict[Side, int] = {}  # EDIT.md §3.9: seeds self._guidance
    steer_seq: dict[Side, int] = {}  # EDIT.md §3.9: seeds self._steer_seq
```

**`__init__`** (`src/harmony/temporal/workflows/thread.py:157-180`): when `thread.carry` is set, `_turns`, `_delivered`, `_consults` and `_owed` come from it. When `thread.reopen` is set:
- `_state = reopen.from_state` and `_side_to_move = None`, so Edit's Update and `resolve_escalation` are refused as on any closed thread while the attempt runs;
- `_silent_turns = 0`, `_crossed = False` and `_closed_by = None`, as for a new thread;
- `_guidance` and `_steer_seq` are seeded from the input.

**`run`** (`src/harmony/temporal/workflows/thread.py:183-209`):

```python
if thread.reopen is not None and not await self._begin_reopen(thread.reopen):
    return ThreadResult(state=self._state, turns=self._total_turns())   # nothing changed
side = thread.reopen.side if thread.reopen else "requester"
… the loop, unchanged …
… the tail (§4.6) …
```

The cap compares `_total_turns()`, which includes carried turns, with `max_turns`, which also includes them. Each run therefore gets 16 new turns.

**`_begin_reopen(reopen) -> bool`:**
1. Activity `record_reopen` (`mirror_activity_options()`). It upserts the steer row from `ReopenInput` (`on_conflict_do_nothing`) and returns its `seq`. Then `_steer_seq[side] = max(_steer_seq[side], seq)`. Without this, EDIT.md §3.7's guard would treat the reopen's own steer as one that arrived mid-turn and hold the first message.
2. `written = write_guidance(steer_id)` with `model_activity_options()`. This is EDIT.md §3.3's guidance part with `kind = reopen`, awaited directly because the first turn must carry it.
   - The prompt's `{reopen}` paragraph and its `{reason}` line come from the thread row, which still holds the old run's `end_reason` and `closed_by`. The row is unchanged during the attempt.
   - There is no rewrite part, because a closed thread has no open items.
3. **Only `updated` continues.** If the activity raised, or `written.outcome` is `unchanged` or `rejected` (any reason), the workflow runs `settle_steer(steer_id, failure=…)` and returns False. The thread row, notices and request are untouched, and no turn is spent.
   - A failure the activity raised maps through `failure_reason` (`src/harmony/temporal/failures.py:39-46`).
   - `unchanged` becomes the reopen-only failure `unchanged`.
   - A rejection keeps the reason `write_guidance` stored (`refused`, `invalid`, `added_details`).
   - Why `unchanged` fails too: the reopened turn would otherwise run with no new direction from the member, which is what a required text (D6) is for.
4. `self._guidance[side] = written.version`.
5. Activity `begin_reopen` (§4.4, mirror options). It returns `start_seq`. If it raises (a non-retryable domain error, or `WorldRetired`), the workflow runs `settle_steer(steer_id, failure="reopen_failed")` and returns False. Its one transaction rolled back, so the thread is still Closed and reopenable.
6. `self._state = "active"`, `self._side_to_move = side`, `self._run_start_seq = start_seq`, and return True.

`settle_steer` is wrapped in its own `try`/`except ActivityError`. If it fails too, the steer stays `working` and the next POST's step 6 settles it as abandoned. So no failure path strands the thread.

**`_rep_turn`** (`src/harmony/temporal/workflows/thread.py:387-419`) passes the following through `RepTurnInput` (`src/harmony/temporal/activities/agents.py:172-185`):
- `reopened=True` for the reopening side's first turn of the run;
- `run_start_seq`;
- `guidance_version=self._guidance.get(side)` and `steer_seq` (EDIT.md §3.6).

**`rep_turn` (`src/harmony/temporal/activities/agents.py:414-522`) when `reopened`:**
- The opening is built in EDIT.md §3.6's order (§4.9).
- `_fallback` (:835-842) returns the new `REOPENED = "The conversation with {counterparty} is open again."` when nothing is owed and nothing new came in, in place of `THREAD_ENDED` and `NOTHING_NEW`. It follows the guidance block.
- `quiet = False`, so the representative is asked to write.
- The opening carries a guidance block, so the turn is `guided` (EDIT.md §3.6). A first call that answers in prose is held for a person with `GUIDED_TEXT_HOLD` whatever the mode, not sent. A `send_message` call follows the org's usual review settings (Auto, Ask, Skip).
- `RepToolScope` gains `run_start_seq`. `_check_turn`'s E1 guard (`src/harmony/agents/tools/rep.py:308-316`, `_has_written` :493-507) counts only `message` rows with `seq > run_start_seq`.
  - So the reopening side's representative can't close before the other side has replied in this run. It gets `NOT_REPLIED` or `NOTHING_SENT`, as for a new thread.
  - The other side, which has the new message, can close at once.

### 4.4 What `begin_reopen` writes (one transaction)

`begin_reopen` is a new `NetworkActivities` method registered in `src/harmony/temporal/worker.py:178-195`. It starts with `ensure_world_live` (`src/harmony/temporal/policies.py:132-142`), then takes the thread lock, then the request lock.

- **Idempotency.** If `threads.run` is already ≥ the input `run`, return that run's stored `start_seq` and write nothing. A retry after the commit is a no-op.
- **Guard.** The thread must be terminal at `run = n` with `run_finished_at` set. Otherwise raise the non-retryable `ReopenConflict`. The API's checks and the `working` steer make this unreachable; it is a defensive check.
- **Run row.** Insert `thread_runs(run n+1)` with `started_at`, `deadline_at`, `max_turns`, `reopened_by_member_id`, `reopened_by_org_id`, `reopened_side`, `steer_id` and `start_seq` (the thread's highest message seq).
- **Archive the summaries.** For each side that has a `rep_sessions` row, copy its `close_summary` into `thread_runs(run n).requester_summary` or `responder_summary`, then set it to null. A side without a row (it never took a turn, for example after an `unsent` run) is skipped; nothing raises. `store_close_summary`'s "first one written stands" (`src/harmony/domain/threads.py:269-289`) then applies per run.
- **Reopen the thread row.** The new `threads.reopen(uow, T, run, side_to_move, deadline_at)` is the only way out of a terminal state, and `set_state` keeps its rule. It sets:
  - `state = active`, `side_to_move = reopening side`, `run = n + 1`;
  - `end_reason`, `ended_at`, `closed_by` and `stop_reason` to null;
  - `run_finished_at = null`;
  - `deadline_at` to the new value;
  - `last_activity_at = now`.

  It then snapshots the row to both orgs (`snapshot`, `src/harmony/domain/threads.py:435-438`).
- **Notices** (§5.3): for each side, drop the `closed` and `reopened` notices that never reached its representative, then queue a `reopened` notice to each side (§5.2).
- **Steer.** Set the reopen steer's status to `done` and snapshot it to the reopening org's thread scope.
- **Request.** Run `requests.reopen(uow, request_id)` (§6.2).

If this transaction fails, none of it happened.

### 4.5 The close guard per run

The E1 guard counts only messages since `run_start_seq` (§4.3). The reopening side therefore has to write first and hear back before it can close, and the other side can close as soon as the reopening side's message arrives.

### 4.6 The tail of a run

The tail is the same for every run, with two additions at the end:

1. Owe the closing results (§4.9), cancel tasks and settle steers, as today and as EDIT.md §3.2 adds.
2. **Run 1:** `_signal_request()`, unchanged. **Runs > 1:** `end_round` (§6.3), then `notify_conversation` when it closed the request. Runs > 1 never signal the request workflow; its `thread_changed` handler would ignore them anyway, because a terminal state is sticky there (`src/harmony/temporal/workflows/network_request.py:71-76`).
3. `finish_run(FinishRunInput)`, the **last** activity of every run, run 1 included. It:
   - updates `thread_runs(run n)` with `state`, `end_reason`, `closed_by`, `stop_reason`, `ended_at`, `carry` and `finished_at`;
   - sets `threads.run_finished_at = now` and snapshots the row. That is what turns Reopen on in open views.

`finish_run` runs after the last summary turn, so the carry includes that turn's owed result. Because it is last, the gap between `run_finished_at` and the old run returning is a single workflow task. That is the window step 11's retry covers.

**Run 1's row** is inserted by `open_thread` (`src/harmony/temporal/activities/network.py:250-265`) with `started_at`, `deadline_at`, `max_turns = 16` and `start_seq = 0`. A thread with no `thread_runs` row is therefore exactly a seeded one.

### 4.7 Idempotency and races

| Case | Behaviour |
|---|---|
| Double click, or a retried POST | Same `client_key`, so the same `steer_id`. The row lock serializes, and the second finds the steer (§4.2 step 4) and returns the same 202 |
| The same member in two tabs, same text | 202 with the first attempt's steer (step 5 or 6) |
| The same member in two tabs, different text | 409 `reopen_in_progress` or `thread_open` naming themselves: "You already reopened this conversation in another tab. Your text is in Edit, ready to send." (once open) |
| Both sides press Reopen at once | The row lock serializes them. The first gets 202. The second sees a `working` steer and gets 409 `reopen_in_progress`, without details for the other org: "Couldn't reopen it just now. Try again in a moment." Once the first attempt has opened the thread, a retry gets 409 `thread_open` with `reopened_by`, and the web moves the text into the composer's Edit mode: "Ridgeline Fabrication reopened it first. Your text is in Edit, ready to send." If the first attempt failed, the retry simply proceeds |
| Two members of the same org | As above. `reopened_by.member` names the teammate: "Raj is reopening this conversation." / "Raj reopened it first." |
| Reopen while the old run is winding down (the summary turn after a close) | The offer is `wrapping_up` (disabled). A stale tab's POST gets 409 `closing_in_progress` |
| Reopen in the instant between `finish_run` and the old run returning | `WorkflowAlreadyStartedError` with no attempt memo: retried in the API for about 1.5 s, then 409 `closing_in_progress` |
| Reopen during a reset | The world is `retiring` (`src/harmony/worlds/service.py:294`, `:389`), so `start_world_workflow` raises `WorldRetired` and the API returns 409 `world_retired`. If retirement begins just after the start, it terminates the new run with the world's other workflows (`src/harmony/worlds/retirer_temporal.py:75`), and the old world is discarded anyway |
| Edit while an attempt runs | The workflow's state is still terminal, so Edit's Update gets `THREAD_CLOSED_ERROR` (409 `thread_closed`). The UI keeps Edit disabled until the thread is open |
| The act-as member changed in another tab | 409 `member_changed` before anything is read or written (step 1) |
| The new run ends again before anything is sent | `end_reason = unsent` ("nothing was sent"). The counterparty's `reopened` notice is still unflushed. A later reopen drops it (§5.3) |

### 4.8 Stuck states and recovery

| Where it stops | What happens | Recovery |
|---|---|---|
| `record_reopen`, `write_guidance` or `begin_reopen` raises | `_begin_reopen` catches `ActivityError`, settles the steer `failed`, and returns. The thread is Closed and reopenable | none needed |
| The attempt's workflow fails or is terminated before settling (a workflow-task bug, `settle_steer` failing too) | The steer stays `working` | The next POST's step 6 sees no RUNNING execution with that memo, settles the steer `failed` / `reopen_failed`, and proceeds |
| A run reaches its terminal state but never runs `finish_run` (failed, terminated, or a domain error in the tail) | `run_finished_at` stays null | The offer shows `wrapping_up` for `WRAP_UP_GRACE`, then `allowed`. A POST then gets `not_reopenable` / `unfinished` from `describe()` (step 8): "This conversation can't be reopened: Claude didn't finish wrapping it up." |
| A side has no `rep_sessions` row (it never took a turn) | `begin_reopen` skips it when archiving | none needed |
| A workflow task stuck RUNNING on a bug | Every POST gets `reopen_in_progress` or `closing_in_progress` | none in this design; the bug has to be fixed |

### 4.9 Owed results carried across runs

The representative's transcript must stay valid across runs. Each request extends the frozen prefix byte for byte, and a turn opens with the tool results it is owed. So the workflow keeps the results it drops today (§1.2):

- **Close branch** (`src/harmony/temporal/workflows/thread.py:449-451`): `self._owe(side, result.owed)` before `_close`. The closing side's `close` call then gets `CLOSED`: "Closed. Nothing more reaches the counterparty."
- **Last turn** (:197-202): `self._owe(last, result.owed)` when it returned a result.
- **Deadline cut-off.** When `_wait_resolution` returns None in `_consult` or `_held` (:513, :531), owe a code-written result for the call's `tool_use_id`:
  - `CUT_OFF_CONSULT = "No answer: the conversation closed before your side decided."`
  - `CUT_OFF_HELD = "Not sent: the conversation closed before your side decided."`
- **Model or consult failure.** Nothing is owed. INTERRUPTED (`src/harmony/agents/runtime.py:125-128`) is true there.
- **Pending results** (`content=None`, `src/harmony/agents/runtime.py:228-235`) are never carried. A consult cut off by the deadline is covered by the cut-off rule above.

At the reopening side's first turn, the opening is built in EDIT.md §3.6's order:
1. its carried results, for example `CLOSED`;
2. a held bare-text result, if any;
3. the `<guidance>` block;
4. any counterparty message it hadn't seen;
5. if nothing was owed and nothing came in, the `REOPENED` fallback, after the guidance.

The `reopened` notice follows as the system message after the opening (`src/harmony/agents/runtime.py:594-595`).

### 4.10 Replay and determinism

- Run 1's commands change only in three ways:
  - `_end` and `_close` pass `end_reason` and `closed_by`;
  - `finish_run` is added as the last activity;
  - owed results are recorded, which is workflow state, not a command.
- Worlds fork on a new build (docs/ARCHITECTURE.md:394), so no live history replays on the changed code. Local worlds need a reset after building this (§0).
- `tests/workflow/test_e4_workflows.py:474-482` replays the histories each test run produces. Reopen tests add their histories to it, including a failed attempt's.

---

## 5. What resets, and what each representative is told

### 5.1 Reset table

| Item | After a reopen | Why |
|---|---|---|
| Turn numbers per side | **continue** (carried) | `turn_id_for(world, thread, org, turn)` = `uuid5(…, f"turn/{thread}/{org}/{turn}")` (`src/harmony/ids.py:22-23`) keys checkpoints (`AgentTurn`, `ModelCall`) and side-effect ids. Reusing a number would replay run 1's turn |
| Consult ordinal per side | **continues** | The cassette key and consult turn (`src/harmony/temporal/workflows/thread.py:499`, `src/harmony/domain/threads.py:391-416`) |
| Delivered seq per side | **continues** | Otherwise messages the representative already saw would be delivered again |
| Owed results | **carried** (§4.9) | The transcript must stay valid |
| Turn limit | **a fresh 16** (D2) | A thread closed at its turn limit could otherwise never be reopened |
| Deadline | **now + 20 h** (D2) | The old deadline may have passed. 20 h fits inside the new run's 24 h |
| Silent-turn count, `_crossed` | **0, false** | "Two silent turns" and "nothing was sent" are judged per run |
| E1 close guard | **per run** (`run_start_seq`) | The reopening side can't close before a reply |
| Budgets (world, org, session) | **not reset** | Spend is spend. The guidance call and turns reserve as usual |
| Frozen representative prompt and tools | **unchanged** | Frozen per (thread, side) (`src/harmony/agents/context.py:414-450`) |
| Open items | **stay cancelled** | Cancelled when the thread ended (`_cancel_open_items`) |
| Guidance | **kept, plus a new version** from the reopen text | EDIT.md §2.2 versions are append-only |
| Close summaries | **archived per run**, current cleared | So the new run can write its own |
| `rep_notices` | undelivered `closed` and `reopened` dropped; a new `reopened` queued per side | §5.3 |
| Settings, mode and sharing changes made while closed | **delivered** at the next turn | `src/harmony/domain/settings.py:403-412` `_live_threads` → `_notice_threads`: drop the `not_in(TERMINAL_STATES)` filter. A notice for an ended thread waits in `rep_notices` until a reopen flushes it. It costs one row per ended thread per change |

### 5.2 Notices (code-written, `src/harmony/agents/context.py`)

`NoticeKind` (:99) gains `"reopened"`. The `rep_notices.kind` column is free text (`src/harmony/db/models/thread.py:188-201`), so this needs no migration.

- **To the reopening side:** "Notice from Harmony: your organization reopened this conversation with {counterparty}, so messages reach them again. Send them your next message."
- **To the other side:** "Notice from Harmony: {reopening org} reopened this conversation, so messages reach them again."
- `_IDENTITY_LINE` (`src/harmony/agents/context.py:108-122`) gains `reopened\s+(?:this|the)\s+conversation`, next to `closed\s+(?:this|the)\s+conversation`. A counterparty line imitating the notice is then marked `[unverified]` inside its delimiter (`wrap_counterparty`, :558-574). "notice from" and "harmony" already match today.

### 5.3 Dropping stale notices

The new `context.drop_undelivered(uow, thread_id, side, kinds=("closed", "reopened"))` runs in `begin_reopen` for both sides. Under `_lock_notices` (`src/harmony/agents/context.py:851-853`), it deletes every notice of those kinds that never reached the representative:
- unflushed ones (`flushed_turn_id IS NULL`);
- flushed ones whose turn has no stored `system` message, which is exactly `_undelivered`'s predicate (:886-912). Those would otherwise be delivered again at the side's next flush (:483).

**Why delete.** No other code references these rows: `RepNotice` is used only in `src/harmony/agents/context.py`. The old run has finished, so no turn can retry with those stamps. Deleting needs no change to any reader, whereas a "superseded" marker would need filters in `_notices`, `_undelivered` and `delivered_notices`.

Other kinds (settings, mode, sharing, world date) are kept, because they are still true.

**The case this fixes.** The non-closer's last summary turn fails on its first call (`src/harmony/temporal/workflows/thread.py:195-202`, then `_note_unsummarized`). Its `closed` notice was stamped before the call (`src/harmony/temporal/activities/agents.py:427-431`) but never stored (`src/harmony/agents/runtime.py:24-31`). Without the drop, run 2 would open that side with "X closed this conversation… call `close` with it now" next to "X reopened this conversation". Once the reopening side had written, the per-run E1 guard would let that `close` through.

### 5.4 What the representatives are never told

- The raw text the member typed. Only internal Claude's guidance reaches the representative, inside the quoted `<guidance>` block, and only the reopening side's (EDIT.md §3.6 and §6).
- Comments. No Claude reads them (EDIT.md §3.8).
- Who reopened. The notice names the organization, never the member.
- The other side's guidance, steers, summaries or reasons. The counterparty learns only that the conversation reopened, then whatever message arrives.
- Anything about a failed attempt. A failed attempt queues no notice.

---

## 6. The parent request

### 6.1 Runs after the first don't talk to the request workflow

`NetworkRequestWorkflow` (`src/harmony/temporal/workflows/network_request.py:43-142`) follows run 1 of each thread, as today. It opens recipients through its sliding window of `max_active_threads` (:93-128), and it ends when every thread it knows of is terminal. Runs > 1 never signal it (§4.6). Its view of a reopened thread stays "terminal", which matters to nothing:
- its `status` query isn't read by the API (the only `run_query` caller is `src/harmony/api/routes/conversations.py:358`, for the conversation workflow);
- a reopened thread doesn't hold a window slot.

What changes is only **who closes the request**. Today the request workflow's tail closes it (`src/harmony/temporal/workflows/network_request.py:59-68`). After this change, closing goes through one locked domain function. Both the request workflow's tail and every reopened run's tail call it, and whichever runs last closes the request.

### 6.2 Request status

`network_requests` gains:
- `round int` (default 1);
- `workflow_done_at timestamptz null`: the request workflow has opened every recipient and seen every run-1 thread end.

**`requests.reopen(uow, request_id)`** is new and is called by `begin_reopen`.
- When `status == "closed"`, it sets `active` and `round += 1`, then snapshots through `_queue` (`src/harmony/domain/requests.py:458-471`).
- Otherwise it does nothing: a request whose workflow is still running, or one already reopened by another thread, stays in its round.
- `set_status` keeps "`closed` is final" (:206), so `reopen` is the one exit.

`requests.status` (:229-341) and `participant` (:380-386) need no change. A reopened thread reads `messaged`, `replied` or `needs_human` again, because they are derived from thread state.

`_requester_summaries` (:419-430) reads the current `close_summary`, which is null until the new run closes. `get_request_status` therefore reports the current round. Earlier summaries stay in `thread_runs` and the workspace (§7). Whether internal Claude should also see them is an open question.

### 6.3 `end_round`: closing the request

**`requests.end_round(uow, request_id, *, from_workflow: bool) -> RoundEnd {closed, round, conversation_id}`**, called by the new `NetworkActivities.end_round` (mirror options):

1. Lock the request row (`with_for_update`, `populate_existing`).
2. If `from_workflow`, set `workflow_done_at` if it is null. Else, if `workflow_done_at` is null, return `closed=False`: the request workflow is still running and will close the request itself.
3. If `status == "closed"`, return `closed=True` with the current round. This is a retried activity, or another caller closed it first.
4. If any thread of the request isn't terminal, return `closed=False`.
5. Otherwise set `closed`, `_queue`, and return `closed=True`.

**The request workflow's tail** (`src/harmony/temporal/workflows/network_request.py:59-69`) replaces `set_request_status(closed)` and `_notify_closed()` with:

```python
ended = await workflow.execute_activity_method(NetworkActivities.end_round,
            EndRoundInput(world_id=…, request_id=…, from_workflow=True), **mirror_activity_options())
self._status = "closed" if ended.closed else "active"
if ended.closed:
    await self._notify_closed(ended.round)
return NetworkRequestResult(status=self._status)
```

When a reopened thread is still live, the request workflow ends without closing the request, and that thread's tail closes it later.

**A reopened run's tail** calls `end_round(from_workflow=False)`. When it returns `closed=True` and the request has a `conversation_id`, it then calls the existing `notify_conversation` activity (`src/harmony/temporal/activities/network.py:418-445`) with `NetworkUpdate(round=ended.round)`.

**Lock order and races.** `begin_reopen` locks the thread, then the request. `end_round` locks the request and reads threads without locking them. Under READ COMMITTED every interleaving ends right:

| Interleaving | Outcome |
|---|---|
| `begin_reopen` commits first | `end_round` sees a live thread and leaves the request active. That thread's own `end_round` closes it later |
| `end_round` commits first | The request closes in round r and the chat is notified. `begin_reopen` then takes the request lock, sees `closed`, and moves it to active, round r + 1 |
| Both are in flight: `begin_reopen` has updated the thread but waits on the request lock | `end_round` reads the committed (terminal) thread, closes the request and commits. `begin_reopen` then re-reads the request (`populate_existing`), sees `closed`, and opens round r + 1 |
| Two reopened threads end together | Each `end_round` runs after its own terminal `set_state` committed. Whichever runs second sees no live thread and closes |
| Duplicate `closed=True` (a retry, or two callers) | Each caller may notify. The chat dedupes by (request, round) (§6.4) |

**What this drops** compared with revision 1: `ThreadChanged.run`, the `follow` input, the signal-with-start activity for runs > 1, the `close_request` refusal loop, and the slot and 24 h interplay with a running request workflow.

**Considered and not taken: offering Reopen only once the whole request has closed.** That would avoid the `workflow_done_at` column, but:
- on a request to several orgs, a closed conversation couldn't be reopened until the others ended, possibly hours later;
- the responder's Closed row would need a reason for the disabled button. Any truthful reason tells the responder the requester is talking to other orgs, which the brief keeps private (`src/harmony/agents/prompts/side_requester.md:7-8`).

This design gets the same simplification (runs > 1 never touch the request workflow) for one column.

### 6.4 What the requester's chat shows

- **While reopened.** The `RequestStatusCard` pill for that recipient goes live again, from the `network_request` snapshot and `requests.status`. Internal Claude is not woken: the member acted in the thread view, not the chat.
- **When it ends again:**
  - `NetworkUpdate` (`src/harmony/temporal/workflows/payloads.py:128-137`) gains `round: int = 1`.
  - `ConversationWorkflow.network_update` (`src/harmony/temporal/workflows/conversation.py:158-164`) dedupes on `(request_id, round)`: `_closed_requests: dict[UUID, int]` holds the highest round taken.
  - `ConversationCarry` (`src/harmony/temporal/workflows/payloads.py:140-152`) gains `closed_request_rounds: dict[UUID, int] = {}`. Ids in the old `closed_request_ids` count as round 1.
  - `network_note` (`src/harmony/temporal/activities/agents.py:783-787`) keeps its round-1 text. For round n > 1 it writes "Harmony: every conversation on request {id} has ended again (round {n}), after one was reopened. Check get_request_status, then tell the member how it went."
  - The text differs per round, so `_unstored_updates` (:745-767), which matches stored notes by text, delivers each round once.
- Internal Claude then posts to the requester's chat as it does today.
- **A responder-side reopen** also wakes the requester's internal Claude when the round ends. Otherwise the requester would only learn what the responder came back with by opening the thread. With at most 2 reopens per conversation (D3), that is at most 2 extra wakes per thread.
- The responder org has no chat for this request, so a reopen is visible to it only in its thread view.

---

## 7. Data, API, events

### 7.1 Schema (`migrations/versions/0004_reopen.py`)

No backfill: worlds from an older build are forked fresh (§2.1).

**`threads` gains public facts.** They ride the thread snapshot to both orgs: the snapshot is "every column except `world_id`" (`src/harmony/db/uow.py:86-95`).

| column | type | notes |
|---|---|---|
| `run` | int, default 1 | The current run. `run − 1` is the number of reopens |
| `end_reason` | text null | `one_of("end_reason", EndReason)`. Null while open |
| `ended_at` | timestamptz null | Set on the move into a terminal state |
| `closed_by` | Side null | The side whose representative closed it, or refused |
| `run_finished_at` | timestamptz null | Set by `finish_run`. It gates Reopen |

**`thread_runs` is new** (`src/harmony/db/models/thread.py`, next to `RepSession`). It is private and never snapshotted raw. Summaries are per org, and the carry is engine state, so neither can sit on the snapshotted `threads` row.

| column | type | notes |
|---|---|---|
| PK (`world_id`, `thread_id`, `run`) | | Only runs that actually started. Failed attempts have no row |
| `started_at`, `deadline_at`, `max_turns` | | What the run started with |
| `reopened_by_member_id`, `reopened_by_org_id`, `reopened_side`, `steer_id` | null for run 1 | `world_fk`s |
| `start_seq` | int, default 0 | The highest message seq when the run began (E1 guard, timeline) |
| `state`, `end_reason`, `closed_by`, `stop_reason`, `ended_at` | null until it ends | How the run ended |
| `requester_summary`, `responder_summary` | text null | Archived at the next reopen. Each is shown only to its own org |
| `carry` | jsonb null | `ThreadCarry`. Written by `finish_run` |
| `finished_at` | timestamptz null | |

**`network_requests` gains** `round` (int, default 1) and `workflow_done_at` (timestamptz null).

**`steers` (EDIT.md §2.1).** `kind = 'reopen'` rows are written by the API (§4.2 step 9), or by `record_reopen`. `SteerFailure` gains `unchanged` and `reopen_failed`.

### 7.2 Endpoint

| Method and path | Request | Response | Errors |
|---|---|---|---|
| `POST /api/threads/{thread_id}/reopen` (`src/harmony/api/routes/threads.py`) | `{client_key: uuid, as_member_id: uuid, body: str(1..4000)}` | 202 `ReopenStarted {steer_id, thread_id}` | 409 `member_changed` (with `member`); 404 not visible; 403 `not_in_org`; 422 `invalid`; 409 `thread_open` (with `reopened_by`); 409 `reopen_in_progress` (with `reopened_by` for the same org only); 409 `closing_in_progress`; 409 `not_reopenable` (with `block`); 409 `world_retired` |

- `ProblemCode` (`src/harmony/api/routes/problems.py:51-63`) gains `member_changed`, `thread_open`, `reopen_in_progress`, `closing_in_progress` and `not_reopenable`.
- `ApiProblem` (:66-75) gains:
  - `reopened_by: ReopenedBy | None` (`{org: OrgRef, member: MemberRef | null}`), next to `resolved_by`;
  - `block: ReopenBlock | None`;
  - `member: MemberRef | None`.
- The route doesn't go through an Update, so these errors are raised directly and not mapped through `_BY_TYPE`.

### 7.3 Read shapes

- **`ThreadSummary`** (`src/harmony/api/routes/threads.py:51-66`) gains:
  - `run`
  - `end_reason`
  - `ended_at`
  - `closed_by`
  - `run_finished_at`
- **`ThreadDetail`** (:91-98) gains:
  - `reopen: ReopenOffer | null` (§3.2), computed for the viewer's side;
  - `runs: list[ThreadRunView]`:

  ```
  ThreadRunView {
    run: int, started_at,
    reopened: {org: OrgRef, side: Side, by: MemberRef | null, steer_id: uuid | null} | null,
    ended: {state, end_reason, closed_by, stop_reason, at} | null,
  }
  ReopenOffer { state: "allowed" | "wrapping_up" | "blocked", block: ReopenBlock | null }
  ReopenBlock = "history" | "unfinished" | "not_asked" | "limit" | "refused" | "they_closed"
  ```

  - `by` and `steer_id` are filled only for the reopening org.
  - The counterparty sees `reopened.org` only.
  - Run 1 has `reopened = null`.
  - `unfinished` comes only from the API's 409, never from this read (§3.2).
- **`ThreadWorkspace`** (`src/harmony/api/routes/workspace.py:176-189`) gains `earlier_summaries: list[{run, text}]`, the actor's side's archived summaries. `close_summary` (:186, :337) stays for the current run. Edit's `steers` list (EDIT.md §8.1) carries the org's `reopen` steers too, failed attempts included.
- **`NetworkEdge`** (`src/harmony/api/routes/network.py:89-101`) gains `end_reason` and `closed_by`.
- Regenerate `web/src/api/schema.d.ts`.

### 7.4 Events

No new entity type.

| Entity | Scope | When |
|---|---|---|
| `thread` (versioned, existing) | both orgs | `begin_reopen`, `set_state` into terminal, `finish_run` |
| `steer` (EDIT.md §8.2) | the reopening org's thread scope | the API records it (`working`); `write_guidance` stores its outcome; `begin_reopen` (`done`) or `settle_steer` (`failed`) |
| `rep_guidance` (EDIT.md §8.2) | the reopening org's thread scope | `write_guidance` when the text changed |
| `network_request` (existing) | as today (`_queue`) | `requests.reopen`; `end_round` |

- The web's `THREAD_ENTITY_TYPES` (`web/src/requests/live.ts:18-26`) already refetches on `thread`. Edit adds `steer` and `rep_guidance`.
- `ENDED` (`web/src/requests/live.ts:99`) keeps both states. It ends the "is typing" row.
- A failed attempt emits only `steer` (and maybe `rep_guidance`, §13) to the reopening org. The counterparty's scope receives nothing.
- The network view's stream skips `steer` and `rep_guidance` (`LENS_HIDDEN_ENTITY_TYPES`, EDIT.md §6 item 7), so it never shows a reopen's text or a failed attempt. It does receive the `thread` snapshot, so it shows the reopen once `begin_reopen` commits.

---

## 8. Transcript and Details (web)

### 8.1 Timeline (`web/src/requests/timeline.ts`)

New entries:

```ts
interface EndedEntry  { type: "ended";  id: string; at: string; run: number; endReason: EndReason | null;
                        closedBy: Side | null; stopReason: FailureReason | null;
                        summary: string | null; latest: boolean }
interface ReopenEntry { type: "reopen"; id: string; at: string; org: OrgRef; ours: boolean;
                        by: MemberRef | null; steer: SteerView | null;
                        outcome: "working" | "opened" | "failed" }
```

- `EndedEntry` comes from `ThreadDetail.runs[].ended`. `summary` is the current `close_summary` for the latest run, and `earlier_summaries[run]` for the others.
- `ReopenEntry` comes from two sources:
  - each run ≥ 2 in `runs` (`outcome: "opened"`, `at = started_at`), joined to the org's steer by `steer_id`;
  - each of the org's own `reopen` steers that no run claims (`working` or `failed`, `at = created_at`).

  The counterparty gets only the first kind, with no steer.
- `ORDER` (:101-108) becomes consult 0, resolution 1, release 2, message 3, close 4, **ended 5, reopen 6**, note 7.
- **Every `reopen` steer folds into a reopen row.** `buildTimeline` (:117-171) leaves steers of kind `reopen` out of Edit's event rows, so the text appears once.

### 8.2 Rows

**Closed row** (`web/src/requests/Transcript.tsx`, replacing `EndedBanner`/`StoppedBanner` in `web/src/requests/RequestView.tsx:354-419`):
- Heading: "Closed · Oct 29 · both sides went quiet", with the reason from §2.1.
- Below it, our side's summary when there is one: "{Claude's} summary. Only {us} sees it." This is the existing copy (:368-369).
- The latest Closed row carries, by `ThreadDetail.reopen`:
  - `allowed`: a **Reopen** button, disabled with `budgetLine` when the budget isn't `ok`;
  - `wrapping_up`: Reopen disabled, hint "Claude is still wrapping up this conversation.";
  - `blocked`: the block's line from §3.2 in place of the button (nothing for `history`).
- When the last entry after it is a reopen row (working or failed), the Closed row shows no button. The reopen row carries the next action.
- Earlier Closed rows have no button.

**Reopen prompt.** It opens inline under the Closed row and is blue like Edit. It matches docs/mockups/round2/composer.html tab 17:
- Heading "Reopen with {counterparty}".
- A text field.
- The note "Claude writes the next message, following your usual review settings. Only {us} sees what you typed."
- **Cancel** and **Reopen** buttons. Reopen is disabled while the field is empty.

The composer stays on Comment. Edit is disabled on a closed thread, with the hint "Reopen the conversation to tell Claude what to pick up" (EDIT.md §3.9). It stays disabled while an attempt is `working`, and turns on once the thread is open.

**Reopen row, for the reopening org.** One quiet 13px row with the reopen icon in the avatar column. Under the heading: the typed text in full, in quotes, as Edit's event row shows an instruction; then the outcome line.

| Outcome | Heading | Outcome line |
|---|---|---|
| working | "Alex is reopening the conversation · Oct 30" | "Claude is picking this up…" (Ridgeline sees nothing yet) |
| opened | "Alex reopened the conversation · Oct 30" | "Claude updated its guidance and writes the next message. **See guidance**" |
| failed | "Alex tried to reopen the conversation · Oct 30" | §9's line, then "Nothing was sent, and {Them} didn't see this. Your text is saved." **Try again** |

**Try again** opens the Reopen prompt under this row, prefilled with the saved text and with a new `client_key`. Edit's Try again (EDIT.md §3.10) doesn't apply to reopen steers.

**Counterparty's reopen row:** "Henderson Lab reopened the conversation · Oct 30". It has no text and no member.

**Header pill and inbox.** "Closed" once ended, and "Closed" while an attempt is working. After a reopen, the usual live labels.

**`useReopen`** (`web/src/requests/useReopen.ts`):
- It mints `client_key` when the prompt opens and keeps it until a 202 or Cancel.
- It POSTs with `as_member_id` set to the shell's current member, and a 20 s client timeout. A retry after a timeout or a 409 reuses the key.
- On 202 it collapses the prompt. The `steer` snapshot then draws the working row.

### 8.3 Details (`web/src/requests/Details.tsx`, `web/src/requests/properties.ts`)

- "Expires" (:202-204) becomes "Closes" while open, and "Closed" with the date once ended.
- While a reopened run is live, add a row "Reopened · by Alex · Oct 30" (the counterparty sees "by Henderson Lab").
- The current guidance appears in Details as EDIT.md §8.3 specifies.

---

## 9. Failure copy

**On the prompt** (the text stays in the prompt unless noted):

| Case | Copy |
|---|---|
| 409 `member_changed` | "You switched to {Raj} in another tab, so this wasn't sent." Nothing is written. The tab then follows the switch (the switching model, task H in docs/FEEDBACK_R2_PLAN.md) |
| 409 `thread_open`, another member or org | The prompt closes and its text moves to the composer in Edit mode: "{Raj \| Ridgeline Fabrication} reopened it first. Your text is in Edit, ready to send." |
| 409 `thread_open`, the actor with different text | Same, with "You already reopened this conversation in another tab." |
| 409 `thread_open` without `reopened_by` | "This conversation is open again." |
| 409 `reopen_in_progress`, same org | "{Raj} is reopening this conversation. Try again once it's open." |
| 409 `reopen_in_progress`, other org | "Couldn't reopen it just now. Try again in a moment." |
| 409 `closing_in_progress` | "Claude is still wrapping up this conversation. Try again in a moment." |
| 409 `not_reopenable` | The block's line from §3.2. For `unfinished`: "This conversation can't be reopened: Claude didn't finish wrapping it up." |
| 409 `world_retired` | The existing world-retired copy |
| Timeout or network error | "Couldn't reach Harmony. Try again." (same `client_key`) |
| Budget not `ok` | Reopen disabled with `budgetLine(state)` |

**On a failed reopen row** (then "Nothing was sent, and {Them} didn't see this. Your text is saved." and **Try again**):

| `failure_reason` | Line |
|---|---|
| budget kinds | "Claude couldn't pick this up: {budgetLine}." Try again appears once the budget reads `ok` |
| `model_error`, paused | "Claude couldn't pick this up: {stoppedLine}." |
| `refused` | "Claude declined to write guidance from this." |
| `added_details` | "Claude's guidance added details you didn't give, so it wasn't used. Try rewording it." (EDIT.md §3.9) |
| `invalid` | "Claude's guidance came back unreadable." |
| `unchanged` | "Claude didn't find anything new to pick up in what you wrote. Try saying what it should do next." |
| `reopen_failed` | "Something went wrong reopening the conversation." |

**On the Closed row:** "Closed · Oct 30 · nothing was sent" when the reopened side's turn sent nothing.

`stoppedLine`'s docstring ("Nothing on a thread can be tried again", `web/src/chat/failureCopy.ts:55-58`) changes to say that Reopen is the way back, except after the other side's refusal (§3.2).

---

## 10. Tests

### 10.1 Backend unit (`tests/unit/`)

- The two `reopened` notice texts. `_IDENTITY_LINE` flags a forged "X reopened this conversation" line.
- `network_note` gives distinct text for rounds 1, 2 and 3.
- `_fallback` returns `REOPENED` when `reopened` is set and nothing is owed or incoming. `quiet` is false then.
- `ThreadCarry` round-trips; a pending owed result is rejected.
- `reopens.reopen_offer`, as a pure function, for each row of §3.2, including:
  - the order of the checks;
  - `wrapping_up` turning into `allowed` after `WRAP_UP_GRACE`;
  - `refused` and `they_closed` for one side only.
- `end_line` copy for each `end_reason` and side.

### 10.2 Backend integration (`tests/integration/`, Postgres)

**Domain**
- `threads.set_state` stores `end_reason`, `ended_at` and `closed_by` only on the move into a terminal state. `src/harmony/agents/tools/rep.py _close` stores `closed` and the side.
- The seed loader sets `end_reason` and `ended_at` on seeded terminal threads and creates no `thread_runs` row.
- `open_thread` creates run 1's `thread_runs` row.
- `begin_reopen`:
  - creates the run row and archives both summaries, and skips a side with no `rep_sessions` row without raising;
  - clears `close_summary`, and a later `store_close_summary` writes a new one;
  - drops undelivered `closed`/`reopened` notices and queues one `reopened` per side, keeping other kinds;
  - sets the steer `done`;
  - moves the request from `closed` to `active` with round 2, and leaves an active request in its round;
  - snapshots the thread to both orgs and the steer to the reopening org only;
  - a second call is a no-op.
- **The stale-notice case.** The non-closer's summary turn fails on its first call after its flush, then the thread is reopened. The counterparty's first run-2 system message holds only the `reopened` notice, and its `close` before the reopening side writes is refused by E1.
- `finish_run` updates the run row with its carry, then sets `run_finished_at`.
- `requests.end_round`:
  - `from_workflow` sets `workflow_done_at`;
  - a thread-side call before that is a no-op;
  - it refuses while any thread is live;
  - it closes otherwise;
  - a repeat returns `closed=True` with the same round.
- `requests.reopen` takes `closed` to active with round + 1, and leaves an active request alone. `test_a_closed_request_stays_closed` (`tests/integration/test_e4_domain.py:783`) stays as it is: `set_status` keeps its rule.
- **E1 per run.** After a reopen, the reopening side's `close` before any reply in this run gets `NOT_REPLIED`. The other side's `close` after the new message succeeds.
- **Settings notices** queue for ended threads and are flushed at the first turn after a reopen.

**API** (`start_reopen`, against the Temporal test server)
- 202, and `ThreadInput` has:
  - the carried counters;
  - `max_turns = carried + 16`;
  - the new deadline;
  - the guidance versions and `steer_seq`, including the new steer;
  - `from_state`;
  - the memo.
- The thread row is unchanged until `begin_reopen`.
- A mismatched `as_member_id` gives 409 `member_changed` and writes nothing: no steer, no start.
- A double POST with the same `client_key` returns the same 202, and only one execution is started.
- The same member with a different key:
  - with the same text, during the attempt and after it opened → 202 with the first steer;
  - with different text → 409 naming themselves.
- The other org during an attempt gets 409 `reopen_in_progress` with no `reopened_by`. After the attempt opened the thread, it gets 409 `thread_open` with `reopened_by`, and `member` is set only for the same org.
- **Abandoned attempt.** A `working` reopen steer whose execution isn't running is settled `reopen_failed`, and the new attempt proceeds.
- `run_finished_at` null:
  - with a RUNNING execution → 409 `closing_in_progress`;
  - with none → 409 `not_reopenable` / `unfinished`.
- `WorkflowAlreadyStartedError`:
  - with this steer's memo → 202 (the commit-failure retry);
  - with no memo → retried, then 409 `closing_in_progress`.
- 409 `not_reopenable` for each block.
- 404 for an invisible thread; 403 for a non-party; 422 for an empty or 4001-character body, and for a reused key with different text; 409 `world_retired` for a retiring world.
- A test-only non-admin with access can reopen.
- `ThreadDetail`:
  - the counterparty view has `by` and `steer_id` null, and no failed attempts;
  - `earlier_summaries` holds only the actor's side;
  - `reopen` is computed per side.

**Existing tests to update for `end_reason`**
- `tests/integration/test_e4_activities.py:578-580`, `:750`
- `tests/integration/test_e2_consult_tools.py:1924`

### 10.3 Workflow (`tests/workflow/test_e4_workflows.py`, stub activities as at :208-367)

- **`end_reason` per path:**
  - cap → `turn_limit` (update :541, :667-670);
  - deadline at the top of the loop, and during a person-wait → `deadline` (:689-692, :2530-2544);
  - two silent turns → `quiet` (:965-966);
  - not crossed → `unsent`;
  - model error, consult failure, refusal → `stopped`, with `closed_by` on a refusal;
  - `close` → `closed`.

  Also update :1405-1406, :1725 and :1771.
- **Owed results carried:**
  - `CLOSED` for the closer, after the close branch;
  - the last turn's `CLOSED`;
  - cut-off results for a consult and a held message when the deadline passes;
  - `finish_run` receives the carry, and it is the last activity of the run.
- **A reopen that opens:**
  - activity order: `record_reopen`, `write_guidance`, `begin_reopen`, then `rep_turn`;
  - the first `rep_turn` is the reopening side's, with `turn = carried + 1`, `reopened=True`, the new `guidance_version`, `steer_seq` at least the steer's seq, and `run_start_seq`;
  - no turn id from run 1 is reused;
  - the cap is reached after 16 new turns;
  - the new deadline applies.
- **A reopen that fails privately.** Three cases: `write_guidance` raises a budget refusal; it returns `unchanged`; it returns `rejected` / `added_details`. In each:
  - `settle_steer` runs with the right reason;
  - no `begin_reopen`, no `rep_turn`, no `end_round`, no `finish_run`, no `set_thread_state`;
  - the workflow returns the old state.
- **`begin_reopen` raises a non-retryable error:** `settle_steer(reopen_failed)` runs, then the workflow returns.
- **Updates during an attempt:** Edit's Update gets `THREAD_CLOSED_ERROR` until `begin_reopen` returns.
- **The reopening side sends nothing:** the run ends `expired`/`unsent`.
- **Responder reopens:** the responder moves first, and the requester answers next.
- **Request workflow:**
  - its tail's `end_round` with a live reopened thread returns `closed=False`, and it doesn't notify;
  - a run-2 tail's `end_round` closes and notifies with the round;
  - run 1 still signals and never calls `end_round`.
- **Conversation:** round 2 wakes internal Claude after round 1 was taken (extend :1910). The carry migrates `closed_request_ids` to rounds.
- The replay check (:474-482) covers the new histories, including a failed attempt.

### 10.4 Backend e2e (`tests/e2e/test_story_scripted.py`)

A scripted (cassette) reopen:
1. close, then Reopen with text;
2. the guidance block reaches the representative, and the raw text doesn't;
3. the representative's next message is held in Ask;
4. approve;
5. the counterparty replies and closes;
6. the requester's chat receives "ended again".

A second scripted reopen whose guidance call is a refusal: the counterparty's thread, notices and inbox are unchanged, and the request stays closed.

Cassette keys continue, so run 1's recordings are untouched. Guidance steps use Edit's `steer` role, with `turn = steers.seq`.

### 10.5 Web unit (vitest)

- `format.test.ts:88`: `rowStatus` for `expired` is "Closed", and for a stop reason is "Closed".
- New: an `endLine` case for each reason and side.
- `network.test.ts:201`: `edgeLabel` for `expired` with no summary is "Closed: both sides went quiet". `threadStatus` for `expired` is the closed style.
- The banner tests in `requests.test.tsx` (`:300-349`, Closed, Stopped and Expired) are rewritten:
  - the Closed row shows the reason for each way a thread can end;
  - Reopen opens the prompt, and Cancel restores the row;
  - Reopen is disabled when the field is empty, when the budget isn't `ok`, and while `wrapping_up`;
  - each `blocked` line shows in place of the button;
  - after a 202 the working row shows "Alex is reopening the conversation" and "Claude is picking this up…", and the Closed row has no button;
  - an opened row, and a failed row for each `failure_reason`, whose Try again opens the prompt prefilled with a new key;
  - each error in §9 shows its copy, and `member_changed` keeps the text;
  - `thread_open` moves the text into Edit;
  - the counterparty fixture shows "Henderson Lab reopened the conversation" with no text, and no failed attempts;
  - earlier Closed rows keep their archived summaries.
- `web/src/requests/timeline.ts`: ordering of `close`, `ended` and `reopen` at the same instant; reopen steers fold into reopen rows, failed ones included.
- `useReopen`: the key is stable from open to 202 or Cancel; `as_member_id` is sent.
- Fixtures: `web/src/fixtures/requests.ts` adds `expired()` with `end_reason`, plus `reopened()`, `reopenWorking()` and `reopenFailed()`. `web/src/fixtures/network.ts:135` updates. The gallery gains the Closed, working, opened, failed and blocked states.

### 10.6 Web e2e (Playwright, `web/e2e/requests.spec.ts`)

- A closed thread shows "Closed · … · both sides went quiet".
- Reopen, type, and press Reopen. The working row appears, then the next message, or a held card in Ask.
- A second browser context as the counterparty sees the reopen row without the text, then the message.
- The composer's Edit is disabled on the closed thread and works after the reopen.

---

## 11. Docs to change

- **`docs/ARCHITECTURE.md`**
  - :255: the thread lifecycle (terminal → reopen as a new run, private part first).
  - :336: the workflow table, to add `record_reopen`, `begin_reopen`, `finish_run` and `end_round`.
  - :344: terminal states, plus `end_reason` and runs.
  - :394: note that reopen relies on worlds forking per build (no migration backfill).
  - :567: "expired shows as Closed", plus the reason copy.
  - A new subsection, "Reopen", that summarises §4 and §6.
- **`docs/P1_P2_CONTRACT.md`**
  - :66, :123, :240, :281, :292, :298, :310, :339, :342, :376-377, :407, :558: state and status vocabulary, `ThreadSummary`/`ThreadDetail` fields, the new endpoint and problem codes, `NetworkUpdate.round`, and the request close moving to `end_round`.
- **`docs/LIVE_DEMO_PLAN.md`**
  - :594: "Closed" only, with a reason.
  - :792: the E1 reason no longer says "can't be reopened"; the guard is per run.
  - :836-837, :1387, :1441: end and request-close behaviour.
  - The build notes: reset local worlds after building this.
- **`docs/UI_REFERENCE.md`**
  - :140: drop "Expired" and "Stopped" from the pill list.
  - :157: Closed rows replace the banners; add Reopen, the prompt, and the reopen rows.
- **`docs/REVISION_UNSTRUCTURED.md`**
  - :94: participant `closed` can return to live.
- **Code docstrings**
  - `src/harmony/agents/tools/rep.py:18`
  - `src/harmony/domain/threads.py:251` ("never leaves its state", except through `threads.reopen`)
  - `src/harmony/domain/requests.py:200`
  - `src/harmony/temporal/workflows/network_request.py:1-9` (who closes the request)
  - `web/src/chat/failureCopy.ts:55-58`
- **The mockups** (done in this build's copies): docs/mockups/round2/turn_states.html's note on limits reads "Each reopen gets a fresh 16 turns and 20 hours. A conversation can be reopened twice.", and its D · Closed and D · Reopen frames show the Edit segment disabled, as composer.html tabs 16 to 22 do.

---

## 12. Decisions (accepted)

The user accepted D1 to D6 as recommended (decision 15).

| # | Question | Decision | Alternative not taken |
|---|---|---|---|
| D1 | Keep `expired` as an internal state? | Keep it. Show "Closed" plus a reason everywhere, using the new `end_reason` | Merge `expired` into `closed` with a data migration. Same UI, more churn in tests, seeds and storyrun |
| D2 | What limits does a reopened conversation get? | A fresh 16 turns and a new 20 h deadline per reopen. Budgets don't reset, and turn numbers continue | Only the turns left over. A conversation that hit its turn limit could then never be reopened |
| D3 | Does the other side have to agree, and what are the limits? | No consent. At most 2 reopens per conversation. After the other side's Claude closes a run your side reopened, or declines to continue, only they can reopen | No cap (revision 1). Or a consent step for the other side, which needs a new waiting state and UI |
| D4 | What happens to the parent request? | It reopens too if it had closed (round + 1). The requester's chat card goes live again, and internal Claude is told when it "has ended again", for a reopen from either side | The request stays closed, and a reopened conversation is a quiet follow-up that never reaches the chat. Or offer Reopen only once the whole request has closed (§6.3: considered, not taken) |
| D5 | Can seeded history threads be reopened? | No. They have no representative sessions and no stored carry, so no button is shown | Reopen them with an empty carry. A guess: the representative would start fresh, without its own side's seeded lines, and could repeat itself |
| D6 | Is the text required? | Yes, 1 to 4000 characters. It is what Claude picks up from, and a reopen goes ahead only when Claude's guidance changed | Allow it empty: Claude continues from the transcript alone, with no guidance |

---

## 13. Risks

- **Reopen spam, now bounded.** Two reopens per conversation, and a deliberate close or refusal by the other side's Claude hands the choice to them (D3). Each attempt, even one that fails, costs one guidance call from the shared budget.
- **The other side's people have no direct lever** while a reopened conversation waits on the reopening side, for example on a held message, for up to 20 h. They can Comment, or Edit for their representative's next turn (§3.1).
- **Reopened threads are outside the request's window.** A reopened thread doesn't hold one of the request workflow's `max_active_threads` slots, so a request's live threads can briefly exceed 8. They are bounded by its recipients and the reopen cap. MAX_TURNS and the window are "safety plumbing" (docs/ARCHITECTURE.md:250-255), and budgets still apply.
- **A request workflow that dies or hits its 24 h timeout** never sets `workflow_done_at`. That request then stays active, and reopened threads can't close it. Today the request also stays active in that case.
- **A guidance version from a failed attempt stays.** If `write_guidance` returned `updated` but `begin_reopen` then failed, the version is kept (append-only, EDIT.md §2.2), and the representative follows it whenever the thread next opens. That needs a domain error after every API check passed, so a bug, or a world being retired.
- **A workflow task stuck on a bug** blocks reopening that thread (`reopen_in_progress` or `closing_in_progress`) until the bug is fixed.
- **`WRAP_UP_GRACE` is a heuristic** (10 min). A last summary turn that takes longer shows Reopen enabled early, and the API then answers `closing_in_progress`.
- **`describe()` runs inside the API's thread lock**, only in the in-flight and not-finished cases. A slow Temporal call holds that one thread's lock.
- **Frozen tool text still says "for good"** on threads started before this build: `CLOSE_TOOL` is frozen per thread side. On such a thread the representative may tell the counterparty a close is final. New threads get the new wording (§0 item 9).
- **Assumptions in storyrun** (`src/harmony/storyrun/checks.py:412-438`, `:497-512`, `ended_at()`) still hold, because storyrun never reopens. A future reopen scenario would break them.

- **Dependency on Edit.** Reopen needs `steers`, `rep_guidance`, `write_guidance`, `settle_steer` and the guidance block as EDIT.md revision 3 specifies them, which includes the changes in §15.
- **Settings notices for ended threads** now accumulate in `rep_notices` (§5.1). The rows are small, but they are never deleted unless the thread is reopened or its world is reset.
- **The first reopened turn holds prose for a person** (Edit's `GUIDED_TEXT_HOLD`), even in Auto. The held card carries Edit's reason line, which says "your side's edit".

## 14. Open questions

Decided by the user (decision 15):
- **Q2.** New threads' `CLOSE_TOOL` and `NOT_REPLIED` drop "for good" (§0 item 9). Yes.
- **Q5.** `POST /notes` takes `as_member_id`, with 409 `member_changed` checked before anything else, as on this endpoint (§4.2 step 1). Yes.

Still open. This build uses the default given:
- **Q1.** Should the requester's `RequestStatusCard` show a line such as "Reopened by Alex", or only the live pill? Default: only the live pill.
- **Q3.** Should the representative's `close` marker (`CloseEntry`, `web/src/requests/timeline.ts:31-37`) fold into the Closed row, so a close shows as one row instead of two? Default: keep both.
- **Q4.** Should `get_request_status` show internal Claude the earlier rounds' summaries as well as the current one? Default: the current round only.

---

## 15. What this design needs from the Edit design

EDIT.md revision 3 takes all six (its §13, R1 to R6).

1. **`as_member_id` on `POST /api/threads/{id}/steers`**, with the same 409 `member_changed` before any read or write, and a route test. An Edit's text goes to the same place as a reopen's, so a stale tab after a switch would send it as the wrong org.
2. **Reopen steers are written by the API** (`steers.record`, §4.2 step 9) or by `record_reopen`, not by `record_steer`. The same `seq` rule and snapshot apply.
3. **`settle_steer` for `kind = reopen`.** Status becomes `done` only through `begin_reopen`. `settle_steer` marks a still-`working` reopen steer `failed`:
   - with the given reason;
   - else with `write_guidance`'s stored rejection reason;
   - else `unchanged` for an `unchanged` outcome;
   - else `reopen_failed` when the guidance was `updated`.
4. **`SteerFailure`** gains `unchanged` and `reopen_failed`.
5. **`write_guidance` for `kind = reopen`** computes `{reason}` from the thread row's `end_reason` and `closed_by`. The row is still Closed during the attempt.
6. **The timeline folds every `kind = reopen` steer into a reopen row** (§8.1), not only those matching a run's `steer_id`, so failed attempts don't also show as Edit rows. Edit's Try again isn't offered on them; the reopen row's Try again is.

---

## 16. Changes after review

Each issue from the engine (E) and abuse (A) checks, and what this revision did. Issues E1 and A5 are the same.

| # | Severity | Issue (short) | What changed |
|---|---|---|---|
| E1 | must-fix | A stale `closed` notice flushed for a turn whose system message was never stored survives the reopen and tells the representative to close | Taken. `begin_reopen` drops every `closed` and `reopened` notice that never reached the representative, unflushed or flushed-but-undelivered (`_undelivered`'s predicate), for both sides (§5.3). Integration test added (§10.2) |
| A5 | should-fix | Same as E1 | As E1 |
| A1 | must-fix | A reopen could run as the wrong org when act-as changes under an open tab | Taken. `ReopenRequest.as_member_id`; 409 `member_changed` before any read or write; the web keeps the text (§4.2 step 1, §9). API test added. The same check for Edit's `/steers` is §15 item 1; for `/notes` it was open question 5, now decided (§14) |
| E3 | should-fix | The guidance ran after the reopen was already visible everywhere | Taken. The run writes the guidance while the thread stays Closed. `begin_reopen` (row, notices, request, snapshots) runs only on success. A failure changes nothing shared (§4.1, §4.3) |
| A2 | should-fix | Shared changes before the private guidance; Edit's non-raising outcomes unmapped; a private stop reason on the shared row | Taken with E3. Only `updated` continues; `unchanged` and every `rejected` reason fail the attempt privately (§4.3 step 3). A failed attempt writes no stop reason to the thread row |
| E2 | should-fix | No recovery if run n+1 dies before `begin_reopen` commits | Taken. Activity failures are caught and settle the steer `failed` (§4.3). A `working` attempt whose execution isn't RUNNING with its memo is settled as abandoned by the next POST (§4.2 step 6, §4.8). Failed attempts have no `thread_runs` row, so they never block a run number |
| A6 | should-fix | Permanent stuck states: a run+1 row with a failed run; `run_finished_at` never set; a missing `rep_sessions` row | Taken. The attempt marker is the steer, with the describe-based escape (E2). A run that never finished gets `not_reopenable` / `unfinished` from `describe()`, and the UI's `wrapping_up` lasts at most `WRAP_UP_GRACE` (§3.2, §4.2 step 8). `begin_reopen` skips a side with no `rep_sessions` row (§4.4). Workflow and API tests added |
| E4 | should-fix | The request-side machinery was heavier than the behaviour needs | Taken, in a variant. Runs > 1 never talk to the request workflow. Closing moves to one locked `end_round`, called by the request workflow's tail and by each reopened run's tail, with a `workflow_done_at` column. `ThreadChanged.run`, `follow`, signal-with-start of requests, the refusal loop and the slot and 24 h interplay are gone (§6). The suggested gate (Reopen only after the whole request closed) was considered and not taken, because its disabled-button reason would tell a responder the requester is talking to other orgs (§6.3) |
| E5 | should-fix | Idempotency was inconsistent: per-submit versus per-open ids, a self-conflict, and a false `closing_in_progress` after a commit failure | Taken. The `client_key` is minted when the prompt opens and kept until a 202 or Cancel. The attempt's id is its server-minted steer id. The same member with the same text gets 202. On `WorkflowAlreadyStartedError`, the memo's `reopen_steer_id` is compared first, and a match returns 202 (§4.2) |
| E6 | should-fix | The D5 pre-ship branch and the guessed backfill are unnecessary | Taken. No backfill, no `ended_at` sentinel. `not_reopenable` / `history` now means seeded only (no `thread_runs` row, since run 1's row is created at open). The seed loader sets `end_reason` and `ended_at`. "Reset local worlds after building this" is added to the build notes (§0, §2.1, §4.6, §11) |
| A3 | should-fix | Unlimited reopens let one side overrule a deliberate close | Taken. A cap of 2 reopens. After the other side's Claude closes a run your side reopened, only they can reopen (D3, §3.2). §3.1 states what the closing org's people can do. Kept: a responder-side reopen still wakes the requester's internal Claude when the round ends, because otherwise the requester never hears what the responder came back with. The cap bounds it to 2 extra wakes (§6.4) |
| A4 | should-fix | The provoking side could reopen a thread the other side's Claude refused | Taken. After a refusal only the refusing side may reopen: `blocked` / `refused`, with copy (§3.2). The refusal-before-crossing branch now records `closed_by` (§2.1). Unit and API tests added |

**EDIT.md §3.9's five requests** of this design (its revision 2):

| # | Request | What changed |
|---|---|---|
| 1 | Use `steers.steer_id_for(world, org, member, …)` | The steer id is `steer_id_for(world, org, member, client_key)`, and it is the attempt's only id (§4.2) |
| 2 | Pass `steer_seq` and raise the watermark | `ThreadInput.steer_seq`, including the reopen steer; `record_reopen` raises `_steer_seq[side]` (§4.2 step 10, §4.3) |
| 3 | Opening order: guidance before the counterparty's messages | Taken (§4.9) |
| 4 | Prose after guidance is held, not sent | Taken (§4.3). Listed as a risk (§13) |
| 5 | Copy for an `added_details` rejection | Taken, with copy for every failure reason (§9) |
