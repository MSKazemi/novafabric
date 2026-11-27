# EXPERIMENTAL example: one Compute Engine VM wired to the provider-neutral
# module. CI only runs `tofu init -backend=false` and `tofu validate` on it; it
# has never been applied by this project. Applying it creates billed resources.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.12"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
