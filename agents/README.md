# Agents

Governed AI agents for this control plane. Phase 4 ships the shared library
(`core`) and the first agent that uses it (`triage`); Phases 5-7 add the CVE
Response, Upgrade, Copilot, DR Drill and Audit agents on the same foundation.

The organising rule is CLAUDE.md rule 3 — **agents propose, humans approve**.
Everything below exists to make that true by construction rather than by
convention: not "the agent is instructed not to merge", but "there is no code
path by which it could".

```
agents/
  core/       day2_agents/  the shared library — every agent goes through it
  triage/     triage/       Phase 4: failed CI run -> diagnosis -> fix PR
  cve-response/ upgrade/ copilot/ dr-drill/ audit/    Phases 5-7
```

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
| Push to `main` | `guardrails.py` → `assert_writable_ref`, twice over: `main` is in `PROTECTED_REFS` *and* fails the `triage/*` pattern |
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

## The six governance pillars, and where each one lives in code

| Pillar | Mechanism | Read it here |
|---|---|---|
| **Least-privilege** | Each agent declares its allowed actions at startup; core refuses anything undeclared. The `Action` enum is the entire vocabulary — it contains no merge, deploy, release or delete, so those are not capabilities a config change could grant. | `core/day2_agents/scopes.py`; declared in `triage/triage/agent.py` → `SCOPES`; tested in `core/tests/test_scopes.py` |
| **Sandboxed execution** | Every external command is a fixed `argv` list run with `shell=False` — no model output is ever interpolated into a shell string. A forbidden-fragment check refuses `gh pr merge`, `--auto`, `--admin` and force-pushes before the argv reaches a subprocess. The agent holds no credential of its own: it inherits the workflow's `GITHUB_TOKEN`, so narrowing it is a three-line edit to `permissions:`. | `core/day2_agents/github.py` → `FORBIDDEN_ARGV_FRAGMENTS`, `_assert_argv_allowed`; tested in `core/tests/test_github.py` |
| **Audit trails** | One entry per externally-visible action, in the exact CLAUDE.md schema, written to *both* the workflow log (readable in the Actions UI) and a file uploaded as a 90-day artifact. Both sinks flush immediately, so a cancelled run still shows what it had already done. `approved_by` is `null` on every agent-written entry — an agent cannot approve its own work; the null is the evidence the proposal was unapproved when it was made. | `core/day2_agents/audit.py`; tested in `core/tests/test_audit.py` |
| **Human-in-the-loop** | The refusals in the table above, plus two platform backstops: GitHub does not trigger workflows for events created with `GITHUB_TOKEN`, so an agent-opened PR cannot even start CI on itself; and branch protection on `main` requires an approving review the agent is structurally unable to supply. A human re-runs CI, reviews, and merges. See [Platform-side controls](#platform-side-controls) — the settings have no diff, so they are recorded here. | `core/day2_agents/guardrails.py`; tested in `core/tests/test_guardrails.py` |
| **Secrets via env/SSM only** | `ANTHROPIC_API_KEY` is read from the environment at call time and never written to disk, never logged, and never placed in a prompt. A missing key is an error naming the variable, not a silent fallback. `GITHUB_TOKEN` is injected by Actions per-run. | `core/day2_agents/claude.py` → `_ensure_client`; blast radius and rotation in [`infra/iam/README.md`](../infra/iam/README.md) |
| **Output verification** | Three independent checks before anything is acted on: the response must parse as the exact JSON contract (`validate_diagnosis` — every field, type and enum value); the diff's paths must survive the guardrails; and `git apply --check` must prove the patch applies to the real failing tree. A confident model with a hallucinated diff is stopped by the third. | `triage/triage/agent.py` → `validate_diagnosis`; `core/day2_agents/diffs.py` → `validate_diff`; tested in `core/tests/test_diffs.py` and `triage/tests/test_agent.py` |

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

`scripts/break.sh` seeds four reproducible breaks, each failing at a *different*
gate — a triage agent that has only ever seen one kind of failure has not been
tested.

| Scenario | File it edits | Expected gate |
|---|---|---|
| `bad-dep` | `app/api/requirements-dev.txt` | `pytest (api)` / Install dependencies |
| `fail-test` | `app/api/tests/test_health.py` | `pytest (api)` / Run tests |
| `bad-env` | `deploy/helm/templates/worker.yaml` | `helm lint` / Chart env contract |
| `vuln-image` | `app/api/Dockerfile` | `image (api)` / Scan image (trivy) |

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

Two of the four seeded scenarios have been run end to end against live CI —
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

### Scenario coverage, stated exactly

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
cd agents/core   && pytest -q     # library: scopes, audit, guardrails, diffs, gh
cd agents/triage && pytest -q     # agent: evidence, verification, both outcomes
```

Both run in CI as `pytest (agents/core)` and `pytest (agents/triage)`, and
publishing images is gated on them.
