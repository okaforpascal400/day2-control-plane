# CLAUDE.md — day2-control-plane

## Project
AI-powered DevOps Control Plane portfolio project. Demo microservices app + enterprise
plumbing (Terraform IaC, GitHub Actions CI/CD, observability) + governed AI agent layer.
Organizing principle: Day 2-first operations (see DAY2.md). Owner: Pascal Okafor.
Public repo — code, docs, commit hygiene all on display.

## Roles
Pascal = director (approves all merges). Claude chat = architect. Claude Code = builder:
execute the current phase in ROADMAP.md, nothing else.

## Hard constraints
1. NO EKS, NO NAT Gateway, NO managed DBs. Cost ceiling ~$15/mo. >$5/mo continuous: ask.
2. Pin everything (providers, Helm versions, images by digest). No `latest`.
3. Agents propose, humans approve: PRs/comments/issues/audit-logs only. Never push main.
4. Least-privilege: scoped IAM, minimal GitHub App scopes, read-only MCP tools.
5. Never fabricate metrics. Demo numbers come from real recorded runs.
6. Ephemeral cloud: terraform destroy must leave zero orphans.

## Workflow
Branches `phaseN/short-desc`, never commit to main. Deploy-first then commit (verify it
works before committing). Stay in current phase. One milestone per session. Conventional
commits (feat/fix/docs/infra/ci/agents). Update ROADMAP.md checkboxes in completing PR.

## Stack (do not relitigate)
Python 3.12 FastAPI + Postgres 16 + minimal web dashboard. Multi-stage digest-pinned
Dockerfiles. CI: ruff, pytest, build, syft SBOM, trivy, semgrep, GHCR. Terraform, state
in S3+DynamoDB. One Helm chart targets Kind (local) and k3s on EC2 spot (cloud).
Observability: kube-prometheus-stack + Loki, 24-48h retention. Agents: Python, shared
agents/core (Claude API client, JSON audit logger, permission scopes, PR helper).
MCP server: read-only tools only.

## Governance (all agent code)
Six pillars: least-privilege, sandboxed execution, audit trails, human-in-the-loop,
secrets via env/SSM only, output verification. Audit entry per action:
{timestamp, agent, trigger, action, target, decision_summary, approved_by}

## When unsure
Ask — especially anything costing money, touching IAM, expanding agent permissions,
or outside the current phase.
