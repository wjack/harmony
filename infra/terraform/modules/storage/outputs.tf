output "endpoint_url" {
  description = "S3 endpoint on Render's private network: the app's S3_ENDPOINT_URL."
  # A private service's internal hostname is its slug (name plus a generated suffix).
  value = "http://${render_private_service.minio.slug}:${var.port}"
}
