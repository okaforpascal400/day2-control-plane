variable "region" {
  description = "AWS region for the state backend. Fenced to ap-southeast-2 by the day2-terraform IAM policy (aws:RequestedRegion)."
  type        = string
  default     = "ap-southeast-2"
}

variable "bucket_name" {
  description = "Terraform state bucket. Must match the ARN the day2-terraform IAM policy is scoped to."
  type        = string
  default     = "day2-control-plane-tfstate-apse2"
}

variable "lock_table_name" {
  description = "DynamoDB lock table. Must match the ARN the day2-terraform IAM policy is scoped to."
  type        = string
  default     = "day2-control-plane-tflock"
}

variable "tags" {
  description = "Tags applied to every resource in this stack."
  type        = map(string)
  default = {
    Project   = "day2-control-plane"
    Component = "state-backend"
    ManagedBy = "terraform"
  }
}
