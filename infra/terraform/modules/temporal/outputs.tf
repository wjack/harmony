output "namespace_id" {
  description = "Namespace ID (<name>.<account>): the app's TEMPORAL_NAMESPACE."
  value       = temporalcloud_namespace.this.id
  # Every world workflow start sets WorldId and OrgId, so the services that read this
  # output are created only after both attributes exist.
  depends_on = [temporalcloud_namespace_search_attribute.this]
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
