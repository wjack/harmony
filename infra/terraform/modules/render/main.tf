# The app on Render: Postgres 17, the web service (API, SSE, SPA) and the Temporal
# worker, both built from the repo's Dockerfile (ARCHITECTURE.md §3, §13). Both deploy
# only once CI is green.

terraform {
  required_providers {
    render = {
      source = "render-oss/render"
    }
  }
}

locals {
  # Changes here can't affect the app image.
  ignored_paths = ["docs/**", "infra/**"]

  database_url = render_postgres.db.connection_info.internal_connection_string
  # Render has no transaction-mode pooler in front of Postgres, so LISTEN and
  # migrations use the same URL.
  web_env = merge(var.app_env, {
    DATABASE_URL        = local.database_url
    DATABASE_DIRECT_URL = local.database_url
  })
  worker_env = merge(local.web_env, var.worker_env)
}

# Basic-1gb, never the free plan (which expires). 100 connections; the budget is in
# src/harmony/config.py.
resource "render_postgres" "db" {
  name           = "${var.name}-db"
  plan           = "basic_1gb"
  version        = "17"
  region         = var.region
  environment_id = var.environment_id
  database_name  = "harmony"
  database_user  = "harmony"
  disk_size_gb   = var.postgres_disk_size_gb
  # Empty: only the private network can connect.
  ip_allow_list = []
}

resource "render_web_service" "web" {
  name              = "${var.name}-web"
  plan              = var.web_plan
  region            = var.region
  environment_id    = var.environment_id
  num_instances     = 1
  health_check_path = "/api/healthz"
  # Migrations run before the new version starts; they are additive. The worker runs
  # the same command, since Render deploys the two services independently.
  pre_deploy_command = "alembic upgrade head"
  # On the service itself: Render starts a service's first deploy, pre-deploy migration
  # included, as soon as it creates the service, before a linked env group could attach.
  env_vars = { for key, value in local.web_env : key => { value = value } }

  runtime_source = {
    docker = {
      repo_url            = var.repo_url
      branch              = var.branch
      dockerfile_path     = "./Dockerfile"
      context             = "."
      auto_deploy_trigger = "checksPass"
      build_filter = {
        ignored_paths = local.ignored_paths
      }
    }
  }
}

resource "render_background_worker" "worker" {
  name           = "${var.name}-worker"
  plan           = var.worker_plan
  region         = var.region
  environment_id = var.environment_id
  num_instances  = 1
  start_command  = "harmony worker"
  # The new code must never poll for activities ahead of its schema, and nothing
  # orders this deploy after web's: it migrates itself (a no-op once web has).
  # migrations/env.py serializes the two runs with an advisory lock.
  pre_deploy_command = "alembic upgrade head"
  # The worker drains in-flight activities for about 90 s after SIGTERM.
  max_shutdown_delay_seconds = 120
  env_vars                   = { for key, value in local.worker_env : key => { value = value } }

  runtime_source = {
    docker = {
      repo_url            = var.repo_url
      branch              = var.branch
      dockerfile_path     = "./Dockerfile"
      context             = "."
      auto_deploy_trigger = "checksPass"
      build_filter = {
        ignored_paths = local.ignored_paths
      }
    }
  }
}
