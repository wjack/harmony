# P0: Foundations — build spec

> **Historical.** This plan describes P0 as it was built. [REVISION_UNSTRUCTURED.md](REVISION_UNSTRUCTURED.md) supersedes its descriptions of knowledge visibility, grants, offers and holding messages, which the revision removed; where they disagree, the revision wins.
>
> The live demo phase ([LIVE_DEMO_PLAN.md](LIVE_DEMO_PLAN.md)) supersedes its hosted settings: production runs live models only and refuses `MODEL_MODE=scripted`, which stays the default for local runs, tests and CI; `ACCESS_CODE` is now the master passcode that gates the whole app, and `sessions.access_code_hash` is gone; each Render service carries its own `env_vars` instead of an env group; uvicorn trusts `X-Forwarded-For` only from private addresses; and the hosted caps are those in [ARCHITECTURE.md §14](ARCHITECTURE.md).

P0 builds the platform the agents will run on. It makes **no paid model calls**. The design is [ARCHITECTURE.md](ARCHITECTURE.md); section references (§n) point there. If this spec and the architecture disagree, the architecture wins. Flag the conflict rather than silently picking one.

**Exit:**
- Switching accounts changes what a curl to the API returns.
- SSE resumes after a disconnect with no gaps.
- A scripted agent turn streams from the worker to the right members only, and survives a worker crash without a second model call.
- The budget guard's tests pass, and `harmony spend` reports $0.00 of $200 for harmony.
- `make ci-local` is green.

## Conventions (every task)

- **Tool paths.** Agent shells don't load `~/.zprofile`. Use the `Makefile` targets, which set `PATH`. Outside make, call `/opt/homebrew/bin/uv`, `/opt/homebrew/bin/pnpm` and `/opt/homebrew/bin/temporal`, and `~/.orbstack/bin/docker`.
- **Python 3.13**, uv project at the repo root, package `src/harmony`. **Node 26**, pnpm 12.15, frontend in `web/`.
- **Secrets.** `.env.local` holds the real keys. Never print it, `cat` it, echo a key, or write a key into another file. Code reads it through `harmony.config`.
- **No paid API calls.** `MODEL_MODE=scripted` is the default everywhere. No test may reach `api.anthropic.com`.
- **Git.** Only the coordinating agent commits. Task agents never run `git commit`, `git push`, `git checkout` or `git stash`.
- **Shared files.** After T1, only T1's owner (or the integration task) edits these:
  - `pyproject.toml` and `uv.lock`
  - `compose.yaml`, `Makefile`, `Procfile.dev`
  - `src/harmony/api/app.py` and `src/harmony/cli.py`

  If another task needs a dependency or a change to one of these files, it reports that in its result instead of editing the file.
- **File ownership.** Each task owns the files listed for it. Don't edit another task's files. If you must, say so in your result.
- **Test isolation.** Tests get a fresh database per test from a migrated template (fixtures in `tests/conftest.py`). When tasks run in parallel, each sets `HARMONY_TEST_DB_PREFIX` to its task id, so template names never collide. Workflow tests use `WorkflowEnvironment` (time-skipping, or `start_local` with search attributes), never the shared Compose Temporal.
- **Style.** Typed Python (pyright strict on `src/`), ruff clean, async SQLAlchemy 2.0 style, Pydantic v2 models for all API and event payloads. Keep modules small and named for what they do. Comments explain why, not what.

## Repo layout

```
pyproject.toml  uv.lock  .python-version  Makefile  compose.yaml  Procfile.dev  alembic.ini
src/harmony/
  config.py  cli.py  cli_*.py
  api/app.py  api/deps.py  api/routes/{health,session,worlds,members,threads,events}.py
  db/engine.py  db/uow.py  db/repo.py  db/models/*.py
  events/{bus,pg_bus,sse,cursor,stream,schemas}.py
  auth/{session,access}.py
  worlds/{service,retirer,retirer_temporal}.py
  seeds/{schema,ids,dates,loader}.py
  llm/{pricing,requests,anthropic_model,checkpoint,ledger,budget,errors,scripted,contract}.py
  temporal/{client,worker,start,policies}.py  temporal/workflows/*.py  temporal/activities/*.py
  storage/{base,localfs,s3}.py
migrations/  scenarios/harmony-demo/  tests/{unit,integration,workflow,e2e,fixtures}/
web/  infra/{minio,terraform}/  scripts/  .github/workflows/
```

## Tasks

### T1 — Scaffold (owner of shared files) · sequential

