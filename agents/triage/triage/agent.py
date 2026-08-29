"""The Triage Agent: a failed CI run in, a reviewable proposal out.

    failed run -> jobs + bounded log -> repo context -> Claude -> verified diff
               -> triage/<run>-<slug> branch -> PR -> comment on the failing commit

Every step above is gated by `agents/core`: the scopes on line one are the
whole of what this agent may do, and the guardrails it cannot do anything
about. What is deliberately absent is as important as what is here — the agent
never re-runs CI, never merges, never pushes to main, and never edits `.github/`.

The two outcomes are both first-class:

* **A fix PR**, when the model is confident and the diff survives verification.
* **A diagnosis comment**, when it is not, or the diff does not apply. This is
  not a failure path. Posting a wrong fix costs a reviewer more than posting no
  fix, so anything short of a verified patch degrades to a comment rather than
  guessing.

There is a third, degraded state that is *not* first-class: the fix is verified
and pushed but `gh pr create` fails. The agent cannot recover from that on its
own, so it does the two things that keep the work reviewable — comments the
diagnosis and the branch name onto the failing commit, and exits non-zero so the
run goes red — and leaves the rest to a human.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Any

from day2_agents.audit import AuditLogger
from day2_agents.claude import ClaudeClient, ModelError, parse_json_object
from day2_agents.diffs import DiffRejected, apply_diff, validate_diff
from day2_agents.github import GitHubError, GitHubHelper
from day2_agents.scopes import Action, PermissionSet
from triage import evidence, prompts

AGENT = "triage"

# Exactly what this agent declares at startup. `agents/core` enforces it, and
# every entry here is exercised by the flow below — there is no scope declared
# "just in case".
SCOPES = (
    Action.READ_CI_RUN,  # read the failed run's jobs and logs
    Action.CALL_MODEL,  # ask Claude for a diagnosis
    Action.CREATE_BRANCH,  # triage/* only, enforced in core
    Action.PUSH_COMMIT,  # the proposed fix, on that branch
    Action.OPEN_PR,  # the proposal itself
    Action.COMMENT_ON_RUN,  # link the outcome from the failing commit
)

VALID_CONFIDENCE = ("high", "medium", "low")
# "low" never yields a pushed branch — it degrades to a diagnosis comment.
FIX_CONFIDENCE = frozenset({"high", "medium"})

PR_MARKER = "Fixes proposed by triage agent — human review required"

MAX_SLUG_LEN = 40


class TriageError(RuntimeError):
    """The run could not be triaged at all."""


@dataclass(frozen=True)
class Diagnosis:
    root_cause: str
    confidence: str
    confidence_reason: str
    fix_available: bool
    summary: str
    commit_message: str
    diff: str
    files_changed: list[str]
    not_changed: list[dict[str, str]]
    verification: str

    @property
    def proposes_fix(self) -> bool:
        return (
            self.fix_available
            and self.confidence in FIX_CONFIDENCE
            and bool(self.diff.strip())
        )


def validate_diagnosis(payload: dict[str, Any]) -> Diagnosis:
    """Check the model's object before a single byte of it is acted on.

    Governance pillar 6, first half. Anything malformed raises here, which
    routes the run to the diagnosis-only path rather than acting on a
    half-understood response.
    """

    def text(key: str, required: bool = True) -> str:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise ModelError(
                f"field {key!r} must be a string, got {type(value).__name__}"
            )
        if required and not value.strip():
            raise ModelError(f"field {key!r} is required and was empty")
        return value.strip()

    confidence = text("confidence").lower()
    if confidence not in VALID_CONFIDENCE:
        raise ModelError(
            f"confidence must be one of {VALID_CONFIDENCE}, got {confidence!r}"
        )

    fix_available = payload.get("fix_available")
    if not isinstance(fix_available, bool):
        raise ModelError("field 'fix_available' must be a boolean")

    files = payload.get("files_changed") or []
    if not isinstance(files, list) or any(not isinstance(f, str) for f in files):
        raise ModelError("field 'files_changed' must be a list of strings")

    raw_not_changed = payload.get("not_changed") or []
    if not isinstance(raw_not_changed, list) or not raw_not_changed:
        raise ModelError("field 'not_changed' must be a non-empty list")
    not_changed = []
    for item in raw_not_changed:
        if not isinstance(item, dict) or not item.get("considered"):
            raise ModelError("each 'not_changed' entry needs a 'considered' key")
        not_changed.append(
            {
                "considered": str(item.get("considered", "")).strip(),
                "why": str(item.get("why", "")).strip(),
            }
        )

    diff = text("diff", required=False)
    if fix_available and not diff:
        raise ModelError("fix_available is true but 'diff' is empty")
    if fix_available and not files:
        raise ModelError("fix_available is true but 'files_changed' is empty")

    return Diagnosis(
        root_cause=text("root_cause"),
        confidence=confidence,
        confidence_reason=text("confidence_reason", required=False),
        fix_available=fix_available,
        summary=text("summary"),
        commit_message=(
            text("commit_message", required=False) or f"fix: {text('summary')}"
        ),
        diff=diff,
        files_changed=files,
        not_changed=not_changed,
        verification=text("verification", required=False),
    )


def slugify(text: str, max_len: int = MAX_SLUG_LEN) -> str:
    """A branch-safe slug. Must satisfy core's `triage/*` pattern."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return slug or "diagnosis"


