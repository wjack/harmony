# Harmony

Harmony is a prototype of a network where every organization is represented by Claude.
Each org runs two agents with a context firewall between them: an internal Claude
that holds the org's full context and talks only to its members, and a representative
("Claude for Northlake") that is the only agent allowed to talk to other orgs. A
researcher asks their Claude for help, the network finds the labs and vendors who can
do the work, and representatives talk it through in free-form threads. Code enforces
only the plumbing: who is speaking, whose turn it is, the firewall, and review by each
org's mode. A teammate approves answers from their side (and, in Ask mode, outgoing
messages) from an inbox; prices, terms and agreement are just messages. The demo lets a
visitor play both sides of that exchange, then watch a request fan out to about 30 orgs.

## Local quickstart

Prerequisites (macOS): Homebrew, OrbStack, uv, Node 26 with pnpm, the Temporal CLI,
Terraform. The Makefile puts Homebrew and OrbStack on `PATH` itself.

```sh
make doctor     # tool versions, .env.local, ledger and Compose status
make env-local  # append local defaults (and fresh dev secrets) to .env.local
make up         # Postgres 17 and Temporal (with WorldId/OrgId) in Compose, healthy
make migrate    # alembic upgrade head
make seed       # load the harmony-demo scenario into a new world
make dev        # api :8000, worker and Vite, with reload
```

`.env.local` holds the real keys and is gitignored; start from `.env.local.example`.
`MODEL_MODE=scripted` is the default for local runs, tests and CI, and makes no model
calls; production is live only. `ACCESS_CODE` is optional locally: setting it gates
`make dev` behind the passcode page, and `harmony run` unlocks itself. Temporal's UI
runs at <http://localhost:8233>, and MinIO is opt-in:
`docker compose --profile storage up -d --build minio` (`harmony storage check` puts,
gets and deletes a probe object through whichever store `OBJECT_STORE` selects).

A Temporal dev server started by hand, outside Compose, needs the same search
attributes: `temporal server start-dev --search-attribute WorldId=Keyword
--search-attribute OrgId=Keyword`. Without them, starting a world or a chat
(`POST /api/conversations`) returns 500. After building a change to a workflow, reset
local worlds: the local build SHA is git HEAD, so a world kept across an uncommitted
change would replay old histories on the new commands.

## Checks

```sh
make lint typecheck test   # uv.lock, ruff, pyright (strict on src/harmony), pytest
make web-ci                # frontend install, typecheck, lint, test, build
make gen-api               # regenerate web/src/api/schema.d.ts (gen-api-check: drift)
make compose-smoke         # build the image, run it on compose.ci.yaml, smoke-test it
make e2e-ui                # the suggested story in a browser (Playwright) on that image
make tf-check              # terraform fmt -check and validate (never applies)
make ci-local              # everything CI runs
make p0-exit               # the P0 exit check: PASS/FAIL per check
```

Tests need `make up` (Postgres). Each test gets its own database cloned from a
migrated template; set `HARMONY_TEST_DB_PREFIX` when several runs share a server.
Tests marked `minio` or `compose` are opt-in: `make up-storage test-minio`.
Stop any local dev stack on the shared Compose Postgres before the backend suite: the
two together can exhaust its `max_connections` (100), which shows up as
`TooManyConnectionsError` in `test_budget_concurrency` or `test_hosted_caps`.

`make compose-smoke` always tears its stack down; `SMOKE_KEEP=1` leaves it running for
debugging, and `SMOKE_ALLOW_MISSING=1` reports a check whose script doesn't exist yet as
SKIP. `make p0-exit` runs the API (port 8100, `P0_EXIT_API_PORT`) and worker in the
background with `MODEL_MODE=scripted`, runs the act-as, SSE-resume, hello, scripted-turn
and spend checks, and stops both afterwards.

