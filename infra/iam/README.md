# IAM — `day2-terraform` permissions

`day2-terraform-policy.json` is the **least-privilege** policy the `day2-terraform`
IAM user needs to run everything in Phase 2, and nothing more. It is committed as
documentation; it is **attached manually in the AWS console** by the account owner
(agents propose, humans approve — CLAUDE.md rule 3). Terraform never manages this
policy or any IAM principal.

## Scope, by statement

| Sid | Grants | Scoping |
|-----|--------|---------|
| `ProviderIdentity` | `sts:GetCallerIdentity` | `*` (global, read-only; the AWS provider calls it on every init) |
| `TfStateBucketManage` | create/configure/delete the state bucket | one bucket ARN: `day2-control-plane-tfstate-apse2` |
| `TfStateObjects` | read/write/delete state objects | objects under that one bucket |
| `TfLockTable` | create/delete the lock table + lock item CRUD | one table ARN: `day2-control-plane-tflock` |
| `Ec2VpcProvisioningRegionFenced` | VPC, subnet, IGW, route table, SG, EIP, key pair, spot EC2, EBS | `*`, **fenced to `ap-southeast-2`** via `aws:RequestedRegion` |

## Why EC2 uses `Resource: "*"`

EC2/VPC networking **create** actions (`CreateVpc`, `RunInstances`, `AllocateAddress`,
…) and every `Describe*` action do **not** support resource-level ARNs — the resource
does not exist yet at create time, and describe calls are list operations. AWS's own
guidance is to constrain these with **condition keys**. This policy fences the entire
EC2 surface to the single region `ap-southeast-2`, and enumerates only the ~60 specific
actions this stack performs — there is no `ec2:*` wildcard. S3 and DynamoDB, which do
support ARN scoping, are pinned to their exact resource names.

## Deliberately NOT included

- **No IAM actions.** The Phase 2 EC2 instance has no instance profile, so Terraform
  needs no `iam:PassRole`/`CreateRole`. The cost-sentinel's OIDC role (if chosen) is a
  separate one-time manual setup, not part of this user's grant.
- **No KMS.** The state bucket uses SSE-S3 (AES256), not a KMS CMK.
- **No `ec2:*`, no `s3:*`, no `dynamodb:*` wildcards.**
- **No NAT Gateway / managed-DB / EKS actions** (CLAUDE.md rule 1).

## Before attaching

Replace `<ACCOUNT_ID>` in the `TfLockTable` resource ARN with the target AWS account ID.
