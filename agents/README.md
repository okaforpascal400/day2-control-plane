# Agents

Governed AI agents for this control plane. Phase 4 shipped the shared library
(`core`) and the first agent that uses it (`triage`); Phase 5 adds the CVE
Response and Upgrade agents on the same foundation, and Phases 6-7 add the
Copilot, DR Drill and Audit agents.

The organising rule is CLAUDE.md rule 3 — **agents propose, humans approve**.
Everything below exists to make that true by construction rather than by
convention: not "the agent is instructed not to merge", but "there is no code
path by which it could".

```
agents/
  core/          day2_agents/    the shared library — every agent goes through it
  triage/        triage/         Phase 4: failed CI run   -> diagnosis -> fix PR
  cve-response/  cve_response/   Phase 5: new CVE         -> verified fix PR / issue
  upgrade/       upgrade/        Phase 5: Renovate PR     -> risk annotation
  copilot/ dr-drill/ audit/                              Phases 6-7
```

## The three agents at a glance

Read this table as a permission budget. Every row is what `agents/core` will
let that agent do, and nothing outside it is reachable — not by prompt, not by
configuration, not by a model that decides it would be helpful.

| | Triage | CVE Response | Upgrade |
|---|---|---|---|
| Trigger | `ci` run fails | daily SBOM re-scan finds a new HIGH/CRITICAL | Renovate opens a PR |
| `call_model` | yes | yes | yes |
| `read_ci_run` | yes | — | — |
| `read_pr` | — | yes (dedupe) | yes |
| `create_branch` / `push_commit` | yes (`triage/*`) | yes (`agent/*`) | **no** |
| `open_pr` | yes | yes | **no** |
| `open_issue` | — | yes | **no** |
| `comment_on_run` | yes | — | — |
| `comment_on_pr` | — | — | yes |
| Merge / approve | **refused in the library, for all three** | | |
| Writes code? | yes, as a proposal | yes, as a proposal | **never** |

The Upgrade agent's column is the interesting one: it holds three scopes and
none of them can change the repository. "It never pushes code" is therefore not
a rule it follows — it is a capability it does not have, and
`upgrade/tests/test_agent.py` asserts exactly that by calling every write on a
helper carrying its real scope set and requiring each to raise.

---

## The Triage Agent

`.github/workflows/triage-agent.yml` fires on `ci.yml` completing with
`failure`, on any branch.

```
ci.yml fails
   |
   v
workflow_run trigger  ── checks out the FAILING commit (not the default branch)
   |
   v
read the run  ─────────  jobs + per-step conclusions -> which step failed
   |
   v
gather evidence ───────  bounded log window around ##[error]
   |                     the failing commit's own diff
   |                     full contents of the files those two name
   v
ask Claude  ───────────  structured JSON: root cause, confidence, diff,
   |                     what it did NOT change, how to verify
   v
verify  ───────────────  parse the diff -> refuse forbidden paths ->
   |                     `git apply --check` against the failing tree
   |
   +--- verified & confident ---> triage/<run-id>-<slug> -> commit -> push -> PR
   |
   +--- low confidence, or ------> diagnosis comment on the failing commit,
        the patch does not apply    nothing pushed
   |
   v
comment on the failing commit linking the outcome; upload the audit trail
```

Both endings are first-class. **A precise diagnosis with no patch is a success.**
A confident-sounding wrong patch costs a reviewer more than silence, so anything
short of a verified diff degrades to a comment. `low` confidence never produces
a branch (`agent.py` → `FIX_CONFIDENCE`).

There is a third state, and it is a failure rather than an ending: the diff is
verified and pushed, and then `gh pr create` fails. The agent cannot open the PR
and will not retry it, so it does the two things that keep the work reviewable —
comments the diagnosis *and the branch name and the exact command to open the PR
by hand* onto the failing commit, and exits non-zero so the run goes red — and
leaves the rest to a human. Only a platform failure degrades this way; a
guardrail violation at the same point stays fatal, because that would mean the
agent tried something it must never do.

### What it will not do

| | Where that is enforced |
|---|---|
| Merge anything | `github.py` → `GitHubHelper.merge_pull_request` always raises; there is no scope that enables it and no argument that changes it |
| Push to `main` | `guardrails.py` → `assert_writable_ref`, twice over: `main` is in `PROTECTED_REFS` *and* fails the agent-namespace pattern |
| Write any branch outside `triage/*` and `agent/*` | `guardrails.py` → `AGENT_REF`. The Phase 5 widening is a whole path segment, anchored: `agents/`, `agent-x/`, `agentfoo/` and `x/agent/y` all stay refused, and `test_guardrails.py` names each one |
| Edit `.github/**` | `guardrails.py` → `FORBIDDEN_DIFF_PREFIXES`; a triage agent that can edit its own trigger or the CI gates that judge it is not reviewable |
| Edit `agents/core/**` | same list — an agent that can rewrite its own guardrails has none |
| Re-run or cancel CI | the workflow grants `actions: read`, never `write` |
| Triage its own PRs | `agent.py` skips `triage/*` branches, and the workflow's `if:` stops it before a runner is allocated |

### Cost

One model call per triage. Cost is computed from the response's own token
counts against a pinned price table (`claude.py` → `PRICE_PER_MTOK`), written
into the audit trail, and printed in the PR body. An unpriced model raises
rather than reporting `$0.00`, because a fabricated metric is worse than a
failed run (CLAUDE.md rule 5).

Measured per-triage figures are in the demo record below — not estimates.

---

## The CVE Response Agent

`.github/workflows/sbom-rescan.yml` re-scans the SBOMs of the published images
against fresh vulnerability data, daily, and hands anything new to the agent.

