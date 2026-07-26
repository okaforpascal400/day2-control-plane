# ADR 0002: Cost-sentinel AWS authentication

**Status:** accepted 2026-07-24 (Pascal). Implemented as code; the IAM role
itself is a manual admin step, tracked in `infra/iam/README.md`.

**Decision as approved:** OIDC role; read-only `Describe*`/`List*` only; trust
policy pinned to `okaforpascal400/day2-control-plane`. The deploy-workflow
ingress-fixer role is explicitly **not** approved — documented as a future
option only.

## Context

The cost sentinel is a scheduled GitHub Actions workflow that reads this
account's spend and opens an issue (or fails loudly) when it drifts toward the
~$15/month ceiling in CLAUDE.md rule 1. It is the enforcement mechanism behind
the "ephemeral cloud" discipline — it catches the node left running over a
weekend, which is the single most likely way this project overspends.

To read spend it needs AWS credentials in CI. Two ways to give it them:

- **A — GitHub OIDC → IAM role.** The workflow presents a short-lived GitHub
  identity token; STS exchanges it for ~1-hour credentials via
  `AssumeRoleWithWebIdentity`. No secret is stored anywhere.
- **B — stored read-only IAM access key.** A long-lived access key pair for a
  dedicated IAM user, held in GitHub repository secrets.

Both can be scoped to identical read-only permissions, so the permission set is
*not* what distinguishes them. What differs is the credential's lifetime,
blast radius, and what happens when something goes wrong.

## Comparison

| | A — OIDC role | B — stored key |
|---|---|---|
| Credential at rest | none — nothing to steal from the repo | long-lived key in GitHub secrets, valid until manually revoked |
| Lifetime | ~1 hour, minted per run | indefinite |
| Rotation | nothing to rotate | a recurring chore that will be forgotten |
| Scoping | trust policy pins the `sub` claim to this repo, and optionally to one branch/environment | none — the key works from anywhere on earth |
| Leak response | revoke the role; no credential is outstanding | rotate the key, and assume it was used until proven otherwise |
| CloudTrail | `AssumeRoleWithWebIdentity` with a session name identifying the run | anonymous-looking calls from a static principal |
| Setup | one-time manual: OIDC provider + role + trust policy | one-time manual: create user, mint key, paste into GitHub |
| Ongoing cost | $0 | $0 |
| Failure mode when misconfigured | workflow cannot authenticate — fails closed | works, permanently, from anywhere — fails open |

Both require the same one-time manual step by the **account owner**, because
`day2-terraform` deliberately grants no IAM actions (see `infra/iam/README.md`)
— identical in shape to the spot service-linked role. Neither is Terraform-managed,
and this ADR does not propose changing that.

## Recommendation: A, OIDC

The deciding argument is the last row. A stored key that is misconfigured still
works — it just works more broadly than intended, silently, forever. An OIDC
trust policy that is misconfigured stops the workflow, which is a bug that
surfaces immediately. For a public repo whose whole thesis is governed
automation, a long-lived cloud credential sitting in a secret store is the wrong
artifact to be demonstrating.

The one real advantage of B — no OIDC provider to understand — is worth little
against a credential nobody will remember to rotate.

### What it would look like

One-time, by the account owner with admin credentials:

1. Create the IAM OIDC identity provider for
   `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. Create role `day2-cost-sentinel` with a trust policy that pins the subject
   to this repository, and to the workflow's own ref — a wildcard such as
   `repo:okaforpascal400/*` would undo most of the benefit:

   ```json
   {
     "Condition": {
       "StringEquals": {
         "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
         "token.actions.githubusercontent.com:sub": "repo:okaforpascal400/day2-control-plane:ref:refs/heads/main"
       }
     }
   }
   ```

3. Attach a read-only policy — `infra/iam/day2-cost-sentinel-policy.json`.

### Data source: resource inventory, not Cost Explorer

Settled alongside the auth decision. The approved scope is **describe/list
only**, which rules out `ce:GetCostAndUsage` — and that turns out to be the
better sentinel anyway:

| Source | Permission | Cost | Lag |
|---|---|---|---|
| **`Describe*`/`List*` inventory (chosen)** | `ec2:Describe*`, `eks:ListClusters`, `rds:DescribeDBInstances` | free | none — reflects the account right now |
| Cost Explorer `GetCostAndUsage` | `ce:GetCostAndUsage` | charged per request (~$0.01 — confirm against current pricing) | ~a day |
| CloudWatch `AWS/Billing` | `cloudwatch:GetMetricStatistics` | free | ~6h, account total only, us-east-1 |

Spend data arrives too late to be useful here. A node left running after a demo
shows up in Cost Explorer the *next day*, having already burned a night's worth
of hours; `DescribeInstances` sees it immediately. So the sentinel counts
resources rather than dollars, and derives burn rate from **live**
`DescribeSpotPriceHistory` quotes rather than any hardcoded price (rule 5).

It also lets the sentinel enforce the *architectural* half of rule 1 — a NAT
gateway, an EKS cluster, or an RDS instance is flagged the moment it exists,
which no spend metric would surface until it had already cost money.

The sentinel is **read-only** — it reports, it does not stop instances. Acting
on a finding is a human decision (rule 3).

In the workflow: `permissions: id-token: write` plus `contents: read`, and
`aws-actions/configure-aws-credentials` pinned by commit SHA like every other
action in this repo.

## Related, not decided here

The tag-triggered deploy workflow (`.github/workflows/deploy.yml`) has an
unresolved network problem with the same root: its `deploy` job cannot reach the
node, because the security group admits only the operator's /32. One fix is to
let the workflow authorize its own runner IP just-in-time and revoke it
afterward — which needs AWS credentials in CI, i.e. this same decision.

**Not approved, not implemented — recorded as a future option only.** If it is
ever built it must be a **second, separate role** gated on
`repo:...:environment:cloud`, not extra permissions bolted onto the sentinel
role: `ec2:AuthorizeSecurityGroupIngress` is a write action against the very
control that keeps the node private, and it has no business sharing a principal
with a spend reader.

Until then the `deploy` job stays inert, and tags are applied from the
operator's machine using the artifact the `release` job publishes.

## Consequences

- No long-lived AWS credential exists for this project outside the operator's
  own `~/.aws` profile.
- CI's AWS access is legible from the trust policy alone — one file answers
  "what can GitHub do in this account."
- Costs one manual setup step, and a dependency on understanding OIDC that a
  stored key would not have required.