1. **Toolchain and branch.** Run `uv python install 3.13`. Create branch `p0-foundations` (the coordinator does this before T1).
2. **`pyproject.toml`.** Pin every P0 dependency now:
   - Runtime: fastapi, uvicorn[standard], sse-starlette, pydantic, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, temporalio, anthropic, itsdangerous, typer, pyyaml, httpx, boto3, watchfiles, honcho.
   - Dev: pytest, pytest-asyncio, hypothesis, ruff, pyright.
   - Configure ruff, pyright (strict for `src/harmony`) and pytest (asyncio mode auto; markers `minio`, `compose`) in the same file.
3. **`Makefile`.** It starts with `export PATH := /opt/homebrew/bin:$(HOME)/.orbstack/bin:$(PATH)`. Targets:
   - setup and run: `doctor`, `up`, `down`, `migrate`, `seed`, `dev` (honcho runs `Procfile.dev`: api, worker, web)
   - checks: `test`, `lint`, `typecheck`, `gen-api`, `web-ci`, `ci-local`, `p0-exit`
4. **`compose.yaml`.**
   - `postgres:17`, with a `pg_isready` healthcheck and a named volume.
   - `temporal`: image `temporalio/temporal:1.9.1`, `user: "0:0"`, command `server start-dev --ip 0.0.0.0 --db-filename /data/temporal.db --search-attribute WorldId=Keyword --search-attribute OrgId=Keyword`, ports 7233 and 8233, a named volume and a healthcheck.
   - `minio`, under profile `storage`, built from `infra/minio/Dockerfile` (written by T10).
5. **`config.py`** (pydantic-settings, reads `.env.local`). Keys:
   - Database: `DATABASE_URL`, `DATABASE_DIRECT_URL`.
   - Temporal: `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY`.
   - Model: `ANTHROPIC_API_KEY`, `MODEL_MODE` (`scripted`|`live`, default `scripted`), `MODEL_OVERRIDE` (default `claude-opus-5-5` when `APP_ENV=local`).
   - Spend: `SPEND_LEDGER_PATH`, `SPEND_PROJECT=harmony`, `DEV_BUDGET_USD=200`, `ALERT_THRESHOLDS=50,100,200`.
   - App: `SESSION_SECRET`, `APP_ENV` (`local`|`ci`|`prod`), `ACCESS_CODE`, `ADMIN_SECRET`, `OBJECT_STORE` (`localfs`|`s3`), the S3 settings, `BUILD_SHA` (from `RENDER_GIT_COMMIT` or git), `SCENARIO_ANCHOR=2026-10-26`.
   - Add a comment with the connection budget (§8).
   - Extend `.env.local.example` with the non-secret keys (no values), plus `SPEND_LEDGER_PATH=/Users/willjack/Projects/ant-project/spend_ledger.jsonl` and local defaults. Append the same non-secret local defaults to `.env.local` without touching existing lines.
6. **Stubs.** Write only Protocols, signatures, typed placeholders and empty routers, so parallel tasks fill disjoint files:
   - `db/engine.py`: the async engine and session factory.
   - `db/uow.py`: the `UnitOfWork` Protocol with `add_event(...)` and commit semantics documented.
   - `events/bus.py`: the `EventBus` Protocol.
   - `events/schemas.py`: the initial typed event union: `world.created`, `world.retired`, `session.member_changed`, `thread.access_changed`, `entity.snapshot`, `demo.notice`, `stream.delta`, `stream.typing`, `stream.reset`, `hello.tick`.
   - `auth/session.py`.
   - `worlds/retirer.py`: the `Retirer` Protocol and a `SyncRetirer`.
   - `llm/__init__.py`: the `ModelClient` Protocol.
   - `storage/base.py`: the `ObjectStore` Protocol.
   - `api/routes/*.py`: empty routers.
7. **`api/app.py`.** `create_app()` mounts every router under `/api`, serves `/api/healthz`, mounts `/assets` from `web/dist/assets` with immutable caching when present, and ends with a catch-all for paths outside `/api` that returns `web/dist/index.html` with `no-cache` (or a placeholder page when the build is absent). No GZip on SSE.
8. **`cli.py`.** The typer root. It lazily registers `cli_seed`, `cli_spend`, `cli_dev` and `cli_llm`, each written by its owning task.
9. **`tests/conftest.py`** provides three fixtures:
   - a session-scoped migrated template database named from `HARMONY_TEST_DB_PREFIX`;
   - a per-test `CREATE DATABASE … TEMPLATE` clone, with an async engine bound to it;
   - a `temporal_env` fixture using `WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which("temporal") or "/opt/homebrew/bin/temporal", search_attributes=[WorldId, OrgId])`, plus a time-skipping variant.