```
daily re-scan of the stored SBOMs (fresh CVE data)
   |
   v
new fixable HIGH/CRITICAL? ── no ──> the workflow ends. No agent, no model call, no cost.
   |
  yes
   v
group by CVE ──────────  one CVE affecting libssl3 AND libcrypto3 is ONE problem
   |
   v
dedupe ────────────────  an open PR or issue naming this CVE suppresses re-filing
   |
   v
gather evidence ───────  the Dockerfile/requirements that install each package,
   |                     in full; the layer each was found in; and what each
   |                     pinned base-image tag resolves to in the registry TODAY
   v
ask Claude ────────────  affected? blast radius? which of three remediations?
   |
   v
verify ────────────────  git apply --check -> git apply -> docker build -> trivy
   |                     the SAME commands ci.yml runs, same pinned scanner
   |
   +--- built and scans clean ---> agent/cve-<id> -> commit -> push -> PR
   |
   +--- anything less ----------> a diagnosis-only issue, nothing pushed
```

### Why a finding is always new, with no state file

`ci.yml` publishes an image only after `trivy image --severity HIGH,CRITICAL
--ignore-unfixed --exit-code 1` passes. Every published image therefore had
**zero** fixable HIGH/CRITICAL at publish. Anything the re-scan finds under the
identical filter was disclosed, or became fixable, afterwards. There is no
"previously seen" ledger to keep and none to go stale.

Cross-*day* dedupe is a different problem and is solved differently: the agent
searches the repository's own open issues and PRs for the CVE id. GitHub's
state is the ledger. A human who closes an issue by hand has made a decision,
and the agent's next run respects it rather than silently overriding it.

Titles only, deliberately. A CVE id in an issue *body* is usually a reference
("supersedes CVE-…"), and suppressing a real finding because something
mentioned it in passing is expensive in both directions — the CVE goes
unpatched and nobody is told why.

### Three endings, and two of them are issues

| Ending | When | Why it is not a failure |
|---|---|---|
| **Fix PR** | confident, the diff applied, the image built, trivy came back clean | — |
| **Diagnosis-only issue** | low confidence, no clean remediation, or the patch failed to build or failed to clear the finding | A speculative security patch spends the review attention the real fix needed, and puts a false "patched" signal on the repo |
| **Assessment issue** | the finding does not affect what we ship (e.g. the package is only in a discarded build stage) | Filing it is a *cost* decision as much as a documentation one — without it the agent re-reaches the same conclusion at full model price every morning |

### The digest rule

CLAUDE.md rule 2 pins every base image by digest, so a base-image bump means
writing a `sha256:` into a Dockerfile — and a model asked to do that will
produce a well-formed digest that does not exist. The diff applies, the branch
pushes, and the build fails on a digest nobody can trace.

So `registry.py` resolves what each pinned tag points at *now* (anonymous,
`HEAD` only, no response body ever parsed) before the model is called, and the
prompt permits a digest in a diff **only** if it appears verbatim in that
evidence. The model decides whether to bump. It never supplies the value.

### The re-scan reads the CycloneDX SBOM, not the SPDX

This looks like a preference and is not. Measured on the `stale-base` scenario,
one image scanned four ways:

