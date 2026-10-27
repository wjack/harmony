# Credentials come from terraform.tfvars when set there (scripts/tfvars_from_env.sh copies
# them from .env.local), else from the environment, which scripts/tf.sh fills from
# .env.local without printing them:
#   render         RENDER_API_KEY, RENDER_OWNER_ID
#   temporalcloud  TEMPORAL_CLOUD_API_KEY (an account-level key)
# Never from files in the repo: terraform.tfvars is gitignored.

provider "render" {
  api_key  = var.render_api_key
  owner_id = var.render_owner_id
}

provider "temporalcloud" {
  api_key = var.temporal_cloud_api_key
  # When set, the provider refuses to touch any other Temporal Cloud account.
  allowed_account_id = var.temporal_account_id
}
