# aws — VPC + spot k3s node

The Phase 2 cloud stack: a minimal public-subnet VPC and a single **spot** EC2
node running a pinned **k3s**. This is the cloud target for the same Helm chart
that runs on Kind locally. State lives in the S3 backend from
`infra/state-backend`.

## What it creates

| Resource | Notes |
|----------|-------|
| VPC `10.0.0.0/16` | DNS support + hostnames on |
| Internet gateway + public subnet `10.0.1.0/24` | `map_public_ip_on_launch` — **NO NAT, NO private subnet** (rule 1) |
| Route table | default route `0.0.0.0/0` → IGW |
| Security group | ingress 22 / 6443 / 80 / 443 from `allowed_cidr` only; egress all |
| Key pair | your public key; private half never leaves your machine |
| Spot EC2 (`t3.small`, x86_64) | Ubuntu 22.04, gp3 30 GiB encrypted, IMDSv2, k3s via cloud-init |

**Why x86_64 and not Graviton?** The images CI publishes to GHCR are single-arch
`linux/amd64` (plain `docker build` on an `ubuntu-24.04` runner). An arm64 node
would fail every app pod with `exec format error`. Spot pricing in
ap-southeast-2 makes the choice cost-neutral — `t3.small` has been quoting at or
below `t4g.small`. Moving to Graviton means teaching CI to emit multi-arch
manifest lists (buildx + QEMU) first; the node arch, the AMI filter in
`main.tf`, and the published image arch must always change together.

No EIP (auto-assigned public IP — avoids the idle-EIP charge on spot
interruption), no instance profile (the IAM policy grants no IAM actions), no
managed DB, no NAT. Ephemeral by design.

## Prerequisites

1. `infra/state-backend` applied (this stack's backend).
2. `day2-terraform-policy.json` attached to the `day2-terraform` user, creds in
   `~/.aws` (profile `day2`) or the environment.
3. An SSH key pair and your public IP — see `example.tfvars`.
4. **The EC2-Spot service-linked role must exist in the account** (one-time).
   The first-ever spot `RunInstances` auto-creates it — but that needs
   `iam:CreateServiceLinkedRole`, which the least-privilege `day2-terraform`
   policy deliberately does NOT grant (no IAM actions — rule 4). So the account
   **owner** creates it once, with admin creds, and Terraform never touches IAM:

   ```
   aws iam create-service-linked-role --aws-service-name spot.amazonaws.com
   ```

   It is account-level and permanent — `terraform destroy` does not (and must
   not) remove it. If it already exists you'll get `InvalidInput ... has been
   taken`, which is fine.

## Apply

```
cd infra/aws
cp example.tfvars terraform.tfvars     # then edit: ssh_public_key + allowed_cidr
AWS_PROFILE=day2 terraform init
AWS_PROFILE=day2 terraform plan  -var-file=terraform.tfvars
AWS_PROFILE=day2 terraform apply -var-file=terraform.tfvars
```

Then reach the node:

```
terraform output ssh_command          # ssh ubuntu@<public-ip>
scp ubuntu@<public-ip>:.kube/config ./kubeconfig-day2
KUBECONFIG=./kubeconfig-day2 kubectl get nodes
```

k3s finishes ~30–60 s after boot; `/var/log/day2-bootstrap.done` marks completion.

## Deploy the app onto it

The Postgres credential is created against the cluster, never committed —
`values-aws.yaml` only names the Secret:

```
export KUBECONFIG=./kubeconfig-day2
kubectl create secret generic day2-postgres-auth \
  --from-literal=postgres-password="$(openssl rand -base64 24)"

helm upgrade --install day2 deploy/helm -f deploy/helm/values-aws.yaml --wait
```

The dashboard is then served by k3s's bundled Traefik on port 80 — one of the
four ports the security group opens to `allowed_cidr`:

```
curl -s "http://$(terraform output -raw public_ip)/api/health"
# then browse http://<public-ip>/
```

NodePorts (30000–32767) are deliberately **not** in the security group, which is
why the chart routes through an Ingress rather than a NodePort Service.

## Deploying from CI

`.github/workflows/deploy.yml` fires on `v*` tags. Its `release` job always
runs and needs nothing: it checks the tag against the chart's `appVersion`,
resolves the three GHCR tags to digests, renders the chart, and archives the
exact manifests. Its `deploy` job applies them, and is inert until configured:

| Setting | Kind | Purpose |
|---------|------|---------|
| `DEPLOY_HOST` | repo **variable** | node public IP; unset ⇒ `deploy` skips |
| `DEPLOY_SSH_KEY` | repo **secret** | private half of `var.ssh_public_key` |
| `cloud` | **environment** | add Pascal as required reviewer (rule 3) |

**Not yet usable from GitHub-hosted runners.** The security group admits only
`allowed_cidr`, so a runner cannot open port 22 to the node. Resolving it means
either just-in-time SG authorization from the workflow (which needs AWS
credentials in CI), a self-hosted runner, or keeping `deploy` skipped and
applying the `release` artifact by hand. That decision is open — see the
cost-sentinel auth proposal, which turns on the same question.

## Cost

Spot `t3.small` in ap-southeast-2, quoted by `describe-spot-price-history` on
2026-07-23: **$0.0099–0.0112/hr** across the three AZs — ~**$7–8/mo** if left
running 24/7. Add gp3 30 GiB at list (~$0.096/GiB-mo) ≈ **$2.9/mo**, for roughly
**$10–11/mo continuous**.

That fits the ~$15/mo ceiling only if nothing else is running, so **destroy it
when idle** — this stack is meant to be ephemeral, and the numbers above assume
a 24/7 node that should not exist. Spot prices move; re-check rather than trust
this line:

```
aws ec2 describe-spot-price-history --region ap-southeast-2 \
  --instance-types t3.small --product-descriptions Linux/UNIX --max-items 3
```

## Destroy (rule 6: zero orphans)

```
AWS_PROFILE=day2 terraform destroy -var-file=terraform.tfvars
```

Removes the instance, key pair, SG, subnet, IGW, route table, and VPC. The root
EBS volume is `delete_on_termination`. Destroy this **before** the state-backend.