10. **`Procfile.dev`.** `api`: uvicorn `harmony.api.app:create_app --factory --reload` on 8000. `worker`: `harmony worker` under watchfiles. `web`: `pnpm -C web dev`.

**Acceptance:**
- `make doctor` prints the tool versions.
- `make up`, then `docker compose ps` shows postgres and temporal healthy.
- `temporal operator search-attribute list --address localhost:7233` lists WorldId and OrgId.
- A workflow started on the Compose Temporal survives `docker compose restart temporal`.
- `make lint typecheck test` passes.
- `uv run harmony --help` works.
- `create_app()` builds.

### T2 — Schema and initial migration · after T1

SQLAlchemy 2 models, one file per aggregate, plus `migrations/versions/0001_initial.py`, covering every table in §4.

- **World-scoped tables:** PK `(world_id, id)`, and every FK to a world-scoped table includes `world_id`. Rows that are snapshot entities carry an integer `version`.
- **`events`:** PK `(world_id, seq)`. Columns: `type`, `scope_kind` (`world`|`org`|`member`|`thread`|`demo`|`session`), `org_id`, `member_id`, `thread_id`, `entity_type`, `entity_id`, `entity_version`, `payload` (jsonb), `created_at`.
- **`world_event_seq`:** `(world_id PK, seq)`, a separate table from `worlds`.
- **`worlds`:** `scenario`, `mode` (`live`|`replay`), `status` (`live`|`retiring`|`retired`), `build_sha`, `created_at`, `ff_offset_s`, `budget_lineage`.
- **`sessions`** (not world-scoped): `id`, `desk_world_id`, `desk_member_id`, `network_world_id`, `network_member_id`, `epoch`, `budget_lineage`, `access_code_hash`, `created_at`, `last_seen_at`.
- **`budgets`** (not world-scoped): `key` PK (`dev`, `lineage:<id>`, `access:<hash>`, `daily:<date>`, `live-network`), `cap_usd`, `spent_usd`, `reserved_usd`. **`budget_reservations`:** `id`, `keys`, `est_usd`, `status`, `created_at`.
- **`model_calls`:** PK `(turn_id, step_idx)`. Columns: `world_id`, `request_text`, `response_text` (**text, not jsonb**), `served_model`, `usage_json`, `cost_usd`, `attempt`, `created_at`.
- **`agent_turns`:** per turn. Columns: `role`, `org_id`, `thread_id` or `conversation_id`, `side`, `exposed_added` (json), `cost_usd`, `latency_ms`.
- **`rep_sessions`:** `(world_id, thread_id, side)`. Columns: `system_text` and `tools_text` (text), `model`, and the exposed set (json).
- **`rep_notices`:** `(world_id, thread_id, side, seq)`. Columns: `kind`, `text`, `flushed_turn_id`.
- **`rep_settings`** holds the versioned, prompt-facing fields. **`rep_policy`** holds the live policy fields.
- **`thread_messages`:** unique `(world_id, thread_id, seq)`. **`chat_messages`:** unique `(world_id, conversation_id, seq)`.
- **`escalations`:** `kind` (`consult`|`offer`|`acceptance`|`countersign`|`outbound_review`), `consult_id`, `offer_id`, `status`, `dedupe_key` (unique per world), `current_draft_rev`. **`escalation_drafts`:** PK `(world_id, escalation_id, draft_rev)`; columns `answer`, `sources` (json), `scope`, `offer_draft` (json), `suggested_actions` (json), `route` (json), `rationale`.
- **Design revision (2026-10-25): build these, not the earlier shape.** There is no `routing_rules`, `capabilities`, `proposals` or `intents` table. Instead:
  - **`offers`:** `thread_id`, `from_org_id`, envelope columns `amount_minor` (bigint), `currency`, `expires_at`, `counterparty_org_id`, plus `terms_md` (text), `created_by_turn_id`, `created_by_member_id`, `supersedes_id`, `status` (`draft`|`offered`|`accepted`|`binding`|`withdrawn`|`expired`), `acceptance` (json), `countersignature` (json), `signatures` (json), `version`.
  - **`consults`:** `thread_id`, `side`, `question`, `status` (`answering`|`released`|`needs_human`|`declined`), `dedupe_key`, `version`.
  - **`routing_decisions`:** `escalation_id`, `assignees` (json), `reason`, `decided_by` (`claude` or a member ID), `corrected_from` (json), `created_at`.
  - **`members`** gains `profile` (text) and `approval_limit_minor`. **`rep_policy`** gains `routing_prompt` (text), `default_assignee_id`, `auto_countersign_limit_minor`, `auto_release_allowlist` (json). **`rep_settings`** has no rulebook column.
  - **`thread_messages.kind`** is `message`|`offer`|`accept`|`decline`|`close`|`holding`, with a nullable `offer_id`.
