# The Temporal Cloud namespace with API-key auth, the search attributes the app sets on
# every world workflow, and the service account + API key that web and worker use.

terraform {
  required_providers {
    temporalcloud = {
      source = "temporalio/temporalcloud"
    }
  }
}

resource "temporalcloud_namespace" "this" {
  name           = var.name
  regions        = [var.region]
  api_key_auth   = true
  retention_days = var.retention_days

  namespace_lifecycle = {
    # Must be turned off in a separate apply before the namespace can be destroyed.
    enable_delete_protection = true
  }

  # The provider's default is 10 minutes. A create that outlives it leaves the
  # namespace in Temporal Cloud but not in state, so the next apply fails on "already
  # exists" until the namespace is imported.
  timeouts {
    create = "30m"
  }
}

# Typed keys in harmony.temporal.search_attributes; RetireWorldWorkflow lists by WorldId.
resource "temporalcloud_namespace_search_attribute" "this" {
  for_each = toset(["WorldId", "OrgId"])

  namespace_id = temporalcloud_namespace.this.id
  name         = each.key
  type         = "keyword"
}

resource "temporalcloud_service_account" "app" {
  name        = "${var.name}-app"
  description = "Harmony web and worker on Render."

  namespace_scoped_access = {
    namespace_id = temporalcloud_namespace.this.id
    permission   = "write"
  }
}

resource "temporalcloud_apikey" "app" {
  display_name = "${var.name}-app"
  description  = "Harmony web and worker on Render."
  owner_type   = "service-account"
  owner_id     = temporalcloud_service_account.app.id
  # A literal date after the interview window (ARCHITECTURE.md §13). To rotate, change
  # it and apply: Terraform replaces the key and both services' env gets the new token.
  expiry_time = "2027-01-31T00:00:00Z"
}
