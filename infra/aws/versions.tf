terraform {
  # Pinned per CLAUDE.md rule 2 (no `latest`, providers pinned).
  required_version = "= 1.15.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.84.0"
    }
  }

  # Remote state lives in the backend the state-backend stack bootstrapped.
  # (Values are the `backend_config` output of infra/state-backend.) Partial
  # config is intentional: no secrets here, and the bucket/table are fixed by
  # the day2-terraform IAM policy.
  backend "s3" {
    bucket         = "day2-control-plane-tfstate-apse2"
    key            = "aws/terraform.tfstate"
    region         = "ap-southeast-2"
    dynamodb_table = "day2-control-plane-tflock"
    encrypt        = true
  }
}