- **`network_requests`:** `needed_by`, `services`, `authority` (json), `shareable` (json).
- **`threads`:** `state`, `side_to_move`, `deadline_at`.
- **`db/repo.py`:** world-scoped repository helpers that require `world_id`. There is no get-by-bare-id helper.

**Acceptance:**
- `make migrate` works on an empty database. `alembic downgrade base && alembic upgrade head` round-trips, and `alembic check` reports no drift.
- `tests/unit/test_schema.py` asserts these by inspecting metadata:
  - every world-scoped PK starts with `world_id`, and every FK to one includes it;
  - `events` has PK `(world_id, seq)`, and `world_event_seq` is a separate table;
  - `model_calls` has PK `(turn_id, step_idx)`, with response text stored as text;
  - `budgets` and `sessions` carry no `world_id` in their PK;
  - `thread_messages` has a unique `seq`;
  - every snapshot entity has a `version`.

### T3 — Seed loader and P0 scenario skeleton · after T2, parallel with T4–T7

- `seeds/schema.py` holds Pydantic models (strict, `extra="forbid"`) for the scenario YAML (§11.7).
- `seeds/ids.py` generates `uuid5(SCENARIO_NS, f"{scenario}/{kind}/{slug}")`.
- `seeds/dates.py` resolves relative dates (`+24d`) against the scenario anchor, **2026-10-26**.
- `seeds/loader.py` loads a world in one transaction through the UnitOfWork and emits `world.created`. It also exposes `load_world(session, scenario) -> world_id` for T4.
- `scenarios/harmony-demo/` holds structurally complete skeletons for henderson, northlake, ridgeline, calder and vantage. Each has members with roles, profiles and approval limits, knowledge at all three visibilities (including a price list as an ordinary knowledge item), `rep_settings`, and `rep_policy` with a routing prompt and default assignee. The members:
  - Henderson: Alex Chen (member, $2,000 limit), Prof. Maya Henderson (approver, $5,000 limit).
  - Northlake: Dr. Helen Ward (admin), Raj Patel (member).
  - Ridgeline: Dana Okafor (admin).
  - Calder: one admin.
  - Vantage: one member.
  - Round 2 (FEEDBACK_R2_PLAN §1.7, E1) made every demo member an admin; tests that need a non-admin make one in their fixtures.
- Take names, slugs, titles, approval limits, routing prompts and knowledge visibility from [SCENARIO.md](SCENARIO.md) wherever it covers them. Skeletons need only a few knowledge items per org; P1 fills in the rest.
- **Access fixtures.** Seed one historical closed Northlake ↔ Henderson thread. Give Helen `thread_access` reason `routed` and Alex reason `routed` on the Henderson side. Raj gets no access. Add one thread routed to Raj only. Access tests and the act-as check depend on this.
- `cli_seed.py` provides `harmony seed --scenario harmony-demo`, which prints the world ID and per-table row counts.

**Acceptance:**
- Seeding twice gives two worlds with identical seed IDs, kept apart by `world_id`.
- `tests/unit/test_seed_loader.py` checks:
  - `+24d` resolves to 2026-11-19;
  - an unknown field or a bad visibility fails with the file and YAML path in the message;
  - a failure in one org leaves no rows behind.

### T4 — Sessions, worlds, act-as, access, read endpoints · after T2/T3

- **`worlds/service.py`.**
  - `create_world(scenario)` runs lazily from `POST /api/session/start`, never on page view.
  - `fork_and_reset(session)` creates a new world under the same `budget_lineage`, repoints the session (keeping the same member slugs), marks the old world `retiring`, and calls the `Retirer`.
  - `ensure_current_build(session)` forks when `world.build_sha` doesn't match the running build.
