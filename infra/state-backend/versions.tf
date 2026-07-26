terraform {
  # Pinned per CLAUDE.md rule 2 (no `latest`, providers pinned).
  required_version = "= 1.15.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.84.0"
    }
  }

  # This stack BOOTSTRAPS the remote backend (the S3 bucket + DynamoDB lock
  # table below). It therefore cannot store its own state in a bucket that does
  # not exist yet — the chicken-and-egg. It intentionally uses local state.
  #
  # `terraform.tfstate` for this stack is git-ignored, not committed (it is
  # tiny and reproducible: `terraform import` the two resources if ever lost).
  # Every OTHER stack (VPC/EC2/etc.) uses the S3 backend this stack creates.
}
