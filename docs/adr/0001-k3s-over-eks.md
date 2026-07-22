# ADR 0001: k3s on EC2 over EKS
**Status:** accepted
**Context:** EKS control plane costs ~$73/month before workloads. Portfolio/demo
system with a $15/month ceiling and ephemeral environments.
**Decision:** k3s on a single EC2 spot instance via Terraform.
**Consequences:** Full K8s API for demos at ~2% of the cost. No managed HA —
acceptable for ephemeral demo env, documented as right-sizing (a Day 2 discipline).
