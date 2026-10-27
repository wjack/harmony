export PATH := /opt/homebrew/bin:$(HOME)/.orbstack/bin:$(PATH)

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# macOS ships GNU Make 3.81, which looks up a recipe's first word on its own startup
# PATH, ignoring the export above. Resolve the tools to absolute paths instead.
which = $(or $(shell PATH="$(PATH)" command -v $(1) 2>/dev/null),$(1))
UV := $(call which,uv)
PNPM := $(call which,pnpm)
DOCKER := $(call which,docker)
TERRAFORM := $(call which,terraform)
COMPOSE := $(DOCKER) compose
SCENARIO ?= harmony-demo

.PHONY: help doctor env-local up up-storage down migrate seed dev \
	test test-minio lint fmt typecheck gen-api gen-api-check web-ci tf-check compose-smoke e2e-ui \
	ci-local p0-exit

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

# --- setup and run -------------------------------------------------------------------

doctor: ## Print tool versions and check local prerequisites
	@bash scripts/doctor.sh

env-local: ## Append missing local defaults to .env.local (prints key names only)
	@$(UV) run --quiet python scripts/ensure_env_local.py

up: ## Start Postgres and Temporal, waiting until both are healthy
	$(COMPOSE) up -d --wait postgres temporal

up-storage: ## Build and start MinIO (Compose profile "storage") and create its bucket
	$(COMPOSE) --profile storage up -d --build --wait minio

down: ## Stop the Compose services (named volumes are kept)
	$(COMPOSE) --profile storage down

migrate: ## Apply database migrations (alembic upgrade head)
	$(UV) run alembic upgrade head

seed: ## Load the demo scenario into a new world
	$(UV) run harmony seed --scenario $(SCENARIO)

dev: ## Run api, worker and web with reload (honcho + Procfile.dev)
	$(UV) run honcho -f Procfile.dev start

# --- checks --------------------------------------------------------------------------

test: ## Python tests (needs `make up`; minio/compose-marked tests are opt-in)
	$(UV) run pytest

test-minio: ## The S3 storage tests against local MinIO (needs `make up-storage`)
	$(UV) run pytest -m minio tests/integration/test_storage_minio.py

lint: ## uv.lock in sync, ruff check + format check
	$(UV) lock --check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Apply ruff fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## pyright (strict on src/harmony)
	$(UV) run pyright

gen-api: ## Regenerate the web client's types from FastAPI's OpenAPI document
	bash scripts/gen_api.sh

gen-api-check: ## Fail if web/src/api/schema.d.ts is out of date with the API
	bash scripts/gen_api.sh --check

web-ci: ## Frontend: frozen install, typecheck, lint, test, build
	$(PNPM) -C web install --frozen-lockfile
	$(PNPM) -C web run typecheck
	$(PNPM) -C web run lint
	$(PNPM) -C web run test
	$(PNPM) -C web run build

tf-check: ## terraform fmt -check, validate and test (mock providers; no backend, never apply)
	$(TERRAFORM) -chdir=infra/terraform fmt -check -recursive
	$(TERRAFORM) -chdir=infra/terraform init -backend=false -input=false
	$(TERRAFORM) -chdir=infra/terraform validate
	$(TERRAFORM) -chdir=infra/terraform test

compose-smoke: ## Build the image and run the smoke checks on compose.ci.yaml
	bash scripts/compose_smoke.sh

e2e-ui: ## The suggested story in a browser (Playwright, ScriptedModel) on compose.ci.yaml
	bash scripts/e2e_ui.sh

ci-local: lint typecheck test gen-api-check web-ci tf-check compose-smoke ## Everything CI runs, locally

p0-exit: ## The P0 exit check (PASS/FAIL per check)
	bash scripts/p0_exit.sh
