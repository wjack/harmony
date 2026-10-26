output "namespace_id" {
  description = "Namespace ID (<name>.<account>): the app's TEMPORAL_NAMESPACE."
  value       = temporalcloud_namespace.this.id
}

output "grpc_address" {
  description = "gRPC endpoint for API-key clients: the app's TEMPORAL_ADDRESS."
  value       = temporalcloud_namespace.this.endpoints.grpc_address
}

output "api_key" {
  description = "The app's TEMPORAL_API_KEY."
  value       = temporalcloud_apikey.app.token
  sensitive   = true
}
