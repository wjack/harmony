# Harmony's production: Render (web, worker, Postgres, MinIO) plus a Temporal Cloud
# namespace (ARCHITECTURE.md §13), 14 resources. Run through scripts/tf.sh.

locals {
  s3_bucket  = "harmony"
  s3_region  = "us-east-1"
  minio_user = "harmony"

  # The user's passcode from terraform.tfvars, else the generated one.
  access_code = coalesce(var.access_code, random_password.access_code.result)

  # Both services' env; the render module adds DATABASE_URL. The pre-deploy migration
  # loads the same settings, so prod's checks (live models, the passcode, every cap)
  # apply to it too.
  app_env = {
    APP_ENV = "prod"
    # Prod refuses anything else: the hosted app has no scripted mode.
    MODEL_MODE              = "live"
    SESSION_SECRET          = random_password.session_secret.result
    ADMIN_SECRET            = random_password.admin_secret.result
    ACCESS_CODE             = local.access_code
    CLIENT_IP_HEADER        = var.client_ip_header
    LINEAGE_BUDGET_USD      = tostring(var.lineage_budget_usd)
    CLIENT_DAILY_BUDGET_USD = tostring(var.client_daily_budget_usd)
    DAILY_BUDGET_USD        = tostring(var.daily_budget_usd)
    GLOBAL_BUDGET_USD       = tostring(var.global_budget_usd)
    TEMPORAL_ADDRESS        = module.temporal.grpc_address
    TEMPORAL_NAMESPACE      = module.temporal.namespace_id
    TEMPORAL_API_KEY        = module.temporal.api_key
    OBJECT_STORE            = "s3"
    S3_ENDPOINT_URL         = module.storage.endpoint_url
    S3_BUCKET               = local.s3_bucket
    S3_REGION               = local.s3_region
    S3_ACCESS_KEY_ID        = local.minio_user
    S3_SECRET_ACCESS_KEY    = random_password.minio_root_password.result
  }

  # The worker alone calls the model, so only it holds the key.
  worker_env = {
    ANTHROPIC_API_KEY        = var.anthropic_api_key
    WORKER_MODEL_CONCURRENCY = tostring(var.worker_model_concurrency)
    WORKER_MODEL_RATE        = tostring(var.worker_model_rate)
    WORKER_CHAT_CONCURRENCY  = tostring(var.worker_chat_concurrency)
  }
}

# App secrets are generated here, so they live only in the (local, gitignored, mode 600)
# state and the services' env. Read one back with `scripts/tf.sh output -raw <name>`.
resource "random_password" "session_secret" {
  length  = 48
  special = false
}

resource "random_password" "admin_secret" {
  length  = 32
  special = false
}

# The master passcode when terraform.tfvars sets no `access_code`: 16 alphanumerics,
# about 95 bits. Unconditional, so setting or clearing `access_code` never replaces it.
resource "random_password" "access_code" {
  length  = 16
  special = false
}

resource "random_password" "minio_root_password" {
  length  = 40
  special = false
}

resource "render_project" "harmony" {
  name = "harmony"
  environments = {
    production = {
      name             = "production"
      protected_status = "unprotected"
    }
  }
}

module "temporal" {
  source = "./modules/temporal"

  region = var.temporal_region
}

module "storage" {
  source = "./modules/storage"

  region         = var.render_region
  environment_id = render_project.harmony.environments["production"].id
  repo_url       = var.repo_url
  branch         = var.branch
  plan           = var.minio_plan
  disk_size_gb   = var.minio_disk_size_gb
  root_user      = local.minio_user
  root_password  = random_password.minio_root_password.result
  bucket         = local.s3_bucket
}

module "render" {
  source = "./modules/render"

  region                = var.render_region
  environment_id        = render_project.harmony.environments["production"].id
  repo_url              = var.repo_url
  branch                = var.branch
  web_plan              = var.web_plan
  worker_plan           = var.worker_plan
  postgres_disk_size_gb = var.postgres_disk_size_gb
  app_env               = local.app_env
  worker_env            = local.worker_env
}