# ------------------------------------------------------------------- rendering


def _not_changed_block(diagnosis: Diagnosis) -> str:
    return "\n".join(
        f"- **{item['considered']}** — {item['why']}" for item in diagnosis.not_changed
    )


def build_pr_body(
    diagnosis: Diagnosis,
    run_url: str,
    audit_url: str,
    cost_usd: float,
    scopes: PermissionSet,
    head_ref: str = "",
) -> str:
    return f"""\
## Diagnosis

{diagnosis.root_cause}

**Confidence: {diagnosis.confidence}** — {diagnosis.confidence_reason}

## What this changes

{chr(10).join(f"- `{path}`" for path in diagnosis.files_changed)}

## What it deliberately does not change

{_not_changed_block(diagnosis)}

## How to verify

{diagnosis.verification or "Re-run the failed workflow on this branch."}

---

### Provenance

| | |
|---|---|
| Failed run | {run_url} |
| Audit trail | {audit_url} |
| Model cost | ${cost_usd:.4f} |
| Agent scopes | `{"`, `".join(scopes.as_list())}` |

The diff was verified with `git apply --check` against the failing commit before
this branch was created. That is the only check it has passed.

CI has **not** run on this branch and will not start on its own: GitHub does not
trigger workflows for events created with `GITHUB_TOKEN`, which is the only
credential this agent holds. That is the intended shape — a proposal cannot
enrol itself into the pipeline. To see it go green, re-run CI on this branch
(`gh workflow run ci.yml --ref {head_ref}`) or push any commit to it.

**{PR_MARKER}**
"""


def build_stranded_block(
    ref: str, base: str, title: str, failure: str, run_url: str, sha: str
) -> str:
    """The headline for a verified fix that is pushed but has no PR.

    Everything the reviewer needs to finish the job by hand: what failed, where
    the commit is, and the one command that turns it into a PR. The agent
    cannot open the PR and will not retry it; a human can, in one line.

    The command is deliberately non-interactive — `gh pr create` without
    `--body` opens an editor prompt, which is not something you can paste out of
    a comment. The body it supplies points back here rather than repeating the
    diagnosis, so there is one copy of it and no chance of the two disagreeing.
    """
    body = (
        f"Proposed by the triage agent for {run_url}. "
        f"Diagnosis in the commit comment on {sha[:12]}.\n\n**{PR_MARKER}**"
    )
    return (
        "**A verified fix is pushed, but the pull request could not be "
        f"opened.** The patch below applies to the failing tree and is "
        f"committed on `{ref}`, but `gh pr create` failed:\n\n"
        f"> {failure}\n\n"
        "Nothing is lost — the diagnosis is below and the commit is on that "
        "branch. To turn it into a reviewable PR:\n\n"
        "```bash\n"
        f"gh pr create --head {ref} --base {base} \\\n"
        f"  --title {shlex.quote(title)} \\\n"
        f"  --body {shlex.quote(body)}\n"
        "```\n\n"
        f"If the fix is not wanted, delete the branch: `git push origin --delete {ref}`. "
        "The agent has no scope to delete it."
    )


def build_comment_body(
    diagnosis: Diagnosis,
    run_url: str,
    audit_url: str,
    cost_usd: float,
    pr_url: str | None = None,
    rejection: str | None = None,
    stranded: str | None = None,
) -> str:
    if pr_url:
        headline = f"Proposed a fix in {pr_url} — **{PR_MARKER}**"
    elif stranded:
        headline = stranded
    elif rejection:
        headline = (
            "**Diagnosis only — no fix proposed.** A patch was drafted but did "
            f"not survive verification, so it was discarded rather than pushed:\n\n"
            f"> {rejection}"
        )
    else:
        headline = (
            f"**Diagnosis only — no fix proposed** (confidence: "
            f"{diagnosis.confidence}). A speculative patch is worse than none, "
            "so this is left to a human."
        )

    return f"""\
### Triage agent

{headline}

**Root cause.** {diagnosis.root_cause}

**Confidence: {diagnosis.confidence}** — {diagnosis.confidence_reason}

**Considered and not changed:**

{_not_changed_block(diagnosis)}

**Suggested verification.** {diagnosis.verification or "n/a"}

<sub>Failed run: {run_url} · Audit trail: {audit_url} · Model cost: \
${cost_usd:.4f}</sub>
"""


