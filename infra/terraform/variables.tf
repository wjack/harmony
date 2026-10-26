variable "repo_url" {
  description = "GitHub repository Render builds from. GitHub must be connected to Render in the dashboard first."
  type        = string
  default     = "https://github.com/wjack/harmony"
}

variable "branch" {
  description = "Branch Render deploys, once its CI checks pass."
  type        = string
  default     = "main"
}

variable "render_region" {
  description = "Render region for every service and the database. Oregon sits next to Temporal Cloud's aws-us-west-2."
  type        = string
  default     = "oregon"

  validation {
    condition     = contains(["frankfurt", "ohio", "oregon", "singapore", "virginia"], var.render_region)
    error_message = "render_region must be one of frankfurt, ohio, oregon, singapore or virginia."
  }
}

variable "temporal_region" {
  description = "Temporal Cloud region for the namespace."
  type        = string
  default     = "aws-us-west-2"
}

variable "temporal_account_id" {
  description = "Temporal Cloud account ID. When set, the provider refuses to act on any other account."
  type        = string
  default     = null
}

variable "web_plan" {
  description = "Render instance type for the web service (API, SSE and SPA)."
  type        = string
  default     = "starter"
}

variable "worker_plan" {
  description = "Render instance type for the Temporal worker. Move to standard if memory requires."
  type        = string
  default     = "starter"
}

variable "minio_plan" {
  description = "Render instance type for the MinIO private service."
  type        = string
  default     = "starter"
}

variable "postgres_disk_size_gb" {
  description = "Postgres storage in GB: 1 or a multiple of 5. It can grow later but never shrink."
  type        = number
  default     = 5

  validation {
    condition     = var.postgres_disk_size_gb == 1 || var.postgres_disk_size_gb % 5 == 0
    error_message = "postgres_disk_size_gb must be 1 or a multiple of 5."
  }
}

variable "minio_disk_size_gb" {
  description = "Size of MinIO's persistent disk in GB."
  type        = number
  default     = 10
}

variable "model_mode" {
  description = "MODEL_MODE for the hosted app. Stays scripted until the hosted demo's spend caps are decided."
  type        = string
  default     = "scripted"

  validation {
    condition     = contains(["scripted", "live"], var.model_mode)
    error_message = "model_mode must be scripted or live."
  }
}

variable "anthropic_api_key" {
  description = "The production Anthropic key, from its own Console workspace with a spend limit. Never the development key."
  type        = string
  default     = null
  sensitive   = true

  validation {
    condition     = var.model_mode != "live" || var.anthropic_api_key != null
    error_message = "model_mode = \"live\" needs anthropic_api_key (the production workspace key)."
  }

  validation {
    condition     = var.anthropic_api_key == null || can(regex("^sk-ant-", var.anthropic_api_key))
    error_message = "anthropic_api_key must be an Anthropic key (sk-ant-...) or left unset."
  }
}
