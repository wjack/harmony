output "web_url" {
  description = "Public URL of the web service."
  value       = module.render.web_url
}

output "temporal_namespace" {
  description = "Temporal Cloud namespace ID (<name>.<account>)."
  value       = module.temporal.namespace_id
}

output "temporal_grpc_address" {
  description = "Temporal Cloud gRPC endpoint for API-key clients."
  value       = module.temporal.grpc_address
}

output "access_code" {
  description = "The master passcode (ACCESS_CODE): terraform.tfvars' access_code, else the generated one."
  value       = local.access_code
  sensitive   = true
}

output "admin_secret" {
  description = "ADMIN_SECRET, sent as X-Admin-Secret to /api/admin/* (scripts/hosted_admin.sh)."
  value       = random_password.admin_secret.result
  sensitive   = true
}
