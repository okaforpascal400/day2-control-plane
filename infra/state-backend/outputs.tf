output "state_bucket" {
  description = "Name of the S3 state bucket."
  value       = aws_s3_bucket.tfstate.id
}

output "lock_table" {
  description = "Name of the DynamoDB lock table."
  value       = aws_dynamodb_table.tflock.name
}

output "region" {
  description = "Region the backend lives in."
  value       = var.region
}

# Copy-paste backend block for every OTHER stack in this repo.
output "backend_config" {
  description = "Paste this into the consuming stack's terraform { backend \"s3\" {...} }."
  value       = <<-EOT
    backend "s3" {
      bucket         = "${aws_s3_bucket.tfstate.id}"
      key            = "<stack-name>/terraform.tfstate"
      region         = "${var.region}"
      dynamodb_table = "${aws_dynamodb_table.tflock.name}"
      encrypt        = true
    }
  EOT
}
