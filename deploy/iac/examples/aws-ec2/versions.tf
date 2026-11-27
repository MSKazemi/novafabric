# EXPERIMENTAL example: one EC2 instance wired to the provider-neutral module.
# CI only runs `tofu init -backend=false` and `tofu validate` on it; it has
# never been applied by this project. Applying it creates billed AWS resources.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.74"
    }
  }
}

provider "aws" {
  region = var.region
}
