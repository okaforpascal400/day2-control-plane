provider "aws" {
  region = var.region

  # Every resource inherits these; individual resources add their own Name.
  default_tags {
    tags = var.tags
  }
}

# Fail fast if credentials point at the wrong account (the IAM policy also
# fences the region, but this makes a mismatch obvious at plan time).
data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# Latest Canonical Ubuntu 22.04 LTS amd64 server image. amd64 to match the
# t3 default — and, more to the point, to match the images CI publishes: the
# GHCR builds are single-arch linux/amd64, so an arm64 node would fail every
# app pod with `exec format error`. DescribeImages is granted, SSM (for the
# public AMI parameter) is deliberately not — hence a direct AMI lookup, not
# ssm_parameter.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}