- **`auth/session.py`.** A signed HttpOnly cookie (itsdangerous; SameSite Lax; Secure outside local) holds only the session ID. The `sessions` row holds worlds, members and epoch. FastAPI dependencies resolve `(session, world, member)`. The world defaults to the desk world; `?world=network` selects the network world.
- **`auth/access.py`.** `can_see_thread(member, thread)` is true when the member is routed, shared or an org admin, per `thread_access`. Every thread query is filtered through it.
- **Routes:**
  - `GET /api/session`
  - `POST /api/session/start`
  - `POST /api/session/act-as {member_id}`: the member must be in the session's world, else 403. Bumps `epoch`, emits `session.member_changed` (session scope), and `pg_notify('session', session_id)`.
  - `GET /api/members` (grouped *Featured* / *All network members*)
  - `POST /api/worlds/reset`
  - `GET /api/threads` (inbox list for the acting member)
  - `GET /api/threads/{id}`: **404, not 403**, without access.
  - `POST /api/threads/{id}/share {member_id, question?}`: grants exactly one member and emits `thread.access_changed`.
  - `POST /api/admin/wipe`: requires `ADMIN_SECRET`.
- Every list or snapshot response carries an `X-Harmony-Cursor` header from T5's `events/cursor.py`. Code against the stub until T5 lands.
- `scripts/p0_actas_check.sh`: curl with a cookie jar, printing PASS/FAIL per check.

