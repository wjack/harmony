# Credentials come from the environment, never from variables or files in the repo:
#   render         RENDER_API_KEY, RENDER_OWNER_ID
#   temporalcloud  TEMPORAL_CLOUD_API_KEY (an account-level key)
# scripts/tf.sh exports them from .env.local without printing them.

provider "render" {}

provider "temporalcloud" {
  # When set, the provider refuses to touch any other Temporal Cloud account.
  allowed_account_id = var.temporal_account_id
}
