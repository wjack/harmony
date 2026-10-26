# State is local (terraform.tfstate, gitignored): `apply` runs only from the dev machine,
# through scripts/tf.sh. CI runs `fmt -check` and `validate` with no backend and no
# credentials (ARCHITECTURE.md §13). The lock file pins exact provider builds.
terraform {
  required_version = "~> 1.17"

  required_providers {
    render = {
      source  = "render-oss/render"
      version = "~> 1.9"
    }
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.9"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}
