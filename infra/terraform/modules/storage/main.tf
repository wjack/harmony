# MinIO, built from source (infra/minio), as a Render private service with a persistent
# disk (ARCHITECTURE.md §13). Only the private network reaches it; browsers never do.
# A service with a disk runs as one instance with no zero-downtime deploys, so it
# rebuilds only when infra/minio changes.

terraform {
  required_providers {
    render = {
      source = "render-oss/render"
    }
  }
}

resource "render_private_service" "minio" {
  name           = var.name
  plan           = var.plan
  region         = var.region
  environment_id = var.environment_id
  num_instances  = 1

  runtime_source = {
    docker = {
      repo_url            = var.repo_url
      branch              = var.branch
      dockerfile_path     = "./infra/minio/Dockerfile"
      context             = "./infra/minio"
      auto_deploy_trigger = "checksPass"
      build_filter = {
        paths = ["infra/minio/**"]
      }
    }
  }

  disk = {
    name       = "${var.name}-data"
    mount_path = "/data"
    size_gb    = var.disk_size_gb
  }

  env_vars = {
    MINIO_ROOT_USER     = { value = var.root_user }
    MINIO_ROOT_PASSWORD = { value = var.root_password }
    S3_BUCKET           = { value = var.bucket }
  }
}