The compose smoke checks include the Playwright story in `web/e2e`, which `make e2e-ui`
runs on its own. Install the browser once with
`pnpm -C web exec playwright install --only-shell chromium`. To drive an app you already
run with `MODEL_MODE=scripted` and `ACCESS_CODE` set instead of the image, set
`E2E_BASE_URL` and `E2E_PASSCODE` (that app's `ACCESS_CODE`). The compose stack runs
with the gate on (`compose.ci.yaml`'s throwaway passcode), and the shell and Python
checks unlock with `HARMONY_PASSCODE`.

## Infrastructure

`infra/terraform` holds the Render and Temporal Cloud modules (14 resources), with local
state. `scripts/tf.sh <terraform args>` loads `.env.local` into the environment without
echoing it, runs Terraform under `umask 077` and keeps every state file at mode 600;
`apply` is refused unless `HARMONY_TF_ALLOW_APPLY=1`, which is set only once the user has
approved a deploy. `infra/terraform/terraform.tfvars` (gitignored; start from
`terraform.tfvars.example`) wins over `.env.local`: it holds `anthropic_api_key`, the
hosted workspace's key, and optionally the master passcode, the caps and the provider
credentials, which `scripts/tfvars_from_env.sh` copies over from `.env.local`.

## Deploy

The runbook is [docs/LIVE_DEMO_PLAN.md](docs/LIVE_DEMO_PLAN.md) section 6. Every step that
spends money, pushes, merges, applies or redeploys waits for the user's explicit approval.

- **Step 1, preconditions:** the live phase merged on `p0-foundations`, integration
  green, and the plan's section 5 (local live verification on Haiku 5.5) done.
- **Step 1a, the production-mix check, local, once** (about $3 of Opus on the dev key,
  ceiling $10; needs approval). With the dev stack and `MODEL_OVERRIDE=none` on the
  command line: `harmony llm ping --live --model opus`, one
  `harmony run harmony-demo --story suggested --live`, and in the browser one typed chat
  as Alex (Opus, "Opus 5.5 · Adaptive") and one as Bayside (Haiku).
- **Steps 2–5:** commit, push, CI green, merge to `main`, each as approved.
- **Step 6, bootstrap by hand:** connect GitHub to Render, set the production Console
  workspace's spend limit, optionally set `access_code` and the caps in
  `terraform.tfvars`, then run `scripts/tfvars_from_env.sh`.
- **Step 7:** `scripts/ping_prod_key.sh`, two calls on the production key (about $0.01).
- **Steps 8–9b:** `scripts/tf.sh plan` (14 resources to add), then
  `HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply`; `make doctor` confirms the state is
  mode 600, and both first deploys are checked.
- **Step 10, post-deploy checks without spend.** Secrets go from Terraform's outputs
  straight into the checks' environment, never to the terminal:

  ```sh
  HARMONY_BASE_URL="$(scripts/tf.sh output -raw web_url)" \
  HARMONY_PASSCODE="$(scripts/tf.sh output -raw access_code)" \
  HARMONY_ADMIN_SECRET="$(scripts/tf.sh output -raw admin_secret)" \
    uv run python scripts/hosted_check.py --build-sha "$(git rev-parse origin/main)" \
      --save-jar "$TMPDIR/harmony.cookies.json"
  # also --client-key, --reset-retires and (while paused) --refused-turn
  scripts/hosted_admin.sh status            # or pause, resume, wipe --yes
  ```

  `--save-jar` keeps the default run's cookies (mode 600) for the rotation drill's
  `--previous-jar`. It refuses a path inside the repository: the jar holds a working gate
  cookie until the rotation. `--refused-turn` needs hosted caps (the hosted app, or a
  local live stack with `HOSTED_CAPS_IN_DEV=1`); on compose-smoke it stops at once.
  `--build-sha` is required: healthz must report main's HEAD, not an older build Render
  kept live. Then `harmony storage check` in Render Shell on the web service; if the API
  is ever down, `harmony live pause` there is the kill switch.
  The six-wrong-attempts check runs last: it blocks the laptop's unlocks for 15 minutes,
  and every `hosted_check.py` mode unlocks afresh (a 429 is reported as the per-client
  limit, with its `Retry-After`).
- **Steps 11–14:** first live use, SSE across a deploy (`hosted_check.py --across-deploy`),
  the rotation drill, and the user shares the URL and the passcode. The rotation drill
  captures the old passcode before the apply, which replaces what the output returns:

  ```sh
  export HARMONY_PREVIOUS_PASSCODE="$(scripts/tf.sh output -raw access_code)"
  ```

  Then change it one of two ways:

  ```sh
  # A passcode set in terraform.tfvars: edit access_code there, then
  HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply
  ```

  ```sh
  # The generated passcode (terraform.tfvars doesn't set access_code):
  HARMONY_TF_ALLOW_APPLY=1 scripts/tf.sh apply -replace=random_password.access_code
  ```

  While terraform.tfvars sets `access_code`, `-replace` changes nothing, and a `-replace`
  given only to `plan` rotates nothing either. Redeploy both services, then:

  ```sh
  HARMONY_BASE_URL="$(scripts/tf.sh output -raw web_url)" \
  HARMONY_PASSCODE="$(scripts/tf.sh output -raw access_code)" \
    uv run python scripts/hosted_check.py --rotation --previous-jar "$TMPDIR/harmony.cookies.json"
  rm "$TMPDIR/harmony.cookies.json"
  ```

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the design, and the source of truth.
- [docs/P0_PLAN.md](docs/P0_PLAN.md): the P0 (foundations) build spec and task split.
- [docs/LIVE_DEMO_PLAN.md](docs/LIVE_DEMO_PLAN.md): the live demo phase and the deploy runbook.
- [docs/FEEDBACK_R2_PLAN.md](docs/FEEDBACK_R2_PLAN.md): feedback round 2 (Edit, Reopen, the cards, switching, desktop sizing), with the designs in [docs/design/](docs/design/).
