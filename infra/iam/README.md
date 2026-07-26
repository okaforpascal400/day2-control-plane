# IAM

Two principals, both created **manually by the account owner** with admin
credentials. Terraform manages neither: `day2-terraform` grants no IAM actions
at all, so it could not create these even if asked (agents propose, humans
approve — CLAUDE.md rule 3).

| File | Principal | Used by |
|------|-----------|---------|
| `day2-terraform-policy.json` | `day2-terraform` IAM user | `terraform apply/destroy` from Pascal's machine |
| `day2-cost-sentinel-policy.json` + `day2-cost-sentinel-trust.json` | `day2-cost-sentinel` IAM **role** (OIDC) | `.github/workflows/cost-sentinel.yml` |

---

# `day2-terraform` permissions

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

---

# `day2-cost-sentinel` role (GitHub OIDC)

Decided in [ADR 0002](../../docs/adr/0002-cost-sentinel-auth.md): the sentinel
authenticates with a short-lived OIDC token, so **no long-lived AWS key exists
for this repository**. Read-only `Describe*`/`List*` only, fenced to
`ap-southeast-2`.

Run once, with **admin credentials** — not `day2-terraform`, which cannot do
IAM by design.

### 1. Create the OIDC identity provider (once per account)

Skip if `token.actions.githubusercontent.com` is already registered — check with
`aws iam list-open-id-connect-providers`.

```
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

AWS no longer verifies a thumbprint for providers whose host uses a well-known
CA, which GitHub's does, so none is supplied here. If your CLI version still
demands `--thumbprint-list`, any syntactically valid value is accepted and
ignored; prefer upgrading the CLI.

### 2. Create the role

Put the account ID into the trust policy first — it is committed with an
`<ACCOUNT_ID>` placeholder so the repo carries no account identifier:

```
account_id="$(aws sts get-caller-identity --query Account --output text)"
sed "s/<ACCOUNT_ID>/${account_id}/" infra/iam/day2-cost-sentinel-trust.json \
  >/tmp/sentinel-trust.json

aws iam create-role \
  --role-name day2-cost-sentinel \
  --description "Read-only inventory for the GitHub Actions cost sentinel" \
  --assume-role-policy-document file:///tmp/sentinel-trust.json

aws iam put-role-policy \
  --role-name day2-cost-sentinel \
  --policy-name day2-cost-sentinel-readonly \
  --policy-document file://infra/iam/day2-cost-sentinel-policy.json
```

An inline policy rather than a managed one: it is used by exactly one role, and
inlining keeps the grant from being attachable to anything else by accident.

### 3. Point the workflow at it

```
gh variable set AWS_SENTINEL_ROLE_ARN \
  --body "arn:aws:iam::${account_id}:role/day2-cost-sentinel"
```

A **variable**, not a secret — a role ARN is not a credential, and having it
visible in logs makes the workflow easier to debug. Until this is set the
sentinel job skips, so the workflow is inert rather than red.

### 4. Verify

```
gh workflow run cost-sentinel.yml
gh run watch
```

### Scope notes

- **Trust is pinned to `refs/heads/main`.** A run from any branch — including
  the PR that introduces this workflow — cannot assume the role. That is the
  intended behaviour: the sentinel becomes live when the code reaches main. Do
  **not** loosen the `sub` claim to a wildcard such as
  `repo:okaforpascal400/*`; that would hand every branch of every repo in the
  account the same access and undo most of the reason OIDC was chosen.
- **Region-fenced to `ap-southeast-2`.** `day2-terraform` cannot create
  resources anywhere else, so anything this project produces is in that region.
  A resource created manually elsewhere would be invisible to the sentinel — to
  sweep more regions, drop the `aws:RequestedRegion` condition and loop the
  script over a region list.
- **Read-only, permanently.** The sentinel opens an issue; it never stops or
  deletes anything. Acting on a finding stays a human decision (rule 3).

## Deliberately NOT created

A **second role for the deploy workflow's ingress-fixer** — one that could
authorize a GitHub runner's IP on the node security group just-in-time — is a
**documented future option only, not approved and not implemented**. See
`.github/workflows/deploy.yml` and ADR 0002. It would need
`ec2:AuthorizeSecurityGroupIngress`/`RevokeSecurityGroupIngress`, which are
write actions against the control that keeps the node private. If it is ever
built it must be its own role gated on `environment:cloud`, never extra
permissions on the read-only sentinel role.
