# ROADMAP — day2-control-plane

> **Phase 6 is complete. NEXT: Phase 7 — DR + Audit + DAY2.md, not started.**
> Claude Code: only work the current phase. Update checkboxes in the completing
> PR. Pascal approves phase transitions — Phase 7 does not start until he says
> so.

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

## Phase 2 — Cloud + IaC
- [x] State backend (S3+DynamoDB); VPC public subnet NO NAT; EC2 spot + k3s
- [x] Same chart on k3s via `values-aws.yaml` — GHCR digests, dashboard on Traefik :80
- [x] Tag-triggered deploy — `release` job complete; `deploy` job inert pending
      the runner-vs-security-group decision (see `infra/aws/README.md`)
- [x] Cost-sentinel workflow — code-complete and verified against a live account;
      goes live when the OIDC role is created (`infra/iam/README.md`)
- [x] Verified: `terraform destroy` leaves zero orphans

## Phase 3 — Observability  ✅ COMPLETE
- [x] kube-prometheus-stack + Loki; 2-3 dashboards; alert rules; load generator
      (`deploy/observability`, app instrumented to 0.2.0) — verified on Kind under load
- [x] Node sized for the stack: `t3.small` → `t3.medium` (measured ~2 GiB actual)
- [x] Cloud (k3s) deploy verified on a t3.medium spot node (2026-08-29) — 0.2.0
      images pulled from GHCR by digest, observability then app releases installed,
      3/3 scrape targets up, all three dashboards populated under `scripts/loadgen.py`
      load (17,946 reqs / 600s), `JobQueueStuck` fired and reached Alertmanager, and
      `terraform destroy` left zero orphans. 80 min, ~$0.03. See
      `deploy/observability/README.md`.

## Phase 4 — Agent Core + Triage Agent (FLAGSHIP)  ✅ COMPLETE
- [x] agents/core: API client, audit logger, permission scopes, PR helper
      (`agents/core/day2_agents`) — 112 tests, and the ones that matter assert
      that it *refuses*: `main` unwritable, `.github/` diffs rejected whole,
      merge/approve/auto-merge raising unconditionally
- [x] Triage Agent: pipeline failure -> diagnosis -> fix PR
      (`agents/triage`, `.github/workflows/triage-agent.yml`) — 55 tests
- [x] 3-4 seeded failure scenarios; audit log on every action
      Four scripted in `scripts/break.sh`, each failing at a different gate.
      Two run live end to end (`bad-dep`, `fail-test`); `bad-env` and
      `vuln-image` have their gates but no triage run yet — stated as such in
      `agents/README.md` rather than counted as agent coverage.
- [x] End-to-end demo run recorded with real per-triage cost
      Costs read out of the audit artifacts, not estimated: `bad-dep`
      **$0.0618** (PR #12), `fail-test` **$0.1181** (PR #15). A first `bad-dep`
      attempt paid **$0.0684** and lost the diagnosis to defect 2, so the whole
      demo cost **$0.2482** across three triage runs. `approved_by: null` on all
      29 entries. Bundles are the 90-day `triage-audit-<failed-run-id>`
      artifacts, addressed by triage run id (expire 2026-11-27). Three defects
      found by running it — two fixed in PR #13, the third (no commit-comment
      fallback when `gh pr create` fails) in PR #17. See the demo record in
      `agents/README.md`.

