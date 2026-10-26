variable "name" {
  description = "Namespace name; Temporal Cloud appends the account ID to form its ID."
  type        = string
  default     = "harmony"
}

variable "region" {
  description = "Temporal Cloud region, prefixed with the cloud provider (aws-us-west-2)."
  type        = string
}

variable "retention_days" {
  description = "Days of closed-workflow history to keep."
  type        = number
  default     = 7
}