# ------------------------------------------------------------------- the agent


def triage_run(
    gh: GitHubHelper,
    claude: ClaudeClient,
    audit: AuditLogger,
    scopes: PermissionSet,
    run_id: str,
    repo: str,
    repo_root: str,
    audit_url: str,
) -> int:
    run = gh.get_run(run_id)
    sha = run.get("head_sha", "")
    branch = run.get("head_branch", "") or "main"
    run_url = run.get("html_url", "")

    # A triage PR that fails CI must not be triaged: that is a loop, and each
    # turn of it costs money. The branch prefix is the cheapest reliable guard.
    if branch.startswith("triage/"):
        audit.record(
            action="skip",
            target=run_url or f"{repo}/actions/runs/{run_id}",
            decision_summary=(
                f"declined to triage {branch!r}: agents do not triage agents"
            ),
        )
        print(f"Skipping {branch}: triage does not triage itself.")
        return 0

    jobs = gh.get_run_jobs(run_id)
    job = evidence.failing_job(jobs)
    if job is None:
        audit.record(
            action="skip",
            target=run_url,
            decision_summary="no job in the run concluded 'failure'; nothing to diagnose",
        )
        print("No failing job found.")
        return 0

    step = evidence.failing_step(job) or {}
    job_name = job.get("name", "?")
    step_name = step.get("name", "?")
    print(f"Failing job: {job_name} / step: {step_name}")

    window = evidence.extract_failure_window(gh.get_job_log(job.get("id", "")))
    commit_diff = evidence.head_commit_diff(sha, repo_root)
    tracked = evidence.tracked_files(repo_root)
    # The commit diff names files the log never mentions — a chart typo, say,
    # surfaces in a contract check that prints no path at all.
    candidates = evidence.candidate_paths(window + "\n" + commit_diff, tracked)
    file_context, included = evidence.build_file_context(repo_root, candidates)

    audit.record(
        action="read_ci_run",
        target=run_url,
        decision_summary=(
            f"collected evidence: job {job_name!r}, step {step_name!r}, "
            f"{len(window)} chars of log, {len(commit_diff)} chars of commit "
            f"diff, {len(included)} file(s) of context"
        ),
        metadata={
            "job": job_name,
            "step": step_name,
            "log_window_chars": len(window),
            "commit_diff_chars": len(commit_diff),
            "context_files": included,
        },
    )

    user_prompt = prompts.build_user_prompt(
        repo=repo,
        workflow=run.get("name", "ci"),
        run_id=str(run_id),
        run_number=str(run.get("run_number", "?")),
        branch=branch,
        sha=sha,
        run_url=run_url,
        job_name=job_name,
        step_name=step_name,
        inventory=evidence.build_inventory(tracked),
        file_context=file_context,
        included_paths=included,
        candidate_paths=candidates,
        log_window=window,
        commit_diff=commit_diff,
    )

    call = claude.complete(
        system=prompts.SYSTEM,
        user=user_prompt,
        target=run_url or f"run/{run_id}",
        decision_summary=f"diagnosing {job_name} / {step_name}",
    )
    diagnosis = validate_diagnosis(parse_json_object(call.text))

    audit.record(
        action="diagnose",
        target=run_url,
        decision_summary=f"{diagnosis.confidence} confidence: {diagnosis.summary}",
        metadata={
            "confidence": diagnosis.confidence,
            "fix_available": diagnosis.fix_available,
            "files_changed": diagnosis.files_changed,
        },
    )

    rejection: str | None = None
    if diagnosis.proposes_fix:
        try:
            validate_diff(diagnosis.diff, repo_root=repo_root)
        except DiffRejected as exc:
            # The model was confident and the patch still did not hold up. This
            # is the check earning its keep; degrade to a comment.
            rejection = str(exc)
            audit.record(
                action="reject_diff",
                target=run_url,
                decision_summary=f"discarded the proposed patch: {rejection}",
            )
    else:
        audit.record(
            action="withhold_fix",
            target=run_url,
            decision_summary=(
                f"confidence {diagnosis.confidence!r}, fix_available="
                f"{diagnosis.fix_available}: posting a diagnosis instead of a patch"
            ),
        )

    pr_url: str | None = None
    stranded: str | None = None
    if diagnosis.proposes_fix and rejection is None:
        ref = f"triage/{run_id}-{slugify(diagnosis.summary)}"
        title = f"[triage] {diagnosis.summary}"
        gh.create_branch(ref, sha)
        # The paths git actually patched — not `diagnosis.files_changed`, which
        # is the model's claim about them — are what the commit is scoped to.
        changed = apply_diff(diagnosis.diff, repo_root=repo_root)
        gh.commit_paths(ref, diagnosis.commit_message, changed)
        gh.push(ref)
        try:
            pr_url = gh.open_pull_request(
                head=ref,
                base=branch,
                title=title,
                body=build_pr_body(
                    diagnosis, run_url, audit_url, claude.total_cost_usd, scopes, ref
                ),
            )
            print(f"Opened {pr_url}")
        # Only `GitHubError` — the platform said no. A `GuardrailViolation`,
        # `PermissionDenied` or `GitHubRefused` here would mean the agent tried
        # something it must not, and that is a bug to be made loud, not an
        # outcome to degrade gracefully into a comment. Those still propagate.
        except GitHubError as exc:
            # The commit is already pushed. Without this, the diagnosis dies
            # with the exception and the branch is stranded with nothing
            # anywhere explaining it — which is exactly what happened on the
            # agent's first live run.
            stranded = build_stranded_block(ref, branch, title, str(exc), run_url, sha)
            audit.record(
                action="open_pr_failed",
                target=f"{repo}:{ref}",
                decision_summary=(
                    f"`gh pr create` failed: {exc}. The verified fix is pushed "
                    f"on {ref}; falling back to a commit comment so the "
                    "diagnosis and the branch are not lost."
                ),
                metadata={"ref": ref, "base": branch, "error": str(exc)},
            )
            print(f"Could not open a PR; the fix is stranded on {ref}", file=sys.stderr)

    if sha:
        gh.comment_on_commit(
            sha,
            build_comment_body(
                diagnosis,
                run_url,
                audit_url,
                claude.total_cost_usd,
                pr_url,
                rejection,
                stranded,
            ),
        )

    if pr_url:
        outcome, summary = "fix_pr", "fix PR opened"
    elif stranded:
        outcome, summary = "branch_without_pr", "fix pushed but no PR — needs a human"
    else:
        outcome, summary = "diagnosis_only", "diagnosis only"

    audit.record(
        action="finish",
        target=pr_url or run_url,
        decision_summary=(
            f"triage complete: {summary}, "
            f"{claude.call_count} model call(s), ${claude.total_cost_usd:.4f}"
        ),
        metadata={
            "outcome": outcome,
            "pr_url": pr_url,
            "model_calls": claude.call_count,
            "total_cost_usd": round(claude.total_cost_usd, 6),
        },
    )
    _write_step_summary(
        diagnosis, pr_url, run_url, claude.total_cost_usd, rejection, stranded
    )
    # Non-zero, so the job goes red and a human looks — but by returning rather
    # than raising, because this is a handled outcome fully described by the
    # trail above, not an unhandled error.
    return 1 if stranded else 0