| Scanned | Fixable HIGH found |
|---|---|
| `trivy image` (the CI gate's own answer) | **2** |
| syft SPDX | 0 |
| syft CycloneDX | 0 |
| trivy CycloneDX | **2** |

Alpine advisories are keyed on the *source* package (`openssl`), not the binary
package (`libssl3`). Trivy's own CycloneDX records that as an
`aquasecurity:trivy:SrcName` property; syft records it only as an `upstream=`
qualifier inside the purl, which trivy's SBOM decoder does not map back. Trivy
identified the OS correctly in every case — this was never distro detection.

A daily re-scan of the syft SBOM would have returned a confident, permanent
zero: it would not go red, it would report "all clear" every morning for ever.
That is the worst failure available to a control whose entire job is to notice
something. `ci.yml` now emits both SBOMs — SPDX as the portable document of
record, CycloneDX as the scannable one — and the re-scan reads the latter.

---

## The Upgrade Agent

`.github/workflows/upgrade-agent.yml` fires when Renovate opens a PR.

```
Renovate opens a dependency PR
   |
   v
is it a dependency bot, and not another agent's branch? ── no ──> skip, no model call
   |
  yes
   v
read the PR ───────────  the pin that moved, read out of the DIFF, not the title
   |                     the release notes, read out of Renovate's own PR body
   v
grep our usage ────────  every tracked file naming the dependency, application
   |                     code ranked above test fixtures
   v
ask Claude ────────────  risk against OUR code; what changed; what to do
   |
   v
one comment on the PR ─  risk level, upstream changes, our affected paths,
                         and one of four recommendations: merge/review/test/hold
```

**Release notes come from Renovate's PR body.** Renovate has already fetched
the upstream changelog for the version jump; giving the agent a scope to reach
arbitrary upstream repositories would be a far larger grant than the job needs.
When a body carries no notes section, the comment says so and the model is told
to lower its confidence rather than reason from what it remembers about the
package — a confident claim about a release it cannot see is the failure mode
that matters here.

**It annotates on `opened` and `reopened` only.** Renovate force-pushes a PR
when a newer version appears. Re-annotating on every push would require the
agent to read its own prior comments to avoid duplicating them — a read it has
no scope for — so rather than widen the grant for a convenience, a superseded
annotation is left standing and a human can re-run it by hand. A real
limitation, written down rather than papered over.

**`pull_request_target`, and why that is safe here.** Renovate bumps pinned
GitHub Actions, so its PRs routinely touch `.github/workflows/`. Under a
`pull_request` trigger the contents of a PR could rewrite the very workflow
that holds `ANTHROPIC_API_KEY`. `pull_request_target` runs the workflow from the
default branch instead. That trigger is dangerous in exactly one situation —
checking out the PR's head and executing it — and this workflow never does:
the checkout has no `ref:`, and the PR reaches the agent as *data* through
`gh api`, never as code that runs.

---

## The six governance pillars, and where each one lives in code

| Pillar | Mechanism | Read it here |
|---|---|---|
| **Least-privilege** | Each agent declares its allowed actions at startup; core refuses anything undeclared. The `Action` enum is the entire vocabulary — it contains no merge, deploy, release or delete, so those are not capabilities a config change could grant. Phase 5 added three members (`read_pr`, `open_issue`, `comment_on_pr`), each a read or a proposal; the absences are unchanged. | `core/day2_agents/scopes.py`; declared in each agent's `agent.py` → `SCOPES`; tested in `core/tests/test_scopes.py` and `upgrade/tests/test_agent.py` |
| **Sandboxed execution** | Every external command is a fixed `argv` list run with `shell=False` — no model output is ever interpolated into a shell string. A forbidden-fragment check refuses `gh pr merge`, `--auto`, `--admin` and force-pushes before the argv reaches a subprocess. The agent holds no credential of its own: it inherits the workflow's `GITHUB_TOKEN`, so narrowing it is a three-line edit to `permissions:`. | `core/day2_agents/github.py` → `FORBIDDEN_ARGV_FRAGMENTS`, `_assert_argv_allowed`; tested in `core/tests/test_github.py` |
| **Audit trails** | One entry per externally-visible action, in the exact CLAUDE.md schema, written to *both* the workflow log (readable in the Actions UI) and a file uploaded as a 90-day artifact. Both sinks flush immediately, so a cancelled run still shows what it had already done. `approved_by` is `null` on every agent-written entry — an agent cannot approve its own work; the null is the evidence the proposal was unapproved when it was made. | `core/day2_agents/audit.py`; tested in `core/tests/test_audit.py` |
| **Human-in-the-loop** | The refusals in the table above, plus two platform backstops: GitHub does not trigger workflows for events created with `GITHUB_TOKEN`, so an agent-opened PR cannot even start CI on itself; and branch protection on `main` requires an approving review the agent is structurally unable to supply. A human re-runs CI, reviews, and merges. See [Platform-side controls](#platform-side-controls) — the settings have no diff, so they are recorded here. | `core/day2_agents/guardrails.py`; tested in `core/tests/test_guardrails.py` |
| **Secrets via env/SSM only** | `ANTHROPIC_API_KEY` is read from the environment at call time and never written to disk, never logged, and never placed in a prompt. A missing key is an error naming the variable, not a silent fallback. `GITHUB_TOKEN` is injected by Actions per-run. | `core/day2_agents/claude.py` → `_ensure_client`; blast radius and rotation in [`infra/iam/README.md`](../infra/iam/README.md) |
| **Output verification** | Three independent checks before anything is acted on: the response must parse as the exact JSON contract (every field, type and enum value); the diff's paths must survive the guardrails; and `git apply --check` must prove the patch applies to the real tree. A confident model with a hallucinated diff is stopped by the third. The CVE agent adds a fourth and fifth — `docker build` and `trivy`, the *same* commands `ci.yml` runs — so its PRs have already passed the gate that will judge them. The Upgrade agent's only output is prose, so it has no `git apply` downstream and its field contract is correspondingly stricter. | `triage/triage/agent.py` → `validate_diagnosis`; `cve-response/cve_response/agent.py` → `validate_assessment`, `verify.py`; `upgrade/upgrade/agent.py` → `validate_annotation`; `core/day2_agents/diffs.py` → `validate_diff` |

The guardrail tests are the ones that matter. They do not assert that the agent
works — they assert that it **refuses**: that `main` is unwritable, that a diff
touching `.github/` is rejected whole, that a hallucinated patch never reaches a
branch, that merging raises no matter how it is called.

### Platform-side controls

Two controls live in GitHub's repository settings rather than in this repo. A
setting has no diff, no review and no history, so it is written down here —
an undocumented setting is indistinguishable from an accident.

**The order they were applied in is itself the control.** Branch protection went
on `main` *first*, verified over the API, and only then was the Actions flag that
lets the agent open a PR turned on. The gap between granting a permission and
establishing the control that bounds it is a real window, and on a public repo
the ordering is part of the governance story rather than an implementation
detail. That is the standing rule for this project: compensating control first,
grant second — never the reverse, and never "we'll add protection after".

| Setting | Value | Why |
|---|---|---|
| Branch protection on `main` | 1 approving review, stale reviews dismissed, conversation resolution required, the seven CI gates required (`strict`), force-pushes and deletions off, `enforce_admins: false` | The load-bearing merge control. |
| Actions → *Allow GitHub Actions to create and approve pull requests* | on (`default_workflow_permissions` stays `read`) | Without it `gh pr create` fails and the agent cannot propose anything. |

**Why branch protection carries the weight now.** That second setting is a
single flag that grants *create* **and** *approve* — GitHub does not separate
them. So enabling the only thing that lets the agent open a PR also, at the
platform level, permits the Actions identity to approve one. Branch protection
is what makes that harmless, and the asymmetry is the whole point:

* `enforce_admins: false` lets the repo owner merge his own work — with one
  maintainer and no self-approval in GitHub, `true` would mean nothing can ever
  merge, and a protection rule toggled off for each merge is worse than none.
* The `GITHUB_TOKEN` identity **is not an admin**, so it gets no bypass. For the
  agent the rule is absolute: it cannot merge, and it cannot supply the approval
  its own PR needs.

The control therefore binds exactly the party it needs to bind. The owner's
bypass is logged and visible; the agent has no path at all.

Defence in depth, because a repository setting can be changed by anyone with
admin and this project should not depend on one checkbox:

* `Action` (`core/day2_agents/scopes.py`) has no approve member, so approval is
  not a scope that exists to be granted.
* `GitHubHelper.approve_pull_request` and `enable_auto_merge` raise
  unconditionally, as `merge_pull_request` always has.
* `FORBIDDEN_ARGV_FRAGMENTS` refuses `gh pr review` alongside `gh pr merge`,
  `--auto` and `--admin`, catching a future caller that hand-builds an argv past
  the typed methods.

**Known gap, not yet verified:** whether GitHub counts an approval from the
Actions identity toward the required review. The controls above mean the agent
should never be able to attempt one, so the question is about the platform's
behaviour rather than this agent's — but it is unverified, and it is recorded
as unverified rather than assumed safe.

---

## The prompt

`triage/triage/prompts.py` is the agent's real behaviour, and it is written to
be read. Four decisions worth knowing about:

* **The output contract is a JSON object with fixed keys**, re-verified in code.
  Structured output the caller checks is a control; asking a model nicely is not.
* **The confidence levels are defined in terms of the evidence** the model
  actually received, not left as a vibe — and the definition is load-bearing,
  because `low` routes to a comment instead of a branch.
* **"What I did not change" is a required field.** It is what makes the PR
  reviewable: it forces the model to name the adjacent changes it considered
  and rejected, which is exactly what a reviewer needs in order to disagree.
* **The failing commit's own diff is supplied as evidence.** CI was green before
  it and red after, so the cause is usually in there. Without it the agent
  cannot tell a deliberate change from a mistake — a flipped assertion looks
  exactly like a specification.

The refusals are repeated in the prompt even though `core` enforces them: a
model told the rule produces a usable fix, while a model that has to be refused
produces a wasted run.

---

## Seeded failure scenarios

`scripts/break.sh` seeds five reproducible breaks. The first four each fail at
a *different* gate — a triage agent that has only ever seen one kind of failure
has not been tested. The fifth is for the CVE agent and is different in kind:
it does not invent a break, it restores a state this repository genuinely
shipped from.

| Scenario | File it edits | Expected gate |
|---|---|---|
| `bad-dep` | `app/api/requirements-dev.txt` | `pytest (api)` / Install dependencies |
| `fail-test` | `app/api/tests/test_health.py` | `pytest (api)` / Run tests |
| `bad-env` | `deploy/helm/templates/worker.yaml` | `helm lint` / Chart env contract |
| `vuln-image` | `app/api/Dockerfile` | `image (api)` / Scan image (trivy) |
| `stale-base` | `app/web/Dockerfile` | `image (web)` / Scan image (trivy), **and** a real finding for the CVE agent |

`stale-base` drops the CVE-2026-14456 bridge from the web runtime stage,
returning the image to what it shipped before commit `4799195`. It removes the
explanatory comment with it — that comment names the CVE, the package and the
fixed version, and a demo that hands the model its own answer has verified
nothing.

CI goes red at the trivy gate, which is the point: `ci.yml` uploads both SBOMs
*before* that gate, so the artifacts exist on the red run and the re-scan can
be aimed at it with `-f source_run=<red ci run id>`. This scenario has been run
live — see the [Phase 5 demo record](#phase-5-demo-record--both-agents-live).
Note that a red `ci` run also wakes `triage-agent.yml`, so seeding this costs
two model calls, not one; both agents responding to one event is the honest
behaviour and both refusals are recorded there.

```bash
git switch -c phase4/scenario-bad-dep
scripts/break.sh bad-dep
git commit -am 'test(triage): seed a bad-dep failure' && git push -u origin HEAD
gh pr create --fill   # ci.yml runs on pull_request; a branch push alone triggers nothing
# CI fails -> triage-agent.yml fires -> a PR appears
scripts/break.sh restore
```

Originals are copied to `.break-backup/` (gitignored) before anything is edited,
so `restore` works whether or not the break was committed. The script never
touches git: one that both breaks the build and pushes is a typo away from
doing it on the wrong branch.

`bad-env` needed a new gate. A misspelled `DAY2_*` env name renders perfectly
happily through `helm lint`, and pydantic-settings is configured with
`extra="ignore"`, so the worker would start on the default and the bug would
surface only as a throughput number nobody is watching — the quietest kind of
config drift. `deploy/helm/scripts/check_env_contract.py` compares the chart's
rendered env names against each service's `Settings` fields, read out of the
source with `ast` so the check needs no dependencies installed.

---

## Demo record

Two of the four triage scenarios have been run end to end against live CI —
seed → CI fails at the predicted gate → `triage-agent.yml` fires → a reviewable
PR, human-merged. Every figure below is read out of the audit artifact that run
produced. None of them is an estimate (CLAUDE.md rule 5).

### Where the evidence lives

One artifact per triage, attached to the **triage-agent run** rather than the
failed `ci` run, named `triage-audit-<failed-run-id>`, retained 90 days:

| Scenario | Failed `ci` run | Triage run — the bundle | Artifact | Outcome |
|---|---|---|---|---|
| `bad-dep` (attempt 1) | [33256422303](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33256422303) | [33256460118](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33256460118) | `triage-audit-33256422303` (1,204 B, 8 entries) | failed at `gh pr create` (defect 2) |
| `bad-dep` (attempt 2) | [33256422303](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33256422303) | [33257066844](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33257066844) | `triage-audit-33256422303` (1,307 B, 10 entries) | [#12](https://github.com/okaforpascal400/day2-control-plane/pull/12) |
| `fail-test` | [33260241253](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33260241253) | [33260274973](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33260274973) | `triage-audit-33260241253` (1,511 B, 11 entries) | [#15](https://github.com/okaforpascal400/day2-control-plane/pull/15) |

The artifact name keys on the **failed** run id, not the triage run id, so the
two `bad-dep` attempts produced two different bundles under the same name on
different runs. Address a bundle by its triage run id; the name alone is not
unique.

Two details that the table alone would let you read too generously. Attempt 2 was
a **hand re-triage** (`workflow_dispatch`) of the same failed run, once the
Actions setting that blocked attempt 1 was in place — so the automatic
`workflow_run` trigger is demonstrated by attempt 1 and by `fail-test`, not by
the run that opened #12. And of the 17 triage-agent runs on this repo, 14 were
`skipped` by the workflow's `if:` gate before a runner was allocated: green ci
runs and `triage/*` branches, stopped at zero cost. Three runs did work; those
are the three above.

```bash
gh run download 33257066844 -D ./evidence    # -> triage-audit-33256422303/triage-audit.jsonl
```

**Both artifacts expire 2026-11-27.** The trail is also written to the workflow
log, which is readable in the Actions UI for as long as the run is retained, and
the diagnosis itself survives independently as a commit comment on the failing
commit and as the PR body. The artifact is the machine-readable copy, not the
only copy — but it is the only copy with the token counts, so anything that
needs to outlive November must be downloaded before then.

### Per-triage cost — measured

One model call per triage attempt, priced from the response's own token counts:

| Triage run | Model | Input tok | Output tok | Log window | Cost |
|---|---|---|---|---|---|
| `bad-dep` attempt 1 | `claude-opus-5` | 7,433 | 1,249 | 0 ch | **$0.0684** |
| `bad-dep` attempt 2 | `claude-opus-5` | 7,433 | 985 | 0 ch | **$0.0618** |
| `fail-test` | `claude-opus-5` | 17,077 | 1,307 | 15,181 ch | **$0.1181** |
| | | | | **total spent** | **$0.2482** |

Three numbers, and they answer different questions:

* **Cost of a triage that produces a reviewable PR: $0.0618–$0.1181.**
* **Cost of getting `bad-dep` triaged: $0.1302** — attempt 1 paid full price for
  a diagnosis, then threw it away when `gh pr create` failed. Defect 2 is not
  only a reviewability gap; it is the one that wastes money.
* **Total spent across the whole Phase 4 demo: $0.2482.**

The spread between the two successful triages is not noise. `fail-test` cost
roughly twice `bad-dep` because it had a log: 15,181 characters of it in the
prompt, where both `bad-dep` attempts carried zero. So the honest reading is not
"triage costs about $0.08" — it is that a *properly evidenced* triage costs about
$0.12, and the cheap ones were cheap because they were flying blind.

### What the trails demonstrate

* `approved_by` is `null` on all 29 entries across all three runs. No agent-written
  entry claims approval, which is the point of the field.
* The same six scopes are declared at startup on every run, and nothing outside
  them appears as an action.
* On `fail-test`, `commit` records `paths: ["app/api/tests/test_health.py"]` —
  the path-scoped commit from [#13](https://github.com/okaforpascal400/day2-control-plane/pull/13)
  working. On `bad-dep`, which predates that fix, the same entry has no paths.
* On `fail-test`, a `read_job_log` entry records the primary transport failing
  and the fallback succeeding. That entry is [#13](https://github.com/okaforpascal400/day2-control-plane/pull/13)'s
  other fix earning its keep on the very next run.

### Three defects, found by running it

The `bad-dep` arc is where the agent was first pointed at a real failure, and it
produced three defects. Recording them is the point: a demo that only shows the
happy path has not tested anything.

| # | Defect | Status |
|---|---|---|
| 1 | `get_job_log` returned `""` on any failure, discarding exit code and stderr both — so the agent diagnosed from the commit diff alone (`log_window_chars: 0`) at full model cost, and *why* the log was missing was unrecoverable | Fixed, [#13](https://github.com/okaforpascal400/day2-control-plane/pull/13) |
| 2 | A failure at `gh pr create` raises out of `triage_run`, so the commit-comment fallback never runs — attempt 1 left a pushed branch, a paid-for diagnosis and no explanation anywhere a reviewer would look | Fixed, [#17](https://github.com/okaforpascal400/day2-control-plane/pull/17) |
| 3 | The agent's own audit log was committed into the proposal | Fixed, [#13](https://github.com/okaforpascal400/day2-control-plane/pull/13) |

**Defect 3 in full**, because it is the one that bears directly on whether this
agent is reviewable. `commit_all` did `git add -A`, `DAY2_AUDIT_LOG` pointed at
the workspace root, and nothing gitignored it — so [#12](https://github.com/okaforpascal400/day2-control-plane/pull/12)
carried `triage-audit.jsonl` as a second changed file alongside the one-line
dependency fix it was proposing, and every future triage PR would have carried
one too. Worse, the committed copy was *half-written*: five of the run's ten
entries, truncated at the instant of commit, so the artifact in the diff
disagreed with the artifact on the run. A reviewer who cannot tell the proposal
from the agent's exhaust cannot review the proposal, which is the entire job of
the PR. Fixed at both ends — `commit_paths` scoped to the paths `apply_diff`
actually patched, and `DAY2_AUDIT_LOG` moved to `runner.temp` so the file is
never in the tree to be staged.

**Defect 1's cause is now known**, and it was not the suspect [#13](https://github.com/okaforpascal400/day2-control-plane/pull/13)
named. That PR fixed the silence and added a second transport without claiming
to know why the first failed, guessing at the documented endpoint's 302 to a
blob store. The `fail-test` trail answers it:

```
read_job_log  primary log transport failed, 'run-view' succeeded with 72400 chars
              — api: exit 1, 0 chars, stderr='the response contains terminal escape
              sequences; pass --allow-escape-sequences to output it anyway'
```

`gh api .../jobs/{id}/logs` refuses to print CI logs at all, because they contain
ANSI colour codes. Not a redirect, not an expiry — a client-side safety check
that fails identically on every run. The fallback is therefore not a fallback in
practice; it is the transport that works. This is exactly the evidence defect 1's
fix existed to capture, captured on the first run after it shipped.

Defect 2 was left open through the wrap-up PR — a real gap, recorded rather
than quietly carried — and closed straight after in [#17](https://github.com/okaforpascal400/day2-control-plane/pull/17):
a `gh pr create` failure now degrades to a commit comment carrying the branch
name and the command to finish the job by hand, while a guardrail violation at
the same point stays fatal. The attempt-1 run above is the trail it was written
from and is left as recorded.

### Phase 5 demo record — both agents, live

Run 2026-08-30 from the `stale-base` seed on [#24](https://github.com/okaforpascal400/day2-control-plane/pull/24). Every figure
below is read out of the audit artifact that run produced; none is an estimate
(CLAUDE.md rule 5).

| Agent | Trigger | Run — the bundle | Artifact | Cost | Outcome |
|---|---|---|---|---|---|
| CVE response | re-scan of ci [33324677924](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324677924) | [33324812354](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324812354) | `cve-audit-33324812354` | **$0.1350** | [#25](https://github.com/okaforpascal400/day2-control-plane/pull/25) |
| Upgrade | `workflow_dispatch` on #19 | [33324691534](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324691534) | `upgrade-audit-19` | **$0.0731** | annotation on [#19](https://github.com/okaforpascal400/day2-control-plane/pull/19) |
| Triage | ci #23 red at `image (worker)` | [33324684932](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324684932) | `triage-audit-33324603859` | **$0.1127** | diagnosis only |
| Triage | ci #24 red at `image (web)` | [33324769639](https://github.com/okaforpascal400/day2-control-plane/actions/runs/33324769639) | `triage-audit-33324677924` | **$0.1680** | diagnosis only |

**$0.4888** for the whole exercise — four runs, one model call each,
`approved_by: null` on every entry.

#### The CVE chain, end to end

`scripts/break.sh stale-base` → `ci` red at `image (web)` / Scan image (trivy)
→ re-scan of *that red run's* SBOMs → 2 findings / 1 CVE → agent → [#25](https://github.com/okaforpascal400/day2-control-plane/pull/25).
The seed strips the explanatory comment along with the bridge, so the model was
never handed the CVE id, the package names or the fixed version.

* **It rediscovered the original remediation unaided.** The agent proposed an
  `apk` bridge to openssl 3.5.8-r0 in the runtime stage, placed after the
  `COPY`s and before `USER 101` — the same shape as commit `4799195`, which it
  could not see. Not the same text, and the difference is defect 4 below.
* **The digest rule (`dd5b0bf`) fired in a live run**, and named the digest from
  the open Renovate PR as the thing it declined to do: *"`base_image_bump` is
  not defensible here — the evidence tells me only that the 1.31-alpine tag has
  moved to `sha256:901e944d...`, not that the republished image carries openssl
  3.5.8-r0."* Defect 3 was fixed on the strength of an argument; this is the
  argument holding when it costs the agent its tidiest-looking option.
* **Blast radius was bounded by evidence, not asserted.** Only `web` was
  reported — the api and worker SBOMs re-scanned clean — so the re-scan
  discriminates rather than flagging everything.
* **One CVE, one PR.** The re-scan emitted two rows (`libcrypto3`, `libssl3`);
  `e1d7ec7`'s grouping collapsed them into a single assessment and a single PR.
* **`verify_fix` ran before the PR opened**, not after: the agent built the web
  image and put it through the same digest-pinned trivy gate `ci.yml` uses —
  *"web: builds, and the trivy gate passes clean."*

#### Both Triage runs withheld their fix

Neither Phase 5 red run produced a triage patch, and both refusals were correct.

* On **#23** — an unseeded event: merging [#22](https://github.com/okaforpascal400/day2-control-plane/pull/22) made Renovate
  rebase, and the `python:3.12-slim` bump went red at `image (worker)` on the
  *same* CVE-2026-14456. Triage diagnosed base-image lag and posted a
  diagnosis, `fix_available=False`, at medium confidence.
* On **#24** — the seeded run. Triage read the commit diff, recognised the red
  trivy gate as the *intended outcome of a scenario branch*, and withheld at
  **high** confidence: *"no fix: red trivy gate is the intended scenario
  outcome."* An agent that had tried to "fix" the seed would have been the more
  expensive failure, and it is the one worth having tested.

#### The Upgrade agent on #19, and two corrections to its annotation

[#19](https://github.com/okaforpascal400/day2-control-plane/pull/19) is authored by `renovate[bot]`, so it was annotated **without**
the `simulate` bypass — the author gate was exercised rather than stepped
around. The agent returned `medium` risk, recommendation `review`, and reached
the digest rule independently of the CVE agent: *"I cannot confirm which
packages moved or which CVEs were added or resolved from the evidence given;
that requires scanning the new digest."*

That caution was not merely principled, it was **right on the facts**: as
Phase 5 defect 3 records, digest `901e944` still ships libssl3/libcrypto3
3.5.7-r0. Upstream republished the tag without the OpenSSL fix.

Two things in that annotation are wrong, recorded here rather than quietly
left standing:

1. It calls the bridge a *"deliberate trivy-gate exception"* and a
   *"suppression"* that may now be *"stale and should be dropped"*. It is
   neither. `apk add --upgrade libssl3 libcrypto3` genuinely patches the
   packages in the shipped image; nothing is suppressed and no finding is
   waived — a `.trivyignore` entry would be a suppression, and the same
   agent's CVE counterpart explicitly refused to write one. The
   recommendation it draws (re-check whether the bridge is now a no-op) is
   right; the mechanism it names is not.
2. It cites `app/web/Dockerfile` **lines 21-23**. The block is lines 21-28 —
   six lines of comment plus `USER root` and the `RUN`. It cited the comment's
   opening and missed the code.

Neither changes the recommendation, and both are the kind of error a reviewer
reading the cited lines catches in seconds. They are recorded because an
annotation that is *nearly* right about a security control is exactly the sort
of output that gets skimmed and believed.

### Phase 5 defects, found by running it

Four. The first three were found by executing the pipeline against a genuinely
vulnerable image rather than by reading it; the fourth is in the diff the agent
actually produced on its first live run. Recorded here for the same reason the
Phase 4 defects are: a demo that only shows the happy path has not tested
anything.

| # | Defect | Status |
|---|---|---|
| 1 | The daily re-scan read the syft SPDX SBOM and returned a permanent, confident **zero** on an image with two fixable HIGH CVEs | Fixed, `e1d7ec7` |
| 2 | One CVE affecting two packages would have been assessed twice and filed as two PRs, breaking one-PR-per-CVE from the inside | Fixed, `e1d7ec7` |
| 3 | A moved base-image tag was presented to the model as though it were a fixed one | Fixed, `dd5b0bf` |
| 4 | The bridge the agent proposed pins `apk` to an exact version, so it fails the build rather than degrading to a no-op once Alpine supersedes it — contradicting the comment the agent wrote directly above it | Fixed, [#27](https://github.com/okaforpascal400/day2-control-plane/pull/27) |

**Defect 1 in full**, because it is the one worth remembering. The workflow
parsed its SBOM, ran clean and went green — while reporting nothing on an image
the CI gate itself flags twice. Measured, one image scanned four ways:

| Scanned | Fixable HIGH found |
|---|---|
| `trivy image` — the CI gate's own answer | **2** |
| syft SPDX — *what the workflow originally re-scanned* | **0** |
| syft CycloneDX | **0** |
| trivy CycloneDX | **2** |

Alpine advisories are keyed on the *source* package (`openssl`), not the binary
package (`libssl3`). Trivy's CycloneDX records that as an
`aquasecurity:trivy:SrcName` property; syft records it only as an `upstream=`
purl qualifier, which trivy's SBOM decoder does not map back. Trivy identified
the OS correctly in every case — this was never distro detection, which is
precisely why no amount of reading the workflow would have found it.

The failure mode is the dangerous one: not loud, but *silent and reassuring*.
No error, no red run, no missing artifact — just a permanent zero from the one
mechanism whose entire job is to notice something, growing more trustworthy the
longer it ran. Fixed by emitting both SBOMs from `ci.yml` and re-scanning the
CycloneDX copy; see [the section above](#the-re-scan-reads-the-cyclonedx-sbom-not-the-spdx).

**Defect 3** came from a real upstream event rather than a seeded one. Renovate
opened [#19](https://github.com/okaforpascal400/day2-control-plane/pull/19)
bumping `nginx-unprivileged:1.31-alpine` to digest `901e944` — the same digest
the agent's own resolver reports. Pulling and inspecting that image: it still
ships `libssl3`/`libcrypto3` 3.5.7-r0. Upstream republished the tag *without*
the OpenSSL fix, so the bridge in `app/web/Dockerfile` is still doing the work
its comment predicted it would stop doing. The evidence line said "this tag has
moved; this digest may be used in a diff", which invites the inference that a
newer digest is a patched one. It now says the opposite in as many words.

**Defect 4 in full**, because it is the first defect found in an agent's own
*output* rather than in the plumbing around it, and because the agent
documented it against itself. The diff it proposed reads:

```dockerfile
# ... BRIDGE, not a fix: when a rebuilt base image carries
# openssl >= 3.5.8-r0 the apk constraint is already satisfied, this RUN becomes
# a no-op, and it should be removed together with a digest bump of NGINX_IMAGE.
USER root
RUN apk add --no-cache --upgrade libssl3=3.5.8-r0 libcrypto3=3.5.8-r0
```

The comment is correct about what a bridge should do. The command does not do
it. `4799195` used the same command *unpinned*; the agent added `=3.5.8-r0` to
both packages, and an exact pin does not degrade to a no-op — it degrades to a
build failure, on exactly the event the comment is describing. Measured against
the pinned base image, exit codes are `apk`'s own:

| `RUN` form | base at 3.5.7-r0 | base already at 3.5.8-r0 | base moved past 3.5.8-r0 |
|---|---|---|---|
| `--upgrade libssl3 libcrypto3` — `4799195` | upgrades to 3.5.8-r0 | `exit 0`, no-op | `exit 0`, no-op |
| `--upgrade libssl3=3.5.8-r0 libcrypto3=3.5.8-r0` — the agent | upgrades to 3.5.8-r0 | `exit 0`, no-op | **`exit 12`, build fails** |

The last column is measured by pinning to an already-superseded version
(`=3.5.6-r0`), which puts `apk` in the identical position it will be in once
3.5.8-r0 is superseded: `ERROR: unable to select packages: breaks:
world[libssl3=3.5.6-r0]`. Alpine's index carries the current version of a
package, not a back-catalogue, so this is the *expected* path rather than an
edge case — and the failure lands on whoever next builds `web`, long after
everyone has stopped thinking about this CVE.

Two things about it are worth more than the fix itself.

**The agent knew.** Its own confidence note, in the PR body, reads: *"pinning
apk to an exact version means the RUN will fail if alpine later supersedes
3.5.8-r0 before this bridge is removed."* It identified the defect, wrote it
down where a reviewer would see it, and shipped the pin anyway. The
human-in-the-loop pillar is doing real work here: the disclosure was good
enough that the review caught it, which is the arrangement working as designed
rather than an argument for trusting the output more.

**`verify_fix` structurally could not catch it.** The agent built the image and
ran the real trivy gate, and the gate passed — because the pin is correct
*today*. This is a failure dated to a future package release, and no
build-and-scan of the present tree can see it. Output verification (pillar 6)
bounds the class of defect that reaches a reviewer; it does not empty it. Every
verified-clean claim in a CVE PR should still be read as "true at build time",
which is what it is.

Fixed at both ends in [#27](https://github.com/okaforpascal400/day2-control-plane/pull/27), because the prompt already asked for
the unpinned form and the model produced the pinned one anyway — so a
prompt-only fix would be repeating the step that just failed. The bridge
paragraph now says the fixed version belongs in the *comment* and never in the
command, and `reject_pinned_apk_bridge()` runs beside `validate_diff` so a
pinned bridge takes the route every other bad patch takes: no branch, no push,
an honest issue. The same shape as `main` being unwritable twice over — the
prompt is the instruction, the gate is the control, and they are allowed to
disagree.

The correction to the proposal itself is `4d06897` on `agent/cve-2026-14456`,
kept as a separate commit beneath the agent's own so `git log` distinguishes
the two. Neither branch merged: [#25](https://github.com/okaforpascal400/day2-control-plane/pull/25) targets the seed branch, and
`main` already carries the correct unpinned bridge from `4799195`, so there was
never anything there for `main` to gain.

### Phase 5 coverage, stated exactly

**Both agents have now had a live run**, recorded above, with a measured cost
each. What that does and does not cover:

* **CVE Response — the full path, live.** Seed → red `ci` → re-scan of that
  run's SBOMs → finding → model call → verified diff → branch → PR. One CVE,
  one PR, and one defect in the output (defect 4 above).
* **Upgrade — live, against a real Renovate PR.** [#19](https://github.com/okaforpascal400/day2-control-plane/pull/19) is
  genuinely authored by `renovate[bot]`, so the `simulate` bypass was not used
  and the author gate was exercised rather than stepped around.
* **Not covered: the daily schedule.** The `schedule:` trigger in
  `sbom-rescan.yml` is still commented out — going live is deliberately a
  reviewed one-line diff rather than a setting with no history. Every run so
  far has been `workflow_dispatch`, so "found within a day" is a property of
  the design, not yet of the deployment.
* **Not covered: the diagnosis-only issue ending.** The CVE agent declares
  `open_issue` and holds the scope, but this finding reached `fix_pr`. Of its
  three endings, one has been taken live.
* **Not covered: `pin_bump` and `base_image_bump`.** The one live finding was
  an OS package in a base image, and the agent reasoned its way to
  `apk_upgrade_bridge`. The other two remediation shapes remain test-covered
  only.
* **Nothing is merged.** [#25](https://github.com/okaforpascal400/day2-control-plane/pull/25) is open pending the defect-4
  correction, and CI has not run on `agent/cve-2026-14456`: GitHub does not
  trigger workflows for events created with `GITHUB_TOKEN`, which is the only
  credential the agent holds. The green run has to be started by a human, and
  that is a property of the design rather than an oversight.

### Phase 4 scenario coverage, stated exactly

`bad-dep` and `fail-test` have each had a full live run. `bad-env` and
`vuln-image` are scripted in `scripts/break.sh` and each has its CI gate in
place (`check_env_contract.py` was written for `bad-env`), but neither has been
seeded against live CI, so neither has a triage run or an audit artifact. They
are coverage of the *gates*, not yet of the agent.


---

## Running the agent by hand

```bash
gh workflow run triage-agent.yml -f run_id=<failed ci run id>
```

Locally, against a real failed run (reads and one model call; it will open a
real PR if it is confident, so use a scratch branch):

```bash
export ANTHROPIC_API_KEY=... GH_TOKEN=$(gh auth token)
export GITHUB_REPOSITORY=okaforpascal400/day2-control-plane TRIAGE_RUN_ID=<id>
PYTHONPATH=agents/core:agents/triage python -m triage.agent
```

## Tests

```bash
cd agents/core         && pytest -q   # library: scopes, audit, guardrails, diffs, gh
cd agents/triage       && pytest -q   # evidence, verification, both outcomes
cd agents/cve-response && pytest -q   # grouping, dedupe, the three endings
cd agents/upgrade      && pytest -q   # the skips, the contract, the refusals
```

All four run in CI as `pytest (agents/<package>)`, and publishing images is
gated on them.

The CVE suite installs an autouse fixture that replaces the registry resolver.
The first draft made real Docker Hub calls from the test suite — 10.8s of
network per run, and a CI job that would go red on an upstream rate limit
rather than on a defect. Nothing is mocked beyond the socket: the module's own
parsing and rendering still run.
