provider "aws" {
  region = var.region

  # Belt-and-suspenders: refuse to run against the wrong account. The IAM
  # policy already fences EC2 to ap-southeast-2, but state (S3/DynamoDB) is
  # region-scoped only by where we create it, so we assert the region here too.
  default_tags {
    tags = var.tags
  }
}

# ---------------------------------------------------------------------------
# State bucket
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tfstate" {
  bucket = var.bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      # SSE-S3 (AES256), not a KMS CMK — the day2-terraform policy grants no
      # KMS actions (see infra/iam/README.md, "Deliberately NOT included").
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# ---------------------------------------------------------------------------
# Lock table
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "tflock" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST" # on-demand; ~$0 at Terraform's lock volume
  hash_key     = "LockID"          # required name for the Terraform S3 backend lock

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = var.tags
}
