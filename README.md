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
`MODEL_MODE=scripted` is the default everywhere and makes no model calls. Temporal's
UI runs at <http://localhost:8233>, and MinIO is opt-in:
`docker compose --profile storage up -d --build minio`.

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

`make compose-smoke` always tears its stack down; `SMOKE_KEEP=1` leaves it running for
debugging, and `SMOKE_ALLOW_MISSING=1` reports a check whose script doesn't exist yet as
SKIP. `make p0-exit` runs the API (port 8100, `P0_EXIT_API_PORT`) and worker in the
background with `MODEL_MODE=scripted`, runs the act-as, SSE-resume, hello, scripted-turn
and spend checks, and stops both afterwards.

The compose smoke checks include the Playwright story in `web/e2e`, which `make e2e-ui`
runs on its own. Install the browser once with
`pnpm -C web exec playwright install --only-shell chromium`. To drive an app you already
run with `MODEL_MODE=scripted` instead of the image, set `E2E_BASE_URL`.

## Infrastructure

`infra/terraform` holds the Render and Temporal Cloud modules, with local state.
`scripts/tf.sh <terraform args>` loads `.env.local` into `TF_VAR_*` without echoing it;
P0 never runs `terraform apply`.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the design, and the source of truth.
- [docs/P0_PLAN.md](docs/P0_PLAN.md): the P0 (foundations) build spec and task split.
