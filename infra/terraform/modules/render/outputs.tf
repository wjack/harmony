output "web_url" {
  description = "Public URL of the web service."
  value       = render_web_service.web.url
}