**Acceptance.** `p0_actas_check.sh` passes:
1. start creates a world;
2. Alex sees only the Henderson-visible thread;
3. Helen sees the routed thread;
4. Raj sees neither, and `GET /api/threads/{id}` returns 404 (round 2 made every demo member an admin, so the script now checks Jordan at Vantage, an org that isn't on the thread);
5. act-as with a member ID from another world returns 403;
6. reset changes the world ID, the old world is `retiring`/`retired`, and the member slugs still resolve.

`tests/integration/test_access.py` and `test_worlds.py` cover cross-world isolation (two worlds share seed IDs, and no endpoint ever crosses), sharing grants exactly one member, and reset carrying the budget lineage.

### T5 — Event outbox, LISTEN bus, gap-free SSE, scoped streams · after T2

- **`db/uow.py`.** The UnitOfWork collects events. At commit, its **last** statements are:
  1. `UPDATE world_event_seq SET seq = seq + :n WHERE world_id = :w RETURNING seq`
  2. the `INSERT` into `events`
  3. `pg_notify('events', '<world>:<seq>')`

  All three run inside the transaction. Nothing takes other locks after the seq update. `world_event_seq` rows are created with the world.
- **`events/pg_bus.py`.** One dedicated asyncpg connection per process on `DATABASE_DIRECT_URL`, outside the SQLAlchemy pool, listening on `events`, `stream` and `session`.
  - Notifications are wake-up hints only.
  - On termination (`add_termination_listener`), it reconnects with backoff and wakes every subscriber.
  - A 5 s poll runs for each world that has subscribers.
- **`events/sse.py`.** Each subscriber keeps a cursor and loops: wait for a wake or poll, then `SELECT … WHERE seq > cursor ORDER BY seq LIMIT 500`, filter by scope, and send with SSE `id = seq`.
  - Scope rules:
    - member events go to that member;
    - org and thread events go to members of that org with thread access, or admins;
    - demo events go to everyone, with no entity payload;
    - session events go to that session;
    - everything goes to the network lens (`?lens=network`).
  - It honors `Last-Event-ID` or `?after=`, sends a heartbeat every 15 s, and closes when the session epoch changes.
- **`events/cursor.py`.** Reads the world's current seq inside the snapshot's transaction.
- **`events/stream.py`.** Publishes coalesced token deltas (about 200 ms) through a dedicated autocommit notify connection, only when the thread has a subscriber.
  - Payload: `(world, org, thread, turn, attempt, text)`.
  - Deltas go only to the producing org's members with thread access, and to the lens. Everyone else gets `stream.typing`. A retried attempt emits `stream.reset`.
- **`api/routes/events.py`.** `GET /api/events`.
- **`scripts/sse_resume_check.py`.** Opens a stream, generates events, drops the connection, generates more, resumes with `Last-Event-ID`, and checks the sequence is contiguous.

**Acceptance** (`tests/integration/test_events_ordering.py` and `test_sse.py`):
- 20 concurrent writers × 50 events into one world, with a reader reconnecting at random cursors, deliver seq 1..1000 exactly once, in order.
- A writer that allocates its seq and then sleeps blocks the next writer, so commit order equals seq order.
- After `pg_terminate_backend` on the LISTEN connection, subscribers receive new events within 6 s.
- A snapshot cursor plus `?after=` delivers events committed between the snapshot and the subscribe.
- A counterparty member receives no `stream.delta` and none of the other org's org-scoped events.
- Demo events carry no entity payload.
- An epoch bump closes the stream.

### T6 — Model client, pricing, $200 budget guard, ledger, alerts, ScriptedModel · after T2

- **`llm/pricing.py`.** Per-model prices:
  - `claude-opus-5-5`: $4 / $20, cache read $0.20, 5-minute write $5, 1-hour write $8.
  - `claude-haiku-5-5`: $0.10 / $0.50 up to 100K prompt tokens, $0.50 / $2.50 above; cache read 0.1× input, writes 1.25× (5-minute) and 2× (1-hour).
  - Opus fallbacks: `claude-opus-5` at $5 / $25 with a $0.50 read, and `claude-opus-4-8`.
  - Web search: $0.01 per search.
  - Cost is computed from `usage.iterations` when present, each iteration priced at its served model.
- **`llm/requests.py`.** Request builders keyed on the **resolved** model, after `MODEL_OVERRIDE`. Each role sets its own effort.
  - Opus 5.5: `thinking: {type: "adaptive"}` (with `display: "updates"` plus its beta when the caller streams to the UI); never disabled thinking; no sampling parameters; no forced `tool_choice`; `fallbacks: "default"` with beta `server-side-fallback-2026-07-01` (use `extra_body` if the SDK lacks the field).
  - Haiku 5.5: no `fallbacks`, no sampling parameters; `thinking: {type: "disabled"}` only when effort ≤ high.
  - Representative calls set `disable_parallel_tool_use`.
  - Streaming client tools set `eager_input_streaming`.
- **`llm/anthropic_model.py`.** The beta client, `AsyncAnthropic(max_retries=1)`. Streams with `on_delta` and heartbeat callbacks. It stores the raw final message as JSON text, without `exclude_none` or `exclude_defaults`, so empty thinking blocks keep their signatures. It records `response.model`. Refusal is handled before content is read.
- **`llm/checkpoint.py`.** `call_model_checkpointed(turn_id, step_idx, request)`:
  - If a `model_calls` row exists, return it with no call.
  - Otherwise reserve, call, then in its own transaction `INSERT … ON CONFLICT DO NOTHING`, and settle.
  - On an exception, settle the partial usage from the last `message_delta`, then re-raise.
- **`llm/ledger.py`.** Append-only JSONL at `SPEND_LEDGER_PATH`, written under `fcntl.flock`.
  - Line fields: `project: "harmony"`, `kind` (`call`|`alert`|`ack`), `env`, `world_id`, `role`, `model_requested`, `model_served`, `turn_id`, `step_idx`, `attempt`, the token fields, `web_searches`, `cost_usd`, `ts`.
  - Alert and ack lines carry `cost_usd: 0.0`, because the existing counterbench reader sums `cost_usd` on every line.
  - Never rewrite or truncate the file.
- **`llm/budget.py`.** `BudgetGuard`, an `asyncio.Lock` plus `flock`.
  - It reserves the worst case (`max_tokens` × output price + estimated input) against the cap, minus harmony-only spend, minus in-flight reservations.
  - It writes one `alert` line on first crossing $50, $100 and $200, and raises the non-retryable `BudgetExhausted` at the cap.
  - With `MODEL_MODE=live` and `APP_ENV=local`, a missing or unreadable ledger means it **refuses to start and never creates the file**.
  - It also reserves and settles against the Postgres `budgets` rows (session lineage and daily) for hosted caps, atomically (`UPDATE … WHERE spent + reserved + est <= cap`).
- **`llm/scripted.py`.** `ScriptedModel` plays cassette YAML keyed by `(scenario, org, role, thread, turn, step)`.
  - It emits stream events and content blocks (empty-text thinking blocks with a placeholder signature, text, tool_use) with synthetic usage priced at $0.
  - It records every request body and writes no ledger lines.
- **`llm/contract.py`.** A checker over recorded requests:
  - every `role:"system"` message follows a user message and is last or followed by an assistant message;
  - consecutive requests for the same (thread, side) are byte-prefix extensions;
  - per-model rules: no sampling parameters; on Opus, no disabled thinking and no forced `tool_choice`; on Haiku, no `fallbacks`.
- **CLI.**
  - `cli_spend.py`: `harmony spend [--ledger PATH] [--new-alerts] [--ack]`.
  - `cli_llm.py`: `harmony llm ping --live`, one tiny Opus call. **Don't run it.** The coordinator decides.

**Acceptance** (unit tests, no network):
- Golden prices per model, including Haiku's step above 100K and a fallback iteration priced at its served model.
- 100 concurrent $3 reservations against a $200 cap never exceed it.
- The 50, 100 and 200 alert lines are each written exactly once.
- A counterbench-style reader, `sum(json.loads(l)["cost_usd"] for l in f)`, works over harmony call, alert and ack lines.
- Live mode with a missing ledger refuses to start.
- The builders' per-model rules hold.
- A stored `model_calls` row is reused with no call.
- An empty thinking block keeps its signature through a store-and-load round trip.
- `harmony spend --ledger /Users/willjack/Projects/ant-project/spend_ledger.jsonl` prints `harmony $0.00 of $200.00` and ignores the 1,355 non-harmony lines. This read-only check against the real file is allowed.

### T7 — Temporal worker, start helpers, policies, RetireWorldWorkflow · after T2

- **`temporal/client.py`.** Connects from env, using the Pydantic data converter:
  - locally, to the dev server;
  - on Cloud, to `<namespace>.<account>.tmprl.cloud:7233` with `api_key` and TLS.
- **`temporal/worker.py`.**
  - Task queues `harmony-main` and `harmony-model`, with capped `max_concurrent_activities` and `max_cached_workflows`.
  - SIGTERM calls `worker.shutdown()`, with a graceful timeout of about 90 s.
  - It logs the build SHA.
- **`temporal/start.py`.** `start_world_workflow()` always sets the typed `WorldId` (and `OrgId` when given) search attributes, sets `execution_timeout=24h` (spanning continue-as-new) and `run_timeout=24h`, and refuses non-live worlds.
- **`temporal/policies.py`.** `model_activity_options()`:
  - `RetryPolicy(maximum_attempts=3)`, with non-retryable `BadRequest`, `BudgetExhausted` and `WorldRetired`;
  - `heartbeat_timeout` 30 s, `start_to_close` 600 s;
  - a world-status guard that raises a non-retryable `ApplicationError`.
- **`workflows/hello.py`.** `HelloWorkflow` emits three `hello.tick` events through an activity, with a timer between them.
- **`workflows/retire_world.py`.** `RetireWorldWorkflow`, ID `retire:{world}`, with **no** `WorldId` search attribute. Its activity:
  1. sets status `retired` and emits `world.retired`;
  2. terminates the root workflows by recorded IDs;
  3. loops `WorldId = x AND ExecutionStatus = 'Running'`, terminating each workflow individually (NotFound is OK), until two consecutive lists are empty;
  4. deletes the world's rows after a configurable TTL (0 in tests).
- **`worlds/retirer_temporal.py`** implements `Retirer`.
- **`cli_dev.py`** provides `harmony worker` and `harmony dev hello --world <id>`.
- **API startup** retires `live` worlds that have workflows on record when Temporal has none of them running, and finishes the retirement of every world left `retiring`. It retires nothing when the configured namespace does not exist.

**Acceptance:**
- `uv run pytest tests/workflow -q`: `HelloWorkflow` runs time-skipping.
- `RetireWorldWorkflow` runs on `start_local` with search attributes:
  - three running workflows in world A are terminated;
  - world B's workflow keeps running;
  - the retire workflow completes and never matches its own query.
- An activity on a retired world raises `WorldRetired`, and the workflow fails without retrying.

### T8 — Web scaffold, typed client, SSE dispatcher · after T1, parallel

- **`web/`.** Vite, React 19, TypeScript (strict), Tailwind v4, TanStack Query, React Router, Zustand, vitest and eslint. `package.json` pins `packageManager: "pnpm@12.15.0"` and `engines.node: ">=26"`. `vite.config.ts` proxies `/api` to `localhost:8000`.
- **`scripts/gen_api.sh`.** Exports FastAPI's `openapi.json` (without starting a server) and runs `openapi-typescript` into `web/src/api/schema.d.ts`. `openapi-fetch` is the client.
- **`src/realtime/dispatcher.ts`.** One EventSource per tab, at `/api/events?after=<snapshot cursor>`.
  - It applies entity snapshots only when their version is newer than the cached one.
  - On `session.member_changed`, it closes the stream, clears the query cache and reopens from a fresh cursor.
  - Demo events go to a subscriber list (toasts, badges, walkthrough) and never touch the cache.
  - A `BroadcastChannel` reloads the visitor's other tabs on an account switch.
- **Minimal UI.** A start button, an account switcher and an inbox list. The real shell is P2.

**Acceptance:**
- `make web-ci` passes: install with a frozen lockfile, typecheck, lint, test and build.
- Vitest covers:
  - version-gated application;
  - close, clear and reopen on `member_changed`;
  - demo events never touching the cache;
  - the BroadcastChannel reload.

### T9 — End-to-end scripted turn · integration, after T4–T8

`ScriptedTurnWorkflow` runs on `harmony-model` with `model_activity_options`. Its activity:
- calls `call_model_checkpointed` with `ScriptedModel`;
- publishes coalesced deltas through `events/stream.py`;
- heartbeats from a background task;
- commits a `thread_message` with ID `uuid5(turn_id, step_idx, tool_use_id)` and its event through the UnitOfWork.

**Acceptance** (`tests/e2e/test_scripted_turn_stream.py`):
- A Northlake member's stream receives `stream.delta` events, then the committed message.
- A Henderson member's stream receives only `stream.typing`, then the committed message.
- After an injected crash once the response is stored, the retry makes no second model call (the ScriptedModel call count stays 1) and writes no duplicate message.
- No ledger line is written.

### T10 — Object storage · after T1, parallel

- `storage/localfs.py` and `storage/s3.py` (boto3 with an endpoint URL) implement the Protocol: `put`, `get`, `delete`, `exists`, keyed by world and org.
- `infra/minio/Dockerfile`:
  - Clones `github.com/minio/minio` at tag `RELEASE.2025-10-15T17-29-55Z` (depth 1). Check the tag, and that it's the CVE-2025-62506 fix, before relying on it.
  - Builds with the Go version its `go.mod` requires.
  - Runs as a non-root user with a data volume and a healthcheck.
  - A small `infra/minio/init.sh` creates the bucket.

**Acceptance:**
- The LocalFs unit tests pass.
- `docker compose --profile storage up -d --build minio` builds and becomes healthy.
- `pytest -m minio tests/integration/test_storage_minio.py` does a put, get and delete through S3.

### T11 — Dockerfile, CI and Terraform skeleton · after T1, parallel

- **`Dockerfile`.**
  - Stage 1: `node:26-bookworm-slim` with corepack or a pinned pnpm, running `pnpm -C web build`.
  - Stage 2: `python:3.13-slim` with a pinned uv binary, running `uv sync --frozen --no-dev`.
  - Shell-form CMD: `uvicorn harmony.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'`.
  - `.dockerignore` excludes `.env*`, `node_modules`, `.venv` and `.git`.
- **`compose.ci.yaml`.** Runs api and worker containers with `MODEL_MODE=scripted` and `OBJECT_STORE=localfs`.
- **`.github/workflows/ci.yml`.** Jobs:
  - `python`: setup-uv, a postgres:17 service, the Temporal CLI, then ruff, pyright and pytest.
  - `web`: node 26, pnpm, `make web-ci`.
  - `compose-smoke`: build the image, start the stack, run `p0_actas_check.sh`, `sse_resume_check.py` and the scripted-turn e2e.
  - `terraform`: `fmt -check` and `validate`.
  - CI never references `ANTHROPIC_API_KEY`.
- **`infra/terraform/`.** Local state; `*.tfstate` is gitignored. Modules:
  - `render`: web, worker, Postgres 17 `basic_1gb`, env group, and the MinIO private service with a disk. All use `auto_deploy_trigger = "checksPass"`. The web service and the worker both run the pre-deploy migration (serialized by an advisory lock), and the worker sets `max_shutdown_delay_seconds`.
  - `temporal`: namespace `harmony` in `aws-us-west-2` with API-key auth, the `WorldId` and `OrgId` search attributes, a service account, and an API key with a literal `expiry_time`.
- **`scripts/tf.sh`.** Loads `.env.local` into `TF_VAR_*` without echoing it. **No `terraform apply`** in P0.

**Acceptance:**
- `docker build` succeeds, and `docker run --rm … python -c "import harmony"` exits 0.
- `terraform fmt -check` and `validate` pass.
- `make ci-local` mirrors the CI jobs.

### T13 — P0 exit check · last

`scripts/p0_exit.sh` (`make p0-exit`):
1. `up`, `migrate`, `seed`.
2. Start the API and worker in the background with `MODEL_MODE=scripted`.
3. Run `p0_actas_check.sh`, `sse_resume_check.py` and the scripted-turn e2e.
4. Run `harmony spend` against a temp ledger with simulated crossings (alerts exactly once), and against the real root ledger (harmony $0.00).
5. Print PASS or FAIL per check, then tear down the background processes.

## Deferred to P0.5 (needs the user)

Terraform `apply` for Render and Temporal Cloud, the deployed SSE heartbeat check, and the MinIO put/get on Render. These start once GitHub is connected to Render and the user approves the spend, about $20–50 a month.
