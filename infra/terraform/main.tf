# Harmony's production: Render (web, worker, Postgres, MinIO) plus a Temporal Cloud
# namespace (ARCHITECTURE.md §13). Run through scripts/tf.sh; P0 only validates.

locals {
  s3_bucket  = "harmony"
  s3_region  = "us-east-1"
  minio_user = "harmony"
}

# App secrets are generated here, so they live only in the (local, gitignored) state
# and the Render env group. Read one back with `scripts/tf.sh output -raw <name>`.
resource "random_password" "session_secret" {
  length  = 48
  special = false
}

resource "random_password" "admin_secret" {
  length  = 32
  special = false
}

# The code in the demo's share link that unlocks live runs.
resource "random_password" "access_code" {
  length  = 12
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

  # Shared by web and worker through the env group; the module adds DATABASE_URL.
  app_env = {
    APP_ENV              = "prod"
    MODEL_MODE           = var.model_mode
    ANTHROPIC_API_KEY    = var.anthropic_api_key
    SESSION_SECRET       = random_password.session_secret.result
    ADMIN_SECRET         = random_password.admin_secret.result
    ACCESS_CODE          = random_password.access_code.result
    TEMPORAL_ADDRESS     = module.temporal.grpc_address
    TEMPORAL_NAMESPACE   = module.temporal.namespace_id
    TEMPORAL_API_KEY     = module.temporal.api_key
    OBJECT_STORE         = "s3"
    S3_ENDPOINT_URL      = module.storage.endpoint_url
    S3_BUCKET            = local.s3_bucket
    S3_REGION            = local.s3_region
    S3_ACCESS_KEY_ID     = local.minio_user
    S3_SECRET_ACCESS_KEY = random_password.minio_root_password.result
  }
}