## Phase 5 — CVE Response + Upgrade Agents  ✅ COMPLETE
- [x] Daily SBOM re-scan; CVE agent -> patch PR + blast radius
      Live on 2026-08-30 from the `stale-base` seed ([#24](https://github.com/okaforpascal400/day2-control-plane/pull/24)): red
      `ci` ([33324677924](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324677924)) -> re-scan of that
      run's SBOMs ([33324812354](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324812354)) -> 1 CVE across
      2 packages in `web` -> `verify_fix` build + trivy gate -> branch ->
      [#25](https://github.com/okaforpascal400/day2-control-plane/pull/25), **$0.1350**, `cve-audit-33324812354`. The agent
      rediscovered `4799195`'s remediation with the naming comment stripped, and
      refused the moved-digest shortcut. Blast radius discriminated: `api` and
      `worker` re-scanned clean, only `web` was reported.
      **The `schedule:` trigger is uncommented in this PR** — 06:17 UTC daily —
      so the *daily* part goes from designed to deployed on merge. It was held
      behind a reviewed diff until the loop had run end to end, which it now has.
      #24 and #25 are closed unmerged on purpose: #24 was the seed and was
      reverted, and the correction the run actually earned (defect 4) landed as
      [#27](https://github.com/okaforpascal400/day2-control-plane/pull/27) against the agent's guardrail rather than as the
      agent's own patch.
- [x] Renovate + Upgrade Agent PR risk annotations
      Live on 2026-08-30 against [#19](https://github.com/okaforpascal400/day2-control-plane/pull/19), a real `renovate[bot]` PR
      annotated without the `simulate` bypass — medium risk, `review`,
      **$0.0731**, `upgrade-audit-19`. It reached the digest rule independently
      of the CVE agent, and was right on the facts. Two accuracy defects in the
      annotation are recorded in `agents/README.md` rather than left standing.
      The `pull_request_target` trigger **is** live and has fired unprompted
      four times — the fourth on the Phase 5 wrap PR itself
      ([33327253751](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33327253751)), skipped at the author
      gate while the diff describing it was under review. The first three:
      [33324677873](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324677873),
      [33325450487](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33325450487),
      [33326274181](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33326274181) — skipping each at the
      author gate because the PR was human-authored. What it has not yet had is
      a `renovate[bot]` PR *opened* since the workflow landed on `main` — the
      three open Renovate PRs all predate it, which is why #19 was annotated by
      `workflow_dispatch`. Deployed and exercised; awaiting its natural trigger.
- Phase 5 cost, read out of the audit artifacts and not estimated: **$0.4888**
  across four live runs — CVE $0.1350, Upgrade $0.0731, and two Triage runs at
  $0.1127 and $0.1680 that both correctly *withheld* a fix. `approved_by: null`
  on every entry. Running total across Phases 4-5: **$0.7370**.
- Four defects found by running the pipeline, the fourth in the agent's own
  proposed diff and fixed in [#27](https://github.com/okaforpascal400/day2-control-plane/pull/27) — `agents/README.md`,
  "Phase 5 defects, found by running it".
- Carried out of the phase: [#28](https://github.com/okaforpascal400/day2-control-plane/issues/28) — `CVE-2026-14456` blocks the
  `python:3.12-slim` bump in [#23](https://github.com/okaforpascal400/day2-control-plane/pull/23) for `api` and `worker`. A real
  finding on a *proposed* digest, caught by the `ci` trivy gate, not by the
  re-scan; `main`'s published images are verified unaffected. The bump is also a
  Debian 12 -> 13 jump wearing a digest bump's clothes.

## Phase 6 — MCP Server + Observability Copilot  ✅ COMPLETE
- [x] Read-only MCP: query_prometheus, search_logs, get_dashboard, read_runbook,
      git_history — plus `get_alerts`, added because "no alert fired" means
      something different when no rule covers the condition.
      `agents/copilot/mcp-server`, 84 tests. The ones that matter assert
      *refusals*: SSRF to the metadata endpoint, traversal that beats a naive
      prefix check, symlink escape, flag injection through a path, shell
      metacharacters being inert, `git show HEAD:.env` refused, and a fail-red
      guard that a handler returning a secret is cleaned at the chokepoint.
      Six new scopes, every one a read; no new write exists in the vocabulary.
- [x] Copilot interface with per-query audit logging
      `agents/copilot` — chat runtime, CLI, stdlib-only web UI (chat + evidence
      sidebar + replay tab), 72 tests. One audit entry per tool call *and* per
      model call, `approved_by: null` throughout.
- [x] **Signed answer receipts** (scope addition) — agent-agnostic schema,
      hash-chained, Ed25519. `python -m copilot.verify` runs for someone with no
      cluster, no API key and no access to the chat. Six live receipts committed
      in `agents/copilot/evidence/`, **including two that record failures**;
      the first five verify as one intact chain.
- [x] **Incident replay** (scope addition) — a window reconstructed as a cited
      timeline. Evidence gathering is scripted rather than model-driven, so
      coverage is a property of the code and cost is fixed at one model call.
      Verified against the real load window: **$0.0835**, 7 cited entries.

**Live run, one chained session:** 5 answers, `PASS (attested)`, chain of 5
intact, **$1.9123** of a $2.00 cap. Two supported with 9 and 5 citations; two
unsupported and honest about why (tool-call ceiling; budget refusal). The
latency question is the one worth reading — it **rejected the question's false
premise** with evidence, checked that a log gap was a real absence rather than
dropped ingestion, and narrowed its own conclusion after noticing a `truncated`
result.

**Measured cost, as a product fact:** a thorough investigation is **~$0.47**
(five runs behind that figure); a replay is **$0.0835**. So a $0.50 session cap
buys one deep question, not four — which is why the demo runs at $2.00.

**Phase 6 cost: $6.9822**, every figure read from an audit trail. That is well
over the $0.35-1.05 originally estimated, and the overrun has two causes worth
separating: **$3.25 went on six runs that found five defects** (three in the
spend cap itself), and **the overrun was observed at run 3 and not reported
until run 6**. The reporting failure is recorded in `agents/README.md` ahead of
the technical ones. A later running total given to the director was also short
by $0.8019 — corrected here.

Running total across Phases 4-6: **$7.7192**.

- Five defects found by running it, all in `agents/README.md`:
  the spend cap was a report rather than a control ($1.1819 against a $0.50
  cap); the token estimate was 3x low; output exceeds `max_tokens` because
  adaptive thinking bills as output; a trim marker inside a `tool_result` that
  the API rejects; and a loop exit that left `tool_use` blocks unanswered,
  breaking every *later* question in a session with the symptom nowhere near the
  cause. Each has a regression test.
- Found while building it, and fixed in this branch: promtail had been `0/1
  Running` for ~45 hours with nothing alerting — `deploy/observability` now has
  a `day2.observability` rule group, verified by evaluating the new expression
  against recorded history.
- Carried out: [#32](https://github.com/okaforpascal400/day2-control-plane/issues/32) — `JobQueueStuck` and `HighApiErrorRate`
  reference series that do not exist, so both evaluate to an empty vector and
  cannot fire. Phase 3 debt, recorded rather than fixed here.

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
