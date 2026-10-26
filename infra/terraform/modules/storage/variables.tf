variable "name" {
  description = "Render service name; its slug is the private-network hostname."
  type        = string
  default     = "harmony-minio"
}

variable "region" {
  description = "Render region (must match the app's, for the private network)."
  type        = string
}

variable "environment_id" {
  description = "Render project environment the service belongs to."
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

variable "plan" {
  description = "Render instance type."
  type        = string
}

variable "disk_size_gb" {
  description = "Persistent disk size in GB, mounted at /data."
  type        = number
}

variable "root_user" {
  description = "MINIO_ROOT_USER; the app signs S3 requests with it."
  type        = string
}

variable "root_password" {
  description = "MINIO_ROOT_PASSWORD."
  type        = string
  sensitive   = true
}

variable "bucket" {
  description = "Bucket the image's init.sh creates on start (the app's S3_BUCKET)."
  type        = string
}

variable "port" {
  description = "MinIO's S3 API port (the image's --address)."
  type        = number
  default     = 9000
}