def _write_step_summary(
    diagnosis: Diagnosis,
    pr_url: str | None,
    run_url: str,
    cost_usd: float,
    rejection: str | None,
    stranded: str | None = None,
) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    outcome = f"Fix PR: {pr_url}" if pr_url else "Diagnosis only (no fix pushed)"
    if stranded:
        outcome = "**Fix pushed, but no PR could be opened.** See the comment on "
        outcome += "the failing commit; the branch needs a PR opened by hand."
    if rejection:
        outcome += f"\n\nPatch discarded: `{rejection}`"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            f"## Triage\n\n{outcome}\n\n"
            f"- Failed run: {run_url}\n"
            f"- Confidence: **{diagnosis.confidence}**\n"
            f"- Model cost: **${cost_usd:.4f}**\n\n"
            f"{diagnosis.root_cause}\n"
        )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("TRIAGE_RUN_ID") or (argv[0] if argv else "")
    if not repo or not run_id:
        raise TriageError("GITHUB_REPOSITORY and TRIAGE_RUN_ID must both be set")

    repo_root = os.environ.get("GITHUB_WORKSPACE", ".")
    audit_url = os.environ.get("TRIAGE_AUDIT_URL", "(artifact on this run)")

    audit = AuditLogger(agent=AGENT, trigger=f"workflow_run:ci#{run_id}")
    scopes = PermissionSet.declare(AGENT, SCOPES)
    audit.record(
        action="declare_scopes",
        target=repo,
        decision_summary=f"declared scopes: {', '.join(scopes.as_list())}",
        metadata={"scopes": scopes.as_list()},
    )

    gh = GitHubHelper(scopes, audit, repo=repo, repo_root=repo_root)
    claude = ClaudeClient(scopes, audit)

    try:
        return triage_run(gh, claude, audit, scopes, run_id, repo, repo_root, audit_url)
    # Broad on purpose: whatever went wrong, the trail must record it before
    # the run dies, and the exception is re-raised so the job still goes red.
    except Exception as exc:
        audit.record(
            action="error",
            target=f"{repo}/actions/runs/{run_id}",
            decision_summary=f"{type(exc).__name__}: {exc}",
        )
        print(json.dumps({"triage_error": str(exc)}), file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
