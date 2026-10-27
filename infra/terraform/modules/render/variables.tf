variable "name" {
  description = "Prefix for the Render resource names."
  type        = string
  default     = "harmony"
}

variable "region" {
  description = "Render region for the database and both services."
  type        = string
}

variable "environment_id" {
  description = "Render project environment the resources belong to."
  type        = string
}

variable "repo_url" {
  description = "GitHub repository to build from."
  type        = string
}

variable "branch" {
  description = "Branch to deploy."
  type        = string
}

variable "web_plan" {
  description = "Instance type for the web service."
  type        = string
}

variable "worker_plan" {
  description = "Instance type for the worker."
  type        = string
}

variable "postgres_disk_size_gb" {
  description = "Postgres storage in GB."
  type        = number
}

variable "app_env" {
  description = "Env both services get (harmony.config keys); the module adds DATABASE_URL."
  type        = map(string)
  sensitive   = true
}

variable "worker_env" {
  description = "Env only the worker gets: the model key and its capacity settings."
  type        = map(string)
  sensitive   = true
}
