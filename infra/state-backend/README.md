# state-backend — Terraform remote state (S3 + DynamoDB)

Bootstraps the remote backend that **every other** Terraform stack in this repo uses:

- **S3 bucket** `day2-control-plane-tfstate-apse2` — versioned, SSE-S3 (AES256),
  all public access blocked, `BucketOwnerEnforced` ownership.
- **DynamoDB table** `day2-control-plane-tflock` — on-demand, `LockID` hash key,
  the state-lock table.

Both names are fixed: the `day2-terraform` IAM policy
(`infra/iam/day2-terraform-policy.json`) is scoped to these exact ARNs. Renaming
either resource here means editing that policy and re-attaching it. Don't.

## Why this stack uses local state

This is the chicken-and-egg stack: it *creates* the S3 bucket + lock table, so it
cannot store its own state in them. It uses **local state** (`terraform.tfstate` in
this dir), which is git-ignored. That's fine — the state is two resources and
trivially reproducible:

```
terraform import aws_s3_bucket.tfstate day2-control-plane-tfstate-apse2
terraform import aws_dynamodb_table.tflock day2-control-plane-tflock
```

Every other stack points its `backend "s3"` here (see the `backend_config` output).

## Prerequisites

1. `day2-terraform-policy.json` attached to the `day2-terraform` IAM user (manual,
   by the account owner — CLAUDE.md rule 3).
2. Credentials for that user available to the AWS provider (env or `~/.aws`).

## Apply

```
cd infra/state-backend
terraform init
terraform plan
terraform apply
terraform output backend_config   # paste into the next stack
```

## Destroy

Because this holds the state for everything else, destroy it **last**, only after
every consuming stack is destroyed:

```
terraform destroy
```

The bucket is versioned; `terraform destroy` empties and removes it (the
`day2-terraform` policy grants `s3:DeleteObject` + `s3:DeleteBucket`). Verify zero
orphans afterward (CLAUDE.md rule 6):

```
aws s3 ls | grep day2-control-plane-tfstate-apse2 || echo "bucket gone"
aws dynamodb list-tables --region ap-southeast-2 | grep day2-control-plane-tflock || echo "table gone"
```
