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
