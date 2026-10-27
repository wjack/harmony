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

# --- Provider credentials -------------------------------------------------------------
# Unset, the providers read RENDER_API_KEY, RENDER_OWNER_ID and TEMPORAL_CLOUD_API_KEY
# from the environment (scripts/tf.sh exports them from .env.local). Set in
# terraform.tfvars (scripts/tfvars_from_env.sh copies them there), they win.

variable "render_api_key" {
  description = "Render API key (Account Settings → API Keys)."
  type        = string
  default     = null
  sensitive   = true
}

variable "render_owner_id" {
  description = "Render workspace ID that owns the resources (tea-… or usr-…)."
  type        = string
  default     = null
  sensitive   = true
}

variable "temporal_cloud_api_key" {
  description = "Temporal Cloud account-level API key, for Terraform only; the app's key is created below."
  type        = string
  default     = null
  sensitive   = true
}

# --- Instances --------------------------------------------------------------------------

variable "web_plan" {
  description = "Render instance type for the web service (API, SSE and SPA)."
  type        = string
  default     = "starter"
}

variable "worker_plan" {
  description = "Render instance type for the Temporal worker. Standard (2 GB): Starter's 512 MB is tight for live model calls on 12 + 4 slots."
  type        = string
  default     = "standard"
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

# --- The app ------------------------------------------------------------------------------

variable "anthropic_api_key" {
  description = "The production Anthropic key, from its own Console workspace with a spend limit. Never the development key. Only the worker gets it: the web service never calls a model."
  type        = string
  nullable    = false
  sensitive   = true

  validation {
    condition     = can(regex("^sk-ant-", var.anthropic_api_key))
    error_message = "anthropic_api_key must be an Anthropic key (sk-ant-...)."
  }
}

variable "model_override" {
  description = "MODEL_OVERRIDE for both services: the one model every role runs on, or \"none\" for the production mix (Opus 5.5 for featured orgs and the synthesizer, Haiku 5.5 for the rest). The hosted demo runs Haiku 5.5 everywhere for now (user, 2026-10-26)."
  type        = string
  default     = "claude-haiku-5-5"

  validation {
    # The app's own prod check (config.PROD_MODEL_OVERRIDES), plus "none".
    condition     = contains(["none", "claude-haiku-5-5", "claude-opus-5-5"], var.model_override)
    error_message = "model_override must be none, claude-haiku-5-5 or claude-opus-5-5."
  }
}

variable "access_code" {
  description = "The master passcode for the whole app (ACCESS_CODE). Unset, the generated random_password.access_code is used; read it with `scripts/tf.sh output -raw access_code`. A passphrase of four or more words works."
  type        = string
  default     = null
  sensitive   = true

  validation {
    # The app's own prod check (config._check_access_code): 16 or more printable ASCII
    # characters, the first and last not a space. Both sides of || are evaluated, so a
    # null reaches regex() too; can() turns that error into false.
    condition     = var.access_code == null || can(regex("^[!-~][ -~]{14,}[!-~]$", var.access_code))
    error_message = "access_code must be at least 16 printable ASCII characters with no whitespace at either end, or left unset."
  }
}

variable "client_ip_header" {
  description = "Where the gate reads a visitor's address (CLIENT_IP_HEADER): x-forwarded-for (its rightmost hop, which Render's proxy appends), true-client-ip or cf-connecting-ip. Check it after deploy with `scripts/hosted_check.py --client-key`."
  type        = string
  default     = "x-forwarded-for"

  validation {
    condition     = contains(["x-forwarded-for", "true-client-ip", "cf-connecting-ip"], var.client_ip_header)
    error_message = "client_ip_header must be x-forwarded-for, true-client-ip or cf-connecting-ip."
  }
}

# Hosted spend caps in US dollars (ARCHITECTURE.md §14). The app refuses to boot in prod
# without the daily, global and client caps, and raises its `global` and today's `daily:`
# rows to these values at every boot. Lineage and client rows keep the cap they were
# created with.

variable "lineage_budget_usd" {
  description = "Live-model cap per browser session (its lineage); Reset never refills it."
  type        = number
  default     = 10

  validation {
    condition     = var.lineage_budget_usd > 0
    error_message = "lineage_budget_usd must be positive."
  }
}

variable "client_daily_budget_usd" {
  description = "Live-model cap per known client network per UTC day."
  type        = number
  default     = 25

  validation {
    condition     = var.client_daily_budget_usd > 0
    error_message = "client_daily_budget_usd must be positive."
  }
}

variable "daily_budget_usd" {
  description = "Live-model cap per UTC day, across every visitor."
  type        = number
  default     = 60

  validation {
    condition     = var.daily_budget_usd > 0
    error_message = "daily_budget_usd must be positive."
  }
}

variable "global_budget_usd" {
  description = "Live-model cap in total. Keep it below the Console workspace's spend limit, so the app's own refusal, which the UI explains, comes first."
  type        = number
  default     = 250

  validation {
    condition     = var.global_budget_usd > 0
    error_message = "global_budget_usd must be positive."
  }
}

# Worker capacity (`WorkerConfig.from_settings`). Model activities are I/O-bound and hold
# no database session while they wait on the model.

variable "worker_model_concurrency" {
  description = "Model-activity slots on harmony-model (representatives and answerers)."
  type        = number
  default     = 12

  validation {
    condition     = var.worker_model_concurrency >= 1 && floor(var.worker_model_concurrency) == var.worker_model_concurrency
    error_message = "worker_model_concurrency must be a whole number of at least 1: the worker reads it as an integer."
  }
}

variable "worker_model_rate" {
  description = "Model-activity starts per second, queue-wide."
  type        = number
  default     = 5

  validation {
    condition     = var.worker_model_rate > 0
    error_message = "worker_model_rate must be positive."
  }
}

variable "worker_chat_concurrency" {
  description = "Slots on harmony-chat, kept for internal Claude's turns so a fan-out never queues a typed chat."
  type        = number
  default     = 4

  validation {
    condition     = var.worker_chat_concurrency >= 1 && floor(var.worker_chat_concurrency) == var.worker_chat_concurrency
    error_message = "worker_chat_concurrency must be a whole number of at least 1: the worker reads it as an integer."
  }
}
