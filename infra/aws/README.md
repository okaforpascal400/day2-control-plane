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
| Spot EC2 (`t4g.small`, arm64) | Ubuntu 22.04, gp3 30 GiB encrypted, IMDSv2, k3s via cloud-init |

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

## Cost

Spot `t4g.small` in ap-southeast-2 ≈ **$0.006–0.008/hr** (~$5/mo if left running
24/7) + gp3 30 GiB ≈ **$2.4/mo**. Under the ~$15/mo ceiling, but **destroy it
when idle** — this stack is meant to be ephemeral.

## Destroy (rule 6: zero orphans)

```
AWS_PROFILE=day2 terraform destroy -var-file=terraform.tfvars
```

Removes the instance, key pair, SG, subnet, IGW, route table, and VPC. The root
EBS volume is `delete_on_termination`. Destroy this **before** the state-backend.
