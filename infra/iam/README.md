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

# `ANTHROPIC_API_KEY` — the project's first stored credential

Phase 4 introduces the first and, so far, only **long-lived secret this
repository holds**: the Anthropic API key the triage agent uses. Every other
credential in this project is either short-lived (the cost sentinel's OIDC
token, the workflow's per-run `GITHUB_TOKEN`) or lives only on Pascal's machine
(`day2-terraform`). This one is stored, so it gets written down.

| | |
|---|---|
| Name | `ANTHROPIC_API_KEY` |
| Kind | GitHub Actions **repository secret** |
| Used by | `.github/workflows/triage-agent.yml` → `agents/core/day2_agents/claude.py` |
| Reaches | `api.anthropic.com` only |
| Set by | Pascal, manually — see the command below |

## Setting it

Run this yourself; nothing in this repo can create it (agents propose, humans
approve — CLAUDE.md rule 3):

```
gh secret set ANTHROPIC_API_KEY --repo okaforpascal400/day2-control-plane
```

The command prompts for the value on stdin rather than taking it as an
argument, so the key never lands in shell history or a process listing. Verify
with `gh secret list` — which shows the name and update time, never the value.

## Why this one cannot use OIDC

The cost sentinel authenticates to AWS with a short-lived OIDC token and holds
no stored credential at all ([ADR 0002](../../docs/adr/0002-cost-sentinel-auth.md)).
That is the right pattern and it is used wherever it is available. It is not
available here.

OIDC works because AWS IAM can be configured to *trust GitHub as an identity
provider*: the workflow presents a signed token asserting "I am
`repo:okaforpascal400/day2-control-plane:ref:refs/heads/main`", and AWS
exchanges it for temporary credentials against a role whose trust policy pins
that exact subject. It requires the receiving service to implement OIDC
federation and to let the customer define the trust relationship.

The Anthropic API authenticates with an API key. There is no federation
endpoint to exchange a GitHub token against, and therefore no way to express
"only this repository, only this branch" on the provider's side. A stored
secret is the only available mechanism, so the controls have to sit around it
rather than inside it.

## Blast radius if it leaks

**What an attacker gets.** The ability to spend money against the Anthropic
account the key belongs to, up to that account's rate and spend limits, and the
ability to send arbitrary prompts. That is the whole of it.

**What they do not get.** The key is not a GitHub credential and grants nothing
in this repository: no push, no PR, no read of private code, no workflow
execution. It is not an AWS credential and touches no infrastructure. It cannot
read anything — the Anthropic API is stateless here, and no conversation,
document or file is stored under this key. Nothing in the audit trail or any
artifact is reachable with it.

**Exposure surface, and why it is small.**

- The secret is injected as an environment variable for one step of one
  workflow. GitHub masks it in logs, and `claude.py` reads it from the
  environment at call time — it is never written to disk, never logged, and
  never placed in a prompt (there is a test asserting the key does not appear
  in the prompt: `agents/triage/tests/test_agent.py`).
- `triage-agent.yml` runs on `workflow_run`, which executes the **default
  branch's** copy of the workflow. A pull request — including one from a fork —
  cannot modify the workflow to exfiltrate the secret, because the version that
  runs is the one already on `main`. This is the main reason the trigger is
  `workflow_run` and not `pull_request`.
- The agent is forbidden from proposing changes under `.github/`
  (`agents/core/day2_agents/guardrails.py`), so it cannot author a PR that
  would widen its own access to the secret even if a human merged it without
  reading it.

**If it leaks:** revoke it in the Anthropic console first (revocation is
immediate and the key is single-purpose, so nothing else breaks), issue a new
one, re-run the `gh secret set` command above, and check the account's usage
for the exposure window. There is no rotation coupling — no other system holds
this key.

## Cost exposure in normal operation

One model call per triage, on a pinned model with a hard `max_tokens` ceiling
(`agents/core/day2_agents/claude.py`). Every call's real cost is computed from
its own token counts and written to the audit trail, so spend is measured
rather than estimated. Triage only fires on a CI failure, which bounds the call
rate to how often the build breaks.

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

- **The `sub` claim carries GitHub's immutable IDs.** GitHub embeds the owner's
  and repository's immutable numeric IDs in the default OIDC subject —
  `repo:<owner>@<owner-id>/<repo>@<repo-id>:ref:refs/heads/main` — so that
  deleting and recreating a repo or org under the same name yields a *different*
  subject that cannot silently inherit this trust. `day2-cost-sentinel-trust.json`
  therefore pins
  `repo:okaforpascal400@171134881/day2-control-plane@1308639798:ref:refs/heads/main`,
  not the name-only `repo:okaforpascal400/day2-control-plane:ref:refs/heads/main`;
  the legacy form is rejected with `Not authorized to perform
  sts:AssumeRoleWithWebIdentity`. The numeric IDs are public GitHub identifiers,
  not secrets — only the AWS account ID stays an `<ACCOUNT_ID>` placeholder. Read
  the current value with `gh api repos/<owner>/<repo> --jq .id` (repo) and
  `gh api users/<owner> --jq .id` (owner).
- **Trust is pinned to `refs/heads/main`.** A run from any branch — including
  the PR that introduces this workflow — cannot assume the role. That is the
  intended behaviour: the sentinel becomes live when the code reaches main. Do
  **not** loosen the `sub` claim to a wildcard such as
  `repo:okaforpascal400@171134881/*`; that would hand every branch of every repo
  in the account the same access and undo most of the reason OIDC was chosen.
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
