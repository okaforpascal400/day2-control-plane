# ROADMAP — day2-control-plane

> Current phase: 2 — Claude Code: only work the current phase. Update checkboxes in
> the completing PR. Pascal approves phase transitions.

## Phase 0 — Environment
- [x] WSL2 Ubuntu relocated to external SSD (`D:\wsl`)
- [x] Docker Desktop data-root on external SSD (`D:\DockerDesktopWSL\main`)
- [x] In WSL: docker CLI, kubectl, kind, helm, terraform, gh, syft, trivy
- [x] Kind cluster up, hello-world pod running
- [x] Repo scaffolded, CLAUDE.md + ROADMAP.md in place

## Phase 1 — App + Pipeline
- [x] FastAPI api (/health, /items, Postgres); worker; web dashboard
- [x] Digest-pinned multi-stage Dockerfiles
- [x] Helm chart deploys all + Postgres to Kind
- [x] CI: ruff, pytest, build, SBOM, trivy, semgrep, GHCR; green on main
- Known debt: [#3](https://github.com/okaforpascal400/day2-control-plane/issues/3)
  api replicas race on `create_schema` (found during the Phase 2 cloud deploy)

## Phase 2 — Cloud + IaC  <-- CURRENT
- [x] State backend (S3+DynamoDB); VPC public subnet NO NAT; EC2 spot + k3s
- [x] Same chart on k3s via `values-aws.yaml` — GHCR digests, dashboard on Traefik :80
- [x] Tag-triggered deploy — `release` job complete; `deploy` job inert pending
      the runner-vs-security-group decision (see `infra/aws/README.md`)
- [x] Cost-sentinel workflow — code-complete and verified against a live account;
      goes live when the OIDC role is created (`infra/iam/README.md`)
- [x] Verified: `terraform destroy` leaves zero orphans

## Phase 3 — Observability
- [ ] kube-prometheus-stack + Loki; 2-3 dashboards; alert rules; load generator

## Phase 4 — Agent Core + Triage Agent (FLAGSHIP)
- [ ] agents/core: API client, audit logger, permission scopes, PR helper
- [ ] Triage Agent: pipeline failure -> diagnosis -> fix PR
- [ ] 3-4 seeded failure scenarios; audit log on every action

## Phase 5 — CVE Response + Upgrade Agents
- [ ] Daily SBOM re-scan; CVE agent -> patch PR + blast radius
- [ ] Renovate + Upgrade Agent PR risk annotations

## Phase 6 — MCP Server + Observability Copilot
- [ ] Read-only MCP: query_prometheus, search_logs, get_dashboard, read_runbook, git_history
- [ ] Copilot interface with per-query audit logging

## Phase 7 — DR + Audit + DAY2.md
- [ ] Postgres backups to S3 + tested restore; RUNBOOK.md; RTO/RPO
- [ ] DR Drill Agent (advisory only); quarterly audit run once
- [ ] DAY2.md fully answered with links

## Phase 8 — Polish + Content
- [ ] README diagram + GIFs; ADRs backfilled; portfolio case study; 8-piece content series

## Day 2 coverage map
| Q | Mechanism | Agent | Phase |
|---|---|---|---|
| Upgrades | Pinning + Renovate | Upgrade | 5 |
| Bus factor | Docs-as-code, ADRs | Copilot | 6 |
| DR | Terraform + S3 backups | DR Drill | 7 |
| CVE speed | Scan/SBOM in CI + daily rescan | CVE Response | 5 |
| Cadence | Quarterly audit action | Audit | 7 |
| Cost drift | Read-only OIDC cost sentinel, every 6h | — | 2 |
