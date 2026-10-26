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
  description = "The share link's access code for live runs."
  value       = random_password.access_code.result
  sensitive   = true
}

output "admin_secret" {
  description = "ADMIN_SECRET for POST /api/admin/wipe."
  value       = random_password.admin_secret.result
  sensitive   = true
}
